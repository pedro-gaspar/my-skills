"""wonka git: the only way background agents run git, pinned to a ticket's worktree or repo."""

import os
import subprocess
from pathlib import Path

from . import store
from .factory import JOB_AGENTS, Factory, fmt
from .store import WonkaError

READS = ("status", "diff", "log", "show", "rev-parse", "ls-files", "blame")
WRITES = ("add", "commit", "merge")
SUBCOMMANDS = READS + WRITES
# the states whose job writes to the worktree: the builder bakes, the fixer merges the base into an open PR
WRITE_STATES = frozenset(JOB_AGENTS[a] for a in ("oompa-loompa-builder", "oompa-loompa-fixer"))
# long options refused (and any abbreviation of them: parse-options accepts unambiguous prefixes), including the
# options that read a file given by path, which can be outside the worktree (--pathspec-from-file, --exclude-from...)
BANNED = {
    "*": ("--output", "--ext-diff", "--textconv", "--no-index", "--contents", "--exec", "--upload-pack",
          "--receive-pack", "--extcmd", "--open-files-in-pager", "--config", "--pathspec-from-file"),
    "add": ("--interactive", "--patch", "--edit", "--all"),
    "commit": ("--amend", "--interactive", "--patch", "--file", "--template"),
    "ls-files": ("--exclude-from", "--exclude-per-directory"),
    "blame": ("--ignore-revs-file",),
    # these rev-parse options test whether a path outside the worktree exists (or is a git dir)
    "rev-parse": ("--resolve-git-dir", "--prefix"),
}
# short flags refused, also inside clusters (-vp); -X, -S and -O<orderfile> read a file given by path
BANNED_SHORT = {"add": "pieA", "commit": "pFt", "ls-files": "X", "blame": "S", "diff": "O", "log": "O", "show": "O"}
# rest of the cluster, or else the next arg, is a value: only options whose value is mandatory belong here
SHORT_WITH_VALUE = {"commit": "mFCct", "ls-files": "x", "blame": "L", "diff": "SG", "show": "SG", "log": "SGLn"}
LONG_WITH_VALUE = {"commit": ("--message", "--author", "--date", "--reuse-message", "--reedit-message", "--fixup",
                              "--squash", "--cleanup", "--trailer"),
                   "ls-files": ("--exclude",), "blame": ("--ignore-rev",)}
# exact option names that are prefixes of a banned name: git takes an exact match over an abbreviation, so they are safe
EXACT_OK = {"ls-files": ("--exclude",), "blame": ("--ignore-rev",)}
NO_EXTERNAL = ("diff", "log", "show", "blame")        # these get --no-ext-diff --no-textconv injected
# only identity and dates pass through: every GIT_CONFIG_* (GLOBAL, SYSTEM, NOSYSTEM, PARAMETERS, COUNT/KEY/VALUE)
# is dropped, since a config the caller picks can run programs (core.pager, gpg.program, filters, hooksPath)
# paths must stay inside the tree: diff turns into --no-index for an outside path; rev-parse reports whether one exists
PATHS_INSIDE = ("diff", "rev-parse")
KEEP_ENV = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
            "GIT_COMMITTER_DATE")


def _check_merge(args):
    """The fixer only ever runs `merge --no-edit <ref>` or `merge --abort`: nothing else gets through."""
    ok = args == ["--abort"] or (len(args) == 2 and args[0] == "--no-edit" and not args[1].startswith("-"))
    if not ok:
        raise WonkaError("git merge only runs as 'merge --no-edit <ref>' or 'merge --abort'")


def _inside(sub, arg):
    """git diff silently turns into `diff --no-index` when a path is outside the work tree, which reads any file:
    so its paths must stay inside the tree (no absolute path, no `..` that climbs out). rev-parse says whether an
    outside path exists, so it gets the same rule."""
    if sub not in PATHS_INSIDE:
        return
    norm = os.path.normpath(arg)
    if os.path.isabs(arg) or norm == ".." or norm.startswith(".." + os.sep):
        why = " (a path outside it turns diff into diff --no-index)" if sub == "diff" else ""
        raise WonkaError(f"git {sub} {arg} is not allowed: paths must stay inside the worktree{why}")


