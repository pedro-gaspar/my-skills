import tempfile
import unittest
from pathlib import Path

from wonka import note


class NoteTest(unittest.TestCase):
    def test_frontmatter_round_trip_keeps_unknown_keys_and_quotes(self):
        path = Path(tempfile.mkdtemp()) / "t.md"
        meta = {"id": "WONKA-00001", "title": "Fix: the thing", "status": "✉️-Inbox", "sub-status": "🕰️-Pending",
                "size": "s", "jira": "", "tags": "x"}
        note.write(path, meta, "## 💡 Idea\nhello\n")
        back, body = note.read(path)
        self.assertEqual(back, meta)
        self.assertIn('title: "Fix: the thing"', path.read_text())
        self.assertEqual(body, "## 💡 Idea\nhello\n")

    def test_sections_replace_and_append(self):
        body = note.set_section("", "idea", "an idea")
        body = note.set_section(body, "spec", "v1")
        body = note.set_section(body, "spec", "v2")
        self.assertEqual(note.get_section(body, "spec"), "v2")
        self.assertEqual(note.get_section(body, "idea"), "an idea")
        self.assertEqual(body.count("## 📜 Spec"), 1)
        self.assertIsNone(note.get_section(body, "questions"))

    def test_questions_answered_only_when_every_question_has_a_tick(self):
        sec = note.render_questions([
            {"q": "Redis or Postgres?", "options": ["Redis", "Postgres"], "recommended": 1, "why": "in stack"},
            {"q": note.CLOSE_Q, "options": ["Yes", "No"], "recommended": 0, "other": False},
        ])
        self.assertIn("Postgres ⭐ recommended — in stack", sec)
        self.assertFalse(note.all_answered(sec))
        sec = sec.replace("- [ ] Redis", "- [x] Redis")
        self.assertFalse(note.all_answered(sec))
        sec = sec.replace("- [ ] Yes", "- [x] Yes")
        self.assertTrue(note.all_answered(sec))
        self.assertTrue(note.picked_yes(sec, note.CLOSE_Q))
        self.assertFalse(note.picked_yes(sec, note.DRAFT_Q))

    def test_approval(self):
        self.assertFalse(note.approved("- [ ] Approve (size m)"))
        self.assertTrue(note.approved("- [x] Approve (size m)"))


if __name__ == "__main__":
    unittest.main()
