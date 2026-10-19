"""~/.wonka: config, the golden ticket status JSON (locked, atomic) and paths."""

import contextlib
import copy
import datetime
import fcntl
import json
import os
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
    "archive_after_days": 7,  # finished tickets (Shipped or Wont-Do) and their plans move to archive/ after this
    "stale_hours": 2,  # a job running longer than this is offered for retry
    "max_review_rounds": 3,  # review rounds with issues before a bad nut is rolled away
    "max_followup_depth": 2,  # a squirrel's follow-up is not filed for a ticket this many follow-ups deep
}
SETTINGS = {  # wonka config <key> <value>
    "wip.triage": int, "wip.drafting": int, "wip.baking": int, "archive_after_days": int, "stale_hours": float,
    "max_review_rounds": int, "max_followup_depth": int,
}
LOCK_TIMEOUT = 600  # seconds a command waits for the state lock before giving up


def set_setting(cfg, key, raw):
    if key not in SETTINGS:
        raise WonkaError(f"unknown setting {key}; one of {', '.join(SETTINGS)}")
    try:
        value = SETTINGS[key](raw)
    except ValueError:
        raise WonkaError(f"{key} must be a number")
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
    return Path(os.environ.get("WONKA_HOME") or Path.home() / ".wonka")


def load_config():
    path = home() / "config.json"
    if not path.exists():
        raise WonkaError("No ~/.wonka/config.json. Run: wonka init --vault <obsidian vault>")
    try:
        cfg = {**copy.deepcopy(DEFAULT_CONFIG), **json.loads(path.read_text())}
    except ValueError as e:
        raise CriticalError(f"{path} is not valid JSON ({e}): fix it or run wonka init again")
    cfg["wip"] = {**DEFAULT_CONFIG["wip"], **cfg["wip"]}
    return cfg


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
    """Rename the old 🎫-Ready column to 🎫-Planned in an existing Factory.base; True when it rewrote the file."""
    path = board(cfg) / BASE_FILE
    if not path.exists():
        return False
    text = path.read_text()
    if "🎫-Ready" not in text:
        return False
    write_atomic(path, text.replace("🎫-Ready", "🎫-Planned"))
    return True


def archive_dir(cfg):
    return board(cfg) / "archive"


def worktrees_dir():
    return home() / "worktrees"


def log(*lines):
    """One line per event in ~/.wonka/factory.log, the place to look when a ticket gets stuck."""
    if not lines:
        return
    home().mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with open(home() / "factory.log", "a") as fh:
        fh.writelines(f"{stamp} {line}\n" for line in lines)


def _load(text):
    if text is None:
        return {"next_id": 1, "tickets": {}}
    try:
        db = json.loads(text)
        machine.migrate(db)  # state files written before WONKA-00100 hold the old keys
        return db
    except ValueError as e:
        path = home() / STATE_FILE
        raise CriticalError(f"{path} is not valid JSON ({e}). The previous version is {path}.bak: check it, then "
                         f"cp {path}.bak {path}")


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
    """The status JSON as it is now, read without the lock: for commands that only read (wonka git, status, show).
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
