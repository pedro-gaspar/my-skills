import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import cli, factory, store


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for patcher in (mock.patch.dict(os.environ, {"WONKA_HOME": str(self.tmp / "home"), "WONKA_MDLINT": "off"}),
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
                                    "--archive-days", "14", "--max-review-rounds", "4")
        self.assertEqual(code, 0)
        cfg = store.load_config()
        self.assertEqual((cfg["wip"], cfg["archive_after_days"]), ({"drafting": 2, "baking": 3}, 14))
        self.assertEqual(cfg["max_review_rounds"], 4)
        self.assertIn("🐿️ review rounds    : 4", out)
        base = (self.tmp / "vault/wonka/Wonka.base").read_text()
        self.assertIn('file.inFolder("wonka/golden-tickets")', base)
        self.assertIn("name: 🍫 Factory floor", base)
        self.assertIn("🍫 Factory ", out)
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
        self.assertEqual(self.run_cli("config", "max_review_rounds", "5")[0], 0)
        self.assertEqual(json.loads((self.tmp / "home/config.json").read_text())["max_review_rounds"], 5)
        code, _, err = self.run_cli("config", "max_review_rounds", "0")
        self.assertEqual(code, 1)
        self.assertIn("too small", err)
        self.assertEqual(self.run_cli("config", "max_review_rounds", "x")[0], 1)

    def test_set_prints_the_summary(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        repo = self.tmp / "app"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=repo, env=env, check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"WONKA_NO_NOTIFY": "1"}):
            self.assertEqual(self.run_cli("add", "x", "--repo", str(repo))[0], 0)
            code, out, _ = self.run_cli("set", "WONKA-00001", "title", "New title")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("WONKA-00001: "), out)

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

    def test_finished_needs_an_agent(self):
        a = cli.parser().parse_args(["finished", "WONKA-00001", "--agent", "wonka:squirrel-backend"])
        self.assertEqual((a.cmd, a.id, a.agent),
                         ("finished", "WONKA-00001", "wonka:squirrel-backend"))
        self.assertIsNone(a.dispatch)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(["finished", "WONKA-00001"])

    def test_finished_and_report_take_a_dispatch_number(self):
        a = cli.parser().parse_args(["finished", "WONKA-00001", "--agent", "wonka:squirrel-backend", "--dispatch", "3"])
        self.assertEqual(a.dispatch, 3)
        args = ["report", "WONKA-00001", "--squirrel", "architect", "--verdict", "good"]
        plain, numbered = cli.parser().parse_args(args), cli.parser().parse_args(args + ["--dispatch", "3"])
        self.assertIsNone(plain.dispatch)
        self.assertEqual(numbered.dispatch, 3)
        self.assertEqual(cli._report(numbered), cli._report(plain))
        self.assertNotIn("dispatch", cli._report(numbered))

    def test_drafted_and_baked_take_a_dispatch_number(self):
        drafted = ["drafted", "WONKA-00001", "--size", "s"]
        self.assertEqual(cli.parser().parse_args(drafted + ["--dispatch", "3"]).dispatch, 3)
        self.assertIsNone(cli.parser().parse_args(drafted).dispatch)
        self.assertEqual(cli.parser().parse_args(["baked", "WONKA-00001", "--dispatch", "3"]).dispatch, 3)
        self.assertIsNone(cli.parser().parse_args(["baked", "WONKA-00001"]).dispatch)

    def test_resolved_takes_an_id(self):
        a = cli.parser().parse_args(["resolved", "WONKA-00001"])
        self.assertEqual((a.cmd, a.id), ("resolved", "WONKA-00001"))

    def test_agent_commands_never_need_json(self):
        """Claude Code blocks '{"' in background agents' Bash commands, so agent docs must not use it."""
        root = Path(cli.__file__).resolve().parent.parent
        for doc in sorted((root / "agents").glob("*.md")) + [root / "skills/wonka/SKILL.md"]:
            self.assertNotIn('{"', doc.read_text(), doc.name)

    def test_suite_is_discoverable_by_path(self):
        """Squirrels run tests path-only (no cd), so discovery from the package dir must find them."""
        root = Path(cli.__file__).resolve().parent.parent
        suite = unittest.TestLoader().discover(start_dir=str(root), top_level_dir=str(root), pattern="test_banner.py")
        self.assertGreater(suite.countTestCases(), 0)

    def test_agent_docs_point_to_the_plans_test_command(self):
        """Agents use the plan's test command and treat a run of 0 tests as a wrong command, not a pass."""
        root = Path(cli.__file__).resolve().parent.parent
        agents = root / "agents"
        runners = [agents / "oompa-loompa-builder.md", agents / "oompa-loompa-fixer.md"]
        runners += sorted(agents.glob("squirrel-*.md"))
        self.assertGreater(len(runners), 2)
        for path in runners:
            text = path.read_text()
            self.assertIn("0 tests", text, path.name)
            self.assertIn("how to verify", text, path.name)
        analyst = (agents / "oompa-loompa-analyst.md").read_text()
        self.assertIn("0 tests", analyst)
        self.assertIn("discover -s wonka -t wonka", analyst)
        readme = (root / "README.md").read_text()
        develop = readme.split("## Develop", 1)[1].split("\n## ", 1)[0]
        self.assertIn("discover -s wonka -t wonka", develop)
        self.assertIn("NO TESTS RAN", develop)

    def test_squirrel_docs_differ_only_in_name_emoji_focus_and_model(self):
        """The squirrel agents share one body; only who they are, what they look at and their model may differ."""
        agents = Path(cli.__file__).resolve().parent.parent / "agents"
        docs = {p.stem.removeprefix("squirrel-"): p.read_text() for p in sorted(agents.glob("squirrel-*.md"))}
        self.assertEqual(sorted(docs), sorted(factory.REVIEWERS))
        bodies = {}
        for name, text in docs.items():
            self.assertIn(f"--squirrel {name} ", text, name)  # the normalisation below is not vacuous
            self.assertNotIn("cd <worktree>", text, name)  # tests take the worktree as a path
            self.assertNotIn("discover -s <worktree>`", text, name)  # bare repo root finds no nested tests
            # Focus first: the architect focus says "architecture", which the name replacement would touch.
            text, focus = re.subn(r"^\*\*Your focus:\*\* .+$", "**Your focus:** FOCUS", text, flags=re.M)
            text, emoji = re.subn(r"🐿️ \S+, the \*\*", "🐿️ EMOJI, the **", text)
            text, model = re.subn(r"^model: (opus|sonnet|haiku)$", "model: MODEL", text, flags=re.M)
            self.assertEqual((focus, emoji, model), (1, 1, 1), name)
            bodies[name] = re.sub(re.escape(name), "NAME", text, flags=re.I)
        self.maxDiff = None  # show the whole drift, not a truncated diff
        first = next(iter(bodies))
        for name, body in bodies.items():
            self.assertEqual(body, bodies[first], f"squirrel-{name}.md drifted from squirrel-{first}.md")

    def test_errors_are_friendly_and_logged(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        code, _, err = self.run_cli("show", "WONKA-00042")
        self.assertEqual((code, err.strip()), (1, "🫐 wonka: No ticket WONKA-00042"))
        self.assertIn("No ticket WONKA-00042", (self.tmp / "home/factory.log").read_text())


if __name__ == "__main__":
    unittest.main()
