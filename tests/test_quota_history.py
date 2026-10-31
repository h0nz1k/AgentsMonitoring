"""Quota history (SQLite samples) + burn-rate forecast."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import config, quota

NOW = 1_800_000_000


def bar(label, used, reset_at=None, state="OK"):
    b = {"label": label, "percent": used, "remaining": round(100 - used, 1), "state": state}
    if reset_at is not None:
        b["reset_at"] = reset_at
    return b


def table(*rows):
    return [{"id": "t", "rows": [{"name": n, "cells": {"w": {"bars": bars}}} for n, bars in rows]}]


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = config.state_dir
        config.state_dir = lambda: Path(self.tmp.name)
        quota.reset_cache()
        quota.start()

    def tearDown(self):
        config.state_dir = self._orig
        quota.reset_cache()
        quota._active = False
        self.tmp.cleanup()

    def rows(self):
        c = sqlite3.connect(str(Path(self.tmp.name) / "quota.sqlite"))
        try:
            return c.execute("SELECT ts,source,label,used_pct,reset_at FROM quota_samples ORDER BY ts").fetchall()
        finally:
            c.close()


class SeriesExtraction(unittest.TestCase):
    def test_tables_are_primary_and_agent_duplicates_skipped(self):
        tables = table(("Codex · agentops", [bar("5h", 10, NOW + 3600), bar("7d", 100, NOW + 86400)]))
        agents = [{"name": "LANA", "cells": {"x": {"bars": [bar("5h", 10, NOW + 3600)]}}},
                  {"name": "Solo", "cells": {"x": {"bars": [bar("5h", 40, NOW + 60)]}}}]
        got = [(s["source"], s["label"], s["used"]) for s in quota.series(tables, agents)]
        self.assertEqual(got, [("Codex · agentops", "5h", 10.0), ("Codex · agentops", "7d", 100.0),
                               ("Solo", "5h", 40.0)])

    def test_unavailable_or_non_numeric_bars_ignored(self):
        tables = table(("A", [bar("x", 5, state="UNAVAILABLE"), {"label": "y", "percent": None},
                              {"percent": 3}, bar("ok", 1)]))
        self.assertEqual([s["label"] for s in quota.series(tables, [])], ["ok"])


class History(TempState):
    def test_throttled_to_five_minutes_unless_value_changes(self):
        s = [{"source": "A", "label": "5h", "used": 10.0, "reset_at": NOW + 3600}]
        quota.record(s, now=NOW)
        quota.record(s, now=NOW + 15)            # same value, too soon → skipped
        quota.record(s, now=NOW + 299)
        self.assertEqual(len(self.rows()), 1)
        quota.record([dict(s[0], used=11.0)], now=NOW + 300 + 30)   # changed → written
        quota.record([dict(s[0], used=11.0)], now=NOW + 330 + 300)  # 5 min elapsed → written
        self.assertEqual([r[0] for r in self.rows()], [NOW, NOW + 330, NOW + 630])

    def test_throttle_survives_restart_via_db(self):
        s = [{"source": "A", "label": "5h", "used": 10.0, "reset_at": None}]
        quota.record(s, now=NOW)
        quota.reset_cache()                       # simulated process restart
        quota.record(s, now=NOW + 60)
        self.assertEqual(len(self.rows()), 1)

    def test_prunes_samples_older_than_30_days(self):
        s = [{"source": "A", "label": "5h", "used": 10.0, "reset_at": None}]
        quota.record(s, now=NOW - 31 * 86400)
        quota.record([dict(s[0], used=20.0)], now=NOW)
        self.assertEqual([r[3] for r in self.rows()], [20.0])

    def test_inert_until_dashboard_starts_it(self):
        quota._active = False
        self.assertEqual(quota.observe(table(("A", [bar("5h", 1, NOW + 60)])), [], now=NOW), [])
        self.assertFalse((Path(self.tmp.name) / "quota.sqlite").exists())

    def test_db_failure_never_raises(self):
        with patch.object(quota, "_conn", side_effect=sqlite3.OperationalError("disk")):
            self.assertEqual(quota.observe(table(("A", [bar("5h", 1, NOW + 60)])), [], now=NOW), [])

    def test_no_fd_leak(self):
        s = [{"source": "A", "label": "5h", "used": 1.0, "reset_at": None}]
        quota.record(s, now=NOW)
        before = len(os.listdir("/dev/fd"))
        for i in range(100):
            quota.record([dict(s[0], used=float(i))], now=NOW + i)
            quota.samples("A", "5h", NOW - 10)
        self.assertLessEqual(len(os.listdir("/dev/fd")) - before, 2)


class Forecast(unittest.TestCase):
    def test_linear_burn_projects_exhaustion_before_reset(self):
        reset = NOW + 10 * 3600
        pts = [(NOW - 3600, 40.0, reset), (NOW - 1800, 45.0, reset), (NOW, 50.0, reset)]
        f = quota.forecast(pts, used=50.0, reset_at=reset, now=NOW)
        self.assertAlmostEqual(f["rate_pct_per_hour"], 10.0, places=3)
        self.assertEqual(f["exhaust_at"], NOW + 5 * 3600)
        self.assertTrue(f["will_exhaust_before_reset"])
        self.assertEqual(f["projected_at_reset"], 100.0)

    def test_slow_burn_reports_projected_percent_at_reset(self):
        reset = NOW + 2 * 3600
        pts = [(NOW - 3600, 40.0, reset), (NOW, 50.0, reset)]
        f = quota.forecast(pts, used=50.0, reset_at=reset, now=NOW)
        self.assertFalse(f["will_exhaust_before_reset"])
        self.assertAlmostEqual(f["projected_at_reset"], 70.0, places=3)

    def test_only_current_window_is_used(self):
        reset_old, reset = NOW - 100, NOW + 3600
        pts = [(NOW - 7200, 90.0, reset_old), (NOW - 3600, 0.0, reset), (NOW - 1800, 5.0, reset),
               (NOW, 10.0, reset)]
        f = quota.forecast(pts, used=10.0, reset_at=reset, now=NOW)
        self.assertAlmostEqual(f["rate_pct_per_hour"], 10.0, places=3)

    def test_monotonic_segment_when_no_reset_at(self):
        pts = [(NOW - 7200, 80.0, None), (NOW - 3600, 0.0, None), (NOW, 20.0, None)]
        f = quota.forecast(pts, used=20.0, reset_at=None, now=NOW)
        self.assertAlmostEqual(f["rate_pct_per_hour"], 20.0, places=3)
        self.assertFalse(f["will_exhaust_before_reset"])
        self.assertIsNone(f["projected_at_reset"])
        self.assertEqual(f["exhaust_at"], NOW + 4 * 3600)

    def test_requires_two_samples_and_ten_minutes(self):
        r = NOW + 3600
        self.assertIsNone(quota.forecast([(NOW, 5.0, r)], used=5.0, reset_at=r, now=NOW))
        self.assertIsNone(quota.forecast([(NOW - 300, 1.0, r), (NOW, 5.0, r)], used=5.0, reset_at=r, now=NOW))

    def test_flat_usage_never_exhausts(self):
        r = NOW + 3600
        f = quota.forecast([(NOW - 3600, 5.0, r), (NOW, 5.0, r)], used=5.0, reset_at=r, now=NOW)
        self.assertIsNone(f["exhaust_at"])
        self.assertFalse(f["will_exhaust_before_reset"])
        self.assertEqual(f["projected_at_reset"], 5.0)

    def test_already_exhausted_has_no_projection(self):
        r = NOW + 3600
        f = quota.forecast([(NOW - 3600, 90.0, r), (NOW, 100.0, r)], used=100.0, reset_at=r, now=NOW)
        self.assertTrue(f["exhausted"])
        self.assertFalse(f["will_exhaust_before_reset"])
        self.assertIsNone(f["exhaust_at"])


class ObserveAndAnnotate(TempState):
    def test_observe_records_and_returns_forecasts(self):
        reset = NOW + 10 * 3600
        for i, used in enumerate((40.0, 45.0, 50.0)):
            out = quota.observe(table(("Codex", [bar("7d", used, reset)])), [], now=NOW + i * 1800)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(out[0]["source"], "Codex")
        self.assertTrue(out[0]["will_exhaust_before_reset"])

    def test_annotate_sets_bar_and_reset_forecast(self):
        reset = NOW + 3600
        tables = table(("Codex", [bar("7d", 50, reset)]))
        agents = [{"name": "LANA", "cells": {"x": {"bars": [bar("7d", 50, reset)]}}}]
        resets = [{"source": "Codex", "label": "7d", "reset_at": reset}]
        fc = [{"source": "Codex", "label": "7d", "used_pct": 50.0, "reset_at": reset, "exhaust_at": NOW + 60,
               "projected_at_reset": 100.0, "will_exhaust_before_reset": True, "rate_pct_per_hour": 1.0}]
        quota.annotate(tables, agents, resets, fc)
        self.assertTrue(tables[0]["rows"][0]["cells"]["w"]["bars"][0]["forecast"]["will_exhaust_before_reset"])
        self.assertEqual(agents[0]["cells"]["x"]["bars"][0]["forecast"]["exhaust_at"], NOW + 60)
        self.assertEqual(resets[0]["forecast"]["exhaust_at"], NOW + 60)
        quota.annotate(tables, agents, resets, [])          # stale forecast removed
        self.assertNotIn("forecast", tables[0]["rows"][0]["cells"]["w"]["bars"][0])



class ForecastUi(unittest.TestCase):
    def test_page_renders_czech_forecast_in_chip_and_tooltip(self):
        from agentsmon import dashboard
        page = dashboard.PAGE
        self.assertIn("function fcText(f)", page)
        self.assertIn("tempem vyčerpáno ~", page)
        self.assertIn("(před resetem)", page)
        self.assertIn("při resetu ~", page)
        self.assertIn('class="reset-fc', page)           # chip line in 'Nejbližší resety kvót'
        self.assertIn("fcText(b.forecast)", page)         # bar tooltip


if __name__ == "__main__":
    unittest.main()
