"""Live status web page — `agentsmon dashboard`.

Pure standard-library HTTP server (no Flask/FastAPI). Serves one self-contained page that polls
``/api/state`` and renders the same layout as a clean status page: a **Persistent agents** table
and one availability card per **service** (status dot, Uptime / Availability-SLA / Latency
metrics, and a day-by-day availability timeline). A background thread probes services on an
interval and appends to the uptime DB, so just leaving the dashboard running builds history.
Binds 127.0.0.1 by default; optional HTTP Basic auth (config.dashboard.auth). UI text is Czech (lang=cs, Intl cs-CZ).
"""
from __future__ import annotations

import base64
import ipaddress
import re
import unicodedata
import hashlib
import hmac
import json
from pathlib import Path
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import alerts, config, db, detect, keepalive, metrics, probe, plugins, quota, workqueue


def password_hash(password: str) -> str:
    """Hash a dashboard password for storage (we never keep the plaintext in config)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _action_allowed(headers) -> bool:
    """A state-changing request must carry the ``X-Agentsmon`` header the page sets. A form or a
    plain fetch from another site cannot add a custom header without a CORS preflight (which
    this server never answers), so a page you happen to have open elsewhere cannot stop or
    restart your agents behind your back — even with HTTP auth cached (audit 2026-09-05)."""
    try:
        return (headers.get("X-Agentsmon") or "").strip().lower() == "action"
    except Exception:
        return False


def _auth_ok(header: str | None, user: str, pwhash: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    u, _, pw = raw.partition(":")
    return hmac.compare_digest(u, user) and hmac.compare_digest(password_hash(pw), pwhash)


AGENT_TABLE = r"""
  <section class="mb-6" data-svc="KEY" HIDDEN>
    <div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200" role="button" tabindex="0" aria-expanded="true">
      <span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0" aria-hidden="true"></span>
      <h2 class="text-base font-semibold">TITLE</h2>
      <span class="agents-count ml-auto text-sm font-medium text-slate-400">načítání…</span>
      <span class="svc-chev text-slate-400 text-xs ml-2 select-none" title="sbalit / rozbalit" aria-hidden="true">▾</span>
    </div>
    <div class="svc-body">
    <p class="scroll-hint text-[11px] text-slate-400 mb-1" aria-hidden="true">↔ tabulku lze posouvat do stran</p>
    <div class="rounded-lg border border-slate-200 bg-white overflow-x-auto">
      <table class="w-full text-sm agents-table" style="table-layout:auto"><thead>
        <tr class="text-[11px] uppercase tracking-wide text-slate-400 border-b border-slate-100">
          <th class="text-left font-medium px-2 py-2">Agent</th>
          <th class="text-left font-medium px-2 py-2">Model</th>
          <th class="text-left font-medium px-2 py-2">Relace / port</th>
          <th class="text-left font-medium px-2 py-2">Spuštěno</th>
          <th class="text-left font-medium px-2 py-2">Stav</th>
          <th class="text-right font-medium px-2 py-2"></th>
        </tr></thead>
        <tbody class="agent-rows"><tr><td colspan="6" class="px-3 py-3 text-slate-400">načítání…</td></tr></tbody>
      </table>
    </div>
    <p class="agents-note text-[11px] text-slate-400 mt-2">NOTE</p>
    </div>
  </section>"""


QUOTA_SECTION = r"""<template id="quota-table-tpl">
  <section class="mb-6 quota-section" data-svc="plugin">
    <div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200" role="button" tabindex="0" aria-expanded="true">
      <span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0" aria-hidden="true"></span>
      <h2 class="text-base font-semibold">Kvóty</h2>
      <span class="agents-count ml-auto text-sm font-medium text-slate-400">načítání…</span>
      <span class="svc-chev text-slate-400 text-xs ml-2 select-none" title="sbalit / rozbalit" aria-hidden="true">▾</span>
    </div>
    <div class="svc-body">
      <ul class="quota-cards" role="list"></ul>
      <details class="pod qc-note mt-1" data-key="quota-note" hidden><summary>Poznámka ke kvótám</summary><p class="pod-body agents-note" style="list-style:none;padding-left:0"></p></details>
    </div>
  </section>
