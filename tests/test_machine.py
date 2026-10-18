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
        self.assertFalse(machine.allowed(("shipped", "done"), ("inbox", "pending")))

    def test_parse_accepts_labels_with_or_without_emoji(self):
        self.assertEqual(machine.parse("🐿️-Reviewing", machine.STATUSES), "reviewing")
        self.assertEqual(machine.parse("🐿-Reviewing", machine.STATUSES), "reviewing")
        self.assertEqual(machine.parse("Wont-Do", machine.STATUSES), "wontdo")
        self.assertEqual(machine.parse("awaiting", machine.PHASES), "awaiting")
        self.assertIsNone(machine.parse("Done-ish", machine.PHASES))


if __name__ == "__main__":
    unittest.main()
