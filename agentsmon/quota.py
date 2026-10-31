"""Quota history + burn-rate forecast.

Every dashboard state build hands the current quota bars (provider tables first, agent gauges only
for windows no table carries) to :func:`observe`. Samples land in ``quota.sqlite`` →
``quota_samples(ts, source, label, used_pct, reset_at)`` — at most once per 5 minutes per series,
immediately when the value or the window changes; anything older than 30 days is pruned.

The forecast fits a line through the samples of the CURRENT window only (same ``reset_at``, and
the monotonic tail since the last drop) and projects when the series hits 100 % and how full it
will be at ``reset_at``. Nothing here may block or crash the dashboard: database errors are logged
and swallowed, and a busy lock means "use the previous result".
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SAMPLE_INTERVAL = 300          # seconds between unchanged samples of one series
RETENTION = 30 * 86400
PRUNE_EVERY = 3600
MIN_SAMPLES = 2
MIN_SPAN = 600                 # seconds of history inside the current window
RESET_TOLERANCE = 600          # reset_at jitter (s) that still counts as the same window
DROP_TOLERANCE = 0.5           # % — smaller dips are rounding noise, not a reset
UNAVAILABLE = {"UNAVAILABLE", "NEDOSTUPNÉ", "N/A"}

_active = False                # set by the dashboard server; CLI/test state builds stay read-free
_lock = threading.Lock()
_last: dict = {}               # (source, label) -> (ts, used, reset_at) of the newest stored sample
_last_prune = 0
_last_forecasts: list = []
_log_seen: dict = {}


def _path() -> str:
    return str(config.state_dir() / "quota.sqlite")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_path(), timeout=5)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        c.execute("""CREATE TABLE IF NOT EXISTS quota_samples(
            ts INTEGER NOT NULL, source TEXT NOT NULL, label TEXT NOT NULL,
            used_pct REAL NOT NULL, reset_at INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_quota_series_ts ON quota_samples(source, label, ts)")
    except BaseException:
        c.close()
        raise
    return c


@contextmanager
def _db():
    """Open, hand out, commit and always CLOSE (see db._db: leaked handles killed the dashboard)."""
    c = _conn()
    try:
        with c:
            yield c
    finally:
        c.close()


def _log(msg: str) -> None:
    """Print once per distinct message per 10 minutes — a broken disk must not flood the journal."""
    now = time.time()
    if now - _log_seen.get(msg, 0) < 600:
        return
    _log_seen[msg] = now
    print(f"{time.strftime('%F %T')} quota: {msg}", flush=True)


def start() -> None:
    global _active
    _active = True


def reset_cache() -> None:
    global _last_prune, _last_forecasts
    _last.clear()
    _last_prune = 0
    _last_forecasts = []


def _num(v):
    if type(v) in (int, float) and math.isfinite(v):
        return float(v)
    return None


def series(tables: list, agents: list) -> list:
    """Current value of every quota series, keyed by (row name, bar label).

    Provider tables are the source of truth; agent gauges usually repeat the same windows, so an
    agent bar is only used when no table bar has the same (used %, reset minute)."""
    out, idents, keys = [], set(), set()
    sources = [(r, True) for t in tables or [] for r in t.get("rows", [])] + [(a, False) for a in agents or []]
    for row, primary in sources:
        name = str(row.get("name") or "").strip()
        for cell in (row.get("cells") or {}).values():
            for b in (cell or {}).get("bars") or []:
                label, used = b.get("label"), _num(b.get("percent"))
                if not name or not label or used is None:
                    continue
                if str(b.get("state") or "").upper() in UNAVAILABLE:
                    continue
                reset_at = b.get("reset_at") if type(b.get("reset_at")) is int else None
                key = (round(used, 1), reset_at // 60 if reset_at else None)
                if (not primary and key in keys) or (name, label) in idents:
                    continue
                idents.add((name, label)); keys.add(key)
                out.append({"source": name, "label": str(label), "used": used, "reset_at": reset_at})
    return out


def _newest(c, source: str, label: str):
    r = c.execute("SELECT ts, used_pct, reset_at FROM quota_samples WHERE source=? AND label=? "
                  "ORDER BY ts DESC LIMIT 1", (source, label)).fetchone()
    return (int(r[0]), float(r[1]), r[2]) if r else None


def _changed(prev, used, reset_at) -> bool:
    if abs(prev[1] - used) >= 0.05:
        return True
    if (prev[2] is None) != (reset_at is None):
        return True
    return prev[2] is not None and abs(prev[2] - reset_at) > RESET_TOLERANCE


def record(items: list, now: float | None = None) -> int:
    """Persist samples that are due; returns how many rows were written. May raise sqlite errors."""
    global _last_prune
    now = int(time.time() if now is None else now)
    written = 0
    with _db() as c:
        for s in items:
            k = (s["source"], s["label"])
            if k not in _last:
                prev = _newest(c, *k)
                if prev:
                    _last[k] = prev
            prev = _last.get(k)
            if prev and now - prev[0] < SAMPLE_INTERVAL and not _changed(prev, s["used"], s["reset_at"]):
                continue
            c.execute("INSERT INTO quota_samples(ts,source,label,used_pct,reset_at) VALUES(?,?,?,?,?)",
                      (now, s["source"], s["label"], s["used"], s["reset_at"]))
            _last[k] = (now, s["used"], s["reset_at"])
            written += 1
        if now - _last_prune >= PRUNE_EVERY:
            c.execute("DELETE FROM quota_samples WHERE ts < ?", (now - RETENTION,))
            _last_prune = now
    return written


def samples(source: str, label: str, since: float) -> list:
    """[(ts, used_pct, reset_at)] oldest first."""
    with _db() as c:
        rows = c.execute("SELECT ts, used_pct, reset_at FROM quota_samples WHERE source=? AND label=? "
                         "AND ts>=? ORDER BY ts", (source, label, int(since))).fetchall()
    return [(int(r[0]), float(r[1]), r[2]) for r in rows]


def _current_window(points: list, reset_at) -> list:
    if reset_at is not None:
        points = [p for p in points if p[2] is not None and abs(p[2] - reset_at) <= RESET_TOLERANCE]
    tail = []
    for p in reversed(points):
        if tail and p[1] > tail[-1][1] + DROP_TOLERANCE:
            break                                   # usage dropped after p → p is the old window
        tail.append(p)
    return list(reversed(tail))


def forecast(points: list, used: float, reset_at, now: float | None = None) -> dict | None:
    """Linear burn-rate projection for one series, or None without enough current-window data."""
    now = int(time.time() if now is None else now)
    pts = sorted(points)
    if not pts or pts[-1][0] < now:
        pts.append((now, float(used), reset_at))
    pts = _current_window(pts, reset_at)
    if len(pts) < MIN_SAMPLES or pts[-1][0] - pts[0][0] < MIN_SPAN:
        return None
    t0 = pts[0][0]
    xs = [p[0] - t0 for p in pts]; ys = [p[1] for p in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.0   # % per second
    rate_h = slope * 3600
    future_reset = reset_at if reset_at is not None and reset_at > now else None
    out = {"used_pct": round(float(used), 2), "reset_at": reset_at,
           "rate_pct_per_hour": round(rate_h, 3), "exhaust_at": None, "projected_at_reset": None,
           "will_exhaust_before_reset": False, "exhausted": used >= 100,
           "samples": len(pts), "span_seconds": pts[-1][0] - pts[0][0]}
    if used >= 100:
        out["projected_at_reset"] = 100.0 if future_reset else None
        return out
    if rate_h > 0.01:
        out["exhaust_at"] = int(round(now + (100 - used) / slope))
    if future_reset:
        grow = slope * (future_reset - now) if rate_h > 0.01 else 0.0
        out["projected_at_reset"] = round(min(100.0, used + grow), 2)
        out["will_exhaust_before_reset"] = out["exhaust_at"] is not None and out["exhaust_at"] < future_reset
    return out


def observe(tables: list, agents: list, now: float | None = None) -> list:
    """Record due samples and return a forecast per series. Never raises."""
    global _last_forecasts
    if not _active:
        return []
    if not _lock.acquire(blocking=False):
        return _last_forecasts                      # another request is already doing this
    try:
        now = int(time.time() if now is None else now)
        items = series(tables, agents)
        record(items, now)
        out = []
        for s in items:
            f = forecast(samples(s["source"], s["label"], now - RETENTION), s["used"], s["reset_at"], now)
            if f:
                out.append({"source": s["source"], "label": s["label"], **f})
        _last_forecasts = out
        return out
    except Exception as exc:                        # the dashboard must keep serving
        _log(f"history/forecast skipped: {type(exc).__name__}: {exc}")
        return []
    finally:
        _lock.release()


COMPACT = ("exhaust_at", "projected_at_reset", "will_exhaust_before_reset", "rate_pct_per_hour")


def annotate(tables: list, agents: list, resets: list, forecasts: list) -> None:
    """Attach compact forecasts to table bars, matching agent gauges and reset entries (in place)."""
    by_ident, by_key = {}, {}
    for f in forecasts or []:
        compact = {k: f.get(k) for k in COMPACT}
        by_ident[(f["source"], f["label"])] = compact
        by_key.setdefault((round(float(f["used_pct"]), 1), f["reset_at"] // 60 if f.get("reset_at") else None),
                          compact)
    for rows, primary in ([r for t in tables or [] for r in t.get("rows", [])], True), (agents or [], False):
        for row in rows:
            name = str(row.get("name") or "").strip()
            for cell in (row.get("cells") or {}).values():
                for b in (cell or {}).get("bars") or []:
                    if not isinstance(b, dict):
                        continue
                    hit = by_ident.get((name, b.get("label")))
                    pct = _num(b.get("percent"))
                    if hit is None and not primary and pct is not None:
                        ts = b.get("reset_at") if type(b.get("reset_at")) is int else None
                        hit = by_key.get((round(pct, 1), ts // 60 if ts else None))
                    if hit:
                        b["forecast"] = hit
                    else:
                        b.pop("forecast", None)
    for e in resets or []:
        e["forecast"] = by_ident.get((e.get("source"), e.get("label")))
