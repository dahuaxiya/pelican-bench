"""本地站点的 ChatGPT 授权；只调用官方账号 RPC，不读取或转发凭据。"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from urllib.parse import urlparse
from runtime_paths import DATA_DIR

AUTH_HOME = DATA_DIR / ".local" / "chatgpt"
_URL_HOSTS = {"auth.openai.com", "chatgpt.com", "auth0.openai.com"}
_MAX_LINE = 2 * 1024 * 1024


def selected_home():
    """返回站点选中的凭据目录；退出后仍保持选中，避免暗中切回桌面账号。"""
    return AUTH_HOME if (AUTH_HOME / ".selected").is_file() else None


def official_url(value):
    """仅允许官方 HTTPS 授权链接；拒绝凭据式 URL、非默认端口及超长内容。"""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        raise ValueError("官方登录返回了无效链接")
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.hostname not in _URL_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("官方登录返回了无效链接")
    return value


class AccountManager:
    """独占一个 stdio 子进程；HTTP 请求串行操作账号，读线程持续处理登录通知。"""

    def __init__(self):
        """初始化锁与非敏感状态；构造本身不启动 CLI 或发起登录。"""
        self.lock = threading.RLock()
        self._state_lock = threading.Lock()
        self.process = None
        self._waiters = {}
        self._completed = {}
        self._sequence = 0
        self.pending = None
        self.message = ""

    def _launch(self):
        """按需启动隔离的官方后端，固定文件凭据仓库，避免碰到桌面 keyring。"""
        import codex_channel
        if self.process and self.process.poll() is None:
            return
        self.close()
        binary = codex_channel.binary_path()
        if not binary:
            raise ValueError("未安装 Codex CLI，暂时无法登录 ChatGPT")
        AUTH_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        AUTH_HOME.chmod(0o700)
        # app-server 不支持 exec 专用的 ignore-user-config；隔离 CODEX_HOME，
        # 且不创建推理线程，账号操作不会加载用户工作区工具或执行 AGENTS 指令。
        command = [binary, "app-server", "--listen", "stdio://"]
        for key, value in {"model_provider": '"openai"', "forced_login_method": '"chatgpt"',
                           "cli_auth_credentials_store": '"file"', "approval_policy": '"never"',
                           "history.persistence": '"none"'}.items():
            command.extend(["-c", key + "=" + value])
        for feature in codex_channel._DISABLED:
            command.extend(["--disable", feature])
        command.extend(["--enable", "skip_host_skill_discovery"])
        # stdio 不对外监听 RPC 端口。stderr 可能含授权资料，不能进入 HTTP 或访问日志。
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, cwd=AUTH_HOME,
                                        env=codex_channel._environment(home=AUTH_HOME), start_new_session=True)
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()
        try:
            self._rpc("initialize", {"clientInfo": {"name": "pelican_bench", "title": "Pelican Bench", "version": "1.0"}})
            self.process.stdin.write(b'{"method":"initialized","params":{}}\n')
            self.process.stdin.flush()
        except Exception:
            self.close()
            raise

    def _read(self, process):
        """持续分发响应及完成通知；按进程归属唤醒等待者，不把原始错误送给网页。"""
        try:
            while True:
                line = process.stdout.readline(_MAX_LINE + 1)
                if not line or len(line) > _MAX_LINE:
                    break
                event = json.loads(line)
                if not isinstance(event, dict):
                    break
                with self._state_lock:
                    if "id" in event and "method" not in event:
                        waiter = self._waiters.get(event["id"])
                        if waiter and waiter[0] is process:
                            waiter[1].put(event)
                    elif event.get("method") == "account/login/completed":
                        data = event.get("params") or {}
                        if self.process is process:
                            # 通知可能先于 login/start 响应到达；暂存 id，不能误判为丢失。
                            self._completed[data.get("loginId")] = data.get("success") is True
        except (OSError, ValueError, TypeError):
            pass
        finally:
            with self._state_lock:
                for owner, waiter in self._waiters.values():
                    if owner is process:
                        waiter.put({"error": True})

    def _rpc(self, method, params=None):
        """发送固定内部 RPC 并有界等待；调用方必须持有操作锁，参数不来自任意网页。"""
        self._sequence += 1
        request_id = self._sequence
        waiter = queue.Queue()
        process = self.process
        with self._state_lock:
            self._waiters[request_id] = (process, waiter)
        try:
            wire = json.dumps({"id": request_id, "method": method, "params": params or {}}).encode() + b"\n"
            process.stdin.write(wire)
            process.stdin.flush()
            event = waiter.get(timeout=30)
            if event.get("error") or not isinstance(event.get("result"), dict):
                raise ValueError("ChatGPT 登录服务请求失败，请重试；浏览器授权失败时可改用设备码")
            return event["result"]
        except (OSError, queue.Empty, AttributeError):
            # 请求超时后的迟到登录不能悄悄继续；关闭本服务拥有的子进程及回调。
            self.close()
            raise ValueError("ChatGPT 登录服务未响应，请检查网络后重试") from None
        finally:
            with self._state_lock:
                self._waiters.pop(request_id, None)

    def _select(self):
        """仅写账号来源标记；标记不含凭据，必须在确认 ChatGPT 认证成功后调用。"""
        marker = AUTH_HOME / ".selected"
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)

    def status(self):
        """读取脱敏账号状态并确认登录终态；第一次打开设置不主动启动授权。"""
        with self.lock:
            if not AUTH_HOME.exists() and not self.process:
                return {"state": "signed_out", "account": None, "login": None, "message": ""}
            self._launch()
            if self.pending and time.time() >= self.pending["expires_at"]:
                self._cancel()
                self.message = "登录已过期，请重新登录"
            account = self._rpc("account/read", {"refreshToken": False}).get("account")
            signed_in = isinstance(account, dict) and account.get("type") == "chatgpt"
            if self.pending:
                with self._state_lock:
                    completed = self._completed.get(self.pending["id"])
                if completed is False:
                    self.pending = None
                    self.message = "登录未完成，请重试或改用设备码"
                elif completed is True and signed_in:
                    self.pending = None
                    self.message = "登录成功"
            if signed_in and not self.pending:
                # 后端重启可能错过完成通知；以隔离目录中的官方认证状态恢复。
                self._select()
            public_account = None
            if signed_in:
                public_account = {"email": str(account.get("email") or "")[:254],
                                  "plan": str(account.get("planType") or "")[:40]}
            return {"state": "pending" if self.pending else "signed_in" if signed_in else "signed_out",
                    "account": public_account, "login": dict(self.pending) if self.pending else None,
                    "message": self.message}

    def start(self, kind):
        """创建或复用一次官方授权；重复点击不生成新流程，只支持浏览器或设备码。"""
        if kind not in ("browser", "device"):
            raise ValueError("登录方式只能是 browser 或 device")
        with self.lock:
            current = self.status()
            if current["state"] == "signed_in":
                return current
            if self.pending and self.pending["kind"] == kind:
                return current
            if self.pending:
                self._cancel()
            self._launch()
            self.message = ""
            with self._state_lock:
                self._completed.clear()
            response = self._rpc("account/login/start", {"type": "chatgpt" if kind == "browser" else "chatgptDeviceCode"})
            try:
                login_id = response.get("loginId")
                if not isinstance(login_id, str) or not login_id or len(login_id) > 200:
                    raise ValueError("官方登录未返回有效会话")
                url = official_url(response.get("authUrl" if kind == "browser" else "verificationUrl"))
                user_code = response.get("userCode", "")
                if not isinstance(user_code, str) or len(user_code) > 100:
                    raise ValueError("官方登录未返回有效设备码")
                self.pending = {"id": login_id, "kind": kind, "url": url,
                                "user_code": user_code, "expires_at": time.time() + 900}
            except ValueError:
                self.close()
                raise
            return self.status()

    def _cancel(self):
        """取消本服务的待授权会话；先等取消回执，防止 UI 隐藏了仍活动的登录。"""
        if self.pending:
            self._rpc("account/login/cancel", {"loginId": self.pending["id"]})
            self.pending = None
        self.message = "登录已取消"

    def cancel(self):
        """取消正在等待用户的授权，不退出已登录的账号。"""
        with self.lock:
            self._cancel()
            return self.status()

    def logout(self):
        """退出隔离目录中的账号；不会退出 Codex 桌面，且禁止退回桌面凭据。"""
        with self.lock:
            self._launch()
            self._cancel()
            self._rpc("account/logout")
            self._select()
            self.message = "已退出本站账号"
            return self.status()

    def models(self):
        """从已授权后端读取可见且支持 medium 的模型；不依赖桌面模型缓存。"""
        import codex_channel
        with self.lock:
            state = self.status()
            if state["state"] != "signed_in":
                raise ValueError("请在设置中登录 ChatGPT")
            models, cursor = set(), None
            for _ in range(20):
                response = self._rpc("model/list", {"cursor": cursor, "limit": 100, "includeHidden": False})
                for row in response.get("data", []):
                    model = row.get("model", "")
                    if (not row.get("hidden") and codex_channel._MODEL.fullmatch(model)
                            and any(level.get("reasoningEffort") == "medium" for level in row.get("supportedReasoningEfforts", []))):
                        models.add(model)
                cursor = response.get("nextCursor")
                if not cursor:
                    return sorted(models)
            raise ValueError("官方模型目录分页过多，请重试")

    def close(self):
        """只回收本站创建的进程组；退出后端或 RPC 超时时不留下回调监听进程。"""
        import codex_channel
        with self.lock:
            process, self.process = self.process, None
            if process:
                codex_channel._kill(process)
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        pipe.close()
            self.pending = None
            with self._state_lock:
                self._completed.clear()


manager = AccountManager()
atexit.register(manager.close)
