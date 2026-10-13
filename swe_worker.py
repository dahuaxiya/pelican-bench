"""每题专用进程：mini-SWE-agent 生成补丁，SWE-bench 官方测试及解析器验收。

模型只看到 issue 和仓库。参考补丁、隐藏测试、站点密钥留在宿主控制进程。
执行容器无网络、无挂载、无 Docker socket；验收重新创建干净容器。
"""
from __future__ import annotations

import json
import math
import os
from email.utils import parsedate_to_datetime
from pathlib import Path
import subprocess
import sys
import time

import litellm
import httpx
import yaml
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import builtin_config_dir
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.exceptions import TimeExceeded
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel
from minisweagent.models.utils.actions_toolcall import parse_toolcall_actions
from swebench.harness.grading import get_eval_report, get_logs_eval
from swebench.harness.constants import MAP_REPO_TO_EXT, MAP_REPO_VERSION_TO_SPECS
from swebench.harness.test_spec.create_scripts import make_eval_script_list
from swebench.harness.test_spec.test_spec import TestSpec

import llm
import swe
import codex_channel


def progress(work: Path, stage: str, detail: str, calls: int = 0, request: dict | None = None) -> None:
    """原子保存公开进度，仅接收执行器固定文案，不能写配置或模型请求。

    @param work 本题私有工作目录
    @param stage 环境、Agent 或验收阶段
    @param detail 固定进度文案
    @param calls 已开始的模型调用数
    @param request 当前物理请求的时间与次数，不能包含回复正文、命令或配置；其他阶段省略
    """
    target = work / "progress.tmp"
    data = {"stage": stage, "detail": detail, "model_calls": calls, "updated_at": time.time()}
    if request is not None:
        data["request"] = request
    target.write_text(json.dumps(data, ensure_ascii=False))
    target.replace(work / "progress.json")


class ProgressAgent(DefaultAgent):
    """在官方 Agent 扩展点记录步骤，不改变提示词或任务判分。"""

    def __init__(self, model, env, work: Path, **kwargs):
        """绑定本题进度目录，不把私有模型配置序列化到磁盘。

        @param model 官方模型适配器
        @param env 隔离的执行容器
        @param work 本题进度目录
        @param kwargs 官方 Agent 预算及模板
        """
        super().__init__(model, env, **kwargs)
        self.work = work

    def query(self) -> dict:
        """开始一次模型请求时显示步骤；请求中等待时间由页面持续计算。

        @return 官方 Agent 解析后的回复，限制及格式异常仍由官方循环处理
        """
        progress(self.work, "agent", f"等待模型回复 · 第 {self.n_calls + 1}/{self.config.step_limit} 步", self.n_calls + 1)
        return super().query()

    def execute_actions(self, message: dict) -> list[dict]:
        """显示命令执行阶段，命令正文不进入公开进度。

        @param message 官方解析器产生的模型回复
        @return 官方格式的命令观察结果
        """
        progress(self.work, "agent", f"执行仓库命令 · 第 {self.n_calls}/{self.config.step_limit} 步", self.n_calls)
        return super().execute_actions(message)


def model_kwargs(site: dict, selected: dict, options: dict) -> dict:
    """使用批次记录的传输方式和站点默认采样参数，保留旧批次的请求行为。

    所有模型统一省略 temperature；runner v2 与旧固定温度成绩分组，不能混排。
    @param site 私有站点连接参数，只供宿主模型调用
    @param selected 模型名称及推理开关
    @param options 已校验的预算、stream 和 action_mode；旧批次为文本命令、180 秒、非流式
    @return 不可持久化的模型请求参数，包含凭据
    """
    # 此值只控制客户端等待，无法延长上游网关的 Proxy Read Timeout；API 错误仍直接保存。
    kwargs = {"api_base": llm._validate_base_url(site["base_url"]), "api_key": site.get("api_key") or "unused",
            "max_tokens": options["max_tokens"], "timeout": options.get("request_timeout", 180), "num_retries": 0,
            "stream": options.get("stream", False),
            "extra_body": llm.thinking_parameters(selected["model"], selected["thinking_mode"])}
    if options.get("action_mode", "text") == "tool_calls":
        # 官方工具模板允许同轮独立命令；执行器仍按返回顺序在隔离容器中执行。
        kwargs["parallel_tool_calls"] = True
    return kwargs


