"""wonka git: the subcommand and option checks, and the wrapper end to end against a real repo."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import cli, gitwrap, store
from wonka.factory import Factory
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
                          ("merge", ["--no-edit", "origin/main"]), ("merge", ["--abort"])):
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
                     "commit -m x --amend", "log --exec=x"):
            sub, *args = line.split()
            with self.subTest(line=line), self.assertRaises(WonkaError):
                check(sub, args)

    def test_merge_only_takes_the_fixer_forms(self):
        for args in ([], ["origin/main"], ["--no-edit"], ["--no-edit", "-s", "ours"], ["--no-edit", "--squash"],
                     ["--no-edit", "a", "b"], ["--abort", "x"], ["-X", "theirs", "origin/main"]):
            with self.subTest(args=args), self.assertRaises(WonkaError):
                check("merge", args)

    def test_command_pins_path_and_global_options(self):
        self.assertEqual(command("/w", "diff", ["x"]),
                         ["git", "--no-pager", "-C", "/w", "-c", "core.fsmonitor=false", "diff", "--no-ext-diff",
                          "--no-textconv", "x"])
        self.assertEqual(command("/w", "status", []),
                         ["git", "--no-pager", "-C", "/w", "-c", "core.fsmonitor=false", "status"])

    def test_env_is_scrubbed(self):
        out = env({"PATH": "/bin", "GIT_DIR": "/x", "GIT_EXTERNAL_DIFF": "sh",
                   "GIT_CONFIG_PARAMETERS": "'core.pager'='sh'", "GIT_CONFIG_COUNT": "1", "GIT_AUTHOR_NAME": "t"})
        self.assertEqual(out, {"PATH": "/bin", "GIT_AUTHOR_NAME": "t", "GIT_EDITOR": "true", "GIT_PAGER": "cat",
                               "GIT_TERMINAL_PROMPT": "0"})

    def test_target(self):
        tmp = Path(tempfile.mkdtemp())
        wt, scout, repo = tmp / "wt", tmp / "scout", tmp / "repo"
        wt.mkdir()
        scout.mkdir()
        baking = ("baking", "progress")
        self.assertEqual(target({"worktree": str(wt), "repo": str(repo)}, ("reviewing", "progress"), "diff"), wt)
        self.assertEqual(target({"worktree": None, "repo": str(repo)}, ("inbox", "pending"), "log"), repo)
        self.assertEqual(target({"worktree": str(tmp / "gone"), "repo": str(repo)}, baking, "log"), repo)
        self.assertEqual(target({"worktree": None, "scout": str(scout), "repo": str(repo)},
                                ("drafting", "progress"), "log"), scout)
        self.assertEqual(target({"worktree": str(wt), "repo": str(repo)}, baking, "commit"), wt)
        self.assertEqual(target({"worktree": str(wt), "repo": str(repo)}, ("shipped", "progress"), "merge"), wt)
        for state in (("reviewing", "progress"), ("drafting", "progress"), ("shipped", "awaiting")):
            with self.subTest(state=state), self.assertRaises(WonkaError):
                target({"worktree": str(wt), "repo": str(repo)}, state, "add")
        with self.assertRaises(WonkaError):
            target({"worktree": None, "repo": str(repo)}, baking, "commit")


def sh(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class WrapperTest(unittest.TestCase):
    """A minimal copy of FactoryTest.setUp: a real repo with a local bare origin."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
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

    def test_usage(self):
        self.assertEqual(self.git()[0], 1)
        self.assertEqual(self.git("WONKA-00001")[0], 1)
        code, err = self.git("WONKA-09999", "status")
        self.assertEqual(code, 1)
        self.assertIn("No ticket", err)

    def test_injected_flags_work_with_real_git(self):
        tid = self.add()
        self.to_baking(tid)
        with contextlib.redirect_stdout(io.StringIO()):
            for sub in ("log", "show", "diff"):
                with self.subTest(sub=sub):
                    self.assertEqual(self.git(tid, sub, "HEAD")[0], 0)
            self.assertEqual(self.git(tid, "blame", "README.md")[0], 0)


if __name__ == "__main__":
    unittest.main()