</template>"""


QUEUE_SECTION = '<section id="fronta" class="mb-6 queue-section" data-svc="fronta" hidden aria-label="Fronta práce"><div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200" role="button" tabindex="0" aria-expanded="true"><span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0" aria-hidden="true"></span><h2 class="text-base font-semibold">Fronta práce</h2><span class="agents-count ml-auto text-sm font-medium text-slate-400">načítání…</span><span class="svc-chev text-slate-400 text-xs ml-2 select-none" title="sbalit / rozbalit" aria-hidden="true">▾</span></div><div class="svc-body"><div class="q-bar"><div class="q-counters flex flex-wrap items-center gap-1" role="group" aria-label="Filtr podle stavu"></div><a class="q-gh q-link" hidden target="_blank" rel="noopener noreferrer">GitHub ↗</a></div><div class="q-tabs" role="tablist" aria-label="Pohled na frontu práce"></div><p class="q-note" role="status" hidden></p><div id="q-panel" role="tabpanel" aria-label="Položky fronty práce"><ul class="q-list" role="list"><li class="q-empty">načítání…</li></ul></div></div></section>'


PAGE = r"""<!DOCTYPE html><html lang="cs"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>Monitorování agentů</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🤖</text></svg>">
<script src="/static/tailwind.js"></script>
<style>
:root{--gap:0.75rem;--touch:44px}
.bar{transition:opacity .15s ease}.bar:hover{opacity:.65}
.copied-flash{color:#059669!important;transition:color .1s}
#toast{transition:opacity .2s ease}
.agent-rows td{overflow:hidden;vertical-align:middle}
.sid{display:block;overflow:hidden;text-overflow:ellipsis}
.sid-btn{display:inline-flex;align-items:center;justify-content:center;min-width:var(--touch);min-height:36px;padding:6px;border-radius:6px;color:#64748b}
.sid-btn:hover,.sid-btn:focus-visible{color:#0284c7;background:#f1f5f9;outline:2px solid #38bdf8;outline-offset:1px}
.sid-btn.sid-muted{color:#94a3b8;cursor:default}
.quota-stack{display:flex;flex-direction:column;gap:4px;min-width:0;max-width:100%}
.quota-row{display:flex;align-items:center;gap:6px;min-width:0;font-size:12px;line-height:1.25}
.quota-gauge{display:inline-flex;align-items:center;gap:6px;min-width:0;max-width:100%;padding:2px 4px;margin:-2px -4px;border-radius:6px;border:0;background:transparent;cursor:help;font:inherit;color:inherit;text-align:left}
.quota-gauge:focus-visible{outline:2px solid #38bdf8;outline-offset:1px}
.quota-gauge[aria-expanded="true"]{background:#f1f5f9}
.quota-name{flex:0 0 auto;min-width:1.75rem;color:#475569;font-weight:600}
.reset-pill{display:inline-flex;align-items:center;gap:.25rem;margin:0 .25rem;padding:.1rem .5rem;border:1px solid #fcd34d;border-radius:999px;background:#fffbeb;color:#92400e;font-size:11px;font-weight:600;white-space:nowrap;vertical-align:middle}
.reset-pill:hover{background:#fef3c7;border-color:#f59e0b}
.quota-until{font-size:10px;color:#94a3b8;font-variant-numeric:tabular-nums;white-space:nowrap}
.resets-sum{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem .5rem;min-height:var(--touch);cursor:pointer;list-style:none}
.resets-sum::-webkit-details-marker{display:none}
.resets-sum::after{content:"▸";margin-left:auto;font-size:12px;color:#94a3b8}
.resets-details[open] .resets-sum::after{content:"▾"}
.resets-details[open] .resets-sum{margin-bottom:.25rem}
.resets-sum:focus-visible{outline:2px solid #38bdf8;outline-offset:2px}
.resets-extra .exh{color:#be123c;font-weight:700}
.resets-list{display:flex;flex-wrap:wrap;gap:.4rem;list-style:none;margin:0;padding:0}
.reset-chip{display:flex;flex-direction:column;gap:1px;min-width:9.5rem;padding:.35rem .6rem;border:1px solid #e2e8f0;border-radius:.5rem;background:#fff;font-size:11px;line-height:1.25}
.reset-chip .reset-when{font-weight:700;font-size:12px;color:#0f172a;font-variant-numeric:tabular-nums}
.reset-chip .reset-what{color:#334155;font-weight:600}
.reset-chip .reset-meta{color:#64748b}
.reset-chip.exhausted{border-color:#fda4af;background:#fff1f2}
.reset-chip.exhausted .reset-when{color:#be123c}
.reset-chip .reset-fc{color:#64748b;font-style:italic}
.reset-chip .reset-fc.warn{color:#b45309;font-weight:600;font-style:normal}
.reset-chip.will-exhaust{border-color:#fcd34d;background:#fffbeb}
.quota-track{flex:1 1 3.5rem;min-width:2.5rem;max-width:5.5rem;height:8px;background:#e2e8f0;border-radius:999px;overflow:hidden}
.quota-fill{display:block;height:100%;border-radius:999px}
.quota-pct{flex:0 0 auto;font-variant-numeric:tabular-nums;font-weight:600;min-width:3.5rem;text-align:right}
.quota-reset{flex:0 0 auto;color:#64748b;font-variant-numeric:tabular-nums}
.quota-state{flex:0 0 auto;font-size:10px;font-weight:700;letter-spacing:.02em;text-transform:uppercase}
.quota-state.ok{color:#059669}.quota-state.stale{color:#d97706}.quota-state.unavail{color:#94a3b8}
.quota-tip{display:none;position:absolute;z-index:40;max-width:min(22rem,calc(100vw - 1.5rem));padding:8px 10px;border-radius:8px;background:#0f172a;color:#f8fafc;font-size:12px;line-height:1.35;box-shadow:0 8px 24px rgba(15,23,42,.25);white-space:normal;word-break:break-word}
.quota-tip.open{display:block}
.quota-wrap{position:relative;display:inline-block;max-width:100%}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.agent-act{min-width:var(--touch);min-height:36px;justify-content:center}
.svc-head{min-height:var(--touch);cursor:pointer}
.svc-head:focus-visible{outline:2px solid #38bdf8;outline-offset:2px}
.svc-head h2{pointer-events:none}
.scroll-hint{display:none}
.acct-chip{display:inline-flex;align-items:center;gap:.3rem;min-height:var(--touch);max-width:100%;padding:0 .6rem;border:1px solid #e2e8f0;border-radius:999px;background:#fff;color:#334155;font-size:12px;font-weight:600;white-space:nowrap;text-decoration:none}
a.acct-chip:hover,a.acct-chip:focus-visible{border-color:#38bdf8;outline:2px solid #38bdf8;outline-offset:1px}
.acct-chip .acct-name{overflow:hidden;text-overflow:ellipsis;max-width:11rem}
.acct-chip .acct-win{font-variant-numeric:tabular-nums}
.acct-chip.warn{border-color:#fcd34d;background:#fffbeb;color:#92400e}
.acct-chip.err{border-color:#fda4af;background:#fff1f2;color:#be123c}
.quota-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,22rem),1fr));gap:.75rem;list-style:none;margin:0;padding:0}
.quota-card{display:flex;flex-direction:column;gap:.35rem;min-width:0;padding:.65rem .8rem;border:1px solid #e2e8f0;border-radius:.5rem;background:#fff;scroll-margin:1rem;transition:box-shadow .3s ease}
.quota-card.exhausted{border-color:#fda4af}
.quota-card.flash{box-shadow:0 0 0 3px #38bdf8}
.quota-card:focus{outline:none}
.qc-head{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem}
.qc-name{font-weight:600;font-size:14px}
.qc-users{font-size:11px;color:#64748b;margin:0}
.qc-exh{font-size:11px;font-weight:700;color:#be123c}
.fresh{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;font-weight:700;letter-spacing:.02em;border-radius:999px;padding:.1rem .5rem;margin-left:auto}
.fresh.ok{background:#ecfdf5;color:#047857}.fresh.stale{background:#fffbeb;color:#b45309}.fresh.unavail{background:#f1f5f9;color:#475569}
.qc-wins{display:flex;flex-direction:column;gap:.35rem}
.qc-win{display:flex;flex-wrap:wrap;align-items:center;column-gap:.45rem;row-gap:1px;font-size:12px;min-width:0}
.win-type{flex:0 0 3.2rem;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:#64748b}
.win-label{flex:0 1 6.5rem;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#334155;font-weight:600}
.qc-win .quota-track{flex:1 1 4rem;max-width:none}
.qc-win .quota-pct{min-width:5.2rem}
.qc-win .quota-reset,.qc-win .win-fc{flex:1 0 100%;padding-left:3.65rem;font-size:11px}
.win-fc{color:#64748b;font-style:italic}.win-fc.warn{color:#b45309;font-weight:600;font-style:normal}
.qc-actions{display:flex;flex-wrap:wrap;gap:.4rem}
.qc-actions .reset-pill{min-height:var(--touch);margin:0}
.pod{font-size:12px;color:#475569;white-space:normal}
.pod>summary{min-height:var(--touch);line-height:var(--touch);cursor:pointer;color:#0369a1;font-weight:600;width:max-content}
.pod>summary:focus-visible{outline:2px solid #38bdf8;outline-offset:1px}
.pod-body{margin:.1rem 0 .3rem;padding-left:1.1rem;list-style:disc;line-height:1.4;word-break:break-word;max-width:48rem}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:flex-end;margin-bottom:1rem}
.toolbar button{min-height:var(--touch);min-width:var(--touch);padding:0 12px;border-radius:8px;border:1px solid #e2e8f0;background:#fff;color:#334155;font-size:13px}
.toolbar button:hover,.toolbar button:focus-visible{border-color:#94a3b8;outline:2px solid #38bdf8;outline-offset:1px}
.toolbar button[aria-pressed="true"]{background:#f1f5f9;font-weight:600}
#app{max-width:min(1480px,100%);padding-left:max(1rem,env(safe-area-inset-left));padding-right:max(1rem,env(safe-area-inset-right))}
.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0.75rem}
@media (max-width:640px){
  .metric-grid{grid-template-columns:1fr}
  .quota-row{flex-wrap:wrap}
  .quota-gauge{flex-wrap:wrap;width:100%}
  .quota-track{flex:1 1 100%;max-width:none}
  th,td{font-size:12px}
  .scroll-hint{display:block}
}
@media (min-width:1440px){
  .quota-track{max-width:7rem}
}
@media (prefers-reduced-motion:reduce){
  .bar,#toast,.copied-flash{transition:none!important}
}
.ovr{display:inline-flex;align-items:center;gap:.35rem;min-height:2rem;padding:0 .7rem;border:1px solid #e2e8f0;border-radius:999px;font-size:13px;font-weight:600;white-space:nowrap;background:#fff;color:#475569}
.ovr.ok{border-color:#a7f3d0;background:#ecfdf5;color:#047857}.ovr.warning{border-color:#fcd34d;background:#fffbeb;color:#92400e}.ovr.critical{border-color:#fda4af;background:#fff1f2;color:#be123c}
.attn{border:1px solid #fcd34d;border-radius:.5rem;background:#fffbeb;padding:.5rem .75rem}
.attn.critical{border-color:#fda4af;background:#fff1f2}
.attn h2{font-size:15px;font-weight:600;margin:0 0 .25rem}
.attn-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.attn-item{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem .6rem;min-height:var(--touch);border-top:1px solid rgba(148,163,184,.3);font-size:13px}
.attn-item:first-child{border-top:0}
.attn-ico{flex:0 0 1.4rem;text-align:center;font-weight:700}
.attn-item.critical .attn-ico{color:#be123c}.attn-item.warning .attn-ico{color:#b45309}.attn-item.info .attn-ico{color:#0369a1}
.attn-text{flex:1 1 16rem;min-width:0;word-break:break-word;color:#334155}
.attn-link,.q-link{display:inline-flex;align-items:center;min-height:var(--touch);color:#0369a1;font-weight:600;text-decoration:none}
.attn-link:hover,.attn-link:focus-visible,.q-link:hover,.q-link:focus-visible{text-decoration:underline;outline-offset:1px}
.attn-item .reset-pill{min-height:var(--touch);margin:0}
.q-bar{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem .4rem;margin-bottom:.5rem}
.q-ctr{min-height:var(--touch);padding:0 .55rem;border:1px solid #e2e8f0;border-radius:999px;background:#fff;color:#334155;font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.q-ctr[aria-pressed="true"]{background:#e0f2fe;border-color:#38bdf8;color:#075985}
.q-ctr.bad{color:#be123c;border-color:#fda4af}.q-ctr.warn{color:#92400e;border-color:#fcd34d}
.q-ctr:focus-visible,.q-tab:focus-visible{outline:2px solid #38bdf8;outline-offset:1px}
.q-sep{color:#94a3b8;font-size:12px}
.q-gh{margin-left:auto}
.q-tabs{display:flex;flex-wrap:wrap;gap:.25rem;margin-bottom:.5rem;border-bottom:1px solid #e2e8f0}
.q-tab{min-height:var(--touch);padding:0 .7rem;border:0;border-bottom:2px solid transparent;background:transparent;color:#475569;font-size:13px;font-weight:600}
.q-tab[aria-selected="true"]{border-bottom-color:#0284c7;color:#0369a1}
.q-note{margin:.25rem 0 .5rem;padding:.4rem .6rem;border:1px solid #fcd34d;border-radius:.4rem;background:#fffbeb;color:#92400e;font-size:12px}
.q-list{list-style:none;margin:0;padding:0;border:1px solid #e2e8f0;border-radius:.5rem;background:#fff}
.q-item{display:grid;grid-template-columns:3.5rem minmax(0,1fr) 9rem 8rem 8.5rem 5.5rem;align-items:center;gap:.25rem .6rem;padding:.3rem .7rem;border-top:1px solid #f1f5f9;font-size:13px}
.q-item:first-child{border-top:0}
.q-empty{padding:.75rem;color:#94a3b8;font-size:13px}
.q-num{font-variant-numeric:tabular-nums;font-weight:600}
.q-title{display:flex;align-items:center;gap:.35rem;min-width:0}
.q-title a,.q-title .q-tt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;color:#0f172a;text-decoration:none}
.q-title a:hover,.q-title a:focus-visible{text-decoration:underline;color:#0369a1}
.q-mm{flex:0 0 auto;display:inline-flex;align-items:center;gap:.2rem;padding:.05rem .4rem;border:1px solid #fcd34d;border-radius:999px;background:#fffbeb;color:#92400e;font-size:11px;font-weight:700;cursor:help}
.q-badge{display:inline-flex;align-items:center;gap:.3rem;padding:.1rem .5rem;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;width:max-content}
.q-badge.running{background:#e0f2fe;color:#075985}.q-badge.queued{background:#f1f5f9;color:#334155}
.q-badge.awaiting_pickup{background:#ede9fe;color:#5b21b6}.q-badge.blocked{background:#fffbeb;color:#92400e;border:1px solid #fcd34d}
.q-badge.failed{background:#fff1f2;color:#be123c;border:1px solid #fda4af}.q-badge.done{background:#ecfdf5;color:#047857}
.q-who,.q-age{color:#64748b;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.q-age{font-variant-numeric:tabular-nums}
.q-pr{display:flex;align-items:center;gap:.3rem;font-size:12px;white-space:nowrap}
.ci{font-weight:700}.ci.success{color:#059669}.ci.failure{color:#be123c}.ci.pending{color:#b45309}
.q-draft{padding:0 .35rem;border:1px solid #cbd5e1;border-radius:.3rem;font-size:10px;font-weight:700;color:#64748b;text-transform:uppercase}
.q-extra{grid-column:2 / -1;display:flex;flex-wrap:wrap;align-items:center;gap:0 .75rem;font-size:12px;color:#64748b}
.q-extra .pod>summary{min-height:2rem;line-height:2rem}
@media (max-width:639px){
  .q-list{border:0;background:transparent;display:flex;flex-direction:column;gap:.5rem}
  .q-item{display:flex;flex-wrap:wrap;border:1px solid #e2e8f0;border-radius:.5rem;background:#fff;padding:.5rem .7rem}
  .q-item:first-child{border-top:1px solid #e2e8f0}
  .q-title{flex:1 1 calc(100% - 4rem)}
  .q-title a,.q-num a,.q-pr a{min-height:var(--touch);display:inline-flex;align-items:center}
  .q-extra .pod>summary{min-height:var(--touch);line-height:var(--touch)}
  .q-gh{margin-left:0}
}
@media (prefers-color-scheme:dark){
  .ovr{background:#0f172a;border-color:#334155;color:#cbd5e1}.ovr.ok{background:rgba(6,95,70,.35);border-color:#065f46;color:#6ee7b7}
  .ovr.warning{background:rgba(120,53,15,.35);border-color:#b45309;color:#fcd34d}.ovr.critical{background:rgba(136,19,55,.35);border-color:#9f1239;color:#fda4af}
  .attn{background:rgba(120,53,15,.25);border-color:#b45309}.attn.critical{background:rgba(136,19,55,.25);border-color:#9f1239}
  .attn-text{color:#e2e8f0}.attn-link,.q-link{color:#38bdf8}
  .attn-item.critical .attn-ico{color:#fda4af}.attn-item.warning .attn-ico{color:#fcd34d}.attn-item.info .attn-ico{color:#7dd3fc}
  .q-ctr{background:#0f172a;border-color:#334155;color:#e2e8f0}.q-ctr[aria-pressed="true"]{background:rgba(7,89,133,.5);border-color:#38bdf8;color:#e0f2fe}
  .q-ctr.bad{color:#fda4af;border-color:#9f1239}.q-ctr.warn{color:#fcd34d;border-color:#b45309}
  .q-tabs{border-color:#1e293b}.q-tab{color:#94a3b8}.q-tab[aria-selected="true"]{color:#38bdf8;border-bottom-color:#38bdf8}
  .q-note{background:rgba(120,53,15,.3);border-color:#b45309;color:#fcd34d}
  .q-list{background:#0f172a;border-color:#1e293b}.q-item{border-color:#1e293b}
  .q-title a,.q-title .q-tt{color:#e2e8f0}.q-title a:hover{color:#38bdf8}
  .q-mm{background:rgba(120,53,15,.35);border-color:#b45309;color:#fcd34d}
  .q-badge.running{background:rgba(7,89,133,.45);color:#bae6fd}.q-badge.queued{background:#1e293b;color:#cbd5e1}
  .q-badge.awaiting_pickup{background:rgba(91,33,182,.4);color:#ddd6fe}.q-badge.blocked{background:rgba(120,53,15,.35);color:#fcd34d;border-color:#b45309}
  .q-badge.failed{background:rgba(136,19,55,.35);color:#fda4af;border-color:#9f1239}.q-badge.done{background:rgba(6,95,70,.4);color:#6ee7b7}
  .q-who,.q-age,.q-extra{color:#94a3b8}.ci.success{color:#34d399}.ci.failure{color:#fb7185}.ci.pending{color:#fbbf24}
  .q-draft{border-color:#475569;color:#94a3b8}
  @media (max-width:639px){.q-list{background:transparent}.q-item{background:#0f172a;border-color:#1e293b}.q-item:first-child{border-top-color:#1e293b}}
  body.bg-slate-50{background-color:#020617!important;color:#e2e8f0!important}
  .bg-white{background-color:#0f172a!important}
  .bg-slate-50{background-color:#020617!important}
  .bg-emerald-50{background-color:rgba(6,78,59,.35)!important}
  .bg-rose-50{background-color:rgba(136,19,55,.35)!important}
  .bg-amber-50{background-color:rgba(120,53,15,.3)!important}.border-amber-200{border-color:#92400e!important}
  .border-slate-200,.border-slate-100{border-color:#1e293b!important}
  .border-emerald-200{border-color:#065f46!important}
  .border-rose-200{border-color:#9f1239!important}
  .text-slate-800,.text-slate-700{color:#e2e8f0!important}
  .text-slate-600,.text-slate-500{color:#94a3b8!important}
  .text-slate-400,.text-slate-300{color:#64748b!important}
  .bg-slate-200,.bg-slate-300{background-color:#334155!important}
  .bg-orange-100{background-color:rgba(154,52,18,.4)!important}.text-orange-700{color:#fdba74!important}
  .bg-emerald-100{background-color:rgba(6,95,70,.4)!important}.text-emerald-700{color:#6ee7b7!important}
  .bg-violet-100{background-color:rgba(91,33,182,.4)!important}.text-violet-700{color:#c4b5fd!important}
  .bg-amber-100{background-color:rgba(146,64,14,.4)!important}.text-amber-700{color:#fcd34d!important}
  .bg-rose-100{background-color:rgba(159,18,57,.4)!important}.text-rose-700{color:#fda4af!important}
  .bg-slate-100{background-color:rgba(51,65,85,.55)!important}
  .bg-sky-100{background-color:rgba(7,89,133,.4)!important}
  .text-emerald-600{color:#34d399!important}.text-amber-600{color:#fbbf24!important}
  .text-rose-600{color:#fb7185!important}.text-sky-600{color:#38bdf8!important}
  .hover\:bg-slate-100:hover{background-color:#1e293b!important}
  .hover\:bg-rose-50:hover{background-color:rgba(136,19,55,.35)!important}
  .sid-btn{color:#94a3b8}.sid-btn:hover,.sid-btn:focus-visible{color:#38bdf8;background:#1e293b}
  .quota-name{color:#94a3b8}.quota-track{background:#334155}.quota-reset{color:#94a3b8}
  .reset-pill{background:rgba(120,53,15,.35);border-color:#b45309;color:#fcd34d}
  .resets-extra .exh{color:#fda4af}
  .reset-chip{background:#0f172a;border-color:#1e293b}.reset-chip .reset-when{color:#e2e8f0}
  .reset-chip .reset-what{color:#cbd5e1}.reset-chip .reset-meta{color:#94a3b8}
  .reset-chip.exhausted{background:rgba(136,19,55,.3);border-color:#9f1239}.reset-chip.exhausted .reset-when{color:#fda4af}
  .reset-chip .reset-fc{color:#94a3b8}.reset-chip .reset-fc.warn{color:#fbbf24}
  .reset-chip.will-exhaust{background:rgba(120,53,15,.3);border-color:#b45309}
  .quota-state.unavail{color:#64748b}
  .acct-chip{background:#0f172a;border-color:#334155;color:#e2e8f0}
  .acct-chip.warn{background:rgba(120,53,15,.35);border-color:#b45309;color:#fcd34d}
  .acct-chip.err{background:rgba(136,19,55,.3);border-color:#9f1239;color:#fda4af}
  .quota-card{background:#0f172a;border-color:#1e293b}.quota-card.exhausted{border-color:#9f1239}
  .qc-users,.win-type,.win-fc{color:#94a3b8}.win-label{color:#cbd5e1}.qc-exh{color:#fda4af}
  .fresh.ok{background:rgba(6,95,70,.4);color:#6ee7b7}.fresh.stale{background:rgba(120,53,15,.4);color:#fcd34d}.fresh.unavail{background:#1e293b;color:#94a3b8}
  .pod{color:#94a3b8}.pod>summary{color:#38bdf8}
  .quota-gauge[aria-expanded="true"]{background:#1e293b}
  .quota-tip{background:#e2e8f0;color:#0f172a}
  .toolbar button{background:#0f172a;border-color:#1e293b;color:#e2e8f0}
  #toast{background:#e2e8f0!important;color:#0f172a!important}
}
</style>
</head><body class="bg-slate-50 text-slate-800 antialiased">
<div id="toast" class="fixed left-1/2 bottom-5 -translate-x-1/2 bg-slate-800 text-white text-sm px-2 py-1.5 rounded-md shadow-lg opacity-0 pointer-events-none" style="z-index:50" role="status" aria-live="polite">&nbsp;</div>
<div id="app" class="mx-auto px-5 py-6">
  <header class="mb-4">
    <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
      <h1 class="text-xl font-semibold tracking-tight">Monitorování agentů</h1>
      <span id="overall-pill" class="ovr" title="Celkový stav">načítání…</span>
    </div>
    <p class="text-sm text-slate-500 mt-1">Přehled persistentních agentů, fronty práce, kvót poskytovatelů a dostupnosti služeb.</p>
  </header>
  <div id="attn-live" class="sr-only" aria-live="polite" aria-atomic="true"></div>
  <section id="attention" class="attn mb-4" hidden aria-labelledby="attn-h">
    <h2 id="attn-h">Vyžaduje pozornost</h2>
    <ul class="attn-list" role="list"></ul>
    <p class="attn-more text-[11px] text-slate-500" hidden></p>
  </section>
  <div class="toolbar" role="toolbar" aria-label="Ovládání dashboardu">
    <button type="button" id="btn-refresh" title="Obnovit teď">Obnovit</button>
    <button type="button" id="btn-pause" aria-pressed="false" title="Pozastavit automatickou obnovu">Pozastavit obnovu</button>
  </div>
AGENT_TABLES
  <div id="services"></div>
  <p class="text-center text-[11px] text-slate-300" id="footer">automatická obnova</p>
</div>

<template id="svc-tpl">
  <section class="mb-6">
    <div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200" role="button" tabindex="0" aria-expanded="true">
      <span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0" aria-hidden="true"></span>
      <h2 class="svc-name text-base font-semibold"></h2>
      <span class="svc-state ml-auto text-sm font-medium text-slate-400">načítání…</span>
      <span class="svc-chev text-slate-400 text-xs ml-2 select-none" title="sbalit / rozbalit" aria-hidden="true">▾</span>
    </div>
    <p class="svc-detail text-xs text-slate-500 mb-3 px-1" role="status"></p>
    <div class="svc-body">
    <div class="metric-grid mb-3">
      <div class="rounded-lg border border-slate-200 bg-white p-3">
        <p class="text-[11px] uppercase tracking-wide text-slate-400">Doba běhu</p>
        <p class="m-uptime text-lg font-semibold mt-0.5">–</p>
        <p class="text-[11px] text-slate-400">od první kontroly nebo posledního výpadku</p>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-3">
        <p class="text-[11px] uppercase tracking-wide text-slate-400">Dostupnost</p>
        <p class="m-sla text-lg font-semibold mt-0.5">–</p>
        <p class="m-sla-sub text-[11px] text-slate-400">SLA</p>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-3">
        <p class="m-x-label text-[11px] uppercase tracking-wide text-slate-400">Latence</p>
        <p class="m-x text-lg font-semibold mt-0.5">–</p>
        <p class="m-x-sub text-[11px] text-slate-400">kontrola zdraví</p>
      </div>
    </div>
    <div class="svc-rows-wrap rounded-lg border border-slate-200 bg-white overflow-x-auto mb-3" hidden>
      <table class="w-full text-sm"><thead>
        <tr class="text-[11px] uppercase tracking-wide text-slate-400 border-b border-slate-200">
          <th class="text-left font-medium px-2 py-2">Kontrola</th>
          <th class="rows-value-label text-left font-medium px-2 py-2">Adresa</th>
          <th class="text-left font-medium px-2 py-2">Latence</th>
          <th class="text-left font-medium px-2 py-2">Běh</th>
          <th class="text-left font-medium px-2 py-2">Stav</th>
        </tr></thead>
        <tbody class="svc-rows"></tbody>
      </table>
    </div>
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="flex items-center justify-between mb-2 gap-2 flex-wrap">
        <h3 class="text-xs font-semibold text-slate-500">Historie dostupnosti</h3>
        <span class="svc-tl-window text-[11px] text-slate-400"></span>
      </div>
      <div class="svc-timeline flex items-end gap-[2px] h-8" role="img" aria-label="Historie dostupnosti"></div>
      <div class="flex items-center justify-between mt-2 text-[11px] text-slate-400 flex-wrap gap-2">
        <span class="svc-tl-start"></span>
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1"><span class="h-2 w-2 rounded-sm bg-emerald-500" aria-hidden="true"></span>V provozu</span>
          <span class="flex items-center gap-1"><span class="h-2 w-2 rounded-sm bg-rose-500" aria-hidden="true"></span>Výpadek</span>
          <span class="flex items-center gap-1"><span class="h-2 w-2 rounded-sm bg-slate-200" aria-hidden="true"></span>Bez dat</span>
        </div>
        <span>teď</span>
      </div>
    </div>
    </div>
  </section>
</template>

<script>
const STATE = {
  operational: ["bg-emerald-500", "V provozu", "text-emerald-600", "bg-emerald-50 border-emerald-200"],
  outage:      ["bg-rose-500", "Výpadek", "text-rose-600", "bg-rose-50 border-rose-200"],
  nodata:      ["bg-slate-300", "Bez dat / neběží", "text-slate-400", "bg-white border-slate-200"],
  stale:       ["bg-amber-400", "Zastaralá data", "text-amber-600", "bg-amber-50 border-amber-200"],
};
const VENDOR = { "anthropic":"bg-orange-100 text-orange-700", "openai":"bg-emerald-100 text-emerald-700",
  "google":"bg-violet-100 text-violet-700", "gold":"bg-amber-100 text-amber-700",
  "red":"bg-rose-100 text-rose-700", "cursor":"bg-sky-100 text-sky-700",
  "other":"bg-slate-100 text-slate-600" };
const NAME_BG = { "red":"bg-rose-100", "gold":"bg-amber-100", "green":"bg-emerald-100", "blue":"bg-sky-100" };
const LOCALE="cs-CZ";
const SERVICE_LABELS={
  "Multi-Agent System Availability":"Dostupnost multiagentního systému",
  "Multi-agent system availability":"Dostupnost multiagentního systému",
  "Telegram Bridge":"Telegram most",
  "LANA Hermes Telegram Lane":"LANA Hermes Telegram lane",
};
function serviceDisplayName(name){return SERVICE_LABELS[name]||name;}
function fmtDuration(s){if(s==null)return "–";const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d} d ${h} h`:h?`${h} h ${m} min`:`${m} min`;}
function fmtTime(u){return u?new Date(u*1000).toLocaleString(LOCALE):"";}
function fmtLat(ms){return ms==null?"–":(ms<1?"<1 ms":ms+" ms");}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
const q=(r,s)=>r.querySelector(s);
let paused=false, pollTimer=null;
let tipSeq=0;
let clockSkew=0;   // server time − browser time, so countdowns follow the host clock
function nowSec(){ return Date.now()/1000+clockSkew; }
function fmtIn(sec){
  sec=Math.max(0,Math.round(sec));
  if(sec<60) return "méně než minuta";
  const d=Math.floor(sec/86400), h=Math.floor(sec%86400/3600), m=Math.floor(sec%3600/60);
  if(d) return `${d} d ${h} h`;
  if(h) return `${h} h ${m} min`;
  return `${m} min`;
}
function fmtShort(sec){
  sec=Math.max(0,Math.round(sec));
  const d=Math.floor(sec/86400), h=Math.floor(sec%86400/3600), m=Math.floor(sec%3600/60);
  return d?`${d}d${h}h`:h?`${h}h${String(m).padStart(2,"0")}`:`${Math.max(1,m)}m`;
}
function untilTxt(ts){ const s=ts-nowSec(); return s<=0?"probíhá reset…":"za "+fmtIn(s); }
function tickResets(){
  document.querySelectorAll("[data-reset-at]").forEach(el=>{
    const ts=Number(el.dataset.resetAt); if(!isFinite(ts)) return;
    if(el.dataset.short) el.textContent="↻ "+(ts<=nowSec()?"teď":fmtShort(ts-nowSec()));
    else el.textContent=(el.dataset.resetLabel?`reset ${el.dataset.resetLabel} · `:"")+untilTxt(ts);
  });
}
setInterval(tickResets, 30000);
// Burn-rate forecast (server: agentsmon/quota.py) → short Czech phrase, "" when unknown.
function fcClock(ts){
  const d=new Date(ts*1000), n=new Date(nowSec()*1000);
  const hm=d.toLocaleTimeString(LOCALE,{hour:"2-digit",minute:"2-digit"});
  return d.toDateString()===n.toDateString()?hm:d.toLocaleDateString(LOCALE,{day:"numeric",month:"numeric"})+" "+hm;
}
function fcText(f){
  if(!f) return "";
  if(f.will_exhaust_before_reset&&f.exhaust_at) return `tempem vyčerpáno ~${fcClock(f.exhaust_at)} (před resetem)`;
  if(f.projected_at_reset!=null) return `při resetu ~${Math.round(Number(f.projected_at_reset))} %`;
  if(f.exhaust_at) return `tempem vyčerpáno ~${fcClock(f.exhaust_at)}`;
  return "";
}
function initResetsPanel(){
  // Collapsed by default on phones (<640px), open on larger screens; the user's choice sticks.
  const d=document.querySelector("#resets-panel .resets-details"); if(!d) return;
  let v=null; try{ v=localStorage.getItem(collapseKey("resets")); }catch(e){}
  d.open=v==null?!(window.matchMedia&&window.matchMedia("(max-width:639px)").matches):v==="0";
}
document.addEventListener("click",e=>{
  const s=e.target.closest("#resets-panel summary"); if(!s) return;
  const d=s.parentElement;
  setTimeout(()=>{ try{ localStorage.setItem(collapseKey("resets"), d.open?"0":"1"); }catch(_){} },0);
});
function renderResets(list){
  const box=document.getElementById("resets-panel"); if(!box) return;
  const items=list||[]; box.hidden=!items.length;
  if(items.length){
    const n=items[0], ex=items.filter(e=>e.exhausted);
    box.querySelector(".resets-next").innerHTML=`${esc(n.source)} · ${esc(n.label)} <span class="tabular-nums" data-reset-at="${Number(n.reset_at)}">${esc(untilTxt(n.reset_at))}</span>`;
    box.querySelector(".resets-extra").innerHTML=
      (ex.length?`<span class="exh">⚠ vyčerpáno: ${esc(ex.map(e=>e.source+" · "+e.label).join(", "))}</span> · `:"")+
      `<span class="text-slate-400">${items.length} ${plural(items.length,"okno","okna","oken")}</span>`;
  }
  box.querySelector(".resets-list").innerHTML=items.map(e=>{
    const used=(e.percent==null)?"":`${Number(Number(e.percent).toFixed(1))} % využito`;
    const lead=e.exhausted?"vyčerpáno · uvolní se":"reset";
    const fc=e.exhausted?"":fcText(e.forecast);
    const warn=!!(fc&&e.forecast&&e.forecast.will_exhaust_before_reset);
    const tip=[e.source,e.label,used,lead+" "+(e.reset_label||""),fc].filter(Boolean).join(" · ");
    return `<li class="reset-chip${e.exhausted?" exhausted":warn?" will-exhaust":""}" title="${esc(tip)}">`+
      `<span class="reset-when" data-reset-at="${Number(e.reset_at)}">${esc(untilTxt(e.reset_at))}</span>`+
      `<span class="reset-what">${esc(e.source)} · ${esc(e.label)}</span>`+
      `<span class="reset-meta">${esc(lead)} ${esc(e.reset_label||"")}${used?" · "+esc(used):""}</span>`+
      (fc?`<span class="reset-fc${warn?" warn":""}">${warn?"⚠ ":""}${esc(fc)}</span>`:"")+`</li>`;
  }).join("");
}

function renderTimeline(root, buckets, windowDays){
  const tl=q(root,".svc-timeline"); tl.innerHTML=""; const GREEN=98;
  buckets.forEach(b=>{
    const el=document.createElement("div"); let cls,status;
    if(b.uptime_pct==null){cls="bg-slate-200";status="bez dat";}
    else if(b.uptime_pct>=GREEN){cls="bg-emerald-500";status=`${b.uptime_pct} % dostupnost`;}
    else{cls="bg-rose-500";status=`výpadek (${b.uptime_pct} % dostupnost)`;}
    el.className="bar flex-1 rounded-sm h-full "+cls;
    el.title=`${new Date(b.start*1000).toLocaleDateString(LOCALE)} · ${status}`;
    tl.appendChild(el);
  });
  q(root,".svc-tl-window").textContent="posledních "+windowDays+" dní";
  if(buckets.length) q(root,".svc-tl-start").textContent=new Date(buckets[0].start*1000).toLocaleDateString(LOCALE);
}
function renderService(root, s){
  const st=STATE[s.state]||STATE.nodata;
  const detail=q(root,".svc-detail");
  if(detail) detail.textContent=(s.stale?`Poslední data před ${fmtDuration(s.data_age)} (${fmtTime(s.last_ts)}) – hodnoty níže nejsou aktuální. `:"")+(s.detail||"");
  q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+st[3];
  q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+st[0];
  const se=q(root,".svc-state"); se.textContent=st[1]; se.className="svc-state ml-auto text-sm font-medium "+st[2];
  q(root,".m-uptime").textContent=fmtDuration(s.uptime_seconds);
  q(root,".m-sla").textContent=s.sla!=null?s.sla.toFixed(2)+" %":"–";
  q(root,".m-sla-sub").textContent="za "+s.sla_window_days+" dní ("+(s.sla_samples||0)+" vzorků)";
  if(s.metric==="agents"){
    q(root,".m-x-label").textContent="Agenti";
    q(root,".m-x").textContent=(s.metric_value!=null?s.metric_value:"–")+(s.metric_value!=null?" běží":"");
    q(root,".m-x-sub").textContent="živě v tmux";
  }else if(s.metric==="system_latency"){
    q(root,".m-x-label").textContent="Průměrná latence";
    q(root,".m-x").textContent=fmtLat(s.system_latency_ms);
    q(root,".m-x-sub").textContent=(s.system_latency_n||0)+" zdroj"+(s.system_latency_n===1?"":"ů");
  }else if(s.metric==="avg_latency"){
    q(root,".m-x-label").textContent="Průměrná latence";
    q(root,".m-x").textContent=fmtLat(s.avg_latency_ms);
    q(root,".m-x-sub").textContent="za "+s.sla_window_days+" dní";
  }else{
    q(root,".m-x-label").textContent="Latence";
    q(root,".m-x").textContent=fmtLat(s.latency_ms);
    q(root,".m-x-sub").textContent=s.metric_sub||"kontrola zdraví";
  }
  renderTimeline(root, s.timeline, s.timeline_days);
  renderRows(root, s.rows||[], s.row_details===true);
}
function renderRows(root, rows, details=false){
  const wrap=q(root,".svc-rows-wrap"); if(!wrap) return;
  wrap.hidden=!rows.length; if(!rows.length) return;
  q(root,".rows-value-label").textContent=details?"Detail":"Adresa";
  const tb=q(root,".svc-rows");
  tb.innerHTML=rows.map(r=>{
    const st=STATE[r.state]||STATE.nodata;
    const addr=r.url?r.url.replace(/^https?:\/\//,"").replace(/\/$/,""):"";
    const staleTip=r.stale?`zastaralá data: poslední před ${fmtDuration(r.data_age)} · `:"";
    const sla=r.sla!=null?` · ${r.sla.toFixed(2)} % / ${r.sla_samples||0} vzorků`:"";
    return `<tr class="border-b border-slate-100 last:border-0">`+
      `<td class="px-2 py-1.5 font-medium text-slate-700 whitespace-nowrap">${esc(r.name)}</td>`+
      `<td class="px-2 py-1.5"><span class="text-xs text-slate-500 ${details?'break-words':'font-mono whitespace-nowrap'}">${esc(details?(r.detail||''):addr)}</span></td>`+
      `<td class="px-2 py-1.5 text-slate-600 text-xs whitespace-nowrap">${fmtLat(r.latency_ms)}</td>`+
      `<td class="px-2 py-1.5 text-slate-600 text-xs whitespace-nowrap" title="čas od posledního skutečného výpadku${esc(sla)}">${fmtDuration(r.uptime_seconds)}</td>`+
      `<td class="px-2 py-1.5" title="${esc(staleTip)}${esc(r.detail||"")}${r.last_ts?" · "+esc(fmtTime(r.last_ts)):""}"><span class="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium ${st[2]}"><span class="h-2 w-2 rounded-full ${st[0]} shrink-0" aria-hidden="true"></span>${st[1]}</span></td>`+
    `</tr>`;}).join("");
}
function quotaStateClass(state){
  const s=String(state||"").toUpperCase();
  if(s==="OK") return "ok";
  if(s==="STALE"||s==="ZASTARALÉ") return "stale";
  return "unavail";
}
function gaugeMetrics(b){
  const used=Number(b.percent);
  const state=String(b.state||"").toUpperCase();
  const remRaw=b.remaining;
  const remaining=(remRaw==null||remRaw==="")?(isFinite(used)?Math.max(0,Math.min(100,100-used)):null):Number(remRaw);
  const unavail=state==="UNAVAILABLE"||state==="NEDOSTUPNÉ"||state==="N/A"||remaining==null||!isFinite(used);
  const remShow=unavail?0:Math.max(0,Math.min(100,remaining));
  const fillCls=unavail?"bg-slate-300":(used>=90?"bg-rose-500":used>=70?"bg-amber-500":"bg-emerald-500");
  const pctTxt=unavail?"neznámé":`${Number(Number(remaining).toFixed(1))} % zbývá`;
  const usedTxt=unavail?"neznámé":`${Number(used.toFixed(1))} % využito`;
  const reset=b.reset_label?String(b.reset_label):(String(b.title||"").match(/reset\s+([^·|/]+)/i)||[])[1];
  const resetLabel=reset?String(reset).trim():"";
  const resetAt=Number(b.reset_at)>0?Number(b.reset_at):null;
  const resetTxt=reset?`reset ${resetLabel}${resetAt?" · "+untilTxt(resetAt):""}`:"reset —";
  const fcTxt=(unavail||used>=100)?"":fcText(b.forecast);
  const tip=[b.title||"", usedTxt, pctTxt, resetTxt, fcTxt, state?`stav ${state}`:"", b.freshness||""].filter(Boolean).join(" · ");
  const aria=unavail
    ?`${b.label||"kvóta"}: údaj není dostupný${state?`, ${state}`:""}`
    :`${b.label||"kvóta"}: zbývá ${remaining} procent, využito ${used} procent${state?`, stav ${state}`:""}`;
  return {used, state, remaining, unavail, remShow, fillCls, pctTxt, resetTxt, resetLabel, resetAt, tip, aria};
}
function placeTip(btn, tip){
  const r=btn.getBoundingClientRect();
  const pad=8; const tw=Math.min(352, window.innerWidth-2*pad);
  let left=Math.min(Math.max(pad, r.left), window.innerWidth-tw-pad);
  let top=r.bottom+6;
  tip.style.width=tw+"px";
  tip.style.left=left+"px";
  tip.style.top=top+"px";
  tip.style.position="fixed";
  requestAnimationFrame(()=>{
    const tr=tip.getBoundingClientRect();
    if(tr.bottom>window.innerHeight-pad){
      tip.style.top=Math.max(pad, r.top-tr.height-6)+"px";
    }
    if(tr.right>window.innerWidth-pad){
      tip.style.left=Math.max(pad, window.innerWidth-tr.width-pad)+"px";
    }
  });
}
function closeQuotaTips(except){
  document.querySelectorAll(".quota-tip.open").forEach(t=>{
    if(except && t===except) return;
    t.classList.remove("open");
    const btn=document.querySelector(`[aria-describedby="${t.id}"]`);
    if(btn) btn.setAttribute("aria-expanded","false");
  });
}
function renderAgentGauge(b){
  const m=gaugeMetrics(b);
  const id="qtip-"+(++tipSeq);
  const short=m.unavail?"—":`${Math.round(m.remShow)}%`;
  return `<span class="quota-wrap">`+
    `<button type="button" class="quota-gauge" aria-expanded="false" aria-describedby="${id}" title="${esc(m.tip)}" data-quota-tip>`+
      `<span class="quota-name">${esc(b.label||"")}</span>`+
      `<span class="quota-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${m.unavail?0:Math.round(m.remShow)}" aria-valuetext="${esc(m.aria)}" aria-label="${esc(m.aria)}"><span class="quota-fill ${m.fillCls}" style="width:${m.remShow}%"></span></span>`+
      `<span class="sr-only">${esc(m.aria)}</span>`+
      `<span aria-hidden="true" class="text-[11px] text-slate-500 font-semibold tabular-nums">${esc(short)}</span>`+
      (m.resetAt&&!m.unavail?`<span aria-hidden="true" class="quota-until" data-reset-at="${m.resetAt}" data-short="1">↻ ${esc(fmtShort(m.resetAt-nowSec()))}</span>`:"")+
    `</button>`+
    `<span id="${id}" role="tooltip" class="quota-tip">${esc(m.tip)}</span>`+
  `</span>`;
}
function renderQuotaBar(b){
  const m=gaugeMetrics(b);
  return `<div class="quota-row" title="${esc(m.tip)}">`+
    `<span class="quota-name">${esc(b.label||"")}</span>`+
    `<span class="quota-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${m.unavail?0:Math.round(m.remShow)}" aria-valuetext="${esc(m.aria)}" aria-label="${esc(m.aria)}"><span class="quota-fill ${m.fillCls}" style="width:${m.remShow}%"></span></span>`+
    `<span class="quota-pct">${esc(m.pctTxt)}</span>`+
    (m.resetAt?`<span class="quota-reset" data-reset-at="${m.resetAt}" data-reset-label="${esc(m.resetLabel)}">${esc(m.resetTxt)}</span>`
               :`<span class="quota-reset">${esc(m.resetTxt)}</span>`)+
    (m.state?`<span class="quota-state ${quotaStateClass(m.state)}">${esc(m.state)}</span>`:"")+
  `</div>`;
}
function renderCell(cell, {compact=false}={}){
  if(!cell) return "–";
  const tones={ok:"text-emerald-600",warning:"text-amber-600",error:"text-rose-600",muted:"text-slate-400"};
  const bars=cell.bars||[];
  if(bars.length){
    // Persistent agents: exactly one simple gauge per column (5h / Týden).
    const shown=compact?bars.slice(0,1):bars;
    if(compact){
      return `<div class="quota-stack">${shown.map(renderAgentGauge).join("")}</div>`;
    }
    return `<div class="quota-stack" title="${esc(cell.title||"")}">${shown.map(renderQuotaBar).join("")}</div>`;
  }
  return `<span title="${esc(cell.title||"")}" class="${tones[cell.tone]||''} whitespace-nowrap text-xs">${esc(cell.text||"–")}</span>`;
}
// ---- P1: account chips, quota cards, details disclosure ----
let ACCTS={};                 // "table|row" → provider row (one account = one card)
const OPEN_DETAILS=new Set(); // <details data-key> the user opened; survives the 15 s re-render
const QUOTA_STALE_S=7200;
const ISO_RE=/\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g;
function fmtIsoText(t){
  return String(t==null?"":t).replace(ISO_RE,m=>{
    const d=new Date(m.replace(/(\.\d{3})\d+/,"$1"));
    return isNaN(d)?m:d.toLocaleString(LOCALE,{day:"numeric",month:"numeric",year:"numeric",hour:"2-digit",minute:"2-digit"});
  });
}
function detailsBlock(text,key){
  const parts=fmtIsoText(text).split(/\s+·\s+/).map(s=>s.trim()).filter(Boolean);
  return `<details class="pod" data-key="${esc(key)}"${OPEN_DETAILS.has(key)?" open":""}><summary>Podrobnosti</summary>`+
    `<ul class="pod-body">${parts.map(p=>`<li>${esc(p)}</li>`).join("")}</ul></details>`;
}
document.addEventListener("toggle",e=>{
  const d=e.target; if(!d||d.tagName!=="DETAILS"||!d.dataset||!d.dataset.key) return;
  if(d.open) OPEN_DETAILS.add(d.dataset.key); else OPEN_DETAILS.delete(d.dataset.key);
},true);
function allBars(row){
  const out=[]; Object.values((row&&row.cells)||{}).forEach(c=>((c&&c.bars)||[]).forEach(b=>out.push({b,c}))); return out;
}
function isUnavail(b){
  const s=String(b.state||"").toUpperCase();
  return s==="UNAVAILABLE"||s==="NEDOSTUPNÉ"||s==="N/A"||b.percent==null||!isFinite(Number(b.percent));
}
function fmtPct(v){ return Number(Number(v).toFixed(1)).toLocaleString(LOCALE)+" %"; }
function winType(b,cell){
  const l=String(b.label||"").toLowerCase();
  if(/\b5\s*h|5 hr|session/.test(l)) return "5h";
  if(/week|týd|\b7\s*d\b|weekly/.test(l)) return "týden";
  if(/bonus|kredit|credit|trial/.test(l)) return "kredit";
  if(!(Number(b.reset_at)>0)&&/kč|czk|usd|eur|\$|€/i.test(String((cell&&cell.text)||""))) return "kredit";
  if(/month|měs|included|\bapi\b|\bmin\b/.test(l)) return "měsíc";
  const s=Number(b.reset_at)>0?Number(b.reset_at)-nowSec():null;
  if(s!=null) return s>8*86400?"měsíc":s>6*3600?"týden":"5h";
  return "limit";
}
function sameAsType(label,typ){
  const l=String(label).trim().toLowerCase();
  return l===typ||(typ==="týden"&&/^(7d|week|weekly|týden)$/.test(l))||(typ==="5h"&&/^(5h|5 h)$/.test(l));
}
function worstWindow(row){
  let w=null;
  allBars(row).forEach(x=>{ if(isUnavail(x.b)) return; if(!w||Number(x.b.percent)>Number(w.b.percent)) w=x; });
  return w;
}
function renderAccountChip(a){
  const acc=a.quota_account||null;
  const row=acc?ACCTS[acc.table+"|"+acc.row]:null;
  const src=row||(allBars(a).length?a:null);
  if(!src) return `<span class="text-slate-300 text-xs" title="Agent nemá přiřazený účet s kvótou">—</span>`;
  const w=worstWindow(src), name=acc?acc.name:"kvóta";
  const used=w?Number(w.b.percent):null;
  const tone=used==null?"":used>=90?" err":used>=70?" warn":"";
  const lab=w?String(w.b.label||winType(w.b,w.c)):"";
  const users=(acc&&acc.shared_with)||[];
  const shared=users.length>1?"Sdílený účet s: "+users.join(", "):(acc?"Účet používá jen tento agent":"");
  const lines=allBars(src).map(x=>`${x.b.label||winType(x.b,x.c)}: ${isUnavail(x.b)?"neznámé":"využito "+fmtPct(x.b.percent)}${x.b.reset_label?" · reset "+x.b.reset_label:""}`);
  const tip=[name,...lines,shared,acc?"Kliknutím přejdete na účet v sekci kvót.":""].filter(Boolean).join("\n");
  const resetAt=w&&Number(w.b.reset_at)>0?Number(w.b.reset_at):null;
  const aria=`${name}: nejvytíženější okno ${lab||"neznámé"}, využito ${used==null?"neznámé":fmtPct(used)}. ${shared}`;
  const inner=`<span class="acct-name">${esc(name)}</span><span aria-hidden="true">▸</span>`+
    `<span class="acct-win">${esc(lab)} ${used==null?"?":esc(fmtPct(used))}</span>`+
    (used!=null&&used>=90?`<span aria-hidden="true">⚠</span>`:"")+
    (resetAt?`<span aria-hidden="true" class="quota-until" data-reset-at="${resetAt}" data-short="1">↻ ${esc(fmtShort(resetAt-nowSec()))}</span>`:"");
  return acc
    ?`<a class="acct-chip${tone}" href="#${esc(acc.anchor)}" data-acct="${esc(acc.anchor)}" title="${esc(tip)}" aria-label="${esc(aria)}">${inner}</a>`
    :`<span class="acct-chip${tone}" title="${esc(tip)}" aria-label="${esc(aria)}">${inner}</span>`;
}
function freshness(r){
  const bars=allBars(r).map(x=>x.b);
  const st=bars.map(b=>String(b.state||"").toUpperCase());
  const status=String(r.status||"");
  const reason=status&&!/^(ok|aktuální záznam)$/i.test(status)?status:"";
  if(r.alive===false||(bars.length&&bars.every(isUnavail)))
    return {cls:"unavail",txt:"NEDOSTUPNÉ",reason:reason||"poskytovatel nevrátil data"};
  if(st.some(s=>s==="STALE"||s==="ZASTARALÉ")||(r.age!=null&&r.age>QUOTA_STALE_S))
    return {cls:"stale",txt:"ZASTARALÉ",reason:r.age!=null?"data před "+fmtDuration(r.age):"plugin hlásí zastaralá data"};
  return {cls:"ok",txt:r.age==null?"aktuální":(r.age<60?"před chvílí":"před "+fmtDuration(r.age)),reason:""};
}
function renderWindow(x){
  const b=x.b, m=gaugeMetrics(b), typ=winType(b,x.c);
  const lab=String(b.label||""), showLab=lab&&!sameAsType(lab,typ);
  const fc=(m.unavail||m.used>=100)?"":fcText(b.forecast);
  const warn=!!(fc&&b.forecast&&b.forecast.will_exhaust_before_reset);
  const credit=(typ==="kredit"&&x.c&&x.c.text&&!/^ok$/i.test(x.c.text))?String(x.c.text):"";
  const resetHtml=m.resetAt
    ?`<span class="quota-reset" data-reset-at="${m.resetAt}" data-reset-label="${esc(m.resetLabel)}">${esc(m.resetTxt)}</span>`
    :((b.reset_label||credit)?`<span class="quota-reset">${b.reset_label?(typ==="kredit"?"platí do ":"reset ")+esc(b.reset_label):""}${b.reset_label&&credit?" · ":""}${esc(credit)}</span>`:"");
  return `<div class="qc-win" title="${esc(fmtIsoText(b.title||""))}">`+
    `<span class="win-type">${esc(typ)}</span><span class="win-label">${showLab?esc(lab):""}</span>`+
    `<span class="quota-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${m.unavail?0:Math.round(m.remShow)}" aria-valuetext="${esc(m.aria)}" aria-label="${esc(typ+" "+lab+": "+m.aria)}"><span class="quota-fill ${m.fillCls}" style="width:${m.remShow}%"></span></span>`+
    `<span class="quota-pct">${esc(m.pctTxt)}</span>`+resetHtml+
    (fc?`<span class="win-fc${warn?" warn":""}">${warn?"⚠ ":""}${esc(fc)}</span>`:"")+
  `</div>`;
}
function plural(n,one,few,many){ return n===1?one:(n>=2&&n<=4)?few:many; }
function renderQuotaCard(r,table){
  const f=freshness(r), wins=allBars(r);
  const exh=wins.filter(x=>!isUnavail(x.b)&&Number(x.b.percent)>=100);
  const users=r.used_by||[];
  const tones={ok:"text-emerald-600",warning:"text-amber-600",error:"text-rose-600",muted:"text-slate-400"};
  const textOnly=Object.values(r.cells||{}).filter(c=>c&&!(c.bars&&c.bars.length)&&c.text);
  const acts=renderActions(r,table,{lock:false});
  return `<li class="quota-card${exh.length?" exhausted":""}"${r.anchor?` id="${esc(r.anchor)}"`:""} tabindex="-1">`+
    `<div class="qc-head"><span class="qc-name">${esc(r.name)}</span>`+
    (r.label?`<span class="inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${VENDOR[r.vendor]||VENDOR.other}">${esc(r.label)}</span>`:"")+
    `<span class="fresh ${f.cls}" title="${esc(f.reason||"stáří dat od poslední aktualizace poskytovatele")}">${esc(f.txt)}${f.reason?" · "+esc(f.reason):""}</span></div>`+
    (exh.length?`<p class="qc-exh">⚠ vyčerpáno: ${esc(exh.map(x=>x.b.label||winType(x.b,x.c)).join(", "))}</p>`:"")+
    `<p class="qc-users">${users.length?esc(plural(users.length,"používá: ","používají: ","používají: ")+users.join(", ")):"nepřiřazeno žádnému agentovi"}</p>`+
    `<div class="qc-wins">${wins.map(renderWindow).join("")}`+
      textOnly.map(c=>`<div class="text-xs ${tones[c.tone]||""}">${esc(c.text)}</div>`).join("")+`</div>`+
    (acts?`<div class="qc-actions">${acts}</div>`:"")+
    (r.status_detail?detailsBlock(r.status_detail,"q:"+(r.anchor||r.id||r.name)):"")+
  `</li>`;
}
function renderQuotaSection(root,t){
  const rows=t.rows||[];
  const exh=rows.filter(r=>allBars(r).some(x=>!isUnavail(x.b)&&Number(x.b.percent)>=100)).length;
  const bad=t.up===false||!!t.error;
  const head=q(root,".svc-head"), dot=q(root,".svc-dot"), cnt=q(root,".agents-count");
  const look=bad?STATE.outage:exh?["bg-amber-400","","text-amber-600","bg-amber-50 border-amber-200"]:STATE.operational;
  head.className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+look[3];
  dot.className="svc-dot h-3 w-3 rounded-full shrink-0 "+look[0];
  cnt.className="agents-count ml-auto text-sm font-medium "+look[2];
  cnt.textContent=`${rows.length} ${plural(rows.length,"účet","účty","účtů")}`+(exh?` · ${exh} ${plural(exh,"vyčerpaný","vyčerpané","vyčerpaných")}`:"");
  q(root,".quota-cards").innerHTML=rows.length?rows.map(r=>renderQuotaCard(r,root.dataset.svc)).join("")
    :`<li class="text-sm text-slate-400">${esc(t.error||"Žádné účty.")}</li>`;
  const note=q(root,".qc-note"), txt=[t.error,t.note].filter(Boolean).join(" ");
  note.hidden=!txt; q(note,".agents-note").textContent=txt;
}
document.addEventListener("click",e=>{
  const a=e.target.closest("a[data-acct]"); if(!a) return;
  const el=document.getElementById(a.dataset.acct); if(!el) return;
  e.preventDefault();
  const sec=el.closest("section"); if(sec&&sec.dataset.collapsed==="1") setCollapsed(sec,false,true);
  const reduce=window.matchMedia&&window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({behavior:reduce?"auto":"smooth",block:"center"});
  el.classList.add("flash"); try{ el.focus({preventScroll:true}); }catch(_){}
  setTimeout(()=>{ const n=document.getElementById(a.dataset.acct); if(n) n.classList.remove("flash"); },2200);
  try{ history.replaceState(null,"","#"+a.dataset.acct); }catch(_){}
});
function cellFor(agent, col){
  const cells=agent.cells||{};
  if(cells[col.id]) return cells[col.id];
  for(const id of (col.aliases||[])){ if(cells[id]) return cells[id]; }
  return null;
}
function renderColumns(root,columns,quotaCol=false){
  const head=q(root,"thead tr");
  head.querySelectorAll(".plugin-column").forEach(n=>n.remove());
  const titles=columns.map(c=>c.title).concat(quotaCol?["Kvóta"]:[]);
  titles.forEach(t=>{const th=document.createElement("th");th.className="plugin-column text-left font-medium px-2 py-2";th.textContent=t;head.insertBefore(th,head.lastElementChild);});
}
function renderActions(a,table,{lock=true}={}){
  const stopped=a.status==="Stopped (manual)"||a.status==="Zastaveno (ručně)";
  const defs=a.plugin?(a.row_actions||[]):(a.actions===false?[]:[{id:"restart",label:"Restartovat "+a.name,icon:"restart"},
    stopped?{id:"stop",label:"Odebrat "+a.name,icon:"stop",confirm:`Odebrat agenta „${a.name}“ z Monitorování agentů? Soubory konverzace zůstanou na disku.`}
           :{id:"stop",label:"Zastavit "+a.name,icon:"stop"}]);
  const paths={restart:'M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6',stop:'M18 6 6 18M6 6l12 12',run:'M8 5l11 7-11 7z'};
  const buttons=defs.map(x=>x.icon==='reset'
    ?`<button type="button" class="agent-act reset-pill" data-act="${esc(x.id)}" data-name="${esc(a.name)}" ${a.plugin?`data-table="${esc(table)}" data-row="${esc(a.id)}"`:''} data-confirm="${esc(x.confirm||'')}" title="${esc(x.confirm||x.label)}" aria-label="${esc(x.label)}">⟲ ${esc(x.label)}</button>`
    :`<button type="button" class="agent-act inline-flex align-middle p-1 rounded text-slate-400 ${x.icon==='stop'?'ml-0.5 hover:text-rose-600 hover:bg-rose-50':'hover:text-sky-600 hover:bg-slate-100'}" data-act="${esc(x.id)}" data-name="${esc(a.name)}" ${a.plugin?`data-table="${esc(table)}" data-row="${esc(a.id)}"`:''} data-confirm="${esc(x.confirm||'')}" title="${esc(x.label)}" aria-label="${esc(x.label)}"><svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[x.icon]||paths.run}"/></svg></button>`).join("");
  return buttons+(lock&&(a.actions===false||a.actions_locked)?'<span class="text-xs text-slate-300 ml-0.5 align-middle" title="Akce řídí poskytovatel">🔒</span>':'');
}
function renderAgents(root, agents, summary=null, columns=[]){
  // Persistent agents: quota windows live once, in the account cards; each agent gets one account
  // chip instead of duplicated 5h/Týden gauges. Columns with any non-quota (text) cell remain.
  const isAgents=root.dataset.svc==="agents";
  const quotaCol=isAgents&&agents.some(a=>a.quota_account||allBars(a).length);
  if(isAgents) columns=columns.filter(c=>agents.some(a=>{const x=cellFor(a,c); return x&&!(x.bars&&x.bars.length);}));
  renderColumns(root, columns, quotaCol);
  const tb=q(root,".agent-rows");
  const on=agents.some(a=>a.alive&&!a.needs_login); const running=agents.filter(a=>a.alive&&!a.needs_login).length;
  q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+(on?"bg-emerald-50 border-emerald-200":"bg-white border-slate-200");
  q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+(on?"bg-emerald-500":"bg-slate-300");
  const cnt=q(root,".agents-count");
  cnt.textContent=`${running}/${agents.length} v pořádku`;
  cnt.className="agents-count ml-auto text-sm font-medium "+(agents.length&&running===agents.length?"text-emerald-600":running?"text-amber-600":"text-slate-400");
  if(summary){
    const state=summary.up?STATE.operational:(summary.rows.length||summary.error?STATE.outage:STATE.nodata);
    q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+state[3];
    q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+state[0];
    cnt.textContent=`${running}/${agents.length} v pořádku`;
    cnt.className="agents-count ml-auto text-sm font-medium "+state[2];
    q(root,".agents-note").textContent=summary.error||summary.note||"";
  }
  if(!agents.length){tb.innerHTML=`<tr><td colspan="${6+columns.length+(quotaCol?1:0)}" class="px-3 py-3 text-slate-400">${esc(summary?(summary.error||"Žádní agenti."):"Žádné tmux relace.")}</td></tr>`;return;}
  tb.innerHTML="";
  const svgIcon=(d)=>`<svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"/></svg>`;
  const ICO_SESSION=svgIcon("M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71");
  const ICO_PORT=svgIcon("M4 9h16M4 15h16M10 3v18M14 3v18");
  const ICO_NONE=svgIcon("M12 8v4M12 16h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0");
  agents.forEach(a=>{
    const tcls=VENDOR[a.vendor]||"bg-slate-100 text-slate-600";
    const copy=a.resume_cmd||a.session_id||"";
    let sid;
    if(a.session_id){
      const tip=[a.session_id, a.session_detail||"", copy&&copy!==a.session_id?copy:"", "kliknutím zkopírujete resume"].filter(Boolean).join(" · ");
      sid=`<button type="button" class="sid-btn" title="${esc(tip)}" aria-label="Kopírovat relaci ${esc(a.session_id)}" data-copy="${esc(copy)}">${ICO_SESSION}</button>`;
    }else if(a.kind==="daemon" && a.port){
      sid=`<span class="sid-btn sid-muted" title="port brány :${esc(a.port)}" aria-label="Port ${esc(a.port)}">${ICO_PORT}</span>`;
    }else{
      sid=`<span class="sid-btn sid-muted" title="bez relace" aria-label="Bez relace">${ICO_NONE}</span>`;
    }
    const ok=a.alive&&!a.needs_login; const dot=a.needs_login?"bg-amber-500":(ok?"bg-emerald-500":(a.plugin?"bg-rose-500":"bg-slate-300"));
    const statusTxt=a.status||(ok?"Běží":"Nečinný");
    const longDetail=String(a.status_detail||"").length>90;
    const statusTip=[longDetail?a.status:(a.status_detail||a.status),
      ok&&a.latency_ms!=null?`latence ${fmtLat(a.latency_ms)}`:""].filter(Boolean).join(" · ");
    const stCls=a.needs_login?"text-amber-600":(ok?"text-slate-600":"text-slate-400");
    const tr=document.createElement("tr"); tr.className="border-b border-slate-100 last:border-0";
    const nameBg=NAME_BG[a.name_color];
    const nameInner=nameBg?`<span class="inline-block rounded px-1.5 py-0.5 ${nameBg}">${esc(a.name)}</span>`:esc(a.name);
    const attachCmd=`tmux attach -t "${a.tmux_session||a.name}"`;
    const nameCell=(a.kind!=="daemon")
      ? `<span class="agent-name cursor-pointer hover:text-sky-600" data-copy="${esc(attachCmd)}" title="${esc(attachCmd)} (kliknutím zkopírujete)">${nameInner}</span>`
      : nameInner;
    const tg=a.telegram_url?` <a href="${esc(a.telegram_url)}" target="_blank" rel="noopener" title="Otevřít @${esc(a.telegram_bot)} v Telegramu" aria-label="Telegram @${esc(a.telegram_bot)}" class="inline-flex align-middle ml-1 hover:opacity-70"><svg viewBox="0 0 24 24" class="h-4 w-4" fill="#229ED9" aria-hidden="true"><path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.27 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71L12.6 16.3l-1.99 1.93c-.23.23-.42.42-.83.42z"/></svg></a>`:"";
    const recovery=a.restart_warning?`<br><span class="text-[11px] text-amber-600" title="${esc(a.restart_warning)}">⚠ Nová konverzace</span>`:"";
    tr.innerHTML=
      `<td class="px-2 py-1.5 font-medium text-slate-700 whitespace-nowrap">${nameCell}${tg}${recovery}</td>`+
      `<td class="px-2 py-1.5 whitespace-nowrap"><span title="${esc(a.account?'Účet: '+a.account:a.label)}" class="inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${tcls}">${esc(a.label)}</span>${a.metadata_error?'<br><span class="text-[11px] text-amber-600">Neúplná data</span>':""}</td>`+
      `<td class="px-2 py-1.5">${sid}</td>`+
      `<td class="px-2 py-1.5 text-slate-500 text-xs whitespace-nowrap">${a.age!=null?"před "+fmtDuration(a.age):"–"}</td>`+
      `<td class="px-2 py-1.5"><span title="${esc(fmtIsoText(statusTip))}" class="inline-flex items-center gap-1.5 whitespace-nowrap ${stCls}"><span class="h-2 w-2 rounded-full ${dot} shrink-0" aria-hidden="true"></span><span class="flex flex-col">${esc(statusTxt)}</span></span>${longDetail?detailsBlock(a.status_detail,"a:"+(root.dataset.svc||"")+":"+(a.id||a.name)):""}</td>`+
      columns.map(c=>`<td class="px-2 py-1.5 text-xs">${renderCell(cellFor(a,c),{compact:isAgents})}</td>`).join("")+
      (quotaCol?`<td class="px-2 py-1.5">${renderAccountChip(a)}</td>`:"")+
      `<td class="px-2 py-1.5 text-right whitespace-nowrap">${renderActions(a, root.dataset.svc)}</td>`;
    tb.appendChild(tr);
  });
}
document.addEventListener("click",e=>{
  const btn=e.target.closest("[data-quota-tip]");
  if(!btn){ closeQuotaTips(); return; }
  const tip=document.getElementById(btn.getAttribute("aria-describedby"));
  if(!tip) return;
  const open=btn.getAttribute("aria-expanded")==="true";
  closeQuotaTips(open?null:tip);
  if(open){ btn.setAttribute("aria-expanded","false"); tip.classList.remove("open"); return; }
  btn.setAttribute("aria-expanded","true"); tip.classList.add("open"); placeTip(btn, tip);
});
document.addEventListener("keydown",e=>{
  if(e.key==="Escape") closeQuotaTips();
  const btn=e.target.closest("[data-quota-tip]");
  if(!btn||(e.key!=="Enter"&&e.key!==" ")) return;
  e.preventDefault(); btn.click();
});
document.addEventListener("focusin",e=>{
  const btn=e.target.closest("[data-quota-tip]");
  if(!btn) return;
  const tip=document.getElementById(btn.getAttribute("aria-describedby"));
  if(!tip) return;
  closeQuotaTips(tip); btn.setAttribute("aria-expanded","true"); tip.classList.add("open"); placeTip(btn, tip);
});
document.addEventListener("focusout",e=>{
  const btn=e.target.closest("[data-quota-tip]");
  if(!btn) return;
  setTimeout(()=>{
    if(document.activeElement&&document.activeElement.closest("[data-quota-tip]")===btn) return;
    closeQuotaTips();
  },0);
});
window.addEventListener("resize",()=>closeQuotaTips());
window.addEventListener("scroll",()=>closeQuotaTips(), true);
const OPEN_BY_DEFAULT=3;
const collapseKey=n=>"agentsmon.collapsed:"+n;
function setCollapsed(root,collapsed,persist){
  const b=q(root,".svc-body"),c=q(root,".svc-chev"); if(!b) return;
  b.hidden=collapsed; if(c) c.textContent=collapsed?"▸":"▾"; root.dataset.collapsed=collapsed?"1":"0";
  const head=q(root,".svc-head"); if(head) head.setAttribute("aria-expanded", collapsed?"false":"true");
  if(persist){ try{ localStorage.setItem(collapseKey(root.dataset.cname||""),collapsed?"1":"0"); }catch(e){} }
}
function initCollapse(root,name,index){
  if(root.dataset.cname===name) return;
  root.dataset.cname=name; let v=null; try{ v=localStorage.getItem(collapseKey(name)); }catch(e){}
  setCollapsed(root, v==null ? index>=OPEN_BY_DEFAULT : v==="1", false);
}
document.addEventListener("click",e=>{
  const h=e.target.closest(".svc-head"); if(!h||e.target.closest("a,button")) return;
  const root=h.closest("section"); if(!root) return;
  setCollapsed(root, root.dataset.collapsed!=="1", true);
});
document.addEventListener("keydown",e=>{
  if(e.key!=="Enter"&&e.key!==" ") return;
  const h=e.target.closest(".svc-head"); if(!h||e.target!==h) return;
  e.preventDefault();
  const root=h.closest("section"); if(!root) return;
  setCollapsed(root, root.dataset.collapsed!=="1", true);
});
// ---- Fronta práce (server: agentsmon/workqueue.py, read-only) ----
const Q_TAB_KEY="agentsmon.queue.tab";
const Q_TABS=[["aktivni","Aktivní"],["problem","Blokováno/Selhalo"],["nezarazene","Nezařazené"],["hotovo","Hotovo 24 h"],["vse","Vše"]];
const Q_STATE={running:["▶","Běží"],queued:["◷","Ve frontě"],awaiting_pickup:["⧗","Čeká na převzetí"],blocked:["⛔","Blokováno"],failed:["✕","Selhalo"],done:["✓","Hotovo"]};
const CI_ICON={success:["✓","CI úspěšné"],failure:["✕","CI selhalo"],pending:["…","CI probíhá"]};
let QTAB=(()=>{ try{ const v=localStorage.getItem(Q_TAB_KEY); return Q_TABS.some(t=>t[0]===v)?v:"aktivni"; }catch(e){ return "aktivni"; } })();
let QFILTER=null;   // "state:<s>" from a header counter; overrides the tab until cleared
let QDATA=null;
function ghUrl(u){ return (typeof u==="string"&&/^https:\/\/github\.com\//.test(u))?u:null; }
function ghLink(u,html,cls,label){
  const s=ghUrl(u);
  return s?`<a class="${cls||""}" href="${esc(s)}" target="_blank" rel="noopener noreferrer"${label?` aria-label="${esc(label)}"`:""}>${html}</a>`:html;
}
function fmtAgo(s){
  if(s==null||!isFinite(Number(s))) return "–";
  s=Math.max(0,Math.round(Number(s))); if(s<60) return "před chvílí";
  const d=Math.floor(s/86400),h=Math.floor(s/3600),m=Math.floor(s/60);
  return d?`před ${d} d`:h?`před ${h} h`:`před ${m} min`;
}
function qMatch(it,view){
  if(view&&view.startsWith("state:")) return !!it.tracked&&it.state===view.slice(6);
  if(view==="nezarazene") return !it.tracked;
  if(!it.tracked) return false;               // untracked items live only in „Nezařazené“
  if(view==="aktivni") return it.state==="running"||it.state==="queued"||it.state==="awaiting_pickup";
  if(view==="problem") return it.state==="blocked"||it.state==="failed";
  if(view==="hotovo") return it.state==="done"&&!(Number(it.age_seconds)>86400);
  return true;
}
function qItem(it){
  const st=Q_STATE[it.state]||["•",it.state];
  const label=it.state_label_cs||st[1];
  const t=it.task||null, pr=it.pr||null;
  const key="qi:"+(it.kind||"")+":"+(it.issue||(t&&t.id)||it.title||"");
  const num=it.issue?ghLink(it.url,`#${esc(it.issue)}`,"q-link",`${it.kind==="pr"?"PR":"Issue"} #${it.issue} na GitHubu`)
    :`<span class="text-slate-400" title="${esc((t&&t.id)||"")}">—</span>`;
  const title=ghUrl(it.url)
    ?`<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer" title="${esc(it.title)}">${esc(it.title)}</a>`
    :`<span class="q-tt" title="${esc(it.title)}">${esc(it.title)}</span>`;
  const mm=it.mismatch?`<span class="q-mm" tabindex="0" title="${esc(it.mismatch)}" aria-label="Nesoulad: ${esc(it.mismatch)}"><span aria-hidden="true">⚠</span>nesoulad</span>`:"";
  const who=[it.worker,it.profile].filter(Boolean).join(" · ");
  let prHtml="";
  if(pr&&pr.number){
    const ci=CI_ICON[pr.ci];
    prHtml=ghLink(pr.url,`PR #${esc(pr.number)}`,"q-link",`PR #${pr.number}${pr.title?": "+pr.title:""}`)+
      (ci?`<span class="ci ${esc(pr.ci)}" role="img" title="${ci[1]}" aria-label="${ci[1]}">${ci[0]}</span>`:"")+
      (pr.draft?`<span class="q-draft" title="Rozpracovaný PR">Draft</span>`:"")+
      (pr.review==="APPROVED"?`<span class="ci success">schváleno</span>`:pr.review==="CHANGES_REQUESTED"?`<span class="ci failure">změny</span>`:"");
  }
  const tries=t&&(Number(t.attempts)||Number(t.max_retries))?`pokus ${Number(t.attempts)||0}${Number(t.max_retries)?"/"+Number(t.max_retries):""}`:"";
  const det=[];
  if(t){ det.push(`Úloha ${t.id||"?"} · stav ${t.status||"?"}`); if(t.lane) det.push("lane "+t.lane); if(t.error) det.push("chyba: "+t.error); }
  if(it.mismatch) det.push("⚠ "+it.mismatch);
  if((it.labels||[]).length) det.push("štítky: "+it.labels.join(", "));
  if(!it.tracked) det.push("Nezařazeno: bez štítku stavu a bez úlohy supervisoru");
  const details=det.length?`<details class="pod" data-key="${esc(key)}"${OPEN_DETAILS.has(key)?" open":""}><summary>Podrobnosti</summary>`+
    `<ul class="pod-body">${det.map(p=>`<li>${esc(p)}</li>`).join("")}</ul></details>`:"";
  return `<li class="q-item">`+
    `<span class="q-num">${num}</span>`+
    `<span class="q-title">${title}${mm}</span>`+
    `<span class="q-badge ${esc(it.state)}"><span aria-hidden="true">${st[0]}</span>${esc(label)}</span>`+
    `<span class="q-who" title="${esc(who)}">${who?esc(who):"–"}</span>`+
    `<span class="q-pr">${prHtml||'<span class="text-slate-300" aria-label="bez PR">–</span>'}</span>`+
    `<span class="q-age" title="${esc(it.updated_at?"aktualizováno "+fmtTime(it.updated_at):"")}">${esc(fmtAgo(it.age_seconds))}</span>`+
    ((tries||details)?`<div class="q-extra">${tries?`<span>${esc(tries)}</span>`:""}${details}</div>`:"")+
  `</li>`;
}
function renderQueue(qd){
  const root=document.getElementById("fronta"); if(!root) return;
  QDATA=qd||null;
  if(!qd||!qd.enabled){ root.hidden=true; return; }
  root.hidden=false; initCollapse(root,"fronta",1);
  const s=qd.summary||{}, m=qd.meta||{}, items=qd.items||[];
  const n=k=>Number(s[k])||0, slots=n("running_slots_max")||2;
  const look=n("failed")?STATE.outage:(n("blocked")||m.stale&&!qd.loading)?STATE.stale:qd.loading?STATE.nodata:STATE.operational;
  q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+look[3];
  q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+look[0];
  const cnt=q(root,".agents-count"); cnt.className="agents-count ml-auto text-sm font-medium "+look[2];
  const prob=n("failed")+n("blocked");
  cnt.textContent=qd.loading?"načítání…":`běží ${n("running")}/${slots} · čeká ${n("queued")+n("awaiting_pickup")}`+(prob?` · ${prob} ${plural(prob,"problém","problémy","problémů")}`:"");
  const ctrs=[["running",`běží ${n("running")}/${slots}`,""],["queued",`fronta ${n("queued")}`,""],
    ["awaiting_pickup",`čeká ${n("awaiting_pickup")}`,""],["blocked",`blok ${n("blocked")}`,n("blocked")?"warn":""],
    ["failed",`selhalo ${n("failed")}`,n("failed")?"bad":""],["done",`hotovo 24 h ${n("done_24h")}`,""]];
  q(root,".q-counters").innerHTML=ctrs.map((c,i)=>(i?`<span class="q-sep" aria-hidden="true">·</span>`:"")+
    `<button type="button" class="q-ctr ${c[2]}" data-qfilter="${c[0]}" aria-pressed="${QFILTER==="state:"+c[0]}" title="Filtrovat: ${esc(Q_STATE[c[0]][1])}">${esc(c[1])}</button>`).join("");
  const gh=q(root,".q-gh"), repo=ghUrl(m.repo_url);
  gh.hidden=!repo; if(repo){ gh.href=repo; gh.title="Otevřít "+(m.repo||"repozitář")+" na GitHubu"; }
  q(root,".q-tabs").innerHTML=Q_TABS.map(([id,lab])=>{
    const c=items.filter(it=>qMatch(it,id)).length, sel=!QFILTER&&QTAB===id;
    return `<button type="button" role="tab" class="q-tab" data-qtab="${id}" aria-selected="${sel}" aria-controls="q-panel">${esc(lab)} <span class="tabular-nums">(${c})</span></button>`;
  }).join("");
  const parts=[], errs=m.source_errors||[];
  if(m.stale&&!qd.loading) parts.push("Zastaralá data fronty"+(m.last_success_at?` – poslední úspěšné načtení ${fmtAgo(nowSec()-Number(m.last_success_at))}`:""));
  if(errs.length) parts.push("Chyby zdrojů: "+errs.join(" · "));
  const note=q(root,".q-note"); note.hidden=!parts.length; note.textContent=parts.join(". ");
  const view=QFILTER||QTAB, shown=items.filter(it=>qMatch(it,view));
  q(root,".q-list").innerHTML=(qd.loading&&!items.length)?`<li class="q-empty">načítání…</li>`
    :shown.length?shown.map(qItem).join(""):`<li class="q-empty">${QFILTER?"Žádné položky v tomto stavu.":"Žádné položky."}</li>`;
}
function setQueueView(tab,filter){
  if(tab){ QTAB=tab; try{ localStorage.setItem(Q_TAB_KEY,tab); }catch(_){} }
  QFILTER=filter||null; renderQueue(QDATA);
}
document.addEventListener("click",e=>{
  const t=e.target.closest("[data-qtab]");
  if(t){ setQueueView(t.dataset.qtab,null); const b=document.querySelector(`[data-qtab="${t.dataset.qtab}"]`); if(b) b.focus(); return; }
  const c=e.target.closest("[data-qfilter]");
  if(c){ const f="state:"+c.dataset.qfilter; setQueueView(null,QFILTER===f?null:f);
    const b=document.querySelector(`[data-qfilter="${c.dataset.qfilter}"]`); if(b) b.focus(); }
});
// ---- Vyžaduje pozornost + celkový stav (spočítáno na serveru: compute_attention) ----
const ATTN_ICON={critical:["✕","Kritické"],warning:["⚠","Upozornění"],info:["ℹ","Informace"]};
let lastAttnCount=null;
function renderAttention(list,ov){
  const box=document.getElementById("attention"); if(!box) return;
  const items=Array.isArray(list)?list:[], total=Math.max(items.length,Number(ov&&ov.total)||0);
  box.hidden=!items.length;
  box.className="attn mb-4"+(ov&&ov.level==="critical"?" critical":"");
  document.getElementById("attn-h").textContent=`Vyžaduje pozornost (${total})`;
  q(box,".attn-list").innerHTML=items.map(a=>{
    const ic=ATTN_ICON[a.severity]||ATTN_ICON.info, href=typeof a.href==="string"?a.href:"";
    let link="";
    if(/^#[A-Za-z0-9_-]+$/.test(href)){
      const id=href.slice(1);
      link=`<a class="attn-link" href="${esc(href)}" data-attn="${esc(id)}"`+(a.queue_tab?` data-qtab-go="${esc(a.queue_tab)}"`:"")+
        (id.startsWith("kvota-")?` data-acct="${esc(id)}"`:"")+`>Detail →</a>`;
    }else if(ghUrl(href)){
      link=`<a class="attn-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">GitHub ↗</a>`;
    }
    const x=a.action;
    const act=(x&&x.kind==="row_action"&&x.table&&x.row)?renderActions({plugin:true,name:x.name||x.row,id:x.row,
      row_actions:[{id:x.id,label:x.label,icon:x.icon||"reset",confirm:x.confirm||""}]},x.table,{lock:false}):"";
    const cd=Number(a.reset_at)>0?` <span class="tabular-nums text-slate-500" data-reset-at="${Number(a.reset_at)}">${esc(untilTxt(Number(a.reset_at)))}</span>`:"";
    return `<li class="attn-item ${esc(a.severity)}"><span class="attn-ico" aria-hidden="true">${ic[0]}</span>`+
      `<span class="attn-text"><span class="sr-only">${ic[1]}: </span>${esc(a.text_cs)}${cd}</span>${act}${link}</li>`;
  }).join("");
  const more=q(box,".attn-more"); more.hidden=!(total>items.length); more.textContent=`… a další ${total-items.length}`;
  const pill=document.getElementById("overall-pill"), lv=(ov&&ov.level)||"ok";
  pill.className="ovr "+(ov?lv:"");
  pill.innerHTML=ov?`<span aria-hidden="true">${{ok:"✓",warning:"⚠",critical:"✕"}[lv]||"•"}</span>${esc(ov.text_cs||"")}`:"–";
  const cnt=Number(ov&&ov.count)||0;
  document.title=(cnt?`(${cnt}) `:"")+"Monitorování agentů";
  if(lastAttnCount!==null&&cnt!==lastAttnCount)
    document.getElementById("attn-live").textContent=cnt?`Vyžaduje pozornost: ${cnt} ${plural(cnt,"položka","položky","položek")}`:"Vše v provozu";
  lastAttnCount=cnt;
}
document.addEventListener("click",e=>{
  const a=e.target.closest("a[data-attn]"); if(!a||a.dataset.acct) return;   // quota links: data-acct handler
  const el=document.getElementById(a.dataset.attn); if(!el) return;
  e.preventDefault();
  const sec=el.closest("section"); if(sec&&sec.dataset.collapsed==="1") setCollapsed(sec,false,true);
  if(a.dataset.qtabGo) setQueueView(a.dataset.qtabGo,null);
  const reduce=window.matchMedia&&window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({behavior:reduce?"auto":"smooth",block:"start"});
});
async function refresh(){
  try{
    const d=await (await fetch("/api/state",{credentials:"same-origin"})).json();
    if(typeof d.time==="number") clockSkew=d.time-Date.now()/1000;
    renderResets(d.resets);
    try{ renderAttention(d.attention,d.overall); }catch(err){ console.error("attention",err); }
    ACCTS={};
    (d.agent_tables||[]).forEach(t=>{ if(t.kind==="quota") (t.rows||[]).forEach(r=>{ ACCTS[t.id+"|"+r.id]=r; }); });
    const agentsRoot=document.querySelector('section[data-svc="agents"]');
    renderAgents(agentsRoot, d.agents, null, d.columns||[]); initCollapse(agentsRoot,"agents",0);
    try{ renderQueue(d.queue); }catch(err){ console.error("queue",err); }
    const tables=document.getElementById("plugin-tables");
    const wanted=(d.agent_tables||[]).map(t=>t.id+"/"+(t.kind||"")).join("|");
    if(tables.dataset.keys!==wanted){
      tables.innerHTML="";
      (d.agent_tables||[]).forEach(t=>{
        const tplId=t.kind==="quota"?"quota-table-tpl":"agent-table-tpl";
        const root=document.getElementById(tplId).content.firstElementChild.cloneNode(true);
        root.dataset.svc=t.id; tables.appendChild(root);
      });
      tables.dataset.keys=wanted;
    }
    (d.agent_tables||[]).forEach((t,i)=>{
      const root=tables.children[i]; q(root,"h2").textContent=t.title; if(t.anchor) root.id=t.anchor;
      if(t.kind==="quota") renderQuotaSection(root,t); else renderAgents(root,t.rows,t,d.columns||[]);
      initCollapse(root,t.id,i+1);
    });
    document.getElementById("plugin-loading").hidden=!d.plugin_loading;
    const errors=document.getElementById("plugin-errors"); errors.replaceChildren();
    (d.plugin_errors||[]).forEach(p=>{
      const box=document.createElement("div"); box.className="mb-4 border border-rose-200 bg-rose-50 rounded-lg p-3";
      box.textContent=p.name+": "+p.detail; errors.appendChild(box);
    });
    const box=document.getElementById("services"); const tpl=document.getElementById("svc-tpl");
    if(box.childElementCount!==d.services.length){
      box.innerHTML=""; d.services.forEach(()=>box.appendChild(tpl.content.cloneNode(true)));
    }
    const sections=box.querySelectorAll("section");
    d.services.forEach((s,i)=>{ const root=sections[i]; if(s.anchor) root.id=s.anchor; q(root,".svc-name").textContent=serviceDisplayName(s.name); renderService(root,s); initCollapse(root,s.name,i+1+(d.agent_tables||[]).length); });
    document.getElementById("footer").textContent="aktualizováno "+new Date().toLocaleTimeString(LOCALE)+(paused?" · pozastaveno":" · automatická obnova")+` · jádro v${d.core.version} (${d.core.state})`;
  }catch(e){document.getElementById("footer").textContent="spojení ztraceno…";}
}
function copyText(text){
  if(navigator.clipboard && window.isSecureContext){ return navigator.clipboard.writeText(text); }
  const ta=document.createElement("textarea"); ta.value=text; ta.style.position="fixed"; ta.style.opacity="0";
  document.body.appendChild(ta); ta.focus(); ta.select();
  try{ document.execCommand("copy"); }catch(e){} document.body.removeChild(ta);
  return Promise.resolve();
}
let _toastTimer;
function showToast(msg){
  const t=document.getElementById("toast"); if(!t) return;
  t.textContent=msg; t.style.opacity="1";
  clearTimeout(_toastTimer); _toastTimer=setTimeout(()=>{ t.style.opacity="0"; }, 1300);
}
document.addEventListener("click", e=>{
  const el=e.target.closest("[data-copy]"); if(!el) return;
  copyText(el.dataset.copy).then(()=>{
    el.classList.add("copied-flash");
    setTimeout(()=>el.classList.remove("copied-flash"), 700);
    showToast("✓ Zkopírováno do schránky");
  });
});
document.addEventListener("click", async e=>{
  const btn=e.target.closest(".agent-act"); if(!btn) return;
  const name=btn.dataset.name, act=btn.dataset.act;
  if(btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  if(act==="stop" && !btn.dataset.confirm && !confirm(`Zastavit agenta „${name}“? Nebude automaticky restartován, dokud nestisknete ↻.`)) return;
  btn.disabled=true; btn.classList.add("opacity-40");
  showToast((act==="redeem_reset"?"⟲ Uplatňuji reset pro ":btn.dataset.table?"Spouštím akci pro ":act==="restart"?"↻ Restartuji ":"✕ Zastavuji ")+name+"…");
  try{
    const r=await fetch("/api/agent/action",{method:"POST",credentials:"same-origin",
      headers:{"Content-Type":"application/json","X-Agentsmon":"action"},body:JSON.stringify({name,action:act,table:btn.dataset.table,row:btn.dataset.row})});
    const j=await r.json().catch(()=>({}));
    showToast((j&&j.ok?"✓ ":"⚠ ")+((j&&j.message)||(r.ok?"hotovo":"selhalo")));
  }catch(err){ showToast("⚠ "+err); }
  setTimeout(refresh, act==="restart"?2500:600);
});
function schedulePoll(){
  clearInterval(pollTimer);
  if(!paused) pollTimer=setInterval(refresh, POLL*1000);
}
document.getElementById("btn-refresh").addEventListener("click",()=>refresh());
document.getElementById("btn-pause").addEventListener("click",ev=>{
  paused=!paused;
  ev.currentTarget.setAttribute("aria-pressed", paused?"true":"false");
  ev.currentTarget.textContent=paused?"Obnovu spustit":"Pozastavit obnovu";
  schedulePoll();
  showToast(paused?"Automatická obnova pozastavena":"Automatická obnova běží");
  refresh();
});
initResetsPanel(); refresh(); schedulePoll();
</script></body></html>"""


PAGE=PAGE.replace('AGENT_TABLES',
    AGENT_TABLE.replace('KEY','agents').replace('HIDDEN','').replace('TITLE','Persistentní agenti')
        .replace('NOTE','tmux relace s agentem, propojené přes <code>--resume</code> session id')
        .replace('data-svc="agents"','id="agenti" data-svc="agents"')+
    QUEUE_SECTION+
    '<section id="resets-panel" hidden class="mb-6" aria-label="Nejbližší resety kvót">'
    '<details class="resets-details">'
    '<summary class="resets-sum rounded-lg border px-3 py-2 bg-white border-slate-200">'
    '<h2 class="inline text-base font-semibold">Nejbližší reset:</h2> <span class="resets-next text-sm font-medium"></span>'
    '<span class="resets-extra text-xs"></span></summary>'
    '<p class="text-[11px] text-slate-400 my-2">Všechny resety kvót · odpočet podle času serveru · vyčerpaná okna červeně</p>'
    '<ul class="resets-list"></ul></details></section>'+
    '<div id="plugin-tables"></div>'+
    '<div id="plugin-loading" role="status" hidden class="mb-4 border border-slate-200 bg-slate-50 text-slate-600 rounded-lg p-3">'
    'Načítání pluginů: čeká se na ohraničenou kontrolu</div><div id="plugin-errors"></div>'+
    QUOTA_SECTION+
    '<template id="agent-table-tpl">'+AGENT_TABLE.replace('KEY','plugin').replace('HIDDEN','')
        .replace('TITLE','Agenti').replace('NOTE','')+'</template>')


STALE_FACTOR = 3   # last sample older than 3 × its check interval → "Zastaralá data", never green


def _staleness(cur, interval: int, now: float | None = None) -> tuple[str | None, bool, int | None]:
    """(state, stale, age_seconds) for the latest sample. A sample older than STALE_FACTOR × the
    check interval is reported as ``stale`` — a probe that stopped writing must not keep the last
    green value forever (Telegram most showed "V provozu" from data 6 days old)."""
    if not cur:
        return "nodata", False, None
    now = time.time() if now is None else now
    age = max(0, int(now - (cur["ts"] or 0)))
    if age > STALE_FACTOR * max(1, int(interval or 60)):
        return "stale", True, age
    return ("operational" if cur["up"] else "outage"), False, age


def _service_state(cfg: dict, running_agents: int = 0) -> list[dict]:
    interval = int(cfg.get("probe", {}).get("interval_seconds", 60) or 60)
    win_days = int(cfg.get("probe", {}).get("sla_window_days", 90))
    tdays = int(cfg.get("probe", {}).get("timeline_days", 90))
    min_outage = int(cfg.get("probe", {}).get("min_outage_samples", 3))
    out = []
    for s in cfg.get("services", []):
        name = s.get("name")
        if not name:
            continue
        cur = db.last(name)
        sla_pct, samples = db.sla(name, win_days * 86400)
        lat = cur["latency"] if (cur and cur["latency"] is not None) else None
        metric = s.get("metric", "latency")   # "latency" | "agents" (show running-agent count)
        state, stale, data_age = _staleness(cur, s.get("interval_seconds") or interval)
        out.append({
            "name": name,
            "up": bool(cur and cur["up"]),
            "state": state, "stale": stale, "data_age": data_age,
            "detail": cur["detail"] if cur else "no data yet",
            "last_ts": cur["ts"] if cur else None,
            "latency_ms": round(lat * 1000) if lat is not None else None,
            "avg_latency_ms": (lambda a: round(a * 1000) if a is not None else None)(db.avg_latency(name, win_days * 86400)),
            "health_url": s.get("health_url"),
            "metric": metric, "metric_value": running_agents if metric == "agents" else None,
            "metric_sub": s.get("latency_label", "module" if s.get("command") else "health check"),
            "source": s.get("source", "config"),
            "row_details": s.get("row_details") is True,
            "rows": _service_rows(name, win_days, min_outage, s.get("interval_seconds") or interval),
            "uptime_seconds": db.uptime_seconds(name, min_outage),
            "sla": sla_pct, "sla_window_days": win_days, "sla_samples": samples,
            "timeline": db.timeline(name, tdays * 86400, tdays), "timeline_days": tdays,
        })
    return out


def _service_rows(service: str, win_days: int, min_outage: int, interval: int = 60) -> list[dict]:
    """Per-row state for a module that reports ``rows`` (one website/tunnel/db per line). Each row
    is its own sample series ``"<service> › <row>"`` with uptime and SLA like a service."""
    out = []
    for r in probe.service_rows(service):
        rname = service + probe.ROW_SEP + r["name"]
        cur = db.last(rname)
        sla_pct, samples = db.sla(rname, win_days * 86400)
        lat = cur["latency"] if (cur and cur["latency"] is not None) else None
        state, stale, data_age = _staleness(cur, interval)
        out.append({"name": r["name"], "url": r.get("url") or "",
                    "up": bool(cur and cur["up"]),
                    "state": state, "stale": stale, "data_age": data_age,
                    "detail": cur["detail"] if cur else "no data yet", "last_ts": cur["ts"] if cur else None,
                    "latency_ms": round(lat * 1000) if lat is not None else None,
                    "sla": sla_pct, "sla_samples": samples,
                    "uptime_seconds": db.uptime_seconds(rname, min_outage)})
    return out


def _tg_link(value: str) -> tuple[str, str]:
    """Normalise a Telegram reference (``@bot``, ``bot``, or a full ``https://t.me/bot`` URL) into
    (url, username)."""
    v = (value or "").strip()
    if v.startswith("http"):
        return v, v.rstrip("/").split("/")[-1].lstrip("@")
    uname = v.lstrip("@")
    return f"https://t.me/{uname}", uname


def _agents_state(cfg: dict) -> list[dict]:
    """Pinned daemons (OpenClaw/Hermes…) first, then running tmux agents, with config display
    overrides (tag → label, vendor → tag colour) applied."""
    overrides = {a["name"]: a for a in cfg.get("agents", []) if a.get("name")}
    from .identity import restart_warnings
    warnings = restart_warnings(cfg)
    # Sessions listed in `hidden_sessions` are owned by a dedicated module (e.g. the sandbox
    # agents block) and must not also appear in this auto-discovered list, where they get a wrong
    # model tag. Each entry is an exact session name or a `prefix*` glob (reported 2026-09-15).
    hidden = cfg.get("hidden_sessions", []) or []
    def _hidden(name):
        for h in hidden:
            if h.endswith("*") and name.startswith(h[:-1]):
                return True
            if name == h:
                return True
        return False
    tmux = [a for a in detect.configured_agents(cfg)
            if (a["alive"] or a['name'] in overrides) and not _hidden(a["name"])]
    for a in tmux:
        ov = overrides.get(a["name"], {})
        if a['name'] in warnings:
            a['restart_warning'] = warnings[a['name']]
        if ov.get("tag"):
            a["label"] = ov["tag"]
        if ov.get("vendor"):
            a["vendor"] = ov["vendor"]
        if ov.get("restart") and not a.get('identity_conflict') and a.get('session_id'):
            from .identity import explicit_id
            old = explicit_id(ov['restart'], detect.UUID_RE)
            if old == a['session_id']:
                a['resume_cmd'] = ov['restart']
    agents = detect.pinned_agents(cfg.get("pinned_daemons", [])) + tmux
    # Telegram deep-link icon. Two sources, explicit wins:
    #  1) an explicit `telegram` field on an agent/daemon config entry (for daemons with their OWN
    #     native bot, e.g. OpenClaw/Hermes) — value is a @username or a full t.me URL;
    #  2) auto: a tmux session bridged via Agent2Telegram (reads only the non-secret bot username).
    explicit = {d["name"]: d["telegram"] for d in
                (cfg.get("pinned_daemons", []) + cfg.get("agents", []))
                if d.get("name") and d.get("telegram")}
    links = detect.telegram_links()
    for a in agents:
        bot = explicit.get(a.get("name")) or links.get(a.get("name"))
        if bot:
            url, uname = _tg_link(bot)
            a["telegram_bot"] = uname
            a["telegram_url"] = url
    return agents


def _set_enabled(name: str, enabled: bool) -> None:
    """Persist enabled=true/false on the named agent/daemon in the RAW config file (so keepalive
    stops reviving a stopped agent). Targeted edit — preserves the rest of the user's file."""
    path = config.DEFAULT_PATH
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    for key in ("agents", "daemons"):
        for entry in raw.get(key, []):
            if entry.get("name") == name:
                entry["enabled"] = enabled
                changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)


def _remove_agent(name: str) -> bool:
    """Drop a stopped agent's entry from the RAW config file (targeted edit, rest preserved)."""
    path = config.DEFAULT_PATH
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    agents = raw.get("agents", [])
    kept = [a for a in agents if a.get("name") != name]
    if len(kept) == len(agents):
        return False
    raw["agents"] = kept
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return True


def _agent_action(name: str, action: str) -> tuple[bool, str]:
    """Dashboard row actions. action: 'restart' | 'stop'. Returns (ok, message)."""
    cfg = config.load()
    tmux_bin = shutil.which(cfg.get("tmux_bin", "tmux")) or "tmux"
    agent = next((a for a in cfg.get("agents", []) if a.get("name") == name), None)
    daemon = next((d for d in cfg.get("daemons", []) if d.get("name") == name), None)
    if not agent and not daemon:
        return False, f"unknown agent '{name}'"
    if daemon and not daemon.get('manager'):
        from . import managed, wizard
        owner = managed.discover(daemon)
        if owner:
            # bootout removes the live PID. Remember its owner before stopping,
            # so a later restart cannot fall back to a standalone command.
            try:
                raw = json.loads(config.DEFAULT_PATH.read_text())
                entry = next(d for d in raw['daemons'] if d['name'] == name)
                entry['manager'] = owner
                if not wizard._write_json(config.DEFAULT_PATH, raw):
                    raise OSError('save failed')
            except (OSError, ValueError, KeyError, StopIteration):
                return False, 'Could not preserve the service owner; action refused.'
            daemon = {**daemon, 'manager':owner}
    if action == "restart":
        if agent:
            from . import identity
            live = detect.discover_agents(config.agent_matches(cfg))
            pins = identity.pin_sessions(cfg, live)
            agent = identity.safe_restart(agent, live, pins)
            if agent is None:
                return False, 'Restart blocked: configure a safe fresh_start command.'
        # Re-enable (in case it was stopped) and relaunch fresh so it picks up new config (e.g. MCP).
        _set_enabled(name, True)
        if agent:
            identity.record_restart(agent)
            if agent.get('restart_warning'):
                keepalive._log(f"agent '{name}': WARNING: {agent['restart_warning']}")
            keepalive._start(agent, tmux_bin, recreate=True)   # kill session + recreate + resume cmd
            return True, agent.get('restart_warning') or f"restarting {name}"
        from . import managed
        return managed.action(daemon, 'restart')
    if action == "stop":
        # A second stop on an already stopped agent removes it from the dashboard/config.
        # Only the monitoring entry goes; conversation files and the CLI stay untouched.
        if agent and not agent.get("enabled", True) and subprocess.run(
                [tmux_bin, "has-session", "-t", name], capture_output=True, timeout=10).returncode != 0:
            return (True, f"removed {name}") if _remove_agent(name) else (False, f"could not remove {name}")
        # Disable first so keepalive won't revive it, then kill the session / process.
        _set_enabled(name, False)
        if agent:
            subprocess.run([tmux_bin, "kill-session", "-t", name], capture_output=True, timeout=10)
            return True, f"stopped {name}"
        from . import managed
        return managed.action(daemon, 'stop')
    return False, f"unknown action '{action}'"


def _state() -> bytes:
    return json.dumps(_state_data()).encode()


def _state_data() -> dict:
    cfg = config.load()
    agents = _agents_state(cfg)
    for a in agents:
        a['id'] = a.get('name', '')
    extra = plugins.collect(cfg, agents)
    hidden = set(extra['hidden_sessions'])
    agents = [a for a in agents if a.get('name') not in hidden]
    for patch in extra['patches']:
        candidates=[(a['id'],a) for a in agents]
        candidates += [(t['id']+':'+a['id'],a) for t in extra['tables'] for a in t['rows']]
        for identity,a in candidates:
            if identity == patch['row_id']:
                a.setdefault('cells', {}).update(patch.get('cells', {}))
                a.update({k:v for k,v in patch.items() if k not in ('row_id','cells')})
    running = sum(1 for a in agents if a.get("alive"))
    services = _service_state(cfg, running)
    services = [s for s in services if s['name'] not in extra['hidden_services']]
    # System-wide current latency = average across every component with a health endpoint
    # (pinned daemons + services), deduped by URL — feeds a "system_latency" metric card.
    lat_by_url: dict[str, int] = {}
    for item in agents + services:
        url, lm = item.get("health_url"), item.get("latency_ms")
        if url and lm is not None:
            lat_by_url.setdefault(url, lm)
    sysavg = round(sum(lat_by_url.values()) / len(lat_by_url)) if lat_by_url else None
    for s in services:
        s["system_latency_ms"] = sysavg
        s["system_latency_n"] = len(lat_by_url)
    from .integrity import status as core_status
    agent_tables = _merge_tables_by_title(extra['tables'])
    link_quota_accounts(agent_tables, agents, extra['patches'])
    resets = upcoming_resets(agent_tables, agents)
    # Quota history + burn-rate forecast + Telegram alerts; each swallows its own errors.
    forecasts = quota.observe(agent_tables, agents)
    quota.annotate(agent_tables, agents, resets, forecasts)
    alerts.tick(cfg, agent_tables, agents, forecasts)
    for s in services:
        s["anchor"] = "sluzba-" + _anchor_slug(s.get("name"))
    for t in agent_tables:
        t.setdefault("anchor", "tabulka-" + _anchor_slug(t.get("id") or t.get("title")))
    data = {"time": int(time.time()), "agents": agents,
            "agent_tables": agent_tables,
            "resets": resets, "forecasts": forecasts,
            "columns": _merge_columns_by_title(extra['columns']),
            "plugin_errors": extra['errors'], "plugins": extra['plugins'],
            "plugin_loading": extra.get('loading', False),
            "core": core_status(), "services": services,
            "queue": workqueue.get(cfg)}
    data["attention"], data["overall"] = compute_attention(data)
    return data


# ---- "Vyžaduje pozornost" + overall status (server-side, so the page only renders) ----------
ATTENTION_MAX = 6
_SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}
SERVICE_LABELS_CS = {
    "Multi-Agent System Availability": "Dostupnost multiagentního systému",
    "Multi-agent system availability": "Dostupnost multiagentního systému",
    "Telegram Bridge": "Telegram most",
    "LANA Hermes Telegram Lane": "LANA Hermes Telegram lane",
}
_STOPPED = ("Stopped (manual)", "Zastaveno (ručně)")


def _cz_plural(n: int, one: str, few: str, many: str) -> str:
    return one if n == 1 else few if 2 <= n <= 4 else many


def _cz_duration(sec) -> str:
    try:
        sec = max(0, int(sec))
    except (TypeError, ValueError):
        return "?"
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{max(1, m)} min"


def _bar_exhausted(b: dict) -> bool:
    state = str(b.get("state") or "").upper()
    if state in ("UNAVAILABLE", "NEDOSTUPNÉ", "N/A"):
        return False
    rem, used = b.get("remaining"), b.get("percent")
    return (state in ("VYČERPÁNO", "EXHAUSTED") or (type(rem) in (int, float) and rem <= 0)
            or (type(used) in (int, float) and used >= 100))


def _gh(url) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://github.com/") else None


def compute_attention(data: dict, now: float | None = None) -> tuple[list, dict]:
    """Everything that needs a human, worst first: ``([{severity, text_cs, href, action}],
    {level, text_cs, count, total})``. ``count`` = critical + warning items (tab title badge);
    the list is capped at ATTENTION_MAX, ``total`` is the uncapped length. Never raises."""
    now = time.time() if now is None else now
    out: list[dict] = []

    def add(sev, text, href=None, action=None, **extra):
        out.append({"severity": sev, "text_cs": text, "href": href, "action": action, **extra})

    try:
        for s in data.get("services") or []:
            name = SERVICE_LABELS_CS.get(s.get("name"), s.get("name") or "služba")
            href = "#" + (s.get("anchor") or "services")
            if s.get("state") == "outage":
                add("critical", f"{name}: výpadek", href)
            elif s.get("state") == "stale":
                age = f" (poslední data před {_cz_duration(s.get('data_age'))})" if s.get("data_age") is not None else ""
                add("warning", f"{name}: Zastaralá data{age}", href)
            bad = [r for r in s.get("rows") or [] if r.get("state") == "outage"]
            if bad and s.get("state") != "outage":
                add("warning", f"{name}: {len(bad)} {_cz_plural(len(bad), 'kontrola', 'kontroly', 'kontrol')} ve výpadku"
                    f" ({', '.join(str(r.get('name')) for r in bad[:3])})", href)
        rows = [(a, "#agenti") for a in data.get("agents") or []]
        rows += [(r, "#" + (t.get("anchor") or "plugin-tables")) for t in data.get("agent_tables") or []
                 if t.get("kind") != "quota" for r in t.get("rows") or []]
        for a, href in rows:
            name = a.get("name") or a.get("id") or "agent"
            if a.get("status") in _STOPPED:
                add("info", f"Agent {name}: zastaven ručně", href)
            elif not a.get("alive"):
                add("critical", f"Agent {name}: neběží", href)
            elif a.get("needs_login"):
                add("warning", f"Agent {name}: čeká na přihlášení", href)
        for t in data.get("agent_tables") or []:
            if t.get("kind") != "quota":
                continue
            for r in t.get("rows") or []:
                exh = [b for c in (r.get("cells") or {}).values() for b in (c or {}).get("bars") or []
                       if _bar_exhausted(b)]
                if not exh:
                    continue
                labels = ", ".join(dict.fromkeys(str(b.get("label") or "okno") for b in exh))
                resets = [b.get("reset_at") for b in exh if type(b.get("reset_at")) is int and b["reset_at"] > now]
                when = [str(b.get("reset_label")) for b in exh if b.get("reset_label")]
                text = f"{r.get('name') or r.get('id')}: vyčerpáno {labels}"
                if when and resets:
                    text += f" – reset {when[0]}"
                elif when:                      # no timestamp, only a period ("měsíc")
                    text += f" ({when[0]})"
                act = next((x for x in r.get("row_actions") or [] if x.get("id") == "redeem_reset"), None)
                action = ({"kind": "row_action", "table": t.get("id"), "row": r.get("id"),
                           "name": r.get("name") or r.get("id"), "id": act.get("id"),
                           "label": act.get("label") or "Uplatnit reset", "icon": act.get("icon") or "reset",
                           "confirm": act.get("confirm") or ""} if act else None)
                add("warning", text, "#" + r["anchor"] if r.get("anchor") else "#plugin-tables", action,
                    reset_at=min(resets) if resets else None)
        for p in data.get("plugin_errors") or []:
            add("warning", f"Plugin {p.get('name')}: {str(p.get('detail') or 'chyba')[:140]}", "#plugin-errors")
        qd = data.get("queue") or {}
        if qd.get("enabled") and not qd.get("loading"):
            sm, meta, items = qd.get("summary") or {}, qd.get("meta") or {}, qd.get("items") or []
            tracked = [i for i in items if i.get("tracked")]

            def nums(state):
                return ", ".join(f"#{i['issue']}" for i in tracked if i.get("state") == state and i.get("issue"))[:60]

            def one_url(state):
                hits = [i for i in tracked if i.get("state") == state]
                return _gh(hits[0].get("url")) if len(hits) == 1 else None

            n = int(sm.get("failed") or 0)
            if n:
                add("critical", f"Fronta: {n} {_cz_plural(n, 'úloha selhala', 'úlohy selhaly', 'úloh selhalo')}"
                    + (f" ({nums('failed')})" if nums("failed") else ""), one_url("failed") or "#fronta",
                    queue_tab="problem")
            n = int(sm.get("blocked") or 0)
            if n:
                add("warning", f"Fronta: {n} {_cz_plural(n, 'úloha blokována', 'úlohy blokovány', 'úloh blokováno')}"
                    + (f" ({nums('blocked')})" if nums("blocked") else ""), one_url("blocked") or "#fronta",
                    queue_tab="problem")
            red = [i for i in items if (i.get("pr") or {}).get("ci") == "failure"]
            seen = set()
            red = [i for i in red if not (i["pr"].get("number") in seen or seen.add(i["pr"].get("number")))]
            if len(red) == 1:
                pr = red[0]["pr"]
                add("warning", f"PR #{pr.get('number')}: CI selhalo – {str(pr.get('title') or red[0].get('title') or '')[:80]}",
                    _gh(pr.get("url")) or "#fronta", queue_tab="vse")
            elif red:
                add("warning", f"{len(red)} PR s chybou CI ({', '.join('#' + str(i['pr'].get('number')) for i in red[:4])})",
                    "#fronta", queue_tab="vse")
            if meta.get("stale") or meta.get("source_errors"):
                errs = meta.get("source_errors") or []
                add("warning", "Fronta práce: Zastaralá data" + (f" – {str(errs[0])[:120]}" if errs else ""), "#fronta")
            n = int(sm.get("awaiting_pickup") or 0)
            if n:
                add("info", f"Fronta: {n} {_cz_plural(n, 'položka čeká', 'položky čekají', 'položek čeká')} na převzetí",
                    "#fronta", queue_tab="aktivni")
            n = int(sm.get("mismatches") or 0)
            if n:
                add("info", f"Fronta: {n} {_cz_plural(n, 'nesoulad', 'nesoulady', 'nesouladů')} GitHub × supervisor",
                    "#fronta", queue_tab="vse")
    except Exception as exc:                      # the panel must never break /api/state
        add("warning", f"Výpočet upozornění selhal: {type(exc).__name__}", None)
    out.sort(key=lambda x: _SEV_ORDER.get(x["severity"], 9))       # stable: keeps source order
    count = sum(1 for x in out if x["severity"] in ("critical", "warning"))
    if any(x["severity"] == "critical" for x in out):
        overall: dict = {"level": "critical", "text_cs": f"Kritický stav ({count})"}
    elif count:
        overall = {"level": "warning", "text_cs": f"Vyžaduje pozornost ({count})"}
    else:
        overall = {"level": "ok", "text_cs": "Vše v provozu"}
    overall.update(count=count, total=len(out))
    return out[:ATTENTION_MAX], overall


def upcoming_resets(tables: list, agents: list, now: float | None = None, limit: int = 24) -> list:
    """Every quota window with a known future reset, soonest first — one entry per window.

    Provider tables are the source of truth (one row per account); agent rows only repeat the
    same windows as compact gauges, so they are used solely for windows no table carries.
    A window counts as exhausted when nothing remains — that is the reset worth waiting for.
    """
    now = time.time() if now is None else now
    out, seen = [], set()
    sources = [(r, True) for t in tables or [] for r in t.get("rows", [])] + [(a, False) for a in agents or []]
    for row, primary in sources:
        for cell in (row.get("cells") or {}).values():
            for b in (cell or {}).get("bars") or []:
                ts = b.get("reset_at")
                if type(ts) is not int or ts <= now:
                    continue
                key = (round(float(b.get("percent") or 0), 1), ts // 60)
                if key in seen and not primary:
                    continue
                name = (row.get("name") or "").strip()
                ident = (name, b.get("label"), ts // 60)
                if ident in seen:
                    continue
                seen.add(ident); seen.add(key)
                rem = b.get("remaining")
                used = b.get("percent")
                state = str(b.get("state") or "").upper()
                out.append({
                    "source": name, "label": b.get("label") or "", "reset_at": ts,
                    "reset_label": b.get("reset_label") or "", "percent": used, "remaining": rem,
                    "state": state,
                    "exhausted": state == "VYČERPÁNO" or (type(rem) in (int, float) and rem <= 0)
                                 or (type(used) in (int, float) and used >= 100),
                })
    out.sort(key=lambda e: (e["reset_at"], e["source"], e["label"]))
    return out[:limit]


def _bar_signature(cells) -> set:
    """Identify quota windows across rows: (reset minute, rounded used %) — or the bar title when
    a window has no reset (credits). Plugin patches copy the provider row's windows onto agent
    rows (sometimes relabelled, e.g. "Claude 5h" → "5h"), so labels alone do not match."""
    sig = set()
    for cell in (cells or {}).values():
        for b in (cell or {}).get("bars") or []:
            ts = b.get("reset_at")
            if type(ts) is int and ts > 0:
                try:
                    sig.add(("r", ts // 60, round(float(b.get("percent") or 0))))
                except (TypeError, ValueError):
                    continue
            elif b.get("title"):
                sig.add(("t", str(b["title"])))
    return sig


def _anchor_slug(text: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-") or "ucet"


def link_quota_accounts(tables: list, agents: list, patches: list) -> None:
    """Mark quota tables (``kind: "quota"``) and link each agent to the provider row (account)
    whose windows a plugin patch copied onto it — so the UI shows one account chip per agent and
    the windows once, in the account card. Adds ``anchor`` + ``used_by`` to provider rows and
    ``quota_account`` to agents; the plugin API and the copied ``cells`` stay untouched.
    An agent without patches falls back to a unique exact name match (agent "Cursor" ↔ row
    labelled "Cursor")."""
    rows, anchors = [], set()
    for t in tables or []:
        quota_rows = [r for r in t.get("rows") or [] if _bar_signature(r.get("cells"))]
        if not quota_rows:
            continue
        t["kind"] = "quota"
        for r in t.get("rows") or []:
            base = "kvota-" + _anchor_slug(r.get("id") or r.get("name"))
            anchor, n = base, 2
            while anchor in anchors:
                anchor, n = f"{base}-{n}", n + 1
            anchors.add(anchor)
            r["anchor"], r["used_by"] = anchor, []
        rows += [(t, r, _bar_signature(r.get("cells"))) for r in quota_rows]
    if not rows:
        return
    for a in agents or []:
        applied = [p for p in patches or [] if p.get("row_id") == a.get("id") and p.get("cells")]
        sig = set().union(*(_bar_signature(p["cells"]) for p in applied)) if applied else set()
        best, score, via = None, 0, "patch"
        for t, r, rsig in rows:
            n = len(sig & rsig)
            if n > score:
                best, score = (t, r), n
        if best is None and not applied:
            name = (a.get("name") or "").strip().casefold()
            hits = [(t, r) for t, r, _ in rows
                    if name and name in ((r.get("label") or "").strip().casefold(),
                                         (r.get("name") or "").split("·")[0].strip().casefold())]
            if len(hits) == 1:
                best, via = hits[0], "name"
        if best:
            t, r = best
            r["used_by"].append(a.get("name") or a.get("id") or "")
            a["quota_account"] = {"table": t.get("id"), "row": r.get("id"), "name": r.get("name") or "",
                                  "anchor": r["anchor"], "via": via, "shared_with": r["used_by"]}


def _merge_columns_by_title(columns: list) -> list:
    """Collapse plugin columns that share a display title (e.g. two plugins both emit 5h)."""
    out, index = [], {}
    for col in columns or []:
        title = col.get("title") or col.get("id")
        if title in index:
            prev = out[index[title]]
            aliases = list(prev.get("aliases") or [prev["id"]])
            if col["id"] not in aliases:
                aliases.append(col["id"])
            prev["aliases"] = aliases
        else:
            index[title] = len(out)
            item = dict(col)
            item["aliases"] = [col["id"]]
            out.append(item)
    return out


def _merge_tables_by_title(tables: list) -> list:
    """One Provider-quotas section even when Codex + Cursor plugins both contribute."""
    out, index = [], {}
    for table in tables or []:
        title = table.get("title") or table.get("id")
        if title in index:
            prev = out[index[title]]
            prev["rows"] = list(prev.get("rows") or []) + list(table.get("rows") or [])
            notes = [n for n in (prev.get("note"), table.get("note")) if n]
            if notes:
                prev["note"] = " ".join(dict.fromkeys(notes))
            prev["up"] = bool(prev.get("up", True) and table.get("up", True))
            if table.get("error"):
                prev["error"] = ((prev.get("error") or "") + " " + table["error"]).strip()
        else:
            index[title] = len(out)
            out.append(dict(table))
    return out


def _quota_loop(stop: threading.Event, interval: int = 60) -> None:
    """Build the state in the background so quota history and alerts keep running with no browser
    open. The state builder is cheap (plugins are cached and refreshed off-thread)."""
    while not stop.wait(interval):
        try:
            data = _state_data()
            # Without a browser polling, the plugin cache is cold: that build returns an empty
            # 'loading' snapshot and starts a refresh. Retry briefly until real data is there.
            for _ in range(10):
                if not data.get("plugin_loading"):
                    break
                if stop.wait(3):
                    return
                data = _state_data()
        except Exception as exc:
            print(f"quota loop: {type(exc).__name__}: {exc}", flush=True)


def _probe_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            probe.probe_once(config.load())
        except Exception:
            pass
        stop.wait(int(config.load().get("probe", {}).get("interval_seconds", 60)))


_STATIC_TAILWIND = (Path(__file__).parent / "static" / "tailwind.js").read_bytes()


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def require_auth_or_die(host: str, cfg: dict) -> None:
    """Require login off-loopback unless an administrator opts into a private LAN bind.

    The LAN exception requires a specific RFC1918 IPv4 address and an actual boolean;
    administrators must restrict incoming clients with the host firewall.
    Use ``agentsmon setup`` to review bind/auth options.
    """
    auth = cfg.get("dashboard", {}).get("auth") or {}
    if _loopback(host) or (auth.get("user") and auth.get("pwhash")):
        return
    if cfg.get("dashboard", {}).get("allow_unauthenticated_lan") is True:
        try:
            address = ipaddress.IPv4Address(host)
            if any(address in ipaddress.IPv4Network(network) for network in
                   ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                return
        except ipaddress.AddressValueError:
            pass
    raise SystemExit(
        f"refusing to listen on {host} without dashboard.auth (user + pwhash) in the config — "
        "the page can stop and restart agents. Bind 127.0.0.1 or set the password via `agentsmon setup`."
    )


def serve(host: str, port: int) -> None:
    # Bring an older config up to the current schema on startup (e.g. fold per-daemon availability
    # cards into the synthetic Multi-Agent System card). The dashboard restarts on every update,
    # so this self-heals existing installs without needing a special migration step.
    try:
        from . import wizard
        cfg0 = config.load()
        if wizard.migrate_config(cfg0):
            config.save(cfg0)
    except Exception:
        pass
    cfg = config.load()
    server = make_server(host, port, cfg)
    print(f"Agents Monitoring dashboard → http://{host}:{port}  (Ctrl-C to stop)")
    if cfg.get("services"):
        threading.Thread(target=_probe_loop, args=(threading.Event(),), daemon=True).start()
    quota.start()
    alerts.start()
    workqueue.start(cfg)      # GitHub/supervisor work queue: background cache, no-op when disabled
    threading.Thread(target=_quota_loop, args=(threading.Event(),), daemon=True,
                     name="agentsmon-quota").start()
    write_pidfile()
    server.serve_forever()


def make_server(host: str, port: int, cfg: dict):
    """Same handler for production and isolated HTTP acceptance tests."""
    require_auth_or_die(host, cfg)
    poll = cfg.get("dashboard", {}).get("poll_seconds", 15)
    page = PAGE.replace("POLL", str(poll)).encode()
    auth = cfg.get("dashboard", {}).get("auth") or {}
    auth_user, auth_hash = auth.get("user"), auth.get("pwhash")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _denied(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Agents Monitoring"')
            self.end_headers()

        def do_GET(self):
            if auth_user and auth_hash and not _auth_ok(self.headers.get("Authorization"),
                                                        auth_user, auth_hash):
                return self._denied()
            if self.path.startswith("/api/state"):
                body = _state()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/metrics" or self.path.startswith("/metrics?"):
                body = metrics.render(_state_data()).encode()
                self.send_response(200)
                self.send_header("Content-Type", metrics.CONTENT_TYPE)
            elif self.path == "/" or self.path.startswith("/index"):
                body = page
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif self.path == "/static/tailwind.js":
                # Served from the package, not cdn.tailwindcss.com: a third party that can change
                # a script running on this origin could drive the control endpoint (audit 2026-09-08).
                body = _STATIC_TAILWIND
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=86400")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if auth_user and auth_hash and not _auth_ok(self.headers.get("Authorization"),
                                                        auth_user, auth_hash):
                return self._denied()
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            if parsed.path != "/api/agent/action":
                self.send_response(404)
                self.end_headers()
                return
            if not _action_allowed(self.headers):
                body = json.dumps({"ok": False, "error": "missing X-Agentsmon header (cross-site request?)"}).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try: length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError: length = -1
            if not 0 <= length <= 16384:
                self.send_error(413); return
            raw = self.rfile.read(length) if length else b""
            params = {}
            try:
                params = json.loads(raw or b"{}")
            except ValueError:
                params = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
            if not isinstance(params, dict):
                self.send_error(400); return
            qs = parse_qs(parsed.query)
            name = params.get("name") or (qs.get("name", [None])[0])
            act = params.get("action") or (qs.get("action", [None])[0])
            table, row = params.get('table'), params.get('row')
            if table is not None:
                if not all(isinstance(x,str) and 0<len(x)<=240 for x in (table,row,act)):
                    self.send_error(400); return
                ok, msg = plugins.manager(config.load()).action(table,row,act)
                body = json.dumps({'ok':ok,'message':msg}).encode()
                status = 200 if ok else 409
            elif not isinstance(name,str) or not name or act not in ("restart", "stop"):
                body = json.dumps({"ok": False, "error": "need name + action (restart|stop)"}).encode()
                status = 400
            else:
                try:
                    ok, msg = _agent_action(name, act)
                except Exception as exc:                      # never 500 the dashboard on a bad action
                    ok, msg = False, str(exc)
                body = json.dumps({"ok": ok, "message": msg}).encode()
                status = 200 if ok else 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)


def pidfile_path():
    return config.state_dir() / "dashboard.pid"


def write_pidfile() -> None:
    """Record our PID so the installer can stop exactly this process. It used to
    ``pkill -f "agentsmon dashboard"``, which also killed any shell whose command line merely
    mentioned that string — including the script that was running the install (2026-09-04)."""
    try:
        pidfile_path().write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
