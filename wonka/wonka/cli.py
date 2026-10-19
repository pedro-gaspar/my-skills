"""wonka: the factory's command line. Agents and the /wonka skill drive tickets only through this."""

import argparse
import copy
import json
import sys
from pathlib import Path

from . import machine, store
from .factory import Factory, fmt
from .store import WonkaError

ROOT = Path(__file__).resolve().parent.parent
LINK = Path.home() / ".local" / "bin" / "wonka"


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
    try:
        cfg = store.load_config()
    except WonkaError:
        cfg = copy.deepcopy(store.DEFAULT_CONFIG)
    cfg["vault"], cfg["folder"] = str(Path(a.vault).expanduser().resolve()), a.folder
    for key, value in (("wip.drafting", a.drafting), ("wip.baking", a.baking),
                       ("archive_after_days", a.archive_days), ("stale_hours", a.stale_hours),
                       ("max_review_rounds", a.max_review_rounds)):
        if value is not None:
            store.set_setting(cfg, key, value)
    store.save_config(cfg)
    for d in (store.tickets_dir, store.plans_dir, store.archive_dir):
        d(cfg).mkdir(parents=True, exist_ok=True)
    base = store.board(cfg) / "Wonka.base"
    if not base.exists():
        base.write_text((ROOT / "templates" / "Wonka.base").read_text().replace("{{folder}}", a.folder))
    ensure_link()
    show_config(cfg)
    print(f"🎩 “Come with me, and you'll be in a world of pure imagination.”\n   🍫 Factory {base}\n   State {store.home()}")


def show_config(cfg):
    print(f"🧪 drafting at once : {cfg['wip']['drafting']}\n"
          f"🍫 baking at once   : {cfg['wip']['baking']}\n"
          f"🗄️ archive after    : {cfg['archive_after_days']} days\n"
          f"⏳ retry jobs after : {cfg['stale_hours']} hours\n"
          f"🐿️ review rounds    : {cfg['max_review_rounds']}")


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


def parse_question(text):
    """'Store sessions where? | Redis | *Postgres, already in the stack' → a question; * marks the recommendation."""
    q, *opts = [part.strip() for part in text.split("|")]
    if not q or len(opts) < 2:
        raise WonkaError(f"--question needs a question and at least two options: {text!r}")
    rec = next((i for i, o in enumerate(opts) if o.startswith("*")), None)
    return {"q": q, "options": [o.lstrip("*").strip() for o in opts], "recommended": rec}


def _questions(a):
    """Questions come as --question flags (what agents use) or, with --stdin, as JSON."""
    qs = [parse_question(x) for x in a.question or []]
    return qs + (_stdin_json() if getattr(a, "stdin", False) else [])


def _report(a):
    if a.stdin:
        return _stdin_json()
    rep = {"verdict": "good nut" if a.verdict == "good" else "bad nut", "issues": a.issue or [],
           "questions": [parse_question(x) for x in a.question or []], "suggest": a.suggest, "why": a.why or ""}
    for fu in a.followup or []:
        title, sep, idea = fu.partition("::")
        rep.setdefault("followups", []).append({"title": title.strip(), "idea": (idea if sep else title).strip()})
    return rep


def run(a, f):
    c = a.cmd
    if c == "add":
        tid = f.add(a.idea, a.title, a.repo, a.pr_repo, a.parent, a.size, a.jira)
        print(f"🎫 {tid} has a golden ticket! It's in the ✉️ Inbox: {f.path(tid)}")
    elif c == "set":
        f.set_field(a.id, a.field, a.value)
        print(f.summary(a.id))
    elif c == "triage":
        f.triage(a.id, a.size, a.title, _questions(a) or None)
        print(f.summary(a.id))
    elif c == "ask":
        qs = _questions(a)
        if not qs:
            raise WonkaError("ask needs at least one --question")
        msg = f.ask(a.id, qs, a.dispatch)
        print(msg or f.summary(a.id))
    elif c == "section":
        msg = f.set_section(a.id, a.key, sys.stdin.read(), a.dispatch)
        if msg:
            print(msg)
    elif c == "plan":
        msg = f.write_plan(a.id, sys.stdin.read(), a.dispatch)
        print(msg or f"📋 {f.plan_path(a.id)}")
    elif c == "drafted":
        msg = f.drafted(a.id, a.size, [r for r in (a.reviewers or "").split(",") if r], a.dispatch)
        print(msg or f.summary(a.id))
    elif c == "baked":
        print(f.baked(a.id, a.dispatch) or f.summary(a.id))
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
        out = f.tick()
        store.log(*out["events"])
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif c == "show":
        print(json.dumps({**f.ticket(a.id), "note": str(f.path(a.id))}, indent=2, ensure_ascii=False))
    elif c == "status":
        for k in f.live():
            meta = f.read(k)[0]
            print(f"{k}  {fmt(f.state(k)):34} {meta.get('size') or '-':3} {meta.get('title', '')}")