class CodexTextModel(LitellmTextbasedModel):
    """官方账号渠道只生成下一步命令，由 mini-SWE-agent 在无网络容器内执行。"""

    abort_exceptions = [Exception]

    def __init__(self, *, request_timeout=600, channel=None, **kwargs):
        """绑定官方渠道和单步总时限；CLI 只回文本，命令由隔离容器执行。"""
        super().__init__(**kwargs)
        self.channel = channel or codex_channel
        self.request_timeout = request_timeout
        self.request_deadline = None
        self.request_retries = 0
        self.request_attempts = 0
        self.retry_events = []
        self.on_stream_progress = None

    def _query(self, messages, **kwargs):
        """只发送 issue 和公开执行历史；参考补丁与隐藏测试不进入 CLI 上下文。"""
        remaining = self.request_timeout
        if self.request_deadline is not None:
            remaining = min(remaining, self.request_deadline - time.monotonic())
        if remaining <= 0:
            raise TimeExceeded("工程题目时间预算已耗尽")
        self.request_attempts += 1
        if self.on_stream_progress:
            self.on_stream_progress({"attempt": self.request_attempts, "started_at": time.time(), "received_at": None})
        # CLI 没有任意 messages API；显式封装 role/content，完整历史随每步输入并进入独立条件组。
        # 它本身不执行命令，避免从题目容器逃到承载登录态的宿主机。
        transcript = [{"role": m["role"], "content": m.get("content", "")} for m in messages]
        prompt = ("Continue the engineering task described by this conversation. Follow its system instructions "
                  "and return only the next assistant response, including exactly one mswea_bash_command block. "
                  "Do not execute tools yourself.\n" + json.dumps(transcript, ensure_ascii=False))
        try:
            response = self.channel.complete(self.config.model_name.removeprefix("openai/"), prompt, remaining, isolated=False)
        except llm.LLMError as exc:
            # 已压缩到整题余额的等待耗尽属于解题超时；普通网络失败仍然属于接口失败。
            if self.request_deadline is not None and time.monotonic() >= self.request_deadline and "总时限" in str(exc):
                raise TimeExceeded("工程题目时间预算已耗尽") from exc
            raise
        if response.get("error"):
            raise llm.LLMError(response["error"])
        return litellm.ModelResponse(model=self.config.model_name,
            choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": response["text"]}}],
            usage={"prompt_tokens": response["usage"].get("prompt_tokens") or 0,
                   "completion_tokens": response["usage"].get("completion_tokens") or 0})

    def _calculate_cost(self, response):
        """mini 的内部计数需要数字；明确标记订阅用量未知，不能对外冒充零费用。"""
        return {"cost": 0.0, "cost_known": False,
                "billing": "chatgpt-plan" if self.channel is codex_channel else "kimi-code-plan"}


class IncompleteModelResponse(RuntimeError):
    """保留被拒绝的流式回复证据；输出达到预算上限与网络失败分开记录。"""

    def __init__(self, reason: str | None, diagnostics: dict):
        """错误不携带客户端配置，诊断正文只供展示，绝不能交给命令执行器。

        @param reason 上游实发的结束原因；None 表示没有收到结束状态
        @param diagnostics 按字段白名单提取的回复、用量及本次输出预算
        """
        super().__init__("模型流未完整结束，未执行本次命令；finish_reason=" + str(reason))
        self.reason = reason
        self.diagnostics = diagnostics


