"""wonka: the factory's command line. Agents and the /wonka skill drive tickets only through this."""

import argparse
import copy
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import machine, prcomments, store
from .factory import Factory, alert, fmt, split_ids
from .store import WonkaError

ROOT = Path(__file__).resolve().parent.parent
LINK = Path.home() / ".local" / "bin" / "wonka"
# handled by hand: argparse would mangle `--` and the options meant for the command
PASSTHROUGH = {"git": "gitwrap", "test": "testrun", "ci-logs": "cilogs"}  # subcommand -> module in this package with main(argv)


def ensure_link():
    """Keep ~/.local/bin/wonka pointing at this copy of the plugin, so agents can call `wonka`."""
    target = ROOT / "bin" / "wonka"
    if LINK.is_symlink() and LINK.resolve() == target.resolve():
        return
    if LINK.exists() and not LINK.is_symlink():
        return
    LINK.parent.mkdir(parents=True, exist_ok=True)
    LINK.unlink(missing_ok=True)
    LINK.symlink_to(target)


def cmd_init(a):
    # an existing config keeps its settings: a broken one is refused, never silently replaced by the defaults
    exists = (store.home() / "config.json").exists()
    cfg = store.load_config() if exists else copy.deepcopy(store.DEFAULT_CONFIG)
    cfg["vault"], cfg["folder"] = str(Path(a.vault).expanduser().resolve()), a.folder
    for key, value in (("wip.triage", a.triage), ("wip.drafting", a.drafting), ("wip.baking", a.baking),
                       ("archive_after_days", a.archive_days), ("stale_hours", a.stale_hours),
                       ("max_review_rounds", a.max_review_rounds)):
        if value is not None:
            store.set_setting(cfg, key, value)
    store.save_config(cfg)
    for d in (store.tickets_dir, store.plans_dir, store.archive_dir):
        d(cfg).mkdir(parents=True, exist_ok=True)
    board = store.board(cfg)
    base, old = board / store.BASE_FILE, board / store.OLD_BASE_FILE
    if not base.exists() and old.exists():
        old.rename(base)
    elif not base.exists():
        base.write_text((ROOT / "templates" / "Factory.base").read_text().replace("{{folder}}", a.folder))
    store.migrate_base(cfg)
    ensure_link()
    show_config(cfg)
    print(f"🎩 “Come with me, and you'll be in a world of pure imagination.”\n   🍫 Factory {base}\n   State {store.home()}")


def show_config(cfg):
    items = sorted((cfg.get("worktree_files") or {}).items())
    files = "; ".join(f"{proj}: {', '.join(paths)}" for proj, paths in items) or "none"
    cmds = sorted((cfg.get("test_commands") or {}).items())
    tests = "; ".join(f"{proj}: {', '.join(c)}" for proj, c in cmds) or "none"
    hosts = "; ".join(f"{proj}: {', '.join(h)}" for proj, h in sorted((cfg.get("ci_hosts") or {}).items())) or "none"
    logcmd = "; ".join(f"{proj}: {c}" for proj, c in sorted((cfg.get("ci_log_command") or {}).items())) or "none"
    print(f"📏 triage at once   : {cfg['wip']['triage']}\n"
          f"🧪 drafting at once : {cfg['wip']['drafting']}\n"
          f"🍫 baking at once   : {cfg['wip']['baking']}\n"
          f"🗄️ archive after    : {cfg['archive_after_days']} days\n"
          f"⏳ retry jobs after : {cfg['stale_hours']} hours\n"
          f"🐿️ review rounds    : {cfg['max_review_rounds']}\n"
          f"🌱 follow-up depth  : {cfg['max_followup_depth']}\n"
          f"📄 worktree files   : {files}\n"
          f"🧪 test commands    : {tests}\n"
          f"🔗 CI hosts         : {hosts}\n"
          f"📜 CI log command   : {logcmd}\n"
          f"⏳ test lock wait   : {cfg['test_lock_wait_minutes']} minutes\n"
          f"🕹️ session launcher : {cfg['session_launcher']}\n"
          f"🗂️ factory folder   : {store.board(cfg) if cfg.get('vault') else '-'}\n"
          f"📜 factory log      : {store.home() / 'factory.log'}")


def cmd_config(a):
    cfg = store.load_config()
    if a.key:
        if a.value is None:
            raise WonkaError("usage: wonka config <key> <value>")
        store.set_setting(cfg, a.key, a.value)
        store.save_config(cfg)
    show_config(cfg)


