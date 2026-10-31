"""Security step 3: dashboard.auth_mode = "tailscale" (loopback bind, Tailscale Serve identity
headers, admin/viewer roles, step-up confirmation for destructive actions)."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentsmon import dashboard  # noqa: E402

ADMIN, OTHER = "owner@example.com", "guest@example.com"


class Base(unittest.TestCase):
    dcfg = {"auth_mode": "tailscale", "admins": [ADMIN], "viewers": []}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = patch.dict(os.environ, {"AGENTSMON_STATE": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.server = dashboard.make_server("127.0.0.1", 0, {"dashboard": dict(self.dcfg)})
        self.port = self.server.server_port
        th = threading.Thread(target=self.server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), th.join(2)))

    def req(self, method, path, login=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if login:
            h["Tailscale-User-Login"] = login
            h["Tailscale-User-Name"] = "=?utf-8?q?Jan_N=C3=A9meth?="
            h["X-Forwarded-For"] = "100.64.0.7"
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def post(self, payload, login=ADMIN, confirm=None, headers=None):
        h = {"Content-Type": "application/json", "X-Agentsmon": "action"}
        if confirm:
            h["X-Agentsmon-Confirm"] = confirm
        h.update(headers or {})
        status, data = self.req("POST", "/api/agent/action", login, h, json.dumps(payload).encode())
        return status, json.loads(data or b"{}")

    def whoami(self, login=None, headers=None):
        status, data = self.req("GET", "/api/whoami", login, headers)
        return status, (json.loads(data) if status == 200 else {"raw": data})

    def audit_lines(self):
        p = self.state / "audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class Roles(Base):
    dcfg = {"auth_mode": "tailscale", "admins": [ADMIN.upper()], "viewers": ["reader@example.com"]}

    def test_header_from_loopback_maps_to_roles(self):
        status, me = self.whoami(ADMIN)
        self.assertEqual((status, me["role"], me["user"], me["source"]), (200, "admin", ADMIN, "tailscale"))
        self.assertEqual(me["name"], "Jan Németh")
        self.assertEqual(self.whoami("reader@example.com")[1]["role"], "viewer")

    def test_unknown_login_gets_czech_403(self):
        status, body = self.req("GET", "/", OTHER)
        self.assertEqual(status, 403)
        self.assertIn("Přístup odepřen", body.decode())
        self.assertIn(OTHER, body.decode())
        self.assertEqual(self.req("GET", "/api/state", OTHER)[0], 403)
        line = self.audit_lines()[-1]
        self.assertEqual((line["user"], line["reason"], line["status"]), (OTHER, "not-allowed", 403))

    def test_local_request_without_identity_is_read_only_viewer(self):
        status, me = self.whoami()
        self.assertEqual((status, me["role"], me["user"], me["source"]), (200, "viewer", None, "loopback"))
        with patch.object(dashboard, "_state", return_value=b"{}"):
            self.assertEqual(self.req("GET", "/api/state")[0], 200)
        with patch.object(dashboard, "_agent_action") as act:
            status, j = self.post({"name": "LANA", "action": "restart"}, login=None)
            act.assert_not_called()
        self.assertEqual((status, j["error"]), (403, "role"))

    def test_proxied_request_without_identity_is_refused(self):
        self.assertEqual(self.whoami(headers={"X-Forwarded-For": "100.64.0.9"})[0], 403)
        self.assertEqual(self.req("GET", "/", headers={"Tailscale-Funnel-Request": "?1"})[0], 403)

    def test_duplicate_identity_header_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/api/whoami", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Tailscale-User-Login", ADMIN)
        conn.putheader("Tailscale-User-Login", "x@example.com")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_viewer_post_forbidden_and_audited(self):
        with patch.object(dashboard, "_agent_action") as act:
            status, j = self.post({"name": "LANA", "action": "restart"}, login="reader@example.com")
            act.assert_not_called()
        self.assertEqual(status, 403)
        self.assertIn("správce", j["message"])
        line = self.audit_lines()[-1]
        self.assertEqual((line["user"], line["role"], line["reason"], line["name"]),
                         ("reader@example.com", "viewer", "role", "LANA"))

    def test_admin_restart_is_single_step_and_audited_with_login(self):
        with patch.object(dashboard, "_agent_action", return_value=(True, "ok")) as act:
            status, j = self.post({"name": "LANA", "action": "restart"})
        self.assertEqual((status, j["ok"]), (200, True))
        act.assert_called_once_with("LANA", "restart")
        line = self.audit_lines()[-1]
        self.assertEqual((line["user"], line["role"], line["source"], line["result"]),
                         (ADMIN, "admin", "tailscale", "ok"))

    def test_page_header_shows_login_and_role(self):
        status, body = self.req("GET", "/", ADMIN)
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn(f"{ADMIN} · správce", html)
        self.assertIn('data-role="admin"', html)
        self.assertIn("reader@example.com · čtenář", self.req("GET", "/", "reader@example.com")[1].decode())
        local = self.req("GET", "/")[1].decode()
        self.assertIn('data-role="viewer"', local)
        self.assertNotIn("__AGENTSMON_", local)


class StepUp(Base):
    def test_stop_needs_one_time_confirmation(self):
        with patch.object(dashboard, "_agent_action", return_value=(True, "stopped")) as act:
            status, j = self.post({"name": "LANA", "action": "stop"})
            self.assertEqual(status, 428)
            self.assertIn("Zastavit agenta „LANA“", j["summary"])
            self.assertEqual(j["expires_in"], 120)
            act.assert_not_called()
            tok = j["confirm_token"]
            status, j = self.post({"name": "LANA", "action": "stop"}, confirm=tok)
            self.assertEqual((status, j["ok"]), (200, True))
            act.assert_called_once_with("LANA", "stop")
            # single use
            status, j = self.post({"name": "LANA", "action": "stop"}, confirm=tok)
            self.assertEqual((status, j["error"]), (403, "confirm-unknown"))
            self.assertEqual(act.call_count, 1)
        results = [(x["status"], x["result"]) for x in self.audit_lines()]
        self.assertIn((428, "confirm-required"), results)
        ok = [x for x in self.audit_lines() if x["result"] == "ok"][0]
        self.assertTrue(ok["confirmed"])
        self.assertEqual(ok["user"], ADMIN)

    def test_token_bound_to_action_target_and_user(self):
        with patch.object(dashboard, "_agent_action", return_value=(True, "x")) as act:
            tok = self.post({"name": "LANA", "action": "stop"})[1]["confirm_token"]
            self.assertEqual(self.post({"name": "OTHER", "action": "stop"}, confirm=tok)[1]["error"],
                             "confirm-mismatch")
            act.assert_not_called()

    def test_token_bound_to_user_and_action(self):
        conf = dashboard.ConfirmTokens(120)
        t = conf.issue(ADMIN, "stop", ("LANA",))
        self.assertEqual(conf.redeem(t, "mallory@example.com", "stop", ("LANA",)), "mismatch")
        t = conf.issue(ADMIN, "stop", ("LANA",))
        self.assertEqual(conf.redeem(t, ADMIN, "redeem_reset", ("LANA",)), "mismatch")
        t = conf.issue(ADMIN, "stop", ("LANA",))
        self.assertIsNone(conf.redeem(t, ADMIN, "stop", ("LANA",)))

    def test_plugin_redeem_reset_needs_confirmation_and_token_expires(self):
        clock = [1000.0]
        conf = dashboard.ConfirmTokens(120, clock=lambda: clock[0])
        t = conf.issue(ADMIN, "redeem_reset", ("codex:accounts", "a1"))
        clock[0] += 121
        self.assertEqual(conf.redeem(t, ADMIN, "redeem_reset", ("codex:accounts", "a1")), "expired")
        payload = {"name": "acct", "action": "redeem_reset", "table": "codex:accounts", "row": "a1"}
        with patch.object(dashboard.plugins, "manager") as mgr:
            status, j = self.post(payload)
            self.assertEqual(status, 428)
            self.assertIn("nevratné", j["summary"])
            mgr.assert_not_called()

    def test_expired_token_via_http(self):
        with patch.object(dashboard, "_agent_action") as act:
            tok = self.post({"name": "LANA", "action": "stop"})[1]["confirm_token"]
            store = self.server.agentsmon_confirms.items
            user, action, target, exp = store[tok]
            store[tok] = (user, action, target, exp - 10_000)      # age it past the 120 s TTL
            status, j = self.post({"name": "LANA", "action": "stop"}, confirm=tok)
            self.assertEqual((status, j["error"]), (403, "confirm-expired"))
            act.assert_not_called()

    def test_classification(self):
        d = {}
        self.assertTrue(dashboard.needs_step_up("stop", None, d))
        self.assertTrue(dashboard.needs_step_up("redeem_reset", "p:t", d))
        self.assertTrue(dashboard.needs_step_up("delete_cache", "p:t", d))
        self.assertFalse(dashboard.needs_step_up("restart", None, d))
        self.assertFalse(dashboard.needs_step_up("restart", "p:t", d))
        self.assertTrue(dashboard.needs_step_up("restart", None, {"step_up_actions": ["restart"]}))


class Startup(unittest.TestCase):
    def test_tailscale_mode_refuses_non_loopback_bind(self):
        for host in ("0.0.0.0", "192.168.1.153", "100.64.0.1", "::", "localhost"):
            with self.subTest(host=host), self.assertRaises(SystemExit):
                dashboard.make_server(host, 0, {"dashboard": {"auth_mode": "tailscale", "admins": [ADMIN]}})

    def test_unknown_mode_refused_and_loopback_ok(self):
        with self.assertRaises(SystemExit):
            dashboard.check_auth_mode("127.0.0.1", {"auth_mode": "magic"})
        self.assertEqual(dashboard.check_auth_mode("::1", {"auth_mode": "tailscale"}), "tailscale")
        self.assertEqual(dashboard.check_auth_mode("192.168.1.153", {}), "basic")

    def test_identity_ignored_from_non_loopback_peer(self):
        class H(dict):
            def get_all(self, k):
                return [self[k]] if k in self else None
        ident = dashboard.tailscale_identity(H({"Tailscale-User-Login": ADMIN}), "192.168.1.20",
                                             {"admins": [ADMIN]})
        self.assertIsNone(ident["role"])
        ident = dashboard.tailscale_identity(H({"Tailscale-User-Login": ADMIN}), "::ffff:127.0.0.1",
                                             {"admins": [ADMIN]})
        self.assertEqual(ident["role"], "admin")


class BasicModeUnchanged(unittest.TestCase):
    def test_no_step_up_and_admin_without_auth_mode(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, {"AGENTSMON_STATE": tmp.name}):
            srv = dashboard.make_server("127.0.0.1", 0, {"dashboard": {}})
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                with patch.object(dashboard, "_agent_action", return_value=(True, "ok")) as act:
                    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
                    conn.request("POST", "/api/agent/action", body=json.dumps({"name": "A", "action": "stop"}),
                                 headers={"Host": f"127.0.0.1:{srv.server_port}", "X-Agentsmon": "action",
                                          "Content-Type": "application/json",
                                          "Tailscale-User-Login": "spoof@example.com"})
                    self.assertEqual(conn.getresponse().status, 200)
                    conn.close()
                    act.assert_called_once_with("A", "stop")
            finally:
                srv.shutdown(); srv.server_close(); th.join(2)


if __name__ == "__main__":
    unittest.main()
