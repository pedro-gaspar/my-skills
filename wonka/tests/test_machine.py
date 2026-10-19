import unittest

from wonka import machine


class MachineTest(unittest.TestCase):
    def test_every_edge_joins_valid_states(self):
        for src, dst in machine.EDGES:
            self.assertTrue(machine.valid(src), src)
            self.assertTrue(machine.valid(dst), dst)

    def test_any_live_ticket_can_be_killed_once(self):
        self.assertTrue(machine.allowed(("baking", "progress"), machine.KILLED))
        self.assertFalse(machine.allowed(machine.KILLED, machine.KILLED))

    def test_skipping_columns_is_illegal(self):
        self.assertFalse(machine.allowed(("inbox", "pending"), ("baking", "pending")))
        self.assertFalse(machine.allowed(("inbox", "awaiting"), ("drafting", "pending")))
        self.assertFalse(machine.allowed(("shipped", "ready"), ("inbox", "pending")))

    def test_open_pr_can_go_back_in_progress_to_fix_conflicts(self):
        self.assertTrue(machine.allowed(("shipped", "awaiting"), ("shipped", "progress")))

    def test_parse_accepts_labels_with_or_without_emoji(self):
        self.assertEqual(machine.parse("🐿️-Reviewing", machine.STATUSES), "reviewing")
        self.assertEqual(machine.parse("🐿-Reviewing", machine.STATUSES), "reviewing")
        self.assertEqual(machine.parse("Wont-Do", machine.STATUSES), "wontdo")
        self.assertEqual(machine.parse("awaiting", machine.PHASES), "awaiting")
        self.assertIsNone(machine.parse("Done-ish", machine.PHASES))

    def test_parse_reads_old_names(self):
        self.assertEqual(machine.parse("🎫-Ready", machine.STATUSES), "planned")
        self.assertEqual(machine.parse("ready", machine.STATUSES), "planned")
        self.assertEqual(machine.parse("Ready", machine.STATUSES), "planned")
        self.assertEqual(machine.parse("🌟-Done", machine.PHASES), "ready")
        self.assertEqual(machine.parse("done", machine.PHASES), "ready")
        self.assertEqual(machine.parse("Ready", machine.PHASES), "ready")
        self.assertEqual(machine.parse("🎫-Planned", machine.STATUSES), "planned")

    def test_migrate_rewrites_old_keys_once(self):
        db = {"tickets": {"a": {
            "last_valid": {"status": "ready", "phase": "pending"},
            "history": [{"from": "inbox/done", "to": "ready/pending"}, {"from": "wontdo/done", "to": "inbox/done"},
                        {"why": "just a note"}]}}}
        self.assertTrue(machine.migrate(db))
        t = db["tickets"]["a"]
        self.assertEqual(t["last_valid"], {"status": "planned", "phase": "pending"})
        self.assertEqual(t["history"], [{"from": "inbox/ready", "to": "planned/pending"},
                                        {"from": "wontdo/ready", "to": "inbox/ready"}, {"why": "just a note"}])
        before = repr(db)
        self.assertFalse(machine.migrate(db))
        self.assertEqual(repr(db), before)


    def test_requested_changes_send_an_open_pr_back_to_baking(self):
        self.assertTrue(machine.allowed(("shipped", "awaiting"), ("baking", "pending")))
        self.assertFalse(machine.allowed(("shipped", "pending"), ("baking", "pending")))

if __name__ == "__main__":
    unittest.main()
