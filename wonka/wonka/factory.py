"""What the factory does to golden tickets. Every status change goes through Factory.move()."""

import datetime as dt
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import banner, machine, note, store
from .machine import KILLED
from .store import WonkaError

REVIEWERS = ("architect", "backend", "frontend", "security", "spark", "airflow")
MAX_REVIEW_ROUNDS = 3
MAX_RETRIES = 3
# the state a ticket is left in while each agent's job is unfinished
JOB_AGENTS = {"oompa-loompa-triage": ("inbox", "pending"), "oompa-loompa-analyst": ("drafting", "progress"),
              "oompa-loompa-builder": ("baking", "progress")}
REVIEW_LOG = re.compile(r"^## 🐿️ (?:Earlier review|Review) round \d+", re.M)


def _split_reviews(text):
    """Split a plan into (plan, review log): the log starts at the first review-round heading."""
    m = REVIEW_LOG.search(text)
    return (text, "") if not m else (text[:m.start()], text[m.start():])


def now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds")


def _age(stamp):
    return now() - dt.datetime.fromisoformat(stamp)


def _run(cmd, cwd=None):
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise WonkaError(f"{cmd[0]} is not installed")
    except subprocess.CalledProcessError as e:
        raise WonkaError(f"{' '.join(cmd[:3])} failed: {(e.stderr or e.stdout).strip()}")
    return out.stdout.strip()


def git(*args, cwd):
    return _run(["git", *args], cwd=cwd)


def gh(*args, cwd=None):
    return _run([os.environ.get("WONKA_GH", "gh"), *args], cwd=cwd)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].rstrip("-") or "ticket"


def parse_remote(url):
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else None


def repo_info(path):
    top = git("rev-parse", "--show-toplevel", cwd=path)
    try:
        base = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=top).split("/", 1)[1]
    except WonkaError:
        base = git("branch", "--show-current", cwd=top) or "main"
    try:
        origin = parse_remote(git("remote", "get-url", "origin", cwd=top))
    except WonkaError:
        origin = None
    return top, base, origin


def _ref_exists(repo, ref):
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, capture_output=True).returncode == 0


def pr_body(tid, meta, spec):
    ids = [f"🎫 **Golden ticket:** {tid}"]
    if meta.get("jira"):
        ids.append(f"🎟️ **Jira:** {meta['jira']}")
    return "  \n".join(ids) + f"\n\n{spec.strip()}\n\n🤖 Baked by the Wonka factory"


def fmt(state):
    return " · ".join(machine.label(state)) if machine.valid(state) else "/".join(map(str, state))


def notify(tid, title):
    if os.environ.get("WONKA_NO_NOTIFY"):
        return
    try:
        subprocess.run(["notify-send", "-a", "Wonka", "🎩 Wonka needs you", f"{tid} — {title}"], check=False)
    except FileNotFoundError:
        pass


