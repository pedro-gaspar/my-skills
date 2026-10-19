"""Shared test fixtures: a real repo with a local bare origin and a Factory driven through its states."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from wonka import store
from wonka.factory import Factory


def sh(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@contextlib.contextmanager
def quiet_output():
    """Silence stdout and stderr at the file descriptors, so the output of subprocesses that inherit them (wonka git
    runs git with the caller's stdout and stderr) is dropped; contextlib.redirect_stdout/stderr only catch Python's
    own writes. sys.stdout is redirected too; sys.stderr is left to the caller (who usually captures it)."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    try:
        with open(os.devnull, "w") as null:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        for fd, copy in zip((1, 2), saved):
            os.dup2(copy, fd)
            os.close(copy)


class RepoFixture:
    """Mixin for unittest.TestCase: a real repo with a local bare origin. List it before TestCase in the bases."""

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

    def to_planned(self, tid):
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        with self.f() as f:
            f.write_plan(tid, "# plan\n")
            f.set_section(tid, "spec", "Add hello.sh")
            f.drafted(tid, "s", ["architect"])
        self.assertEqual(self.state(tid), ("planned", "pending"))

    def to_baking(self, tid):
        self.to_planned(tid)
        job = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(job["job"], "bake")
        self.dispatch = job["dispatch"]
        return Path(job["worktree"])
