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

    FOLDED = ("---\nid: WONKA-00001\ntitle: >-\n  Handle block scalars and trailing colons in note YAML so that\n"
              "  long titles survive\nstatus: ✉️-Inbox\nphase: 🕰️-Pending\nsize: s\njira:\n---\nbody\n")
    LONG = "Handle block scalars and trailing colons in note YAML so that long titles survive"

    def _meta(self, frontmatter):
        path = Path(tempfile.mkdtemp()) / "t.md"
        path.write_text(f"---\n{frontmatter}\n---\nbody\n")
        return note.read(path)[0]

    def test_reads_folded_block_scalar_as_js_yaml_writes_long_titles(self):
        path = Path(tempfile.mkdtemp()) / "t.md"
        path.write_text(self.FOLDED)
        meta, body = note.read(path)
        self.assertEqual(meta["title"], self.LONG)
        self.assertEqual((meta["status"], meta["phase"], meta["jira"]), ("✉️-Inbox", "🕰️-Pending", ""))
        self.assertEqual(body, "body\n")

    def test_reads_literal_and_clipped_block_scalars(self):
        self.assertEqual(self._meta("title: |-\n  one\n  two\nsize: s")["title"], "one\ntwo")
        self.assertEqual(self._meta("title: >\n  a\n  b\n\n  c\n\nsize: s")["title"], "a b\nc")
        self.assertEqual(self._meta("title: > # c\n  x")["title"], "x")
        self.assertEqual(self._meta("title: >-\nsize: s"), {"title": "", "size": "s"})

    def test_block_scalar_title_round_trips_to_one_quoted_line(self):
        path = Path(tempfile.mkdtemp()) / "t.md"
        path.write_text(self.FOLDED)
        meta, body = note.read(path)
        note.write(path, meta, body)
        self.assertIn(f"\ntitle: {self.LONG}\nstatus:", path.read_text(), "one plain line: nothing to quote")
        self.assertEqual(note.read(path), (meta, body))
        path.write_text(self.FOLDED.replace("so that\n", "so that:\n"))
        meta, body = note.read(path)
        note.write(path, meta, body)
        self.assertIn(f'\ntitle: "{self.LONG.replace("so that", "so that:")}"\nstatus:', path.read_text())
        self.assertEqual(note.read(path), (meta, body))

    def test_reads_plain_multiline_scalar(self):
        self.assertEqual(self._meta("title: foo\n  bar\n\nsize: s"), {"title": "foo bar", "size": "s"})
        self.assertEqual(self._meta("title: foo\n\nsize: s")["title"], "foo")

    def test_quotes_values_plain_yaml_would_mangle(self):
        path = Path(tempfile.mkdtemp()) / "t.md"
        for title in ("Fix:", "x:", " padded ", "a\tb", "a\nb", "~"):
            with self.subTest(title=title):
                note.write(path, {"id": "WONKA-00001", "title": title}, "body")
                line = next(l for l in path.read_text().splitlines() if l.startswith("title:"))
                self.assertTrue(line.startswith('title: "'), line)
                self.assertEqual(note.read(path)[0]["title"], title)
        note.write(path, {"id": "WONKA-00001", "title": "plain title"}, "body")
        text = path.read_text()
        self.assertIn("\nid: WONKA-00001\n", text)
        self.assertIn("\ntitle: plain title\n", text)

    def test_new_questions_archives_an_answered_round(self):
        body = note.set_section("", "idea", "an idea")
        body = note.set_section(body, "log", "> - event")
        body = note.new_questions(body, note.render_questions([{"q": "Round A?", "options": ["a1", "a2"]}]))
        body = body.replace("- [ ] a1", "- [x] a1").replace("  - [ ] Other (write below)",
                                                              "  - [ ] Other (write below)\nmy free text")
        body = note.new_questions(body, note.render_questions([{"q": "Round B?", "options": ["b1", "b2"]}]))
        open_round = note.get_section(body, "questions")
        self.assertIn("Round B?", open_round)
        self.assertNotIn("Round A?", open_round)
        answers = note.get_section(body, "answers")
        self.assertTrue(answers.startswith("### Round 1\n"))
        self.assertIn("  - [x] a1", answers)
        self.assertIn("my free text", answers)
        self.assertFalse(note.all_answered(open_round))
        self.assertEqual(body.count("## ❓ Questions"), 1)
        self.assertGreater(body.index("## 🎩 Factory log"), body.index("## ❓ Questions"), "log stays last")
        self.assertGreater(body.index("## ❓ Questions"), body.index("## ✅ Answers"))

        body = body.replace("- [ ] b2", "- [x] b2")
        body = note.new_questions(body, note.render_questions([{"q": "Round C?", "options": ["c1"]}]))
        answers = note.get_section(body, "answers")
        self.assertLess(answers.index("### Round 1"), answers.index("### Round 2"))
        self.assertLess(answers.index("Round A?"), answers.index("### Round 2"))
        self.assertGreater(answers.index("  - [x] b2"), answers.index("### Round 2"))
        self.assertIn("Round C?", note.get_section(body, "questions"))
        self.assertEqual(body.count("## ✅ Answers"), 1)

    def test_new_questions_archives_a_round_with_only_free_text(self):
        cases = {
            "under the question": ("- **Q1. Round A?**", "- **Q1. Round A?**\n    I'd use Postgres instead"),
            "under an unticked Other": ("  - [ ] Other (write below)",
                                        "  - [ ] Other (write below)\n    I'd use Postgres instead"),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name):
                body = note.set_section("", "idea", "an idea")
                body = note.set_section(body, "log", "> - event")
                body = note.new_questions(body, note.render_questions([{"q": "Round A?", "options": ["a1", "a2"]}]))
                body = body.replace(old, new)
                self.assertNotIn("[x]", body)
                body = note.new_questions(body, note.render_questions([{"q": "Round B?", "options": ["b1"]}]))
                answers = note.get_section(body, "answers")
                self.assertTrue(answers.startswith("### Round 1\n"))
                self.assertIn("Round A?", answers)
                self.assertIn("I'd use Postgres instead", answers)
                open_round = note.get_section(body, "questions")
                self.assertIn("Round B?", open_round)
                self.assertNotIn("Round A?", open_round)

    def test_new_questions_replaces_an_unanswered_round(self):
        body = note.set_section("", "idea", "an idea")
        body = note.new_questions(body, note.render_questions([{"q": "Round A?", "options": ["a1"]}]))
        body = note.new_questions(body, note.render_questions([{"q": "Round B?", "options": ["b1"]}]))
        self.assertIsNone(note.get_section(body, "answers"))
        self.assertNotIn("Round A?", body)
        self.assertIn("Round B?", note.get_section(body, "questions"))

        body = body.replace("- **Q1. Round B?**", "- **Q1. Round B?**\n   \n\n\t")
        body = note.new_questions(body, note.render_questions([{"q": "Round C?", "options": ["c1"]}]))
        self.assertIsNone(note.get_section(body, "answers"))
        self.assertNotIn("Round B?", body)
        self.assertIn("Round C?", note.get_section(body, "questions"))

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

    def test_all_recommended(self):
        skip = (note.CLOSE_Q, note.DRAFT_Q)
        sec = note.render_questions([
            {"q": "Redis or Postgres?", "options": ["Redis", "Postgres"], "recommended": 1, "why": "in stack"},
            {"q": note.CLOSE_Q, "options": ["Yes", "No"], "recommended": 0, "other": False},
        ])
        self.assertFalse(note.all_recommended(sec, skip=skip), "unanswered")
        answered = sec.replace("- [ ] Postgres", "- [x] Postgres").replace("- [ ] No", "- [x] No")
        self.assertTrue(note.all_recommended(answered, skip=skip))
        self.assertFalse(note.all_recommended(answered), "the CLOSE_Q 'No' is not recommended")
        other_opt = sec.replace("- [ ] Redis", "- [x] Redis").replace("- [ ] No", "- [x] No")
        self.assertFalse(note.all_recommended(other_opt, skip=skip))
        other = sec.replace("- [ ] Other (write below)", "- [x] Other (write below)").replace("- [ ] No", "- [x] No")
        self.assertFalse(note.all_recommended(other, skip=skip))
        lines = answered.splitlines()
        lines.insert(1, "    I'd rather use X")
        self.assertFalse(note.all_recommended("\n".join(lines), skip=skip), "free text under a question")
        lines[1] = "   "
        self.assertTrue(note.all_recommended("\n".join(lines), skip=skip), "blank lines are not free text")
        no_rec = note.render_questions([{"q": "Name?", "options": ["hello", "hi"]}]).replace("- [ ] hello", "- [x] hello")
        self.assertFalse(note.all_recommended(no_rec))
        self.assertFalse(note.all_recommended(""))
        self.assertFalse(note.all_recommended(None))

    def test_approval_and_retry_checkboxes(self):
        self.assertFalse(note.approved("- [ ] Approve (size m)"))
        self.assertTrue(note.approved("- [x] Approve (size m)"))
        self.assertFalse(note.approved_retry("- [ ] Retry"))
        self.assertTrue(note.approved_retry("- [X] Retry (stuck)"))

    def test_set_banner_replaces_only_the_old_banner(self):
        old = "# 🎫 T\n\n> [!info] x\n> y\n> *q*\n> \n> facts\n\n"
        user = "> my own quote\n\n# My heading\n\n"
        new = "# 🎫 T\n\n> [!info] new"
        text = note.set_banner(old + user + "## 💡 Idea\nidea\n", new)
        self.assertTrue(text.startswith(new + "\n\n"), text)
        self.assertEqual(text.count("> my own quote"), 1)
        self.assertEqual(text.count("# My heading"), 1)
        self.assertNotIn("> [!info] x", text)
        self.assertNotIn("> y", text)
        self.assertTrue(text.endswith("## 💡 Idea\nidea\n"))
        self.assertEqual(note.set_banner(text, new), text, "idempotent")

    def test_set_banner_on_a_note_without_one(self):
        self.assertEqual(note.set_banner("## 💡 Idea\nidea\n", "# 🎫 T\n\n> [!info] new"),
                         "# 🎫 T\n\n> [!info] new\n\n## 💡 Idea\nidea\n")


if __name__ == "__main__":
    unittest.main()
