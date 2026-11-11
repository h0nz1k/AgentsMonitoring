"""Immediate state replies, disk lifetime, background warming and action invalidation."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agentsmon import dashboard as d


class StateCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, AGENTSMON_STATE=self.tmp.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        d.invalidate_state_cache()
        d._state_building = False
        d._state_disk_checked = False
        self.addCleanup(d.invalidate_state_cache)

    def build_gate(self):
        release = threading.Event()
        started = threading.Event()
        def slow():
            started.set()
            release.wait(3)
            return json.dumps({'agents': [], 'plugin_loading': False, 'marker': 'new'}).encode()
        self.addCleanup(release.set)
        return slow, started, release

    def finish(self, release):
        release.set()
        deadline = time.monotonic()+3
        while d._state_building and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(d._state_building)

    def test_cold_disk_snapshot_is_immediate_stale_and_readonly(self):
        path = Path(self.tmp.name)/'state-snapshot.json'
        path.write_text(json.dumps({'agents': [], 'marker': 'disk', 'agent_tables': [
            {'rows': [{'row_actions': [{'id': 'reset'}]}]}], 'overall': {'level': 'ok'}}))
        slow, started, release = self.build_gate()
        with patch.object(d, '_state', side_effect=slow):
            t0 = time.monotonic()
            state = json.loads(d._cached_state())
            self.assertLess(time.monotonic()-t0, .1)
            self.assertEqual(state['marker'], 'disk')
            self.assertTrue(state['stale'])
            self.assertEqual(state['overall']['level'], 'loading')
            row = state['agent_tables'][0]['rows'][0]
            self.assertNotIn('row_actions', row)
            self.assertTrue(row['actions_locked'])
            self.assertTrue(started.wait(1))
            self.finish(release)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(d._cached_state())['marker'], 'new')

    def test_expired_and_corrupt_disk_snapshot_are_ignored(self):
        path = Path(self.tmp.name)/'state-snapshot.json'
        for body, age in [(json.dumps({'agents': [], 'marker': 'ancient'}), 901), ('{', 0)]:
            path.write_text(body)
            os.utime(path, (time.time()-age,)*2)
            d.invalidate_state_cache()
            d._state_disk_checked = False
            slow, _, release = self.build_gate()
            with patch.object(d, '_state', side_effect=slow):
                state = json.loads(d._cached_state())
                self.assertNotIn('marker', state)
                self.assertEqual(state['overall']['level'], 'loading')
                self.finish(release)

    def test_idle_memory_snapshot_is_immediate_with_age(self):
        d._state_cache = (time.monotonic()-65, b'{"agents":[],"marker":"old"}')
        slow, started, release = self.build_gate()
        with patch.object(d, '_state', side_effect=slow) as st:
            t0 = time.monotonic()
            self.assertEqual(json.loads(d._cached_state())['marker'], 'old')
            self.assertLess(time.monotonic()-t0, .1)
            self.assertTrue(started.wait(1))
            self.assertGreater(json.loads(d._cached_state())['state_age_seconds'], 60)
            self.assertEqual(st.call_count, 1)
            self.finish(release)

    def test_action_discards_inflight_build_and_disk(self):
        slow, started, release = self.build_gate()
        with patch.object(d, '_state', side_effect=slow):
            d._cached_state()
            self.assertTrue(started.wait(1))
            d.invalidate_state_cache()
            self.finish(release)
        self.assertIsNone(d._state_cache)
        self.assertFalse((Path(self.tmp.name)/'state-snapshot.json').exists())
        with patch.object(d, '_state', return_value=b'{"agents":[],"marker":"post-action"}'):
            d._cached_state()
            self.finish(release)
        self.assertEqual(json.loads(d._cached_state())['marker'], 'post-action')

    def test_failed_build_releases_flag_and_retries(self):
        with patch.object(d, '_state', side_effect=RuntimeError('x')):
            d._cached_state()
            self.finish(threading.Event())
        self.assertFalse(d._state_building)
        self.assertIsNone(d._state_cache)

    def test_loop_fills_cache_immediately_without_browser(self):
        stop = threading.Event()
        def build():
            stop.set()
            return b'{"agents":[],"marker":"loop"}'
        with patch.object(d, '_state', side_effect=build):
            d._quota_loop(stop, interval=.01)
            self.finish(threading.Event())
        self.assertEqual(json.loads(d._state_cache[1])['marker'], 'loop')

    def test_disk_quotas_survive_initial_empty_build_but_have_fixed_expiry(self):
        data = {'agents': [], 'services': [], 'agent_tables': [], 'plugins': [
            {'id': 'slow', 'loading': True}, {'id': 'removed', 'loading': False}], 'plugin_loading': True}
        old = {'time': int(time.time()-65), 'agent_tables': [
            {'id': 'slow:t', 'title': 'Quotas', 'rows': [{'id': 'a', 'alive': True, 'row_actions': [1]}]},
            {'id': 'removed:t', 'title': 'Removed', 'rows': [{'id': 'b'}]}]}
        kept = d._retain_pending_tables(data, old, 65)
        self.assertEqual(len(kept['agent_tables']), 1)
        self.assertTrue(kept['agent_tables'][0]['stale'])
        self.assertNotIn('row_actions', kept['agent_tables'][0]['rows'][0])
        self.assertEqual(kept['overall']['level'], 'loading')
        kept['agent_tables'][0]['rows'][0]['snapshot_at'] = time.time()-601
        fresh = {'agent_tables': [], 'plugins': [{'id': 'slow', 'loading': True}]}
        self.assertEqual(d._retain_pending_tables(fresh, kept, 0)['agent_tables'], [])
