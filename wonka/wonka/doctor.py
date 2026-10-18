"""wonka doctor: is this machine ready to run the factory unattended?"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import store

SETTINGS = (Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "settings.local.json")


def _ok(cmd):
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _permissions(paths=None):
    """Merged allow rules and additional directories from every settings file that exists."""
    allow, dirs = [], []
    for path in paths or SETTINGS:
        try:
            perms = json.loads(path.read_text()).get("permissions", {})
        except (OSError, ValueError, AttributeError):
            continue
        allow += perms.get("allow", [])
        dirs += [str(Path(d).expanduser()) for d in perms.get("additionalDirectories", [])]
    return allow, dirs


def checks():
    """Yield (level, message): level is ok, warn or fail."""
    yield ("ok" if sys.version_info >= (3, 11) else "fail"), f"python {sys.version.split()[0]} (needs 3.11+)"
    yield ("ok" if shutil.which("git") else "fail"), "git"
    yield ("ok" if _ok(["gh", "auth", "status"]) else "fail"), "gh installed and logged in (opens PRs, checks merges)"
    yield ("ok" if shutil.which("notify-send") else "warn"), "notify-send (desktop pings when a ticket needs you)"
    try:
        cfg = store.load_config()
    except store.WonkaError as e:
        yield "fail", str(e)
        return
    board = store.board(cfg)
    yield ("ok" if (board / "Wonka.base").exists() else "fail"), f"board {board / 'Wonka.base'}"
    path = store.home() / store.STATE_FILE
    try:
        n = len(json.loads(path.read_text())["tickets"]) if path.exists() else 0
        yield "ok", f"state {path} ({n} tickets)"
    except (ValueError, KeyError) as e:
        yield "fail", f"state {path} is unreadable: {e}"

    allow, dirs = _permissions()
    want_dirs = [str(store.worktrees_dir()), str(board)]
    missing = [r for r in ("Bash(wonka *)", "Bash(git *)") if r not in allow and r.replace(" *", ":*") not in allow]
    missing_dirs = [d for d in want_dirs if d not in dirs]
    if missing or missing_dirs:
        snippet = {"permissions": {"allow": missing, "additionalDirectories": missing_dirs}}
        yield "warn", ("background agents will ask you before every wonka/git call; to run unattended add to ~/.claude/settings.json (or settings.local.json):\n"
                       + json.dumps(snippet, indent=2))
    else:
        yield "ok", "Claude Code permissions for the CLI, git, worktrees and board"


def doctor():
    icons = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    worst = "ok"
    for level, msg in checks():
        print(f"{icons[level]} {msg}")
        worst = level if level == "fail" or worst == "ok" else worst
    return 1 if worst == "fail" else 0
