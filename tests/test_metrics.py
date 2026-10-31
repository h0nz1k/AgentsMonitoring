"""Prometheus /metrics: text exposition format 0.0.4."""
import base64
import json
import re
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from agentsmon import dashboard, metrics

DATA = {
    "time": 1_800_000_000,
    "agent_tables": [{"id": "codex-quota:providers", "rows": [
        {"name": "Codex · agentops", "cells": {
            "a": {"bars": [{"label": "5h", "percent": 12.5, "reset_at": 1_800_003_600},
                           {"label": "7d", "percent": 100.0, "reset_at": 1_800_086_400}]}}},
        {"name": 'We"ird\\name\nx', "cells": {"b": {"bars": [{"label": "Actions min", "percent": 3}]}}},
    ]}],
    "agents": [{"name": "LANA", "kind": "codex", "vendor": "openai", "alive": True,
                "cells": {"x": {"bars": [{"label": "5h", "percent": 12.5, "reset_at": 1_800_003_600}]}}},
               {"name": "Claude", "kind": "claude-code", "vendor": "anthropic", "alive": False}],
    "forecasts": [{"source": "Codex · agentops", "label": "5h", "used_pct": 12.5, "reset_at": 1_800_003_600,
                   "exhaust_at": 1_800_002_000, "will_exhaust_before_reset": True}],
    "plugin_errors": [{"name": "x", "detail": "boom"}],
}

LINE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\\n]|\\["\\n])*",?)*\})? '
                  r'-?[0-9.e+-]+$')


class Render(unittest.TestCase):
    def setUp(self):
        self.text = metrics.render(DATA)
        self.lines = self.text.splitlines()

    def test_every_line_is_valid_exposition(self):
        self.assertTrue(self.text.endswith("\n"))
        for line in self.lines:
            if line.startswith("#"):
                self.assertRegex(line, r"^# (HELP|TYPE) agentsmon_[a-z_]+ ")
            else:
                self.assertRegex(line, LINE)

    def test_type_declared_once_before_samples(self):
        for name in ("agentsmon_quota_used_percent", "agentsmon_quota_reset_timestamp_seconds",
                     "agentsmon_quota_forecast_exhaust_timestamp_seconds", "agentsmon_agent_up",
                     "agentsmon_plugin_errors"):
            types = [i for i, l in enumerate(self.lines) if l == f"# TYPE {name} gauge"]
            self.assertEqual(len(types), 1, name)
            samples = [i for i, l in enumerate(self.lines) if l.startswith(name + "{") or l.startswith(name + " ")]
            self.assertTrue(samples and min(samples) > types[0], name)

    def test_quota_series_values(self):
        self.assertIn('agentsmon_quota_used_percent{source="Codex · agentops",label="7d"} 100', self.lines)
        self.assertIn('agentsmon_quota_used_percent{source="Codex · agentops",label="5h"} 12.5', self.lines)
        self.assertIn('agentsmon_quota_reset_timestamp_seconds{source="Codex · agentops",label="7d"} 1800086400',
                      self.lines)
        self.assertIn('agentsmon_quota_forecast_exhaust_timestamp_seconds{source="Codex · agentops",label="5h"} '
                      '1800002000', self.lines)
        # agent gauge duplicating a provider window is not a second series
        self.assertNotIn('source="LANA"', self.text)

    def test_label_values_escaped(self):
        self.assertIn('agentsmon_quota_used_percent{source="We\\"ird\\\\name\\nx",label="Actions min"} 3',
                      self.lines)

    def test_agents_and_plugin_errors(self):
        self.assertIn('agentsmon_agent_up{agent="LANA",kind="codex",vendor="openai"} 1', self.lines)
        self.assertIn('agentsmon_agent_up{agent="Claude",kind="claude-code",vendor="anthropic"} 0', self.lines)
        self.assertIn("agentsmon_plugin_errors 1", self.lines)


class Endpoint(unittest.TestCase):
    def serve(self, cfg):
        srv = dashboard.make_server("127.0.0.1", 0, cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/metrics"

    def test_metrics_served_with_exposition_content_type(self):
        url = self.serve({"dashboard": {}})
        with patch.object(dashboard, "_state_data", return_value=DATA):
            with urllib.request.urlopen(url, timeout=5) as r:
                body, ctype = r.read().decode(), r.headers["Content-Type"]
        self.assertEqual(ctype, "text/plain; version=0.0.4; charset=utf-8")
        self.assertIn("agentsmon_quota_used_percent", body)

    def test_metrics_respect_basic_auth(self):
        cfg = {"dashboard": {"auth": {"user": "u", "pwhash": dashboard.password_hash("p")}}}
        url = self.serve(cfg)
        with patch.object(dashboard, "_state_data", return_value=DATA):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(url, timeout=5)
            self.assertEqual(ctx.exception.code, 401)
            req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"u:p").decode()})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)


class StateForecast(unittest.TestCase):
    def test_state_exposes_forecasts_and_annotated_resets(self):
        fc = [{"source": "Codex", "label": "7d", "used_pct": 50.0, "reset_at": 1_900_000_000,
               "exhaust_at": 1_899_000_000, "projected_at_reset": 100.0,
               "will_exhaust_before_reset": True, "rate_pct_per_hour": 2.0}]
        extra = {"tables": [{"id": "t", "title": "T", "rows": [{"name": "Codex", "cells": {"w": {"bars": [
            {"label": "7d", "percent": 50.0, "reset_at": 1_900_000_000, "state": "OK"}]}}}]}],
                 "columns": [], "patches": [], "hidden_sessions": [], "hidden_services": [],
                 "errors": [], "plugins": []}
        with patch.object(dashboard, "_agents_state", return_value=[]), \
                patch.object(dashboard.plugins, "collect", return_value=extra), \
                patch.object(dashboard, "_service_state", return_value=[]), \
                patch.object(dashboard.quota, "observe", return_value=fc) as obs, \
                patch.object(dashboard.alerts, "tick") as tick:
            data = json.loads(dashboard._state())
        obs.assert_called_once()
        tick.assert_called_once()
        self.assertEqual(data["forecasts"], fc)
        self.assertTrue(data["resets"][0]["forecast"]["will_exhaust_before_reset"])
        bar = data["agent_tables"][0]["rows"][0]["cells"]["w"]["bars"][0]
        self.assertEqual(bar["forecast"]["exhaust_at"], 1_899_000_000)


if __name__ == "__main__":
    unittest.main()
