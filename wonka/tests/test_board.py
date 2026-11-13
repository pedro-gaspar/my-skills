import unittest

from tests.fixtures import RepoFixture
from wonka import board, note, store
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

    def test_dragging_an_idea_to_triage_starts_it(self):
        tid = self.idea()
        with self.f() as f:
            board.drag(f, tid, "triage")
        self.assertEqual(self.state(tid), ("triage", "pending"))

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

    def test_editing_the_idea_rewrites_the_note(self):
        tid = self.idea()
        with self.f() as f:
            board.answer(f, tid, [{"op": "idea", "text": "Add a bye script too"}])
            self.assertEqual(note.get_section(f.read(tid)[1], "idea"), "Add a bye script too")
            self.assertIn("Add a bye script too", f.path(tid).read_text())

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
