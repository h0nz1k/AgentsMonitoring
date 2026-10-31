"""P1 UX revision: keyboard-accessible sections, honest stale data, quota dedupe, details."""
from pathlib import Path
from contextlib import ExitStack
import tempfile
import unittest
from unittest.mock import patch

from agentsmon import config, dashboard, db


class SectionHeadersKeyboard(unittest.TestCase):
    def test_every_collapsible_head_is_a_focusable_button(self):
        page = dashboard.PAGE
        heads = page.count('class="svc-head')
        self.assertGreaterEqual(heads, 2)
        self.assertEqual(heads, page.count('role="button" tabindex="0" aria-expanded="true"'))
        self.assertIn('head.setAttribute("aria-expanded"', page)
        self.assertIn(".svc-head:focus-visible", page)


class StaleServiceData(unittest.TestCase):
    NOW = 1_800_000_000

    def setUp(self):
        stack = ExitStack(); self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.object(config, "state_dir", return_value=Path(tmp)))
        stack.enter_context(patch.object(db.time, "time", return_value=self.NOW))
        self.cfg = {"services": [{"name": "Bridge"}], "probe": {"interval_seconds": 60}}

    def test_fresh_green_sample_stays_operational(self):
        db.record("Bridge", True, ts=self.NOW - 120)
        card = dashboard._service_state(self.cfg)[0]
        self.assertEqual(card["state"], "operational")
        self.assertFalse(card["stale"])
        self.assertEqual(card["data_age"], 120)

    def test_sample_older_than_three_intervals_is_stale_not_green(self):
        db.record("Bridge", True, ts=self.NOW - 6 * 86400)
        card = dashboard._service_state(self.cfg)[0]
        self.assertEqual(card["state"], "stale")
        self.assertTrue(card["stale"])
        self.assertEqual(card["data_age"], 6 * 86400)

    def test_per_service_interval_wins(self):
        self.cfg["services"][0]["interval_seconds"] = 3600
        db.record("Bridge", False, ts=self.NOW - 600)
        self.assertEqual(dashboard._service_state(self.cfg)[0]["state"], "outage")

    def test_no_sample_is_nodata(self):
        card = dashboard._service_state(self.cfg)[0]
        self.assertEqual((card["state"], card["stale"], card["data_age"]), ("nodata", False, None))

    def test_page_labels_stale_amber(self):
        self.assertIn('stale:       ["bg-amber-400", "Zastaralá data"', dashboard.PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
