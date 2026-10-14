"""基础能力评测执行器，复用站点配置和客户端，结果与 SVG 跑批分开保存。

一个格子固定为配置×题目×重复编号，已获得完整回答的格子不再请求。每题完成即原子保存，
进程重启后的未完成批次标记 interrupted，不自动重发可能已计费的请求。
每次执行结束后自动原地重试接口失败，至多三轮；也可显式重试。
旧尝试留在原记录内，已经作答的题目不重新请求。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import re
import statistics
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

import config
import iq_tasks
import llm
from query_cache import QueryCache, code_revision

ROOT = os.path.join(config.ROOT, "iq-runs")
_RUNS: dict[str, "Run"] = {}
_LOCK = threading.RLock()
_VALID_ID = re.compile(r"^[a-f0-9]{32}$")
AUTO_RETRY_DELAYS = (1, 2, 4)
_SUMMARY_CACHE: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
_SUMMARY_LOCK = threading.RLock()
_DISK_SUMMARIES = QueryCache(entries=256)
_SUMMARY_CODE = None


def _integer(value: Any, low: int, high: int, name: str) -> int:
    """验证整数预算，拒绝布尔值与隐式截断的小数。

    @param value 客户端原值
    @param low 下界，包含
    @param high 上界，包含
    @param name 错误提示字段名
    @return 已验证的整数；非法时抛 ValueError
    """
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name}必须是 {low} 到 {high} 的整数")
    return value


def _path(run_id: str) -> str:
    """限定文件标识为 UUID hex，任何客户端路径都不得直接参与文件访问。

    @param run_id 32 位小写十六进制批次标识
    @return 固定目录中的结果文件绝对路径
    """
    if not isinstance(run_id, str) or not _VALID_ID.fullmatch(run_id):
        raise ValueError("无效的评测 ID")
    return os.path.join(ROOT, run_id + ".json")


def _persist(data: dict[str, Any]) -> None:
    """原子保存无凭据快照；写入失败必须停止继续调度付费请求。

    @param data 含 run_id 的公开批次数据
    """
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    target = _path(data["run_id"])
    temporary = target + ".tmp"
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def _redact(text: str, cfg: dict[str, Any]) -> str:
    """在落盘前去掉上游可能回显的凭据，不把密钥带入异常或原回答。

    @param text 上游返回的文本
    @param cfg 仅留在内存中的私有配置
    @return 凭据已替换的文本
    """
    for site in cfg.get("sites", []):
        secret = site.get("api_key")
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _cost(cfg: dict[str, Any], site: dict[str, Any], model: str, usage: dict[str, Any]) -> float | None:
    """按已知用量和两项完整单价估算费用；不把估算 token 或缺失单价当免费。

    @param cfg 本批配置
    @param site 站点配置
    @param model 请求模型名称
    @param usage 客户端归一化用量，reasoning 已包含在 completion 中
    @return 估算美元金额；信息不完整时返回 None
    """
    prices = config.pricing_for(cfg, site, model)
    if usage.get("estimated", True) or any(prices.get(k) is None for k in ("input", "output")):
        return None
    if any(not math.isfinite(prices[k]) or prices[k] < 0 for k in ("input", "output")):
        return None
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens")):
        return None
    return (usage["prompt_tokens"] * prices["input"] + usage["completion_tokens"] * prices["output"]) / 1_000_000


def _attempt(cfg: dict[str, Any], selected: dict[str, Any], question: dict[str, Any], repeat: int,
             options: dict[str, Any], cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """执行一次无历史、无补考的请求，服务错误与模型答案错误分开保存。

    @param cfg 私有配置快照，永不直接序列化
    @param selected 已验证的站点/模型组合及推理配置
    @param question 固定题目；只发送 prompt
    @param repeat 从 1 开始的重复编号
    @param options 本次统一预算
    @param cancel_event 批次停止信号；仅中断未完成的请求，不改写已返回的完整成绩
    @return 可持久化的单次调用与判分结果
    """
    started = time.perf_counter()
    site = next(s for s in cfg["sites"] if s["name"] == selected["site"])
    result = {"site": selected["site"], "model": selected["model"], "question_id": question["id"],
              "category": question["category"], "repeat": repeat, "started_at": time.time(),
              "score": None, "status": "service_error", "response_ok": False, "raw": "",
              "reason": "", "ttft": None, "cost_estimate_usd": None, "usage": None}
    try:
        # 全局及站点 SVG 提示词不能进入题目；站点输出预算也不覆盖本批统一上限。
        response = llm.stream_chat(site["base_url"], site.get("api_key", ""), selected["model"], question["prompt"],
                                   max_tokens=options["max_tokens"], temperature=0,
                                   timeout=options["timeout_seconds"], thinking_mode=selected["thinking_mode"],
                                   retry_stream_options=False, cancel_event=cancel_event)
        result.update(raw=_redact(response["text"], cfg), usage=response["usage"], ttft=response["ttft"],
                      finish_reason=response["finish_reason"], response_model=response.get("response_model"),
                      cost_estimate_usd=_cost(cfg, site, selected["model"], response["usage"]))
        if not response.get("completed") and not response.get("truncated"):
            result["reason"] = "响应流缺少完成标记，按服务中断记录"
        else:
            result["response_ok"] = True
            if response.get("truncated"):
                result.update(status="truncated", score=0, reason="输出额度耗尽或正文超限")
            elif response.get("error"):
                result.update(status="empty_answer", score=0, reason=_redact(response["error"], cfg))
            else:
                result.update(iq_tasks.grade(question, response["text"]))
    except llm.LLMCancelled as exc:
        result.update(status="cancelled", reason=str(exc))
    except llm.LLMError as exc:
        result["reason"] = _redact(str(exc), cfg)
    except Exception as exc:  # 调用或评分自身的 bug 不伪装成模型答错。
        result.update(status="runner_error", reason=f"评测内部错误：{type(exc).__name__}", score=None)
    result["duration_seconds"] = round(time.perf_counter() - started, 3)
    return result


def summaries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """按固定题目和重复次数汇总；缺测时不发布完整能力分，不以最好结果替代平均。

    @param data 不含凭据的批次记录
    @return 每个组合的成绩、覆盖率、模型完成时间、延迟与费用；未完成成绩为 None
    """
    output = []
    repeat_count = data["options"]["repeats"]
    total = len(data["questions"]) * repeat_count
    official = "benchmark" in data["suite"]
    expected_cells = {(q["id"], repeat) for q in data["questions"] for repeat in range(1, repeat_count + 1)}
    for selected in data["configurations"]:
        rows = [r for r in data["results"] if (r["site"], r["model"]) == (selected["site"], selected["model"])]
        valid = sum(r["response_ok"] for r in rows)
        categories = {}
        weighted = 0.0
        # 数量相同也可能有重复结果或漏题；必须核对每一个题号与重复编号。
        complete = (len(rows) == total and {(r["question_id"], r["repeat"]) for r in rows} == expected_cells
                    and not any(r["status"] in ("runner_error", "cancelled") for r in rows))
        for category in data["suite"]["weights"]:
            questions = [q for q in data["questions"] if q["category"] == category]
            subset = [r for r in rows if r["category"] == category]
            expected = len(questions) * repeat_count
            scores = [sum((r["score"] or 0) for r in subset if r["question_id"] == q["id"]) / repeat_count for q in questions]
            weighted += 100 * statistics.mean(scores) * data["suite"]["weights"][category]
            categories[category] = {"completed": len(subset), "expected": expected,
                                    "correct": sum(r["status"] == "correct" for r in subset),
                                    "valid": sum(r["response_ok"] for r in subset),
                                    "score": round(100 * statistics.mean(scores), 2)
                                    if complete and (not official or all(r["response_ok"] for r in subset)) else None}
        # 仅在最终展示时四舍五入；否则三次中答对一次会被换算成 49.99 而不是 50。
        delivery = round(weighted, 2) if complete and (not official or valid == total) else None
        ability = delivery if complete and valid == total else None
        rank_score = ability if data["suite"].get("formal", True) else None
        costs = [r["cost_estimate_usd"] for r in rows if r["cost_estimate_usd"] is not None]
        # 接口失败也可能已经计费；累加已知部分，未知部分不能因重试成功变成免费。
        prior_cost = next((p for p in data.get("retry", {}).get("prior_costs", [])
                           if (p["site"], p["model"]) == (selected["site"], selected["model"])), {})
        if prior_cost.get("known_cost_usd") is not None:
            costs.append(prior_cost["known_cost_usd"])
        latencies = [r["ttft"] for r in rows if r["ttft"] is not None]
        output.append({**selected, "completed": len(rows), "expected": total, "valid": valid,
                       "correct": sum(r["status"] == "correct" for r in rows), "categories": categories,
                       "ability_score": ability, "rank_score": rank_score,
                       # 单模型最后一题的完成时间不受同批其他模型耗时影响，也可从旧记录重建。
                       "completed_at": max((r["started_at"] + r["duration_seconds"] for r in rows),
                                           default=None) if complete else None,
                       # 旧 API 保留历史指数以兼容已有导出；新基准和页面均只使用百分比。
                       "iq_index": round(weighted * 1.5, 2) if ability is not None and not official else None,
                       "delivery_score": delivery, "response_rate": round(valid / len(rows) * 100, 2) if rows else None,
                       "incorrect": sum(r["response_ok"] and r["score"] == 0 for r in rows),
                       "missing": total - len(rows),
                       "elapsed_seconds": round(max((r["started_at"] + r["duration_seconds"] for r in rows), default=data["started_at"]) -
                                                min((r["started_at"] for r in rows), default=data["started_at"]), 3),
                       "service_errors": sum(r["status"] == "service_error" for r in rows),
                       "runner_errors": sum(r["status"] == "runner_error" for r in rows),
                       "cancelled": sum(r["status"] == "cancelled" for r in rows),
                       "mean_seconds": round(statistics.mean(r["duration_seconds"] for r in rows), 3) if rows else None,
                       "ttft_median": round(statistics.median(latencies), 3) if latencies else None,
                       "known_cost_usd": round(sum(costs), 6) if costs else None,
                       "cost_complete": all(r["cost_estimate_usd"] is not None for r in rows) and len(rows) == total
                       and prior_cost.get("complete", True), "complete": complete})
    return output


class Run:
    """一个批次的串行持久化状态；后台调度与 HTTP 停止请求共享锁。"""

    def __init__(self, cfg: dict[str, Any], data: dict[str, Any]) -> None:
        """保留私有连接配置与公开结果两个独立对象，防止无意泄出凭据。

        @param cfg 已深拷贝的私有配置
        @param data 可持久化的批次初始状态
        """
        self.cfg = cfg
        self.data = data
        self.lock = threading.RLock()
        self.stop = threading.Event()

    def snapshot(self, full: bool = False, *, reviews: bool = True,
                 review_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """生成线程一致的快照；轮询只回摘要，完整回答按需获取。

        @param full 是否包含题干、答案和原始回答
        @param reviews 是否合并复核结论；内部提交复核时关闭，避免递归和重复扫描
        @param review_snapshot 历史列表共享的本批复核快照，省略时自行读取
        @return 独立副本，不能通过修改它改变运行状态
        """
        with self.lock:
            result = copy.deepcopy(self.data)
        result["summary"] = summaries(result)
        # 规范化在独立快照上运行；历史文件、在途结果和官方评分保持可追溯。
        import iq_scoring
        iq_scoring.annotate(result)
        if reviews:
            import iq_review
            iq_review.annotate(result, review_snapshot)
        _annotate_retry_progress(result)
        result["retryable_errors"] = sum(r["status"] == "service_error" and not r["response_ok"] for r in result["results"])
        if result.get("retry") and result["retry"].get("mode") != "in_place":
            targets = {tuple(cell) for cell in result["retry"]["targets"]}
            result["retry"]["done"] = sum(_cell(r) in targets for r in result["results"])
        if not full:
            result.pop("results")
            result.pop("questions")
            for attempt in [result.get("retry", {}), *result.get("retry_history", [])]:
                attempt.pop("previous_results", None)
                attempt.pop("attempts", None)
            for attempt in result.get("retry_history", []):
                attempt.pop("items", None)
        return result

    def save(self) -> None:
        """在状态锁内保存，避免停止请求把较旧状态覆盖到较新结果之上。"""
        with self.lock:
            _persist(self.data)


def start(cfg: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """验证并创建批次；全站同时只允许一个 IQ 批次，UUID 保证重复提交幂等。

    并发允许 1 到 200；未传入时沿用 32，放宽上限不改变默认请求量。

    @param cfg 当前保存的站点配置，将固定为本批快照
    @param request 题集、重复次数、预算、站点/模型列表和 request_id
    @return 新批次或同一请求的已有批次摘要
    """
    run_id = request.get("request_id")
    _path(run_id)
    suite = request.get("suite", "bbeh-smoke")
    tasks = iq_tasks.questions(suite)
    official = suite not in ("quick", "full")
    thinking = request.get("thinking_mode", "default")
    if thinking not in ("default", "enabled", "disabled"):
        raise ValueError("无效的推理模式")
    options = {"repeats": _integer(request.get("repeats", 1 if official else 3), 1, 20, "重复次数"),
               "max_tokens": _integer(request.get("max_tokens", 32768 if official else 8192), 128, 262144, "输出上限"),
               "timeout_seconds": _integer(request.get("timeout_seconds", 600 if official else 180), 10, 1800, "读取超时"),
               "concurrency": _integer(request.get("concurrency", 32), 1, 200, "并发"), "temperature": 0}
    chosen = request.get("tasks")
    if not isinstance(chosen, list) or not chosen:
        raise ValueError("请至少选择一个站点/模型组合")
    configurations = []
    seen = set()
    for choice in chosen:
        if not isinstance(choice, dict) or not isinstance(choice.get("site"), str) or not isinstance(choice.get("model"), str):
            raise ValueError("模型组合格式错误")
        site = next((s for s in cfg["sites"] if s["name"] == choice["site"]), None)
        model = choice["model"].strip()
        if not site or not site.get("base_url") or not model or len(model) > 200:
            raise ValueError("站点不存在、服务地址为空或模型名无效")
        pair = (site["name"], model)
        if pair in seen:
            raise ValueError("不能重复选择同一站点和模型")
        seen.add(pair)
        # 官方评测使用批次统一选项，不继承各站点可能互不相同的推理开关。
        mode = thinking if official else site.get("thinking_mode", "default")
        configurations.append({"site": site["name"], "model": model, "thinking_mode": mode,
                               "effective_thinking_mode": llm.effective_thinking_mode(model, mode),
                               "endpoint_sha256": hashlib.sha256(site["base_url"].encode()).hexdigest()})
        import official_channels
        channel = official_channels.adapter(site["base_url"])
        if channel:
            execution = channel.execution(model)
            configurations[-1].update(execution=execution, effective_thinking_mode=
                "codex-medium" if execution["transport"] == "codex-cli" else "kimi-default")
    fingerprint = hashlib.sha256(json.dumps({"options": options, "suite": iq_tasks.manifest(suite),
                                              "configs": configurations}, sort_keys=True).encode()).hexdigest()
    with _LOCK:
        existing = get(run_id)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("同一请求 ID 不能用于不同评测配置")
            return existing
        if any(run.data["status"] in ("running", "stopping") for run in _RUNS.values()):
            raise ValueError("已有 IQ 评测在运行，请等待完成或先停止")
        seed = int(uuid.uuid4().hex[:8], 16)
        data = {"run_id": run_id, "fingerprint": fingerprint, "suite": iq_tasks.manifest(suite), "options": options,
                "configurations": configurations, "questions": tasks, "results": [], "seed": seed,
                "started_at": time.time(), "finished_at": None, "status": "running", "error": "",
                "auto_retry": {"max_rounds": len(AUTO_RETRY_DELAYS), "round": 0},
                "total": len(tasks) * options["repeats"] * len(configurations), "done": 0,
                "pricing": [{"site": c["site"], "model": c["model"],
                             "prices": config.pricing_for(cfg, next(s for s in cfg["sites"] if s["name"] == c["site"]), c["model"])} for c in configurations]}
        run = Run(copy.deepcopy(cfg), data)
        run.save()  # 首次写入失败时不启动任何请求。
        _RUNS[run_id] = run
        threading.Thread(target=_execute, args=(run,), daemon=True).start()
        return run.snapshot()


def _cell(row: dict[str, Any]) -> tuple:
    """用逻辑测量位置关联原失败与重试，不把传输尝试次数混入题目重复编号。"""
    return tuple(row[k] for k in ("site", "model", "question_id", "repeat"))


def _editable_data(snapshot: dict[str, Any]) -> dict[str, Any]:
    """剥离查询时计算的评分摘要；编辑副本，不把派生字段重新写入原始结果。"""
    data = copy.deepcopy(snapshot)
    for key in ("summary", "normalization_version", "review_version", "retryable_errors", "auto_review"):
        data.pop(key, None)
    for row in data["results"]:
        row.pop("normalized", None)
        row.pop("final", None)
    return data


def retry_failed(cfg: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """在原批次中显式重试接口失败项，原 ID、题目和其他回答保持不变。

    本次显式请求完成后仍有接口失败时，另有最多三轮自动重试；重复请求 ID
    只读取已有执行，不能重置自动重试额度。

    Args:
        cfg: 当前连接凭据；站点端点必须与原批次一致，不能借重试悄悄切换渠道。
        request: 只允许 run_id 和新的 request_id，不接受预算、题目、答案或筛选条件。
    Returns:
        原批次摘要。request_id 标识一次重试操作，跨重启幂等，不是新的评测 ID。
    """
    if not isinstance(request, dict) or set(request) != {"run_id", "request_id"}:
        raise ValueError("重试只接受原评测 ID 和请求 ID，不能修改题目或参数")
    source_id, request_id = request["run_id"], request["request_id"]
    _path(source_id)
    _path(request_id)
    if source_id == request_id:
        raise ValueError("重试必须使用新的请求 ID")
    with _LOCK:
        source = get(source_id, full=True)
        if not source:
            raise ValueError("找不到原评测")
        source_id = source["run_id"]  # 兼容已合并旧重试记录的链接，始终操作原批次。
        rounds = [source.get("retry", {}), *source.get("retry_history", [])]
        if any(r.get("request_id") == request_id for r in rounds):
            return Run({}, _editable_data(source)).snapshot()
        if os.path.isfile(_path(request_id)):
            raise ValueError("请求 ID 已被其他评测使用")
        for previous in history(limit=None):
            if previous["run_id"] != source_id and any(r.get("request_id") == request_id for r in
                    [previous.get("retry", {}), *previous.get("retry_history", [])]):
                raise ValueError("同一请求 ID 不能用于不同评测")
        if source["status"] in ("running", "stopping"):
            raise ValueError("请等原评测结束后再重试")
        if any(run.data["status"] in ("running", "stopping") for run in _RUNS.values()):
            raise ValueError("已有 IQ 评测在运行，请等待完成或先停止")
        failed = [r for r in source["results"] if r["status"] == "service_error" and not r["response_ok"]]
        if not failed:
            raise ValueError("没有可以重试的接口失败项")
        if iq_tasks.manifest(source["suite"]["id"])["grader_version"] != source["suite"].get("grader_version"):
            raise ValueError("原评分器版本已变更，不能将新旧评分合并，请新建评测")
        targets = {_cell(r) for r in failed}
        if len({_cell(r) for r in source["results"]}) != len(source["results"]):
            raise ValueError("原评测存在重复结果，不能自动重试")
        configs = copy.deepcopy(source["configurations"])
        planned = {(c["site"], c["model"], q["id"], repeat) for c in configs for q in source["questions"]
                   for repeat in range(1, source["options"]["repeats"] + 1)}
        if not targets <= planned:
            raise ValueError("失败项与原题目计划不一致，不能自动重试")
        executions = []
        for selected in configs:
            errors = [r for r in failed if (r["site"], r["model"]) == (selected["site"], selected["model"])]
            if not errors:
                continue
            site = next((s for s in cfg["sites"] if s["name"] == selected["site"]), None)
            if not site or not site.get("base_url") or hashlib.sha256(site["base_url"].encode()).hexdigest() != selected.get("endpoint_sha256"):
                raise ValueError("原站点不存在或服务地址已变更，请恢复原地址后重试，或新建评测")
            import official_channels
            channel = official_channels.adapter(site["base_url"])
            if channel:
                executions.append({"site": selected["site"], "model": selected["model"],
                                   "execution": channel.execution(selected["model"])})
        data = _editable_data(source)
        data["auto_retry"] = {"max_rounds": len(AUTO_RETRY_DELAYS), "round": 0}
        _prepare_retry(data, failed, request_id, executions)
        run = Run(copy.deepcopy(cfg), data)
        run.save()
        _RUNS[source_id] = run
        threading.Thread(target=_execute, args=(run,), daemon=True).start()
        return run.snapshot()


def _prepare_retry(data: dict[str, Any], failed: list[dict[str, Any]], request_id: str,
                   executions: list[dict[str, Any]], *, automatic: bool = False) -> None:
    """为已校验的失败项准备原地重试；调用方持锁并在发请求前持久化。

    Args:
        data: 当前批次的可写状态，保留同一 run_id 和逻辑题目数。
        failed: 本轮仍为 service_error 的原结果，不能包含完整答案。
        request_id: 本轮唯一标识；手动请求重放时用它保证幂等。
        executions: 本次实际使用的官方渠道适配器信息。
        automatic: 自动执行时记录本链轮次和等待时间；显式重试从新额度开始。
    """
    previous = data.get("retry")
    if previous:
        data.setdefault("retry_history", []).append(copy.deepcopy(previous))
    auto = data["auto_retry"]
    delay = 0
    if automatic:
        delay = AUTO_RETRY_DELAYS[auto["round"]]
        auto["round"] += 1
    # 旧失败在新响应保存前始终保留。每轮尝试和累积费用单独留证，
    # 不能把重试次数算成新题目，也不能用取消结果覆盖原失败后丢失重试入口。
    started = time.time()
    data.update(status="running", finished_at=None, error="", result_revision=data.get("result_revision", 0) + 1,
                retry={"mode": "in_place", "request_id": request_id, "source_run_id": data["run_id"],
                       "trigger": "automatic" if automatic else "manual",
                       "auto_round": auto["round"], "auto_max_rounds": auto["max_rounds"],
                       "round": len(data.get("retry_history", [])) + 1, "status": "running",
                       "started_at": started, "finished_at": None, "count": len(failed), "done": 0,
                       "next_attempt_at": started + delay,
                       "retained": len(data["results"]) - len(failed), "targets": [list(_cell(r)) for r in failed],
                       "items": [{**{k: r[k] for k in ("site", "model", "question_id", "repeat", "category")},
                                  "status": "queued", "started_at": None, "finished_at": None,
                                  "duration_seconds": None, "reason": ""} for r in failed],
                       "previous_results": [_editable_result(r) for r in failed], "attempts": [],
                       "executions": copy.deepcopy(executions),
                       "prior_costs": copy.deepcopy((previous or {}).get("prior_costs", []))})


def _editable_result(row: dict[str, Any]) -> dict[str, Any]:
    """保存一次原始尝试的副本，不把查询时的规范化结论当作原始记录。"""
    result = copy.deepcopy(row)
    result.pop("normalized", None)
    result.pop("final", None)
    return result


def _add_prior_cost(retry: dict[str, Any], row: dict[str, Any]) -> None:
    """累计被替换或被取消尝试的费用；只在结果写入时追加，防止等待期间双算。"""
    costs = retry.setdefault("prior_costs", [])
    prior = next((p for p in costs if (p["site"], p["model"]) == (row["site"], row["model"])), None)
    if prior is None:
        prior = {"site": row["site"], "model": row["model"], "known_cost_usd": None, "complete": True}
        costs.append(prior)
    amount = row.get("cost_estimate_usd")
    if amount is None:
        prior["complete"] = False
    else:
        prior["known_cost_usd"] = (prior["known_cost_usd"] or 0) + amount


def _annotate_retry_progress(data: dict[str, Any]) -> None:
    """给查询副本补齐旧重试的逐项结果，并纠正重启后遗留的运行状态。

    Args:
        data: 独立的公开快照；不能传正在被执行线程修改的原始状态。
    """
    retry = data.get("retry")
    if not retry:
        return
    if "items" not in retry:
        # 旧版本只有结束后的 attempts。只能恢复实际记录的结果，不能把没有记录
        # 的请求一律说成排队或运行中，也不能拿上一轮失败的耗时冒充本轮耗时。
        attempts = retry.get("attempts", []) if retry.get("mode") == "in_place" else data["results"]
        recorded = {_cell(row): row for row in attempts}
        retry["items"] = []
        for cell in retry.get("targets", []):
            row = recorded.get(tuple(cell), {})
            retry["items"].append({**dict(zip(("site", "model", "question_id", "repeat"), cell)),
                                   "status": row.get("status", "unknown"),
                                   "started_at": row.get("started_at"),
                                   "duration_seconds": row.get("duration_seconds"),
                                   "reason": row.get("reason", "旧记录未保存该项运行进度")})
    if data["status"] == "interrupted":
        retry["status"] = "interrupted"
        for item in retry["items"]:
            if item["status"] in ("queued", "running", "unknown"):
                # 未保存开始状态的请求也可能已经发出；重启后不能再显示它正在运行。
                item.update(status="interrupted", reason="服务曾重启，该项未完成；没有自动重发",
                            duration_seconds=None)


def _retry_attempt(run: Run, selected: dict[str, Any], question: dict[str, Any], repeat: int) -> dict[str, Any]:
    """在线程实际开始执行时登记单项进度，避免把线程池队列误报为请求中。

    Args:
        run: 当前原地重试的批次；与停止入口共用状态锁。
        selected: 本次已验证的站点和模型配置。
        question: 固定题目，只向模型发送原 prompt。
        repeat: 原评测的重复编号，不能当作本轮传输重试次数。
    Returns:
        一次调用的原始结果；替换旧答案和累计完成数仍由调度线程统一处理。
    """
    with run.lock:
        item = next(row for row in run.data["retry"]["items"]
                    if _cell(row) == (selected["site"], selected["model"], question["id"], repeat))
        # 状态只写内存；保存答案时一并持久化，避免每个开始事件重写整份大题集。
        # 重启时 snapshot 会将未收尾状态标为中断，不会自动补发付费请求。
        item.update(status="running", started_at=time.time())
    return _attempt(run.cfg, selected, question, repeat, run.data["options"], run.stop)


def _execute(run: Run) -> None:
    """在同一执行器中收尾并最多自动重试三轮，最后统一复核并释放凭据。

    Args:
        run: 已登记批次；停止信号在整条重试链中共用，重启不会重新执行此链。
    """
    try:
        while _execute_round(run):
            # 等待可立即取消，不创建另一条线程或重置 stop。下一轮先检查停止，
            # 因而等待期间点击停止也不会向模型提交新的请求。
            delay = max(0, run.data["retry"]["next_attempt_at"] - time.time())
            run.stop.wait(delay)
        with run.lock:
            if run.data["status"] != "failed":
                # 所有传输尝试保存完毕后才复核，避免中间轮次复核旧答案或重复收费。
                try:
                    import iq_review
                    iq_review.schedule_run(run.cfg, run.snapshot(full=True, reviews=False))
                except Exception as exc:
                    run.data["auto_review_error"] = "自动复核未启动：" + type(exc).__name__
                    try:
                        run.save()
                    except OSError:
                        pass  # 复核异常不改写已经收尾的测评结果。
    finally:
        run.cfg = {}  # 整条重试链结束后释放凭据，不能在轮次之间提前清空。


def _execute_round(run: Run) -> bool:
    """按题目交错执行各配置；停止信号同时中断在途请求，回收后再结束批次。

    Args:
        run: 首轮或原地重试的同一批次，轮次转换与停止共用状态锁。
    Returns:
        下一轮自动重试已持久化时返回 True；结束、停止或保存失败返回 False。
    """
    rng = random.Random(run.data["seed"])
    tasks = list(run.data["questions"])
    rng.shuffle(tasks)
    jobs = []
    for repeat in range(1, run.data["options"]["repeats"] + 1):
        for task in tasks:
            choices = list(run.data["configurations"])
            rng.shuffle(choices)
            jobs.extend((choice, task, repeat) for choice in choices)
    if run.data.get("retry"):
        # 重试沿用原测量网格，只调度已核对的传输失败位置；答错及格式错误不补考。
        targets = {tuple(cell) for cell in run.data["retry"]["targets"]}
        jobs = [job for job in jobs if (job[0]["site"], job[0]["model"], job[1]["id"], job[2]) in targets]
    iterator = iter(jobs)
    fatal = False
    try:
        with ThreadPoolExecutor(max_workers=run.data["options"]["concurrency"]) as pool:
            active = {}
            exhausted = False
            while active or not exhausted:
                # 停止和提交共享锁：停止请求返回后不能再有新的提交。
                with run.lock:
                    while not exhausted and not run.stop.is_set() and len(active) < run.data["options"]["concurrency"]:
                        job = next(iterator, None)
                        if job is None:
                            exhausted = True
                            break
                        if run.data.get("retry", {}).get("mode") == "in_place":
                            future = pool.submit(_retry_attempt, run, *job)
                        else:
                            future = pool.submit(_attempt, run.cfg, *job, run.data["options"], run.stop)
                        active[future] = job
                    if run.stop.is_set():
                        exhausted = True
                if not active:
                    break
                ready, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in ready:
                    active.pop(future)
                    result = future.result()
                    with run.lock:
                        if run.data.get("retry"):
                            result["retry_source_run_id"] = run.data["retry"]["source_run_id"]
                            execution = next((e["execution"] for e in run.data["retry"]["executions"]
                                              if (e["site"], e["model"]) == (result["site"], result["model"])), None)
                            if execution:
                                result["execution"] = execution
                        if run.data.get("retry", {}).get("mode") == "in_place":
                            retry = run.data["retry"]
                            result["retry_request_id"] = retry["request_id"]
                            index = next(i for i, row in enumerate(run.data["results"]) if _cell(row) == _cell(result))
                            retry["attempts"].append(copy.deepcopy(result))
                            retry["done"] = len(retry["attempts"])
                            item = next(row for row in retry["items"] if _cell(row) == _cell(result))
                            item.update(status=result["status"], finished_at=time.time(),
                                        duration_seconds=result["duration_seconds"], reason=result["reason"])
                            # 取消没有产生可评分的答案，保留原失败项，让用户仍可再次重试。
                            # 其他结果（包括答错）替换旧失败，绝不与旧尝试挑选最高分。
                            if result["status"] == "cancelled":
                                _add_prior_cost(retry, result)
                            else:
                                _add_prior_cost(retry, run.data["results"][index])
                                run.data["results"][index] = result
                            run.data["result_revision"] = run.data.get("result_revision", 0) + 1
                        else:
                            run.data["results"].append(result)
                        run.data["done"] = len(run.data["results"])
                        if result["status"] == "runner_error":
                            fatal = True
                            run.stop.set()
                        try:
                            run.save()
                        except OSError:
                            # 持久化失败仍收集在途结果，但停止扩大不可追溯的付费调用。
                            fatal = True
                            run.stop.set()
                            run.data["error"] = "保存结果失败，已停止继续调度"
    except Exception as exc:
        fatal = True
        with run.lock:
            run.data["error"] = f"评测执行中断：{type(exc).__name__}"
    with run.lock:
        # 取消记录也会计入已处理数量，但不能把“全部请求已收尾”误报成“评测完成”。
        complete = (run.data["done"] == run.data["total"] and
                    not any(r["status"] == "cancelled" for r in run.data["results"]))
        if run.data.get("retry", {}).get("mode") == "in_place":
            retry = run.data["retry"]
            complete = complete and retry["done"] == retry["count"] and not run.stop.is_set()
        final_status = "failed" if fatal else ("completed" if complete and not run.stop.is_set() else "cancelled")
        finished_at = time.time()
        if run.data.get("retry", {}).get("mode") == "in_place":
            run.data["retry"].update(status=final_status, finished_at=finished_at)
            for item in run.data["retry"]["items"]:
                if item["status"] in ("queued", "running"):
                    # 未调度和异常中断的项也要收尾，但不增加 done、不替换原失败答案。
                    started = item["started_at"]
                    item.update(status="interrupted" if fatal else "cancelled",
                                finished_at=finished_at,
                                duration_seconds=round(finished_at - started, 3) if started else None,
                                reason="执行中断，未收到结果" if fatal else
                                       ("请求已取消" if started else "停止前尚未发起请求"))
        auto = run.data.get("auto_retry", {"max_rounds": 0, "round": 0})
        failed = [r for r in run.data["results"] if r["status"] == "service_error" and not r["response_ok"]]
        again = final_status == "completed" and bool(failed) and auto["round"] < auto["max_rounds"]
        if again:
            # 不向外发布短暂 completed 状态，否则另一个批次可能抢占运行名额。
            # 同一执行复用配置和实际适配器，不能在自动轮次中偷换渠道或预算。
            executions = run.data.get("retry", {}).get("executions")
            if executions is None:
                executions = [{"site": c["site"], "model": c["model"], "execution": c["execution"]}
                              for c in run.data["configurations"] if c.get("execution")]
            _prepare_retry(run.data, failed, uuid.uuid4().hex, executions, automatic=True)
        else:
            run.data.update(status=final_status, finished_at=finished_at)
        try:
            run.save()
        except OSError:
            run.data["status"] = "failed"
            run.data["finished_at"] = time.time()
            run.data["error"] = "结果未完整保存，请先导出当前记录"
            run.stop.set()
            return False
        return again


def get(run_id: str, full: bool = False, *, reviews: bool = True,
        review_snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """读取内存或磁盘批次；进程重启遗留的运行状态标为中断，绝不自动补考。

    @param run_id 合法批次 ID
    @param full 是否包含逐题回答
    @param reviews 是否合并复核结论；复核调度内部关闭，查询默认开启
    @param review_snapshot 历史查询预先读取的复核快照，省略时自行读取
    @return 批次快照；不存在时为 None
    """
    global _SUMMARY_CODE
    path = _path(run_id)
    with _LOCK:
        run = _RUNS.get(run_id)
    if run:
        # 在途运行仍读取加锁的实时数据，不能用磁盘摘要掩盖尚未持久化的重试进度。
        return run.snapshot(full, reviews=reviews, review_snapshot=review_snapshot)
    if not os.path.isfile(path):
        return None
    import iq_review
    import iq_scoring
    if reviews and review_snapshot is None:
        review_snapshot = iq_review.snapshot_index().get(run_id, {})
    # 只缓存摘要，完整题干和原回答按需读取，避免缓存随长回答无限增长。
    # 同时绑定评分版本和复核内容；原地重试或复核结论变化都会立即失效。
    with _SUMMARY_LOCK:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        key = (path, stamp, reviews, iq_scoring.VERSION, iq_review.VERSION,
               (review_snapshot or {}).get("revision"))
        if not full and key in _SUMMARY_CACHE:
            _SUMMARY_CACHE.move_to_end(key)
            return copy.deepcopy(_SUMMARY_CACHE[key])
        def build():
            """Rebuild a view from original answers, retaining restart interruption semantics."""
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            if data["status"] in ("running", "stopping"):
                data["status"] = "interrupted"
                data["error"] = "服务曾重启，未完成调用没有自动重发"
            return Run({}, data).snapshot(full, reviews=reviews, review_snapshot=review_snapshot)

        if full:
            result = build()
        else:
            if _SUMMARY_CODE is None:
                import iq_benchmarks
                _SUMMARY_CODE = code_revision(__file__, iq_scoring.__file__, iq_review.__file__,
                                              iq_tasks.__file__, iq_benchmarks.__file__)
            # Reuse only derived summaries after restart. Raw answers remain in their
            # original files; any answer, review or scoring-code change rebuilds the view.
            result = _DISK_SUMMARIES.get((path, reviews), (key, _SUMMARY_CODE), build,
                                         os.path.join(ROOT, ".summary-cache"))
        latest = os.stat(path)
        if not full and stamp == (latest.st_mtime_ns, latest.st_ctime_ns, latest.st_size, latest.st_ino):
            # 并发的榜单和历史读取共用一次计算；读到原子替换前的文件时不缓存到新版本。
            _SUMMARY_CACHE[key] = result
            if len(_SUMMARY_CACHE) > 256:
                _SUMMARY_CACHE.popitem(last=False)
            return copy.deepcopy(result)
        return result


def history(limit: int | None = 50) -> list[dict[str, Any]]:
    """列出批次摘要，跳过损坏文件但不删除原始记录。

    @param limit 默认取最近五十批；排行榜传 None，不能遗漏较早的唯一成绩
    @return 按创建时间倒序的运行历史
    """
    ids = set()
    if os.path.isdir(ROOT):
        ids.update(name[:-5] for name in os.listdir(ROOT) if name.endswith(".json") and _VALID_ID.fullmatch(name[:-5]))
    with _LOCK:
        ids.update(_RUNS)
    import iq_review
    review_index = iq_review.snapshot_index()
    rows = []
    for run_id in ids:
        try:
            data = get(run_id, review_snapshot=review_index.get(run_id, {}))
            if data:
                rows.append(data)
        except (ValueError, OSError, KeyError):
            continue
    return sorted(rows, key=lambda r: (r["started_at"], r["run_id"]), reverse=True)[:limit]


def leaderboard(cohort_id: str | None = None, benchmark: str | None = None,
                scoring: str = "reviewed") -> dict[str, Any]:
    """跨全部历史构建长期榜，每个站点和模型取最近完成的有效成绩。

    @param cohort_id 题库和评分版本哈希；未指定时选择最近产生有效成绩的版本
    @param benchmark 可选基准过滤；正式榜不混入该基准试跑，foundation 保留旧题集
    @param scoring reviewed 默认发布最终分；comparison 同批展示复核前后，旧审计口径保持兼容
    @return 版本选项和有效成绩；运行参数随成绩返回，不作为拆榜条件
    """
    if benchmark not in (None, "bbeh-mini", "arc-agi-2", "foundation"):
        raise ValueError("未知的基准")
    if scoring not in ("official", "normalized", "reviewed", "comparison"):
        raise ValueError("未知的评分方式")
    cohorts = {}
    for run in history(limit=None):
        if not run["suite"].get("formal", True):
            continue
        if benchmark and run["suite"].get("benchmark", "foundation") != benchmark:
            continue
        for summary in run["summary"]:
            if scoring == "comparison":
                # 两列必须来自同一份回答，不能拼接两次测试的各自最好或最新分数。
                summary = {**summary, "before_review": {
                    k: copy.deepcopy(summary.get(k)) for k in ("rank_score", "correct", "categories")
                }, "after_review": copy.deepcopy(summary["final"])}
            if scoring == "normalized":
                summary = {**summary, **summary["normalized"]}
            elif scoring == "reviewed":
                summary = {**summary, **summary["final"]}
            # 只检验当前模型，不等待整个批次。缺测不能入榜，零分仍然有效。
            # 对比模式允许原始成绩入榜，待复核的最终分保持为空。
            score = summary.get("rank_score")
            if not summary["complete"] or type(score) not in (int, float) or not math.isfinite(score):
                continue
            options = copy.deepcopy(run["options"])
            # 旧记录只保存站点选项。沿用当时相同的模型适配规则推导实际参数，
            # 不改写历史文件，也不把未发送 thinking 的 GPT 等模型误列为关闭推理。
            effective_mode = summary.get("effective_thinking_mode", llm.effective_thinking_mode(
                summary["model"], summary["thinking_mode"]))
            conditions = {"suite": run["suite"], "options": options, "thinking_mode": effective_mode}
            if summary.get("execution"):
                conditions["execution"] = summary["execution"]
                options.update(max_tokens=None, temperature=None)
            if run.get("retry"):
                conditions["retry"] = {k: copy.deepcopy(run["retry"][k]) for k in ("source_run_id", "count", "executions")}
            # 参数不同也属于同一长期榜，但题目、提示词、评分协议不同仍不能混排。
            # 展示名称和类别文案不参与身份计算，改标题不应丢失已有榜单。
            edition = {k: v for k, v in run["suite"].items() if k not in ("name", "description", "categories")}
            identity = {"suite": edition, "scoring": {"mode": scoring}}
            if scoring == "normalized":
                identity["scoring"]["version"] = run["normalization_version"]
            elif scoring in ("reviewed", "comparison"):
                identity["scoring"].update(version=run["review_version"], normalization=run["normalization_version"])
            key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                            allow_nan=False).encode()).hexdigest()
            completed_at = summary["completed_at"]
            group = cohorts.setdefault(key, {"id": key, "suite": copy.deepcopy(run["suite"]),
                                             "scoring": identity["scoring"],
                                             "latest_at": completed_at, "entries": {}})
            group["latest_at"] = max(group["latest_at"], completed_at)
            pair = (summary["site"], summary["model"])
            existing = group["entries"].get(pair)
            # 文件遍历顺序和批次启动顺序都不代表模型完成顺序；重启后也按同一顺序选择。
            latest = (completed_at, run["started_at"], run["run_id"])
            if existing is not None:
                previous = (existing["completed_at"], existing["started_at"], existing["run_id"])
                if scoring == "comparison":
                    # 已复核成绩继续有效，新的待复核记录不能让正式名次消失。
                    # 尚无最终成绩时允许展示最新原始分，但复核后仍无分数和名次。
                    latest = (bool(summary["after_review"].get("ready")), *latest)
                    previous = (bool(existing["after_review"].get("ready")), *previous)
                if latest <= previous:
                    continue
            group["entries"][pair] = {**copy.deepcopy(summary),
                                      "run_id": run["run_id"], "started_at": run["started_at"],
                                      "finished_at": run["finished_at"], "status": run["status"],
                                      "conditions": copy.deepcopy(conditions), "unranked_reason": ""}
    groups = sorted(cohorts.values(), key=lambda group: (group["latest_at"], group["id"]), reverse=True)
    selected = cohort_id or (groups[0]["id"] if groups else None)
    if selected is not None and selected not in cohorts:
        raise ValueError("找不到这个题库或评分版本，请刷新排行榜")
    entries = list(cohorts[selected]["entries"].values()) if selected else []
    entries.sort(key=lambda row: (row["rank_score"] is None, -(row["rank_score"] or 0), row["site"], row["model"]))
    import iq_scoring
    import iq_review
    return {"cohort_id": selected, "entries": entries, "scoring": scoring,
            "review_version": iq_review.VERSION,
            "normalization_version": iq_scoring.VERSION,
            "cohorts": [{k: v for k, v in group.items() if k != "entries"} for group in groups]}


def cancel(run_id: str) -> dict[str, Any] | None:
    """停止调度并中断在途请求；保留完整结果，不承诺撤销上游已产生的费用。

    @param run_id 合法批次 ID
    @return 当前状态；不存在为 None
    """
    _path(run_id)
    with _LOCK:
        run = _RUNS.get(run_id)
    if run:
        with run.lock:
            if run.data["status"] == "running":
                run.stop.set()
                run.data["status"] = "stopping"
                run.save()
    return get(run_id)
