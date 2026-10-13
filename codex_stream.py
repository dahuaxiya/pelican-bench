"""本站 ChatGPT 账号的性能测速：通过官方 app-server 观测正文增量，不读取凭据。"""
import json
import os
import selectors
import subprocess
import tempfile
import time

import codex_auth
import codex_channel as channel
import llm


def _command(binary):
    """构造仅供本站独立账号使用的 stdio 后端；不开放端口或加载业务工具。

    Args:
        binary: channel.binary_path 返回的可执行文件，不接受网页参数。
    Returns:
        固定官方提供方、medium 推理及只读沙箱的参数数组。
    """
    command = [binary, "app-server", "--listen", "stdio://"]
    settings = {"model_provider": "openai", "forced_login_method": "chatgpt",
                "cli_auth_credentials_store": "file", "approval_policy": "never",
                "sandbox_mode": "read-only", "model_reasoning_effort": "medium",
                "web_search": "disabled", "project_doc_max_bytes": 0,
                "skills.include_instructions": False, "history.persistence": "none",
                "tools.update_plan.enabled": False,
                "tools.experimental_request_user_input.enabled": False,
                "suppress_unstable_features_warning": True}
    for key, value in settings.items():
        command.extend(["-c", key + "=" + json.dumps(value)])
    for feature in channel._DISABLED:
        command.extend(["--disable", feature])
    command.extend(["--enable", "skip_host_skill_discovery"])
    return command


