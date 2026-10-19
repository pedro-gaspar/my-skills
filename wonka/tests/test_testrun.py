"""wonka test: prefix checks, the per-project lock and the run itself, against a real repo."""

import contextlib
import fcntl
import io
import json
import time
import unittest
from unittest import mock

from wonka import cli, store, testrun
from wonka.store import WonkaError

from . import test_gitwrap


class OpenPrefixTest(unittest.TestCase):
    def test_open(self):
        for p in ["python3", "python3 -c", "python3 -m", "/bin/sh -c", "bash", "bash -x", "node -e", "env", "npx",
                  "uv run", "poetry run", "python3.12 -c"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))

    def test_pinned(self):
        for p in ["python3 -m unittest", "python3 -m pytest", "bash ./test.sh", "sh -c pytest", "./test.sh",
                  "make test", "pytest", "uv run pytest", "npm test", "npm run test"]:
            with self.subTest(p):
                self.assertFalse(testrun.open_prefix(p))

    def test_unparsable_or_empty(self):
        self.assertFalse(testrun.open_prefix('"oops'))
        self.assertFalse(testrun.open_prefix(""))

    def test_open_prefixes(self):
        cfg = {"test_commands": {"b": ["sh -c", "make test"], "a": ["./t.sh", "env", "bash"]}}
        self.assertEqual(testrun.open_prefixes(cfg), [("a", "env"), ("a", "bash"), ("b", "sh -c")])
        self.assertEqual(testrun.open_prefixes(cfg, "b"), [("b", "sh -c")])
        self.assertEqual(testrun.open_prefixes(cfg, "zzz"), [])
        self.assertEqual(testrun.open_prefixes({}), [])
        self.assertEqual(testrun.open_prefixes({"test_commands": None}), [])


class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(testrun._parse(["T", "--", "x"]), ("T", None, ["x"]))
        self.assertEqual(testrun._parse(["T", "--dispatch", "2", "--", "x", "--", "y"]), ("T", 2, ["x", "--", "y"]))
        for argv in (["T"], ["T", "--"], ["T", "x"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(WonkaError, "usage"):
                testrun._parse(argv)

    def test_prefixes_match_whole_tokens(self):
        self.assertIsNone(testrun.check(["make", "test", "-k"], ["make test"], "p"))
        self.assertIsNone(testrun.check(["./t.sh"], ["./t.sh"], "p"))
        for argv in (["make"], ["./t.sh.evil"], ["sh", "-c", "true"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(WonkaError, "allowed prefixes"):
                testrun.check(argv, ["make test", "./t.sh"], "p")
        with self.assertRaisesRegex(WonkaError, "wonka config test_commands.p"):
            testrun.check(["x"], [], "p")


class RunTest(unittest.TestCase):
    f = test_gitwrap.WrapperTest.f
    add = test_gitwrap.WrapperTest.add
    tick = test_gitwrap.WrapperTest.tick
    state = test_gitwrap.WrapperTest.state
    to_planned = test_gitwrap.WrapperTest.to_planned
    to_baking = test_gitwrap.WrapperTest.to_baking

    def setUp(self):
        test_gitwrap.WrapperTest.setUp(self)
        self.cfg = {**self.cfg, "test_commands": {"app": ["./t.sh", "python3 -c"]}, "test_lock_wait_minutes": 0.01}
        store.save_config(self.cfg)

    def wtest(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["test", *argv])
        return code, err.getvalue()

    def test_allowed_prefix_runs_in_the_worktree_and_passes_the_exit_code(self):
        tid = self.add()
        wt = self.to_baking(tid)
        code, _ = self.wtest(tid, "--", "python3", "-c",
                             "import os,sys; open('cwd.txt','w').write(os.getcwd()); sys.exit(3)")
        self.assertEqual(code, 3)
        self.assertTrue((wt / "cwd.txt").exists())
        self.assertFalse((self.repo / "cwd.txt").exists())

    def test_disallowed_prefix_is_refused(self):
        tid = self.add()
        self.to_baking(tid)
        code, err = self.wtest(tid, "--", "sh", "-c", "true")
        self.assertEqual(code, 1)
        self.assertIn("./t.sh", err)
        self.assertEqual(self.wtest(tid, "--", "./t.sh.evil")[0], 1)

    def test_project_without_test_commands_is_refused(self):
        tid = self.add()
        self.to_baking(tid)
        store.save_config({**self.cfg, "test_commands": {}})
        code, err = self.wtest(tid, "--", "./t.sh")
        self.assertEqual(code, 1)
        self.assertIn("wonka config test_commands.", err)

    def test_busy_lock_waits_then_exits_75_naming_the_holder(self):
        tid = self.add()
        wt = self.to_baking(tid)
        lp = testrun.lock_path("app")
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "w") as held:
            held.write(json.dumps({"id": "WONKA-00012", "dispatch": 1, "started": "2026-10-19T14:03:00"}))
            held.flush()
            fcntl.flock(held, fcntl.LOCK_EX)
            start = time.monotonic()
            code, err = self.wtest(tid, "--", "python3", "-c", "open('ran.txt','w')")
            waited = time.monotonic() - start
        self.assertEqual(code, 75)
        self.assertIn("tests busy: WONKA-00012 has held the lock since 14:03, run it again", err)
        self.assertGreaterEqual(waited, 0.5)
        self.assertFalse((wt / "ran.txt").exists())

    def test_holder_is_recorded_and_lock_released(self):
        tid = self.add()
        wt = self.to_baking(tid)
        lp = testrun.lock_path("app")
        code, _ = self.wtest(tid, "--dispatch", "4", "--", "python3", "-c",
                             "import sys; open('holder.json','w').write(open(sys.argv[1]).read())", str(lp))
        self.assertEqual(code, 0)
        holder = json.loads((wt / "holder.json").read_text())
        self.assertEqual((holder["id"], holder["dispatch"]), (tid, 4))
        with open(lp) as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_drafting_ticket_runs_in_the_scout(self):
        wt, scout = self.tmp / "wt", self.tmp / "scout"
        wt.mkdir()
        scout.mkdir()
        t = {"project": "app", "repo": str(self.repo), "worktree": str(wt), "scout": str(scout)}
        stub = mock.Mock()
        stub.return_value.ticket.return_value = t
        stub.return_value.state.return_value = ("drafting", "progress")
        with mock.patch.object(testrun, "Factory", stub), \
                mock.patch.object(testrun.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(self.wtest("T", "--", "./t.sh")[0], 0)
        self.assertEqual(run.call_args[1]["cwd"], scout)

    def test_refuses_to_run_in_the_main_checkout(self):
        tid = self.add()
        with mock.patch.object(testrun.subprocess, "run") as run:
            code, err = self.wtest(tid, "--", "./t.sh")
        self.assertEqual(code, 1)
        self.assertIn("no worktree or scout", err)
        run.assert_not_called()

    def test_runs_while_a_tick_holds_the_state_lock(self):
        tid = self.add()
        self.to_baking(tid)
        with open(self.tmp / "home" / ".lock", "w") as held, mock.patch.object(store, "LOCK_TIMEOUT", 0.3):
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.wtest(tid, "--", "python3", "-c", "import sys; sys.exit(5)")[0], 5)

    def test_signal_exit_maps_to_128_plus_n(self):
        tid = self.add()
        self.to_baking(tid)
        with mock.patch.object(testrun.subprocess, "run", return_value=mock.Mock(returncode=-9)):
            self.assertEqual(self.wtest(tid, "--", "./t.sh")[0], 137)
