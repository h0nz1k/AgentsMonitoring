"""Work-queue wiring + server-side "Vyžaduje pozornost" / overall status."""
import unittest
from unittest.mock import patch

from agentsmon import dashboard, workqueue

NOW = 1_800_000_000


def _queue(**summary):
    base = {"running": 0, "running_slots_max": 2, "queued": 0, "awaiting_pickup": 0, "blocked": 0,
            "failed": 0, "done_24h": 0, "open_issues": 0, "open_prs": 0, "untracked": 0, "mismatches": 0}
    base.update(summary)
    return {"enabled": True, "items": [], "summary": base,
            "meta": {"stale": False, "source_errors": [], "repo_url": "https://github.com/o/r"}}


def _item(num, state, tracked=True, pr=None, mismatch=None):
    return {"kind": "issue", "issue": num, "title": f"t{num}", "url": f"https://github.com/o/r/issues/{num}",
            "state": state, "tracked": tracked, "pr": pr, "task": None, "mismatch": mismatch}


class AttentionTest(unittest.TestCase):
    def test_all_clear_is_ok(self):
        att, ov = dashboard.compute_attention({"agents": [{"name": "A", "alive": True}],
                                               "services": [{"name": "S", "state": "operational"}]}, now=NOW)
        self.assertEqual(att, [])
        self.assertEqual(ov["level"], "ok")
        self.assertEqual(ov["text_cs"], "Vše v provozu")
        self.assertEqual(ov["count"], 0)

    def test_services_outage_and_stale(self):
        att, ov = dashboard.compute_attention({"services": [
            {"name": "Telegram Bridge", "state": "stale", "data_age": 6 * 86400, "anchor": "sluzba-telegram-bridge"},
            {"name": "X", "state": "outage", "anchor": "sluzba-x"}]}, now=NOW)
        self.assertEqual([a["severity"] for a in att], ["critical", "warning"])
        self.assertEqual(att[0]["text_cs"], "X: výpadek")
        self.assertIn("Telegram most: Zastaralá data", att[1]["text_cs"])
        self.assertEqual(att[1]["href"], "#sluzba-telegram-bridge")
        self.assertEqual(ov["level"], "critical")

    def test_agents_dead_stopped_login(self):
        att, ov = dashboard.compute_attention({"agents": [
            {"name": "Dead", "alive": False}, {"name": "Off", "alive": False, "status": "Zastaveno (ručně)"},
            {"name": "Login", "alive": True, "needs_login": True}]}, now=NOW)
        sev = {a["text_cs"]: a["severity"] for a in att}
        self.assertEqual(sev["Agent Dead: neběží"], "critical")
        self.assertEqual(sev["Agent Off: zastaven ručně"], "info")
        self.assertEqual(sev["Agent Login: čeká na přihlášení"], "warning")
        self.assertTrue(all(a["href"] == "#agenti" for a in att))

    def test_exhausted_quota_with_existing_redeem_action(self):
        act = {"id": "redeem_reset", "label": "Uplatnit reset (1×)", "icon": "reset", "confirm": "Opravdu?"}
        tables = [{"id": "codex-quota:providers", "kind": "quota", "rows": [
            {"id": "agentops", "name": "Codex · agentops", "anchor": "kvota-agentops", "row_actions": [act],
             "cells": {"w": {"bars": [{"label": "7d", "percent": 100.0, "remaining": 0.0, "state": "OK",
                                       "reset_label": "04.11 07:02", "reset_at": NOW + 3600}]},
                       "h": {"bars": [{"label": "5h", "percent": 10.0, "remaining": 90.0}]}}},
            {"id": "github", "name": "GitHub Actions", "anchor": "kvota-github",
             "cells": {"m": {"bars": [{"label": "Actions min", "percent": 100.0, "state": "EXHAUSTED"}]}}},
            {"id": "ok", "name": "OK", "cells": {"m": {"bars": [{"label": "x", "percent": 5.0}]}}}]}]
        att, ov = dashboard.compute_attention({"agent_tables": tables}, now=NOW)
        self.assertEqual(len(att), 2)
        codex = att[0]
        self.assertEqual(codex["text_cs"], "Codex · agentops: vyčerpáno 7d – reset 04.11 07:02")
        self.assertEqual(codex["href"], "#kvota-agentops")
        self.assertEqual(codex["action"]["id"], "redeem_reset")
        self.assertEqual(codex["action"]["table"], "codex-quota:providers")
        self.assertEqual(codex["action"]["row"], "agentops")
        self.assertEqual(codex["reset_at"], NOW + 3600)
        self.assertIsNone(att[1]["action"])
        self.assertEqual(ov["level"], "warning")
        self.assertEqual(ov["text_cs"], "Vyžaduje pozornost (2)")

    def test_queue_counts_ci_and_mismatches(self):
        q = _queue(failed=1, blocked=2, awaiting_pickup=3, mismatches=1)
        q["items"] = [_item(7, "failed"), _item(30, "blocked"), _item(18, "blocked"),
                      _item(5, "queued", pr={"number": 9, "url": "https://github.com/o/r/pull/9",
                                             "ci": "failure", "title": "fix"}),
                      _item(6, "queued", tracked=False, mismatch="nesoulad")]
        att, ov = dashboard.compute_attention({"queue": q}, now=NOW)
        texts = [a["text_cs"] for a in att]
        self.assertEqual(att[0]["severity"], "critical")
        self.assertEqual(texts[0], "Fronta: 1 úloha selhala (#7)")
        self.assertEqual(att[0]["href"], "https://github.com/o/r/issues/7")
        self.assertIn("Fronta: 2 úlohy blokovány (#30, #18)", texts)
        self.assertIn("PR #9: CI selhalo – fix", texts)
        self.assertIn("Fronta: 3 položky čekají na převzetí", texts)
        self.assertIn("Fronta: 1 nesoulad GitHub × supervisor", texts)
        self.assertEqual(ov["count"], 3)   # info items do not count into the tab title

    def test_queue_disabled_or_loading_adds_nothing(self):
        for q in ({"enabled": False}, dict(_queue(failed=3), loading=True)):
            att, ov = dashboard.compute_attention({"queue": q}, now=NOW)
            self.assertEqual(att, [])

    def test_queue_stale_source_errors(self):
        q = _queue()
        q["meta"].update(stale=True, source_errors=["GitHub issues: timeout"])
        att, _ = dashboard.compute_attention({"queue": q}, now=NOW)
        self.assertEqual(att[0]["text_cs"], "Fronta práce: Zastaralá data – GitHub issues: timeout")

    def test_sorted_and_capped(self):
        agents = [{"name": f"S{i}", "alive": False, "status": "Stopped (manual)"} for i in range(4)]
        agents += [{"name": f"D{i}", "alive": False} for i in range(4)]
        att, ov = dashboard.compute_attention({"agents": agents}, now=NOW)
        self.assertEqual(len(att), dashboard.ATTENTION_MAX)
        self.assertEqual([a["severity"] for a in att], ["critical"] * 4 + ["info"] * 2)
        self.assertEqual(ov["total"], 8)
        self.assertEqual(ov["text_cs"], "Kritický stav (4)")

    def test_never_raises(self):
        att, ov = dashboard.compute_attention({"agents": [None]}, now=NOW)
        self.assertEqual(att[0]["severity"], "warning")


