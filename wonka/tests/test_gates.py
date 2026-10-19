"""The factory's safety gates: what baked, drafted, resolved and report refuse, the branches Wonka never writes, and
where the fixer fetches a PR's base from."""

import unittest
from unittest import mock

from wonka import factory
from wonka.store import WonkaError

from .fixtures import RepoFixture, sh


class BakedGateTest(RepoFixture, unittest.TestCase):
    def test_uncommitted_changes_are_refused(self):
        tid = self.add()
        wt = self.to_baking(tid)
        (wt / "hello.sh").write_text("echo hi\n")
        sh("git", "add", "hello.sh", cwd=wt)
        sh("git", "commit", "-qm", "bake", cwd=wt)
        (wt / "left-over.txt").write_text("not committed\n")
        with self.f() as f, self.assertRaisesRegex(WonkaError, "uncommitted changes: commit them first"):
            f.baked(tid, self.dispatch)
        self.assertEqual(self.state(tid), ("baking", "progress"))

    def test_no_commits_on_the_branch_is_refused(self):
        tid = self.add()
        wt = self.to_baking(tid)
        with self.f() as f, self.assertRaisesRegex(WonkaError, "no commits on wonka/.* yet"):
            f.baked(tid, self.dispatch)
        self.assertEqual(self.state(tid), ("baking", "progress"))
        self.assertEqual(sh("git", "status", "--porcelain", cwd=wt), "")


class DraftedGateTest(RepoFixture, unittest.TestCase):
    def drafting(self):
        tid = self.add()
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        self.assertEqual(self.state(tid), ("drafting", "progress"))
        return tid

    def test_unknown_reviewers_are_refused(self):
        tid = self.drafting()
        with self.f() as f:
            f.write_plan(tid, "# plan\n")
            f.set_section(tid, "spec", "Add hello.sh")
        with self.f() as f, self.assertRaisesRegex(WonkaError, r"unknown reviewers \['ux'\]"):
            f.drafted(tid, "s", ["architect", "ux"])
        self.assertEqual(self.state(tid), ("drafting", "progress"))

    def test_no_plan_is_refused(self):
        tid = self.drafting()
        with self.f() as f:
            f.set_section(tid, "spec", "Add hello.sh")
        with self.f() as f, self.assertRaisesRegex(WonkaError, "write the detailed plan first"):
            f.drafted(tid, "s", ["architect"])
        self.assertEqual(self.state(tid), ("drafting", "progress"))

    def test_no_spec_is_refused(self):
        tid = self.drafting()
        with self.f() as f:
            f.write_plan(tid, "# plan\n")
        with self.f() as f, self.assertRaisesRegex(WonkaError, "add the Spec first"):
            f.drafted(tid, "s", ["architect"])
        self.assertEqual(self.state(tid), ("drafting", "progress"))


class ResolvedAndReportGateTest(RepoFixture, unittest.TestCase):
    def baked(self):
        tid = self.add()
        wt = self.to_baking(tid)
        (wt / "hello.sh").write_text("echo hi\n")
        sh("git", "add", "hello.sh", cwd=wt)
        sh("git", "commit", "-qm", "bake", cwd=wt)
        with self.f() as f:
            f.baked(tid, self.dispatch)
        return tid, wt

    def test_resolved_refused_when_not_fixing_conflicts(self):
        tid, _ = self.baked()
        with self.f() as f:
            f.ticket(tid)["last_valid"] = {"status": "shipped", "phase": "progress"}
            f.ticket(tid)["merge_ref"] = "origin/main"
        with self.f() as f, mock.patch.object(factory, "git", wraps=factory.git) as git:
            with self.assertRaisesRegex(WonkaError, "is not fixing merge conflicts"):
                f.resolved(tid)
        self.assertNotIn("push", [c.args[0] for c in git.call_args_list])
        self.assertEqual(self.state(tid), ("shipped", "progress"))

    def test_report_from_an_unknown_squirrel_is_refused(self):
        tid, _ = self.baked()
        job = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(job["job"], "review")
        with self.f() as f, self.assertRaisesRegex(WonkaError, "unknown squirrel ux; one of "):
            f.report(tid, "ux", {"verdict": "good nut"}, job["dispatch"])
        with self.f() as f:
            self.assertNotIn("ux", f.ticket(tid).get("reports", {}))
        self.assertEqual(self.state(tid), ("reviewing", "progress"))


class CheckBranchTest(unittest.TestCase):
    def test_the_pr_base_is_never_written(self):
        """A stacked PR's base is another wonka/ branch: it is still never committed to or pushed."""
        with self.assertRaisesRegex(WonkaError, "refusing to write to branch wonka/a"):
            factory.check_branch({"branch": "wonka/a", "base_branch": "main", "pr_base": "wonka/a"})
        with self.assertRaisesRegex(WonkaError, "refusing to write to branch wonka/a"):
            factory.check_branch({"branch": "wonka/a", "base_branch": "wonka/a"})
        self.assertIsNone(factory.check_branch({"branch": "wonka/b", "base_branch": "main", "pr_base": "wonka/a"}))
        self.assertIsNone(factory.check_branch({"branch": "wonka/b", "base_branch": "main"}))


class FetchPrBaseTest(RepoFixture, unittest.TestCase):
    """The fixer rebases onto the base GitHub compares against: from origin, or from the PR repo when origin is a fork."""

    def fetch(self, origin_url, **fields):
        tid = self.add()
        calls = []

        def git(*args, cwd):
            calls.append(args)
            if args[:3] == ("remote", "get-url", "origin"):
                if origin_url is None:
                    raise WonkaError("no such remote 'origin'")
                return origin_url
            return ""

        with self.f() as f, mock.patch.object(factory, "git", side_effect=git):
            f.ticket(tid).update({"worktree": str(self.repo), "base_branch": "main", **fields})
            ref = f._fetch_pr_base(tid)
        return ref, [c for c in calls if c[0] == "fetch"]

    def test_fork_origin_fetches_from_the_pr_repo(self):
        ref, fetches = self.fetch("git@github.com:someone/app.git")
        self.assertEqual(ref, "wonka-pr/main")
        self.assertEqual(fetches, [("fetch", "--quiet", "https://github.com/acme/app.git",
                                    "+refs/heads/main:refs/remotes/wonka-pr/main")])

    def test_fork_origin_fetches_the_pr_base(self):
        ref, fetches = self.fetch("https://github.com/someone/app", pr_base="release")
        self.assertEqual(ref, "wonka-pr/release")
        self.assertEqual(fetches, [("fetch", "--quiet", "https://github.com/acme/app.git",
                                    "+refs/heads/release:refs/remotes/wonka-pr/release")])

    def test_origin_that_is_the_pr_repo_or_not_github_fetches_from_origin(self):
        for url in ("git@github.com:acme/app.git", "https://github.com/acme/app", "/srv/git/app.git", None):
            with self.subTest(url=url):
                ref, fetches = self.fetch(url, pr_base="release")
                self.assertEqual(ref, "origin/release")
                self.assertEqual(fetches, [("fetch", "--quiet", "origin", "release")])


if __name__ == "__main__":
    unittest.main()
