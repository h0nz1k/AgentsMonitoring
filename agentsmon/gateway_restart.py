"""Safe manual Hermes gateway restart from a Telegram-lane outage.

Multiplex gateway has no per-lane restart — the only recovery is restarting the whole
``hermes-gateway`` unit. This module adds step-up copy, a Telegram getMe precheck (token
never returned or logged), a 10-minute cooldown, and an actions.jsonl audit trail.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, detect

ACTION_ID = "restart_gateway"
GATEWAY_DAEMON = "Hermes"
COOLDOWN_SEC = 600
GETME_TIMEOUT = 8
_TOKEN_RE = re.compile(
    r"(?im)^(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*[\"']?(\d{6,}:[A-Za-z0-9_-]{30,})"
)
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOCK = threading.Lock()

PRECHECK_OK_CS = "Telegram bota obsluhuje (getMe OK) → restart pravděpodobně pomůže"
PRECHECK_DOWN_CS = "Telegram na bota neodpovídá → výpadek je na straně Telegramu, restart nepomůže"
PRECHECK_NO_TOKEN_CS = "Token bota nenalezen → předkontrolu nelze ověřit"
STEP_UP_SUMMARY_CS = (
    "Restart Hermes gateway: přeruší rozpracované konverzace všech profilů "
    "({profiles}); lane se znovu připojí za ~1 min."
)


def normalize_profile(profile) -> str:
    if isinstance(profile, str) and _PROFILE_RE.fullmatch(profile):
        return profile
    return "lana"


def hermes_profiles(cfg: dict | None = None) -> list[str]:
    """Display names of Hermes profiles known from config (default + pinned)."""
    cfg = cfg if cfg is not None else config.load()
    found = ["default"]
    for d in (cfg.get("pinned_daemons") or []) + (cfg.get("daemons") or []):
        p = d.get("hermes_profile")
        if isinstance(p, str) and _PROFILE_RE.fullmatch(p):
            label = "default" if p in ("main", "default") else p
            if label not in found:
                found.append(label)
    if "lana" not in found:
        found.append("lana")
    return found


def step_up_summary(cfg: dict | None = None) -> str:
    profiles = ", ".join(hermes_profiles(cfg))
    return STEP_UP_SUMMARY_CS.format(profiles=profiles)


def is_lane_service(service: dict) -> bool:
    """True when a service card is the Hermes Telegram-lane health probe."""
    if not isinstance(service, dict):
        return False
    name = str(service.get("name") or "")
    detail = str(service.get("detail") or "")
    if "lane_state=" in detail:
        return True
    return "Telegram Lane" in name or name.endswith("Telegram")


def profile_for_name(name: str, cfg: dict | None = None) -> str:
    """Best-effort profile for a UI row / service / attention source name."""
    cfg = cfg if cfg is not None else config.load()
    text = str(name or "")
    low = text.lower()
    for d in cfg.get("pinned_daemons") or []:
        if d.get("name") == text and d.get("hermes_profile"):
            return normalize_profile(d["hermes_profile"])
    for d in cfg.get("pinned_daemons") or []:
        prof = d.get("hermes_profile")
        if not isinstance(prof, str):
            continue
        dn = str(d.get("name") or "").lower()
        token = dn.split("(")[0].strip()
        if prof.lower() in low or (token and token in low):
            return normalize_profile(prof)
    if "lana" in low:
        return "lana"
    return "lana"


def read_bot_token(profile: str) -> str | None:
    """Load TELEGRAM_BOT_TOKEN from the profile ``.env``. Never log the return value."""
    profile = normalize_profile(profile)
    path = detect._hermes_profile_home(profile) / ".env"
    try:
        text = path.read_text("utf-8")
    except OSError:
        return None
    m = _TOKEN_RE.search(text)
    return m.group(1) if m else None


def telegram_getme(token: str, timeout: float = GETME_TIMEOUT, urlopen=None) -> dict:
    """Call Telegram getMe. Result never includes the token."""
    urlopen = urlopen or urllib.request.urlopen
    if not token:
        return {"ok": False, "status": "no_token"}
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("ok"):
            user = (data.get("result") or {}).get("username") or ""
            return {"ok": True, "status": "ok", "username": str(user)[:64]}
        return {"ok": False, "status": "api_error"}
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}".lower()
        if isinstance(exc, TimeoutError) or "timed out" in msg or "timeout" in msg:
            return {"ok": False, "status": "timeout"}
        if isinstance(exc, urllib.error.URLError) and "timed out" in str(exc.reason).lower():
            return {"ok": False, "status": "timeout"}
        return {"ok": False, "status": "error"}


def precheck(profile: str = "lana", urlopen=None) -> dict:
    """Server-side Telegram reachability check for the lane bot (no secrets in result)."""
    profile = normalize_profile(profile)
    token = read_bot_token(profile)
    if not token:
        return {
            "ok": False,
            "helps": None,
            "status": "no_token",
            "message_cs": PRECHECK_NO_TOKEN_CS,
            "profile": profile,
        }
    result = telegram_getme(token, urlopen=urlopen)
    if result.get("ok"):
        return {
            "ok": True,
            "helps": True,
            "status": "ok",
            "message_cs": PRECHECK_OK_CS,
            "profile": profile,
        }
    status = result.get("status") or "error"
    return {
        "ok": False,
        "helps": False,
        "status": status,
        "message_cs": PRECHECK_DOWN_CS,
        "profile": profile,
    }


def cooldown_path() -> Path:
    return config.state_dir() / "gateway_restart.json"


def cooldown_remaining(now: float | None = None) -> int:
    """Seconds until another gateway restart is allowed (0 = allowed)."""
    now = time.time() if now is None else now
    try:
        data = json.loads(cooldown_path().read_text("utf-8"))
        last = float(data.get("last_ts") or 0)
    except (OSError, ValueError, TypeError):
        return 0
    left = int(COOLDOWN_SEC - (now - last))
    return left if left > 0 else 0


def mark_restart(now: float | None = None) -> None:
    now = time.time() if now is None else now
    path = cooldown_path()
    try:
        path.write_text(json.dumps({"last_ts": now}), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass


def actions_log_path() -> Path:
    return config.state_dir() / "actions.jsonl"


def log_action(entry: dict) -> None:
    """Append one JSON line to ``actions.jsonl`` (0600). Never raises; never stores secrets."""
    try:
        safe = {k: v for k, v in entry.items() if k not in ("token", "bot_token", "password", "secret")}
        blob = json.dumps(safe, ensure_ascii=False, default=str)
        # Defence in depth: refuse to write if a bot token shape slipped in.
        if _TOKEN_RE.search(blob) or re.search(r"\d{6,}:[A-Za-z0-9_-]{20,}", blob):
            blob = json.dumps({
                "ts": safe.get("ts"),
                "user": safe.get("user"),
                "action": safe.get("action"),
                "result": "redacted",
                "reason": "secret-shape-blocked",
            }, ensure_ascii=False)
        line = (blob + "\n").encode("utf-8")
        with _LOCK:
            path = actions_log_path()
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except Exception:
        pass


def find_gateway_daemon(cfg: dict | None = None) -> dict | None:
    cfg = cfg if cfg is not None else config.load()
    for d in cfg.get("daemons") or []:
        if d.get("name") == GATEWAY_DAEMON:
            return d
    return None


def lane_service_name(cfg: dict | None = None) -> str | None:
    cfg = cfg if cfg is not None else config.load()
    for s in cfg.get("services") or []:
        if is_lane_service(s):
            return s.get("name")
    return "LANA Hermes Telegram Lane"


def perform(cfg: dict | None = None, runner=None) -> tuple[bool, str]:
    """Restart the Hermes gateway daemon via managed.action (injectable for tests)."""
    cfg = cfg if cfg is not None else config.load()
    daemon = find_gateway_daemon(cfg)
    if not daemon:
        return False, f"daemon '{GATEWAY_DAEMON}' není v konfiguraci"
    if runner is None:
        from . import managed
        runner = managed.action
    return runner(daemon, "restart")


def confirm_summary(pre: dict, cfg: dict | None = None) -> str:
    return step_up_summary(cfg) + "\n\n" + str(pre.get("message_cs") or "")
