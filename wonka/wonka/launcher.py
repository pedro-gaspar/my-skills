"""Workers: with `launcher: claude`, wonka tick runs each job's agents itself, as detached headless `claude -p`
processes, and a worker reports back when its claude exits, so no Claude session has to dispatch or collect them.

The run registry (~/.wonka/runs) holds one record per worker not yet reported: <id>-d<dispatch>-<agent>.run.json
({id, agent, dispatch, pid, log, started_at}), next to the worker's .prompt (claude's stdin), .log.json (claude's
stdout) and .err (stderr). Records are created, read and deleted only under store.state(); a worker writes only its
own log files."""

import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import store
from .factory import Factory, _age, _clip, _iso, now
from .store import WonkaError

# The claude program: tests patch this, never an environment variable (as factory.GH)
CLAUDE = "claude"
WONKA = Path(__file__).resolve().parent.parent / "bin" / "wonka"
# The user's global hooks (desktop state, session start) must not fire for every worker
HOOKS_OFF = '{"disableAllHooks":true}'
# A record gets its pid just after the tick's lock is released: until then it counts as running, for this long
LAUNCH_GRACE = dt.timedelta(seconds=60)
RESULT_MAX = 150
PROC = Path("/proc")


def runs_dir():
    return store.home() / "runs"


def floor():
    """Where the workers' claude runs: a neutral folder, never a repo."""
    return store.home() / "floor"


def _paused_flag():
    return store.home() / "paused"


def paused():
    return _paused_flag().exists()


def pause():
    """Stop finished workers from turning the factory: they only report back, so no new job starts."""
    _paused_flag().touch()
    running = len(list(runs_dir().glob("*.run.json"))) if runs_dir().exists() else 0
    return f"⏸️ factory paused: {running} worker(s) still finishing, they start nothing new; wonka tick resumes it"


def _resume():
    """True when the factory was paused."""
    try:
        _paused_flag().unlink()
    except FileNotFoundError:
        return False
    return True


def short(agent):
    return agent.removeprefix("wonka:").removeprefix("oompa-loompa-")


def _sibling(run, suffix):
    return run.with_name(run.name.removesuffix(".run.json") + suffix)


def _read(run):
    try:
        return json.loads(run.read_text())
    except FileNotFoundError:
        raise WonkaError(f"{run}: no such run record (already reported, or never launched)")
    except ValueError as e:
        raise WonkaError(f"{run} is not a run record ({e}): delete it")


def _write(run, rec):
    store.write_atomic(run, json.dumps(rec, indent=2, ensure_ascii=False) + "\n")


def _records():
    return [(run, _read(run)) for run in sorted(runs_dir().glob("*.run.json"))]


def alive(run, rec):
    """True while the record's worker runs. The pid alone is not enough: after a reboot it can be another process,
    so its command line must name this run file."""
    if rec["pid"] is None:
        return _age(rec["started_at"]) < LAUNCH_GRACE
    try:
        os.kill(rec["pid"], 0)
    except OSError:
        return False
    return os.fsencode(str(run)) in _cmdline(rec["pid"])


def _cmdline(pid):
    """The process's command line, b"" once it is gone. macOS has no /proc, so ask ps there."""
    if (PROC / "self").exists():
        try:
            return (PROC / str(pid) / "cmdline").read_bytes()
        except OSError:
            return b""
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True).stdout


def _outcome(log):
    """What the worker's claude said, in one line: its cost and the start of its result."""
    try:
        out = json.loads(Path(log).read_text())
        text = _clip(out.get("result") or out.get("subtype") or "", RESULT_MAX)
        cost = f"${out['total_cost_usd']:.2f} · " if "total_cost_usd" in out else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return "no output"
    return f"{'⚠️ error · ' if out.get('is_error') else ''}{cost}{text}" if text else "no output"


def reap(f, own=None):
    """Report every worker that has ended (and `own`, the caller's, which still runs) with finished(), then drop its
    record. Call under store.state(): the record is gone before the lock is, so no worker is reported twice."""
    events = []
    for run, rec in _records():
        if run != own and alive(run, rec):
            continue
        try:
            msg = f.finished(rec["id"], rec["agent"], rec["dispatch"])
        except (WonkaError, OSError) as e:  # a ticket that is gone must not keep its record forever
            msg = f"{rec['id']}: ⚠️ {rec['agent']} ended, not recorded: {e}"
        events.append(f"{msg} · {_outcome(rec['log'])}")
        run.unlink()
    return events


def _worker_groups():
    """The process groups of every worker of this factory, found by command line (ps works on Linux and macOS), so a
    worker whose pid is not recorded yet is found too. A worker leads its own group, which holds its claude."""
    out = subprocess.run(["ps", "-axo", "pgid=,command="], capture_output=True, text=True, check=True).stdout
    mark = str(runs_dir())
    return {int(line.split(None, 1)[0]) for line in out.splitlines() if mark in line and " worker " in line}


