"""各站点的容量检测和长输出测速；独立于能力排行榜，不追加应用层重试。"""
from concurrent.futures import ThreadPoolExecutor
import copy
import math
import json
from pathlib import Path
import threading
import time
import uuid

import codex_channel
import codex_stream
import config
import llm
import official_channels
from performance_archive import ProbeArchive

TIMEOUT = 45
CONCURRENCY = 2
MAX_CONCURRENCY = 200
PROMPT = "Reply with exactly OK. Do not use tools."
PERFORMANCE_PROMPT = (
    "Write a neutral 600-800 word explanation in English of how a computer network "
    "delivers a message, from an application through the network to another application. "
    "Use plain paragraphs. Start the explanation immediately. Do not use tools, "
    "read files, ask questions, or include a preamble about this request."
)
MODES = {
    "capacity": {"prompt": PROMPT, "timeout": TIMEOUT, "concurrency": CONCURRENCY},
    "performance": {"prompt": PERFORMANCE_PROMPT, "timeout": 180, "concurrency": 32},
}


def _nonnegative_number(value):
    """读取上游数值字段；非法值按未知处理，避免展示伪造的速度。

    Args:
        value: 上游 usage 或性能字段。
    Returns:
        非负有限数值，其他情况返回 None。
    """
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return None
    return value


def performance_metrics(response, elapsed_seconds, mode="capacity"):
    """计算端到端吞吐；用量和逐 token 时序不足时不制造纯生成速度。

    Args:
        response: 渠道结果；只有完成且无错误的请求可以产生吞吐。
        elapsed_seconds: 本地总耗时，作为缺失 total_seconds 时的兜底。
        mode: capacity 不计算短回答速度；performance 使用长输出样本。
    Returns:
        不含答案或凭据的指标，缺失用量保留 None。
    """
    usage = response.get("usage") or {}
    performance = response.get("performance") or {}
    total = _nonnegative_number(performance.get("total_seconds"))
    if total is None:
        total = _nonnegative_number(elapsed_seconds)
    metrics = {"total_seconds": round(total, 3) if total is not None else None}
    if not response.get("completed") or response.get("error"):
        # 失败响应可能带部分用量；不能把部分输出作为成功的测速样本。
        return metrics
    response_seconds = _nonnegative_number(performance.get("response_seconds"))
    if response_seconds is None:
        response_seconds = _nonnegative_number(response.get("total"))
    first_message = _nonnegative_number(performance.get("first_message_seconds"))
    output_tokens = _nonnegative_number(usage.get("completion_tokens"))
    input_tokens = _nonnegative_number(usage.get("prompt_tokens"))
    reasoning_tokens = _nonnegative_number(usage.get("reasoning_tokens"))
    cached_tokens = _nonnegative_number(usage.get("cached_tokens"))
    # 普通 API 的缺失用量由公共客户端估算；测速只展示真实用量，不能把估算值当实测。
    if usage.get("estimated"):
        output_tokens = input_tokens = reasoning_tokens = cached_tokens = None
    sample_short = len(response.get("text") or "") < 1000 or (output_tokens is not None and output_tokens < 256)
    rate = None
    # 上游 output_tokens 可能包含推理 token，必须除以完整响应耗时。
    # 短回答和估算用量不出速度；不能用字符数冒充 tokenizer 的计数。
    if (mode == "performance" and not sample_short and not usage.get("estimated")
            and output_tokens is not None and response_seconds is not None
            and response_seconds > 0 and total is not None and response_seconds <= total):
        rate = round(output_tokens / response_seconds, 2)
    ttft = _nonnegative_number(response.get("ttft"))
    if response_seconds is None or (ttft is not None and ttft > response_seconds):
        ttft = None
    metrics.update({"response_seconds": round(response_seconds, 3) if response_seconds is not None else None,
                    "first_message_seconds": round(first_message, 3) if first_message is not None else None,
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "reasoning_tokens": reasoning_tokens, "cached_input_tokens": cached_tokens,
                    "end_to_end_tokens_per_second": rate,
                    "ttft_seconds": ttft,
                    "generation_tokens_per_second": None})
    if mode == "performance":
        metrics["sample_short"] = sample_short
    return metrics


