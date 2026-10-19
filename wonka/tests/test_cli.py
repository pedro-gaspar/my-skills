import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import cli, factory, store


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
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
        self.assertEqual((cfg["wip"], cfg["archive_after_days"]), ({"triage": 2, "drafting": 2, "baking": 3}, 14))
        self.assertEqual(cfg["max_review_rounds"], 4)
        self.assertIn("🐿️ review rounds    : 4", out)
        base = (self.tmp / "vault/wonka/🍫 Factory.base").read_text()
        self.assertFalse((self.tmp / "vault/wonka/Wonka.base").exists())
        self.assertIn('file.inFolder("wonka/golden-tickets")', base)
        self.assertIn("name: 🍫 Factory floor", base)
        self.assertIn("🍫 Factory ", out)
        self.assertTrue((self.tmp / "vault/wonka/golden-tickets").is_dir())
        self.assertTrue(cli.LINK.is_symlink())
        self.assertIn("🍫 baking at once   : 3", out)

    def test_init_renames_old_wonka_base(self):
        board = self.tmp / "vault/wonka"
        board.mkdir(parents=True)
        (board / "Wonka.base").write_text("views: [] # mine\n")
        self.assertEqual(self.run_cli("init", "--vault", str(self.tmp / "vault"))[0], 0)
        self.assertEqual((board / "🍫 Factory.base").read_text(), "views: [] # mine\n")
        self.assertFalse((board / "Wonka.base").exists())

    def test_init_renames_the_old_ready_column(self):
        board = self.tmp / "vault/wonka"
        board.mkdir(parents=True)
        (board / "🍫 Factory.base").write_text("order:\n  - ✉️-Inbox\n  - 🎫-Ready\nmine: 1\n")
        self.assertEqual(self.run_cli("init", "--vault", str(self.tmp / "vault"))[0], 0)
        self.assertEqual((board / "🍫 Factory.base").read_text(), "order:\n  - 📏-Triage\n  - 🎫-Planned\nmine: 1\n")

    def test_init_keeps_both_when_both_exist(self):
        board = self.tmp / "vault/wonka"
        board.mkdir(parents=True)
        (board / "Wonka.base").write_text("old\n")
        (board / "🍫 Factory.base").write_text("new\n")
        self.assertEqual(self.run_cli("init", "--vault", str(self.tmp / "vault"))[0], 0)
        self.assertEqual((board / "Wonka.base").read_text(), "old\n")
        self.assertEqual((board / "🍫 Factory.base").read_text(), "new\n")

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

    def test_config_worktree_files_round_trip(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        cfg_file = self.tmp / "home/config.json"
        code, out, _ = self.run_cli("config", "worktree_files.beyondpricing", "server/.env, web/.env")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(cfg_file.read_text())["worktree_files"], {"beyondpricing": ["server/.env", "web/.env"]})
        self.assertIn("beyondpricing: server/.env, web/.env", out)
        self.assertEqual(self.run_cli("config", "worktree_files.beyondpricing", "")[0], 0)
        self.assertEqual(json.loads(cfg_file.read_text())["worktree_files"], {})
        self.assertEqual(self.run_cli("config", "worktree_files.beyondpricing", "/etc/passwd")[0], 1)
        self.assertEqual(self.run_cli("config", "worktree_files.beyondpricing", "../x")[0], 1)

    def test_config_warns_on_open_test_prefix(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        cfg_file = self.tmp / "home/config.json"
        code, _, err = self.run_cli("config", "test_commands.app", "python3 -c, ./test.sh")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(cfg_file.read_text())["test_commands"], {"app": ["python3 -c", "./test.sh"]})
        self.assertIn("test_commands.app: 'python3 -c'", err)
        self.assertNotIn("'./test.sh'", err)
        code, _, err = self.run_cli("config", "test_commands.app", "python3 -m pytest")
        self.assertEqual(code, 0)
        self.assertNotIn("test_commands", err)

    def test_config_test_commands_round_trip(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        cfg_file = self.tmp / "home/config.json"
        code, out, err = self.run_cli("config", "test_commands.beyondpricing", "./test.sh, make test")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(cfg_file.read_text())["test_commands"], {"beyondpricing": ["./test.sh", "make test"]})
        self.assertIn("beyondpricing: ./test.sh, make test", out)
        self.assertIn("test lock wait", out)
        self.assertEqual(self.run_cli("config", "test_commands.beyondpricing", "")[0], 0)
        self.assertEqual(json.loads(cfg_file.read_text())["test_commands"], {})
        self.assertEqual(self.run_cli("config", "test_commands.beyondpricing", '"oops')[0], 1)
        self.assertEqual(self.run_cli("config", "test_lock_wait_minutes", "0.5")[0], 0)
        self.assertEqual(json.loads(cfg_file.read_text())["test_lock_wait_minutes"], 0.5)
        self.assertEqual(self.run_cli("config", "test_lock_wait_minutes", "-1")[0], 1)

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
            "--suggest", "draft", "--why", "the spec missed the API", "--dispatch", "1"])
        self.assertEqual(cli._report(a), {
            "verdict": "bad nut", "issues": ["calc.py:4: no zero test → add one"],
            "questions": [{"q": "Keep the old API?", "options": ["Yes", "No, nothing uses it"], "recommended": 1}],
            "suggest": "draft", "why": "the spec missed the API",
            "followups": [{"title": "Cache lookups", "idea": "memoise the user lookup"}]})

    def test_finished_needs_an_agent(self):
        a = cli.parser().parse_args(["finished", "WONKA-00001", "--agent", "wonka:squirrel-backend", "--dispatch", "3"])
        self.assertEqual((a.cmd, a.id, a.agent, a.dispatch),
                         ("finished", "WONKA-00001", "wonka:squirrel-backend", 3))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(["finished", "WONKA-00001", "--dispatch", "3"])

    def test_report_ignores_the_dispatch(self):
        args = ["report", "WONKA-00001", "--squirrel", "architect", "--verdict", "good", "--dispatch", "3"]
        a = cli.parser().parse_args(args)
        self.assertEqual(a.dispatch, 3)
        self.assertNotIn("dispatch", cli._report(a))

    def test_agent_commands_require_a_dispatch_number(self):
        """Without it the factory cannot tell a late agent from the live one, so argparse refuses the call."""
        for argv in (["ask", "WONKA-00001", "--question", "Q? | a | *b"], ["plan", "WONKA-00001"],
                     ["section", "WONKA-00001", "spec"], ["resolved", "WONKA-00001"],
                     ["drafted", "WONKA-00001", "--size", "s"], ["baked", "WONKA-00001"], ["pr-body", "WONKA-00001"],
                     ["triage", "WONKA-00001", "--size", "s"],
                     ["report", "WONKA-00001", "--squirrel", "architect", "--verdict", "good"],
                     ["finished", "WONKA-00001", "--agent", "wonka:squirrel-backend"]):
            self.assertEqual(cli.parser().parse_args(argv + ["--dispatch", "3"]).dispatch, 3, argv)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=argv):
                cli.parser().parse_args(argv)

    def test_agent_docs_pass_the_dispatch(self):
        root = Path(cli.__file__).resolve().parent.parent
        for name, verbs in (("oompa-loompa-builder", ("pr-body", "baked")), ("oompa-loompa-analyst", ("ask", "plan", "section", "drafted")),
                            ("oompa-loompa-fixer", ("resolved",)), ("oompa-loompa-triage", ("triage",))):
            lines = (root / "agents" / f"{name}.md").read_text().splitlines()
            for verb in verbs:
                cmds = [ln for ln in lines if f"wonka {verb} <id>" in ln]
                self.assertTrue(cmds, (name, verb))
                for ln in cmds:
                    self.assertIn("--dispatch <dispatch>", ln, (name, verb))

    def test_drafted_takes_split(self):
        base = ["drafted", "WONKA-00001", "--size", "m", "--dispatch", "1"]
        self.assertTrue(cli.parser().parse_args(base + ["--split"]).split)
        self.assertFalse(cli.parser().parse_args(base).split)

    def test_repos_is_read_only_and_prints_known_repos(self):
        self.assertIn("repos", cli.READ_ONLY)
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        repo = self.tmp / "proj"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"],
                       cwd=repo, check=True)
        self.run_cli("add", "--repo", str(repo), "x")
        code, out, _ = self.run_cli("repos")
        self.assertEqual(code, 0)
        self.assertIn(str(repo.resolve()), out)
        self.assertTrue(out.rstrip().endswith(" -"))

    def test_analyst_doc_splits_by_repo(self):
        root = Path(cli.__file__).resolve().parent.parent
        text = (root / "agents" / "oompa-loompa-analyst.md").read_text()
        for needle in ("wonka repos", "--split", "One repo per ticket"):
            self.assertIn(needle, text)

    def test_resolved_takes_an_id(self):
        a = cli.parser().parse_args(["resolved", "WONKA-00001", "--dispatch", "1"])
        self.assertEqual((a.cmd, a.id), ("resolved", "WONKA-00001"))

    def test_triage_takes_a_duplicate_of(self):
        args = ["triage", "WONKA-00002", "--size", "s", "--dispatch", "1"]
        self.assertEqual(cli.parser().parse_args(args + ["--duplicate-of", "WONKA-00001"]).duplicate_of, "WONKA-00001")
        self.assertIsNone(cli.parser().parse_args(args).duplicate_of)

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

    def test_agent_docs_run_project_tests_through_wonka_test(self):
        agents = Path(cli.__file__).resolve().parent.parent / "agents"
        names = ["oompa-loompa-analyst", "oompa-loompa-builder", "oompa-loompa-fixer"]
        names += [p.stem for p in agents.glob("squirrel-*.md")]
        self.assertGreaterEqual(len(names), 9)
        for name in names:
            text = (agents / f"{name}.md").read_text()
            for needle in ("test_commands", "wonka test <id> --dispatch <dispatch> -- <command>", "600000", "75"):
                self.assertIn(needle, text, name)
        self.assertNotIn("wonka test", (agents / "oompa-loompa-triage.md").read_text())

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

    def test_readme_documents_the_sandbox_boundary(self):
        readme = (Path(cli.__file__).resolve().parent.parent / "README.md").read_text()
        self.assertLess(readme.index("## Sandbox boundary"), readme.index("\n## Use"))
        section = readme.split("## Sandbox boundary", 1)[1].split("\n## ", 1)[0]
        for text in ["not a sandbox", "--repo", "/sandbox", '"wonka *"', "allowUnsandboxedCommands", "allowWrite",
                     "denyRead"]:
            self.assertIn(text, section)

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

    def test_critical_errors_ping_the_desktop(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        (self.tmp / "home" / store.STATE_FILE).write_text("{")
        with mock.patch.object(cli, "alert") as alert:
            code, _, err = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        alert.assert_called_once()
        self.assertIn("not valid JSON", alert.call_args[0][0])

    def test_missing_vault_pings_the_desktop(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        shutil.rmtree(self.tmp / "vault")
        with mock.patch.object(cli, "alert") as alert:
            code, _, err = self.run_cli("tick")
        self.assertEqual(code, 1)
        self.assertIn("is missing", alert.call_args[0][0])

    def test_ordinary_errors_do_not_ping(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        with mock.patch.object(cli, "alert") as alert:
            self.assertEqual(self.run_cli("show", "WONKA-00042")[0], 1)
        alert.assert_not_called()

    def test_errors_are_friendly_and_logged(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        code, _, err = self.run_cli("show", "WONKA-00042")
        self.assertEqual((code, err.strip()), (1, "🗑️ wonka: No ticket WONKA-00042"))
        self.assertIn("No ticket WONKA-00042", (self.tmp / "home/factory.log").read_text())


    def test_needs_human_spec_review_flag(self):
        self.assertTrue(cli.parser().parse_args(["add", "x", "--needs-human-spec-review"]).needs_human_spec_review)
        self.assertFalse(cli.parser().parse_args(["add", "x"]).needs_human_spec_review)
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        repo = self.tmp / "app"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=repo, env=env, check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"WONKA_NO_NOTIFY": "1"}):
            self.assertEqual(self.run_cli("add", "x", "--repo", str(repo), "--needs-human-spec-review")[0], 0)
        notes = list((self.tmp / "vault").rglob("WONKA-00001.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("spec_review: human\n", notes[0].read_text())


if __name__ == "__main__":
    unittest.main()
