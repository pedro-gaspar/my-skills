"""~/.wonka: config, the golden ticket status JSON (locked, atomic) and paths."""

import contextlib
import fcntl
import json
import os
from pathlib import Path

STATE_FILE = "golden_ticket_status.json"
DEFAULT_CONFIG = {
    "vault": None,
    "folder": "wonka",
    "wip": {"drafting": 1, "baking": 2},
    "archive_after_days": 7,
    "stale_hours": 2,
}


class WonkaError(Exception):
    pass


def home():
    return Path(os.environ.get("WONKA_HOME") or Path.home() / ".wonka")


def load_config():
    path = home() / "config.json"
    if not path.exists():
        raise WonkaError("No ~/.wonka/config.json. Run: wonka init --vault <obsidian vault>")
    return {**DEFAULT_CONFIG, **json.loads(path.read_text())}


def save_config(cfg):
    home().mkdir(parents=True, exist_ok=True)
    (home() / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def board(cfg):
    return Path(cfg["vault"]).expanduser() / cfg["folder"]


def tickets_dir(cfg):
    return board(cfg) / "tickets"


def plans_dir(cfg):
    return board(cfg) / "plans"


def archive_dir(cfg):
    return board(cfg) / "archive"


def worktrees_dir():
    return home() / "worktrees"


@contextlib.contextmanager
def state():
    """Yield the whole status JSON under an exclusive lock; it is written back on success."""
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    path = root / STATE_FILE
    with open(root / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = json.loads(path.read_text()) if path.exists() else {"next_id": 1, "tickets": {}}
        yield db
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
