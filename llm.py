"""OpenAI 兼容站点的最小客户端。

只用标准库，不引第三方依赖，方便直接拷到任何有 python3 的机器上跑。

关于目标地址：base_url 由使用者自己在设置页填，本工具的用途就是"把请求打到我
指定的中转站"，所以这里不做 host 白名单——加白名单等于废掉工具。但仍然限制
scheme 只能是 http/https，避免 file:// 之类被 urllib 当本地文件读走。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# urllib 的 timeout 限制每次阻塞读取；可取消请求另外在父进程限制整次调用时长。
_USER_AGENT = "pelican-bench/1.0"


class LLMError(Exception):
    """站点调用失败。消息直接展示给用户，所以要写成人能看懂的原因。"""


class LLMCancelled(LLMError):
    """调用方主动取消；不能作为接口失败、答错或触发兼容性重试。"""


def _cancellable_chat(request: dict[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
    """在可回收的进程中执行网络请求，取消和总时限不受 DNS、TLS 或心跳影响。

    Args:
        request: stream_chat 的参数，不包含 cancel_event；凭据仅经匿名管道传入。
        cancel_event: 批次共享的停止信号，设置后不再等待上游响应。
    Returns:
        完整的客户端结果。取消抛 LLMCancelled，网络失败抛 LLMError。
    """
    if cancel_event.is_set():
        raise LLMCancelled("请求已取消")
    encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
    raw_timeout = request.get("timeout")
    try:
        total_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        raise LLMError("请求总时限必须为正数") from None
    if not math.isfinite(total_timeout) or total_timeout <= 0:
        raise LLMError("请求总时限必须为有限正数")
    # urllib 的 timeout 只限制一次读操作；这里单独维护绝对截止时间，避免上游
    # 用心跳或零碎增量不断刷新读超时，导致父批次永远等不到这个 worker 收尾。
    deadline = time.monotonic() + total_timeout
    # urllib 的阻塞读取和 Python 线程都不能可靠地被 Future.cancel 中断。
    # 每个在途请求独占一个标准库进程，终止它能同时关闭连接，不遗留后台请求线程。
    # 断开本地连接不保证上游停止生成或计费，这仍由站点决定。
    # 不使用 shell，不把地址、密钥、题干放进进程参数或临时文件。
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--request-worker"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    exchanged = threading.Event()
    transport = {"output": b"", "failed": False}

    def exchange() -> None:
        """一次性写完整请求并读完结果；只操作本次 worker 的匿名管道。"""
        try:
            transport["output"], _ = process.communicate(input=encoded)
        except Exception:
            # 管道或本地进程异常属于执行器故障，不把请求体或异常详情回显到日志。
            transport["failed"] = True
        finally:
            exchanged.set()

    # macOS/POSIX 下 communicate(input=None) 的重入不会重新注册 stdin 写事件。
    # 首轮 100ms 超时后若请求超过管道容量，剩余题干因此永远送不到 worker。
    # 独立线程只负责一次完整管道交换；主线程仍能按 100ms 检查取消和总时限。
    # 网络阻塞留在子进程中，终止子进程即可让管道线程结束，不遗留网络线程。
    exchange_thread = threading.Thread(target=exchange, name="llm-request-pipes", daemon=True)
    try:
        exchange_thread.start()
        while True:
            if cancel_event.is_set():
                raise LLMCancelled("请求已取消；上游已产生的费用可能仍会结算")
            if time.monotonic() >= deadline:
                raise LLMError(f"超时（>{raw_timeout}s）")
            wait_for = min(0.1, max(0.001, deadline - time.monotonic()))
            if exchanged.wait(wait_for):
                break
        if process.returncode != 0 or transport["failed"]:
            raise RuntimeError("模型请求进程异常退出")
        result = json.loads(transport["output"])
        if result["kind"] == "service_error":
            raise LLMError(result["message"])
        if result["kind"] != "response":
            raise RuntimeError("模型请求进程内部错误")
        return result["response"]
    finally:
        # 任何退出路径都要回收进程和管道；不能只把界面标为停止而留下网络请求。
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # worker 不派生持有管道的子进程。先终止并回收它，再等管道交换线程收尾，
        # 最后关闭文件对象，避免并发关闭 communicate 正在使用的文件描述符。
        if exchange_thread.ident is not None:
            exchange_thread.join()
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()


def _validate_base_url(base_url: str) -> str:
    """校验并规整 base_url。

    只允许 http/https。scheme 校验放在这里而不是配置层，是因为 base_url 也可能
    从命令行临时传入，统一在出网前这一道关卡上拦住最可靠。

    @param base_url 用户填写的站点地址
    @return 去掉尾部斜杠的地址
    @throws LLMError scheme 不是 http/https，或地址没写主机名
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("站点地址为空")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LLMError(f"站点地址协议不支持：{parsed.scheme or '(空)'}，只允许 http/https")
    if not parsed.netloc:
        raise LLMError("站点地址缺少主机名")
    return url


