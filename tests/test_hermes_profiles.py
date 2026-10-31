import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import detect


def _db(path: Path, routes=(), scope=None, usage=(), sessions=()):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE gateway_routing(scope TEXT, entry_json TEXT, updated_at REAL)")
        c.execute("CREATE TABLE session_model_usage(session_id TEXT, model TEXT, last_seen REAL)")
        c.execute("CREATE TABLE sessions(id TEXT, model TEXT)")
        for entry, stamp in routes:
            c.execute("INSERT INTO gateway_routing VALUES(?,?,?)", (scope, json.dumps(entry), stamp))
        c.executemany("INSERT INTO session_model_usage VALUES(?,?,?)", usage)
        c.executemany("INSERT INTO sessions VALUES(?,?)", sessions)


def _route(key, sid, stamp, model=None):
    e = {"session_key": key, "session_id": sid, "platform": "telegram"}
    if model:
        e["model_override"] = {"model": model, "provider": "x"}
    return e, stamp


class HermesProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.p = patch.object(Path, "home", return_value=self.home)
        self.p.start()
        self.root = self.home / ".hermes"
        (self.root / "profiles" / "lana").mkdir(parents=True)

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_each_profile_reads_only_its_own_route(self):
        _db(self.root / "state.db", scope=str(self.root / "sessions"), routes=[
            _route("agent:main:telegram:dm:1", "m1", 10, "claude-opus-5-5"),
            _route("agent:lana:telegram:dm:1", "l1", 20, "google/gemini-4-argon"),
        ])
        self.assertEqual(detect.daemon_model("Hermes", "main"), "Opus 5.5")
        self.assertEqual(detect.daemon_model("LANA (Hermes)", "lana"), "Gemini 4 Argon")
        # legacy (no profile): most recent route of any profile
        self.assertEqual(detect.daemon_model("Hermes"), "Gemini 4 Argon")

    def test_profile_usage_comes_from_profile_store(self):
        _db(self.root / "state.db", scope=str(self.root / "sessions"),
            routes=[_route("agent:lana:telegram:dm:1", "l1", 20)])
        _db(self.root / "profiles" / "lana" / "state.db",
            usage=[("l1", "gemini-3.8-flash", 1), ("l1", "claude-opus-5-5", 9)])
        self.assertEqual(detect._hermes_live_model("lana"), "Opus 5.5")

    def test_profile_without_route_falls_back_to_profile_config(self):
        _db(self.root / "state.db", scope=str(self.root / "sessions"),
            routes=[_route("agent:main:telegram:dm:1", "m1", 10, "gpt-6.1-sol")])
        (self.root / "profiles" / "lana" / "config.yaml").write_text("model:\n  default: claude-opus-5-5\n")
        (self.root / "config.yaml").write_text("model:\n  default: gpt-6.1-sol\n")
        self.assertIsNone(detect._hermes_live_model("lana"))
        self.assertEqual(detect.daemon_model("LANA (Hermes)", "lana"), "Opus 5.5")

    def test_unsafe_profile_name_stays_in_default_home(self):
        self.assertEqual(detect._hermes_profile_home("../x"), self.root)

    def test_pinned_row_respects_profile_and_hides_actions(self):
        _db(self.root / "state.db", scope=str(self.root / "sessions"), routes=[
            _route("agent:main:telegram:dm:1", "m1", 10, "gpt-6.1-sol"),
            _route("agent:lana:telegram:dm:1", "l1", 20, "claude-opus-5-5"),
        ])
        none = subprocess.CompletedProcess([], 1, "")
        with patch.object(detect, "_run", return_value=none):
            rows = detect.pinned_agents([
                {"name": "Hermes", "process": "x", "hermes_profile": "main"},
                {"name": "LANA (Hermes)", "process": "x", "hermes_profile": "lana", "actions": False},
            ])
        self.assertEqual([r["label"] for r in rows], ["GPT-6.1 Sol", "Opus 5.5"])
        self.assertEqual(rows[1]["vendor"], "anthropic")
        self.assertNotIn("actions", rows[0])
        self.assertIs(rows[1]["actions"], False)


if __name__ == "__main__":
    unittest.main()
