"""Hardening step 1 (no access-policy change): Host allowlist vs DNS rebinding, Fetch-Metadata /
Origin checks on POST, security headers + CSP, audit log, POST rate limit, optional /metrics token
and salted scrypt password hashes (legacy SHA-256 still accepted)."""
import base64
import hashlib
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentsmon import dashboard  # noqa: E402

AUTH = "Basic " + base64.b64encode(b"fixture:test-password").decode()


class Base(unittest.TestCase):
    cfg = {"dashboard": {}}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = patch.dict(os.environ, {"AGENTSMON_STATE": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.server = dashboard.make_server("127.0.0.1", 0, self.cfg)
        self.port = self.server.server_port
        th = threading.Thread(target=self.server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), th.join(2)))

    def req(self, method, path, host=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def post(self, payload=None, headers=None, host=None):
        h = {"Content-Type": "application/json", "X-Agentsmon": "action"}
        h.update(headers or {})
        body = json.dumps(payload if payload is not None else {"name": "LANA", "action": "restart"}).encode()
        return self.req("POST", "/api/agent/action", host=host, headers=h, body=body)

    def audit_lines(self):
        p = self.state / "audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class HostAllowlist(Base):
    def test_bad_host_gets_421_on_every_route(self):
        for path in ("/", "/api/state", "/metrics", "/static/tailwind.js", "/nope"):
            with self.subTest(path=path):
                status, headers, body = self.req("GET", path, host="evil.example")
                self.assertEqual(status, 421)
                self.assertIn("neznámá adresa", body.decode())
                self.assertEqual(headers.get("x-frame-options"), "DENY")
        for host in (f"evil.example:{self.port}", "127.0.0.1:1", "127.0.0.1.evil.example", f"user@127.0.0.1:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.req("GET", "/", host=host)[0], 421)

    def test_bad_host_post_is_rejected_and_audited(self):
        with patch.object(dashboard, "_agent_action") as act:
            self.assertEqual(self.post(host="rebind.attacker.example")[0], 421)
            act.assert_not_called()
        line = self.audit_lines()[-1]
        self.assertEqual((line["status"], line["result"], line["reason"]), (421, "rejected", "host"))
        self.assertEqual(line["host"], "rebind.attacker.example")

    def test_good_hosts_are_served(self):
        hn = socket.gethostname().lower()
        with patch.object(dashboard, "_state", return_value=b"{}"):
            for host in (f"127.0.0.1:{self.port}", "127.0.0.1", f"localhost:{self.port}", f"[::1]:{self.port}",
                         f"{hn}:{self.port}", f"{hn}.local:{self.port}", f"{hn}.lan", f"LOCALHOST:{self.port}"):
                with self.subTest(host=host):
                    self.assertEqual(self.req("GET", "/api/state", host=host)[0], 200)

    def test_allowlist_for_the_lan_bind_and_config_extras(self):
        cfg = {"dashboard": {"allowed_hosts": ["ai-agents.tail1234.ts.net", "proxy.example:443", "bad/x"]}}
        allow = dashboard.allowed_hosts("192.168.1.153", 8765, cfg)
        ok = ["192.168.1.153:8765", "192.168.1.153", "localhost:8765", "ai-agents.tail1234.ts.net",
              "ai-agents.tail1234.ts.net:8765", "proxy.example:443"]
        bad = ["192.168.1.153:80", "proxy.example", "proxy.example:8765", "10.0.0.1:8765", "", None, "bad/x"]
        for h in ok:
            self.assertTrue(dashboard._host_allowed(h, allow), h)
        for h in bad:
            self.assertFalse(dashboard._host_allowed(h, allow), h)


class Headers(Base):
    def test_security_headers_on_page_api_and_errors(self):
        with patch.object(dashboard, "_state", return_value=b"{}"):
            for path in ("/", "/api/state", "/nope"):
                status, h, _ = self.req("GET", path)
                with self.subTest(path=path):
                    self.assertEqual(h["x-content-type-options"], "nosniff")
                    self.assertEqual(h["referrer-policy"], "no-referrer")
                    self.assertEqual(h["x-frame-options"], "DENY")
                    self.assertEqual(h["cross-origin-opener-policy"], "same-origin")
                    self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
                    self.assertIn("default-src 'self'", h["content-security-policy"])
                    self.assertNotIn("python", h.get("server", "").lower())
            self.assertEqual(self.req("GET", "/api/state")[1]["cache-control"], "no-store")
            # The page embeds identity/role and action JS: never reuse a cached copy.
            self.assertIn("no-store", self.req("GET", "/")[1].get("cache-control", ""))

    def test_csp_fits_the_page(self):
        import re
        page = dashboard.PAGE
        self.assertFalse(re.search(r'<script[^>]+src=["\']https?://', page))
        self.assertFalse(re.search(r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']https?://', page))
        self.assertFalse(re.search(r'fetch\(\s*["\']https?://', page))
        self.assertIn("script-src 'self' 'unsafe-inline'", dashboard.CSP)
        self.assertIn("img-src 'self' data:", dashboard.CSP)   # favicon is a data: SVG


class PostGuards(Base):
    def test_cross_site_fetch_metadata_and_origin_are_refused(self):
        with patch.object(dashboard, "_agent_action", return_value=(True, "ok")) as act:
            for extra in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                          {"Origin": "http://evil.example"}, {"Origin": "null"},
                          {"Origin": f"http://127.0.0.1:{self.port}.evil.example"}):
                with self.subTest(extra=extra):
                    status, _, body = self.post(headers=extra)
                    self.assertEqual(status, 403)
                    self.assertFalse(json.loads(body)["ok"])
            act.assert_not_called()
            for extra in ({"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{self.port}"},
                          {"Sec-Fetch-Site": "none"}, {}):
                with self.subTest(extra=extra):
                    self.assertEqual(self.post(headers=extra)[0], 200)
        reasons = [l.get("reason") for l in self.audit_lines() if l["status"] == 403]
        self.assertEqual(set(reasons), {"sec-fetch-site", "origin"})

    def test_json_content_type_required(self):
        with patch.object(dashboard, "_agent_action", return_value=(True, "ok")) as act:
            status, _, body = self.post(headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 415)
            self.assertEqual(self.post(headers={"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(self.post(headers={"Content-Type": "application/json; charset=utf-8"})[0], 200)
            self.assertEqual(act.call_count, 1)
        self.assertEqual(self.req("POST", "/api/agent/action", headers={"Content-Type": "application/json",
                         "X-Agentsmon": "action"}, body=b"name=x&action=stop")[0], 400)

    def test_missing_custom_header_still_refused(self):
        with patch.object(dashboard, "_agent_action") as act:
            self.assertEqual(self.post(headers={"X-Agentsmon": ""})[0], 403)
            act.assert_not_called()


class AuditAndRate(Base):
    cfg = {"dashboard": {"auth": {"user": "fixture", "pwhash": dashboard.password_hash("test-password")},
                         "post_rate_per_min": 4}}

    def test_action_is_audited_without_secrets(self):
        with patch.object(dashboard, "_agent_action", return_value=(True, "Restartováno")):
            status, _, body = self.post(headers={"Authorization": AUTH, "User-Agent": "UA/" + "x" * 400})
        self.assertEqual(status, 200)
        raw = (self.state / "audit.jsonl").read_text()
        self.assertNotIn("test-password", raw)
        self.assertNotIn(AUTH.split()[1], raw)
        line = self.audit_lines()[-1]
        self.assertEqual((line["user"], line["name"], line["action"], line["status"], line["result"]),
                         ("fixture", "LANA", "restart", 200, "ok"))
        self.assertEqual(line["client_ip"], "127.0.0.1")
        self.assertEqual(line["route"], "/api/agent/action")
        self.assertLessEqual(len(line["user_agent"]), 201)
        self.assertEqual(line["message"], "Restartováno")
        self.assertEqual(os.stat(self.state / "audit.jsonl").st_mode & 0o777, 0o600)

    def test_plugin_row_action_and_unauthenticated_attempt_are_audited(self):
        with patch.object(dashboard.plugins, "manager") as manager, \
             patch.object(dashboard.config, "load", return_value=self.cfg):
            manager.return_value.action.return_value = (False, "Reset už byl uplatněn")
            self.assertEqual(self.post({"table": "quota:acc", "row": "r1", "action": "redeem_reset"})[0], 401)
            status, _, _ = self.post({"table": "quota:acc", "row": "r1", "action": "redeem_reset"},
                                     headers={"Authorization": AUTH})
        self.assertEqual(status, 409)
        first, second = self.audit_lines()[-2:]
        self.assertEqual((first["status"], first["reason"], first["user"]), (401, "auth", None))
        self.assertEqual((second["table"], second["row"], second["action"], second["result"]),
                         ("quota:acc", "r1", "redeem_reset", "error"))

    def test_rate_limit_returns_429(self):
        with patch.object(dashboard, "_agent_action", return_value=(True, "ok")) as act:
            codes = [self.post(headers={"Authorization": AUTH})[0] for _ in range(6)]
        self.assertEqual(codes[:4], [200] * 4)
        self.assertEqual(codes[4:], [429, 429])
        self.assertEqual(act.call_count, 4)
        self.assertEqual(self.audit_lines()[-1]["reason"], "rate-limit")

    def test_bucket_refills(self):
        rl = dashboard._RateLimiter(10)
        self.assertEqual(sum(rl.allow("a", now=0.0) for _ in range(12)), 10)
        self.assertFalse(rl.allow("a", now=1.0))
        self.assertTrue(rl.allow("a", now=7.0))
        self.assertTrue(rl.allow("b", now=7.0))

    def test_audit_rotates(self):
        with patch.object(dashboard, "AUDIT_MAX_BYTES", 300):
            for i in range(10):
                dashboard.audit({"i": i, "pad": "x" * 50})
        self.assertTrue((self.state / "audit.jsonl.1").exists())
        self.assertLessEqual((self.state / "audit.jsonl").stat().st_size, 300)
        self.assertFalse((self.state / "audit.jsonl.2").exists())


class MetricsToken(Base):
    cfg = {"dashboard": {"metrics_token": "s3cret-token"}}

    def test_loopback_without_token_and_remote_with_token(self):
        with patch.object(dashboard, "_state_data", return_value={}), \
             patch.object(dashboard.metrics, "render", return_value="x 1\n"):
            self.assertEqual(self.req("GET", "/metrics")[0], 200)                 # loopback client
            with patch.object(dashboard, "_loopback_peer", return_value=False):
                status, h, _ = self.req("GET", "/metrics")
                self.assertEqual(status, 401)
                self.assertIn("Bearer", h["www-authenticate"])
                self.assertEqual(self.req("GET", "/metrics", headers={"Authorization": "Bearer wrong"})[0], 401)
                status, h, body = self.req("GET", "/metrics", headers={"Authorization": "Bearer s3cret-token"})
                self.assertEqual((status, body), (200, b"x 1\n"))
                self.assertEqual(h["cache-control"], "no-store")
                self.assertEqual(self.req("GET", "/", headers={})[0], 200)  # token gates /metrics only

    def test_unset_token_keeps_old_behaviour(self):
        self.assertEqual(dashboard._metrics_token({}), "")
        self.assertTrue(dashboard._loopback_peer("::ffff:127.0.0.1"))
        self.assertFalse(dashboard._loopback_peer("192.168.1.20"))


class Hashes(unittest.TestCase):
    def test_new_hashes_are_salted_scrypt(self):
        a, b = dashboard.password_hash("pw"), dashboard.password_hash("pw")
        self.assertTrue(a.startswith("scrypt$16384$8$1$"))
        self.assertNotEqual(a, b)
        self.assertTrue(dashboard.verify_password("pw", a))
        self.assertFalse(dashboard.verify_password("pW", a))

    def test_legacy_sha256_still_accepted(self):
        legacy = hashlib.sha256(b"old-pw").hexdigest()
        self.assertEqual(dashboard.legacy_password_hash("old-pw"), legacy)
        self.assertTrue(dashboard.verify_password("old-pw", legacy))
        self.assertTrue(dashboard.verify_password("old-pw", legacy.upper()))
        self.assertFalse(dashboard.verify_password("other", legacy))
        hdr = "Basic " + base64.b64encode(b"admin:old-pw").decode()
        self.assertTrue(dashboard._auth_ok(hdr, "admin", legacy))
        self.assertFalse(dashboard._auth_ok(hdr, "root", legacy))

    def test_garbage_and_hostile_hashes_rejected(self):
        for stored in ("", None, "abc", "scrypt$x$8$1$00$00", "scrypt$1048577$8$1$00$" + "00" * 32,
                       "scrypt$16384$8$1$zz$" + "00" * 32, "scrypt$4194304$64$1$00$" + "00" * 32):
            self.assertFalse(dashboard.verify_password("pw", stored), stored)

    def test_auth_header_edge_cases(self):
        h = dashboard.password_hash("pw")
        ok = "Basic " + base64.b64encode(b"admin:pw").decode()
        self.assertTrue(dashboard._auth_ok(ok, "admin", h))
        self.assertTrue(dashboard._auth_ok(ok, "admin", h))          # cached path
        self.assertFalse(dashboard._auth_ok(ok, "admin", dashboard.password_hash("pw2")))
        uni = "Basic " + base64.b64encode("žluťoučký:pw".encode()).decode()
        self.assertFalse(dashboard._auth_ok(uni, "admin", h))         # non-ASCII must not raise
        for bad in (None, "", "Bearer x", "Basic !!!", "Basic " + base64.b64encode(b"nocolon").decode()):
            self.assertFalse(dashboard._auth_ok(bad, "admin", h))


if __name__ == "__main__":
    unittest.main()
