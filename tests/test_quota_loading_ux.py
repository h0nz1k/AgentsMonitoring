"""Quota loading UX: no fake 'all OK' before plugin data, cache not invalidated by probe noise."""
import time
import unittest
from unittest.mock import patch

from agentsmon import dashboard, plugins


class OverallWhileLoading(unittest.TestCase):
    def test_loading_without_tables_is_not_ok(self):
        _, ov = dashboard.compute_attention({"plugin_loading": True, "agent_tables": [], "agents": [], "services": []})
        self.assertEqual(ov["level"], "loading")
        self.assertNotIn("Vše běží", ov["summary_cs"])

    def test_loaded_or_stale_data_uses_normal_level(self):
        _, ov = dashboard.compute_attention({"plugin_loading": False, "agent_tables": [], "agents": [], "services": []})
        self.assertEqual(ov["level"], "ok")
        _, ov = dashboard.compute_attention({"plugin_loading": True, "agent_tables": [{"id": "t", "rows": []}],
                                             "agents": [], "services": []})
        self.assertNotEqual(ov["level"], "loading")

    def test_ui_has_loading_ring(self):
        self.assertIn('lv==="loading"', dashboard.INDEX_HTML if hasattr(dashboard, "INDEX_HTML") else
                      open(dashboard.__file__, encoding="utf-8").read())


class CacheSignature(unittest.TestCase):
    def test_latency_change_keeps_fresh_cache(self):
        cfg = {"modules_dir": "/nonexistent-agentsmon-test"}
        snap = {"tables": [{"id": "t"}]}
        with patch.multiple(plugins, _manager=None, _signature=None, _cache=None, _refreshing=False):
            plugins.manager(cfg)
            a1 = [{"id": "a", "name": "A", "alive": True, "latency_ms": 10, "age": 5}]
            a2 = [{"id": "a", "name": "A", "alive": True, "latency_ms": 99, "age": 9}]
            sig = __import__("json").dumps([{k: v for k, v in a.items() if k in plugins.PUBLIC_FIELDS
                                             and k not in ("age", "latency_ms")} for a in a1], sort_keys=True)
            plugins._cache = (sig, time.monotonic(), snap)
            with patch.object(plugins.threading, "Thread") as th:
                self.assertIs(plugins.collect(cfg, a2), snap)
                th.assert_not_called()


class WarmUp(unittest.TestCase):
    def test_warm_plugins_retries_until_loaded(self):
        snaps = iter([{"plugin_loading": True}, {"plugin_loading": True}, {"plugin_loading": False}])
        with patch.object(dashboard, "_state_data", side_effect=lambda: next(snaps)) as sd, \
                patch.object(dashboard.time, "sleep") as sl:
            self.assertTrue(dashboard._warm_plugins(tries=5, pause=0))
        self.assertEqual(sd.call_count, 3)
        self.assertEqual(sl.call_count, 2)


if __name__ == "__main__":
    unittest.main()
