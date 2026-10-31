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
CFG = dict(alerts.DEFAULTS, timezone="Europe/Prague", digest_seconds=0, quiet_hours=[])   # 1 tick → 1 digest


def item(used, reset_at=RESET, source="Codex · agentops", label="7d"):
    return {"source": source, "label": label, "used": float(used), "reset_at": reset_at}


def fc(will, exhaust_at=NOW + 3 * 3600, reset_at=RESET, source="Codex · agentops", label="7d", span=3600):
    return {"source": source, "label": label, "used_pct": 60.0, "reset_at": reset_at, "exhaust_at": exhaust_at,
            "projected_at_reset": 100.0 if will else 70.0, "will_exhaust_before_reset": will,
            "rate_pct_per_hour": 5.1, "span_seconds": span}


def body(msg):
    """Item lines of a digest (without header/sections/link)."""
    return [ln[2:].replace(" · reset 04.11 07:02", "") for ln in msg.split("\n") if ln.startswith("• ")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        self.sent = []

    def tearDown(self):
        config.state_dir = self._orig
        self.tmp.cleanup()

    def run_eval(self, items, forecasts=(), now=NOW, cfg=None, agents=()):
        return alerts.process(cfg or CFG, items, list(forecasts), list(agents), now, self.sent.append)

    def lines(self):
        return [ln for m in self.sent for ln in body(m)]


class Settings(unittest.TestCase):
    def test_defaults_and_override(self):
        s = alerts.settings({})
        self.assertTrue(s["enabled"])
        self.assertEqual(s["thresholds"], [80, 95])
        self.assertEqual(s["min_interval_seconds"], 1800)
        self.assertEqual(s["digest_seconds"], 600)
        self.assertEqual(s["quiet_hours"], ["22:00", "07:00"])
        self.assertEqual(s["forecast_min_span_seconds"], 3600)
        self.assertEqual(s["forecast_min_elapsed_fraction"], 0.2)
        self.assertEqual(s["forecast_max_horizon_seconds"], 86400)
        self.assertEqual(s["forecast_min_lead_seconds"], 1800)
        self.assertTrue(s["notify_on_model_change"])
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

    def test_window_seconds_from_label(self):
        self.assertEqual(alerts.window_seconds({"label": "Claude 5h"}), 5 * 3600)
        self.assertEqual(alerts.window_seconds({"label": "7d"}), 7 * 86400)
        self.assertEqual(alerts.window_seconds({"label": "Claude/GPT week"}), 7 * 86400)
        self.assertIsNone(alerts.window_seconds({"label": "Included"}))

    def test_quiet_hours_wrap_midnight(self):
        s = dict(alerts.DEFAULTS)
        at = lambda h, m=0: datetime(2026, 10, 31, h, m, tzinfo=TZ).timestamp()
        self.assertTrue(alerts.is_quiet(s, at(23)))
        self.assertTrue(alerts.is_quiet(s, at(6, 59)))
        self.assertFalse(alerts.is_quiet(s, at(7)))
        self.assertFalse(alerts.is_quiet(s, at(21, 59)))
        self.assertFalse(alerts.is_quiet(dict(s, quiet_hours=[]), at(23)))


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
        self.assertIn("⚠️ Prahy\n• Codex · agentops 7d: 81 % · reset 04.11 07:02\n", self.sent[0])

    def test_higher_threshold_after_rate_limit(self):
        self.run_eval([item(50)])
        self.run_eval([item(81)], now=NOW + 60)
        self.run_eval([item(96)], now=NOW + 120)          # within 30 min → held back
        self.assertEqual(len(self.sent), 1)
        self.run_eval([item(96)], now=NOW + 60 + 1800)    # interval passed → delivered once
        self.run_eval([item(97)], now=NOW + 60 + 3700)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Codex · agentops 7d: 96 %", self.sent[1])

    def test_exhausted_message(self):
        self.run_eval([item(10)])
        self.run_eval([item(100)], now=NOW + 60)
        self.assertIn("🛑 Vyčerpáno\n• Codex · agentops 7d · reset 04.11 07:02", self.sent[0])

    def test_capacity_back_after_reset_once(self):
        self.run_eval([item(100)])                          # seeded: already exhausted
        nxt = RESET + 7 * 86400
        self.run_eval([item(0, reset_at=nxt)], now=RESET + 60)
        self.run_eval([item(1, reset_at=nxt)], now=RESET + 4000)
        self.assertEqual(self.lines(), ["Codex · agentops 7d (reset proběhl)"])
        self.assertIn("✅ Kapacita zpět", self.sent[0])

    def test_capacity_back_when_usage_drops_without_reset(self):
        self.run_eval([item(10)])
        self.run_eval([item(85)], now=NOW + 60)
        self.run_eval([item(20)], now=NOW + 60 + 1800)
        self.assertEqual(body(self.sent[1]), ["Codex · agentops 7d (využito 20 %)"])

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
        self.assertIn("📈 Hrozí vyčerpání\n• Codex · agentops 7d: ~15:00 (reset 04.11 07:02) · 60 %, +5,1 %/h",
                      self.sent[0])
        self.run_eval([item(61)], [fc(False)], now=NOW + 9000)
        self.assertEqual(len(self.sent), 1)

    def test_forecast_disabled(self):
        cfg = dict(CFG, notify_on_forecast_exhaust=False)
        self.run_eval([item(60)], cfg=cfg)
        self.run_eval([item(60)], [fc(True)], now=NOW + 60, cfg=cfg)
        self.assertEqual(self.sent, [])

    def test_dedupe_survives_restart(self):
        self.run_eval([item(50)])
        self.run_eval([item(96)], now=NOW + 60)
        quota.reset_cache()                                 # no in-memory state matters
        alerts._reset_for_tests()
        self.run_eval([item(96)], now=NOW + 99999)
        self.assertEqual(len(self.sent), 1)

    def test_series_are_independent_but_share_one_digest(self):
        self.run_eval([item(10), item(10, label="5h")])
        self.run_eval([item(90), item(90, label="5h")], now=NOW + 60)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(body(self.sent[0])), 2)


