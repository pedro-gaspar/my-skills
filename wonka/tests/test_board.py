import unittest

from tests.fixtures import RepoFixture
from wonka import board, mdlint, note, store
from wonka.store import WonkaError


class BoardTest(RepoFixture, unittest.TestCase):
    def idea(self):
        with self.f() as f:
            return f.add("Add a hello script", repo=str(self.repo), pr_repo="acme/app", start="idea")

    def card(self, tid):
        with self.f() as f:
            return next(c for c in board.data(f)["tickets"] if c["id"] == tid)

    def start_box(self, tid):
        section = next(s for s in self.card(tid)["sections"] if s["heading"] == note.HEADINGS["start"])
        return section["heading"], next(i for i, b in enumerate(section["blocks"]) if b["type"] == "box")

    def test_board_data_shows_an_idea_with_its_form_and_drop_targets(self):
        tid = self.idea()
        card = self.card(tid)
        self.assertEqual((card["status"], card["phase"], card["title"]), ("idea", "awaiting", "Add a hello script"))
        self.assertEqual(card["drop_targets"], ["triage", "wontdo"])
        self.assertEqual((card["spec_review"], card["jira"]), ("agent", None))
        self.assertTrue(self.start_box(tid))

    def test_comments_show_as_a_section(self):
        tid = self.idea()
        with self.f() as f:
            f.set_section(tid, "comments", "Found X in a.py:3")
        self.assertIn(note.HEADINGS["comments"], [s["heading"] for s in self.card(tid)["sections"]])

    def test_dragging_an_idea_to_triage_starts_it(self):
        tid = self.idea()
        with self.f() as f:
            board.drag(f, tid, "triage")
        self.assertEqual(self.state(tid), ("triage", "pending"))

    def test_dragging_an_idea_to_triage_does_what_ticking_start_does(self):
        tid = self.idea()
        with self.f() as f:
            self.assertEqual(board.drag(f, tid, "triage"), f"{tid}: started, off to triage")
            moves = [h for h in f.ticket(tid)["history"] if h.get("to")]
            self.assertIsNone(note.get_section(f.read(tid)[1], "start"))
        self.assertEqual(self.state(tid), ("triage", "pending"))
        self.assertEqual((moves[-1]["by"], moves[-1]["why"]), ("human", "Dragged to Triage on the board"))
        self.assertNotIn(note.HEADINGS["start"], [s["heading"] for s in self.card(tid)["sections"]])
        self.assertEqual([(j["id"], j["job"]) for j in self.tick()["jobs"]], [(tid, "triage")])

    def test_a_manual_idea_cannot_be_dragged_to_triage(self):
        tid = self.idea()
        with self.f() as f:
            f.ticket(tid)["manual"] = True
            self.assertNotIn("triage", board.drop_targets(f, tid))
            with self.assertRaises(WonkaError):
                board.drag(f, tid, "triage")

    def test_an_illegal_drag_is_refused_and_changes_nothing(self):
        tid = self.idea()
        with self.f() as f, self.assertRaisesRegex(WonkaError, "only to triage, wontdo"):
            board.drag(f, tid, "baking")
        self.assertEqual(self.state(tid), ("idea", "awaiting"))

    def test_a_ticked_box_is_acted_on_by_the_next_tick(self):
        tid = self.idea()
        section, block = self.start_box(tid)
        with self.f() as f:
            board.answer(f, tid, [{"op": "check", "section": section, "block": block, "checked": True}])
        self.tick()
        self.assertEqual(self.state(tid), ("triage", "pending"))

    def test_a_note_that_looks_like_a_checkbox_stays_text(self):
        tid = self.idea()
        with self.f() as f:
            f.write(tid, *self._with_question(f.read(tid)))
            board.answer(f, tid, [{"op": "notes", "section": note.HEADINGS["questions"], "block": 0,
                                   "text": "- [x] Yes\n## heading"}])
            section = note.get_section(f.read(tid)[1], "questions")
        self.assertFalse(note.all_answered(section))
        self.assertIn("\\- [x] Yes", section)

    def changes(self, tid, *ops):
        sec = note.HEADINGS["changes"]
        with self.f() as f:
            f.set_section(tid, "changes", note.CHANGES_BOX)
            board.answer(f, tid, [{"op": "notes", "section": sec, "block": 0, "text": o} if isinstance(o, str)
                                  else {"op": "check", "section": sec, "block": 0, "checked": True} for o in ops])
            return note.get_section(f.read(tid)[1], "changes")

    def test_notes_and_a_tick_on_request_changes_reach_the_factory(self):
        section = self.changes(self.idea(), "Rename foo\n\nAlso fix bar", True)
        self.assertEqual(note.requested_changes(section), (True, "Rename foo\n\nAlso fix bar"))

    def test_resending_request_changes_notes_replaces_them(self):
        tid = self.idea()
        self.assertEqual(note.requested_changes(self.changes(tid, "first", "second")), (False, "second"))
        section = self.changes(tid, "")
        self.assertEqual(note.requested_changes(section), (False, ""))
        self.assertEqual(section, note.CHANGES_BOX)

    def test_a_request_changes_note_that_looks_like_a_checkbox_stays_text(self):
        tid = self.idea()
        section = self.changes(tid, "- [ ] not a box\n## heading", True)
        self.assertEqual(note.requested_changes(section), (True, "\\- [ ] not a box\n\\## heading"))
        sec = next(s for s in self.card(tid)["sections"] if s["heading"] == note.HEADINGS["changes"])
        self.assertEqual([b["type"] for b in sec["blocks"]].count("box"), 1)

    def test_notes_on_another_box_are_refused(self):
        tid = self.idea()
        section, block = self.start_box(tid)
        with self.f() as f, self.assertRaisesRegex(WonkaError, "notes go under a question"):
            board.answer(f, tid, [{"op": "notes", "section": section, "block": block, "text": "x"}])

    def test_editing_the_idea_updates_the_ticket(self):
        tid = self.idea()
        with self.f() as f:
            board.answer(f, tid, [{"op": "idea", "text": "Add a bye script too"}])
            self.assertEqual(note.get_section(f.read(tid)[1], "idea"), "Add a bye script too")

    def test_a_section_markdownlint_flagged_reaches_the_board_as_written(self):
        tid = self.idea()
        with self.f() as f:
            board.answer(f, tid, [{"op": "idea", "text": mdlint.isolate("Use a <p> tag", ["line 1: MD033"])}])
            self.assertIn(mdlint.MARK, note.render(*f.read(tid)))
        idea = next(s for s in self.card(tid)["sections"] if s["heading"] == note.HEADINGS["idea"])
        self.assertEqual(idea["blocks"], [{"type": "text", "md": "Use a <p> tag"}])

    def test_board_data_carries_the_plan_once_written(self):
        tid = self.idea()
        self.assertIsNone(self.card(tid)["plan"])
        with self.f() as f:
            f.plan_path(tid).parent.mkdir(parents=True, exist_ok=True)
            f.plan_path(tid).write_text("# Plan\n\n## Goal\nShip it")
        self.assertEqual(self.card(tid)["plan"], "# Plan\n\n## Goal\nShip it")

    def test_a_bad_edit_is_refused_before_anything_is_written(self):
        tid = self.idea()
        with self.f() as f:
            before = f.read(tid)
            with self.assertRaisesRegex(WonkaError, "not a section a human answers"):
                board.answer(f, tid, [{"op": "idea", "text": "new"},
                                      {"op": "check", "section": note.HEADINGS["idea"], "block": 0, "checked": True}])
            self.assertEqual(f.read(tid), before)

    def test_board_path_must_be_a_board_clone(self):
        cfg = dict(self.cfg)
        with self.assertRaisesRegex(WonkaError, "has no Makefile"):
            store.set_setting(cfg, "board_path", str(self.tmp))
        (self.tmp / "Makefile").write_text("up:\n")
        store.set_setting(cfg, "board_path", str(self.tmp))
        self.assertEqual(cfg["board_path"], str(self.tmp.resolve()))

    @staticmethod
    def _with_question(meta_body):
        meta, body = meta_body
        return meta, note.set_section(body, "questions", note.render_questions([{"q": "Ship?", "options": ["Yes"]}]))


