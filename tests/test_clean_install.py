import json
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request

from agentsmon import __version__


class CleanInstall(unittest.TestCase):
    def archive_fixture(self, home, version):
        archive = home / 'fixture.tgz'
        with tarfile.open(archive, 'w:gz') as out:
            for name, text in {
                'pyproject.toml': '[project]\nname="agents-monitoring"\n',
                'agentsmon/__init__.py': f'__version__ = "{version}"\n',
            }.items():
                data = text.encode()
                item = tarfile.TarInfo('AgentsMonitoring-main/' + name)
                item.size = len(data)
                out.addfile(item, io.BytesIO(data))
        return archive

    def installer_fixture(self, home):
        bindir = home / 'bin'
        bindir.mkdir()
        scripts = {
            'git': '#!/bin/sh\nexit 1\n',
            'sleep': '#!/bin/sh\nexit 0\n',
            'curl': '#!/bin/sh\n[ "${FAIL_DOWNLOAD:-0}" = 0 ] || exit 1\n'
                    'while [ "$1" != -o ]; do shift; done\ncp "$FIXTURE_ARCHIVE" "$2"\n',
        }
        for name, text in scripts.items():
            path = bindir / name
            path.write_text(text)
            path.chmod(0o755)
        return {'HOME': str(home),
                'PATH': f'{bindir}:{Path(sys.executable).parent}:{os.defpath}',
                'FIXTURE_ARCHIVE': str(home / 'fixture.tgz')}

    def run_installer(self, home, env, cwd=None):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run(['bash', str(root / 'install.sh'), '--no-setup', '--no-pip'],
                              cwd=cwd or home, env=env, capture_output=True, text=True, timeout=20)

    def test_archive_install_can_be_updated_by_rerunning_installer(self):
        for from_source in (False, True):
            with self.subTest(from_source=from_source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = self.installer_fixture(home)
                self.archive_fixture(home, 'old')
                first = self.run_installer(home, env)
                self.assertEqual(first.returncode, 0, first.stderr)
                source = home / '.agentsmon-src'
                (source / 'obsolete.txt').write_text('old release file')
                config = home / '.config/agentsmon/config.json'
                config.parent.mkdir(parents=True)
                config.write_text('{"preserve": true}')
                self.archive_fixture(home, 'new')
                second = self.run_installer(home, env, source if from_source else home)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertIn('new', (source / 'agentsmon/__init__.py').read_text())
                self.assertFalse((source / 'obsolete.txt').exists())
                self.assertEqual(config.read_text(), '{"preserve": true}')
                self.assertTrue((source / '.agentsmon-archive-install').is_file())
                self.assertEqual(list(home.glob('.agentsmon-src.stage.*')), [])

    def test_failed_archive_update_preserves_previous_release(self):
        for failure in ('download', 'invalid_archive', 'publish'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = self.installer_fixture(home)
                source = home / '.agentsmon-src'
                source.mkdir()
                (source / '.agentsmon-archive-install').write_text('agentsmon-archive-v1\n')
                (source / 'sentinel').write_text('previous release')
                archive = self.archive_fixture(home, 'new')
                if failure == 'download':
                    env['FAIL_DOWNLOAD'] = '1'
                elif failure == 'invalid_archive':
                    archive.write_text('invalid archive')
                else:
                    mv = home / 'bin/mv'
                    mv.write_text('#!/bin/sh\ncase "$1" in */AgentsMonitoring-*) exit 1;; esac\n'
                                  'exec /bin/mv "$@"\n')
                    mv.chmod(0o755)
                result = self.run_installer(home, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((source / 'sentinel').read_text(), 'previous release')
                self.assertEqual(list(home.glob('.agentsmon-src.stage.*')), [])

    def test_legacy_archive_without_marker_updates_from_home_and_source(self):
        for from_source in (False, True):
            with self.subTest(from_source=from_source), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = self.installer_fixture(home)
                source = home / '.agentsmon-src'
                (source / 'agentsmon').mkdir(parents=True)
                (source / 'pyproject.toml').write_text('[project]\nname="agents-monitoring"\n')
                (source / 'agentsmon/__init__.py').write_text('__version__ = "old"\n')
                (source / 'obsolete.txt').write_text('old release')
                self.archive_fixture(home, 'new')
                result = self.run_installer(home, env, source if from_source else home)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('new', (source / 'agentsmon/__init__.py').read_text())
                self.assertFalse((source / 'obsolete.txt').exists())
                self.assertEqual((source / '.agentsmon-archive-install').read_text(),
                                 'agentsmon-archive-v1\n')

    def test_legacy_detection_refuses_incomplete_foreign_and_symlink_sources(self):
        cases = ('missing_project', 'foreign_project', 'missing_init', 'symlink_source',
                 'symlink_project', 'symlink_package', 'symlink_init', 'invalid_marker', 'git_file')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = self.installer_fixture(home)
                self.archive_fixture(home, 'new')
                source = home / '.agentsmon-src'
                (source / 'agentsmon').mkdir(parents=True)
                project = source / 'pyproject.toml'
                project.write_text('[project]\nname="agents-monitoring"\n')
                init = source / 'agentsmon/__init__.py'
                init.write_text('__version__ = "old"\n')
                (source / 'sentinel').write_text('keep')
                if case == 'missing_project':
                    project.unlink()
                elif case == 'foreign_project':
                    project.write_text('[project]\nname="something-else"\n')
                elif case == 'missing_init':
                    init.unlink()
                elif case == 'invalid_marker':
                    (source / '.agentsmon-archive-install').write_text('unknown-marker')
                elif case == 'git_file':
                    (source / '.git').write_text('gitdir: elsewhere')
                else:
                    target = {'symlink_source': source, 'symlink_project': project,
                              'symlink_package': source / 'agentsmon', 'symlink_init': init}[case]
                    outside = home / 'outside'
                    target.rename(outside)
                    target.symlink_to(outside)
                result = self.run_installer(home, env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((source / 'sentinel').read_text(), 'keep')
                self.assertIn('preserved', result.stderr)

    def test_git_pull_retries_noninteractively_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = self.installer_fixture(home)
            source = home / '.agentsmon-src'
            (source / '.git').mkdir(parents=True)
            (source / 'sentinel').write_text('local work')
            git = home / 'bin/git'
            git.write_text('#!/bin/sh\n[ "$GIT_TERMINAL_PROMPT" = 0 ] || exit 91\n'
                           'printf "attempt\\n" >> "$HOME/attempts"\n'
                           '[ "$(wc -l < "$HOME/attempts")" -ge 2 ]\n')
            result = self.run_installer(home, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((home / 'attempts').read_text().splitlines(), ['attempt', 'attempt'])
            self.assertEqual((source / 'sentinel').read_text(), 'local work')

    def test_failed_update_never_falls_back_over_existing_checkout(self):
        self.check_existing_install_is_preserved(git_checkout=True)

    def test_existing_non_git_source_is_never_replaced(self):
        self.check_existing_install_is_preserved(git_checkout=False)

    def check_existing_install_is_preserved(self, git_checkout):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / '.agentsmon-src'
            source.mkdir()
            if git_checkout:
                (source / '.git').mkdir()
            sentinel = source / 'user-work.txt'
            sentinel.write_text('uncommitted work')
            bindir = home / 'bin'
            bindir.mkdir()
            for name, text in [('git', '#!/bin/sh\nprintf "attempt\\n" >> "$HOME/attempts"\nexit 1\n'),
                               ('sleep', '#!/bin/sh\nexit 0\n'),
                               ('curl', '#!/bin/sh\ntouch "$HOME/network-attempt"\nexit 1\n')]:
                path = bindir / name
                path.write_text(text)
                path.chmod(0o755)
            env = {'HOME': tmp, 'PATH': f'{bindir}:{Path(sys.executable).parent}:{os.defpath}'}
            run = subprocess.run(['bash', str(root / 'install.sh'), '--no-setup', '--no-pip'],
                                 cwd=home, env=env, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(sentinel.read_text(), 'uncommitted work')
            self.assertFalse((home / 'network-attempt').exists(), 'Must not replace an existing install')
            self.assertIn('preserved', run.stderr)
            if git_checkout:
                self.assertEqual((home / 'attempts').read_text().splitlines(), ['attempt'] * 3)
                self.assertIn('re-run', run.stderr)

    def test_install_without_controlling_terminal_finishes_successfully(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            env={'HOME':tmp,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.defpath,
                 'AGENTSMON_STATE':str(Path(tmp)/'state'),'AGENTSMON_CONFIG':str(Path(tmp)/'config.json')}
            # setsid: even a developer running this test from a terminal cannot supply /dev/tty.
            probe=subprocess.run(['sh','-c','{ : </dev/tty; } 2>/dev/null'],
                                 stdin=subprocess.DEVNULL,start_new_session=True,capture_output=True)
            self.assertNotEqual(probe.returncode,0,'Fixture unexpectedly has a controlling terminal')
            run=subprocess.run(['bash',str(root/'install.sh'),'--no-pip'],cwd=root,env=env,
                               stdin=subprocess.DEVNULL,start_new_session=True,capture_output=True,
                               text=True,timeout=20)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('Finish setup with: agentsmon setup',run.stdout)
            self.assertNotIn('/dev/tty:',run.stderr)
            self.assertFalse((Path(tmp)/'config.json').exists(),'Setup must not run without a terminal')
            self.assertTrue((Path(tmp)/'.local/bin/agentsmon').is_file())

    def test_clean_home_installer_and_real_dashboard_without_plugins(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            env={'HOME':tmp,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.defpath,'LANG':'en_US.UTF-8',
                 'AGENTSMON_STATE':str(home/'state'),'AGENTSMON_CONFIG':str(home/'config.json'),
                 'PYTHONDONTWRITEBYTECODE':'1'}
            # No inherited auth/config/TMUX, no wizard, pip, service or boot registration.
            run=subprocess.run(['bash',str(root/'install.sh'),'--no-setup','--no-pip'],
                               cwd=root,env=env,capture_output=True,text=True,timeout=20)
            self.assertEqual(run.returncode,0,run.stderr)
            cli=home/'.local/bin/agentsmon'
            self.assertTrue(cli.is_file())
            version=subprocess.check_output([str(cli),'--version'],env=env,text=True,timeout=10)
            self.assertIn(__version__,version)
            (home/'config.json').write_text(json.dumps({'tmux_bin':'/nonexistent-tmux',
                'services':[],'agents':[],'daemons':[],'pinned_daemons':[],
                'modules_dir':str(home/'modules'),'keepalive':{'enabled':False}}))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            proc=subprocess.Popen([str(cli),'dashboard','--host','127.0.0.1','--port',str(port)],
                                  env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                data=None; deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/state',timeout=2) as r: data=json.load(r)
                        if not data.get('loading'):
                            break
                        time.sleep(.1)
                    except OSError: time.sleep(.1)
                self.assertIsNotNone(data,'Clean HOME dashboard failed to start')
                self.assertEqual(data['agent_tables'],[]); self.assertEqual(data['plugin_errors'],[])
                self.assertEqual(data['core']['state'],'clean')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=2) as r:
                    self.assertIn('Persistent',r.read().decode())
            finally:
                proc.terminate()
                try: proc.wait(5)
                except subprocess.TimeoutExpired: proc.kill();proc.wait()
