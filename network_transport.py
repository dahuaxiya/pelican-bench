"""Local Clash control and authenticated Codex handshakes, without model input."""
from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlparse, quote, urlencode

HOST = "chatgpt.com"
ENDPOINT = "/backend-api/codex/responses"
BETA = "responses_websockets=2026-02-06"


def local_address(value):
    """Accept only a literal loopback HTTP endpoint; never resolve arbitrary DNS."""
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("需要本机 HTTP 地址")
    try:
        url = urlparse(value)
        port = url.port
        if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1")
                or not port or url.username is not None or url.password is not None
                or url.path not in ("", "/") or url.params or url.query or url.fragment):
            raise ValueError()
    except ValueError:
        raise ValueError("只支持 http://127.0.0.1:端口 或 IPv6 回环地址") from None
    return ("127.0.0.1" if url.hostname == "localhost" else url.hostname, port)


def header_value(value):
    """Reject control characters before interpolating locally stored credentials."""
    if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("认证信息不可用，请重新登录")
    return value


def load_auth():
    """Use the website account when selected, including after explicit logout."""
    import codex_auth
    root = codex_auth.selected_home() or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        tokens = json.loads((root / "auth.json").read_text())["tokens"]
        return {key: header_value(tokens[key]) for key in ("access_token", "account_id")}
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("需要 ChatGPT 登录态，请到设置登录；API Key 不适用") from None