def hard_stop(cfg):
    """Pause the factory, kill every worker with all it runs, and put their jobs back in the queue without using up a
    try. Workers that had already ended are reported as a reap would."""
    _paused_flag().touch()
    events = ["⏸️ factory paused: wonka tick resumes it"]
    with store.state() as db:
        f = Factory(cfg, db)
        groups = _worker_groups()
        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        killed = []
        for run, rec in _records():
            if rec["pid"] is None or rec["pid"] in groups:
                killed.append(rec)
                run.unlink()
        events += [f"{rec['id']}: 💀 {short(rec['agent'])} killed (dispatch {rec['dispatch']})" for rec in killed]
        events += [f._safe(tid, lambda k: f.retry(k, "killed by hard stop", counted=False))
                   for tid in sorted({rec["id"] for rec in killed})]
        events += reap(f)
    store.log(*events)
    return events


def reap_now(cfg, own=None):
    with store.state() as db:
        events = reap(Factory(cfg, db), own)
    store.log(*events)
    return events


def _retry_stale(f, stale):
    """Put the stale jobs no worker runs back in the queue (a worker lost with its record, a job from a session)."""
    running = {rec["id"] for _, rec in _records()}
    return [f._safe(k, lambda k: f.retry(k, "the job went stale and no worker runs it"))
            for k in stale if k not in running]


def plan(f, jobs):
    """Write a prompt and a run record for each agent of each job. Call under store.state(), with the jobs it just
    claimed. Returns (events, the run files to spawn)."""
    if not jobs:
        return [], []
    if not shutil.which(CLAUDE):
        return [f._safe(job["id"], lambda k: f"{k}: ⚠️ claude is not on PATH, no agent launched: "
                        + f.retry(k, "claude is not on PATH").split(": ", 1)[1]) for job in jobs], []
    events, runs = [], []
    runs_dir().mkdir(parents=True, exist_ok=True)
    for job in jobs:
        prompt = json.dumps(job, indent=2, ensure_ascii=False) + f"\nWork golden ticket {job['id']} ({job['job']}).\n"
        for agent in job["agents"]:
            run = runs_dir() / f"{job['id']}-d{job['dispatch']}-{short(agent)}.run.json"
            _sibling(run, ".prompt").write_text(prompt)
            _write(run, {"id": job["id"], "agent": agent, "dispatch": job["dispatch"], "pid": None,
                         "log": str(_sibling(run, ".log.json")), "started_at": _iso(now())})
            runs.append(run)
            events.append(f"{job['id']}: 🚀 {short(agent)} launched (dispatch {job['dispatch']})")
    return events, runs


def spawn(runs):
    """Start a detached worker per run file, then record their pids. Call after the tick's lock is released: the
    state the workers act on is saved by then, and a worker never waits on the lock that started it."""
    if not runs:
        return
    floor().mkdir(parents=True, exist_ok=True)
    pids = {}
    try:
        for run in runs:
            with open(_sibling(run, ".err"), "a") as err:
                pids[run] = subprocess.Popen([sys.executable, str(WONKA), "worker", str(run)], cwd=floor(),
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                             start_new_session=True).pid
    except OSError as e:
        raise WonkaError(f"could not start a worker ({e}): its job is retried in {LAUNCH_GRACE.seconds} s")
    finally:
        with store.state():
            for run, pid in pids.items():
                if run.exists():
                    _write(run, {**_read(run), "pid": pid})


def tick(cfg, drop_missing=False, own=None, resume=True):
    """One turn of the factory, as `wonka tick` and every finished worker run it. With `launcher: claude` the jobs
    are started here as workers and `jobs` comes back empty; with `session` they are returned for the manager."""
    events = reap_now(cfg, own)
    if own is None and resume and _resume():  # only a tick someone ran resumes a paused factory
        events.append("▶️ factory resumed")
    # GitHub is read and the bases fetched before the lock is taken, so the agents' commands never wait on the network
    fetched = Factory(cfg, store.snapshot()).prefetch()
    runs = []
    with store.state() as db:
        f = Factory(cfg, db, fetched)
        out = f.tick(drop_missing=drop_missing)
        if cfg["launcher"] == "claude":
            launched, runs = plan(f, out["jobs"])
            out["events"] += _retry_stale(f, out["stale"]) + launched
            out["jobs"] = []
        store.log(*out["events"])
    spawn(runs)
    return {**out, "events": events + out["events"], "launcher": cfg["launcher"]}


def _claude(rec):
    return [CLAUDE, "-p", "--agent", rec["agent"], "--permission-mode", "auto", "--permission-prompts", "none",
            "--output-format", "json", "--settings", HOOKS_OFF, "--add-dir", str(store.worktrees_dir()),
            "--add-dir", str(store.drafts_dir()), "-n", f"{rec['id']} {short(rec['agent'])}"]


def work(run):
    """A worker: run the record's agent in a headless claude, then report it and turn the factory once, so the
    ticket moves on and the next jobs start with no session involved."""
    run = Path(run)
    with store.state():
        rec = _read(run)
    try:
        floor().mkdir(parents=True, exist_ok=True)
        # the prompt goes in on stdin: --add-dir takes every argument after it, a prompt included
        with open(_sibling(run, ".prompt")) as prompt, open(rec["log"], "w") as log:
            subprocess.run(_claude(rec), stdin=prompt, stdout=log, cwd=floor())
    except OSError as e:
        store.log(f"{rec['id']}: ⚠️ could not run claude for {rec['agent']}: {e}")
    cfg = store.load_config()  # read now: the launcher may have changed while claude ran
    if cfg["launcher"] == "claude" and not paused():
        tick(cfg, own=run)
    else:  # paused, or a tick here would claim jobs that only the session can dispatch
        reap_now(cfg, own=run)
