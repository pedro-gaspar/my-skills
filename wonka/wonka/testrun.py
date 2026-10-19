"""wonka test: run a project's test command in a ticket's worktree, one run per project at a time."""

import contextlib
import datetime
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import gitwrap, store
from .factory import Factory, project_test_prefixes
from .store import WonkaError

USAGE = "usage: wonka test <id> [--dispatch <n>] -- <command> [args...]"
EX_TEMPFAIL = 75
POLL = 0.5


def _parse(argv):
    """argv is everything after `wonka test`: <id> [--dispatch <n>] -- <command> [args...]."""
    tid, dispatch, rest = gitwrap.parse_id(argv, USAGE)
    if rest[:1] != ["--"] or len(rest) < 2:
        raise WonkaError(USAGE)
    return tid, dispatch, rest[1:]


# Programs that run whatever code or program they are given.
RUNNERS = frozenset(
    "sh bash zsh dash ksh fish csh tcsh "
    "python pypy ruby perl node deno bun php lua Rscript osascript pwsh powershell tclsh "
    "env exec command xargs nice nohup timeout time sudo doas npx pnpx bunx uvx".split())
# Launchers whose subcommand runs any command.
RUNNER_SUBCOMMANDS = frozenset({
    ("uv", "run"), ("poetry", "run"), ("pipenv", "run"), ("npm", "exec"), ("pnpm", "exec"), ("pnpm", "dlx"),
    ("yarn", "exec"), ("yarn", "dlx"), ("bundle", "exec"), ("hatch", "run"), ("pdm", "run")})
