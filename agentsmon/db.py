"""Tiny SQLite uptime store — powers per-service SLA + uptime + timeline.

Tables:
  ``probes(ts, service, up, latency, detail)`` — raw samples
  ``probes_daily(service, day, up_n, total_n, avg_latency)`` — day aggregates (UTC midnight)
  ``maintenance(id, service, start_ts, end_ts, note)`` — windows excluded from SLA

From these we derive current status (including pending), uptime streak, SLA, continuous
timeline, incidents, and latency sparklines. Standard library only (sqlite3, WAL).
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
#: Keep daily aggregates at least this long (and ≥ retention).
DAILY_RETENTION_DAYS = 400

_local = threading.local()
_last_prune = 0.0
_prune_lock = threading.Lock()
_last_rollup = 0.0
_rollup_lock = threading.Lock()


def _path() -> str:
    return str(config.state_dir() / "uptime.sqlite")


def _retention_seconds() -> int:
    try:
        days = int((config.load().get("probe") or {}).get("retention_days", 30))
    except (TypeError, ValueError):
        days = 30
    return max(1, days) * 86400


def _day_start(ts: int) -> int:
    """UTC midnight for the calendar day containing ``ts``."""
    return int(ts) - (int(ts) % 86400)


def _conn() -> sqlite3.Connection:
    """One live connection per thread (DDL once). Re-opens when the state-dir path changes.

    Migrations are idempotent ``CREATE TABLE/INDEX IF NOT EXISTS`` — an older DB opens without
    data loss; new tables appear alongside ``probes``.
    """
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
        c.execute("""CREATE TABLE IF NOT EXISTS probes_daily(
            service TEXT NOT NULL, day INTEGER NOT NULL,
            up_n INTEGER NOT NULL, total_n INTEGER NOT NULL,
            avg_latency REAL,
            PRIMARY KEY(service, day))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_probes_daily_day ON probes_daily(day)")
        c.execute("""CREATE TABLE IF NOT EXISTS maintenance(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            note TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_maint_service ON maintenance(service, start_ts, end_ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS attention_mutes(
            mute_key TEXT PRIMARY KEY,
            until_ts INTEGER NOT NULL,
            hours REAL,
            user TEXT,
            created_ts INTEGER,
            text_cs TEXT)""")
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


def _maintenance_clauses(service: str, since: int, until: int | None = None) -> tuple[str, list]:
    """SQL fragment excluding probe timestamps that fall inside a maintenance window."""
    until = int(time.time() if until is None else until)
    with _db() as c:
        rows = c.execute(
            "SELECT start_ts, end_ts FROM maintenance WHERE service=? AND end_ts>? AND start_ts<?",
            (service, since, until)).fetchall()
    if not rows:
        return "", []
    parts, args = [], []
    for r in rows:
        parts.append("NOT (ts>=? AND ts<?)")
        args.extend([int(r["start_ts"]), int(r["end_ts"])])
    return " AND " + " AND ".join(parts), args


def add_maintenance(service: str, start_ts: int, end_ts: int, note: str = "") -> int:
    """Record a maintenance window. Returns row id. ``end_ts`` must be > ``start_ts``."""
    start_ts, end_ts = int(start_ts), int(end_ts)
    if end_ts <= start_ts:
        raise ValueError("maintenance end must be after start")
    with _db() as c:
        cur = c.execute(
            "INSERT INTO maintenance(service,start_ts,end_ts,note) VALUES(?,?,?,?)",
            (service, start_ts, end_ts, (note or "")[:200]))
        return int(cur.lastrowid)


