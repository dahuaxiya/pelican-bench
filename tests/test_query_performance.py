"""Cache freshness and control responsiveness using isolated files and no real model calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import app
import bench
import config
import iq
import iq_review
import swe
from query_cache import QueryCache, fingerprint
from test_iq_history_cache import sample_run


class QueryPerformanceTests(unittest.TestCase):
    """Derived caches must never become the source of truth for results or live state."""

    def setUp(self):
        """Keep cache, config and history writes outside the developer's real workspace."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_restart_cache_corruption_revision_and_bounds(self):
        """Restart reuses a matching view, corruption rebuilds, and memory stays bounded."""
        directory = self.root / "cache"
        build = Mock(return_value={"score": [1]})
        cache = QueryCache(entries=2)
        self.assertEqual(cache.get("run", "v1", build, directory), {"score": [1]})
        result = QueryCache().get("run", "v1", build, directory)
        self.assertEqual(build.call_count, 1)
        result["score"].append(0)
        self.assertEqual(cache.get("run", "v1", build)["score"], [1])
        cache.get("run", "v2", build, directory)
        self.assertEqual(build.call_count, 2)
        next(directory.glob("*.json")).write_text("broken")
        QueryCache().get("run", "v2", build, directory)
        self.assertEqual(build.call_count, 3)
        for i in range(5):
            cache.get(str(i), 1, build, directory)
        self.assertEqual(len(cache.values), 2)
        self.assertEqual(len(list(directory.glob("*.json"))), 2)
        tiny = QueryCache(byte_limit=1)
        self.assertEqual(tiny.get("large", 1, build), {"score": [1]})
        self.assertFalse(tiny.values)

    def test_concurrent_cache_miss_builds_once_and_io_failure_falls_back(self):
        """Concurrent readers share computation; an unwritable cache still returns source data."""
        cache, build = QueryCache(), Mock(return_value={"done": 1})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: cache.get("run", 1, build), range(16)))
        self.assertEqual(build.call_count, 1)
        self.assertTrue(all(result == {"done": 1} for result in results))
        blocked = self.root / "not-a-directory"
        blocked.write_text("file")
        self.assertEqual(QueryCache().get("run", 1, build, blocked), {"done": 1})

    def test_svg_history_replacement_deletion_and_copy_isolation(self):
        """Lists, gallery and individual SVGs agree after external atomic replacement."""
        with patch.object(config, "RUNS_DIR", str(self.root)), patch.object(bench, "_RUNS", {}):
            target = self.root / "20261014-fixture" / "results.json"
            target.parent.mkdir()
            data = {"total": 1, "done": 1, "finished_at": 2, "results": [
                {"site": "s", "model": "m", "ok": True, "svg_bytes": 8, "svg": "<svg/>"}]}
            target.write_text(json.dumps(data))
            before = fingerprint(target)
            self.assertEqual(bench.list_runs()[0]["done"], 1)
            self.assertEqual(bench.latest_gallery()["cells"][0]["svg_bytes"], 8)
            bench.load_run_file(target.parent.name)["results"].clear()
            self.assertEqual(bench.run_svg(target.parent.name, "s", "m"), "<svg/>")
            data["results"][0].update(svg="<svg />", svg_bytes=9)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(data))
            os.utime(temporary, ns=(before[0], before[0]))
            temporary.replace(target)
            self.assertEqual(bench.latest_gallery()["cells"][0]["svg_bytes"], 9)
            self.assertEqual(bench.run_svg(target.parent.name, "s", "m"), "<svg />")
            target.unlink()
            self.assertEqual(bench.list_runs(), [])
            self.assertEqual(bench.latest_gallery()["cells"], [])

    def test_iq_restart_summary_matches_full_and_revalidates_reviews(self):
        """A new process cache skips scoring only while answers and review evidence still match."""
        data = sample_run()
        with patch.object(iq, "ROOT", str(self.root / "iq")), patch.object(iq, "_RUNS", {}), \
                patch.object(iq_review, "ROOT", self.root / "reviews"), \
                patch.object(iq_review, "_JOBS", {}), patch.object(iq_review, "_BATCHES", {}):
            iq._persist(data)
            first = iq.get(data["run_id"])
            with patch.object(iq, "_SUMMARY_CACHE", iq.OrderedDict()), \
                    patch.object(iq, "_DISK_SUMMARIES", QueryCache()), \
                    patch.object(iq, "summaries", wraps=iq.summaries) as calculate:
                self.assertEqual(iq.get(data["run_id"]), first)
                calculate.assert_not_called()
                # Full detail remains authoritative and contains the original answer.
                self.assertEqual(iq.get(data["run_id"], full=True)["results"][0]["raw"], "2")
                data["results"][0].update(raw="1", score=1, status="correct")
                iq._persist(data)
                self.assertEqual(iq.get(data["run_id"])["summary"][0]["final"]["rank_score"], 100)
                self.assertEqual(calculate.call_count, 2)

    def test_environment_refresh_and_model_cache_credentials(self):
        """Task admission checks again; changed endpoint credentials cannot reuse an old catalog."""
        with patch.object(swe, "_ENV_CHECK", None), patch.object(swe, "_environment_issues", return_value=[]) as checks:
            swe.environment()
            swe.environment()
            self.assertEqual(checks.call_count, 1)
            swe.environment(refresh=True)
            self.assertEqual(checks.call_count, 2)
        site = {"base_url": "http://127.0.0.1:1/v1", "api_key": "test-one"}
        with patch.object(app, "_MODEL_RESULTS", app.OrderedDict()), \
                patch.object(app.llm, "list_models", return_value=["model"]) as fetch:
            app._model_catalog(site)
            app._model_catalog(site)
            self.assertEqual(fetch.call_count, 1)
            app._model_catalog(site, refresh=True)
            app._model_catalog({**site, "api_key": "test-two"})
            self.assertEqual(fetch.call_count, 3)

    def test_swe_history_does_not_cache_live_or_changed_results(self):
        """Large evidence is omitted only in summaries; file updates and live progress remain visible."""
        run_id = "c" * 32
        data = {"run_id": run_id, "status": "completed", "done": 1,
                "results": [{"status": "unresolved", "patch": "large patch"}]}
        with patch.object(swe, "ROOT", self.root), patch.object(swe, "_RUNS", {}), \
                patch.object(swe, "summarize", return_value=[]):
            swe.save(data)
            self.assertNotIn("patch", swe.get(run_id)["results"][0])
            self.assertEqual(swe.get(run_id, full=True)["results"][0]["patch"], "large patch")
            data["results"][0]["status"] = "resolved"
            swe.save(data)
            self.assertEqual(swe.get(run_id)["results"][0]["status"], "resolved")
            swe._RUNS[run_id] = {"data": {**copy.deepcopy(data), "status": "running", "done": 2}}
            self.assertEqual(swe.get(run_id)["done"], 2)

    def test_cancel_does_not_wait_for_unrelated_action_and_keeps_origin_guard(self):
        """A held login/controller lock cannot stall cancel; cross-origin cancels still fail."""
        with patch.object(app.codex_probe.manager, "cancel", return_value={"status": "stopping"}) as cancel, \
                patch.object(app.Handler, "log_message", return_value=None):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with app._ACCOUNT_ACTION_LOCK:
                    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/probe/cancel",
                        data=b"{}", headers={"Content-Type": "application/json"})
                    with opener.open(request, timeout=1) as response:
                        self.assertEqual(json.load(response)["status"], "stopping")
                    request.add_header("Origin", "https://other.example")
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        opener.open(request, timeout=1)
                    self.assertEqual(failure.exception.code, 403)
                    failure.exception.close()
                cancel.assert_called_once()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