class CompleteStreamMixin:
    """文本和工具适配器共用完整流检查，断流时不执行任何部分命令。"""

    # 第三方的隐式重试关闭；只允许这里按已保存的预算重试同一步，并记录实际请求次数。
    abort_exceptions = [Exception]
    completed_reasons = {"stop"}

    def __init__(self, *, request_retries=0, request_deadline=None, on_retry=None, on_stream_progress=None,
                 output_limit_policy="abort", **kwargs):
        """绑定有限重试策略；这些控制参数不得作为模型 API 参数发送。

        @param request_retries 同一步允许的额外请求次数；旧批次为 0
        @param request_deadline 整题的 monotonic 截止时间；重试等待不得延长它
        @param on_retry 可选进度回调，接收公开事件和是否仍在退避等待
        @param on_stream_progress 可选回调，只接收物理请求次数及开始、收流时间，不接收正文
        @param output_limit_policy abort 保留旧批次行为；agent_feedback 允许官方 Agent 提示精简回复
        @param kwargs 官方模型配置，私有凭据只保留在内存
        """
        super().__init__(**kwargs)
        self.request_retries = request_retries
        self.request_deadline = request_deadline
        self.on_retry = on_retry
        self.on_stream_progress = on_stream_progress
        self.output_limit_policy = output_limit_policy
        self.request_attempts = 0
        self.retry_events = []

    def _remaining_time(self) -> float | None:
        """读取整题剩余时间；耗尽时用官方退出状态，不伪装成上游接口故障。

        @return 剩余秒数；未设置整题截止时间时为 None。耗尽时中断当前 Agent 流程
        """
        if self.request_deadline is None:
            return None
        remaining = self.request_deadline - time.monotonic()
        if remaining <= 0:
            # 官方 Agent 在请求之间检查预算；请求内部因剩余时间缩短而超时，
            # 也必须走同一个 TimeExceeded 分支，避免取决于超时发生在哪个调用层。
            raise TimeExceeded({"role": "exit", "content": "TimeExceeded",
                                "extra": {"exit_status": "TimeExceeded", "submission": ""}})
        return remaining

    def _retry_delay(self, exc: Exception, retry_index: int) -> float | None:
        """仅对暂时性传输故障和提前 EOF 退避，鉴权、配额、过滤及格式错误不重试。

        @param exc 本次请求异常，不能把异常中的私有请求配置写入重试事件
        @param retry_index 从 0 开始的额外请求序号
        @return 至少 30 秒的等待时间；不应重试时为 None
        """
        status = getattr(exc, "status_code", None)
        headers, current, visited = {}, exc, set()
        # LiteLLM 会重建异常，外层 body/response 可能为空；原始配额错误在异常链中，
        # Retry-After 则可能放在 litellm_response_headers。只读取所需字段，不导出异常配置。
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            body = getattr(current, "body", None)
            error = body.get("error", body) if isinstance(body, dict) else {}
            if isinstance(error, dict) and error.get("code") in ("insufficient_quota", "billing_hard_limit_reached"):
                return None
            response = getattr(current, "response", None)
            candidate = getattr(current, "litellm_response_headers", None) or getattr(response, "headers", {})
            if not headers and (candidate.get("retry-after") is not None or candidate.get("retry-after-ms") is not None):
                headers = candidate
            current = current.__cause__ or current.__context__
        transient = isinstance(exc, (litellm.Timeout, litellm.APIConnectionError,
            httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
        # 网关可能以正常 HTTP EOF 结束一个空流或部分回复，SDK 不一定抛网络异常。
        # 缺少实发 finish_reason 仍属未完成传输：丢弃本次全部命令，按同一预算重发。
        # length、content_filter 等明确结束原因不能按 EOF 重试，避免绕过输出或过滤限制。
        transient = transient or (isinstance(exc, IncompleteModelResponse) and exc.reason is None)
        if status not in (408, 429, 500, 502, 503, 504, 524) and not transient:
            return None
        delay = 30.0 * (2 ** retry_index)
        value = headers.get("retry-after")
        try:
            if value is not None:
                try:
                    requested = float(value)
                except ValueError:
                    requested = parsedate_to_datetime(value).timestamp() - time.time()
            else:
                requested = float(headers.get("retry-after-ms", 0)) / 1000
            if math.isfinite(requested):
                delay = max(delay, requested)
        except (TypeError, ValueError, OverflowError):
            pass  # 非法 Retry-After 不改变本地有限退避策略。
        return delay

    def _query(self, messages: list[dict[str, str]], **kwargs):
        """在同一解题步骤内重试暂时失败；成功回复只交给解析器和执行器一次。

        @param messages 原始上下文，每次重试相同，不追加失败请求产生的部分回答
        @param kwargs 官方调用覆盖项；客户端自身重试仍关闭
        @return 唯一成功的完整响应；上游失败保留异常，本地总时间耗尽使用官方 TimeExceeded
        """
        for attempt in range(self.request_retries + 1):
            attempt_kwargs = kwargs
            remaining = self._remaining_time()
            if remaining is not None:
                # 重试沿用整题截止时间，不能重新获得完整的单次请求等待预算。
                timeout = kwargs.get("timeout", self.config.model_kwargs.get("timeout", remaining))
                attempt_kwargs = kwargs | {"timeout": min(timeout, remaining)}
            self.request_attempts += 1
            try:
                return self._query_once(messages, **attempt_kwargs)
            except Exception as exc:
                # 收流中 HTTPX 超时会被 LiteLLM 包成 MidStreamFallbackError，必须查原始原因。
                # 只将真实超时且整题期限已到归为预算结束；429/503 或余额不足以再退避
                # 仍保留接口失败，不能当成有效零分。异常链防环，避免第三方包装形成死循环。
                current, visited = exc, set()
                while current is not None and id(current) not in visited:
                    visited.add(id(current))
                    if isinstance(current, (litellm.Timeout, httpx.TimeoutException)):
                        self._remaining_time()
                        break
                    current = current.__cause__ or current.__context__
                delay = self._retry_delay(exc, attempt)
                if attempt >= self.request_retries or delay is None:
                    raise
                if self.request_deadline is not None and time.monotonic() + delay >= self.request_deadline:
                    raise  # 不能为了重试额外扩充已经记录的整题预算。
                event = {"request_attempt": self.request_attempts, "retry": attempt + 1,
                         "delay_seconds": delay, "error_type": type(exc).__name__,
                         "status_code": getattr(exc, "status_code", None)}
                self.retry_events.append(event)
                if self.on_retry:
                    self.on_retry(event, True)
                # 此时本次回复尚未交给 Agent，没有任何新命令被执行；不重跑以前成功的步骤。
                time.sleep(delay)
                if self.request_deadline is not None and time.monotonic() >= self.request_deadline:
                    raise
                if self.on_retry:
                    self.on_retry(event, False)

    def _query_once(self, messages: list[dict[str, str]], **kwargs):
        """沿用 LiteLLM 请求与拼接，拒绝断流；完整的 length 结束按批次策略处理。

        @param messages 官方 Agent 准备好的完整上下文，不在此处改写
        @param kwargs 单次请求覆盖项；流式时固定收集 usage，不接受自动拼接
        @return 期限内收完的 ModelResponse；失败或超时不返回部分命令，持续收流不会延长整题预算
        """
        options = self.config.model_kwargs | kwargs
        if not options.get("stream", False):
            return super()._query(messages, **kwargs)
        # 每次重试都有独立的请求计时；进度不得把上次断流的数据伪装成本次仍在接收。
        request_started_at = time.time()
        if self.on_stream_progress:
            self.on_stream_progress({"attempt": self.request_attempts,
                "started_at": request_started_at, "received_at": None})
        stream = super()._query(messages, **(kwargs | {
            "stream_options": {"include_usage": True}, "complete_response": False}))
        try:
            # 不边收边执行，也不在首个 finish chunk 处退出：其后可能还有 usage 或错误。
            chunks = []
            last_reported = None
            for chunk in stream:
                # HTTP 读取超时会随新数据重新计时；持续输出可能永远不触发它。
                # 每段都检查固定的整题期限，超时直接走官方 TimeExceeded 并关闭流，
                # 即使命令 JSON 已完整也不能继续拼接、执行或请求重试。
                self._remaining_time()
                chunks.append(chunk)
                now = time.monotonic()
                # 首段立即显示，此后最多每五秒写一次，避免每个 token 都写盘。
                # 只暴露收流时间；正文和工具参数继续留在内存，完整校验后才能执行。
                if self.on_stream_progress and (last_reported is None or now - last_reported >= 5):
                    self.on_stream_progress({"attempt": self.request_attempts,
                        "started_at": request_started_at, "received_at": time.time()})
                    last_reported = now
            # [DONE] 和 EOF 可能被 SDK 消费而不产生 chunk；最后一次等待同样计入预算。
            self._remaining_time()
            # 固定依赖 LiteLLM 1.102.0 的 OpenAI 适配器。它在 EOF 时会合成 stop，
            # received_finish_reason 才是上游实发的结束原因；升级依赖必须跑断流回归。
            # 缺少此属性也拒绝执行，不能回退使用可能是合成值的 chunk.finish_reason。
            reason = getattr(stream, "received_finish_reason", None)
            # length 是上游明确结束本次生成，不等于没有收到结束状态。
            # 新策略只允许它进入格式纠错；解析器仍必须拒绝其中的全部命令。
            feedback = reason == "length" and self.output_limit_policy == "agent_feedback"
            if reason not in self.completed_reasons and not feedback:
                diagnostics = {"finish_reason": reason, "max_tokens": options.get("max_tokens")}
                try:
                    # 只为排查保留部分回复。不得为了生成诊断而补全参数、执行命令或重发请求。
                    partial = litellm.stream_chunk_builder(chunks, messages=messages)
                    if partial:
                        diagnostics["choices"] = [{"message": public_message(choice.message.model_dump())}
                                                  for choice in partial.choices]
                        usage = partial.usage.model_dump() if partial.usage else {}
                        diagnostics["usage"] = {key: usage[key] for key in
                            ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage}
                except Exception as exc:
                    # 损坏的流可能连诊断也无法拼接，仍保留真实结束原因，不能被二次异常覆盖。
                    diagnostics["capture_error"] = type(exc).__name__
                raise IncompleteModelResponse(reason, diagnostics)
            response = litellm.stream_chunk_builder(chunks, messages=messages)
            if not response or not response.choices:
                raise RuntimeError("模型流没有可解析的回复，未执行本次命令")
            if feedback:
                response.choices[0].finish_reason = "length"
            return response
        finally:
            # LiteLLM 的同步 wrapper 没有 close；关闭其底层 OpenAI Stream，
            # 断流、解析失败和正常结束都释放连接，不等垃圾回收。
            close = getattr(getattr(stream, "completion_stream", None), "close", None)
            if close is not None:
                close()


class CompleteStreamModel(CompleteStreamMixin, LitellmTextbasedModel):
    """历史批次使用原文本命令解析器，不能把旧成绩解释成工具调用。"""


class CompleteToolStreamModel(CompleteStreamMixin, LitellmModel):
    """新批次使用官方 bash 工具解析和 tool_call_id 回传，不从正文提取命令。"""

    completed_reasons = {"stop", "tool_calls"}

    def _parse_actions(self, response) -> list[dict]:
        """截断回复只触发官方格式纠错，正常回复沿用官方工具解析器。

        @param response 已收完流且记录真实结束原因的回复；length 中的命令一律不执行
        @return 正常回复的可执行动作；截断时由官方解析器抛出 FormatError
        """
        if self.output_limit_policy == "agent_feedback" and response.choices[0].finish_reason == "length":
            # 即使部分工具参数恰好是合法 JSON，也不能据此执行截断的回复。
            # 传空动作列表让官方模板生成精简提示；Agent 会保留证据，并将下一次调用
            # 计入原步数和时间预算，连续三次格式错误由官方循环终止。
            return parse_toolcall_actions([], format_error_template=self.config.format_error_template,
                                          template_kwargs={"finish_reason": "length"})
        return super()._parse_actions(response)


def public_message(message: dict) -> dict:
    """仅导出协议正文和工具调用，不复制未知扩展或私有配置。

    @param message 官方适配器生成的消息或原始响应中的 message
    @return 可公开的消息证据；模型文本保留原样，入口统一做密钥脱敏
    """
    output = {key: message[key] for key in ("role", "content", "tool_call_id") if key in message}
    if message.get("tool_calls"):
        output["tool_calls"] = [{"id": call.get("id"), "type": call.get("type"),
            "function": {key: call.get("function", {}).get(key) for key in ("name", "arguments")}}
            for call in message["tool_calls"]]
    return output


def public_trajectory(messages: list[dict]) -> list[dict]:
    """保留格式错误的原始回复、工具参数和执行结果，禁止序列化 Agent 配置。

    @param messages 官方 Agent 的完整消息序列，可能含格式错误诊断
    @return 通过字段白名单整理的执行记录，不改写后续实际发送的上下文
    """
    output = []
    for message in messages:
        item = public_message(message)
        extra = message.get("extra", {})
        diagnostics = {key: extra[key] for key in ("interrupt_type", "exit_status", "returncode",
            "exception_info", "timestamp", "model_response") if key in extra}
        response = extra.get("response")
        if isinstance(response, dict):
            # FormatError 的原始回复只存在 extra.response；过去仅存 content 丢掉了原因。
            # 不整体复制 response/extra，避免第三方扩展把请求配置带入公开记录。
            diagnostics["response"] = {"choices": [{"finish_reason": choice.get("finish_reason"),
                "message": public_message(choice.get("message") or {})}
                for choice in response.get("choices", [])]}
        if diagnostics:
            item["diagnostics"] = diagnostics
        output.append(item)
    return output


def submission_failure(info: dict, messages: list[dict]) -> str:
    """按真实退出原因解释未提交，不能把格式失败一律写成预算耗尽。

    @param info 官方 Agent 的退出结果
    @param messages 官方消息，用于提取最后一次格式错误
    @return 页面可读的失败原因，原始证据仍保留在执行记录中
    """
    status = info.get("exit_status", "unknown")
    if status == "RepeatedFormatError":
        last = next((msg.get("content", "") for msg in reversed(messages)
                     if msg.get("extra", {}).get("interrupt_type") == "FormatError"), "")
        return "模型连续返回无法执行的命令格式，Agent 已停止。最后一次错误：" + last
    return {"LimitsExceeded": "达到模型调用步数上限，未提交补丁",
            "TimeExceeded": "达到模型解题时间上限，未提交补丁",
            "Submitted": "模型提交了空补丁"}.get(status, "Agent 未提交补丁；退出原因：" + status)


class VerificationError(RuntimeError):
    """验收没有形成可信结果；保留诊断日志，但不当作模型答错。"""

    def __init__(self, message: str, log: str):
        """保留有界诊断输出，供页面解释环境故障。

        @param message 错误阶段和原因
        @param log 官方测试进程的输出，最多保存末尾 60000 字符
        """
        super().__init__(message)
        self.log = log[-60000:]


class ImagePreparationError(RuntimeError):
    """镜像准备失败；保留 Docker 原始诊断，不把环境错误记为模型答错。"""

    def __init__(self, message: str, log: str):
        """保存有界日志，让页面和导出都能解释真正的 Docker 故障。

        @param message 页面摘要，包含最后一次失败的具体原因
        @param log 镜像检查和各次拉取诊断，最多保留末尾 16000 字符
        """
        super().__init__(message)
        self.log = log[-16000:]


def docker(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """执行固定 Docker argv，模型命令不经过宿主 shell。

    @param args Docker 参数数组
    @param timeout 整个命令的截止秒数
    @return 捕获的输出，非零退出由调用方按阶段分类
    """
    return subprocess.run([swe.DOCKER, "--context", swe.CONTEXT, *args], capture_output=True,
                          text=True, timeout=timeout)


def prepare_image(reference: str, work: Path) -> str:
    """优先复用官方镜像缓存；只对短暂拉取故障重试，返回不可变 digest。

    @param reference 执行器从官方 instance_id 构造的完整镜像名，不接受模型输出
    @param work 本题进度目录；镜像准备不调用模型、不占用模型请求重试次数
    @return Docker 保存的镜像 digest；检查、拉取或解析失败抛 ImagePreparationError
    """
    progress(work, "environment", "检查任务镜像")
    inspect = docker(["image", "inspect", reference, "--format", "{{json .RepoDigests}}"])
    logs = []
    if inspect.returncode:
        detail = (inspect.stderr or inspect.stdout or "Docker 未返回诊断").strip()
        logs.append("镜像检查：" + detail[-4000:])
        # Docker 不可用或无权访问 socket 时，拉取不会解决问题，不能误报成网络失败。
        if "no such image" not in detail.lower():
            raise ImagePreparationError("任务镜像检查失败：" + detail[-1500:], "\n".join(logs))
        for attempt in range(1, 4):
            progress(work, "environment", f"下载任务镜像 · 第 {attempt}/3 次")
            try:
                pull = docker(["pull", "--platform", "linux/amd64", reference], timeout=1200)
                if pull.returncode == 0:
                    break
                detail = (pull.stderr or pull.stdout or "Docker 未返回诊断").strip()
                # 鉴权、镜像不存在、磁盘不足等持续错误不能靠反复拉取修复。
                transient = any(marker in detail.lower() for marker in (
                    "i/o timeout", "tls handshake timeout", "connection reset", "connection refused",
                    "unexpected eof", "temporary failure", "service unavailable", "bad gateway",
                    "gateway timeout", "context deadline exceeded"))
            except subprocess.TimeoutExpired:
                detail, transient = "Docker pull 超过 1200 秒仍未结束", True
            logs.append(f"第 {attempt} 次拉取：{detail[-4000:]}")
            if not transient or attempt == 3:
                raise ImagePreparationError("任务镜像拉取失败：" + detail[-1500:], "\n".join(logs))
            delay = 2 ** attempt
            progress(work, "environment", f"镜像下载暂时失败，{delay} 秒后重试 · 第 {attempt}/3 次")
            # 只重试环境准备；停止仍由宿主终止本题 worker，不等待退避或整次下载结束。
            time.sleep(delay)
        inspect = docker(["image", "inspect", reference, "--format", "{{json .RepoDigests}}"])
    if inspect.returncode:
        detail = (inspect.stderr or inspect.stdout or "Docker 未返回诊断").strip()
        raise ImagePreparationError("拉取后镜像检查失败：" + detail[-1500:], "\n".join([*logs, detail]))
    try:
        digests = json.loads(inspect.stdout)
        if not isinstance(digests, list) or not digests or not isinstance(digests[0], str) or not digests[0]:
            raise ValueError("empty digest")
    except (ValueError, TypeError) as exc:
        raise ImagePreparationError("镜像缺少有效 digest，不能生成可复现成绩", "\n".join(logs)) from exc
    return digests[0]


class IsolatedEnvironment(DockerEnvironment):
    """沿用官方 Docker 环境协议，使用同步清理避免遗留模型进程。"""

    def cleanup(self) -> None:
        """仅删除本实例创建的容器；不能全局清理其他项目的容器或镜像。"""
        if getattr(self, "container_id", None):
            identifier, self.container_id = self.container_id, None
            try:
                docker(["rm", "-f", identifier], timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass  # 主流程结束后仍按任务 label 做一次精确清理。


def new_environment(image: str, label: str) -> IsolatedEnvironment:
    """按固定隔离策略启动仓库，策略变更必须升级榜单 runner 版本。

    @param image 已拉取并固定的镜像 digest
    @param label 仅用于精确回收本题容器
    @return 可供官方 Agent 使用的环境
    """
    return IsolatedEnvironment(image=image, cwd="/testbed", executable=swe.DOCKER,
        interpreter=["bash", "-c"], timeout=90, pull_timeout=120,
        env={"BASH_ENV": "/root/.bashrc", "PAGER": "cat", "PIP_PROGRESS_BAR": "off"},
        forward_env=[], container_timeout="3h", run_args=["--rm", "--platform", "linux/amd64",
            "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "512", "--memory", "5g", "--cpus", "3", "--label", "pelican-swe=" + label])


def test_spec(task: dict) -> TestSpec:
    """用官方验收脚本构造只用于判分的规范，不重复构建预制镜像。

    官方 make_test_spec 会为构建镜像联网取历史 requirements；这里使用已经准备好的
    官方镜像，仅调用相同的 make_eval_script_list，避免判分时再次依赖 GitHub 网络。
    @param task 校验过的官方任务，含固定 base_commit 与 test_patch
    @return 供官方日志解析器使用的 TestSpec
    """
    specs = MAP_REPO_VERSION_TO_SPECS[task["repo"]][task["version"]]
    return TestSpec(instance_id=task["instance_id"], repo=task["repo"], version=task["version"],
        repo_script_list=[], env_script_list=[],
        eval_script_list=make_eval_script_list(task, specs, "testbed", "/testbed", task["base_commit"], task["test_patch"]),
        arch="x86_64", FAIL_TO_PASS=json.loads(task["FAIL_TO_PASS"]), PASS_TO_PASS=json.loads(task["PASS_TO_PASS"]),
        language=MAP_REPO_TO_EXT[task["repo"]], docker_specs=specs.get("docker_specs", {}), namespace=None)


def evaluate(task: dict, patch: str, image: str, work: Path, label: str, name: str) -> dict:
    """在全新镜像中应用补丁并运行官方测试，宿主仅解析测试日志。

    @param task 完整官方任务，仅此阶段允许访问 test_patch
    @param patch 模型补丁、参考补丁或空基线
    @param image 固定镜像 digest，禁止读取 Agent 修改过的文件系统
    @param work 本题私有目录
    @param label 本题容器归属
    @param name 内部固定阶段名，用于保存日志
    @return resolved、环境状态和官方逐测试结果
    """
    spec = test_spec(task)
    env = new_environment(image, label)
    try:
        # 官方 eval_script 恢复测试文件并应用 gold test patch；不让 Agent 自己判分。
        script = work / (name + "-eval.sh")
        script.write_text(spec.eval_script)
        # docker cp 会保留宿主 UID 和权限；容器已去掉 DAC_OVERRIDE，
        # 0600 文件即使容器内 root 也读不到。外层目录仍为 0700，脚本不含凭据。
        script.chmod(0o644)
        copied = docker(["cp", str(script), env.container_id + ":/tmp/pelican-eval.sh"])
        if copied.returncode:
            raise RuntimeError("无法复制官方验收脚本")
        if patch:
            patch_file = work / (name + ".patch")
            patch_file.write_text(patch)
            patch_file.chmod(0o644)
            if docker(["cp", str(patch_file), env.container_id + ":/tmp/pelican.patch"]).returncode:
                raise RuntimeError("无法复制待验收补丁")
            applied = env.execute({"command": "git apply --verbose /tmp/pelican.patch"})
            if applied["returncode"]:
                return {"resolved": False, "patch_applied": False, "test_log": applied["output"][:40000]}
        test = env.execute({"command": "bash /tmp/pelican-eval.sh"}, timeout=900)
        log = work / (name + "-tests.log")
        log.write_text(test["output"])
        parsed, found = get_logs_eval(spec, str(log))
        if test.get("exception_info") or not found or not parsed:
            raise VerificationError("验收未产生可解析的官方测试结果", test["output"])
        report = get_eval_report(spec, {"instance_id": task["instance_id"], "model_patch": patch}, str(log), True)[task["instance_id"]]
        return {**report, "patch_applied": True, "test_log": test["output"][-60000:]}
    finally:
        env.cleanup()


def run(payload: dict) -> dict:
    """执行一题；先跑空基线和参考修复，环境不可靠时不得花费模型额度。

    @param payload 来自固定 Web worker 的私有 stdin 数据，不能直接作为公开 JSON
    @return 公开成绩；由入口再次去掉凭据后保存
    """
    task, selected, options = payload["task"], payload["selected"], payload["options"]
    work, label = Path(payload["work"]), payload["label"]
    reference = "docker.io/swebench/sweb.eval.x86_64." + task["instance_id"].replace("__", "_1776_").lower() + ":latest"
    image = ""
    started = time.perf_counter()
    result = {"status": "environment_error", "reason": "", "image": "", "patch": "", "test_log": ""}
    phase = "environment"
    try:
        image = prepare_image(reference, work)
        result["image"] = image
        progress(work, phase, "验证空补丁基线")
        baseline = evaluate(task, "", image, work, label, "baseline")
        progress(work, phase, "验证官方参考补丁")
        oracle = evaluate(task, task["patch"], image, work, label, "oracle")
        if baseline["resolved"] or not oracle["resolved"]:
            raise RuntimeError("环境自检失败：空补丁必须失败，官方参考补丁必须通过")
        result["preflight"] = {"baseline_resolved": False, "reference_resolved": True}
        phase = "agent"
        import official_channels
        channel = official_channels.adapter(payload["site"].get("base_url"))
        official = channel is not None
        action_mode = "text" if official else options.get("action_mode", "text")
        # 模板和解析器必须成对选择，不靠格式失败后悄悄换协议，避免同榜条件不一致。
        adapters = {"text": ("swebench_backticks.yaml", CompleteStreamModel),
                    "tool_calls": ("swebench.yaml", CompleteToolStreamModel)}
        template_name, model_class = adapters[action_mode]
        template = yaml.safe_load((builtin_config_dir / "benchmarks" / template_name).read_text())
        site = payload["site"]
        common = {"model_name": "openai/" + selected["model"], "cost_tracking": "ignore_errors",
                  "observation_template": template["model"]["observation_template"],
                  "format_error_template": template["model"]["format_error_template"]}
        if official:
            model = CodexTextModel(**common, request_timeout=options.get("request_timeout", 600), channel=channel)
            result["execution"] = selected.get("execution")
        else:
            model = model_class(**common, model_kwargs=model_kwargs(site, selected, options),
                request_retries=options.get("request_retries", 0),
                output_limit_policy=options.get("output_limit_policy", "abort"))
        # mini 和 LiteLLM 的隐式重试关闭，实际请求由本地有限重试策略统一计数。
        model.abort_exceptions = [Exception]
        env = new_environment(image, label)
        agent = ProgressAgent(model, env, work, **{**template["agent"], "step_limit": options["step_limit"],
            "cost_limit": 0, "wall_time_limit_seconds": options["task_timeout"], "output_path": None})

        def report_retry(event: dict, waiting: bool) -> None:
            """退避与重新请求分别显示，停止仍由宿主终止当前 worker 并清理容器。

            @param event 不含凭据和响应正文的重试摘要
            @param waiting True 表示等待退避，False 表示马上重发相同请求
            """
            detail = (f'上游暂时失败，等待 {event["delay_seconds"]:g} 秒后重试' if waiting else '重新请求模型')
            progress(work, "agent", f'{detail} · 第 {event["retry"]}/{model.request_retries} 次重试 · 实际请求 {model.request_attempts} 次', agent.n_calls)

        model.on_retry = report_retry

        def report_stream(event: dict) -> None:
            """区分等待首段响应和正在收流；不影响请求参数、Agent 步数或验收规则。

            @param event 当前物理请求的公开时间信息，不能包含正文或工具参数
            """
            detail = '正在接收模型回复' if event["received_at"] is not None else '等待模型响应'
            progress(work, "agent", f'{detail} · 第 {agent.n_calls}/{agent.config.step_limit} 步',
                     agent.n_calls, request=event)

        model.on_stream_progress = report_stream
        model.request_deadline = time.monotonic() + options["task_timeout"]
        try:
            info = agent.run(task["problem_statement"])
        finally:
            # 不使用 agent.save：它会把 model_kwargs（含 API key）序列化。
            result["trajectory"] = public_trajectory(agent.messages)
            result["model_calls"] = agent.n_calls
            result["request_attempts"] = model.request_attempts
            result["retry_events"] = model.retry_events
            env.cleanup()
        result["exit_status"] = info.get("exit_status")
        patch = info.get("submission", "")
        if info.get("exit_status") != "Submitted" or not patch.strip():
            result.update(status="unresolved", reason=submission_failure(info, agent.messages))
        else:
            result["patch"] = patch
            phase = "verify"
            progress(work, phase, "在干净容器中运行官方验收测试", agent.n_calls)
            verdict = evaluate(task, patch, image, work, label, "model")
            result.update(status="resolved" if verdict["resolved"] else "unresolved", reason="", **verdict)
    except Exception as exc:
        # 模型请求阶段失败不等于能力不足；验收容器异常也不能混成零分。
        result.update(status="service_error" if phase == "agent" else "environment_error",
                      reason=f"{phase}: {type(exc).__name__}: {exc}")
        if isinstance(exc, IncompleteModelResponse):
            result["response_diagnostics"] = exc.diagnostics
            if exc.reason == "length":
                # 输出预算截断不是接口宕机，也没有可信补丁验收；单独展示，不能混入有效零分。
                result["status"] = "output_limit"
        if isinstance(exc, VerificationError):
            result["test_log"] = exc.log
        if isinstance(exc, ImagePreparationError):
            result["image_pull_log"] = exc.log
    finally:
        progress(work, "cleanup", "保存结果并回收容器", result.get("model_calls", 0))
        result["duration_seconds"] = round(time.perf_counter() - started, 3)
        try:
            containers = docker(["ps", "-aq", "--filter", "label=pelican-swe=" + label]).stdout.split()
            if containers:
                docker(["rm", "-f", *containers])
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def main() -> None:
    """读取单次私有输入，保存去密钥的结果；不将私有配置输出到日志。"""
    os.umask(0o077)
    payload = json.load(sys.stdin)
    result = run(payload)
    text = json.dumps(result, ensure_ascii=False)
    secret = payload["site"].get("api_key")
    if secret:
        text = text.replace(secret, "[redacted]")
    work = Path(payload["work"])
    temporary = work / "result.tmp"
    temporary.write_text(text)
    temporary.replace(work / "result.json")


if __name__ == "__main__":
    main()
