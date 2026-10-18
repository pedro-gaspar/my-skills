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

FAILING_GH = """#!/bin/sh
echo "$@" >> "$WONKA_FAKE_PR_STATE.calls"
echo "gh: HTTP 401: Bad credentials" >&2
exit 1
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

    def set_gh(self, script):
        (self.tmp / "gh").write_text(script)

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

    def test_repeated_followup_across_rounds_is_not_duplicated(self):
        tid = self.add()
        self.to_ready(tid, reviewers=("architect", "security"))
        children = lambda: self.f_ticket(tid)["children"]

        self.bake(tid)
        bye = {"title": "Add a bye script", "idea": "bye.sh"}
        self.review(tid, architect={"verdict": "bad nut", "issues": ["x"], "followups": [bye]},
                    security={"verdict": "bad nut", "issues": ["y"], "followups": [dict(bye)]})
        self.assertEqual(self.state(tid), ("baking", "pending"))
        self.assertEqual(len(children()), 1, "same follow-up from two squirrels in one round is filed once")

        self.bake(tid, "fix.sh")
        self.review(tid, architect={"verdict": "bad nut", "issues": ["x"],
                                    "followups": [{"title": "  add A BYE script ", "idea": "bye.sh again"},
                                                  {"title": "Add a ciao script", "idea": "ciao.sh"}]},
                    security={"verdict": "bad nut", "issues": ["y"],
                              "followups": [{"idea": "Add a bye script\nno title, idea's first line matches"}]})
        kids = children()
        self.assertEqual(len(kids), 2)
        self.assertEqual([self.note(k)[0]["title"] for k in kids], ["Add a bye script", "Add a ciao script"])
        skipped = [h["why"] for h in self.f_ticket(tid)["history"] if "skipped" in h.get("why", "")]
        self.assertEqual(len(skipped), 3, "one skip in round 1 (same round), two in round 2")
        self.assertIn("Follow-up already on the board, skipped: add A BYE script", skipped)

    def f_ticket(self, tid):
        with self.f() as f:
            return f.ticket(tid)

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

    def test_analyst_questions_keep_the_triage_answers(self):
        tid = self.add()
        self.tick()
        with self.f() as f:
            f.triage(tid, "m", questions=[{"q": "Bash or Python?", "options": ["Bash", "Python"], "recommended": 0}])
        self.edit_note(tid, lambda t: t.replace("- [ ] Approve", "- [x] Approve").replace("- [ ] Bash", "- [x] Bash"))
        self.tick()
        self.assertEqual(self.state(tid), ("drafting", "progress"))
        with self.f() as f:
            f.ask(tid, [{"q": "Name?", "options": ["hello", "hi"], "recommended": 0}])
        body = self.note(tid)[1]
        answers = note.get_section(body, "answers")
        self.assertIn("### Round 1", answers)
        self.assertIn("- [x] Bash", answers)
        open_round = note.get_section(body, "questions")
        self.assertIn("Name?", open_round)
        self.assertNotIn("Bash or Python?", open_round)
        self.assertEqual(self.state(tid), ("drafting", "awaiting"))
        self.assertEqual(self.tick()["jobs"], [], "an archived answered round does not resume the analyst")
        self.edit_note(tid, lambda t: t.replace("- [ ] hello", "- [x] hello"))
        jobs = self.tick()["jobs"]
        self.assertEqual([(j["id"], j["job"]) for j in jobs], [(tid, "draft")])
        self.assertEqual(self.state(tid), ("drafting", "progress"))
        with self.f() as f:
            f.ask(tid, [{"q": "Where?", "options": ["bin", "scripts"], "recommended": 0}])
        answers = note.get_section(self.note(tid)[1], "answers")
        self.assertLess(answers.index("### Round 1"), answers.index("### Round 2"))
        self.assertGreater(answers.index("- [x] hello"), answers.index("### Round 2"))
        self.assertIn("Where?", note.get_section(self.note(tid)[1], "questions"))

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

    def test_fix_round_needs_a_new_commit_before_baked(self):
        tid = self.add()
        self.to_ready(tid)
        self.bake(tid)
        self.review(tid, architect={"verdict": "bad nut", "issues": ["fix it"]})
        self.assertEqual(self.state(tid), ("baking", "pending"))
        job = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(job["job"], "bake")
        with self.f() as f:
            with self.assertRaisesRegex(WonkaError, "no new commits"):
                f.baked(tid)
        self.assertEqual(self.state(tid), ("baking", "progress"))
        wt = Path(job["worktree"])
        (wt / "fix.sh").write_text("echo fixed\n")
        sh("git", "add", ".", cwd=wt)
        sh("git", "commit", "-qm", "fix", cwd=wt)
        with self.f() as f:
            f.baked(tid)
        self.assertEqual(self.state(tid), ("reviewing", "pending"))

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
        with self.f() as f:
            self.assertNotIn("reviewed_head", f.ticket(b), "redrafting clears the reviewed head")

    def test_redraft_keeps_earlier_review_rounds(self):
        tid = self.add()
        self.to_ready(tid)
        with self.f() as f:
            plan = f.plan_path(tid)
        self.assertEqual(plan.read_text(), "# plan\n", "a plan without reviews is written as given")
        self.bake(tid)
        self.review(tid, architect={"verdict": "bad nut", "issues": ["no shebang"], "suggest": "draft",
                                    "why": "wrong approach"})
        self.edit_note(tid, lambda t: t.replace("  - [ ] Yes ⭐", "  - [x] Yes ⭐"))
        self.tick()
        self.assertEqual(self.state(tid), ("drafting", "progress"))

        with self.f() as f:
            f.write_plan(tid, "# plan v2\n")
        text = plan.read_text()
        self.assertTrue(text.startswith("# plan v2\n"))
        self.assertIn("## 🐿️ Earlier review round 1", text)
        self.assertIn("no shebang", text)
        self.assertNotRegex(text, r"(?m)^## 🐿️ Review round")

        with self.f() as f:
            f.write_plan(tid, "# plan v3\n")
        text = plan.read_text()
        self.assertEqual(text.count("Earlier review round 1"), 1, "rewriting keeps the log once")
        self.assertNotIn("plan v2", text)

        with self.f() as f:
            f.write_plan(tid, "# plan v4\n\n## 🐿️ Review round 1\n### squirrel-architect: bad nut\n- no shebang\n")
        text = plan.read_text()
        self.assertEqual(text.count("no shebang"), 1, "pasted rounds are dropped")
        self.assertNotRegex(text, r"(?m)^## 🐿️ Review round")

        with self.f() as f:
            f.drafted(tid, "s", ["architect"])
        self.bake(tid, "fix.sh")
        self.review(tid, architect={"verdict": "bad nut", "issues": ["new issue"]})
        text = plan.read_text()
        earlier, current = text.find("## 🐿️ Earlier review round 1"), text.find("\n## 🐿️ Review round 1")
        self.assertTrue(0 < earlier < current, text)
        self.assertIn("new issue", text[current:])

    QUESTION = {"q": "Rename it?", "options": ["Keep hello", "Rename to hi"], "recommended": 0}

    def bad_rounds(self, tid, n=2):
        for i in range(n):
            self.bake(tid, f"f{i}.sh")
            self.review(tid, architect={"verdict": "bad nut", "issues": ["still wrong"]})
        self.assertEqual(self.state(tid), ("baking", "pending"))

    def test_clean_review_with_questions_ships_when_answers_match(self):
        tid = self.add()
        self.to_ready(tid)
        self.bake(tid)
        self.review(tid, architect={"verdict": "good nut", "questions": [self.QUESTION]})
        self.assertEqual(self.state(tid), ("reviewing", "awaiting"))
        self.edit_note(tid, lambda t: t.replace("  - [ ] Keep hello ⭐", "  - [x] Keep hello ⭐"))
        tick = self.tick()
        self.assertIn(f"{tid}: clean review answered, off to ship", tick["events"])
        self.assertNotIn("bake", [j["job"] for j in tick["jobs"] if j["id"] == tid])
        self.assertEqual(self.state(tid), ("shipped", "awaiting"), "reconciled, shipped and PR opened in one tick")
        with self.f() as f:
            self.assertNotIn("round_issues", f.ticket(tid))

    def test_clean_review_answer_off_recommendation_rebakes_without_kill(self):
        tid = self.add()
        self.to_ready(tid)
        self.bad_rounds(tid)
        self.bake(tid, "f2.sh")
        self.review(tid, architect={"verdict": "good nut", "questions": [self.QUESTION]})
        self.assertEqual(self.state(tid), ("reviewing", "awaiting"))
        self.edit_note(tid, lambda t: t.replace("  - [ ] Rename to hi", "  - [x] Rename to hi"))
        self.tick()
        self.assertEqual(self.state(tid), ("baking", "progress"), "back to baking (and re-claimed), not killed")
        with self.f() as f:
            self.assertNotIn("round_issues", f.ticket(tid))
            wt = Path(f.ticket(tid)["worktree"])
        (wt / "hi.sh").write_text("echo hi\n")
        sh("git", "add", ".", cwd=wt)
        sh("git", "commit", "-qm", "rename", cwd=wt)
        with self.f() as f:
            f.baked(tid)
        self.review(tid, architect={"verdict": "good nut"})
        self.assertEqual(self.state(tid), ("shipped", "pending"))

    def test_round_three_clean_review_with_questions_is_not_killed(self):
        tid = self.add()
        self.to_ready(tid)
        self.bad_rounds(tid)
        self.bake(tid, "f2.sh")
        self.review(tid, architect={"verdict": "good nut", "questions": [self.QUESTION]})
        self.edit_note(tid, lambda t: t.replace("  - [ ] Keep hello ⭐", "  - [x] Keep hello ⭐"))
        self.tick()
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))

    def test_review_with_issues_and_questions_still_rebakes(self):
        tid = self.add()
        self.to_ready(tid)
        self.bake(tid)
        self.review(tid, architect={"verdict": "bad nut", "issues": ["wrong"], "questions": [self.QUESTION]})
        self.edit_note(tid, lambda t: t.replace("  - [ ] Keep hello ⭐", "  - [x] Keep hello ⭐"))
        self.tick()
        self.assertEqual(self.state(tid), ("baking", "progress"), "back to baking and re-claimed")

    def test_round_three_review_with_issues_and_questions_is_killed(self):
        tid = self.add()
        self.to_ready(tid)
        self.bad_rounds(tid)
        self.bake(tid, "f2.sh")
        self.review(tid, architect={"verdict": "bad nut", "issues": ["wrong"], "questions": [self.QUESTION]})
        self.edit_note(tid, lambda t: t.replace("  - [ ] Keep hello ⭐", "  - [x] Keep hello ⭐"))
        self.tick()
        self.assertEqual(self.state(tid), ("wontdo", "done"))

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

    def finished(self, tid, agent):
        with self.f() as f:
            return f.finished(tid, agent)

    def ticket(self, tid):
        with self.f() as f:
            return dict(f.ticket(tid))

    def test_finished_is_a_noop_when_the_agent_did_its_job(self):
        tid = self.add()
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        msg = self.finished(tid, "wonka:oompa-loompa-triage")
        self.assertIn("moved on", msg)
        self.assertTrue(msg.startswith(f"{tid}: "))
        self.assertEqual(self.state(tid), ("inbox", "done"))
        self.assertNotIn("retries", self.ticket(tid))
        self.assertEqual([j["job"] for j in self.tick()["jobs"]], ["draft"])
        with self.f() as f:
            f.write_plan(tid, "# plan\n")
            f.set_section(tid, "spec", "Add hello.sh")
            f.drafted(tid, "s", ["architect"])
        self.assertIn("moved on", self.finished(tid, "wonka:oompa-loompa-analyst"))
        self.assertEqual(self.state(tid), ("ready", "pending"))
        self.bake(tid)
        self.assertIn("moved on", self.finished(tid, "oompa-loompa-builder"))
        self.assertEqual(self.state(tid), ("reviewing", "pending"))
        self.assertNotIn("retries", self.ticket(tid))

    def test_finished_retries_an_agent_that_left_no_result(self):
        tid = self.add()
        self.tick()
        msg = self.finished(tid, "wonka:oompa-loompa-triage")
        self.assertEqual(msg, f"{tid}: wonka:oompa-loompa-triage left no result, back in the queue")
        self.assertEqual(self.state(tid), ("inbox", "pending"))
        t = self.ticket(tid)
        self.assertEqual(t["retries"], 1)
        self.assertNotIn("job_started_at", t)
        self.assertIn("moved on", self.finished(tid, "wonka:oompa-loompa-triage"), "no double retry")
        self.assertEqual(self.ticket(tid)["retries"], 1)
        self.assertEqual([j["job"] for j in self.tick()["jobs"]], ["triage"])
        with self.f() as f:
            f.triage(tid, "s")
        self.assertEqual([j["job"] for j in self.tick()["jobs"]], ["draft"])
        self.assertEqual(self.state(tid), ("drafting", "progress"))
        self.assertIn("left no result", self.finished(tid, "wonka:oompa-loompa-analyst"))
        self.assertEqual(self.state(tid), ("drafting", "pending"))
        self.assertEqual(self.ticket(tid)["retries"], 1, "a job that moved on resets the count")
        self.assertEqual([j["job"] for j in self.tick()["jobs"]], ["draft"])

    def test_parallel_squirrels_do_not_retry_each_other(self):
        tid = self.add()
        self.to_ready(tid, reviewers=("architect", "backend"))
        self.bake(tid)
        job = self.review(tid, architect={"verdict": "good nut"})
        msg = self.finished(tid, "wonka:squirrel-architect")
        self.assertEqual(msg, f"{tid}: squirrel-architect reported, waiting for backend")
        self.assertEqual(self.state(tid), ("reviewing", "progress"))
        self.assertEqual(self.ticket(tid)["review_round"], job["review_round"])
        with self.f() as f:
            f.report(tid, "backend", {"verdict": "good nut"})
        self.assertEqual(self.state(tid), ("shipped", "pending"))
        self.assertNotIn("finished", self.ticket(tid))
        self.assertIn("moved on", self.finished(tid, "wonka:squirrel-backend"))
        self.assertEqual(self.state(tid), ("shipped", "pending"))
        self.assertNotIn("retries", self.ticket(tid))

    def test_a_silent_squirrel_is_retried_once_the_others_finish(self):
        tid = self.add()
        self.to_ready(tid, reviewers=("architect", "backend"))
        self.bake(tid)
        job = self.review(tid)
        msg = self.finished(tid, "wonka:squirrel-architect")
        self.assertEqual(msg, f"{tid}: squirrel-architect left no report, retrying once backend finish")
        self.assertEqual(self.state(tid), ("reviewing", "progress"))
        with self.f() as f:
            f.report(tid, "backend", {"verdict": "good nut"})
        msg = self.finished(tid, "wonka:squirrel-backend")
        self.assertEqual(msg, f"{tid}: no report from architect, back in the queue")
        self.assertEqual(self.state(tid), ("reviewing", "pending"))
        t = self.ticket(tid)
        self.assertEqual(t["review_round"], job["review_round"] - 1)
        self.assertNotIn("reports", t)
        self.assertNotIn("finished", t)
        again = [j for j in self.tick()["jobs"] if j["id"] == tid][0]
        self.assertEqual(again["review_round"], job["review_round"])
        self.assertEqual(again["agents"], ["wonka:squirrel-architect", "wonka:squirrel-backend"])
        self.assertEqual(self.ticket(tid)["finished"], [])

    def test_finished_rejects_unknown_agents(self):
        tid = self.add()
        self.tick()
        with self.assertRaisesRegex(WonkaError, "unknown agent"):
            self.finished(tid, "wonka:oompa-loompa-chef")
        with self.assertRaisesRegex(WonkaError, "unknown squirrel"):
            self.finished(tid, "wonka:squirrel-ux")
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        with self.f() as f:
            f.write_plan(tid, "# plan\n")
            f.set_section(tid, "spec", "Add hello.sh")
            f.drafted(tid, "s", ["architect"])
        self.bake(tid)
        self.review(tid)
        with self.assertRaisesRegex(WonkaError, "not reviewing this ticket"):
            self.finished(tid, "wonka:squirrel-security")

    def test_finished_leaves_a_stuck_ticket_alone(self):
        tid = self.add()
        self.tick()
        for _ in range(3):
            with self.f() as f:
                f.retry(tid)
        before = self.ticket(tid)
        self.assertEqual(self.finished(tid, "wonka:oompa-loompa-triage"),
                         f"{tid}: stuck, waiting for a human to tick Retry")
        self.assertEqual(self.ticket(tid)["retries"], before["retries"])
        self.assertEqual(self.state(tid), ("inbox", "pending"))

    def test_failed_pr_is_retried_each_tick_then_stuck_until_a_human_ticks_retry(self):
        tid = self.add()
        self.to_ready(tid)
        self.bake(tid)
        self.review(tid, architect={"verdict": "good nut"})
        self.assertEqual(self.state(tid), ("shipped", "pending"))

        self.set_gh(FAILING_GH)
        tick = self.tick()
        self.assertEqual(self.state(tid), ("shipped", "pending"))
        self.assertTrue(any(e.startswith(f"{tid}: PR failed") for e in tick["events"]), tick["events"])
        self.assertEqual(tick["stale"], [])
        with self.f() as f:
            self.assertEqual(f.ticket(tid)["retries"], 1)
            self.assertNotIn("job_started_at", f.ticket(tid))
        body = self.note(tid)[1]
        self.assertIn("Retry 1/3", body)
        self.assertIn("Bad credentials", body)

        self.tick()
        events = self.tick()["events"]
        self.assertTrue(any("stuck after 3 tries" in e for e in events), events)
        with self.f() as f:
            self.assertTrue(f.ticket(tid).get("stuck"))
        stuck = note.get_section(self.note(tid)[1], "stuck")
        self.assertTrue(stuck.startswith("- [ ] Retry"), stuck)
        self.assertIn("Bad credentials", stuck)
        self.assertEqual(len(stuck.splitlines()), 1, "the Retry checkbox stays on one line")

        calls = Path(f"{self.pr_state}.calls")
        n = len(calls.read_text().splitlines())
        self.tick()
        self.assertEqual(len(calls.read_text().splitlines()), n, "a stuck ticket is not shipped")
        self.assertEqual(self.state(tid), ("shipped", "pending"))

        self.set_gh(FAKE_GH)
        self.edit_note(tid, lambda t: t.replace("- [ ] Retry", "- [x] Retry"))
        tick = self.tick()
        self.assertIn(f"{tid}: unstuck by a human", tick["events"])
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))
        self.assertIn("pull/7", self.note(tid)[1])

    def test_missing_pr_repo_is_retried_too(self):
        tid = self.add()
        with self.f() as f:
            f.ticket(tid)["pr_repo"] = None
        self.to_ready(tid)
        self.bake(tid)
        self.review(tid, architect={"verdict": "good nut"})
        self.assertEqual(self.state(tid), ("shipped", "pending"))
        events = self.tick()["events"]
        self.assertTrue(any(e.startswith(f"{tid}: PR failed: no pr_repo") for e in events), events)
        self.assertEqual(self.state(tid), ("shipped", "pending"))
        with self.f() as f:
            self.assertEqual(f.ticket(tid)["retries"], 1)

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

    def test_add_with_unknown_parent_leaves_nothing_behind(self):
        with self.assertRaises(WonkaError):
            self.add(parent="WONKA-09999")
        folder = store.tickets_dir(self.cfg)
        self.assertEqual(list(folder.glob("*.md")) if folder.exists() else [], [])
        tid = self.add()
        self.assertEqual(tid, "WONKA-00001")
        self.assertFalse([e for e in self.tick()["events"] if "not a Wonka ticket" in e])

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
