import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class PluginContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def plugin(self, name='fixture', body=None, **extra):
        folder=self.root/name; folder.mkdir()
        (folder/'plugin.py').write_text(body or "def collect(ctx, settings):\n return {'tables':[{'id':'team','title':'Team','rows':[{'id':'one','name':'Worker','alive':True,'label':'Model'}]}]}\n")
        (folder/'agentsmon-plugin.json').write_text(json.dumps({'api_version':1,'id':name,'version':'1.0.0','module':'plugin.py',**extra}))
        return {'modules_dir':str(self.root),'dashboard':{'auth':{'pwhash':'PRIVATE_AUTH'}},'secret':'PRIVATE_TOKEN'}

    def test_discovery_namespace_and_no_auth_context(self):
        from agentsmon.plugins import Manager
        cfg=self.plugin(body="def collect(ctx, settings):\n assert 'PRIVATE' not in str(ctx) and 'PRIVATE' not in str(settings)\n return {'tables':[{'id':'t','title':'Team','rows':[{'id':'a','name':'A','alive':True}]}]}\n")
        result=Manager(cfg).collect([{'name':'A','restart':'PRIVATE_TOKEN','id':'a'}])
        self.assertFalse(result['errors'])
        self.assertEqual(result['tables'][0]['id'],'fixture:t')

    def test_exception_timeout_and_bad_version_are_visible_and_other_plugin_survives(self):
        from agentsmon.plugins import Manager
        cfg=self.plugin()
        self.plugin('crash',"def collect(ctx, settings):\n raise RuntimeError('secret')\n")
        self.plugin('hang',"import time\ndef collect(ctx, settings):\n time.sleep(30)\n",timeout_seconds=.2)
        self.plugin('future',api_version=999)
        start=time.monotonic(); result=Manager(cfg).collect([])
        self.assertLess(time.monotonic()-start,4)
        self.assertEqual(len(result['tables']),1)
        self.assertEqual(len(result['errors']),3)
        self.assertNotIn('secret',str(result['errors']))

    def test_action_requires_advertised_plugin_row_action_and_cannot_target_core(self):
        from agentsmon.plugins import Manager
        cfg=self.plugin(body="def collect(ctx, settings):\n return {'tables':[{'id':'t','title':'Team','rows':[{'id':'a','name':'A','alive':True,'row_actions':[{'id':'reload','label':'Reload','icon':'restart'}]}]}]}\ndef action(request, settings):\n return {'ok':True,'message':request['row_id']}\n")
        manager=Manager(cfg)
        self.assertEqual(manager.action('fixture:t','a','reload'),(True,'a'))
        for args in [('fixture:t','other','reload'),('persistent','a','reload'),('fixture:t','a','stop')]:
            self.assertFalse(manager.action(*args)[0])

    def test_invalid_markup_and_executable_urls_are_rejected(self):
        from agentsmon.plugins import Manager
        cfg=self.plugin(body="def collect(ctx, settings):\n return {'tables':[{'id':'t','title':'T','rows':[{'id':'a','name':'A','alive':True,'telegram_url':'javascript:alert(1)'}]}]}\n")
        self.assertTrue(Manager(cfg).collect([])['errors'])

    def test_environment_secrets_and_main_process_imports_are_not_shared(self):
        import sys
        from agentsmon.plugins import Manager
        cfg=self.plugin(body="import os\ndef collect(ctx, settings):\n assert 'CANARY_SECRET' not in os.environ\n return {}\n")
        with patch.dict(os.environ,{'CANARY_SECRET':'PRIVATE_TOKEN'}):
            self.assertFalse(Manager(cfg).collect([])['errors'])
        self.assertNotIn('agentsmon_extension',sys.modules)

    def test_output_flood_is_bounded_and_plugin_is_error(self):
        from agentsmon.plugins import Manager
        cfg=self.plugin(body="def collect(ctx, settings):\n return {'x':'a'*2000000}\n")
        self.assertTrue(Manager(cfg).collect([])['errors'])

    def test_output_cap_does_not_become_an_inherited_file_limit_for_actions(self):
        from agentsmon.plugins import Manager
        cfg=self.plugin(body="import resource\ndef collect(ctx, settings):\n soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE)\n assert soft==resource.RLIM_INFINITY or soft>8*1024*1024\n return {}\n")
        self.assertFalse(Manager(cfg).collect([])['errors'])

    def test_api_plugin_helper_does_not_become_legacy_service(self):
        from agentsmon import config
        cfg=self.plugin();(self.root/'fixture/plugin.py').chmod(0o755)
        self.assertEqual(config.discover_modules(cfg),[])

    def test_patches_are_limited_to_display_fields(self):
        from agentsmon.plugins import Manager
        cfg=self.plugin(body="def collect(ctx, settings):\n return {'patches':[{'row_id':'a','restart':'touch PRIVATE'}]}\n")
        self.assertTrue(Manager(cfg).collect([])['errors'])

    def test_entry_points_require_metadata_opt_in_and_errors_are_loud(self):
        from agentsmon.plugins import Manager
        metadata=json.dumps({'fixture':{'id':'fixture','version':'1.2.3','api_version':1}})
        entry=SimpleNamespace(name='fixture',value='agentsmon.usage_plugin',dist=SimpleNamespace(read_text=lambda name:metadata))
        with patch('agentsmon.plugins.importlib.metadata.entry_points',return_value=[entry]):
            cfg={'modules_dir':str(self.root),'plugin_entry_points':[]}
            self.assertEqual(Manager(cfg).plugins,[])
            cfg['plugin_entry_points']=['fixture']
            result=Manager(cfg).collect([])
            self.assertFalse(result['errors']);self.assertEqual(result['plugins'][0]['version'],'1.2.3')
            cfg['plugin_entry_points']=['missing']
            self.assertTrue(Manager(cfg).collect([])['errors'])

    def test_duplicate_id_and_table_error_fail_closed(self):
        from agentsmon.plugins import Manager,Plugin,validate_result
        cfg=self.plugin()
        self.plugin('conflict',id='fixture')
        result=Manager(cfg).collect([])
        self.assertFalse(result['tables']);self.assertEqual(len(result['errors']),2)
        p=Plugin('test','1',{}, {})
        for value in ({'error':'Supervisor unavailable'},{'up':False}):
            table={'id':'t','title':'Team','rows':[{'id':'one','name':'A','alive':True}],**value}
            self.assertFalse(validate_result(p,{'tables':[table]})['tables'][0]['up'])

    def test_http_collection_is_nonblocking_and_stale_cache_cannot_stay_green(self):
        from agentsmon import plugins
        cfg=self.plugin('hang',"import time\ndef collect(ctx, settings):\n time.sleep(30)\n",timeout_seconds=.2)
        with patch.multiple(plugins,_manager=None,_signature=None,_cache=None,_refreshing=False):
            start=time.monotonic();data=plugins.collect(cfg,[])
            self.assertLess(time.monotonic()-start,.15)
            self.assertEqual(data['errors'],[])
            self.assertTrue(data['loading'])
            deadline=time.monotonic()+3
            while plugins._refreshing and time.monotonic()<deadline: time.sleep(.02)
            data=plugins.collect(cfg,[])
            self.assertIn('timed out',data['errors'][0]['detail'])
            self.assertFalse(data.get('loading',False))
            # 31 s old: shown, but visibly stale and never actionable while the refresh runs.
            row={'id':'r','name':'R','alive':True,'row_actions':[{'id':'reset','label':'Reset'}]}
            plugins._cache=('',time.monotonic()-31,{'tables':[{'up':True,'rows':[row]}],'patches':[{'row_actions':[1]}]})
            data=plugins.collect(cfg,[])
            self.assertTrue(data['loading'])
            self.assertTrue(data['tables'][0]['stale'])
            self.assertIn('Aktualizuji', data['tables'][0]['note'])
            self.assertNotIn('row_actions',data['tables'][0]['rows'][0])
            self.assertTrue(data['tables'][0]['rows'][0]['actions_locked'])
            self.assertNotIn('row_actions',data['patches'][0])
            self.assertIn('row_actions',plugins._cache[2]['tables'][0]['rows'][0], 'cache itself untouched')
            while plugins._refreshing and time.monotonic()<deadline: time.sleep(.02)
            # Older than MAX_STALE_SECONDS: no plugin data at all rather than old numbers.
            plugins._cache=('',time.monotonic()-plugins.MAX_STALE_SECONDS-1,{'tables':[{'up':True}]})
            data=plugins.collect(cfg,[])
            self.assertFalse(data['tables'])
            self.assertTrue(data['loading'])
            while plugins._refreshing and time.monotonic()<deadline: time.sleep(.02)
