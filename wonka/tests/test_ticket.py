import unittest

from wonka import note, ticket

BODY = """# 🎫 T

> [!tip] banner

## 💡 Idea
Fix it.
- [ ] a plain checklist stays text

## ❓ Questions
- **Q1. Which endpoint?**
  - [x] /days ⭐ recommended — already exists
  - [ ] /score
        only if /days is slow
  - [ ] Other (write below)
also cache it

- **Q2. Ship it?**
  - [ ] Yes

## 💡 Start
- [X] Start (send it to triage)

## My own heading
kept
"""


class TicketTest(unittest.TestCase):
    def ticket(self):
        meta = {"id": "WONKA-00001", "title": "T", "tags": note.Raw("tags:\n  - a")}
        return ticket.from_note("WONKA-00001", meta, BODY)

    def section(self, heading):
        return next(s for s in self.ticket()["sections"] if s["heading"] == heading)

    def test_round_trip_gives_the_note_back(self):
        meta, body = ticket.to_note(self.ticket())
        self.assertEqual(body, BODY.replace("[X]", "[x]"))
        self.assertIsInstance(meta["tags"], note.Raw)

    def test_questions_are_data(self):
        q1 = next(b for b in self.section("❓ Questions")["blocks"] if b["type"] == "question")
        self.assertEqual(q1["q"], "Q1. Which endpoint?")
        self.assertEqual(q1["options"][0], {"text": "/days", "checked": True, "recommended": True,
                                            "why": "already exists", "after": []})
        self.assertEqual(q1["options"][1]["recommended"], False)
        self.assertEqual(q1["options"][1]["after"], ["        only if /days is slow"])
        self.assertEqual(q1["options"][2]["text"], "Other (write below)")
        self.assertEqual(q1["notes"], ["also cache it"])

    def test_boxes_are_data_only_in_sections_a_human_answers(self):
        self.assertEqual(self.section("💡 Start")["blocks"][0], {"type": "box", "text": "Start (send it to triage)",
                                                                "checked": True})
        self.assertEqual([b["type"] for b in self.section("💡 Idea")["blocks"]], ["text"])

    def test_answering_in_the_json_shows_in_the_markdown_the_factory_reads(self):
        t = self.ticket()
        q2 = [b for s in t["sections"] for b in s["blocks"] if b["type"] == "question"][1]
        q2["options"][0]["checked"] = True
        body = ticket.to_note(t)[1]
        self.assertTrue(note.all_answered(note.get_section(body, "questions")))
