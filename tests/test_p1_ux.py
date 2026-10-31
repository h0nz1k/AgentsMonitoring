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


def _bar(label, pct, reset, title=None):
    return {"label": label, "percent": pct, "remaining": 100 - pct, "reset_at": reset,
            "title": title or f"{label} {pct}"}


class QuotaAccountLinks(unittest.TestCase):
    R5, RW = 1_900_000_000, 1_900_300_000

    def fixture(self):
        codex = {"id": "agentops", "name": "Codex · agentops", "label": "Codex · Plus",
                 "cells": {"c:5h": {"bars": [_bar("5h", 0.0, self.R5)]},
                           "c:wk": {"bars": [_bar("7d", 100.0, self.RW)]}}}
        claude = {"id": "claude", "name": "Claude · Max 5x", "label": "Claude",
                  "cells": {"p:wk": {"bars": [_bar("Claude 5h", 14.0, self.R5 + 60),
                                              _bar("Claude week", 8.0, self.RW + 60)]}}}
        cursor = {"id": "cursor", "name": "Cursor · Pro", "label": "Cursor",
                  "cells": {"p:wk": {"bars": [_bar("Included", 73.6, self.RW + 999)]}}}
        tables = [{"id": "codex-quota:providers", "rows": [codex, claude, cursor]},
                  {"id": "other:plain", "rows": [{"id": "x", "cells": {"t": {"text": "OK"}}}]}]
        agents = [{"id": n, "name": n} for n in ("Hermes", "Codex", "LANA", "Claude", "Cursor", "LANA (Hermes)")]
        codex_cells = {"c:5h": codex["cells"]["c:5h"], "c:wk": codex["cells"]["c:wk"]}
        claude_cells = {"p:5h": {"bars": [_bar("5h", 14.0, self.R5 + 60)]},
                        "p:wk": {"bars": [_bar("Týden", 8.0, self.RW + 60)]}}
        patches = [{"row_id": n, "cells": codex_cells} for n in ("Hermes", "Codex", "LANA")]
        patches.append({"row_id": "Claude", "cells": claude_cells})
        return tables, agents, patches

    def test_patch_copies_link_agents_to_the_provider_row(self):
        tables, agents, patches = self.fixture()
        dashboard.link_quota_accounts(tables, agents, patches)
        by = {a["name"]: a.get("quota_account") for a in agents}
        self.assertEqual(by["Codex"]["row"], "agentops")
        self.assertEqual(by["Codex"]["anchor"], "kvota-agentops")
        self.assertEqual(by["Hermes"]["shared_with"], ["Hermes", "Codex", "LANA"])
        self.assertEqual((by["Claude"]["row"], by["Claude"]["via"]), ("claude", "patch"))
        self.assertEqual((by["Cursor"]["row"], by["Cursor"]["via"]), ("cursor", "name"))
        self.assertIsNone(by["LANA (Hermes)"])
        rows = {r["id"]: r for r in tables[0]["rows"]}
        self.assertEqual(rows["agentops"]["used_by"], ["Hermes", "Codex", "LANA"])
        self.assertEqual(rows["cursor"]["used_by"], ["Cursor"])

    def test_only_tables_with_bars_become_quota_tables(self):
        tables, agents, patches = self.fixture()
        dashboard.link_quota_accounts(tables, agents, patches)
        self.assertEqual(tables[0]["kind"], "quota")
        self.assertNotIn("kind", tables[1])

    def test_anchors_are_unique_and_safe(self):
        rows = [{"id": "Účet <x>", "cells": {"a": {"bars": [_bar("5h", 1, self.R5)]}}},
                {"id": "ucet-x", "cells": {"a": {"bars": [_bar("5h", 2, self.R5)]}}}]
        dashboard.link_quota_accounts([{"id": "t", "rows": rows}], [], [])
        self.assertEqual([r["anchor"] for r in rows], ["kvota-ucet-x", "kvota-ucet-x-2"])

    def test_plugin_cells_stay_on_agent_rows_for_compatibility(self):
        tables, agents, patches = self.fixture()
        agents[1]["cells"] = dict(patches[1]["cells"])
        dashboard.link_quota_accounts(tables, agents, patches)
        self.assertIn("c:wk", agents[1]["cells"])


class QuotaPageRendering(unittest.TestCase):
    def test_agents_get_one_account_chip_not_gauges(self):
        page = dashboard.PAGE
        self.assertIn("function renderAccountChip(a)", page)
        self.assertIn("Sdílený účet s: ", page)
        self.assertIn('titles=columns.map(c=>c.title).concat(quotaCol?["Kvóta"]:[])', page)
        self.assertIn('href="#${esc(acc.anchor)}"', page)

    def test_quota_cards_have_own_template_types_and_freshness(self):
        page = dashboard.PAGE
        self.assertIn('<template id="quota-table-tpl">', page)
        self.assertIn('t.kind==="quota"?"quota-table-tpl":"agent-table-tpl"', page)
        for word in ('"5h"', '"týden"', '"měsíc"', '"kredit"', '"ZASTARALÉ"', '"NEDOSTUPNÉ"', '"používají: "'):
            self.assertIn(word, page)
        self.assertIn("renderActions(r,table,{lock:false})", page)
        self.assertIn('"X-Agentsmon":"action"', page)

    def test_long_detail_is_disclosed_and_iso_times_are_localised(self):
        page = dashboard.PAGE
        self.assertIn("<summary>Podrobnosti</summary>", page)
        self.assertIn("function fmtIsoText(t)", page)
        self.assertIn("OPEN_DETAILS", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
