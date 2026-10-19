import unittest

from wonka import machine


class MachineTest(unittest.TestCase):
    def test_old_status_tables_point_at_current_statuses(self):
        for key in machine.OLD_STATUSES:
            self.assertIn(key, machine.OLD_STATUS_KEYS)
        for new in machine.OLD_STATUS_KEYS.values():
            self.assertIn(new, machine.STATUSES)

    def test_every_edge_joins_valid_states(self):
        for src, dst in machine.EDGES:
            self.assertTrue(machine.valid(src), src)
            self.assertTrue(machine.valid(dst), dst)

    def test_any_live_ticket_can_be_killed_once(self):
        self.assertTrue(machine.allowed(("baking", "progress"), machine.KILLED))
        self.assertFalse(machine.allowed(machine.KILLED, machine.KILLED))

    def test_flaky_ci_edge(self):
        self.assertTrue(machine.allowed(("baking", "progress"), ("shipped", "awaiting")))
        self.assertFalse(machine.allowed(("baking", "pending"), ("shipped", "awaiting")))

    def test_skipping_columns_is_illegal(self):
        self.assertFalse(machine.allowed(("triage", "pending"), ("baking", "pending")))
        self.assertFalse(machine.allowed(("triage", "awaiting"), ("drafting", "pending")))
        self.assertFalse(machine.allowed(("shipped", "ready"), ("triage", "pending")))

    def test_open_pr_can_go_back_in_progress_to_fix_conflicts(self):
        self.assertTrue(machine.allowed(("shipped", "awaiting"), ("shipped", "progress")))

    def test_idea_only_goes_to_triage_or_wont_do(self):
        idea = ("idea", "awaiting")
        self.assertTrue(machine.allowed(idea, ("triage", "pending")))
        for dst in (("baking", "pending"), ("triage", "ready"), ("drafting", "pending")):
            self.assertFalse(machine.allowed(idea, dst), dst)
        self.assertTrue(machine.allowed(idea, machine.KILLED))
        self.assertFalse(machine.valid(("idea", "pending")))
        self.assertEqual(machine.parse("💡-Idea", machine.STATUSES), "idea")
        self.assertEqual(machine.parse("Idea", machine.STATUSES), "idea")

    def test_migrate_moves_ideas_to_awaiting(self):
        db = {"tickets": {"A": {"last_valid": {"status": "idea", "phase": "pending"},
                                "history": [{"from": "idea/pending", "to": "triage/pending"}]}}}
        self.assertTrue(machine.migrate(db))
        t = db["tickets"]["A"]
        self.assertEqual(t["last_valid"], {"status": "idea", "phase": "awaiting"})
        self.assertEqual(t["history"][0]["from"], "idea/awaiting")
        before = repr(db)
        self.assertFalse(machine.migrate(db))
        self.assertEqual(repr(db), before)

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
        for old in ("✉️-Inbox", "✉-Inbox", "Inbox", "inbox", "📏-Triage", "triage", "Triage"):
            self.assertEqual(machine.parse(old, machine.STATUSES), "triage", old)

    def test_migrate_renames_inbox_to_triage(self):
        db = {"tickets": {"a": {"last_valid": {"status": "inbox", "phase": "awaiting"},
                                "history": [{"from": "inbox/pending", "to": "inbox/awaiting"}]}}}
        self.assertTrue(machine.migrate(db))
        t = db["tickets"]["a"]
        self.assertEqual(t["last_valid"], {"status": "triage", "phase": "awaiting"})
        self.assertEqual(t["history"], [{"from": "triage/pending", "to": "triage/awaiting"}])
        self.assertFalse(machine.migrate(db))

    def test_migrate_rewrites_old_keys_once(self):
        db = {"tickets": {"a": {
            "last_valid": {"status": "ready", "phase": "pending"},
            "history": [{"from": "inbox/done", "to": "ready/pending"}, {"from": "wontdo/done", "to": "inbox/done"},
                        {"why": "just a note"}]}}}
        self.assertTrue(machine.migrate(db))
        t = db["tickets"]["a"]
        self.assertEqual(t["last_valid"], {"status": "planned", "phase": "pending"})
        self.assertEqual(t["history"], [{"from": "triage/ready", "to": "planned/pending"},
                                        {"from": "wontdo/ready", "to": "triage/ready"}, {"why": "just a note"}])
        before = repr(db)
        self.assertFalse(machine.migrate(db))
        self.assertEqual(repr(db), before)


    def test_requested_changes_send_an_open_pr_back_to_baking(self):
        self.assertTrue(machine.allowed(("shipped", "awaiting"), ("baking", "pending")))
        self.assertFalse(machine.allowed(("shipped", "pending"), ("baking", "pending")))

    def test_spec_approval_edge(self):
        self.assertTrue(machine.allowed(("drafting", "awaiting"), ("planned", "pending")))
        self.assertFalse(machine.allowed(("triage", "awaiting"), ("planned", "pending")))


if __name__ == "__main__":
    unittest.main()
