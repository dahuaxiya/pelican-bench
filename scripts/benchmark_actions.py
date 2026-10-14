"""Time every mutating HTTP route in a disposable workspace with external actions stubbed."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer


def audit(source, output, samples=3):
    """Exercise real dispatch, guards and local persistence; never launch paid or account work.

    Args:
        source: Installation or checkout to benchmark in a fresh interpreter.
        output: Timing-only artifact; no responses, account data or credentials are saved.
        samples: Number of successful requests per route.
    Returns:
        Timing rows. Assertions fail if a route errors or local persistence is not applied.
    """
    sys.path.insert(0, str(Path(source).resolve()))
    import app
    import config
    import official_channels
    rows = []
    with tempfile.TemporaryDirectory(prefix="pelican-api-audit-") as directory, ExitStack() as stack:
        root = Path(directory)
        for name, value in (("ROOT", str(root)), ("CONFIG_PATH", str(root / "config.json")),
                            ("RUNS_DIR", str(root / "runs"))):
            stack.enter_context(patch.object(config, name, value))
        stack.enter_context(patch.object(official_channels, "adapters", return_value=[]))
        stack.enter_context(patch.object(app, "_evaluation_busy", return_value=False))
        result = {"id": "a" * 32, "run_id": "a" * 32, "status": "completed"}
        # Workers, account providers and the controller are intentional external boundaries.
        # HTTP validation, routing, JSON, config saves and report generation stay real.
        for obj, methods in (
            (app.codex_probe.manager, ("start", "start_batch", "cancel", "snapshot")),
            (app.codex_auth.manager, ("start", "cancel", "logout")),
            (app.kimi_channel.manager, ("start", "cancel", "logout")),
            (app.iq, ("start", "cancel", "retry_failed", "get")),
            (app.iq_review, ("start", "schedule_run")),
            (app.swe, ("start", "cancel")),
            (app.network_probe.manager, ("configure", "start", "apply_best", "cancel", "discover", "recover")),
        ):
            for method in methods:
                stack.enter_context(patch.object(obj, method, return_value=result.copy()))
        stack.enter_context(patch.object(app.network_probe.manager, "busy", return_value=False))
        for manager in (app.codex_auth.manager, app.kimi_channel.manager):
            stack.enter_context(patch.object(manager, "pending", None))
        stack.enter_context(patch.object(app.bench, "start_run", return_value=Mock(run_id="fixture", total=1)))
        stack.enter_context(patch.object(app.Handler, "log_message", return_value=None))
        config.save({**config.DEFAULTS, "sites": [{"name": "fixture", "base_url": "http://127.0.0.1:1/v1", "models": ["model"]}]})
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        server.daemon_threads = True
        server.instance_id, server.control_token = "audit", "test-only-control"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        routes = [
            ("/api/config", {"sites": [{"name": "fixture", "base_url": "http://127.0.0.1:1/v1", "models": ["model"]}]}),
            ("/api/cli/site", {"action": "set", "name": "fixture", "models": ["model"]}),
            ("/api/probe", {"model": "model", "site": "fixture"}),
            ("/api/chatgpt/probe", {"model": "model"}),
            ("/api/probe/cancel", {}), ("/api/chatgpt/probe/cancel", {}),
            ("/api/kimi/login", {}), ("/api/kimi/cancel", {}), ("/api/kimi/logout", {}),
            ("/api/chatgpt/login", {}), ("/api/chatgpt/cancel", {}), ("/api/chatgpt/logout", {}),
            ("/api/iq/run", {}), ("/api/iq/cancel", {"run_id": "a" * 32}),
            ("/api/iq/review", {}), ("/api/iq/review-run", {"run_id": "a" * 32}),
            ("/api/iq/retry", {"run_id": "a" * 32}),
            ("/api/iq/review-settings", {"iq_auto_review_site": "fixture", "iq_auto_review_model": "model"}),
            ("/api/swe/run", {}), ("/api/swe/cancel", {"run_id": "a" * 32}),
            ("/api/run", {"tasks": [{"site": "fixture", "model": "model"}]}),
            ("/api/network/config", {}), ("/api/network/run", {}),
            ("/api/network/apply", {}), ("/api/network/cancel", {"run_id": "a" * 12}),
            ("/api/network/discover", {}), ("/api/network/recover", {}),
            ("/api/runtime/stop", {}),
        ]
        try:
            for path, body in routes:
                timings = []
                for _ in range(samples):
                    request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json", "X-Pelican-Control": server.control_token,
                                 "Origin": base, "X-Pelican-Instance": "audit"})
                    start = time.perf_counter()
                    # The stop route still verifies the private token and sets stopping;
                    # only its final shutdown callback is suppressed until all samples finish.
                    with patch.object(server, "shutdown"):
                        with opener.open(request, timeout=10) as response:
                            payload = json.load(response)
                            assert response.status == 200, (path, payload)
                    timings.append(round((time.perf_counter() - start) * 1000, 2))
                    server.stopping = False
                rows.append({"method": "POST", "path": path, "samples_ms": timings,
                             "median_ms": round(statistics.median(timings), 2), "status": 200,
                             "scope": "isolated-handler; external actions stubbed"})
            assert json.loads((root / "config.json").read_text())["iq_auto_review_model"] == "model"
            # Reports intentionally write an export: use a synthetic saved run, never live data.
            run_id = "20261014-000000-audit"
            run_dir = root / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "results.json").write_text(json.dumps({"run_id": run_id, "results": [],
                "config": {}, "running": False, "total": 0, "done": 0, "started_at": 1, "finished_at": 2}))
            timings = []
            for _ in range(samples):
                start = time.perf_counter()
                with opener.open(base + "/report/" + run_id, timeout=10) as response:
                    assert response.status == 200
                    response.read()
                timings.append(round((time.perf_counter() - start) * 1000, 2))
            assert (run_dir / "report.html").is_file()
            rows.append({"method": "GET", "path": "/report/<id>", "samples_ms": timings,
                         "median_ms": round(statistics.median(timings), 2), "status": 200,
                         "scope": "isolated real report persistence"})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
    Path(output).write_text(json.dumps({"source": str(source), "results": rows}, indent=2))
    print(json.dumps({"routes": len(rows), "max_median_ms": max(r["median_ms"] for r in rows)}))
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    audit(args.source, args.output)
