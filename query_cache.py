"""Bounded caches for derived public history; source files always remain authoritative."""
from collections import OrderedDict
import copy
import hashlib
import json
from pathlib import Path
import os
import tempfile
import threading


def fingerprint(path):
    """Identify a source revision, including atomic replacement with preserved mtime."""
    stat = os.stat(path)
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def code_revision(*paths):
    """Invalidate derived disk views when any participating implementation changes."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()


class QueryCache:
    """Cache independent JSON snapshots with bounded memory and optional restart reuse."""

    def __init__(self, entries=128, byte_limit=32 * 1024 * 1024):
        """Set total memory bounds; large individual results are returned without retention."""
        self.entries, self.byte_limit = entries, byte_limit
        self.values, self.size = OrderedDict(), 0
        self.lock = threading.RLock()

    def get(self, key, revision, build, directory=None, *, copy_result=True):
        """Return a private copy for this exact revision, rebuilding missing/corrupt views.

        Args:
            key: Stable source identity; never a user supplied disk path.
            revision: JSON-compatible dependency signature, including code versions.
            build: Pure local calculation; must not make remote calls or mutate source data.
            directory: Optional disposable cache directory; only public derived data belongs here.
            copy_result: False is reserved for internal readers that never modify returned objects.
        Returns:
            Independent JSON data. Cache I/O failures fall back to source calculation.
        """
        signature = hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()
        cache_key = str(key)
        # One computation per revision avoids simultaneous page requests parsing the same
        # large history repeatedly. This lock never guards task execution or network I/O.
        with self.lock:
            entry = self.values.get(cache_key)
            if entry and entry[0] == signature:
                self.values.move_to_end(cache_key)
                return copy.deepcopy(entry[1]) if copy_result else entry[1]
            target = (Path(directory) / (hashlib.sha256(cache_key.encode()).hexdigest() + ".json")) if directory else None
            value, found = None, False
            if target:
                try:
                    saved = json.loads(target.read_bytes())
                    if saved["revision"] == signature:
                        value, found = saved["value"], True
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Disposable cache corruption must not hide a valid source record.
            if not found:
                value = build()
            encoded = json.dumps({"revision": signature, "value": value}, ensure_ascii=False).encode()
            if target and not found:
                self._save(target, encoded)
            previous = self.values.pop(cache_key, None)
            if previous:
                self.size -= previous[2]
            if len(encoded) <= self.byte_limit:
                self.values[cache_key] = (signature, value, len(encoded))
                self.size += len(encoded)
                while len(self.values) > self.entries or self.size > self.byte_limit:
                    self.size -= self.values.popitem(last=False)[1][2]
            return copy.deepcopy(value) if copy_result else value

    def _save(self, target, encoded):
        """Atomically replace a derived view; keep disk failures out of query semantics."""
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(encoded)
            os.replace(temporary, target)
            # One file per source identity, plus a cap for deleted historical sources.
            paths = sorted(target.parent.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            for path in paths[self.entries:]:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
