"""Telegram alerts for quota windows (via ``hermes send`` — gateway bot credentials, no LLM).

Rules, evaluated per series (row name + bar label) at most once a minute:

* **threshold** — once when used % crosses a configured threshold upward (highest one wins);
* **capacity back** — once when a series that had alerted drops below the lowest threshold
  (typically because its window reset);
* **forecast** — once per window when the burn-rate forecast says it runs out before reset.

Dedupe state lives in SQLite (``alert_state`` in ``quota.sqlite``) so restarts don't re-spam. A
series seen for the first time is *seeded silently* from its current value — enabling alerts on a
window that is already exhausted produces no flood. Each series is rate limited
(``min_interval_seconds``); a held-back alert is delivered once the interval has passed. Sending
runs on a background worker with a timeout and never raises into the dashboard.
"""
from __future__ import annotations

import copy
import queue
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from . import config, quota

DEFAULTS = {
    "enabled": True,
    "dry_run": False,
    "thresholds": [80, 95],
    "notify_on_reset": True,
    "notify_on_forecast_exhaust": True,
    "forecast_min_span_seconds": 1800,   # a 10-minute trend is fine on screen, too jumpy to page on
    "min_interval_seconds": 1800,
    "interval_seconds": 60,
    "timeout_seconds": 30,
    "timezone": "Europe/Prague",
    "target": "telegram",
    "command": ["hermes", "--profile", "lana", "send", "-q", "-t", "telegram"],
}
HYSTERESIS = 5.0

_active = False
_tick_lock = threading.Lock()
_last_tick = 0.0
_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def settings(cfg: dict) -> dict:
    s = copy.deepcopy(DEFAULTS)
    user = (cfg or {}).get("alerts")
    if isinstance(user, dict):
        s.update({k: v for k, v in user.items() if k in DEFAULTS})
    return s


def start() -> None:
    """Called by the dashboard server only — tests and CLI state builds never alert."""
    global _active
    _active = True


def _reset_for_tests() -> None:
    global _active, _last_tick
    _active, _last_tick = False, 0.0


def _log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    print("alerts: " + line, flush=True)
    try:
        with (config.state_dir() / "alerts.log").open("a", encoding="utf-8") as out:
            out.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- formatting (Czech)
def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name) if isinstance(name, str) else name
    except Exception:
        return None


def fmt_pct(v: float) -> str:
    return f"{float(v):.1f}".rstrip("0").rstrip(".").replace(".", ",") + " %"


def fmt_in(sec: float) -> str:
    sec = max(0, int(round(sec)))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{max(1, m)} min"


def _dt(ts, tz):
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def fmt_reset(reset_at: int, now: float, tz) -> str:
    return f"{_dt(reset_at, tz):%d.%m %H:%M} (za {fmt_in(reset_at - now)})"


def _fmt_clock(ts, now, tz) -> str:
    t, n = _dt(ts, tz), _dt(now, tz)
    return f"{t:%H:%M}" if t.date() == n.date() else f"{t:%d.%m %H:%M}"


def _name(it) -> str:
    return f"{it['source']} {it['label']}"


def msg_threshold(it, now, tz) -> str:
    head = (f"🛑 {_name(it)}: vyčerpáno ({fmt_pct(it['used'])})" if it["used"] >= 100
            else f"⚠️ {_name(it)}: využito {fmt_pct(it['used'])}")
    if it.get("reset_at") and it["reset_at"] > now:
        head += f" · reset {fmt_reset(it['reset_at'], now, tz)}"
    return head


def msg_back(it, after_reset: bool) -> str:
    why = "reset proběhl" if after_reset else f"využito {fmt_pct(it['used'])}"
    return f"✅ {_name(it)}: kapacita zpět ({why})"


def msg_forecast(it, f, now, tz) -> str:
    rate = f"{float(f.get('rate_pct_per_hour') or 0):.1f}".replace(".", ",")
    return (f"📈 {_name(it)}: tempem vyčerpáno ~{_fmt_clock(f['exhaust_at'], now, tz)} "
            f"(před resetem {_dt(it['reset_at'], tz):%d.%m %H:%M}) · využito {fmt_pct(it['used'])}, +{rate} %/h")


# ---------------------------------------------------------------- dedupe state
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(quota._path(), timeout=5)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        c.execute("""CREATE TABLE IF NOT EXISTS alert_state(
            source TEXT NOT NULL, label TEXT NOT NULL, level REAL NOT NULL DEFAULT 0,
            reset_at INTEGER, forecast_for INTEGER NOT NULL DEFAULT 0,
            last_sent INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(source, label))""")
    except BaseException:
        c.close()
        raise
    return c


@contextmanager
def _db():
    c = _conn()
    try:
        with c:
            yield c
    finally:
        c.close()


def _save(c, it, st, now):
    c.execute("INSERT OR REPLACE INTO alert_state(source,label,level,reset_at,forecast_for,last_sent,updated) "
              "VALUES(?,?,?,?,?,?,?)", (it["source"], it["label"], st["level"], st["reset_at"],
                                        st["forecast_for"], st["last_sent"], int(now)))


