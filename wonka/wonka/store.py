"""~/.wonka: config, the golden ticket status JSON (locked, atomic) and paths."""

import contextlib
import copy
import datetime
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from . import machine

STATE_FILE = "golden_ticket_status.json"
BASE_FILE = "🍫 Factory.base"  # the Obsidian board; its file name is what Obsidian shows
OLD_BASE_FILE = "Wonka.base"  # name before WONKA-00084; wonka init renames it
DEFAULT_CONFIG = {
    "vault": None,
    "folder": "wonka",
    "wip": {"triage": 2, "drafting": 1, "baking": 2},  # tickets worked on at the same time
    "archive_after_days": 7,  # finished tickets (Shipped or Wont Do) and their plans move to archive/ after this
    "stale_hours": 2,  # a job running longer than this is offered for retry
    "max_review_rounds": 3,  # review rounds with issues before a bad nut is rolled away
    "max_followup_depth": 2,  # a squirrel's follow-up is not filed for a ticket this many follow-ups deep
    "worktree_files": {},  # project -> repo-relative gitignored files copied into its worktrees
    "test_commands": {},  # project -> argv prefixes `wonka test` may run, under a per-project lock
    "ci_hosts": {},  # project -> hosts `wonka ci-logs` may fetch from
    "ci_log_command": {},  # project -> the CI log tool's command prefix, run by `wonka ci-logs`
    "test_lock_wait_minutes": 3,  # how long `wonka test` waits for the lock; wait plus run must fit the agents' 10-minute Bash timeout
}
SETTINGS = {  # wonka config <key> <value>
    "wip.triage": int, "wip.drafting": int, "wip.baking": int, "archive_after_days": int, "stale_hours": float,
    "max_review_rounds": int, "max_followup_depth": int,
    "test_lock_wait_minutes": float,
}
CI_LOG_OPEN_WARNING = ("lets an agent run any program outside the sandbox through wonka ci-logs; "
                       "pin it to the log tool (e.g. beyond ci logs)")
LOCK_TIMEOUT = 600  # seconds a command waits for the state lock before giving up


def safe_relpath(p):
    """True for a path that is relative and stays inside the repo."""
    return bool(p) and not os.path.isabs(p) and ".." not in Path(p).parts


def set_setting(cfg, key, raw):
    if key.startswith("worktree_files."):
        project = key.split(".", 1)[1]
        if not project:
            raise WonkaError("usage: wonka config worktree_files.<project> <path>[,<path>...]")
        paths = []
        for p in (x.strip() for x in raw.split(",")):
            if not p:
                continue
            if not safe_relpath(p):
                raise WonkaError(f"{p}: worktree_files takes paths relative to the repo, inside it")
            paths.append(str(Path(p)))
        files = dict(cfg.get("worktree_files") or {})
        if paths:
            files[project] = paths
        else:
            files.pop(project, None)
        cfg["worktree_files"] = files
        return
    if key.startswith("test_commands."):
        project = key.split(".", 1)[1]
        if not project:
            raise WonkaError("usage: wonka config test_commands.<project> <command>[,<command>...]")
        prefixes = []
        for p in (x.strip() for x in raw.split(",")):
            if not p:
                continue
            try:
                tokens = shlex.split(p)
            except ValueError as e:
                raise WonkaError(f"{p}: not a valid command prefix ({e})")
            if not tokens:
                continue
            from .testrun import OPEN_WARNING, open_prefix  # lazily: testrun imports store
            if open_prefix(p):
                raise WonkaError(f"{p} {OPEN_WARNING}")
            prefixes.append(p)
        cmds = dict(cfg.get("test_commands") or {})
        if prefixes:
            cmds[project] = prefixes
        else:
            cmds.pop(project, None)
        cfg["test_commands"] = cmds
        return
    if key.startswith("ci_hosts."):
        project = key.split(".", 1)[1]
        if not project:
            raise WonkaError("usage: wonka config ci_hosts.<project> <host>[,<host>...]")
        hosts = []
        for h in (x.strip().lower() for x in raw.split(",")):
            h = h[:-1] if h.endswith(".") else h
            if not h:
                continue
            if (re.search(r"[/@?#\\\s]", h) or "://" in h
                    or not re.fullmatch(r"[a-z0-9.-]+(:[0-9]{1,5})?", h)):
                raise WonkaError(f"{h}: ci_hosts takes bare hosts (host or host:port), not URLs")
            hosts.append(h)
        cur = dict(cfg.get("ci_hosts") or {})
        if hosts:
            cur[project] = hosts
        else:
            cur.pop(project, None)
        cfg["ci_hosts"] = cur
        return
    if key.startswith("ci_log_command."):
        project = key.split(".", 1)[1]
        if not project:
            raise WonkaError("usage: wonka config ci_log_command.<project> <command>")
        value = raw.strip()
        if value:
            from .cilogs import parse_command  # lazily: cilogs imports store
            problem = parse_command(value)[1]
            if problem:
                raise WonkaError(f"ci_log_command.{project} {problem}")
        cur = dict(cfg.get("ci_log_command") or {})
        if value:
            cur[project] = value
        else:
            cur.pop(project, None)
        cfg["ci_log_command"] = cur
        return
    if key not in SETTINGS:
        raise WonkaError(f"unknown setting {key}; one of {', '.join(SETTINGS)}, worktree_files.<project>, "
                         "test_commands.<project>, ci_hosts.<project>, ci_log_command.<project>")
    try:
        value = SETTINGS[key](raw)
    except ValueError:
        raise WonkaError(f"{key} must be a number")
    if not math.isfinite(value):  # inf or nan: `wonka test` would wait for the lock forever
        raise WonkaError(f"{key} must be a finite number")
    if value < (1 if key.startswith("wip.") or key == "max_review_rounds" else 0):
        raise WonkaError(f"{key} is too small")
    *parents, leaf = key.split(".")
    node = cfg
    for k in parents:
        node = node.setdefault(k, {})
    node[leaf] = value