def list_maintenance(service: str | None = None, now: int | None = None) -> list[dict]:
    """Active or upcoming maintenance windows (end_ts > now)."""
    now = int(time.time() if now is None else now)
    with _db() as c:
        if service:
            rows = c.execute(
                "SELECT * FROM maintenance WHERE service=? AND end_ts>? ORDER BY start_ts",
                (service, now)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM maintenance WHERE end_ts>? ORDER BY start_ts", (now,)).fetchall()
    return [dict(r) for r in rows]


def in_maintenance(service: str, ts: int | None = None) -> bool:
    ts = int(time.time() if ts is None else ts)
    with _db() as c:
        r = c.execute(
            "SELECT 1 FROM maintenance WHERE service=? AND start_ts<=? AND end_ts>? LIMIT 1",
            (service, ts, ts)).fetchone()
    return r is not None


def rollup_daily(now: int | None = None, force: bool = False) -> int:
    """Aggregate completed UTC days from ``probes`` into ``probes_daily``. Idempotent upsert.

    Today's incomplete day is left in raw probes. Returns number of day-rows touched.
    """
    global _last_rollup
    now = int(time.time() if now is None else now)
    with _rollup_lock:
        if not force and _last_rollup and now - _last_rollup < 3600:
            return 0
        today = _day_start(now)
        touched = 0
        with _db() as c:
            services = [r[0] for r in c.execute(
                "SELECT DISTINCT service FROM probes WHERE ts<?", (today,)).fetchall()]
            for service in services:
                rows = c.execute(
                    "SELECT (ts - ts % 86400) AS day, "
                    "SUM(up) AS up_n, COUNT(*) AS total_n, AVG(latency) AS avg_latency "
                    "FROM probes WHERE service=? AND ts<? GROUP BY day",
                    (service, today)).fetchall()
                for r in rows:
                    c.execute(
                        "INSERT INTO probes_daily(service,day,up_n,total_n,avg_latency) "
                        "VALUES(?,?,?,?,?) "
                        "ON CONFLICT(service,day) DO UPDATE SET "
                        "up_n=excluded.up_n, total_n=excluded.total_n, "
                        "avg_latency=excluded.avg_latency",
                        (service, int(r["day"]), int(r["up_n"] or 0), int(r["total_n"] or 0),
                         r["avg_latency"]))
                    touched += 1
        _last_rollup = now
        return touched


def prune_probes(now: int | None = None, force: bool = False) -> int:
    """Roll up then delete raw probe rows older than retention. At most once per day unless force."""
    global _last_prune
    now = int(time.time() if now is None else now)
    with _prune_lock:
        if not force and _last_prune and now - _last_prune < PRUNE_EVERY:
            return 0
        rollup_daily(now=now, force=True)
        cutoff = now - _retention_seconds()
        daily_cut = _day_start(now) - DAILY_RETENTION_DAYS * 86400
        with _db() as c:
            cur = c.execute("DELETE FROM probes WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            c.execute("DELETE FROM probes_daily WHERE day < ?", (daily_cut,))
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


def consecutive_downs(service: str, limit: int = 64) -> int:
    """How many newest samples in a row are down (0 if latest is up or no data)."""
    with _db() as c:
        rows = c.execute(
            "SELECT up FROM probes WHERE service=? ORDER BY ts DESC LIMIT ?",
            (service, max(1, int(limit)))).fetchall()
    n = 0
    for r in rows:
        if r["up"]:
            break
        n += 1
    return n


def had_up_before_downs(service: str, downs: int) -> bool:
    """True when the sample immediately before the current down-run was up."""
    if downs <= 0:
        return False
    with _db() as c:
        rows = c.execute(
            "SELECT up FROM probes WHERE service=? ORDER BY ts DESC LIMIT ?",
            (service, downs + 1)).fetchall()
    if len(rows) <= downs:
        return False
    return bool(rows[downs]["up"])


def probe_state(service: str, min_outage: int = 3) -> str:
    """``operational`` | ``pending`` | ``outage`` | ``nodata`` from raw probes (Uptime Kuma style).

    A confirmed outage needs ``min_outage`` consecutive failed checks after a prior success;
    fewer consecutive fails → ``pending`` (yellow). Never-up-yet stays ``outage`` immediately.
    """
    cur = last(service)
    if not cur:
        return "nodata"
    if cur["up"]:
        return "operational"
    min_outage = max(1, int(min_outage))
    downs = consecutive_downs(service, limit=min_outage + 1)
    if downs < min_outage and had_up_before_downs(service, downs):
        return "pending"
    return "outage"


def sla(service: str, window_seconds: int, now: int | None = None) -> tuple[float | None, int]:
    """(% of countable samples that were up in the window, sample count).

    Prefers ``probes_daily`` for completed UTC days; today's partial day still uses raw
    ``probes``. Samples inside maintenance windows are excluded.
    """
    now = int(time.time() if now is None else now)
    since = now - int(window_seconds)
    today = _day_start(now)
    excl, excl_args = _maintenance_clauses(service, since, now)
    up = total = 0
    used_daily = False
    with _db() as c:
        # Completed days from aggregate (day start >= since and day < today).
        day_from = _day_start(max(since, 0))
        if day_from < today:
            rows = c.execute(
                "SELECT day, up_n, total_n FROM probes_daily "
                "WHERE service=? AND day>=? AND day<?",
                (service, day_from, today)).fetchall()
            maint_full = set()
            if excl:
                for m in c.execute(
                    "SELECT start_ts, end_ts FROM maintenance WHERE service=?",
                    (service,)).fetchall():
                    # Day fully covered when maintenance spans [day, day+86400].
                    for day in range(day_from, today, 86400):
                        if int(m["start_ts"]) <= day and int(m["end_ts"]) >= day + 86400:
                            maint_full.add(day)
            for r in rows:
                if int(r["day"]) in maint_full:
                    continue
                used_daily = True
                up += int(r["up_n"] or 0)
                total += int(r["total_n"] or 0)
        if used_daily:
            # Today's partial day (and nothing older — those came from the aggregate).
            rows = c.execute(
                f"SELECT up, COUNT(*) n FROM probes WHERE service=? AND ts>=? AND ts<=?{excl} "
                "GROUP BY up",
                (service, today, now, *excl_args)).fetchall()
            for r in rows:
                total += int(r["n"])
                if r["up"]:
                    up += int(r["n"])
        else:
            # Fresh DB / pre-rollup: all countable samples from raw probes.
            rows = c.execute(
                f"SELECT up, COUNT(*) n FROM probes WHERE service=? AND ts>=?{excl} GROUP BY up",
                (service, since, *excl_args)).fetchall()
            total = sum(int(r["n"]) for r in rows)
            up = sum(int(r["n"]) for r in rows if r["up"])
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
    """Bucket the window into *buckets* slices. Each → {start: epoch, uptime_pct: float|None}.

    Prefers ``probes_daily`` when bucket size ≥ 1 day; otherwise uses raw probes. The UI paints
    a continuous colour from the percentage (not a binary green/red cut).
    """
    now = int(time.time())
    start = now - window_seconds
    size = max(1, window_seconds // max(1, buckets))
    agg = [[0, 0] for _ in range(buckets)]   # [up_samples, total_samples]
    use_daily = size >= 86400
    with _db() as c:
        daily_used = False
        if use_daily:
            day0 = _day_start(start)
            rows = c.execute(
                "SELECT day, up_n, total_n FROM probes_daily "
                "WHERE service=? AND day>=? AND day<? ORDER BY day",
                (service, day0, _day_start(now))).fetchall()
            for r in rows:
                daily_used = True
                idx = min(buckets - 1, max(0, (int(r["day"]) - start) // size))
                agg[idx][1] += int(r["total_n"] or 0)
                agg[idx][0] += int(r["up_n"] or 0)
            raw_since = _day_start(now) if daily_used else start
            rows = c.execute(
                "SELECT ts, up FROM probes WHERE service=? AND ts>=? ORDER BY ts",
                (service, raw_since)).fetchall()
        else:
            rows = c.execute("SELECT ts, up FROM probes WHERE service=? AND ts>=? ORDER BY ts",
                             (service, start)).fetchall()
        for r in rows:
            idx = min(buckets - 1, max(0, (int(r["ts"]) - start) // size))
            agg[idx][1] += 1
            agg[idx][0] += int(r["up"])
    out = []
    for i, (up, total) in enumerate(agg):
        out.append({"start": start + i * size,
                    "uptime_pct": (round(100.0 * up / total, 2) if total else None)})
    return out


def incidents(service: str, window_seconds: int, min_outage: int = 3,
              now: int | None = None) -> list[dict]:
    """Outage incidents derived from probes: [{start, end, duration, detail}, …].

    An incident starts after ``min_outage`` consecutive downs (same rule as uptime). ``end`` is
    None while the outage is still open. Newest first, capped at 50.
    """
    now = int(time.time() if now is None else now)
    since = now - int(window_seconds)
    min_outage = max(1, int(min_outage))
    with _db() as c:
        rows = c.execute(
            "SELECT ts, up, detail FROM probes WHERE service=? AND ts>=? ORDER BY ts",
            (service, since)).fetchall()
    out: list[dict] = []
    run = 0
    run_start = None
    last_detail = ""
    open_inc = None
    for r in rows:
        ts, up = int(r["ts"]), bool(r["up"])
        if not up:
            if run == 0:
                run_start = ts
            run += 1
            if r["detail"]:
                last_detail = str(r["detail"])[:200]
            if run == min_outage:
                open_inc = {"start": run_start, "end": None, "detail": last_detail}
        else:
            if open_inc is not None:
                open_inc["end"] = ts
                open_inc["duration"] = int(ts - open_inc["start"])
                open_inc["detail"] = last_detail
                out.append(open_inc)
                open_inc = None
            run = 0
            run_start = None
    if open_inc is not None:
        open_inc["duration"] = int(now - open_inc["start"])
        open_inc["detail"] = last_detail
        out.append(open_inc)
    out.reverse()
    return out[:50]


def latency_sparkline(service: str, window_seconds: int = 86400, buckets: int = 24,
                      now: int | None = None) -> list[float | None]:
    """Average latency (ms) per bucket over the window; None when a bucket has no samples."""
    now = int(time.time() if now is None else now)
    start = now - int(window_seconds)
    buckets = max(1, int(buckets))
    size = max(1, int(window_seconds) // buckets)
    sums = [0.0] * buckets
    counts = [0] * buckets
    with _db() as c:
        rows = c.execute(
            "SELECT ts, latency FROM probes WHERE service=? AND ts>=? AND latency IS NOT NULL",
            (service, start)).fetchall()
    for r in rows:
        idx = min(buckets - 1, max(0, (int(r["ts"]) - start) // size))
        sums[idx] += float(r["latency"])
        counts[idx] += 1
    return [round(sums[i] / counts[i] * 1000, 1) if counts[i] else None for i in range(buckets)]


def daily_rows(service: str, since_day: int | None = None) -> list[dict]:
    """Test/helper: list ``probes_daily`` rows for a service."""
    with _db() as c:
        if since_day is None:
            rows = c.execute(
                "SELECT * FROM probes_daily WHERE service=? ORDER BY day", (service,)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM probes_daily WHERE service=? AND day>=? ORDER BY day",
                (service, int(since_day))).fetchall()
    return [dict(r) for r in rows]


def mute_attention(mute_key: str, hours: float, user: str = "", text_cs: str = "",
                   now: int | None = None) -> dict:
    """Silence a 'K řešení' item for ``hours``. Returns the stored mute row."""
    now = int(time.time() if now is None else now)
    hours = max(0.25, min(168.0, float(hours)))
    until = now + int(hours * 3600)
    key = str(mute_key)[:64]
    with _db() as c:
        c.execute(
            "INSERT INTO attention_mutes(mute_key,until_ts,hours,user,created_ts,text_cs) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(mute_key) DO UPDATE SET "
            "until_ts=excluded.until_ts, hours=excluded.hours, user=excluded.user, "
            "created_ts=excluded.created_ts, text_cs=excluded.text_cs",
            (key, until, hours, (user or "")[:120], now, (text_cs or "")[:240]))
    return {"mute_key": key, "until_ts": until, "hours": hours}


def active_mutes(now: int | None = None) -> dict[str, dict]:
    """``{mute_key: row}`` for mutes that have not expired; drops expired rows."""
    now = int(time.time() if now is None else now)
    with _db() as c:
        c.execute("DELETE FROM attention_mutes WHERE until_ts<=?", (now,))
        rows = c.execute("SELECT * FROM attention_mutes WHERE until_ts>?", (now,)).fetchall()
    return {str(r["mute_key"]): dict(r) for r in rows}


def is_muted(mute_key: str, now: int | None = None) -> bool:
    now = int(time.time() if now is None else now)
    with _db() as c:
        r = c.execute(
            "SELECT 1 FROM attention_mutes WHERE mute_key=? AND until_ts>? LIMIT 1",
            (str(mute_key)[:64], now)).fetchone()
    return r is not None
