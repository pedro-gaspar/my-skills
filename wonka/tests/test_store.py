import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import store


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"WONKA_HOME": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.tmp / store.STATE_FILE
        self.bak = self.tmp / (store.STATE_FILE + ".bak")

    def next_ids(self):
        return (json.loads(self.path.read_text())["next_id"],
                json.loads(self.bak.read_text())["next_id"] if self.bak.exists() else None)

    def test_first_write_makes_no_backup(self):
        with store.state() as db:
            db["next_id"] = 2
        self.assertEqual(self.next_ids(), (2, None))

    def test_later_writes_keep_the_previous_version(self):
        with store.state() as db:
            db["next_id"] = 2
        with store.state() as db:
            db["next_id"] = 3
        self.assertEqual(self.next_ids(), (3, 2))

    def test_unchanged_state_is_not_rewritten(self):
        with store.state() as db:
            db["next_id"] = 2
        with store.state() as db:
            db["next_id"] = 3
        with store.state():
            pass
        self.assertEqual(self.next_ids(), (3, 2), "the backup is not overwritten by an identical copy")

    def test_an_error_writes_nothing(self):
        with store.state() as db:
            db["next_id"] = 2
        with self.assertRaises(RuntimeError), store.state() as db:
            db["next_id"] = 9
            raise RuntimeError("boom")
        self.assertEqual(self.next_ids(), (2, None))

    def test_old_config_without_max_review_rounds_gets_the_default(self):
        (self.tmp / "config.json").write_text(json.dumps({"vault": "/x"}))
        self.assertEqual(store.load_config()["max_review_rounds"], 3)


    def test_corrupt_state_is_a_friendly_error(self):
        self.path.write_text("")  # a crash mid-write on an old version
        with self.assertRaisesRegex(store.WonkaError, r"not valid JSON.*\.bak"):
            with store.state():
                pass
        with self.assertRaisesRegex(store.WonkaError, "not valid JSON"):
            store.snapshot()

    def test_corrupt_config_is_a_friendly_error(self):
        (self.tmp / "config.json").write_text("{")
        with self.assertRaisesRegex(store.WonkaError, "config.json is not valid JSON"):
            store.load_config()

    def test_a_held_lock_times_out_but_snapshots_still_read(self):
        with store.state() as db:
            db["next_id"] = 5
        import fcntl
        with open(self.tmp / ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(store.snapshot()["next_id"], 5)
            with mock.patch.object(store, "LOCK_TIMEOUT", 0.3), \
                    self.assertRaisesRegex(store.WonkaError, "the factory is busy"):
                with store.state():
                    pass

    def test_writes_leave_no_temp_file(self):
        with store.state() as db:
            db["next_id"] = 2
        with store.state() as db:
            db["next_id"] = 3
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         [".lock", store.STATE_FILE, store.STATE_FILE + ".bak"])

    def test_old_state_keys_are_migrated_on_read_and_saved_with_a_backup(self):
        old = {"next_id": 2, "tickets": {"WONKA-1": {"last_valid": {"status": "ready", "phase": "pending"},
                                                      "history": [{"from": "inbox/done", "to": "ready/pending"}]}}}
        self.path.write_text(json.dumps(old))
        snap = store.snapshot()
        self.assertEqual(snap["tickets"]["WONKA-1"]["last_valid"], {"status": "planned", "phase": "pending"})
        with store.state():
            pass
        new = json.loads(self.path.read_text())
        self.assertEqual(new["tickets"]["WONKA-1"]["history"], [{"from": "triage/ready", "to": "planned/pending"}])
        self.assertEqual(json.loads(self.bak.read_text()), old)

    def test_old_inbox_state_keys_are_migrated_on_read_and_saved_with_a_backup(self):
        old = {"next_id": 2, "tickets": {"WONKA-1": {"last_valid": {"status": "inbox", "phase": "pending"},
                                                      "history": [{"from": "inbox/pending", "to": "inbox/ready"}]}}}
        self.path.write_text(json.dumps(old))
        snap = store.snapshot()
        self.assertEqual(snap["tickets"]["WONKA-1"]["last_valid"], {"status": "triage", "phase": "pending"})
        with store.state():
            pass
        new = json.loads(self.path.read_text())
        self.assertEqual(new["tickets"]["WONKA-1"]["history"], [{"from": "triage/pending", "to": "triage/ready"}])
        self.assertEqual(json.loads(self.bak.read_text()), old)

    def test_migrate_base_renames_the_column_only(self):
        cfg = {"vault": str(self.tmp / "vault"), "folder": "wonka"}
        base = self.tmp / "vault/wonka" / store.BASE_FILE
        self.assertEqual(store.migrate_base(cfg), [])
        base.parent.mkdir(parents=True)
        base.write_text("groupOrder:\n  - 🎫-Ready\n# mine\n")
        self.assertEqual(store.migrate_base(cfg), [("🎫-Ready", "🎫-Planned")])
        self.assertEqual(base.read_text(), "groupOrder:\n  - 🎫-Planned\n# mine\n")
        self.assertEqual(store.migrate_base(cfg), [])
        base.write_text("groupOrder:\n  - ✉️-Inbox\n  - 🎫-Ready\n# mine\n")
        self.assertEqual(store.migrate_base(cfg),
                         [("🎫-Ready", "🎫-Planned"), ("✉️-Inbox", "📏-Triage"), (None, "💡-Idea")])
        self.assertEqual(base.read_text(), "groupOrder:\n  - 💡-Idea\n  - 📏-Triage\n  - 🎫-Planned\n# mine\n")
        self.assertEqual(store.migrate_base(cfg), [])

    def test_migrate_base_adds_the_idea_column_once(self):
        cfg = {"vault": str(self.tmp / "vault"), "folder": "wonka"}
        base = self.tmp / "vault/wonka" / store.BASE_FILE
        base.parent.mkdir(parents=True)
        base.write_text("groupOrder:\n    - 📏-Triage\n    - 🧪-Drafting\n")
        self.assertEqual(store.migrate_base(cfg), [(None, "💡-Idea")])
        self.assertEqual(base.read_text(), "groupOrder:\n    - 💡-Idea\n    - 📏-Triage\n    - 🧪-Drafting\n")
        self.assertEqual(store.migrate_base(cfg), [])
        self.assertEqual(base.read_text(), "groupOrder:\n    - 💡-Idea\n    - 📏-Triage\n    - 🧪-Drafting\n")
        base.write_text("groupOrder:\n  - 🧪-Drafting\n")
        self.assertEqual(store.migrate_base(cfg), [])
        self.assertEqual(base.read_text(), "groupOrder:\n  - 🧪-Drafting\n")


if __name__ == "__main__":
    unittest.main()
