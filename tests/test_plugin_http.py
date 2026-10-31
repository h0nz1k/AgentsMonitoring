import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from agentsmon import dashboard


class PluginHTTPGuard(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{'AGENTSMON_STATE':tmp.name});env.start();self.addCleanup(env.stop)
        self.cfg={'dashboard':{'auth':{'user':'fixture','pwhash':dashboard.password_hash('test-password')}}}
        self.server=dashboard.make_server('127.0.0.1',0,self.cfg)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def post(self,headers):
        payload=json.dumps({'table':'fixture:team','row':'one','action':'reload','name':'collision'}).encode()
        req=urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/api/agent/action',data=payload,headers={'Content-Type':'application/json',**headers})
        try:
            with urllib.request.urlopen(req,timeout=2) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as e:return e.code,None

    def test_auth_and_cross_site_guard_precede_plugin_action(self):
        with patch.object(dashboard.plugins,'manager') as manager,patch.object(dashboard.config,'load',return_value=self.cfg),patch.object(dashboard,'_agent_action') as core:
            manager.return_value.action.return_value=(True,'Done')
            self.assertEqual(self.post({'X-Agentsmon':'action'})[0],401)
            auth='Basic '+base64.b64encode(b'fixture:test-password').decode()
            self.assertEqual(self.post({'Authorization':auth})[0],403)
            manager.assert_not_called()
            status,payload=self.post({'Authorization':auth,'X-Agentsmon':'action'})
            self.assertEqual(status,200);self.assertTrue(payload['ok'])
            manager.return_value.action.assert_called_once_with('fixture:team','one','reload')
            core.assert_not_called()
