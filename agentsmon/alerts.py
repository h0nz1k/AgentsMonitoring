"""Telegram alerts for quota windows and agent models (via ``hermes send`` — gateway bot, no LLM).

Rules, evaluated per series (row name + bar label) at most once a minute:

* **threshold** — once when used % crosses a configured threshold upward (highest one wins);
* **capacity back** — once when a series that had alerted drops below the lowest threshold
  (typically because its window reset);
* **forecast** — once per window when the burn-rate forecast says it runs out before reset, and
  only when it is actionable: enough history in the current window, enough of the window
  elapsed, exhaustion within the next ``forecast_max_horizon_seconds`` and well before the reset;
* **model change** — an agent row's detected model label changed and stayed changed for two
  consecutive ticks (first sight and transitions to/from an unknown label are silent).

Alerts are never sent one by one: every event goes into a *digest buffer* (``alert_buffer`` in
``quota.sqlite``, written in the same transaction as the dedupe state, so restarts neither lose
nor repeat anything). The buffer is flushed as ONE grouped message — at most once per
``digest_seconds`` for non-critical items, immediately when a critical item (exhausted, or a
threshold >= 95 %) arrives. During ``quiet_hours`` (Europe/Prague) only an exhausted window
flushes; everything else waits for the end of the quiet hours. A series seen for the first time is
*seeded silently*. Sending runs on a background worker with a timeout and never raises.
"""
from __future__ import annotations

import copy
import queue
import re
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
    "critical_threshold": 95,               # a crossed threshold >= this flushes the digest at once
    "notify_on_reset": True,
    "notify_on_forecast_exhaust": True,
    "notify_on_model_change": True,
    "forecast_min_span_seconds": 3600,      # history inside the current window before we page
    "forecast_min_elapsed_fraction": 0.2,   # share of the window that must have elapsed
    "forecast_max_horizon_seconds": 86400,  # exhaustion further away is not actionable
    "forecast_min_lead_seconds": 1800,      # exhaustion must come this long before the reset
    "forecast_short_window_min_reset_seconds": 3600,   # 5h windows: skip when reset is this close
    "digest_seconds": 600,
    "digest_max_lines": 15,
    "quiet_hours": ["22:00", "07:00"],
    "dashboard_url": "http://192.168.1.153:8765",
    "min_interval_seconds": 1800,
    "interval_seconds": 60,
    "timeout_seconds": 30,
    "timezone": "Europe/Prague",
    "target": "telegram",
    "command": ["hermes", "--profile", "lana", "send", "-q"],   # build_command appends -t <target>
}
HYSTERESIS = 5.0
SHORT_WINDOW = 6 * 3600
SECTIONS = [("exhausted", "🛑 Vyčerpáno"), ("threshold", "⚠️ Prahy"), ("back", "✅ Kapacita zpět"),
            ("forecast", "📈 Hrozí vyčerpání"), ("model", "🔀 Změna modelu")]
LEVEL_KINDS = ("exhausted", "threshold", "back")    # newest level event of a series supersedes older
UNKNOWN_MODELS = {"", "?", "-", "—", "–", "unknown", "neznámý", "neznámo", "n/a", "na", "none", "null"}

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
    line = f"{time.strftime('%F %T')} {msg}".replace("\n", " ⏎ ")
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


def _reset_tail(it, now, tz) -> str:
    r = it.get("reset_at")
    return f" · reset {_fmt_clock(r, now, tz)}" if r and r > now else ""


def line_threshold(it, now, tz) -> str:
    if it["used"] >= 100:
        return f"{_name(it)}{_reset_tail(it, now, tz)}"
    return f"{_name(it)}: {fmt_pct(it['used'])}{_reset_tail(it, now, tz)}"


def line_back(it, after_reset: bool) -> str:
    return f"{_name(it)} ({'reset proběhl' if after_reset else 'využito ' + fmt_pct(it['used'])})"


def line_forecast(it, f, now, tz) -> str:
    rate = f"{float(f.get('rate_pct_per_hour') or 0):.1f}".replace(".", ",")
    return (f"{_name(it)}: ~{_fmt_clock(f['exhaust_at'], now, tz)} (reset {_fmt_clock(it['reset_at'], now, tz)})"
            f" · {fmt_pct(it['used'])}, +{rate} %/h")


def line_model(name, old, new) -> str:
    return f"{name}: {old} → {new}"


def render(rows: list, now: float, s: dict) -> str:
    """One digest message: header, grouped sections, truncation note, dashboard link."""
    tz = _tz(s.get("timezone"))
    out = [f"📊 AgentsMonitoring · {_dt(now, tz):%H:%M}"]
    budget = max(2, int(s.get("digest_max_lines") or 15))
    shown = 0
    for kind, title in SECTIONS:
        part = [r for r in rows if r["kind"] == kind]
        if not part or budget < 2:
            continue
        out.append(title)
        budget -= 1
        for r in part:
            if budget < 1:
                break
            out.append("• " + r["text"])
            budget -= 1
            shown += 1
    if len(rows) > shown:
        out.append(f"… a dalších {len(rows) - shown}")
    if s.get("dashboard_url"):
        out.append(f"Dashboard: {s['dashboard_url']}")
    return "\n".join(out)


