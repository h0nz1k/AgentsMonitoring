"""Boot persistence — keep the dashboard + keepalive running across reboots.

We use **cron** (a launcher run `@reboot` and every minute) rather than systemd ``--user`` or a
macOS LaunchAgent. On a headless server reached over SSH there's often no user D-Bus / systemd
instance (``systemctl --user`` fails with "Failed to connect to bus: No medium found") and a
macOS LaunchAgent needs a GUI login session. A cron launcher that nohups the dashboard (guarded
by pgrep) and runs one keepalive pass works everywhere, no login session required.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import agentsmon
from . import config

MARKER = "agentsmon-launch.sh"   # identifies our crontab lines


def _python() -> str:
    return sys.executable or "python3"


def _pythonpath() -> str:
    # Parent of the package dir, so the launcher imports agentsmon whether pip-installed or run
    # straight from a clone.
    return str(Path(agentsmon.__file__).resolve().parent.parent)


SYSTEMD_UNIT = "agentsmon-dashboard.service"


def _launcher_path() -> Path:
    return config.state_dir() / MARKER


def _launcher_content() -> str:
    state = config.state_dir()
    log = state / "agentsmon.log"
    tmux = shlex.quote(config.load().get("tmux_bin") or "tmux")
    return f"""#!/bin/sh
# Agents Monitoring launcher — started by cron (@reboot + every minute). Idempotent: starts the
# dashboard only if it isn't running, then runs one keepalive pass (a no-op if disabled / no agents).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"   # lets cron query systemctl --user
export PYTHONPATH="{_pythonpath()}"
export AGENTSMON_CONFIG="{config.DEFAULT_PATH}"
export AGENTSMON_STATE="{config.state_dir()}"
PY="{_python()}"
mkdir -p "{state}"
# Start tmux from this shell before Python: macOS TCC follows the server's launcher.
{tmux} start-server \\; set -g exit-empty off >/dev/null 2>&1 || true
# Guard on the dashboard's own pidfile (a plain `pgrep -f "agentsmon dashboard"` also matched any
# shell whose command line mentioned the dashboard, and then nothing was started — 2026-09-04).
PIDFILE="{state}/dashboard.pid"
# Single supervisor: when the {SYSTEMD_UNIT} systemd user unit is enabled it owns the dashboard; this
# launcher then only runs keepalive (only a unit serving this same state dir counts). (2026-11-08: after a reboot this cron line won the port race with a
# stale checkout and the unit crash-looped on "Address already in use".)
if command -v systemctl >/dev/null 2>&1 && systemctl --user is-enabled --quiet {SYSTEMD_UNIT} 2>/dev/null \\
   && systemctl --user show -p Environment {SYSTEMD_UNIT} 2>/dev/null | grep -q "AGENTSMON_STATE={state}\\( \\|$\\)"; then
  :
elif ! ( [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null ) \\
   && ! pgrep -u "$(id -u)" -f -- "-m agentsmon dashboard$" >/dev/null 2>&1; then
  nohup "$PY" -m agentsmon dashboard >> "{log}" 2>&1 &
fi
"$PY" -m agentsmon keepalive >> "{log}" 2>&1
"""


def refresh_bytecode(package: Path) -> None:
    """A rapid git rollback can preserve a .pyc's integer mtime and source size.

    Force checked-hash caches before launching new code, including the first
    upgrade invoked by an old, already-loaded updater. No source files change.
    """
    import compileall
    import py_compile
    if not compileall.compile_dir(str(package), quiet=1, force=True,
                                  invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH):
        raise RuntimeError('Could not refresh Python bytecode; service reload aborted')


def _write_launcher() -> Path:
    package = Path(agentsmon.__file__).resolve().parent
    # Git checkouts need this; a pip/system package may be read-only and its
    # bytecode belongs to the package installer, not the user's service setup.
    if (package.parent / '.git').exists():
        refresh_bytecode(package)
    path = _launcher_path()
    # A cron shell may still be reading the old launcher: never truncate it in place.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".launcher-", delete=False) as out:
        staged = Path(out.name)
        try:
            out.write(_launcher_content())
            out.flush()
            os.fchmod(out.fileno(), 0o755)
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    return path


def refresh_launcher() -> bool:
    """Repair an installed cron launcher even after an update ran with OLD loaded code.

    Do not install a service for manual users. The next cron tick uses the repaired
    UID-scoped guard; no recursive launcher call or healthy dashboard restart is needed.
    """
    path = _launcher_path()
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    if current == _launcher_content():
        return False
    _write_launcher()
    return True


def _dashboard_pids() -> list[int]:
    """The running dashboard's PID(s): the pidfile it writes on start, else a pattern that only
    matches this user's dashboard command line (``-m agentsmon dashboard``). A bare
    ``pkill -f "agentsmon dashboard"`` also killed any shell script whose text mentioned the
    dashboard — the very script driving the install (2026-09-04)."""
    from . import dashboard as _dash
    pids: list[int] = []
    try:
        pid = int(_dash.pidfile_path().read_text("utf-8").strip())
        os.kill(pid, 0)                              # still alive?
        pids.append(pid)
    except (OSError, ValueError):
        pass
    if not pids:
        out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "--", r"-m agentsmon dashboard$"],
                             capture_output=True, text=True).stdout
        pids = [int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()]
    return pids


def stop_dashboard(wait: float = 8.0) -> None:
    """Stop the running dashboard and WAIT until it is gone (port freed) before relaunching.
    Otherwise the launcher's guard still sees the dying process, or the new one hits "address
    already in use" — either way the STALE pre-config dashboard keeps serving."""
    import signal
    pids = _dashboard_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + wait
    while pids and time.time() < deadline:
        pids = [p for p in pids if _alive(p)]
        if pids and time.time() > deadline - wait / 2:
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)  # stubborn → escalate
                except OSError:
                    pass
        time.sleep(0.3)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def install() -> int:
    if not shutil.which("cron") and not shutil.which("crontab"):
        print("⚠️  crontab not found. Run these yourself under any process manager:")
        print(f"    {_python()} -m agentsmon dashboard &")
        print(f"    {_python()} -m agentsmon keepalive --loop &")
        return 1
    launcher = _write_launcher()
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except OSError:
        existing = ""
    lines = [ln for ln in existing.splitlines() if MARKER not in ln]
    lines.append(f"@reboot {launcher}")
    lines.append(f"* * * * * {launcher}")
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True,
                          capture_output=True)
    if proc.returncode != 0:
        print(f"✗ couldn't update crontab: {proc.stderr.strip()}")
        return 1
    # Stop any dashboard already running, so the launcher restarts it with the CURRENT config
    # (host/port/auth). Without this, a re-run can't change a live dashboard — its pgrep guard
    # would just leave the stale one bound to the old address. (Safe: our own process is
    # "agentsmon setup/service", not "agentsmon dashboard".)
    stop_dashboard()
    # Kick it once now so the dashboard comes up immediately on the configured host.
    subprocess.run(["sh", str(launcher)], capture_output=True)
    print("  ✓ installed cron launcher (@reboot + every minute) — survives logout/reboot.")
    print(f"    launcher: {launcher}")
    print("    No systemd/launchd needed; works headless over SSH.")
    return 0


def uninstall_cron() -> None:
    """Remove our crontab lines (used by the uninstaller)."""
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except OSError:
        return
    kept = [ln for ln in existing.splitlines() if MARKER not in ln]
    subprocess.run(["crontab", "-"], input="\n".join(kept) + ("\n" if kept else ""), text=True,
                   capture_output=True)


def main() -> int:
    return install()
