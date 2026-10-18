"""The factory end to end, against a real git repo, a local 'origin' and a fake gh."""

import contextlib
import datetime as dt
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import factory, note, store
from wonka.factory import Factory
from wonka.store import WonkaError

FAKE_GH = """#!/bin/sh
case "$1 $2" in
  "pr create") echo "https://github.com/acme/app/pull/7" ;;
  "pr view") cat "$WONKA_FAKE_PR_STATE" ;;
esac
"""


def sh(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FactoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "app"
        gh = self.tmp / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.pr_state = self.tmp / "pr_state"
        self.pr_state.write_text("OPEN")
        env = {"WONKA_HOME": str(self.tmp / "home"), "WONKA_NO_NOTIFY": "1", "WONKA_GH": str(gh),
               "WONKA_FAKE_PR_STATE": str(self.pr_state),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        patcher = mock.patch.dict(os.environ, env)
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

    def add(self, idea="Add a hello script", **kw):
        with self.f() as f:
            return f.add(idea, repo=str(self.repo), pr_repo="acme/app", **kw)

    def tick(self):
        with self.f() as f:
            return f.tick()

    def state(self, tid):
        with self.f() as f:
            return f.state(tid)

    def note(self, tid):
        with self.f() as f:
            return f.read(tid)

    def edit_note(self, tid, fn):
        with self.f() as f:
            path = f.path(tid)
        path.write_text(fn(path.read_text()))

    def to_ready(self, tid, size="s", reviewers=("architect",)):
        """Walk a fresh ticket through triage and drafting."""
        self.tick()
        with self.f() as f:
            f.triage(tid, size)
        jobs = self.tick()["jobs"]
        self.assertEqual([j["job"] for j in jobs if j["id"] == tid], ["draft"])
        with self.f() as f:
            f.plan_path(tid).parent.mkdir(parents=True, exist_ok=True)
            f.plan_path(tid).write_text("# plan\n")
            f.set_section(tid, "spec", "Add hello.sh")
            f.drafted(tid, size, list(reviewers))
        self.assertEqual(self.state(tid), ("ready", "pending"))

    def bake(self, tid, filename="hello.sh"):
        jobs = [j for j in self.tick()["jobs"] if j["id"] == tid]
        self.assertEqual(jobs[0]["job"], "bake")
        wt = Path(jobs[0]["worktree"])
        (wt / filename).write_text("echo hi\n")
        sh("git", "add", ".", cwd=wt)
        sh("git", "commit", "-qm", "bake", cwd=wt)
        with self.f() as f:
            f.baked(tid)
        return wt

    def review(self, tid, **reports):
        job = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(job["job"], "review")
        with self.f() as f:
            for name, rep in reports.items():
                f.report(tid, name, rep)
        return job

    def test_xs_ticket_goes_from_inbox_to_archive(self):
        tid = self.add()
        self.assertEqual(tid, "WONKA-00001")
        self.assertEqual(self.tick()["jobs"][0]["agents"], ["grandpa-joe"])
        self.to_ready(tid, reviewers=("architect", "security"))
        self.assertIn("[[WONKA-00001-plan]]", note.get_section(self.note(tid)[1], "spec"))

        wt = self.bake(tid)
        job = self.review(tid, architect={"verdict": "good nut"},
                          security={"verdict": "bad nut", "issues": ["hello.sh:1: no shebang → add one"],
                                    "followups": [{"title": "Add a bye script", "idea": "bye.sh"}]})
        self.assertEqual(job["agents"], ["squirrel-architect", "squirrel-security"])
        self.assertEqual(self.state(tid), ("baking", "pending"))
        self.assertEqual(self.state("WONKA-00002"), ("inbox", "pending"))
        with self.f() as f:
            self.assertIn("no shebang", Path(f.ticket(tid)["plan"]).read_text())

        self.bake(tid, "fix.sh")
        self.review(tid, architect={"verdict": "good nut"}, security={"verdict": "good nut"})
        self.assertEqual(self.state(tid), ("shipped", "pending"))

        self.tick()
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))
        self.assertTrue(wt.exists(), "worktree lives until shipped")
        self.assertIn("pull/7", self.note(tid)[1])

        self.pr_state.write_text("MERGED")
        self.tick()
        self.assertEqual(self.state(tid), ("shipped", "done"))
        self.assertFalse(wt.exists())
        meta = self.note(tid)[0]
        self.assertEqual((meta["status"], meta["sub-status"]), ("🛗-Shipped", "🌟-Done"))

        later = factory.now() + dt.timedelta(days=8)
        with mock.patch.object(factory, "now", lambda: later):
            self.assertIn(f"{tid}: archived", self.tick()["events"])
        self.assertTrue((Path(self.cfg["vault"]) / "wonka/archive" / f"{tid}.md").exists())

    def test_illegal_drag_is_reverted_but_wont_do_is_allowed(self):
        a, b = self.add(), self.add("Second")
        self.edit_note(a, lambda t: t.replace("✉️-Inbox", "🍫-Baking"))
        self.edit_note(b, lambda t: t.replace("✉️-Inbox", "🫐-Wont-Do"))
        events = self.tick()["events"]
        self.assertIn(f"{a}: reverted illegal 🍫-Baking/🕰️-Pending", events)
        self.assertEqual(self.note(a)[0]["status"], "✉️-Inbox")
        self.assertIn("Reverted", self.note(a)[1])
        self.assertEqual(self.state(b), ("wontdo", "done"))

    def test_medium_ticket_waits_for_approval_and_answers(self):
        tid = self.add()
        self.tick()
        with self.f() as f:
            f.triage(tid, "m", questions=[{"q": "Bash or Python?", "options": ["Bash", "Python"], "recommended": 0}])
        self.assertEqual(self.state(tid), ("inbox", "awaiting"))
        self.edit_note(tid, lambda t: t.replace("- [ ] Approve", "- [x] Approve"))
        self.tick()
        self.assertEqual(self.state(tid), ("inbox", "awaiting"), "a question is still open")
        self.edit_note(tid, lambda t: t.replace("- [ ] Bash", "- [x] Bash"))
        self.tick()
        self.assertEqual(self.state(tid), ("drafting", "progress"), "approved, promoted and claimed in one tick")

    def test_drafting_questions_resume_charlie(self):
        tid = self.add()
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        with self.f() as f:
            f.ask(tid, [{"q": "Name?", "options": ["hello", "hi"], "recommended": 0}])
        self.assertEqual(self.state(tid), ("drafting", "awaiting"))
        self.assertEqual(self.tick()["jobs"], [])
        self.edit_note(tid, lambda t: t.replace("- [ ] hello", "- [x] hello"))
        jobs = self.tick()["jobs"]
        self.assertEqual([(j["id"], j["job"]) for j in jobs], [(tid, "draft")])
        self.assertEqual(self.tick()["jobs"], [], "not dispatched twice")

    def test_large_ticket_is_split_and_closed(self):
        tid = self.add("Build a whole CMS")
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        with self.f() as f:
            with self.assertRaises(WonkaError):
                f.drafted(tid, "l")
            f.add("Pages", title="Pages", repo=str(self.repo), parent=tid)
            f.add("Users", title="Users", repo=str(self.repo), parent=tid)
            f.drafted(tid, "l")
        self.assertEqual(self.state(tid), ("wontdo", "done"))
        toobig = note.get_section(self.note(tid)[1], "toobig")
        self.assertEqual(toobig, "- [[WONKA-00002]] Pages\n- [[WONKA-00003]] Users")

    def test_three_bad_review_rounds_kill_the_ticket(self):
        tid = self.add()
        self.to_ready(tid)
        for n in range(3):
            self.bake(tid, f"f{n}.sh")
            self.review(tid, architect={"verdict": "bad nut", "issues": ["still wrong"]})
        self.assertEqual(self.state(tid), ("wontdo", "done"))
        self.assertIn("3 review rounds", self.note(tid)[1])

    def test_review_questions_can_close_or_redraft(self):
        a, b = self.add(), self.add("Other")
        for tid, suggest in ((a, "close"), (b, "draft")):
            self.to_ready(tid)
            self.bake(tid)
            self.review(tid, architect={"verdict": "bad nut", "suggest": suggest, "why": "because"})
        self.assertEqual(self.state(a), ("reviewing", "awaiting"))
        self.edit_note(a, lambda t: t.replace("  - [ ] Yes ⭐", "  - [x] Yes ⭐"))
        self.edit_note(b, lambda t: t.replace("  - [ ] Yes ⭐", "  - [x] Yes ⭐"))
        self.tick()
        self.assertEqual(self.state(a), ("wontdo", "done"))
        self.assertEqual(self.state(b), ("drafting", "progress"), "back to drafting and re-claimed")

    def test_wip_limits(self):
        ids = [self.add(f"t{n}") for n in range(3)]
        self.tick()
        with self.f() as f:
            for tid in ids:
                f.triage(tid, "xs")
        self.tick()
        drafting = [t for t in ids if self.state(t)[0] == "drafting"]
        self.assertEqual(len(drafting), 1)

    def test_retry_puts_a_stuck_job_back(self):
        tid = self.add()
        self.to_ready(tid)
        self.tick()
        self.assertEqual(self.state(tid), ("baking", "progress"))
        later = factory.now() + dt.timedelta(hours=3)
        with mock.patch.object(factory, "now", lambda: later):
            self.assertEqual(self.tick()["stale"], [tid])
        with self.f() as f:
            f.retry(tid)
        self.assertEqual([j["job"] for j in self.tick()["jobs"]], ["bake"])


if __name__ == "__main__":
    unittest.main()
