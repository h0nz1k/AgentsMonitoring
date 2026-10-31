"""P2/P3: 'pracuje na #N' agent ↔ queue mapping and tmux 'Čeká na vstup' detection."""
import unittest
from unittest.mock import patch

from agentsmon import dashboard, detect


def _agents():
    return [
        {"name": "Hermes", "kind": "daemon", "alive": True},
        {"name": "LANA (Hermes)", "kind": "daemon", "alive": True},
        {"name": "Claude", "kind": "claude-code", "alive": True},
        {"name": "Codex", "kind": "codex", "alive": True},
        {"name": "Cursor", "kind": "Cursor", "alive": True},
        {"name": "LANA", "kind": "codex", "alive": True},
        {"name": "LANA - Agy", "kind": "antigravity", "alive": True},
    ]


def _item(num, state, worker=None, profile=None, lane=None, tracked=True, kind="issue"):
    return {"kind": kind, "issue": num, "title": f"Úkol {num}", "state": state,
            "state_label_cs": {"running": "Běží", "awaiting_pickup": "Čeká na převzetí"}.get(state, state),
            "url": f"https://github.com/o/r/issues/{num}", "tracked": tracked, "worker": worker,
            "profile": profile, "task": {"lane": lane} if lane else None}


def _by_name(agents):
    return {a["name"]: a.get("work") for a in agents}


class AgentWork(unittest.TestCase):
    def test_live_shape_worker_cursor_profile_lana_goes_to_cursor(self):
        agents = _agents()
        q = {"enabled": True, "items": [
            _item(18, "blocked", worker="h0nz1k"),                       # human assignee → nobody
            _item(12, "awaiting_pickup", worker="cursor", profile="lana"),
            _item(33, "done", worker="cursor_json", lane="cursor_json"),  # history, not work
            _item(8, "queued")]}
        dashboard.assign_agent_work(agents, q)
        w = _by_name(agents)
        self.assertEqual(w["Cursor"]["issue"], 12)
        self.assertEqual(w["Cursor"]["url"], "https://github.com/o/r/issues/12")
        self.assertEqual(w["Cursor"]["state_label_cs"], "Čeká na převzetí")
        self.assertTrue(all(v is None for k, v in w.items() if k != "Cursor"))

    def test_lane_suffix_and_running_wins_over_waiting(self):
        agents = _agents()
        dashboard.assign_agent_work(agents, {"enabled": True, "items": [
            _item(5, "awaiting_pickup", worker="cursor"),
            _item(6, "running", worker="cursor_json")]})
        self.assertEqual(_by_name(agents)["Cursor"]["issue"], 6)

    def test_ambiguous_is_null(self):
        agents = _agents() + [{"name": "Agy 2", "kind": "antigravity", "alive": True}]
        dashboard.assign_agent_work(agents, {"enabled": True, "items": [
            _item(1, "running", worker="gemini"),           # two antigravity agents → skip
            _item(2, "running", worker="claude"),
            _item(3, "running", worker="claude-code")]})    # two equal-rank items → don't guess
        w = _by_name(agents)
        self.assertIsNone(w["LANA - Agy"]); self.assertIsNone(w["Agy 2"]); self.assertIsNone(w["Claude"])
        self.assertIsNone(w["Codex"]); self.assertIsNone(w["LANA"])

    def test_exact_name_and_profile_only(self):
        agents = _agents()
        dashboard.assign_agent_work(agents, {"enabled": True, "items": [
            _item(7, "running", worker="lana"),              # exact agent name
            _item(9, "queued", profile="lana")]})            # no worker → Hermes profile daemon
        w = _by_name(agents)
        self.assertEqual(w["LANA"]["issue"], 7)
        self.assertEqual(w["LANA (Hermes)"]["issue"], 9)

    def test_disabled_untracked_and_unsafe_url(self):
        agents = _agents()
        dashboard.assign_agent_work(agents, {"enabled": False, "items": [_item(1, "running", "cursor")]})
        self.assertTrue(all(a["work"] is None for a in agents))
        bad = _item(4, "running", "cursor"); bad["url"] = "javascript:alert(1)"
        dashboard.assign_agent_work(agents, {"enabled": True, "items": [_item(2, "running", "cursor", tracked=False), bad]})
        self.assertEqual(_by_name(agents)["Cursor"]["issue"], 4)
        self.assertIsNone(_by_name(agents)["Cursor"]["url"])