class StateQueueTest(unittest.TestCase):
    def _state(self, cfg, queue=None):
        extra = {"tables": [], "patches": [], "hidden_sessions": [], "hidden_services": [], "columns": [],
                 "errors": [], "plugins": []}
        with patch.object(dashboard.config, "load", return_value=cfg), \
             patch.object(dashboard, "_agents_state", return_value=[]), \
             patch.object(dashboard.plugins, "collect", return_value=extra), \
             patch.object(dashboard, "_service_state", return_value=[{"name": "Telegram Bridge", "state": "stale"}]), \
             patch.object(dashboard.quota, "observe", return_value={}), \
             patch.object(dashboard.quota, "annotate"), patch.object(dashboard.alerts, "tick"), \
             patch("agentsmon.integrity.status", return_value={"version": "x", "state": "ok"}):
            if queue is None:
                return dashboard._state_data()
            with patch.object(dashboard.workqueue, "get", return_value=queue) as g:
                data = dashboard._state_data()
                g.assert_called_once_with(cfg)
                return data

    def test_queue_disabled(self):
        data = self._state({})
        self.assertEqual(data["queue"], {"enabled": False})
        self.assertEqual(data["services"][0]["anchor"], "sluzba-telegram-bridge")
        self.assertEqual(data["overall"]["level"], "warning")
        self.assertIn("Zastaralá data", data["attention"][0]["text_cs"])

    def test_queue_enabled(self):
        q = _queue(failed=1)
        data = self._state({"queue": {"enabled": True}}, queue=q)
        self.assertIs(data["queue"], q)
        self.assertEqual(data["overall"]["level"], "critical")
        self.assertTrue(data["attention"][0]["text_cs"].startswith("Fronta: 1 úloha selhala"))

    def test_workqueue_disabled_get_is_cheap(self):
        self.assertEqual(workqueue.get({"queue": {"enabled": False}}), {"enabled": False})


class PageTest(unittest.TestCase):
    def test_new_sections_present(self):
        page = dashboard.PAGE
        for needle in ('id="attention"', 'id="overall-pill"', 'id="attn-live"', 'aria-live="polite"',
                       'id="fronta"', 'id="agenti"', "Fronta práce", "Vyžaduje pozornost", 'id="q-panel"',
                       "GitHub ↗", "agentsmon.queue.tab", "noopener noreferrer", "Nezařazené", "Hotovo 24 h"):
            self.assertIn(needle, page)
        # Order: attention panel → agents → queue → quotas (plugin tables)
        self.assertLess(page.index('id="attention"'), page.index('id="agenti"'))
        self.assertLess(page.index('id="agenti"'), page.index('id="fronta"'))
        self.assertLess(page.index('id="fronta"'), page.index('id="plugin-tables"'))

    def test_queue_links_are_github_only(self):
        self.assertIn(r"/^https:\/\/github\.com\//", dashboard.PAGE)

    def test_serve_starts_workqueue(self):
        import inspect
        self.assertIn("workqueue.start(cfg)", inspect.getsource(dashboard.serve))


if __name__ == "__main__":
    unittest.main()