def _headers(api_key: str) -> dict[str, str]:
    """组装请求头。

    @param api_key 站点 key，允许为空（少数自建站点不校验）
    @return 请求头字典
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def list_models(base_url: str, api_key: str, timeout: int = 30) -> list[str]:
    """拉站点的模型列表。

    @param base_url 站点地址，形如 https://x/v1
    @param api_key 站点 key
    @param timeout 超时秒数
    @return 去重并排序后的模型 id 列表
    @throws LLMError 网络失败、鉴权失败或返回结构不是预期的 {"data": [...]}
    """
    url = _validate_base_url(base_url) + "/models"
    req = urllib.request.Request(url, headers=_headers(api_key), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LLMError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连接失败：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError("返回内容不是合法 JSON，可能地址填的不是 OpenAI 兼容端点") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise LLMError("返回结构里没有 data 数组，可能不是 OpenAI 兼容端点")

    models = {str(item.get("id")).strip() for item in data if isinstance(item, dict) and item.get("id")}
    return sorted(m for m in models if m)


# 单个响应最多收这么多字符。SVG 再复杂也用不到 2MB，设上限是防某些模型陷入
# 重复生成把内存撑爆（跑一批几十个模型时尤其要命）。
_MAX_OUTPUT_CHARS = 2_000_000


def thinking_parameters(model: str, mode: str) -> dict[str, Any]:
    """返回实际发送的推理参数；未适配的模型必须保持上游默认。

    @param model 原始模型名，与请求中 model 一致
    @param mode 站点要求的 default、disabled 或 enabled
    @return 已适配模型的 thinking 参数；空字典表示没有指定推理行为
    """
    if mode in ("disabled", "enabled") and model.lower().startswith(("deepseek-", "glm-", "kimi-")):
        return {"thinking": {"type": mode}}
    return {}


def effective_thinking_mode(model: str, mode: str) -> str:
    """按实际请求参数归组；这不能证明上游是否遵守了请求。

    @param model 请求的模型名
    @param mode 保存的站点推理选项
    @return 实际请求模式，没有参数时为 default
    """
    return mode if thinking_parameters(model, mode) else "default"


def stream_chat(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int = 8000,
    temperature: float = 0.7,
    timeout: int = 180,
    thinking_mode: str = "default",
    retry_stream_options: bool = True,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """流式发一次对话，同时测时延和 token 用量。

    必须用流式：非流式只能拿到总耗时，测不出首 token 时延（TTFT），而 TTFT 是
    区分"模型慢"和"排队/冷启动慢"的关键指标。

    usage 优先取站点返回的真实值（stream_options.include_usage）。部分中转站不认
    这个参数会直接 400，所以这里做一次降级重试：去掉 stream_options 再发一遍，
    拿不到 usage 就按字符数估算并标记 estimated，避免整条用例直接失败。

    @param base_url 站点地址；内置官方登录渠道通过本机 CLI，温度和 token 上限不适用
    @param api_key 站点 key
    @param model 模型 id
    @param prompt 提示词
    @param max_tokens 最大输出 token
    @param temperature 采样温度
    @param timeout 超时秒数
    @param thinking_mode default 沿用站点默认；disabled/enabled 只覆盖 DeepSeek、GLM、Kimi
    @param retry_stream_options 是否允许兼容性补发；评测必须关闭，保证每个样本只请求一次
    @param cancel_event 可选停止信号；提供时使用可终止的请求进程，并按 timeout 限制整次调用
    @return 含正文、时延、用量和 error 的结果；无正文仍保留诊断指标，调用方须检查 error
    """
    import official_channels
    channel = official_channels.adapter(base_url)
    if channel:
        # 任务入口已验证模型并保存条件；桌面客户端会刷新共享模型目录，不能中途据此拒绝已排队的模型。
        return channel.complete(model, prompt, timeout, cancel_event)
    if cancel_event is not None:
        return _cancellable_chat({"base_url": base_url, "api_key": api_key, "model": model, "prompt": prompt,
                                  "max_tokens": max_tokens, "temperature": temperature, "timeout": timeout,
                                  "thinking_mode": thinking_mode, "retry_stream_options": retry_stream_options}, cancel_event)
    url = _validate_base_url(base_url) + "/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # 这些模型使用 thinking.type；GPT/Claude 的推理参数不同，不能向所有模型盲发。
    # 不自动移除被拒绝的 thinking 参数重试，否则可能悄悄恢复推理并再次耗尽额度。
    body.update(thinking_parameters(model, thinking_mode))

    try:
        return _do_stream(url, api_key, body, timeout)
    except LLMError as exc:
        # 只对"参数不被接受"这类 4xx 做一次降级，网络错误重试没意义。
        if retry_stream_options and "stream_options" in str(exc) and ("HTTP 400" in str(exc) or "HTTP 422" in str(exc)):
            body.pop("stream_options", None)
            return _do_stream(url, api_key, body, timeout)
        raise


def _do_stream(url: str, api_key: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    """实际发一次 SSE 请求并解析。

    时间点定义：
    - ttfb：拿到 HTTP 响应头的时刻，反映连接 + 排队；
    - ttft：第一个非空 content 增量到达的时刻，也就是用户体感的"开始出字"；
    - total：流结束的时刻。

    ttft 必须排掉空增量：很多站点第一帧只带 role 或空 content，把它当首 token
    会算出一个明显偏小的假值。

    @param url 完整的 chat/completions 地址
    @param api_key 站点 key
    @param body 请求体
    @param timeout 超时秒数
    @return 结果字典；流正常结束但没有正文时用 error 描述原因，保留已消费的 token
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method="POST")

    started = time.perf_counter()
    chunks = 0
    parts: list[str] = []
    reasoning_chars = 0
    usage: dict[str, Any] = {}
    finish_reason = ""
    completed = False
    response_model = None
    truncated = False
    ttft: float | None = None

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ttfb = time.perf_counter() - started
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                # SSE 的空行是事件分隔，注释行以冒号开头，都直接跳过。
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if payload_text == "[DONE]":
                    completed = True
                    break
                try:
                    event = json.loads(payload_text)
                except json.JSONDecodeError:
                    # 个别站点会在流里插非 JSON 的心跳文本，忽略即可，不该让整次调用失败。
                    continue
                if not isinstance(event, dict):
                    continue
                if isinstance(event.get("model"), str):
                    response_model = event["model"]

                # 有的站点不返 HTTP 错误码，而是在流里塞一个 error 事件，这里必须识别。
                err = event.get("error")
                if isinstance(err, dict):
                    raise LLMError(f"站点返回错误：{err.get('message') or err}")
                if isinstance(err, str) and err:
                    raise LLMError(f"站点返回错误：{err}")

                # usage 通常只在最后一帧出现，且那一帧的 choices 往往是空数组。
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]

                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])

                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue

                # 推理模型的思维链走单独字段，不能混进 SVG 正文，但要记长度，
                # 否则会出现"耗时很长但输出很短"的结果看起来不合理。
                for field in ("reasoning_content", "reasoning"):
                    extra = delta.get(field)
                    if isinstance(extra, str):
                        reasoning_chars += len(extra)

                piece = delta.get("content")
                if not isinstance(piece, str) or not piece:
                    continue
                if ttft is None:
                    ttft = time.perf_counter() - started
                chunks += 1
                parts.append(piece)
                if sum(len(p) for p in parts) > _MAX_OUTPUT_CHARS:
                    # 超过上限就主动收尾，已收到的内容仍然保留供人工判断。
                    truncated = True
                    break
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise LLMError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连接失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"超时（>{timeout}s）") from exc
    except OSError as exc:
        # 读流中途断开走到这里，socket.timeout 在 3.10+ 已经是 TimeoutError 的别名。
        raise LLMError(f"读取响应失败：{exc}") from exc

    total = time.perf_counter() - started
    text = "".join(parts)
    normalised_usage = _normalise_usage(
        usage, prompt_chars=_prompt_chars(body), output_chars=len(text) + reasoning_chars,
    )
    error = ""
    # 无正文也可能已经消费完整的推理额度；不能抛成普通连接失败再丢弃 usage。
    if not text.strip():
        if finish_reason == "length":
            error = "输出 token 上限已耗尽，未生成正文；可关闭推理或提高 max_tokens"
        elif reasoning_chars:
            error = "站点只返回了推理内容，未生成正文"
        else:
            error = "站点返回了空内容"
        error += (f"（finish_reason={finish_reason or '未提供'}，"
                  f"推理字符={reasoning_chars}，推理 token={normalised_usage['reasoning_tokens']}，"
                  f"输出 token={normalised_usage['completion_tokens']}）")
    elif finish_reason == "length" or truncated:
        # 清洗器可能把截断 SVG 修成一个合法的空壳；额度或本地上限截断必须仍算失败。
        error = f"输出被截断（finish_reason={finish_reason or '本地长度上限'}），请提高 token 上限或简化提示词"

    return {
        "text": text,
        "error": error,
        "ttfb": ttfb,
        # 推理事件不算首个正文 token；没有正文时不伪造 TTFT。
        "ttft": ttft,
        "total": total,
        "chunks": chunks,
        "reasoning_chars": reasoning_chars,
        "finish_reason": finish_reason,
        "truncated": truncated or finish_reason == "length",
        # 无结束标记的 EOF 不能被客观评测误当作完整响应，即使正文恰好是合法 JSON。
        "completed": completed or bool(finish_reason),
        "response_model": response_model,
        "usage": normalised_usage,
    }