class WonkaError(Exception):
    pass


class CriticalError(WonkaError):
    """The whole factory is stopped until a human fixes something (not just one ticket): the CLI pings the desktop."""


def home():
    """~/.wonka, or WONKA_HOME (the tests, a second factory). An agent that set it could load a config and state it
    wrote, but a background agent cannot: `WONKA_HOME=<dir> wonka git ...` does not match the per-command allow rules
    (`Bash(wonka git *)`), so Claude Code would ask a human. As a cheap second line, a relative WONKA_HOME (it would
    resolve against the agent's worktree) or one inside the default home (its worktrees and drafts, which agents
    write) is refused."""
    default = Path.home() / ".wonka"
    raw = os.environ.get("WONKA_HOME")
    if not raw:
        return default
    path = Path(raw)
    if not path.is_absolute():
        raise WonkaError(f"WONKA_HOME must be an absolute path, not {raw!r}")
    if default.resolve() in path.resolve().parents:
        raise WonkaError(f"WONKA_HOME {raw} is inside {default}, whose worktrees agents write: refusing it")
    return path


def load_config():
    path = home() / "config.json"
    if not path.exists():
        raise WonkaError("No ~/.wonka/config.json. Run: wonka init --vault <obsidian vault>")
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise CriticalError(f"{path} is not valid JSON ({e}): fix it, or delete it and run wonka init to start over from the defaults")
    if not isinstance(data, dict):
        raise CriticalError(f"{path} must hold a JSON object: fix it, or delete it and run wonka init to start over from the defaults")
    cfg = {**copy.deepcopy(DEFAULT_CONFIG), **data}
    _check_config(cfg, path)
    cfg["wip"] = {**DEFAULT_CONFIG["wip"], **cfg["wip"]}
    return cfg


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_config(cfg, path):
    """A hand edit gone wrong (null, a string for a list, a list for an object) is named here, not met as a
    traceback later. Settings Wonka reads with a fallback (worktree_files and the other per-project maps) may be null."""
    def bad(key, what):
        return CriticalError(f"{path}: {key} must be {what}: fix it, or delete it and run wonka init to start over from the defaults")

    for key in ("vault", "folder"):
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise bad(key, "a folder path" + (" (wonka init --vault <obsidian vault>)" if key == "vault" else ""))
    if not isinstance(cfg["wip"], dict):
        raise bad("wip", 'an object like {"triage": 2, "drafting": 1, "baking": 2}')
    for key, value in cfg["wip"].items():
        if not _number(value):
            raise bad(f"wip.{key}", "a number")
    for key in SETTINGS:
        if "." not in key and not _number(cfg[key]):
            raise bad(key, "a number")
    for key in ("worktree_files", "test_commands", "ci_hosts", "ci_log_command"):
        if cfg[key] is None:
            cfg[key] = {}
        if not isinstance(cfg[key], dict):
            raise bad(key, "an object keyed by project")
        if key == "ci_log_command":
            continue  # a bad command stops only its project: wonka ci-logs refuses it and the doctor warns
        for project, value in cfg[key].items():
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                raise bad(f"{key}.{project}", "a list of strings")


