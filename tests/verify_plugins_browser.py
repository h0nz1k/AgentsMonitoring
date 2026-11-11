"""Optional real-browser acceptance with local fixtures; no live services/actions.

python tests/verify_plugins_browser.py --browser /path/to/chrome --out /tmp/evidence
Optionally --reference-page old-dashboard.html compares the previous renderer.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentsmon import dashboard


async def verify(args):
    from playwright.async_api import async_playwright
    args.out.mkdir(parents=True,exist_ok=True)
    sid='11111111-2222-3333-4444-555555555555'
    shared={'name':'Example Agent','kind':'codex','vendor':'openai','label':'GPT-6 Astra',
            'alive':True,'age':3600,'session_id':sid,'account':'Fixture account','telegram_bot':'example_test_bot',
            'telegram_url':'https://t.me/example_test_bot'}
    daemon={'name':'Example Gateway','kind':'daemon','vendor':'openai','label':'Example model',
            'alive':True,'port':9999,'latency_ms':1,'status_detail':'Health endpoint OK','actions':False}
    extension={**shared,'id':'example','kind':'sandbox','plugin':'fixture','actions':False,
               'actions_locked':True,'tmux_session':'example','row_actions':[
                   {'id':'reload','icon':'restart','label':'Safe reload','confirm':'Reload fixture?'}]}
    table={'id':'fixture:team','title':'Example Team','rows':[extension],'up':True,'error':'','note':'Fixture'}
    payload={'agents':[shared,daemon],'agent_tables':[table], 'columns':[], 'services':[],
             'plugins':[],'plugin_errors':[], 'core':{'version':'0.3.0','state':'clean'},
             'sandbox_agents':{'enabled':True,'rows':[extension],'up':True,'stale':False,'age_s':1}}
    service={'name':'Knowledge health','up':True,'state':'operational','detail':'OK',
             'row_details':True,'uptime_seconds':60,'sla':100,'sla_window_days':90,'sla_samples':1,
             'latency_ms':2,'timeline':[],'timeline_days':90,
             'rows':[{'name':'Index','up':True,'state':'operational','url':'http://127.0.0.1:1234/',
                      'detail':'42 notes · last write 2026-01-02 <img src=x onerror="window.PWNED=1">',
                      'latency_ms':1,'uptime_seconds':60,'last_ts':None}]}
    payload['services']=[service]
    posts=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            if self.path=='/': body=dashboard.PAGE.replace('POLL','3600').encode(); kind='text/html'
            elif self.path=='/reference' and args.reference_page:
                body=args.reference_page.read_text().replace('POLL','3600').encode();kind='text/html'
            elif self.path=='/api/state': body=json.dumps(payload).encode();kind='application/json'
            elif self.path in dashboard._STATIC_ASSETS: body,kind=dashboard._STATIC_ASSETS[self.path]
            else: self.send_error(404); return
            self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body=b'{"ok":true,"message":"Fixture only"}'
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    async def geometry(root):
        return await root.locator('tbody tr').first.locator('td').evaluate_all(
            '(cells)=>cells.map(c=>({x:c.getBoundingClientRect().x,width:c.getBoundingClientRect().width,height:c.getBoundingClientRect().height}))')
    def same(a,b):
        assert len(a)==len(b)
        for one,two in zip(a,b):
            for key in ('x','width','height'): assert abs(one[key]-two[key])<1,(key,one,two)
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(executable_path=args.browser,headless=True)
            try:
                page=await browser.new_page(viewport={'width':1280,'height':900},device_scale_factor=1)
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                upper=page.locator('[data-svc="agents"]')
                lower=page.locator('[data-svc="fixture:team"]')
                await lower.locator('tbody td').first.wait_for()
                card=page.locator('#services section').first
                # A module can opt into visible metrics without changing stable row identities.
                assert await card.locator('.rows-value-label').text_content()=='Detail'
                assert '42 notes' in await card.locator('tbody').inner_text()
                assert await card.locator('img').count()==0
                assert await page.evaluate('window.PWNED || null') is None
                await card.screenshot(path=str(args.out/'service-details.png'))
                service['row_details']=False
                await page.evaluate('refresh()')
                assert await card.locator('.rows-value-label').text_content()=='Address'
                assert '127.0.0.1:1234' in await card.locator('tbody').inner_text()
                assert '42 notes' not in await card.locator('tbody').inner_text()
                await page.wait_for_timeout(350)
                daemon_status=upper.locator('tbody tr').nth(1).locator('td').nth(4)
                await page.screenshot(path=str(args.out/'daemon-status.png'),full_page=True)
                assert await daemon_status.inner_text()=='Running',await daemon_status.inner_text()
                assert 'latency 1 ms' in await daemon_status.get_attribute('title')
                assert 'Health endpoint OK' in await daemon_status.get_attribute('title')
                assert await upper.locator('tbody tr').first.locator('td').nth(4).inner_text()=='Running'
                # A live login gate must be yellow; liveness still prevents keeper restarts.
                shared.update(needs_login=True, status='Needs login')
                await page.evaluate('refresh()')
                login = upper.locator('tbody tr').first.locator('td').nth(4)
                assert await login.inner_text() == 'Needs login'
                assert await login.locator('.bg-amber-500').count() == 1
                assert await login.locator('.bg-emerald-500').count() == 0
                await page.screenshot(path=str(args.out/'needs-login.png'), full_page=True)
                shared.update(needs_login=False, alive=False, status='Stopped (manual)')
                await page.evaluate('refresh()')
                assert await login.inner_text() == 'Stopped (manual)'
                assert 'Example Agent' in await upper.inner_text()
                shared.update(alive=True, status='Running')
                await page.evaluate('refresh()')
                # Zero latency is still a valid observation; no observation invents no latency.
                for latency,expected in ((0,'latency <1 ms'),(None,None)):
                    daemon['latency_ms']=latency
                    await page.evaluate('refresh()')
                    assert await daemon_status.inner_text()=='Running'
                    tooltip=await daemon_status.get_attribute('title')
                    if expected:
                        assert expected in tooltip,tooltip
                    else:
                        assert 'latency' not in tooltip,tooltip
                # A stale latency must not disguise a down/timeout/error row as healthy.
                daemon.update(alive=False,latency_ms=1,status_detail='Health endpoint failed')
                for status in (None,'Down','Timeout','Error'):
                    daemon['status']=status
                    await page.evaluate('refresh()')
                    assert await daemon_status.inner_text()==(status or 'Idle')
                    assert await daemon_status.locator('.bg-emerald-500').count()==0
                    assert 'Health endpoint failed' in await daemon_status.get_attribute('title')
                await page.wait_for_timeout(350)
                await page.screenshot(path=str(args.out/'daemon-error.png'),full_page=True)
                # An explicit error from a provider retains precedence, even with alive=true.
                daemon.update(alive=True,status='Error')
                await page.evaluate('refresh()')
                assert await daemon_status.inner_text()=='Error'
                daemon.update(alive=True,status=None,latency_ms=1,status_detail='Health endpoint OK')
                await page.evaluate('refresh()')
                await page.wait_for_timeout(350)
                same(await geometry(upper),await geometry(lower))
                assert await upper.locator('thead').inner_text()==await lower.locator('thead').inner_text()
                assert sid in await lower.inner_text()
                assert await lower.locator('[title="Account: Fixture account"]').count()==1
                assert await lower.locator('a[href="https://t.me/example_test_bot"]').count()==1
                assert await lower.locator('[data-act="reload"]').count()==1
                assert await lower.locator('[data-act="stop"]').count()==0
                assert 'core v0.3.0 (clean)' in await page.locator('#footer').inner_text()
                await page.screenshot(path=str(args.out/'plugin-parity.png'),full_page=True)
                current=await geometry(upper)
                if args.reference_page:
                    old=await browser.new_page(viewport={'width':1280,'height':900})
                    await old.goto(f'http://127.0.0.1:{server.server_port}/reference')
                    await old.locator('[data-svc="sandbox-agents"] tbody td').first.wait_for()
                    await old.wait_for_timeout(350)
                    same(current,await geometry(old.locator('[data-svc="agents"]')))
                    await old.screenshot(path=str(args.out/'reference.png'),full_page=True)
                    await old.close()
                page.on('dialog',lambda d:d.accept())
                # The action schedules a delayed refresh. Wait for it before changing
                # the fixture; otherwise it can detach cells during geometry checks.
                async with page.expect_response(lambda r:r.url.endswith('/api/state')):
                    await lower.locator('[data-act="reload"]').click()
                await page.wait_for_function("""() => {
                    const button=document.querySelector('[data-svc="fixture:team"] [data-act="reload"]');
                    return button && !button.disabled;
                }""")
                assert posts==[{'name':'Example Agent','action':'reload','table':'fixture:team','row':'example'}],posts
                payload['columns']=[{'id':'usage:limits','title':'Usage'}]
                for row in (shared,extension):
                    row['cells']={'usage:limits':{'text':'','bars':[{'label':'5h','percent':25,'title':'Fixture account'}]}}
                await page.evaluate('refresh()')
                await page.wait_for_timeout(300)
                await page.screenshot(path=str(args.out/'usage-debug.png'),full_page=True)
                same(await geometry(upper),await geometry(lower))
                await page.screenshot(path=str(args.out/'usage.png'),full_page=True)
                extension.update(alive=False,status='Unresponsive',status_detail='Last recovery: fixture')
                table.update(up=False,error='Supervisor unavailable')
                payload['plugin_errors']=[{'name':'Plugin broken failed','detail':'Plugin timed out'}]
                await page.evaluate('refresh()')
                assert 'Unresponsive' in await lower.inner_text()
                assert 'Supervisor unavailable' in await lower.inner_text()
                assert 'bg-rose-500' in await lower.locator('.svc-dot').get_attribute('class')
                assert 'Plugin timed out' in await page.locator('#plugin-errors').inner_text()
                assert await page.locator('#plugin-errors .bg-rose-50').count()==1
                assert 'Running' in await upper.inner_text()
                extension['name']='<img src=x onerror="window.PWNED=1">'
                await page.evaluate('refresh()')
                assert await lower.locator('img').count()==0
                assert await page.evaluate('window.PWNED || null') is None
                await page.screenshot(path=str(args.out/'failure-isolation.png'),full_page=True)
                # Expired/initial plugin data: neutral progress, no stale table or actions.
                payload.update(agent_tables=[],columns=[],plugin_errors=[],plugin_loading=True)
                await page.evaluate('refresh()')
                loading=page.locator('#plugin-loading')
                await loading.wait_for(state='visible')
                assert 'Awaiting a fresh bounded check' in await loading.inner_text()
                assert 'bg-slate-50' in await loading.get_attribute('class')
                assert 'rose' not in await loading.get_attribute('class')
                assert await page.locator('#plugin-errors').inner_text()==''
                assert await page.locator('#plugin-tables [data-act]').count()==0
                assert await page.locator('#plugin-tables section').count()==0
                assert 'Running' in await upper.inner_text()
                await page.wait_for_timeout(350)
                await page.screenshot(path=str(args.out/'plugin-loading.png'),full_page=True)
                # A real failure still renders red after the neutral state.
                payload.update(plugin_loading=False,plugin_errors=[{'name':'Plugin failed','detail':'Plugin timed out'}])
                await page.evaluate('refresh()')
                assert not await loading.is_visible()
                assert await page.locator('#plugin-errors .bg-rose-50').count()==1
                assert 'Plugin timed out' in await page.locator('#plugin-errors').inner_text()
                await page.wait_for_timeout(350)
                await page.screenshot(path=str(args.out/'plugin-timeout.png'),full_page=True)
                payload.update(agent_tables=[table],columns=[{'id':'usage:limits','title':'Usage'}])
                # Two healthy samples in a day are minutes of observed uptime, not a whole day.
                extension.update(name='Example Agent',alive=True,status='Running',status_detail='Ready')
                table.update(up=True,error='')
                payload['plugin_errors']=[]
                now=int(time.time())
                fresh={'name':'Fresh service','state':'operational','uptime_seconds':120,
                       'sla':100,'sla_samples':2,'sla_window_days':90,'latency_ms':1,
                       'timeline':[{'start':now-(90-i)*86400,'uptime_pct':100 if i==89 else None}
                                   for i in range(90)],'timeline_days':90}
                payload['services']=[fresh]
                await page.evaluate('refresh()')
                uptime=page.locator('#services .m-uptime')
                await page.wait_for_timeout(350)
                await page.screenshot(path=str(args.out/'fresh-install-uptime.png'),full_page=True)
                assert await uptime.inner_text()=='2m',await uptime.inner_text()
                for seconds,expected in ((None,'–'),(0,'0m'),(26*3600,'1d 2h')):
                    fresh['uptime_seconds']=seconds
                    await page.evaluate('refresh()')
                    assert await uptime.inner_text()==expected,await uptime.inner_text()
                payload['services']=[]
                if args.private_state:
                    private=json.loads(args.private_state.read_text())
                    payload.update(agent_tables=private['tables'],columns=[],plugin_errors=private['errors'])
                    shared.pop('cells',None)
                    await page.evaluate('refresh()');await page.wait_for_timeout(350)
                    await page.screenshot(path=str(args.out/'private-plugin.png'),full_page=True)
                assert not errors,errors
                print('PASS: daemon Running/latency tooltip, unhealthy statuses, shared columns/layout, reference geometry, usage, action routing, error isolation, neutral plugin loading without stale actions, red timeouts, HTML escaping, observed uptime.')
            finally: await browser.close()
    finally: server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True)
    parser.add_argument('--out',required=True,type=Path);parser.add_argument('--reference-page',type=Path)
    parser.add_argument('--private-state',type=Path,help='Optional already sanitized plugin result, never checked into this repo')
    asyncio.run(verify(parser.parse_args()))