def _prompt_chars(body: dict[str, Any]) -> int:
    """统计请求里提示词的字符数，供 usage 估算兜底用。

    @param body 请求体
    @return 所有 message content 的字符数之和
    """
    total = 0
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            total += len(msg["content"])
    return total


def _normalise_usage(usage: dict[str, Any], *, prompt_chars: int, output_chars: int) -> dict[str, Any]:
    """规整 token 用量，站点没给就按字符数粗估。

    估算系数取 4 字符≈1 token，这是英文的经验值。这里只为了在站点不返 usage 时
    还能给出一个量级参考，所以结果必须带 estimated 标记，报告里要显式提示"是估的"，
    不能让用户拿估算值当真实账单对。

    @param usage 站点返回的 usage，可能为空
    @param prompt_chars 提示词字符数
    @param output_chars 正文和推理的总字符数，仅在站点缺失用量时估算
    @return 含输入、输出、推理 token 和 estimated 的字典；未报告的推理 token 为 None
    """
    def _as_int(value: Any) -> int | None:
        """读取可选 token 数，无法转换时返回 None。

        @param value 站点返回的 token 值，可为空
        @return 整数或 None，调用方不能把缺失值当成真实的零
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    details = usage.get("completion_tokens_details")
    reasoning_tokens = _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else None
    if prompt_tokens is not None and completion_tokens is not None:
        total = _as_int(usage.get("total_tokens"))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total if total is not None else prompt_tokens + completion_tokens,
            "estimated": False,
            "reasoning_tokens": reasoning_tokens,
        }

    est_prompt = max(1, round(prompt_chars / 4))
    est_completion = max(1, round(output_chars / 4))
    return {
        "prompt_tokens": est_prompt,
        "completion_tokens": est_completion,
        "total_tokens": est_prompt + est_completion,
        "estimated": True,
        "reasoning_tokens": reasoning_tokens,
    }


def _request_worker() -> None:
    """读取一次父进程管道请求并返回 JSON；只供本机固定启动命令使用。

    不在子进程评分或写成绩，确保取消后只有主进程能更新批次。异常不输出凭据或 traceback。
    """
    try:
        request = json.load(sys.stdin)
        response = stream_chat(**request)
        result = {"kind": "response", "response": response}
    except LLMError as exc:
        result = {"kind": "service_error", "message": str(exc)}
    except Exception:
        result = {"kind": "runner_error"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__" and sys.argv[1:] == ["--request-worker"]:
    _request_worker()
