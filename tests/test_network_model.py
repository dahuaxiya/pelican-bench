"""Exercise attributed model tunnels and rankings without sending real model requests."""
import contextlib
import io
import json
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import cli_commands as cli
import network_model as model
import network_probe as network
from network_transport import Cancellation, read_headers
from test_network import FakeController


class ProxyHandler(socketserver.BaseRequestHandler):
    """Stand in for a local proxy, observing exactly what the relay forwards."""

    def handle(self):
        """Return mock TLS bytes only after the client's hello, then capture later data."""
        self.request.settimeout(2)
        try:
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                chunk = self.request.recv(1)
                if not chunk:
                    return
                header.extend(chunk)
            self.server.headers.append(bytes(header))
            self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
            self.server.peer = self.client_address[1]
            self.server.hello = self.request.recv(64)
            self.request.sendall(b"server-hello")
            self.server.after_hello = self.request.recv(64)
        except OSError:
            pass
        finally:
            self.server.finished.set()


class ModelTunnelTests(unittest.TestCase):
    """Use real loopback sockets to check attribution before application traffic."""

    def setUp(self):
        """Start a temporary proxy; all listener and worker threads are reclaimed."""
        self.proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ProxyHandler)
        self.proxy.headers, self.proxy.after_hello = [], b""
        self.proxy.finished = threading.Event()
        self.thread = threading.Thread(target=self.proxy.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.cleanup_proxy)

    def cleanup_proxy(self):
        """Bound test cleanup even when an assertion interrupts a tunnel."""
        self.proxy.shutdown()
        self.proxy.server_close()
        self.thread.join(2)

    def relay(self, controller=None):
        """Create a relay with a fixed group and register mandatory cleanup."""
        relay = model.Relay(f"http://127.0.0.1:{self.proxy.server_address[1]}",
                            controller or Mock(), "a", "group", Cancellation(), 3)
        self.addCleanup(relay.close)
        return relay

    def connect(self, relay, host="chatgpt.com"):
        """Connect through the real relay and return the parsed HTTP status."""
        sock = socket.create_connection(("127.0.0.1", urlparse(relay.url).port), timeout=2)
        self.addCleanup(sock.close)
        sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
        status, _ = read_headers(sock, time.monotonic() + 2)
        return sock, status

    def test_each_connection_is_attributed_before_server_bytes(self):
        """No server hello reaches the client until this socket's node is verified."""
        controller, reached, release = Mock(), threading.Event(), threading.Event()

        def verify(port, node, group):
            """Hold attribution so the test can detect premature forwarding."""
            self.assertEqual((port, node, group), (self.proxy.peer, "a", "group"))
            reached.set()
            self.assertTrue(release.wait(1))

        controller.verify_route.side_effect = verify
        relay = self.relay(controller)
        sock, status = self.connect(relay)
        self.assertEqual(status, 200)
        sock.sendall(b"client-hello")
        self.assertTrue(reached.wait(1))
        sock.settimeout(.05)
        with self.assertRaises(TimeoutError):
            sock.recv(64)
        release.set()
        sock.settimeout(1)
        self.assertEqual(sock.recv(64), b"server-hello")
        sock.sendall(b"application-data")
        self.assertTrue(self.proxy.finished.wait(1))
        self.assertEqual(self.proxy.after_hello, b"application-data")
        self.assertEqual(relay.verified, 1)

    def test_wrong_route_and_other_hosts_are_blocked(self):
        """Reject nonofficial destinations and withhold all unverified server bytes."""
        controller = Mock()
        controller.verify_route.side_effect = ValueError("route_mismatch")
        relay = self.relay(controller)
        _, status = self.connect(relay, "example.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.proxy.headers, [])
        sock, status = self.connect(relay)
        sock.sendall(b"client-hello")
        self.assertEqual(sock.recv(64), b"")
        self.assertTrue(self.proxy.finished.wait(1))
        self.assertEqual(self.proxy.after_hello, b"")
        self.assertEqual(relay.failure, "route_unverified")
        self.assertEqual(relay.verified, 0)

    def test_close_interrupts_idle_connections(self):
        """Closing a sample promptly drains even a client stalled before CONNECT."""
        relay = self.relay()
        sock = socket.create_connection(relay.server_address, timeout=1)
        self.addCleanup(sock.close)
        sock.sendall(b"CONNECT ")
        started = time.monotonic()
        relay.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(relay.thread.is_alive())
        self.assertFalse(relay.workers)


class ModelSampleTests(unittest.TestCase):
    """Keep measured usage separate from answer content and inferred timing."""

    def sample(self, response, verified=1, failure=None):
        """Inject a completed official response without credentials or network IO."""
        relay = Mock(verified=verified, failure=failure, url="http://127.0.0.1:12345")
        with patch.object(model, "Relay", return_value=relay), patch.object(model.codex_stream, "complete", return_value=response) as request:
            result = model.probe_model("http://127.0.0.1:7890", Mock(), "a", "group", Cancellation(), "gpt-test", 180)
        self.assertEqual(request.call_args.kwargs["proxy_url"], relay.url)
        relay.close.assert_called_once()
        return result

    def response(self):
        """Return upstream counts and body timing with enough text for a throughput sample."""
        return {"completed": True, "text": "private-answer " * 100, "ttft": 2,
                "response_model": "gpt-test", "usage": {"completion_tokens": 600, "cached_tokens": 200},
                "performance": {"total_seconds": 11, "response_seconds": 10}}

    def test_actual_usage_and_missing_metrics(self):
        """Report measured counts and first deltas; never store the generated answer."""
        response = self.response()
        result = self.sample(response)
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["end_to_end_tokens_per_second"], 60)
        self.assertEqual(result["metrics"]["ttft_seconds"], 2)
        self.assertNotIn("private-answer", json.dumps(result))
        response.update(usage={}, ttft=None)
        result = self.sample(response)
        self.assertIsNone(result["metrics"]["end_to_end_tokens_per_second"])
        self.assertIsNone(result["metrics"]["ttft_seconds"])

    def test_reroute_and_unverified_connections_never_succeed(self):
        """A different actual model or node cannot supply a valid comparison sample."""
        response = self.response()
        self.assertFalse(self.sample(response, verified=0)["ok"])
        self.assertFalse(self.sample(response, failure="route_unverified")["ok"])
        response["response_model"] = "gpt-other"
        self.assertFalse(self.sample(response)["ok"])

    def test_ranking_requires_comparable_metrics_and_prioritizes_reliability(self):
        """Criterion changes only valid complete samples; partial and invalid data remain unranked."""
        def row(name, ttft, speed, count=2, failed=False):
            """Produce equal samples except when explicitly testing a missing or failed one."""
            samples = [{"ok": True, "elapsed_ms": 10000, "metrics": {
                "ttft_seconds": ttft, "end_to_end_tokens_per_second": speed}} for _ in range(count)]
            if failed:
                samples[-1] = {"ok": False, "elapsed_ms": 10000}
            return {"name": name, "samples": samples}
        rows = [row("fast-start", 0, 30), row("fast-output", 2, 60), row("unreliable", 0, 300, failed=True),
                row("partial", 0, 500, count=1), row("unknown", 1, None), row("invalid", True, 500), row("infinite", 1, float("inf"))]
        options = {"mode": "model", "repeats": 2, "sort_by": "ttft"}
        ranked = network.rank_results(rows, options)
        self.assertEqual([r["name"] for r in ranked[:3]], ["fast-start", "fast-output", "unreliable"])
        self.assertTrue(all(r["rank"] is None for r in ranked[3:]))
        options["sort_by"] = "throughput"
        self.assertEqual(network.rank_results(rows, options)[0]["name"], "fast-output")


