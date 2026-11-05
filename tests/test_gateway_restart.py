"""Manual Hermes gateway restart from a Telegram-lane outage: step-up, getMe precheck,
10-minute cooldown, actions.jsonl audit without secrets."""
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentsmon import dashboard, gateway_restart as gw  # noqa: E402

ADMIN = "owner@example.com"
FAKE_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


class FakeHTTP:
    """urlopen stand-in: records the URL (for secret checks) and returns a canned body."""

    def __init__(self, body=b'{"ok":true,"result":{"username":"lane_bot"}}', exc=None):
        self.body, self.exc, self.urls = body, exc, []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return io.BytesIO(self.body)


class PrecheckUnit(unittest.TestCase):
    def test_ok_and_timeout_messages_never_echo_token(self):
        with patch.object(gw, "read_bot_token", return_value=FAKE_TOKEN):
            ok = gw.precheck("lana", urlopen=FakeHTTP())
            self.assertTrue(ok["ok"])
            self.assertTrue(ok["helps"])
            self.assertEqual(ok["status"], "ok")
            self.assertIn("getMe OK", ok["message_cs"])
            self.assertNotIn(FAKE_TOKEN, json.dumps(ok))

            down = gw.precheck("lana", urlopen=FakeHTTP(exc=TimeoutError("timed out")))
            self.assertFalse(down["ok"])
            self.assertFalse(down["helps"])
            self.assertEqual(down["status"], "timeout")
            self.assertIn("neodpovídá", down["message_cs"])
            self.assertNotIn(FAKE_TOKEN, json.dumps(down))

    def test_missing_token(self):
        with patch.object(gw, "read_bot_token", return_value=None):
            r = gw.precheck("lana", urlopen=FakeHTTP())
            self.assertEqual(r["status"], "no_token")
            self.assertIsNone(r["helps"])

    def test_read_bot_token_from_profile_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = home / "profiles" / "lana" / ".env"
            env.parent.mkdir(parents=True)
            env.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nOTHER=x\n", encoding="utf-8")
            with patch.object(gw.detect, "_hermes_profile_home", return_value=home / "profiles" / "lana"):
                self.assertEqual(gw.read_bot_token("lana"), FAKE_TOKEN)

    def test_actions_log_redacts_token_shape(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AGENTSMON_STATE": tmp}):
            gw.log_action({"ts": "t", "user": "u", "action": "restart_gateway",
                           "message": f"leak {FAKE_TOKEN}", "result": "ok", "precheck": "ok"})
            raw = (Path(tmp) / "actions.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(FAKE_TOKEN, raw)
            self.assertIn("redacted", raw)


class AttentionAndUi(unittest.TestCase):
    def test_lane_outage_gets_restart_gateway_action(self):
        att, _ = dashboard.compute_attention({"services": [{
            "name": "LANA Hermes Telegram Lane", "state": "outage",
            "anchor": "sluzba-lana-hermes-telegram-lane", "restart_gateway": True,
            "gateway_profile": "lana",
            "detail": "served=True; lane_state=disconnected",
        }]})
        self.assertEqual(att[0]["severity"], "critical")
        self.assertIn("výpadek", att[0]["text_cs"])
        act = att[0]["action"]
        self.assertEqual(act["id"], "restart_gateway")
        self.assertEqual(act["profile"], "lana")
        self.assertEqual(act["name"], "Hermes")

    def test_page_wires_gateway_restart_controls(self):
        self.assertIn("restart_gateway", dashboard.PAGE)
        self.assertIn("watchLaneAfterRestart", dashboard.PAGE)
        self.assertIn("gatewayRestartBtn", dashboard.PAGE)
        self.assertIn("a.gateway_restart", dashboard.PAGE)

    def test_step_up_summary_lists_profiles(self):
        text = gw.step_up_summary({"pinned_daemons": [
            {"name": "Hermes", "hermes_profile": "main"},
            {"name": "LANA (Hermes)", "hermes_profile": "lana"},
        ]})
        self.assertIn("default", text)
        self.assertIn("lana", text)
        self.assertIn("přeruší rozpracované konverzace", text)


class HttpFlow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = patch.dict(os.environ, {"AGENTSMON_STATE": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        cfg = {
            "dashboard": {"auth_mode": "tailscale", "admins": [ADMIN], "viewers": []},
            "daemons": [{"name": "Hermes", "pattern": "hermes", "manager": {
                "type": "systemd", "unit": "hermes-gateway.service"}}],
            "services": [{"name": "LANA Hermes Telegram Lane", "command": "true"}],
            "pinned_daemons": [
                {"name": "LANA (Hermes)", "hermes_profile": "lana", "actions": False},
            ],
        }
        self.server = dashboard.make_server("127.0.0.1", 0, cfg)
        self.port = self.server.server_port
        th = threading.Thread(target=self.server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), th.join(2)))

    def post(self, payload, confirm=None, origin=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json",
             "X-Agentsmon": "action", "Tailscale-User-Login": ADMIN}
        if confirm:
            h["X-Agentsmon-Confirm"] = confirm
        if origin is not None:
            h["Origin"] = origin
        conn.request("POST", "/api/agent/action", body=json.dumps(payload).encode(), headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, json.loads(data or b"{}"), data

    def actions_lines(self):
        p = self.state / "actions.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def audit_raw(self):
        p = self.state / "audit.jsonl"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_step_up_required_and_precheck_in_summary(self):
        with patch.object(gw, "precheck", return_value={
            "ok": True, "helps": True, "status": "ok", "message_cs": gw.PRECHECK_OK_CS,
            "profile": "lana",
        }) as pre, patch.object(gw, "perform") as perform:
            status, j, raw = self.post({"name": "Hermes", "action": "restart_gateway", "profile": "lana"})
            self.assertEqual(status, 428)
            self.assertEqual(j["error"], "confirm-required")
            self.assertIn("přeruší rozpracované konverzace", j["summary"])
            self.assertIn("getMe OK", j["summary"])
            blob = raw.decode() + self.audit_raw() + "".join(json.dumps(x) for x in self.actions_lines())
            self.assertNotIn(FAKE_TOKEN, blob)
            self.assertNotIn(FAKE_TOKEN.split(":")[1], blob)
            perform.assert_not_called()
            pre.assert_called()
            tok = j["confirm_token"]
        with patch.object(gw, "precheck", return_value={
            "ok": True, "helps": True, "status": "ok", "message_cs": gw.PRECHECK_OK_CS,
            "profile": "lana",
        }), patch.object(gw, "perform", return_value=(True, "Hermes: restart verified")) as perform:
            status, j, raw = self.post(
                {"name": "Hermes", "action": "restart_gateway", "profile": "lana"}, confirm=tok)
        self.assertEqual((status, j["ok"]), (200, True))
        perform.assert_called_once()
        self.assertEqual(j.get("lane"), "LANA Hermes Telegram Lane")
        lines = self.actions_lines()
        self.assertTrue(any(x.get("result") == "confirm-required" for x in lines))
        done = [x for x in lines if x.get("result") == "ok"][-1]
        self.assertEqual(done["user"], ADMIN)
        self.assertEqual(done["precheck"], "ok")
        self.assertEqual(done["action"], "restart_gateway")
        self.assertNotIn(FAKE_TOKEN, json.dumps(done) + raw.decode() + self.audit_raw())

    def test_invalid_confirm_token_runs_nothing(self):
        with patch.object(gw, "precheck", return_value={
            "ok": False, "helps": False, "status": "timeout", "message_cs": gw.PRECHECK_DOWN_CS,
            "profile": "lana",
        }), patch.object(gw, "perform") as perform:
            status, j, _ = self.post({"name": "Hermes", "action": "restart_gateway", "profile": "lana"})
            self.assertEqual(status, 428)
            status, j, _ = self.post(
                {"name": "Hermes", "action": "restart_gateway", "profile": "lana"},
                confirm="not-a-real-token")
            self.assertEqual(j["error"], "confirm-unknown")
            perform.assert_not_called()

    def test_cooldown_blocks_second_restart(self):
        with patch.object(gw, "precheck", return_value={
            "ok": True, "helps": True, "status": "ok", "message_cs": gw.PRECHECK_OK_CS,
            "profile": "lana",
        }), patch.object(gw, "perform", return_value=(True, "ok")):
            status, j, _ = self.post({"name": "Hermes", "action": "restart_gateway", "profile": "lana"})
            tok = j["confirm_token"]
            self.assertEqual(self.post(
                {"name": "Hermes", "action": "restart_gateway", "profile": "lana"}, confirm=tok)[0], 200)
            status, j, _ = self.post({"name": "Hermes", "action": "restart_gateway", "profile": "lana"})
            self.assertEqual(status, 429)
            self.assertEqual(j["error"], "gateway-cooldown")
            self.assertIn("až za", j["message"])
            self.assertTrue(any(x.get("result") == "rate-limit" for x in self.actions_lines()))

    def test_bad_origin_rejected(self):
        with patch.object(gw, "perform") as perform:
            status, j, _ = self.post(
                {"name": "Hermes", "action": "restart_gateway", "profile": "lana"},
                origin="https://evil.example")
            self.assertEqual(status, 403)
            perform.assert_not_called()


if __name__ == "__main__":
    unittest.main()