def _stdin_json():
    try:
        return json.loads(sys.stdin.read())
    except ValueError as e:
        raise WonkaError(f"stdin is not valid JSON: {e}")


def _text(a):
    """Markdown from --file (what agents use: a heredoc keeps the call inside the sandbox) or from stdin."""
    if a.file is None:
        return sys.stdin.read()
    try:
        return Path(a.file).expanduser().read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise WonkaError(f"cannot read --file {a.file}: {e}")


def parse_question(text):
    """'Store sessions where? | Redis | *Postgres, already in the stack' → a question; * marks the recommendation."""
    q, *opts = [part.strip() for part in text.split("|")]
    if not q or len(opts) < 2:
        raise WonkaError(f"--question needs a question and at least two options: {text!r}")
    rec = next((i for i, o in enumerate(opts) if o.startswith("*")), None)
    return {"q": q, "options": [o.lstrip("*").strip() for o in opts], "recommended": rec}


def _strings(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def check_questions(qs, what="questions"):
    """JSON questions must be a list of {"q": str, "options": [str], "recommended": int, "why": str}."""
    if not isinstance(qs, list):
        raise WonkaError(f"{what} must be a JSON list of questions")
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict) or not isinstance(q.get("q"), str) or not q["q"].strip():
            raise WonkaError(f"{what}[{i}] needs a \"q\" with the question text")
        if not _strings(q.get("options", [])):
            raise WonkaError(f"{what}[{i}].options must be a list of strings")
        rec = q.get("recommended")
        if rec is not None and (isinstance(rec, bool) or not isinstance(rec, int)):
            raise WonkaError(f"{what}[{i}].recommended must be an option's index")
        if not isinstance(q.get("why") or "", str):
            raise WonkaError(f"{what}[{i}].why must be a string")
    return qs


def check_report(rep):
    """A squirrel report: refused here, before the state is touched, rather than half-filed."""
    if not isinstance(rep, dict):
        raise WonkaError("the report must be a JSON object")
    if not isinstance(rep.get("verdict", ""), str):
        raise WonkaError("verdict must be a string (good nut or bad nut)")
    if not _strings(rep.get("issues", [])):
        raise WonkaError("issues must be a list of strings")
    check_questions(rep.get("questions", []))
    followups = rep.get("followups", [])
    if not isinstance(followups, list):
        raise WonkaError("followups must be a list")
    for i, fu in enumerate(followups, 1):
        if not isinstance(fu, dict) or not isinstance(fu.get("idea"), str) or not fu["idea"].strip():
            raise WonkaError(f"followups[{i}] needs an \"idea\" with the idea text")
        if not isinstance(fu.get("title") or "", str):
            raise WonkaError(f"followups[{i}].title must be a string")
    if rep.get("suggest") not in (None, "close", "draft"):
        raise WonkaError("suggest must be close or draft")
    if not isinstance(rep.get("why", ""), str):
        raise WonkaError("why must be a string")
    return rep


def _questions(a):
    """Questions come as --question flags (what agents use) or, with --stdin, as JSON."""
    qs = [parse_question(x) for x in a.question or []]
    return qs + (check_questions(_stdin_json(), "stdin") if getattr(a, "stdin", False) else [])


def _report(a):
    if a.stdin:
        return check_report(_stdin_json())
    rep = {"verdict": "good nut" if a.verdict == "good" else "bad nut", "issues": a.issue or [],
           "questions": [parse_question(x) for x in a.question or []], "suggest": a.suggest, "why": a.why or ""}
    for fu in a.followup or []:
        title, sep, idea = fu.partition("::")
        rep.setdefault("followups", []).append({"title": title.strip(), "idea": (idea if sep else title).strip()})
    return check_report(rep)