class ForecastGates(Base):
    def check(self, f, it=None, now=NOW + 60):
        it = it or item(60)
        self.run_eval([dict(it, used=10.0)], now=NOW)      # seed without a forecast
        self.run_eval([it], [f], now=now)
        return self.sent

    def test_needs_an_hour_of_history(self):
        self.assertEqual(self.check(fc(True, span=1800)), [])

    def test_needs_twenty_percent_of_the_window_elapsed(self):
        # 7d window starting 1 day before NOW → 14 % elapsed
        reset = NOW + 6 * 86400
        f = fc(True, reset_at=reset)
        self.assertEqual(self.check(f, item(60, reset_at=reset)), [])

    def test_exhaustion_beyond_24h_is_not_actionable(self):
        self.assertEqual(self.check(fc(True, exhaust_at=NOW + 25 * 3600)), [])

    def test_needs_half_an_hour_lead_before_reset(self):
        reset = NOW + 3 * 3600
        f = fc(True, exhaust_at=reset - 1200, reset_at=reset)
        self.assertEqual(self.check(f, item(60, reset_at=reset)), [])

    def test_five_hour_window_close_to_reset_is_skipped(self):
        reset = NOW + 50 * 60                               # 5h window, 4h10 elapsed, reset in 50 min
        f = fc(True, exhaust_at=NOW + 10 * 60, reset_at=reset, label="5h")
        self.assertEqual(self.check(f, item(60, reset_at=reset, label="5h")), [])

    def test_five_hour_window_passing_all_gates(self):
        reset = NOW + 3 * 3600                              # 2 h elapsed (40 %)
        f = fc(True, exhaust_at=NOW + 3600, reset_at=reset, label="5h")
        self.assertEqual(len(self.check(f, item(60, reset_at=reset, label="5h"))), 1)

    def test_thresholds_are_configurable(self):
        s = dict(CFG, forecast_max_horizon_seconds=30 * 3600)
        self.run_eval([item(10)], cfg=s)
        self.run_eval([item(60)], [fc(True, exhaust_at=NOW + 25 * 3600)], now=NOW + 60, cfg=s)
        self.assertEqual(len(self.sent), 1)


DIGEST = dict(alerts.DEFAULTS, timezone="Europe/Prague", quiet_hours=["22:00", "07:00"])


