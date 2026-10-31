"""Work queue ("Fronta práce") model for the dashboard.

Joins two read-only sources into one list of work items:

* GitHub (authoritative) via the ``gh`` CLI: open issues, open PRs and issues closed in the
  last 24 h of one repository. Only read commands (``gh issue list`` / ``gh pr list``) are run.
* The local LANA supervisor adapter: ``supervisor.status.json`` and ``tasks/*.json`` in the
  orchestration state directory. Only a whitelist of fields is read; free text is truncated
  and absolute paths / token-like strings are scrubbed.

``build_model()`` is a pure function of the raw fetched data (unit-testable with fixtures).
``WorkQueue`` adds a background refresher (TTL, serial gh calls with timeouts, exponential
backoff, last-good data kept and marked stale) so the dashboard poll only reads a cache.

Config (``cfg['queue']``, all optional): ``enabled`` (default False), ``repo``
(default ``h0nz1k/agentops-config``), ``ttl_seconds`` (120), ``state_dir``
(``~/.local/state/lana/orchestration``).
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_REPO = "h0nz1k/agentops-config"
DEFAULT_STATE_DIR = "~/.local/state/lana/orchestration"
DEFAULT_TTL = 120
GH_TIMEOUT = 15
MAX_BACKOFF = 900
DONE_WINDOW = 24 * 3600
MAX_TASK_FILES = 500
MAX_TASK_BYTES = 256 * 1024

STATES = ("failed", "blocked", "running", "awaiting_pickup", "queued", "done")
SEVERITY = {s: i for i, s in enumerate(STATES)}
STATE_LABEL_CS = {
    "queued": "Ve frontě",
    "running": "Běží",
    "awaiting_pickup": "Čeká na převzetí",
    "blocked": "Blokováno",
    "failed": "Selhalo",
    "done": "Hotovo",
}

# GitHub label -> unified state. Two vocabularies are in use: ``state:*`` (supervisor bridge)
# and ``agent:*`` (agent queue). Unknown values are ignored.
LABEL_STATE = {
    "state:queued": "queued", "state:pending": "queued",
    "state:running": "running",
    "state:receipt_ready": "awaiting_pickup", "state:review": "awaiting_pickup",
    "state:blocked": "blocked",
    "state:failed": "failed",
    "state:done": "done",
    "agent:pending": "queued", "agent:queued": "queued",
    "agent:running": "running",
    "agent:review": "awaiting_pickup", "agent:receipt_ready": "awaiting_pickup",
    "agent:blocked": "blocked",
    "agent:failed": "failed",
    "agent:done": "done",
}

# Supervisor task status -> unified state. ``None`` = ignore the task (template etc.).
TASK_STATE = {
    "queued": "queued", "pending": "queued", "requeued": "queued", "ready": "queued",
    "admitted": "queued", "waiting": "queued",
    "running": "running", "starting": "running", "launching": "running",
    "in_progress": "running", "active": "running",
    "receipt_ready": "awaiting_pickup", "review": "awaiting_pickup",
    "awaiting_review": "awaiting_pickup",
    "blocked": "blocked", "paused": "blocked", "on_hold": "blocked",
    "failed": "failed", "error": "failed", "timeout": "failed", "timed_out": "failed",
    "retries_exhausted": "failed", "crashed": "failed",
    "done": "done", "completed": "done", "superseded": "done", "merged": "done",
    "closed": "done", "cancelled": "done", "canceled": "done",
    "template": None,
}

# (github_state, supervisor_state) pairs that are NOT a disagreement.
_COMPATIBLE = {
    ("blocked", "failed"), ("failed", "blocked"),
    ("done", "awaiting_pickup"), ("awaiting_pickup", "done"),
    # Issue re-queued after an earlier task finished.
    ("queued", "done"), ("queued", "awaiting_pickup"),
}

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PATH_RE = re.compile(r"(?<![^\s'\"=(:,])~?/(?:[^\s/'\"]+/)+([^\s/'\"]*)")
_TOKEN_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{10,}"
                       r"|xox[abp]-[A-Za-z0-9-]{10,}|[A-Fa-f0-9]{40,}|eyJ[A-Za-z0-9_-]{20,}\S*)")


# --------------------------------------------------------------------------- helpers

def _clean(text, limit=160):
    """Single-line, truncated text with absolute paths reduced to their last name and
    token-like strings redacted."""
    if text is None:
        return None
    s = " ".join(str(text).split())
    s = _TOKEN_RE.sub("[skryto]", s)
    s = _PATH_RE.sub(lambda m: m.group(1) or "…", s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def safe_url(url):
    """Return the URL only if it is a plain https://github.com/... link, else None."""
    if not isinstance(url, str) or len(url) > 300:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme != "https" or p.netloc != "github.com" or p.username or p.password:
        return None
    if any(c in url for c in " \t\r\n\"'<>\\"):
        return None
    return url


