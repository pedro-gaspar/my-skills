"""wonka doctor: is this machine ready to run the factory unattended?"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import cilogs, note, store, testrun
from .factory import notifier

SETTINGS = (Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "settings.local.json")


def _ok(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=30).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
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


def _sandbox_writes(paths=None):
    """sandbox.filesystem.allowWrite from every settings file that exists (odd shapes skipped)."""
    writes = []
    for path in paths or SETTINGS:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        box = data.get("sandbox") if isinstance(data, dict) else None
        fs = box.get("filesystem") if isinstance(box, dict) else None
        writes += _strings(fs.get("allowWrite")) if isinstance(fs, dict) else []
    return writes


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
            except (OSError, store.WonkaError) as e:
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


def _test_command_checks(cfg):
    """A warn for test_commands prefixes that leave the program or code to the caller, else ok (or nothing)."""
    found = testrun.open_prefixes(cfg)
    if found:
        yield "warn", ("test_commands prefixes that " + testrun.OPEN_WARNING + " (wonka test refuses them; "
                       "a human pins each with wonka config test_commands.<project> <command>):\n"
                       + "\n".join(f"  {proj}: {p}" for proj, p in found))
    elif cfg.get("test_commands"):
        yield "ok", "test_commands: no shell or interpreter prefixes"


def _worktree_file_checks(cfg, tickets):
    """(level, message) for each project in config worktree_files: its sources must be files in the project's
    main checkout that git ignores there, or the CLI never copies them into worktrees and scouts."""
    files = cfg.get("worktree_files") or {}
    for project, paths in sorted(files.items()):
        repos = sorted({t["repo"] for t in tickets.values()
                        if isinstance(t, dict) and t.get("project") == project and t.get("repo")})
        if not repos:
            yield "warn", (f"worktree_files.{project}: no ticket is in a repo named {project}, so its files are not "
                           "checked (is the project name right?)")
            continue
        for repo in repos:
            bad = False
            for rel in paths:
                if not store.safe_relpath(rel):
                    msg = f"worktree_files.{project}: {rel} must be relative to the repo, inside it; it is never copied"
                elif not (Path(repo) / rel).is_file():
                    msg = (f"worktree_files.{project}: {rel} is missing in {repo}; "
                           "it is never copied into worktrees and scouts")
                elif not _ok(["git", "-C", repo, "check-ignore", "-q", "--", rel]):
                    msg = (f"worktree_files.{project}: {rel} is not gitignored in {repo}; "
                           "it is never copied (a builder could commit it)")
                else:
                    continue
                bad = True
                yield "warn", msg
            if not bad:
                yield "ok", f"worktree files for {project} in {repo}: {len(paths)} present and gitignored"


# The subcommands background agents and the /loop manager run. The rest (init, config, set, move, block, unblock,
# doctor) change settings or stand in for the human's ticks, so they must ask the human: an agent that read a
# hostile CI log or diff must not be able to switch the sandbox off (`wonka config test_commands.<p> bash`) or pass
# its own gates (`wonka move`, `wonka set <id> spec_review agent`).
AGENT_COMMANDS = ("git", "test", "ci-logs", "triage", "ask", "section", "plan", "drafted", "pr-body", "baked",
                  "resolved", "report", "add", "show", "status", "repos", "metrics", "tick", "finished", "retry")
BROAD = ("Bash(wonka)", "Bash(wonka *)", "Bash(wonka:*)")


def _covered(allow, cmd):
    return f"Bash(wonka {cmd} *)" in allow or f"Bash(wonka {cmd}:*)" in allow


def permission_checks(allow, dirs, want_dirs):
    """Yield (level, message) for Claude Code permissions (allow rules and additional directories)."""
    missing = [f"Bash(wonka {c} *)" for c in AGENT_COMMANDS if not _covered(allow, c)]
    missing_dirs = [d for d in want_dirs if d not in dirs]
    if missing or missing_dirs:
        perms = {k: v for k, v in (("allow", missing), ("additionalDirectories", missing_dirs)) if v}
        yield "warn", ("background agents will ask you before every wonka call; to run unattended add to "
                       "~/.claude/settings.json (or settings.local.json):\n"
                       + json.dumps({"permissions": perms}, indent=2))
    else:
        yield "ok", "Claude Code permissions for the CLI (agents run git through wonka git), worktrees and factory folder"
    broad = [r for r in allow if r in BROAD]
    if broad:
        yield "warn", ("this allow rule lets any agent run every wonka command, the human ones too: wonka config "
                       "(an agent could set test_commands to bash and run anything outside the sandbox), move, set; "
                       "replace it with the per-command rules above: " + ", ".join(broad))
    git = [r for r in allow if r == "Bash(git)" or r.startswith(("Bash(git ", "Bash(git:"))]
    if git:
        yield "warn", ("git allow rules are no longer needed: background agents run git through 'wonka git', which "
                       "is covered by Bash(wonka git *). These rules let any session run git with any options "
                       "(git -c core.pager=..., config, push --force); remove them from ~/.claude/settings.json "
                       "unless you want them for your own sessions: " + ", ".join(git))


def sandbox_checks(writes):
    """Yield (level, message) for sandbox.filesystem.allowWrite: ~/.wonka itself holds the config and the state,
    which sandboxed test code must never edit. Only its worktrees folder may be writable."""
    home = store.home().resolve()
    bad = [w for w in writes if Path(w).expanduser().resolve() in (home, *home.parents)]
    if bad:
        yield "warn", (f"sandbox.filesystem.allowWrite lets sandboxed commands (any test code) edit {home}, "
                       "Wonka's config and state, and so turn off the sandbox for wonka test; remove "
                       + ", ".join(bad) + " (agents now hand text over with --file, so wonka never runs sandboxed)")


def _notifier_checks():
    """Desktop pings: notify-send (+ xdg-open for the click) on Linux, terminal-notifier or osascript on macOS."""
    tool = notifier()
    if tool == "notify-send":
        yield "ok", "notify-send (desktop pings when a ticket needs you)"
        yield ("ok" if shutil.which("xdg-open") else "warn"), "xdg-open (clicking a PR notification opens the PR)"
    elif tool == "terminal-notifier":
        yield "ok", "terminal-notifier (desktop pings when a ticket needs you; a click opens the PR)"
    elif tool == "osascript":
        yield "warn", ("osascript (desktop pings work, but clicking a PR notification opens nothing: "
                       "brew install terminal-notifier)")
    else:
        yield "warn", ("no desktop notifier (notify-send on Linux, terminal-notifier or osascript on macOS): "
                       "nothing pings you when a ticket needs you or the factory stops")


def _canonical(command):
    """A command with its whitespace and quoting normalised, so 'beyond  ci logs' matches 'beyond ci logs'; None
    when it does not parse or is not a string."""
    try:
        return shlex.join(shlex.split(command)) if isinstance(command, str) else None
    except ValueError:
        return None


def _rule_lets(rule, command):
    """True when an allow rule like 'Bash(beyond  ci *)' lets an agent run `command` (normalised) with any URL."""
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    body = rule[5:-1]
    wild = body.endswith((":*", " *"))
    body = _canonical(body[:-2] if wild else body)
    if not body:
        return False
    return (body == command or body.startswith(command + " ")
            or (wild and command.startswith(body + " ")))


def _ci_log_checks(cfg, allow):
    """Yield (level, message) for the CI log tool config and for allow rules that bypass the host check."""
    cmds = cfg.get("ci_log_command") or {}
    hosts = cfg.get("ci_hosts") or {}
    projects = sorted(set(cmds) | set(hosts))
    if not projects:
        return
    clean = True
    for p in projects:
        if not (cmds.get(p) and hosts.get(p)):
            clean = False
            yield "warn", (f"ci_log_command.{p} needs ci_hosts.{p} (and the reverse): "
                           "wonka ci-logs refuses until both are set")
    for p in sorted(cmds):
        problem = cmds[p] and cilogs.command_problem(cmds[p])
        if problem:
            clean = False
            yield "warn", (f"ci_log_command.{p} {problem}; wonka ci-logs refuses to run it until a human sets it "
                           f"with wonka config ci_log_command.{p} <command>")
    tools = [c for c in map(_canonical, cmds.values()) if c]
    bypass = [r for r in allow for c in tools if _rule_lets(r, c)]
    if bypass:
        clean = False
        yield "warn", ("these allow rules let any agent run the CI log tool on any URL, bypassing the CI host check; "
                       "agents use wonka ci-logs (covered by Bash(wonka ci-logs *)); remove them and the tool's sandbox "
                       "exclusion: " + ", ".join(dict.fromkeys(bypass)))
    if clean:
        yield "ok", f"CI log tools: {len(cmds)} project(s) pinned to their CI hosts"


def checks():
    """Yield (level, message): level is ok, warn or fail."""
    yield ("ok" if sys.version_info >= (3, 11) else "fail"), f"python {sys.version.split()[0]} (needs 3.11+)"
    yield ("ok" if shutil.which("git") else "fail"), "git"
    yield ("ok" if _ok(["gh", "auth", "status"]) else "fail"), "gh installed and logged in (opens PRs, checks merges)"
    yield from _notifier_checks()
    if "WONKA_MDLINT" not in os.environ:
        yield ("ok" if shutil.which("npx") else "warn"), "npx (markdownlint checks Idea and Spec sections; without it they are written unchecked)"
    try:
        cfg = store.load_config()
    except store.WonkaError as e:
        yield "fail", str(e)
        return
    board = store.board(cfg)
    base, old = board / store.BASE_FILE, board / store.OLD_BASE_FILE
    if base.exists():
        yield "ok", f"🍫 factory {base}"
    elif old.exists():
        yield "warn", f"🍫 factory {old} has the old name: run wonka init to rename it to {store.BASE_FILE}"
    else:
        yield "fail", f"🍫 factory {base}"
    yield from _orphans(cfg)
    yield from _test_command_checks(cfg)
    path = store.home() / store.STATE_FILE
    tickets = {}
    try:
        tickets = store.snapshot()["tickets"]  # the same checks every command makes
        yield "ok", f"state {path} ({len(tickets)} tickets)"
    except (OSError, ValueError, store.WonkaError) as e:
        yield "fail", f"state {path} is unreadable: {e}"
    yield from _worktree_file_checks(cfg, tickets)

    allow, dirs = _permissions()
    yield from _ci_log_checks(cfg, allow)
    yield from permission_checks(allow, dirs, [str(store.worktrees_dir()), str(board)])
    yield from sandbox_checks(_sandbox_writes())


def doctor():
    icons = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    worst = "ok"
    for level, msg in checks():
        print(f"{icons[level]} {msg}")
        worst = level if level == "fail" or worst == "ok" else worst
    return 1 if worst == "fail" else 0
