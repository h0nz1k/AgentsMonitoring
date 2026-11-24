"""Acceptance tests for issue #90 — fail on baseline, pass after the improvements.

0. plugin_loading only on first snapshot (not during refresh over a valid cache)
1. pending (yellow) until min_outage consecutive fails
2. probes_daily aggregation; SLA/timeline read the aggregate
3. incidents + continuous timeline colouring in the page
4. maintenance excluded from SLA; attention mute (admin + audit)
5. latency sparkline 24 h on service cards
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentsmon import config, dashboard, db, detect, plugins


class T0PluginLoadingFirstOnly(unittest.TestCase):
    def setUp(self):
        self.manager = plugins.Manager({"modules_dir": "/nonexistent-fixture-90"})
        self.manager.plugins = [
            SimpleNamespace(id="quotas", version="1", timeout=15,
                            settings={"refresh_seconds": 15})
        ]

    def test_after_first_snapshot_loading_false_even_while_refresh_pending(self):
        data = {"tables": [{"id": "t", "rows": []}]}
        self.manager._snapshots["quotas"] = ("[]", time.monotonic() - 20, data)
        with patch.object(plugins.threading, "Thread") as th:
            result = self.manager.snapshot([])
        self.assertTrue(th.called)  # refresh scheduled (TTL 15 s, age 20 s)
        self.assertIn("quotas", self.manager._pending)
        plug = next(p for p in result["plugins"] if p["id"] == "quotas")
        self.assertFalse(plug["loading"])
        self.assertFalse(result["loading"])
        _, ov = dashboard.compute_attention({
            "plugin_loading": result["loading"], "agent_tables": [{"id": "t", "rows": []}],
            "agents": [], "services": []})
        self.assertNotEqual(ov["level"], "loading")

    def test_first_load_still_reports_loading(self):
        with patch.object(plugins.threading, "Thread"):
            result = self.manager.snapshot([])
        self.assertTrue(result["loading"])
        self.assertTrue(result["plugins"][0]["loading"])

    def test_refresh_ttl_from_plugin_settings(self):
        p = SimpleNamespace(timeout=3, settings={"refresh_seconds": 15})
        self.assertEqual(plugins._refresh_ttl(p), 15.0)
        p2 = SimpleNamespace(timeout=15, settings={})
        self.assertGreaterEqual(plugins._refresh_ttl(p2), 15.0)


class T1PendingStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        db.close_thread_connection()
        db._last_prune = 0
        detect._ALIVE_FAILS.clear()
        detect._ALIVE_SEEN_UP.clear()
        self.addCleanup(db.close_thread_connection)
        self.addCleanup(lambda: setattr(config, "state_dir", self._orig))

    def test_service_pending_then_outage(self):
        now = int(time.time())
        db.record("Svc", True, ts=now - 90)
        db.record("Svc", False, ts=now - 30)
        self.assertEqual(db.probe_state("Svc", min_outage=3), "pending")
        card = dashboard._service_state({
            "services": [{"name": "Svc"}],
            "probe": {"sla_window_days": 1, "timeline_days": 1, "min_outage_samples": 3,
                      "interval_seconds": 60},
        })[0]
        self.assertEqual(card["state"], "pending")
        self.assertTrue(card["up"])
        db.record("Svc", False, ts=now - 15)
        db.record("Svc", False, ts=now)
        self.assertEqual(db.probe_state("Svc", min_outage=3), "outage")
        self.assertEqual(dashboard._service_state({
            "services": [{"name": "Svc"}],
            "probe": {"sla_window_days": 1, "timeline_days": 1, "min_outage_samples": 3},
        })[0]["state"], "outage")

    def test_pinned_exposes_pending_state(self):
        detect._ALIVE_SEEN_UP.add("Hermes")
        detect._ALIVE_FAILS["Hermes"] = 0
        pinned = [{"name": "Hermes", "process": "", "health_url": "http://127.0.0.1:9/h"}]
        with patch("agentsmon.probe._http", return_value=(False, None)):
            r1 = detect.pinned_agents(pinned, min_outage=3)
            self.assertEqual(r1[0]["state"], "pending")
            self.assertTrue(r1[0]["alive"])
            r2 = detect.pinned_agents(pinned, min_outage=3)
            self.assertEqual(r2[0]["state"], "pending")
            r3 = detect.pinned_agents(pinned, min_outage=3)
            self.assertEqual(r3[0]["state"], "outage")
            self.assertFalse(r3[0]["alive"])

    def test_page_has_pending_state_style(self):
        self.assertIn("pending:", dashboard.PAGE)
        self.assertIn("Ověřuji", dashboard.PAGE)


class T2DailyAggregation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        db.close_thread_connection()
        db._last_prune = 0
        db._last_rollup = 0
        self.addCleanup(db.close_thread_connection)
        self.addCleanup(lambda: setattr(config, "state_dir", self._orig))

    def test_rollup_creates_probes_daily_idempotent(self):
        now = int(time.time())
        today = now - (now % 86400)
        yesterday = today - 86400
        for i in range(10):
            db.record("Agg", i % 2 == 0, latency=0.05, ts=yesterday + i * 60)
        db.record("Agg", True, latency=0.01, ts=today + 10)
        n = db.rollup_daily(now=now, force=True)
        self.assertGreaterEqual(n, 1)
        rows = db.daily_rows("Agg")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["day"], yesterday)
        self.assertEqual(rows[0]["total_n"], 10)
        self.assertEqual(rows[0]["up_n"], 5)
        n2 = db.rollup_daily(now=now, force=True)
        self.assertGreaterEqual(n2, 1)
        self.assertEqual(len(db.daily_rows("Agg")), 1)

    def test_sla_uses_daily_when_present(self):
        now = int(time.time())
        today = now - (now % 86400)
        yesterday = today - 86400
        with db._db() as c:
            c.execute(
                "INSERT INTO probes_daily(service,day,up_n,total_n,avg_latency) VALUES(?,?,?,?,?)",
                ("Agg", yesterday, 90, 100, 0.05))
            c.execute("INSERT INTO probes(ts,service,up,latency,detail) VALUES(?,?,?,?,?)",
                      (today + 1, "Agg", 1, 0.01, ""))
        pct, n = db.sla("Agg", 2 * 86400, now=now)
        self.assertEqual(n, 101)
        self.assertAlmostEqual(pct, 100.0 * 91 / 101, places=2)

    def test_old_db_opens_without_data_loss(self):
        path = Path(self.tmp.name) / "uptime.sqlite"
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE probes(ts INTEGER, service TEXT, up INTEGER, latency REAL, detail TEXT)")
        c.execute("INSERT INTO probes VALUES(?,?,?,?,?)", (1_700_000_000, "Legacy", 1, 0.1, "ok"))
        c.commit()
        c.close()
        db.close_thread_connection()
        row = db.last("Legacy")
        self.assertEqual(row["service"], "Legacy")
        self.assertEqual(row["up"], 1)
        # New tables exist after open.
        with db._db() as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("probes_daily", names)
        self.assertIn("maintenance", names)
        self.assertIn("attention_mutes", names)


class T3IncidentsAndTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        db.close_thread_connection()
        self.addCleanup(db.close_thread_connection)
        self.addCleanup(lambda: setattr(config, "state_dir", self._orig))

    def test_incidents_from_confirmed_outage(self):
        now = 1_700_000_000
        db.record("I", True, ts=now - 600)
        for i, ts in enumerate((now - 500, now - 400, now - 300)):
            db.record("I", False, detail=f"down-{i}", ts=ts)
        db.record("I", True, ts=now - 100)
        items = db.incidents("I", 86400, min_outage=3, now=now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["start"], now - 500)
        self.assertEqual(items[0]["end"], now - 100)
        self.assertEqual(items[0]["duration"], 400)
        self.assertIn("down-", items[0]["detail"])

    def test_service_card_includes_incidents(self):
        now = int(time.time())
        db.record("Card", True, ts=now - 600)
        for ts in (now - 500, now - 400, now - 300):
            db.record("Card", False, detail="x", ts=ts)
        db.record("Card", True, ts=now)
        card = dashboard._service_state({
            "services": [{"name": "Card"}],
            "probe": {"sla_window_days": 1, "timeline_days": 1, "min_outage_samples": 3},
        })[0]
        self.assertTrue(card["incidents"])

    def test_page_uses_continuous_timeline_color(self):
        self.assertIn("timelineColor", dashboard.PAGE)
        self.assertIn("incidentsHtml", dashboard.PAGE)
        self.assertNotIn("const GREEN=99", dashboard.PAGE)


class T4MaintenanceAndMute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        db.close_thread_connection()
        self.addCleanup(db.close_thread_connection)
        self.addCleanup(lambda: setattr(config, "state_dir", self._orig))

    def test_maintenance_excluded_from_sla(self):
        now = int(time.time())
        for i in range(10):
            db.record("M", False, ts=now - 500 + i)  # would be 0 % without maint
        db.add_maintenance("M", now - 600, now + 60, note="planned")
        pct, n = db.sla("M", 3600, now=now)
        # All samples fall inside maintenance → excluded → no countable samples.
        self.assertIsNone(pct)
        self.assertEqual(n, 0)

    def test_mute_hides_attention_item(self):
        payload = {
            "services": [{"name": "Svc", "state": "outage", "anchor": "services"}],
            "agents": [], "agent_tables": [],
        }
        att, _ = dashboard.compute_attention(payload)
        hit = next(a for a in att if "výpadek" in a["text_cs"])
        db.mute_attention(hit["mute_key"], hours=2, user="admin@example.com",
                          text_cs=hit["text_cs"])
        att2, _ = dashboard.compute_attention(payload)
        self.assertFalse(any("výpadek" in a["text_cs"] for a in att2))

    def test_page_has_mute_control_and_csrf_header(self):
        self.assertIn("mute_attention", dashboard.PAGE)
        self.assertIn("Ztlumit 4 h", dashboard.PAGE)
        self.assertIn('"X-Agentsmon":"action"', dashboard.PAGE.replace(" ", ""))


class T5LatencySparkline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        db.close_thread_connection()
        self.addCleanup(db.close_thread_connection)
        self.addCleanup(lambda: setattr(config, "state_dir", self._orig))

    def test_sparkline_buckets_from_probes(self):
        now = int(time.time())
        for i in range(24):
            db.record("Lat", True, latency=0.01 * (i + 1), ts=now - (23 - i) * 3600)
        spark = db.latency_sparkline("Lat", 86400, 24, now=now)
        self.assertEqual(len(spark), 24)
        self.assertTrue(any(v is not None for v in spark))
        card = dashboard._service_state({
            "services": [{"name": "Lat"}],
            "probe": {"sla_window_days": 1, "timeline_days": 1, "min_outage_samples": 3},
        })[0]
        self.assertEqual(len(card["latency_sparkline"]), 24)
        self.assertIn("sparklineSvg", dashboard.PAGE)
        self.assertIn("lat-spark", dashboard.PAGE)


if __name__ == "__main__":
    unittest.main()
