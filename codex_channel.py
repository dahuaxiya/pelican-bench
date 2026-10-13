"""评测共用的官方 Codex 登录态渠道；认证完全交给 CLI，不读取认证文件。"""
from __future__ import annotations

import json
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import llm

SITE = "OpenAI 官方（Codex 登录态）"
BASE_URL = "codex://official"
POLICY = "codex-bench-v2"
_MODEL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")
_LIMIT = 4 * 1024 * 1024
_CACHE = None
_LOCK = threading.Lock()
_DISABLED = (
    "shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "remote_plugin",
    "hooks", "memories", "chronicle", "multi_agent", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "computer_use", "in_app_browser", "image_generation",
    "view_image", "code_mode", "code_mode_host", "skill_search", "skill_mcp_dependency_install",
    "workspace_dependencies", "goals", "sleep_tool", "unbounded_connection_retries",
)


def binary_path():
    """优先独立 CLI；npm 安装直启其原生文件，保证停止时不会只杀 Node 外壳。"""
    for path in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", shutil.which("codex")):
        if not path:
            continue
        if os.path.isfile(path) and os.access(path, os.X_OK):
            resolved = Path(path).resolve()
            if resolved.name != "codex.js":
                return str(resolved)
            system, machine = platform.system(), platform.machine()
            os_name = {"Darwin": "darwin", "Linux": "linux"}.get(system)
            arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(machine)
            triple = ("aarch64" if arch == "arm64" else "x86_64") + ("-apple-darwin" if system == "Darwin" else "-unknown-linux-musl")
            root = resolved.parent.parent
            for vendor in (root / "node_modules" / "@openai" / f"codex-{os_name}-{arch}" / "vendor", root / "vendor"):
                native = vendor / triple / "bin" / "codex"
                if native.is_file() and os.access(native, os.X_OK):
                    return str(native)
            # 未识别的 npm 布局不能只启动外壳，否则工程任务取消后可能留下计费子进程。
    return None


def site_config():
    """内置站点只存在于运行时，不能通过设置页修改端点、认证或提示词。"""
    return {**catalog(), "base_url": BASE_URL, "api_key": "", "builtin": True,
            "pricing": {}, "thinking_mode": "default", "max_tokens": None, "prompt": ""}


def _environment(home=None):
    """构造隔离环境；home 仅由站点内部传入，未指定时选择已授权的站点账号。"""
    import codex_auth
    allowed = {"HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
               "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    selected = home if home is not None else codex_auth.selected_home()
    if selected is not None:
        env["CODEX_HOME"] = str(selected)
    # macOS GUI 代理不会自动进入 CLI 环境；复用系统已配置的代理，不改系统设置。
    for protocol, address in urllib.request.getproxies().items():
        key = protocol.upper() + "_PROXY"
        if key in allowed and key not in env and key.lower() not in env:
            env[key] = address
    return env


def catalog(refresh=False):
    """读取 CLI 登录状态及非敏感模型缓存；不发起推理，失败时禁用此渠道。"""
    import codex_auth
    global _CACHE
    with _LOCK:
        # 授权期间暂停新官方任务，避免同一批任务一半使用桌面账号、一半使用站点账号。
        if codex_auth.manager.pending:
            return {"name": SITE, "transport": "codex-cli", "models": [], "ready": False,
                    "error": "正在登录 ChatGPT，请先完成或取消授权", "auth_source": "site"}
        if not refresh and _CACHE and time.monotonic() - _CACHE[0] < 30:
            selected = "site" if codex_auth.selected_home() else "local"
            if _CACHE[1].get("auth_source", "local") == selected:
                return dict(_CACHE[1])
        result = {"name": SITE, "transport": "codex-cli", "models": [], "ready": False,
                  "auth_source": "site" if codex_auth.selected_home() else "local",
                  "note": "ChatGPT/Codex 额度 · medium 推理 · API 温度与输出上限不适用"}
        binary = binary_path()
        try:
            if not binary:
                raise ValueError("未安装 Codex CLI")
            version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                     timeout=10, env=_environment(), check=True).stdout.strip()
            if codex_auth.selected_home():
                models = codex_auth.manager.models()
                if not models:
                    raise ValueError("账号已登录，暂无可用模型，请刷新重试")
                result.update(models=models, ready=True, cli_version=version)
                _CACHE = (time.monotonic(), result)
                return dict(result)
            login = subprocess.run([binary, "login", "status"], capture_output=True, text=True,
                                   timeout=10, env=_environment())
            if login.returncode or "Logged in using ChatGPT" not in login.stdout + login.stderr:
                raise ValueError("请在设置中登录 ChatGPT")
            # 这里只读公开模型元数据；auth.json、Cookie、令牌均不进入站点进程。
            home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            source = home / "models_cache.json"
            if source.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("Codex 模型缓存过大")
            cache = json.loads(source.read_text())
            models = sorted(m["slug"] for m in cache["models"]
                            if m.get("visibility") == "list" and _MODEL.fullmatch(m.get("slug", ""))
                            and any(r.get("effort") == "medium" for r in m.get("supported_reasoning_levels", [])))
            if not models:
                raise ValueError("暂无 Codex 模型目录，请先打开已登录的 Codex 客户端刷新")
            result.update(models=models, ready=True, cli_version=version,
                          catalog_updated_at=cache.get("fetched_at"))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            # CLI 原始 stderr 可能包含账号信息，列表接口不回显它。
            result["error"] = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "无法读取 Codex 登录状态或模型目录"
        _CACHE = (time.monotonic(), result)
        return dict(result)


