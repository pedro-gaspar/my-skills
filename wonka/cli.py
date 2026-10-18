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
                       ("archive_after_days", a.archive_days), ("stale_hours", a.stale_hours)):
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
    print(f"🎩 “Come with me, and you'll be in a world of pure imagination.”\n   Board {base}\n   State {store.home()}")


def show_config(cfg):
    print(f"🧪 drafting at once : {cfg['wip']['drafting']}\n"
          f"🍫 baking at once   : {cfg['wip']['baking']}\n"
          f"🗄️ archive after    : {cfg['archive_after_days']} days\n"
          f"⏳ retry jobs after : {cfg['stale_hours']} hours")


def cmd_config(a):
    cfg = store.load_config()
    if a.key:
        if a.value is None:
            raise WonkaError("usage: wonka config <key> <value>")
        store.set_setting(cfg, a.key, a.value)
        store.save_config(cfg)
    show_config(cfg)


def _stdin_json():
    return json.loads(sys.stdin.read())


def run(a, f):
    c = a.cmd
    if c == "add":
        tid = f.add(a.idea, a.title, a.repo, a.pr_repo, a.parent, a.size, a.jira)
        print(f"🎫 {tid} has a golden ticket! It's in the ✉️ Inbox: {f.path(tid)}")
    elif c == "set":
        f.set_field(a.id, a.field, a.value)
    elif c == "triage":
        f.triage(a.id, a.size, a.title, _stdin_json() if a.questions else None)
        print(f.summary(a.id))
    elif c == "ask":
        f.ask(a.id, _stdin_json())
        print(f.summary(a.id))
    elif c == "section":
        f.set_section(a.id, a.key, sys.stdin.read())
    elif c == "plan":
        f.write_plan(a.id, sys.stdin.read())
        print(f"📋 {f.plan_path(a.id)}")
    elif c == "drafted":
        f.drafted(a.id, a.size, [r for r in (a.reviewers or "").split(",") if r])
        print(f.summary(a.id))
    elif c == "baked":
        f.baked(a.id)
        print(f.summary(a.id))
    elif c == "report":
        print(f.report(a.id, a.squirrel, _stdin_json()))
    elif c == "retry":
        print(f.retry(a.id))
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
    s.add_argument("--drafting", help="tickets drafted at the same time (default 1)")
    s.add_argument("--baking", help="tickets baked at the same time (default 2)")
    s.add_argument("--archive-days", help="days before Done tickets are archived (default 7)")
    s.add_argument("--stale-hours", help="hours before a running job is offered for retry (default 2)")
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
    s = sub.add_parser("triage", help="grandpa-joe: size an Inbox ticket")
    s.add_argument("id")
    s.add_argument("--size", required=True, choices=machine.SIZES)
    s.add_argument("--title")
    s.add_argument("--questions", action="store_true", help="read questions JSON from stdin")
    s = sub.add_parser("ask", help="charlie: questions JSON on stdin")
    s.add_argument("id")
    s = sub.add_parser("section", help="write a ## section from stdin (e.g. spec)")
    s.add_argument("id")
    s.add_argument("key")
    s = sub.add_parser("plan", help="charlie: the detailed plan (markdown) on stdin")
    s.add_argument("id")
    s = sub.add_parser("drafted", help="charlie: spec and plan are done")
    s.add_argument("id")
    s.add_argument("--size", required=True, choices=machine.SIZES)
    s.add_argument("--reviewers", help="comma list: architect,backend,frontend,security")
    s = sub.add_parser("baked", help="oompa-loompa: the code is committed")
    s.add_argument("id")
    s = sub.add_parser("report", help="squirrel: review report JSON on stdin")
    s.add_argument("id")
    s.add_argument("--squirrel", required=True)
    s = sub.add_parser("retry", help="give a stuck job back to the queue")
    s.add_argument("id")
    s = sub.add_parser("move", help="a manual move, still checked by the state machine")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("phase")
    s.add_argument("--why")
    sub.add_parser("tick", help="reconcile the board and hand out jobs (JSON)")
    s = sub.add_parser("show", help="a ticket's JSON state")
    s.add_argument("id")
    sub.add_parser("status", help="one line per live ticket")
    sub.add_parser("doctor", help="check the factory's setup")
    return p


def main(argv=None):
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
