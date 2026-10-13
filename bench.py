"""跑批执行器。

一次"跑批"= 用户勾选的若干 (站点, 模型) 组合，每个组合发一次请求，收集时延、
token、费用和 SVG。

设计上刻意做成"单个用例失败不影响整批"：跑几十个模型时总会有几个模型不存在、
key 没权限或站点抽风，这些都记成失败结果继续跑，而不是抛出去中断整批。

进度状态放在内存里由前端轮询。每次跑批的结果同时落盘到 runs/<run_id>/，
这样进程重启后历史报告还能看。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import config
import llm
import svg_sanitize

class Run:
    """一次跑批的状态容器。

    工作线程写、HTTP 轮询线程读，所有对 results/done 的改动都要拿 _lock，
    否则前端可能读到半个 result 字典。
    """

    def __init__(self, run_id: str, tasks: list[dict[str, str]], cfg: dict[str, Any]) -> None:
        """初始化跑批状态。

        @param run_id 跑批 id，同时用作 runs/ 下的目录名
        @param tasks 待跑的 (site, model) 组合列表
        @param cfg 本次使用的配置快照
        """
        self.run_id = run_id
        self.cfg = cfg
        self.total = len(tasks)
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.results: list[dict[str, Any]] = []
        # 原样留一份任务清单，首页要按 (站点, 模型) 标出"这一格正在重跑"。
        # 不从 pending 里反解：pending 存的是 "站点 / 模型" 拼接串，站点名本身允许含
        # 空格和斜杠，按分隔符拆回去会拆错。tasks 初始化后只读，不需要加锁。
        self.tasks = [dict(t) for t in tasks]
        self.pending = [f"{t['site']} / {t['model']}" for t in tasks]
        self.cancelled = False
        self._lock = threading.Lock()

    def add(self, result: dict[str, Any]) -> None:
        """记录一条完成的用例。

        @param result 单个用例结果
        """
        label = f"{result['site']} / {result['model']}"
        with self._lock:
            self.results.append(result)
            if label in self.pending:
                self.pending.remove(label)

    def finish(self) -> None:
        """标记整批结束。"""
        with self._lock:
            self.finished_at = time.time()

    def snapshot(self, *, include_svg: bool = False) -> dict[str, Any]:
        """生成给前端轮询用的状态快照。

        默认不带 SVG 源码：轮询是每秒一次的高频请求，几十张 SVG 拼进 JSON 会让
        每次响应到几 MB。前端只在渲染卡片墙时单独取一次全量。

        @param include_svg 是否带上 svg 与原始输出
        @return 可直接 json 序列化的状态字典
        """
        with self._lock:
            results = [dict(r) for r in self.results]
            pending = list(self.pending)
            finished_at = self.finished_at
        if not include_svg:
            for r in results:
                r.pop("svg", None)
                r.pop("raw", None)
        return {
            "run_id": self.run_id,
            "total": self.total,
            "done": len(results),
            "pending": pending,
            "running": finished_at is None,
            "cancelled": self.cancelled,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "results": results,
        }


# 所有跑批的内存索引。只保留当前进程跑过的批次，历史批次从 runs/ 目录读。
_RUNS: dict[str, Run] = {}
_RUNS_LOCK = threading.Lock()


def get_run(run_id: str) -> Run | None:
    """按 id 取内存中的跑批。

    @param run_id 跑批 id
    @return 对应 Run；不存在时返回 None
    """
    with _RUNS_LOCK:
        return _RUNS.get(run_id)


def start_run(cfg: dict[str, Any], tasks: list[dict[str, str]]) -> Run:
    """启动一次跑批，立刻返回，实际执行放到后台线程。

    必须立刻返回：HTTP 请求不能等一整批跑完（几十个模型 × 最长 180s），
    前端靠 /api/run/<id> 轮询进度。

    @param cfg 配置快照，含明文 key
    @param tasks 待跑的 (site, model) 组合
    @return 已登记的 Run 对象
    @raises ValueError tasks 为空
    """
    if not tasks:
        raise ValueError("没有选择任何模型")

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run = Run(run_id, tasks, cfg)
    with _RUNS_LOCK:
        _RUNS[run_id] = run

    worker = threading.Thread(target=_execute, args=(run, tasks), daemon=True)
    worker.start()
    return run


def _execute(run: Run, tasks: list[dict[str, str]]) -> None:
    """后台执行整批用例。

    并发度取配置里的 concurrency（默认 4）。不追求更高并发：中转站普遍有速率限制，
    并发太高会把正常结果压成 429，测出来的时延就没有意义了。
    结果按实际完成顺序发布，避免慢模型挡住已完成用例的进度和预览。

    @param run 跑批状态容器
    @param tasks 待跑组合
    """
    cfg = run.cfg
    sites = {s["name"]: s for s in cfg.get("sites") or []}
    concurrency = max(1, min(16, int(cfg.get("concurrency") or 4)))

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(_run_one, cfg, sites.get(t["site"]), t["site"], t["model"])
                for t in tasks
            ]
            # 按提交顺序调用 result 会在第一个慢模型上等待，隐藏其它已完成结果。
            for fut in as_completed(futures):
                # _run_one 内部已兜住所有异常，这里再包一层是防它本身出 bug
                # 把整个线程池拖崩，导致前端永远等不到结束。
                try:
                    run.add(fut.result())
                except Exception as exc:  # noqa: BLE001
                    run.add(_failure("?", "?", f"执行器内部错误：{exc}", 0.0))
    finally:
        run.finish()
        _persist(run)


def _run_one(
    cfg: dict[str, Any],
    site: dict[str, Any] | None,
    site_name: str,
    model: str,
) -> dict[str, Any]:
    """跑单个 (站点, 模型) 用例。

    这里吞掉所有异常并转成失败结果：跑批场景下"某个模型挂了"是常态，
    必须记下原因继续跑，不能让一个模型的失败中断整批。

    @param cfg 全量配置
    @param site 站点配置；可覆盖推理模式、token 上限和提示词，为 None 表示配置中已无此站点
    @param site_name 站点名，用于结果标注
    @param model 模型 id
    @return 单个用例结果字典
    """
    started = time.perf_counter()
    if not site:
        return _failure(site_name, model, "配置里找不到这个站点", 0.0)

    try:
        execution = None
        import official_channels
        channel = official_channels.adapter(site.get("base_url"))
        if channel:
            execution = channel.execution(model)
        resp = llm.stream_chat(
            site["base_url"],
            site.get("api_key", ""),
            model,
            site.get("prompt") or cfg.get("prompt") or config.DEFAULT_PROMPT,
            max_tokens=int(site.get("max_tokens") or cfg.get("max_tokens") or 8000),
            temperature=float(cfg.get("temperature") or 0.7),
            timeout=int(cfg.get("timeout_seconds") or 180),
            thinking_mode=site.get("thinking_mode", "default"),
        )
    except llm.LLMError as exc:
        return _failure(site_name, model, str(exc), time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001
        # 兜住未预期异常（比如解码问题），否则这条用例会变成"永远 pending"。
        return _failure(site_name, model, f"未预期错误：{type(exc).__name__}: {exc}", time.perf_counter() - started)

    raw = resp["text"]
    svg = svg_sanitize.extract_svg(raw)
    svg, notes = svg_sanitize.sanitize(svg)
    usage = resp["usage"]
    # 正常收完的空正文仍带有真实用量，保留这些字段才能区分额度耗尽和连接失败。
    error = resp.get("error") or ("" if svg else "响应里没有找到 <svg> 标签（模型没按格式输出）")

    return {
        "site": site_name,
        "model": model,
        "execution": execution,
        "ok": bool(svg) and not error,
        # 请求本身成功但没抽到 SVG，属于"没遵循输出格式"，和网络失败要区分开。
        "error": error,
        "ttfb": round(resp["ttfb"], 3) if resp["ttfb"] is not None else None,
        "ttft": round(resp["ttft"], 3) if resp["ttft"] is not None else None,
        "total": round(resp["total"], 3),
        "chunks": resp["chunks"],
        "reasoning_chars": resp["reasoning_chars"],
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "finish_reason": resp["finish_reason"],
        "truncated": resp["truncated"],
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "tokens_estimated": usage["estimated"],
        "output_tps": _tps(usage["completion_tokens"], resp["total"] - resp["ttft"]) if resp["ttft"] is not None else None,
        "cost": _cost(cfg, site, model, usage),
        "svg_bytes": len(svg.encode("utf-8")),
        # 动画标记必须在这里算并落进结果里：首页轮询走的是 snapshot()（不带 svg），
        # 历史批次走 _disk_cells 读的也是精简字段，两条路都拿不到源码，
        # 事后无法再判断。清洗之后再算，保证和最终渲染的那份内容一致。
        "animated": svg_sanitize.has_animation(svg),
        "sanitize_notes": notes,
        "svg": svg,
        "raw": raw,
    }


def _failure(site_name: str, model: str, reason: str, elapsed: float) -> dict[str, Any]:
    """构造失败结果，字段与成功结果保持一致，方便前端统一渲染表格。

    @param site_name 站点名
    @param model 模型 id
    @param reason 失败原因，直接展示给用户
    @param elapsed 已耗时秒数
    @return 失败结果字典
    """
    return {
        "site": site_name,
        "model": model,
        "ok": False,
        "error": reason,
        "ttfb": None,
        "ttft": None,
        "total": round(elapsed, 3) if elapsed else None,
        "chunks": 0,
        "reasoning_chars": 0,
        "reasoning_tokens": None,
        "finish_reason": "",
        "truncated": False,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "tokens_estimated": False,
        "output_tps": None,
        "cost": None,
        "svg_bytes": 0,
        "animated": False,
        "sanitize_notes": [],
        "svg": "",
        "raw": "",
    }


def _tps(completion_tokens: int | None, seconds: float) -> float | None:
    """算输出速度（token/s）。

    分母用"总耗时 - 首 token 时延"，即纯生成阶段，把排队和冷启动排掉，
    否则排队久的站点会显得模型很慢。

    @param completion_tokens 输出 token 数
    @param seconds 生成阶段秒数
    @return token/s；数据不足时返回 None
    """
    if not completion_tokens or seconds <= 0.01:
        return None
    return round(completion_tokens / seconds, 2)


def _cost(
    cfg: dict[str, Any],
    site: dict[str, Any],
    model: str,
    usage: dict[str, Any],
) -> dict[str, Any] | None:
    """按配置单价估算本次调用花费。

    单价按"每百万 token"计价，这是各家价目表的通用口径。没配单价就返回 None，
    对应用户的要求"未填则不算钱"，报告里显示成 "-" 而不是 0，避免误读成免费。

    @param cfg 全量配置
    @param site 站点配置
    @param model 模型 id
    @param usage token 用量
    @return 含 input/output/total/estimated 的费用字典；没配单价时为 None
    """
    prices = config.pricing_for(cfg, site, model)
    if not prices:
        return None
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    in_cost = prompt_tokens / 1_000_000 * float(prices.get("input", 0.0))
    out_cost = completion_tokens / 1_000_000 * float(prices.get("output", 0.0))
    return {
        "input": round(in_cost, 6),
        "output": round(out_cost, 6),
        "total": round(in_cost + out_cost, 6),
        # token 是估的，钱自然也是估的，这个标记要一路带到报告上。
        "estimated": bool(usage.get("estimated")),
    }


def _persist(run: Run) -> None:
    """把整批结果落盘。

    落盘失败不抛出：结果已经在内存里能看，落盘只是为了重启后还能翻历史，
    为它中断整批不值得。

    @param run 已结束的跑批
    """
    try:
        run_dir = os.path.join(config.RUNS_DIR, run.run_id)
        os.makedirs(run_dir, exist_ok=True)
        snapshot = run.snapshot(include_svg=True)
        # 配置快照里含明文 key，落盘前必须掩码，否则 runs/ 目录变成 key 副本。
        snapshot["config"] = config.public_config(run.cfg)
        with open(os.path.join(run_dir, "results.json"), "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"[warn] 结果落盘失败：{exc}")


def load_run_file(run_id: str) -> dict[str, Any] | None:
    """读历史跑批结果。

    run_id 由前端传入，用来拼目录名，所以必须挡住 ../ 和绝对路径，
    否则可以借这个参数读到 runs/ 以外的任意 json 文件。

    @param run_id 跑批 id
    @return 结果快照；id 非法或文件不存在时返回 None
    """
    if not run_id or not all(ch.isalnum() or ch == "-" for ch in run_id):
        return None
    path = os.path.join(config.RUNS_DIR, run_id, "results.json")
    # 二次确认最终路径落在 runs/ 内，防止字符白名单之外的意外绕过。
    if os.path.commonpath([os.path.abspath(path), os.path.abspath(config.RUNS_DIR)]) != os.path.abspath(config.RUNS_DIR):
        return None
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def list_runs() -> list[dict[str, Any]]:
    """列出磁盘上的历史跑批，新的在前。

    @return 每项含 run_id/total/done/finished_at 的摘要列表
    """
    out: list[dict[str, Any]] = []
    if not os.path.isdir(config.RUNS_DIR):
        return out
    for name in sorted(os.listdir(config.RUNS_DIR), reverse=True):
        data = load_run_file(name)
        if not data:
            continue
        out.append({
            "run_id": name,
            "total": data.get("total"),
            "done": data.get("done"),
            "finished_at": data.get("finished_at"),
        })
    return out


# ---------------------------------------------------------------------------
# 首页卡片墙
#
# 首页要的不是"某一批的结果"，而是跨所有批次、每个 (站点, 模型) 只留最新的那一条。
# 实现方式是把 run_id 全局倒序走一遍，先遇到的就是最新的，后面同 key 的直接丢弃。
#
# 之所以能靠 run_id 排序当时间序：run_id 是 "%Y%m%d-%H%M%S-<随机6位>" 生成的，
# 前缀定长且字典序与时间序一致，字符串倒序排就是从新到旧。不用 finished_at 排，
# 因为还在跑的批次根本没有 finished_at。
# ---------------------------------------------------------------------------

# 历史跑批的卡片缓存：run_id -> 该批次的精简结果列表。
#
# 必须缓存：首页是秒级轮询，而每个 runs/<id>/results.json 里塞着整批 SVG 源码，
# 跑过几十批之后，不缓存等于每次轮询都重新解析几十 MB JSON。
# 缓存不会脏：results.json 只在批次结束时写一次；而且本进程跑的批次一直留在 _RUNS 里，
# 永远走内存分支，不会命中这个缓存。只缓存解析成功的结果，避免把"暂时读不到"固化下来。
_GALLERY_CACHE: dict[str, list[dict[str, Any]]] = {}
_GALLERY_CACHE_LOCK = threading.Lock()


def _gallery_cell(result: dict[str, Any], run_id: str, finished_at: float | None) -> dict[str, Any]:
    """把一条跑批结果压成首页卡片需要的最小字段集。

    刻意不带 svg：首页一屏几十张图，轮询响应里塞源码会到几 MB，
    图走 /api/gallery/svg 单独取，靠 run_id 当版本号判断要不要重取。

    @param result 单条跑批结果
    @param run_id 该结果所属批次 id
    @param finished_at 该批次结束时间戳，未结束时为 None
    @return 首页卡片字典
    """
    return {
        "site": result.get("site") or "",
        "model": result.get("model") or "",
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "ttft": result.get("ttft"),
        "total": result.get("total"),
        "output_tps": result.get("output_tps"),
        "cost": result.get("cost"),
        "svg_bytes": result.get("svg_bytes"),
        # 旧批次的结果文件里没有这个字段，取不到就按静图算，不影响老数据能看。
        "animated": bool(result.get("animated")),
        "sanitize_notes": result.get("sanitize_notes") or [],
        # run_id 同时当图片版本号用：前端发现某格的 run_id 变了才重新拉图。
        "run_id": run_id,
        "finished_at": finished_at,
        "running": False,
    }


def _disk_cells(run_id: str) -> list[dict[str, Any]]:
    """读一个历史批次的卡片列表，带缓存。

    @param run_id 跑批 id
    @return 精简后的卡片列表；批次不存在或文件损坏时为空列表
    """
    with _GALLERY_CACHE_LOCK:
        hit = _GALLERY_CACHE.get(run_id)
    if hit is not None:
        return hit
    data = load_run_file(run_id)
    if not data:
        # 不缓存失败结果：runs/ 下可能混着 .DS_Store 之类的杂项，
        # load_run_file 对它们是纯字符串判断，重试代价可以忽略。
        return []
    cells = [
        _gallery_cell(r, run_id, data.get("finished_at"))
        for r in data.get("results") or []
    ]
    with _GALLERY_CACHE_LOCK:
        _GALLERY_CACHE[run_id] = cells
    return cells


def _all_run_ids() -> list[str]:
    """汇总内存和磁盘上的全部批次 id，新的在前。

    内存和磁盘会有重叠（本进程跑完的批次两边都有），这里先并成集合去重，
    取数据时再优先用内存那份——正在跑的批次只有内存里是全的。

    @return 倒序排列的 run_id 列表
    """
    ids: set[str] = set()
    with _RUNS_LOCK:
        ids.update(_RUNS.keys())
    if os.path.isdir(config.RUNS_DIR):
        try:
            ids.update(os.listdir(config.RUNS_DIR))
        except OSError as exc:
            print(f"[warn] 读取 runs 目录失败：{exc}")
    return sorted(ids, reverse=True)


def latest_gallery() -> dict[str, Any]:
    """汇总每个 (站点, 模型) 的最新一次结果，供首页卡片墙使用。

    去重规则是用户明确要求的：同一个站点同一个模型只保留最新那张图，不做历史堆叠。

    只有出图成功的结果才进首页，这也是用户明确要求的。所以失败结果不参与去重，
    某个组合最近一次跑挂了，首页继续显示它上一次成功的图，而不是翻成一张失败卡；
    从来没成功过的组合干脆不出现在墙上。失败详情不会丢，去「历史」里按批次看。

    正在跑的格子做叠加而不是替换：旧图继续显示并保持动画，只额外打一个"重跑中"标记，
    等新结果出来再整格换掉。这样刷新过程中卡片墙不会出现空洞。

    @return {"cells": 卡片列表（按站点、模型排序）, "running": 是否有批次在跑}
    """
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    running_keys: set[tuple[str, str]] = set()
    any_running = False

    for run_id in _all_run_ids():
        run = get_run(run_id)
        if run is not None:
            snapshot = run.snapshot()
            if snapshot["running"]:
                any_running = True
                pending = set(snapshot["pending"])
                for task in run.tasks:
                    if f"{task['site']} / {task['model']}" in pending:
                        running_keys.add((task["site"], task["model"]))
            items = [
                _gallery_cell(r, run_id, snapshot.get("finished_at"))
                for r in snapshot["results"]
            ]
        else:
            items = _disk_cells(run_id)

        # 先到先得：外层已经按 run_id 倒序走，第一次见到的就是最新的那条。
        # 只收成功且真出了图的：ok 为真但 svg_bytes 为 0 的情况是存在的（模型回了话
        # 却没吐出 <svg>），那种格子放上去也是一片空白，和失败没区别，一起挡掉。
        for item in items:
            if not item.get("ok") or not item.get("svg_bytes"):
                continue
            cells.setdefault((item["site"], item["model"]), item)

    # 没有历史成功图的组合不建占位格：首页只展示成功结果，空壳格子会让人以为
    # 出了图却渲染失败。它正在跑这件事「跑批」页有进度条，不必在墙上再说一遍。
    #
    # 必须先拷贝再打标记：_disk_cells 返回的是缓存里的原对象，直接改会把"重跑中"
    # 永久写进缓存，这批跑完之后历史格子还会一直顶着那个角标。
    for key in running_keys:
        cell = cells.get(key)
        if cell is not None:
            cells[key] = {**cell, "running": True}

    ordered = sorted(cells.values(), key=lambda c: (c["site"], c["model"]))
    return {"cells": ordered, "running": any_running}


def model_history(site: str, model: str) -> dict[str, Any]:
    """列出一个站点模型组合的所有已完成版本，包括失败结果。

    @param site 精确匹配的站点名，允许查询已删除站点的历史
    @param model 精确匹配的模型名
    @return 倒序版本列表及该组合是否仍有待完成请求，不包含原始输出或配置
    """
    versions = []
    running = False
    for run_id in _all_run_ids():
        run = get_run(run_id)
        if run is not None:
            snapshot = run.snapshot()
            # 用结构化字段判断组合，不能拆分带斜杠的显示名称。已产出结果的
            # 模型不再算等待中，即使同一批次还有其他慢模型。
            completed = {(r.get("site"), r.get("model")) for r in snapshot["results"]}
            if snapshot["running"] and (site, model) not in completed:
                running |= any(t["site"] == site and t["model"] == model for t in run.tasks)
            items = [_gallery_cell(r, run_id, snapshot["finished_at"]) for r in snapshot["results"]]
        else:
            items = _disk_cells(run_id)
        versions.extend(dict(item) for item in items if item["site"] == site and item["model"] == model)
    return {"versions": versions, "running": running}


def run_svg(run_id: str, site: str, model: str) -> str | None:
    """取某批次里某个 (站点, 模型) 的 SVG 源码。

    直接按前端带回来的 run_id 定位，不再全量倒序扫一遍：run_id 是首页卡片自带的
    版本号，前端只会拿它请求自己刚拿到的那一版，省掉一次全历史遍历。

    @param run_id 跑批 id，非法值由 load_run_file 兜底拦截
    @param site 站点名
    @param model 模型名
    @return SVG 源码（已在跑批时清洗过）；找不到或该用例没出图时返回 None
    """
    run = get_run(run_id)
    if run is not None:
        results = run.snapshot(include_svg=True)["results"]
    else:
        data = load_run_file(run_id)
        results = (data or {}).get("results") or []
    for result in results:
        if result.get("site") == site and result.get("model") == model:
            return result.get("svg") or None
    return None
