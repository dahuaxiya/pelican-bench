"""Node selection recovery and measurement validity, without contacting real nodes."""
import contextlib
import base64
import hashlib
import io
import json
import socket
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cli_commands as cli
import network_probe as network
import network_transport as transport


class Wire:
    """Simulate both CONNECT and TLS bytes so upgrade validation is exercised."""

    def __init__(self, valid=True, status=101):
        """Prepare a connection that can deliberately forge an invalid WS accept."""
        self.valid, self.status = valid, status
        self.response, self.requests = b"", []

    def __enter__(self):
        """Use the same observable transport before and after the TLS wrapper."""
        return self

    def __exit__(self, *args):
        """The fake has no OS resources to close."""

    def settimeout(self, timeout):
        """Allow nonblocking TLS polling; all blocking operations retain a deadline."""
        assert timeout >= 0

    def getsockname(self):
        """Expose a fixed source port for controller attribution."""
        return ("127.0.0.1", 34567)

    def do_handshake(self):
        """TLS succeeds here; failure/timeout tests exercise later protocol checks."""

    def sendall(self, data):
        """Generate a standards-compliant upgrade only for the received nonce."""
        self.requests.append(data)
        if data.startswith(b"CONNECT"):
            self.response = b"HTTP/1.1 200 OK\r\n\r\n"
        elif data.startswith(b"GET"):
            key = next(line.split(b": ", 1)[1] for line in data.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode() if self.valid else "forged"
            self.response = (f"HTTP/1.1 {self.status} Upgrade\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode()

    def recv(self, count):
        """Consume bounded bytes exactly as a stream socket would."""
        value, self.response = self.response[:count], self.response[count:]
        return value


class TransportTests(unittest.TestCase):
    """No credentials may be sent without both verified TLS and route attribution."""

    def probe_wire(self, wire, route_error=False):
        """Inject transport edges while retaining real header parsing and validation."""
        from unittest.mock import Mock
        controller = Mock()
        if route_error:
            controller.verify_route.side_effect = ValueError("wrong route")
        context = Mock()
        context.wrap_socket.return_value = wire
        with patch.object(transport.socket, "create_connection", return_value=wire), patch.object(transport.ssl, "create_default_context", return_value=context):
            result = transport.probe({"access_token": "test-token", "account_id": "test-account"},
                                     "http://127.0.0.1:7890", controller, "node", "group", transport.Cancellation())
        controller.verify_route.assert_called_once_with(34567, "node", "group")
        return result

    def test_upgrade_requires_matching_accept_and_101(self):
        """A 200 page or forged 101 must never be reported as a valid official connection."""
        for valid, status, expected in ((True, 101, True), (False, 101, False), (True, 200, False)):
            wire = Wire(valid, status)
            result = self.probe_wire(wire)
            self.assertEqual(result["ok"], expected)
            self.assertNotIn("test-token", json.dumps(result))
            self.assertFalse(any(b"response.create" in request for request in wire.requests))

    def test_wrong_route_never_receives_auth(self):
        """An unverified route stops before sending the authenticated request."""
        wire = Wire()
        result = self.probe_wire(wire, route_error=True)
        self.assertEqual(result["stage"], "route")
        self.assertFalse(result["ok"])
        self.assertEqual(len(wire.requests), 1)

    def test_slow_tls_releases_route_before_waiting_for_server(self):
        """A tracked outbound unlocks other nodes while TLS remains incomplete."""
        from unittest.mock import Mock
        wire = Wire()
        wire.do_handshake = Mock(side_effect=[transport.ssl.SSLWantReadError(), None])
        binding = Mock()

        def wait_for_tls(*args):
            """The first wait must occur after releasing the selector, before any auth."""
            binding.close.assert_called_once()
            self.assertFalse(wire.requests)
            return [], [], []

        with patch.object(transport.select, "select", side_effect=wait_for_tls):
            transport.handshake_attributed(wire, Mock(), 34567, "a", "group", transport.Cancellation(),
                                           time.monotonic() + 2, binding, {})
        self.assertEqual(wire.do_handshake.call_count, 2)

    def test_website_logout_does_not_fall_back_to_desktop(self):
        """Once a website account is selected, missing credentials mean signed out."""
        import codex_auth
        with tempfile.TemporaryDirectory() as directory, patch.object(codex_auth, "selected_home", return_value=Path(directory)):
            with self.assertRaises(ValueError):
                transport.load_auth()

    def test_cancel_interrupts_all_worker_sockets(self):
        """A single stop must wake every registered worker, not just the last one."""
        cancel = transport.Cancellation()
        ready = threading.Barrier(4)
        drained = []

        def worker():
            """Wait on a real socket so cancellation has to interrupt a blocking read."""
            left, right = socket.socketpair()
            with left, right:
                left.settimeout(2)
                cancel.attach(left)
                ready.wait(2)
                try:
                    left.recv(1)
                    drained.append(True)
                finally:
                    cancel.detach()

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        ready.wait(2)
        started = time.monotonic()
        cancel.cancel()
        for thread in threads:
            thread.join(1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(drained), 3)
        self.assertFalse(cancel.sockets)


class FakeController:
    """Simulate external selection changes and controller restore failures."""

    def __init__(self):
        """Use one baseline and two nodes with deterministic HTTPS screening."""
        self.current = "original"
        self.switches = []
        self.fail_restore = False

    def selection(self, group):
        """Return the externally mutable selector state."""
        return self.current

    def select(self, group, node):
        """Track only successful mutations, including final restoration."""
        if self.fail_restore and node == "original":
            raise ValueError("offline")
        self.switches.append(node)
        self.current = node

    def proxies(self):
        """Include an excluded direct route and nested group in the live catalog."""
        return {"group": {"type": "Selector", "all": ["a", "b", "original", "DIRECT", "nested"]},
                **{n: {"type": "Shadowsocks"} for n in ("a", "b", "original")},
                "DIRECT": {"type": "Direct"}, "nested": {"type": "URLTest", "all": ["a"]}}

    def delay(self, node):
        """Make the baseline slower so it must be added beyond the top candidates."""
        return {"delay_ms": {"a": 1, "b": 2, "original": 3}[node]}


class NetworkTests(unittest.TestCase):
    """Assert restoration and equal-sample ranking are part of task completion."""

    def setUp(self):
        """Isolate both history and the cross-install controller lease."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = network.NetworkManager(self.root, self.root / "leases")
        self.manager.configure({"group": "group"})
        self.controller = FakeController()
        self.addCleanup(self.manager.close)

    def run_job(self, probe, options=None):
        """Keep legacy lifecycle checks serial; fake transports still acquire route ownership."""
        def attributed(*args, route_guard):
            """Simulate tunnel admission, releasing the route before waiting for a response."""
            with route_guard():
                pass
            return probe(*args)

        with patch.object(network, "Controller", return_value=self.controller), patch.object(network, "load_auth", return_value={}), patch.object(network, "probe", side_effect=attributed):
            result = self.manager.start(options or {"top": 2, "repeats": 2, "concurrency": 1})
            self.manager.thread.join(3)
            self.assertFalse(self.manager.busy())
        return self.manager.get(result["id"])

    def success(self, *args):
        """A valid attributed handshake sample, used by deterministic lifecycle tests."""
        return {"ok": True, "elapsed_ms": 50, "stage": "websocket", "status": 101}

    def apply(self, run_id=None, probe=None):
        """Run explicit switching with real persistence and an injected verification."""
        with patch.object(network, "Controller", return_value=self.controller), patch.object(network, "load_auth", return_value={}), patch.object(network, "probe", side_effect=probe or self.success):
            result = self.manager.apply_best(run_id)
            self.manager.thread.join(3)
            self.assertFalse(self.manager.busy())
        return self.manager.get(result["id"])

    def test_apply_retains_verified_best_without_new_benchmark(self):
        """Applying keeps the verified winner and leaves samples and batch identity intact."""
        before = self.run_job(self.success)
        result = self.apply()
        self.assertEqual(result["id"], before["id"])
        self.assertEqual(result["rows"], before["rows"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["application"]["status"], "succeeded")
        self.assertEqual(result["application"]["restoration"], {"state": "retained", "node": "a"})
        self.assertEqual(self.controller.current, "a")
        self.assertEqual(len(list(self.manager.history_dir.glob("*.json"))), 1)
        self.assertFalse(list((self.root / "leases").glob("*.json")))

    def test_apply_failure_restores_click_time_selection(self):
        """A stale ranking's original node must not replace the actual pre-click choice."""
        before = self.run_job(self.success)
        self.controller.current = "b"
        result = self.apply(before["id"], lambda *args: {"ok": False, "stage": "websocket", "status": 403, "elapsed_ms": 15})
        self.assertEqual(result["application"]["status"], "failed")
        self.assertEqual(result["application"]["restoration"], {"state": "restored", "node": "b"})
        self.assertEqual(self.controller.current, "b")
        self.assertEqual(result["rows"], before["rows"])

    def test_apply_does_not_overwrite_external_change(self):
        """A success response cannot commit a route that changed during verification."""
        before = self.run_job(self.success)

        def external(*args):
            """Change selection between the probe and commit readback."""
            self.controller.current = "b"
            return self.success()

        result = self.apply(before["id"], external)
        self.assertEqual(result["application"]["status"], "failed")
        self.assertEqual(result["application"]["restoration"]["state"], "external_change")
        self.assertEqual(self.controller.current, "b")

    def test_apply_cancel_and_duplicate_requests(self):
        """Verification is cancellable, blocks duplicates and never cancels the ranking."""
        before = self.run_job(self.success)
        entered = threading.Event()

        def blocked(auth, proxy, controller, node, group, cancel, timeout):
            """Hold the verification open until cancellation arrives."""
            entered.set()
            cancel.event.wait(2)
            return self.success()

        with patch.object(network, "Controller", return_value=self.controller), patch.object(network, "load_auth", return_value={}), patch.object(network, "probe", side_effect=blocked):
            self.manager.apply_best(before["id"])
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.manager.apply_best(before["id"])
            with self.assertRaises(ValueError):
                self.manager.start({})
            self.manager.cancel(before["id"])
            self.manager.thread.join(3)
        result = self.manager.snapshot()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["application"]["status"], "cancelled")
        self.assertEqual(self.controller.current, "original")

    def test_apply_rejects_incomplete_mismatched_and_missing_candidates(self):
        """Only complete matching samples and a live concrete node may be applied."""
        with self.assertRaises(ValueError):
            self.manager.apply_best()
        before = self.run_job(self.success)
        for replacement in ({"status": "cancelled"}, {"rows": []}, {"settings": {"group": "other"}}):
            self.manager.data = {**before, **replacement}
            with self.assertRaises(ValueError):
                self.manager.apply_best(before["id"])
        self.manager.data = before
        with patch.object(network, "Controller", return_value=self.controller), patch.object(network, "load_auth", return_value={}), patch.object(self.controller, "proxies", return_value={"group": {"type": "Selector", "all": []}}):
            with self.assertRaises(ValueError):
                self.manager.apply_best(before["id"])
        for invalid in (123, "../config"):
            with self.assertRaises(ValueError):
                self.manager.apply_best(invalid)

    def test_apply_failed_restore_can_recover_without_erasing_scores(self):
        """A failed rollback leaves a recoverable journal but doesn't erase good samples."""
        before = self.run_job(self.success)
        self.controller.fail_restore = True
        result = self.apply(before["id"], lambda *args: {"ok": False, "stage": "tls", "error": "timeout", "elapsed_ms": 100})
        self.assertEqual(result["application"]["restoration"]["state"], "failed")
        self.controller.fail_restore = False
        with patch.object(network, "Controller", return_value=self.controller):
            self.manager.recover()
        result = self.manager.get(before["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["rows"], before["rows"])
        self.assertEqual(result["recommendations"], before["recommendations"])
        self.assertEqual(self.controller.current, "original")

    def test_apply_crash_recovery_distinguishes_committed_choice(self):
        """Restart retains a durable verified choice and rolls back an unfinished one."""
        before = self.run_job(self.success)
        for committed in (False, True):
            with self.subTest(committed=committed):
                application = {"id": "c" * 12, "status": "running", "target": "a", "previous": "b"}
                saved = {**before, "application": application}
                network.save_json(self.manager.history_dir / (before["id"] + ".json"), saved)
                lease = network.RouteLease(self.manager.settings(False), self.root / "leases")
                journal = {"operation": "apply", "run_id": before["id"], "group": "group", "original": "b", "owned": "a", "pending": None}
                if committed:
                    journal.update(committed=True, application={**application, "status": "succeeded", "verification": self.success()})
                network.save_json(lease.path, journal)
                lease.close()
                self.controller.current = "a"
                restored = network.NetworkManager(self.root, self.root / "leases")
                with patch.object(network, "Controller", return_value=self.controller):
                    restored.restore_history()
                result = restored.snapshot()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["rows"], before["rows"])
                self.assertEqual(result["application"]["status"], "succeeded" if committed else "interrupted")
                self.assertEqual(self.controller.current, "a" if committed else "b")

    def test_cli_apply_waits_and_returns_failure_after_failed_verification(self):
        """The CLI cannot exit successfully just because the source benchmark completed."""
        from unittest.mock import Mock
        args = cli.parser().parse_args(["vpn", "apply", "--json"])
        running = {"id": "a" * 12, "kind": "network", "status": "completed", "recommendations": ["a"],
                   "application": {"id": "b" * 12, "status": "running", "phase": "verifying", "target": "a"}}
        finished = {**running, "application": {**running["application"], "status": "failed"}}
        client = Mock()
        client.request.side_effect = [running, finished]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.execute(args, client), 3)
        self.assertEqual(client.request.call_args_list[0].args, ("/api/network/apply", {}))
        self.assertTrue(cli.pending("network", running))
        self.assertFalse(cli.pending("network", finished))

    def test_apply_default_uses_creation_time_not_last_history_write(self):
        """Applying an old result must not make it the latest completed benchmark."""
        older = self.run_job(self.success)
        newer = self.run_job(self.success)
        network.save_json(self.manager.history_dir / (older["id"] + ".json"), older)
        result = self.apply()
        self.assertEqual(result["id"], newer["id"])

    def test_restart_before_first_switch_does_not_offer_endless_recovery(self):
        """An orphaned submission without a route journal has nothing to roll back."""
        before = self.run_job(self.success)
        before["application"] = {"status": "running", "target": "a", "previous": "original", "restoration": {"state": "pending"}}
        network.save_json(self.manager.history_dir / (before["id"] + ".json"), before)
        restarted = network.NetworkManager(self.root, self.root / "leases")
        with patch.object(network, "Controller", return_value=self.controller):
            restarted.restore_history()
        self.assertEqual(restarted.snapshot()["application"]["restoration"]["state"], "not_needed")
        self.assertEqual(self.controller.current, "original")

    def test_equal_rounds_restore_and_durable_history(self):
        """Every candidate receives equal rounds; the original is included and restored."""
        result = self.run_job(self.success)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["done"], 6)
        self.assertTrue(all(row["attempts"] == 2 for row in result["rows"]))
        self.assertEqual(self.controller.switches, ["a", "b", "original"] * 2)
        self.assertEqual(result["restoration"]["state"], "restored")
        self.assertEqual(len(self.manager.history()["items"]), 1)
        self.assertEqual(network.eligible(self.controller.proxies(), "group"), ["a", "b", "original"])

    def test_cancel_interrupts_and_restores_before_terminal_state(self):
        """A cancellation cannot publish completion while the selector is still changed."""
        entered = threading.Event()

        def blocked(auth, proxy, controller, node, group, cancel, timeout, route_guard):
            """Block a sample until the user cancels through the public manager API."""
            with route_guard():
                pass
            entered.set()
            cancel.event.wait(2)
            return {"ok": False, "elapsed_ms": 100, "stage": "websocket", "error": "cancelled"}

        with patch.object(network, "Controller", return_value=self.controller), patch.object(network, "load_auth", return_value={}), patch.object(network, "probe", side_effect=blocked):
            run = self.manager.start({"top": 1, "repeats": 2, "concurrency": 1})
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.manager.cancel("stale-id")
            self.manager.cancel(run["id"])
            self.manager.thread.join(3)
        result = self.manager.snapshot()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["restoration"]["state"], "restored")
        self.assertEqual(self.controller.current, "original")
        self.assertEqual(result["recommendations"], [])

    def test_default_concurrency_overlaps_nodes_and_keeps_equal_rounds(self):
        """All three attributed responses must overlap; a serial scheduler cannot pass."""
        barrier = threading.Barrier(3)
        seen = []

        def response(auth, proxy, controller, node, group, cancel, timeout):
            """Wait until every node has been admitted, independently of selector changes."""
            seen.append(node)
            barrier.wait(2)
            return self.success()

        result = self.run_job(response, {"top": 2, "repeats": 2})
        self.assertEqual(result["status"], "completed", result.get("failure"))
        self.assertEqual(result["options"]["concurrency"], 6)
        self.assertEqual(result["done"], 6)
        self.assertCountEqual(seen[:3], ["a", "b", "original"])
        self.assertTrue(all(row["attempts"] == 2 for row in result["rows"]))
        self.assertEqual(self.controller.current, "original")

    def test_failure_interrupts_concurrent_peers_before_restoring(self):
        """A fatal upstream error wakes active peers and preserves the failure reason."""
        barrier = threading.Barrier(3)
        drained = []

        def response(auth, proxy, controller, node, group, cancel, timeout):
            """Two peers wait for the failing node to cancel the shared batch."""
            barrier.wait(2)
            if node == "a":
                return {"ok": False, "stage": "websocket", "status": 429, "elapsed_ms": 10}
            self.assertTrue(cancel.event.wait(1))
            drained.append(node)
            cancel.check()

        result = self.run_job(response, {"top": 2, "repeats": 2})
        self.assertEqual(result["status"], "failed")
        self.assertIn("429", result["failure"])
        self.assertCountEqual(drained, ["b", "original"])
        self.assertEqual(result["done"], 1)
        self.assertEqual(self.controller.current, "original")

    def test_concurrency_bounds_reject_without_mutation(self):
        """Booleans, fractional and excessive concurrency never create a batch."""
        for value in (0, 51, True, 1.5, "6"):
            with self.assertRaises(ValueError):
                self.manager.start({"concurrency": value})
        self.assertEqual(self.controller.switches, [])
        self.assertEqual(self.manager.history()["total"], 0)

    def test_external_change_is_not_overwritten(self):
        """An external choice ends the test without restoring over that choice."""
        def external(*args):
            """Simulate a user switching while the official handshake is in flight."""
            self.controller.current = "user-choice"
            return self.success()
        result = self.run_job(external)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["done"], 0)
        self.assertEqual(result["restoration"]["state"], "external_change")
        self.assertEqual(self.controller.current, "user-choice")

    def test_auth_and_upstream_failure_stop_remaining_candidates(self):
        """Auth expiry and upstream throttling are not multiplied across every node."""
        for status in (401, 429, 503):
            with self.subTest(status=status):
                result = self.run_job(lambda *args: {"ok": False, "stage": "websocket", "status": status, "elapsed_ms": 12})
                self.assertEqual(result["done"], 1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(self.controller.current, "original")

    def test_recovery_journal_survives_restore_failure(self):
        """Failed restoration blocks another mutation until an explicit retry succeeds."""
        def failed(*args):
            """Force immediate terminal auth failure and an unavailable restore call."""
            self.controller.fail_restore = True
            return {"ok": False, "stage": "websocket", "status": 401, "elapsed_ms": 12}
        result = self.run_job(failed)
        self.assertEqual(result["restoration"]["state"], "failed")
        self.assertEqual(len(list((self.root / "leases").glob("*.json"))), 1)
        with self.assertRaises(ValueError):
            self.manager.configure({"controller": "http://127.0.0.1:9999"})
        self.controller.fail_restore = False
        with patch.object(network, "Controller", return_value=self.controller):
            recovered = self.manager.recover()
        self.assertEqual(recovered["state"], "restored")
        self.assertEqual(self.controller.current, "original")
        self.assertEqual(list((self.root / "leases").glob("*.json")), [])

    def test_crash_between_put_and_readback_is_recoverable(self):
        """Persisted intent covers a PUT that succeeded before the process crashed."""
        lease = network.RouteLease(self.manager.settings(False), self.root / "leases")
        network.save_json(lease.path, {"group": "group", "original": "original", "owned": "original", "pending": "a", "run_id": "a" * 12})
        self.controller.current = "a"
        lease.close()
        with patch.object(network, "Controller", return_value=self.controller):
            self.manager.restore_history()
        self.assertEqual(self.controller.current, "original")

    def test_lock_covers_aliases_and_different_data_directories(self):
        """localhost and 127.0.0.1 cannot run conflicting tests in parallel."""
        lease = network.RouteLease(self.manager.settings(False), self.root / "leases")
        try:
            with self.assertRaises(ValueError):
                network.RouteLease({"controller": "http://localhost:9090"}, self.root / "leases")
        finally:
            lease.close()

    def test_ranking_prioritizes_success_and_excludes_partial(self):
        """Partial and zero samples cannot win by looking artificially fast."""
        rows = [{"name": "fast-flaky", "samples": [{"ok": True, "elapsed_ms": 1}, {"ok": False, "elapsed_ms": 1000}]},
                {"name": "stable", "samples": [{"ok": True, "elapsed_ms": 20}] * 2},
                {"name": "partial", "samples": [{"ok": True, "elapsed_ms": 1}]},
                {"name": "empty", "samples": []}]
        ranked = network.rank_nodes(rows, 2)
        self.assertEqual(ranked[0]["name"], "stable")
        self.assertIsNone(next(r for r in ranked if r["name"] == "partial")["rank"])
        self.assertIsNone(next(r for r in ranked if r["name"] == "empty")["median_ms"])

    def test_secret_stays_private_and_endpoints_are_loopback_only(self):
        """Secret readback and endpoint validation are enforced in the backend."""
        public = self.manager.configure({"secret": "private-test-secret"})
        self.assertNotIn("private-test-secret", json.dumps(public))
        self.assertEqual(self.manager.config_path.stat().st_mode & 0o777, 0o600)
        for url in ("http://example.com:80", "https://127.0.0.1:80", "http://user@127.0.0.1:80", "http://127.0.0.1:80/path", "http://127.0.0.1:80?x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.manager.configure({"controller": url})
        with self.assertRaises(ValueError):
            self.manager.get("../config")

    def test_cli_routes_to_shared_manager(self):
        """CLI node tests don't require model targets and JSON output stays machine-readable."""
        args = cli.parser().parse_args(["vpn", "test", "--node", "a", "--repeats", "3", "--no-wait", "--json"])
        class Client:
            """Capture only the submitted endpoint and body."""
            def request(inner, path, body):
                """Return a queued batch without spawning any local process."""
                self.assertEqual(path, "/api/network/run")
                self.assertEqual(body["nodes"], ["a"])
                return {"id": "a" * 12, "status": "running"}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.execute(args, Client()), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "running")
        self.assertEqual(cli.result_path("network", "a" * 12), "/api/network/run/" + "a" * 12)


class ApiTests(unittest.TestCase):
    """Exercise the actual dispatcher without opening another service port."""

    def handler(self, path, origin=None):
        """Create a request with the same JSON and Host metadata as the local UI."""
        import app
        from email.message import Message
        from types import SimpleNamespace
        from unittest.mock import Mock
        handler = app.Handler.__new__(app.Handler)
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8780"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = "2"
        if origin:
            handler.headers["Origin"] = origin
        handler.path, handler.rfile = path, io.BytesIO(b"{}")
        handler.server = SimpleNamespace(stopping=False)
        handler._json = Mock()
        return handler

    def test_cross_origin_cannot_switch_or_recover(self):
        """Every new mutation route rejects foreign browser origins before doing work."""
        for path in ("config", "discover", "run", "recover", "cancel", "apply"):
            handler = self.handler("/api/network/" + path, "https://foreign.example")
            handler.do_POST()
            self.assertEqual(handler._json.call_args.args[1], 403)

    def test_mutual_exclusion_in_both_directions(self):
        """A route switch and evaluation creation cannot overlap through the API."""
        import app
        with patch.object(app, "_evaluation_busy", return_value=True):
            for path in ("run", "apply"):
                handler = self.handler("/api/network/" + path)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 409)
        with patch.object(network.manager, "busy", return_value=True):
            for path in ("/api/probe", "/api/iq/run", "/api/iq/retry", "/api/iq/review", "/api/swe/run", "/api/run"):
                handler = self.handler(path)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[1], 409)


if __name__ == "__main__":
    unittest.main()
