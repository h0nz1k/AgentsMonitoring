"""Tiny SQLite uptime store — powers per-service SLA + uptime + timeline.

One table, ``probes(ts, service, up, latency, detail)``: every probe of a monitored service
appends a row. From that we derive current status, current uptime streak, SLA over a window, and
a bucketed timeline for the dashboard. Standard library only (sqlite3, WAL mode for safe
concurrent read while the probe thread writes).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

#: Default raw-probe retention (seconds). Overridable via ``probe.retention_days``.
RETENTION_SECONDS = 30 * 86400
#: Prune at most once per day (wall clock).
PRUNE_EVERY = 86400

_local = threading.local()
_last_prune = 0.0
_prune_lock = threading.Lock()


def _path() -> str:
    return str(config.state_dir() / "uptime.sqlite")


def _retention_seconds() -> int:
    try:
        days = int((config.load().get("probe") or {}).get("retention_days", 30))
    except (TypeError, ValueError):
        days = 30
    return max(1, days) * 86400


def _conn() -> sqlite3.Connection:
    """One live connection per thread (DDL once). Re-opens when the state-dir path changes."""
    path = _path()
    c = getattr(_local, "conn", None)
    if c is not None and getattr(_local, "path", None) == path:
        return c
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _local.conn = None
    c = sqlite3.connect(path, timeout=10)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("""CREATE TABLE IF NOT EXISTS probes(
            ts INTEGER NOT NULL, service TEXT NOT NULL, up INTEGER NOT NULL,
            latency REAL, detail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_probes_service_ts ON probes(service, ts)")
    except BaseException:
        c.close()
        raise
    _local.conn = c
    _local.path = path
    return c


@contextmanager
def _db():
    """Hand out the thread-local connection; commit/rollback via ``with c``, never close on success.

    Closing every call used to leak FDs when combined with WAL; keeping one handle per thread
    avoids hundreds of connect+DDL cycles per state build. On path change ``_conn`` replaces it.
    """
    c = _conn()
    with c:
        yield c


def close_thread_connection() -> None:
    """Test/helper: drop the thread-local handle (e.g. after switching ``state_dir``)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None


def prune_probes(now: int | None = None, force: bool = False) -> int:
    """Delete probe rows older than retention. At most once per day unless ``force``. Returns deleted."""
    global _last_prune
    now = int(time.time() if now is None else now)
    with _prune_lock:
        if not force and _last_prune and now - _last_prune < PRUNE_EVERY:
            return 0
        cutoff = now - _retention_seconds()
        with _db() as c:
            cur = c.execute("DELETE FROM probes WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        _last_prune = now
        return deleted


def record(service: str, up: bool, latency: float | None = None, detail: str = "",
           ts: int | None = None) -> None:
    with _db() as c:
        c.execute("INSERT INTO probes(ts,service,up,latency,detail) VALUES(?,?,?,?,?)",
                  (int(ts if ts is not None else time.time()), service, 1 if up else 0, latency, detail))
    prune_probes()


def last(service: str) -> dict | None:
    with _db() as c:
        r = c.execute("SELECT * FROM probes WHERE service=? ORDER BY ts DESC LIMIT 1",
                      (service,)).fetchone()
    return dict(r) if r else None


def sla(service: str, window_seconds: int) -> tuple[float | None, int]:
    """(% of samples that were up in the window, sample count)."""
    since = int(time.time()) - window_seconds
    with _db() as c:
        rows = c.execute("SELECT up, COUNT(*) n FROM probes WHERE service=? AND ts>=? GROUP BY up",
                         (service, since)).fetchall()
    total = sum(r["n"] for r in rows)
    up = sum(r["n"] for r in rows if r["up"])
    return (100.0 * up / total if total else None), total


def avg_latency(service: str, window_seconds: int) -> float | None:
    """Average health-check latency (seconds) over the window — the card's 'avg latency' metric
    (distinct from the agent row's current latency)."""
    since = int(time.time()) - window_seconds
    with _db() as c:
        r = c.execute("SELECT AVG(latency) a FROM probes WHERE service=? AND ts>=? "
                      "AND latency IS NOT NULL", (service, since)).fetchone()
    return r["a"] if r and r["a"] is not None else None


def uptime_seconds(service: str, min_outage: int = 3) -> int | None:
    """Seconds in the current up-streak. A *real outage* is ``min_outage`` or more consecutive
    down samples; isolated transient blips (e.g. one slow health check while the process stays up)
    do NOT reset uptime — so this reads as "time since the last real restart/outage", matching how
    a status page reports uptime. Returns 0 if currently down, None if there's no data.

    Only the retention window is scanned (``WHERE ts >= ?``) so the streak is bounded by retention.
    """
    now = int(time.time())
    since = now - _retention_seconds()
    with _db() as c:
        rows = c.execute("SELECT ts, up FROM probes WHERE service=? AND ts>=? ORDER BY ts",
                         (service, since)).fetchall()
    if not rows:
        return None
    if not rows[-1]["up"]:
        return 0
    streak_start = int(rows[0]["ts"])
    run = 0
    for r in rows:
        if not r["up"]:
            run += 1
        else:
            if run >= min_outage:           # a real outage just ended → uptime restarts here
                streak_start = int(r["ts"])
            run = 0
    return now - streak_start


def history_seconds(service: str) -> int:
    """How long we've been recording this service (newest − oldest sample)."""
    with _db() as c:
        r = c.execute("SELECT MIN(ts) a, MAX(ts) b FROM probes WHERE service=?", (service,)).fetchone()
    return int(r["b"] - r["a"]) if r and r["a"] is not None else 0


def timeline(service: str, window_seconds: int, buckets: int) -> list[dict]:
    """Bucket the window into *buckets* slices. Each → {start: epoch, uptime_pct: float|None}
    (percent of up samples in that bucket; None when no data). The UI greens a bucket at ≥99 %."""
    now = int(time.time())
    start = now - window_seconds
    size = max(1, window_seconds // buckets)
    agg = [[0, 0] for _ in range(buckets)]   # [up_samples, total_samples]
    with _db() as c:
        rows = c.execute("SELECT ts, up FROM probes WHERE service=? AND ts>=? ORDER BY ts",
                         (service, start)).fetchall()
    for r in rows:
        idx = min(buckets - 1, (int(r["ts"]) - start) // size)
        agg[idx][1] += 1
        agg[idx][0] += int(r["up"])
    out = []
    for i, (up, total) in enumerate(agg):
        out.append({"start": start + i * size,
                    "uptime_pct": (round(100.0 * up / total, 2) if total else None)})
    return out