def complete(model, prompt, timeout, cancel_event=None, on_diagnostic=None, on_output=None):
    """测量一次独立流式请求；只认正文 delta，不用完整消息补造首字时间。

    Args:
        model: 经过站点目录验证的模型名。
        prompt: 性能测试的固定题干，仅通过 stdin 发送。
        timeout: 包含启动、排队及内部重连的总时限，单位秒。
        cancel_event: 在初始化和生成期间都可生效的取消信号。
        on_diagnostic: 非阻塞回调，只接收脱敏错误类别和相对时间。
        on_output: 非阻塞回调，只接收首个正文增量的相对时间，不含正文。
    Returns:
        完整回答、真实用量和观测时间；没有 delta 时 ttft 保持 None。
    """
    home = codex_auth.selected_home()
    if home is None:
        # app-server 没有 exec 的 ignore-user-config。桌面账号继续用已隔离的旧路径，
        # 避免为了计时加载用户全局 MCP、hooks 或规则；网站登录后才启用此适配器。
        result = channel.complete(model, prompt, timeout, cancel_event,
                                  on_diagnostic=on_diagnostic, on_output=on_output)
        result["transport"] = "codex-cli"
        return result
    if not isinstance(model, str) or not channel._MODEL.fullmatch(model):
        raise llm.LLMError("Codex 模型名无效")
    binary = channel.binary_path()
    if not binary:
        raise llm.LLMError("未安装 Codex CLI")
    if cancel_event and cancel_event.is_set():
        raise llm.LLMCancelled("已取消 Codex 请求")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pelican-stream-") as directory, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(_command(binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, cwd=directory, env=channel._environment(home=home),
                                   start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer, size, stderr_size = b"", 0, 0
        thread_id = turn_id = None
        expected_id = 1
        ttft = first_message = response_seconds = None
        text, usage, diagnostics = "", {}, []
        chunks = error_events = 0
        capacity_detected = finished = False
        response_model = None

        def send(method, params, request_id=None):
            """发送内部固定 RPC；题干不进入命令行，写入失败由外层统一清理进程。

            Args:
                method: 适配器固定的方法名。
                params: 适配器组装的参数。
                request_id: 本次握手的序号；None 表示通知。
            """
            event = {"method": method, "params": params}
            if request_id is not None:
                event["id"] = request_id
            process.stdin.write(json.dumps(event).encode() + b"\n")
            process.stdin.flush()

        def report(value, source):
            """仅向日志和观察者暴露固定分类；恢复成功后仍保留容量证据。

            Args:
                value: 不可信上游错误，只用于本地分类。
                source: 固定事件来源名称。
            """
            nonlocal capacity_detected, diagnostics
            diagnostic = channel._diagnostic(value)
            capacity_detected |= "[capacity]" in diagnostic
            if diagnostic not in diagnostics:
                diagnostics = (diagnostics + [diagnostic])[-16:]
            if on_diagnostic:
                on_diagnostic({"category": diagnostic, "source": source,
                               "elapsed_seconds": round(time.monotonic() - started, 3)})

        try:
            send("initialize", {"clientInfo": {"name": "pelican_bench", "version": "0.4.0"}}, 1)
            while not finished:
                if cancel_event and cancel_event.is_set():
                    raise llm.LLMCancelled("已取消 Codex 请求")
                if time.monotonic() - started > timeout:
                    raise llm.LLMError("Codex 流式请求总时限已到 [timeout]")
                length = os.fstat(errors.fileno()).st_size
                if length > channel._LIMIT:
                    raise llm.LLMError("Codex 诊断输出超限")
                if length != stderr_size and not capacity_detected:
                    stderr_size = length
                    tail = os.pread(errors.fileno(), 65536, max(0, length - 65536)).decode("utf-8", "replace")
                    for line in tail.splitlines():
                        if "[capacity]" in channel._diagnostic(line):
                            report(line, "stderr")
                            break
                for key, _ in selector.select(.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        raise llm.LLMError("Codex 流式连接中断，缺少 turn/completed [network]")
                    size += len(chunk)
                    if size > channel._LIMIT:
                        raise llm.LLMError("Codex 事件输出超限")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("invalid event")
                        method, params = event.get("method"), event.get("params") or {}
                        if "id" in event:
                            # 本适配器只允许纯文本请求。服务端要求审批或执行工具时立即失败，
                            # 不能代用户授权，也不能把工具辅助输出当作模型直接生成的测速结果。
                            if method:
                                raise llm.LLMError("Codex 请求了额外能力，本次不计分")
                            if event["id"] != expected_id:
                                raise ValueError("unexpected RPC response")
                            if event.get("error"):
                                report(event["error"], "rpc")
                                raise llm.LLMError("Codex 流式 RPC 失败")
                            result = event.get("result")
                            if not isinstance(result, dict):
                                raise ValueError("invalid RPC result")
                            if expected_id == 1:
                                send("initialized", {})
                                expected_id = 2
                                send("thread/start", {"model": model, "modelProvider": "openai",
                                    "ephemeral": True, "cwd": directory, "sandbox": "read-only",
                                    "approvalPolicy": "never", "developerInstructions":
                                    "Answer the user's question directly. Do not use tools or access files. Follow the requested answer format."}, 2)
                            elif expected_id == 2:
                                thread_id = result["thread"]["id"]
                                if not isinstance(thread_id, str) or not thread_id:
                                    raise ValueError("invalid thread")
                                expected_id = 3
                                send("turn/start", {"threadId": thread_id, "model": model, "effort": "medium",
                                     "input": [{"type": "text", "text": prompt}]}, 3)
                            elif expected_id == 3:
                                returned_id = result["turn"]["id"]
                                if not isinstance(returned_id, str) or not returned_id or (turn_id and turn_id != returned_id):
                                    raise ValueError("invalid turn")
                                turn_id, expected_id = returned_id, None
                            continue
                        # 只计入本次 thread/turn；初始化、推理摘要、空 delta 均不是首字。
                        if params.get("threadId") != thread_id or thread_id is None:
                            continue
                        if method == "turn/started" and turn_id is None:
                            turn_id = params["turn"]["id"]
                        event_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
                        if not turn_id or event_turn != turn_id:
                            continue
                        if method == "item/agentMessage/delta":
                            delta = params["delta"]
                            if not isinstance(delta, str):
                                raise ValueError("invalid delta")
                            if delta.strip():
                                chunks += 1
                                if ttft is None:
                                    ttft = time.monotonic() - started
                                    if on_output:
                                        on_output({"elapsed_seconds": ttft, "kind": "text_delta"})
                        elif method in ("item/started", "item/completed"):
                            item = params["item"]
                            if item.get("type") not in ("userMessage", "agentMessage", "reasoning"):
                                raise llm.LLMError("Codex 产生工具事件，本次不计分")
                            if method == "item/completed" and item["type"] == "agentMessage":
                                text = item["text"]
                                if not isinstance(text, str):
                                    raise ValueError("invalid text")
                                if text.strip() and first_message is None:
                                    first_message = time.monotonic() - started
                        elif method == "thread/tokenUsage/updated":
                            # 单独线程的 total 包含本轮内部重试，不用最后一个请求的 last 冒充总用量。
                            usage = params["tokenUsage"]["total"]
                            if not isinstance(usage, dict) or any(type(v) is not int or v < 0 for v in usage.values()):
                                raise ValueError("invalid usage")
                        elif method == "model/rerouted":
                            candidate = params.get("toModel")
                            if isinstance(candidate, str) and channel._MODEL.fullmatch(candidate):
                                response_model = candidate
                        elif method == "error":
                            error_events += 1
                            report(params.get("error", {}), "error")
                        elif method == "turn/completed":
                            if params["turn"].get("status") != "completed":
                                report(params["turn"].get("error") or {}, "turn/completed")
                                raise llm.LLMError("Codex 流式请求未成功完成")
                            response_seconds = time.monotonic() - started
                            finished = True
                            break
        except llm.LLMCancelled:
            raise
        except (llm.LLMError, OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, llm.LLMError) else "Codex 流式事件格式异常或连接失败"
            raise llm.LLMError(channel._failure_message(reason, diagnostics, error_events, errors)) from None
        finally:
            # app-server 是常驻协议，不会因一轮完成自行退出。只回收本次新建的进程组，
            # 完成、失败和取消均不触碰网站登录进程或用户桌面中的 Codex。
            selector.close()
            channel._kill(process)
            process.stdin.close()
            process.stdout.close()
    return {"text": text, "completed": True, "truncated": False, "finish_reason": "turn/completed",
            "response_model": response_model, "ttft": ttft,
            "error": None if text.strip() else "Codex 未返回答案", "transport": "codex-app-server",
            "transport_diagnostics": {"capacity_detected": capacity_detected,
                                      "error_events": error_events, "categories": diagnostics},
            "performance": {"total_seconds": time.monotonic() - started,
                            "response_seconds": response_seconds, "first_message_seconds": first_message},
            "usage": {"prompt_tokens": usage.get("inputTokens"), "completion_tokens": usage.get("outputTokens"),
                      "total_tokens": usage.get("totalTokens"), "reasoning_tokens": usage.get("reasoningOutputTokens"),
                      "cached_tokens": usage.get("cachedInputTokens")}, "chunks": chunks}
