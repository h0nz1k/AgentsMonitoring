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

    def test_partial_tables_cannot_claim_overall_ok(self):
        _, ov = dashboard.compute_attention({"plugin_loading": False, "agent_tables": [], "agents": [], "services": []})
        self.assertEqual(ov["level"], "ok")
        _, ov = dashboard.compute_attention({"plugin_loading": True, "agent_tables": [{"id": "t", "rows": []}],
                                             "agents": [], "services": []})
        self.assertEqual(ov["level"], "loading")

    def test_ui_has_loading_ring(self):
        self.assertIn('lv==="loading"', dashboard.INDEX_HTML if hasattr(dashboard, "INDEX_HTML") else
                      open(dashboard.__file__, encoding="utf-8").read())


class LoadingBanner(unittest.TestCase):
    def test_banner_only_without_any_plugin_data(self):
        src = open(dashboard.__file__, encoding="utf-8").read()
        self.assertIn('const plWait=!!d.plugin_loading&&!(d.agent_tables||[]).length;', src)
        self.assertIn('pl.hidden=!plWait', src)
        self.assertNotIn('pl.hidden=!d.plugin_loading', src)


class CacheSignature(unittest.TestCase):
    def test_latency_change_keeps_fresh_cache(self):
        cfg = {"modules_dir": "/nonexistent-agentsmon-test"}
        with patch.multiple(plugins, _manager=None, _signature=None):
            manager = plugins.manager(cfg)
            a1 = [{"id": "a", "name": "A", "alive": True, "latency_ms": 10, "age": 5}]
            a2 = [{"id": "a", "name": "A", "alive": True, "latency_ms": 99, "age": 9}]
            from types import SimpleNamespace
            manager.plugins = [SimpleNamespace(id="fixture", version="1")]
            sig = __import__("json").dumps([{"id": "a", "name": "A", "alive": True}], sort_keys=True)
            manager._snapshots["fixture"] = (sig, time.monotonic(), {"tables": [{"id": "t"}]})
            with patch.object(plugins.threading, "Thread") as th:
                self.assertEqual(plugins.collect(cfg, a1)['tables'], plugins.collect(cfg, a2)['tables'])
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
