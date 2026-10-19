"""wonka doctor: is this machine ready to run the factory unattended?"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import note, store

SETTINGS = (Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "settings.local.json")


def _ok(cmd):
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _permissions(paths=None):
    """Merged allow rules and additional directories from every settings file that exists.

    Odd shapes are skipped rather than trusted: a file whose permissions is not an object,
    an allow/additionalDirectories that is not a list, and non-string items.
    """
    allow, dirs = [], []
    for path in paths or SETTINGS:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        perms = data.get("permissions") if isinstance(data, dict) else None
        if not isinstance(perms, dict):
            continue
        allow += _strings(perms.get("allow"))
        dirs += [str(Path(d).expanduser()) for d in _strings(perms.get("additionalDirectories"))]
    return allow, dirs


def _strings(value):
    """The string items of value when it is a list, else nothing."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _orphans(cfg):
    """(level, message) for notes holding a ## section Wonka doesn't know, or one ok line."""
    found, n = [], 0
    for folder in (store.tickets_dir(cfg), store.archive_dir(cfg)):
        for path in sorted(folder.glob("*.md")) if folder.exists() else []:
            if path.stem.endswith("-plan"):
                continue
            n += 1
            try:
                body = note.read(path)[1]
            except (OSError, UnicodeDecodeError) as e:
                found.append(f"{path}: unreadable ({e})")
                continue
            for heading, above in note.unknown_sections(body):
                found.append(f"{path}: ## {heading}" + (f" (below ## {above})" if above else ""))
    if not found:
        yield "ok", f"notes: no unknown ## sections ({n} notes)"
        return
    yield "warn", ("notes with ## sections Wonka doesn't know. A section ends at the next '## ' line, so these are "
                   "probably the tail of the section above, split off before headings in content were demoted. "
                   "To merge one back, change its '## ' to '### ' (not below the Factory log: that section is "
                   "rewritten on every update, so move the text above it first). Leave it if you added it on purpose:\n"
                   + "\n".join(f"  {line}" for line in found))


def permission_checks(allow, dirs, want_dirs):
    """Yield (level, message) for Claude Code permissions (allow rules and additional directories)."""
    missing = [] if "Bash(wonka *)" in allow or "Bash(wonka:*)" in allow else ["Bash(wonka *)"]
    missing_dirs = [d for d in want_dirs if d not in dirs]
    if missing or missing_dirs:
        perms = {k: v for k, v in (("allow", missing), ("additionalDirectories", missing_dirs)) if v}
        yield "warn", ("background agents will ask you before every wonka call; to run unattended add to "
                       "~/.claude/settings.json (or settings.local.json):\n"
                       + json.dumps({"permissions": perms}, indent=2))
    else:
        yield "ok", "Claude Code permissions for the CLI (agents run git through wonka git), worktrees and factory folder"
    broad = [r for r in allow if r == "Bash(git)" or r.startswith(("Bash(git ", "Bash(git:"))]
    if broad:
        yield "warn", ("git allow rules are no longer needed: background agents run git through 'wonka git', which "
                       "is covered by Bash(wonka *). These rules let any session run git with any options "
                       "(git -c core.pager=..., config, push --force); remove them from ~/.claude/settings.json "
                       "unless you want them for your own sessions: " + ", ".join(broad))


def checks():
    """Yield (level, message): level is ok, warn or fail."""
    yield ("ok" if sys.version_info >= (3, 11) else "fail"), f"python {sys.version.split()[0]} (needs 3.11+)"
    yield ("ok" if shutil.which("git") else "fail"), "git"
    yield ("ok" if _ok(["gh", "auth", "status"]) else "fail"), "gh installed and logged in (opens PRs, checks merges)"
    yield ("ok" if shutil.which("notify-send") else "warn"), "notify-send (desktop pings when a ticket needs you)"
    yield ("ok" if shutil.which("xdg-open") else "warn"), "xdg-open (clicking a PR notification opens the PR)"
    if "WONKA_MDLINT" not in os.environ:
        yield ("ok" if shutil.which("npx") else "warn"), "npx (markdownlint checks Idea and Spec sections; without it they are written unchecked)"
    try:
        cfg = store.load_config()
    except store.WonkaError as e:
        yield "fail", str(e)
        return
    board = store.board(cfg)
    yield ("ok" if (board / "Wonka.base").exists() else "fail"), f"🍫 factory {board / 'Wonka.base'}"
    yield from _orphans(cfg)
    path = store.home() / store.STATE_FILE
    try:
        n = len(json.loads(path.read_text())["tickets"]) if path.exists() else 0
        yield "ok", f"state {path} ({n} tickets)"
    except (ValueError, KeyError) as e:
        yield "fail", f"state {path} is unreadable: {e}"

    allow, dirs = _permissions()
    yield from permission_checks(allow, dirs, [str(store.worktrees_dir()), str(board)])


def doctor():
    icons = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    worst = "ok"
    for level, msg in checks():
        print(f"{icons[level]} {msg}")
        worst = level if level == "fail" or worst == "ok" else worst
    return 1 if worst == "fail" else 0