def run(a, f):
    c = a.cmd
    if c == "add":
        tid = f.add(a.idea, a.title, a.repo, a.pr_repo, a.parent, a.size, a.jira,
                  spec_review="human" if a.review_spec else None, blocked_by=split_ids(a.blocked_by),
                  start="triage" if a.triage else "idea")
        if a.manual:
            f.session(tid)
            print(f"🎫 {tid} has a golden ticket, 🕹️ manual from the start: no agent works it until you hand it "
                  f"back. Open it with: wonka session {tid}")
        elif a.triage:
            print(f"🎫 {tid} has a golden ticket! It's in 📏 Triage: {f.path(tid)}")
        else:
            print(f"🎫 {tid} has a golden ticket! It's a 💡 Idea: tick Start or drag it to 📏 Triage when it's ready: "
                  f"{f.path(tid)}")
    elif c == "block":
        f.block(a.id, a.blockers)
        print(f.summary(a.id))
    elif c == "unblock":
        f.unblock(a.id, a.blockers)
        print(f.summary(a.id))
    elif c == "set":
        f.set_field(a.id, a.field, a.value)
        print(f.summary(a.id))
    elif c == "triage":
        msg = f.triage(a.id, a.size, a.title, _questions(a) or None, duplicate_of=a.duplicate_of, dispatch=a.dispatch)
        print(msg or f.summary(a.id))
    elif c == "ask":
        qs = _questions(a)
        if not qs:
            raise WonkaError("ask needs at least one --question")
        msg = f.ask(a.id, qs, a.dispatch)
        print(msg or f.summary(a.id))
    elif c == "section":
        msg = f.write_spec(a.id, _text(a), a.dispatch)
        if msg:
            print(msg)
    elif c == "plan":
        msg = f.write_plan(a.id, _text(a), a.dispatch)
        print(msg or f"📋 {f.plan_path(a.id)}")
    elif c == "drafted":
        msg = f.drafted(a.id, a.size, [r for r in (a.reviewers or "").split(",") if r], a.dispatch,
                        split=a.split)
        print(msg or f.summary(a.id))
    elif c == "pr-body":
        msg = f.write_pr_body(a.id, _text(a), a.dispatch)
        print(msg or f"📝 {f.pr_body_path(a.id)}")
    elif c == "baked":
        print(f.baked(a.id, a.dispatch, flaky=a.flaky, disputes=a.dispute or ()) or f.summary(a.id))
    elif c == "resolved":
        print(f.resolved(a.id, a.dispatch) or f.summary(a.id))
    elif c == "report":
        if not a.stdin and not a.verdict:
            raise WonkaError("report needs --verdict good|bad")
        print(f.report(a.id, a.squirrel, _report(a), a.dispatch))
    elif c == "retry":
        print(f.retry(a.id))
    elif c == "finished":
        print(f.finished(a.id, a.agent, a.dispatch))
    elif c == "move":
        f.move(a.id, (_key(a.status, machine.STATUSES), _key(a.phase, machine.PHASES)), "human", a.why)
        print(f.summary(a.id))
    elif c == "tick":
        ensure_link()
        out = f.tick(drop_missing=a.drop_missing)
        store.log(*out["events"])
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif c == "show":
        print(json.dumps({**f.ticket(a.id), "note": str(f.path(a.id))}, indent=2, ensure_ascii=False))
    elif c == "repos":
        for project, repo, pr_repo in f.repos():
            print(f"{project:20} {repo}  {pr_repo or '-'}")
    elif c == "metrics":
        for m in f.metrics(a.since):
            print(json.dumps(m, ensure_ascii=False))
    elif c == "pr-comments":
        for row in prcomments.for_ticket(f.ticket(a.id), a.id):
            print(json.dumps(row, ensure_ascii=False))
    elif c == "session":
        tid = f.find(a.ref)
        brief = f.session(tid, force=a.force, from_branch=a.from_branch)
        t = f.ticket(tid)
        return {"id": tid, "brief": brief, "worktree": t["worktree"], "pr_url": t.get("pr_url"),
                "title": f.read(tid)[0].get("title") or tid}
    elif c == "worktree":
        tid = f.find(a.ref)
        print(f.worktree_info(tid) if a.info else f.worktree_path(tid))
    elif c == "status":
        for k in f.live():
            try:
                meta = f.read(k)[0]
            except (WonkaError, OSError) as e:  # a lost or broken note never hides the other tickets
                meta = {"title": f"⚠️ {e}"}
            print(f"{k}  {fmt(f.state(k)):34} {meta.get('size') or '-':3} {meta.get('title', '')}"
                  + ("  🕹️" if f.ticket(k).get("manual") else "")
                  + (f"  ⛓️ {','.join(w)}" if (w := f.waiting_on(k)) else ""))