class ModelJobTests(unittest.TestCase):
    """Exercise model mode with real history and selector restoration."""

    def setUp(self):
        """Keep mutations within an isolated controller and temporary history directory."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.manager = network.NetworkManager(root, root / "leases")
        self.manager.configure({"group": "group"})
        self.addCleanup(self.manager.close)
        self.controller = FakeController()

    def run_job(self, sample, **overrides):
        """Run isolated lifecycle checks, simulating transport route admission."""
        def attributed(*args, route_guard, **kwargs):
            """Release the route before returning a fake model response or first delta."""
            with route_guard():
                pass
            return sample(*args, **kwargs)

        with patch.object(network, "Controller", return_value=self.controller), \
                patch.object(network, "load_auth", return_value={}), \
                patch.object(model, "catalog", return_value={"ready": True, "models": ["gpt-test"]}), \
                patch.object(model, "probe_model", side_effect=attributed):
            result = self.manager.start({"mode": "model", "model": "gpt-test", "top": 2, "repeats": 2, "concurrency": 1, **overrides})
            self.manager.thread.join(3)
            self.assertFalse(self.manager.busy())
            return self.manager.get(result["id"])

    def test_equal_model_rounds_progress_restore_and_apply(self):
        """All nodes use one model and round count; apply follows the stored metric criterion."""
        seen = []

        def sample(proxy, controller, node, group, cancel, selected_model, timeout, on_output):
            """Emit a first delta and deterministic measurements for each node."""
            seen.append((node, selected_model, timeout))
            on_output({"kind": "text_delta", "elapsed_seconds": 1})
            row = next(r for r in self.manager.snapshot()["rows"] if r["name"] == node)
            self.assertEqual(row["output_observation"]["elapsed_seconds"], 1)
            return {"ok": True, "stage": "model", "elapsed_ms": 10000, "metrics": {
                "ttft_seconds": 1, "end_to_end_tokens_per_second": {"a": 20, "b": 80, "original": 50}[node]}}

        result = self.run_job(sample, sort_by="throughput")
        self.assertEqual(result["status"], "completed")
        self.assertEqual([s[0] for s in seen], ["a", "b", "original"] * 2)
        self.assertTrue(all(s[1:] == ("gpt-test", 180) for s in seen))
        self.assertEqual(self.controller.current, "original")
        self.assertEqual(result["recommendations"], ["b", "original"])
        with patch.object(network, "Controller", return_value=self.controller), patch.object(network, "load_auth", return_value={}), \
                patch.object(network, "probe", return_value={"ok": True, "stage": "websocket", "status": 101, "elapsed_ms": 20}) as handshake:
            self.manager.apply_best(result["id"])
            self.manager.thread.join(3)
        self.assertEqual(self.controller.current, "b")
        self.assertEqual(handshake.call_args.args[-1], 60)

    def test_capacity_stops_batch_and_restores(self):
        """Account-wide capacity failures are not multiplied across every node."""
        sample = Mock(return_value={"ok": False, "stage": "model", "elapsed_ms": 10, "error": "[capacity] upstream capacity"})
        result = self.run_job(sample)
        self.assertEqual(result["status"], "failed")
        sample.assert_called_once()
        self.assertEqual(self.controller.current, "original")
        self.assertEqual(result["recommendations"], [])

    def test_invalid_model_is_rejected_without_switching(self):
        """A model outside the account catalog cannot create a task or mutate the selector."""
        with self.assertRaises(ValueError):
            self.run_job(Mock(), model="unknown")
        self.assertEqual(self.controller.switches, [])
        self.assertEqual(self.manager.history()["total"], 0)

    def test_cli_passes_model_settings_and_requires_explicit_model(self):
        """Model testing shares the backend and requires an intentional model selection."""
        client = Mock()
        client.request.return_value = {"id": "a" * 12, "status": "running"}
        args = cli.parser().parse_args(["vpn", "test", "--mode", "model", "--model", "gpt-test", "--sort-by", "throughput", "--no-wait", "--json"])
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.execute_vpn(args, client), 0)
        body = client.request.call_args.args[1]
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["sort_by"], "throughput")
        self.assertEqual(body["concurrency"], 6)
        self.assertNotIn("timeout", body)
        self.assertEqual(json.loads(output.getvalue())["id"], "a" * 12)
        args.model = None
        with self.assertRaises(ValueError):
            cli.execute_vpn(args, client)
