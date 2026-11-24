"""GET /api/ha/summary — allowlisted read-only Home Assistant payload from the state cache."""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agentsmon import dashboard as d


ADMIN, HA_NODE, OTHER_NODE = "owner@example.com", "homeassistant", "other-node"


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


SAMPLE_STATE = {
    "time": 1_700_000_000,
    "stale": False,
    "loading": False,
    "overall": {"level": "warning", "count": 2, "summary_cs": "1 kritický · 1 upozornění"},
    "attention": [
        {"severity": "critical", "text_cs": "Agent X: neběží"},
        {"severity": "warning", "text_cs": "Claude: vyčerpáno"},
        {"severity": "info", "text_cs": "Fronta čeká"},
    ],
    "agents": [
        {"name": "LANA", "label": "GPT-5", "alive": True, "waiting": False,
         "session_id": "SECRET-SESSION", "status_detail": "/home/secret", "cwd": "/secret",
         "restart": "codex resume SECRET"},
        {"name": "Writer", "label": "Claude Sonnet", "alive": False, "waiting": True},
    ],
    "services": [
        {"name": "Telegram Bridge", "state": "operational", "sla": 99.5,
         "health_url": "http://127.0.0.1:9/secret", "detail": "ok"},
        {"name": "HumanAgentWiki", "state": "outage", "sla": 90.0},
    ],
    "agent_tables": [{
        "id": "quotas", "kind": "quota", "rows": [
            {"name": "Claude", "cells": {"w": {"bars": [
                {"label": "5h", "percent": 40.0, "remaining": 60.0, "reset_at": 1_700_003_600},
                {"label": "7d", "percent": 100.0, "remaining": 0.0, "reset_at": 1_700_086_400,
                 "state": "EXHAUSTED"}]}}},
            {"name": "Cursor", "cells": {"w": {"bars": [
                {"label": "Included", "percent": 12.5, "remaining": 87.5, "reset_at": 1_700_050_000},
                {"label": "API", "percent": 3.0, "remaining": 97.0}]}}},
        ],
    }],
    "forecasts": [
        {"source": "Claude", "label": "7d", "will_exhaust_before_reset": True, "exhausted": True},
        {"source": "Cursor", "label": "Included", "will_exhaust_before_reset": False},
    ],
    "queue": {
        "enabled": True,
        "summary": {"queued": 4, "running": 1, "blocked": 2, "awaiting_pickup": 3, "failed": 0},
    },
}


class HaSummaryUnit(unittest.TestCase):
    def test_schema_allowlist_and_no_secrets(self):
        summary = d.build_ha_summary(SAMPLE_STATE, age=1.0, now=1_700_000_100)
        self.assertEqual(summary["schema"], d.HA_SUMMARY_SCHEMA)
        self.assertEqual(summary["overall"], "warning")
        self.assertEqual(summary["attention_count"], 2)
        self.assertEqual(summary["critical_count"], 1)
        self.assertEqual(summary["generated_at"], "2023-11-14T22:13:20Z")
        self.assertFalse(summary["stale"])
        self.assertEqual(summary["queue"], {"pending": 4, "running": 1, "blocked": 2, "review": 3})
        self.assertEqual(
            {(a["name"], a["model_label"], a["alive"], a["waiting"]) for a in summary["agents"]},
            {("LANA", "GPT-5", True, False), ("Writer", "Claude Sonnet", False, True)})
        self.assertEqual(summary["services"][0],
                         {"name": "Telegram Bridge", "state": "operational", "sla_24h": 99.5})
        claude7 = next(q for q in summary["quotas"] if q["provider"] == "Claude" and q["window"] == "7d")
        self.assertEqual(claude7["used_pct"], 100.0)
        self.assertEqual(claude7["remaining_pct"], 0.0)
        self.assertTrue(claude7["exhausted"])
        self.assertTrue(claude7["will_exhaust_before_reset"])
        self.assertEqual(claude7["reset_at"], "2023-11-15T22:13:20Z")
        keys = set(_walk_keys(summary))
        allowed = (d.HA_SUMMARY_KEYS | d.HA_QUOTA_KEYS | d.HA_AGENT_KEYS |
                   d.HA_SERVICE_KEYS | d.HA_QUEUE_KEYS)
        self.assertTrue(keys <= allowed, keys - allowed)
        blob = json.dumps(summary)
        for banned in ("SECRET", "session_id", "status_detail", "/home/secret", "resume",
                       "health_url", "cwd", "restart"):
            self.assertNotIn(banned, blob)

    def test_stale_when_cache_older_than_two_intervals(self):
        fresh = d.build_ha_summary(SAMPLE_STATE, age=d.HA_STALE_AFTER_SECONDS - 1)
        self.assertFalse(fresh["stale"])
        old = d.build_ha_summary(SAMPLE_STATE, age=d.HA_STALE_AFTER_SECONDS + 0.1)
        self.assertTrue(old["stale"])
        flagged = d.build_ha_summary({**SAMPLE_STATE, "stale": True}, age=0)
        self.assertTrue(flagged["stale"])