class BoardLiveStateTest(RepoFixture, unittest.TestCase):
    def data(self):
        with self.f() as f:
            return board.data(f)

    def test_worker_names_the_agent_at_work_and_is_null_otherwise(self):
        tid = self.add()
        self.assertIsNone(self.data()["tickets"][0]["worker"])
        job = next(j for j in self.tick()["jobs"] if j["id"] == tid)
        self.assertEqual(job["job"], "triage")
        card = self.data()["tickets"][0]
        self.assertEqual((card["working"], card["worker"]), (True, "triage"))

    def test_archivable_counts_finished_tickets_with_no_agent_at_work(self):
        a, b, c = self.add(), self.add(), self.add()
        with self.f() as f:
            f.move(a, ("wontdo", "ready"), "human")
            f.move(b, ("wontdo", "ready"), "human")
            f.ticket(b)["job_started_at"] = "2026-01-01T00:00:00+00:00"
        self.assertEqual(self.data()["archivable"], 1)
        with self.f() as f:
            f.archive_all()
        self.assertEqual(self.data()["archivable"], 0)
        self.assertEqual(c, self.data()["tickets"][-1]["id"])

    def test_obsidian_synced_at_is_null_until_a_sync(self):
        self.add()
        self.assertIsNone(self.data()["obsidian_synced_at"])
        with self.f() as f:
            f.sync_obsidian()
        self.assertRegex(self.data()["obsidian_synced_at"], r"^\d{4}-\d\d-\d\dT")