class Probe:
    """调度当前检测并持续归档；所有快照和诊断回写共用锁。"""

    def __init__(self, state_path=None):
        """恢复当前快照并初始化历史归档，不自动重发中断请求。

        Args:
            state_path: 可选的服务端固定结果路径，测试实例默认只使用内存。
        """
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.data = {"status": "idle", "rows": []}
        self.connections = {}
        self.state_path = Path(state_path) if state_path else None
        self.archive = ProbeArchive(self.state_path.parent / "performance-runs" if self.state_path else None)
        if self.state_path and self.state_path.is_file():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                if (isinstance(saved, dict) and isinstance(saved.get("rows"), list)
                        and saved.get("status") in ("idle", "running", "stopping", "cancelled", "completed")):
                    self.data = saved
                    # 进程重启不能继续持有旧连接；已有结果保留，在途请求明确标为中断。
                    if self.busy():
                        self.data.update(status="cancelled", finished_at=time.time())
                        for row in self.data["rows"]:
                            if row.get("status") in ("queued", "running"):
                                row.update(status="cancelled", reason="服务重启，检测已中断")
            except (OSError, ValueError):
                pass

    def restore_history(self):
        """服务成功绑定端口后恢复历史；避免测试导入模块或重复启动时改写在途记录。"""
        with self.lock:
            self.archive.interrupt_incomplete()
            if self.data.get("id"):
                # 归档先于兼容快照落盘；若后者写入失败，旧快照不得把成功结果倒退。
                archived = self.archive.get(self.data["id"])
                if archived:
                    self.data = archived
                self._save()
            # 旧版导出备份是唯一可恢复的历史；按 ID 去重，不覆盖新归档。
            self.archive.import_snapshots([
                *sorted((Path(config.ROOT) / "output").glob("performance-*.json")),
                *sorted((Path(config.ROOT) / "output").glob("capacity-check-*.json")),
                *sorted((Path(config.ROOT) / "output").glob("probe-before-performance-*.json")),
            ])

    def _save(self):
        """持锁更新当前快照及独立批次归档；磁盘错误不改变上游结果，但要告知用户。"""
        if self.data.get("id"):
            self.archive.record(self.data)
            if self.archive.error:
                self.data["storage_error"] = self.archive.error
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError:
            # 持久化是附加能力；磁盘问题不能把上游成功响应改成接口失败。
            self.data["storage_error"] = "当前测试快照保存失败，请检查本地磁盘"

    def busy(self):
        """停止中也视为在途，避免退出账号或重复点击影响未回收的 CLI。"""
        with self.lock:
            return self.data["status"] in ("running", "stopping")

    def snapshot(self):
        """返回独立副本；调用方不能修改后台工作线程使用的状态。"""
        with self.lock:
            return copy.deepcopy(self.data)

    def _catalog(self, name):
        """从已保存站点读取连接和模型目录；网络查询不能占用任务锁。

        Args:
            name: 服务端配置的站点名称，不接受地址或凭据。
        Returns:
            私有连接配置、可用模型列表和官方账号来源；凭据不进入快照。
        """
        if not isinstance(name, str) or not name:
            raise ValueError("请选择已配置的站点")
        channel = next((item for item in official_channels.adapters() if item.SITE == name), None)
        # 目录查询可能等待网络，不持有任务锁，否则状态查询与取消也会被阻塞。
        if channel:
            catalog = channel.catalog()
            if not catalog.get("ready"):
                raise ValueError("请先在设置中登录该官方账号")
            connection = {"name": name, "base_url": channel.BASE_URL}
            models = catalog.get("models", [])
        else:
            connection = next((item for item in config.load()["sites"] if item["name"] == name), None)
            if not connection:
                raise ValueError("请选择已配置的站点")
            catalog = {}
            try:
                models = llm.list_models(connection["base_url"], connection.get("api_key", ""), timeout=30)
            except llm.LLMError:
                # 与现有模型列表接口一致，目录不可用时仍允许明确配置过的模型。
                models = connection.get("models") or []
                if not models:
                    raise ValueError("无法读取站点模型目录，请刷新模型后重试") from None
        models = list(dict.fromkeys(value for value in models if isinstance(value, str) and value))
        return connection, models, catalog.get("auth_source")

    def start(self, model, mode="capacity", site=None, concurrency=None):
        """兼容单站点入口，统一交给批量调度，避免两套任务相互覆盖。

        Args:
            model: 该站点模型名或 all。
            mode: capacity 或 performance，保持固定请求预算。
            site: 已配置站点；省略时使用 OpenAI 官方。
            concurrency: 1 到 200 的整数；省略时使用当前模式的默认并发。
        Returns:
            当前批次快照；重复提交返回在途批次。
        """
        return self.start_batch([{"site": codex_channel.SITE if site is None else site,
                                  "model": model}], mode, concurrency)

    def start_batch(self, targets, mode="capacity", concurrency=None):
        """原子创建跨站点批次；同站点同模型去重，不合并不同站点的同名模型。

        Args:
            targets: 非空列表，每项只允许 site 和 model；model 可为 all。
            mode: capacity 使用短请求；performance 使用长输出测速。
            concurrency: 本轮最大同时请求数，1 到 200；省略时容量检测用 2，测速用 32。
        Returns:
            已验证的批次快照；任一目标非法时不启动任何推理请求。
        """
        with self.lock:
            if self.busy():
                return self.snapshot()
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("请选择容量检测或输出测速")
        if concurrency is None:
            concurrency = MODES[mode]["concurrency"]
        # bool 也是 Python int 的子类，必须排除；在查目录或消费额度前拒绝非法预算。
        if type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ValueError("并发数必须是 1 到 200 的整数")
        if not isinstance(targets, list) or not targets:
            raise ValueError("请至少选择一个站点模型")
        # 先校验所有条目的结构，再查询目录；不允许某项传入新的地址、密钥或提示词。
        for target in targets:
            if (not isinstance(target, dict) or set(target) != {"site", "model"}
                    or any(not isinstance(target[key], str) or not target[key] for key in ("site", "model"))):
                raise ValueError("每个测试项只接受已配置的站点名和模型名")
        catalogs, selected = {}, {}
        for target in targets:
            name, model = target["site"], target["model"]
            if name not in catalogs:
                try:
                    catalogs[name] = self._catalog(name)
                except ValueError as exc:
                    raise ValueError(f"{name}：{exc}") from None
            _, models, _ = catalogs[name]
            if model != "all" and model not in models:
                raise ValueError(f"{name}：请选择该站点目录中的模型")
            if not models:
                raise ValueError(f"{name}：该站点尚无可测试模型")
            for value in models if model == "all" else [model]:
                selected[(name, value)] = {"site": name, "model": value}
        options = MODES[mode]
        with self.lock:
            # 两次目录查询可能同时返回，只有第一个提交者可建立任务。
            if self.busy():
                return self.snapshot()
            self.connections = {name: copy.deepcopy(catalog[0]) for name, catalog in catalogs.items()}
            self.stop = threading.Event()
            single_site = next(iter(catalogs)) if len(catalogs) == 1 else None
            self.data = {"id": uuid.uuid4().hex, "status": "running", "started_at": time.time(),
                         "finished_at": None, "mode": mode, "site": single_site, "sites": list(catalogs),
                         "timeout_seconds": options["timeout"], "concurrency": concurrency,
                         "auth_source": catalogs[single_site][2] if single_site else None,
                         "rows": [{**target, "status": "queued", "events": [],
                                   "capacity_detected": False} for target in selected.values()]}
            self._save()
            threading.Thread(target=self._run, daemon=True, name="model-performance-check").start()
            return self.snapshot()

    def cancel(self):
        """停止排队和在途请求，保留已经观察到的容量信号与成功结果。"""
        with self.lock:
            if self.busy():
                self.data["status"] = "stopping"
                self.stop.set()
            return self.snapshot()

    def _run(self):
        """按本轮并发上限执行；所有请求回收前保持运行锁，取消仍阻止排队项发出。"""
        try:
            # 并发预算属于批次快照；不能使用模式默认值覆盖用户选择或历史记录。
            with ThreadPoolExecutor(max_workers=min(self.data["concurrency"], len(self.data["rows"]))) as pool:
                list(pool.map(self._one, self.data["rows"]))
        except Exception:
            # 调度失败不能留下 running 行，更不能显示为全部请求成功。
            with self.lock:
                for row in self.data["rows"]:
                    if row["status"] in ("queued", "running"):
                        row.update(status="failed", reason="检测调度失败，无法判断容量")
        finally:
            with self.lock:
                self.data.update(status="cancelled" if self.stop.is_set() else "completed",
                                 finished_at=time.time())
                self._save()

    def _one(self, row):
        """执行一次固定预算请求，回写诊断与指标；取消或失败不产生吞吐。

        Args:
            row: 本轮共享的模型状态，只能持锁修改。
        """
        with self.lock:
            if self.stop.is_set():
                row["status"] = "cancelled"
                return
            row.update(status="running", started_at=time.time())
            mode = self.data["mode"]
            options = MODES[mode]
        started = time.monotonic()

        def observe(event):
            """仅接收渠道已脱敏的事件；记录有界列表，容量标志始终保留。"""
            with self.lock:
                row["capacity_detected"] |= "[capacity]" in event["category"]
                row["events"] = (row["events"] + [event])[-40:]

        def observe_output(event):
            """记录首个输出事件的时间，正文和后续增量不进入检测结果。

            Args:
                event: 官方渠道提供的输出时间；只有 text_delta 证明首字已经到达。
            """
            with self.lock:
                row["output_observation"] = event

        try:
            # 同名模型可能来自不同站点，凭据必须按该行站点获取，不能使用全局当前站点。
            connection = self.connections[row["site"]]
            channel = official_channels.adapter(connection["base_url"])
            if channel is codex_channel:
                complete = codex_stream.complete if mode == "performance" else channel.complete
                response = complete(row["model"], options["prompt"], options["timeout"], self.stop,
                                    on_diagnostic=observe, on_output=observe_output)
            else:
                # 复用评测的渠道适配与可终止请求进程。固定提示词，禁用应用层兼容重试，
                # 避免测速混入多次请求；配置和密钥仅从服务端已保存站点中读取。
                response = llm.stream_chat(connection["base_url"], connection.get("api_key", ""),
                    row["model"], options["prompt"], timeout=options["timeout"], max_tokens=4096,
                    temperature=0.7, thinking_mode=connection.get("thinking_mode", "default"),
                    retry_stream_options=False, cancel_event=self.stop)
            with self.lock:
                # completed 证明请求完成；正文无需是 OK，也不保存模型正文或订阅凭据。
                row["status"] = "success" if response.get("completed") and not response.get("error") else "failed"
                row["capacity_detected"] |= response.get("transport_diagnostics", {}).get("capacity_detected", False)
                row["metrics"] = performance_metrics(response, time.monotonic() - started, mode)
                row["transport"] = response.get("transport", "codex-cli") if channel is codex_channel else "kimi-acp" if channel else "api-stream"
                if row["status"] == "failed":
                    row["reason"] = "响应未完整结束或正文无效，未计算速度"
                if response.get("response_model"):
                    row["response_model"] = response["response_model"]
        except llm.LLMCancelled:
            with self.lock:
                row["status"] = "cancelled"
        except llm.LLMError as exc:
            with self.lock:
                # API 异常可能含凭据或上游原文，统一变成固定类别后才能返回浏览器。
                reason = _failure_category(str(exc))
                row.update(status="failed", reason=reason)
                row["capacity_detected"] |= "[capacity]" in reason
                row["metrics"] = {"total_seconds": round(time.monotonic() - started, 3)}
        except Exception:
            with self.lock:
                row.update(status="failed", reason="检测程序异常，无法判断容量")
                row["metrics"] = {"total_seconds": round(time.monotonic() - started, 3)}
        finally:
            with self.lock:
                row.update(duration_seconds=round(time.monotonic() - started, 3), finished_at=time.time())
                row.setdefault("metrics", {"total_seconds": row["duration_seconds"]})
                capacity_events = [e for e in row["events"] if "[capacity]" in e["category"]]
                if row["status"] == "success" and capacity_events:
                    row["recovery_seconds"] = round(max(0, row["duration_seconds"] - capacity_events[0]["elapsed_seconds"]), 3)
                self._save()


def _failure_category(message):
    """保留已识别的诊断类别；其他上游文字仅用于分类，不直接展示。

    Args:
        message: 渠道返回的异常文本，可能包含不应回显的认证材料。
    Returns:
        固定的中文类别；通用 429/503、配额和超时不等同于容量不足。
    """
    for key, title in (("capacity", "所选模型容量不足"), ("quota", "账号额度不足"),
                       ("rate_limit", "请求限流"), ("authentication", "认证失败")):
        if f"[{key}]" in message:
            return f"{title} [{key}]"
    if "超时" in message:
        return "上游或网络超时 [timeout]"
    if "连接失败" in message or "读取响应失败" in message:
        return "网络或流连接异常 [network]"
    return codex_channel._diagnostic(message).replace("未识别的 CLI 错误", "未识别的渠道错误")


manager = Probe(Path(config.ROOT) / ".local" / "performance-probe.json")
