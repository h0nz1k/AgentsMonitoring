"""GH #102: simplified overview without duplicates + stable overall status.

These assertions fail on 8746c5b (post-#90) and encode the UX proposal of 2026-11-24.
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import dashboard as d
from agentsmon import detect

try:
    from test_incremental_render import NODE, JSDOM, STATE, _JSDOM_OK, _JSDOM_SKIP
except ModuleNotFoundError:
    from tests.test_incremental_render import NODE, JSDOM, STATE, _JSDOM_OK, _JSDOM_SKIP

NOW = 1_800_000_000
REBUILD_FAIL_LIMIT = getattr(d, "STATE_REBUILD_FAIL_LIMIT", 3)


def _quota_row(name, *bars, vendor="openai", row_id=None):
    return {"id": row_id or name.lower().replace(" ", "-"), "name": name, "vendor": vendor,
            "anchor": "kvota-" + (row_id or name.lower().replace(" ", "-")),
            "cells": {"w": {"bars": list(bars)}}}


def _tables(*rows):
    return [{"id": "provider-quotas:providers", "kind": "quota", "title": "Kvóty", "rows": list(rows)}]


class StableOverallAfterFirstBuild(unittest.TestCase):
    """Bod 0: after a successful build, timeout/slow plugin must not flip overall to loading."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, AGENTSMON_STATE=self.tmp.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        d.invalidate_state_cache()
        d._state_building = False
        d._state_disk_checked = False
        d._state_rebuild_failures = 0
        self.addCleanup(d.invalidate_state_cache)

    def test_plugin_timeout_with_prior_success_is_warning_not_loading(self):
        tables = _tables(_quota_row("Codex", {"label": "5h", "percent": 10, "remaining": 90}))
        data = {
            "agents": [{"name": "A", "alive": True}],
            "services": [{"name": "S", "state": "operational"}],
            "agent_tables": tables,
            "plugins": [{"id": "provider-quotas", "loading": False, "error": "Plugin timed out"}],
            "plugin_errors": [{"name": "Plugin provider-quotas failed", "detail": "Plugin timed out"}],
            "plugin_loading": False,
        }
        att, ov = d.compute_attention(data, now=NOW)
        self.assertNotEqual(ov["level"], "loading")
        self.assertEqual(ov["level"], "warning")
        self.assertTrue(any("timed out" in a["text_cs"] or "Plugin" in a["text_cs"] for a in att))

    def test_plugin_loading_with_retained_tables_is_not_loading(self):
        tables = _tables(_quota_row("Codex", {"label": "7d", "percent": 50, "remaining": 50}))
        att, ov = d.compute_attention({
            "plugin_loading": True, "agent_tables": tables,
            "agents": [{"name": "A", "alive": True}], "services": [],
        }, now=NOW)
        self.assertNotEqual(ov["level"], "loading")
        self.assertEqual(ov["level"], "ok")

    def test_retain_error_plugin_keeps_stale_not_loading(self):
        data = {"agents": [], "services": [], "agent_tables": [], "plugins": [
            {"id": "slow", "loading": False, "error": "Plugin timed out"}],
            "plugin_errors": [{"name": "Plugin slow failed", "detail": "Plugin timed out"}],
            "plugin_loading": False}
        old = {"time": int(time.time() - 20), "agent_tables": [
            {"id": "slow:t", "title": "Kvóty", "kind": "quota",
             "rows": [{"id": "a", "name": "Codex", "plugin": "slow", "alive": True,
                       "cells": {"w": {"bars": [{"label": "5h", "percent": 1}]}}}]}]}
        kept = d._retain_pending_tables(data, old, 20)
        self.assertEqual(len(kept["agent_tables"]), 1)
        self.assertTrue(kept["agent_tables"][0].get("stale"))
        self.assertNotEqual(kept["overall"]["level"], "loading")

    def test_stale_age_alone_does_not_claim_rebuild_failed(self):
        body = json.dumps({
            "agents": [], "overall": {"level": "ok", "text_cs": "Vše v provozu", "count": 0, "total": 0},
            "attention": [], "loading": False, "plugin_loading": False,
        }).encode()
        d._state_cache = (time.monotonic() - 45, body)
        d._state_from_disk = False
        d._state_rebuild_failures = 0
        with patch.object(d, "_state", side_effect=lambda: time.sleep(9) or body):
            # Do not start a real long rebuild: keep building flag false and age high.
            d._state_building = True
            state = json.loads(d._cached_state())
        self.assertNotEqual(state["overall"].get("text_cs"), "Stav se nepodařilo obnovit")
        self.assertEqual(state["overall"]["level"], "ok")

    def test_rebuild_failed_only_after_n_consecutive_failures(self):
        good = json.dumps({
            "agents": [], "overall": {"level": "ok", "text_cs": "Vše v provozu", "count": 0},
            "attention": [], "loading": False,
        }).encode()
        d._state_cache = (time.monotonic() - 45, good)
        d._state_from_disk = False
        d._state_rebuild_failures = REBUILD_FAIL_LIMIT
        d._state_building = True
        state = json.loads(d._cached_state())
        self.assertEqual(state["overall"]["text_cs"], "Stav se nepodařilo obnovit")
        self.assertEqual(state["overall"]["level"], "warning")

    def test_disk_snapshot_keeps_prior_overall_not_loading(self):
        path = Path(self.tmp.name) / "state-snapshot.json"
        path.write_text(json.dumps({
            "agents": [{"name": "A", "alive": True}],
            "agent_tables": [],
            "overall": {"level": "ok", "text_cs": "Vše v provozu", "count": 0, "total": 0},
            "attention": [], "loading": False, "plugin_loading": False,
        }))
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(3)
            return json.dumps({"agents": [], "overall": {"level": "ok"}, "loading": False}).encode()

        self.addCleanup(release.set)
        with patch.object(d, "_state", side_effect=slow):
            state = json.loads(d._cached_state())
            self.assertTrue(state["stale"])
            self.assertNotEqual(state["overall"]["level"], "loading")
            self.assertEqual(state["overall"]["level"], "ok")
            self.assertTrue(started.wait(1))
            release.set()
            deadline = time.monotonic() + 3
            while d._state_building and time.monotonic() < deadline:
                time.sleep(0.01)

    def test_ha_summary_overall_not_loading_after_ready_cache(self):
        d._state_cache = (time.monotonic(), json.dumps({
            "time": NOW, "stale": False, "loading": False, "plugin_loading": False,
            "overall": {"level": "ok", "count": 0, "summary_cs": "Vše běží."},
            "attention": [], "agents": [], "services": [], "agent_tables": [], "forecasts": [],
            "queue": {"enabled": False},
        }).encode())
        d._state_from_disk = False
        summary = json.loads(d.ha_summary_bytes())
        self.assertNotEqual(summary["overall"], "loading")
        self.assertEqual(summary["overall"], "ok")
        self.assertEqual(summary["schema"], d.HA_SUMMARY_SCHEMA)


