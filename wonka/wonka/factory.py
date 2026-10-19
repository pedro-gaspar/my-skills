"""What the factory does to golden tickets. Every status change goes through Factory.move()."""

import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from . import banner, machine, mdlint, note, store
from .machine import KILLED, review_budget
from .store import CriticalError, WonkaError

REVIEWERS = ("architect", "backend", "frontend", "security", "spark", "airflow")
# free-text sections, checked with markdownlint; Wonka-rendered sections (approval, questions, stuck, log, ...) never are
LINTED = ("idea", "spec")
MAX_RETRIES = 3
MAX_CONFLICT_FIXES = 3  # fixes in a row that GitHub still calls CONFLICTING
# the state a ticket is left in while each agent's job is unfinished
JOB_AGENTS = {"oompa-loompa-triage": ("inbox", "pending"), "oompa-loompa-analyst": ("drafting", "progress"),
              "oompa-loompa-builder": ("baking", "progress"), "oompa-loompa-fixer": ("shipped", "progress")}
# the in-progress states whose job runs inside the ticket's worktree, and the agent that runs it
# (reviewing/progress is handled apart: it has several squirrels)
WORKTREE_JOBS = {("baking", "progress"): "oompa-loompa-builder", ("shipped", "progress"): "oompa-loompa-fixer"}
REVIEW_LOG = re.compile(r"^## (?:🐿️ (?:Earlier review|Review) round|🔁 (?:Earlier change|Change) request) \d+", re.M)
# PR comments by anyone else are not copied into the plan: the builder runs unattended and would read them as instructions
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")


def _split_reviews(text):
    """Split a plan into (plan, review log): the log starts at the first review-round heading."""
    m = REVIEW_LOG.search(text)
    return (text, "") if not m else (text[:m.start()], text[m.start():])


def _age_log(text):
    """Mark the review rounds and change requests already in a plan's log as done, so only new ones are fixed."""
    text = re.sub(r"^## 🐿️ Review round", "## 🐿️ Earlier review round", text, flags=re.M)
    return re.sub(r"^## 🔁 Change request", "## 🔁 Earlier change request", text, flags=re.M)


def now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds")


def _age(stamp):
    return now() - dt.datetime.fromisoformat(stamp)


TIMEOUT = 120  # seconds any git or gh call may take; most run inside the state lock
# git, gh and Git Credential Manager must never ask a human anything
NONINTERACTIVE = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GCM_INTERACTIVE": "never"}


def _exec(cmd, cwd=None):
    """Run a command with no stdin, no terminal and a time limit. Returns (returncode, stdout, stderr)."""
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **NONINTERACTIVE}, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except FileNotFoundError as e:
        if cwd is not None and e.filename == str(cwd):
            raise WonkaError(f"working directory {cwd} is missing")
        raise WonkaError(f"{cmd[0]} is not installed")
    try:
        out, err = p.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        p.stdout.close()
        p.stderr.close()
        raise WonkaError(f"{' '.join(cmd[:3])} timed out after {TIMEOUT} s: is it waiting for a password, "
                         "passphrase or host key, or is the network down?")
    return p.returncode, out, err


def _run(cmd, cwd=None):
    code, out, err = _exec(cmd, cwd)
    if code:
        raise WonkaError(f"{' '.join(cmd[:3])} failed: {(err or out).strip()}")
    return out.strip()


def git(*args, cwd):
    return _run(["git", *args], cwd=cwd)


def gh(*args, cwd=None):
    return _run([os.environ.get("WONKA_GH", "gh"), *args], cwd=cwd)


def gh_json(*args, default, cwd=None):
    """gh output parsed as JSON of the same type as `default` (used when gh prints nothing)."""
    out = gh(*args, cwd=cwd)
    what = " ".join(args[:2])
    try:
        data = json.loads(out) if out else default
    except ValueError as e:
        raise WonkaError(f"gh {what} returned invalid JSON: {e}") from e
    if not isinstance(data, type(default)):
        raise WonkaError(f"gh {what} returned {type(data).__name__}, expected {type(default).__name__}")
    return data


def _parse_stamp(stamp, what):
    try:
        return dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError) as e:
        raise WonkaError(f"unreadable {what} timestamp {stamp!r}") from e


def _json_pages(text):
    """Every item from `gh api --paginate` output, which is one JSON array or several concatenated ones."""
    dec, items, i = json.JSONDecoder(), [], 0
    text = text or ""
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return items
        page, i = dec.raw_decode(text, i)
        if not isinstance(page, list):
            raise ValueError(f"page is {type(page).__name__}, expected list")
        items.extend(page)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].rstrip("-") or "ticket"


def parse_remote(url):
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else None


def repo_info(path):
    top = git("rev-parse", "--show-toplevel", cwd=path)
    try:
        origin_url = git("remote", "get-url", "origin", cwd=top)
    except WonkaError:
        origin_url = None
    return top, _base_branch(top, has_origin=origin_url is not None), parse_remote(origin_url)


def _base_branch(top, has_origin):
    """The branch tickets in this repo branch from and open PRs against. Never the checked-out branch."""
    if has_origin:
        try:
            return git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=top).split("/", 1)[1]
        except WonkaError:
            pass
        for name in ("main", "master"):
            if _ref_exists(top, f"refs/remotes/origin/{name}"):
                return name
        raise WonkaError(f"can't tell the base branch of {top}: origin/HEAD is not set and there is no "
                         "origin/main or origin/master. Run `git fetch origin && git remote set-head origin --auto` "
                         "(or `git remote set-head origin <branch>`) and try again")
    for name in ("main", "master"):
        if _ref_exists(top, f"refs/heads/{name}"):
            return name
    raise WonkaError(f"can't tell the base branch of {top}: it has no origin remote and no main or master branch")


def _ref_exists(repo, ref):
    return _exec(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo)[0] == 0


def _drop_stash_entry(repo, sha):
    """Take the stash entry `sha` out of the shared stash list (refs/stash), wherever it now sits."""
    try:
        entries = git("log", "-g", "--format=%H", "refs/stash", cwd=repo).splitlines()
    except WonkaError:
        return
    if sha in entries:
        git("stash", "drop", "--quiet", f"stash@{{{entries.index(sha)}}}", cwd=repo)


def _worktree_branch(repo, path):
    """The branch git has checked out in the registered worktree at path, or None when it is not one."""
    want, cur = str(Path(path).resolve()), None
    for line in git("worktree", "list", "--porcelain", cwd=repo).splitlines():
        if line.startswith("worktree "):
            cur = str(Path(line[len("worktree "):]).resolve())
        elif line.startswith("branch refs/heads/") and cur == want:
            return line[len("branch refs/heads/"):]
    return None


def _rebasing(wt):
    """True while a rebase is unfinished in the worktree (it stopped on a conflict)."""
    return any(Path(git("rev-parse", "--path-format=absolute", "--git-path", d, cwd=wt)).exists()
               for d in ("rebase-merge", "rebase-apply"))


# git says "(fetch first)" when the remote has objects we never fetched and "(non-fast-forward)" when we have them:
# both mean the remote branch has commits the local one lacks. This matches git's English output; under another
# locale it misses and the push failure takes the normal retry path.
_REJECTED = re.compile(r"\[rejected\].*\((?:fetch first|non-fast-forward)\)")


def _push_rejected(err):
    """True when git push failed because the remote branch has commits the local one lacks."""
    return bool(_REJECTED.search(str(err)))


_REASON_MAX = 200  # retry() cuts longer reasons, which would lose the closing "then tick Retry"


def _push_rejected_reason(branch):
    """What to do after a rejected push. Names the branch when it fits in _REASON_MAX, else uses the
    current-branch form of the same command (the worktree is on that branch)."""
    head = "push rejected: the PR branch has commits on GitHub that the worktree lacks. In the worktree run "
    why = f"{head}git pull --rebase origin {branch}, then tick Retry"
    if len(why) <= _REASON_MAX:
        return why
    return f"{head}git pull --rebase origin $(git branch --show-current), then tick Retry"


class _PushRejected(WonkaError):
    """git push was rejected as not a fast-forward: retrying cannot help, a human must rebase."""


class _Diverged(WonkaError):
    """The local PR branch has commits origin lacks and vice versa: retrying cannot help, a human must rebase."""


class _Merged(WonkaError):
    """The ticket's PR was merged while a fix round ran: a new PR would repeat its commits, a human must decide."""


class _NotOurs(WonkaError):
    """An open PR on the ticket's branch was not opened by Wonka for it: retrying cannot help, a human must look."""