# ---------------------------------------------------------------- time windows
def _hm(v):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(v or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def is_quiet(s: dict, ts: float) -> bool:
    q = s.get("quiet_hours")
    if not isinstance(q, (list, tuple)) or len(q) != 2:
        return False
    a, b = _hm(q[0]), _hm(q[1])
    if a is None or b is None or a == b:
        return False
    t = _dt(ts, _tz(s.get("timezone")))
    m = t.hour * 60 + t.minute
    return a <= m < b if a < b else (m >= a or m < b)


def window_seconds(it) -> int | None:
    """Window length from the series (explicit ``window_seconds``) or its label (5h, 7d, week…)."""
    if type(it.get("window_seconds")) is int and it["window_seconds"] > 0:
        return it["window_seconds"]
    label = str(it.get("label") or "").lower()
    m = re.search(r"(?<![\d.])(\d+)\s*(h|d)\b", label)
    if m:
        return int(m.group(1)) * (3600 if m.group(2) == "h" else 86400)
    if "week" in label or "týd" in label or "tyd" in label:
        return 7 * 86400
    if "month" in label or "měsíc" in label:
        return 30 * 86400
    return None


def forecast_actionable(s: dict, it: dict, f: dict | None, now: float) -> bool:
    """Is a 'runs out before reset' forecast worth a notification? (the dashboard shows all)"""
    reset_at, used = it.get("reset_at"), it["used"]
    if not (f and f.get("will_exhaust_before_reset") and f.get("exhaust_at") and reset_at and used < 100):
        return False
    ex = f["exhaust_at"]
    if (f.get("span_seconds") or 0) < int(s.get("forecast_min_span_seconds") or 0):
        return False
    if ex - now > int(s.get("forecast_max_horizon_seconds") or 86400):
        return False
    if reset_at - ex < int(s.get("forecast_min_lead_seconds") or 0):
        return False
    dur = window_seconds(it)
    if dur:
        elapsed = now - (reset_at - dur)
        if elapsed < float(s.get("forecast_min_elapsed_fraction") or 0) * dur:
            return False
        if dur <= SHORT_WINDOW and reset_at - now < int(s.get("forecast_short_window_min_reset_seconds") or 0):
            return False
    return True


# ---------------------------------------------------------------- persistent state
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
        c.execute("""CREATE TABLE IF NOT EXISTS alert_buffer(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER NOT NULL, kind TEXT NOT NULL,
            key TEXT NOT NULL, critical INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL)""")
        c.execute("CREATE TABLE IF NOT EXISTS alert_meta(key TEXT PRIMARY KEY, value INTEGER)")
        c.execute("""CREATE TABLE IF NOT EXISTS model_state(
            agent TEXT PRIMARY KEY, label TEXT, pending TEXT, pending_count INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0)""")
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


def _buffer(c, ev: dict, now: float) -> None:
    kinds = LEVEL_KINDS if ev["kind"] in LEVEL_KINDS else (ev["kind"],)
    c.execute(f"DELETE FROM alert_buffer WHERE key=? AND kind IN ({','.join('?' * len(kinds))})",
              (ev["key"], *kinds))
    c.execute("INSERT INTO alert_buffer(created,kind,key,critical,text) VALUES(?,?,?,?,?)",
              (int(now), ev["kind"], ev["key"], int(bool(ev.get("critical"))), ev["text"]))
    _log(f"do souhrnu ({ev['kind']}): {ev['text']}")


def evaluate(s: dict, items: list, forecasts: list, now: float) -> list:
    """Apply the quota rules to *items*; buffer the resulting events (same transaction). Returns them."""
    thresholds = sorted(float(t) for t in s.get("thresholds") or [] if type(t) in (int, float))
    if not thresholds:
        return []
    lowest = thresholds[0]
    critical_at = float(s.get("critical_threshold") or 95)
    tz = _tz(s.get("timezone"))
    fmap = {(f["source"], f["label"]): f for f in forecasts or []}
    events = []
    with _db() as c:
        for it in items:
            used, reset_at = it["used"], it.get("reset_at")
            f = fmap.get((it["source"], it["label"]))
            will = forecast_actionable(s, it, f, now)
            crossed = max([t for t in thresholds if used >= t], default=0.0)
            row = c.execute("SELECT * FROM alert_state WHERE source=? AND label=?",
                            (it["source"], it["label"])).fetchone()
            if row is None:                         # first sight → seed silently, never flood
                _save(c, it, {"level": crossed, "reset_at": reset_at,
                              "forecast_for": reset_at if will else 0, "last_sent": 0}, now)
                continue
            st = dict(row)
            new = dict(st)
            ev = None
            key = f"{it['source']}\x1f{it['label']}"
            if crossed > st["level"]:
                ev = {"kind": "exhausted" if used >= 100 else "threshold", "key": key,
                      "critical": used >= 100 or crossed >= critical_at, "text": line_threshold(it, now, tz)}
                new["level"] = crossed
            elif st["level"] > 0 and used < lowest:
                after_reset = st["reset_at"] is not None and (
                    st["reset_at"] <= now or (reset_at is not None and not _same_window(reset_at, st["reset_at"])))
                if s.get("notify_on_reset", True):
                    ev = {"kind": "back", "key": key, "critical": False, "text": line_back(it, after_reset)}
                new["level"] = 0.0
            elif st["level"] > crossed and used < st["level"] - HYSTERESIS:
                new["level"] = crossed              # partial drop: re-arm the higher threshold quietly
            if (ev is None and will and s.get("notify_on_forecast_exhaust", True)
                    and not _same_window(st["forecast_for"] or None, reset_at)):
                ev = {"kind": "forecast", "key": key, "critical": False, "text": line_forecast(it, f, now, tz)}
                new["forecast_for"] = reset_at
            if ev is not None:
                if now - st["last_sent"] < int(s.get("min_interval_seconds") or 0):
                    continue                        # rate limited → retried on a later tick
                new["last_sent"] = int(now)
            new["reset_at"] = reset_at
            if new != st:
                _save(c, it, new, now)
            if ev is not None:
                _buffer(c, ev, now)
                events.append(ev)
    return events


def _model_label(a: dict) -> str:
    for k in ("model", "label", "tag"):
        v = a.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _known(label) -> bool:
    return bool(label) and str(label).strip().lower() not in UNKNOWN_MODELS


def track_models(s: dict, agents: list, now: float) -> list:
    """Debounced model-change events per agent row (buffered). First sight is seeded silently."""
    events = []
    with _db() as c:
        seen = set()
        for a in agents or []:
            agent = str(a.get("id") or a.get("name") or "").strip()
            if not agent or agent in seen:
                continue
            seen.add(agent)
            label = _model_label(a)
            row = c.execute("SELECT * FROM model_state WHERE agent=?", (agent,)).fetchone()
            if row is None:
                if _known(label):
                    c.execute("INSERT INTO model_state(agent,label,pending,pending_count,updated) "
                              "VALUES(?,?,NULL,0,?)", (agent, label, int(now)))
                continue
            if not _known(label):                   # unknown breaks the streak, never alerts
                if row["pending"] is not None:
                    c.execute("UPDATE model_state SET pending=NULL,pending_count=0,updated=? WHERE agent=?",
                              (int(now), agent))
                continue
            if not _known(row["label"]):            # from unknown → adopt silently
                c.execute("UPDATE model_state SET label=?,pending=NULL,pending_count=0,updated=? WHERE agent=?",
                          (label, int(now), agent))
                continue
            if label == row["label"]:
                if row["pending"] is not None:      # flapped back before confirmation
                    c.execute("UPDATE model_state SET pending=NULL,pending_count=0,updated=? WHERE agent=?",
                              (int(now), agent))
                continue
            count = row["pending_count"] + 1 if row["pending"] == label else 1
            if count < 2:
                c.execute("UPDATE model_state SET pending=?,pending_count=?,updated=? WHERE agent=?",
                          (label, count, int(now), agent))
                continue
            c.execute("UPDATE model_state SET label=?,pending=NULL,pending_count=0,updated=? WHERE agent=?",
                      (label, int(now), agent))
            if s.get("notify_on_model_change", True):
                ev = {"kind": "model", "key": agent, "critical": False,
                      "text": line_model(str(a.get("name") or agent), row["label"], label)}
                _buffer(c, ev, now)
                events.append(ev)
    return events


def flush(s: dict, now: float, send) -> str | None:
    """Send the buffered events as one digest when due; returns the message (or None)."""
    with _db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM alert_buffer ORDER BY id").fetchall()]
        if not rows:
            return None
        window = int(s.get("digest_seconds") or 0)
        last = c.execute("SELECT value FROM alert_meta WHERE key='last_flush'").fetchone()
        last = int(last[0]) if last and last[0] is not None else 0
        if is_quiet(s, now):
            due = any(r["kind"] == "exhausted" for r in rows)
        else:
            due = (any(r["critical"] for r in rows)
                   or any(is_quiet(s, r["created"]) for r in rows)           # quiet hours just ended
                   or (now - min(r["created"] for r in rows) >= window and now - last >= window))
        if not due:
            return None
        message = render(rows, now, s)
        c.execute(f"DELETE FROM alert_buffer WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
        c.execute("INSERT OR REPLACE INTO alert_meta(key,value) VALUES('last_flush',?)", (int(now),))
    send(message)                                   # after commit: a crash can't cause a resend loop
    return message


def process(s: dict, items: list, forecasts: list, agents: list, now: float, send) -> str | None:
    evaluate(s, items, forecasts, now)
    track_models(s, agents, now)
    return flush(s, now, send)


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
        process(s, items, forecasts, agents, now, lambda m: dispatch(m, s))
    except Exception as exc:
        _log(f"vyhodnocení přeskočeno: {type(exc).__name__}: {exc}")
    finally:
        _tick_lock.release()
