"""Kimi Code 官方设备码登录和无工具 ACP 评测；凭据由官方 CLI 独立管理。"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

import codex_channel
import llm
from runtime_paths import CODE_DIR, DATA_DIR

SITE = "Kimi 官方（Kimi Code 登录）"
BASE_URL = "kimi://official"
AUTH_HOME = DATA_DIR / ".local" / "kimi"
PROFILE = CODE_DIR / "kimi-bench-agent.md"
POLICY = "kimi-acp-journal-v1"
MODEL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}$")
LIMIT = 4 * 1024 * 1024


def binary_path():
    """优先本机新版 Kimi Code；旧 Python kimi-cli 不具备本渠道要求的 ACP。"""
    for candidate in (Path.home() / ".kimi-code/bin/kimi", shutil.which("kimi")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def environment():
    """保留必要系统环境与代理，隔离 Kimi 配置，禁止继承自定义端点和 API key。"""
    env = codex_channel._environment()
    env.pop("CODEX_HOME", None)
    env.update(KIMI_CODE_HOME=str(AUTH_HOME), KIMI_DISABLE_TELEMETRY="1",
               KIMI_CODE_BUILTIN_PRODUCT_SKILLS="0", KIMI_CODE_WATCH="0",
               KIMI_CODE_SEARCH_WORKER="0", KIMI_CODE_BACKGROUND_KEEP_ALIVE_ON_EXIT="0")
    return env


def prepare():
    """只创建本站私有目录，不读取、迁移或更改用户原有 Kimi 配置。"""
    AUTH_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    AUTH_HOME.chmod(0o700)
    binary = binary_path()
    if not binary:
        raise ValueError("未安装新版 Kimi Code CLI，暂时无法使用 Kimi 登录")
    return binary


def official_url(value):
    """设备授权只允许 Kimi 官方 HTTPS 地址，不能把异常输出变成任意跳转。"""
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise ValueError("Kimi 返回了无效授权链接")
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.hostname not in
            {"auth.kimi.com", "auth.kimi.ai", "www.kimi.com", "kimi.com", "www.kimi.ai", "kimi.ai"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Kimi 返回了无效授权链接")
    return value


class RpcError(llm.LLMError):
    """仅保留错误码与固定分类，原始 RPC 诊断不进入网页和结果。"""

    def __init__(self, error):
        """对可能携带令牌的上游错误做分类，认证错误供登录状态判断。"""
        self.code = error.get("code") if isinstance(error, dict) else None
        super().__init__("Kimi 请求失败：" + codex_channel._diagnostic(error))


class ACP:
    """一个受限 stdio 会话；超时、取消或协议错误后回收自己创建的进程。"""

    def __init__(self, timeout=30, cancel_event=None, isolated=True):
        """总时限覆盖启动和所有 RPC；工程 worker 的子进程留在其进程组内。"""
        self.deadline = time.monotonic() + timeout
        self.cancel_event = cancel_event
        self.isolated = isolated
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.buffer = b""
        self.size = 0
        self.sequence = 0
        self.session_id = None
        self.work = None

    def __enter__(self):
        """在独立工作目录启动 ACP，不声明客户端文件或终端能力。"""
        try:
            binary = prepare()
            self.work = tempfile.TemporaryDirectory(prefix="pelican-kimi-")
            directory = Path(self.work.name)
            # 显式项目边界和最高项目优先级的 agent 覆盖，阻断父目录及全局默认工具。
            (directory / ".git").mkdir()
            agents = directory / ".kimi-code" / "agents"
            agents.mkdir(parents=True)
            shutil.copyfile(PROFILE, agents / "agent.md")
            self.process = subprocess.Popen([binary, "acp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=directory, env=environment(), start_new_session=self.isolated)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.info = self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "pelican-bench", "version": "1"}})
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, *_):
        """成功与失败路径均执行同样的进程清理，不留下后台计费请求。"""
        self.close()

    def check(self):
        """每次等待读写前检查取消和总时限，停止不必等待模型先返回。"""
        if self.cancel_event and self.cancel_event.is_set():
            raise llm.LLMCancelled("已取消 Kimi 请求")
        if time.monotonic() >= self.deadline:
            raise llm.LLMError("Kimi 单题总时限已到")

    def call(self, method, params=None, on_update=None):
        """发送内部固定 RPC；分块写长提示词并轮询，拒绝所有反向工具请求。"""
        self.check()
        self.sequence += 1
        request_id = self.sequence
        wire = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": params or {}}, ensure_ascii=False).encode() + b"\n"
        # ACP 通过 stdin 传题目，避免 argv 长度限制和进程列表泄露题目正文。
        fd = self.process.stdin.fileno()
        os.set_blocking(fd, False)
        self.selector.register(fd, selectors.EVENT_WRITE)
        sent = 0
        try:
            while True:
                self.check()
                for key, _ in self.selector.select(.1):
                    if key.fd == fd:
                        sent += os.write(fd, wire[sent:sent + 8192])
                        if sent == len(wire):
                            self.selector.unregister(fd)
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        raise llm.LLMError("Kimi 连接提前结束，未收到完整结果")
                    self.size += len(chunk)
                    if self.size > LIMIT:
                        raise llm.LLMError("Kimi 事件输出超限")
                    self.buffer += chunk
                    while b"\n" in self.buffer:
                        line, self.buffer = self.buffer.split(b"\n", 1)
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("invalid ACP event")
                        if "id" in event and "method" in event:
                            raise llm.LLMError("Kimi 请求工具权限，本题不计分")
                        if event.get("id") == request_id:
                            if "error" in event:
                                raise RpcError(event["error"])
                            result = event.get("result") or {}
                            if not isinstance(result, dict):
                                raise ValueError("invalid ACP result")
                            return result
                        if event.get("method") == "session/update":
                            data = event.get("params") or {}
                            update = data.get("update") or {}
                            if update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
                                raise llm.LLMError("Kimi 产生工具事件，本题不计分")
                            if on_update and data.get("sessionId") == self.session_id:
                                on_update(update)
        except (OSError, ValueError, TypeError) as exc:
            raise llm.LLMError("Kimi 协议异常，请检查 CLI 版本或重试") from exc
        finally:
            with contextlib.suppress(KeyError):
                self.selector.unregister(fd)

    def session(self):
        """创建不带 MCP 的新会话；目录查询也不复用已有业务会话或发送题目。"""
        data = self.call("session/new", {"cwd": self.work.name, "mcpServers": []})
        session_id = data.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise llm.LLMError("Kimi 未返回有效会话")
        self.session_id = session_id
        return data

    def verified_answer(self):
        """关闭会话等待官方日志刷盘，再核验真实终态和最终正文；记录缺失时不计分。"""
        # Kimi 2.1.1 的 ACP 把部分失败也映射成 end_turn，且重试前的片段可能已发给客户端。
        # 因此仅凭 ACP 终态或拼接的 delta 不够。close 会刷完本题 journal，不能先 delete。
        self.call("session/close", {"sessionId": self.session_id})
        if not re.fullmatch(r"session_[a-zA-Z0-9-]{1,100}", self.session_id):
            raise llm.LLMError("Kimi 会话格式不兼容，无法核验回答")
        paths = list(AUTH_HOME.glob("sessions/*/" + self.session_id + "/agents/main/wire.jsonl"))
        if len(paths) != 1 or not paths[0].resolve().is_relative_to(AUTH_HOME.resolve()):
            raise llm.LLMError("Kimi 未保存本题完成记录，不能计分")
        try:
            with paths[0].open("rb") as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("journal too large")
            events = [json.loads(line) for line in data.splitlines() if line.strip()]
            ends = [e for e in events if e.get("type") == "turn.ended"]
            prompts = [e for e in events if e.get("type") == "prompt.completed"]
            if (len(ends) != 1 or ends[0].get("reason") != "completed" or
                    len(prompts) != 1 or prompts[0].get("reason") != "completed"):
                raise llm.LLMError("Kimi 未正常完成回答：" + codex_channel._diagnostic(ends))
            messages = [e["message"] for e in events if e.get("type") == "agent.message.appended"
                        and e.get("message", {}).get("message", {}).get("role") == "assistant"]
            if not messages:
                raise llm.LLMError("Kimi 未保存完整答案")
            if any(m["message"].get("toolCalls") for m in messages):
                raise llm.LLMError("Kimi 产生工具事件，本题不计分")
            answer = messages[-1]
            if answer.get("meta", {}).get("finish", {}).get("finishReason") != "completed":
                raise llm.LLMError("Kimi 答案截断或结束原因不明确，不能计分")
            parts = answer["message"]["content"]
            return "".join(p["text"] for p in parts if p.get("type") == "text")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise llm.LLMError("Kimi 完成记录格式不兼容，不能计分") from exc

    def close(self):
        """回收进程和临时目录；取消时不额外等待会话清理 RPC。"""
        if self.process:
            process, self.process = self.process, None
            codex_channel._kill(process, self.isolated)
            for pipe in (process.stdin, process.stdout):
                pipe.close()
        self.selector.close()
        if self.work:
            self.work.cleanup()


class AccountManager:
    """官方 CLI 持有 OAuth token，本站只保留一次性设备码和脱敏登录状态。"""

    def __init__(self):
        """初次读取不创建子进程；锁保护登录、取消、退出和目录缓存。"""
        self.lock = threading.RLock()
        self.process = None
        self.pending = None
        self.message = ""
        self.cache = None
        self.started_at = 0

    def _snapshot(self, models=False):
        """使用官方认证 RPC 确认状态；模型别名来自同一隔离账号的 ACP 目录。"""
        with ACP() as client:
            try:
                client.call("authenticate", {"methodId": "login"})
            except RpcError as exc:
                if exc.code == -32000:
                    return {"state": "signed_out", "models": [], "cli_version": client.info.get("agentInfo", {}).get("version")}
                raise
            available = []
            if models:
                data = client.session()
                for option in data.get("configOptions", []):
                    if option.get("id") == "model":
                        available = [item["value"] for item in option.get("options", [])
                                     if isinstance(item.get("value"), str) and MODEL.fullmatch(item["value"])]
                client.call("session/delete", {"sessionId": client.session_id})
            return {"state": "signed_in", "models": sorted(set(available)),
                    "cli_version": client.info.get("agentInfo", {}).get("version")}

    def status(self, refresh=False):
        """授权进行中只读内存进度；结束后用 ACP 重新核对，退出码本身不代表登录成功。"""
        with self.lock:
            if self.process:
                if not self.pending and time.monotonic() - self.started_at >= 60:
                    self._cancel()
                    self.message = "获取 Kimi 设备码超时，请检查网络后重试"
                elif self.pending and time.time() >= self.pending["expires_at"]:
                    self._cancel()
                    self.message = "Kimi 登录已过期，请重新登录"
                elif self.process.poll() is None:
                    return {"state": "pending" if self.pending else "starting", "login": self.pending,
                            "account": None, "message": self.message}
                else:
                    code = self.process.returncode
                    self._cancel()
                    self.message = "Kimi 授权已结束" if code == 0 else "Kimi 登录未完成，请重试"
                    refresh = True
            if not AUTH_HOME.exists():
                snapshot = {"state": "signed_out", "models": []}
            elif not refresh and self.cache and time.monotonic() - self.cache[0] < 30:
                snapshot = self.cache[1]
            else:
                snapshot = self._snapshot(models=True)
                self.cache = (time.monotonic(), snapshot)
            return {"state": snapshot["state"], "login": None, "account": None, "message": self.message}

    def _read_login(self, process, region):
        """仅提取官方 CLI 的固定设备码行；不回显完整 stderr 或失败正文。"""
        buffer = ""
        try:
            while True:
                chunk = os.read(process.stderr.fileno(), 2048)
                if not chunk:
                    return
                buffer += chunk.decode("utf-8", errors="replace")
                if len(buffer) > 32768:
                    raise ValueError("login output too large")
                url = re.search(r"Opening browser for Kimi device login: (https://[^\s]+)", buffer)
                code = re.search(r"enter code: ([A-Za-z0-9-]{3,100})", buffer)
                expiry = re.search(r"Code expires in (\d+)s", buffer)
                if url and code:
                    login = {"kind": "device", "region": region, "url": official_url(url[1]),
                             "user_code": code[1], "expires_at": time.time() + min(int(expiry[1]) if expiry else 900, 900)}
                    with self.lock:
                        if self.process is process and not self.pending:
                            self.pending = login
                            self.message = "请在 Kimi 官方页面输入设备码并完成授权"
        except (ValueError, OSError):
            with self.lock:
                if self.process is process:
                    codex_channel._kill(process)
                    self.message = "Kimi 未返回有效授权信息，请重试"

    def start(self, region="mainland-cn"):
        """重复点击复用进行中的授权；region 只能选择官方中国区或国际区。"""
        if region not in ("mainland-cn", "global"):
            raise ValueError("请选择有效的 Kimi 账号区域")
        with self.lock:
            state = self.status()
            if state["state"] in ("signed_in", "pending", "starting"):
                return state
            binary = prepare()
            self.cache = None
            self.message = "正在获取 Kimi 设备码"
            self.started_at = time.monotonic()
            self.process = subprocess.Popen([binary, "login", "--region", region], cwd=AUTH_HOME,
                env=environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, start_new_session=True)
            threading.Thread(target=self._read_login, args=(self.process, region), daemon=True).start()
            return {"state": "starting", "login": None, "account": None, "message": self.message}

    def _cancel(self):
        """先终止官方轮询再清除界面状态，防止取消之后迟到的授权继续生效。"""
        if self.process:
            process, self.process = self.process, None
            codex_channel._kill(process)
            process.stderr.close()
        self.pending = None
        self.cache = None

    def cancel(self):
        """取消本站设备码流程，再读取真实状态，保留恰好已完成的登录。"""
        with self.lock:
            self._cancel()
            self.message = "登录已取消"
            return self.status(refresh=True)

    def logout(self):
        """通过官方 ACP 退出本站账号，不改用户 ~/.kimi-code 中的已有凭据。"""
        with self.lock:
            self._cancel()
            with ACP() as client:
                client.call("logout")
            self.message = "已退出本站 Kimi 账号"
            return self.status(refresh=True)

    def catalog(self, refresh=False):
        """向各评测页提供同一模型目录；登录期间不可启动新的 Kimi 任务。"""
        with self.lock:
            result = {"name": SITE, "base_url": BASE_URL, "transport": "kimi-acp", "builtin": True,
                      "auth_source": "site", "models": [], "ready": False,
                      "note": "Kimi Code 额度 · 官方默认推理 · API 温度及输出上限不适用"}
            try:
                state = self.status(refresh=refresh)
                if state["state"] != "signed_in":
                    result["error"] = "请先完成或取消 Kimi 授权" if state["state"] in ("starting", "pending") else "请在设置中登录 Kimi"
                elif self.cache and self.cache[1]["models"]:
                    result.update(ready=True, models=self.cache[1]["models"], cli_version=self.cache[1].get("cli_version"))
                else:
                    result["error"] = "Kimi 账号已登录，但没有可用模型，请刷新重试"
            except (ValueError, OSError, llm.LLMError):
                result["error"] = "无法读取 Kimi 登录状态，请检查新版 Kimi Code CLI 或重试"
            return result

    def close(self):
        """后端退出时回收本站待授权进程，已保存的官方凭据继续保留。"""
        with self.lock:
            self._cancel()


manager = AccountManager()
atexit.register(manager.close)


def catalog(refresh=False):
    """返回固定官方账号目录，禁止客户端指定凭据或覆盖上游服务地址。"""
    return manager.catalog(refresh)


def site_config():
    """生成只读站点卡片；账号信息不混入业务 API key 配置。"""
    return {**catalog(), "api_key": "", "pricing": {}, "thinking_mode": "default", "max_tokens": None, "prompt": ""}


def execution(model):
    """任务开始前核对可用模型并记录真实条件，与 API 测评条件分开排名。"""
    info = catalog()
    if not info["ready"]:
        raise ValueError(info["error"])
    if model not in info["models"]:
        raise ValueError("模型不在当前 Kimi 官方目录中，请刷新模型列表")
    return {"transport": "kimi-acp", "policy": POLICY, "cli_version": info["cli_version"],
            "reasoning_effort": "default", "temperature": None, "max_tokens": None,
            "timeout_kind": "wall", "billing": "kimi-code-plan"}


def complete(model, prompt, timeout, cancel_event=None, isolated=True):
    """独立 ACP 会话；只接收正常终态，首字延迟仅统计非空正文增量。

    Args:
        model: 当前官方目录的模型标识。
        prompt: 本次任务文本，不启用工具。
        timeout: 包含会话初始化的总时限秒数。
        cancel_event: 可选共享取消信号。
        isolated: 是否隔离执行环境；线上评测必须保持 True。
    Returns:
        正文、真实时序及用量缺失标记；不将上下文占用充当计费 token。
    """
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        raise llm.LLMError("Kimi 模型名无效")
    started = time.monotonic()
    texts, thinking, first = [], 0, None

    def receive(update):
        """只收当前会话的文字事件；usage_update 是上下文占用，不是本次计费 tokens。"""
        nonlocal thinking, first
        kind, content = update.get("sessionUpdate"), update.get("content", {})
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            if content.get("type") != "text" or not isinstance(content.get("text"), str):
                raise llm.LLMError("Kimi 返回非文本答案")
            # 思考增量和空事件都不代表用户开始看到回答，不能据此提前报告 TTFT。
            if kind == "agent_message_chunk" and content["text"] and first is None:
                first = time.monotonic() - started
            if kind == "agent_message_chunk":
                texts.append(content["text"])
            else:
                thinking += len(content["text"])

    with ACP(timeout, cancel_event, isolated) as client:
        client.session()
        client.call("session/set_model", {"sessionId": client.session_id, "modelId": model})
        terminal = client.call("session/prompt", {"sessionId": client.session_id,
            "prompt": [{"type": "text", "text": prompt}]}, receive)
        response_seconds = time.monotonic() - started
        reason = terminal.get("stopReason")
        # 正常结束是必需证据；出现文本但连接断开、限额结束、拒答都不能按正确答案计分。
        if reason != "end_turn":
            raise llm.LLMError("Kimi 未正常完成回答：" + (reason if reason in
                ("max_tokens", "max_turn_requests", "refusal", "cancelled") else "未知结束状态"))
        text = client.verified_answer()
        # 回答已有完整结束证据，清理失败不能把有效答案改成接口失败；退出仍回收进程。
        client.deadline = min(client.deadline, time.monotonic() + 2)
        with contextlib.suppress(llm.LLMError):
            client.call("session/delete", {"sessionId": client.session_id})
    return {"text": text, "completed": True, "truncated": False, "finish_reason": "end_turn",
            "response_model": None, "ttft": first, "ttfb": first, "total": time.monotonic() - started,
            "chunks": len(texts), "reasoning_chars": thinking,
            "performance": {"response_seconds": response_seconds,
                            "total_seconds": time.monotonic() - started},
            "error": None if text.strip() else "Kimi 未返回答案",
            "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                      "reasoning_tokens": None, "cached_tokens": None, "estimated": True}}
