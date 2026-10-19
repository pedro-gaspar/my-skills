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


if __name__ == "__main__":
    unittest.main()