class Digest(Base):
    def test_grouping_order_and_link(self):
        rows = [{"kind": k, "text": t} for k, t in [
            ("forecast", "A 5h: ~13:00"), ("model", "LANA: X → Y"), ("exhausted", "B 7d"),
            ("threshold", "C week: 81 %"), ("back", "D 5h (reset proběhl)")]]
        msg = alerts.render(rows, NOW, DIGEST)
        self.assertEqual(msg.split("\n"), [
            "📊 AgentsMonitoring · 12:00", "🛑 Vyčerpáno", "• B 7d", "⚠️ Prahy", "• C week: 81 %",
            "✅ Kapacita zpět", "• D 5h (reset proběhl)", "📈 Hrozí vyčerpání", "• A 5h: ~13:00",
            "🔀 Změna modelu", "• LANA: X → Y", "Dashboard: http://192.168.1.153:8765"])

    def test_truncation(self):
        rows = [{"kind": "forecast", "text": f"S{i}"} for i in range(30)]
        lines = alerts.render(rows, NOW, DIGEST).split("\n")
        self.assertEqual(len([ln for ln in lines if ln.startswith("• ")]), 14)   # 15 lines incl. section title
        self.assertEqual(lines[-2], "… a dalších 16")
        self.assertEqual(lines[-1], "Dashboard: http://192.168.1.153:8765")

    def seed(self, n=3):
        self.items = [item(10, label=f"s{i}") for i in range(n)]
        self.run_eval(self.items, cfg=DIGEST)

    def test_non_critical_items_are_coalesced(self):
        self.seed()
        self.run_eval([item(81, label="s0"), item(10, label="s1"), item(10, label="s2")], now=NOW + 60, cfg=DIGEST)
        self.run_eval([item(81, label="s0"), item(85, label="s1"), item(10, label="s2")], now=NOW + 300, cfg=DIGEST)
        self.assertEqual(self.sent, [])                     # window of 600 s not over yet
        self.run_eval([item(81, label="s0"), item(85, label="s1"), item(10, label="s2")], now=NOW + 660, cfg=DIGEST)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(body(self.sent[0]), ["Codex · agentops s0: 81 %", "Codex · agentops s1: 85 %"])
        # a new non-critical item right after a flush waits for the next window
        self.run_eval([item(81, label="s0"), item(85, label="s1"), item(82, label="s2")], now=NOW + 700, cfg=DIGEST)
        self.run_eval([item(81, label="s0"), item(85, label="s1"), item(82, label="s2")], now=NOW + 1200, cfg=DIGEST)
        self.assertEqual(len(self.sent), 1)
        self.run_eval([item(81, label="s0"), item(85, label="s1"), item(82, label="s2")], now=NOW + 1300, cfg=DIGEST)
        self.assertEqual(len(self.sent), 2)

    def test_critical_flushes_immediately_with_buffer(self):
        self.seed()
        self.run_eval([item(81, label="s0"), item(10, label="s1"), item(10, label="s2")], now=NOW + 60, cfg=DIGEST)
        self.assertEqual(self.sent, [])
        self.run_eval([item(81, label="s0"), item(96, label="s1"), item(10, label="s2")], now=NOW + 120, cfg=DIGEST)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(body(self.sent[0]), ["Codex · agentops s0: 81 %", "Codex · agentops s1: 96 %"])
        self.run_eval([item(81, label="s0"), item(96, label="s1"), item(100, label="s2")], now=NOW + 180, cfg=DIGEST)
        self.assertEqual(len(self.sent), 2)                 # exhausted: immediate even right after a flush
        self.assertIn("🛑 Vyčerpáno", self.sent[1])

    def test_quiet_hours_hold_all_but_exhausted_and_flush_at_end(self):
        night = int(datetime(2026, 10, 31, 23, 0, tzinfo=TZ).timestamp())
        self.items = [item(10, label=f"s{i}") for i in range(3)]
        self.run_eval(self.items, now=night, cfg=DIGEST)
        self.run_eval([item(96, label="s0"), item(10, label="s1"), item(10, label="s2")], now=night + 60, cfg=DIGEST)
        self.run_eval([item(96, label="s0"), item(10, label="s1"), item(10, label="s2")], now=night + 4000, cfg=DIGEST)
        self.assertEqual(self.sent, [])                     # >= 95 % is held during quiet hours
        self.run_eval([item(96, label="s0"), item(100, label="s1"), item(10, label="s2")], now=night + 4060, cfg=DIGEST)
        self.assertEqual(len(self.sent), 1)                 # 100 % goes out, together with the buffer
        self.assertEqual(len(body(self.sent[0])), 2)
        self.run_eval([item(96, label="s0"), item(100, label="s1"), item(85, label="s2")], now=night + 5000, cfg=DIGEST)
        morning = int(datetime(2026, 11, 1, 6, 59, tzinfo=TZ).timestamp())
        self.run_eval([item(96, label="s0"), item(100, label="s1"), item(85, label="s2")], now=morning, cfg=DIGEST)
        self.assertEqual(len(self.sent), 1)
        self.run_eval([item(96, label="s0"), item(100, label="s1"), item(85, label="s2")], now=morning + 61, cfg=DIGEST)
        self.assertEqual(len(self.sent), 2)                 # flushed as soon as quiet hours end
        self.assertEqual(body(self.sent[1]), ["Codex · agentops s2: 85 %"])

    def test_buffer_survives_restart_without_duplicates(self):
        self.seed()
        self.run_eval([item(81, label="s0"), item(10, label="s1"), item(10, label="s2")], now=NOW + 60, cfg=DIGEST)
        quota.reset_cache(); alerts._reset_for_tests()      # fresh module state, same DB
        self.run_eval([item(81, label="s0"), item(10, label="s1"), item(10, label="s2")], now=NOW + 700, cfg=DIGEST)
        quota.reset_cache(); alerts._reset_for_tests()
        self.run_eval([item(81, label="s0"), item(10, label="s1"), item(10, label="s2")], now=NOW + 2000, cfg=DIGEST)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(body(self.sent[0]), ["Codex · agentops s0: 81 %"])