def launch(a, cfg, s):
    """Open the session's Claude in the ticket's worktree: --print only prints the brief; the orca launcher opens an
    Orca tab there; the terminal launcher turns this process into claude (it never returns)."""
    if a.print:
        print(s["brief"])
        return
    how = "orca" if a.orca else "terminal" if a.terminal else cfg.get("session_launcher", "terminal")
    if how == "orca":
        _launch_orca(s)
        return
    if not shutil.which("claude"):
        raise WonkaError(f"claude is not on PATH: {s['id']} is manual now; run wonka session {s['id']} --print and "
                         "start claude in the worktree yourself")
    os.chdir(s["worktree"])
    os.execvp("claude", ["claude", s["brief"]])


def _launch_orca(s):
    orca = shutil.which("orca")
    if not orca:
        raise WonkaError(f"orca is not on PATH: {s['id']} is manual now; use --terminal or --print")
    wt = f"path:{s['worktree']}"
    label = [orca, "worktree", "set", "--worktree", wt, "--display-name", f"{s['id']} · {s['title']}"]
    pr = (s.get("pr_url") or "").rstrip("/").rsplit("/", 1)[-1]
    if pr.isdigit():
        label += ["--pr", pr]
    if subprocess.run(label, capture_output=True, text=True).returncode:  # only a label: the tab opens anyway
        print(f"⚠️ wonka: could not name the Orca workspace {s['worktree']}", file=sys.stderr)
    p = subprocess.run([orca, "agent", "launch", "--agent", "claude", "--worktree", wt, "--prompt", s["brief"],
                        "--focus"], capture_output=True, text=True)
    if p.returncode:
        raise WonkaError(f"orca agent launch failed: {(p.stderr or p.stdout).strip()}; {s['id']} is manual now, use "
                         "--terminal or --print")
    print(f"🕹️ {s['id']}: Claude is open in Orca, in {s['worktree']}")


def _key(value, table):
    key = machine.parse(value, table)
    if not key:
        raise WonkaError(f"unknown value {value!r}; use one of {', '.join(table)}")
    return key


FILE = "read the markdown from this file instead of stdin"
DISPATCH = "the dispatch number from the job JSON (lets the factory ignore an agent whose job was re-dispatched)"
# read a snapshot of the state, so they never wait for a long tick
READ_ONLY = ("status", "show", "repos", "metrics", "pr-comments", "worktree")


def _day(v):
    try:
        dt.date.fromisoformat(v)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD")
    return v


