"""wonka ci-logs: URL checks against the project's CI hosts and the run itself, against a real repo."""

import contextlib
import io
import unittest
from unittest import mock

from wonka import cilogs, cli, store
from wonka.store import WonkaError

from .fixtures import RepoFixture

HOSTS = ["ci.example.com"]


class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cilogs._parse(["T", "https://h/x"]), ("T", None, "https://h/x"))
        self.assertEqual(cilogs._parse(["T", "--dispatch", "2", "https://h/x"]), ("T", 2, "https://h/x"))
        for argv in (["T"], ["T", "a", "b"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(WonkaError, "usage"):
                cilogs._parse(argv)


class CheckUrlTest(unittest.TestCase):
    def test_allowed(self):
        for url, hosts in [("https://ci.example.com/teams/x/builds/1", HOSTS),
                           ("HTTPS://CI.Example.com./x", HOSTS),
                           ("https://ci.example.com:8443/x", ["ci.example.com:8443"])]:
            with self.subTest(url=url):
                cilogs.check_url(url, hosts, "p")

    def test_refused(self):
        for url in ["http://ci.example.com/x", "https://evil.com/x", "https://ci.example.com.evil.com/x",
                    "https://evil.ci.example.com/x", "https://user@ci.example.com/x",
                    "https://ci.example.com@evil.com/x", "https://ci.example.com:8443/x", "-https://ci.example.com",
                    "https://ci.example.com/x y", "https://ci.example.com/x\n", "https://ci.example.com\\@evil.com/",
                    "file:///etc/passwd", "https://[::1/"]:
            with self.subTest(url=url), self.assertRaisesRegex(WonkaError, "not on a CI host"):
                cilogs.check_url(url, HOSTS, "p")

    def test_no_hosts(self):
        with self.assertRaisesRegex(WonkaError, "wonka config ci_hosts.p"):
            cilogs.check_url("https://ci.example.com/x", [], "p")


class RunTest(RepoFixture, unittest.TestCase):
    URL = "https://ci.example.com/teams/x/builds/1"

    def setUp(self):
        super().setUp()
        cmd = "python3 -c \"import os,sys; open('args.txt','w').write(os.getcwd()+' '+sys.argv[1]); sys.exit(3)\""
        self.cfg = {**self.cfg, "ci_hosts": {"app": HOSTS}, "ci_log_command": {"app": cmd}}
        store.save_config(self.cfg)

    def run_logs(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["ci-logs", *argv])
        return code, err.getvalue()

    def test_allowed_url_runs_in_the_worktree_and_passes_the_exit_code(self):
        tid = self.add()
        wt = self.to_baking(tid)
        code, _ = self.run_logs(tid, "--dispatch", "1", self.URL)
        self.assertEqual(code, 3)
        self.assertTrue((wt / "args.txt").read_text().endswith(" " + self.URL))
        self.assertFalse((self.repo / "args.txt").exists())

    def test_disallowed_url_never_runs(self):
        tid = self.add()
        self.to_baking(tid)
        with mock.patch("wonka.cilogs.subprocess.run") as run:
            code, err = self.run_logs(tid, "https://evil.com/x")
        self.assertEqual(code, 1)
        self.assertIn("not on a CI host", err)
        run.assert_not_called()

    def test_missing_command_is_refused(self):
        tid = self.add()
        self.to_baking(tid)
        store.save_config({**self.cfg, "ci_log_command": {}})
        code, err = self.run_logs(tid, self.URL)
        self.assertEqual(code, 1)
        self.assertIn("wonka config ci_log_command.", err)

    def test_no_worktree_is_refused(self):
        tid = self.add()
        with mock.patch("wonka.cilogs.subprocess.run") as run:
            code, _ = self.run_logs(tid, self.URL)
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_command_not_found(self):
        tid = self.add()
        self.to_baking(tid)
        store.save_config({**self.cfg, "ci_log_command": {"app": "no-such-log-tool-xyz"}})
        code, err = self.run_logs(tid, self.URL)
        self.assertEqual(code, 1)
        self.assertIn("command not found", err)