CLAUDE_APPROVAL = """\
● Bash(rm -rf build/)
╭──────────────────────────────────────────────╮
│ Bash command                                 │
│   rm -rf build/                              │
│ Do you want to proceed?                      │
│ ❯ 1. Yes                                     │
│   2. Yes, and don't ask again for rm         │
│   3. No, and tell Claude what to do (esc)    │
╰──────────────────────────────────────────────╯
"""
CLAUDE_IDLE = """\
● Hotovo. Přidat ho můžete: claude mcp add --header "Authorization: Bearer ***"
✻ Sautéed for 8s · done 10:37 PM
──────────────────────────────────────────────────
❯ \n──────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)        control this session from your phone
"""
CODEX_APPROVAL = """\
\x1b[1mWould you like to run the following command?\x1b[0m
  $ curl -H 'Authorization: Bearer sk-live-0123456789abcdefghijklmnop' https://api.example.com/?token=abc
› 1. Yes, proceed (y)
  2. No, and tell Codex what to do differently (esc)
"""
CODEX_IDLE = """\
⚠ Heads up, you have less than 5% of your weekly limit left. Run /status for a
  breakdown.
› Ask Codex to do anything
  GPT-6.1-Sol medium · ~/ai-agenti · Respond to TG greeting
  ← for agents · ? for shortcuts                      ⚠ 4 warnings · f2 to view
"""
AGY_IDLE = """\
 ┃ Verbosity now defaults to medium
 ┃ esc to dismiss
> Plan mode: research & plan only (shift+tab to cycle)
? for shortcuts                                    plan · Gemini 3.8 Flash · low
"""
CURSOR_IDLE = """\
  Cursor Agent
  Tip: Use /skills to give Cursor specialized knowledge for tasks.
  → Plan, search, build anything
  Ask (shift+tab to cycle)
"""
OLD_PROMPT_SCROLLED_AWAY = "Do you want to proceed?\n❯ 1. Yes\n" + "\n".join(f"řádek {i}" for i in range(30))


class WaitingDetection(unittest.TestCase):
    def test_prompts_detected(self):
        self.assertEqual(detect.waiting_prompt(CLAUDE_APPROVAL), "Do you want to proceed?")
        hint = detect.waiting_prompt(CODEX_APPROVAL)
        self.assertEqual(hint, "Would you like to run the following command?")
        for text in ("Allow command? (y/n)", "Press Enter to continue", "Apply changes? [y/N]",
                     "● 1. Yes, allow once", "Waiting for user confirmation", "Run this command?"):
            self.assertTrue(detect.waiting_prompt("output\n" + text + "\n"), text)

    def test_idle_screens_not_waiting(self):
        for text in (CLAUDE_IDLE, CODEX_IDLE, AGY_IDLE, CURSOR_IDLE, OLD_PROMPT_SCROLLED_AWAY, "", None):
            self.assertIsNone(detect.waiting_prompt(text), text)

    def test_configurable_patterns(self):
        self.assertIsNone(detect.waiting_prompt(CLAUDE_APPROVAL, []))
        self.assertEqual(detect.waiting_prompt("Čekám na tebe, šéfe\n", [r"čekám na tebe", "(bad["]),
                         "Čekám na tebe, šéfe")

    def test_hint_sanitized(self):
        h = detect.sanitize_hint("\x1b[31mAllow? token=abcd1234 key: sk-proj-AAAABBBBCCCCDDDD "
                                 "123456789:AAbbccddeeffgghhiijjkkllmmnnoopp https://x/?code=s3cr3t "
                                 "Bearer eyJhbGciOiJIUzI1NiJ9.payload" + " x" * 200)
        self.assertLessEqual(len(h), detect.HINT_MAX)
        for secret in ("\x1b", "abcd1234", "sk-proj", "AAbbcc", "s3cr3t", "eyJhbG"):
            self.assertNotIn(secret, h)
        self.assertIn("•••", h)

    def test_annotate_waiting_and_cache(self):
        detect._PANE_CACHE.clear()
        rows = [{"name": "Claude", "kind": "claude-code", "alive": True},
                {"name": "Cursor", "kind": "Cursor", "alive": True},
                {"name": "Shell", "kind": "shell", "alive": False},
                {"name": "Login", "kind": "codex", "alive": True, "needs_login": True}]
        panes = {"Claude": CLAUDE_APPROVAL, "Cursor": CURSOR_IDLE}
        calls = []

        class R:
            def __init__(self, out): self.stdout, self.returncode = out, 0

        def fake_run(args, timeout=8):
            calls.append((args, timeout))
            self.assertNotIn("send-keys", args)
            return R(panes.get(args[args.index("-t") + 1], ""))

        with patch.object(detect, "_run", side_effect=fake_run):
            detect.annotate_waiting(rows, {}, now=1000.0)
            detect.annotate_waiting([dict(rows[0])], {}, now=1005.0)     # cached within 10 s
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(a[1:4] == ["capture-pane", "-p", "-t"] and a[-2:] == ["-S", "-30"] and t == 2.0
                            for a, t in calls))
        self.assertTrue(rows[0]["waiting"])
        self.assertEqual(rows[0]["status"], "Čeká na vstup")
        self.assertEqual(rows[0]["activity"], "waiting")
        self.assertNotIn("waiting", rows[1])
        self.assertNotIn("waiting", rows[3])
        detect._PANE_CACHE.clear()
        with patch.object(detect, "_run", side_effect=fake_run):
            r2 = [{"name": "Claude", "kind": "claude-code", "alive": True}]
            detect.annotate_waiting(r2, {"detect": {"waiting_patterns": []}}, now=2000.0)
        self.assertNotIn("waiting", r2[0])
        detect._PANE_CACHE.clear()

    def test_attention_lists_waiting_agent(self):
        att, ov = dashboard.compute_attention({"agents": [
            {"name": "Claude", "alive": True, "waiting": True, "status": "Čeká na vstup"}]})
        self.assertEqual(att[0]["severity"], "warning")
        self.assertEqual(att[0]["text_cs"], "Claude čeká na vstup")
        self.assertEqual(att[0]["href"], "#agenti")
        self.assertEqual(ov["level"], "warning")


if __name__ == "__main__":
    unittest.main()
