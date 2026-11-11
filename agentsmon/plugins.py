"""Plugin API v1: bounded subprocesses, declarative UI, namespaced actions.

Trusted administrator-installed code, NOT an OS security sandbox. No dashboard
auth/config object is handed to a plugin. Every plugin has its own settings.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from dataclasses import dataclass
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time

API_VERSION=1
ID=re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
PUBLIC_FIELDS={'id','name','kind','label','vendor','alive','session_id','age','port','latency_ms','tmux_session','account'}
ROW_FIELDS=PUBLIC_FIELDS | {'status','status_detail','telegram_bot','telegram_url','name_color','metadata_error','row_actions','cells','actions_locked'}


def ident(value):
    if not isinstance(value,str) or not ID.fullmatch(value): raise ValueError('Invalid id')
    return value


def text(value, limit=500):
    if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 for c in value): raise ValueError('Invalid text')
    return value


def number(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0: raise ValueError('Invalid number')
    return value


def objects(value, limit=200):
    if not isinstance(value,list) or len(value)>limit or not all(isinstance(x,dict) for x in value): raise ValueError('Invalid list')
    return value


def cell(value):
    if isinstance(value,str): return {'text':text(value)}
    if not isinstance(value,dict) or set(value)-{'text','title','tone','bars'}: raise ValueError('Invalid cell')
    result={'text':text(value.get('text','')),'title':text(value.get('title',''))}
    if value.get('tone') not in (None,'muted','ok','warning','error'): raise ValueError('Invalid tone')
    result['tone']=value.get('tone')
    if 'bars' in value:
        result['bars']=[]
        for b in objects(value['bars'],4):
            if set(b)-{'label','percent','title','remaining','reset_label','reset_at','state','parent','cap','meta'}: raise ValueError('Invalid bar')
            pct=number(b['percent'])
            if pct>100: raise ValueError('Invalid percent')
            bar={'label':text(b['label'],30),'percent':pct,'title':text(b.get('title',''))}
            # Optional gauge metadata: unknown remaining must stay null (never coerced to 0/100).
            if 'remaining' in b:
                rem=b['remaining']
                if rem is None:
                    bar['remaining']=None
                else:
                    rem=number(rem)
                    if rem>100: raise ValueError('Invalid remaining')
                    bar['remaining']=rem
            if b.get('reset_label') is not None:
                bar['reset_label']=text(str(b.get('reset_label')),40)
            # Optional machine-readable reset moment (Unix epoch seconds) — lets the core show a
            # live countdown and the cross-provider "Nejbližší resety" overview.
            if b.get('reset_at') is not None:
                ts=number(b['reset_at'])
                if not 1_000_000_000<=ts<=10_000_000_000: raise ValueError('Invalid reset_at')
                bar['reset_at']=int(ts)
            state=b.get('state')
            if state is not None:
                state=text(str(state),24).upper()
                if state not in ('OK','STALE','UNAVAILABLE','N/A','NEDOSTUPNÉ','VYČERPÁNO'):
                    # Allow Czech UI states through as free text (bounded).
                    pass
                bar['state']=state
            # Optional sub-limit metadata (core feature 'bar_meta'): a scoped limit that lives INSIDE
            # another window of the same row (``parent`` = that bar's label) with its own ``cap`` %.
            if b.get('parent') is not None:
                bar['parent']=text(str(b['parent']),30)
            if b.get('cap') is not None:
                cap=number(b['cap'])
                if not 0<cap<=100: raise ValueError('Invalid cap')
                bar['cap']=cap
            if b.get('meta') is not None:
                bar['meta']=bar_meta(b['meta'])
            result['bars'].append(bar)
    return result


#: Optional capabilities announced to plugins in ``context['features']`` — a plugin may only emit
#: the matching extra fields when the core lists them (older cores reject unknown bar fields).
FEATURES=('bar_meta',)


def bar_meta(value):
    """Small flat dict of numbers / short texts (e.g. credit: spent_usd, remaining, expires_at)."""
    if not isinstance(value,dict) or len(value)>16: raise ValueError('Invalid meta')
    out={}
    for k,v in value.items():
        key=ident(k)
        if isinstance(v,bool): out[key]=v
        elif type(v) in (int,float):
            if not math.isfinite(v): raise ValueError('Invalid meta')
            out[key]=v
        elif isinstance(v,str): out[key]=text(v,80)
        elif v is None: out[key]=None
        else: raise ValueError('Invalid meta')
    return out


def row(raw, plugin):
    if set(raw)-ROW_FIELDS: raise ValueError('Unknown row field')
    out={'id':ident(raw['id']),'name':text(raw['name'],240),'alive':raw['alive'],'actions':False,'plugin':plugin}
    if type(out['alive']) is not bool: raise ValueError('Invalid alive')
    for k,v in raw.items():
        if k in ('age','port','latency_ms'): out[k]=None if v is None else number(v)
        elif k in ('metadata_error','actions_locked'):
            if type(v) is not bool: raise ValueError('Invalid flag')
            out[k]=v
        elif k=='row_actions':
            actions=[]; seen=set()
            for a in objects(v,8):
                aid=ident(a['id'])
                if aid in seen: raise ValueError('Duplicate action')
                seen.add(aid)
                # 'reset' = provider quota reset (e.g. a banked Codex reset credit): rendered as a
                # labelled pill, since spending it is irreversible and must never look like ↻.
                if a.get('icon','restart') not in ('restart','stop','run','reset'): raise ValueError('Invalid icon')
                actions.append({'id':aid,'label':text(a['label'],160),'icon':a.get('icon','restart'),
                                'confirm':text(a.get('confirm',''),240)})
            out[k]=actions
        elif k=='cells':
            if not isinstance(v,dict) or len(v)>8: raise ValueError('Invalid cells')
            out[k]={plugin+':'+ident(key):cell(val) for key,val in v.items()}
        elif k not in ('id','alive'):
            if v is not None: out[k]=text(v,500)
    if out.get('telegram_url') and not re.fullmatch(r'https://t\.me/[A-Za-z][A-Za-z0-9_]{4,31}',out['telegram_url']):
        raise ValueError('Unsafe link')
    return out


@dataclass
class Plugin:
    id: str
    version: str
    target: dict
    settings: dict
    timeout: float=3
    action_timeout: float=210
    error: str=''


def discover(cfg):
    found=[]; ids=set()
    def add(raw,target):
        pid=ident(raw['id'])
        if pid in ids: raise ValueError('Duplicate plugin id')
        ids.add(pid)
        if type(raw.get('api_version')) is not int or raw['api_version']!=API_VERSION: raise ValueError('Unsupported plugin API')
        version=text(raw['version'],80)
        timeout=number(raw.get('timeout_seconds',3)); at=number(raw.get('action_timeout_seconds',210))
        if not .1<=timeout<=10 or not .1<=at<=300: raise ValueError('Invalid timeout')
        settings=raw.get('settings',{})
        if not isinstance(settings,dict): raise ValueError('Invalid settings')
        found.append(Plugin(pid,version,target,settings,timeout,at))
    directory=Path(cfg.get('modules_dir') or '~/.config/agentsmon/modules').expanduser()
    if directory.is_dir():
        manifests=list(directory.glob('*.agentsmon.json'))+list(directory.glob('*/agentsmon-plugin.json'))
        if len(manifests)>64:
            found.append(Plugin('discovery','?',{}, {},error='Too many plugin manifests (maximum 64)'))
        for path in sorted(manifests)[:64]:
            error_id=path.name
            try:
                if path.stat().st_size>65536: raise ValueError('Oversized manifest')
                raw=json.loads(path.read_text())
                error_id=ident(raw['id'])
                module=(path.parent/raw['module']).resolve()
                if not module.is_file() or module.suffix!='.py' or not module.is_relative_to(path.parent.resolve()): raise ValueError('Unsafe module path')
                add(raw,{'type':'file','path':str(module)})
            except Exception:
                found.append(Plugin(error_id,'?',{}, {},error='Invalid manifest or unsupported API version'))
    # Explicit opt-in: discovery does not import arbitrary installed packages.
    enabled=cfg.get('plugin_entry_points',[])
    if enabled:
        try:
            if not isinstance(enabled,list) or len(enabled)>64: raise ValueError('Invalid entry points')
            for name in enabled: ident(name)
            entries=importlib.metadata.entry_points(group='agentsmon.plugins')
            for name in enabled:
                matches=[ep for ep in entries if ep.name==name]
                try:
                    if len(matches)!=1: raise ValueError('Missing or ambiguous entry point')
                    ep=matches[0]
                    metadata=json.loads(ep.dist.read_text('agentsmon-plugins.json'))
                    add(metadata[ep.name],{'type':'entry','value':ep.value})
                except Exception:
                    found.append(Plugin(name,'?',{}, {},error='Missing, ambiguous or invalid entry point metadata'))
        except Exception:
            found.append(Plugin('entry-points','?',{}, {},error='Invalid entry point configuration or discovery failed'))
    if cfg.get('usage_plugin'):
        try:
            add({'api_version':1,'id':'usage','version':'1.0.0','settings':cfg['usage_plugin']},
                {'type':'entry','value':'agentsmon.usage_plugin'})
        except Exception:
            found.append(Plugin('usage','?',{}, {},error='Invalid or duplicate usage plugin'))
    # An ambiguous id must not keep one of the conflicting plugins executable.
    duplicates={p.id for p in found if sum(q.id==p.id for q in found)>1}
    for p in found:
        if p.id in duplicates: p.error='Duplicate plugin id'
    return found


def invoke(plugin, method, context):
    if plugin.error: raise ValueError(plugin.error)
    request=json.dumps({'target':plugin.target,'method':method,'context':context,'settings':plugin.settings},allow_nan=False).encode()
    if len(request)>1024*1024: raise ValueError('Oversized context')
    env={k:v for k,v in os.environ.items() if k in ('HOME','PATH','LANG','LC_ALL','TMPDIR')}
    env.update(PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
    timeout=plugin.action_timeout if method=='action' else plugin.timeout
    proc=subprocess.Popen([sys.executable,'-I',str(Path(__file__).with_name('plugin_worker.py'))],
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          env=env,cwd=tempfile.gettempdir(),start_new_session=True,bufsize=0)
    deadline=time.monotonic()+timeout
    raw=bytearray(); sent=0
    try:
        # Bound only this protocol's output. RLIMIT_FSIZE would be inherited by a
        # lifecycle action and its descendants, breaking their unrelated log/state writes.
        with selectors.DefaultSelector() as selector:
            for stream,event in ((proc.stdin,selectors.EVENT_WRITE),(proc.stdout,selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(),False);selector.register(stream,event)
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0: raise subprocess.TimeoutExpired('plugin worker',timeout)
                for key,events in selector.select(min(.1,remaining)):
                    if events & selectors.EVENT_READ:
                        try: chunk=os.read(key.fd,65536)
                        except BlockingIOError: continue
                        if not chunk: selector.unregister(key.fileobj);continue
                        raw.extend(chunk)
                        if len(raw)>1024*1024: raise ValueError('Oversized output')
                    else:
                        try: sent+=os.write(key.fd,request[sent:sent+65536])
                        except BlockingIOError: continue
                        except BrokenPipeError: sent=len(request)
                        if sent==len(request): selector.unregister(key.fileobj);proc.stdin.close()
        proc.wait(timeout=max(.001,deadline-time.monotonic()))
        if proc.returncode: raise ValueError('Worker failed')
        return json.loads(raw)
    finally:
        # Also reap descendants which kept running in this worker's process group.
        try: os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait()
        proc.stdin.close();proc.stdout.close()


def validate_result(plugin, value):
    if not isinstance(value,dict) or set(value)-{'tables','patches','columns','hidden_sessions','hidden_services'}: raise ValueError('Invalid result')
    tables=[]; seen=set(); columns=[]; patches=[]
    for c in objects(value.get('columns',[]),8):
        columns.append({'id':plugin.id+':'+ident(c['id']),'title':text(c['title'],80)})
    for t in objects(value.get('tables',[]),16):
        tid=ident(t['id'])
        if tid in seen: raise ValueError('Duplicate table')
        seen.add(tid)
        rows=[row(r,plugin.id) for r in objects(t['rows'])]
        if len({r['id'] for r in rows})!=len(rows): raise ValueError('Duplicate row')
        error=text(t.get('error',''))
        up=t.get('up',True)
        if type(up) is not bool: raise ValueError('Invalid table health')
        tables.append({'id':plugin.id+':'+tid,'title':text(t['title'],100),'rows':rows,
                       'note':text(t.get('note','')),'up':up and not error and all(r['alive'] for r in rows),
                       'error':error})
    for p in objects(value.get('patches',[])):
        if set(p)-{'row_id','label','vendor','account','cells','name_color'}: raise ValueError('Invalid patch')
        update={'row_id':text(p['row_id'],240)}
        for k,v in p.items():
            if k=='cells': update[k]={plugin.id+':'+ident(key):cell(val) for key,val in v.items()}
            else: update[k]=text(v,240)
        patches.append(update)
    hidden=value.get('hidden_sessions',[])
    if not isinstance(hidden,list) or len(hidden)>200: raise ValueError('Invalid hidden sessions')
    services=value.get('hidden_services',[])
    if not isinstance(services,list) or len(services)>200: raise ValueError('Invalid hidden services')
    return {'tables':tables,'columns':columns,'patches':patches,'hidden_sessions':[text(x,240) for x in hidden],
            'hidden_services':[text(x,240) for x in services]}


class Manager:
    def __init__(self,cfg):
        self.plugins=discover(cfg)
        self._snapshot_lock=threading.RLock()
        self._snapshots={}
        self._pending=set()
        self._errors={}
        self._action_locks={p.id:threading.Lock() for p in self.plugins}

    def _one(self,plugin,context):
        try: return validate_result(plugin,invoke(plugin,'collect',context)),''
        except subprocess.TimeoutExpired: return None,'Plugin timed out'
        except Exception: return None,plugin.error or 'Plugin failed or returned invalid data'

    def collect(self,agents,publish=None):
        """Blocking batch API; publish each validated result immediately, in completion order."""
        context=self._context(agents)
        result=self._empty()
        signature=self._agent_signature(context)
        with self._snapshot_lock:
            self._pending.update(p.id for p in self.plugins)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(self._one,p,context):p for p in self.plugins}
            for future in as_completed(futures):
                plugin=futures[future]
                data,error=future.result()
                with self._snapshot_lock:
                    self._record(plugin,signature,data,error)
                if publish: publish(plugin,data,error)
                result['plugins'].append({'id':plugin.id,'version':plugin.version,'api_version':1,'error':error})
                if error: result['errors'].append({'name':'Plugin '+plugin.id+' failed','detail':error})
                else:
                    for key in data: result[key].extend(data[key])
        return result

    @staticmethod
    def _empty():
        return {k:[] for k in ('tables','columns','patches','hidden_sessions','hidden_services','errors','plugins')}

    @staticmethod
    def _context(agents):
        return {'api_version':1,'features':list(FEATURES),
                'agents':[{k:v for k,v in a.items() if k in PUBLIC_FIELDS} for a in agents]}

    @staticmethod
    def _agent_signature(context):
        return json.dumps([{k:v for k,v in a.items() if k not in ('age','latency_ms')}
                           for a in context['agents']],sort_keys=True)

    def _record(self,plugin,signature,data,error):
        """Caller holds _snapshot_lock; successful publication never waits for sibling workers."""
        if error: self._errors[plugin.id]=(time.monotonic(),error)
        else:
            self._snapshots[plugin.id]=(signature,time.monotonic(),data)
            self._errors.pop(plugin.id,None)
        self._pending.discard(plugin.id)

    def snapshot(self,agents):
        """Independent refresh and stale lifetime for every plugin. Never wait on a worker."""
        context=self._context(agents)
        signature=self._agent_signature(context)
        now=time.monotonic()
        with self._snapshot_lock:
            result=self._empty()
            result['loading']=False
            for plugin in self.plugins:
                pid=plugin.id
                cached=self._snapshots.get(pid)
                last=self._errors.get(pid)
                fresh=cached and cached[0]==signature and now-cached[1]<10 and not last
                retry=not last or now-last[0]>=10
                if not fresh and pid not in self._pending and retry:
                    self._pending.add(pid)
                    def refresh(p=plugin):
                        data,error=self._one(p,context)
                        with self._snapshot_lock:
                            self._record(p,signature,data,error)
                    threading.Thread(target=refresh,daemon=True,name='agentsmon-plugin-'+pid).start()
                loading=pid in self._pending
                error=last[1] if last else ''
                result['plugins'].append({'id':pid,'version':plugin.version,'api_version':1,
                                          'loading':loading,'error':error})
                result['loading'] |= loading
                if error: result['errors'].append({'name':'Plugin '+pid+' failed','detail':error})
                if cached and now-cached[1]<MAX_STALE_SECONDS:
                    data=_stale_snapshot(cached[2],now-cached[1]) if loading or error else cached[2]
                    for key in ('tables','columns','patches','hidden_sessions','hidden_services'):
                        result[key].extend(copy.deepcopy(data.get(key,[])))
            return result

    def action(self,table_id,row_id,action_id):
        matches=[p for p in self.plugins if table_id.startswith(p.id+':') and not p.error]
        if len(matches)!=1: return False,'Unknown plugin'
        plugin=matches[0]
        lock=self._action_locks[plugin.id]
        if not lock.acquire(blocking=False): return False,'Plugin action is already running'
        try:
            data=validate_result(plugin,invoke(plugin,'collect',{'api_version':1,'agents':[]}))
            tables=[t for t in data['tables'] if t['id']==table_id]
            rows=[r for t in tables for r in t['rows'] if r['id']==row_id]
            if len(rows)!=1 or action_id not in {a['id'] for a in rows[0].get('row_actions',[])}:
                return False,'Unknown row or action; refresh the dashboard'
            result=invoke(plugin,'action',{'table_id':table_id.split(':',1)[1],'row_id':row_id,'action_id':action_id})
            if not isinstance(result,dict) or type(result.get('ok')) is not bool: raise ValueError('Invalid action result')
            return result['ok'],text(result['message'])
        except Exception:
            return False,'Plugin action failed or timed out; verify the result before retrying'
        finally: lock.release()


_lock=threading.RLock()
_manager=None
_signature=None


def manager(cfg):
    global _manager,_signature
    signature=json.dumps({k:cfg.get(k) for k in ('modules_dir','plugin_entry_points','usage_plugin')},sort_keys=True)
    with _lock:
        if _manager is None or signature!=_signature:
            _manager=Manager(cfg); _signature=signature
        return _manager


#: How long the last good plugin snapshot may be shown (marked stale, read-only) while a refresh runs.
#: Beyond this the dashboard shows no plugin data at all rather than old numbers.
MAX_STALE_SECONDS=600


def _stale_snapshot(result,age):
    """Copy of a cached snapshot for display during a refresh: flagged stale, never actionable."""
    out=copy.deepcopy(result)
    mins=max(1,int(age//60))
    note='Data z doby před %d min, aktualizuji…'%mins if age>=60 else 'Aktualizuji…'
    for t in out.get('tables',[]):
        t['stale']=True
        t['note']=(str(t.get('note') or '')+' '+note).strip()
        for r in t.get('rows',[]):
            r.pop('row_actions',None)
            r['actions_locked']=True
    for p in out.get('patches',[]):
        if isinstance(p,dict): p.pop('row_actions',None)
    out['loading']=True
    out['stale_seconds']=int(age)
    return out


def collect(cfg,agents):
    """Never run or wait for plugins on the HTTP request thread."""
    return manager(cfg).snapshot(agents)