def agent(model, name="LANA"):
    return {"id": name, "name": name, "kind": "codex", "label": model}


class ModelChange(Base):
    def test_seeded_silently_then_debounced(self):
        self.run_eval([], agents=[agent("GPT-6.1 Sol"), agent("Opus 5.5", "Claude")])
        self.run_eval([], agents=[agent("Gemini 3.8 Flash"), agent("Opus 5.5", "Claude")], now=NOW + 60)
        self.assertEqual(self.sent, [])                     # 1 tick only → could be flapping
        self.run_eval([], agents=[agent("Gemini 3.8 Flash"), agent("Opus 5.5", "Claude")], now=NOW + 120)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("🔀 Změna modelu\n• LANA: GPT-6.1 Sol → Gemini 3.8 Flash", self.sent[0])
        self.run_eval([], agents=[agent("Gemini 3.8 Flash")], now=NOW + 180)
        self.assertEqual(len(self.sent), 1)

    def test_flapping_is_ignored(self):
        self.run_eval([], agents=[agent("A")])
        for i, m in enumerate(["B", "A", "B", "A", "C", "A"], 1):
            self.run_eval([], agents=[agent(m)], now=NOW + 60 * i)
        self.assertEqual(self.sent, [])

    def test_unknown_labels_are_ignored(self):
        self.run_eval([], agents=[agent("")])               # unknown first → nothing stored
        self.run_eval([], agents=[agent("A")], now=NOW + 60)
        self.run_eval([], agents=[agent("?")], now=NOW + 120)
        self.run_eval([], agents=[agent("—")], now=NOW + 180)
        self.run_eval([], agents=[agent("A")], now=NOW + 240)
        self.assertEqual(self.sent, [])

    def test_disabled_tracks_silently(self):
        cfg = dict(CFG, notify_on_model_change=False)
        for i, m in enumerate(["A", "B", "B"]):
            self.run_eval([], agents=[agent(m)], now=NOW + 60 * i, cfg=cfg)
        self.assertEqual(self.sent, [])
        self.run_eval([], agents=[agent("B")], now=NOW + 300)   # re-enabled: no stale burst
        self.assertEqual(self.sent, [])

    def test_restart_keeps_model_state(self):
        self.run_eval([], agents=[agent("A")])
        self.run_eval([], agents=[agent("B")], now=NOW + 60)
        quota.reset_cache(); alerts._reset_for_tests()
        self.run_eval([], agents=[agent("B")], now=NOW + 120)
        quota.reset_cache(); alerts._reset_for_tests()
        self.run_eval([], agents=[agent("B")], now=NOW + 180)
        self.assertEqual(len(self.sent), 1)


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
        self.assertIn("Codex · agentops 7d: 99 %", d.call_args[0][0])
        self.assertTrue(d.call_args[0][0].startswith("📊 AgentsMonitoring · "))

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