def execution(model):
    """验证目录中的模型并返回真实运行条件，不伪称支持 API 的温度及 token 预算。"""
    info = catalog()
    if not info["ready"]:
        raise ValueError(info["error"])
    if model not in info["models"]:
        raise ValueError("模型不在当前 Codex 官方目录中，请刷新模型列表")
    return {"transport": "codex-cli", "policy": POLICY, "cli_version": info["cli_version"],
            "reasoning_effort": "medium", "temperature": None, "max_tokens": None,
            "timeout_kind": "wall", "billing": "chatgpt-plan"}


def _command(binary, model, directory):
    """构造固定官方提供方的参数，不允许网页传入路径、命令、端点或配置覆盖。"""
    command = [binary, "exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
               "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
               "--color", "never", "--json", "--cd", directory, "--model", model]
    settings = {"model_provider": '"openai"', "forced_login_method": '"chatgpt"',
                "approval_policy": '"never"', "model_reasoning_effort": '"medium"',
                "web_search": '"disabled"', "project_doc_max_bytes": "0",
                "suppress_unstable_features_warning": "true",
                "skills.include_instructions": "false", "history.persistence": '"none"',
                "tools.update_plan.enabled": "false", "tools.experimental_request_user_input.enabled": "false",
                "developer_instructions": json.dumps("Answer the user's question directly. Do not use tools or access files. Follow the requested answer format.")}
    import codex_auth
    if codex_auth.selected_home():
        # 与账号后端使用同一个文件凭据仓库；不依赖用户全局 keyring 配置。
        settings["cli_auth_credentials_store"] = '"file"'
    for key, value in settings.items():
        command.extend(["-c", key + "=" + value])
    for feature in _DISABLED:
        command.extend(["--disable", feature])
    command.extend(["--enable", "skip_host_skill_discovery", "-"])
    return command


def _kill(process, isolated=True):
    """回收本次进程组；macOS 拒绝组信号时回收直属进程，不覆盖原请求错误。

    Args:
        process: 本次调用创建的 Popen，不接受外部进程标识。
        isolated: 仅当 Popen 使用新会话时允许发送进程组信号。
    """
    try:
        if isolated:
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin 上短命进程组退出时可能返回 EPERM 而非 ESRCH。poll 会回收已退出
        # 的直属进程；仍在运行时仅杀本次子进程，不扩大到其他进程组或掩盖解析错误。
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    process.wait()


