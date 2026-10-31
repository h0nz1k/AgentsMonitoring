"""Telegram quota alerts: thresholds, capacity-back, forecast, dedupe, rate limit, async sender."""
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agentsmon import alerts, config, quota

TZ = ZoneInfo("Europe/Prague")
# 31.10.2026 12:00 Prague; the 7d window resets 04.11 07:02 Prague.
NOW = int(datetime(2026, 10, 31, 12, 0, tzinfo=TZ).timestamp())
RESET = int(datetime(2026, 11, 4, 7, 2, tzinfo=TZ).timestamp())
CFG = dict(alerts.DEFAULTS, timezone="Europe/Prague")


def item(used, reset_at=RESET, source="Codex · agentops", label="7d"):
    return {"source": source, "label": label, "used": float(used), "reset_at": reset_at}


def fc(will, exhaust_at=NOW + 3 * 3600, reset_at=RESET, source="Codex · agentops", label="7d"):
    return {"source": source, "label": label, "used_pct": 60.0, "reset_at": reset_at, "exhaust_at": exhaust_at,
            "projected_at_reset": 100.0 if will else 70.0, "will_exhaust_before_reset": will,
            "rate_pct_per_hour": 5.1, "span_seconds": 3600}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        self.sent = []

    def tearDown(self):
        config.state_dir = self._orig
        self.tmp.cleanup()

    def run_eval(self, items, forecasts=(), now=NOW, cfg=None):
        return alerts.evaluate(cfg or CFG, items, list(forecasts), now, self.sent.append)


class Settings(unittest.TestCase):
    def test_defaults_and_override(self):
        s = alerts.settings({})
        self.assertTrue(s["enabled"])
        self.assertEqual(s["thresholds"], [80, 95])
        self.assertEqual(s["min_interval_seconds"], 1800)
        self.assertFalse(s["dry_run"])
        self.assertEqual(s["command"], ["hermes", "--profile", "lana", "send", "-q"])
        self.assertNotIn("-t", s["command"])   # the target is appended once, by build_command
        self.assertEqual(s["target"], "telegram")
        self.assertEqual(alerts.settings({"alerts": {"thresholds": [50], "dry_run": True}})["thresholds"], [50])

    def test_command_appends_message_last(self):
        cmd = alerts.build_command(alerts.settings({}), "ahoj")
        self.assertEqual(cmd, ["hermes", "--profile", "lana", "send", "-q", "-t", "telegram", "ahoj"])
        cmd = alerts.build_command(alerts.settings({"alerts": {"target": "telegram:42"}}), "m")
        self.assertEqual(cmd, ["hermes", "--profile", "lana", "send", "-q", "-t", "telegram:42", "m"])
        cmd = alerts.build_command(dict(alerts.DEFAULTS, command=["x", "send", "-t", "own"]), "m")
        self.assertEqual(cmd, ["x", "send", "-t", "own", "m"])
        cmd = alerts.build_command(dict(alerts.DEFAULTS, command=["x", "send"], target="telegram:1"), "m")
        self.assertEqual(cmd, ["x", "send", "-t", "telegram:1", "m"])


class Formatting(unittest.TestCase):
    def test_reset_text(self):
        self.assertEqual(alerts.fmt_reset(RESET, NOW, TZ), "04.11 07:02 (za 3 d 19 h)")

    def test_percent_czech(self):
        self.assertEqual(alerts.fmt_pct(95.0), "95 %")
        self.assertEqual(alerts.fmt_pct(73.64), "73,6 %")


class Rules(Base):
    def test_first_sight_seeds_silently_even_when_exhausted(self):
        self.run_eval([item(100)])
        self.run_eval([item(100)], now=NOW + 7200)
        self.assertEqual(self.sent, [])

    def test_threshold_crossing_alerts_once(self):
        self.run_eval([item(50)])
        self.run_eval([item(81)], now=NOW + 60)
        self.run_eval([item(82)], now=NOW + 120)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("⚠️ Codex · agentops 7d: využito 81 %", self.sent[0])
        self.assertIn("reset 04.11 07:02 (za 3 d 19 h)", self.sent[0])

    def test_higher_threshold_after_rate_limit(self):
        self.run_eval([item(50)])
        self.run_eval([item(81)], now=NOW + 60)
        self.run_eval([item(96)], now=NOW + 120)          # within 30 min → held back
        self.assertEqual(len(self.sent), 1)
        self.run_eval([item(96)], now=NOW + 60 + 1800)    # interval passed → delivered once
        self.run_eval([item(97)], now=NOW + 60 + 3700)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("využito 96 %", self.sent[1])

    def test_exhausted_message(self):
        self.run_eval([item(10)])
        self.run_eval([item(100)], now=NOW + 60)
        self.assertTrue(self.sent[0].startswith("🛑 Codex · agentops 7d: vyčerpáno (100 %)"))

    def test_capacity_back_after_reset_once(self):
        self.run_eval([item(100)])                          # seeded: already exhausted
        nxt = RESET + 7 * 86400
        self.run_eval([item(0, reset_at=nxt)], now=RESET + 60)
        self.run_eval([item(1, reset_at=nxt)], now=RESET + 4000)
        self.assertEqual(self.sent, ["✅ Codex · agentops 7d: kapacita zpět (reset proběhl)"])

    def test_capacity_back_when_usage_drops_without_reset(self):
        self.run_eval([item(10)])
        self.run_eval([item(85)], now=NOW + 60)
        self.run_eval([item(20)], now=NOW + 60 + 1800)
        self.assertEqual(self.sent[1], "✅ Codex · agentops 7d: kapacita zpět (využito 20 %)")

    def test_notify_on_reset_disabled(self):
        cfg = dict(CFG, notify_on_reset=False)
        self.run_eval([item(100)], cfg=cfg)
        self.run_eval([item(0, reset_at=RESET + 86400)], now=RESET + 60, cfg=cfg)
        self.assertEqual(self.sent, [])

    def test_forecast_exhaustion_once_per_window(self):
        self.run_eval([item(60)])
        self.run_eval([item(60)], [fc(True)], now=NOW + 60)
        self.run_eval([item(61)], [fc(True)], now=NOW + 7200)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("📈 Codex · agentops 7d: tempem vyčerpáno ~15:00 (před resetem 04.11 07:02)", self.sent[0])
        self.run_eval([item(61)], [fc(False)], now=NOW + 9000)
        self.assertEqual(len(self.sent), 1)

    def test_forecast_needs_enough_history_for_an_alert(self):
        self.run_eval([item(60)])
        self.run_eval([item(60)], [dict(fc(True), span_seconds=900)], now=NOW + 60)
        self.assertEqual(self.sent, [])

    def test_forecast_disabled(self):
        cfg = dict(CFG, notify_on_forecast_exhaust=False)
        self.run_eval([item(60)], cfg=cfg)
        self.run_eval([item(60)], [fc(True)], now=NOW + 60, cfg=cfg)
        self.assertEqual(self.sent, [])

    def test_dedupe_survives_restart(self):
        self.run_eval([item(50)])
        self.run_eval([item(96)], now=NOW + 60)
        quota.reset_cache()                                 # no in-memory state matters
        self.run_eval([item(96)], now=NOW + 99999)
        self.assertEqual(len(self.sent), 1)

    def test_series_are_independent(self):
        self.run_eval([item(10), item(10, label="5h")])
        self.run_eval([item(90), item(90, label="5h")], now=NOW + 60)
        self.assertEqual(len(self.sent), 2)


