"""Regression tests for the 2026-11-22 reliability review (items 1–12).

Each assertion below fails on baseline commit 31a5a30 and passes after the fixes.
"""
from __future__ import annotations

import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import alerts, config, dashboard, db, detect, gateway_restart as gw, probe


class B1MetricsUsesCache(unittest.TestCase):
    def test_metrics_handler_calls_cached_state_not_state_data(self):
        src = inspect.getsource(dashboard.make_server)
        # Handler.do_GET is nested; search the whole make_server source.
        self.assertIn("metrics.render(json.loads(_cached_state()))", src)
        self.assertNotRegex(src, r"metrics\.render\(_state_data\(\)\)")


class B2UptimeRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        db._last_prune = 0
        db.close_thread_connection()

    def tearDown(self):
        db.close_thread_connection()
        config.state_dir = self._orig
        self.tmp.cleanup()

    def test_uptime_seconds_sql_has_time_bound(self):
        src = " ".join(inspect.getsource(db.uptime_seconds).split())
        self.assertIn("WHERE service=? AND ts>=?", src)

    def test_prune_deletes_old_probes_at_most_daily(self):
        now = int(time.time())
        # Insert without triggering record()'s auto-prune so retention has something to delete.
        with db._db() as c:
            c.execute("INSERT INTO probes(ts,service,up,latency,detail) VALUES(?,?,?,?,?)",
                      (now - 40 * 86400, "svc", 1, 0.1, ""))
            c.execute("INSERT INTO probes(ts,service,up,latency,detail) VALUES(?,?,?,?,?)",
                      (now, "svc", 1, 0.1, ""))
        db._last_prune = 0
        n = db.prune_probes(now=now, force=True)
        self.assertGreaterEqual(n, 1)
        with db._db() as c:
            rows = c.execute("SELECT ts FROM probes WHERE service=?", ("svc",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["ts"]), now)
        self.assertEqual(db.prune_probes(now=now), 0)


class B3ThreadLocalConn(unittest.TestCase):
    def test_conn_reused_on_same_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = config.state_dir
            config.state_dir = lambda: Path(tmp)
            db.close_thread_connection()
            try:
                a = db._conn()
                b = db._conn()
                self.assertIs(a, b)
            finally:
                db.close_thread_connection()
                config.state_dir = orig


class M1M2QuotaFreshness(unittest.TestCase):
    def test_empty_bars_are_unavailable_not_ok(self):
        page = dashboard.PAGE
        # Empty card / no bars → NEDOSTUPNÉ (not gated on bars.length alone).
        self.assertIn("!bars.length||bars.every(isUnavail)", page.replace(" ", ""))
        self.assertNotIn("QUOTA_STALE_S=7200", page)
        self.assertIn("Math.min(3*", page)
        self.assertIn("1800", page)


class M3FailingRebuild(unittest.TestCase):
    def test_rebuild_logs_exception_message(self):
        src = inspect.getsource(dashboard._rebuild_state)
        self.assertIn("{type(exc).__name__}: {exc}", src)

    def test_cached_state_demotes_overall_when_age_over_30(self):
        d = dashboard
        d._state_cache = (time.monotonic() - 45, json.dumps({
            "overall": {"level": "ok", "text_cs": "Vše v provozu", "count": 0, "total": 0},
            "agents": [], "services": [], "agent_tables": [],
        }).encode())
        d._state_from_disk = False
        d._state_building = True  # prevent background rebuild
        d._state_disk_checked = True
        try:
            data = json.loads(d._cached_state())
            self.assertTrue(data["stale"])
            self.assertEqual(data["overall"]["level"], "warning")
        finally:
            d._state_cache = None
            d._state_building = False


class M5DebounceAndTimeout(unittest.TestCase):
    def setUp(self):
        detect._ALIVE_FAILS.clear()
        detect._ALIVE_SEEN_UP.clear()

    def test_pinned_uses_shared_health_timeout(self):
        src = inspect.getsource(detect.pinned_agents)
        self.assertIn("probe.HEALTH_TIMEOUT", src)
        self.assertNotIn("timeout=2", src)
        self.assertEqual(probe.HEALTH_TIMEOUT, probe._http.__defaults__[0])

    def test_pinned_debounce_needs_min_outage_fails(self):
        calls = {"n": 0}

        def fake_http(url, timeout=4):
            calls["n"] += 1
            return False, None

        pinned = [{"name": "Hermes", "process": "", "health_url": "http://127.0.0.1:9/health"}]
        with patch.object(probe, "_http", side_effect=fake_http):
            # seed as previously up
            detect._ALIVE_SEEN_UP.add("Hermes")
            detect._ALIVE_FAILS["Hermes"] = 0
            r1 = detect.pinned_agents(pinned, min_outage=3)
            self.assertTrue(r1[0]["alive"])  # 1st fail → still up
            r2 = detect.pinned_agents(pinned, min_outage=3)
            self.assertTrue(r2[0]["alive"])  # 2nd fail → still up
            r3 = detect.pinned_agents(pinned, min_outage=3)
            self.assertFalse(r3[0]["alive"])  # 3rd fail → down


