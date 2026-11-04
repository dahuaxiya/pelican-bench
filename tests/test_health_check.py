"""Health checks use isolated files and fake inference, never real account quota."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import urllib.error
import urllib.request

import app
import health_check as module
import llm


class HealthTests(unittest.TestCase):
    """Exercise evidence boundaries, persistence and cancellation end to end."""

    def setUp(self):
        """Redirect every dependency before a worker can start."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = module.HealthChecks(self.temp.name)
        self.request = dict(request_id="a" * 32, environment="company", model="gpt-test", repeats=1, model_check=True)
        self.catalog = {"ready": True, "models": ["gpt-test"], "auth_source": "site", "cli_version": "test"}
        self.report = module.sanitize_doctor({"codexVersion": "1.0", "checks": [
            {"id": key, "status": "ok", "durationMs": 2} for key in module.CHECKS]})
        self.patch("codex_channel.catalog", return_value=self.catalog)
        self.patch("route_context", return_value={"cli": {}, "probe": {}})
        self.doctor = self.patch("doctor", return_value=self.report)
        self.complete = self.patch("codex_stream.complete", return_value={"completed": True, "text": "OK", "ttft": .1, "total": .2})
        self.patch("SAMPLE_PAUSE", 0)
        self.addCleanup(self.manager.close)

    def patch(self, name, *args, **kwargs):
        """Keep dependency replacements active until this test's worker is closed."""
        mock = patch("health_check." + name, *args, **kwargs)
        self.addCleanup(mock.stop)
        return mock.start()

    def run_check(self, **changes):
        """Wait for a bounded fake run, asserting the busy slot is released."""
        self.request.update(changes)
        self.manager.start(self.request)
        self.manager.thread.join(3)
        self.assertFalse(self.manager.busy())
        return self.manager.get(self.request["request_id"])

    def test_persists_and_idempotent_start_never_replays(self):
        """Restarting or submitting an identical request must not use quota again."""
        result = self.run_check()
        self.assertEqual(result["verdict"]["code"], "passed")
        self.assertEqual(result["overview"]["passed"], len(module.CHECKS) + 1)
        restarted = module.HealthChecks(self.temp.name)
        self.assertEqual(restarted.start(self.request), result)
        self.assertEqual(self.complete.call_count, 1)
        with self.assertRaises(ValueError):
            restarted.start({**self.request, "environment": "home"})
        self.assertEqual(restarted.history()["total"], 1)
        self.assertNotIn("text", result["samples"][0])

    def test_recovered_capacity_remains_visible(self):
        """A completed response does not erase an earlier explicit capacity event."""
        def answer(*args, on_diagnostic, **kwargs):
            """Emit the same sanitized diagnostic callback as the real adapter."""
            on_diagnostic({"category": "所选模型容量不足 [capacity]", "source": "stderr", "elapsed_seconds": .1})
            return {"completed": True, "text": "OK"}
        self.complete.side_effect = answer
        result = self.run_check()
        self.assertEqual(result["verdict"]["code"], "capacity")
        self.assertEqual(result["verdict"]["success"], 1)
        self.assertEqual(result["overview"]["issues"], 1)

    def test_errors_are_classified_without_secret_echo(self):
        """503, quota, timeout, rate limits and capacity remain separate."""
        for index, (message, expected, category) in enumerate([
            ("HTTP 503 secret-token", "inconclusive", "[server]"),
            ("HTTP 429 secret-token", "inconclusive", "[rate_limit]"),
            ("quota exceeded secret-token", "quota", "[quota]"),
            ("TLS certificate secret-token", "inconclusive", "[tls]"),
            ("handshake timed out secret-token", "inconclusive", "[timeout]"),
            ("Selected model is at capacity secret-token", "capacity", "[capacity]")]):
            self.complete.side_effect = llm.LLMError(message)
            result = self.run_check(request_id=f"{index:032x}")
            self.assertEqual(result["verdict"]["code"], expected)
            self.assertIn(category, result["samples"][0]["reason"])
            self.assertNotIn("secret-token", json.dumps(result))

    def test_network_only_and_unready_login_do_not_infer(self):
        """One-click diagnostics still work without a model login."""
        result = self.run_check(model_check=False, model="")
        self.assertEqual(result["verdict"]["code"], "not_tested")
        self.catalog.update(ready=False, models=[])
        blocked = self.run_check(request_id="b" * 32, model_check=True)
        self.assertIn("model_blocked", blocked)
        self.complete.assert_not_called()

    def test_empty_incomplete_and_truncated_are_not_success(self):
        """Transport completion cannot hide missing or truncated generated text."""
        for index, response in enumerate([{"completed": True, "text": ""},
                {"completed": False, "text": "OK"}, {"completed": True, "text": "OK", "truncated": True}]):
            self.complete.return_value = response
            self.assertEqual(self.run_check(request_id=f"{index:032x}")["verdict"]["code"], "inconclusive")

    def test_quota_stops_remaining_requests(self):
        """Do not repeatedly hit an exhausted account during a diagnostic batch."""
        self.complete.side_effect = llm.LLMError("usage_limit_reached")
        result = self.run_check(repeats=3)
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(self.complete.call_count, 1)

    def test_cancel_and_duplicate_submit(self):
        """Cancellation reaches only this worker and duplicate starts reuse its ID."""
        ready = threading.Event()
        def blocked(model, prompt, timeout, stop, **kwargs):
            """Wait on the actual cancellation event, simulating an in-flight request."""
            ready.set()
            stop.wait(3)
            raise llm.LLMCancelled("stopped")
        self.complete.side_effect = blocked
        self.manager.start(self.request)
        self.assertTrue(ready.wait(2))
        self.manager.start(self.request)
        with self.assertRaises(ValueError):
            self.manager.start({**self.request, "request_id": "b" * 32})
        with self.assertRaises(ValueError):
            self.manager.cancel("b" * 32)
        self.manager.cancel(self.request["request_id"])
        self.manager.thread.join(2)
        self.assertFalse(self.manager.busy())
        self.assertEqual(self.manager.get(self.request["request_id"])["status"], "cancelled")
        self.assertEqual(self.complete.call_count, 1)

    def test_disk_failure_never_launches_or_sticks_busy(self):
        """Initial persistence is required before starting any external work."""
        with patch.object(module, "save_json", side_effect=OSError("private-path")):
            with self.assertRaisesRegex(ValueError, "未启动"):
                self.manager.start(self.request)
        self.assertFalse(self.manager.busy())
        self.complete.assert_not_called()

    def test_shutdown_crash_and_changed_routes_remain_explicit(self):
        """Unexpected failures release the slot; interrupted files never restart."""
        self.complete.side_effect = RuntimeError("private-path")
        result = self.run_check()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("private-path", json.dumps(result))
        result["status"] = "running"
        module.save_json(Path(self.temp.name) / (result["id"] + ".json"), result)
        self.assertEqual(module.HealthChecks(self.temp.name).get(result["id"])["status"], "interrupted")
        self.complete.side_effect = None
        with patch.object(module, "route_context", side_effect=[{"cli": {}}, {"cli": {"http_proxy": "http://localhost:1"}}]):
            self.assertTrue(self.run_check(request_id="b" * 32)["route_changed"])

    def test_bad_parameters_and_path_traversal_are_rejected(self):
        """Browser inputs cannot select commands, paths, proxies or unbounded requests."""
        for fields in ({"environment": []}, {"repeats": 4}, {"repeats": True}, {"model": ";rm -rf /"},
                       {"request_id": "../auth"}, {"model_check": "yes"}, {"proxy": "http://evil"},
                       {"diagnostics": "false"}, {"diagnostics": False, "model_check": False}):
            with self.assertRaises(ValueError):
                self.manager.start({**self.request, **fields})
        self.complete.assert_not_called()

    def test_speed_check_skips_optional_doctor_and_uses_long_output(self):
        """A model-first check measures real usage and never runs unselected local diagnostics."""
        self.complete.return_value = {"completed": True, "text": "word " * 400, "ttft": 2,
            "usage": {"completion_tokens": 400, "prompt_tokens": 50, "reasoning_tokens": 0},
            "performance": {"total_seconds": 12, "response_seconds": 10}}
        result = self.run_check(diagnostics=False)
        self.doctor.assert_not_called()
        self.assertEqual(self.complete.call_args.args[1], module.PERFORMANCE_PROMPT)
        self.assertEqual(self.complete.call_args.args[2], 180)
        self.assertEqual(result["protocol"], "codex-health-v3")
        self.assertEqual(result["sample_mode"], "performance")
        self.assertEqual(result["overview"]["total"], 1)
        self.assertEqual(result["overview"]["unknown"], 0)
        metrics = result["samples"][0]["metrics"]
        self.assertEqual(metrics["end_to_end_tokens_per_second"], 40)
        self.assertEqual(metrics["ttft_seconds"], 2)
        self.assertIsNone(metrics["generation_tokens_per_second"])
        self.assertNotIn("word word", json.dumps(result))

    def test_optional_doctor_runs_after_saved_model_result(self):
        """Slow optional diagnostics cannot prevent a completed model sample from being visible."""
        def inspect(stop):
            """Check persisted model evidence before simulating the optional diagnostic response."""
            saved = self.manager.get(self.request["request_id"])
            self.assertEqual(saved["samples"][0]["status"], "success")
            self.assertEqual(saved["phase"], "补充本机诊断")
            self.assertFalse(stop.is_set())
            return self.report
        self.doctor.side_effect = inspect
        self.run_check(diagnostics=True)
        self.doctor.assert_called_once()

    def test_short_estimated_and_failed_outputs_never_publish_speed(self):
        """Old OK-sized, estimated-usage and truncated responses cannot manufacture a throughput."""
        for index, update in enumerate([{"text": "OK"}, {"usage": {"completion_tokens": 400, "estimated": True}}, {"truncated": True}]):
            self.complete.return_value = {"completed": True, "text": "word " * 400,
                "usage": {"completion_tokens": 400}, "performance": {"total_seconds": 12, "response_seconds": 10}, **update}
            result = self.run_check(request_id=f"{index:032x}", diagnostics=False)
            self.assertIsNone(result["samples"][0]["metrics"]["end_to_end_tokens_per_second"])

    def test_doctor_and_routes_discard_untrusted_details(self):
        """Only fixed evidence survives; raw endpoints and credentials stay out of history."""
        report = module.sanitize_doctor({"codexVersion": "1.2.3", "checks": [
            {"id": "network.websocket_reachability", "status": "warning", "notes": ["handshake timed out private-secret"],
             "details": {"endpoint": "wss://user:private-secret@host/path", "model provider": "openai"}},
            {"id": "auth.credentials", "status": [], "details": "private-secret"}, {"id": []}]})
        self.assertNotIn("private-secret", json.dumps(report))
        self.assertIn("[timeout]", next(row for row in report["checks"] if row["id"] == "network.websocket_reachability")["reason"])
        self.assertEqual(next(row for row in report["checks"] if row["id"] == "installation")["status"], "unknown")
        # Call the unpatched function via a fresh reference to avoid leaking real process env.
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location("health_route_test", module.__file__)
        fresh = module_from_spec(spec); spec.loader.exec_module(fresh)
        with patch.dict(module.os.environ, {"HTTPS_PROXY": "http://user:private-secret@localhost:7890/private?q=secret"}, clear=True), \
                patch.object(module.codex_channel, "_environment", return_value={"ALL_PROXY": "bad-secret"}):
            context = fresh.route_context()
        self.assertNotIn("secret", json.dumps(context))
        self.assertEqual(context["cli"]["https_proxy"], "http://localhost:7890")

    def test_doctor_supports_array_and_keyed_schema(self):
        """Codex 0.161 moved check IDs from array rows to object keys."""
        rows = [{"id": key, "status": "ok", "durationMs": 1} for key in module.CHECKS]
        array = module.sanitize_doctor({"codexVersion": "1.0", "checks": rows})
        keyed = module.sanitize_doctor({"codexVersion": "1.0", "checks": {row["id"]: {k: v for k, v in row.items() if k != "id"} for row in rows}})
        self.assertEqual(array, keyed)

    def test_config_keywords_do_not_become_network_diagnoses(self):
        """A config warning can mention WebSocket without observing a network failure."""
        result = module.sanitize_doctor({"checks": {
            "config.load": {"status": "warning", "details": {"flags": "websocket proxy"}},
            "network.env": {"status": "ok", "details": {"respect system proxy": "disabled", "system proxy": "manual"}}}})
        config = next(row for row in result["checks"] if row["id"] == "config.load")
        self.assertNotIn("[network]", config["reason"])
        env = next(row for row in result["checks"] if row["id"] == "network.env")
        self.assertEqual(len(env["facts"]), 2)

    def test_http_guards_busy_and_idempotency(self):
        """Creation observes origin/account locks; duplicate IDs bypass only busy checks."""
        with patch.object(module, "manager", self.manager):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = "http://127.0.0.1:" + str(server.server_port)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(path, data, origin=base, content="application/json"):
                """Send local test JSON without using the developer's proxy."""
                request = urllib.request.Request(base + path, data=json.dumps(data).encode(), headers={"Origin": origin, "Content-Type": content})
                return opener.open(request, timeout=3)
            try:
                for origin, content, code in (("https://evil.invalid", "application/json", 403), (base, "text/plain", 415)):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        post("/api/health-check/start", self.request, origin, content)
                    self.assertEqual(caught.exception.code, code); caught.exception.close()
                with patch.object(app, "_evaluation_busy", return_value=True):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        post("/api/health-check/start", self.request)
                    self.assertEqual(caught.exception.code, 409); caught.exception.close()
                    self.run_check()
                    with post("/api/health-check/start", self.request) as response:
                        self.assertEqual(response.status, 202)
                with opener.open(base + "/api/health-check/history", timeout=3) as response:
                    self.assertEqual(json.load(response)["total"], 1)
                # The new browser supplies diagnostics=false; exercise the real HTTP
                # dispatch too, not just the manager's direct-call input validation.
                with post("/api/health-check/start", {**self.request, "request_id": "c" * 32, "diagnostics": False}) as response:
                    created = json.load(response)
                    self.assertEqual(created["protocol"], "codex-health-v3")
                    self.assertFalse(created["diagnostics"])
                self.manager.thread.join(3)
                self.assertEqual(self.complete.call_count, 2)
                self.assertEqual(self.doctor.call_count, 1)
            finally:
                server.shutdown(); server.server_close(); thread.join(2)

    def test_svg_adds_one_request_and_persists_clean_preview_separately(self):
        """默认网页额外一次出图，不混入文字测速中位数，重读或重发不重复请求。"""
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>private-script</script><circle cx="5" cy="5" r="4"/></svg>'
        self.complete.side_effect = [
            {"completed": True, "text": "OK"},
            {"completed": True, "text": svg, "ttft": .5,
             "usage": {"completion_tokens": 50}, "performance": {"total_seconds": 2, "response_seconds": 1.9}}]
        result = self.run_check(svg_check=True, diagnostics=False)
        self.assertEqual(self.complete.call_count, 2)
        self.assertEqual(self.complete.call_args_list[1].args[1], module.config.DEFAULT_PROMPT)
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(result["svg_result"]["status"], "success")
        self.assertEqual(result["svg_result"]["metrics"]["ttft_seconds"], .5)
        self.assertNotIn("private-script", json.dumps(result))
        self.assertNotIn("<script", result["svg_result"]["svg"])
        self.assertEqual(result["verdict"]["success"], 2)
        self.assertEqual(result["overview"]["passed"], 2)
        self.assertEqual(result["overview"]["total"], 2)
        self.assertEqual(module.HealthChecks(self.temp.name).start(self.request), result)
        self.assertNotIn("<svg", json.dumps(self.manager.history()))
        self.assertEqual(self.complete.call_count, 2)

    def test_svg_empty_malformed_oversized_or_truncated_is_not_a_pass(self):
        """文字成功不掩盖 SVG 格式失败，失败正文不存入预览。"""
        for index, response in enumerate([
            {"text": "no SVG"},
            {"text": "<svg><rect></svg>"},
            {"text": "<svg></svg>"},
            {"text": "<svg><rect/>" + "a" * module.SVG_LIMIT + "</svg>"},
            {"text": "<svg><rect/></svg>", "truncated": True},
        ]):
            self.complete.side_effect = [{"completed": True, "text": "OK"}, {"completed": True, **response}]
            result = self.run_check(request_id=f"{index:032x}", svg_check=True, diagnostics=False)
            self.assertEqual(result["svg_result"]["status"], "failed")
            self.assertNotIn("svg", result["svg_result"])
            self.assertEqual(result["verdict"]["code"], "inconclusive")
            self.assertEqual(result["overview"]["passed"], 1)
            self.assertEqual(result["overview"]["issues"], 1)

    def test_svg_errors_and_recovered_capacity_remain_distinct(self):
        """SVG 出现容量错误影响总体结论，但不改写之前成功的文字记录。"""
        self.complete.side_effect = [{"completed": True, "text": "OK"}, llm.LLMError("Selected model is at capacity private-secret")]
        result = self.run_check(svg_check=True, diagnostics=False)
        self.assertEqual(result["verdict"]["code"], "capacity")
        self.assertEqual(result["samples"][0]["status"], "success")
        self.assertEqual(result["svg_result"]["status"], "failed")
        self.assertNotIn("private-secret", json.dumps(result))

    def test_svg_skips_when_account_is_blocked_or_text_request_cancelled(self):
        """已知额度失败或取消后不再追加出图请求，关闭诊断也能正常释放运行槽。"""
        self.complete.side_effect = llm.LLMError("usage_limit_reached")
        result = self.run_check(svg_check=True, diagnostics=False)
        self.assertEqual(self.complete.call_count, 1)
        self.assertEqual(result["svg_result"]["status"], "skipped")
        self.assertEqual(result["verdict"]["code"], "quota")

    def test_svg_phase_is_saved_and_cancellation_reaches_generation(self):
        """图形生成中能读取进度，重复提交不重发，停止信号到达在途适配器。"""
        ready = threading.Event()
        def answer(model, prompt, timeout, stop, **kwargs):
            """先返回文字结果，再等待 SVG 的取消事件。"""
            if prompt == module.PERFORMANCE_PROMPT:
                return {"completed": True, "text": "OK"}
            ready.set()
            stop.wait(3)
            raise llm.LLMCancelled("stopped")
        self.complete.side_effect = answer
        self.request.update(svg_check=True, diagnostics=False)
        self.manager.start(self.request)
        self.assertTrue(ready.wait(2))
        pending = self.manager.start(self.request)
        self.assertEqual(pending["phase"], "正在生成 SVG")
        self.assertEqual(pending["svg_result"]["status"], "running")
        self.assertEqual(pending["verdict"]["code"], "pending")
        self.manager.cancel(self.request["request_id"])
        self.manager.thread.join(2)
        result = self.manager.get(self.request["request_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["svg_result"]["status"], "cancelled")
        self.assertEqual(self.complete.call_count, 2)
        self.assertFalse(self.manager.busy())

    def test_svg_validation_rejects_entities_and_parameter_injection(self):
        """新开关必须为布尔值，SVG 不允许声明实体或自定义提示词。"""
        for fields in ({"svg_check": 1}, {"svg_check": True, "model_check": False},
                       {"svg_check": True, "svg_prompt": "custom"}):
            with self.assertRaises(ValueError):
                self.manager.start({**self.request, **fields})
        with self.assertRaisesRegex(ValueError, "文档声明"):
            module.checked_svg('<svg><!DOCTYPE x><rect/></svg>')
        self.complete.assert_not_called()

    def test_interrupted_svg_history_does_not_restart_generation(self):
        """崩溃遗留的 SVG 运行态读回为中断，不自动请求或污染旧记录。"""
        result = self.run_check(diagnostics=False)
        result.update(status="running", svg_check=True, svg_result={"status": "running"})
        path = Path(self.temp.name) / (result["id"] + ".json")
        module.save_json(path, result)
        restored = module.HealthChecks(self.temp.name).get(result["id"])
        self.assertEqual(restored["status"], "interrupted")
        self.assertEqual(restored["svg_result"]["status"], "interrupted")
        self.assertEqual(json.loads(path.read_text())["svg_result"]["status"], "running")
        self.assertEqual(self.complete.call_count, 1)


if __name__ == "__main__":
    unittest.main()
