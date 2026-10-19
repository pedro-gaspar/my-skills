import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import machine, store


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

    def test_config_of_the_wrong_shape_names_the_key(self):
        cases = [
            ([], "must hold a JSON object"),
            ({"vault": None}, "vault must be a folder path"),
            ({"vault": "/x", "folder": None}, "folder must be a folder path"),
            ({"vault": "/x", "wip": None}, "wip must be an object"),
            ({"vault": "/x", "wip": {"baking": "2"}}, "wip.baking must be a number"),
            ({"vault": "/x", "stale_hours": "2"}, "stale_hours must be a number"),
            ({"vault": "/x", "max_review_rounds": True}, "max_review_rounds must be a number"),
            ({"vault": "/x", "test_commands": {"app": "./test.sh"}}, "test_commands.app must be a list of strings"),
            ({"vault": "/x", "ci_hosts": {"app": "ci.example.com"}}, "ci_hosts.app must be a list of strings"),
            ({"vault": "/x", "worktree_files": {"app": [".env", 1]}}, "worktree_files.app must be a list of strings"),
            ({"vault": "/x", "ci_hosts": ["ci.example.com"]}, "ci_hosts must be an object keyed by project"),
            ({"vault": "/x", "ci_log_command": ["beyond ci logs"]}, "ci_log_command must be an object"),
        ]
        for data, msg in cases:
            with self.subTest(data=data):
                (self.tmp / "config.json").write_text(json.dumps(data))
                with self.assertRaisesRegex(store.CriticalError, msg):
                    store.load_config()

    def test_config_non_finite_numbers_are_refused(self):
        for raw in ("Infinity", "NaN"):
            (self.tmp / "config.json").write_text(f'{{"vault": "/x", "test_lock_wait_minutes": {raw}}}')
            with self.assertRaisesRegex(store.CriticalError, "test_lock_wait_minutes must be a number"):
                store.load_config()

    def test_config_null_project_maps_read_as_empty(self):
        (self.tmp / "config.json").write_text(json.dumps({"vault": "/x", "test_commands": None, "ci_log_command": None}))
        cfg = store.load_config()
        self.assertEqual((cfg["test_commands"], cfg["ci_log_command"]), ({}, {}))

    def test_a_bad_ci_log_command_stops_only_its_project(self):
        (self.tmp / "config.json").write_text(json.dumps({"vault": "/x", "ci_log_command": {"app": 1}}))
        self.assertEqual(store.load_config()["ci_log_command"], {"app": 1})

    def test_set_setting_refuses_non_finite_numbers(self):
        cfg = {}
        for key, raw in (("stale_hours", "inf"), ("test_lock_wait_minutes", "nan"), ("stale_hours", "-inf")):
            with self.subTest(raw=raw), self.assertRaisesRegex(store.WonkaError, f"{key} must be a finite number"):
                store.set_setting(cfg, key, raw)
        self.assertEqual(cfg, {})

    def test_state_of_the_wrong_shape_is_a_friendly_error(self):
        cases = [([], "not a JSON object"), ({"next_id": 1, "tickets": []}, "tickets must be an object"),
                 ({"tickets": {}}, "next_id must be a number"),
                 ({"next_id": 2, "tickets": {"WONKA-00001": "x"}}, "ticket WONKA-00001 must be an object")]
        for data, msg in cases:
            with self.subTest(data=data):
                self.path.write_text(json.dumps(data))
                with self.assertRaisesRegex(store.CriticalError, rf"not a Wonka state file \({msg}.*\.bak"):
                    store.snapshot()
                with self.assertRaisesRegex(store.CriticalError, msg):
                    with store.state():
                        pass

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
        self.assertEqual(store.migrate_base(cfg), [("🎫-Ready", "🎫 Planned")])
        self.assertEqual(base.read_text(), "groupOrder:\n  - 🎫 Planned\n# mine\n")
        self.assertEqual(store.migrate_base(cfg), [])
        base.write_text("groupOrder:\n  - ✉️-Inbox\n  - 🎫-Ready\n# mine\n")
        self.assertEqual(store.migrate_base(cfg),
                         [("🎫-Ready", "🎫 Planned"), ("✉️-Inbox", "📏 Triage"), (None, "💡 Idea")])
        self.assertEqual(base.read_text(), "groupOrder:\n  - 💡 Idea\n  - 📏 Triage\n  - 🎫 Planned\n# mine\n")
        self.assertEqual(store.migrate_base(cfg), [])

    def test_migrate_base_follows_the_machine_tables(self):
        cfg = {"vault": str(self.tmp / "vault"), "folder": "wonka"}
        base = self.tmp / "vault/wonka" / store.BASE_FILE
        base.parent.mkdir(parents=True)
        base.write_text("groupOrder:\n  - 💡 Idea\n  - 📏 Triage\n  - 🧪-Brewing\n")
        with mock.patch.dict(machine.OLD_STATUSES, {"drafting_old": "🧪-Brewing"}), \
                mock.patch.dict(machine.OLD_STATUS_KEYS, {"drafting_old": "drafting"}):
            self.assertEqual(store.migrate_base(cfg), [("🧪-Brewing", "🧪 Drafting")])
        self.assertIn("🧪 Drafting", base.read_text())
        self.assertNotIn("🧪-Brewing", base.read_text())

    def test_migrate_base_adds_the_idea_column_once(self):
        cfg = {"vault": str(self.tmp / "vault"), "folder": "wonka"}
        base = self.tmp / "vault/wonka" / store.BASE_FILE
        base.parent.mkdir(parents=True)
        base.write_text("groupOrder:\n    - 📏 Triage\n    - 🧪 Drafting\n")
        self.assertEqual(store.migrate_base(cfg), [(None, "💡 Idea")])
        self.assertEqual(base.read_text(), "groupOrder:\n    - 💡 Idea\n    - 📏 Triage\n    - 🧪 Drafting\n")
        self.assertEqual(store.migrate_base(cfg), [])
        self.assertEqual(base.read_text(), "groupOrder:\n    - 💡 Idea\n    - 📏 Triage\n    - 🧪 Drafting\n")
        base.write_text("groupOrder:\n  - 🧪 Drafting\n")
        self.assertEqual(store.migrate_base(cfg), [])
        self.assertEqual(base.read_text(), "groupOrder:\n  - 🧪 Drafting\n")


if __name__ == "__main__":
    unittest.main()
