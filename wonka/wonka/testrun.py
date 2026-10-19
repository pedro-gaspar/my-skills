"""wonka test: run a project's test command in a ticket's worktree, one run per project at a time."""

import contextlib
import datetime
import fcntl
import json
import os
import re
import shlex
import stat
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


# A test_commands prefix is open when what an agent appends to it can pick the program or the inline code to run,
# rather than only pass arguments to the program, script or target the prefix names:
# - shells and interpreters (RUNNERS) are open until a script, module or code operand pins them (python3 -m pytest);
# - wrappers and launchers (WRAPPERS, RUNNER_SUBCOMMANDS) are open when the command they wrap is (env FOO=1 bash), and
#   a launcher with no subcommand is open too (uv, npm, cargo: the caller could add run or exec);
# - ALWAYS_OPEN programs take a command anywhere in their arguments (find -exec, watch, script -c): nothing pins them;
# - build and test tools that run the repo's own files are not open, even bare: make, npm test, cargo test and pytest
#   run what the Makefile, package.json or conftest.py say, files the builder can edit anyway, like ./test.sh.
# This only catches the obvious: with Claude Code's sandbox on, wonka test runs inside it whatever the prefix.
RUNNERS = frozenset(
    "sh bash zsh dash ksh fish csh tcsh busybox "
    "python pypy ruby perl node deno bun php lua Rscript osascript pwsh powershell tclsh awk gawk mawk nawk java julia "
    "elixir env exec command xargs nice ionice nohup timeout time sudo doas setsid setpriv stdbuf flock unshare "
    "nsenter chroot taskset chrt strace ltrace valgrind fakeroot eatmydata firejail systemd-run "
    "npx pnpx bunx uvx".split())
# Launchers whose subcommand runs any command (or the file or code it is given).
RUNNER_SUBCOMMANDS = frozenset({
    ("uv", "run"), ("poetry", "run"), ("pipenv", "run"), ("npm", "exec"), ("npm", "x"), ("pnpm", "exec"),
    ("pnpm", "dlx"), ("yarn", "exec"), ("yarn", "dlx"), ("yarn", "node"), ("bundle", "exec"), ("hatch", "run"),
    ("pdm", "run"), ("rye", "run"), ("pixi", "run"), ("conda", "run"), ("mamba", "run"), ("go", "run"),
    ("cargo", "run"), ("deno", "run"), ("deno", "eval"), ("bun", "run"), ("bun", "x"), ("bun", "exec"),
    ("mix", "run")})
LAUNCHERS = frozenset(program for program, _ in RUNNER_SUBCOMMANDS)
# Runners whose first operand is another command.
WRAPPERS = frozenset(
    "env exec command xargs nice ionice nohup timeout time sudo doas busybox setsid setpriv stdbuf flock unshare "
    "nsenter chroot taskset chrt strace ltrace valgrind fakeroot eatmydata firejail systemd-run".split())
