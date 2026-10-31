"""Work queue ("Fronta práce") model: label/status mapping, PR linking, CI, mismatches,
URL whitelist, gh failures with stale data, disabled config. Fixtures are scrubbed copies of
real `gh ... --json` output and supervisor task records."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from agentsmon import workqueue as wq

REPO = "h0nz1k/agentops-config"
NOW = 1793428900.0  # 2026-10-31T06:41:40Z
GH = "https://github.com/%s" % REPO


def iso(offset):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - offset))


def issue(num, labels=(), offset=3600, title=None, url=None, assignees=()):
    return {"number": num, "title": title or "[agent] úkol %d" % num,
            "url": url or "%s/issues/%d" % (GH, num), "state": "OPEN",
            "labels": [{"id": "LA_x%d" % i, "name": n, "description": "", "color": "ededed"}
                       for i, n in enumerate(labels)],
            "assignees": [{"id": "U_x", "login": a, "name": ""} for a in assignees],
            "createdAt": iso(offset + 600), "updatedAt": iso(offset)}


def check(status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": "validate", "status": status,
            "conclusion": conclusion, "workflowName": "validate",
            "detailsUrl": "%s/actions/runs/1/job/2" % GH}


def pr(num, branch, closes=(), draft=False, checks=None, offset=1800, review=""):
    return {"number": num, "title": "PR %d" % num, "url": "%s/pull/%d" % (GH, num),
            "isDraft": draft, "headRefName": branch, "updatedAt": iso(offset),
            "reviewDecision": review, "labels": [],
            "statusCheckRollup": [check()] if checks is None else checks,
            "closingIssuesReferences": [
                {"id": "I_x", "number": n, "url": "%s/issues/%d" % (GH, n),
                 "repository": {"id": "R_x", "name": "agentops-config",
                                "owner": {"id": "U_x", "login": "h0nz1k"}}} for n in closes]}


def task_new(num, status, offset=600, error=None):
    """New-style cursor_json task (source.kind = github_issue)."""
    return {"id": "GH-agentops-config-%d" % num, "status": status, "lane": "cursor_json",
            "ownership": "cursor_queue_v1",
            "source": {"kind": "github_issue", "repo": REPO, "number": num},
            "goal": "[agent] úkol %d" % num, "created_at": iso(offset + 60),
            "updated_at": iso(offset).replace("Z", "+00:00"), "pid": None, "error": error,
            "attempt": {"attempt_id": "GH-agentops-config-%d-0c203cfda314" % num,
                        "output_dir": "/home/someone/.local/share/lana/out/x"}}


def task_old(tid, num, status, retries=0, offset=7200, error=None):
    """Older-style task (issue_number / issue_url / github_repo)."""
    return {"id": tid, "status": status, "issue_number": num,
            "issue_url": "%s/issues/%d" % (GH, num), "github_repo": REPO,
            "retries": retries, "max_retries": 2, "error": error, "goal": "",
            "workspace": "/home/someone/work", "command_fingerprint": "ab" * 32,
            "updated_at": iso(offset).replace("Z", "+00:00")}


SUPERVISOR = {"at": iso(5).replace("Z", "+00:00"), "llm_calls": 0,
              "concurrency": {"running": 1, "queued": 0, "max_running": 3, "slots_free": 2},
              "by_status": {"done": 3}, "tasks": []}


def by_issue(model):
    return {it["issue"]: it for it in model["items"] if it["issue"] is not None and it["kind"] != "pr"}


class StateMapping(unittest.TestCase):
    def test_both_label_vocabularies(self):
        self.assertEqual(wq.state_from_labels(["profile:lana", "state:running"]), "running")
        self.assertEqual(wq.state_from_labels(["agent:running"]), "running")
        self.assertEqual(wq.state_from_labels(["state:queued"]), "queued")
        self.assertEqual(wq.state_from_labels(["agent:pending"]), "queued")
        self.assertEqual(wq.state_from_labels(["state:receipt_ready"]), "awaiting_pickup")
        self.assertEqual(wq.state_from_labels(["agent:review"]), "awaiting_pickup")
        self.assertEqual(wq.state_from_labels(["agent:blocked"]), "blocked")
        self.assertEqual(wq.state_from_labels(["agent:done"]), "done")
        self.assertIsNone(wq.state_from_labels(["bug", "worker:cursor"]))
        # several state labels: the most severe wins
        self.assertEqual(wq.state_from_labels(["state:running", "agent:blocked"]), "blocked")

    def test_supervisor_statuses(self):
        exp = {"running": "running", "queued": "queued", "requeued": "queued",
               "receipt_ready": "awaiting_pickup", "blocked": "blocked", "failed": "failed",
               "done": "done", "completed": "done", "superseded": "done", "weird_new": "blocked"}
        for status, state in exp.items():
            self.assertEqual(wq.normalize_task({"id": "T", "status": status})["state"], state, status)
        self.assertIsNone(wq.normalize_task({"id": "TEMPLATE", "status": "template"}))

    def test_task_issue_linking_variants(self):
        self.assertEqual(wq.task_issue(task_new(22, "done"), REPO), 22)
        self.assertEqual(wq.task_issue(task_old("A", 12, "done"), REPO), 12)
        self.assertEqual(wq.task_issue({"issue": "%s/issues/18" % GH}, REPO), 18)
        self.assertEqual(wq.task_issue({"issue": 167}, REPO), 167)
        self.assertIsNone(wq.task_issue({"source": {"kind": "github_issue", "repo": "x/y", "number": 3}}, REPO))
        self.assertIsNone(wq.task_issue({"source": "hermes-intake"}, REPO))

    def test_labels_and_supervisor_in_model(self):
        raw = {"issues": [issue(1, ["profile:lana", "worker:cursor", "state:running"]),
                          issue(2, ["agent:pending"]),
                          issue(3, ["agent:review"]),
                          issue(4, []),
                          issue(5, ["bug"])],
               "tasks": [task_new(1, "running"), task_new(4, "failed", error="exit_code=1")],
               "supervisor": SUPERVISOR}
        m = wq.build_model(raw, now=NOW, repo=REPO)
        it = by_issue(m)
        self.assertEqual(it[1]["state"], "running")
        self.assertEqual(it[1]["state_label_cs"], "Běží")
        self.assertEqual(it[1]["worker"], "cursor")
        self.assertEqual(it[1]["profile"], "lana")
        self.assertEqual(it[2]["state_label_cs"], "Ve frontě")
        self.assertEqual(it[3]["state_label_cs"], "Čeká na převzetí")
        self.assertEqual(it[4]["state"], "failed")          # unlabeled: supervisor fills in
        self.assertEqual(it[4]["task"]["error"], "exit_code=1")
        self.assertFalse(it[5]["tracked"])                  # no queue label, no task
        s = m["summary"]
        self.assertEqual((s["running"], s["queued"], s["awaiting_pickup"], s["failed"]), (1, 1, 1, 1))
        self.assertEqual(s["running_slots_max"], 3)
        self.assertEqual(s["supervisor_running"], 1)
        self.assertEqual(s["open_issues"], 5)
        self.assertEqual(s["untracked"], 1)
        # severity order: failed, ..., running, awaiting, queued
        self.assertEqual([i["state"] for i in m["items"]][:2], ["failed", "running"])

    def test_sorting_severity_then_updated(self):
        raw = {"issues": [issue(1, ["agent:pending"], offset=900), issue(2, ["agent:pending"], offset=100),
                          issue(3, ["agent:blocked"], offset=5000)]}
        m = wq.build_model(raw, now=NOW, repo=REPO)
        self.assertEqual([i["issue"] for i in m["items"]], [3, 2, 1])
        self.assertEqual(m["items"][1]["age_seconds"], 100)

    def test_default_slots_and_closed_done_24h(self):
        closed = issue(28, ["agent:done"], offset=4000)
        closed.update(state="CLOSED", closedAt=iso(4000))
        old = issue(9, ["agent:done"], offset=90000)
        old.update(state="CLOSED", closedAt=iso(90000))
        m = wq.build_model({"closed": [closed, old], "tasks": [task_new(28, "receipt_ready")]},
                           now=NOW, repo=REPO)
        self.assertEqual(m["summary"]["running_slots_max"], 2)
        self.assertEqual(m["summary"]["done_24h"], 1)
        it = by_issue(m)[28]
        self.assertEqual((it["state"], it["state_label_cs"]), ("done", "Hotovo"))
        self.assertIsNone(it["mismatch"])                   # receipt_ready vs done is compatible


class PrLinking(unittest.TestCase):
    def test_closing_references_and_branch_fallback(self):
        prs = [pr(19, "feat/delivery-framework-v2-20261029-001", closes=[18], draft=True),
               pr(31, "feature/30-docs-review"),
               pr(32, "fix/issue-7-typo"),
               pr(13, "feature/rt01-quota-aware-001")]   # "001" must NOT link to issue 1
        links, unlinked = wq.link_prs(prs, REPO)
        self.assertEqual([p["number"] for p in links[18]], [19])
        self.assertEqual([p["number"] for p in links[30]], [31])
        self.assertEqual([p["number"] for p in links[7]], [32])
        self.assertNotIn(1, links)
        self.assertEqual([p["number"] for p in unlinked], [13])

    def test_pr_attached_and_unlinked_pr_listed(self):
        raw = {"issues": [issue(18, ["agent:running"]), issue(1, ["state:queued"])],
               "prs": [pr(19, "feat/x", closes=[18], review="REVIEW_REQUIRED"),
                       pr(13, "feature/rt01-quota-aware-001", checks=[check(conclusion="FAILURE")])],
               "tasks": [task_new(18, "running")]}
        m = wq.build_model(raw, now=NOW, repo=REPO)
        it = by_issue(m)
        self.assertEqual(it[18]["pr"], {"number": 19, "url": "%s/pull/19" % GH, "title": "PR 19",
                                        "draft": False, "ci": "success", "review": "REVIEW_REQUIRED"})
        self.assertIsNone(it[1]["pr"])
        prs = [i for i in m["items"] if i["kind"] == "pr"]
        self.assertEqual([(p["pr"]["number"], p["state"], p["tracked"]) for p in prs], [(13, "failed", False)])
        self.assertEqual(m["summary"]["open_prs"], 2)
        self.assertEqual(m["summary"]["failed"], 0)          # untracked PR not counted


class CiSummary(unittest.TestCase):
    def test_ci(self):
        self.assertEqual(wq.summarize_ci([]), "none")
        self.assertEqual(wq.summarize_ci(None), "none")
        self.assertEqual(wq.summarize_ci([check(), check()]), "success")
        self.assertEqual(wq.summarize_ci([check(), check("IN_PROGRESS", "")]), "pending")
        self.assertEqual(wq.summarize_ci([check("IN_PROGRESS", ""), check(conclusion="FAILURE")]), "failure")
        self.assertEqual(wq.summarize_ci([check(conclusion="SKIPPED"), check(conclusion="NEUTRAL")]), "success")
        self.assertEqual(wq.summarize_ci([{"__typename": "StatusContext", "state": "PENDING"}]), "pending")
        self.assertEqual(wq.summarize_ci([{"__typename": "StatusContext", "state": "ERROR"}]), "failure")


class Mismatch(unittest.TestCase):
    def model(self, labels, tasks, closed=False):
        iss = issue(1, labels)
        raw = {"tasks": tasks}
        if closed:
            iss.update(state="CLOSED", closedAt=iso(3600))
            raw["closed"] = [iss]
        else:
            raw["issues"] = [iss]
        return wq.build_model(raw, now=NOW, repo=REPO)

    def test_github_running_without_task(self):
        it = by_issue(self.model(["state:running"], []))[1]
        self.assertEqual(it["state"], "running")             # GitHub stays authoritative
        self.assertIn("nemá k issue žádnou úlohu", it["mismatch"])

    def test_github_running_task_finished(self):
        it = by_issue(self.model(["state:running"], [task_old("TASK-007", 1, "superseded")]))[1]
        self.assertIn("Hotovo", it["mismatch"])
        self.assertIn("superseded", it["mismatch"])

    def test_supervisor_running_label_queued(self):
        it = by_issue(self.model(["agent:pending"], [task_new(1, "running")]))[1]
        self.assertIn("Supervisor úlohu právě spouští", it["mismatch"])

    def test_queued_vs_blocked(self):
        it = by_issue(self.model(["state:queued"], [task_old("X", 1, "blocked")]))[1]
        self.assertIn("Blokováno", it["mismatch"])

    def test_compatible_pairs(self):
        self.assertIsNone(by_issue(self.model(["agent:blocked"], [task_new(1, "failed")]))[1]["mismatch"])
        self.assertIsNone(by_issue(self.model(["state:queued"], [task_old("X", 1, "done")]))[1]["mismatch"])
        self.assertIsNone(by_issue(self.model(["state:running"], [task_new(1, "running")]))[1]["mismatch"])

    def test_closed_issue_with_running_task(self):
        it = by_issue(self.model(["agent:done"], [task_new(1, "running")], closed=True))[1]
        self.assertIn("není otevřené", it["mismatch"])

    def test_active_task_preferred_over_newer_finished(self):
        tasks = [task_old("OLD-RUN", 1, "running", offset=9000), task_old("NEW-DONE", 1, "done", offset=60)]
        it = by_issue(self.model(["state:running"], tasks))[1]
        self.assertEqual(it["task"]["id"], "OLD-RUN")
        self.assertIsNone(it["mismatch"])

    def test_orphan_tasks(self):
        raw = {"tasks": [{"id": "LOCAL-1", "status": "running", "goal": "lokální úloha",
                          "updated_at": iso(30)},
                         {"id": "LOCAL-OLD", "status": "blocked", "updated_at": iso(30)},
                         task_new(77, "queued")]}
        m = wq.build_model(raw, now=NOW, repo=REPO)
        ids = {i["task"]["id"]: i for i in m["items"]}
        self.assertEqual(set(ids), {"LOCAL-1", "GH-agentops-config-77"})
        self.assertIn("není otevřené", ids["GH-agentops-config-77"]["mismatch"])
        self.assertEqual(m["summary"]["running"], 1)


class Sanitizing(unittest.TestCase):
    def test_url_whitelist(self):
        self.assertEqual(wq.safe_url("%s/issues/1" % GH), "%s/issues/1" % GH)
        for bad in ("http://github.com/a/b", "https://github.com.evil.io/a", "https://evil.io/github.com/",
                    "javascript:alert(1)", "https://user:pw@github.com/a", "https://gist.github.com/x",
                    'https://github.com/a"onmouseover=x', None, 5):
            self.assertIsNone(wq.safe_url(bad), bad)

    def test_non_github_urls_dropped_from_items(self):
        raw = {"issues": [issue(1, ["agent:pending"], url="https://evil.example/issues/1")],
               "prs": [dict(pr(2, "x", closes=[1]), url="javascript:alert(1)")]}
        it = by_issue(wq.build_model(raw, now=NOW, repo=REPO))[1]
        self.assertIsNone(it["url"])
        self.assertIsNone(it["pr"]["url"])

    def test_task_whitelist_and_scrubbing(self):
        t = task_old("T", 1, "failed", error="boom in /home/someone/.config/secret/key.json token ghp_" + "a" * 36)
        t["authorization"] = {"token": "ghp_" + "b" * 36}
        out = wq.build_model({"issues": [issue(1)], "tasks": [t]}, now=NOW, repo=REPO)
        blob = json.dumps(out)
        self.assertNotIn("/home/someone", blob)
        self.assertNotIn("ghp_", blob)
        self.assertNotIn("command_fingerprint", blob)
        self.assertNotIn("authorization", blob)
        self.assertIn("key.json", by_issue(out)[1]["task"]["error"])
        long = wq._clean("x" * 500, 160)
        self.assertEqual(len(long), 160)


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "tasks").mkdir()
        (d / "supervisor.status.json").write_text(json.dumps(SUPERVISOR))
        (d / "tasks" / "GH-agentops-config-1.json").write_text(json.dumps(task_new(1, "running")))
        (d / "tasks" / "_TEMPLATE.json").write_text(json.dumps({"id": "TEMPLATE", "status": "template"}))
        (d / "tasks" / "broken.json").write_text("{nope")
        self.dir = str(d)
        self.fail_gh = False
        self.calls = []
        self.clock = [NOW]

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, args, timeout):
        self.calls.append(args)
        self.assertLessEqual(timeout, 15)
        self.assertIn(args[:2], (["issue", "list"], ["pr", "list"]))
        if self.fail_gh:
            raise RuntimeError("gh selhalo (1): HTTP 502")
        if args[0] == "pr":
            return json.dumps([pr(5, "feature/1-x")])
        if "closed" in args:
            return "[]"
        return json.dumps([issue(1, ["state:running", "worker:cursor"])])

    def queue(self):
        return wq.WorkQueue(repo=REPO, state_dir=self.dir, ttl=120, runner=self.runner,
                            clock=lambda: self.clock[0])

    def test_refresh_and_snapshot(self):
        q = self.queue()
        self.assertTrue(q.snapshot()["loading"])
        self.assertTrue(q.refresh())
        self.assertEqual(len(self.calls), 3)
        m = q.snapshot()
        self.assertFalse(m["meta"]["stale"])
        self.assertEqual(m["meta"]["source_errors"], ["supervisor: 1 nečitelných souborů úloh"])
        it = by_issue(m)[1]
        self.assertEqual((it["state"], it["task"]["status"], it["pr"]["number"]), ("running", "running", 5))
        self.assertIsNone(it["mismatch"])
        self.assertEqual(m["summary"]["running_slots_max"], 3)
        self.assertEqual(q.next_delay(), 120)

    def test_gh_failure_keeps_stale_data_and_backs_off(self):
        q = self.queue()
        q.refresh()
        self.fail_gh = True
        self.clock[0] += 130
        self.assertFalse(q.refresh())
        m = q.snapshot()
        self.assertTrue(m["meta"]["stale"])
        self.assertTrue(any("GitHub issues" in e and "502" in e for e in m["meta"]["source_errors"]))
        self.assertEqual(by_issue(m)[1]["state"], "running")   # last good GitHub data kept
        self.assertEqual(m["meta"]["last_success_at"], NOW)
        d1 = q.next_delay()
        q.refresh()
        self.assertGreater(q.next_delay(), d1)
        for _ in range(10):
            q.refresh()
        self.assertEqual(q.next_delay(), wq.MAX_BACKOFF)
        self.fail_gh = False
        q.refresh()
        self.assertEqual(q.next_delay(), 120)
        self.assertFalse(q.snapshot()["meta"]["stale"])

    def test_gh_failure_without_previous_data(self):
        self.fail_gh = True
        q = self.queue()
        q.refresh()
        m = q.snapshot()
        self.assertEqual(len([e for e in m["meta"]["source_errors"] if e.startswith("GitHub")]), 3)
        self.assertTrue(m["meta"]["stale"])
        # the supervisor still contributes its running task
        self.assertEqual(m["summary"]["running"], 1)

    def test_invalid_json_from_gh(self):
        q = wq.WorkQueue(repo=REPO, state_dir=self.dir, runner=lambda a, t: "<html>", clock=lambda: NOW)
        q.refresh()
        self.assertEqual(len([e for e in q.snapshot()["meta"]["source_errors"] if e.startswith("GitHub")]), 3)

    def test_invalid_repo_never_calls_gh(self):
        q = wq.WorkQueue(repo="evil; rm -rf /", state_dir=self.dir, runner=self.runner, clock=lambda: NOW)
        q.refresh()
        self.assertEqual(self.calls, [])
        self.assertIn("neplatný název repozitáře", q.snapshot()["meta"]["source_errors"])

    def test_run_gh_rejects_write_commands(self):
        for args in (["issue", "edit", "1"], ["pr", "merge", "1"], ["api", "x"], []):
            with self.assertRaises(RuntimeError):
                wq.run_gh(args)


class ConfigApi(unittest.TestCase):
    def test_disabled(self):
        self.assertEqual(wq.get({}), {"enabled": False})
        self.assertEqual(wq.get({"queue": {"enabled": False}}), {"enabled": False})
        self.assertEqual(wq.get({"queue": None}), {"enabled": False})
        self.assertEqual(wq.get(None), {"enabled": False})
        wq.start({"queue": {}})   # no thread, no error

    def test_defaults(self):
        q = wq._qcfg({"queue": {"enabled": 1}})
        self.assertEqual((q["repo"], q["ttl"], q["enabled"]), (REPO, 120, True))

    def test_get_never_raises(self):
        orig = wq._get_instance
        wq._get_instance = lambda cfg: (_ for _ in ()).throw(ValueError("x"))
        try:
            out = wq.get({"queue": {"enabled": True}})
        finally:
            wq._get_instance = orig
        self.assertTrue(out["meta"]["stale"])
        self.assertTrue(out["meta"]["source_errors"])


if __name__ == "__main__":
    unittest.main()
