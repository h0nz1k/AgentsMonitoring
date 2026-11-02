"""Render the redesign with provider metadata and exercise the local UI controls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from agentsmon import dashboard
from test_incremental_render import NODE, JSDOM, STATE


@unittest.skipUnless(os.path.exists(NODE) and os.path.isdir(JSDOM), "node + jsdom not available")
class RedesignFrontend(unittest.TestCase):
    def test_counts_sublimits_credit_theme_polling_and_viewer(self):
        state = copy.deepcopy(STATE)
        state["overall"].update(count_cs="1 + 1 info", total=2, critical=0, warning=1, info=1,
                                summary_cs="Jedna věc vyžaduje pozornost.")
        state["attention"].append({"severity": "info", "text_cs": "Fronta: čeká na převzetí", "href": "#fronta"})
        bars = state["agent_tables"][0]["rows"][0]["cells"]["w"]["bars"]
        bars[0].update(label="Claude week", percent=22,
                       forecast={"projected_at_reset": 30, "will_exhaust_before_reset": False})
        bars.append({"label": "Fable week", "percent": 6, "parent": "Claude week", "cap": 50})
        bars.append({"label": "GCP Bonus", "percent": 6, "meta": {
            "spent_usd": 18, "initial_usd": 300, "remaining_credit": 6000, "currency": "CZK",
            "today_usd": 2.5, "expires_at": "2027-01-28", "estimate": True}})
        script = r'''
const {JSDOM,VirtualConsole}=require(process.env.JSDOM_PATH),fs=require('fs'),assert=require('assert');
const state=JSON.parse(fs.readFileSync(process.argv[3])),errors=[];
const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
let requests=0,visible='visible',systemListener;const intervals=new Map();
const dom=new JSDOM(fs.readFileSync(process.argv[2],'utf8'),{runScripts:'dangerously',url:'http://localhost/',virtualConsole:vc,beforeParse(w){
 const setInterval=w.setInterval.bind(w),clearInterval=w.clearInterval.bind(w);
 w.setInterval=(fn,ms)=>{const id=setInterval(fn,ms);intervals.set(id,ms);return id};
 w.clearInterval=id=>{intervals.delete(id);clearInterval(id)};
 w.fetch=async()=>{requests++;return {ok:true,json:async()=>structuredClone(state)}};
 w.matchMedia=()=>({matches:true,addEventListener:(type,fn)=>systemListener=fn});
 Object.defineProperty(w.document,'visibilityState',{get:()=>visible});
}});
const w=dom.window,d=w.document;
(async()=>{
 await new Promise(r=>setTimeout(r,100));
 assert.deepEqual(errors,[]);
 assert.equal(d.querySelector('.ring-center b').textContent,'1 + 1 info');
 assert.equal(d.querySelector('#attn-h').textContent,'K řešení (1 + 1 info)');
 assert.equal(d.querySelectorAll('.attn-item').length,2);
 assert.equal(d.title,'(1 + 1 info) Agenti');
 assert.equal(d.querySelector('#overview-summary').textContent,state.overall.summary_cs);
 assert.equal(d.querySelectorAll('.limit-row').length,2);
 assert.match(d.querySelector('.sub-limit').textContent,/z toho Fable týden 6 % · strop 50 %/);
 assert.match(d.querySelector('.verdict').textContent,/Vystačí – zbude ~70 %/);
 const credit=d.querySelector('.credit-meta').textContent;
 assert.match(credit,/Utraceno ≈ 18 USD \(odhad\)/);assert.match(credit,/6\s000 Kč/);
 assert.match(credit,/Platnost do 28\. 1\. 2027/);assert.match(credit,/Cílové tempo[\d,]+ USD\/den/);
 assert.equal(d.documentElement.dataset.theme,'dark');
 d.querySelector('#btn-theme').click();assert.equal(d.documentElement.dataset.theme,'light');
 assert.equal(w.localStorage.getItem('agentsmon.theme'),'light');systemListener();assert.equal(d.documentElement.dataset.theme,'light');
 visible='hidden';d.dispatchEvent(new w.Event('visibilitychange'));
 const timer=w.eval('pollTimer');assert.ok(![...intervals.values()].includes(15000));assert.equal(w.eval('paused'),false);
 const before=requests;visible='visible';d.dispatchEvent(new w.Event('visibilitychange'));
 assert.equal(requests,before+1);assert.ok([...intervals.values()].includes(15000));assert.notEqual(w.eval('pollTimer'),timer);
 d.querySelector('#btn-pause').click();assert.equal(w.eval('paused'),true);
 assert.equal(d.querySelector('#btn-pause').getAttribute('aria-pressed'),'true');
 assert.ok(![...intervals.values()].includes(15000));
 const pausedRequests=requests;visible='hidden';d.dispatchEvent(new w.Event('visibilitychange'));
 visible='visible';d.dispatchEvent(new w.Event('visibilitychange'));assert.equal(requests,pausedRequests);
 d.querySelector('#btn-pause').click();assert.equal(requests,pausedRequests+1);
 const head=d.querySelector('#agenti .svc-head');head.click();
 assert.equal(w.localStorage.getItem('agentsmon.collapsed:agents'),'1');
 head.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
 assert.equal(w.localStorage.getItem('agentsmon.collapsed:agents'),'0');
 assert.ok([...d.querySelectorAll('.agent-act')].every(b=>b.disabled));
 // Untrusted plugin text stays text, including inside provider and attention cards.
 w.renderAttention([{severity:'warning',text_cs:'<img src=x onerror=alert(1)>',href:'javascript:alert(1)'}],state.overall);
 assert.equal(d.querySelectorAll('.attn-item img,.attn-item a').length,0);
 assert.match(d.querySelector('.attn-item').textContent,/<img/);
 w.renderAttention([{severity:'warning',kind:'exhausted',text_cs:'Vyčerpáno'}],{level:'warning',count:1,total:1,count_cs:'1',warning:1,critical:0,info:0});
 assert.equal(d.querySelector('#status-ring circle').getAttribute('stroke'),'var(--bad)');
 w.renderAttention([],{level:'ok',text_cs:'Vše v provozu',summary_cs:'Vše je v pořádku.',count:0,total:0,count_cs:'0'});
 assert.equal(d.querySelector('.ring-center b').textContent,'✓');
 assert.equal(d.querySelector('#attention').hidden,true);
 assert.match(d.querySelector('#status-ring circle').getAttribute('stroke'),/--ok/);
 await new Promise(r=>setTimeout(r,20));assert.deepEqual(errors,[]);console.log('redesign UI OK');w.close();
})().catch(e=>{console.error(e);process.exitCode=1;w.close()});
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.html").write_text(dashboard.PAGE.replace("POLL", "15").replace("__AGENTSMON_ROLE__", "viewer"))
            (root / "state.json").write_text(json.dumps(state))
            (root / "test.cjs").write_text(script)
            result = subprocess.run([NODE, str(root / "test.cjs"), str(root / "page.html"), str(root / "state.json")],
                                    capture_output=True, text=True, timeout=60,
                                    env={**os.environ, "JSDOM_PATH": JSDOM})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("redesign UI OK", result.stdout)


class RedesignHooks(unittest.TestCase):
    def test_mobile_and_security_hooks(self):
        for hook in ('id="btn-theme"', 'id="updated-age"', 'id="overview-chips"', 'id="status-ring"',
                     'class="limit-ring"', 'class="sub-limit"', 'document.visibilityState',
                     'agentsmon.theme', 'Europe/Prague', 'text-wrap:balance'):
            self.assertIn(hook, dashboard.PAGE)
        self.assertNotIn('id="btn-mute"', dashboard.PAGE)
        self.assertIn("script-src 'self' 'unsafe-inline'", dashboard.CSP)
        self.assertNotIn('https://cdn', dashboard.PAGE)