# Programs that run a command given anywhere in their arguments, or a shell without one.
ALWAYS_OPEN = frozenset("find watch script parallel gdb su runuser".split())
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
    "awk": frozenset({"-v", "-F", "--assign", "--field-separator"}),
    "gawk": frozenset({"-v", "-F", "--assign", "--field-separator"}),
    "mawk": frozenset({"-v", "-F"}),
    "nawk": frozenset({"-v", "-F"}),
    "java": frozenset({"-cp", "-classpath", "--class-path", "-p", "--module-path", "--add-modules"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "flock": frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}),
    "unshare": frozenset({"-S", "--setuid", "-G", "--setgid", "-R", "--root", "-w", "--wd"}),
    "nsenter": frozenset({"-t", "--target", "-S", "--setuid", "-G", "--setgid"}),
    "chroot": frozenset({"--userspec", "--groups"}),
    "strace": frozenset({"-o", "-e", "-p", "-s", "-u", "-E", "-P"}),
    "ltrace": frozenset({"-o", "-e", "-p", "-s", "-u"}),
    "systemd-run": frozenset({"-p", "--property", "-u", "--unit", "-E", "--setenv", "-M", "--machine",
                              "--description", "--slice"}),
    "npm": frozenset({"--prefix", "-w", "--workspace"}),
    "conda": frozenset({"-n", "--name", "-p", "--prefix"}),
    "mamba": frozenset({"-n", "--name", "-p", "--prefix"}),
    "cargo": frozenset({"--manifest-path", "--config", "-Z", "-C"}),
    "go": frozenset({"-C", "-exec", "-tags", "-ldflags", "-gcflags", "-mod", "-o"}),
}
# Positionals before the wrapped command (timeout DURATION COMMAND, flock FILE COMMAND, chroot NEWROOT COMMAND).
LEADING_ARGS = {"timeout": 1, "flock": 1, "chroot": 1, "taskset": 1, "chrt": 1}
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
    if program == "flock" and rest[:1] in (["-c"], ["--command"]):
        rest = ["sh", "-c", *rest[1:]]  # flock FILE -c COMMAND runs COMMAND with sh -c
    return rest


def _open(tokens):
    program = _program(tokens[0])
    if program in ALWAYS_OPEN:
        return True
    if program in LAUNCHERS:
        head = _operands(program, tokens[1:])
        if head[:1] and head[0].startswith("+"):  # cargo +nightly run
            head = _operands(program, head[1:])
        if not head:
            return True  # the caller picks the subcommand: uv run, npm exec
        if (program, head[0]) in RUNNER_SUBCOMMANDS:
            ops = _operands(program, head[1:])
            return not ops or _open(ops)
    if program not in RUNNERS:
        return False
    ops = _operands(program, tokens[1:])
    if not ops:
        return True
    return program in WRAPPERS and _open(ops)


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
    """Only a command that starts with one of the project's configured prefixes (whole tokens) may run, and never
    through an open prefix (bash, python3 -c): that would run anything outside the sandbox, so it is refused here
    as well as warned about by wonka config and the doctor, in case it was set by hand."""
    refused = None
    for p in allowed:
        try:
            tokens = shlex.split(p)
        except ValueError:
            continue
        if tokens and argv[:len(tokens)] == tokens:
            if not _open(tokens):
                return
            refused = p
    if refused is not None:
        raise WonkaError(f"test_commands.{project} {refused!r} {OPEN_WARNING}; refusing to run it. "
                         f"A human pins it with wonka config test_commands.{project} <command>")
    if not allowed:
        raise WonkaError(f"{project} has no test commands: a human adds them with "
                         f"wonka config test_commands.{project} ./test.sh")
    raise WonkaError(f"{shlex.join(argv)} is not a test command of {project}; allowed prefixes: {', '.join(allowed)}")


def lock_path(project):
    """In the worktrees folder, never elsewhere in ~/.wonka: wonka test must be able to run inside the sandbox, which
    can write only there, so the test command it starts is confined too."""
    return store.test_locks_dir() / f"{project}-tests.lock"


def _open_lock(path):
    """The lock file for reading and writing, created if missing and never truncated (that would wipe the current
    holder's info before we even try to lock). Sandboxed test code can write the locks folder, so a symlink or hard
    link planted there is refused: a wonka test run outside the sandbox must never write through it into another file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    refusal = WonkaError(f"{path} is not a plain lock file: delete it and run again")
    if path.parent.is_symlink():
        raise refusal
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except OSError:  # ELOOP for a symlink
        raise refusal
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise refusal
    return os.fdopen(fd, "r+")


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
    with _open_lock(lock_path(project)) as fh:
        if not acquire(fh, float(cfg["test_lock_wait_minutes"]) * 60):
            msg = _busy_message(_holder(fh))
            store.try_log(f"test {tid}: {msg}")  # inside the sandbox ~/.wonka is read-only: no log line then
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