def _epoch(value):
    """ISO-8601 string / number -> epoch seconds (float) or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _label_names(obj):
    out = []
    for lab in (obj or {}).get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else lab
        if isinstance(name, str) and name:
            out.append(name[:50])
    return out[:20]


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None


def state_from_labels(labels):
    """Unified state from GitHub labels (most severe wins if several are present)."""
    found = [LABEL_STATE[n.lower()] for n in labels if n.lower() in LABEL_STATE]
    if not found:
        return None
    return min(found, key=lambda s: SEVERITY[s])


def _prefixed(labels, prefix):
    for n in labels:
        if n.lower().startswith(prefix):
            return n.split(":", 1)[1][:40] or None
    return None


def summarize_ci(rollup):
    """statusCheckRollup -> 'success' | 'failure' | 'pending' | 'none'."""
    if not rollup:
        return "none"
    bad = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    pending = False
    for c in rollup:
        if not isinstance(c, dict):
            continue
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
            st = str(c.get("state") or "").upper()
            if st in ("FAILURE", "ERROR"):
                return "failure"
            if st in ("PENDING", "EXPECTED", ""):
                pending = True
            continue
        status = str(c.get("status") or "").upper()
        concl = str(c.get("conclusion") or "").upper()
        if status and status != "COMPLETED":
            pending = True
        elif concl in bad:
            return "failure"
    return "pending" if pending else "success"


_BRANCH_PATTERNS = (
    re.compile(r"(?:^|[/_-])(?:issues?|gh|fix|closes|resolves)[-_/#]?(\d+)(?=$|[/_-])", re.I),
    re.compile(r"(?:^|/)(\d+)[-_]"),                    # feature/30-docs, 30-docs
    re.compile(r"agentops-config-(\d+)(?=$|[/_-])", re.I),  # GH-agentops-config-30 style
)


def _issues_from_branch(branch):
    if not isinstance(branch, str):
        return []
    for rx in _BRANCH_PATTERNS:
        m = rx.search(branch)
        if m:
            return [int(m.group(1))]
    return []


def _pr_view(pr):
    return {
        "number": _int(pr.get("number")),
        "url": safe_url(pr.get("url")),
        "title": _clean(pr.get("title"), 140),
        "draft": bool(pr.get("isDraft")),
        "ci": summarize_ci(pr.get("statusCheckRollup")),
        "review": _clean(pr.get("reviewDecision") or None, 40),
    }


def link_prs(prs, repo=None):
    """Map issue number -> list of PRs. closingIssuesReferences first, branch-name fallback."""
    links, unlinked = {}, []
    for pr in prs or []:
        nums = []
        for ref in pr.get("closingIssuesReferences") or []:
            if not isinstance(ref, dict):
                continue
            rrepo = ref.get("repository") or {}
            full = "%s/%s" % ((rrepo.get("owner") or {}).get("login", ""), rrepo.get("name", ""))
            if repo and rrepo and full.lower() != repo.lower():
                continue
            n = _int(ref.get("number"))
            if n is not None:
                nums.append(n)
        if not nums:
            nums = _issues_from_branch(pr.get("headRefName"))
        if nums:
            for n in nums:
                links.setdefault(n, []).append(pr)
        else:
            unlinked.append(pr)
    return links, unlinked


def task_issue(task, repo=None):
    """Issue number a supervisor task belongs to (or None)."""
    src = task.get("source")
    if isinstance(src, dict) and src.get("kind", "github_issue") == "github_issue":
        srepo = src.get("repo")
        if repo and srepo and str(srepo).lower() != repo.lower():
            return None
        n = _int(src.get("number"))
        if n is not None:
            return n
    trepo = task.get("github_repo") or task.get("repo")
    if repo and isinstance(trepo, str) and _REPO_RE.match(trepo) and trepo.lower() != repo.lower():
        return None
    n = _int(task.get("issue_number"))
    if n is not None:
        return n
    for key in ("issue", "issue_url"):
        v = task.get(key)
        if _int(v) is not None:
            return _int(v)
        if isinstance(v, str):
            m = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", v)
            if m and (not repo or m.group(1).lower() == repo.lower()):
                return int(m.group(2))
    return None


def normalize_task(raw, repo=None, mtime=None):
    """Whitelist of supervisor task fields. Returns None for templates / unusable records."""
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "").strip().lower()
    if status in TASK_STATE:
        state = TASK_STATE[status]
        if state is None:
            return None
    elif not status:
        state = "queued"
    else:
        state = "blocked"  # unknown status: surface it rather than hide it
    tid = raw.get("id") or raw.get("task_id")
    retries = raw.get("retries")
    if retries is None:
        retries = raw.get("attempt_number") or raw.get("current_attempt")
    updated = None
    for key in ("updated_at", "finished_at", "started_at", "created_at"):
        updated = _epoch(raw.get(key))
        if updated:
            break
    return {
        "id": _clean(tid, 80),
        "status": _clean(status, 40),
        "state": state,
        "issue": task_issue(raw, repo),
        "attempts": _int(retries) if _int(retries) is not None else 0,
        "max_retries": _int(raw.get("max_retries")),
        "error": _clean(raw.get("error") or raw.get("blocked_reason"), 160),
        "lane": _clean(raw.get("lane") or raw.get("provider"), 40),
        "title": _clean(raw.get("title") or raw.get("goal"), 140),
        "updated_at": updated or mtime,
    }


def _pick_task(tasks):
    """Most relevant task for one issue: active ones first, then the newest."""
    active = [t for t in tasks if t["state"] in ("running", "queued")]
    pool = active or tasks
    return max(pool, key=lambda t: ((t["state"] == "running"), t.get("updated_at") or 0))


def _mismatch(gh_state, task, issue_open=True):
    tstate = task["state"] if task else None
    if not issue_open:
        if tstate in ("running", "queued"):
            return ("Supervisor má úlohu ve stavu „%s“, ale issue na GitHubu není otevřené."
                    % STATE_LABEL_CS[tstate])
        return None
    if gh_state == "running" and tstate != "running":
        if task is None:
            return "GitHub uvádí „Běží“, ale supervisor nemá k issue žádnou úlohu."
        return ("GitHub uvádí „Běží“, ale úloha supervisoru je ve stavu „%s“ (%s)."
                % (STATE_LABEL_CS[tstate], task.get("status")))
    if task is None or gh_state is None or gh_state == tstate:
        return None
    if tstate == "running":
        return ("Supervisor úlohu právě spouští, ale štítek na GitHubu je „%s“."
                % STATE_LABEL_CS[gh_state])
    if (gh_state, tstate) in _COMPATIBLE:
        return None
    return ("Štítek na GitHubu je „%s“, ale supervisor hlásí „%s“ (%s)."
            % (STATE_LABEL_CS[gh_state], STATE_LABEL_CS[tstate], task.get("status")))


def _task_view(t):
    if not t:
        return None
    return {k: t.get(k) for k in ("id", "status", "attempts", "max_retries", "error", "lane")}


def _item(now, *, issue, title, url, state, tracked, worker=None, profile=None, labels=(),
          updated=None, pr=None, task=None, mismatch=None, kind="issue"):
    return {
        "kind": kind,
        "issue": issue,
        "title": title or "",
        "url": safe_url(url),
        "state": state,
        "state_label_cs": STATE_LABEL_CS[state],
        "tracked": tracked,
        "worker": worker,
        "profile": profile,
        "labels": list(labels),
        "updated_at": updated,
        "age_seconds": int(max(0, now - updated)) if updated else None,
        "pr": pr,
        "task": _task_view(task),
        "mismatch": mismatch,
    }


# --------------------------------------------------------------------------- pure model

def build_model(raw, now=None, repo=DEFAULT_REPO):
    """Build the work-queue model from raw fetched data.

    ``raw`` keys (all optional): ``issues``, ``prs``, ``closed`` (gh JSON lists),
    ``supervisor`` (supervisor.status.json dict), ``tasks`` (list of task dicts or
    ``(dict, mtime)`` tuples), ``source_errors`` (list of str), ``stale`` (bool),
    ``fetched_at`` (epoch).
    """
    now = time.time() if now is None else now
    issues = [i for i in raw.get("issues") or [] if isinstance(i, dict) and _int(i.get("number")) is not None]
    prs = [p for p in raw.get("prs") or [] if isinstance(p, dict)]
    closed = [i for i in raw.get("closed") or [] if isinstance(i, dict) and _int(i.get("number")) is not None]
    sup = raw.get("supervisor") if isinstance(raw.get("supervisor"), dict) else {}

    tasks = []
    for entry in raw.get("tasks") or []:
        t, mt = (entry if isinstance(entry, tuple) else (entry, None))
        nt = normalize_task(t, repo, mt)
        if nt:
            tasks.append(nt)
    by_issue = {}
    for t in tasks:
        if t["issue"] is not None:
            by_issue.setdefault(t["issue"], []).append(t)

    pr_links, unlinked_prs = link_prs(prs, repo)
    items, seen = [], set()

    def best_pr(num):
        cands = pr_links.get(num) or []
        if not cands:
            return None
        cands = sorted(cands, key=lambda p: (not p.get("isDraft"), _epoch(p.get("updatedAt")) or 0))
        return _pr_view(cands[-1])

    for iss in issues:
        num = _int(iss["number"])
        seen.add(num)
        labels = _label_names(iss)
        gh_state = state_from_labels(labels)
        task = _pick_task(by_issue[num]) if num in by_issue else None
        # GitHub is authoritative; supervisor state only fills in unlabeled issues.
        state = gh_state or (task["state"] if task else "queued")
        worker = _prefixed(labels, "worker:") or (task.get("lane") if task else None)
        if not worker:
            assg = [a.get("login") for a in iss.get("assignees") or [] if isinstance(a, dict)]
            worker = _clean(assg[0], 40) if assg else None
        items.append(_item(
            now, issue=num, title=_clean(iss.get("title"), 140), url=iss.get("url"),
            state=state, tracked=bool(gh_state or task), worker=worker,
            profile=_prefixed(labels, "profile:"), labels=labels,
            updated=max(filter(None, [_epoch(iss.get("updatedAt")), task and task.get("updated_at")]), default=None),
            pr=best_pr(num), task=task, mismatch=_mismatch(gh_state, task)))

    done_24h = 0
    for iss in closed:
        num = _int(iss["number"])
        if num in seen:
            continue
        closed_at = _epoch(iss.get("closedAt")) or _epoch(iss.get("updatedAt"))
        if closed_at and now - closed_at > DONE_WINDOW:
            continue
        seen.add(num)
        done_24h += 1
        labels = _label_names(iss)
        task = _pick_task(by_issue[num]) if num in by_issue else None
        items.append(_item(
            now, issue=num, title=_clean(iss.get("title"), 140), url=iss.get("url"),
            state="done", tracked=True, worker=_prefixed(labels, "worker:") or (task and task.get("lane")),
            profile=_prefixed(labels, "profile:"), labels=labels, updated=closed_at,
            pr=best_pr(num), task=task, mismatch=_mismatch("done", task, issue_open=False)))

    # PRs that are not linked to any listed issue.
    for pr in unlinked_prs + [p for n, ps in pr_links.items() if n not in seen for p in ps]:
        view = _pr_view(pr)
        labels = _label_names(pr)
        state = "failed" if view["ci"] == "failure" else ("queued" if view["draft"] else "awaiting_pickup")
        items.append(_item(
            now, issue=None, title=view["title"], url=view["url"], state=state, tracked=False,
            worker=_prefixed(labels, "worker:"), profile=_prefixed(labels, "profile:"),
            labels=labels, updated=_epoch(pr.get("updatedAt")), pr=view, kind="pr"))

    # Supervisor tasks without an open/recent issue: show only active or freshly failed ones.
    for num_or_none, group in list(by_issue.items()) + [(None, [t for t in tasks if t["issue"] is None])]:
        if num_or_none is not None and num_or_none in seen:
            continue
        for t in group:
            fresh_fail = t["state"] == "failed" and t.get("updated_at") and now - t["updated_at"] <= DONE_WINDOW
            if t["state"] not in ("running", "queued") and not fresh_fail:
                continue
            mm = None
            if num_or_none is not None and t["state"] in ("running", "queued"):
                mm = _mismatch(None, t, issue_open=False)
            url = ("https://github.com/%s/issues/%d" % (repo, num_or_none)) if num_or_none else None
            items.append(_item(
                now, issue=num_or_none, title=t.get("title") or t.get("id"), url=url,
                state=t["state"], tracked=True, worker=t.get("lane"), updated=t.get("updated_at"),
                task=t, mismatch=mm, kind="task"))

    items.sort(key=lambda it: (SEVERITY[it["state"]], -(it["updated_at"] or 0)))

    conc = sup.get("concurrency") if isinstance(sup.get("concurrency"), dict) else {}
    slots = _int(conc.get("max_running"))
    counts = {s: 0 for s in STATES}
    for it in items:
        if it["tracked"]:
            counts[it["state"]] += 1
    summary = {
        "running": counts["running"],
        "running_slots_max": slots if slots and slots > 0 else 2,
        "supervisor_running": _int(conc.get("running")),
        "queued": counts["queued"],
        "awaiting_pickup": counts["awaiting_pickup"],
        "blocked": counts["blocked"],
        "failed": counts["failed"],
        "done_24h": done_24h,
        "open_issues": len(issues),
        "open_prs": len(prs),
        "untracked": sum(1 for it in items if not it["tracked"]),
        "mismatches": sum(1 for it in items if it["mismatch"]),
    }
    meta = {
        "fetched_at": raw.get("fetched_at") or now,
        "source_errors": [_clean(e, 200) for e in raw.get("source_errors") or []],
        "stale": bool(raw.get("stale")),
        "repo": repo,
        "repo_url": safe_url("https://github.com/%s" % repo),
        "supervisor_at": _epoch(sup.get("at")),
    }
    return {"enabled": True, "items": items, "summary": summary, "meta": meta}


# --------------------------------------------------------------------------- fetching

def run_gh(args, timeout=GH_TIMEOUT):
    """Run a read-only gh command and return stdout. Raises RuntimeError on failure."""
    if len(args) < 2 or args[0] not in ("issue", "pr") or args[1] not in ("list", "view"):
        raise RuntimeError("nepovolený příkaz gh")
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env={**os.environ, "GH_PROMPT_DISABLED": "1",
                                                          "GH_NO_UPDATE_NOTIFIER": "1"})
    except FileNotFoundError:
        raise RuntimeError("gh CLI není nainstalováno")
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh: vypršel časový limit %d s" % timeout)
    if p.returncode != 0:
        raise RuntimeError("gh selhalo (%d): %s" % (p.returncode, _clean(p.stderr, 160)))
    return p.stdout


def gh_commands(repo, now=None):
    now = time.time() if now is None else now
    since = datetime.fromtimestamp(now - DONE_WINDOW, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "issues": ["issue", "list", "-R", repo, "--state", "open", "--limit", "50", "--json",
                   "number,title,url,labels,assignees,updatedAt,createdAt,state"],
        "prs": ["pr", "list", "-R", repo, "--state", "open", "--limit", "30", "--json",
                "number,title,url,isDraft,headRefName,updatedAt,reviewDecision,statusCheckRollup,"
                "closingIssuesReferences,labels"],
        "closed": ["issue", "list", "-R", repo, "--state", "closed", "--search", "closed:>=" + since,
                   "--limit", "50", "--json", "number,title,url,labels,updatedAt,closedAt,state"],
    }


def read_supervisor(state_dir):
    """Return (status_dict, [(task_dict, mtime)], errors)."""
    d = Path(os.path.expanduser(str(state_dir)))
    errors, status, tasks = [], {}, []
    try:
        status = json.loads((d / "supervisor.status.json").read_text())
        if not isinstance(status, dict):
            status = {}
    except FileNotFoundError:
        errors.append("supervisor: chybí supervisor.status.json")
    except (OSError, ValueError) as e:
        errors.append("supervisor: nečitelný stav (%s)" % type(e).__name__)
    try:
        files = sorted((d / "tasks").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    bad = 0
    for f in files[:MAX_TASK_FILES]:
        try:
            st = f.stat()
            if st.st_size > MAX_TASK_BYTES:
                bad += 1
                continue
            tasks.append((json.loads(f.read_text()), st.st_mtime))
        except (OSError, ValueError):
            bad += 1
    if bad:
        errors.append("supervisor: %d nečitelných souborů úloh" % bad)
    return status, tasks, errors


# --------------------------------------------------------------------------- runtime cache

def _qcfg(cfg):
    q = (cfg or {}).get("queue") or {}
    if not isinstance(q, dict):
        q = {}
    repo = q.get("repo") or DEFAULT_REPO
    try:
        ttl = max(30, int(q.get("ttl_seconds") or DEFAULT_TTL))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    return {"enabled": bool(q.get("enabled")), "repo": repo, "ttl": ttl,
            "state_dir": q.get("state_dir") or DEFAULT_STATE_DIR}


class WorkQueue:
    """Background-refreshed cache of the work-queue model."""

    def __init__(self, repo=DEFAULT_REPO, state_dir=DEFAULT_STATE_DIR, ttl=DEFAULT_TTL,
                 runner=run_gh, clock=time.time):
        self.repo, self.state_dir, self.ttl = repo, state_dir, ttl
        self.runner, self.clock = runner, clock
        self._lock = threading.Lock()
        self._raw = {}            # last good raw data per source
        self._model = None
        self._failures = 0
        self._last_ok = None
        self._thread = None
        self._stop = threading.Event()

    # -- one refresh cycle (serial gh calls) -------------------------------------
    def refresh(self):
        now = self.clock()
        errors, stale = [], False
        if not _REPO_RE.match(self.repo or ""):
            errors.append("neplatný název repozitáře")
            fresh = {}
        else:
            fresh = {}
            for key, args in gh_commands(self.repo, now).items():
                try:
                    data = json.loads(self.runner(args, GH_TIMEOUT) or "[]")
                    if not isinstance(data, list):
                        raise ValueError("neočekávaný formát")
                    fresh[key] = data
                except Exception as e:  # noqa: BLE001 - every failure becomes a source error
                    errors.append("GitHub %s: %s" % (key, _clean(e, 160)))
        gh_ok = not errors
        try:
            status, tasks, sup_err = read_supervisor(self.state_dir)
            errors.extend(sup_err)
            fresh["supervisor"], fresh["tasks"] = status, tasks
        except Exception as e:  # noqa: BLE001
            errors.append("supervisor: %s" % _clean(e, 160))
        with self._lock:
            for key in ("issues", "prs", "closed", "supervisor", "tasks"):
                if key in fresh:
                    self._raw[key] = fresh[key]
                elif key in self._raw:
                    stale = True
            if gh_ok:
                self._failures, self._last_ok = 0, now
            else:
                self._failures += 1
            raw = dict(self._raw, source_errors=errors, stale=stale, fetched_at=now)
        try:
            model = build_model(raw, now=now, repo=self.repo)
        except Exception as e:  # noqa: BLE001 - keep the previous model
            with self._lock:
                if self._model:
                    self._model["meta"]["source_errors"] = ["model: %s" % _clean(e, 160)]
                    self._model["meta"]["stale"] = True
            return False
        model["meta"]["last_success_at"] = self._last_ok
        with self._lock:
            self._model = model
        return gh_ok

    def next_delay(self):
        if self._failures <= 0:
            return self.ttl
        return min(MAX_BACKOFF, 30 * 2 ** (self._failures - 1))

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the loop must never die
                self._failures += 1
            self._stop.wait(self.next_delay())

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="agentsmon-workqueue", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        now = self.clock()
        with self._lock:
            model = copy.deepcopy(self._model)
            last_ok = self._last_ok
        if model is None:
            summary = {k: 0 for k in ("running", "queued", "awaiting_pickup", "blocked", "failed",
                                      "done_24h", "open_issues", "open_prs", "untracked", "mismatches")}
            summary["running_slots_max"] = 2
            return {"enabled": True, "loading": True, "items": [], "summary": summary,
                    "meta": {"fetched_at": None, "source_errors": [], "stale": True,
                             "repo": self.repo, "repo_url": safe_url("https://github.com/%s" % self.repo)}}
        if last_ok is None or now - last_ok > 3 * self.ttl:
            model["meta"]["stale"] = True
        fetched = model["meta"].get("fetched_at")
        for it in model["items"]:  # ages relative to the read time, not the fetch time
            if it.get("updated_at"):
                it["age_seconds"] = int(max(0, now - it["updated_at"]))
        model["meta"]["age_seconds"] = int(max(0, now - fetched)) if fetched else None
        return model


_instance = None
_instance_key = None
_ilock = threading.Lock()


def _get_instance(cfg):
    global _instance, _instance_key
    q = _qcfg(cfg)
    key = (q["repo"], q["state_dir"], q["ttl"])
    with _ilock:
        if _instance is None or _instance_key != key:
            if _instance is not None:
                _instance.stop()
            _instance = WorkQueue(repo=q["repo"], state_dir=q["state_dir"], ttl=q["ttl"])
            _instance_key = key
        return _instance


def start(cfg):
    """Start the background refresher (no-op when the queue is disabled)."""
    try:
        if _qcfg(cfg)["enabled"]:
            _get_instance(cfg).start()
    except Exception:  # noqa: BLE001
        pass


def get(cfg):
    """Latest cached model; never raises, never blocks on gh."""
    try:
        if not _qcfg(cfg)["enabled"]:
            return {"enabled": False}
        inst = _get_instance(cfg)
        inst.start()
        return inst.snapshot()
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "items": [], "summary": {},
                "meta": {"source_errors": ["workqueue: %s" % _clean(e, 160)], "stale": True}}
