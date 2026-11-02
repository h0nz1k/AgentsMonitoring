"""Redesign 003: Czech local times/labels, forecast attention items, single count, sub-limits."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from agentsmon import dashboard

TZ = ZoneInfo("Europe/Prague")
NOW = int(datetime(2026, 11, 2, 9, 0, tzinfo=TZ).timestamp())     # Monday


def ts(*a):
    return int(datetime(*a, tzinfo=TZ).timestamp())


class CzFormat(unittest.TestCase):
    def test_when_today_tomorrow_and_weekday(self):
        self.assertEqual(dashboard.cz_when(ts(2026, 11, 2, 10, 45), NOW), "dnes 10:45")
        self.assertEqual(dashboard.cz_when(ts(2026, 11, 3, 7, 2), NOW), "zítra 07:02")
        self.assertEqual(dashboard.cz_when(ts(2026, 11, 6, 16, 11), NOW), "pá 6. 11. 16:11")
        self.assertEqual(dashboard.cz_when(ts(2027, 1, 28, 0, 0), NOW), "čt 28. 1. 2027 00:00")
        self.assertEqual(dashboard.cz_when(None, NOW), "")

    def test_labels_have_no_english_week(self):
        self.assertEqual(dashboard.cz_label("Claude week"), "Claude týden")
        self.assertEqual(dashboard.cz_label("7d"), "týden")
        self.assertEqual(dashboard.cz_label("Included"), "V předplatném")
        self.assertEqual(dashboard.cz_label("Claude/GPT 5h"), "Claude/GPT 5 h")

    def test_one_percent_format(self):
        self.assertEqual(dashboard._cz_pct(97), "97\u00a0%")
        self.assertEqual(dashboard._cz_pct(6.25), "6,2\u00a0%")


def quota_table(*bars, name="Codex · agentops"):
    return [{"id": "p", "kind": "quota", "rows": [{"id": "r", "name": name, "anchor": "kvota-r",
                                                   "cells": {"w": {"bars": list(bars)}}}]}]


class ForecastAttention(unittest.TestCase):
    def fc(self, **kw):
        f = {"source": "Codex · agentops", "label": "5h", "used_pct": 47.0, "reset_at": ts(2026, 11, 2, 10, 45),
             "exhaust_at": ts(2026, 11, 2, 10, 14), "will_exhaust_before_reset": True, "exhausted": False}
        f.update(kw)
        return f

    def test_will_exhaust_before_reset_is_an_attention_item(self):
        data = {"agent_tables": quota_table({"label": "5h", "percent": 47.0}), "forecasts": [self.fc()]}
        att, ov = dashboard.compute_attention(data, now=NOW)
        self.assertEqual(att[0]["text_cs"], "Codex · agentops 5 h dojde ~10:14, obnova 10:45")
        self.assertEqual((att[0]["severity"], att[0]["kind"], att[0]["href"]), ("warning", "forecast", "#kvota-r"))
        self.assertEqual((ov["level"], ov["count"]), ("warning", 1))

    def test_safe_or_far_or_exhausted_forecasts_are_ignored(self):
        fcs = [self.fc(will_exhaust_before_reset=False), self.fc(exhausted=True),
               self.fc(exhaust_at=NOW + 8 * 86400, reset_at=NOW + 9 * 86400), self.fc(exhaust_at=NOW - 60)]
        att, ov = dashboard.compute_attention({"forecasts": fcs}, now=NOW)
        self.assertEqual(att, [])
        self.assertEqual(ov["text_cs"], "Vše v provozu")

    def test_sub_limit_forecast_is_not_an_item(self):
        bar = {"label": "Fable week", "percent": 6.0, "cap": 50.0, "parent": "Claude week"}
        data = {"agent_tables": quota_table(bar, name="Claude"),
                "forecasts": [self.fc(source="Claude", label="Fable week")]}
        self.assertEqual(dashboard.compute_attention(data, now=NOW)[0], [])


class SubLimitAttention(unittest.TestCase):
    def test_cap_reached_is_one_warning_and_not_exhausted(self):
        bar = {"label": "Fable week", "percent": 52.0, "cap": 50.0, "parent": "Claude week"}
        att, ov = dashboard.compute_attention({"agent_tables": quota_table(bar, name="Claude")}, now=NOW)
        self.assertEqual([a["text_cs"] for a in att], ["Claude: Fable týden dosáhl stropu 50\u00a0%"])
        self.assertEqual(ov["count"], 1)

    def test_below_cap_is_silent(self):
        bar = {"label": "Fable week", "percent": 6.0, "cap": 50.0, "parent": "Claude week"}
        self.assertEqual(dashboard.compute_attention({"agent_tables": quota_table(bar)}, now=NOW)[0], [])


class SingleCount(unittest.TestCase):
    def test_count_and_summary_sentence(self):
        agents = [{"name": "A", "alive": False}, {"name": "B", "alive": True, "waiting": True},
                  {"name": "S", "alive": False, "status": "Stopped (manual)"}]
        att, ov = dashboard.compute_attention({"agents": agents}, now=NOW)
        self.assertEqual((ov["critical"], ov["warning"], ov["info"]), (1, 1, 1))
        self.assertEqual(ov["count_cs"], "2 + 1 info")
        self.assertEqual(ov["text_cs"], "Kritický stav (2 + 1 info)")
        self.assertTrue(ov["summary_cs"].startswith("1 kritický problém a 1 věc vyžaduje pozornost"))

    def test_info_only_is_ok(self):
        att, ov = dashboard.compute_attention({"agents": [{"name": "S", "alive": False,
                                                            "status": "Stopped (manual)"}]}, now=NOW)
        self.assertEqual((ov["level"], ov["text_cs"]), ("ok", "Vše v provozu (1 info)"))


if __name__ == "__main__":
    unittest.main()