class HaSummaryHttp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"AGENTSMON_STATE": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        d.invalidate_state_cache()
        d._state_building = False
        d._state_disk_checked = False
        self.addCleanup(d.invalidate_state_cache)
        self.dcfg = {
            "auth_mode": "tailscale",
            "admins": [ADMIN],
            "viewers": [],
            "ha_readonly_nodes": [HA_NODE],
        }
        self.server = d.make_server("127.0.0.1", 0, {"dashboard": dict(self.dcfg)})
        self.port = self.server.server_port
        th = threading.Thread(target=self.server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), th.join(2)))
        d._state_cache = (time.monotonic(), json.dumps(SAMPLE_STATE).encode())
        d._state_disk_checked = True
        d._state_from_disk = False

    def req(self, method, path, login=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if login:
            h["Tailscale-User-Login"] = login
            h["X-Forwarded-For"] = "100.64.0.7"
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_empty_ha_readonly_nodes_is_404(self):
        srv = d.make_server("127.0.0.1", 0, {"dashboard": {
            "auth_mode": "tailscale", "admins": [ADMIN], "ha_readonly_nodes": []}})
        port = srv.server_port
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), th.join(2)))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/ha/summary", headers={
            "Host": f"127.0.0.1:{port}", "Tailscale-User-Login": ADMIN, "X-Forwarded-For": "100.64.0.1"})
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()

    def test_other_node_forbidden(self):
        status, body = self.req("GET", "/api/ha/summary", login=OTHER_NODE)
        self.assertEqual(status, 403)
        self.assertIn(b"forbidden", body)

    def test_allowed_node_gets_summary_from_cache_without_build(self):
        with patch.object(d, "_state", side_effect=AssertionError("build must not run")) as st, \
             patch.object(d, "_rebuild_state", side_effect=AssertionError("rebuild must not run")):
            status, raw = self.req("GET", "/api/ha/summary", login=HA_NODE)
            st.assert_not_called()
        self.assertEqual(status, 200)
        summary = json.loads(raw)
        self.assertEqual(summary["schema"], 1)
        self.assertEqual(summary["overall"], "warning")
        self.assertIn("quotas", summary)
        keys = set(_walk_keys(summary))
        allowed = (d.HA_SUMMARY_KEYS | d.HA_QUOTA_KEYS | d.HA_AGENT_KEYS |
                   d.HA_SERVICE_KEYS | d.HA_QUEUE_KEYS)
        self.assertTrue(keys <= allowed)

    def test_allowed_node_cannot_read_state_or_post(self):
        self.assertEqual(self.req("GET", "/api/state", login=HA_NODE)[0], 403)
        self.assertEqual(self.req("GET", "/", login=HA_NODE)[0], 403)
        status, body = self.req(
            "POST", "/api/agent/action", login=HA_NODE,
            headers={"Content-Type": "application/json", "X-Agentsmon": "action"},
            body=json.dumps({"name": "LANA", "action": "restart"}).encode())
        self.assertEqual(status, 403)
        self.assertIn(b"ha/summary", body)

    def test_tagged_node_via_whois_xff(self):
        with patch.object(d, "resolve_tailscale_node", return_value=HA_NODE) as who:
            status, raw = self.req(
                "GET", "/api/ha/summary",
                headers={"X-Forwarded-For": "100.84.133.100"})
            who.assert_called()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["schema"], 1)

    def test_stale_flag_from_cache_age(self):
        d._state_cache = (time.monotonic() - (d.HA_STALE_AFTER_SECONDS + 5),
                          json.dumps(SAMPLE_STATE).encode())
        status, raw = self.req("GET", "/api/ha/summary", login=HA_NODE)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["stale"])


class HaSummaryIdentity(unittest.TestCase):
    def test_login_maps_to_ha_readonly(self):
        class H(dict):
            def get_all(self, k):
                return [self[k]] if k in self else None
        ident = d.tailscale_identity(
            H({"Tailscale-User-Login": HA_NODE}), "127.0.0.1",
            {"admins": [ADMIN], "ha_readonly_nodes": [HA_NODE]})
        self.assertEqual(ident["role"], "ha-readonly")
        ident = d.tailscale_identity(
            H({"Tailscale-User-Login": OTHER_NODE, "X-Forwarded-For": "100.64.0.1"}),
            "127.0.0.1", {"admins": [ADMIN], "ha_readonly_nodes": [HA_NODE]})
        self.assertIsNone(ident["role"])


if __name__ == "__main__":
    unittest.main()
