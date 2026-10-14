"""Measure real read-only routes without saving responses or invoking model generation."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


def measure(base, path, count):
    """Return timing samples and the last body for discovery; never persist that body."""
    samples, payload = [], None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(count):
        start = time.perf_counter()
        try:
            response = opener.open(base + path, timeout=65)
        except urllib.error.HTTPError as exc:
            response = exc
        except OSError as exc:
            samples.append({"ms": round((time.perf_counter() - start) * 1000, 2),
                            "status": type(exc).__name__, "bytes": 0})
            continue
        with response:
            body = response.read()
            samples.append({"ms": round((time.perf_counter() - start) * 1000, 2),
                            "status": response.status, "bytes": len(body),
                            "sha256": hashlib.sha256(body).hexdigest()})
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
    return {"path": path, "samples": samples,
            "median_ms": round(statistics.median(s["ms"] for s in samples), 2)}, payload


def main():
    """Exercise query routes using existing IDs; mutation and report routes are isolated elsewhere."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8780")
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--plan", help="Reuse exact paths from a previous report")
    parser.add_argument("--data-dir", help="Also sample the largest saved run in each engine")
    args = parser.parse_args()
    paths = ["/api/runtime", "/api/runtime/status", "/api/config", "/api/chatgpt/status",
             "/api/kimi/status", "/api/network/config", "/api/network/models", "/api/network",
             "/api/network/history", "/api/probe", "/api/chatgpt/probe", "/api/probe/compare",
             "/api/probe/history", "/api/iq/suites", "/api/iq/runs",
             "/api/iq/leaderboard?benchmark=bbeh-mini&scoring=comparison", "/api/iq/reviews",
             "/api/swe/environment", "/api/swe/runs", "/api/swe/leaderboard",
             "/api/runs", "/api/gallery", "/", "/index.html", "/static/app.js", "/static/network.js"]
    results, values = [], {}
    def visit(path):
        """Print only measurements and retain discovery data in process memory."""
        row, payload = measure(args.base, path, args.samples)
        results.append(row)
        values[path] = payload
        print(f'{row["median_ms"]:10.2f} ms {path} {[s["status"] for s in row["samples"]]}', flush=True)
        # Save incrementally so a slow external catalog cannot lose prior measurements.
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps({"base": args.base, "results": results}, indent=2))
    if args.plan:
        for row in json.loads(Path(args.plan).read_text())["results"]:
            visit(row["path"])
        return
    for path in paths:
        visit(path)
    for family in ("iq", "swe"):
        runs = (values.get(f"/api/{family}/runs") or {}).get("runs", [])
        for run in runs[:1]:
            run_id = run["run_id"]
            visit(f"/api/{family}/run/{run_id}")
            visit(f"/api/{family}/run/{run_id}?full=1")
            if family == "iq":
                path = "/api/iq/reviews?run_id=" + run_id
                visit(path)
                for job in (values[path] or {}).get("reviews", [])[:1]:
                    visit("/api/iq/review/" + job["id"])
    for run in (values.get("/api/runs") or {}).get("runs", [])[:1]:
        visit("/api/run/" + run["run_id"])
        visit("/api/run/" + run["run_id"] + "?full=1")
    for cell in (values.get("/api/gallery") or {}).get("cells", [])[:1]:
        visit("/api/gallery/svg?" + urllib.parse.urlencode({k: cell[k] for k in ("run_id", "site", "model")}))
        visit("/api/gallery/history?" + urllib.parse.urlencode({k: cell[k] for k in ("site", "model")}))
    for family, key, route in (("network", "runs", "/api/network/run/"),
                               ("probe", "entries", "/api/probe/history/")):
        data = values.get(f"/api/{family}/history") or {}
        for run in data.get(key, data.get("items", []))[:1]:
            visit(route + run.get("run_id", run.get("id")))
    for site in (values.get("/api/config") or {}).get("sites", []):
        visit("/api/models?" + urllib.parse.urlencode({"site": site["name"]}))
    if args.data_dir:
        for pattern, route in (("iq-runs/*.json", "/api/iq/run/"),
                               ("swe-runs/*/run.json", "/api/swe/run/"),
                               ("runs/*/results.json", "/api/run/")):
            files = list(Path(args.data_dir).glob(pattern))
            if files:
                largest = max(files, key=lambda path: path.stat().st_size)
                run_id = largest.stem if pattern.startswith("iq") else largest.parent.name
                for suffix in ("", "?full=1"):
                    if route + run_id + suffix not in values:
                        visit(route + run_id + suffix)


if __name__ == "__main__":
    main()