def _diagnostic(value):
    """把不可信 CLI 诊断归为固定类别；不回显原文、账号、地址或认证材料。"""
    message = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)).lower()
    status_match = re.search(
        r'\b(?:http(?:/\d(?:\.\d)?)?|(?:http[_ ]?)?status(?:[_ ]?code)?)'
        r'[\s\"\x27:=]*(4\d\d|5\d\d)\b', message)
    status = int(status_match[1]) if status_match else None
    # 只保留已知错误码。简单替换 token 无法覆盖 stderr 中所有凭据、路径和响应正文。
    codes = ("insufficient_quota", "usage_limit_reached", "rate_limit_exceeded",
             "invalid_api_key", "token_expired", "refresh_token_reused", "authentication_error",
             "model_not_found", "model_not_available", "unsupported_model", "context_length_exceeded",
             "server_error", "overloaded_error")
    code = next((c for c in codes if re.search(r'\b' + c + r'\b', message)), None)
    if code in ("insufficient_quota", "usage_limit_reached") or "usage limit" in message or "quota" in message:
        category = "账号额度不足 [quota]"
    elif status == 401 or code in ("invalid_api_key", "token_expired", "refresh_token_reused", "authentication_error") or "not logged in" in message:
        category = "认证失败 [authentication]"
    elif re.search(r'\b(?:selected\s+)?model(?:\s+is)?(?:\s+currently)?\s+at\s+capacity\b', message):
        # 只有明确指向模型容量的上游原文才归入此类，不能用 429/503 推断。
        category = "所选模型容量不足 [capacity]"
    elif code in ("model_not_found", "model_not_available", "unsupported_model") or "model is not supported" in message:
        category = "模型不可用 [model]"
    elif status == 403:
        category = "访问被拒绝 [permission]"
    elif status == 429 or code == "rate_limit_exceeded" or "rate limit" in message or "too many requests" in message:
        category = "请求限流 [rate_limit]"
    elif code == "context_length_exceeded" or "context window" in message:
        category = "输入超出上下文限制 [context]"
    elif "certificate" in message or "tls" in message or "ssl" in message:
        category = "TLS/证书错误 [tls]"
    elif "timed out" in message or "timeout" in message or status in (408, 504):
        category = "上游或网络超时 [timeout]"
    elif (status and status >= 500) or code in ("server_error", "overloaded_error"):
        category = "上游服务错误 [server]"
    elif any(s in message for s in ("reconnect", "stream disconnected", "connection", "dns", "error sending request", "websocket")):
        category = "网络或流连接异常 [network]"
    else:
        category = "未识别的 CLI 错误 [unknown]"
    return "，".join([category] + ([f"HTTP {status}"] if status else []) + ([f"code={code}"] if code else []))


def _failure_message(reason, diagnostics, error_events, errors, returncode=None):
    """组合有限诊断；stderr 仅在失败时读取末尾，经过分类后才进入异常和日志。"""
    details = list(diagnostics)
    length = os.fstat(errors.fileno()).st_size
    if length:
        # CLI 日志可能很大；只看最后 64 KiB，且绝不保存或转发这段原始内容。
        # pread 不移动与子进程共享的文件偏移，避免仍在写 stderr 时互相覆盖。
        diagnostic = _diagnostic(os.pread(errors.fileno(), 65536, max(0, length - 65536)).decode("utf-8", errors="replace"))
        if diagnostic not in details and "[unknown]" not in diagnostic:
            details.append("stderr: " + diagnostic)
    suffix = [f"error 事件 {error_events} 次"] if error_events else []
    if returncode is not None:
        suffix.append(f"退出码 {returncode}")
    return "；".join([reason] + details + suffix)


