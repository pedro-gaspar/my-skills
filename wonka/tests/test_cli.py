import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import cli, store


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for patcher in (mock.patch.dict(os.environ, {"WONKA_HOME": str(self.tmp / "home")}),
                        mock.patch.object(cli, "LINK", self.tmp / "bin" / "wonka")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_init_writes_settings_board_and_link(self):
        code, out, _ = self.run_cli("init", "--vault", str(self.tmp / "vault"), "--drafting", "2", "--baking", "3",
                                    "--archive-days", "14")
        self.assertEqual(code, 0)
        cfg = store.load_config()
        self.assertEqual((cfg["wip"], cfg["archive_after_days"]), ({"drafting": 2, "baking": 3}, 14))
        base = (self.tmp / "vault/wonka/Wonka.base").read_text()
        self.assertIn('file.inFolder("wonka/golden-tickets")', base)
        self.assertTrue((self.tmp / "vault/wonka/golden-tickets").is_dir())
        self.assertTrue(cli.LINK.is_symlink())
        self.assertIn("🍫 baking at once   : 3", out)

    def test_config_changes_and_validates(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        self.assertEqual(self.run_cli("config", "wip.baking", "4")[0], 0)
        self.assertEqual(json.loads((self.tmp / "home/config.json").read_text())["wip"]["baking"], 4)
        code, _, err = self.run_cli("config", "wip.baking", "0")
        self.assertEqual(code, 1)
        self.assertIn("too small", err)
        self.assertEqual(self.run_cli("config", "colour", "blue")[0], 1)

    def test_question_flags_mark_the_recommendation_with_a_star(self):
        q = cli.parse_question("Store sessions where? | Redis | *Postgres, already in the stack")
        self.assertEqual(q, {"q": "Store sessions where?", "options": ["Redis", "Postgres, already in the stack"],
                             "recommended": 1})
        with self.assertRaises(store.WonkaError):
            cli.parse_question("Only one option? | Yes")

    def test_report_flags_build_the_same_report_as_json(self):
        a = cli.parser().parse_args([
            "report", "WONKA-00001", "--squirrel", "architect", "--verdict", "bad",
            "--issue", "calc.py:4: no zero test → add one",
            "--followup", "Cache lookups :: memoise the user lookup",
            "--question", "Keep the old API? | Yes | *No, nothing uses it",
            "--suggest", "draft", "--why", "the spec missed the API"])
        self.assertEqual(cli._report(a), {
            "verdict": "bad nut", "issues": ["calc.py:4: no zero test → add one"],
            "questions": [{"q": "Keep the old API?", "options": ["Yes", "No, nothing uses it"], "recommended": 1}],
            "suggest": "draft", "why": "the spec missed the API",
            "followups": [{"title": "Cache lookups", "idea": "memoise the user lookup"}]})

    def test_agent_commands_never_need_json(self):
        """Claude Code blocks '{"' in background agents' Bash commands, so agent docs must not use it."""
        root = Path(cli.__file__).resolve().parent.parent
        for doc in sorted((root / "agents").glob("*.md")) + [root / "skills/wonka/SKILL.md"]:
            self.assertNotIn('{"', doc.read_text(), doc.name)

    def test_errors_are_friendly_and_logged(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        code, _, err = self.run_cli("show", "WONKA-00042")
        self.assertEqual((code, err.strip()), (1, "🫐 wonka: No ticket WONKA-00042"))
        self.assertIn("No ticket WONKA-00042", (self.tmp / "home/factory.log").read_text())


if __name__ == "__main__":
    unittest.main()
