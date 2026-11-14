import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from wonka import cli


class HelpDocTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"WONKA_HOME": tmp + "/home"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_full_reference_documents_every_command(self):
        sub = next(a for a in cli.parser()._actions if a.dest == "cmd")
        summaries = {a.dest: a.help for a in sub._choices_actions}
        code, out, _ = self.run_cli("help")
        self.assertEqual(code, 0)
        for name in sub.choices:
            with self.subTest(name=name):
                self.assertIn(f"## wonka {name}", out)
                self.assertIn(summaries[name].split()[0], out)
                self.assertIn(f"usage: wonka {name}", out)
        self.assertIn("--vault", out)
        self.assertIn("--dispatch", out)

    def test_one_command(self):
        code, out, _ = self.run_cli("help", "init")
        self.assertEqual(code, 0)
        self.assertIn("--vault", out)
        self.assertNotIn("## wonka tick", out)

    def test_unknown_command_fails_with_the_list(self):
        code, _, err = self.run_cli("help", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown command 'nope'", err)
        self.assertIn("init", err)

    def test_works_with_a_refused_wonka_home(self):
        with mock.patch.dict(os.environ, {"WONKA_HOME": "rel/home"}):
            code, _, _ = self.run_cli("help")
        self.assertEqual(code, 0)

    def test_dash_h_stays_argparse_short_list(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-h"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertNotIn("## wonka init", out.getvalue())