def check_branch(t):
    """Wonka only commits and pushes the ticket's own wonka/ branch: never main, master or the PR's base branch."""
    branch = t.get("branch") or ""
    if not branch.startswith("wonka/") or branch in (t.get("base_branch"), t.get("pr_base")):
        raise WonkaError(f"refusing to write to branch {branch or '(none)'}: Wonka only commits and pushes its own "
                         "wonka/ branches, never main, master or the base branch")


def _pr_mark(tid):
    """The line every PR Wonka opens starts its description with."""
    return f"🎫 **Golden ticket:** {tid}"


def pr_body(tid, meta, spec):
    ids = [_pr_mark(tid)]
    if meta.get("jira"):
        ids.append(f"🎟️ **Jira:** {meta['jira']}")
    return "  \n".join(ids) + f"\n\n{spec.strip()}\n\n🤖 Baked by the Wonka factory"


def fmt(state):
    return " · ".join(machine.label(state)) if machine.valid(state) else "/".join(map(str, state))


# Wait for the click in the background; open the URL only when the default action (a click) was chosen.
_OPEN_ON_CLICK = 'u=$1; o=$2; shift 2; [ "$("$@")" = default ] && exec "$o" "$u"'
NOTIFIERS = ("notify-send", "terminal-notifier", "osascript")  # Linux; macOS with a click action; macOS built in
# macOS's built-in notifier: the text goes in as arguments, never into the AppleScript source
_OSASCRIPT = ["osascript", "-e", "on run argv", "-e",
              "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]
_QUIET = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def notifier():
    """The first desktop notifier found on this machine, or None."""
    return next((n for n in NOTIFIERS if shutil.which(n)), None)


def _ping(title, body, url=None, urgent=False):
    """Ping the desktop with whatever notifier this machine has. With a url, clicking opens it (notify-send waits
    detached, never blocks; osascript cannot open anything, so there the click does nothing)."""
    if os.environ.get("WONKA_NO_NOTIFY"):
        return
    tool = notifier()
    try:
        if tool == "notify-send":
            cmd = ["notify-send", "-a", "Wonka", *(["-u", "critical"] if urgent else []), title, body]
            if not url:
                subprocess.run(cmd, check=False)
                return
            cmd[-1] += "\nClick to open the PR"
            cmd += ["-A", "default=Open PR"]
            opener = os.environ.get("WONKA_OPEN", "xdg-open")
            subprocess.Popen(["sh", "-c", _OPEN_ON_CLICK, "wonka-notify", url, opener, *cmd], **_QUIET,
                             start_new_session=True)
        elif tool == "terminal-notifier":
            cmd = ["terminal-notifier", "-title", "Wonka", "-subtitle", title,
                   "-message", body + ("\nClick to open the PR" if url else "")]
            cmd += (["-open", url] if url else []) + (["-sound", "default"] if urgent else [])
            subprocess.run(cmd, check=False, **_QUIET)
        elif tool == "osascript":
            subprocess.run([*_OSASCRIPT, title, body], check=False, **_QUIET)
    except OSError:
        pass


def notify(tid, title, url=None):
    """Ping the desktop when a ticket needs a human. With a url, clicking the notification opens it."""
    _ping("🎩 Wonka needs you", f"{tid} — {title}", url)


def alert(message):
    """Ping the desktop, urgently, about a problem that stops the whole factory rather than one ticket."""
    _ping("🎩 Wonka is stopped", message, urgent=True)


def _default_title(idea):
    return idea.strip().splitlines()[0][:60]


class Factory:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self._base_cache = None  # {(repo, base): ref} during tick(); None outside a tick: always fetch

    # -- tickets and notes ------------------------------------------------

    def ticket(self, tid):
        try:
            return self.db["tickets"][tid]
        except KeyError:
            raise WonkaError(f"No ticket {tid}")

    def _max_rounds(self):
        return self.cfg["max_review_rounds"]

    def _budget(self, t):
        return review_budget(t, self._max_rounds())

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
        final = dst in (("shipped", "ready"), KILLED)  # merged or killed: the ticket is finished
        t["last_valid"] = {"status": dst[0], "phase": dst[1]}
        t["history"].append({"at": _iso(now()), "from": "/".join(src), "to": "/".join(dst), "by": by,
                             **({"why": why} if why else {})})
        if dst == KILLED:
            self._hold_for_live_job(tid, src)
        t.pop("job_started_at", None)
        if dst != ("shipped", "progress"):
            # a conflict fix only lives in shipped/progress; anywhere else (back to awaiting, a retry through
            # ship(), killed, merged) starts clean and the next tick re-detects conflicts
            t.pop("conflicts", None)
            t.pop("merge_ref", None)
            t.pop("lease", None)
        t.pop("conflict_error", None)  # a fixer that started, or any other move, ends the failed-start episode
        if dst[0] != "shipped" or dst[1] == "ready":
            # the count of conflict fixes lives across one open PR's shipped states; a change request, a kill or a
            # merge starts clean (kept through pending/progress so a failed fixer's retry does not reset it)
            t.pop("conflict_fixes", None)
        if dst[1] == "ready":
            t.pop("pr_base", None)
        if final:
            t["entered_done_at"] = _iso(now())
        meta, body = self.read(tid)
        meta["status"], meta["phase"] = machine.label(dst)
        if final:
            body = note.drop_section(body, "changes")  # a merged or closed PR takes no more change requests
        if (by != "wonka" or final) and t.pop("stuck", None):
            body = note.drop_section(body, "stuck")  # finished, or moved by a human: nothing left to retry
        self.write(tid, meta, body)
        store.log(f"{tid} {'/'.join(src)} → {'/'.join(dst)} by {by}" + (f": {why}" if why else ""))
        if by != "wonka" or final:
            t.pop("retries", None)
        if final:
            t.pop("cleanup", None)  # the flag reflects only this attempt
            self.drop_worktree(tid)
            if dst == ("shipped", "ready"):
                self.drop_branch(tid)
        if t.get("scout") and dst not in (("inbox", "pending"), ("drafting", "pending"), ("drafting", "progress")):
            self.drop_scout(tid)  # no triage or draft job left to read it
        if dst[1] == "awaiting":
            notify(tid, meta.get("title", ""), t.get("pr_url") if dst == ("shipped", "awaiting") else None)

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
            if jira is None and self.path(parent).exists():  # a deleted parent note just means no key to inherit
                jira = self.read(parent)[0].get("jira") or None
        tid = f"WONKA-{self.db['next_id']:05d}"
        self.db["next_id"] += 1
        title = title or _default_title(idea)
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
        self.write(tid, meta, note.set_section("", "idea", mdlint.guard(idea)))
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

    def triage(self, tid, size, title=None, questions=None, duplicate_of=None, dispatch=None):
        if self._stale_dispatch(tid, dispatch):
            return self._stale_msg(tid, "triage result", dispatch)
        self.expect(tid, ("inbox", "pending"))
        if size not in machine.SIZES:
            raise WonkaError(f"size must be one of {', '.join(machine.SIZES)}")
        if duplicate_of:
            if questions:
                raise WonkaError("triage takes --duplicate-of or --question, not both")
            if duplicate_of == tid:
                raise WonkaError(f"{tid} cannot be a duplicate of itself")
            other = self.ticket(duplicate_of)
            if other["repo"] != self.ticket(tid)["repo"]:
                raise WonkaError(f"{duplicate_of} builds in another repo, not a duplicate")
            if self.state(duplicate_of) == KILLED:
                raise WonkaError(f"{duplicate_of} is Wont-Do: triage {tid} on its own")
        meta, body = self.read(tid)
        meta["size"] = size
        if title:
            meta["title"] = title
        if duplicate_of:
            try:
                other_title = self.read(duplicate_of)[0].get("title", "")
            except OSError:
                other_title = ""
            body = note.set_section(body, "duplicate", f"- Duplicate of [[{duplicate_of}]] {other_title}".rstrip())
            self.write(tid, meta, body)
            self.move(tid, KILLED, "triage", f"Duplicate of {duplicate_of}")
            self.log_event(duplicate_of, f"{tid} was closed as a duplicate of this ticket", by="triage")
            return
        if size not in machine.AUTO_READY:
            body = note.set_section(body, "approval", f"- [ ] Approve (size **{size}**: tick to send it to Drafting)")
        if questions:
            body = note.new_questions(body, note.render_questions(questions))
        self.write(tid, meta, body)
        if size in machine.AUTO_READY and not questions:
            self.move(tid, ("inbox", "ready"), "triage")
        else:
            self.move(tid, ("inbox", "awaiting"), "triage", "The triage Oompa-Loompa needs a human: see Approval / Questions")

    # -- drafting ---------------------------------------------------------

    def _stale_msg(self, tid, what, dispatch):
        return (f"{tid}: {what} is from an earlier dispatch "
                f"({dispatch}, now {self.ticket(tid)['dispatch']}), ignored")

    def set_section(self, tid, key, content, dispatch=None):
        if self._stale_dispatch(tid, dispatch):
            return self._stale_msg(tid, f"analyst {key} section", dispatch)
        if key in LINTED:
            content = mdlint.guard(content)
        meta, body = self.read(tid)
        self.write(tid, meta, note.set_section(body, key, content))

    def ask(self, tid, questions, dispatch=None):
        if self._stale_dispatch(tid, dispatch):
            return self._stale_msg(tid, "analyst questions", dispatch)
        self.expect(tid, ("drafting", "progress"))
        meta, body = self.read(tid)
        self.write(tid, meta, note.new_questions(body, note.render_questions(questions)))
        self.move(tid, ("drafting", "awaiting"), "analyst", "The analyst has questions: tick an answer for each")

    def plan_path(self, tid):
        folder = store.archive_dir if self.ticket(tid).get("archived") else store.plans_dir
        return folder(self.cfg) / f"{tid}-plan.md"

    def write_plan(self, tid, text, dispatch=None):
        if self._stale_dispatch(tid, dispatch):
            return self._stale_msg(tid, "analyst plan", dispatch)
        self.expect(tid, ("drafting", "progress"))
        path = self.plan_path(tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the squirrels' review rounds from an earlier draft, renamed so the builder reads them as context,
        # not as a fix round. Rounds the analyst pasted into the new text are dropped: the old file's copy wins.
        _, log = _split_reviews(path.read_text() if path.exists() else "")
        log = _age_log(log)
        new, _pasted = _split_reviews(text)
        out = new.rstrip("\n") + "\n"
        if log.strip("\n"):
            out += "\n" + log.strip("\n") + "\n"
        path.write_text(out)

    def repos(self):
        """The distinct repos of all known tickets: sorted (project, repo, pr_repo) tuples."""
        found = {}
        for t in self.db["tickets"].values():
            if t.get("repo"):
                found[t["repo"]] = (t.get("project", ""), t["repo"], t.get("pr_repo"))
        return sorted(found.values(), key=lambda r: (r[0], r[1]))

    def drafted(self, tid, size, reviewers=(), dispatch=None, split=False):
        if self._stale_dispatch(tid, dispatch):
            return (f"{tid}: analyst result is from an earlier dispatch "
                    f"({dispatch}, now {self.ticket(tid)['dispatch']}), ignored")
        self.expect(tid, ("drafting", "progress"))
        t = self.ticket(tid)
        if split:
            kids = self._live_children(t)
            if not kids:
                raise WonkaError(f"{tid}: add one ticket per repo first with "
                                 f"`wonka add --repo <path> --parent {tid} ...`")
            if reviewers:
                raise WonkaError("--split takes no --reviewers: each child is reviewed on its own")
            if all(self.ticket(k)["repo"] == t["repo"] for k in kids):
                raise WonkaError(f"{tid}: every child builds in {t['repo']}; --split is for work in other repos "
                                 f"(split a ticket that is too big for one repo with --size l)")
            self.set_field(tid, "size", size)
            repos = {self.ticket(k)["repo"] for k in kids}
            self._close_with_children(
                tid, kids, "split",
                lambda k: f"{self.read(k)[0].get('title', '')} · {self.ticket(k)['project']}",
                f"Split by repo: {len(kids)} tickets in {len(repos)} repos")
            return
        self.set_field(tid, "size", size)
        meta, body = self.read(tid)
        if size not in machine.BUILDABLE:
            kids = self._live_children(t)
            if not kids:
                raise WonkaError(f"{tid} is {size}: split it first with `wonka add --parent {tid} ...`")
            self._close_with_children(tid, kids, "toobig", lambda k: self.read(k)[0].get("title", ""),
                                      f"Too big ({size}): split into {len(kids)} tickets")
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
        self.move(tid, ("planned", "pending"), "analyst")

    # -- baking -----------------------------------------------------------

    def ensure_worktree(self, tid):
        t = self.ticket(tid)
        if t["worktree"] and Path(t["worktree"]).is_dir():
            return
        title = self.read(tid)[0].get("title", "")
        path = store.worktrees_dir() / t["project"] / tid
        branch = t["branch"] or f"wonka/{tid}-{slug(title)}"
        path.parent.mkdir(parents=True, exist_ok=True)
        recreated = bool(t["worktree"])  # the folder was deleted from outside
        if path.is_dir() and _worktree_branch(t["repo"], path) == branch:
            # made by an earlier call whose state was never saved (a crash): take it as is
            t["worktree"], t["branch"] = str(path), branch
            return
        if not path.is_dir():
            git("worktree", "prune", cwd=t["repo"])  # forget a registration whose folder was deleted
        if _ref_exists(t["repo"], f"refs/heads/{branch}"):
            git("worktree", "add", str(path), branch, cwd=t["repo"])
        else:
            start = self._latest_base(t)
            git("worktree", "add", "--no-track", "-b", branch, str(path), start, cwd=t["repo"])
            t["base_ref"] = start
        if recreated:
            self.log_event(tid, f"Worktree folder was gone, recreated it from {branch}: {path}")
        t["worktree"], t["branch"] = str(path), branch

    def _latest_base(self, t):
        """Fetch origin's base branch and return the freshest ref to start from: origin/<base>, else the local base.

        Fetched at most once per (repo, base) within a tick."""
        key = (t["repo"], t["base_branch"])
        cache = self._base_cache
        if cache is not None and key in cache:
            return cache[key]
        ref = t["base_branch"]
        try:
            git("fetch", "--quiet", "origin", t["base_branch"], cwd=t["repo"])
            if _ref_exists(t["repo"], f"refs/remotes/origin/{t['base_branch']}"):
                ref = f"origin/{t['base_branch']}"
        except WonkaError:
            pass  # offline or no origin: use the local base branch
        if cache is not None:
            cache[key] = ref
        return ref

    # -- scout: a read-only checkout of the latest base for triage and drafting --

    def scout_path(self, tid):
        t = self.ticket(tid)
        return store.worktrees_dir() / t["project"] / f"{tid}-scout"  # a sibling of the builder's worktree

    def _remove_scout_dir(self, repo, path):
        """Throw a scout checkout away. --force is safe: it is Wonka's own read-only tree, never user work."""
        try:
            git("worktree", "remove", "--force", str(path), cwd=repo)
        except WonkaError:
            shutil.rmtree(path, ignore_errors=True)
            git("worktree", "prune", cwd=repo)

    def fresh_scout(self, tid):
        """(Re)create the scout at the latest base, so every job starts from a clean tree. Returns (path, ref)."""
        t = self.ticket(tid)
        ref = self._latest_base(t)
        path = self.scout_path(tid)
        if path.exists():
            self._remove_scout_dir(t["repo"], path)
        else:
            git("worktree", "prune", cwd=t["repo"])  # the dir was deleted from outside: forget its stale registration
        path.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", "--quiet", str(path), ref, cwd=t["repo"])
        t["scout"], t["scout_ref"] = str(path), ref
        return str(path), ref

    def drop_scout(self, tid):
        """Best effort, never raises: the ticket no longer has a triage or draft job."""
        t = self.ticket(tid)
        if not t.get("scout"):
            return
        path = Path(t["scout"])
        if path.exists():
            try:
                self._remove_scout_dir(t["repo"], path)
            except (WonkaError, OSError) as e:
                store.log(f"{tid} wonka: could not remove the scout {path} ({e})")
        else:
            try:
                git("worktree", "prune", cwd=t["repo"])  # the dir is gone: do not leave its registration behind
            except (WonkaError, OSError) as e:
                store.log(f"{tid} wonka: could not prune the scout registration {path} ({e})")
        t.pop("scout", None)
        t.pop("scout_ref", None)

    def base_ref(self, tid):
        t = self.ticket(tid)
        return t.get("base_ref") or t["base_branch"]

    def drop_worktree(self, tid, quiet=False):
        """Remove the ticket's worktree; never forced. Returns whether it is gone. A kept worktree (it has local
        changes) flags the ticket for cleanup, which tick() retries."""
        t = self.ticket(tid)
        if not t.get("worktree"):
            return True
        agents = self._held(tid)
        if agents and Path(t["worktree"]).is_dir():
            t["cleanup"] = True
            if not quiet:
                self.log_event(tid, f"Worktree kept until the running {', '.join(agents)} ends: {t['worktree']}. "
                                    "Wonka retries each tick")
            return False
        if Path(t["worktree"]).is_dir():
            try:
                git("worktree", "remove", t["worktree"], cwd=t["repo"])
            except WonkaError as e:
                t["cleanup"] = True
                if not quiet:
                    self.log_event(tid, f"Worktree kept, it has local changes: {t['worktree']} ({e}). "
                                        "Wonka retries each tick until it is clean or gone")
                return False
        else:
            try:
                git("worktree", "prune", cwd=t["repo"])  # deleted by hand: drop its registration so the branch can go
            except (WonkaError, OSError) as e:
                store.log(f"{tid} wonka: could not prune the worktree registration {t['worktree']} ({e})")
        t["worktree"] = None
        return True

    def _hold_for_live_job(self, tid, src):
        """Just before a ticket is killed: if an agent is still at work in its worktree, hold the worktree for it
        until it ends (finished()) or its job goes stale."""
        t = self.ticket(tid)
        if not t.get("job_started_at") or not t.get("worktree"):
            return
        if src in WORKTREE_JOBS:
            agents = [WORKTREE_JOBS[src]]
        elif src == ("reviewing", "progress") and "reports" in t:
            # no reports key means _verdict() is running: every squirrel already reported
            agents = [f"squirrel-{r}" for r in t["reviewers"] or ["architect"]
                      if r not in t["reports"] and r not in t.get("finished", [])]
        else:
            agents = []
        if agents:
            t["held_by"] = {"dispatch": t.get("dispatch"), "since": t["job_started_at"], "agents": agents}

    def _held(self, tid):
        """The agents still holding a killed ticket's worktree, or None. A hold expires after stale_hours."""
        t = self.ticket(tid)
        held = t.get("held_by")
        if not held:
            return None
        hours = self.cfg["stale_hours"]
        try:
            expired = _age(held["since"]) > dt.timedelta(hours=hours)
        except (ValueError, TypeError, KeyError):
            expired = True
        if expired or not held.get("agents"):
            t.pop("held_by", None)
            if expired:
                store.log(f"{tid} wonka: no word from {', '.join(held.get('agents') or [])} for {hours}h, "
                          "releasing its worktree")
            return None
        return held["agents"]

    def drop_branch(self, tid, quiet=False):
        """After a merged PR: delete the local branch. Best effort, only once the worktree is gone.
        Returns whether the branch is gone; a kept one flags the ticket for cleanup."""
        t = self.ticket(tid)
        if not t.get("branch"):
            return True
        if t.get("worktree"):
            t["cleanup"] = True  # the worktree was kept (it still has the branch checked out)
            return False
        if not Path(t["repo"]).is_dir() or not _ref_exists(t["repo"], f"refs/heads/{t['branch']}"):
            return True
        try:
            git("branch", "-D", t["branch"], cwd=t["repo"])
        except WonkaError as e:
            t["cleanup"] = True
            if not quiet:
                self.log_event(tid, f"Branch kept, could not delete {t['branch']} ({e})")
            return False
        return True

    def cleanup(self, tid):
        """Retry what a done ticket kept: its worktree (it had local changes) and, after a merge, its branch.
        Quiet on failure (it runs every tick); never forces, so local changes are never lost."""
        t, s = self.ticket(tid), self.state(tid)
        if s not in (("shipped", "ready"), KILLED):
            t.pop("cleanup", None)
            return None
        had_wt = bool(t.get("worktree"))
        if not self.drop_worktree(tid, quiet=True):
            return None
        had_branch = (s == ("shipped", "ready") and bool(t.get("branch")) and Path(t["repo"]).is_dir()
                      and _ref_exists(t["repo"], f"refs/heads/{t['branch']}"))
        if s == ("shipped", "ready") and not self.drop_branch(tid, quiet=True):
            return None
        t.pop("cleanup", None)
        if not had_wt and not had_branch:
            return None  # nothing was left after all
        what = " and ".join(w for w, kept in (("worktree", had_wt), ("branch", had_branch)) if kept)
        self.log_event(tid, f"Cleaned up the kept {what}")
        return f"{tid}: cleaned up the kept {what}"

    def baked(self, tid, dispatch=None):
        if self._stale_dispatch(tid, dispatch):
            return (f"{tid}: builder result is from an earlier dispatch "
                    f"({dispatch}, now {self.ticket(tid)['dispatch']}), ignored")
        self.expect(tid, ("baking", "progress"))
        t = self.ticket(tid)
        if git("status", "--porcelain", cwd=t["worktree"]):
            raise WonkaError("the worktree has uncommitted changes: commit them first")
        if git("rev-list", "--count", f"{self.base_ref(tid)}..HEAD", cwd=t["worktree"]) == "0":
            raise WonkaError(f"no commits on {t['branch']} yet")
        if t.get("reviewed_head") and git("rev-parse", "HEAD", cwd=t["worktree"]) == t["reviewed_head"]:
            raise WonkaError(f"no new commits on {t['branch']} since the last review: commit the fixes first")
        self.move(tid, ("baking", "ready"), "builder")
        self.move(tid, ("reviewing", "pending"), "wonka")

    # -- reviewing --------------------------------------------------------

    def _stale_dispatch(self, tid, dispatch):
        """True when an agent's dispatch number is from an earlier hand-out than the current one."""
        cur = self.ticket(tid).get("dispatch")
        return dispatch is not None and cur is not None and dispatch != cur

    def report(self, tid, reviewer, rep, dispatch=None):
        if reviewer not in REVIEWERS:
            raise WonkaError(f"unknown squirrel {reviewer}; one of {', '.join(REVIEWERS)}")
        if self._stale_dispatch(tid, dispatch):
            return (f"{tid}: squirrel-{reviewer} report is from an earlier dispatch "
                    f"({dispatch}, now {self.ticket(tid)['dispatch']}), ignored")
        self.expect(tid, ("reviewing", "progress"))
        t = self.ticket(tid)
        t.setdefault("reports", {})[reviewer] = rep
        missing = [r for r in t["reviewers"] if r not in t["reports"]]
        if missing:
            return f"{tid}: waiting for {', '.join(missing)}"
        return self._verdict(tid)

    def _child_titles(self, tid):
        """Casefolded titles of a ticket's children, in any state, so a repeated follow-up is not filed twice."""
        titles = set()
        for c in self.ticket(tid).get("children", []):
            try:
                titles.add((self.read(c)[0].get("title") or "").strip().casefold())
            except (WonkaError, OSError):
                pass  # lost note or unknown child: nothing to match against
        return titles

    def _live_children(self, t):
        """The ticket's children that still exist in the db, in order."""
        return [k for k in t["children"] if k in self.db["tickets"]]

    def _close_with_children(self, tid, kids, section, line, why):
        """List the children as `- [[KID]] <line(KID)>` in a note section and close the ticket (KILLED)."""
        meta, body = self.read(tid)
        text = "\n".join(f"- [[{k}]] {line(k)}" for k in kids)
        self.write(tid, meta, note.set_section(body, section, text))
        self.move(tid, KILLED, "analyst", why)

    def _depth(self, tid):
        """How many parents up the ticket's family goes: follow-ups of follow-ups stop at max_followup_depth."""
        depth, seen, cur = 0, {tid}, self.db["tickets"].get(tid, {}).get("parent")
        while cur in self.db["tickets"] and cur not in seen:
            seen.add(cur)
            depth, cur = depth + 1, self.db["tickets"][cur].get("parent")
        return depth

    def _verdict(self, tid):
        t = self.ticket(tid)
        reps, rnd = t.pop("reports"), t["review_round"]
        t.pop("finished", None)
        seen = self._child_titles(tid)
        depth, max_depth = self._depth(tid), self.cfg.get("max_followup_depth", 2)
        for rep in reps.values():
            for f in rep.get("followups", []):
                title = (f.get("title") or _default_title(f["idea"])).strip()
                if title.casefold() in seen:
                    self.log_event(tid, f"Follow-up already in the factory, skipped: {title}")
                    continue
                if depth >= max_depth:  # follow-ups breeding follow-ups would never end without a human
                    idea = " ".join(f["idea"].split())
                    idea = idea if len(idea) <= 300 else idea[:299] + "…"
                    self.log_event(tid, f"Follow-up not filed, this ticket is {depth} follow-ups deep (max_followup_depth "
                                        f"{max_depth}); add it by hand if you want it: {title}: {idea}")
                    seen.add(title.casefold())
                    continue
                self.add(f["idea"], title, t["repo"], t["pr_repo"], parent=tid)
                seen.add(title.casefold())
        issues = [(name, i) for name, rep in reps.items() for i in rep.get("issues", [])]
        if issues and t["plan"] and Path(t["plan"]).exists():
            log = [f"\n## 🐿️ Review round {rnd}"]  # only squirrels with issues: a clean verdict is noise to the builder
            for name, rep in reps.items():
                if rep.get("issues"):
                    log.append(f"### squirrel-{name}: {rep.get('verdict', '?')}")
                    log += [f"- {i}" for i in rep["issues"]]
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
            self.move(tid, ("reviewing", "awaiting"), "squirrels", f"Review round {rnd}/{self._budget(t)}: the squirrels have questions")
            return f"{tid}: review questions for a human"
        if issues:
            return self._back_to_baking(tid, f"Review round {rnd}/{self._budget(t)}: {len(issues)} issue(s), back to baking")
        self.move(tid, ("reviewing", "ready"), "squirrels", f"Review round {rnd}/{self._budget(t)}: a good nut")
        self.move(tid, ("shipped", "pending"), "wonka")
        return f"{tid}: clean review, off to ship"

    def _log_decisions(self, tid, section):
        """Append the human's answers to the squirrels' questions to the plan, under this round's review heading."""
        t = self.ticket(tid)
        body = note.decisions(section, skip_no=(note.CLOSE_Q, note.DRAFT_Q))
        if not body or not t["plan"] or not Path(t["plan"]).exists():
            return
        plan = Path(t["plan"])
        text = plan.read_text()
        head = f"## 🐿️ Review round {t['review_round']}"
        rounds = list(REVIEW_LOG.finditer(text))
        sep = "" if not text or text.endswith("\n") else "\n"
        if rounds and rounds[-1].group(0) == head:
            add = f"### Decisions\n{body}\n"
        else:  # a clean round with questions logged no heading yet
            add = f"\n{head}\n### Decisions\n{body}\n"
        plan.write_text(text + sep + add)

    def _back_to_baking(self, tid, why):
        t = self.ticket(tid)
        if t["review_round"] >= self._budget(t):
            self.move(tid, KILLED, "wonka", f"Bad nut: {self._max_rounds()} review rounds without a clean bill, rolled away")
            return f"{tid}: killed after {self._max_rounds()} review rounds"
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
            if t.get("pr_url"):  # a retry or a fix round: the human may have closed the PR meanwhile
                state = (gh("pr", "view", t["pr_url"], "--json", "state", "-q", ".state").split() or [""])[0]
                if state == "CLOSED":
                    self.move(tid, KILLED, "github", "PR closed without merging")
                    return f"{tid}: PR closed"
                if state == "MERGED":
                    raise _Merged(f"{t['pr_url']} was merged while the fix round ran")
            check_branch(t)
            ours = self._own_prs(tid)  # before the push: pushing the branch changes every PR opened from it
            try:
                git("push", "-u", "origin", t["branch"], cwd=t["worktree"])
            except WonkaError as e:
                if _push_rejected(e):
                    raise _PushRejected(str(e)) from e
                raise
            origin = parse_remote(git("remote", "get-url", "origin", cwd=t["worktree"]))
            head = t["branch"] if origin in (None, t["pr_repo"]) else f"{origin.split('/')[0]}:{t['branch']}"
            meta, body = self.read(tid)
            spec = (note.get_section(body, "spec") or note.get_section(body, "idea") or "").replace(f"📋 Plan: [[{tid}-plan]]", "")
            out = ours[0] if ours else ""
            reused = bool(out)
            if not reused:
                out = gh("pr", "create", "--repo", t["pr_repo"], "--base", t["base_branch"], "--head", head,
                         "--title", f"{tid}: {meta.get('title', '')}",
                         "--body", pr_body(tid, meta, spec))
        except (WonkaError, OSError) as e:
            err = " ".join(str(e).split())
            if isinstance(e, _NotOurs):  # someone else's PR is on the branch: never touch it, a human decides
                why = (f"an open PR on {t['branch']} was not opened by Wonka, so Wonka will not push to it: close it "
                       "or move it off the branch, then tick Retry")
                outcome = self.retry(tid, why, final=True).split(": ", 1)[1]
                self.log_event(tid, err)
                return f"{tid}: PR failed: {why}; {outcome}"
            if isinstance(e, _Merged):  # never open a second PR with the merged commits in it
                why = (f"the PR was merged while the fix round ran: the new commits are only on {t['branch']}. "
                       "Open a PR for them by hand, then drag the card to Wont-Do")
                outcome = self.retry(tid, why, final=True).split(": ", 1)[1]
                self.log_event(tid, err)
                return f"{tid}: PR failed: {why}; {outcome}"
            if isinstance(e, _PushRejected):  # the human pushed to the PR: retrying cannot help, say what to do
                why = _push_rejected_reason(t["branch"])
                outcome = self.retry(tid, why, final=True).split(": ", 1)[1]
                self.log_event(tid, f"Push rejected, not a fast-forward: {err}")
                return f"{tid}: PR failed: {why}; {outcome}"
            outcome = self.retry(tid, f"could not open the PR: {err}").split(": ", 1)[1]
            return f"{tid}: PR failed: {err}; {outcome}"
        t["pr_url"] = out.splitlines()[-1]
        self.set_section(tid, "changes", note.CHANGES_BOX)
        if reused:
            what = "PR updated with the requested changes" if t.get("change_requests") else "PR updated"
            self.move(tid, ("shipped", "awaiting"), "wonka", f"{what}, review and merge it: {t['pr_url']}")
            return f"{tid}: PR updated {t['pr_url']}"
        self.move(tid, ("shipped", "awaiting"), "wonka", f"PR opened, approve and merge it: {t['pr_url']}")
        return f"{tid}: PR {t['pr_url']}"

    def _own_prs(self, tid):
        """The URLs of the open PRs on the ticket's branch, which must all be Wonka's own for this ticket: the PR it
        recorded, or one whose description starts with its golden ticket line (opened by Wonka, e.g. before a crash
        lost the URL). Raises _NotOurs for any other PR: Wonka never pushes to a PR it did not open."""
        t = self.ticket(tid)
        prs = gh_json("pr", "list", "--repo", t["pr_repo"], "--head", t["branch"], "--state", "open",
                      "--json", "url,body", default=[])
        ours, others = [], []
        for pr in prs:
            pr = pr if isinstance(pr, dict) else {}
            mine = pr.get("url") and (pr["url"] == t.get("pr_url") or (pr.get("body") or "").startswith(_pr_mark(tid)))
            (ours if mine else others).append(pr.get("url") or "?")
        if others:
            raise _NotOurs(f"open PR {', '.join(others)} on {t['branch']} was not opened by Wonka for {tid}: "
                           "Wonka never pushes to it")
        return ours

    def _pr_feedback(self, tid, since=None):
        """The PR's comments, reviews and inline comments by trusted authors newer than `since`, as markdown
        bullets, plus how many were skipped because their author is not an owner, member or collaborator, plus
        the newest GitHub timestamp among everything fetched (filtered or not), or None when nothing has one."""
        t = self.ticket(tid)
        num = t["pr_url"].rstrip("/").rsplit("/", 1)[-1]
        view = gh_json("pr", "view", t["pr_url"], "--json", "comments,reviews", default={})
        try:
            inline = _json_pages(gh("api", "--paginate", f"repos/{t['pr_repo']}/pulls/{num}/comments?per_page=100"))
        except ValueError as e:
            raise WonkaError(f"gh api returned invalid JSON: {e}") from e
        items = []
        for c in view.get("comments") or []:
            if not isinstance(c, dict):
                continue
            items.append(((c.get("author") or {}).get("login"), c.get("authorAssociation"), c.get("body"),
                          c.get("createdAt"), ""))
        for r in view.get("reviews") or []:
            if not isinstance(r, dict):
                continue
            state = (r.get("state") or "").lower().replace("_", " ")
            items.append(((r.get("author") or {}).get("login"), r.get("authorAssociation"), r.get("body"),
                          r.get("submittedAt"), f" (review, {state})" if state else " (review)"))
        for c in inline:
            if not isinstance(c, dict):
                continue
            line = c.get("line") or c.get("original_line")
            where = f"{c.get('path')}:{line}" if line else f"{c.get('path')}"
            items.append(((c.get("user") or {}).get("login"), c.get("author_association"), c.get("body"),
                          c.get("created_at"), f" on `{where}`"))
        cutoff = _parse_stamp(since, "changes cutoff") if since else None
        kept, skipped, newest = [], 0, None
        for who, assoc, text, stamp, kind in items:
            at = _parse_stamp(stamp, "PR comment") if stamp else None
            if at and (newest is None or at > newest):
                newest = at
            if not (text or "").strip():
                continue
            if cutoff and (at is None or at <= cutoff):
                continue
            if (assoc or "").upper() not in TRUSTED:
                skipped += 1
                continue
            first, *rest = text.strip().splitlines()
            bullet = "\n".join([f"- **@{who}**{kind}: {first}", *(f"    > {line}".rstrip() for line in rest)])
            kept.append((at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), note.demote(bullet)))
        kept.sort(key=lambda k: k[0])
        return [b for _, b in kept], skipped, newest

    def _request_changes(self, tid, notes):
        """The human ticked 🔁 Request changes: write their notes and the PR feedback into the plan, back to baking."""
        t = self.ticket(tid)
        pr, skipped, newest = self._pr_feedback(tid, t.get("changes_cutoff"))  # fetch first: a gh failure changes nothing
        if not notes.strip() and not pr:
            self.set_section(tid, "changes", note.CHANGES_BOX)
            self.log_event(tid, "Request changes was ticked, but there were no notes under it and no new PR comments: "
                                "write what to change, then tick it again")
            return f"{tid}: request changes ticked with nothing to change"
        n = t.get("change_requests", 0) + 1
        # GitHub's clock, not ours: the newest stamp this fetch saw, never moving back past the previous cutoff
        prev = t.get("changes_cutoff")
        stamps = [s for s in (newest, dt.datetime.fromisoformat(prev) if prev else None) if s]
        t["change_requests"], t["changes_cutoff"] = n, (_iso(max(stamps)) if stamps else None)
        plan = Path(t["plan"]) if t.get("plan") else None
        if plan and plan.exists():
            head, log = _split_reviews(plan.read_text())
            text = (head + _age_log(log)).rstrip("\n") + "\n"
            text += "\n".join([
                "", f"## 🔁 Change request {n}",
                f"The human asked for these changes on the open PR {t['pr_url']}. "
                "Fix them with new commits on the same branch.",
                "### From the note", note.demote(notes.strip()) if notes.strip() else "(none)",
                "### PR comments", "\n".join(pr) if pr else "(none)",
            ]) + "\n"
            plan.write_text(text)
        t["review_round"], t["fixing_changes"] = 0, True
        t.pop("extra_rounds", None)
        meta, body = self.read(tid)
        self.write(tid, meta, note.drop_section(body, "changes"))
        why = f"Changes requested ({len(pr)} PR comment(s)" + (
            f", {skipped} from non-collaborators skipped" if skipped else "") + "), back to baking"
        self.move(tid, ("baking", "pending"), "human", why)
        return f"{tid}: changes requested, back to baking"

    def _fetch_pr_base(self, tid):
        """Fetch the PR's base branch into the worktree and return the ref it now lives at."""
        t = self.ticket(tid)
        wt, base = t["worktree"], t.get("pr_base") or t["base_branch"]  # the base GitHub compares against now
        try:
            origin = parse_remote(git("remote", "get-url", "origin", cwd=wt))
        except WonkaError:
            origin = None
        if origin in (None, t["pr_repo"]):
            git("fetch", "--quiet", "origin", base, cwd=wt)
            return f"origin/{base}"
        git("fetch", "--quiet", f"https://github.com/{t['pr_repo']}.git",
            f"+refs/heads/{base}:refs/remotes/wonka-pr/{base}", cwd=wt)
        return f"wonka-pr/{base}"

    def _sync_pr_branch(self, tid):
        """Fast-forward the worktree to the PR branch on origin (review suggestions, 'Update branch', a human fix),
        so the fixer rebases what GitHub has and the push in resolved() replaces exactly that."""
        t = self.ticket(tid)
        wt, branch = t["worktree"], t["branch"]
        git("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=wt)
        try:
            git("merge", "--ff-only", "--quiet", f"origin/{branch}", cwd=wt)
        except WonkaError as e:
            raise _Diverged(f"{branch} has diverged from origin/{branch}: {' '.join(str(e).split())}") from e

    def _stash_leftovers(self, tid):
        """Put uncommitted changes and untracked files in the worktree aside (git stash, never lost), so the
        ff-only sync and the fixer's rebase start from a clean tree. Ignored files are left alone."""
        wt = self.ticket(tid)["worktree"]
        if not git("status", "--porcelain", cwd=wt):
            return
        git("stash", "push", "--include-untracked", "--quiet", "-m",
            f"wonka: leftovers before fixing conflicts on {tid}", cwd=wt)
        sha = git("rev-parse", "refs/stash", cwd=wt)
        base = ref = f"refs/wonka/stash/{tid}/{now().strftime('%Y%m%dT%H%M%SZ')}"
        n = 1
        while _ref_exists(wt, ref):
            n += 1
            ref = f"{base}-{n}"
        git("update-ref", ref, sha, cwd=wt)
        _drop_stash_entry(wt, sha)
        self.log_event(tid, f"Stashed uncommitted leftovers in {wt} before the conflict fix as {ref} "
                            f"(git stash apply {ref} there to get them back)")

    def _fixer_failed_to_start(self, tid, exc):
        """The fixer could not be dispatched: count it like a retry, stuck after MAX_RETRIES (at once if diverged)."""
        t = self.ticket(tid)
        err = " ".join(str(exc).split())
        err = err if len(err) <= 200 else err[:199] + "…"
        final = isinstance(exc, _Diverged)
        why = (f"{t['branch']} has diverged from the PR branch on GitHub. In the worktree {t['worktree']} run "
               f"git pull --rebase origin {t['branch']} and git push origin {t['branch']}, then tick Retry"
               if final else f"the fixer could not start: {err}")
        retries, stuck = self._count_failure(tid, why, final=final)
        tries = f"try {min(retries, MAX_RETRIES)}/{MAX_RETRIES}"
        t["conflict_error"] = f"{tries}: {err}"
        self.log_event(tid, f"Fixer could not start ({tries}): {err}")
        if stuck:
            return f"{tid}: ⚠️ fixer could not start, stuck: {err}"
        return f"{tid}: ⚠️ fixer could not start ({tries}), retried next tick: {err}"

    def resolved(self, tid, dispatch=None):
        """The fixer rebased the branch onto the base: check it, push it with --force-with-lease (it only replaces
        the PR branch GitHub had when the fixer started) and wait for the human again. A call from an earlier
        dispatch is ignored."""
        if self._stale_dispatch(tid, dispatch):
            return self._stale_msg(tid, "fixer result", dispatch)
        self.expect(tid, ("shipped", "progress"))
        t = self.ticket(tid)
        if not t.get("conflicts"):
            raise WonkaError(f"{tid} is not fixing merge conflicts")
        wt = t["worktree"]
        if git("status", "--porcelain", cwd=wt) or _ref_exists(wt, "MERGE_HEAD") or _rebasing(wt):
            raise WonkaError("the worktree has uncommitted changes or an unfinished rebase: finish the rebase first")
        rebased = _exec(["git", "merge-base", "--is-ancestor", t["merge_ref"], "HEAD"], cwd=wt)[0] == 0
        if not rebased:
            raise WonkaError(f"{t['branch']} is not rebased onto {t['merge_ref']} yet")
        check_branch(t)
        self._own_prs(tid)
        # a fixer started before rebasing existed has no lease; its merge is a fast-forward, pushed without force
        lease = [f"--force-with-lease={t['branch']}:{t['lease']}"] if t.get("lease") else []
        git("push", *lease, "origin", t["branch"], cwd=wt)
        # the fix landed: the next conflict episode gets the full start budget and no stale warning
        t.pop("retries", None)
        t.pop("conflict_error", None)
        # retries is reset here on purpose, so this count is what bounds a PR that GitHub keeps calling conflicting
        n = t["conflict_fixes"] = t.get("conflict_fixes", 0) + 1
        self.move(tid, ("shipped", "awaiting"), "fixer", f"Merge conflicts fixed and pushed (fix {n}/{MAX_CONFLICT_FIXES})")

    # -- the tick ---------------------------------------------------------

    def _find_renamed(self, foreign):
        """Notes renamed in Obsidian (their frontmatter id is a live ticket whose note is gone) get their name back,
        so a rename is never taken for a deletion. Returns the events."""
        events = []
        folder = store.tickets_dir(self.cfg)
        for name in foreign:
            try:
                tid = str(note.read(folder / name)[0].get("id") or "")
            except (OSError, UnicodeDecodeError):
                continue
            t = self.db["tickets"].get(tid)
            if not t or t.get("archived") or self.path(tid).exists():
                continue
            (folder / name).rename(self.path(tid))
            self.log_event(tid, f"The note was renamed to {name}: renamed it back to {self.path(tid).name} "
                                "(Wonka finds tickets by file name)")
            events.append(f"{tid}: note was renamed to {name}, renamed back")
        return events

    def _check_notes(self, drop_missing):
        """Refuse to tick when the tickets folder is missing, or when every live ticket's note is gone at once: that
        is an unmounted, moved or reconfigured vault, not a human deleting notes, and dropping is final."""
        folder = store.tickets_dir(self.cfg)
        if not folder.is_dir():
            raise CriticalError(f"the tickets folder {folder} is missing (vault not mounted or moved, or `vault` changed "
                             "in ~/.wonka/config.json): tick skipped so no ticket is dropped")
        live = self.live()
        missing = [k for k in live if not self.path(k).exists()]
        if not drop_missing and len(live) > 1 and len(missing) == len(live):
            raise CriticalError(f"none of the {len(live)} live tickets has a note in {folder}: tick skipped so no "
                             "ticket is dropped. If you deleted them all on purpose, run wonka tick --drop-missing")

    def reconcile(self, tid):
        """Put illegal board edits back and act on ticked checkboxes and merged PRs."""
        t = self.ticket(tid)
        if not self.path(tid).exists():
            src = self.state(tid)
            self._hold_for_live_job(tid, src)
            t.pop("job_started_at", None)  # the ticket is archived: its job is no longer tracked
            t["last_valid"], t["archived"], t["lost"] = {"status": "wontdo", "phase": "ready"}, True, True
            t["history"].append({"at": _iso(now()), "from": "/".join(src), "to": "wontdo/ready", "by": "human"})
            self.drop_worktree(tid)
            self.drop_scout(tid)
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
        if (meta.get("status"), meta.get("phase")) != machine.label(last):
            # silent migration of old labels (🌟-Done, 🎫-Ready) and hand-typed names
            meta["status"], meta["phase"] = machine.label(last)
            self.write(tid, meta, body)
        if t.get("stuck") and note.approved_retry(note.get_section(body, "stuck")):
            t.pop("stuck"), t.pop("retries", None), t.pop("conflict_fixes", None)
            self.write(tid, meta, note.drop_section(body, "stuck"))
            return f"{tid}: unstuck by a human"
        if last == ("inbox", "awaiting"):
            appr, qs = note.get_section(body, "approval"), note.get_section(body, "questions")
            if (appr is None or note.approved(appr)) and (qs is None or note.all_answered(qs)):
                self.move(tid, ("inbox", "ready"), "human", "Approved")
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
                self._log_decisions(tid, sec)  # before a redraft resets review_round
                if note.picked_yes(sec, note.DRAFT_Q):
                    t["review_round"] = 0
                    t.pop("extra_rounds", None)
                    t.pop("reviewed_head", None)
                    self.move(tid, ("drafting", "pending"), "human", "Back to drafting after review")
                    return f"{tid}: back to drafting"
                if not issues:
                    if note.all_recommended(sec, skip=(note.CLOSE_Q, note.DRAFT_Q)):
                        self.move(tid, ("reviewing", "ready"), "human", "Review answers in, all as recommended: a good nut")
                        self.move(tid, ("shipped", "pending"), "wonka")
                        return f"{tid}: clean review answered, off to ship"
                    t["extra_rounds"] = t.get("extra_rounds", 0) + 1
                    return self._back_to_baking(
                        tid, f"Review answers in, a clean round but the answers change the plan, back to baking "
                             f"(round {t['review_round']}/{self._budget(t)}, 1 extra round granted)")
                return self._back_to_baking(tid, f"Review answers in, back to baking (round {t['review_round']}/{self._budget(t)})")
        elif last == ("shipped", "awaiting") and t["pr_url"]:
            out = gh("pr", "view", t["pr_url"], "--json", "state,mergeable,baseRefName",
                     "-q", '.state + " " + .mergeable + " " + .baseRefName')
            pr, mergeable, pr_base = (out.split() + ["", "", ""])[:3]
            if pr == "OPEN" and pr_base:
                t["pr_base"] = pr_base  # the base may have been changed on GitHub since the ticket was added
            if pr == "MERGED":
                self.move(tid, ("shipped", "ready"), "github", "PR merged 🎉")
                return f"{tid}: shipped"
            if pr == "CLOSED":
                self.move(tid, KILLED, "github", "PR closed without merging")
                return f"{tid}: PR closed"
            if pr == "OPEN" and mergeable == "CONFLICTING" and not t.get("conflicts") and not t.get("stuck"):
                fixes = t.get("conflict_fixes", 0)
                if fixes >= MAX_CONFLICT_FIXES:
                    base = t.get("pr_base") or t["base_branch"]
                    t["stuck"] = True
                    self.set_section(tid, "stuck", f"- [ ] Retry ({fixes} conflict fixes were pushed but GitHub still "
                                                   f"reports the PR as conflicting with `{base}`: check the PR's base "
                                                   "branch on GitHub or fix the conflicts by hand. See ~/.wonka/factory.log)")
                    self.log_event(tid, f"The PR is still conflicting after {fixes} conflict fixes: stuck, needs a human")
                    notify(tid, "stuck: tick Retry when it's fixed")
                    return f"{tid}: stuck, PR still conflicting after {fixes} conflict fixes"
                t["conflicts"] = True
                self.log_event(tid, "The PR has merge conflicts: a fixer Oompa-Loompa is called")
                return f"{tid}: PR has merge conflicts"
            if pr == "OPEN" and mergeable == "MERGEABLE":
                t.pop("retries", None)  # conflicts gone (by hand or a fixer): the next episode gets the full budget
                t.pop("conflict_fixes", None)
                t.pop("conflict_error", None)
                # fixed by hand before a fixer ran (or while it could not start): drop the banner's warning
                dirty = bool(t.pop("conflicts", None))
                if t.pop("stuck", None):  # a fixer that could not start, or a PR that stayed conflicting
                    body, dirty = note.drop_section(body, "stuck"), True
                    self.log_event(tid, "The PR is mergeable again: unstuck")
                if dirty:
                    self.write(tid, meta, body)
            changes = note.get_section(body, "changes")
            if changes is None:  # a PR opened before change requests existed gets the box too
                self.set_section(tid, "changes", note.CHANGES_BOX)
                return None
            ticked, notes = note.requested_changes(changes)
            if ticked:
                return self._request_changes(tid, notes)
        return None

    def archive(self, tid):
        t = self.ticket(tid)
        stamp = t.get("entered_done_at")
        if t.get("archived") or self.state(tid) not in (("shipped", "ready"), KILLED) or not stamp:
            return None
        if _age(stamp) < dt.timedelta(days=self.cfg["archive_after_days"]):
            return None
        src, plan = self.path(tid), self.plan_path(tid)
        folder = store.archive_dir(self.cfg)
        folder.mkdir(parents=True, exist_ok=True)
        if plan.exists():  # the plan first: if the note's move fails, the ticket is still live and retried next tick
            shutil.move(plan, folder / plan.name)
            t["plan"] = str(folder / plan.name)
        shutil.move(src, folder / src.name)
        t["archived"] = True
        return f"{tid}: archived"

    def start(self, tid):
        """Claim a job for an agent and return what it needs to know."""
        t, s = self.ticket(tid), self.state(tid)
        job = {"inbox": "triage", "drafting": "draft", "baking": "bake", "reviewing": "review",
               "shipped": "conflicts"}[s[0]]
        base_ref = None
        if s == ("drafting", "pending"):
            self.move(tid, ("drafting", "progress"), "wonka")
        elif s == ("baking", "pending"):
            if (failed := self._setup_worktree(tid)) is not None:
                return failed
            if t.get("pr_url"):  # a fix round on an open PR: take what was pushed there, so the ship's push fast-forwards
                try:
                    self._sync_pr_branch(tid)
                except WonkaError as e:  # offline or diverged: never block the builder, ship() explains a rejected push
                    self.log_event(tid, "Could not catch up with the PR branch on GitHub, baking on the local branch "
                                        f"({' '.join(str(e).split())})")
            self.move(tid, ("baking", "progress"), "wonka")
        elif s == ("reviewing", "pending"):
            if (failed := self._setup_worktree(tid)) is not None:
                return failed
            t["reviewed_head"] = git("rev-parse", "HEAD", cwd=t["worktree"])
            t["review_round"] += 1
            t.pop("fixing_changes", None)
            t["reports"] = {}
            t["finished"] = []
            self.move(tid, ("reviewing", "progress"), "wonka")
        elif s == ("shipped", "awaiting"):
            try:  # all before moving: a failure is counted like a retry and the ticket stays awaiting
                self.ensure_worktree(tid)
                for unfinished, abort in ((_ref_exists(t["worktree"], "MERGE_HEAD"), "merge"),
                                          (_rebasing(t["worktree"]), "rebase")):
                    if unfinished:
                        try:
                            git(abort, "--abort", cwd=t["worktree"])  # a half-done earlier attempt
                        except WonkaError:
                            pass
                self._stash_leftovers(tid)
                self._sync_pr_branch(tid)
                # what GitHub has now: the rebased branch may only replace this (--force-with-lease in resolved())
                t["lease"] = git("rev-parse", f"refs/remotes/origin/{t['branch']}", cwd=t["worktree"])
                base_ref = t["merge_ref"] = self._fetch_pr_base(tid)
            except (WonkaError, OSError) as e:
                return self._fixer_failed_to_start(tid, e)
            self.move(tid, ("shipped", "progress"), "wonka", "Merge conflicts on the PR: a fixer Oompa-Loompa is on it")
        code, ref = t["worktree"], base_ref or self.base_ref(tid)
        if job in ("triage", "draft"):
            try:
                code, ref = self.fresh_scout(tid)
            except (WonkaError, OSError) as e:  # never strand the job: the draft has already moved to progress
                self.log_event(tid, f"No fresh checkout of the base, the agent reads the main checkout ({e})")
                code, ref = t["repo"], self.base_ref(tid)
        t.pop("resume", None)
        t["job_started_at"] = _iso(now())
        t["dispatch"] = t.get("dispatch", 0) + 1  # only ever goes up: tells a late agent from the live one
        agents = {"triage": ["oompa-loompa-triage"], "draft": ["oompa-loompa-analyst"], "bake": ["oompa-loompa-builder"],
                  "review": [f"squirrel-{r}" for r in t["reviewers"] or ["architect"]],
                  "conflicts": ["oompa-loompa-fixer"]}[job]
        agents = [f"wonka:{a}" for a in agents]
        return {"id": tid, "job": job, "agents": agents, "note": str(self.path(tid)), "plan": str(self.plan_path(tid)),
                "repo": t["repo"], "code": code, "worktree": t["worktree"], "branch": t["branch"], "base_ref": ref,
                "review_round": t["review_round"], "dispatch": t["dispatch"]}

    def _setup_worktree(self, tid):
        """ensure_worktree() before a bake or review job; a failure counts like a retry, stuck after MAX_RETRIES.
        Returns None when the worktree is ready, else the event to report."""
        try:
            self.ensure_worktree(tid)
            return None
        except (WonkaError, OSError) as e:
            err = " ".join(str(e).split())
            err = err if len(err) <= 150 else err[:149] + "…"
            retries, stuck = self._count_failure(tid, f"could not set up the worktree: {err}")
            self.log_event(tid, f"Could not set up the worktree (try {min(retries, MAX_RETRIES)}/{MAX_RETRIES}): {err}")
            if stuck:
                return f"{tid}: ⚠️ could not set up the worktree, stuck: {err}"
            return f"{tid}: ⚠️ could not set up the worktree (try {retries}/{MAX_RETRIES}), retried next tick: {err}"

    def retry(self, tid, why="the last job did not finish", final=False):
        """Put the job back in the queue, or stuck after MAX_RETRIES (at once when `final`: retrying cannot help)."""
        t, s = self.ticket(tid), self.state(tid)
        if s[1] not in ("pending", "progress"):
            raise WonkaError(f"{tid} is {fmt(s)}: no job is running or queued, nothing to retry")
        retries = t.get("retries", 0) + 1
        if final:
            retries = max(retries, MAX_RETRIES)
        why = " ".join(str(why).split())
        why = why if len(why) <= _REASON_MAX else why[:_REASON_MAX - 1] + "…"
        if s[1] == "progress":
            if s[0] == "reviewing":
                t["review_round"] -= 1
                t.pop("reports", None)
                t.pop("finished", None)
            self.move(tid, (s[0], "pending"), "wonka",
                      f"Stuck: {why}" if final else f"Retry {retries}/{MAX_RETRIES}: {why}")
        t.pop("job_started_at", None)
        retries, stuck = self._count_failure(tid, why, final=final)
        if stuck:
            return f"{tid}: stuck, needs a human" if final else f"{tid}: stuck after {retries} tries"
        return f"{tid}: back in the queue"

    def _count_failure(self, tid, why, final=False):
        """Bump the retry count; at MAX_RETRIES (at once when `final`) mark the ticket stuck with a Retry checkbox.
        Returns (retries, stuck)."""
        t = self.ticket(tid)
        retries = t.get("retries", 0) + 1
        if final:
            retries = max(retries, MAX_RETRIES)
        t["retries"] = retries
        if retries < MAX_RETRIES:
            return retries, False
        t["stuck"] = True
        failed = "" if final else f"{retries} tries failed, the last one: "
        self.set_section(tid, "stuck", f"- [ ] Retry ({failed}{why}. See ~/.wonka/factory.log)")
        notify(tid, "stuck: tick Retry when it's fixed")
        return retries, True

    def finished(self, tid, agent, dispatch=None):
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
        if self._stale_dispatch(tid, dispatch):
            return f"{tid}: {agent} is from an earlier dispatch ({dispatch}, now {t['dispatch']}), ignored"
        held = t.get("held_by")
        if (held and name in held.get("agents", [])
                and (dispatch is None or held.get("dispatch") is None or held["dispatch"] == dispatch)):
            held["agents"].remove(name)
            if held["agents"]:
                return f"{tid}: {agent} ended, the ticket is killed; its worktree waits for {', '.join(held['agents'])}"
            t.pop("held_by")
            return f"{tid}: {agent} ended, the ticket is killed; its worktree is cleaned up next tick"
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

    def _foreign(self):
        folder = store.tickets_dir(self.cfg)
        if not folder.exists():
            return []
        return sorted(p.name for p in folder.glob("*.md") if p.stem not in self.db["tickets"])

    def _triaging(self):
        return sum(1 for k in self.live()
                   if self.state(k) == ("inbox", "pending") and self.ticket(k).get("job_started_at"))

    def tick(self, drop_missing=False):
        self._base_cache = {}  # one base fetch per (repo, base) per tick
        try:
            events = self._find_renamed(self._foreign())
            self._check_notes(drop_missing)
            if store.migrate_base(self.cfg):
                events.append("🍫 Factory.base: column 🎫-Ready renamed to 🎫-Planned")
            events += [m for k in self.live()
                       for m in [self._safe(k, self.reconcile), self._safe(k, self.archive)] if m]
            # retry what done tickets kept (archived and lost ones too): never forced, quiet until it works
            events += [m for k, t in sorted(self.db["tickets"].items())
                       if (t.get("cleanup") or t.get("worktree")) and self.state(k) in (("shipped", "ready"), KILLED)
                       for m in [self._safe(k, self.cleanup)] if m]
            foreign = self._foreign()
            seen = set(self.db.get("foreign_notes", []))
            events += [f"{n}: not a Wonka ticket, use /wonka add instead" for n in foreign if n not in seen]
            self.db["foreign_notes"] = foreign
            for src, dst, limit in ((("inbox", "ready"), ("drafting", "pending"), "drafting"),
                                    (("planned", "pending"), ("baking", "pending"), "baking")):
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
                elif s == ("inbox", "pending") and self._triaging() >= self.cfg["wip"].get("triage", 2):
                    continue  # triage has a limit too, or a burst of follow-ups dispatches a triage agent each
                elif s in (("inbox", "pending"), ("drafting", "pending"), ("baking", "pending"), ("reviewing", "pending")) \
                        or (s == ("drafting", "progress") and t.get("resume")) \
                        or (s == ("shipped", "awaiting") and t.get("conflicts")):
                    job = self._safe(k, self.start)
                    (events if isinstance(job, str) else jobs).append(job)
            stale_after = dt.timedelta(hours=self.cfg["stale_hours"])
            stale = [k for k in self.live() if self.ticket(k).get("job_started_at")
                     and _age(self.ticket(k)["job_started_at"]) > stale_after]
            awaiting = [k for k in self.live() if self.state(k)[1] == "awaiting"]
            return {"events": events, "jobs": jobs, "awaiting_human": awaiting, "stale": stale}
        finally:
            self._base_cache = None
