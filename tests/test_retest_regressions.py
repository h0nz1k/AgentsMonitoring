"""Offline reproductions from the clean-install/multi-user retest.

All process, installer and uninstall fixtures live under a temporary HOME.
"""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentsmon import config, dashboard, keepalive, plugins, service, uninstaller

ROOT = Path(__file__).resolve().parents[1]


class RetestRegressions(unittest.TestCase):
    def test_old_launcher_repairs_itself_even_when_agent_keepalive_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bindir = home / '.local/bin'
            bindir.mkdir(parents=True)
            tmux = bindir / 'tmux'
            tmux.write_text('#!/bin/sh\nexit 0\n')
            tmux.chmod(0o755)
            state = home / 'state'
            cfg = home / 'config.json'
            cfg.write_text(json.dumps({'keepalive': {'enabled': False}}))
            calls = home / 'calls'
            # The unscoped old guard sees another user's dashboard; our UID has none.
            pgrep = bindir / 'pgrep'
            pgrep.write_text('#!/bin/sh\n[ "$1" = "-u" ] && exit 1\nexit 0\n')
            pgrep.chmod(0o755)
            runner = bindir / 'fixture-python'
            runner.write_text(f'#!{sys.executable}\n'
                              'import os, sys\nfrom pathlib import Path\n'
                              f'with Path({str(calls)!r}).open("a") as out: out.write(sys.argv[-1]+"\\n")\n'
                              'if sys.argv[-1] == "keepalive":\n'
                              ' from agentsmon import keepalive, service\n'
                              ' service._python = lambda: str(Path(__file__))\n'
                              ' raise SystemExit(keepalive.run())\n')
            runner.chmod(0o755)
            env = {'HOME': tmp, 'PATH': os.defpath, 'AGENTSMON_STATE': str(state),
                   'AGENTSMON_CONFIG': str(cfg), 'PYTHONDONTWRITEBYTECODE': '1'}
            with patch.dict(os.environ, env), patch.object(config, 'DEFAULT_PATH', cfg), \
                    patch.object(service, '_python', return_value=str(runner)):
                launcher = service._write_launcher()
                # Simulate the launcher kept by the already-loaded 303aea9 updater.
                launcher.write_text(launcher.read_text().replace(' -u "$(id -u)"', ''))
                for _ in range(2):
                    run = subprocess.run(['sh', str(launcher)], env=env, capture_output=True,
                                         text=True, timeout=10)
                    self.assertEqual(run.returncode, 0, run.stderr)
                deadline = time.monotonic() + 3
                while 'dashboard' not in calls.read_text() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn('dashboard', calls.read_text(), 'Old-code update never recovers our dashboard')
                self.assertIn('pgrep -u "$(id -u)"', launcher.read_text())
                self.assertEqual(calls.read_text().splitlines().count('dashboard'), 1)
                before = launcher.stat().st_mtime_ns
                keepalive.run()
                self.assertEqual(launcher.stat().st_mtime_ns, before, 'Unchanged template must not be rewritten')

    def test_keepalive_does_not_install_launcher_for_manual_users(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AGENTSMON_STATE': tmp}), \
                patch.object(config, 'load', return_value={'keepalive': {'enabled': False}}):
            self.assertEqual(keepalive.run(), 0)
            self.assertFalse(service._launcher_path().exists())

    def test_launcher_replacement_preserves_open_reader_and_old_file_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AGENTSMON_STATE': tmp}):
            launcher = service._launcher_path()
            launcher.write_text('old launcher\n')
            with launcher.open() as reader:
                self.assertTrue(service.refresh_launcher())
                self.assertEqual(reader.read(), 'old launcher\n', 'Running shell must keep its old inode')
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
            launcher.write_text('old launcher\n')
            with patch.object(service.os, 'replace', side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError):
                    service.refresh_launcher()
            self.assertEqual(launcher.read_text(), 'old launcher\n')
            self.assertEqual(list(Path(tmp).glob('.launcher-*')), [])

    def test_loop_refreshes_launcher_on_every_pass(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AGENTSMON_STATE': tmp}), \
                patch.object(config, 'load', return_value={'keepalive': {'enabled': True}}), \
                patch.object(keepalive, 'tick', return_value=0), \
                patch.object(service, 'refresh_launcher', return_value=False) as refresh, \
                patch.object(keepalive.time, 'sleep', side_effect=[None, InterruptedError]):
            with self.assertRaises(InterruptedError):
                keepalive.run(loop=True)
            self.assertEqual(refresh.call_count, 2)

    def test_installer_keeps_parsed_body_when_git_pull_overwrites_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            src = home / '.agentsmon-src'
            (src / '.git').mkdir(parents=True)
            installer = src / 'install.sh'
            installer.write_bytes((ROOT / 'install.sh').read_bytes())
            bindir = home / 'fake-bin'
            bindir.mkdir()
            fake_git = bindir / 'git'
            fake_git.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                                f'Path({str(home / "pulled")!r}).touch()\n'
                                f'Path({str(installer)!r}).write_text("# new installer\\n" * 1000 + '
                                '"echo UNPARSED_NEW_TAIL\\nexit 73\\n")\n')
            fake_git.chmod(0o755)
            env = {'HOME': tmp, 'PATH': f'{bindir}:{Path(sys.executable).parent}:{os.defpath}'}
            run = subprocess.run(['bash', str(installer), '--no-setup', '--no-pip'], cwd=home,
                                 env=env, stdin=subprocess.DEVNULL, start_new_session=True,
                                 capture_output=True, text=True, timeout=15)
            self.assertTrue((home / 'pulled').exists(), 'Fixture must overwrite the running script')
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn('UNPARSED_NEW_TAIL', run.stdout)
            self.assertIn('Finish setup with:', run.stdout)
            self.assertNotIn('Starting setup', run.stdout)
            self.assertTrue((bindir / 'agentsmon').is_file())

    def test_loading_is_neutral_and_expired_rows_and_actions_are_removed(self):
        manager = SimpleNamespace(plugins=['fixture'])
        stale = {'tables': [{'up': True, 'rows': [{'row_actions': [{'id': 'reload'}]}]}],
                 'patches': [{'row_id': 'old'}], 'columns': [{'id': 'old'}], 'errors': []}
        expired = ('old', time.monotonic() - plugins.MAX_STALE_SECONDS - 1, stale)
        for name, cache in (('initial', None), ('expired', expired)):
            with self.subTest(case=name), \
                    patch.object(plugins, 'manager', return_value=manager), \
                    patch.object(plugins.threading.Thread, 'start'), \
                    patch.multiple(plugins, _cache=cache, _refreshing=False):
                data = plugins.collect({}, [])
                self.assertEqual(data['tables'], [])
                self.assertEqual(data['patches'], [])
                self.assertEqual(data['columns'], [])
                self.assertEqual(data['errors'], [], 'Loading is not a failed check')
                self.assertTrue(data['loading'])
        # Within MAX_STALE_SECONDS (2026-11-09 operator request: no blank page that looks healthy):
        # the last snapshot is shown, flagged stale, and never actionable.
        with patch.object(plugins, 'manager', return_value=manager), \
                patch.object(plugins.threading.Thread, 'start'), \
                patch.multiple(plugins, _cache=('old', time.monotonic() - 31, stale), _refreshing=False):
            data = plugins.collect({}, [])
            self.assertTrue(data['loading'])
            self.assertEqual(data['errors'], [])
            self.assertTrue(all(t.get('stale') for t in data['tables']))
            self.assertFalse(any('row_actions' in r for t in data['tables'] for r in t['rows']))
            self.assertTrue(all(r.get('actions_locked') for t in data['tables'] for r in t['rows']))

    def test_http_state_exposes_loading_separately_from_real_errors(self):
        empty = {k: [] for k in ('tables', 'columns', 'patches', 'hidden_sessions',
                                 'hidden_services', 'errors', 'plugins')}
        with patch.object(config, 'load', return_value={}), \
                patch.object(dashboard, '_agents_state', return_value=[]), \
                patch.object(dashboard, '_service_state', return_value=[]), \
                patch('agentsmon.integrity.status', return_value={'state': 'fixture'}):
            for loading in (True, False):
                errors = [] if loading else [{'name': 'Plugin failed', 'detail': 'Plugin timed out'}]
                with self.subTest(loading=loading), \
                        patch.object(plugins, 'collect', return_value={**empty, 'loading': loading, 'errors': errors}):
                    result = json.loads(dashboard._state())
                    self.assertEqual(result['plugin_loading'], loading)
                    self.assertEqual(result['plugin_errors'], errors)

    def test_refused_dashboard_does_not_announce_listening_url(self):
        from agentsmon import wizard
        with patch.object(config, 'load', return_value={}), \
                patch.object(wizard, 'migrate_config', return_value=False):
            with redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit):
                    dashboard.serve('0.0.0.0', 8765)
                self.assertNotIn('http://', output.getvalue())
            for error in (OSError('Address already in use'),):
                with self.subTest(error=type(error).__name__), \
                        patch.object(dashboard, 'make_server', side_effect=error), \
                        redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(type(error)):
                        dashboard.serve('0.0.0.0', 8765)
                    self.assertNotIn('http://', output.getvalue())

    def uninstall_in_fixture(self, home):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'HOME': str(home)}))
            stack.enter_context(patch.object(config, 'DEFAULT_PATH', home / 'config/config.json'))
            stack.enter_context(patch.object(service, 'uninstall_cron'))
            stack.enter_context(patch.object(service, 'stop_dashboard'))
            stack.enter_context(patch.object(uninstaller.shutil, 'which', return_value=None))
            stack.enter_context(patch.object(uninstaller.shutil, 'rmtree'))
            stack.enter_context(patch.object(uninstaller.subprocess, 'run'))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(uninstaller.run(yes=True), 0)
            return output.getvalue()

    def test_uninstall_removes_only_own_wrapper_and_leaves_shell_config(self):
        for source in (ROOT, None):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                cli = home / '.local/bin/agentsmon'
                cli.parent.mkdir(parents=True)
                src = source or home / '.agentsmon-src'
                cli.write_text(f'#!/bin/sh\nexec env PYTHONPATH="{src}" "{sys.executable}" -m agentsmon "$@"\n')
                rc = home / '.bashrc'
                rc.write_text(f'export PATH="{cli.parent}:$PATH"\n# custom content\n')
                before = rc.read_bytes()
                output = self.uninstall_in_fixture(home)
                self.assertFalse(cli.exists(), 'Installed CLI wrapper was left behind')
                self.assertEqual(rc.read_bytes(), before)
                self.assertIn('PATH', output)
                self.assertIn('manually', output)

    def test_uninstall_preserves_custom_wrappers_and_symlinks(self):
        for kind in ('custom', 'extra-command', 'other-source', 'symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                cli = home / '.local/bin/agentsmon'
                cli.parent.mkdir(parents=True)
                wrapper = f'#!/bin/sh\nexec env PYTHONPATH="{ROOT}" "{sys.executable}" -m agentsmon "$@"\n'
                if kind == 'symlink':
                    target = home / 'keep-me'
                    target.write_text(wrapper)
                    cli.symlink_to(target)
                else:
                    cli.write_text({'custom': '#!/bin/sh\necho custom\n',
                                    'extra-command': wrapper + 'echo custom\n',
                                    'other-source': wrapper.replace(str(ROOT), '/another/project')}[kind])
                before = cli.read_bytes()
                self.uninstall_in_fixture(home)
                self.assertTrue(cli.exists())
                self.assertEqual(cli.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
