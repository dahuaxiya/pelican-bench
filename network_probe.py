"""Repeatable VPN rankings with durable selector ownership and local history."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import threading
import time
import uuid

from runtime_paths import DATA_DIR
from network_transport import Controller, Cancellation, load_auth, local_address, header_value, probe
import network_model

DEFAULTS = {"controller": "http://127.0.0.1:9090", "proxy": "http://127.0.0.1:7890", "group": "", "secret": ""}
DEFAULT_CONCURRENCY = 6


def save_json(path, value):
    """Atomically persist private state before any corresponding external mutation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".network-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    """A missing file is expected; corrupt recovery state must not be ignored."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def eligible(proxies, group):
    """Only directly selectable concrete leaves are tested; nested groups stay intact."""
    selected = proxies.get(group, {})
    if selected.get("type") != "Selector":
        raise ValueError("请先选择手动 Selector 组")
    return [name for name in selected.get("all", []) if name in proxies
            and not proxies[name].get("all")
            and str(proxies[name].get("type", "")).lower() not in ("direct", "reject", "rejectdrop", "pass", "selector", "urltest", "fallback", "loadbalance")]


def rank_nodes(rows, repeats):
    """Rank only equal, complete samples; partial/failed-only rows remain inspectable."""
    result = []
    for row in rows:
        samples = row.get("samples", [])
        good = [s["elapsed_ms"] for s in samples if s.get("ok")]
        result.append({**copy.deepcopy(row), "success": len(good), "attempts": len(samples),
                       "complete": len(samples) == repeats,
                       "success_rate": len(good) / len(samples) if samples else None,
                       "median_ms": statistics.median(good) if good else None,
                       "max_success_ms": max(good) if good else None,
                       "spread_ms": round(max(good) - min(good), 2) if len(good) > 1 else None,
                       "max_attempt_ms": max((s["elapsed_ms"] for s in samples), default=None)})
    result.sort(key=lambda r: (not r["complete"], -(r["success_rate"] or 0),
                               r["median_ms"] if r["median_ms"] is not None else math.inf,
                               r["max_success_ms"] if r["max_success_ms"] is not None else math.inf))
    position = 0
    for row in result:
        row["rank"] = None
        if row["complete"] and row["success"]:
            position += 1
            row["rank"] = position
    return result


def rank_results(rows, options):
    """Keep handshake and model metrics separate; missing usage never wins a speed rank."""
    result = rank_nodes(rows, options["repeats"])
    if options.get("mode", "handshake") != "model":
        return result
    def finite(value):
        """Reject booleans and non-finite measurements, including imported old history."""
        return type(value) in (int, float) and math.isfinite(value)

    for row in result:
        measured = [s["metrics"] for s in row["samples"] if s.get("ok") and
                    finite(s.get("metrics", {}).get("ttft_seconds")) and
                    finite(s.get("metrics", {}).get("end_to_end_tokens_per_second")) and
                    s["metrics"]["ttft_seconds"] >= 0 and s["metrics"]["end_to_end_tokens_per_second"] > 0]
        row.update(metric_samples=len(measured), rank=None,
                   median_ttft_ms=statistics.median(m["ttft_seconds"] * 1000 for m in measured) if measured else None,
                   max_ttft_ms=max((m["ttft_seconds"] * 1000 for m in measured), default=None),
                   median_tokens_per_second=statistics.median(m["end_to_end_tokens_per_second"] for m in measured) if measured else None)
    def order(row):
        """Require complete comparable samples, then prioritize reliability before speed."""
        valid = row["complete"] and row["success"] > 0 and row["metric_samples"] == row["success"]
        ttft = row["median_ttft_ms"] if row["median_ttft_ms"] is not None else math.inf
        speed = row["median_tokens_per_second"] or 0
        return (not valid, -(row["success_rate"] or 0),
                -speed if options.get("sort_by") == "throughput" else ttft,
                ttft if options.get("sort_by") == "throughput" else -speed)
    result.sort(key=order)
    position = 0
    for row in result:
        if row["complete"] and row["success"] > 0 and row["metric_samples"] == row["success"]:
            position += 1
            row["rank"] = position
    return result


