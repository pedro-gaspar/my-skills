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
  "pr create") printf '%s\\n' "$@" > "$WONKA_FAKE_PR_STATE.args"; echo "https://github.com/acme/app/pull/7" ;;
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
            f.write_plan(tid, "# plan\n")
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
        self.assertEqual(self.tick()["jobs"][0]["agents"], ["wonka:oompa-loompa-triage"])
        self.to_ready(tid, reviewers=("architect", "security"))
        self.assertIn("[[WONKA-00001-plan]]", note.get_section(self.note(tid)[1], "spec"))

        wt = self.bake(tid)
        job = self.review(tid, architect={"verdict": "good nut"},
                          security={"verdict": "bad nut", "issues": ["hello.sh:1: no shebang → add one"],
                                    "followups": [{"title": "Add a bye script", "idea": "bye.sh"}]})
        self.assertEqual(job["agents"], ["wonka:squirrel-architect", "wonka:squirrel-security"])
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
        meta, body = self.note(tid)
        self.assertEqual((meta["status"], meta["phase"]), ("🛗-Shipped", "🌟-Done"))
        self.assertIn("> [!success] Shipped 🎉\n> Merged: https://github.com/acme/app/pull/7.", body)
        self.assertEqual(body.count("# 🎫 "), 1, "the title heading is replaced, not stacked")
        self.assertIn("plan [[WONKA-00001-plan]] · [PR](https://github.com/acme/app/pull/7)", body)
        self.assertEqual(body.count("> [!"), 2, "one banner, one log, nothing piled up")

        later = factory.now() + dt.timedelta(days=8)
        with mock.patch.object(factory, "now", lambda: later):
            self.assertIn(f"{tid}: archived", self.tick()["events"])
        archive = Path(self.cfg["vault"]) / "wonka/archive"
        self.assertTrue((archive / f"{tid}.md").exists())
        self.assertTrue((archive / f"{tid}-plan.md").exists(), "plans are archived with their tickets")
        self.assertFalse((Path(self.cfg["vault"]) / "wonka/plans" / f"{tid}-plan.md").exists())

    def test_pr_description_carries_the_wonka_and_jira_ids(self):
        tid = self.add(jira="CALC-42")
        self.to_ready(tid)
        self.bake(tid)
        self.review(tid, architect={"verdict": "good nut"})
        self.tick()
        args = Path(f"{self.pr_state}.args").read_text()
        self.assertIn(f"🎫 **Golden ticket:** {tid}  \n🎟️ **Jira:** CALC-42\n\nAdd hello.sh", args)
        self.assertIn(f"{tid}: Add a hello script", args)

    def test_pr_description_without_jira_has_only_the_wonka_id(self):
        tid = self.add()
        self.to_ready(tid)
        self.bake(tid)
        self.review(tid, architect={"verdict": "good nut"})
        self.tick()
        args = Path(f"{self.pr_state}.args").read_text()
        self.assertIn(f"🎫 **Golden ticket:** {tid}\n\nAdd hello.sh", args)
        self.assertNotIn("Jira", args)

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
        body = self.note(tid)[1]
        self.assertTrue(body.startswith("# 🎫 Add a hello script\n\n> [!question] 🙋 Your turn: approve it\n> The triage Oompa-Loompa sized it **m**"))
        self.assertIn("· triage · ✉️-Inbox 🕰️-Pending → ✉️-Inbox 🙋-Awaiting-Human", body)
        self.assertGreater(body.index("## 🎩 Factory log"), body.index("## ❓ Questions"), "log stays last")
        self.edit_note(tid, lambda t: t.replace("- [ ] Approve", "- [x] Approve"))
        self.tick()
        self.assertEqual(self.state(tid), ("inbox", "awaiting"), "a question is still open")
        self.edit_note(tid, lambda t: t.replace("- [ ] Bash", "- [x] Bash"))
        self.tick()
        self.assertEqual(self.state(tid), ("drafting", "progress"), "approved, promoted and claimed in one tick")

    def test_drafting_questions_resume_the_analyst(self):
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

    def test_spark_and_airflow_squirrels_review_data_tickets(self):
        tid = self.add()
        self.to_ready(tid, reviewers=("spark", "airflow"))
        self.bake(tid)
        job = self.review(tid, spark={"verdict": "good nut"}, airflow={"verdict": "good nut"})
        self.assertEqual(job["agents"], ["wonka:squirrel-spark", "wonka:squirrel-airflow"])
        self.assertEqual(self.state(tid), ("shipped", "pending"))

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

    def test_three_failed_jobs_get_stuck_until_a_human_ticks_retry(self):
        tid = self.add()
        self.tick()
        for _ in range(3):
            with self.f() as f:
                msg = f.retry(tid)
        self.assertIn("stuck", msg)
        self.assertEqual(self.tick()["jobs"], [], "stuck tickets get no jobs")
        self.edit_note(tid, lambda t: t.replace("- [ ] Retry", "- [x] Retry"))
        tick = self.tick()
        self.assertIn(f"{tid}: unstuck by a human", tick["events"])
        self.assertEqual([j["id"] for j in tick["jobs"]], [tid])
        self.assertIsNone(note.get_section(self.note(tid)[1], "stuck"))

    def test_deleted_and_foreign_notes(self):
        tid = self.add()
        with self.f() as f:
            path = f.path(tid)
        path.unlink()
        (path.parent / "my scribble.md").write_text("hi\n")
        events = self.tick()["events"]
        self.assertIn(f"{tid}: note was deleted, ticket dropped", events)
        self.assertIn("my scribble.md: not a Wonka ticket, use /wonka add instead", events)
        self.assertEqual(self.state(tid), ("wontdo", "done"))

    def test_deleted_note_with_dirty_worktree_does_not_wedge_the_tick(self):
        tid = self.add()
        self.to_ready(tid)
        job = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(job["job"], "bake")
        wt = Path(job["worktree"])
        (wt / "scratch.txt").write_text("wip\n")
        with self.f() as f:
            path = f.path(tid)
        path.unlink()

        events = self.tick()["events"]
        self.assertIn(f"{tid}: note was deleted, ticket dropped", events)
        self.assertEqual(self.state(tid), ("wontdo", "done"))
        self.assertTrue(wt.exists())
        with self.f() as f:
            history = f.ticket(tid)["history"]
        self.assertTrue(any(h.get("why", "").startswith("Worktree kept") for h in history))
        self.tick()

    def test_one_unreadable_note_does_not_block_other_tickets(self):
        a = self.add("Add a hello script")
        b = self.add("Add a bye script")
        with self.f() as f:
            path = f.path(a)
        path.unlink()
        path.mkdir()
        tick = self.tick()
        self.assertTrue(any(e.startswith(f"{a}: ⚠️") for e in tick["events"]), tick["events"])
        self.assertIn(b, [j["id"] for j in tick["jobs"]])
        self.assertFalse(any("not a Wonka ticket" in e for e in tick["events"]))

    def test_baking_starts_from_the_latest_origin_base(self):
        tid = self.add()
        other = self.tmp / "other"
        sh("git", "clone", "-q", "-b", "main", str(self.tmp / "origin.git"), str(other), cwd=self.tmp)
        (other / "new.txt").write_text("upstream\n")
        sh("git", "add", ".", cwd=other)
        sh("git", "commit", "-qm", "upstream", cwd=other)
        sh("git", "push", "-q", "origin", "main", cwd=other)
        self.to_ready(tid)
        job = self.tick()["jobs"][0]
        self.assertEqual(job["base_ref"], "origin/main")
        self.assertTrue((Path(job["worktree"]) / "new.txt").exists())


if __name__ == "__main__":
    unittest.main()