class AttentionDedup(unittest.TestCase):
    """Bod 2: each problem once in K řešení (source + window)."""

    def test_exhausted_same_source_window_once(self):
        bar = {"label": "7d", "percent": 100.0, "remaining": 0.0, "state": "EXHAUSTED",
               "reset_at": NOW + 3600}
        tables = [
            {"id": "codex-quota:providers", "kind": "quota", "rows": [
                _quota_row("Codex · agentops", bar, row_id="a")]},
            {"id": "provider-quotas:providers", "kind": "quota", "rows": [
                _quota_row("Codex · agentops", dict(bar), row_id="b")]},
        ]
        # Merged title path still may pass two rows with same name+window before merge.
        att, _ = d.compute_attention({"agent_tables": tables}, now=NOW)
        exh = [a for a in att if a.get("kind") == "exhausted"]
        self.assertEqual(len(exh), 1, exh)

    def test_forecast_and_exhausted_same_window_dedup_to_exhausted(self):
        tables = _tables(_quota_row(
            "Codex · agentops",
            {"label": "5h", "percent": 100.0, "remaining": 0.0, "state": "EXHAUSTED",
             "reset_at": NOW + 1800}))
        fc = {"source": "Codex · agentops", "label": "5h", "used_pct": 100.0,
              "reset_at": NOW + 1800, "exhaust_at": NOW + 60,
              "will_exhaust_before_reset": True, "exhausted": False, "span_seconds": 3600}
        with patch.object(d.alerts, "forecast_actionable", return_value=True):
            att, _ = d.compute_attention({"agent_tables": tables, "forecasts": [fc]}, now=NOW)
        kinds = [a.get("kind") for a in att if "Codex" in a.get("text_cs", "")]
        self.assertEqual(kinds.count("exhausted"), 1)
        self.assertEqual(kinds.count("forecast"), 0)


