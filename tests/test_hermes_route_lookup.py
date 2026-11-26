import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import alerts, detect

SCHEMA = ("CREATE TABLE gateway_routing (scope TEXT NOT NULL DEFAULT '', session_key TEXT NOT NULL, "
          "entry_json TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (scope, session_key))")


class HermesRouteLookup(unittest.TestCase):
    """Production schema: hundreds of foreign routes rewritten with one shared ``updated_at``
    must not push a profile's Telegram route out of the lookup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.p = patch.object(Path, "home", return_value=home)
        self.p.start()
        self.root = home / ".hermes"
        (self.root / "profiles" / "lana").mkdir(parents=True)
        (self.root / "config.yaml").write_text("model:\n  default: gpt-6.2-sol\n")
        (self.root / "profiles" / "lana" / "config.yaml").write_text("model:\n  default: gpt-6.2-sol\n")
        self.db = self.root / "state.db"
        scope = str(self.root / "sessions")
        with sqlite3.connect(self.db) as c:
            c.execute(SCHEMA)
            c.execute("CREATE TABLE session_model_usage(session_id TEXT, model TEXT, last_seen REAL)")
            c.execute("CREATE TABLE sessions(id TEXT, model TEXT)")

            def add(key, platform, model=None):
                e = {"session_key": key, "session_id": "s-" + key, "platform": platform}
                if model:
                    e["model_override"] = {"model": model, "provider": "x"}
                c.execute("INSERT INTO gateway_routing VALUES(?,?,?,?)", (scope, key, json.dumps(e), 100.0))

            add("agent:main:telegram:dm:1", "telegram", "claude-opus-5-5")
            add("agent:lana:telegram:dm:2", "telegram", "google/gemini-4-argon")
            for i in range(300):  # inserted later → higher rowid, same updated_at
                add("agent:lana:local:%d" % i, "local", "gpt-wrong")

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_main_profile_route_found_behind_many_foreign_routes(self):
        self.assertEqual(detect._hermes_live_model("main"), "Opus 5.5")
        self.assertEqual(detect.daemon_model("Hermes", "main"), "Opus 5.5")

    def test_lana_profile_route_found_behind_many_foreign_routes(self):
        self.assertEqual(detect._hermes_live_model("lana"), "Gemini 4 Argon")

    def test_without_profile_returns_latest_telegram_route(self):
        # Same updated_at: the later-written row (higher rowid) wins deterministically.
        self.assertEqual(detect._hermes_live_model(), "Gemini 4 Argon")

    def test_like_wildcards_in_profile_name_are_literal(self):
        self.assertIsNone(detect._hermes_live_model("l_na"))
        self.assertIsNone(detect._hermes_live_model("%"))

    def test_unreadable_store_does_not_fall_back_to_config(self):
        status = {}
        with patch.object(detect.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertIsNone(detect._hermes_live_model("main", status))
            self.assertTrue(status["unavailable"])
            self.assertIsNone(detect.daemon_model("Hermes", "main"))

    def test_locked_store_is_unknown_and_never_alerts(self):
        locker = sqlite3.connect(self.db)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            self.assertIsNone(detect.daemon_model("Hermes", "main"))
        finally:
            locker.rollback()
            locker.close()
        row = {"name": "Hermes", "label": "Hermes", "model": "unknown"}
        self.assertFalse(alerts._known(alerts._model_label(row)))

    def test_missing_route_still_uses_config(self):
        (self.root / "profiles" / "other").mkdir()
        (self.root / "profiles" / "other" / "config.yaml").write_text("model:\n  default: gpt-6.2-sol\n")
        self.assertEqual(detect.daemon_model("X (Hermes)", "other"), "GPT-6.2 Sol")


if __name__ == "__main__":
    unittest.main()