class M6ForecastThresholds(unittest.TestCase):
    def test_short_span_forecast_not_in_attention(self):
        now = time.time()
        f = {"source": "Codex", "label": "7d", "used_pct": 60.0, "reset_at": now + 3 * 86400,
             "exhaust_at": now + 3600, "will_exhaust_before_reset": True, "exhausted": False,
             "span_seconds": 600}
        att, _ = dashboard.compute_attention({"forecasts": [f]}, now=now)
        self.assertEqual(att, [])

    def test_page_requires_actionable_for_dojde_drive(self):
        self.assertIn("f.actionable", dashboard.PAGE)
        self.assertIn("will_exhaust_before_reset&&f.actionable", dashboard.PAGE.replace(" ", ""))


class M4WaitingStable(unittest.TestCase):
    def test_tail_is_three_and_requires_two_reads(self):
        self.assertEqual(detect.WAITING_TAIL_LINES, 3)
        detect._PANE_CACHE.clear()
        detect._PANE_PREV.clear()
        rows = [{"name": "Claude", "kind": "claude-code", "alive": True}]
        text = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"

        class R:
            def __init__(self, out):
                self.stdout, self.returncode = out, 0

        with patch.object(detect, "_run", return_value=R(text)):
            detect.annotate_waiting(rows, {}, now=1000.0)
            self.assertNotIn("waiting", rows[0])
            detect.annotate_waiting(rows, {}, now=1011.0)
            self.assertTrue(rows[0].get("waiting"))
        detect._PANE_CACHE.clear()
        detect._PANE_PREV.clear()


class M8NoInstallDefaults(unittest.TestCase):
    def test_alerts_defaults_have_no_lan_ip_or_lana_profile(self):
        self.assertEqual(alerts.DEFAULTS["dashboard_url"], "")
        self.assertNotIn("192.168.1.153", alerts.DEFAULTS["dashboard_url"])
        self.assertNotIn("--profile", alerts.DEFAULTS["command"])
        self.assertNotIn("lana", alerts.DEFAULTS["command"])
        self.assertEqual(gw.normalize_profile(None), "")
        self.assertNotIn("lana", gw.hermes_profiles({"pinned_daemons": [], "daemons": []}))


class M9ShellSafeCopy(unittest.TestCase):
    def test_attach_cmd_uses_single_quote_escape(self):
        page = dashboard.PAGE
        self.assertIn("replace(/'/g,\"'\\\\''\")", page)
        self.assertNotIn('tmux attach -t "${a.tmux_session||a.name}"', page)


class M11MobileVisible(unittest.TestCase):
    def test_hero_resets_and_who_not_hidden_under_700px(self):
        page = dashboard.PAGE
        self.assertNotRegex(page, r"@media\(max-width:699px\)\{[^}]*\.who\{display:none}")
        self.assertNotRegex(page, r"@media\(max-width:699px\)\{[^}]*#hero-resets\{display:none}")


class M12CiFrontend(unittest.TestCase):
    def test_ci_workflow_installs_node_and_jsdom(self):
        yml = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        text = yml.read_text(encoding="utf-8")
        self.assertIn("setup-node", text)
        self.assertIn("jsdom", text)
        self.assertIn("AGENTSMON_NODE", text)
        self.assertIn("AGENTSMON_JSDOM", text)
        self.assertIn('CI: "true"', text)

    def test_jsdom_tests_fail_not_skip_when_ci_missing_tools(self):
        import importlib.util
        path = Path(__file__).with_name("test_incremental_render.py")
        spec = importlib.util.spec_from_file_location("test_incremental_render", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = inspect.getsource(mod.IncrementalRenderJsdom.setUp)
        self.assertIn("CI", src)
        self.assertIn("self.fail", src)


class MinorFixes(unittest.TestCase):
    def test_timeline_green_is_99(self):
        self.assertIn("const GREEN=99", dashboard.PAGE)

    def test_actions_jsonl_rotates(self):
        src = inspect.getsource(gw.log_action)
        self.assertIn("ACTIONS_MAX_BYTES", src)
        self.assertIn(".1", src)

    def test_probe_typo_and_weekend_label(self):
        self.assertNotIn("délší dobu", inspect.getsource(probe))
        self.assertIn("delší dobu", inspect.getsource(probe))
        self.assertIn(r"\bWeekend\b", dashboard.PAGE)


if __name__ == "__main__":
    unittest.main()
