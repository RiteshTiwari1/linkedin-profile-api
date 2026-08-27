"""SQLite-backed profile cache.

Caching is not a performance nicety here, it is the availability strategy. The
upstream budget is ~100 profile views a day across the whole service, so any
repeated request that reaches LinkedIn is a wasted unit of a scarce resource.

Two tiers off one stored copy:

* Younger than CACHE_TTL_SECONDS  -> fresh. Served immediately, LinkedIn is
  never contacted.
* Older than the TTL but younger than CACHE_STALE_MAX_SECONDS -> stale. Tried
  upstream first; if LinkedIn refuses, the stale copy is served with
  `meta.stale = true` and its real age. Degraded and honest beats a 502.

SQLite rather than Redis so the submission runs with zero external services,
and so a warmed cache can be baked into the deployment.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    public_identifier TEXT PRIMARY KEY,
    payload           TEXT NOT NULL,
    strategy          TEXT,
    fetched_at        REAL NOT NULL,
    hits              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_profiles_fetched_at ON profiles(fetched_at);
"""


class CacheEntry:
    __slots__ = ("age_seconds", "fetched_at", "payload", "strategy")

    def __init__(self, payload: dict[str, Any], fetched_at: float, strategy: str | None) -> None:
        self.payload = payload
        self.fetched_at = fetched_at
        self.strategy = strategy
        self.age_seconds = int(time.time() - fetched_at)

    def is_fresh(self, ttl: int) -> bool:
        return self.age_seconds < ttl

    def is_usable(self, stale_max: int) -> bool:
        return self.age_seconds < stale_max


class ProfileCache:
    def __init__(self, path: str, *, ttl_seconds: int, stale_max_seconds: int) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.stale_max_seconds = stale_max_seconds
        self._lock = threading.Lock()

        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + an explicit lock: FastAPI runs handlers on a
        # threadpool, and these writes are far too small to justify a pool.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, public_identifier: str) -> CacheEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at, strategy FROM profiles "
                "WHERE public_identifier = ?",
                (public_identifier.lower(),),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE profiles SET hits = hits + 1 WHERE public_identifier = ?",
                (public_identifier.lower(),),
            )
            self._conn.commit()
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return CacheEntry(payload, row[1], row[2])

    def set(self, public_identifier: str, payload: dict[str, Any], strategy: str | None) -> None:
        blob = json.dumps(payload, separators=(",", ":"), default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO profiles (public_identifier, payload, strategy, fetched_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(public_identifier) DO UPDATE SET "
                "payload = excluded.payload, strategy = excluded.strategy, "
                "fetched_at = excluded.fetched_at",
                (public_identifier.lower(), blob, strategy, time.time()),
            )
            self._conn.commit()
        self.purge_expired()

    def purge_expired(self) -> int:
        """Delete entries past the stale window.

        Data minimisation, not housekeeping. This cache holds the personal data
        of every person anyone looks up, and an entry older than
        `stale_max_seconds` can never be served again -- so keeping it serves no
        purpose and only widens what a disk or backup would expose. Called on
        startup and after each write, which is often enough for a service of
        this size and costs one indexed DELETE.
        """
        cutoff = time.time() - self.stale_max_seconds
        with self._lock:
            cur = self._conn.execute("DELETE FROM profiles WHERE fetched_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def purge_all(self) -> int:
        """Drop every cached profile. For a "forget everything" request."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM profiles")
            self._conn.commit()
            return cur.rowcount

    def delete(self, public_identifier: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM profiles WHERE public_identifier = ?",
                (public_identifier.lower(),),
            )
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0), MIN(fetched_at), MAX(fetched_at) "
                "FROM profiles"
            ).fetchone()
        count, hits, oldest, newest = row
        now = time.time()
        with self._lock:
            fresh = self._conn.execute(
                "SELECT COUNT(*) FROM profiles WHERE fetched_at > ?",
                (now - self.ttl_seconds,),
            ).fetchone()[0]
        return {
            "entries": count,
            "fresh_entries": fresh,
            "total_hits": hits,
            "oldest_age_seconds": int(now - oldest) if oldest else None,
            "newest_age_seconds": int(now - newest) if newest else None,
            "ttl_seconds": self.ttl_seconds,
            "stale_max_seconds": self.stale_max_seconds,
            "path": self.path,
        }

    def list_identifiers(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT public_identifier FROM profiles ORDER BY public_identifier"
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