class Controller:
    """A bounded local-only client; no redirects or environment proxy inheritance."""

    def __init__(self, settings):
        """Keep the controller secret in memory; public results never include it."""
        self.address = local_address(settings["controller"])
        self.secret = settings.get("secret", "")
        if self.secret:
            header_value(self.secret)

    def request(self, path, body=None, timeout=3):
        """Call only internally built controller paths and discard raw error bodies."""
        connection = http.client.HTTPConnection(*self.address, timeout=timeout)
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        try:
            connection.request("GET" if body is None else "PUT", path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            if response.status not in (200, 204):
                raise ValueError(f"控制接口 HTTP {response.status}，请检查地址和控制密钥")
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("控制接口响应过大")
            return json.loads(raw) if raw else {}
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            raise ValueError("无法读取本机 Clash/Mihomo 控制接口") from None
        finally:
            connection.close()

    def proxies(self):
        """Read the live proxy catalog, not a subscription file."""
        return self.request("/proxies")["proxies"]

    def selection(self, group):
        """Read one exact selector; missing or non-manual groups are unsupported."""
        result = self.request("/proxies/" + quote(group, safe=""))
        if result.get("type") != "Selector" or not isinstance(result.get("now"), str):
            raise ValueError("请选择可手动切换的 Selector 组")
        return result["now"]

    def select(self, group, node):
        """Switch a single group; never clear connections or change proxy rules."""
        self.request("/proxies/" + quote(group, safe=""), {"name": node})

    def delay(self, node):
        """HTTPS prescreen is independent of selection and never proves WS support."""
        try:
            data = self.request("/proxies/" + quote(node, safe="") + "/delay?" + urlencode({
                "url": "https://chatgpt.com/cdn-cgi/trace", "timeout": 8000}), timeout=9)
            value = data.get("delay")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return {"delay_ms": value}
            return {"delay_ms": None, "error": "https_unavailable"}
        except ValueError:
            return {"delay_ms": None, "error": "https_unavailable"}

    def verify_route(self, source_port, node, group):
        """Attribute this socket, not an unrelated existing ChatGPT connection."""
        for connection in self.request("/connections", timeout=2).get("connections", []):
            meta = connection.get("metadata", {})
            if str(meta.get("sourcePort")) == str(source_port) and meta.get("host") == HOST:
                chain = connection.get("chains", [])
                if node in chain and group in chain:
                    return
                raise ValueError("route_mismatch")
        raise ValueError("route_unverified")


class Cancellation:
    """Interrupt every worker socket as well as future samples on cancellation."""

    def __init__(self):
        """A lock closes the race between socket registration and cancellation."""
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.sockets = {}

    def check(self):
        """Raise before starting any further work after the cancellation flag."""
        if self.event.is_set():
            raise InterruptedError("cancelled")

    def attach(self, sock):
        """Replace this worker's raw/TLS socket without losing other active workers."""
        with self.lock:
            self.check()
            self.sockets[threading.get_ident()] = sock

    def detach(self):
        """Forget only this worker's socket; cancellation remains latched globally."""
        with self.lock:
            self.sockets.pop(threading.get_ident(), None)

    def cancel(self):
        """Wake all blocked TLS/reads; route recovery uses a separate connection."""
        with self.lock:
            self.event.set()
            for sock in self.sockets.values():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def remaining(deadline):
    """Apply one total deadline across CONNECT, TLS and response headers."""
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError()
    return value


def read_headers(sock, deadline):
    """Bound response size; return headers only, never capture response bodies."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        sock.settimeout(remaining(deadline))
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError()
        data.extend(chunk)
        if len(data) > 32768:
            raise ValueError("header_too_large")
    lines = data.decode("iso-8859-1").split("\r\n")
    protocol, status, *_ = lines[0].split()
    if protocol not in ("HTTP/1.1", "HTTP/1.0") or not status.isdigit():
        raise ValueError("invalid_http")
    fields = {}
    for line in lines[1:]:
        if not line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        fields[key] = fields[key] + "," + value.strip() if key in fields else value.strip()
    return int(status), fields


def handshake_attributed(tls, controller, source_port, node, group, cancel, deadline, binding, result):
    """Release selector ownership as soon as the proxy confirms this socket's outbound.

    Clash may acknowledge CONNECT before choosing an outbound. Sending ClientHello
    triggers dialing; the controller's connection chain then pins the route even
    if the remote TLS response is slow. No credentials are sent until TLS completes.
    """
    complete = attributed = False
    attribution_time = 0
    tls.settimeout(0)
    while not (complete and attributed):
        cancel.check()
        want_write = False
        if not complete:
            try:
                tls.do_handshake()
                complete = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLWantWriteError:
                want_write = True
        if not attributed:
            result["stage"] = "route"
            check_started = time.monotonic()
            try:
                controller.verify_route(source_port, node, group)
            except ValueError as exc:
                # An absent connection can mean dialing is still in progress.
                # A known but wrong route is fatal and must never receive auth.
                if str(exc) != "route_unverified":
                    raise
            else:
                binding.close()
                attributed = True
            finally:
                attribution_time += time.monotonic() - check_started
        if complete and attributed:
            break
        result["stage"] = "tls" if attributed else "route"
        delay = min(.1, remaining(deadline))
        select.select([] if want_write or complete else [tls], [tls] if want_write else [], [], delay)
    return attribution_time


def probe(auth, proxy, controller, node, group, cancel, timeout=12, route_guard=None):
    """Measure an authenticated upgrade only after TLS and route attribution.

    Tokens can only reach the fixed official host. No model input is sent. Timings
    exclude the local controller attribution call, but include TCP, CONNECT, TLS
    and the complete validated upgrade. route_guard serializes only route binding;
    the authenticated upgrade runs concurrently once the socket is attributed.
    """
    started = time.monotonic()
    deadline = started + timeout
    attribution_time = 0
    result = {"ok": False, "stage": "proxy_tcp", "tested_at": time.time()}
    binding = ExitStack()
    try:
        cancel.check()
        if route_guard:
            binding.enter_context(route_guard())
            # Waiting for another socket's route is scheduling time, not network
            # latency. The sample deadline and stopwatch start after admission.
            result["route_wait_ms"] = round((time.monotonic() - started) * 1000, 2)
            started = time.monotonic()
            deadline = started + timeout
            result["tested_at"] = time.time()
        with socket.create_connection(local_address(proxy), timeout=min(3, remaining(deadline))) as sock:
            cancel.attach(sock)
            result["stage"] = "proxy_connect"
            sock.settimeout(remaining(deadline))
            sock.sendall(f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n\r\n".encode())
            status, _ = read_headers(sock, deadline)
            if status != 200:
                result.update(proxy_status=status, error="proxy_connect_failed")
                return result
            source_port = sock.getsockname()[1]
            result["stage"] = "tls"
            context = ssl.create_default_context()
            # Register the SSL socket before handshake so cancellation can interrupt it.
            with context.wrap_socket(sock, server_hostname=HOST, do_handshake_on_connect=False) as tls:
                cancel.attach(tls)
                attribution_time = handshake_attributed(tls, controller, source_port, node, group,
                                                        cancel, deadline, binding, result)
                result["stage"] = "websocket"
                key = base64.b64encode(os.urandom(16)).decode()
                headers = {"Host": HOST, "Upgrade": "websocket", "Connection": "Upgrade",
                           "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13",
                           "Authorization": "Bearer " + header_value(auth["access_token"]),
                           "ChatGPT-Account-ID": header_value(auth["account_id"]),
                           "OpenAI-Beta": BETA, "User-Agent": "pelican-bench-node-probe"}
                request = f"GET {ENDPOINT} HTTP/1.1\r\n" + "".join(k + ": " + v + "\r\n" for k, v in headers.items()) + "\r\n"
                tls.settimeout(remaining(deadline))
                tls.sendall(request.encode("ascii"))
                status, fields = read_headers(tls, deadline)
                result["status"] = status
                expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                result["ok"] = (status == 101 and fields.get("upgrade", "").lower() == "websocket"
                                and "upgrade" in [v.strip().lower() for v in fields.get("connection", "").split(",")]
                                and fields.get("sec-websocket-accept") == expected)
                if not result["ok"]:
                    result["error"] = "upgrade_rejected" if status != 101 else "upgrade_validation_failed"
                result["elapsed_ms"] = round((time.monotonic() - started - attribution_time) * 1000, 2)
                if result["ok"]:
                    mask = os.urandom(4)
                    payload = bytes(v ^ mask[i % 4] for i, v in enumerate(b"\x03\xe8"))
                    try:
                        tls.settimeout(min(1, remaining(deadline)))
                        tls.sendall(b"\x88\x82" + mask + payload)
                    except OSError:
                        pass  # A failed close cannot erase an already validated handshake.
    except Exception as exc:
        result["error"] = "cancelled" if cancel.event.is_set() else type(exc).__name__
    finally:
        try:
            binding.close()
        finally:
            cancel.detach()
            result.setdefault("elapsed_ms", round((time.monotonic() - started - attribution_time) * 1000, 2))
    return result
