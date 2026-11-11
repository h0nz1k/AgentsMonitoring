"""Per-plugin publication, independent refresh, errors and stale action safety."""
import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from agentsmon import plugins


class LazyPlugins(unittest.TestCase):
    def setUp(self):
        self.manager = plugins.Manager({'modules_dir': '/nonexistent-fixture'})
        self.manager.plugins = [SimpleNamespace(id=x, version='1') for x in ('slow', 'fast')]
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.data = {'tables': [{'id': 't', 'rows': [{'id': 'r', 'alive': True,
                     'row_actions': [{'id': 'reset'}]}]}]}

    def wait_for(self, predicate):
        deadline = time.monotonic()+2
        while not predicate() and time.monotonic()<deadline:
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_fast_is_published_and_refreshed_while_slow_is_still_running(self):
        calls = []
        def one(p, ctx):
            calls.append(p.id)
            if p.id == 'slow': self.release.wait(3)
            return copy.deepcopy(self.data), ''
        with patch.object(self.manager, '_one', side_effect=one):
            t0 = time.monotonic()
            first = self.manager.snapshot([])
            self.assertLess(time.monotonic()-t0, .1)
            self.assertTrue(first['loading'])
            self.wait_for(lambda: 'fast' in self.manager._snapshots)
            result = self.manager.snapshot([])
            self.assertEqual(len(result['tables']), 1)
            self.assertTrue(result['loading'])
            self.assertTrue(next(p for p in result['plugins'] if p['id']=='slow')['loading'])
            sig, _, data = self.manager._snapshots['fast']
            self.manager._snapshots['fast'] = (sig, time.monotonic()-31, data)
            result = self.manager.snapshot([])
            self.assertTrue(result['tables'][0]['stale'])
            self.assertNotIn('row_actions', result['tables'][0]['rows'][0])
            self.assertTrue(result['tables'][0]['rows'][0]['actions_locked'])
            self.assertIn('row_actions', data['tables'][0]['rows'][0])
            self.wait_for(lambda: calls.count('fast') == 2)
            self.assertEqual(calls.count('slow'), 1)
            self.release.set()
            self.wait_for(lambda: not self.manager._pending)

    def test_failure_keeps_only_its_own_stale_data_until_expiration(self):
        self.manager.plugins = self.manager.plugins[1:]
        data = copy.deepcopy(self.data)
        self.manager._snapshots['fast'] = ('[]', time.monotonic()-31, data)
        with patch.object(self.manager, '_one', return_value=(None, 'Plugin timed out')):
            self.manager.snapshot([])
            self.wait_for(lambda: not self.manager._pending)
            result = self.manager.snapshot([])
        self.assertFalse(result['loading'])
        self.assertIn('timed out', result['plugins'][0]['error'])
        self.assertTrue(result['tables'][0]['stale'])
        self.assertNotIn('row_actions', result['tables'][0]['rows'][0])
        self.manager._snapshots['fast'] = ('[]', time.monotonic()-601, data)
        self.assertFalse(self.manager.snapshot([])['tables'])

    def test_blocking_manager_callback_publishes_in_completion_order(self):
        published = threading.Event()
        def one(p, ctx):
            if p.id == 'slow': self.release.wait(3)
            return {}, ''
        result = []
        with patch.object(self.manager, '_one', side_effect=one):
            thread = threading.Thread(target=lambda: self.manager.collect([], lambda p, d, e:
                                      (result.append(p.id), published.set())))
            thread.start()
            self.assertTrue(published.wait(1))
            self.assertEqual(result, ['fast'])
            self.assertIn('fast', self.manager._snapshots)
            self.assertNotIn('slow', self.manager._snapshots)
            self.assertIn('slow', self.manager._pending)
            self.release.set()
            thread.join(2)
            self.assertEqual(result, ['fast', 'slow'])
