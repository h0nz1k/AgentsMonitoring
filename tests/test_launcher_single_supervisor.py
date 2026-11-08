"""Cron launcher must not start a second dashboard when the systemd unit owns it."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import config, service


class LauncherSingleSupervisorTests(unittest.TestCase):
    def run_launcher(self, unit_enabled, same_state=True):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bindir = home / '.local/bin'
            bindir.mkdir(parents=True)
            trace = home / 'trace'
            for name, body in {
                'tmux': 'exit 0',
                'pgrep': 'exit 1',  # no dashboard running
                'systemctl': 'printf "systemctl:%s\\n" "$*" >> "$TRACE"\n'
                             'case "$*" in *is-enabled*agentsmon-dashboard.service*) exit "$UNIT_RC";; '
                             '*show*Environment*) echo "Environment=HOME=/h AGENTSMON_STATE=$UNIT_STATE LANG=C";; esac\nexit 0',
            }.items():
                f = bindir / name
                f.write_text('#!/bin/sh\n' + body + '\n')
                f.chmod(0o755)
            python = home / 'python-stub'
            python.write_text('#!/bin/sh\nprintf "python:%s\\n" "$*" >> "$TRACE"\n')
            python.chmod(0o755)
            cfg = home / 'config.json'
            cfg.write_text(json.dumps({'keepalive': {'enabled': False}}))
            env = {**os.environ, 'HOME': str(home), 'TRACE': str(trace), 'AGENTSMON_STATE': str(home / 'state'),
                   'UNIT_RC': '0' if unit_enabled else '1',
                   'UNIT_STATE': str(home / 'state') if same_state else '/other/state'}
            env.pop('XDG_RUNTIME_DIR', None)  # cron has none; launcher must supply it
            with patch.dict(os.environ, env), patch.object(config, 'DEFAULT_PATH', cfg), \
                    patch.object(service, '_python', return_value=str(python)):
                launcher = service._write_launcher()
                result = subprocess.run(['/bin/sh', str(launcher)], env=env, capture_output=True, text=True,
                                        timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            for _ in range(50):  # nohup'd start is backgrounded
                if trace.exists() and 'keepalive' in trace.read_text() and (
                        (unit_enabled and same_state) or 'dashboard' in trace.read_text()):
                    break
                time.sleep(0.05)
            return trace.read_text().splitlines()

    def test_enabled_unit_owns_dashboard(self):
        lines = self.run_launcher(unit_enabled=True)
        self.assertIn('systemctl:--user is-enabled --quiet agentsmon-dashboard.service', lines)
        self.assertNotIn('python:-m agentsmon dashboard', lines)
        self.assertIn('python:-m agentsmon keepalive', lines)

    def test_without_unit_launcher_starts_dashboard(self):
        lines = self.run_launcher(unit_enabled=False)
        self.assertIn('python:-m agentsmon dashboard', lines)
        self.assertIn('python:-m agentsmon keepalive', lines)

    def test_unit_for_other_state_dir_does_not_count(self):
        lines = self.run_launcher(unit_enabled=True, same_state=False)
        self.assertIn('python:-m agentsmon dashboard', lines)

    def test_launcher_exports_runtime_dir_for_cron(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, 'state_dir', return_value=Path(tmp)), patch.object(config, 'load', return_value={}):
            source = service._launcher_content()
        self.assertIn('export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"', source)


if __name__ == '__main__':
    unittest.main()
