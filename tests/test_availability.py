"""Site history, cancellation and HTTP boundaries without external model requests."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import urllib.error
import urllib.request

import app
import availability as module
import llm


class AvailabilityTests(unittest.TestCase):
    """Exercise durable outcomes rather than checking implementation structure."""

    def setUp(self):
        """Use a private database and a fake endpoint with a sentinel credential."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.connection = {"name": "站点 A", "base_url": "https://example.invalid/v1", "api_key": "never-expose-me", "models": ["chat"]}
        self.manager = module.Availability(Path(self.directory.name) / "history.sqlite3",
                                           lambda: {self.connection["name"]: self.connection}, self.success)
        self.manager.start_service()
        self.addCleanup(self.manager.close)
        self.configure(False)

    @staticmethod
    def success(*args, **kwargs):
        """Return a completed nonempty response without an external request."""
        return {"text": "OK", "completed": True}

    def configure(self, enabled, model="chat"):
        """Select the known site; a paused monitor still permits a one-shot check."""
        return self.manager.configure({"enabled": enabled, "sites": [
            {"site": self.connection["name"], "enabled": True, "model": model}]})

    def run_check(self):
        """Wait for this fake batch and fail quickly if its resources are not released."""
        result = self.manager.check()
        self.manager.worker.join(3)
        self.assertFalse(self.manager.busy())
        return result

    def elapse_interval(self):
        """Age isolated test attempts by five minutes so a later scenario may probe.

        This affects only the temporary test database; production history is never
        rewritten to work around cooldowns.
        """
        with self.manager.database() as db:
            db.execute("UPDATE samples SET started_at=started_at-?,finished_at=finished_at-?",
                       (module.INTERVAL + 1, module.INTERVAL + 1))

    def insert(self, status, started_at, model="chat"):
        """Seed only the isolated test database with a historical monitoring outcome."""
        with self.manager.database() as db:
            db.execute("INSERT INTO samples(site,model,started_at,finished_at,status,reason) VALUES(?,?,?,?,?,?)",
                       (self.connection["name"], model, started_at, started_at + 1, status, "test"))

    def test_failure_then_success_preserves_both_attempts(self):
        """A later recovery must not rewrite the earlier failure or inflate uptime."""
        self.manager.probe = lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("HTTP 401 never-expose-me [authentication]"))
        self.run_check()
        self.elapse_interval()
        self.manager.probe = self.success
        self.run_check()
        row = self.manager.snapshot()["rows"][0]
        self.assertEqual((row["success"], row["failure"], row["availability"]), (1, 1, 50))
        self.assertEqual(row["state"], "success")
        history = self.manager.history(self.connection["name"], time.time() - 600, time.time() + 1)
        self.assertEqual(history["total"], 2)
        self.assertNotIn("never-expose-me", json.dumps(history))

    def test_empty_truncated_and_incomplete_responses_are_not_available(self):
        """HTTP success alone is insufficient when no complete usable answer arrived."""
        for response in ({"text": "", "completed": True}, {"text": "OK", "completed": False},
                         {"text": "OK", "completed": True, "truncated": True},
                         {"text": "OK", "completed": True, "error": "bad"}):
            self.manager.probe = lambda *a, **k: response
            self.run_check()
            self.elapse_interval()
        row = self.manager.snapshot()["rows"][0]
        self.assertEqual((row["success"], row["failure"], row["availability"]), (0, 4, 0))

    def test_missing_history_and_stale_success_remain_unknown(self):
        """Do not backfill downtime or present an old green sample as current health."""
        row = self.manager.snapshot()["rows"][0]
        self.assertIsNone(row["availability"])
        self.assertEqual(sum(b["success"] for b in row["buckets"]), 0)
        self.insert("success", time.time() - 3600)
        row = self.manager.snapshot()["rows"][0]
        self.assertEqual(row["state"], "stale")
        self.assertEqual(sum(b["success"] for b in row["buckets"]), 1)
        self.assertEqual(sum(not any(b[k] for k in ("success", "failure", "unknown", "running")) for b in row["buckets"]), 59)

    def test_bucket_preview_matches_the_last_real_attempt(self):
        """Hover details retain an unknown last attempt and match full history exactly."""
        now = time.time()
        self.insert("success", now - 20)
        self.insert("failure", now - 10)
        self.insert("unknown", now - 5)
        row = self.manager.snapshot()["rows"][0]
        bucket = row["buckets"][-1]
        history = self.manager.history(self.connection["name"], now - 60, now + 1)
        self.assertEqual((bucket["success"], bucket["failure"], bucket["unknown"]), (1, 1, 1))
        self.assertEqual(bucket["latest"], history["rows"][0])
        self.assertEqual(bucket["latest"]["status"], "unknown")
        self.assertTrue(all(b["latest"] is None for b in row["buckets"][:-1]))
        self.assertNotIn("never-expose-me", json.dumps(row))

    def test_pause_cancels_request_without_counting_a_failure(self):
        """Pause wakes the transport and leaves interrupted attempts out of the denominator."""
        ready = threading.Event()

        def pending(*args, cancel_event, **kwargs):
            """Block on the real cancellation event to verify stop propagation."""
            ready.set()
            cancel_event.wait(3)
            raise llm.LLMCancelled("cancelled")

        self.manager.probe = pending
        self.manager.check()
        self.assertTrue(ready.wait(2))
        self.assertFalse(self.manager.check()["started"])
        self.configure(False)
        self.manager.worker.join(3)
        row = self.manager.snapshot()["rows"][0]
        self.assertEqual((row["unknown"], row["total"]), (1, 0))
        self.assertIsNone(row["availability"])

    def test_recovery_preserves_success_and_marks_inflight_unknown(self):
        """Restart recovery must not silently turn an unfinished attempt into failure."""
        self.insert("success", time.time() - 60)
        self.insert("running", time.time() - 30)
        self.manager.close()
        recovered = module.Availability(self.manager.path, self.manager.site_loader, self.success)
        recovered.start_service()
        self.addCleanup(recovered.close)
        row = recovered.snapshot()["rows"][0]
        self.assertEqual((row["success"], row["unknown"], row["availability"]), (1, 1, 100))
        self.assertEqual(row["state"], "unknown")

    def test_window_and_pagination_keep_site_boundaries(self):
        """Filtering by site and time excludes older data and resists SQL-like names."""
        now = time.time()
        for i in range(55):
            self.insert("success", now - i - 1)
        self.insert("failure", now - 2 * 86400)
        self.assertEqual(self.manager.snapshot(24)["rows"][0]["total"], 55)
        self.assertEqual(self.manager.snapshot(168)["rows"][0]["total"], 56)
        history = self.manager.history(self.connection["name"], now - 600, now + 1, 2)
        self.assertEqual((history["total"], len(history["rows"])), (55, 5))
        self.assertEqual(self.manager.history("' OR 1=1 --", now - 600, now + 1)["total"], 0)
        with self.assertRaises(ValueError):
            self.manager.history(self.connection["name"], 0, now)

    def test_discovery_chooses_once_and_caches_the_model(self):
        """A chosen model stays stable across checks and does not rotate after a failure."""
        self.configure(False, "")
        self.connection["models"] = ["gpt-image-2", "gpt-6.1-sol"]
        self.run_check()
        self.assertEqual(self.manager.settings()["sites"][0]["model"], "gpt-6.1-sol")
        self.connection["models"] = ["other"]
        self.elapse_interval()
        self.run_check()
        self.assertEqual(self.manager.snapshot()["rows"][0]["latest"]["model"], "gpt-6.1-sol")

    def test_lightweight_defaults_preserve_explicit_model_choice(self):
        """Prefer small text models only for automatic selection, not saved user choices."""
        self.assertEqual(module.choose_model(["gpt-6-astra", "gpt-6.1-sol", "gpt-4o-mini", "gpt-image-2"]), "gpt-4o-mini")
        self.assertEqual(module.choose_model(["claude-fable-5", "claude-haiku-4-5"]), "claude-haiku-4-5")
        self.configure(False, "gpt-6-astra")
        self.connection["models"] = ["gpt-4o-mini"]
        self.run_check()
        self.assertEqual(self.manager.snapshot()["rows"][0]["latest"]["model"], "gpt-6-astra")

    def test_usage_counts_failed_generation_and_survives_restart(self):
        """A failed empty answer can be billed; cache and reasoning are not added twice."""
        self.manager.probe = lambda *a, **k: {"completed": True, "text": "", "usage": {
            "prompt_tokens": 100, "completion_tokens": 30, "reasoning_tokens": 28,
            "cached_tokens": 90, "estimated": False, "secret": "never-expose-me"}}
        self.run_check()
        row = self.manager.snapshot()["rows"][0]
        self.assertEqual(row["latest"]["status"], "failure")
        self.assertEqual(row["latest"]["usage"]["total_tokens"], 130)
        self.assertEqual(row["today_usage"]["total_tokens"], 130)
        self.assertNotIn("never-expose-me", json.dumps(row))
        self.manager.close()
        recovered = module.Availability(self.manager.path, self.manager.site_loader, self.success)
        recovered.start_service()
        self.addCleanup(recovered.close)
        history = recovered.history(self.connection["name"], time.time() - 600, time.time() + 1)
        self.assertEqual(history["rows"][0]["usage"], row["latest"]["usage"])

    def test_usage_missing_estimated_and_reported_zero_stay_distinct(self):
        """Only explicit reported counts contribute; missing data cannot imply free calls."""
        cases = [({}, "missing", None),
                 ({"prompt_tokens": 2, "completion_tokens": 3, "estimated": True}, "estimated", 5),
                 ({"prompt_tokens": 0, "completion_tokens": 0, "estimated": False}, "reported", 0),
                 ({"total_tokens": 9, "estimated": False}, "reported", 9),
                 ({"total_tokens": True, "prompt_tokens": -1, "completion_tokens": "5", "estimated": False}, "missing", None),
                 ({"total_tokens": 10**20, "estimated": False}, "missing", None)]
        now = time.time()
        for value, source, total in cases:
            usage = module.probe_usage(value)
            self.assertEqual((usage["source"], usage["total_tokens"]), (source, total))
            with self.manager.database() as db:
                db.execute("INSERT INTO samples(site,model,started_at,status,usage) VALUES(?,?,?,'success',?)",
                           (self.connection["name"], "chat", now, json.dumps(usage)))
        self.insert("running", now)
        row = self.manager.snapshot()["rows"][0]
        daily = row["today_usage"]
        self.assertEqual(daily["total_tokens"], 9)
        self.assertEqual(daily["estimated_tokens"], 5)
        self.assertEqual([daily[key] for key in ("requests", "reported_requests", "estimated_requests", "missing_requests", "running_requests")], [7, 2, 1, 3, 1])

    def test_today_uses_local_day_even_when_dst_day_exceeds_graph_window(self):
        """Calendar-day usage includes the extra DST hour and ignores yesterday and tomorrow."""
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                now = time.mktime((2026, 11, 1, 23, 30, 0, -1, -1, -1))
                start = time.mktime((2026, 11, 1, 0, 0, 0, -1, -1, -1))
                self.assertEqual(now - start, 24.5 * 3600)
                usage = module.probe_usage({"total_tokens": 10, "estimated": False})
                with self.manager.database() as db:
                    for timestamp in (start - 1, start + 60, now - 1, now + 3600):
                        db.execute("INSERT INTO samples(site,model,started_at,status,usage) VALUES(?,?,?,'success',?)",
                                   (self.connection["name"], "chat", timestamp, json.dumps(usage)))
                with patch.object(module.time, "time", return_value=now):
                    daily = self.manager.snapshot(24)["rows"][0]["today_usage"]
                    self.assertEqual((daily["start"], daily["requests"], daily["total_tokens"]), (start, 2, 20))
                    self.assertEqual(daily, self.manager.snapshot(720)["rows"][0]["today_usage"])
        finally:
            time.tzset()

    def test_legacy_database_migration_preserves_history_and_unknown_usage(self):
        """Adding usage neither rewrites old outcomes nor invents counters after restart."""
        path = Path(self.directory.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, site TEXT NOT NULL, model TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL, duration_seconds REAL, reason TEXT NOT NULL DEFAULT '')")
            db.execute("INSERT INTO samples VALUES(1,?,?,?,?,'success',1,'preserved')",
                       (self.connection["name"], "legacy-model", time.time() - 10, time.time() - 9))
        for _ in range(2):
            recovered = module.Availability(path, self.manager.site_loader, self.success)
            recovered.start_service()
            try:
                row = recovered.snapshot()["rows"][0]
                self.assertEqual((row["total"], row["latest"]["model"], row["latest"]["reason"]), (1, "legacy-model", "preserved"))
                self.assertIsNone(row["today_usage"]["total_tokens"])
                self.assertEqual(row["today_usage"]["missing_requests"], 1)
            finally:
                recovered.close()

    def test_invalid_settings_do_not_partially_write(self):
        """Reject forged site names, duplicate targets and nonboolean switches atomically."""
        before = self.manager.settings()
        for data in ({"enabled": 1, "sites": []}, {"enabled": True, "sites": []},
                     {"enabled": True, "sites": [{"site": "unknown", "model": "x", "enabled": True}]},
                     {"enabled": True, "sites": [{"site": "站点 A", "model": "x\n", "enabled": True}]}):
            with self.assertRaises(ValueError):
                self.manager.configure(data)
        self.assertEqual(before, self.manager.settings())

    def test_network_switch_defers_without_inserting_failure(self):
        """Do not test against a temporarily selected VPN route or invent an outage."""
        self.manager.blocked = lambda: True
        with self.assertRaises(ValueError):
            self.manager.check()
        self.assertEqual(self.manager.snapshot()["rows"][0]["total"], 0)

    def test_scheduler_samples_while_page_is_closed(self):
        """The service owns scheduling, so no browser polling is needed to collect history."""
        ready = threading.Event()

        def respond(*args, **kwargs):
            """Notify that the scheduler independently issued a model call."""
            ready.set()
            return self.success()

        self.manager.probe = respond
        self.configure(True)
        self.assertTrue(ready.wait(3))
        self.manager.worker.join(2)
        self.assertEqual(self.manager.snapshot()["rows"][0]["total"], 1)
        self.assertGreater(self.manager.next_at, time.time())

    def test_completed_and_failed_attempts_cannot_be_repeated_within_five_minutes(self):
        """Legacy manual calls share the same durable interval, including after failure."""
        self.assertTrue(self.run_check()["started"])
        last = self.manager.snapshot()["rows"][0]["latest"]["started_at"]
        with patch.object(module.time, "time", return_value=last + module.INTERVAL - 0.01):
            result = self.manager.check()
        self.assertEqual(result, {"started": False, "count": 0, "next_at": last + module.INTERVAL})
        self.elapse_interval()
        self.manager.probe = lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("HTTP 503"))
        self.assertTrue(self.run_check()["started"])
        for _ in range(3):
            self.assertFalse(self.manager.check()["started"])
        self.assertEqual(self.manager.snapshot()["rows"][0]["total"], 2)

    def test_settings_resume_and_restart_preserve_the_next_allowed_probe(self):
        """A model edit, resume or new process cannot turn a recent sample into a retry."""
        self.run_check()
        last = self.manager.snapshot()["rows"][0]["latest"]["started_at"]
        self.configure(True, "new-model")
        self.assertEqual(self.manager.next_at, last + module.INTERVAL)
        self.assertFalse(self.manager.check()["started"])
        self.configure(False)
        self.configure(True)
        self.assertEqual(self.manager.next_at, last + module.INTERVAL)
        self.manager.close()
        recovered = module.Availability(self.manager.path, self.manager.site_loader, self.success)
        recovered.start_service()
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.next_at, last + module.INTERVAL)
        self.assertFalse(recovered.check()["started"])
        self.assertEqual(recovered.snapshot()["rows"][0]["total"], 1)

    def test_unknown_attempt_also_respects_interval(self):
        """Cancelled or interrupted attempts remain unknown and are not immediately retried."""
        self.insert("unknown", time.time() - 30)
        self.assertFalse(self.manager.check()["started"])
        self.assertEqual(self.manager.snapshot()["rows"][0]["unknown"], 1)

    def test_http_requires_json_and_same_origin(self):
        """A third-party webpage cannot turn on usage-consuming background monitoring."""
        with patch.object(module, "manager", self.manager):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:" + str(server.server_port)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                for content, origin, expected in (("application/json", "https://evil.invalid", 403),
                                                  ("text/plain", base, 415)):
                    request = urllib.request.Request(base + "/api/availability/check", data=b"{}",
                        headers={"Content-Type": content, "Origin": origin})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        opener.open(request, timeout=3)
                    self.assertEqual(caught.exception.code, expected)
                    caught.exception.close()
                with opener.open(base + "/api/availability", timeout=3) as response:
                    self.assertNotIn("never-expose-me", response.read().decode())
            finally:
                server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