class Factory:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db

    # -- tickets and notes ------------------------------------------------

    def ticket(self, tid):
        try:
            return self.db["tickets"][tid]
        except KeyError:
            raise WonkaError(f"No ticket {tid}")

    def path(self, tid):
        folder = store.archive_dir if self.ticket(tid).get("archived") else store.tickets_dir
        return folder(self.cfg) / f"{tid}.md"

    def read(self, tid):
        return note.read(self.path(tid))

    def write(self, tid, meta, body):
        """Every write re-renders the banner and the factory log from the JSON state."""
        t = self.ticket(tid)
        body = note.set_banner(body, banner.render(tid, t, meta, self.cfg))
        body = note.set_section(body, "log", banner.log(t))
        note.write(self.path(tid), meta, body)

    def summary(self, tid):
        """What a human or agent should read after a command: where the ticket is and what comes next."""
        return f"{tid}: {fmt(self.state(tid))}: {banner.headline(tid, self.ticket(tid), self.read(tid)[0], self.cfg)}"

    def log_event(self, tid, why, by="wonka"):
        self.ticket(tid)["history"].append({"at": _iso(now()), "by": by, "why": why})
        store.log(f"{tid} {by}: {why}")
        if self.path(tid).exists():
            meta, body = self.read(tid)
            self.write(tid, meta, body)

    def state(self, tid):
        lv = self.ticket(tid)["last_valid"]
        return lv["status"], lv["phase"]

    def expect(self, tid, *states):
        if self.state(tid) not in states:
            raise WonkaError(f"{tid} is {fmt(self.state(tid))}, expected {' or '.join(map(fmt, states))}")

    def move(self, tid, dst, by, why=None):
        t, src = self.ticket(tid), self.state(tid)
        if not machine.allowed(src, dst):
            raise WonkaError(f"{tid}: {fmt(src)} → {fmt(dst)} is not a legal move")
        t["last_valid"] = {"status": dst[0], "phase": dst[1]}
        t["history"].append({"at": _iso(now()), "from": "/".join(src), "to": "/".join(dst), "by": by,
                             **({"why": why} if why else {})})
        t.pop("job_started_at", None)
        if dst[1] == "done" and dst[0] in ("shipped", "wontdo"):
            t["entered_done_at"] = _iso(now())
        meta, body = self.read(tid)
        meta["status"], meta["phase"] = machine.label(dst)
        self.write(tid, meta, body)
        store.log(f"{tid} {'/'.join(src)} → {'/'.join(dst)} by {by}" + (f": {why}" if why else ""))
        if by != "wonka":
            t.pop("retries", None)
        if dst[1] == "done" and dst[0] in ("shipped", "wontdo"):
            self.drop_worktree(tid)
        if dst[1] == "awaiting":
            notify(tid, meta.get("title", ""))

    # -- intake -----------------------------------------------------------

    def add(self, idea, title=None, repo=None, pr_repo=None, parent=None, size=None, jira=None):
        if size and size not in machine.SIZES:
            raise WonkaError(f"size must be one of {', '.join(machine.SIZES)}")
        parent_t = self.ticket(parent) if parent else None  # fail before touching anything
        top, base, origin = repo_info(repo or (parent_t and parent_t["repo"]) or os.getcwd())
        if parent_t:
            # A child defaults to its parent's repo, PR target and Jira key. The PR target only
            # carries over when the child builds in the parent's repo; elsewhere it means nothing.
            if pr_repo is None and top == parent_t["repo"]:
                pr_repo = parent_t["pr_repo"]
            if jira is None:
                jira = self.read(parent)[0].get("jira") or None
        tid = f"WONKA-{self.db['next_id']:05d}"
        self.db["next_id"] += 1
        title = title or idea.strip().splitlines()[0][:60]
        meta = {"id": tid, "title": title, "size": size or "", "jira": jira or ""}
        meta["status"], meta["phase"] = machine.label(("inbox", "pending"))
        store.tickets_dir(self.cfg).mkdir(parents=True, exist_ok=True)
        self.db["tickets"][tid] = {
            "project": Path(top).name, "repo": top, "pr_repo": pr_repo or origin, "base_branch": base,
            "branch": None, "worktree": None, "pr_url": None, "plan": None, "reviewers": [],
            "review_round": 0, "parent": parent, "children": [],
            "last_valid": {"status": "inbox", "phase": "pending"},
            "created_at": _iso(now()), "history": [],
        }
        if parent_t:
            parent_t["children"].append(tid)
        self.write(tid, meta, note.set_section("", "idea", idea))
        return tid

    def set_field(self, tid, field, value):
        if field == "size" and value not in machine.SIZES:
            raise WonkaError(f"size must be one of {', '.join(machine.SIZES)}")
        if field in ("title", "size", "jira"):
            meta, body = self.read(tid)
            meta[field] = value
            self.write(tid, meta, body)
        elif field == "pr_repo":
            self.ticket(tid)["pr_repo"] = value
        else:
            raise WonkaError("settable fields: title, size, jira, pr_repo")

    def triage(self, tid, size, title=None, questions=None):
        self.expect(tid, ("inbox", "pending"))
        if size not in machine.SIZES:
            raise WonkaError(f"size must be one of {', '.join(machine.SIZES)}")
        meta, body = self.read(tid)
        meta["size"] = size
        if title:
            meta["title"] = title
        if size not in machine.AUTO_DONE:
            body = note.set_section(body, "approval", f"- [ ] Approve (size **{size}**: tick to send it to Drafting)")
        if questions:
            body = note.new_questions(body, note.render_questions(questions))
        self.write(tid, meta, body)
        if size in machine.AUTO_DONE and not questions:
            self.move(tid, ("inbox", "done"), "triage")
        else:
            self.move(tid, ("inbox", "awaiting"), "triage", "The triage Oompa-Loompa needs a human: see Approval / Questions")

    # -- drafting ---------------------------------------------------------

    def set_section(self, tid, key, content):
        meta, body = self.read(tid)
        self.write(tid, meta, note.set_section(body, key, content))

    def ask(self, tid, questions):
        self.expect(tid, ("drafting", "progress"))
        meta, body = self.read(tid)
        self.write(tid, meta, note.new_questions(body, note.render_questions(questions)))
        self.move(tid, ("drafting", "awaiting"), "analyst", "The analyst has questions: tick an answer for each")

    def plan_path(self, tid):
        folder = store.archive_dir if self.ticket(tid).get("archived") else store.plans_dir
        return folder(self.cfg) / f"{tid}-plan.md"

    def write_plan(self, tid, text):
        self.expect(tid, ("drafting", "progress"))
        path = self.plan_path(tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the squirrels' review rounds from an earlier draft, renamed so the builder reads them as context,
        # not as a fix round. Rounds the analyst pasted into the new text are dropped: the old file's copy wins.
        _, log = _split_reviews(path.read_text() if path.exists() else "")
        log = re.sub(r"^## 🐿️ Review round", "## 🐿️ Earlier review round", log, flags=re.M)
        new, _pasted = _split_reviews(text)
        out = new.rstrip("\n") + "\n"
        if log.strip("\n"):
            out += "\n" + log.strip("\n") + "\n"
        path.write_text(out)

    def drafted(self, tid, size, reviewers=()):
        self.expect(tid, ("drafting", "progress"))
        t = self.ticket(tid)
        self.set_field(tid, "size", size)
        meta, body = self.read(tid)
        if size not in machine.BUILDABLE:
            kids = [k for k in t["children"] if k in self.db["tickets"]]
            if not kids:
                raise WonkaError(f"{tid} is {size}: split it first with `wonka add --parent {tid} ...`")
            lines = [f"- [[{k}]] {self.read(k)[0].get('title', '')}" for k in kids]
            self.write(tid, meta, note.set_section(body, "toobig", "\n".join(lines)))
            self.move(tid, KILLED, "analyst", f"Too big ({size}): split into {len(kids)} tickets")
            return
        bad = [r for r in reviewers if r not in REVIEWERS]
        if bad:
            raise WonkaError(f"unknown reviewers {bad}; pick from {', '.join(REVIEWERS)}")
        plan = self.plan_path(tid)
        if not plan.exists():
            raise WonkaError(f"write the detailed plan first: wonka plan {tid} < plan.md")
        spec = note.get_section(body, "spec")
        if not spec:
            raise WonkaError(f"add the Spec first: wonka section {tid} spec < spec.md")
        if f"[[{tid}-plan]]" not in spec:
            body = note.set_section(body, "spec", f"{spec}\n\n📋 Plan: [[{tid}-plan]]")
        self.write(tid, meta, body)
        t["plan"], t["reviewers"] = str(plan), list(reviewers) or ["architect"]
        self.move(tid, ("ready", "pending"), "analyst")

    # -- baking -----------------------------------------------------------

    def ensure_worktree(self, tid):
        t = self.ticket(tid)
        if t["worktree"] and Path(t["worktree"]).exists():
            return
        title = self.read(tid)[0].get("title", "")
        path = store.worktrees_dir() / t["project"] / tid
        branch = t["branch"] or f"wonka/{tid}-{slug(title)}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if _ref_exists(t["repo"], f"refs/heads/{branch}"):
            git("worktree", "add", str(path), branch, cwd=t["repo"])
        else:
            start = t["base_branch"]
            try:
                git("fetch", "--quiet", "origin", t["base_branch"], cwd=t["repo"])
                if _ref_exists(t["repo"], f"refs/remotes/origin/{t['base_branch']}"):
                    start = f"origin/{t['base_branch']}"
            except WonkaError:
                pass  # offline or no origin: bake from the local base branch
            git("worktree", "add", "--no-track", "-b", branch, str(path), start, cwd=t["repo"])
            t["base_ref"] = start
        t["worktree"], t["branch"] = str(path), branch

    def base_ref(self, tid):
        t = self.ticket(tid)
        return t.get("base_ref") or t["base_branch"]

    def drop_worktree(self, tid):
        t = self.ticket(tid)
        if not t.get("worktree"):
            return
        if Path(t["worktree"]).exists():
            try:
                git("worktree", "remove", t["worktree"], cwd=t["repo"])
            except WonkaError as e:
                self.log_event(tid, f"Worktree kept, it has local changes: {t['worktree']} ({e})")
                return
        t["worktree"] = None

    def baked(self, tid):
        self.expect(tid, ("baking", "progress"))
        t = self.ticket(tid)
        if git("status", "--porcelain", cwd=t["worktree"]):
            raise WonkaError("the worktree has uncommitted changes: commit them first")
        if git("rev-list", "--count", f"{self.base_ref(tid)}..HEAD", cwd=t["worktree"]) == "0":
            raise WonkaError(f"no commits on {t['branch']} yet")
        if t["review_round"] > 0 and git("rev-parse", "HEAD", cwd=t["worktree"]) == t.get("reviewed_head"):
            raise WonkaError(f"no new commits on {t['branch']} since review round {t['review_round']}: "
                             "commit the fixes first")
        self.move(tid, ("baking", "done"), "builder")
        self.move(tid, ("reviewing", "pending"), "wonka")

    # -- reviewing --------------------------------------------------------

    def report(self, tid, reviewer, rep):
        self.expect(tid, ("reviewing", "progress"))
        if reviewer not in REVIEWERS:
            raise WonkaError(f"unknown squirrel {reviewer}; one of {', '.join(REVIEWERS)}")
        t = self.ticket(tid)
        t.setdefault("reports", {})[reviewer] = rep
        missing = [r for r in t["reviewers"] if r not in t["reports"]]
        if missing:
            return f"{tid}: waiting for {', '.join(missing)}"
        return self._verdict(tid)

    def _verdict(self, tid):
        t = self.ticket(tid)
        reps, rnd = t.pop("reports"), t["review_round"]
        t.pop("finished", None)
        for rep in reps.values():
            for f in rep.get("followups", []):
                self.add(f["idea"], f.get("title"), t["repo"], t["pr_repo"], parent=tid)
        issues = [(name, i) for name, rep in reps.items() for i in rep.get("issues", [])]
        if t["plan"] and Path(t["plan"]).exists():
            log = [f"\n## 🐿️ Review round {rnd}"]
            for name, rep in reps.items():
                log.append(f"### squirrel-{name}: {rep.get('verdict', '?')}")
                log += [f"- {i}" for i in rep.get("issues", [])]
            with open(t["plan"], "a") as fh:
                fh.write("\n".join(log) + "\n")
        qs = [q for rep in reps.values() for q in rep.get("questions", [])]
        why = {s: [r.get("why", "") for r in reps.values() if r.get("suggest") == s] for s in ("close", "draft")}
        if why["close"]:
            qs.append({"q": note.CLOSE_Q, "options": ["Yes", "No"], "recommended": 0, "why": "; ".join(why["close"]), "other": False})
        if why["draft"]:
            qs.append({"q": note.DRAFT_Q, "options": ["Yes", "No"], "recommended": 0, "why": "; ".join(why["draft"]), "other": False})
        if qs:
            self.set_section(tid, "review-questions", note.render_questions(qs))
            t["round_issues"] = len(issues)
            self.move(tid, ("reviewing", "awaiting"), "squirrels", f"Review round {rnd}/{MAX_REVIEW_ROUNDS}: the squirrels have questions")
            return f"{tid}: review questions for a human"
        if issues:
            return self._back_to_baking(tid, f"Review round {rnd}/{MAX_REVIEW_ROUNDS}: {len(issues)} issue(s), back to baking")
        self.move(tid, ("reviewing", "done"), "squirrels", f"Review round {rnd}/{MAX_REVIEW_ROUNDS}: a good nut")
        self.move(tid, ("shipped", "pending"), "wonka")
        return f"{tid}: clean review, off to ship"

    def _back_to_baking(self, tid, why, kill=True):
        if kill and self.ticket(tid)["review_round"] >= MAX_REVIEW_ROUNDS:
            self.move(tid, KILLED, "wonka", f"Bad nut: {MAX_REVIEW_ROUNDS} review rounds without a clean bill, rolled away")
            return f"{tid}: killed after {MAX_REVIEW_ROUNDS} review rounds"
        self.move(tid, ("baking", "pending"), "wonka", why)
        return f"{tid}: back to baking"

    # -- shipping ---------------------------------------------------------

    def ship(self, tid):
        self.expect(tid, ("shipped", "pending"))
        t = self.ticket(tid)
        self.move(tid, ("shipped", "progress"), "wonka")
        try:
            if not t["pr_repo"]:
                raise WonkaError("no pr_repo: set it with `wonka set ID pr_repo owner/name`")
            git("push", "-u", "origin", t["branch"], cwd=t["worktree"])
            origin = parse_remote(git("remote", "get-url", "origin", cwd=t["worktree"]))
            head = t["branch"] if origin in (None, t["pr_repo"]) else f"{origin.split('/')[0]}:{t['branch']}"
            meta, body = self.read(tid)
            spec = (note.get_section(body, "spec") or note.get_section(body, "idea") or "").replace(f"📋 Plan: [[{tid}-plan]]", "")
            out = gh("pr", "list", "--repo", t["pr_repo"], "--head", t["branch"], "--state", "open",
                     "--json", "url", "-q", ".[0].url") or gh("pr", "create", "--repo", t["pr_repo"], "--base", t["base_branch"], "--head", head,
                     "--title", f"{tid}: {meta.get('title', '')}",
                     "--body", pr_body(tid, meta, spec))
        except WonkaError as e:
            err = " ".join(str(e).split())
            outcome = self.retry(tid, f"could not open the PR: {err}").split(": ", 1)[1]
            return f"{tid}: PR failed: {err}; {outcome}"
        t["pr_url"] = out.splitlines()[-1]
        self.move(tid, ("shipped", "awaiting"), "wonka", f"PR opened, approve and merge it: {t['pr_url']}")
        return f"{tid}: PR {t['pr_url']}"

    # -- the tick ---------------------------------------------------------

    def reconcile(self, tid):
        """Put illegal board edits back and act on ticked checkboxes and merged PRs."""
        t = self.ticket(tid)
        if not self.path(tid).exists():
            src = self.state(tid)
            t["last_valid"], t["archived"], t["lost"] = {"status": "wontdo", "phase": "done"}, True, True
            t["history"].append({"at": _iso(now()), "from": "/".join(src), "to": "wontdo/done", "by": "human"})
            self.drop_worktree(tid)
            return f"{tid}: note was deleted, ticket dropped"
        meta, body = self.read(tid)
        seen = (machine.parse(meta.get("status"), machine.STATUSES), machine.parse(meta.get("phase"), machine.PHASES))
        last = self.state(tid)
        if seen != last:
            if seen[0] == "wontdo" and last != KILLED:
                self.move(tid, KILLED, "human", "Dragged to Wont-Do by a human")
                return f"{tid}: killed by a human"
            shown = f"{meta.get('status')}/{meta.get('phase')}"
            meta["status"], meta["phase"] = machine.label(last)
            why = f"Reverted {shown}: not a legal move, back to {fmt(last)}"
            t["history"].append({"at": _iso(now()), "by": "wonka", "why": why})
            store.log(f"{tid} wonka: {why}")
            self.write(tid, meta, body)
            return f"{tid}: reverted illegal {shown}"
        if t.get("stuck") and note.approved_retry(note.get_section(body, "stuck")):
            t.pop("stuck"), t.pop("retries", None)
            self.write(tid, meta, note.drop_section(body, "stuck"))
            return f"{tid}: unstuck by a human"
        if last == ("inbox", "awaiting"):
            appr, qs = note.get_section(body, "approval"), note.get_section(body, "questions")
            if (appr is None or note.approved(appr)) and (qs is None or note.all_answered(qs)):
                self.move(tid, ("inbox", "done"), "human", "Approved")
                return f"{tid}: approved"
        elif last == ("drafting", "awaiting") and note.all_answered(note.get_section(body, "questions")):
            self.move(tid, ("drafting", "progress"), "human", "Questions answered, the analyst resumes")
            t["resume"] = True
            return f"{tid}: answered, drafting resumes"
        elif last == ("reviewing", "awaiting"):
            sec = note.get_section(body, "review-questions")
            if note.all_answered(sec):
                issues = t.pop("round_issues", 1)  # tickets that were already waiting keep the old path
                if note.picked_yes(sec, note.CLOSE_Q):
                    self.move(tid, KILLED, "human", "Closed after review")
                    return f"{tid}: closed after review"
                if note.picked_yes(sec, note.DRAFT_Q):
                    t["review_round"] = 0
                    t.pop("reviewed_head", None)
                    self.move(tid, ("drafting", "pending"), "human", "Back to drafting after review")
                    return f"{tid}: back to drafting"
                if not issues:
                    if note.all_recommended(sec, skip=(note.CLOSE_Q, note.DRAFT_Q)):
                        self.move(tid, ("reviewing", "done"), "human", "Review answers in, all as recommended: a good nut")
                        self.move(tid, ("shipped", "pending"), "wonka")
                        return f"{tid}: clean review answered, off to ship"
                    return self._back_to_baking(
                        tid, f"Review answers in, a clean round but the answers change the plan, back to baking "
                             f"(round {t['review_round']}/{MAX_REVIEW_ROUNDS})", kill=False)
                return self._back_to_baking(tid, f"Review answers in, back to baking (round {t['review_round']}/{MAX_REVIEW_ROUNDS})")
        elif last == ("shipped", "awaiting") and t["pr_url"]:
            pr = gh("pr", "view", t["pr_url"], "--json", "state", "-q", ".state")
            if pr == "MERGED":
                self.move(tid, ("shipped", "done"), "github", "PR merged 🎉")
                return f"{tid}: shipped"
            if pr == "CLOSED":
                self.move(tid, KILLED, "github", "PR closed without merging")
                return f"{tid}: PR closed"
        return None

    def archive(self, tid):
        t = self.ticket(tid)
        stamp = t.get("entered_done_at")
        if t.get("archived") or self.state(tid) not in (("shipped", "done"), KILLED) or not stamp:
            return None
        if _age(stamp) < dt.timedelta(days=self.cfg["archive_after_days"]):
            return None
        src, plan = self.path(tid), self.plan_path(tid)
        store.archive_dir(self.cfg).mkdir(parents=True, exist_ok=True)
        t["archived"] = True
        shutil.move(src, self.path(tid))
        if plan.exists():
            shutil.move(plan, self.plan_path(tid))
            t["plan"] = str(self.plan_path(tid))
        return f"{tid}: archived"

    def start(self, tid):
        """Claim a job for an agent and return what it needs to know."""
        t, s = self.ticket(tid), self.state(tid)
        job = {"inbox": "triage", "drafting": "draft", "baking": "bake", "reviewing": "review"}[s[0]]
        if s == ("drafting", "pending"):
            self.move(tid, ("drafting", "progress"), "wonka")
        elif s == ("baking", "pending"):
            self.ensure_worktree(tid)
            self.move(tid, ("baking", "progress"), "wonka")
        elif s == ("reviewing", "pending"):
            self.ensure_worktree(tid)
            t["reviewed_head"] = git("rev-parse", "HEAD", cwd=t["worktree"])
            t["review_round"] += 1
            t["reports"] = {}
            t["finished"] = []
            self.move(tid, ("reviewing", "progress"), "wonka")
        t.pop("resume", None)
        t["job_started_at"] = _iso(now())
        agents = {"triage": ["oompa-loompa-triage"], "draft": ["oompa-loompa-analyst"], "bake": ["oompa-loompa-builder"],
                  "review": [f"squirrel-{r}" for r in t["reviewers"] or ["architect"]]}[job]
        agents = [f"wonka:{a}" for a in agents]
        return {"id": tid, "job": job, "agents": agents, "note": str(self.path(tid)), "plan": str(self.plan_path(tid)),
                "repo": t["repo"], "worktree": t["worktree"], "branch": t["branch"], "base_ref": self.base_ref(tid),
                "review_round": t["review_round"]}

    def retry(self, tid, why="the last job did not finish"):
        t, s = self.ticket(tid), self.state(tid)
        retries = t.get("retries", 0) + 1
        why = " ".join(str(why).split())
        why = why if len(why) <= 200 else why[:199] + "…"
        if s[1] == "progress":
            if s[0] == "reviewing":
                t["review_round"] -= 1
                t.pop("reports", None)
                t.pop("finished", None)
            self.move(tid, (s[0], "pending"), "wonka", f"Retry {retries}/{MAX_RETRIES}: {why}")
        t.pop("job_started_at", None)
        t["retries"] = retries
        if retries >= MAX_RETRIES:
            t["stuck"] = True
            self.set_section(tid, "stuck", f"- [ ] Retry ({retries} tries failed, the last one: {why}. See ~/.wonka/factory.log)")
            notify(tid, "stuck: tick Retry when it's fixed")
            return f"{tid}: stuck after {retries} tries"
        return f"{tid}: back in the queue"

    def finished(self, tid, agent):
        """An agent finished: retry its job only if it left no result, else do nothing. Says which."""
        name = agent.removeprefix("wonka:")
        squirrel = name.removeprefix("squirrel-") if name.startswith("squirrel-") else None
        if squirrel is not None:
            if squirrel not in REVIEWERS:
                raise WonkaError(f"unknown squirrel {squirrel}; one of {', '.join(REVIEWERS)}")
            expected = ("reviewing", "progress")
        elif name in JOB_AGENTS:
            expected = JOB_AGENTS[name]
        else:
            known = [f"wonka:{a}" for a in JOB_AGENTS] + [f"wonka:squirrel-{r}" for r in REVIEWERS]
            raise WonkaError(f"unknown agent {agent}; one of {', '.join(known)}")
        t, s = self.ticket(tid), self.state(tid)
        if t.get("stuck"):
            return f"{tid}: stuck, waiting for a human to tick Retry"
        if s != expected or not t.get("job_started_at"):
            return f"{tid}: {agent} done, the ticket moved on ({fmt(s)})"
        if squirrel is None:
            return f"{tid}: {agent} left no result, " + self.retry(tid).split(": ", 1)[1]
        reviewers = t["reviewers"] or ["architect"]
        if squirrel not in reviewers:
            raise WonkaError(f"{tid}: squirrel-{squirrel} is not reviewing this ticket")
        done = t.setdefault("finished", [])
        if squirrel not in done:
            done.append(squirrel)
        reports = t.get("reports", {})
        waiting = [r for r in reviewers if r not in reports and r not in done]
        missing = [r for r in reviewers if r not in reports and r in done]
        if waiting:
            if squirrel in reports:
                return f"{tid}: squirrel-{squirrel} reported, waiting for {', '.join(waiting)}"
            return f"{tid}: squirrel-{squirrel} left no report, retrying once {', '.join(waiting)} finish"
        if missing:
            return f"{tid}: no report from {', '.join(missing)}, " + self.retry(tid).split(": ", 1)[1]
        return f"{tid}: squirrel-{squirrel} done, every squirrel reported"

    def _count(self, status):
        return sum(1 for k in self.live() if self.state(k) in ((status, "pending"), (status, "progress")))

    def live(self):
        return sorted(k for k, t in self.db["tickets"].items() if not t.get("archived"))

    def _safe(self, tid, fn):
        try:
            return fn(tid)
        except (WonkaError, OSError) as e:
            return f"{tid}: ⚠️ {e}"

    def tick(self):
        events = [m for k in self.live() for m in [self._safe(k, self.reconcile), self._safe(k, self.archive)] if m]
        folder = store.tickets_dir(self.cfg)
        events += [f"{p.name}: not a Wonka ticket, use /wonka add instead" for p in sorted(folder.glob("*.md"))
                   if p.stem not in self.db["tickets"]] if folder.exists() else []
        for src, dst, limit in ((("inbox", "done"), ("drafting", "pending"), "drafting"),
                                (("ready", "pending"), ("baking", "pending"), "baking")):
            for k in self.live():
                if self.state(k) == src and self._count(dst[0]) < self.cfg["wip"][limit]:
                    events.append(self._safe(k, lambda k: self.move(k, dst, "wonka") or f"{k}: → {fmt(dst)}"))
        jobs = []
        for k in self.live():
            t, s = self.ticket(k), self.state(k)
            if t.get("stuck") or (t.get("job_started_at") and not t.get("resume")):
                continue
            if s == ("shipped", "pending"):
                events.append(self._safe(k, self.ship))
            elif s in (("inbox", "pending"), ("drafting", "pending"), ("baking", "pending"), ("reviewing", "pending")) \
                    or (s == ("drafting", "progress") and t.get("resume")):
                job = self._safe(k, self.start)
                (events if isinstance(job, str) else jobs).append(job)
        stale_after = dt.timedelta(hours=self.cfg["stale_hours"])
        stale = [k for k in self.live() if self.ticket(k).get("job_started_at")
                 and _age(self.ticket(k)["job_started_at"]) > stale_after]
        awaiting = [k for k in self.live() if self.state(k)[1] == "awaiting"]
        return {"events": events, "jobs": jobs, "awaiting_human": awaiting, "stale": stale}
