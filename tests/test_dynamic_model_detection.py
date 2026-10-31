import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import detect


def _store(root: Path, routes, usage=(), sessions=()):
    db = root / "state.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE gateway_routing(scope TEXT, entry_json TEXT, updated_at REAL)")
        c.execute("CREATE TABLE session_model_usage(session_id TEXT, model TEXT, last_seen REAL)")
        c.execute("CREATE TABLE sessions(id TEXT, model TEXT)")
        for entry, stamp in routes:
            c.execute("INSERT INTO gateway_routing VALUES(?,?,?)", (str(root / "sessions"), json.dumps(entry), stamp))
        c.executemany("INSERT INTO session_model_usage VALUES(?,?,?)", usage)
        c.executemany("INSERT INTO sessions VALUES(?,?)", sessions)


class HermesLiveModel(unittest.TestCase):
    def _run(self, **kw):
        with tempfile.TemporaryDirectory() as d, patch.object(Path, "home", return_value=Path(d)):
            root = Path(d) / ".hermes"; root.mkdir()
            _store(root, **kw)
            return detect._hermes_live_model(), detect.daemon_model("Hermes")

    def test_runtime_override_wins(self):
        live, _ = self._run(routes=[({"session_id": "s1", "platform": "telegram",
                                      "model_override": {"model": "google/gemini-4-argon"}}, 10)],
                            usage=[("s1", "gpt-6.1-sol", 99)])
        self.assertEqual(live, "Gemini 4 Argon")

    def test_last_used_model_without_override(self):
        live, _ = self._run(routes=[({"session_id": "s1", "platform": "telegram"}, 10)],
                            usage=[("s1", "gpt-6.1-sol", 1), ("s1", "claude-opus-5-5", 5)])
        self.assertEqual(live, "Opus 5.5")

    def test_sessions_row_fallback_and_other_platforms_ignored(self):
        live, _ = self._run(routes=[({"session_id": "s1", "platform": "telegram"}, 10),
                                    ({"session_id": "x", "platform": "local",
                                      "model_override": {"model": "gpt-wrong"}}, 20)],
                            sessions=[("s1", "gemini-3.8-flash")])
        self.assertEqual(live, "Gemini 3.8 Flash")

    def test_missing_store_falls_back_to_config(self):
        with tempfile.TemporaryDirectory() as d, patch.object(Path, "home", return_value=Path(d)):
            p = Path(d) / ".hermes"; p.mkdir()
            (p / "config.yaml").write_text("model:\n  default: gpt-6.1-sol\n")
            self.assertIsNone(detect._hermes_live_model())
            self.assertFalse((p / "state.db").exists())
            self.assertEqual(detect.daemon_model("Hermes"), "GPT-6.1 Sol")


class PrettyAndVendor(unittest.TestCase):
    def test_provider_prefixed_and_cursor_ids(self):
        self.assertEqual(detect.prettify_model("google/gemini-4-argon"), "Gemini 4 Argon")
        self.assertEqual(detect.prettify_model("antigravity-subscription-directsdk/claude-sonnet-4-6"), "Sonnet 4.6")
        self.assertEqual(detect.prettify_model("composer-2.5"), "Composer 2.5")

    def test_opus_is_anthropic_not_openai(self):
        self.assertEqual(detect.vendor_for_model("Opus 5.5"), "anthropic")
        self.assertEqual(detect.vendor_for_model("O3 Mini"), "openai")
        self.assertEqual(detect.vendor_for_model("Composer 2.5"), "cursor")

    def test_cursor_agent_argv_model(self):
        cmd = "/home/u/.local/bin/agent --resume abc --model composer-2.5 --workspace /w"
        self.assertEqual(detect._model_from_argv([cmd]), "Composer 2.5")


if __name__ == "__main__":
    unittest.main()
