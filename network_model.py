"""Per-sample official model requests through an attributed local CONNECT relay."""
from contextlib import ExitStack
import select
import socket
import socketserver
import threading
import time

import codex_auth
import codex_channel
import codex_stream
import llm
from codex_probe import PERFORMANCE_PROMPT, performance_metrics, _failure_category
from network_transport import HOST, local_address, read_headers, remaining


def catalog():
    """Use the website's isolated ChatGPT account so real deltas are observable."""
    if codex_auth.selected_home() is None:
        return {"ready": False, "models": [], "error": "请先在本站设置中登录 ChatGPT，才能测量模型首字耗时"}
    return codex_channel.catalog()


class Relay(socketserver.ThreadingTCPServer):
    """A short-lived loopback tunnel pins this sample's traffic to the chosen proxy.

    TLS stays end to end between the official client and ChatGPT. No credentials
    or model content are decrypted or logged. Every new connection is attributed
    before the upstream TLS handshake response is passed back to the client.
    """
    daemon_threads = True
    block_on_close = False

    def __init__(self, proxy, controller, node, group, cancel, timeout, route_guard=None):
        """Bind a private port; route_guard must protect every connection, including retries."""
        self.proxy = local_address(proxy)
        self.controller, self.node, self.group = controller, node, group
        self.cancel, self.deadline = cancel, time.monotonic() + timeout
        self.lock, self.closed = threading.Lock(), threading.Event()
        self.sockets, self.workers = set(), set()
        self.verified, self.failure = 0, None
        self.route_guard = route_guard
        super().__init__(("127.0.0.1", 0), Tunnel)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        """Return the per-sample loopback address passed only to its new CLI subprocess."""
        return f"http://127.0.0.1:{self.server_address[1]}"

    def track(self, sock):
        """Register sockets before blocking so closing a sample interrupts all tunnels."""
        with self.lock:
            if self.closed.is_set():
                sock.close()
                raise InterruptedError()
            self.sockets.add(sock)

    def check(self):
        """Enforce the sample's total deadline and cancellation during bidirectional IO."""
        if self.closed.is_set():
            raise InterruptedError()
        self.cancel.check()
        remaining(self.deadline)

    def close(self):
        """Drain this relay only; never close other VPN connections or CLI processes."""
        self.closed.set()
        self.shutdown()
        self.server_close()
        with self.lock:
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            workers = list(self.workers)
        for worker in workers:
            worker.join(3)
        self.thread.join(1)


class Tunnel(socketserver.BaseRequestHandler):
    """Forward encrypted streams to one fixed official host without acting as an open proxy."""

    def handle(self):
        """Verify each actual upstream socket before forwarding server TLS bytes.

        Clash's CONNECT acknowledgement may precede its upstream connection. The
        first ClientHello triggers dialing; a tracked connection can release the
        route lock before slow server TLS bytes arrive. Server bytes are always
        withheld until attribution, so unverified paths never receive account data.
        """
        relay, upstream = self.server, None
        binding = ExitStack()
        worker = threading.current_thread()
        with relay.lock:
            relay.workers.add(worker)
        try:
            relay.track(self.request)
            self.request.settimeout(min(3, remaining(relay.deadline)))
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                relay.check()
                chunk = self.request.recv(1)
                if not chunk or len(header) >= 16384:
                    raise ValueError("invalid_connect")
                header.extend(chunk)
            if bytes(header).split(b"\r\n", 1)[0] not in (
                    f"CONNECT {HOST}:443 HTTP/1.1".encode(), f"CONNECT {HOST}:443 HTTP/1.0".encode()):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            if relay.route_guard:
                # The official client may reconnect after another sample switched
                # the selector. Bind every tunnel, not just the first model request.
                binding.enter_context(relay.route_guard(check=relay.check))
            relay.check()
            upstream = socket.create_connection(relay.proxy, timeout=min(3, remaining(relay.deadline)))
            relay.track(upstream)
            upstream.sendall(f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n\r\n".encode())
            status, _ = read_headers(upstream, relay.deadline)
            if status != 200:
                raise ConnectionError("proxy_connect_failed")
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            verified = hello_sent = False

            def attribute(allow_pending=False):
                """Pin this tunnel as soon as it appears; mismatches stop before auth."""
                try:
                    relay.controller.verify_route(upstream.getsockname()[1], relay.node, relay.group)
                    binding.close()
                except ValueError as exc:
                    if allow_pending and str(exc) == "route_unverified":
                        return False
                    with relay.lock:
                        relay.failure = "route_unverified"
                    raise
                with relay.lock:
                    relay.verified += 1
                return True

            while True:
                relay.check()
                readable, _, _ = select.select([self.request, upstream], [], [], .1)
                if hello_sent and not verified and not readable:
                    # Do not hold the selector while a correctly routed upstream
                    # stalls on TLS. Its established tunnel already pins the node.
                    verified = attribute(allow_pending=True)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    if source is upstream and not verified:
                        verified = attribute()
                    destination = self.request if source is upstream else upstream
                    destination.settimeout(min(2, remaining(relay.deadline)))
                    destination.sendall(data)
                    if source is self.request:
                        hello_sent = True
        except (OSError, ValueError, InterruptedError):
            # A normal client EOF and transient transport failures are handled by
            # the official client. A route mismatch remains fatal even after retry.
            pass
        finally:
            try:
                binding.close()
            finally:
                with relay.lock:
                    relay.sockets.discard(self.request)
                    if upstream:
                        relay.sockets.discard(upstream)
                    relay.workers.discard(worker)
                if upstream:
                    upstream.close()


def probe_model(proxy, controller, node, group, cancel, model, timeout, on_output=None, route_guard=None):
    """Measure one model request; each new tunnel uses route_guard before forwarding TLS."""
    started = time.monotonic()
    result = {"ok": False, "stage": "model", "tested_at": time.time(), "model": model}
    relay = Relay(proxy, controller, node, group, cancel, timeout, route_guard=route_guard)
    try:
        response = codex_stream.complete(model, PERFORMANCE_PROMPT, timeout, cancel.event,
                                         on_output=on_output, proxy_url=relay.url)
        cancel.check()
        metrics = performance_metrics(response, time.monotonic() - started, "performance")
        result.update(metrics=metrics, response_model=response.get("response_model") or model,
                      transport=response.get("transport"), diagnostics=response.get("transport_diagnostics", {}))
        if relay.failure or not relay.verified:
            result.update(stage="route", error="模型连接没有确认经过所选节点")
        elif response.get("response_model") and response["response_model"] != model:
            result["error"] = "上游更换了模型，本次不参与节点排名"
        elif not response.get("completed") or response.get("error"):
            result["error"] = "模型响应未完整结束"
        else:
            result["ok"] = True
    except llm.LLMCancelled:
        raise InterruptedError() from None
    except llm.LLMError as exc:
        result["error"] = _failure_category(str(exc))
    finally:
        relay.close()
        if relay.failure:
            result.update(ok=False, stage="route", error="模型连接没有确认经过所选节点")
        result["verified_connections"] = relay.verified
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result
