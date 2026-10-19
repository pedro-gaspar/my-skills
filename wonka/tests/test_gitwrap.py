"""wonka git: the subcommand and option checks, and the wrapper end to end against a real repo."""

import contextlib
import io
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import cli, factory, gitwrap, store
from wonka.factory import JOB_AGENTS, Factory
from wonka.gitwrap import check, command, env, target
from wonka.store import WonkaError


class CheckTest(unittest.TestCase):
    def test_allowed_commands_pass(self):
        for sub, args in (("diff", ["main...HEAD"]), ("log", ["--oneline", "main..HEAD"]), ("log", ["--all", "--stat"]),
                          ("status", ["--porcelain"]), ("show", ["HEAD", "--no-textconv"]), ("add", ["a.py", "b/c.py"]),
                          ("add", ["-v", "--", "-p"]), ("commit", ["-m", "fix push and config -c"]),
                          ("commit", ["-m", "--amend is gone"]), ("commit", ["-sm", "- bullet"]),
                          ("commit", ["--message", "--patch"]), ("commit", ["--message=x", "--no-edit"]),
                          ("commit", ["-mfix -p", "--no-edit"]), ("commit", ["--no-edit"]),
                          ("diff", ["--stat", "--", "--output"]), ("rev-parse", ["HEAD"]),
                          ("diff", ["--name-only", "--diff-filter=U"]),
                          ("merge", ["--no-edit", "origin/main"]), ("merge", ["--abort"]),
                          ("ls-files", ["--exclude", "*.pyc", "-o"]), ("ls-files", ["-x", "-X"]),
                          ("ls-files", ["-x*.pyc", "--exclude-standard", "-o"]), ("ls-files", ["--exclude=*.o"]),
                          ("blame", ["--ignore-rev", "HEAD~1", "README.md"]), ("blame", ["-L", "1,5", "README.md"]),
                          ("blame", ["-L/Sx/,+3", "f"]), ("log", ["-SOLD", "--oneline"]), ("diff", ["-GOx"]),
                          ("log", ["-n", "5"]), ("commit", ["-m", "x", "--pathspec-file-nul"]),
                          ("add", ["--no-pathspec-from-file", "a.py"])):
            with self.subTest(sub=sub, args=args):
                self.assertIsNone(check(sub, args))

    def test_other_subcommands_are_refused(self):
        for sub in ("config", "push", "reset", "grep", "difftool", "mergetool", "submodule", "rebase", "bisect",
                    "filter-branch", "archive", "fetch", "clone", "worktree", "-c", "-C", "--exec-path=/x",
                    "--git-dir=/x"):
            with self.subTest(sub=sub), self.assertRaises(WonkaError):
                check(sub, [])

    def test_risky_options_are_refused(self):
        for line in ("diff --output=x", "log --output x", "diff --ext-diff", "show --textconv", "diff --no-index a b",
                     "blame --contents f x", "commit --amend", "commit --am", "commit --amen=x", "commit -F f",
                     "commit -sF f", "commit -t f", "commit --template=f", "commit -p", "commit --interactive",
                     "add -p", "add -vp", "add -A", "add --all", "add --al", "add -i", "add -e", "add --patch",
                     "commit -m x --amend", "log --exec=x",
                     "add --pathspec-from-file=/etc/passwd", "add --pathspec-from-file /etc/passwd",
                     "add --pathspec-from=x", "commit -m x --pathspec-from-file /etc/passwd",
                     "commit --pathspec-from-file=x", "ls-files -X /etc/passwd", "ls-files -coX /etc/passwd",
                     "ls-files --exclude-from=/etc/passwd", "ls-files --exclude-fr x",
                     "ls-files --exclude-per-directory ../../etc/passwd", "ls-files --exclude-p=x",
                     "blame -S /etc/passwd f", "blame -wS /etc/passwd f", "blame --ignore-revs-file=/etc/x f",
                     "blame --ignore-revs f", "diff -O/etc/passwd", "log -p -O/etc/x", "show -O x",
                     "status --pathspec-from-file=x", "rev-parse --resolve-git-dir /home/u/other/.git",
                     "rev-parse --resolve-git-dir=/x/.git", "rev-parse --prefix /etc/ passwd",
                     "rev-parse --prefix=/etc/ passwd"):
            sub, *args = line.split()
            with self.subTest(line=line), self.assertRaises(WonkaError):
                check(sub, args)

    def test_diff_paths_must_stay_inside_the_tree(self):
        for args in (["/etc/hostname", "/dev/null"], ["--", "/etc/hostname", "/dev/null"],
                     ["--stat", "/home/u/.bashrc", "/dev/null"], ["../x", "a"], ["a/../../x"], ["HEAD", "--", ".."],
                     [".."]):
            with self.subTest(args=args), self.assertRaises(WonkaError):
                check("diff", args)
        for args in (["a", "b/c"], ["HEAD", "--", "src/x.py"], ["a/../b"], ["main...HEAD", "--", "."]):
            with self.subTest(args=args):
                self.assertIsNone(check("diff", args))

    def test_rev_parse_paths_must_stay_inside_the_tree(self):
        for args in (["/etc/passwd"], ["HEAD", "/etc/passwd"], ["../x"], ["a/../../x"], [".."],
                     ["--", "/etc/passwd"]):
            with self.subTest(args=args), self.assertRaises(WonkaError):
                check("rev-parse", args)
        for args in (["HEAD"], ["--abbrev-ref", "HEAD"], ["--show-toplevel"], ["--git-path", "hooks"],
                     ["HEAD:wonka/x.py"], ["main...HEAD"], ["--verify", "HEAD~1"]):
            with self.subTest(args=args):
                self.assertIsNone(check("rev-parse", args))

    def test_merge_only_takes_the_fixer_forms(self):
        for args in ([], ["origin/main"], ["--no-edit"], ["--no-edit", "-s", "ours"], ["--no-edit", "--squash"],
                     ["--no-edit", "a", "b"], ["--abort", "x"], ["-X", "theirs", "origin/main"]):
            with self.subTest(args=args), self.assertRaises(WonkaError):
                check("merge", args)

    def test_command_pins_path_and_global_options(self):
        self.assertEqual(command("/w", "/r/.git/worktrees/w", "diff", ["x"]),
                         ["git", "--no-pager", "-C", "/w", "--git-dir=/r/.git/worktrees/w", "--work-tree=/w",
                          "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", "x"])
        self.assertEqual(command("/w", "/r/.git/worktrees/w", "status", []),
                         ["git", "--no-pager", "-C", "/w", "--git-dir=/r/.git/worktrees/w", "--work-tree=/w",
                          "-c", "core.fsmonitor=false", "status"])

    def test_env_is_scrubbed(self):
        with mock.patch.object(gitwrap, "_passwd_home", return_value="/home/real"):
            out = env({"PATH": "/bin", "HOME": "/tmp/evil", "XDG_CONFIG_HOME": "/tmp/evil",
                       "GIT_DIR": "/x", "GIT_EXTERNAL_DIFF": "sh",
                       "GIT_CONFIG_PARAMETERS": "'core.pager'='sh'", "GIT_CONFIG_COUNT": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_CONFIG_GLOBAL": "/tmp/evil.gitconfig",
                       "GIT_CONFIG_SYSTEM": "/tmp/evil", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_KEY_0": "core.pager", "GIT_CONFIG_VALUE_0": "sh", "GIT_COMMITTER_EMAIL": "t@t"})
        self.assertEqual(out, {"PATH": "/bin", "HOME": "/home/real", "GIT_AUTHOR_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "GIT_EDITOR": "true", "GIT_PAGER": "cat",
                               "GIT_TERMINAL_PROMPT": "0"})

    def test_env_without_passwd_entry_ignores_global_config(self):
        with mock.patch.object(gitwrap, "_passwd_home", return_value=None):
            out = env({"PATH": "/bin", "HOME": "/tmp/evil", "XDG_CONFIG_HOME": "/tmp/evil"})
        self.assertNotIn("HOME", out)
        self.assertNotIn("XDG_CONFIG_HOME", out)
        self.assertEqual(out["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_passwd_home_ignores_HOME(self):
        with mock.patch.dict(os.environ, {"HOME": "/tmp/evil"}):
            self.assertEqual(gitwrap._passwd_home(), pwd.getpwuid(os.getuid()).pw_dir)

    def test_write_states_follow_the_job_map(self):
        self.assertEqual(gitwrap.WRITE_STATES, {JOB_AGENTS["oompa-loompa-builder"], JOB_AGENTS["oompa-loompa-fixer"]})
        self.assertEqual(gitwrap.WRITE_STATES, {("baking", "progress"), ("shipped", "progress")})

    def test_target(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wt, scout, repo = tmp / "wt", tmp / "scout", tmp / "repo"
        wt.mkdir()
        scout.mkdir()
        baking = ("baking", "progress")
        self.assertEqual(target({"worktree": str(wt), "repo": str(repo)}, ("reviewing", "progress"), "diff"), wt)
        self.assertEqual(target({"worktree": None, "repo": str(repo)}, ("inbox", "pending"), "log"), repo)
        self.assertEqual(target({"worktree": str(tmp / "gone"), "repo": str(repo)}, baking, "log"), repo)
        self.assertEqual(target({"worktree": None, "scout": str(scout), "repo": str(repo)},
                                ("drafting", "progress"), "log"), scout)
        # a re-drafted ticket keeps its old worktree: reads go to the fresh scout the analyst's job reads
        self.assertEqual(target({"worktree": str(wt), "scout": str(scout), "repo": str(repo)},
                                ("drafting", "progress"), "log"), scout)
        self.assertEqual(target({"worktree": str(wt), "scout": str(tmp / "gone"), "repo": str(repo)},
                                ("reviewing", "progress"), "log"), wt)
        self.assertEqual(target({"worktree": str(wt), "scout": str(scout), "repo": str(repo)}, baking, "commit"), wt)
        self.assertEqual(target({"worktree": str(wt), "repo": str(repo)}, baking, "commit"), wt)
        self.assertEqual(target({"worktree": str(wt), "repo": str(repo)}, ("shipped", "progress"), "merge"), wt)
        for state in (("reviewing", "progress"), ("drafting", "progress"), ("shipped", "awaiting")):
            with self.subTest(state=state), self.assertRaises(WonkaError):
                target({"worktree": str(wt), "repo": str(repo)}, state, "add")
        with self.assertRaises(WonkaError):
            target({"worktree": None, "repo": str(repo)}, baking, "commit")


def sh(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# `pr list` prints nothing, so ship() creates the PR; `pr view` always reports conflicts
FAKE_GH = """#!/bin/sh
case "$1 $2" in
  "pr create") echo "https://github.com/acme/app/pull/7" ;;
  "pr view") echo "OPEN CONFLICTING" ;;
esac
exit 0
"""


class WrapperTest(unittest.TestCase):
    """A minimal copy of FactoryTest.setUp: a real repo with a local bare origin."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "app"
        env_ = {"WONKA_HOME": str(self.tmp / "home"), "WONKA_NO_NOTIFY": "1", "WONKA_MDLINT": "off",
                "WONKA_GH": "false",
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        patcher = mock.patch.dict(os.environ, env_)
        patcher.start()
        self.addCleanup(patcher.stop)
        sh("git", "init", "-q", "--bare", str(self.tmp / "origin.git"), cwd=self.tmp)
        self.repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "README.md").write_text("app\n")
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "commit", "-qm", "init", cwd=self.repo)
        sh("git", "remote", "add", "origin", str(self.tmp / "origin.git"), cwd=self.repo)
        sh("git", "push", "-qu", "origin", "main", cwd=self.repo)
        self.cfg = {**store.DEFAULT_CONFIG, "vault": str(self.tmp / "vault")}
        store.save_config(self.cfg)

    @contextlib.contextmanager
    def f(self):
        with store.state() as db:
            yield Factory(self.cfg, db)

    def add(self):
        with self.f() as f:
            return f.add("Add a hello script", repo=str(self.repo), pr_repo="acme/app")

    def tick(self):
        with self.f() as f:
            return f.tick()

    def state(self, tid):
        with self.f() as f:
            return f.state(tid)

    def to_ready(self, tid):
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        with self.f() as f:
            f.write_plan(tid, "# plan\n")
            f.set_section(tid, "spec", "Add hello.sh")
            f.drafted(tid, "s", ["architect"])
        self.assertEqual(self.state(tid), ("ready", "pending"))

    def to_baking(self, tid):
        self.to_ready(tid)
        job = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(job["job"], "bake")
        return Path(job["worktree"])

    def git(self, *argv):
        """cli.main(["git", ...]) with stderr captured: (exit code, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["git", *argv])
        return code, err.getvalue()

    def test_builder_commits_in_the_worktree(self):
        tid = self.add()
        wt = self.to_baking(tid)
        head, status = sh("git", "rev-parse", "HEAD", cwd=self.repo), sh("git", "status", "--porcelain", cwd=self.repo)
        (wt / "hello.sh").write_text("echo hi\n")
        self.assertEqual(self.git(tid, "add", "hello.sh")[0], 0)
        self.assertEqual(self.git(tid, "commit", "-q", "-m", "bake")[0], 0)
        self.assertEqual(sh("git", "rev-list", "--count", "origin/main..HEAD", cwd=wt), "1")
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.repo), head)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.repo), status)

    def test_write_refused_outside_baking(self):
        tid = self.add()
        self.to_ready(tid)
        code, err = self.git(tid, "commit", "-m", "x")
        self.assertEqual(code, 1)
        self.assertIn("only runs while the ticket is baking", err)

    def test_read_runs_in_repo_without_worktree(self):
        tid = self.add()
        with mock.patch.object(gitwrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(self.git(tid, "log", "--oneline")[0], 0)
        argv, kw = run.call_args[0][0], run.call_args[1]
        self.assertEqual(argv[argv.index("-C") + 1], str(self.repo))
        self.assertEqual(kw["env"]["GIT_PAGER"], "cat")
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_write_refused_on_a_protected_branch(self):
        tid = self.add()
        self.to_baking(tid)
        with self.f() as f:
            f.ticket(tid)["branch"] = "main"
        with mock.patch.object(gitwrap.subprocess, "run") as run:
            code, err = self.git(tid, "commit", "-m", "x")
        self.assertEqual(code, 1)
        self.assertIn("refusing to write to branch main", err)
        run.assert_not_called()

    def test_write_refused_when_the_worktree_left_its_branch(self):
        tid = self.add()
        wt = self.to_baking(tid)
        sh("git", "checkout", "-q", "--detach", cwd=wt)
        (wt / "hello.sh").write_text("echo hi\n")
        code, err = self.git(tid, "add", "hello.sh")
        self.assertEqual(code, 1)
        self.assertIn("but the worktree is on a detached HEAD", err)
        sh("git", "checkout", "-q", "-b", "other", cwd=wt)
        code, err = self.git(tid, "commit", "-m", "x")
        self.assertEqual(code, 1)
        self.assertIn("but the worktree is on other", err)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=wt), "?? hello.sh")

    def test_exit_code_passes_through(self):
        tid = self.add()
        with mock.patch.object(gitwrap.subprocess, "run", return_value=mock.Mock(returncode=128)):
            self.assertEqual(self.git(tid, "status")[0], 128)

    def test_refused_option_never_runs_git(self):
        tid = self.add()
        with mock.patch.object(gitwrap.subprocess, "run") as run:
            self.assertEqual(self.git(tid, "diff", "--output=/tmp/x")[0], 1)
        run.assert_not_called()
        self.assertIn(f"git {tid} diff", (self.tmp / "home" / "factory.log").read_text())

    def test_diff_outside_the_tree_never_runs_git(self):
        tid = self.add()
        with mock.patch.object(gitwrap.subprocess, "run") as run:
            code, err = self.git(tid, "diff", "/etc/hostname", "/dev/null")
        self.assertEqual(code, 1)
        self.assertIn("inside the worktree", err)
        run.assert_not_called()

    def test_usage(self):
        self.assertEqual(self.git()[0], 1)
        self.assertEqual(self.git("WONKA-00001")[0], 1)
        code, err = self.git("WONKA-09999", "status")
        self.assertEqual(code, 1)
        self.assertIn("No ticket", err)

    def test_git_dir_is_found_from_the_repo_side(self):
        tid = self.add()
        wt = self.to_baking(tid)
        real = gitwrap.git_dir(self.repo, wt)
        self.assertEqual(real.parent, (self.repo / ".git" / "worktrees").resolve())
        self.assertEqual(gitwrap.git_dir(self.repo, self.repo), (self.repo / ".git").resolve())
        stray = self.tmp / "stray"
        stray.mkdir()
        sh("git", "init", "-q", cwd=stray)
        with self.assertRaises(WonkaError):
            gitwrap.git_dir(self.repo, stray)

    def test_rewritten_dot_git_file_is_ignored(self):
        """An agent repoints <wt>/.git at a git dir it planted (here: one whose clean filter writes a file).
        wonka git keeps using the worktree's registered git dir, so the planted config never runs."""
        tid = self.add()
        wt = self.to_baking(tid)
        head = sh("git", "rev-parse", "HEAD", cwd=wt)
        evil = wt / "evil.gitdir"
        sh("git", "clone", "-q", "--bare", str(self.repo), str(evil), cwd=self.tmp)
        pwned = self.tmp / "pwned"
        for key, value in (("core.bare", "false"), ("core.worktree", str(wt)),
                           ("filter.x.clean", f"touch {pwned}; cat")):
            sh("git", "config", "-f", str(evil / "config"), key, value, cwd=self.tmp)
        sh("git", "--git-dir", str(evil), "read-tree", "HEAD", cwd=self.tmp)
        (wt / ".gitattributes").write_text("* filter=x\n")
        (wt / ".git").write_text(f"gitdir: {evil}\n")
        (wt / "README.md").write_text("changed\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.git(tid, "diff", "--stat")[0], 0)
            self.assertEqual(self.git(tid, "status")[0], 0)
        self.assertFalse(pwned.exists())
        with mock.patch.object(gitwrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.git(tid, "rev-parse", "HEAD")
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("-C") + 1], str(wt))
        self.assertIn(f"--git-dir={gitwrap.git_dir(self.repo, wt)}", argv)
        self.assertEqual(sh("git", "--git-dir", str(gitwrap.git_dir(self.repo, wt)), "rev-parse", "HEAD", cwd=wt), head)

    def test_global_config_from_env_is_ignored(self):
        tid = self.add()
        cfg = self.tmp / "evil.gitconfig"
        cfg.write_text("[core]\n\tpager = sh\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1"}), \
                mock.patch.object(gitwrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(self.git(tid, "log", "--oneline")[0], 0)
        kw = run.call_args[1]
        self.assertNotIn("GIT_CONFIG_GLOBAL", kw["env"])
        self.assertNotIn("GIT_CONFIG_NOSYSTEM", kw["env"])
        self.assertEqual(kw["env"]["GIT_AUTHOR_NAME"], "t")

    def test_global_config_filter_never_runs(self):
        """A caller points GIT_CONFIG_GLOBAL at a config whose clean filter writes a file: wonka git drops it."""
        tid = self.add()
        wt = self.to_baking(tid)
        pwned = self.tmp / "pwned"
        cfg = self.tmp / "evil.gitconfig"
        cfg.write_text(f'[filter "x"]\n\tclean = touch {pwned}; cat\n')
        (wt / ".gitattributes").write_text("* filter=x\n")
        (wt / "README.md").write_text("changed\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.git(tid, "diff", "--stat")[0], 0)
            self.assertEqual(self.git(tid, "status")[0], 0)
        self.assertFalse(pwned.exists())

    def test_home_and_xdg_config_from_env_are_ignored(self):
        tid = self.add()
        real = self.tmp / "realhome"
        real.mkdir()
        with mock.patch.dict(os.environ, {"HOME": "/tmp/evil", "XDG_CONFIG_HOME": "/tmp/evil"}), \
                mock.patch.object(gitwrap, "_passwd_home", return_value=str(real)), \
                mock.patch.object(gitwrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(self.git(tid, "log", "--oneline")[0], 0)
        kw = run.call_args[1]
        self.assertEqual(kw["env"]["HOME"], str(real))
        self.assertNotIn("XDG_CONFIG_HOME", kw["env"])

    def test_home_and_xdg_config_filters_never_run(self):
        """A caller points HOME and XDG_CONFIG_HOME at configs whose clean filter writes a file: wonka git ignores them."""
        tid = self.add()
        wt = self.to_baking(tid)
        pwned = self.tmp / "pwned"
        evil, xdg = self.tmp / "evil", self.tmp / "xdg"
        (xdg / "git").mkdir(parents=True)
        evil.mkdir()
        real = self.tmp / "realhome"
        real.mkdir()
        body = f'[filter "x"]\n\tclean = touch {pwned}; cat\n'
        (evil / ".gitconfig").write_text(body)
        (xdg / "git" / "config").write_text(body)
        (wt / ".gitattributes").write_text("* filter=x\n")
        (wt / "README.md").write_text("changed\n")
        with mock.patch.dict(os.environ, {"HOME": str(evil), "XDG_CONFIG_HOME": str(xdg)}), \
                mock.patch.object(gitwrap, "_passwd_home", return_value=str(real)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.git(tid, "diff", "--stat")[0], 0)
            self.assertEqual(self.git(tid, "status")[0], 0)
        self.assertFalse(pwned.exists())

    def test_injected_flags_work_with_real_git(self):
        tid = self.add()
        self.to_baking(tid)
        with contextlib.redirect_stdout(io.StringIO()):
            for sub in ("log", "show", "diff"):
                with self.subTest(sub=sub):
                    self.assertEqual(self.git(tid, sub, "HEAD")[0], 0)
            self.assertEqual(self.git(tid, "blame", "README.md")[0], 0)

    def to_fixing(self, conflict=True):
        """Bake, review, ship, then push a change to main: the next tick hands the PR to a fixer.
        Returns (tid, worktree, base_ref) with the ticket in shipped/progress."""
        gh = self.tmp / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        os.environ["WONKA_GH"] = str(gh)  # the setUp patcher restores os.environ
        tid = self.add()
        wt = self.to_baking(tid)
        (wt / "hello.sh").write_text("echo hi\n")
        sh("git", "add", "hello.sh", cwd=wt)
        sh("git", "commit", "-qm", "bake", cwd=wt)
        with self.f() as f:
            f.baked(tid)
        self.tick()
        with self.f() as f:
            f.report(tid, "architect", {"verdict": "good nut"})
        self.tick()
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))
        name = "hello.sh" if conflict else "other.txt"
        sh("git", "checkout", "-q", "main", cwd=self.repo)
        (self.repo / name).write_text("echo upstream\n")
        sh("git", "add", name, cwd=self.repo)
        sh("git", "commit", "-qm", f"upstream {name}", cwd=self.repo)
        sh("git", "push", "-q", "origin", "main", cwd=self.repo)
        job = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(job["job"], "conflicts")
        self.assertEqual(self.state(tid), ("shipped", "progress"))
        return tid, wt, job["base_ref"]

    def hostile_git(self, *argv):
        """self.git under a caller environment that breaks git unless the wrapper scrubs it and sets GIT_EDITOR.
        EDITOR/VISUAL are set to false (not left unset) so a regression fails fast instead of opening vi."""
        hostile = {"GIT_EDITOR": "false", "EDITOR": "false", "VISUAL": "false",
                   "GIT_DIR": str(self.tmp / "nowhere"), "GIT_WORK_TREE": str(self.tmp),
                   "GIT_INDEX_FILE": str(self.tmp / "stray-index"),
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        with mock.patch.dict(os.environ, hostile), contextlib.redirect_stdout(io.StringIO()):
            return self.git(*argv)[0]

    def conflicted(self):
        """to_fixing() plus a conflicting merge through the wrapper: (tid, wt, base_ref, head before the merge)."""
        tid, wt, ref = self.to_fixing()
        head = sh("git", "rev-parse", "HEAD", cwd=wt)
        self.assertEqual(self.hostile_git(tid, "merge", "--no-edit", ref), 1)
        self.assertTrue(factory._ref_exists(wt, "MERGE_HEAD"))
        return tid, wt, ref, head

    def test_fixer_merges_and_commits_through_wonka_git(self):
        tid, wt, ref, head = self.conflicted()
        main_head = sh("git", "rev-parse", "HEAD", cwd=self.repo)
        status = sh("git", "status", "--porcelain", cwd=self.repo)
        (wt / "hello.sh").write_text("echo hi\necho upstream\n")
        self.assertEqual(self.hostile_git(tid, "add", "hello.sh"), 0)
        self.assertEqual(self.hostile_git(tid, "commit", "--no-edit"), 0)
        self.assertFalse(factory._ref_exists(wt, "MERGE_HEAD"))
        self.assertEqual(sh("git", "rev-parse", "HEAD^1", "HEAD^2", cwd=wt).split(),
                         [head, sh("git", "rev-parse", ref, cwd=wt)])
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.repo), main_head)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.repo), status)
        with self.f() as f:
            f.resolved(tid)
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))

    def test_plain_commit_finishes_a_merge_with_the_wrapper_editor(self):
        """Fails if env() stops setting GIT_EDITOR=true: git would run the caller's `false` editor."""
        tid, wt, _, _ = self.conflicted()
        (wt / "hello.sh").write_text("echo hi\necho upstream\n")
        self.assertEqual(self.hostile_git(tid, "add", "hello.sh"), 0)
        self.assertEqual(self.hostile_git(tid, "commit"), 0)
        self.assertTrue(sh("git", "log", "-1", "--format=%s", cwd=wt).startswith("Merge"))

    def test_fixer_merge_abort_through_wonka_git(self):
        tid, wt, _, head = self.conflicted()
        self.assertEqual(self.hostile_git(tid, "merge", "--abort"), 0)
        self.assertFalse(factory._ref_exists(wt, "MERGE_HEAD"))
        self.assertEqual(sh("git", "status", "--porcelain", cwd=wt), "")
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=wt), head)

    def test_clean_fixer_merge_through_wonka_git(self):
        tid, wt, ref = self.to_fixing(conflict=False)
        self.assertEqual(self.hostile_git(tid, "merge", "--no-edit", ref), 0)
        self.assertEqual(sh("git", "rev-parse", "HEAD^2", cwd=wt), sh("git", "rev-parse", ref, cwd=wt))
        self.assertFalse(factory._ref_exists(wt, "MERGE_HEAD"))


if __name__ == "__main__":
    unittest.main()
