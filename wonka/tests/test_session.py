"""wonka session: a ticket taken into a Claude session (🕹️ manual), stepped through its stages with the note's
🕹️ Manual question, and handed back to the factory."""

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from wonka import cli, factory, note
from wonka.factory import BACK, FACTORY, NEXT
from wonka.store import WonkaError

from . import test_factory as tf  # the module, not the class: its tests must not run twice
from .fixtures import RepoFixture

sh = tf.sh


class SessionTest(unittest.TestCase):
    setUp = tf.FactoryTest.setUp
    f = tf.FactoryTest.f
    add = tf.FactoryTest.add
    tick = tf.FactoryTest.tick
    state = tf.FactoryTest.state
    note = tf.FactoryTest.note
    f_ticket = tf.FactoryTest.f_ticket
    edit_note = tf.FactoryTest.edit_note
    to_planned = tf.FactoryTest.to_planned

    def session(self, tid, **kw):
        with self.f() as f:
            return f.session(tid, **kw)

    def pick(self, tid, option):
        """Tick an option of the 🕹️ Manual question, as the human does in Obsidian, and run a tick."""
        self.edit_note(tid, lambda text: text.replace(f"- [ ] {option}", f"- [x] {option}", 1))
        return self.tick()

    def jobs_for(self, out, tid):
        return [j for j in out["jobs"] if j["id"] == tid]

    def manual_section(self, tid):
        return note.get_section(self.note(tid)[1], "manual")

    def commit(self, tid, name="hello.sh"):
        wt = Path(self.f_ticket(tid)["worktree"])
        (wt / name).write_text("echo hi\n")
        sh("git", "add", ".", cwd=wt)
        sh("git", "commit", "-qm", "by hand", cwd=wt)
        return wt

    def manual_baking(self):
        tid = self.add(start="idea")
        self.session(tid)
        for _ in range(3):  # idea → triage → drafting → baking
            self.pick(tid, NEXT)
        self.assertEqual(self.state(tid), ("baking", "awaiting"))
        return tid

    def manual_reviewed(self, **reports):
        tid = self.manual_baking()
        self.commit(tid)
        job = self.jobs_for(self.pick(tid, NEXT), tid)[0]  # the tick that moves it hands out the review
        self.assertEqual(self.state(tid), ("reviewing", "progress"))
        self.assertEqual(job["agents"], ["wonka:squirrel-architect"])
        with self.f() as f:
            f.report(tid, "architect", reports.get("architect", {"verdict": "good nut"}), job["dispatch"])
        self.assertEqual(self.state(tid), ("reviewing", "awaiting"))
        return tid

    def test_an_idea_stays_an_idea_with_the_manual_question(self):
        tid = self.add(start="idea")
        brief = self.session(tid)
        t = self.f_ticket(tid)
        self.assertEqual(self.state(tid), ("idea", "awaiting"))
        self.assertTrue(t["manual"])
        self.assertTrue(Path(t["worktree"]).is_dir(), "the shell opens in the ticket's worktree, even for an idea")
        body = self.note(tid)[1]
        self.assertNotIn("## 💡 Start", body)
        self.assertIn(f"- [ ] {NEXT}: 📏 Triage", self.manual_section(tid))
        self.assertIn(f"- [ ] {FACTORY}", self.manual_section(tid))
        self.assertNotIn(BACK, self.manual_section(tid))
        self.assertIn("🕹️ Manual: you are shaping the idea", body)
        self.assertIn(t["worktree"], brief)
        self.assertEqual(self.jobs_for(self.tick(), tid), [])

    def test_a_board_drag_to_triage_is_put_back_while_manual(self):
        tid = self.add(start="idea")
        self.session(tid)
        self.edit_note(tid, lambda text: text.replace("status: 💡 Idea", "status: 📏 Triage"))
        self.tick()
        self.assertEqual(self.state(tid), ("idea", "awaiting"))
        self.assertTrue(self.f_ticket(tid)["manual"])

    def test_next_steps_through_the_stages_with_no_agent(self):
        tid = self.add(start="idea")
        self.session(tid)
        out = self.pick(tid, NEXT)
        self.assertEqual(self.state(tid), ("triage", "awaiting"))
        self.assertIn(f"{tid}: 🕹️ next, now 📏 Triage · 🙋 Awaiting Human", out["events"])
        self.assertEqual(self.jobs_for(out, tid), [])
        self.assertIn(f"- [ ] {BACK}: 💡 Idea", self.manual_section(tid))
        self.assertEqual(self.jobs_for(self.pick(tid, NEXT), tid), [])
        self.assertEqual(self.state(tid), ("drafting", "awaiting"))
        self.pick(tid, BACK)
        self.assertEqual(self.state(tid), ("triage", "awaiting"))

    def test_back_to_the_factory_from_triage_follows_the_size(self):
        for size, dst in (("", ("triage", "pending")), ("s", ("triage", "ready"))):
            with self.subTest(size=size):
                tid = self.add(start="idea")
                self.session(tid)
                self.pick(tid, NEXT)
                if size:
                    with self.f() as f:
                        f.set_field(tid, "size", size)
                self.pick(tid, FACTORY)
                self.assertNotEqual(self.state(tid), ("triage", "awaiting"))
                self.assertFalse(self.f_ticket(tid).get("manual"))
                self.assertIsNone(self.manual_section(tid))
                history = [h.get("to") for h in self.f_ticket(tid)["history"]]
                self.assertIn("/".join(dst), history)

    def test_a_spec_waiting_for_approval_keeps_its_spec_and_plan_and_drops_the_approval(self):
        tid = self.add(spec_review="human")
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        with self.f() as f:
            f.write_plan(tid, "# plan\nAdd hello.sh\n")
            f.set_section(tid, "spec", "Add hello.sh")
            f.drafted(tid, "s", ["architect"])
        self.assertEqual(self.state(tid), ("drafting", "awaiting"))
        self.assertIn(note.SPEC_Q, self.note(tid)[1])
        brief = self.session(tid)
        body = self.note(tid)[1]
        self.assertEqual(self.state(tid), ("drafting", "awaiting"))
        self.assertNotIn(note.SPEC_Q, body)
        self.assertIn("Add hello.sh", note.get_section(body, "spec"))
        self.assertFalse(self.f_ticket(tid).get("spec_review_pending"))
        with self.f() as f:
            self.assertIn(f"- Plan: {f.plan_path(tid)}\n", brief, "the plan exists: no 'not written yet'")
        self.assertIn("spec review question was dropped", body)
        self.pick(tid, FACTORY)
        self.assertNotEqual(self.state(tid), ("drafting", "awaiting"), "nothing left to approve")

    def test_a_busy_agent_is_refused_unless_forced(self):
        tid = self.add()
        self.to_planned(tid)
        job = self.jobs_for(self.tick(), tid)[0]
        self.assertEqual(self.state(tid), ("baking", "progress"))
        with self.assertRaisesRegex(WonkaError, "an Oompa-Loompa is at work on it"):
            self.session(tid)
        self.assertEqual(self.state(tid), ("baking", "progress"))
        self.session(tid, force=True)
        self.assertEqual(self.state(tid), ("baking", "awaiting"))
        self.assertEqual(self.f_ticket(tid)["dispatch"], job["dispatch"] + 1)
        with self.f() as f:
            self.assertIn("dispatch", f.baked(tid, job["dispatch"]) or "")
        self.assertEqual(self.state(tid), ("baking", "awaiting"))

    def test_a_triaging_ticket_taken_by_force_drops_its_scout(self):
        tid = self.add()
        self.assertEqual(self.jobs_for(self.tick(), tid)[0]["job"], "triage")
        scout = self.f_ticket(tid)["scout"]
        self.assertTrue(Path(scout).is_dir())
        self.session(tid, force=True)
        self.assertEqual(self.state(tid), ("triage", "awaiting"))
        self.assertFalse(Path(scout).exists())

    def test_find_by_wonka_id_or_jira_key(self):
        a = self.add(jira="DATA-1")
        b = self.add(jira="DATA-2")
        self.add(jira="DATA-2")
        with self.f() as f:
            self.assertEqual(f.find(a.lower()), a)
            self.assertEqual(f.find("data-1"), a)
            with self.assertRaisesRegex(WonkaError, f"Jira key of {b}, WONKA-00003"):
                f.find("DATA-2")
            with self.assertRaisesRegex(WonkaError, "no ticket DATA-9"):
                f.find("DATA-9")

    def test_reviewing_needs_commits_and_comes_back_to_the_human(self):
        tid = self.manual_baking()
        out = self.pick(tid, NEXT)
        self.assertEqual(self.state(tid), ("baking", "awaiting"))
        self.assertIn(f"{tid}: 🕹️ not moved, no commits on", " ".join(out["events"]))
        self.assertIn(f"- [ ] {NEXT}", self.manual_section(tid), "the refused option is unticked")
        self.commit(tid)
        job = self.jobs_for(self.pick(tid, NEXT), tid)[0]
        self.assertEqual(self.state(tid), ("reviewing", "progress"))
        self.assertIn("squirrels are reviewing", self.manual_section(tid))
        self.assertIn("🕹️ Manual: the squirrels are reviewing your bake", self.note(tid)[1])
        with self.f() as f:
            f.report(tid, "architect", {"verdict": "bad nut", "issues": ["hello.sh:1: no shebang → add one"],
                                        "questions": [{"q": "Keep bash?", "options": ["Yes", "No"]}]},
                     job["dispatch"])
        self.assertEqual(self.state(tid), ("reviewing", "awaiting"), "no rebake: the human decides")
        plan = Path(self.f_ticket(tid)["plan"]).read_text()
        self.assertIn("no shebang", plan)
        self.assertIn("❓ Keep bash? (Yes / No)", plan)
        self.assertIsNone(note.get_section(self.note(tid)[1], "review-questions"))
        self.assertIn(f"- [ ] {NEXT}: 🚢 Shipped", self.manual_section(tid))

    def test_next_from_reviewing_opens_the_pr_and_stays_manual(self):
        tid = self.manual_reviewed()
        self.pick(tid, NEXT)  # reconcile moves it to shipped/pending, the same tick ships it
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))
        t = self.f_ticket(tid)
        self.assertTrue(t["manual"])
        self.assertEqual(t["pr_url"], "https://github.com/acme/app/pull/7")
        body = self.note(tid)[1]
        self.assertIsNone(note.get_section(body, "changes"), "change requests are made in the session")
        self.assertIn(f"- [ ] {BACK}: 🍫 Baking", self.manual_section(tid))
        self.assertIn("🕹️ Manual: the PR is yours", body)

    def test_a_merged_pr_is_recorded_while_manual(self):
        tid = self.manual_reviewed()
        self.pick(tid, NEXT)
        self.pick(tid, BACK)
        self.assertEqual(self.state(tid), ("baking", "awaiting"))
        (self.tmp / "pr_state").write_text("MERGED")
        self.tick()
        self.assertEqual(self.state(tid), ("shipped", "ready"))
        self.assertFalse(self.f_ticket(tid).get("manual"))
        self.assertIsNone(self.manual_section(tid))

    def test_back_to_the_factory_from_shipped_watches_the_pr_again(self):
        tid = self.manual_reviewed()
        self.pick(tid, NEXT)
        self.pick(tid, FACTORY)
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))
        self.assertFalse(self.f_ticket(tid).get("manual"))
        body = self.note(tid)[1]
        self.assertEqual(note.get_section(body, "changes"), note.CHANGES_BOX)
        self.assertIsNone(note.get_section(body, "manual"))

    def test_back_to_the_factory_from_reviewing(self):
        clean = self.manual_reviewed()
        self.pick(clean, FACTORY)
        self.assertEqual(self.state(clean), ("shipped", "awaiting"), "a good nut ships")
        bad = self.manual_reviewed(architect={"verdict": "bad nut", "issues": ["x: y → z"]})
        out = self.pick(bad, FACTORY)
        self.assertEqual(self.state(bad), ("baking", "progress"), "a builder fixes the issues")
        self.assertEqual(self.jobs_for(out, bad)[0]["job"], "bake")

    def test_back_to_the_factory_from_baking_sends_it_to_the_squirrels(self):
        tid = self.manual_baking()
        self.commit(tid)
        out = self.pick(tid, FACTORY)
        self.assertEqual(self.jobs_for(out, tid)[0]["job"], "review")
        self.assertFalse(self.f_ticket(tid).get("manual"))

    def test_back_to_the_factory_from_drafting_needs_a_plan(self):
        tid = self.add(start="idea")
        self.session(tid)
        self.pick(tid, NEXT)
        self.pick(tid, NEXT)
        out = self.pick(tid, FACTORY)
        self.assertEqual(self.state(tid), ("drafting", "awaiting"))
        self.assertIn("no plan yet", " ".join(out["events"]))
        with self.f() as f:
            plan = f.plan_path(tid)
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("# plan\nAdd hello.sh\n")
        self.pick(tid, FACTORY)
        self.assertNotIn(self.state(tid), {("drafting", "awaiting")})
        self.assertEqual(self.f_ticket(tid)["plan"], str(plan))
        self.assertIn("drafting/awaiting", [h.get("from") for h in self.f_ticket(tid)["history"]])

    def test_two_ticked_options_are_refused(self):
        tid = self.add(start="idea")
        self.session(tid)
        self.edit_note(tid, lambda text: text.replace("- [ ] ", "- [x] "))
        out = self.tick()
        self.assertEqual(self.state(tid), ("idea", "awaiting"))
        self.assertIn("tick just one", " ".join(out["events"]))
        self.assertNotIn("- [x]", self.manual_section(tid))

    def test_a_deleted_manual_section_comes_back(self):
        tid = self.add(start="idea")
        self.session(tid)
        self.edit_note(tid, lambda text: note.drop_section(text, "manual"))
        self.tick()
        self.assertIn(NEXT, self.manual_section(tid))

    def test_the_brief_says_where_everything_is(self):
        tid = self.manual_reviewed()
        self.pick(tid, NEXT)
        brief = self.session(tid)  # again on a manual ticket: only the brief
        t = self.f_ticket(tid)
        with self.f() as f:
            note_path, plan = f.path(tid), f.plan_path(tid)
        for text in (tid, str(note_path), str(plan), t["worktree"], t["branch"], t["pr_url"], "🕹️ Where to next?",
                     f"wonka pr-comments {tid}"):
            self.assertIn(text, brief)
        self.assertEqual(self.state(tid), ("shipped", "awaiting"))

    def test_an_existing_worktree_is_reused_untouched(self):
        tid = self.manual_baking()
        wt = Path(self.f_ticket(tid)["worktree"])
        (wt / "wip.txt").write_text("not committed\n")
        self.session(tid)
        self.assertEqual((wt / "wip.txt").read_text(), "not committed\n")

    def test_a_branch_checked_out_elsewhere_is_refused(self):
        tid = self.add(start="idea")
        self.session(tid)
        t = self.f_ticket(tid)
        sh("git", "worktree", "remove", "--force", t["worktree"], cwd=self.repo)
        other = self.tmp / "orca-workspace"
        sh("git", "worktree", "add", "-q", str(other), t["branch"], cwd=self.repo)
        with self.assertRaisesRegex(WonkaError, "checked out in .*orca-workspace"):
            self.session(tid)

    def test_from_seeds_the_branch_only_while_it_has_no_commits(self):
        sh("git", "switch", "-qc", "mine", cwd=self.repo)
        (self.repo / "started.txt").write_text("by hand\n")
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "commit", "-qm", "started by hand", cwd=self.repo)
        tip = sh("git", "rev-parse", "HEAD", cwd=self.repo)
        sh("git", "switch", "-q", "main", cwd=self.repo)
        tid = self.add(start="idea")
        self.session(tid, from_branch="mine")
        wt = Path(self.f_ticket(tid)["worktree"])
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=wt), tip)
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo), "main", "never touched")
        with self.assertRaisesRegex(WonkaError, "already manual"):
            self.session(tid, from_branch="mine")
        with self.f() as f, self.assertRaisesRegex(WonkaError, "commit\\(s\\) of its own"):
            f._seed_branch(tid, "main")

    def test_worktreeinclude_files_are_copied(self):
        (self.repo / ".gitignore").write_text(".env\n")
        (self.repo / ".worktreeinclude").write_text(".env\nnotes.txt\n")
        sh("git", "add", ".", cwd=self.repo)
        sh("git", "commit", "-qm", "include", cwd=self.repo)
        sh("git", "push", "-q", "origin", "main", cwd=self.repo)
        (self.repo / ".env").write_text("SECRET=1\n")
        (self.repo / "notes.txt").write_text("not ignored\n")
        self.assertEqual(factory.worktree_includes(str(self.repo)), [".env", "notes.txt"])
        tid = self.add(start="idea")
        self.session(tid)
        wt = Path(self.f_ticket(tid)["worktree"])
        self.assertEqual((wt / ".env").read_text(), "SECRET=1\n")
        self.assertFalse((wt / "notes.txt").exists(), "a file git does not ignore is never copied")

    def test_doctor_reports_worktreeinclude(self):
        from wonka import doctor
        (self.repo / ".gitignore").write_text(".env\n")
        (self.repo / ".worktreeinclude").write_text(".env\nnotes.txt\nREADME.md\n")
        (self.repo / ".env").write_text("SECRET=1\n")
        (self.repo / "notes.txt").write_text("not ignored\n")
        tickets = {"WONKA-00001": {"repo": str(self.repo)}}
        found = list(doctor._worktree_include_checks(tickets))
        self.assertIn(("ok", f"{self.repo}/.worktreeinclude: 1 file(s) copied into worktrees and scouts"), found)
        warns = " ".join(m for level, m in found if level == "warn")
        self.assertIn("tracked files, which worktrees already have: README.md", warns)
        self.assertIn("not ignore, never copied (a builder could commit them): notes.txt", warns)

    def test_a_finished_ticket_is_refused(self):
        tid = self.add(start="idea")
        self.edit_note(tid, lambda text: text.replace("status: 💡 Idea", "status: 🗑️ Wont Do"))
        self.tick()
        with self.assertRaisesRegex(WonkaError, "finished"):
            self.session(tid)


