import tempfile
import unittest
from pathlib import Path

from wonka import note


class NoteTest(unittest.TestCase):
    def test_frontmatter_round_trip_quotes_titles(self):
        path = Path(tempfile.mkdtemp()) / "t.md"
        meta = {"id": "WONKA-00001", "title": "Fix: the thing", "status": "✉️-Inbox", "phase": "🕰️-Pending",
                "size": "s", "jira": ""}
        note.write(path, meta, "## 💡 Idea\nhello\n")
        back, body = note.read(path)
        self.assertEqual(back, meta)
        self.assertIn('title: "Fix: the thing"', path.read_text())
        self.assertEqual(body, "## 💡 Idea\nhello\n")

    def test_frontmatter_keeps_obsidian_lists_byte_for_byte(self):
        path = Path(tempfile.mkdtemp()) / "t.md"
        text = "---\nid: WONKA-00001\ntitle: T\nstatus: ✉️-Inbox\nphase: 🕰️-Pending\nsize: s\njira:\ntags:\n  - a\n  - b\ncssclasses: wide\n---\nbody\n"
        path.write_text(text)
        meta, body = note.read(path)
        meta["size"] = "m"
        note.write(path, meta, body)
        self.assertEqual(path.read_text(), text.replace("size: s", "size: m"))

    def test_reads_frontmatter_as_obsidian_rewrites_it_after_a_drag(self):
        """A kanban drop rewrites the frontmatter through Obsidian's YAML serializer (processFrontMatter)."""
        path = Path(tempfile.mkdtemp()) / "t.md"
        path.write_text("---\nid: WONKA-00001\ntitle: 'It''s: done'\nstatus: \"🍫-Baking\"\nphase: 🕰️-Pending\n"
                        "size: s\njira: null\n---\nbody\n")
        meta, _ = note.read(path)
        self.assertEqual((meta["title"], meta["status"], meta["jira"]), ("It's: done", "🍫-Baking", ""))

    def test_sections_replace_and_append(self):
        body = note.set_section("", "idea", "an idea")
        body = note.set_section(body, "spec", "v1")
        body = note.set_section(body, "spec", "v2")
        self.assertEqual(note.get_section(body, "spec"), "v2")
        self.assertEqual(note.get_section(body, "idea"), "an idea")
        self.assertEqual(body.count("## 📜 Spec"), 1)
        self.assertIsNone(note.get_section(body, "questions"))

    def test_section_content_headings_are_demoted(self):
        body = note.set_section("", "idea", "an idea")
        body = note.set_section(body, "spec", "intro\n## Changes\n- a\n# Top\n### Keep\n#tag")
        self.assertEqual(note.get_section(body, "spec"), "intro\n### Changes\n- a\n### Top\n### Keep\n#tag")
        body = note.set_section(body, "spec", "v2\n## Changes\n- b")
        self.assertNotIn("\n## Changes", body)
        self.assertEqual(body.count("## 📜 Spec"), 1)
        self.assertEqual(note.get_section(body, "spec"), "v2\n### Changes\n- b")
        self.assertEqual(note.get_section(body, "idea"), "an idea")
        self.assertNotIn("- a", body)

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

    def test_approval_and_retry_checkboxes(self):
        self.assertFalse(note.approved("- [ ] Approve (size m)"))
        self.assertTrue(note.approved("- [x] Approve (size m)"))
        self.assertFalse(note.approved_retry("- [ ] Retry"))
        self.assertTrue(note.approved_retry("- [X] Retry (stuck)"))


if __name__ == "__main__":
    unittest.main()