def complete(model, prompt, timeout, cancel_event=None, isolated=True, on_diagnostic=None, on_output=None):
    """执行无工具的官方请求；总时限包含 CLI 内部重试。

    on_diagnostic 和 on_output 是内部的非阻塞回调，只接收已分类的诊断或时间元数据，
    不能接收原始 stderr、错误正文或模型答案。它们用于快速检测容量和性能。
    其余参数保持普通评测语义；isolated 控制是否拥有可安全回收的进程组。

    Args:
        model: 已通过目录验证的模型名。
        prompt: 单次请求正文，通过 stdin 传递。
        timeout: 包含启动与内部重试的总时限，单位秒。
        cancel_event: 可选的取消信号，在途等待也会检查。
        isolated: 是否为本次 CLI 创建独立进程组。
        on_diagnostic: 可选的安全诊断观察者，不能阻塞。
        on_output: 可选的首条完整消息时间观察者，不接收正文。
    Returns:
        答案、用量及观测时间；不从 CLI 消息事件推算首 token 时间。
    """
    if not _MODEL.fullmatch(model):
        raise llm.LLMError("Codex 模型名无效")
    binary = binary_path()
    if not binary:
        raise llm.LLMError("未安装 Codex CLI")
    if cancel_event and cancel_event.is_set():
        raise llm.LLMCancelled("已取消 Codex 请求")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="iq-codex-") as directory, \
            tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as errors:
        # 临时 stdin 文件避免大题干写满管道；不把题目、认证或答案放到进程命令行。
        source.write(prompt.encode("utf-8"))
        source.seek(0)
        process = subprocess.Popen(_command(binary, model, directory), stdin=source,
                                   stdout=subprocess.PIPE, stderr=errors, cwd=directory,
                                   env=_environment(), start_new_session=isolated)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = b""
        size = 0
        texts = []
        usage = None
        finished = False
        turn_started = False
        diagnostics = []
        error_events = 0
        capacity_detected = False
        stderr_size = 0
        first_message_seconds = None
        response_seconds = None

        def report_diagnostic(diagnostic, source):
            """保留安全分类并通知观察者；容量证据不能因后来重试成功而清除。"""
            nonlocal diagnostics, capacity_detected
            capacity_detected |= "[capacity]" in diagnostic
            if diagnostic not in diagnostics:
                diagnostics = (diagnostics + [diagnostic])[-16:]
            if on_diagnostic is not None:
                on_diagnostic({"category": diagnostic, "source": source,
                               "elapsed_seconds": round(time.monotonic() - started, 3)})

        def check_capacity_stderr():
            """补采只出现在 stderr 的容量错误；不持久化正文，也不把泛化日志算成错误。"""
            nonlocal stderr_size
            length = os.fstat(errors.fileno()).st_size
            if length != stderr_size and not capacity_detected:
                stderr_size = length
                tail = os.pread(errors.fileno(), 65536, max(0, length - 65536)).decode("utf-8", errors="replace")
                # 分行判定，避免另一条历史日志中的 quota/401 抢走容量错误的分类。
                for line in tail.splitlines():
                    diagnostic = _diagnostic(line)
                    if "[capacity]" in diagnostic:
                        report_diagnostic(diagnostic, "stderr")
                        break

        def report_output(item):
            """通知首条完整消息的时间；CLI 的 item 事件不能证明首 token 到达。

            Args:
                item: 已校验类型的完整 agent_message，仅本地检查正文是否为空。
            """
            nonlocal first_message_seconds
            text = item.get("text")
            if first_message_seconds is not None or not isinstance(text, str) or not text.strip():
                return
            first_message_seconds = time.monotonic() - started
            if on_output is not None:
                on_output({"elapsed_seconds": round(first_message_seconds, 3), "kind": "message_completed"})

        try:
            while selector.get_map():
                check_capacity_stderr()
                if cancel_event and cancel_event.is_set():
                    raise llm.LLMCancelled("已取消 Codex 请求")
                if time.monotonic() - started > timeout:
                    raise llm.LLMError(_failure_message("Codex 单题总时限已到", diagnostics, error_events, errors))
                if os.fstat(errors.fileno()).st_size > _LIMIT:
                    raise llm.LLMError("Codex 诊断输出超限")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    size += len(chunk)
                    if size > _LIMIT:
                        raise llm.LLMError("Codex 事件输出超限")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("expected event object")
                        kind = event.get("type")
                        item = event.get("item", {})
                        if kind == "turn.started":
                            if turn_started or finished:
                                raise ValueError("unexpected additional turn")
                            turn_started = True
                        if kind in ("item.started", "item.updated", "item.completed") and not isinstance(item, dict):
                            raise ValueError("expected item object")
                        # code_mode_only 模型在禁用执行器时报告提示；这是本渠道主动禁用工具的结果。
                        if (not turn_started and kind == "item.completed" and item.get("type") == "error"
                                and isinstance(item.get("message"), str)
                                and item["message"].startswith("Code Mode is unavailable because code-mode host is disabled.")):
                            continue
                        # CLI 升级或模型意外发起工具时拒绝计分，不能把工具辅助成绩冒充纯推理。
                        if kind in ("item.started", "item.updated", "item.completed"):
                            if finished:
                                raise ValueError("item after terminal event")
                            if item.get("type") not in ("agent_message", "reasoning"):
                                raise llm.LLMError("Codex 产生工具事件，本题不计分")
                            if kind == "item.completed" and item["type"] == "agent_message":
                                report_output(item)
                                text = item.get("text", "")
                                if not isinstance(text, str):
                                    raise ValueError("expected text answer")
                                texts.append(text)
                        elif kind == "turn.completed":
                            if not turn_started or finished:
                                raise ValueError("unexpected terminal event")
                            finished = True
                            # 用上游完成事件截断响应耗时，避免 CLI 退出和临时文件清理拖低吞吐。
                            # 该时间仍包含启动、排队、重试和思考，不等于纯生成时长。
                            response_seconds = time.monotonic() - started
                            usage = event.get("usage") or {}
                            if not isinstance(usage, dict) or any(
                                    type(usage[k]) is not int or usage[k] < 0 for k in
                                    ("input_tokens", "output_tokens", "reasoning_output_tokens", "cached_input_tokens")
                                    if k in usage and usage[k] is not None):
                                raise ValueError("invalid token usage")
                        elif kind in ("error", "turn.failed"):
                            # exec JSONL 的 error 是诊断通知，不是 turn 终态。CLI 可以在同一进程
                            # 内重连后正常完成；这里只记录分类，不能杀进程或由应用再发一次题目。
                            diagnostic = _diagnostic(event.get("error", event.get("message", "")))
                            report_diagnostic(diagnostic, kind)
                            if kind == "turn.failed":
                                raise llm.LLMError(_failure_message("Codex 请求失败 (turn.failed)", diagnostics, error_events, errors))
                            error_events += 1
                            if finished:
                                raise ValueError("error after terminal event")
            # stdout 可以先于进程关闭；这一阶段也必须继续响应取消，不能阻塞到整题超时。
            while process.poll() is None:
                check_capacity_stderr()
                if cancel_event and cancel_event.is_set():
                    raise llm.LLMCancelled("已取消 Codex 请求")
                if time.monotonic() - started > timeout:
                    raise llm.LLMError(_failure_message("Codex 单题总时限已到", diagnostics, error_events, errors))
                if os.fstat(errors.fileno()).st_size > _LIMIT:
                    raise llm.LLMError("Codex 诊断输出超限")
                time.sleep(.05)
            check_capacity_stderr()
            if process.returncode or buffer.strip() or not finished:
                reason = "Codex 未正常完成：" + ("事件流不完整" if buffer.strip() else "缺少 turn.completed" if not finished else "CLI 异常退出")
                raise llm.LLMError(_failure_message(reason, diagnostics, error_events, errors, process.returncode))
        except (ValueError, TypeError, subprocess.TimeoutExpired) as exc:
            raise llm.LLMError(_failure_message("Codex 事件格式异常或进程退出超时", diagnostics, error_events, errors)) from exc
        finally:
            selector.close()
            try:
                _kill(process, isolated)
            finally:
                process.stdout.close()
    text = texts[-1] if texts else ""
    usage = usage or {}
    elapsed = time.monotonic() - started
    return {"text": text, "completed": True, "truncated": False, "finish_reason": "turn.completed",
            "response_model": None, "ttft": None, "error": None if text.strip() else "Codex 未返回答案",
            "transport_diagnostics": {"error_events": error_events, "categories": diagnostics,
                                      "capacity_detected": capacity_detected},
            "performance": {"total_seconds": elapsed, "response_seconds": response_seconds,
                            "first_message_seconds": first_message_seconds},
            "ttfb": None, "total": elapsed, "chunks": None, "reasoning_chars": None,
            "usage": {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                      "total_tokens": usage["input_tokens"] + usage["output_tokens"]
                      if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None else None,
                      "reasoning_tokens": usage.get("reasoning_output_tokens"),
                      "cached_tokens": usage.get("cached_input_tokens"), "estimated": not bool(usage)}}