def parser():
    p = argparse.ArgumentParser(prog="wonka", description="🎩 Wonka's golden ticket factory")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="point the factory at an Obsidian vault")
    s.add_argument("--vault", required=True)
    s.add_argument("--folder", default="wonka")
    s.add_argument("--drafting", help="tickets the analyst works on at once; "
                                      "tickets waiting on your answers don't count (default 1)")
    s.add_argument("--baking", help="tickets the builder works on at once; "
                                    "reviewing and shipped tickets don't count (default 2)")
    s.add_argument("--triage", help="tickets the triage Oompa-Loompa sizes at once (default 2)")
    s.add_argument("--archive-days", help="days before finished tickets (Shipped or Wont Do) are archived (default 7)")
    s.add_argument("--stale-hours", help="hours before a running job is offered for retry (default 2)")
    s.add_argument("--max-review-rounds", help="review rounds with issues before a ticket is rolled away (default 3)")
    s = sub.add_parser("config", help="show settings, or change one: wonka config wip.baking 3")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s = sub.add_parser("add", help="drop a new golden ticket on the board as an 💡 Idea (or straight in Triage with --triage)")
    s.add_argument("idea")
    s.add_argument("--triage", action="store_true",
                   help="skip the Idea column: an agent triages it on the next tick (use when you want it built now; "
                        "analyst splits use it)")
    s.add_argument("--manual", action="store_true",
                   help="take it into a Claude session at once, as wonka session does (🕹️ manual: no agent works it "
                        "until you hand it back); open the session with wonka session <id>")
    s.add_argument("--title")
    s.add_argument("--repo", help="git repo the ticket builds in (default: cwd)")
    s.add_argument("--pr-repo", help="GitHub owner/name the PR targets (default: origin)")
    s.add_argument("--parent")
    s.add_argument("--size", choices=machine.SIZES)
    s.add_argument("--jira")
    s.add_argument("--review-spec", action="store_true",
                   help="a human approves the spec and plan before baking (spec_review: human)")
    s.add_argument("--blocked-by", action="append",
                   help="ids this ticket waits on (comma list or repeat): WONKA-00001,WONKA-00002")
    s = sub.add_parser("block", help="record that a ticket waits on other tickets (only before baking)")
    s.add_argument("id")
    s.add_argument("blockers", nargs="+", help="blocker ids, space or comma separated")
    s = sub.add_parser("unblock", help="remove blockers from a ticket; no ids clears them all")
    s.add_argument("id")
    s.add_argument("blockers", nargs="*")
    s = sub.add_parser("set", help="set title, size, jira, pr_repo or spec_review (agent|human)")
    s.add_argument("id")
    s.add_argument("field")
    s.add_argument("value")
    s = sub.add_parser("triage", help="triage: size a ticket in Triage")
    s.add_argument("id")
    s.add_argument("--size", required=True, choices=machine.SIZES)
    s.add_argument("--title")
    s.add_argument("--question", action="append", help='"Question? | option | *recommended option" (repeatable)')
    s.add_argument("--stdin", action="store_true", help="also read questions as JSON from stdin")
    s.add_argument("--duplicate-of", help="an existing ticket this one duplicates: closes it as Wont Do")
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("ask", help="analyst: ask a human, one --question per question")
    s.add_argument("id")
    s.add_argument("--question", action="append", help='"Question? | option | *recommended option" (repeatable)')
    s.add_argument("--stdin", action="store_true", help="also read questions as JSON from stdin")
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("section", help="analyst: write the ## 📜 Spec section (markdown) from --file or stdin")
    s.add_argument("id")
    s.add_argument("key", choices=["spec"])
    s.add_argument("--file", help=FILE)
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("plan", help="analyst: the detailed plan (markdown) from --file or stdin")
    s.add_argument("id")
    s.add_argument("--file", help=FILE)
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("drafted", help="analyst: spec and plan are done")
    s.add_argument("id")
    s.add_argument("--size", required=True, choices=machine.SIZES)
    s.add_argument("--reviewers", help="comma list: architect,backend,frontend,security,spark,airflow")
    s.add_argument("--split", action="store_true", help="the idea spans several repos: close it, one child ticket "
                                                        "per repo already added with wonka add --repo <path> --parent <id>")
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    sub.add_parser("repos", help="the repos the factory knows: project, local path, PR repo")
    s = sub.add_parser("metrics", help="one JSON line per ticket, archived ones too: outcome, hours per status, "
                                       "review rounds and stats")
    s.add_argument("--since", type=_day, help="only tickets created on or after this date (YYYY-MM-DD, UTC)")
    s = sub.add_parser("pr-comments", help="one JSON line per human comment, review and inline comment on a "
                                           "ticket's PR (owners, members and collaborators; no bots)")
    s.add_argument("id")
    s = sub.add_parser("pr-body", help="builder: the PR description (markdown) from --file or stdin, written from the "
                                       "repo's PR template")
    s.add_argument("id")
    s.add_argument("--file", help=FILE)
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("baked", help="builder: the code is committed")
    s.add_argument("id")
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s.add_argument("--flaky", metavar="WHY", help="builder, CI fix round only: the checks failed for an "
                   "infrastructure reason (lost runner, network, preemption); no new commits, back to the open PR")
    s.add_argument("--dispute", action="append",
                   help='"<issue> :: <why>": an issue from the last review round you decline to fix (repeatable)')
    s = sub.add_parser("resolved", help="fixer: the merge conflicts are committed")
    s.add_argument("id")
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("report", help="squirrel: file a review verdict")
    s.add_argument("id")
    s.add_argument("--squirrel", required=True)
    s.add_argument("--verdict", choices=("good", "bad"))
    s.add_argument("--issue", action="append", help='"file[:line]: problem → fix" (repeatable)')
    s.add_argument("--followup", action="append", help='"Title :: self-contained idea" (repeatable)')
    s.add_argument("--question", action="append", help='"Question? | option | *recommended option" (repeatable)')
    s.add_argument("--suggest", choices=("close", "draft"))
    s.add_argument("--why", help="why you suggest closing or re-drafting")
    s.add_argument("--stdin", action="store_true", help="read the whole report as JSON from stdin instead")
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("retry", help="give a stuck job back to the queue")
    s.add_argument("id")
    s = sub.add_parser("finished", help="manager: an agent finished; retry only if it left no result")
    s.add_argument("id")
    s.add_argument("--agent", required=True, help="the agent dispatched, e.g. wonka:squirrel-backend")
    s.add_argument("--dispatch", type=int, required=True, help=DISPATCH)
    s = sub.add_parser("move", help="a manual move, still checked by the state machine")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("phase")
    s.add_argument("--why")
    s = sub.add_parser("tick", help="reconcile the factory and hand out jobs (JSON)")
    s.add_argument("--drop-missing", action="store_true",
                   help="drop every live ticket whose note is gone, even when all of them are (normally refused)")
    s = sub.add_parser("session", help="take a ticket into a Claude session (🕹️ manual: no agent works it until you "
                                       "hand it back) and open Claude in its worktree with what the factory did")
    s.add_argument("ref", help="the ticket: WONKA-00012, or the Jira key in its note")
    s.add_argument("--print", action="store_true", help="only print the brief (what /wonka session uses)")
    how = s.add_mutually_exclusive_group()
    how.add_argument("--terminal", action="store_true", help="run claude here, in the worktree")
    how.add_argument("--orca", action="store_true", help="open claude in an Orca tab, in the worktree")
    s.add_argument("--force", action="store_true", help="take it even while an agent works on it (its results are "
                                                        "then ignored)")
    s.add_argument("--from", dest="from_branch", metavar="BRANCH",
                   help="start the ticket's branch from this branch's tip (work begun by hand elsewhere); only while "
                        "the ticket's branch has no commits of its own")
    s = sub.add_parser("worktree", help="where a ticket's worktree is (its path; --info for branch, commits, changes, "
                                        "state and PR)")
    s.add_argument("ref", help="the ticket: WONKA-00012, or the Jira key in its note")
    s.add_argument("--info", action="store_true")
    s = sub.add_parser("show", help="a ticket's JSON state")
    s.add_argument("id")
    sub.add_parser("status", help="one line per live ticket")
    sub.add_parser("doctor", help="check the factory's setup")
    sub.add_parser("git", help="agents: run git in a ticket's worktree: wonka git <id> [--dispatch <n>] "
                               "status|diff|log|show|rev-parse|ls-files|blame|add|commit|rebase ...")
    sub.add_parser("test", help="agents: run a project's test command under a per-project lock: "
                                "wonka test <id> [--dispatch <n>] -- <command> ...")
    sub.add_parser("ci-logs", help="agents: fetch a failed CI log with the project's log tool, only from its CI "
                                   "hosts: wonka ci-logs <id> --dispatch <n> <url>")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        store.home()  # a refused WONKA_HOME stops here, before anything (the log too) is read or written
    except WonkaError as e:
        print(f"🗑️ wonka: {e}", file=sys.stderr)
        return 1
    if argv[:1] and argv[0] in PASSTHROUGH:
        try:
            run_main = importlib.import_module(f".{PASSTHROUGH[argv[0]]}", __package__).main
            return run_main(argv[1:])
        except WonkaError as e:
            store.try_log(f"{argv[0]} {' '.join(argv[1:3])}: {e}")  # wonka test runs sandboxed, ~/.wonka read-only
            print(f"🗑️ wonka: {e}", file=sys.stderr)
            if isinstance(e, store.CriticalError):
                alert(str(e))
            return 1
    a = parser().parse_args(argv)
    try:
        if a.cmd == "init":
            cmd_init(a)
            return 0
        if a.cmd == "config":
            cmd_config(a)
            return 0
        if a.cmd == "doctor":
            from .doctor import doctor
            return doctor()
        cfg = store.load_config()
        if a.cmd in READ_ONLY:
            run(a, Factory(cfg, store.snapshot()))
            return 0
        # a tick reads GitHub and fetches before it takes the lock, so the agents' commands never wait on the network
        fetched = Factory(cfg, store.snapshot()).prefetch() if a.cmd == "tick" else None
        with store.state() as db:
            out = run(a, Factory(cfg, db, fetched))
        if a.cmd == "session":
            launch(a, cfg, out)  # after the lock: the session can run for hours
        return 0
    except WonkaError as e:
        store.log(f"{a.cmd} {getattr(a, 'id', '')}: {e}".replace("  ", " "))
        print(f"🗑️ wonka: {e}", file=sys.stderr)
        if isinstance(e, store.CriticalError):
            alert(str(e))
        return 1