def save_config(cfg):
    home().mkdir(parents=True, exist_ok=True)
    write_atomic(home() / "config.json", json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def write_atomic(path, text):
    """Write text to path so a reader (or a crash) sees the old file or the new one, never half of it. The temp file
    is a dotfile next to it, so Obsidian and the tickets folder scan never pick it up."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def board(cfg):
    return Path(cfg["vault"]).expanduser() / cfg["folder"]


def tickets_dir(cfg):
    return board(cfg) / "golden-tickets"


def plans_dir(cfg):
    return board(cfg) / "plans"


def migrate_base(cfg):
    """Rename the old columns (and the hyphenated labels) and add new ones (💡 Idea before 📏 Triage); returns (old, new) pairs, old is None for an
    added column."""
    path = board(cfg) / BASE_FILE
    if not path.exists():
        return []
    text = path.read_text()
    done = []
    renames = [(old, machine.STATUSES[machine.OLD_STATUS_KEYS[key]]) for key, old in machine.OLD_STATUSES.items()]
    renames += [(machine.hyphenated(new), new) for new in (*machine.STATUSES.values(), *machine.PHASES.values())]
    for old, new in renames:
        if old != new and old in text:
            text = text.replace(old, new)
            done.append((old, new))
    idea = machine.STATUSES["idea"]
    triage = machine.STATUSES["triage"]
    if idea not in text:
        found = re.search(rf"^([ \t]*- ){re.escape(triage)}[ \t]*$", text, re.M)
        if found:
            text = text[:found.start()] + f"{found.group(1)}{idea}\n" + text[found.start():]
            done.append((None, idea))
    if done:
        write_atomic(path, text)
    return done


def archive_dir(cfg):
    return board(cfg) / "archive"


def worktrees_dir():
    return home() / "worktrees"


def test_locks_dir():
    """Where `wonka test` keeps its per-project locks: inside the worktrees folder, which sandboxed commands may write,
    so `wonka test` can run inside the sandbox and confine the test command it starts."""
    return worktrees_dir() / ".locks"


def drafts_dir():
    """Where agents put the markdown they hand to `wonka plan|section|pr-body --file`: inside the worktrees folder,
    which sandboxed commands may write, unlike the rest of ~/.wonka (config and state)."""
    return worktrees_dir() / ".drafts"


def log(*lines):
    """One line per event in ~/.wonka/factory.log, the place to look when a ticket gets stuck."""
    if not lines:
        return
    home().mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with open(home() / "factory.log", "a") as fh:
        fh.writelines(f"{stamp} {line}\n" for line in lines)


def try_log(*lines):
    """log, skipped silently when ~/.wonka is not writable: `wonka test` runs inside the sandbox, which never may write
    there (it holds the config and the state)."""
    with contextlib.suppress(OSError):
        log(*lines)


def _load(text):
    if text is None:
        return {"next_id": 1, "tickets": {}}
    path = home() / STATE_FILE
    try:
        db = json.loads(text)
    except ValueError as e:
        raise CriticalError(f"{path} is not valid JSON ({e}). The previous version is {path}.bak: check it, then "
                         f"cp {path}.bak {path}")
    problem = _state_problem(db)
    if problem:
        raise CriticalError(f"{path} is not a Wonka state file ({problem}). The previous version is {path}.bak: "
                            f"check it, then cp {path}.bak {path}")
    machine.migrate(db)  # state files written before WONKA-00100 and WONKA-00140 hold the old keys
    return db


def _state_problem(db):
    """What is wrong with the shape of a loaded state file, or None."""
    if not isinstance(db, dict):
        return "not a JSON object"
    if not isinstance(db.get("tickets"), dict):
        return "tickets must be an object keyed by ticket id"
    if isinstance(db.get("next_id"), bool) or not isinstance(db.get("next_id"), int):
        return "next_id must be a number"
    bad = next((k for k, t in db["tickets"].items() if not isinstance(t, dict)), None)
    return f"ticket {bad} must be an object" if bad else None


def _lock(fh, timeout):
    """flock with a time limit, so a stuck command can never block every other one forever."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise CriticalError(f"the factory is busy: another wonka command has held {home() / '.lock'} for "
                                 f"{timeout:g} s. Try again; if it stays busy, look for a hung wonka process")
            time.sleep(0.2)


def snapshot():
    """The status JSON as it is now, read without the lock: for commands that only read (wonka git, status, show, repos, metrics).
    Every write replaces the file atomically, so this is always one whole version."""
    path = home() / STATE_FILE
    return _load(path.read_text() if path.exists() else None)


@contextlib.contextmanager
def state():
    """Yield the whole status JSON under an exclusive lock; it is written back on success if it changed,
    and the previous version is kept as golden_ticket_status.json.bak."""
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    path = root / STATE_FILE
    with open(root / ".lock", "w") as lock:
        _lock(lock, LOCK_TIMEOUT)
        old = path.read_text() if path.exists() else None
        db = _load(old)
        yield db
        new = json.dumps(db, indent=2, ensure_ascii=False) + "\n"
        if new == old:
            return
        if old is not None:
            shutil.copy2(path, root / (STATE_FILE + ".bak"))
        write_atomic(path, new)
