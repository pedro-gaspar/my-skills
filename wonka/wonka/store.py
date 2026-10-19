"""~/.wonka: config, the golden ticket status JSON (locked, atomic) and paths."""

import contextlib
import copy
import datetime
import fcntl
import json
import os
import shutil
from pathlib import Path

STATE_FILE = "golden_ticket_status.json"
BASE_FILE = "🍫 Factory.base"  # the Obsidian board; its file name is what Obsidian shows
OLD_BASE_FILE = "Wonka.base"  # name before WONKA-00084; wonka init renames it
DEFAULT_CONFIG = {
    "vault": None,
    "folder": "wonka",
    "wip": {"drafting": 1, "baking": 2},  # tickets worked on at the same time
    "archive_after_days": 7,  # Done tickets (and their plans) move to archive/ after this
    "stale_hours": 2,  # a job running longer than this is offered for retry
    "max_review_rounds": 3,  # review rounds with issues before a bad nut is rolled away
}
SETTINGS = {  # wonka config <key> <value>
    "wip.drafting": int, "wip.baking": int, "archive_after_days": int, "stale_hours": float,
    "max_review_rounds": int,
}


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


def home():
    return Path(os.environ.get("WONKA_HOME") or Path.home() / ".wonka")


def load_config():
    path = home() / "config.json"
    if not path.exists():
        raise WonkaError("No ~/.wonka/config.json. Run: wonka init --vault <obsidian vault>")
    cfg = {**copy.deepcopy(DEFAULT_CONFIG), **json.loads(path.read_text())}
    cfg["wip"] = {**DEFAULT_CONFIG["wip"], **cfg["wip"]}
    return cfg


def save_config(cfg):
    home().mkdir(parents=True, exist_ok=True)
    (home() / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def board(cfg):
    return Path(cfg["vault"]).expanduser() / cfg["folder"]


def tickets_dir(cfg):
    return board(cfg) / "golden-tickets"


def plans_dir(cfg):
    return board(cfg) / "plans"


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


@contextlib.contextmanager
def state():
    """Yield the whole status JSON under an exclusive lock; it is written back on success if it changed,
    and the previous version is kept as golden_ticket_status.json.bak."""
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    path = root / STATE_FILE
    with open(root / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = path.read_text() if path.exists() else None
        db = json.loads(old) if old is not None else {"next_id": 1, "tickets": {}}
        yield db
        new = json.dumps(db, indent=2, ensure_ascii=False) + "\n"
        if new == old:
            return
        if old is not None:
            shutil.copy2(path, root / (STATE_FILE + ".bak"))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new)
        os.replace(tmp, path)