class Tick(Base):
    def setUp(self):
        super().setUp()
        alerts._reset_for_tests()

    def tables(self, used):
        return [{"rows": [{"name": "Codex · agentops", "cells": {"w": {"bars": [
            {"label": "7d", "percent": used, "reset_at": RESET, "state": "OK"}]}}}]}]

    def test_inactive_until_started(self):
        with patch.object(alerts, "dispatch") as d:
            alerts.tick({"alerts": CFG}, self.tables(10), [], [], now=NOW)
            alerts.tick({"alerts": CFG}, self.tables(99), [], [], now=NOW + 120)
        d.assert_not_called()
        self.assertFalse(Path(self.tmp.name, "quota.sqlite").exists())

    def test_started_tick_dispatches_and_throttles(self):
        alerts.start()
        with patch.object(alerts, "dispatch") as d:
            alerts.tick({"alerts": CFG}, self.tables(10), [], [], now=NOW)
            alerts.tick({"alerts": CFG}, self.tables(99), [], [], now=NOW + 5)    # throttled (<60 s)
            d.assert_not_called()
            alerts.tick({"alerts": CFG}, self.tables(99), [], [], now=NOW + 120)
        d.assert_called_once()
        self.assertIn("využito 99 %", d.call_args[0][0])

    def test_empty_snapshot_does_not_consume_the_tick(self):
        alerts.start()
        with patch.object(alerts, "dispatch") as d:
            alerts.tick({"alerts": CFG}, self.tables(10), [], [], now=NOW)
            alerts.tick({"alerts": CFG}, [], [], [], now=NOW + 120)        # plugins still loading
            alerts.tick({"alerts": CFG}, self.tables(99), [], [], now=NOW + 125)
        d.assert_called_once()

    def test_disabled_config(self):
        alerts.start()
        with patch.object(alerts, "dispatch") as d:
            alerts.tick({"alerts": {"enabled": False}}, self.tables(10), [], [], now=NOW)
            alerts.tick({"alerts": {"enabled": False}}, self.tables(99), [], [], now=NOW + 120)
        d.assert_not_called()

    def test_db_failure_never_raises(self):
        alerts.start()
        with patch.object(alerts, "_conn", side_effect=sqlite3.OperationalError("disk")):
            alerts.tick({"alerts": CFG}, self.tables(99), [], [], now=NOW)


class Sender(Base):
    def test_dry_run_never_spawns(self):
        with patch.object(alerts.subprocess, "run") as run:
            alerts.dispatch("zpráva", dict(CFG, dry_run=True))
            alerts._drain()
        run.assert_not_called()
        self.assertIn("DRY-RUN zpráva", Path(self.tmp.name, "alerts.log").read_text(encoding="utf-8"))

    def test_real_send_runs_command_in_background_with_timeout(self):
        done = threading.Event()

        def fake_run(cmd, **kw):
            self.assertEqual(cmd[-1], "zpráva")
            self.assertIn("timeout", kw)
            self.assertNotEqual(threading.current_thread(), threading.main_thread())
            done.set()
            return type("R", (), {"returncode": 0})()
        with patch.object(alerts.subprocess, "run", side_effect=fake_run):
            alerts.dispatch("zpráva", CFG)
            alerts._drain()
        self.assertTrue(done.is_set())

    def test_sender_failure_never_raises(self):
        with patch.object(alerts.subprocess, "run", side_effect=FileNotFoundError("hermes")):
            alerts.dispatch("zpráva", CFG)
            alerts._drain()
        self.assertIn("selhalo", Path(self.tmp.name, "alerts.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
