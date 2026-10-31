"""P3: incremental rendering (keyed rows, focus kept) + layout/services markup, exercised in jsdom when available."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentsmon import dashboard

NODE = os.environ.get("AGENTSMON_NODE", "/home/agentops/.hermes/tools/node-26.12.1-linux-x64/bin/node")
JSDOM = os.environ.get("AGENTSMON_JSDOM", "/home/agentops/.hermes/hermes-agent/node_modules/jsdom")

STATE = {
    "time": 1793434000,
    "agents": [
        {"name": "Claude", "id": "Claude", "kind": "claude-code", "label": "Opus", "vendor": "anthropic", "alive": True,
         "age": 100, "session_id": "s1", "waiting": True, "status": "Čeká na vstup",
         "waiting_hint": "Do you want to proceed?", "work": None},
        {"name": "Cursor", "id": "Cursor", "kind": "Cursor", "label": "Composer", "vendor": "cursor", "alive": True,
         "age": 200, "session_id": "s2",
         "work": {"issue": 12, "kind": "issue", "url": "https://github.com/o/r/issues/12", "title": "Úkol <b>",
                  "state": "running", "state_label_cs": "Běží"}},
    ],
    "agent_tables": [{"id": "codex-quota:providers", "kind": "quota", "title": "Kvóty", "anchor": "kvoty",
                      "rows": [{"id": "claude", "name": "Claude", "anchor": "kvota-claude", "age": 30, "cells": {
                          "w": {"bars": [{"label": "5h", "percent": 40, "reset_at": 1793440000,
                                          "reset_label": "12:00"}]}}}]}],
    "resets": [{"source": "Claude", "label": "5h", "reset_at": 1793440000, "reset_label": "12:00", "percent": 40}],
    "forecasts": {}, "columns": [], "plugin_errors": [], "plugins": [], "plugin_loading": False,
    "core": {"version": "t", "state": "clean"},
    "services": [{"name": "Telegram Bridge", "anchor": "sluzba-telegram-bridge", "state": "operational",
                  "latency_ms": 42, "uptime_seconds": 3600, "sla": 99.9, "sla_window_days": 90, "sla_samples": 10,
                  "timeline": [{"start": 1793400000, "uptime_pct": 100}], "timeline_days": 90, "rows": []}],
    "queue": {"enabled": True, "summary": {"running": 1}, "meta": {}, "items": [
        {"kind": "issue", "issue": 12, "title": "Úkol", "url": "https://github.com/o/r/issues/12", "state": "running",
         "state_label_cs": "Běží", "tracked": True, "worker": "cursor"}]},
    "attention": [{"severity": "warning", "text_cs": "Claude čeká na vstup", "href": "#agenti"}],
    "overall": {"level": "warning", "text_cs": "Vyžaduje pozornost (1)", "count": 1, "total": 1},
}

HARNESS = r"""
const J=require(process.env.JSDOM_PATH); const fs=require('fs');
const html=fs.readFileSync(process.argv[2],'utf8'); let S=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const errs=[]; const vc=new J.VirtualConsole();
vc.on('jsdomError',e=>errs.push(String(e.detail&&e.detail.stack||e.message))); vc.on('error',(...a)=>errs.push(a.join(' ')));
const dom=new J.JSDOM(html,{runScripts:'dangerously',url:'http://localhost/',virtualConsole:vc,beforeParse(w){
  w.fetch=async()=>({ok:true,json:async()=>JSON.parse(JSON.stringify(S))});
  w.matchMedia=()=>({matches:false}); w.HTMLElement.prototype.scrollIntoView=function(){};}});
const w=dom.window,d=w.document,sleep=ms=>new Promise(r=>setTimeout(r,ms)),o={};
(async()=>{
  await sleep(300);
  const row=n=>d.querySelector(`#agenti tr[data-key="r:${n}"]`);
  o.work=row('Cursor').querySelector('a.work-link').getAttribute('href');
  o.waiting=row('Claude').querySelector('.st-wait').textContent;
  o.services=d.querySelectorAll('#services .svc-item').length;
  const r0=row('Cursor'), b=r0.querySelector('[data-act="restart"]'); b.focus();
  let muts=0; new w.MutationObserver(l=>{muts+=l.filter(m=>!m.target.closest||!m.target.closest('#footer')).length;})
    .observe(d.getElementById('app'),{childList:true,subtree:true,attributes:true,characterData:true});
  await w.refresh(); await sleep(20);
  o.identicalMutations=muts; o.sameFocus=d.activeElement===b;
  S.agents[1].age+=3600; await w.refresh(); await sleep(20);
  const a=d.activeElement; o.sameRow=row('Cursor')===r0;
  o.focusRestored=a!==b&&a.dataset.act==='restart'&&a.closest('tr').dataset.key==='r:Cursor';
  console.log(JSON.stringify(o)); console.log(JSON.stringify(errs)); process.exit(0);
})();
"""


class PageMarkup(unittest.TestCase):
    def test_layout_services_and_incremental_helpers_present(self):
        p = dashboard.PAGE
        self.assertIn("@media (min-width:1280px)", p)
        self.assertLess(p.index('class="col-main"'), p.index('class="col-side"'))
        self.assertLess(p.index('id="agenti"'), p.index('class="col-side"'))
        self.assertLess(p.index('class="col-side"'), p.index('id="resets-panel"'))
        self.assertLess(p.index('id="plugin-tables"'), p.index('id="services"'))
        for needle in ("function patchRows(parent,rows,tag)", "function sectionChanged(k,v)",
                       "function setHTML(el,html)", "restoreUi(", "Historie a metriky", "work-link",
                       "Čeká na vstup", "data-reset-at"):
            self.assertIn(needle, p)
        # The server does PAGE.replace("POLL", seconds): the token may appear only in the poll timer.
        self.assertEqual(p.count("POLL"), p.count("POLL*1000"))


@unittest.skipUnless(os.path.exists(NODE) and os.path.isdir(JSDOM), "node + jsdom not available")
class IncrementalRenderJsdom(unittest.TestCase):
    def test_keyed_rows_keep_nodes_and_focus(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "page.html").write_text(dashboard.PAGE.replace("POLL", "15"), "utf-8")
            (t / "state.json").write_text(json.dumps(STATE), "utf-8")
            (t / "h.js").write_text(HARNESS, "utf-8")
            r = subprocess.run([NODE, str(t / "h.js"), str(t / "page.html"), str(t / "state.json")],
                               capture_output=True, text=True, timeout=60, env={**os.environ, "JSDOM_PATH": JSDOM})
        lines = r.stdout.strip().splitlines()
        self.assertGreaterEqual(len(lines), 2, r.stdout + r.stderr)
        o, errs = json.loads(lines[-2]), json.loads(lines[-1])
        self.assertEqual(errs, [])
        self.assertEqual(o["work"], "https://github.com/o/r/issues/12")
        self.assertIn("Čeká na vstup", o["waiting"])
        self.assertEqual(o["services"], 1)
        self.assertEqual(o["identicalMutations"], 0)
        self.assertTrue(o["sameFocus"])
        self.assertTrue(o["sameRow"])
        self.assertTrue(o["focusRestored"])


if __name__ == "__main__":
    unittest.main()
