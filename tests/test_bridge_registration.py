from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from agentsmon import bridges, config, detect, probe


class BridgeRegistration(unittest.TestCase):
    def test_two_configs_are_independent_and_registration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d, patch.object(config,'state_dir',return_value=Path(d)):
            cfg = {'daemons':[{'name':'Telegram Bridge','pattern':'agent2telegram run'}],
                   'services':[{'name':'Telegram Bridge Status'}]}
            first = bridges.entry('/tmp/one.json', '/venv/bin/python', '/source path', '/tmp/state-one')
            second = bridges.entry('/tmp/two.json', '/venv/bin/python')
            for item in (first, second, first):
                bridges.register(cfg, item)
            self.assertEqual(len(cfg['daemons']), 2)
            self.assertEqual(cfg['services'], [{'name':'Telegram Bridge','kind':'bridges'}])
            with patch.object(bridges,'processes',return_value=[(123, {'config':'/tmp/one.json'})]):
                status = detect.daemon_status(cfg['daemons'])
                self.assertEqual([r['up'] for r in status], [True, False])
                with patch.object(probe.db,'record') as record:
                    probe.probe_once(cfg)
                self.assertEqual([c.args[:2] for c in record.call_args_list], [('Telegram Bridge', False)])
                self.assertIn('1/2 bridges running', record.call_args_list[0].args[3])
                self.assertIn(cfg['daemons'][1]['name'], record.call_args_list[0].args[3])
            args = shlex.split(first['restart'])
            self.assertIn('PYTHONPATH=/source path', args)
            self.assertIn('AGENT2TELEGRAM_STATE=/tmp/state-one', args)
            self.assertIn('/tmp/one.json', args)

    def test_public_cli_preserves_existing_config_and_disabled_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root/'config.json'
            path.write_text('{"custom":"preserve","modules_dir":""}')
            args = Namespace(bridge_config='/tmp/one.json', bridge_python='/venv/bin/python',
                             bridge_pythonpath='', bridge_state='/tmp/state')
            with patch.object(config,'DEFAULT_PATH',path), patch.object(config,'state_dir',return_value=root), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(bridges.add_noninteractive(args), 0)
                cfg = json.loads(path.read_text())
                cfg['daemons'][0]['enabled'] = False
                config.save(cfg)
                self.assertEqual(bridges.add_noninteractive(args), 0)
            cfg = json.loads(path.read_text())
            self.assertEqual(len(cfg['daemons']), 1)
            self.assertFalse(cfg['daemons'][0]['enabled'])
            self.assertEqual(cfg['custom'], 'preserve')

    def test_default_explicit_and_env_config_are_distinct(self):
        with tempfile.TemporaryDirectory() as d, patch.object(Path,'home',return_value=Path(d)), \
             patch.object(bridges,'process_env',return_value={}):
            default = bridges.process_info(123, '/venv/bin/python -m agent2telegram run')
            custom = bridges.process_info(456, '/venv/bin/python -m agent2telegram run --config /tmp/second.json')
            shell = bridges.process_info(789, 'sh -c "python -m agent2telegram run"')
        self.assertTrue(default['config'].endswith('/.config/agent2telegram/config.json'))
        self.assertEqual(custom['config'], '/tmp/second.json')
        self.assertIsNone(shell)
        raw = b'AGENT2TELEGRAM_CONFIG=/tmp/third.json\0PYTHONPATH=/src\0SECRET=never-copy\0'
        with patch.object(Path,'read_bytes',return_value=raw):
            third = bridges.process_info(333, '/venv/bin/python -m agent2telegram run')
        self.assertEqual(third['config'], '/tmp/third.json')
        self.assertNotIn('never-copy', json.dumps(third))

    def test_unreadable_env_is_not_guessed_as_default_bridge(self):
        with patch.object(bridges,'process_env',return_value=None):
            self.assertIsNone(bridges.process_info(123,'/venv/bin/python -m agent2telegram run'))

    def test_mac_restart_fields_exclude_credentials(self):
        from subprocess import CompletedProcess
        raw = '/venv/bin/python -m agent2telegram run PYTHONPATH=/source path AGENT2TELEGRAM_CONFIG=/tmp/a.json SECRET=never-copy'
        with patch.object(bridges.sys,'platform','darwin'), patch.object(Path,'read_bytes',side_effect=OSError), \
             patch.object(detect,'_run',return_value=CompletedProcess([],0,raw,'')):
            env = bridges.process_env(123)
        self.assertEqual(env, {'PYTHONPATH':'/source path','AGENT2TELEGRAM_CONFIG':'/tmp/a.json'})


class SharedBridgeCard(unittest.TestCase):
    def test_one_card_is_green_only_when_every_bridge_runs(self):
        cfg = {'daemons':[{'name':'A','bridge_config':'/tmp/a.json'},
                          {'name':'B','bridge_config':'/tmp/b.json'},
                          {'name':'Off','bridge_config':'/tmp/c.json','enabled':False}]}
        both = [(1, {'config':'/tmp/a.json'}), (2, {'config':'/tmp/b.json'})]
        with patch.object(bridges, 'processes', return_value=both):
            self.assertEqual(bridges.health(cfg), (True, '2/2 bridges running'))
        with patch.object(bridges, 'processes', return_value=both[:1]):
            self.assertEqual(bridges.health(cfg), (False, '1/2 bridges running; down: B'))
        self.assertIsNone(bridges.health({'daemons':[]}))

    def test_bridge_is_named_after_its_session_and_legacy_title_is_renamed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'config.json'
            path.write_text(json.dumps({'tmux_session':'Helper - Codex', 'token':'never-read'}))
            item = bridges.entry(str(path), '/usr/bin/python3')
            self.assertEqual(item['name'], 'Telegram Bridge: Helper - Codex')
            self.assertNotIn('never-read', json.dumps(item))
            cfg = {'daemons':[{'name':'Telegram Bridge: config [5580834b604c]',
                               'bridge_config':item['bridge_config'], 'enabled':False}]}
            bridges.register(cfg, item)
            self.assertEqual(cfg['daemons'][0]['name'], 'Telegram Bridge: Helper - Codex')
            self.assertFalse(cfg['daemons'][0]['enabled'])
            custom = {'daemons':[{'name':'My bot', 'bridge_config':item['bridge_config']}]}
            bridges.register(custom, item)
            self.assertEqual(custom['daemons'][0]['name'], 'My bot')