class RouteLease:
    """Serialize all Pelican selectors on one controller, even across data directories."""

    def __init__(self, settings, root):
        """Canonicalize localhost so aliases cannot bypass the process-wide file lock."""
        key = hashlib.sha256(repr(local_address(settings["controller"])).encode()).hexdigest()[:20]
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / (key + ".json")
        self.fd = os.open(root / (key + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise ValueError("另一批节点测试正在使用此控制口") from None

    def close(self):
        """Release ownership only after recovery state has been saved or cleared."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def switch(self, controller, journal, node):
        """Write intent before PUT; timeouts leave enough information for safe recovery."""
        current = controller.selection(journal["group"])
        if current != journal["owned"]:
            raise ValueError("检测到外部切换，测试已停止并保留新的选择")
        journal["pending"] = node
        save_json(self.path, journal)
        controller.select(journal["group"], node)
        if controller.selection(journal["group"]) != node:
            raise ValueError("节点切换回读不一致，已停止测试")
        journal.update(owned=node, pending=None)
        save_json(self.path, journal)

    def restore(self, controller):
        """Compare before restoring; never overwrite a user's intervening selection.

        The controller has no compare-and-swap API. We check immediately before
        each PUT and after it, but another client choosing the exact same node is
        indistinguishable. Do not run another selector automation during a test.
        """
        journal = read_json(self.path)
        if not journal:
            return {"state": "not_needed"}
        current = controller.selection(journal["group"])
        if journal.get("committed"):
            # A verified explicit choice is permanent. Recovery after a crash must
            # retain it, while still respecting a later choice made outside Pelican.
            self.path.unlink()
            return {"state": "retained" if current == journal["owned"] else "external_change", "node": current}
        if current == journal["original"]:
            self.path.unlink()
            return {"state": "restored", "node": current}
        if current not in (journal["owned"], journal.get("pending")):
            self.path.unlink()
            return {"state": "external_change", "node": current}
        controller.select(journal["group"], journal["original"])
        current = controller.selection(journal["group"])
        if current != journal["original"]:
            raise ValueError("恢复回读不一致")
        self.path.unlink()
        return {"state": "restored", "node": current}

    def commit(self, controller, journal, application):
        """Durably retain a verified explicit choice; uncommitted intents still roll back."""
        if controller.selection(journal["group"]) != journal["owned"]:
            raise ValueError("检测到外部切换，已保留当前选择")
        # Persist the result with the commit marker so a crash before history is
        # updated can replay success without undoing the user's explicit choice.
        journal.update(committed=True, application=application)
        save_json(self.path, journal)


class RouteBinding:
    """Serialize selector changes until each socket has a verified, fixed outbound."""

    def __init__(self, controller, lease, journal, cancel):
        """Share one route mutex and failure across all samples and reconnects."""
        self.controller, self.lease, self.journal, self.cancel = controller, lease, journal, cancel
        self.lock = threading.Lock()
        self.failure = None

    @contextmanager
    def bind(self, node, check=None):
        """Hold ownership through CONNECT/TLS attribution; callers release before model IO.

        Existing tunnels retain their outbound when the selector changes. New
        tunnels, including SDK retries, must always enter this context again.
        """
        while not self.lock.acquire(timeout=.1):
            self.cancel.check()
            if check:
                check()
        try:
            self.cancel.check()
            if check:
                check()
            self.lease.switch(self.controller, self.journal, node)
            yield
            self._verify()
        except ValueError as exc:
            # A route failure in a relay worker must stop the whole batch even if
            # the official client catches the disconnected socket and retries.
            self.failure = str(exc)
            self.cancel.cancel()
            raise
        finally:
            self.lock.release()

    def _verify(self):
        """Check ownership while holding the route lock, avoiding our own switch races."""
        if self.controller.selection(self.journal["group"]) != self.journal["owned"]:
            raise ValueError("检测到外部切换，测试已停止并保留新的选择")

    def verify(self):
        """Validate completion against the last owned choice, not this sample's node."""
        with self.lock:
            self._verify()


class NetworkManager:
    """Own one bounded node benchmark and its recovery, progress and durable results."""

    def __init__(self, data_dir=DATA_DIR, lease_dir=None):
        """Allow isolated paths in tests; production locking is shared across installs."""
        self.root = Path(data_dir)
        self.config_path = self.root / ".local" / "network.json"
        self.history_dir = self.root / "network-runs"
        self.lease_dir = Path(lease_dir) if lease_dir else Path.home() / ".local" / "state" / "pelican-bench-network"
        self.lock = threading.RLock()
        self.thread = None
        self.cancel_token = Cancellation()
        self.data = None

    def settings(self, public=True):
        """Read configuration; controller secrets never leave the local backend."""
        value = {**DEFAULTS, **read_json(self.config_path, {})}
        if public:
            return {k: v for k, v in value.items() if k != "secret"} | {"secret_set": bool(value["secret"])}
        return value

    def configure(self, values):
        """Save explicit local endpoints, preserving a secret omitted by the caller."""
        with self.lock:
            if self.busy():
                raise ValueError("请先停止当前节点测试")
            if not isinstance(values, dict) or set(values) - set(DEFAULTS):
                raise ValueError("节点配置字段不受支持")
            settings = {**self.settings(False), **values}
            for key in ("controller", "proxy"):
                host, port = local_address(settings[key])
                settings[key] = f'http://{"[::1]" if host == "::1" else host}:{port}'
            if not isinstance(settings["group"], str) or len(settings["group"]) > 256:
                raise ValueError("组名不合法")
            if not isinstance(settings["secret"], str):
                raise ValueError("控制密钥需要是字符串")
            if settings["secret"]:
                header_value(settings["secret"])
            old = self.settings(False)
            lease = RouteLease(old, self.lease_dir)
            try:
                # Keep recovery reachable using the old controller/group; updating its
                # secret remains allowed so an authentication failure can be repaired.
                if lease.path.exists() and any(settings[k] != old[k] for k in ("controller", "group")):
                    raise ValueError("有待恢复的节点，请先恢复再更换控制地址或选择组")
            finally:
                lease.close()
            save_json(self.config_path, settings)
            return self.settings()

    def discover(self):
        """Read live groups and observed routing; don't infer it from GLOBAL selection."""
        settings = self.settings(False)
        controller = Controller(settings)
        proxies = controller.proxies()
        chains = []
        for connection in controller.request("/connections").get("connections", []):
            if connection.get("metadata", {}).get("host") == "chatgpt.com":
                chain = connection.get("chains", [])
                if chain not in chains:
                    chains.append(chain)
        groups = [{"name": name, "current": item.get("now"), "nodes": eligible(proxies, name)}
                  for name, item in proxies.items() if item.get("type") == "Selector"]
        groups = [group for group in groups if group["nodes"]]
        suggested = next((g["name"] for g in groups if any(g["name"] in c for c in chains)), "")
        try:
            load_auth()
            auth_ready = True
        except ValueError:
            auth_ready = False
        return {"groups": groups, "observed_chains": chains, "suggested_group": suggested,
                "auth_ready": auth_ready, "settings": self.settings()}

    def busy(self):
        """Keep tests and explicit switching exclusive through verification and recovery."""
        return bool(self.thread and self.thread.is_alive())

    def snapshot(self):
        """Return a copy so HTTP serialization never races with sample updates."""
        with self.lock:
            return copy.deepcopy(self.data) if self.data else {"status": "idle"}

    def _save(self):
        """Persist every transition; caller holds the manager lock."""
        save_json(self.history_dir / (self.data["id"] + ".json"), self.data)

    def update(self, **fields):
        """Publish progress without replacing the immutable batch identity."""
        with self.lock:
            self.data.update(fields)
            self._save()

    def start(self, options):
        """Validate the whole plan before taking a route lease or creating a batch."""
        with self.lock:
            if self.busy():
                raise ValueError("已有节点测试运行中")
            if not isinstance(options, dict) or set(options) - {"repeats", "top", "timeout", "nodes", "mode", "model", "sort_by", "concurrency"}:
                raise ValueError("节点测试参数不受支持")
            settings = self.settings(False)
            mode = options.get("mode", "handshake")
            if mode not in ("handshake", "model"):
                raise ValueError("请选择握手测速或模型实测")
            values = {"mode": mode, "repeats": options.get("repeats", 5), "top": options.get("top", 6),
                      "concurrency": options.get("concurrency", DEFAULT_CONCURRENCY),
                      "timeout": options.get("timeout", 180 if mode == "model" else 12)}
            for key, limits in (("repeats", (1, 20)), ("top", (1, 50)), ("concurrency", (1, 50)), ("timeout", (3, 600 if mode == "model" else 60))):
                value = values[key]
                if type(value) is not int or not limits[0] <= value <= limits[1]:
                    raise ValueError(f"{key} 需要在 {limits[0]}..{limits[1]} 之间")
            if mode == "model":
                catalog = network_model.catalog()
                if not catalog.get("ready"):
                    raise ValueError(catalog.get("error") or "请先在本站登录 ChatGPT")
                if not isinstance(options.get("model"), str) or options["model"] not in catalog["models"]:
                    raise ValueError("请选择账号目录中的官方模型")
                if options.get("sort_by", "ttft") not in ("ttft", "throughput"):
                    raise ValueError("排名依据需要是 ttft 或 throughput")
                values.update(model=options["model"], sort_by=options.get("sort_by", "ttft"),
                              prompt_version="network-explanation-v1", reasoning_effort="medium")
            auth = load_auth()
            controller = Controller(settings)
            proxies = controller.proxies()
            nodes = eligible(proxies, settings["group"])
            requested = options.get("nodes")
            if requested is not None:
                if not isinstance(requested, list) or not requested or any(n not in nodes for n in requested):
                    raise ValueError("nodes 必须是所选组内的具体节点")
                nodes = list(dict.fromkeys(requested))
            if not nodes or len(nodes) > 500:
                raise ValueError("选择组需要包含 1..500 个具体节点")
            lease = RouteLease(settings, self.lease_dir)
            try:
                if lease.path.exists():
                    raise ValueError("上次切换尚未恢复，请先点击恢复节点或执行 vpn recover")
                original = controller.selection(settings["group"])
                self.cancel_token = Cancellation()
                self.data = {"id": uuid.uuid4().hex[:12], "kind": "network", "status": "running",
                             "phase": "https", "created_at": time.time(), "settings": self.settings(),
                             "original": original, "options": values, "screened": 0, "screen_total": len(nodes),
                             "screening": [], "rows": [], "done": 0, "total": 0,
                             "restoration": {"state": "not_needed"}, "recommendations": []}
                self._save()
                self.thread = threading.Thread(target=self._run, args=(controller, lease, auth, nodes), daemon=True)
                self.thread.start()
            except Exception:
                lease.close()
                raise
            return self.snapshot()

    def apply_best(self, run_id=None):
        """Apply a displayed completed ranking, or the newest matching completed batch.

        Historical results nominate a candidate only. A fresh authenticated probe
        must succeed before retaining it; rollback uses the selection at click time.
        """
        with self.lock:
            if self.busy():
                raise ValueError("节点测试或切换仍在运行，请等待结束")
            settings = self.settings(False)
            keys = ("controller", "proxy", "group")
            if run_id is None:
                completed = [read_json(p) for p in self.history_dir.glob("*.json")]
                completed = [v for v in completed if v.get("status") == "completed" and
                             all(v.get("settings", {}).get(k) == settings[k] for k in keys)]
                value = max(completed, key=lambda v: v["created_at"], default=None)
            else:
                value = self.get(run_id)
            if not value or value.get("status") != "completed":
                raise ValueError("请先完成一次节点测试，再切换最优节点")
            if any(value.get("settings", {}).get(k) != settings[k] for k in keys):
                raise ValueError("当前代理配置与此批次不同，请重新测速")
            repeats = value["options"]["repeats"]
            # Recompute from samples rather than trusting a client-supplied node
            # or the cached recommendation. Partial and flaky rows cannot win.
            ranked = rank_results(value["rows"], value["options"])
            winner = next((r for r in ranked if r["rank"] and r["success"] == repeats), None)
            if not winner:
                raise ValueError("本轮没有全部成功且指标完整的节点，请重新测速")
            auth = load_auth()
            controller = Controller(settings)
            if winner["name"] not in eligible(controller.proxies(), settings["group"]):
                raise ValueError("推荐节点已不在当前选择组中，请重新测速")
            lease = RouteLease(settings, self.lease_dir)
            try:
                if lease.path.exists():
                    raise ValueError("有待恢复的节点，请先执行恢复节点")
                original = controller.selection(settings["group"])
                self.cancel_token = Cancellation()
                self.data = copy.deepcopy(value)
                self.data["application"] = {"id": uuid.uuid4().hex[:12], "status": "running",
                    "phase": "switching", "target": winner["name"], "previous": original,
                    "started_at": time.time(), "restoration": {"state": "pending"}}
                self._save()
                self.thread = threading.Thread(target=self._apply, args=(controller, lease, auth), daemon=True)
                self.thread.start()
            except Exception:
                lease.close()
                raise
            return self.snapshot()

    def _apply(self, controller, lease, auth):
        """Verify one explicit switch; commit success or restore the pre-click selection."""
        application = copy.deepcopy(self.data["application"])
        settings = self.data["settings"]
        journal = {"operation": "apply", "run_id": self.data["id"], "application_id": application["id"],
                   "group": settings["group"], "original": application["previous"],
                   "owned": application["previous"], "pending": None}
        try:
            self.cancel_token.check()
            lease.switch(controller, journal, application["target"])
            application["phase"] = "verifying"
            self.update(application=copy.deepcopy(application))
            sample = probe(auth, settings["proxy"], controller, application["target"], settings["group"],
                           self.cancel_token, min(60, self.data["options"]["timeout"]))
            application["verification"] = sample
            self.cancel_token.check()
            if not sample["ok"]:
                reason = "HTTP " + str(sample["status"]) if sample.get("status") else sample.get("error", sample["stage"])
                raise ValueError("推荐节点连接验证失败：" + reason)
            application.update(status="succeeded", finished_at=time.time())
            lease.commit(controller, journal, copy.deepcopy(application))
            # Save success before clearing the journal. Otherwise a crash between
            # journal deletion and history write would lose a committed choice.
            application.update(phase="finished", restoration={"state": "retained", "node": application["target"]})
            self.update(application=copy.deepcopy(application))
        except InterruptedError:
            application.update(status="cancelled", failure="已取消切换")
        except ValueError as exc:
            application.update(status="failed", failure=str(exc))
        except Exception as exc:
            application.update(status="failed", failure="节点切换异常：" + type(exc).__name__)
        finally:
            # Always recover even if saving UI progress fails. Only a durable commit
            # marker permits retaining the new route after restart or a lost response.
            try:
                restoration = lease.restore(controller)
            except Exception:
                restoration = {"state": "failed", "node": application["previous"]}
                application.update(status="failed", failure="节点状态核对失败，请检查 VPN 后点击恢复节点")
            finally:
                lease.close()
            if restoration["state"] == "external_change":
                application.update(status="failed", failure="检测到外部切换，已保留当前选择")
            application.update(phase="finished", finished_at=time.time(), restoration=restoration)
            self.update(application=application)

    def _run(self, controller, lease, auth, nodes):
        """Run equal rounds concurrently, pinning each connection before model or WS IO."""
        final_status, failure = "completed", ""
        settings, options, original = self.data["settings"], self.data["options"], self.data["original"]
        journal = {"group": settings["group"], "original": original, "owned": original, "pending": None,
                   "run_id": self.data["id"]}
        routes = RouteBinding(controller, lease, journal, self.cancel_token)
        try:
            screening = []
            # Prescreen requests are already node-specific and need no route lock.
            with ThreadPoolExecutor(max_workers=options["concurrency"]) as pool:
                futures = {pool.submit(controller.delay, node): node for node in nodes}
                for future in as_completed(futures):
                    if self.cancel_token.event.is_set():
                        for pending in futures:
                            pending.cancel()
                        raise InterruptedError()
                    screening.append({"name": futures[future], **future.result()})
                    self.update(screening=screening.copy(), screened=len(screening))
            self.cancel_token.check()
            candidates = [r["name"] for r in sorted(screening, key=lambda r: r["delay_ms"] or math.inf)
                          if r["delay_ms"] is not None][:options["top"]]
            if original in nodes and original not in candidates:
                candidates.append(original)
            if not candidates:
                raise ValueError("HTTPS 初筛全部失败，没有可验证的候选")
            rows = [{"name": name, "samples": [], "state": "queued"} for name in candidates]
            model_mode = options.get("mode") == "model"
            self.update(phase="model" if model_mode else "websocket", rows=rank_results(rows, options),
                        total=len(rows) * options["repeats"], restoration={"state": "pending"})
            def publish(row, **changes):
                """Mutate one row and publish a consistent multi-worker progress snapshot."""
                with self.lock:
                    row.update(changes)
                    self.update(rows=rank_results(rows, options), done=sum(len(r["samples"]) for r in rows),
                                running=sum(r["state"] == "running" for r in rows),
                                queued=sum(r["state"] == "queued" for r in rows))

            def sample_row(row, round_index):
                """Measure once per round; never allow one node to overlap its own samples."""
                self.cancel_token.check()
                publish(row, state="running", round=round_index + 1, started_at=time.time(), output_observation=None)
                guard = lambda **kwargs: routes.bind(row["name"], **kwargs)
                try:
                    if model_mode:
                        sample = network_model.probe_model(settings["proxy"], controller, row["name"], settings["group"],
                            self.cancel_token, options["model"], options["timeout"],
                            on_output=lambda event: publish(row, output_observation=event), route_guard=guard)
                    else:
                        sample = probe(auth, settings["proxy"], controller, row["name"], settings["group"],
                                       self.cancel_token, options["timeout"], route_guard=guard)
                    self.cancel_token.check()
                    routes.verify()
                    with self.lock:
                        row["samples"].append(sample)
                        publish(row, state="completed")
                    if sample["stage"] == "route":
                        raise ValueError("请求没有确认经过所选节点，请检查选择组、代理入口和规则")
                    status = sample.get("status", 0)
                    if status == 401:
                        raise ValueError("官方认证已失效，请重新登录后测试")
                    if status == 429 or status >= 500:
                        raise ValueError(f"官方返回 HTTP {status}，停止测试以免把上游故障算成节点速度")
                    if model_mode and any(tag in sample.get("error", "") for tag in
                                          ("[authentication]", "[quota]", "[rate_limit]", "[capacity]")):
                        raise ValueError("停止模型实测：" + sample["error"])
                except Exception:
                    # Stop peers before the executor drains; waiting for its context
                    # to exit first would leave live model requests running on failure.
                    self.cancel_token.cancel()
                    raise

            with ThreadPoolExecutor(max_workers=options["concurrency"]) as pool:
                for round_index in range(options["repeats"]):
                    with self.lock:
                        for row in rows:
                            row.update(state="queued", output_observation=None)
                        publish(rows[0])
                    futures = [pool.submit(sample_row, row, round_index) for row in rows]
                    errors = []
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            errors.append(exc)
                            self.cancel_token.cancel()
                    if errors:
                        # Cancellation from a peer can finish first. Preserve the
                        # real failure instead of mislabelling it a user cancellation.
                        raise next((e for e in errors if not isinstance(e, InterruptedError)), errors[0])
        except InterruptedError:
            final_status, failure = ("failed", routes.failure) if routes.failure else ("cancelled", "")
        except ValueError as exc:
            final_status, failure = "failed", str(exc)
        except Exception as exc:
            final_status, failure = "failed", "节点测试异常：" + type(exc).__name__
        finally:
            # Recovery must still run when progress persistence fails, and uses no
            # cancellation token because restoring is required after a stop request.
            try:
                self.update(phase="restoring")
            finally:
                try:
                    restoration = lease.restore(controller)
                except Exception:
                    restoration = {"state": "failed", "node": original}
                    final_status = "failed"
                    failure = "恢复原节点失败；检查 VPN 后点击恢复节点，或执行 pelican-bench vpn recover"
                finally:
                    lease.close()
            if restoration["state"] == "external_change":
                final_status, failure = "failed", "检测到外部切换，已保留当前节点，没有覆盖你的选择"
            good = [r["name"] for r in self.data["rows"] if r["rank"] and r["success"] == options["repeats"]]
            with self.lock:
                for row in self.data["rows"]:
                    if row.get("state") in ("running", "queued"):
                        row["state"] = "stopped"
                self.update(status=final_status, phase="finished", finished_at=time.time(), current_node=None, running=0, queued=0,
                            restoration=restoration, failure=failure,
                            recommendations=good[:2] if final_status == "completed" else [])

    def cancel(self, run_id):
        """Stop this batch or its switch verification without changing benchmark scores."""
        with self.lock:
            if not self.data or run_id != self.data["id"]:
                raise ValueError("该批次不是当前任务")
            if self.busy():
                self.cancel_token.cancel()
                application = self.data.get("application", {})
                if application.get("status") in ("running", "stopping"):
                    self.update(application={**application, "status": "stopping"})
                else:
                    self.update(status="stopping")
            return self.snapshot()

    def get(self, run_id):
        """Allow only generated IDs; never interpret an ID as a filesystem path."""
        if not isinstance(run_id, str) or len(run_id) != 12 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("批次 ID 不合法")
        with self.lock:
            if self.data and self.data["id"] == run_id:
                return self.snapshot()
        return read_json(self.history_dir / (run_id + ".json"))

    def history(self, page=1):
        """Paginate summaries while retaining full measurements on disk indefinitely."""
        page = max(1, int(page))
        paths = sorted(self.history_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        items = []
        for path in paths[(page - 1) * 20:page * 20]:
            value = read_json(path)
            items.append({k: value.get(k) for k in ("id", "status", "created_at", "done", "total", "original", "recommendations", "restoration", "options")})
        return {"items": items, "page": page, "total": len(paths)}

    def recover(self):
        """Recover route ownership; applying a ranking never invalidates its measurements."""
        with self.lock:
            if self.busy():
                raise ValueError("测试仍在运行，请先取消并等待恢复")
            settings = self.settings(False)
            lease = RouteLease(settings, self.lease_dir)
            try:
                journal = read_json(lease.path)
                result = lease.restore(Controller(settings))
                if journal:
                    value = self.get(journal["run_id"])
                    if value:
                        if journal.get("operation") == "apply":
                            application = journal.get("application") or value.get("application", {})
                            application.update(status="succeeded" if result["state"] == "retained" else "interrupted",
                                               phase="finished", restoration=result, finished_at=time.time())
                            value["application"] = application
                        else:
                            value.update(status="interrupted", phase="finished", restoration=result, finished_at=time.time(), recommendations=[])
                        save_json(self.history_dir / (value["id"] + ".json"), value)
                        if self.data and self.data["id"] == value["id"]:
                            self.data = value
                return result
            finally:
                lease.close()

    def restore_history(self):
        """Recover orphaned tests and explicit switches, preserving completed rankings."""
        orphaned_applications = []
        for path in self.history_dir.glob("*.json"):
            value = read_json(path)
            if value.get("status") in ("running", "stopping"):
                value.update(status="interrupted", phase="finished", recommendations=[], finished_at=time.time())
                save_json(path, value)
            application = value.get("application", {})
            if application.get("status") in ("running", "stopping"):
                application.update(status="interrupted", phase="finished", finished_at=time.time())
                save_json(path, value)
                orphaned_applications.append(path)
        try:
            result = self.recover()
            if result["state"] == "not_needed":
                # A crash before the first PUT has no journal and no route to undo.
                # Don't leave an endless "recover" action on an untouched selector.
                for path in orphaned_applications:
                    value = read_json(path)
                    value["application"]["restoration"] = result
                    save_json(path, value)
        except Exception:
            # Keep recovery failure visible after a crash; the retained journal also
            # blocks a new mutation until the original controller can be reached.
            listing = self.history()
            if listing["items"]:
                value = self.get(listing["items"][0]["id"])
                if value.get("restoration", {}).get("state") == "pending":
                    value["restoration"] = {"state": "failed", "node": value["original"]}
                    save_json(self.history_dir / (value["id"] + ".json"), value)
                if value.get("application", {}).get("restoration", {}).get("state") == "pending":
                    value["application"]["restoration"] = {"state": "failed", "node": value["application"]["previous"]}
                    save_json(self.history_dir / (value["id"] + ".json"), value)
        listing = self.history()
        if listing["items"]:
            self.data = self.get(listing["items"][0]["id"])

    def close(self):
        """On process shutdown wait for bounded probes and restoration before exiting."""
        self.cancel_token.cancel()
        if self.thread:
            self.thread.join(timeout=20)


manager = NetworkManager()