def _same_window(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) <= quota.RESET_TOLERANCE


def evaluate(s: dict, items: list, forecasts: list, now: float, send) -> list:
    """Apply the rules to the current *items*; call ``send(message)`` for each alert. Returns them."""
    thresholds = sorted(float(t) for t in s.get("thresholds") or [] if type(t) in (int, float))
    if not thresholds:
        return []
    lowest = thresholds[0]
    tz = _tz(s.get("timezone"))
    fmap = {(f["source"], f["label"]): f for f in forecasts or []}
    sent = []
    with _db() as c:
        for it in items:
            used, reset_at = it["used"], it.get("reset_at")
            f = fmap.get((it["source"], it["label"]))
            will = bool(f and f.get("will_exhaust_before_reset") and f.get("exhaust_at") and reset_at
                        and used < 100
                        and (f.get("span_seconds") or 0) >= int(s.get("forecast_min_span_seconds") or 0))
            crossed = max([t for t in thresholds if used >= t], default=0.0)
            row = c.execute("SELECT * FROM alert_state WHERE source=? AND label=?",
                            (it["source"], it["label"])).fetchone()
            if row is None:                         # first sight → seed silently, never flood
                _save(c, it, {"level": crossed, "reset_at": reset_at,
                              "forecast_for": reset_at if will else 0, "last_sent": 0}, now)
                continue
            st = dict(row)
            new = dict(st)
            msg = None
            if crossed > st["level"]:
                msg = msg_threshold(it, now, tz)
                new["level"] = crossed
            elif st["level"] > 0 and used < lowest:
                after_reset = st["reset_at"] is not None and (
                    st["reset_at"] <= now or (reset_at is not None and not _same_window(reset_at, st["reset_at"])))
                if s.get("notify_on_reset", True):
                    msg = msg_back(it, after_reset)
                new["level"] = 0.0
            elif st["level"] > crossed and used < st["level"] - HYSTERESIS:
                new["level"] = crossed              # partial drop: re-arm the higher threshold quietly
            if (msg is None and will and s.get("notify_on_forecast_exhaust", True)
                    and not _same_window(st["forecast_for"] or None, reset_at)):
                msg = msg_forecast(it, f, now, tz)
                new["forecast_for"] = reset_at
            if msg is not None:
                if now - st["last_sent"] < int(s.get("min_interval_seconds") or 0):
                    continue                        # rate limited → retried on a later tick
                new["last_sent"] = int(now)
            new["reset_at"] = reset_at
            if new != st:
                _save(c, it, new, now)
            if msg is not None:
                sent.append(msg)
    for m in sent:                                  # after commit: a crash can't cause a resend loop
        send(m)
    return sent


# ---------------------------------------------------------------- sending
def build_command(s: dict, message: str) -> list:
    cmd = [str(x) for x in s.get("command") or DEFAULTS["command"]]
    if s.get("target") and "-t" not in cmd and "--to" not in cmd:
        cmd += ["-t", str(s["target"])]
    return cmd + [message]


def _run(message: str, s: dict) -> None:
    try:
        r = subprocess.run(build_command(s, message), stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=int(s.get("timeout_seconds") or 30))
        _log(f"odesláno rc={r.returncode}: {message}" if r.returncode == 0
             else f"odeslání selhalo rc={r.returncode}: {message}")
    except Exception as exc:
        _log(f"odeslání selhalo ({type(exc).__name__}): {message}")


def _loop() -> None:
    while True:
        message, s = _queue.get()
        try:
            _run(message, s)
        finally:
            _queue.task_done()


def dispatch(message: str, s: dict) -> None:
    """Queue *message* for the background sender (or just log it in dry-run). Never raises."""
    global _worker
    try:
        if s.get("dry_run"):
            _log(f"DRY-RUN {message}")
            return
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_loop, daemon=True, name="agentsmon-alerts")
                _worker.start()
        _queue.put((message, s))
    except Exception as exc:
        _log(f"odeslání selhalo ({type(exc).__name__}): {message}")


def _drain() -> None:
    _queue.join()


def tick(cfg: dict, tables: list, agents: list, forecasts: list, now: float | None = None) -> None:
    """Evaluate alerts for the current state (dashboard server only, throttled). Never raises."""
    global _last_tick
    if not _active or not _tick_lock.acquire(blocking=False):
        return
    try:
        s = settings(cfg)
        now = time.time() if now is None else now
        if not s.get("enabled") or now - _last_tick < int(s.get("interval_seconds") or 60):
            return
        items = quota.series(tables, agents)
        if not items:                               # cold plugin cache: wait for real data
            return
        _last_tick = now
        evaluate(s, items, forecasts, now, lambda m: dispatch(m, s))
    except Exception as exc:
        _log(f"vyhodnocení přeskočeno: {type(exc).__name__}: {exc}")
    finally:
        _tick_lock.release()