def _key(value, table):
    key = machine.parse(value, table)
    if not key:
        raise WonkaError(f"unknown value {value!r}; use one of {', '.join(table)}")
    return key


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
    s.add_argument("--archive-days", help="days before Done tickets are archived (default 7)")
    s.add_argument("--stale-hours", help="hours before a running job is offered for retry (default 2)")
    s.add_argument("--max-review-rounds", help="review rounds with issues before a ticket is rolled away (default 3)")
    s = sub.add_parser("config", help="show settings, or change one: wonka config wip.baking 3")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s = sub.add_parser("add", help="drop a new golden ticket in the Inbox")
    s.add_argument("idea")
    s.add_argument("--title")
    s.add_argument("--repo", help="git repo the ticket builds in (default: cwd)")
    s.add_argument("--pr-repo", help="GitHub owner/name the PR targets (default: origin)")
    s.add_argument("--parent")
    s.add_argument("--size", choices=machine.SIZES)
    s.add_argument("--jira")
    s = sub.add_parser("set", help="set title, size, jira or pr_repo")
    s.add_argument("id")
    s.add_argument("field")
    s.add_argument("value")
    s = sub.add_parser("triage", help="triage: size an Inbox ticket")
    s.add_argument("id")
    s.add_argument("--size", required=True, choices=machine.SIZES)
    s.add_argument("--title")
    s.add_argument("--question", action="append", help='"Question? | option | *recommended option" (repeatable)')
    s.add_argument("--stdin", action="store_true", help="also read questions as JSON from stdin")
    s = sub.add_parser("ask", help="analyst: ask a human, one --question per question")
    s.add_argument("id")
    s.add_argument("--question", action="append", help='"Question? | option | *recommended option" (repeatable)')
    s.add_argument("--stdin", action="store_true", help="also read questions as JSON from stdin")
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
    s = sub.add_parser("section", help="write a ## section from stdin (e.g. spec)")
    s.add_argument("id")
    s.add_argument("key")
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
    s = sub.add_parser("plan", help="analyst: the detailed plan (markdown) on stdin")
    s.add_argument("id")
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
    s = sub.add_parser("drafted", help="analyst: spec and plan are done")
    s.add_argument("id")
    s.add_argument("--size", required=True, choices=machine.SIZES)
    s.add_argument("--reviewers", help="comma list: architect,backend,frontend,security,spark,airflow")
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
    s = sub.add_parser("baked", help="builder: the code is committed")
    s.add_argument("id")
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
    s = sub.add_parser("resolved", help="fixer: the merge conflicts are committed")
    s.add_argument("id")
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
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
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
    s = sub.add_parser("retry", help="give a stuck job back to the queue")
    s.add_argument("id")
    s = sub.add_parser("finished", help="manager: an agent finished; retry only if it left no result")
    s.add_argument("id")
    s.add_argument("--agent", required=True, help="the agent dispatched, e.g. wonka:squirrel-backend")
    s.add_argument("--dispatch", type=int, help="the dispatch number from the job JSON")
    s = sub.add_parser("move", help="a manual move, still checked by the state machine")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("phase")
    s.add_argument("--why")
    sub.add_parser("tick", help="reconcile the factory and hand out jobs (JSON)")
    s = sub.add_parser("show", help="a ticket's JSON state")
    s.add_argument("id")
    sub.add_parser("status", help="one line per live ticket")
    sub.add_parser("doctor", help="check the factory's setup")
    sub.add_parser("git", help="agents: run git in a ticket's worktree: wonka git <id> "
                               "status|diff|log|show|rev-parse|ls-files|blame|add|commit|merge ...")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["git"]:  # by hand: argparse would mangle `--` and the options meant for git
        try:
            from .gitwrap import main as git_main
            return git_main(argv[1:])
        except WonkaError as e:
            store.log(f"git {' '.join(argv[1:3])}: {e}")
            print(f"🫐 wonka: {e}", file=sys.stderr)
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
        with store.state() as db:
            run(a, Factory(cfg, db))
        return 0
    except WonkaError as e:
        store.log(f"{a.cmd} {getattr(a, 'id', '')}: {e}".replace("  ", " "))
        print(f"🫐 wonka: {e}", file=sys.stderr)
        return 1