class SessionCliTest(RepoFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(cli, "LINK", self.tmp / "bin" / "wonka")
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def idea(self, jira="DATA-3806"):
        with self.f() as f:
            return f.add("Add a hello script", repo=str(self.repo), pr_repo="acme/app", jira=jira, start="idea")

    def test_print_shows_the_brief_by_jira_key(self):
        tid = self.idea()
        code, out, err = self.cli("session", "data-3806", "--print")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Wonka golden ticket {tid}", out)
        self.assertIn("- Jira: DATA-3806", out)

    def test_the_terminal_launcher_execs_claude_in_the_worktree(self):
        tid = self.idea()
        with mock.patch.object(cli.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(cli.os, "chdir") as chdir, mock.patch.object(cli.os, "execvp") as execvp:
            code, _, err = self.cli("session", tid)
        self.assertEqual(code, 0, err)
        with self.f() as f:
            wt = f.ticket(tid)["worktree"]
        chdir.assert_called_once_with(wt)
        self.assertEqual(execvp.call_args[0][0], "claude")
        self.assertIn(wt, execvp.call_args[0][1][1])

    def test_the_orca_launcher_opens_a_tab_in_the_worktree(self):
        tid = self.idea()
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(cli.shutil, "which", return_value="/bin/orca"), \
                mock.patch.object(cli.subprocess, "run", return_value=done) as run:
            code, out, err = self.cli("session", tid, "--orca")
        self.assertEqual(code, 0, err)
        with self.f() as f:
            wt = f.ticket(tid)["worktree"]
        label, launch = (c[0][0] for c in run.call_args_list)
        self.assertEqual(label[1:5], ["worktree", "set", "--worktree", f"path:{wt}"])
        self.assertEqual(launch[1:7], ["agent", "launch", "--agent", "claude", "--worktree", f"path:{wt}"])
        self.assertIn(f"Wonka golden ticket {tid}", launch[launch.index("--prompt") + 1])
        self.assertIn("Claude is open in Orca", out)

    def test_worktree_prints_the_path_and_info(self):
        tid = self.idea()
        code, _, err = self.cli("worktree", tid)
        self.assertEqual(code, 1)
        self.assertIn("no worktree yet", err)
        self.cli("session", tid, "--print")
        with self.f() as f:
            wt = f.ticket(tid)["worktree"]
        self.assertEqual(self.cli("worktree", "DATA-3806")[1].strip(), wt)
        out = self.cli("worktree", tid, "--info")[1]
        for text in (f"path     {wt}", "commits  0 ahead of", "none, the tree is clean", "🕹️ manual", "PR       -"):
            self.assertIn(text, out)

    def test_add_manual_creates_a_ticket_already_in_a_session(self):
        code, out, err = self.cli("add", "--repo", str(self.repo), "--jira", "DATA-7", "--manual", "Add a hello script")
        self.assertEqual(code, 0, err)
        self.assertIn("🕹️ manual from the start", out)
        self.assertIn("wonka session WONKA-00001", out)
        with self.f() as f:
            t, s = f.ticket("WONKA-00001"), f.state("WONKA-00001")
            body = f.read("WONKA-00001")[1]
        self.assertEqual(s, ("idea", "awaiting"))
        self.assertTrue(t["manual"])
        self.assertTrue(Path(t["worktree"]).is_dir())
        self.assertIn("## 🕹️ Manual", body)
        self.assertNotIn("## 💡 Start", body)
        self.cli("add", "--repo", str(self.repo), "--manual", "--triage", "Another one")
        with self.f() as f:
            self.assertEqual(f.state("WONKA-00002"), ("triage", "awaiting"))
            self.assertEqual(f.tick()["jobs"], [], "no agent works a manual ticket")

    def test_status_marks_manual_tickets(self):
        tid = self.idea()
        self.cli("session", tid, "--print")
        self.assertIn("🕹️", self.cli("status")[1])

    def test_session_launcher_setting(self):
        self.assertEqual(self.cli("config", "session_launcher", "orca")[0], 0)
        code, _, err = self.cli("config", "session_launcher", "tmux")
        self.assertEqual(code, 1)
        self.assertIn("terminal or orca", err)


if __name__ == "__main__":
    unittest.main()
