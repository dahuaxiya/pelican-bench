"""Local, site-level availability history. Importing this module never starts probes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
import threading
import time

import config
import codex_probe
import llm
import official_channels

INTERVAL = 300
TIMEOUT = 45
WINDOWS = (24, 168, 720)


def sites():
    """Read saved connections without refreshing official login state over the network.

    Returns:
        Private connection objects for the configured sites and built-in channels.
        Callers must never return these objects directly to the browser.
    """
    values = config.load(include_official=False)["sites"]
    values.extend({"name": channel.SITE, "base_url": channel.BASE_URL, "models": []}
                  for channel in official_channels.adapters())
    return {site["name"]: site for site in values}


def choose_model(models):
    """Choose one text model for a site probe, excluding known non-chat capabilities.

    Args:
        models: Upstream catalog or explicitly saved model list.
    Returns:
        A model identifier; an empty catalog raises ValueError instead of reporting
        a successful /models request as a successful model call.
    """
    candidates = [m for m in models if isinstance(m, str) and m.strip()
                  and not any(kind in m.lower() for kind in
                              ("embedding", "whisper", "tts", "dall-e", "image", "sora", "moderation"))]
    # Prefer lightweight text models only when the caller has no saved choice.
    # This is a capability-family heuristic, not a claim about a reseller's price.
    for preferred in ("gpt-4.1-nano", "gpt-4.1-mini", "gpt-4o-mini", "gpt-5.6-luna", "gpt-6-luna"):
        if preferred in candidates:
            return preferred
    lightweight = [m for m in candidates if re.search(r"(?:^|[-/_.])(mini|nano|haiku|flash|luna)(?:$|[-/_.])", m.lower())]
    if lightweight:
        return sorted(lightweight)[0]
    if not candidates:
        raise ValueError("没有可用的文本模型，请指定探测模型")
    return sorted(candidates)[0]


def probe_usage(value):
    """Keep only bounded token counters from the shared transport's usage result.

    Args:
        value: Normalized transport usage, possibly missing or estimated.
    Returns:
        Safe counters and a reported/estimated/missing source. Cached input and
        reasoning output are subsets; they are never added to the total again.
    """
    value = value if isinstance(value, dict) else {}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens")
    result = {key: value.get(key) if type(value.get(key)) is int and 0 <= value[key] <= 10**12 else None
              for key in fields}
    if result["total_tokens"] is None and all(result[key] is not None for key in fields[:2]):
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    result["source"] = ("missing" if result["total_tokens"] is None else
                        "reported" if value.get("estimated") is False else "estimated")
    return result


def public_sample(row):
    """Decode server-written usage for history and previews, retaining legacy unknowns.

    Args:
        row: A persisted sample from the migrated local database.
    Returns:
        Public sample with structured usage; old records are never backfilled.
    """
    result = dict(row)
    result["usage"] = json.loads(result["usage"]) or probe_usage(None)
    return result


class Availability:
    """Persist samples and run at most one bounded probe batch at a time."""

    def __init__(self, path, site_loader=sites, probe=None):
        """Prepare an inert manager; start_service owns recovery and scheduling.

        Args:
            path: Server-owned SQLite path, never supplied by an HTTP caller.
            site_loader: Read current private connection settings for each run.
            probe: Optional test transport, with the stream_chat call signature.
        """
        self.path = Path(path)
        self.site_loader = site_loader
        self.probe = probe or llm.stream_chat
        self.lock = threading.RLock()
        self.action_lock = threading.RLock()
        self.closed = threading.Event()
        self.cancel = threading.Event()
        self.worker = None
        self.scheduler = None
        self.active = set()
        self.next_at = 0
        self.error = ""
        self.blocked = lambda: False

    @contextmanager
    def database(self):
        """Open a short transaction; never hold SQLite locks across network calls."""
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start_service(self, action_lock=None, blocked=None):
        """Recover interrupted samples only after the app has bound its listening port.

        Args:
            action_lock: The app's shared account/network mutation lock.
            blocked: Reports network switching or app shutdown, during which probes
                are deferred and no fabricated failures are inserted.
        """
        with self.lock:
            if self.scheduler:
                return
            self.action_lock = action_lock or self.action_lock
            self.blocked = blocked or self.blocked
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.database() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS samples (
                        id INTEGER PRIMARY KEY, site TEXT NOT NULL, model TEXT NOT NULL,
                        started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                        duration_seconds REAL, reason TEXT NOT NULL DEFAULT '');
                    CREATE INDEX IF NOT EXISTS samples_site_time ON samples(site, started_at);
                    CREATE INDEX IF NOT EXISTS samples_time ON samples(started_at);
                """)
                # Add a nullable-in-meaning field without replacing history. Old
                # requests may have been billed; an absent counter is not zero.
                columns = {row["name"] for row in db.execute("PRAGMA table_info(samples)")}
                if "usage" not in columns:
                    db.execute("ALTER TABLE samples ADD COLUMN usage TEXT NOT NULL DEFAULT '{}'")
                db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)",
                           (json.dumps({"enabled": False, "sites": {}}),))
                # A killed process cannot tell whether its request succeeded upstream.
                # Preserve that uncertainty rather than worsening the site's uptime.
                db.execute("UPDATE samples SET status='unknown', finished_at=?, reason=? WHERE status='running'",
                           (time.time(), "服务重启，检测结果未知"))
                last = db.execute("SELECT MAX(started_at) FROM samples").fetchone()[0]
                self.next_at = max(time.time(), (last or 0) + INTERVAL)
            self.path.chmod(0o600)
            self.scheduler = threading.Thread(target=self._schedule, name="site-availability", daemon=True)
            self.scheduler.start()

    def settings(self):
        """Read durable switches and probe models, adding newly configured sites paused.

        Returns:
            Public settings with no addresses, keys, or login materials.
        """
        connections = self.site_loader()
        with self.database() as db:
            saved = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        return {"enabled": bool(saved["enabled"]), "interval_seconds": INTERVAL,
                "timeout_seconds": TIMEOUT, "sites": [
                    {"site": name, "enabled": bool(saved["sites"].get(name, {}).get("enabled", False)),
                     "model": saved["sites"].get(name, {}).get("model", "")}
                    for name in connections]}

    def configure(self, data):
        """Validate and atomically save monitoring settings; pausing cancels active probes.

        Args:
            data: Exactly enabled (boolean) and sites (site/enabled/model entries).
                Models may be blank to choose once from the real catalog. Saving or
                resuming never bypasses the durable five-minute detection interval.
        Returns:
            Saved public settings. Unknown sites or invalid types cause no write.
        """
        if (not isinstance(data, dict) or set(data) != {"enabled", "sites"}
                or type(data["enabled"]) is not bool or not isinstance(data["sites"], list)):
            raise ValueError("请提交监测开关和站点设置")
        connections, selected = self.site_loader(), {}
        for item in data["sites"]:
            if (not isinstance(item, dict) or set(item) != {"site", "enabled", "model"}
                    or not isinstance(item["site"], str) or item["site"] not in connections
                    or item["site"] in selected or type(item["enabled"]) is not bool
                    or not isinstance(item["model"], str) or len(item["model"]) > 256
                    or any(ord(c) < 32 for c in item["model"])):
                raise ValueError("站点或探测模型设置无效")
            selected[item["site"]] = {"enabled": item["enabled"], "model": item["model"].strip()}
        if data["enabled"] and not any(item["enabled"] for item in selected.values()):
            raise ValueError("请至少选择一个监测站点")
        with self.lock:
            if self.busy() and data["enabled"]:
                raise ValueError("检测正在进行，请等待结束或先暂停监测")
            with self.database() as db:
                db.execute("UPDATE settings SET value=? WHERE id=1",
                           (json.dumps({"enabled": data["enabled"], "sites": selected}),))
            if not data["enabled"]:
                self.cancel.set()
            self.next_at = max(time.time(), self._next_check_at())
        return self.settings()

    def _next_check_at(self):
        """Read the earliest allowed batch start from durable attempts.

        Returns:
            Last recorded attempt start plus five minutes, or zero before the first
            probe. Caller holds self.lock when making scheduling decisions.
        """
        with self.database() as db:
            last = db.execute("SELECT MAX(started_at) FROM samples").fetchone()[0]
        # Include failures, cancellations and the last queued site. They consumed
        # an attempt too; saving settings or restarting must not trigger a retry.
        return last + INTERVAL if last is not None else 0

    def busy(self):
        """Include the launch-to-worker gap so account changes cannot race a new probe."""
        with self.lock:
            return bool(self.active) or bool(self.worker and self.worker.is_alive())

    def check(self):
        """Launch a batch only after the durable five-minute interval has elapsed.

        Returns:
            Whether a new batch started and its enabled-site count. Calls do not
            wait for model discovery or generation, keeping the page responsive.
            Legacy manual callers get started=false and next_at during cooldown.
        """
        with self.action_lock, self.lock:
            if self.closed.is_set():
                raise ValueError("服务正在停止")
            if self.busy():
                return {"started": False, "count": len(self.active)}
            # Use persisted starts, not just the in-memory timer: this guard also
            # covers old browser tabs, concurrent calls, resume and service restart.
            due_at = self._next_check_at()
            if time.time() < due_at:
                self.next_at = due_at
                return {"started": False, "count": 0, "next_at": due_at}
            if self.blocked():
                raise ValueError("账号登录或节点切换正在进行，监测稍后继续")
            targets = [s for s in self.settings()["sites"] if s["enabled"]]
            if not targets:
                raise ValueError("请先选择监测站点")
            connections = self.site_loader()
            self.cancel = threading.Event()
            self.active = {s["site"] for s in targets}
            self.next_at = time.time() + INTERVAL
            self.error = ""
            self.worker = threading.Thread(target=self._run, args=(targets, connections, self.cancel),
                                           name="site-availability-batch", daemon=True)
            self.worker.start()
            return {"started": True, "count": len(targets)}

    def _schedule(self):
        """Check the durable switch without holding the account lock while sleeping."""
        while not self.closed.wait(1):
            try:
                if self.settings()["enabled"] and time.time() >= self.next_at and not self.busy():
                    self.check()
            except ValueError:
                # Route switching defers sampling. Missing time remains gray in history.
                self.next_at = time.time() + 10
            except (OSError, sqlite3.Error):
                self.error = "监测记录无法读写，请检查本地磁盘"
                self.next_at = time.time() + 30

    def _run(self, targets, connections, cancel):
        """Run a bounded batch, then schedule from its last actual request start.

        Args:
            targets: Public per-site settings frozen at launch.
            connections: Private connections frozen at launch, never persisted.
            cancel: This batch's cancellation signal; later batches cannot reset it.
        """
        try:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="availability") as pool:
                futures = [pool.submit(self._sample, target, connections[target["site"]], cancel)
                           for target in targets]
                for future in futures:
                    future.result()
        except Exception:
            # Storage/executor errors are a monitor fault, not a provider outage.
            self.error = "监测执行或保存失败，请检查本地服务日志"
        finally:
            with self.lock:
                self.active.clear()
                # Queued sites can start later than the batch. Waiting from the
                # last start keeps every site's interval at least five minutes.
                self.next_at = max(self.next_at, self._next_check_at())

    def _sample(self, target, connection, cancel):
        """Persist intent before calling a model, then save only sanitized outcome data.

        Args:
            target: Site name and optional explicit model.
            connection: Server-loaded credentials, used only by the transport.
            cancel: Cancels queued and in-flight requests; cancellation is unknown.
        """
        if cancel.is_set():
            return
        started, clock = time.time(), time.monotonic()
        model, status, reason = target["model"], "unknown", "检测未完成"
        usage = probe_usage(None)
        with self.database() as db:
            sample_id = db.execute("INSERT INTO samples(site,model,started_at,status) VALUES(?,?,?,'running')",
                                   (target["site"], model, started)).lastrowid
        try:
            if not model:
                channel = official_channels.adapter(connection["base_url"])
                if channel:
                    catalog = channel.catalog()
                    if not catalog.get("ready"):
                        raise llm.LLMError("账号未登录 [authentication]")
                    models = catalog.get("models", [])
                else:
                    models = connection.get("models") or llm.list_models(
                        connection["base_url"], connection.get("api_key", ""), timeout=10)
                model = choose_model(models)
                with self.lock, self.database() as db:
                    saved = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
                    entry = saved["sites"].get(target["site"])
                    # Cache the chosen model once. Do not silently rotate models on
                    # later failures or overwrite an explicitly edited selection.
                    if entry is not None and not entry["model"]:
                        entry["model"] = model
                        db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(saved),))
            if cancel.is_set():
                raise llm.LLMCancelled("已暂停")
            response = self.probe(connection["base_url"], connection.get("api_key", ""), model,
                                  codex_probe.PROMPT, max_tokens=1024, temperature=0.7, timeout=TIMEOUT,
                                  thinking_mode=connection.get("thinking_mode", "default"),
                                  retry_stream_options=False, cancel_event=cancel)
            # Empty/truncated answers can still consume tokens. Save usage before
            # classifying success and never replace missing values with estimates.
            usage = probe_usage(response.get("usage"))
            if response.get("completed") and not response.get("error") and not response.get("truncated") and str(response.get("text") or "").strip():
                status, reason = "success", "收到完整的非空回答"
            else:
                status, reason = "failure", "回答为空、被截断或未完整结束"
        except llm.LLMCancelled:
            reason = "监测已暂停，结果未知"
        except llm.LLMError as exc:
            status, reason = "failure", codex_probe._failure_category(str(exc))
        except ValueError:
            reason = "无法选择探测模型，请在监测设置中指定"
        except Exception:
            reason = "本机检测异常，无法判断站点状态"
        finally:
            with self.database() as db:
                db.execute("UPDATE samples SET model=?,finished_at=?,status=?,duration_seconds=?,reason=?,usage=? WHERE id=?",
                           (model, time.time(), status, round(time.monotonic() - clock, 3), reason,
                            json.dumps(usage), sample_id))
            with self.lock:
                self.active.discard(target["site"])

    def snapshot(self, hours=24):
        """Aggregate fixed time buckets; missing intervals never become successes.

        Args:
            hours: One of 24, 168, 720. Current state is derived independently of
                this window so an old successful sample cannot appear healthy.
        Returns:
            Site rows, 60 timeline buckets each, settings and monitor state.
            Each bucket includes its last sanitized sample for instant hover details.
            Today's usage always uses server-local midnight, independent of hours.
        """
        if hours not in WINDOWS:
            raise ValueError("请选择近 24 小时、7 天或 30 天")
        now, settings = time.time(), self.settings()
        local = time.localtime(now)
        # mktime with isdst=-1 resolves midnight's own offset, including DST days.
        # Attribute cross-midnight requests to their start day, just like history.
        day_start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, -1, -1, -1))
        start, width = now - hours * 3600, hours * 3600 / 60
        with self.lock:
            active, next_at, error = set(self.active), self.next_at, self.error
        with self.database() as db:
            records = db.execute("SELECT * FROM samples WHERE started_at>=? ORDER BY started_at,id",
                                 (start,)).fetchall()
            # A separate bounded query keeps 'today' independent of the graph
            # window, including the 25-hour day when daylight saving time ends.
            today_records = db.execute("SELECT * FROM samples WHERE started_at>=? AND started_at<=?",
                                       (day_start, now)).fetchall()
            names = set(s["site"] for s in settings["sites"])
            names.update(row[0] for row in db.execute("SELECT DISTINCT site FROM samples"))
            latest = {name: db.execute("SELECT * FROM samples WHERE site=? ORDER BY started_at DESC,id DESC LIMIT 1",
                                       (name,)).fetchone() for name in names}
        rows = {}
        selections = {s["site"]: s for s in settings["sites"]}
        for name in sorted(names, key=str.casefold):
            buckets = [{"start": start + i * width, "end": start + (i + 1) * width,
                        "success": 0, "failure": 0, "unknown": 0, "running": 0, "latest": None} for i in range(60)]
            item = public_sample(latest[name]) if latest[name] else None
            selected = selections.get(name, {"enabled": False, "model": ""})
            state = "running" if name in active else "unknown"
            if name not in active and item:
                state = item["status"] if now - item["started_at"] <= INTERVAL * 2 else "stale"
            rows[name] = {"site": name, "enabled": selected["enabled"], "model": selected["model"],
                          "configured": name in selections, "state": state, "latest": item,
                          "success": 0, "failure": 0, "unknown": 0, "running": 0, "buckets": buckets,
                          "today_usage": {"start": day_start, "requests": 0, "reported_requests": 0,
                                          "estimated_requests": 0, "missing_requests": 0, "running_requests": 0,
                                          "total_tokens": None, "estimated_tokens": None}}
        for sample in today_records:
            daily = rows[sample["site"]]["today_usage"]
            daily["requests"] += 1
            if sample["status"] == "running":
                daily["running_requests"] += 1
                continue
            usage = public_sample(sample)["usage"]
            daily[usage["source"] + "_requests"] += 1
            # Estimated counts remain separate from the reported total, including
            # estimates supplied by ordinary API fallback adapters.
            if usage["source"] != "missing":
                key = "total_tokens" if usage["source"] == "reported" else "estimated_tokens"
                daily[key] = (daily[key] or 0) + usage["total_tokens"]
        for sample in records:
            # Buckets cover [start, now]; tolerate a tiny wall-clock adjustment.
            index = min(59, max(0, int((sample["started_at"] - start) / width)))
            row = rows[sample["site"]]
            row[sample["status"]] += 1
            row["buckets"][index][sample["status"]] += 1
            # Keep the exact last attempt, including failures or unknown outcomes.
            # The tooltip must not present a bucket average as one real request.
            row["buckets"][index]["latest"] = public_sample(sample)
        for row in rows.values():
            total = row["success"] + row["failure"]
            row["availability"] = round(row["success"] / total * 100, 2) if total else None
            row["total"] = total
        return {"settings": settings, "rows": list(rows.values()), "hours": hours,
                "now": now, "start": start, "busy": self.busy(), "active_count": len(active),
                "next_at": next_at if settings["enabled"] else None, "error": error}

    def history(self, site, start, end, page=1):
        """Read a bounded page of raw samples for a clicked time bucket or site.

        Args:
            site: Exact site name; SQL values are always bound parameters.
            start: Inclusive Unix timestamp.
            end: Exclusive timestamp, later than start and within a 31-day span.
            page: One-based page number; fixed page size avoids huge responses.
        Returns:
            Up to 50 sanitized outcomes and the total matching count.
        """
        if not site or page < 1 or not 0 <= start < end or end - start > 31 * 86400:
            raise ValueError("历史查询范围无效")
        params = (site, start, end)
        with self.database() as db:
            total = db.execute("SELECT COUNT(*) FROM samples WHERE site=? AND started_at>=? AND started_at<?", params).fetchone()[0]
            rows = db.execute("SELECT * FROM samples WHERE site=? AND started_at>=? AND started_at<? ORDER BY started_at DESC,id DESC LIMIT 50 OFFSET ?",
                              (*params, (page - 1) * 50)).fetchall()
        return {"site": site, "rows": [public_sample(row) for row in rows], "total": total, "page": page, "page_size": 50}

    def close(self):
        """Cancel local work at shutdown; interrupted attempts remain unknown, never down."""
        self.closed.set()
        self.cancel.set()
        if self.scheduler:
            self.scheduler.join(timeout=2)
        if self.worker:
            self.worker.join(timeout=15)


manager = Availability(Path(config.ROOT) / ".local" / "availability.sqlite3")