class HeaderAndQuotaGroupingPage(unittest.TestCase):
    """Body 1 + 3: header without quotas; quotas grouped per provider in page source."""

    def test_header_has_no_quota_chip_and_resets_stay_empty(self):
        page = d.PAGE
        self.assertIn('id="hero-resets"', page)
        self.assertIn("Nejbližší obnovy", page)
        # Chips must not advertise quota problems (those live only in K řešení / Kvóty).
        self.assertNotIn("`Kvóty · ${issues}", page)
        self.assertNotIn("Kvóty · ${issues}", page)
        self.assertIn("renderResets([])", page)
        self.assertIn("provider-quota-row", page)
        self.assertIn("quota-edge-details", page)
        self.assertIn("+ další", page)

    def test_overview_counts_include_queue_and_age_not_quotas(self):
        page = d.PAGE
        self.assertIn("renderOverviewChips", page)
        self.assertIn("fronta", page.lower())
        self.assertRegex(page, r"data.?age|stáří|min|state_age|posílá|aktualiz")


class AgentsServicesCompact(unittest.TestCase):
    def test_services_hide_ideal_behind_details(self):
        page = d.PAGE
        self.assertIn("idealServices", page)
        self.assertIn("nad 99", page)

    def test_agents_auto_expand_on_problem(self):
        page = d.PAGE
        self.assertIn("autoCollapseSection", page)


class CodexRolloutModelLive(unittest.TestCase):
    """Bod 5: model from the live session/rollout, not a stale head/config cache."""

    def test_latest_model_in_rollout_wins_over_early_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-x.jsonl"
            lines = [
                json.dumps({"cwd": "/w", "model": "gpt-6.1-sol"}),
                json.dumps({"type": "response", "payload": {"x": 1}}),
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-6.2-sol"}}),
            ]
            # Pad so the latest model is past the old 60-line head window.
            lines = [json.dumps({"pad": i}) for i in range(80)] + lines
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(detect._rollout_model(str(path)), "GPT-6.2 Sol")

    def test_argv_beats_stale_config_toml_when_rollout_silent(self):
        with patch.object(detect, "_codex_model", return_value="GPT-6.1 Sol"), \
                patch.object(detect, "_rollout_model", return_value=None):
            model = detect._model_from_argv(["codex --model gpt-6.2-sol"]) or detect._codex_model()
            # discover_agents priority: argv before global config when rollout empty.
            self.assertEqual(detect._model_from_argv(["codex --model gpt-6.2-sol"]), "GPT-6.2 Sol")
            self.assertEqual(model, "GPT-6.2 Sol")


