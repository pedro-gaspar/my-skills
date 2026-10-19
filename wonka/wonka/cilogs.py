"""wonka ci-logs: run a project's CI log tool, only on an https URL on one of the project's CI hosts."""

import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from . import gitwrap, store, testrun
from .factory import Factory
from .store import WonkaError

USAGE = "usage: wonka ci-logs <id> [--dispatch <n>] <url>"


def _parse(argv):
    """argv is everything after `wonka ci-logs`: <id> [--dispatch <n>] <url>."""
    tid, dispatch, rest = gitwrap.parse_id(argv, USAGE)
    if len(rest) != 1:
        raise WonkaError(USAGE)
    return tid, dispatch, rest[0]


def check_url(url, hosts, project):
    """Raise WonkaError unless url is https on exactly one of the project's configured hosts."""
    if not hosts:
        raise WonkaError(f"{project} has no CI hosts: a human adds them with wonka config ci_hosts.{project} <host>")
    refusal = WonkaError(f"{url[:200]!r} is not on a CI host of {project}; allowed: {', '.join(hosts)}")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in url):
        raise refusal
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        parts.port  # noqa: B018 - validates the port
    except ValueError:
        raise refusal
    if parts.scheme.lower() != "https" or "@" in netloc:
        raise refusal
    host, sep, port = netloc.lower().partition(":")
    if host.endswith("."):
        host = host[:-1]
    if (host + sep + port) not in {h.lower() for h in hosts}:
        raise refusal


def command_problem(command):
    """Why a configured ci_log_command (non-empty) must not run, or None when it is fine."""
    if not isinstance(command, str):
        return "is not a string command"
    try:
        shlex.split(command)
    except ValueError as e:
        return f"is not a valid command prefix ({e})"
    if testrun.open_prefix(command):
        return f"is {command!r}, which {store.CI_LOG_OPEN_WARNING}"
    return None


def main(argv):
    """argv is everything after `wonka ci-logs`; returns the log tool's exit code."""
    tid, _dispatch, url = _parse(argv)
    cfg = store.load_config()
    f = Factory(cfg, store.snapshot())
    t = f.ticket(tid)
    project = t["project"]
    command = (cfg.get("ci_log_command") or {}).get(project)
    if not command:
        raise WonkaError(f'{project} has no CI log command: a human sets it with '
                         f'wonka config ci_log_command.{project} "beyond ci logs"')
    problem = command_problem(command)
    if problem:
        raise WonkaError(f'ci_log_command.{project} {problem}; refusing to run it. '
                         f'A human fixes it with wonka config ci_log_command.{project} "beyond ci logs"')
    check_url(url, (cfg.get("ci_hosts") or {}).get(project) or [], project)
    path = gitwrap.target(t, f.state(tid), "status")
    if Path(path).resolve() == Path(t["repo"]).resolve():
        raise WonkaError(f"no worktree or scout for {tid}: wonka ci-logs only runs in a ticket's worktree or scout")
    store.log(f"ci-logs {tid}: {url[:200]}")
    cmd = shlex.split(command) + [url]  # command_problem already checked it parses
    try:
        rc = subprocess.run(cmd, cwd=path, stdin=subprocess.DEVNULL).returncode
    except FileNotFoundError:
        raise WonkaError(f"{cmd[0]}: command not found in {path}")
    except PermissionError:
        raise WonkaError(f"{cmd[0]} is not executable")
    return 128 - rc if rc < 0 else rc
