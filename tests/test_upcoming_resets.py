import unittest

from agentsmon import dashboard, plugins


def bar(label, used, ts, state="OK"):
    return {"label": label, "percent": used, "remaining": round(100 - used, 1),
            "reset_label": "x", "reset_at": ts, "state": state}


class ResetAtSchema(unittest.TestCase):
    def test_reset_at_accepted_as_int_epoch(self):
        c = plugins.cell({"text": "OK", "bars": [bar("5h", 10, 1_900_000_000.7)]})
        self.assertEqual(c["bars"][0]["reset_at"], 1_900_000_000)

    def test_reset_at_rejects_garbage(self):
        for bad in ("zítra", -5, 12, 10**12):
            with self.assertRaises(ValueError):
                plugins.cell({"text": "OK", "bars": [bar("5h", 10, bad)]})

    def test_bar_without_reset_at_unchanged(self):
        c = plugins.cell({"text": "OK", "bars": [{"label": "5h", "percent": 1}]})
        self.assertNotIn("reset_at", c["bars"][0])


class UpcomingResets(unittest.TestCase):
    NOW = 1_800_000_000

    def test_sorted_deduped_and_exhausted(self):
        tables = [{"rows": [
            {"name": "Codex", "cells": {"five_hour": {"bars": [bar("5h", 0, self.NOW + 3600)]},
                                        "weekly": {"bars": [bar("7d", 100, self.NOW + 86400)]}}},
            {"name": "Claude · Max 5x", "cells": {"weekly": {"bars": [
                bar("Claude week", 6, self.NOW + 7200), bar("Fable week", 6, self.NOW + 7200)]}}},
            {"name": "Old", "cells": {"weekly": {"bars": [bar("past", 5, self.NOW - 1)]}}},
        ]}]
        # Agent rows repeat the provider windows as compact gauges → must not duplicate.
        agents = [{"name": "LANA", "cells": {"codex:five_hour": {"bars": [bar("5h", 0, self.NOW + 3600)]}}},
                  {"name": "Solo", "cells": {"x": {"bars": [bar("5h", 40, self.NOW + 60)]}}}]
        out = dashboard.upcoming_resets(tables, agents, now=self.NOW)
        self.assertEqual([(e["source"], e["label"]) for e in out], [
            ("Solo", "5h"), ("Codex", "5h"), ("Claude · Max 5x", "Claude week"),
            ("Claude · Max 5x", "Fable week"), ("Codex", "7d")])
        self.assertEqual([e["exhausted"] for e in out], [False, False, False, False, True])

    def test_bars_without_machine_reset_are_ignored(self):
        tables = [{"rows": [{"name": "GH", "cells": {"w": {"bars": [{"label": "min", "percent": 100}]}}}]}]
        self.assertEqual(dashboard.upcoming_resets(tables, [], now=self.NOW), [])


if __name__ == "__main__":
    unittest.main()