@unittest.skipIf(_JSDOM_SKIP, "node + jsdom not available")
class UxSimplifyJsdom(unittest.TestCase):
    def setUp(self):
        if os.environ.get("CI") and not _JSDOM_OK:
            self.fail("AGENTSMON_NODE + AGENTSMON_JSDOM required when CI=true")

    def test_header_no_quota_attention_unique_quotas_grouped(self):
        state = copy.deepcopy(STATE)
        state["overall"].update(
            level="warning", text_cs="Běží vše · 2 věci k řešení", count=2, total=2,
            critical=0, warning=2, info=0, count_cs="2",
            summary_cs="Codex dojde před obnovou; Antigravity vyčerpán.")
        state["attention"] = [
            {"severity": "warning", "kind": "forecast", "text_cs": "Codex dojde před obnovou",
             "detail_cs": "týden zbývá 11 %", "href": "#kvota-codex"},
            {"severity": "warning", "kind": "exhausted", "text_cs": "Antigravity: vyčerpáno týden",
             "detail_cs": "obnova zítra", "href": "#kvota-agy"},
            # Intentional duplicate of the same problem — server should not emit this; UI must
            # still show each text once if it somehow arrives.
            {"severity": "warning", "kind": "exhausted", "text_cs": "Antigravity: vyčerpáno týden",
             "detail_cs": "obnova zítra", "href": "#kvota-agy"},
        ]
        state["resets"] = [
            {"source": "Claude", "label": "5h", "reset_at": NOW + 3600, "percent": 10},
            {"source": "Codex", "label": "7d", "reset_at": NOW + 86400, "percent": 90},
        ]
        state["agent_tables"] = [{
            "id": "provider-quotas:providers", "kind": "quota", "title": "Kvóty", "anchor": "kvoty",
            "rows": [
                {"id": "claude", "name": "Claude", "vendor": "anthropic", "anchor": "kvota-claude",
                 "cells": {"w": {"bars": [
                     {"label": "5h", "percent": 9, "remaining": 91, "reset_at": NOW + 3600},
                     {"label": "Claude week", "percent": 87, "remaining": 13, "reset_at": NOW + 86400}]}}},
                {"id": "codex", "name": "Codex", "vendor": "openai", "anchor": "kvota-codex",
                 "cells": {"w": {"bars": [
                     {"label": "5h", "percent": 0, "remaining": 100},
                     {"label": "7d", "percent": 89, "remaining": 11, "reset_at": NOW + 90000}]}}},
                {"id": "vertex", "name": "Vertex AI", "vendor": "google", "anchor": "kvota-vertex",
                 "cells": {"w": {"bars": [
                     {"label": "GCP Bonus", "percent": 37, "remaining": 63,
                      "meta": {"spent_usd": 10, "initial_usd": 300, "remaining_credit": 6000,
                               "currency": "CZK"}}]}}},
            ],
        }]
        state["state_age_seconds"] = 12
        script = r'''
const {JSDOM,VirtualConsole}=require(process.env.JSDOM_PATH),fs=require('fs'),assert=require('assert');
const state=JSON.parse(fs.readFileSync(process.argv[3],'utf8')),errors=[];
const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
const dom=new JSDOM(fs.readFileSync(process.argv[2],'utf8'),{runScripts:'dangerously',url:'http://localhost/',virtualConsole:vc,beforeParse(w){
  w.fetch=async()=>({ok:true,json:async()=>structuredClone(state)});
  w.matchMedia=()=>({matches:false,addEventListener(){}});
}});
const w=dom.window,d=w.document;
(async()=>{
  await new Promise(r=>setTimeout(r,120));
  assert.deepEqual(errors,[]);
  const header=d.querySelector('.overview');
  const headerText=header.textContent;
  assert.ok(!/Nejbližší obnovy/.test(headerText)||d.getElementById('hero-resets').hidden);
  assert.ok(!/\d+\s*%/.test(d.getElementById('overview-chips').textContent),
            'quota percents must not appear in header chips');
  assert.ok(!/Kvóty ·/.test(d.getElementById('overview-chips').textContent));
  const attn=[...d.querySelectorAll('#attention .attn-item')].map(li=>li.textContent.replace(/\s+/g,' ').trim());
  const titles=attn.map(t=>t.replace(/Detail.*/,'').trim());
  assert.equal(new Set(titles).size, titles.length, 'each problem once: '+JSON.stringify(titles));
  assert.ok(d.querySelector('.provider-quota-row')||d.querySelector('.quota-card'));
  const edge=d.querySelector('.quota-edge-details');
  assert.ok(edge, 'edge quotas collapsed');
  assert.match(edge.textContent,/\+ další/);
  console.log('ux simplify jsdom OK');
  w.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.html").write_text(d.PAGE.replace("POLL", "15").replace("__AGENTSMON_ROLE__", "viewer"))
            (root / "state.json").write_text(json.dumps(state))
            (root / "test.cjs").write_text(script)
            result = __import__("subprocess").run(
                [NODE, str(root / "test.cjs"), str(root / "page.html"), str(root / "state.json")],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "JSDOM_PATH": JSDOM})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ux simplify jsdom OK", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
