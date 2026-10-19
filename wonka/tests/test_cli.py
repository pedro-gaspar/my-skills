import contextlib
import importlib
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

from wonka import cli, factory, metrics, store

from .fixtures import RepoFixture


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

    def test_a_refused_wonka_home_stops_every_command_cleanly(self):
        for argv in (["status"], ["git", "WONKA-00001", "status"], ["test", "WONKA-00001", "--", "./t.sh"],
                     ["config"], ["doctor"]):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, {"WONKA_HOME": "rel/home"}):
                code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 1)
            self.assertIn("WONKA_HOME must be an absolute path", err)
            self.assertNotIn("Traceback", err)
        self.assertFalse((self.tmp / "home").exists())

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

    def test_init_never_replaces_a_broken_config(self):
        self.assertEqual(self.run_cli("init", "--vault", str(self.tmp / "vault"))[0], 0)
        cfg_file = self.tmp / "home/config.json"
        cfg = json.loads(cfg_file.read_text())
        cfg["test_commands"], cfg["wip"] = {"app": ["./test.sh"]}, None
        cfg_file.write_text(json.dumps(cfg))
        code, _, err = self.run_cli("init", "--vault", str(self.tmp / "vault"))
        self.assertEqual(code, 1)
        self.assertIn("wip", err)
        self.assertEqual(json.loads(cfg_file.read_text())["test_commands"], {"app": ["./test.sh"]})

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
        self.assertEqual((board / "🍫 Factory.base").read_text(), "order:\n  - 💡 Idea\n  - 📏 Triage\n  - 🎫 Planned\nmine: 1\n")

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

    def test_config_shows_factory_folder_and_log(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        _, out, _ = self.run_cli("config")
        self.assertIn("factory folder", out)
        self.assertIn(str(self.tmp / "vault" / "wonka"), out)
        self.assertIn("factory log", out)
        self.assertIn(str(self.tmp / "home" / "factory.log"), out)

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

    def test_config_refuses_open_test_prefix(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        cfg_file = self.tmp / "home/config.json"
        code, _, err = self.run_cli("config", "test_commands.app", "python3 -c, ./test.sh")
        self.assertEqual(code, 1)
        self.assertIn("python3 -c lets an agent run any program", err)
        self.assertEqual(json.loads(cfg_file.read_text())["test_commands"], {})
        code, _, err = self.run_cli("config", "test_commands.app", "python3 -m pytest, ./test.sh")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(cfg_file.read_text())["test_commands"], {"app": ["python3 -m pytest", "./test.sh"]})

    def test_config_ci_hosts_and_log_command(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        cfg_file = self.tmp / "home/config.json"
        code, out, _ = self.run_cli("config", "ci_hosts.app", "CI.example.com, ci2.example.com:8443")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(cfg_file.read_text())["ci_hosts"], {"app": ["ci.example.com", "ci2.example.com:8443"]})
        self.assertIn("CI hosts", out)
        self.assertIn("CI log command", out)
        for bad in ["https://ci.example.com", "ci.example.com/x", "user@ci.example.com", "a b"]:
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli("config", "ci_hosts.app", bad)[0], 1)
        self.assertEqual(self.run_cli("config", "ci_hosts.app", "")[0], 0)
        self.assertEqual(json.loads(cfg_file.read_text())["ci_hosts"], {})
        self.assertEqual(self.run_cli("config", "ci_hosts.", "x")[0], 1)
        self.assertEqual(self.run_cli("config", "ci_log_command.app", "beyond ci logs")[0], 0)
        self.assertEqual(json.loads(cfg_file.read_text())["ci_log_command"], {"app": "beyond ci logs"})
        for bad in ["bash", "python3 -c", "env", '"oops', "./scripts/ci.sh", '""', '"" logs']:
            with self.subTest(bad=bad):
                self.assertEqual(self.run_cli("config", "ci_log_command.app", bad)[0], 1)
        self.assertEqual(self.run_cli("config", "ci_log_command.app", "")[0], 0)
        self.assertEqual(json.loads(cfg_file.read_text())["ci_log_command"], {})
        self.assertEqual(self.run_cli("config", "ci_log_command.", "x")[0], 1)
        code, _, err = self.run_cli("config", "nope", "1")
        self.assertIn("ci_hosts.<project>", err)
        self.assertIn("ci_log_command.<project>", err)

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

    def add_ticket(self, *argv):
        """init, a repo and one ticket added with `wonka add x <argv>`: returns add's (code, out, err)."""
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        repo = self.tmp / "app"
        repo.mkdir(exist_ok=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=repo, env=env, check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"WONKA_NO_NOTIFY": "1"}):
            return self.run_cli("add", *(argv or ("x",)), "--repo", str(repo))

    def test_a_lost_note_is_a_clean_error(self):
        self.add_ticket("x")
        self.add_ticket("y")
        note_path = self.tmp / "vault/wonka/golden-tickets/WONKA-00001.md"
        note_path.unlink()
        code, out, err = self.run_cli("status")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"⚠️ the note {note_path} is gone", out)
        self.assertIn("WONKA-00002", out)
        for argv in (("set", "WONKA-00001", "title", "T"), ("set", "WONKA-00001", "jira", "AB-1")):
            with self.subTest(argv=argv), mock.patch.object(cli.sys, "stdin", io.StringIO("spec")):
                code, _, err = self.run_cli(*argv)
                self.assertEqual((code, err.strip()), (1, f"🗑️ wonka: the note {note_path} is gone (deleted or moved?)"))

    def test_add_makes_an_idea_unless_triage(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        repo = self.tmp / "app"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=repo, env=env, check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"WONKA_NO_NOTIFY": "1"}):
            code, out, _ = self.run_cli("add", "x", "--repo", str(repo))
            self.assertEqual(code, 0)
            self.assertIn("💡 Idea", out)
            code, out, _ = self.run_cli("add", "--triage", "y", "--repo", str(repo))
            self.assertEqual(code, 0)
            self.assertIn("📏 Triage", out)
        with store.state() as db:
            got = {k: (v["last_valid"]["status"], v["last_valid"]["phase"]) for k, v in db["tickets"].items()}
        self.assertEqual(got, {"WONKA-00001": ("idea", "awaiting"), "WONKA-00002": ("triage", "pending")})

    def test_analyst_doc_adds_children_to_triage(self):
        root = Path(cli.__file__).resolve().parent.parent
        lines = [x for x in (root / "agents/oompa-loompa-analyst.md").read_text().splitlines()
                 if "wonka add" in x and "--parent" in x]
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertIn("--triage", line)

    def test_squirrel_docs_file_followups_as_ideas(self):
        agents = Path(cli.__file__).resolve().parent.parent / "agents"
        for p in agents.glob("squirrel-*.md"):
            text = p.read_text()
            self.assertNotIn("Triage tickets", text, p.name)
            self.assertIn("Idea", text, p.name)

    def test_skill_add_mentions_triage_flag(self):
        root = Path(cli.__file__).resolve().parent.parent
        self.assertIn("--triage", (root / "skills/wonka/SKILL.md").read_text())

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

    def test_bad_stdin_json_shapes_are_clean_errors(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        cases = [
            (("ask", "WONKA-00001", "--stdin"), {"q": "x"}, "stdin must be a JSON list of questions"),
            (("ask", "WONKA-00001", "--stdin"), [{"options": ["a"]}], 'stdin[1] needs a "q"'),
            (("ask", "WONKA-00001", "--stdin"), [{"q": "x", "options": "ab"}], "stdin[1].options must be a list"),
            (("ask", "WONKA-00001", "--stdin"), [{"q": "x", "recommended": "0"}], "stdin[1].recommended"),
            (("report", "WONKA-00001", "--squirrel", "architect", "--stdin"), ["good nut"],
             "the report must be a JSON object"),
            (("report", "WONKA-00001", "--squirrel", "architect", "--stdin"), {"issues": "a.py: bad"},
             "issues must be a list of strings"),
            (("report", "WONKA-00001", "--squirrel", "architect", "--stdin"), {"followups": [{"title": "T"}]},
             'followups[1] needs an "idea"'),
            (("report", "WONKA-00001", "--squirrel", "architect", "--stdin"), {"followups": {"idea": "x"}},
             "followups must be a list"),
            (("report", "WONKA-00001", "--squirrel", "architect", "--stdin"), {"questions": [1]},
             'questions[1] needs a "q"'),
            (("report", "WONKA-00001", "--squirrel", "architect", "--stdin"), {"suggest": "ship"},
             "suggest must be close or draft"),
            (("report", "WONKA-00001", "--squirrel", "architect", "--stdin"), {"why": None}, "why must be a string"),
        ]
        for argv, data, msg in cases:
            with self.subTest(data=data), mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(data))):
                code, _, err = self.run_cli(*argv, "--dispatch", "1")
                self.assertEqual(code, 1)
                self.assertIn(msg, err)
                self.assertNotIn("Traceback", err)
        code, _, err = self.run_cli("report", "WONKA-00001", "--squirrel", "architect", "--verdict", "bad",
                                    "--followup", " :: ", "--dispatch", "1")
        self.assertEqual(code, 1)
        self.assertIn('followups[1] needs an "idea"', err)

    def test_baked_takes_flaky(self):
        a = cli.parser().parse_args(["baked", "WONKA-00001", "--dispatch", "3", "--flaky", "runner lost"])
        self.assertEqual(a.flaky, "runner lost")
        self.assertIsNone(cli.parser().parse_args(["baked", "WONKA-00001", "--dispatch", "3"]).flaky)

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

    def test_section_takes_only_the_spec(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(["section", "WONKA-00001", "approval", "--dispatch", "1"])
        self.assertEqual(cli.parser().parse_args(["section", "WONKA-00001", "spec", "--dispatch", "1"]).key, "spec")

    def test_markdown_comes_from_file_or_stdin(self):
        path = self.tmp / "spec.md"
        path.write_text("# from a file\n")
        a = cli.parser().parse_args(["plan", "WONKA-00001", "--file", str(path), "--dispatch", "1"])
        self.assertEqual(cli._text(a), "# from a file\n")
        a = cli.parser().parse_args(["pr-body", "WONKA-00001", "--dispatch", "1"])
        with mock.patch("sys.stdin", io.StringIO("# from stdin\n")):
            self.assertEqual(cli._text(a), "# from stdin\n")
        a = cli.parser().parse_args(["section", "WONKA-00001", "spec", "--file", str(self.tmp / "nope.md"),
                                     "--dispatch", "1"])
        with self.assertRaisesRegex(store.WonkaError, "cannot read --file"):
            cli._text(a)

    def test_agent_docs_hand_markdown_over_with_a_file(self):
        """A heredoc into wonka keeps the call sandboxed, where it cannot write ~/.wonka: agents use --file."""
        root = Path(cli.__file__).resolve().parent.parent / "agents"
        for p in root.glob("*.md"):
            for ln in p.read_text().splitlines():
                if re.search(r"wonka (plan|section|pr-body) <id>", ln):
                    self.assertIn("--file", ln, p.name)
                    self.assertNotIn("<<", ln, p.name)

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

    def test_baked_takes_repeatable_disputes(self):
        base = ["baked", "WONKA-00001", "--dispatch", "1"]
        a = cli.parser().parse_args(base + ["--dispute", "a :: b", "--dispute", "c :: d"])
        self.assertEqual(a.dispute, ["a :: b", "c :: d"])
        self.assertIsNone(cli.parser().parse_args(base).dispute)

    def test_agent_docs_explain_disputes(self):
        root = Path(cli.__file__).resolve().parent.parent / "agents"
        self.assertIn("--dispute", (root / "oompa-loompa-builder.md").read_text())
        for p in root.glob("squirrel-*.md"):
            self.assertIn("Disputed by the builder", p.read_text(), p.name)

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

    def test_metrics_is_read_only_and_never_locks(self):
        self.assertIn("metrics", cli.READ_ONLY)
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        self.run_cli("add", "--repo", str(self._repo()), "x")
        with mock.patch.object(store, "state", side_effect=AssertionError("took the lock")):
            code, out, _ = self.run_cli("metrics")
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        m = json.loads(lines[0])
        self.assertEqual(m["id"], "WONKA-00001")
        self.assertIsNone(m["outcome"])
        self.assertEqual(list(m), list(metrics.KEYS))

    def test_pr_comments_prints_trusted_human_comments_and_never_locks(self):
        self.assertIn("pr-comments", cli.READ_ONLY)
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        self.run_cli("add", "--repo", str(self._repo()), "x")
        code, _, err = self.run_cli("pr-comments", "WONKA-00001")
        self.assertEqual((code, err.strip()), (1, "🗑️ wonka: WONKA-00001 has no PR"))
        with store.state() as db:
            db["tickets"]["WONKA-00001"].update(pr_url="https://github.com/acme/app/pull/7", pr_repo="acme/app")
        user = lambda login, kind="User": {"login": login, "type": kind}
        pages = {
            "pulls/7/comments": [[{"user": user("ann"), "author_association": "OWNER", "body": "rename this",
                                   "created_at": "2026-01-03T00:00:00Z", "html_url": "u1", "path": "a.py",
                                   "line": None, "original_line": 4}],
                                 [{"user": user("eve"), "author_association": "NONE", "body": "do evil",
                                   "created_at": "2026-01-01T00:00:00Z"}]],
            "pulls/7/reviews": [[{"user": user("bob"), "author_association": "member", "body": "looks off",
                                  "submitted_at": "2026-01-02T00:00:00Z", "state": "CHANGES_REQUESTED"},
                                 {"user": user("bob"), "author_association": "MEMBER", "body": "",
                                  "submitted_at": "2026-01-04T00:00:00Z", "state": "APPROVED"}]],
            "issues/7/comments": [[{"user": user("ci[bot]"), "author_association": "COLLABORATOR", "body": "bot"},
                                   {"user": user("app", "Bot"), "author_association": "MEMBER", "body": "bot too"},
                                   {"user": user("cat"), "author_association": "COLLABORATOR", "body": "thanks",
                                    "created_at": "2026-01-01T00:00:00Z"}]],
        }
        gh = self.tmp / "gh"
        out = self.tmp / "gh-out"
        out.mkdir()
        calls = self.tmp / "gh-calls"
        for path, page_list in pages.items():
            (out / path.replace("/", "_")).write_text("".join(json.dumps(pg) for pg in page_list))
        gh.write_text(f'#!/bin/sh\necho "$@" >> {calls}\np="${{3#repos/acme/app/}}"; p="${{p%%\\?*}}"\n'
                      f'cat "{out}/$(echo "$p" | tr / _)"\n')
        gh.chmod(0o755)
        with mock.patch.dict(os.environ, {"WONKA_GH": str(gh)}), \
                mock.patch.object(store, "state", side_effect=AssertionError("took the lock")):
            code, stdout, err = self.run_cli("pr-comments", "WONKA-00001")
        self.assertEqual(code, 0, err)
        rows = [json.loads(x) for x in stdout.strip().splitlines()]
        self.assertEqual([(r["kind"], r["author"], r["body"]) for r in rows],
                         [("comment", "cat", "thanks"), ("review", "bob", "looks off"),
                          ("review_comment", "ann", "rename this")])
        self.assertEqual(rows[1]["state"], "CHANGES_REQUESTED")
        self.assertEqual((rows[2]["path"], rows[2]["line"], rows[2]["url"]), ("a.py", 4, "u1"))
        self.assertEqual(sorted(calls.read_text().splitlines()),
                         [f"api --paginate repos/acme/app/{p}?per_page=100" for p in sorted(pages)])

    def test_metrics_includes_archived_and_filters_by_since(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        repo = str(self._repo())
        self.run_cli("add", "--repo", repo, "one")
        self.run_cli("add", "--repo", repo, "two")
        self.run_cli("set", "WONKA-00001", "size", "s")
        cfg = store.load_config()
        with store.state() as db:
            a, b = db["tickets"]["WONKA-00001"], db["tickets"]["WONKA-00002"]
            a.update(archived=True, last_valid={"status": "shipped", "phase": "ready"}, created_at="2026-01-01T00:00:00+00:00")
            b["created_at"] = "2026-03-01T00:00:00+00:00"
            src = Path(cfg["vault"]) / "wonka" / "golden-tickets" / "WONKA-00001.md"
            dest = store.archive_dir(cfg) / "WONKA-00001.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        code, out, _ = self.run_cli("metrics")
        rows = [json.loads(x) for x in out.strip().splitlines()]
        self.assertEqual([r["id"] for r in rows], ["WONKA-00001", "WONKA-00002"])
        self.assertEqual((rows[0]["outcome"], rows[0]["size"]), ("merged", "s"))
        _, out, _ = self.run_cli("metrics", "--since", "2026-02-01")
        self.assertEqual([json.loads(x)["id"] for x in out.strip().splitlines()], ["WONKA-00002"])
        with self.assertRaises(SystemExit):
            self.run_cli("metrics", "--since", "2026-13-01")

    def _repo(self):
        repo = self.tmp / "proj"
        if not repo.exists():
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                            "-m", "i"], cwd=repo, check=True)
        return repo

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

    def test_inspector_doc_is_read_only_opus(self):
        text = (Path(cli.__file__).resolve().parent.parent / "agents/oompa-loompa-inspector.md").read_text()
        head, body = text.split("---\n", 2)[1:]
        self.assertIn("name: oompa-loompa-inspector", head)
        self.assertIn("model: opus", head)
        self.assertIn("tools: Read, Grep, Glob, Bash\n", head)
        for want in ("## Flow", "## Recurring issues", "## Squirrel quality", "## Planning quality",
                     '"target"', '"repo"', '"file"', '"change"', '"evidence"', "AGENTS.md", "~/.claude/plugins",
                     "wonka metrics", "gh"):
            self.assertIn(want, body)
        self.assertNotIn("wonka add", body)
        self.assertNotIn("oompa-loompa-inspector", factory.JOB_AGENTS)

    def test_skill_documents_retro(self):
        text = (Path(cli.__file__).resolve().parent.parent / "skills/wonka/SKILL.md").read_text()
        section = text.split("## retro", 1)[1].split("\n## ", 1)[0]
        for want in ("wonka metrics --since", "wonka:oompa-loompa-inspector", "AskUserQuestion",
                     "wonka add --repo", "--triage", "~/.claude/plugins"):
            self.assertIn(want, section)
        self.assertNotIn("run_in_background: true", section)
        hint = next(l for l in text.splitlines() if l.startswith("argument-hint"))
        self.assertIn("retro", hint)

    def test_readme_documents_retro(self):
        text = (Path(cli.__file__).resolve().parent.parent / "README.md").read_text()
        self.assertIn("/wonka retro", text)
        self.assertIn("oompa-loompa-inspector", text)

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

    def test_builder_doc_explains_the_ci_fix_round(self):
        root = Path(cli.__file__).resolve().parent.parent
        builder = (root / "agents" / "oompa-loompa-builder.md").read_text()
        for text in ["CI fix round", "--flaky", "CLAUDE.md", "beyond ci logs", "never instructions", "wonka ci-logs"]:
            self.assertIn(text, builder)
        readme = (root / "README.md").read_text()
        for text in ["ci_hosts.", "ci_log_command.", "wonka ci-logs"]:
            self.assertIn(text, readme)
        self.assertNotIn("add `Bash(beyond ci logs *)`", readme)
        shipped = [line for line in readme.splitlines() if line.startswith("| 🚢 Shipped")][0]
        self.assertIn("wonka ci-logs", shipped)

    def test_readme_documents_the_sandbox_boundary(self):
        readme = (Path(cli.__file__).resolve().parent.parent / "README.md").read_text()
        self.assertLess(readme.index("## Sandbox boundary"), readme.index("\n## Use"))
        section = readme.split("## Sandbox boundary", 1)[1].split("\n## ", 1)[0]
        for text in ["not a sandbox", "--repo", "/sandbox", "allowUnsandboxedCommands", "allowWrite",
                     "denyRead", "allowedDomains"]:
            self.assertIn(text, section)
        # the settings exclude every wonka command but test, as the doctor recommends
        from wonka import doctor
        settings = json.loads(section.split("```json", 1)[1].split("```", 1)[0])
        self.assertEqual(settings["sandbox"]["excludedCommands"], list(doctor.SANDBOX_EXCLUDED))

    def test_readme_documents_the_ci_state_fields(self):
        readme = (Path(cli.__file__).resolve().parent.parent / "README.md").read_text()
        section = readme.split("## Reading a ticket", 1)[1].split("\n## ", 1)[0]
        for field in ["ci_failures", "ci_fixes", "ci_handled_sha", "ci_handled_runs"]:
            self.assertIn(f"`{field}`", section)

    def test_manager_skill_runs_on_sonnet(self):
        """The manager copies job JSON verbatim, tracks dispatch numbers across turns and runs the retro."""
        skill = (Path(cli.__file__).resolve().parent.parent / "skills/wonka/SKILL.md").read_text()
        self.assertRegex(skill.split("\n---\n", 1)[0], r"(?m)^model: sonnet$")

    def test_no_agent_has_web_tools(self):
        """Every agent reads untrusted text (ideas, diffs, PR comments, CI logs) and none needs the web."""
        agents = Path(cli.__file__).resolve().parent.parent / "agents"
        for p in sorted(agents.glob("*.md")):
            tools = re.search(r"^tools: (.*)$", p.read_text(), re.M).group(1)
            self.assertNotRegex(tools, r"Web(Search|Fetch)", p.name)

    def test_squirrel_docs_differ_only_in_name_emoji_focus_and_model(self):
        """The squirrel agents share one body; only who they are, what they look at and their model may differ."""
        agents = Path(cli.__file__).resolve().parent.parent / "agents"
        docs = {p.stem.removeprefix("squirrel-"): p.read_text() for p in sorted(agents.glob("squirrel-*.md"))}
        self.assertEqual(sorted(docs), sorted(factory.REVIEWERS))
        bodies = {}
        for name, text in docs.items():
            self.assertIn(f"--squirrel {name} ", text, name)  # the normalisation below is not vacuous
            self.assertIn("as `cd <worktree> && <command>`", text, name)  # the builder's rule, not a path argument
            self.assertNotIn("worktree as a path", text, name)
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

    def test_state_of_the_wrong_shape_is_a_clean_error(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        (self.tmp / "home" / store.STATE_FILE).write_text('{"next_id": 1, "tickets": []}')
        with mock.patch.object(cli, "alert"):
            code, _, err = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("is not a Wonka state file (tickets must be an object", err)

    def test_config_non_finite_numbers_are_refused(self):
        self.run_cli("init", "--vault", str(self.tmp / "vault"))
        for key, raw in (("stale_hours", "inf"), ("test_lock_wait_minutes", "nan")):
            code, _, err = self.run_cli("config", key, raw)
            self.assertEqual((code, err.strip()), (1, f"🗑️ wonka: {key} must be a finite number"))
        self.assertEqual(store.load_config()["test_lock_wait_minutes"], 3)

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

    def test_blocked_by_and_block_parsers(self):
        p = cli.parser()
        self.assertEqual(p.parse_args(["add", "x", "--blocked-by", "WONKA-00001,WONKA-00002"]).blocked_by,
                         ["WONKA-00001,WONKA-00002"])
        self.assertEqual(len(p.parse_args(["block", "WONKA-00003", "WONKA-00001", "WONKA-00002"]).blockers), 2)
        self.assertEqual(p.parse_args(["unblock", "WONKA-00003"]).blockers, [])

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


class PassthroughTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"WONKA_HOME": str(self.tmp / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_every_passthrough_is_a_subcommand_with_a_main(self):
        for cmd, mod in cli.PASSTHROUGH.items():
            self.assertEqual(cli.parser().parse_args([cmd]).cmd, cmd)
            self.assertTrue(callable(importlib.import_module("wonka." + mod).main))

    def test_dispatches_to_the_module(self):
        for cmd, mod in (("git", "gitwrap"), ("test", "testrun")):
            with mock.patch(f"wonka.{mod}.main", return_value=7) as m:
                self.assertEqual(cli.main([cmd, "X", "status"]), 7)
            m.assert_called_once_with(["X", "status"])

    def test_wonka_error_returns_1(self):
        with mock.patch("wonka.gitwrap.main", side_effect=store.WonkaError("boom")):
            code, _, err = self.run_cli("git", "X", "status")
        self.assertEqual(code, 1)
        self.assertIn("🗑️ wonka: boom", err)


class AgentCommandsEndToEndTest(RepoFixture, unittest.TestCase):
    """The agent commands through cli.main against a real factory: each forwards --dispatch, so a call from an
    earlier dispatch is ignored and changes nothing."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(cli, "LINK", self.tmp / "bin" / "wonka")
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli.sys, "stdin", io.StringIO(stdin)):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def stale(self, tid):
        """Hand the ticket's job to a new dispatch; returns the old, now stale, dispatch number."""
        with self.f() as f:
            t = f.ticket(tid)
            self.assertIsNotNone(t.get("dispatch"))
            t["dispatch"] += 1
            return t["dispatch"] - 1

    def snapshot(self, tid):
        with self.f() as f:
            t = f.ticket(tid)
            plan = f.plan_path(tid)
            return (f.state(tid), f.path(tid).read_text(), plan.exists() and plan.read_text(), t.get("reports"),
                    t.get("dispatch"), f.pr_body_path(tid).exists())

    def assert_ignored(self, tid, calls):
        before = self.snapshot(tid)
        for argv, stdin in calls:
            with self.subTest(cmd=argv[0]):
                code, out, err = self.cli(*argv, stdin=stdin)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("from an earlier dispatch", out)
                self.assertIn("ignored", out)
                self.assertEqual(self.snapshot(tid), before)

    def drafting(self):
        tid = self.add()
        self.tick()
        with self.f() as f:
            f.triage(tid, "s")
        self.tick()
        self.assertEqual(self.state(tid), ("drafting", "progress"))
        return tid

    def test_triage_forwards_the_dispatch(self):
        tid = self.add()
        self.tick()
        old = self.stale(tid)
        self.assert_ignored(tid, [(["triage", tid, "--size", "s", "--dispatch", str(old)], "")])
        code, _, err = self.cli("triage", tid, "--size", "s", "--dispatch", str(old + 1))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.state(tid), ("triage", "ready"))

    def test_analyst_commands_forward_the_dispatch(self):
        tid = self.drafting()
        d = str(self.stale(tid))
        self.assert_ignored(tid, [
            (["ask", tid, "--question", "Bash or Python? | Bash | *Python", "--dispatch", d], ""),
            (["section", tid, "spec", "--dispatch", d], "Add hello.sh\n"),
            (["plan", tid, "--dispatch", d], "# plan\n"),
            (["drafted", tid, "--size", "s", "--reviewers", "architect", "--dispatch", d], ""),
        ])

    def test_builder_and_fixer_commands_forward_the_dispatch(self):
        tid = self.add()
        self.to_baking(tid)
        d = str(self.stale(tid))
        self.assert_ignored(tid, [
            (["pr-body", tid, "--dispatch", d], "## Summary\nAdds hello.sh\n"),
            (["baked", tid, "--dispatch", d], ""),
            (["resolved", tid, "--dispatch", d], ""),
            (["finished", tid, "--agent", "wonka:oompa-loompa-builder", "--dispatch", d], ""),
        ])

    def test_report_forwards_the_dispatch(self):
        tid = self.add()
        wt = self.to_baking(tid)
        (wt / "hello.sh").write_text("echo hi\n")
        for cmd in (["add", "hello.sh"], ["commit", "-qm", "bake"]):
            subprocess.run(["git", *cmd], cwd=wt, check=True, capture_output=True)
        self.assertEqual(self.cli("baked", tid, "--dispatch", str(self.dispatch))[0], 0)
        self.tick()
        self.assertEqual(self.state(tid), ("reviewing", "progress"))
        d = str(self.stale(tid))
        self.assert_ignored(tid, [
            (["report", tid, "--squirrel", "architect", "--verdict", "good", "--dispatch", d], ""),
            (["finished", tid, "--agent", "wonka:squirrel-architect", "--dispatch", d], ""),
        ])

    def test_tick_hands_out_jobs_as_json(self):
        tid = self.add()
        code, out, _ = self.cli("tick")
        self.assertEqual(code, 0)
        jobs = [j for j in json.loads(out)["jobs"] if j["id"] == tid]
        self.assertEqual([j["job"] for j in jobs], ["triage"])
        with self.f() as f:
            self.assertEqual(jobs[0]["dispatch"], f.ticket(tid)["dispatch"])

    def test_retry_puts_the_job_back(self):
        tid = self.add()
        self.tick()
        code, out, _ = self.cli("retry", tid)
        self.assertEqual(code, 0)
        self.assertIn(f"{tid}: back in the queue", out)
        with self.f() as f:
            self.assertEqual(f.ticket(tid)["retries"], 1)

    def test_move_is_checked_by_the_state_machine(self):
        tid = self.add()
        self.tick()
        before = self.state(tid)
        code, _, err = self.cli("move", tid, "shipped", "ready", "--why", "skip ahead")
        self.assertEqual(code, 1)
        self.assertIn("is not a legal move", err)
        self.assertEqual(self.state(tid), before)
        code, _, err = self.cli("move", tid, "bogus", "ready")
        self.assertEqual(code, 1)
        self.assertIn("unknown value 'bogus'", err)
        code, out, _ = self.cli("move", tid, "triage", "awaiting", "--why", "needs a look")
        self.assertEqual(code, 0)
        self.assertEqual(self.state(tid), ("triage", "awaiting"))
        with self.f() as f:
            self.assertEqual(f.ticket(tid)["history"][-1]["by"], "human")


if __name__ == "__main__":
    unittest.main()