def check(sub, args):
    """Raise WonkaError when the subcommand or one of its options is not allowed."""
    if sub not in SUBCOMMANDS:
        raise WonkaError(f"git {sub} is not allowed; use one of {', '.join(SUBCOMMANDS)}")
    if sub == "merge":
        return _check_merge(args)
    banned = BANNED["*"] + BANNED.get(sub, ())
    banned_short, short_value = BANNED_SHORT.get(sub, ""), SHORT_WITH_VALUE.get(sub, "")
    long_value, exact_ok = LONG_WITH_VALUE.get(sub, ()), EXACT_OK.get(sub, ())
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            for rest in args[i:]:
                _inside(sub, rest)
            return
        if not arg.startswith("-") or arg == "-":
            _inside(sub, arg)
        elif arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name not in exact_ok and any(b.startswith(name) for b in banned):
                raise WonkaError(f"git {sub} {name} is not allowed (it can run programs, write or read files outside "
                                 "the repo, or rewrite history; abbreviations of such options are refused too)")
            if "=" not in arg and name in long_value:
                i += 1  # the next arg is this option's value
        elif arg.startswith("-") and len(arg) > 1:
            for n, c in enumerate(arg[1:], 1):
                if c in banned_short:
                    raise WonkaError(f"git {sub} -{c} is not allowed")
                if c in short_value:
                    if n == len(arg) - 1:
                        i += 1  # the value is the next arg
                    break  # else the rest of the cluster is the value
    return None


def command(path, git_dir, sub, args):
    """git dir and work tree are both pinned, so git never reads the `.git` file inside the tree (an agent can
    rewrite it to point at a git dir it planted, with filters, hooks or core.worktree in its config)."""
    injected = ["--no-ext-diff", "--no-textconv"] if sub in NO_EXTERNAL else []
    return ["git", "--no-pager", "-C", str(path), f"--git-dir={git_dir}", f"--work-tree={path}",
            "-c", "core.fsmonitor=false", sub, *injected, *args]


def env(base=None):
    out = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.startswith("GIT_") or k in KEEP_ENV}
    out.update(GIT_EDITOR="true", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0")
    return out


def git_dir(repo, path):
    """The git dir for path (the repo itself, or one of its linked worktrees), found from the repo's side: the
    worktree whose registered `gitdir` file points back at <path>/.git. The `.git` file inside the tree is never read.
    """
    repo, path = Path(repo).resolve(), Path(path).resolve()
    common = repo / ".git"
    if not common.is_dir():
        try:
            out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                 env=env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            raise WonkaError(f"{repo} is not a git repository")
        common = Path(out.stdout.strip())
    if path == repo:
        return common
    wts = common / "worktrees"
    for d in sorted(wts.iterdir()) if wts.is_dir() else ():
        try:
            back = Path((d / "gitdir").read_text().strip())
        except OSError:
            continue
        back = back if back.is_absolute() else d / back
        if back.name == ".git" and back.parent.resolve() == path:
            return d
    raise WonkaError(f"{path} is not a registered worktree of {repo}")


def target(t, state, sub):
    """The directory git runs in: writes only in the worktree while its job runs; reads in the scout (the fresh
    checkout of the base that a triage or draft job reads, so it wins over an older worktree of a re-drafted
    ticket), else the worktree, else the repo."""
    wt = t.get("worktree")
    has_wt = bool(wt) and Path(wt).is_dir()
    if sub in WRITES:
        if state not in WRITE_STATES:
            raise WonkaError(f"git {sub} only runs while the ticket is baking or fixing merge conflicts "
                             f"(it is {fmt(state)})")
        if not has_wt:
            raise WonkaError(f"no worktree for this ticket: git {sub} only runs in its worktree")
        return Path(wt)
    scout = t.get("scout")
    if scout and Path(scout).is_dir():
        return Path(scout)
    if has_wt:
        return Path(wt)
    return Path(t["repo"])


def main(argv):
    """argv is everything after `wonka git`; returns git's exit code."""
    if len(argv) < 2:
        raise WonkaError("usage: wonka git <id> <subcommand> [args...]")
    tid, sub, args = argv[0], argv[1], list(argv[2:])
    check(sub, args)
    cfg = store.load_config()
    with store.state() as db:
        f = Factory(cfg, db)
        t = f.ticket(tid)
        path = target(t, f.state(tid), sub)
        repo = t["repo"]
    # the state lock is released: a commit hook may run for minutes
    try:
        gd = git_dir(repo, path)
        return subprocess.run(command(path, gd, sub, args), env=env(), stdin=subprocess.DEVNULL).returncode
    except FileNotFoundError:
        raise WonkaError("git is not installed")
