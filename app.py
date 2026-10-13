"""本地 Web 应用入口。

只监听 127.0.0.1：这个服务持有各站点的 api key，并且能代表使用者向任意地址发
请求，绝不能暴露到局域网。除了绑定回环地址，还额外校验 Host 头，防 DNS rebinding
——攻击者可以让一个恶意页面把域名解析到 127.0.0.1，浏览器就会带着同源身份访问
本服务，此时只绑回环是挡不住的。

用法：
    python3 app.py            # 默认 http://127.0.0.1:8777
    python3 app.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import bench
import config
import llm
import iq
import iq_tasks
import iq_review
import swe
import report
import codex_auth
import kimi_channel
import codex_probe

STATIC_DIR = os.path.join(config.SOURCE_ROOT, "static")

# Host 头白名单。只允许回环地址的各种写法，其它一律拒。
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
# 登录切换和任务创建共用锁，防止检查空闲之后另一请求恰好启动新任务。
_ACCOUNT_ACTION_LOCK = threading.RLock()


def _evaluation_busy():
    """只看内存中仍活动的任务及复核队列，避免磁盘历史被当成在途工作。"""
    if codex_probe.manager.busy():
        return True
    with iq._LOCK:
        if any(run.data.get("status") in ("running", "stopping") for run in iq._RUNS.values()):
            return True
    with swe._LOCK:
        if any(run["data"].get("status") in ("running", "stopping") for run in swe._RUNS.values()):
            return True
    with bench._RUNS_LOCK:
        if any(run.finished_at is None for run in bench._RUNS.values()):
            return True
    with iq_review._LOCK:
        # 自动调度线程可能刚取走队列、尚未注册第一个 job；这段间隙仍算在途。
        return bool(iq_review._ACTIVE or iq_review._AUTO_QUEUE
                    or (iq_review._AUTO_THREAD and iq_review._AUTO_THREAD.is_alive()))

class Handler(BaseHTTPRequestHandler):
    """HTTP 路由。

    所有 API 都走 JSON。异常统一转成 4xx/5xx JSON，避免默认的 HTML 错误页把
    内部堆栈吐给页面。
    """

    server_version = "pelican-bench"
    # 不暴露 Python 版本，少给一点指纹信息。
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        """精简访问日志，去掉默认的时间戳前缀。

        @param fmt 格式串
        @param args 格式参数
        """
        print(f"[http] {fmt % args}")

    # ---------- 基础设施 ----------

    def _host_ok(self) -> bool:
        """校验 Host 头是否指向本机回环。

        DNS rebinding 防护：浏览器会把 Host 设成用户输入的域名，恶意域名即使解析到
        127.0.0.1 也会带着自己的 Host 过来，这里据此拒掉。

        @return Host 合法时返回 True
        """
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in _ALLOWED_HOSTS

    def _json(self, payload: Any, status: int = 200) -> None:
        """回一段 JSON。

        @param payload 可序列化对象
        @param status HTTP 状态码
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 这是本地工具页面，不需要被任何站点嵌入或跨源读取。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        """读请求体并解析 JSON。

        限制体积上限，防止本地误操作传个超大文件把内存吃满。

        @return 解析后的字典
        @throws ValueError 体积超限或不是合法 JSON 对象
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        """处理 GET；CLI 请求核对实例，评测历史按固定 ID 访问，不含凭据。"""
        if not self._host_ok():
            self._json({"error": "只允许通过 127.0.0.1 访问"}, 403)
            return
        instance = self.headers.get("X-Pelican-Instance")
        if instance and instance != getattr(self.server, "instance_id", None):
            self._json({"error": "服务实例已变化，请重新执行命令"}, 409)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            # IQ 服务可单独升级；工程任务仍由原进程持有，不能在新进程里误判成中断。
            engineering_port = getattr(self.server, "engineering_port", None)
            if engineering_port and (path == "/static/engineering.html" or path.startswith("/api/swe/")):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{engineering_port}" + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/runtime":
                # CLI 复用已有实例前核对随机标识；此接口不触发账号检查或模型请求。
                self._json({"application": "pelican-bench",
                            "instance": getattr(self.server, "instance_id", None)})
            elif path == "/api/config":
                # 只回掩码后的配置，明文 key 不出进程。
                self._json(config.public_config(config.load()))
            elif path == "/api/runtime/status":
                self._json({"state": "stopping" if getattr(self.server, "stopping", False) else "running",
                            "busy": _evaluation_busy()})
            elif path == "/api/models":
                self._api_models(query)
            elif path == "/api/chatgpt/status":
                self._json(codex_auth.manager.status())
            elif path in ("/api/probe", "/api/chatgpt/probe"):
                self._json(codex_probe.manager.snapshot())
            elif path in ("/api/probe/compare", "/api/probe/history"):
                self._json(codex_probe.manager.archive.listing(
                    view=path.rsplit("/", 1)[1], site=(query.get("site") or [""])[0],
                    model=(query.get("model") or [""])[0], concurrency=(query.get("concurrency") or [""])[0],
                    sort=(query.get("sort") or ["speed"])[0], page=(query.get("page") or [1])[0],
                    page_size=(query.get("page_size") or [20])[0]))
            elif path.startswith("/api/probe/history/"):
                data = codex_probe.manager.archive.get(path.rsplit("/", 1)[1])
                self._json(data if data else {"error": "找不到该测试记录"}, 200 if data else 404)
            elif path == "/api/kimi/status":
                self._json(kimi_channel.manager.status())
            elif path == "/api/iq/suites":
                self._json({"suites": [iq_tasks.manifest(name) for name in
                                      ("bbeh-smoke", "bbeh-mini", "arc-smoke", "arc-agi-2", "quick", "full")]})
            elif path == "/api/swe/environment":
                self._json(swe.environment())
            elif path == "/api/swe/runs":
                self._json({"runs": swe.history()})
            elif path == "/api/swe/leaderboard":
                self._json(swe.leaderboard((query.get("cohort") or [None])[0]))
            elif path.startswith("/api/swe/run/"):
                data = swe.get(path[len("/api/swe/run/"):], full=query.get("full") == ["1"])
                self._json(data if data else {"error": "找不到这次评测"}, 200 if data else 404)
            elif path == "/api/iq/runs":
                self._json({"runs": iq.history()})
            elif path == "/api/iq/leaderboard":
                self._json(iq.leaderboard((query.get("cohort") or [None])[0], (query.get("benchmark") or [None])[0],
                                          (query.get("scoring") or ["reviewed"])[0]))
            elif path == "/api/iq/reviews":
                run_id = (query.get("run_id") or [None])[0]
                self._json({"reviews": iq_review.history(run_id), "automatic": iq_review.automatic(run_id)})
            elif path.startswith("/api/iq/review/"):
                data = iq_review.get(path[len("/api/iq/review/"):])
                self._json(data if data else {"error": "找不到复核记录"}, 200 if data else 404)
            elif path.startswith("/api/iq/run/"):
                data = iq.get(path[len("/api/iq/run/"):], full=query.get("full") == ["1"])
                self._json(data if data else {"error": "找不到这次评测"}, 200 if data else 404)
            elif path == "/api/runs":
                self._json({"runs": bench.list_runs()})
            elif path == "/api/gallery":
                # 首页卡片墙的元数据，不含 SVG 源码，图按格单独取。
                self._json(bench.latest_gallery())
            elif path == "/api/gallery/svg":
                self._api_gallery_svg(query)
            elif path == "/api/gallery/history":
                self._api_model_history(query)
            elif path.startswith("/api/run/"):
                self._api_run_status(path[len("/api/run/"):], query)
            elif path.startswith("/report/"):
                self._serve_report(path[len("/report/"):])
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            # 统一兜底：只回类型和消息，不回堆栈。
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        """串行处理任务创建及账号切换，保证账号切换的空闲检查与实际动作不交错。"""
        with _ACCOUNT_ACTION_LOCK:
            self._dispatch_post()

    def _dispatch_post(self) -> None:
        """在共享操作锁内核对实例、同源和停机状态，避免检查空闲后又创建任务。"""
        if not self._host_ok():
            self._json({"error": "只允许通过 127.0.0.1 访问"}, 403)
            return
        path = urlparse(self.path).path
        try:
            instance = self.headers.get("X-Pelican-Instance")
            if instance and instance != getattr(self.server, "instance_id", None):
                self._json({"error": "服务实例已变化，请重新执行命令"}, 409)
                return
            if getattr(self.server, "stopping", False):
                self._json({"error": "服务正在停止，不再接受操作"}, 503)
                return
            if path in ("/api/run", "/api/config", "/api/probe", "/api/probe/cancel", "/api/cli/site", "/api/runtime/stop") or path.startswith(("/api/chatgpt/", "/api/kimi/")):
                # SVG 与设置同样可触发账号用量或改变路由，不能允许第三方网页借本机登录态代发。
                origin = self.headers.get("Origin")
                if origin and origin != "http://" + self.headers.get("Host", ""):
                    self._json({"error": "不允许跨源操作"}, 403)
                    return
                if self.headers.get_content_type() != "application/json":
                    self._json({"error": "需要 application/json 请求"}, 415)
                    return
            if path == "/api/runtime/stop":
                token = getattr(self.server, "control_token", None)
                supplied = self.headers.get("X-Pelican-Control", "")
                if not token or not secrets.compare_digest(token, supplied):
                    self._json({"error": "仅允许本机 CLI 停止当前实例"}, 403)
                    return
                if _evaluation_busy():
                    self._json({"error": "仍有评测或复核运行中；先 cancel 对应批次，或 stop --wait --timeout 600"}, 409)
                    return
                self.server.stopping = True
                self._json({"state": "stopping"})
                # shutdown 必须在 serve_forever 以外的线程执行；先阻止新任务，再停止监听。
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/cli/site":
                self._api_cli_site()
                return
            if getattr(self.server, "engineering_port", None) and path.startswith("/api/swe/"):
                # 不转发带副作用的请求或绕过原服务的同源检查，请到持有任务的页面操作。
                self._json({"error": "工程任务由原服务管理，请从工程评测入口进入后操作"}, 409)
                return
            if path in ("/api/probe", "/api/probe/cancel", "/api/chatgpt/probe", "/api/chatgpt/probe/cancel"):
                data = self._read_json()
                if not isinstance(data, dict) or set(data) - ({"site", "model", "mode", "targets", "concurrency", "require_new"} if path.endswith("/probe") else {"run_id"}):
                    raise ValueError("测试参数不受支持，请刷新页面后重试")
                if path.endswith("/cancel"):
                    # CLI 按明确 ID 取消；网页旧请求仍允许取消当前任务。
                    if "run_id" in data and data["run_id"] != codex_probe.manager.snapshot().get("id"):
                        self._json({"error": "该批次不是当前任务，未取消其他测试"}, 409)
                        return
                    result = codex_probe.manager.cancel()
                elif "targets" in data:
                    if data.get("require_new") and codex_probe.manager.busy():
                        self._json({"error": "已有测速或容量检测运行中，请等待或取消该批次"}, 409)
                        return
                    # 批量与旧版单项参数必须互斥，避免界面与后端对实际测试范围理解不同。
                    if "site" in data or "model" in data:
                        raise ValueError("批量测试不能同时指定单项站点或模型")
                    result = codex_probe.manager.start_batch(data["targets"], data.get("mode", "capacity"), data.get("concurrency"))
                else:
                    result = codex_probe.manager.start(data.get("model"), data.get("mode", "capacity"), data.get("site"), data.get("concurrency"))
                self._json(result)
            elif path in ("/api/kimi/login", "/api/kimi/cancel", "/api/kimi/logout"):
                data = self._read_json()
                if set(data) - ({"region"} if path.endswith("/login") else set()):
                    raise ValueError("不接受自定义账号、路径、端点或令牌")
                if not path.endswith("/cancel") and _evaluation_busy():
                    self._json({"error": "请先结束当前评测或复核，再登录或退出账号"}, 409)
                    return
                if path.endswith("/login"):
                    result = kimi_channel.manager.start(data.get("region", "mainland-cn"))
                elif path.endswith("/cancel"):
                    result = kimi_channel.manager.cancel()
                else:
                    result = kimi_channel.manager.logout()
                self._json(result)
            elif path in ("/api/chatgpt/login", "/api/chatgpt/cancel", "/api/chatgpt/logout"):
                data = self._read_json()
                if set(data) - ({"kind"} if path.endswith("/login") else set()):
                    raise ValueError("不接受自定义账号、路径、端点或令牌")
                if path != "/api/chatgpt/cancel" and _evaluation_busy():
                    self._json({"error": "请先结束当前评测或复核，再登录或退出账号"}, 409)
                    return
                if path.endswith("/login"):
                    result = codex_auth.manager.start(data.get("kind", "browser"))
                elif path.endswith("/cancel"):
                    result = codex_auth.manager.cancel()
                else:
                    result = codex_auth.manager.logout()
                import codex_channel
                with codex_channel._LOCK:
                    codex_channel._CACHE = None
                self._json(result)
            elif path in ("/api/iq/run", "/api/iq/cancel", "/api/iq/review", "/api/iq/review-run", "/api/iq/retry", "/api/iq/review-settings"):
                self._api_iq_post(path)
            elif path in ("/api/swe/run", "/api/swe/cancel"):
                self._api_iq_post(path)
            elif path == "/api/config":
                self._api_save_config()
            elif path == "/api/run":
                self._api_start_run()
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------- 具体处理 ----------

    def _api_cli_site(self):
        """仅更新指定普通站点，锁内读取最新配置，保留其他站点、提示词和复核设置。"""
        import official_channels
        data = self._read_json()
        if set(data) - {"action", "name", "base_url", "api_key", "models"}:
            raise ValueError("不支持的站点字段")
        name, action = data.get("name"), data.get("action")
        if not isinstance(name, str) or not name.strip() or action not in ("set", "remove"):
            raise ValueError("需要有效站点名和 set/remove 操作")
        name = name.strip()
        if official_channels.reserved({"name": name, "base_url": data.get("base_url")}):
            raise ValueError("内置账号渠道请在设置页登录，不能通过普通站点配置替换")
        with config._LOCK:
            cfg = config.load()
            old = next((s for s in cfg["sites"] if s["name"] == name), None)
            if action == "remove":
                if old is None:
                    raise ValueError("站点不存在")
                cfg["sites"] = [s for s in cfg["sites"] if s["name"] != name]
            else:
                site = {**(old or {}), **{k: v for k, v in data.items() if k not in ("action", "name")}, "name": name}
                parsed = urlparse(str(site.get("base_url", "")))
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("base-url 需要不含凭据、查询参数或片段的 HTTP(S) 地址")
                # 更换路由时不能把旧站点密钥静默发送到新地址；显式传入新 key 才保留。
                if old and site["base_url"].rstrip("/") != old["base_url"].rstrip("/") and "api_key" not in data:
                    site["api_key"] = ""
                if "models" in data and (not isinstance(data["models"], list) or any(not isinstance(m, str) or not m.strip() for m in data["models"])):
                    raise ValueError("models 必须是非空模型名组成的数组")
                if "api_key" in data and (not isinstance(data["api_key"], str) or len(data["api_key"]) > 65536):
                    raise ValueError("api_key 必须为字符串，最多 64 KiB")
                normal = config._normalise_site(site)
                cfg["sites"] = [normal if s["name"] == name else s for s in cfg["sites"]]
                if old is None:
                    cfg["sites"].append(normal)
            config.save(cfg)
            self._json({"sites": config.public_config(cfg)["sites"]})

    def _api_iq_post(self, path: str) -> None:
        """处理评测和复核设置，拒绝跨源网页或表单代发请求。

        @param path 已匹配的固定评测路由，不接受前端指定上游地址或凭据
        """
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self._json({"error": "不允许跨源发起评测"}, 403)
            return
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "需要 application/json 请求"}, 415)
            return
        data = self._read_json()
        if path == "/api/iq/review-settings":
            result = config.save_review_settings(data)
        elif path == "/api/iq/review":
            result = iq_review.start(config.load(), data)
        elif path == "/api/iq/review-run":
            # 升级前已结束的记录也能按同一规则复核；读取历史不触发收费。
            # schedule_run 对同一轮幂等，拒绝运行中的记录，不再次请求被测模型。
            run = iq.get(data.get("run_id"), full=True)
            result = iq_review.schedule_run(config.load(), run) if run else None
        elif path == "/api/iq/retry":
            result = iq.retry_failed(config.load(), data)
        else:
            engine = swe if path.startswith("/api/swe/") else iq
            result = engine.start(config.load(), data) if path.endswith("/run") else engine.cancel(data.get("run_id"))
        self._json(result if result else {"error": "找不到这次评测"}, 200 if result else 404)

    def _serve_static(self, rel: str) -> None:
        """回静态文件。

        rel 来自 URL，必须确认最终路径仍在 static/ 内，否则可以用 ../ 读到
        config.json（里面是明文 key）。

        @param rel 相对 static/ 的路径
        """
        target = os.path.abspath(os.path.join(STATIC_DIR, rel))
        root = os.path.abspath(STATIC_DIR)
        if os.path.commonpath([target, root]) != root or not os.path.isfile(target):
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _api_models(self, query: dict[str, list[str]]) -> None:
        """拉某个站点的模型列表。

        站点用名字指定，base_url 和 key 都从服务端配置取，不接受前端直接传地址，
        这样前端无法借这个接口把请求打到任意地址。

        @param query URL 查询参数，需含 site
        """
        name = (query.get("site") or [""])[0]
        import official_channels
        for channel in official_channels.adapters():
            if name == channel.SITE:
                self._json(channel.catalog(refresh=True))
                return
        cfg = config.load()
        site = next((s for s in cfg["sites"] if s["name"] == name), None)
        if not site:
            self._json({"error": f"找不到站点：{name}"}, 404)
            return
        try:
            models = llm.list_models(site["base_url"], site.get("api_key", ""), timeout=30)
        except llm.LLMError as exc:
            self._json({"error": str(exc), "models": site.get("models") or []}, 200)
            return
        self._json({"models": models})

    def _api_model_history(self, query: dict[str, list[str]]) -> None:
        """读取一个模型组合的历史，不接受客户端指定文件路径。

        @param query 必须包含非空的 site 和 model
        """
        site = (query.get("site") or [""])[0]
        model = (query.get("model") or [""])[0]
        if not site or not model:
            self._json({"error": "缺少 site / model"}, 400)
            return
        self._json(bench.model_history(site, model))

    def _api_gallery_svg(self, query: dict[str, list[str]]) -> None:
        """取首页某一格的 SVG 源码。

        用 JSON 包一层而不是直接回 image/svg+xml：SVG 文档带 Content-Type
        image/svg+xml 时，浏览器把它当成可执行的文档上下文，用户把这个地址
        直接开成一个标签页就等于在本地源上执行模型给的内容。包成 JSON 之后
        它只能被前端当字符串取回、塞进 shadow root 渲染，没有独立文档上下文。
        内容本身在跑批阶段已经过 svg_sanitize 清洗。

        @param query URL 查询参数，需含 run_id、site、model
        """
        run_id = (query.get("run_id") or [""])[0]
        site = (query.get("site") or [""])[0]
        model = (query.get("model") or [""])[0]
        if not run_id or not site or not model:
            self._json({"error": "缺少 run_id / site / model"}, 400)
            return
        svg = bench.run_svg(run_id, site, model)
        if not svg:
            self._json({"error": "没有找到对应的图"}, 404)
            return
        self._json({"svg": svg})

    def _api_save_config(self) -> None:
        """保存设置页提交的配置。

        空 key 沿用原值；该表单不负责复核配置，不能用缺失或过期的字段覆盖它。
        与复核设置共用读改写锁，避免并发保存丢失配置。非法配置抛出 ValueError。
        """
        incoming = self._read_json()
        with config._LOCK:
            old = config.load()
            merged = config.merge_secrets(incoming, old)
            # 只覆盖该表单负责的字段；其余能力的配置必须取锁内最新值，不能用前端旧快照。
            normalised = {
                **old,
                "prompt": str(merged.get("prompt") or ""),
                "concurrency": int(merged.get("concurrency") or 4),
                "timeout_seconds": int(merged.get("timeout_seconds") or 180),
                "max_tokens": int(merged.get("max_tokens") or 8000),
                "temperature": float(merged.get("temperature") or 0.7),
                "pricing": config._normalise_pricing(merged.get("pricing")),
                "sites": [config._normalise_site(s) for s in merged.get("sites") or [] if isinstance(s, dict)],
            }
            config.save(normalised)
            result = config.public_config(config.load())
        self._json(result)

    def _api_start_run(self) -> None:
        """启动一次跑批。

        @throws ValueError tasks 为空或结构不对
        """
        data = self._read_json()
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError("没有选择任何模型")
        tasks = [
            {"site": str(t.get("site") or ""), "model": str(t.get("model") or "")}
            for t in raw_tasks
            if isinstance(t, dict) and t.get("site") and t.get("model")
        ]
        if not tasks:
            raise ValueError("选择的模型无效")
        run = bench.start_run(config.load(), tasks)
        self._json({"run_id": run.run_id, "total": run.total})

    def _api_run_status(self, run_id: str, query: dict[str, list[str]]) -> None:
        """查跑批进度或结果。

        内存里没有就去磁盘找，这样重启后历史批次仍可查。

        @param run_id 跑批 id
        @param query 查询参数，full=1 时带 SVG
        """
        full = (query.get("full") or ["0"])[0] == "1"
        run = bench.get_run(run_id)
        if run:
            self._json(run.snapshot(include_svg=full))
            return
        data = bench.load_run_file(run_id)
        if not data:
            self._json({"error": "找不到这次跑批"}, 404)
            return
        if not full:
            for r in data.get("results") or []:
                r.pop("svg", None)
                r.pop("raw", None)
        self._json(data)

    def _serve_report(self, run_id: str) -> None:
        """生成并回一份 HTML 报告。

        run_id 会参与拼路径，交给 bench.load_run_file 做白名单校验；
        内存中的批次直接用内存快照，避免必须等落盘完成才能看报告。

        @param run_id 跑批 id
        """
        run = bench.get_run(run_id)
        snapshot = run.snapshot(include_svg=True) if run else bench.load_run_file(run_id)
        if not snapshot:
            self._json({"error": "找不到这次跑批"}, 404)
            return
        html_text = report.render(snapshot, interactive=True)
        # 批次已结束时顺手落一份到磁盘，方便用户直接把文件发出去。
        if not snapshot.get("running"):
            try:
                report.write(snapshot)
            except OSError as exc:
                print(f"[warn] 报告落盘失败：{exc}")
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main(argv=None, on_listening=None, instance_id=None, control_token=None) -> None:
    """绑定回环端口后恢复历史再提供服务，供源码入口及 npm 启动器共用。

    Args:
        argv: 命令行参数列表；None 时读取进程参数，端口 0 由系统分配。
        on_listening: 可选回调，端口绑定且恢复完成后接收实际首页 URL。
        instance_id: 启动器生成的实例标识，仅用于识别本机服务，不是认证令牌。
        control_token: 启动器保存在私有文件的随机停机令牌；原始 app.py 入口不提供 CLI 停机。
    """
    parser = argparse.ArgumentParser(description="鹈鹕骑自行车 · 多站点多模型跑批")
    parser.add_argument("--port", type=int, default=8777, help="监听端口，默认 8777")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--engineering-port", type=int, help="将工程页面交给已运行的本机端口，当前进程不接受工程任务操作")
    parser.add_argument("--active-engineering-port", type=int, help="仅回读旧端口尚未结束的工程任务，新任务使用当前执行器")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port 必须在 0 到 65535 之间")
    if args.engineering_port is not None and (not 1 <= args.engineering_port <= 65535 or args.engineering_port == args.port):
        parser.error("engineering-port 必须是不同于监听端口的有效端口")
    if args.active_engineering_port is not None and (not 1 <= args.active_engineering_port <= 65535 or args.active_engineering_port == args.port):
        parser.error("active-engineering-port 必须是不同于监听端口的有效端口")
    if args.engineering_port and args.active_engineering_port:
        parser.error("两种工程升级模式不能同时使用")
    swe.ACTIVE_PORT = args.active_engineering_port

    os.makedirs(config.RUNS_DIR, exist_ok=True)
    # 地址写死 127.0.0.1，不提供 --host 参数：这个服务持有 api key，
    # 留一个能绑 0.0.0.0 的开关就等于给自己留个泄露入口。
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.instance_id = instance_id
    server.control_token = control_token
    server.stopping = False
    codex_probe.manager.restore_history()
    server.engineering_port = args.engineering_port
    url = f"http://127.0.0.1:{server.server_port}/"
    if on_listening:
        on_listening(url)
    print(f"起来了：{url}（Ctrl+C 停）")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停了")
    finally:
        codex_probe.manager.cancel()
        server.server_close()
        codex_auth.manager.close()
        kimi_channel.manager.close()


if __name__ == "__main__":
    main()
