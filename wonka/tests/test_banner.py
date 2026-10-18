import unittest

from wonka import banner, machine, store


class BannerTest(unittest.TestCase):
    def test_every_legal_state_tells_a_story(self):
        cfg = dict(store.DEFAULT_CONFIG)
        for status, subs in machine.VALID.items():
            for sub in subs:
                t = {"last_valid": {"status": status, "phase": sub}, "project": "app", "history": [],
                     "review_round": 1, "reviewers": ["architect"], "entered_done_at": "2026-10-18T10:00:00+00:00"}
                text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
                self.assertTrue(text.startswith("# 🎫 WONKA-00001\n\n> [!"), (status, sub))
                self.assertIn(banner.QUIPS[(status, sub)], text)

    def test_log_is_newest_first_and_folded(self):
        t = {"history": [
            {"at": "2026-10-18T10:00:00+00:00", "from": "inbox/pending", "to": "inbox/done", "by": "triage"},
            {"at": "2026-10-18T11:00:00+00:00", "by": "wonka", "why": "Reverted"},
        ]}
        lines = banner.log(t).splitlines()
        self.assertEqual(lines[0], "> [!note]- 2 events")
        self.assertIn("Reverted", lines[1])
        self.assertIn("✉️-Inbox 🕰️-Pending → ✉️-Inbox 🌟-Done", lines[2])

    def test_review_round_uses_the_max_rounds_setting(self):
        cfg = {**store.DEFAULT_CONFIG, "max_review_rounds": 5}
        t = {"last_valid": {"status": "reviewing", "phase": "progress"}, "project": "app", "history": [],
             "review_round": 2, "reviewers": ["architect"], "entered_done_at": "2026-10-18T10:00:00+00:00"}
        text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("review round 2/5", text)


if __name__ == "__main__":
    unittest.main()