# Runners whose first operand is another command.
WRAPPERS = frozenset("env exec command xargs nice nohup timeout time sudo doas".split())
# Options that take the next token as their value (not the program or code to run), by program.
OPTION_VALUES = {
    "python": frozenset({"-W", "-X"}),
    "pypy": frozenset({"-W", "-X"}),
    "node": frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader", "--title"}),
    "sh": frozenset({"-o", "-O"}),
    "bash": frozenset({"-o", "-O"}),
    "zsh": frozenset({"-o", "-O"}),
    "dash": frozenset({"-o", "-O"}),
    "ksh": frozenset({"-o", "-O"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group"}),
    "doas": frozenset({"-u"}),
    "xargs": frozenset({"-n", "-I", "-P", "-L", "-d", "-s", "-E", "-a"}),
    "uv": frozenset({"--with", "--python", "-p", "--project", "--directory", "--package", "--extra", "--group"}),
}
# Positionals before the wrapped command (timeout DURATION COMMAND).
LEADING_ARGS = {"timeout": 1}
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
OPEN_WARNING = ("lets an agent run any program or inline code outside the sandbox through wonka test; "
                "pin it to a script or module (e.g. ./test.sh, python3 -m pytest)")


def _program(token):
    """The program name of a token: basename without a trailing version (python3.12 -> python)."""
    name = os.path.basename(token)
    return re.sub(r"[\d.]+$", "", name) or name


def _operands(program, tokens):
    """tokens from the first operand on: leading options (with their values), `--` and, for env, assignments dropped."""
    values = OPTION_VALUES.get(program, ())
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            i += 1
            break
        if t.startswith("-"):
            i += 2 if t in values else 1
        elif program == "env" and ENV_ASSIGNMENT.match(t):
            i += 1
        else:
            break
    rest = tokens[i:]
    lead = LEADING_ARGS.get(program, 0)
    if rest and lead:
        rest = rest[lead:]
    return rest


def _open(tokens):
    if len(tokens) >= 2 and (_program(tokens[0]), tokens[1]) in RUNNER_SUBCOMMANDS:
        program, rest, wrapper = _program(tokens[0]), tokens[2:], True
    elif _program(tokens[0]) in RUNNERS:
        program, rest = _program(tokens[0]), tokens[1:]
        wrapper = program in WRAPPERS
    else:
        return False
    ops = _operands(program, rest)
    if not ops:
        return True
    return wrapper and _open(ops)


def open_prefix(prefix):
    """True when prefix is a shell, interpreter or launcher with nothing pinned after it (python3 -c, bash, env).

    Option values (python3 -W ignore), env assignments (env FOO=1) and timeout's duration do not pin it; a
    wrapper (env, timeout, uv run, ...) is open when the command it wraps is (env FOO=1 python3 -c)."""
    try:
        tokens = shlex.split(prefix)
    except ValueError:
        return False
    if not tokens:
        return False
    return _open(tokens)


def open_prefixes(cfg, project=None):
    """(project, prefix) for each open test_commands prefix, sorted by project (only `project` when given)."""
    projects = sorted(cfg.get("test_commands") or {}) if project is None else [project]
    return [(proj, p) for proj in projects for p in project_test_prefixes(cfg, proj) if open_prefix(p)]


def check(argv, allowed, project):
    """Only a command that starts with one of the project's configured prefixes (whole tokens) may run."""
    for p in allowed:
        tokens = shlex.split(p)
        if tokens and argv[:len(tokens)] == tokens:
            return
    if not allowed:
        raise WonkaError(f"{project} has no test commands: a human adds them with "
                         f"wonka config test_commands.{project} ./test.sh")
    raise WonkaError(f"{shlex.join(argv)} is not a test command of {project}; allowed prefixes: {', '.join(allowed)}")


def lock_path(project):
    return store.home() / "locks" / f"{project}-tests.lock"


def _holder(fh):
    try:
        fh.seek(0)
        holder = json.loads(fh.read())
    except (ValueError, OSError):
        return None
    return holder if isinstance(holder, dict) else None


def _busy_message(holder):
    try:
        hhmm = datetime.datetime.fromisoformat(holder["started"]).strftime("%H:%M")
        return f"tests busy: {holder['id']} has held the lock since {hhmm}, run it again"
    except (KeyError, TypeError, ValueError):
        return "tests busy: another run holds the lock, run it again"


def acquire(fh, wait_seconds):
    """Take the exclusive lock, polling until the deadline (like store._lock). False when it stayed busy."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(POLL, remaining))


def main(argv):
    """argv is everything after `wonka test`; returns the command's exit code (75 when the lock stayed busy)."""
    tid, dispatch, cmd = _parse(argv)
    cfg = store.load_config()
    # a snapshot, never the state lock, so a test run never blocks a tick and a tick never blocks a run
    f = Factory(cfg, store.snapshot())
    t = f.ticket(tid)
    project = t["project"]
    check(cmd, project_test_prefixes(cfg, project), project)
    path = gitwrap.target(t, f.state(tid), "status")
    if Path(path).resolve() == Path(t["repo"]).resolve():
        raise WonkaError(f"no worktree or scout for {tid}: wonka test only runs in a ticket's worktree or scout")
    lp = lock_path(project)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "a+") as fh:  # never "w": that would wipe the current holder's info before we even try to lock
        if not acquire(fh, float(cfg["test_lock_wait_minutes"]) * 60):
            msg = _busy_message(_holder(fh))
            store.log(f"test {tid}: {msg}")
            print(f"🗑️ wonka: {msg}", file=sys.stderr)
            return EX_TEMPFAIL
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"id": tid, "dispatch": dispatch,
                             "started": datetime.datetime.now().isoformat(timespec="seconds")}))
        fh.flush()
        try:
            try:
                rc = subprocess.run(cmd, cwd=path, stdin=subprocess.DEVNULL).returncode
            except FileNotFoundError:
                raise WonkaError(f"{cmd[0]}: command not found in {path}")
            except PermissionError:
                raise WonkaError(f"{cmd[0]} is not executable")
            return 128 - rc if rc < 0 else rc
        finally:
            with contextlib.suppress(OSError):  # clear the holder while we still hold the lock, so no waiter reads it stale
                fh.seek(0)
                fh.truncate()
                fh.flush()
