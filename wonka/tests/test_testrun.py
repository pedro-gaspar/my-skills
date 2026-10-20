"""wonka test: prefix checks, the per-project lock and the run itself, against a real repo."""

import contextlib
import fcntl
import io
import json
import shlex
import time
import unittest
from unittest import mock

from wonka import cli, store, testrun
from wonka.store import WonkaError

from .fixtures import RepoFixture


class OpenPrefixTest(unittest.TestCase):
    def test_open(self):
        for p in ["python3", "python3 -c", "python3 -m", "/bin/sh -c", "bash", "bash -x", "node -e", "env", "npx",
                  "uv run", "poetry run", "python3.12 -c",
                  "python3 -W ignore", "python3 -X dev -W error", "python3.12 -W ignore -c", "node -r ./setup.js",
                  "node --require ./setup.js -e", "env FOO=1", "env -u HOME FOO=1 BAR=2", "env -C /tmp",
                  "bash -o pipefail", "timeout 600", "timeout -k 5 600", "nice -n 10", "sudo -u ci",
                  "env FOO=1 python3 -c", "env bash", "timeout 600 sh -c", "uv run python3",
                  "uv run --with pytest python3 -c", "env -- bash"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))

    def test_pinned(self):
        for p in ["python3 -m unittest", "python3 -m pytest", "bash ./test.sh", "sh -c pytest", "./test.sh",
                  "make test", "pytest", "uv run pytest", "npm test", "npm run test",
                  "python3 -W ignore -m pytest", "python3 -W ignore ./run_tests.py", "node -r ./setup.js ./test.js",
                  "env FOO=1 pytest", "env FOO=1 python3 -m pytest", "timeout 600 ./test.sh",
                  "timeout 600 sh -c pytest", "nice -n 10 make test", "uv run --with pytest pytest",
                  "bash -o pipefail ./test.sh"]:
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
        self.assertEqual(testrun.open_prefixes({"test_commands": {"p": None}}, "p"), [])
        self.assertEqual(testrun.open_prefixes({"test_commands": {"p": None}}), [])


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


class RunTest(RepoFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # a pinned prefix that still runs the code a test passes: python3 -c itself is open, so wonka test refuses it
        runner = self.tmp / "run.py"
        runner.write_text("import sys\nexec(sys.argv.pop(1))\n")  # argv as with python3 -c
        self.py = ("python3", str(runner))
        self.cfg = {**self.cfg, "test_commands": {"app": ["./t.sh", shlex.join(self.py)]},
                    "test_lock_wait_minutes": 0.01}
        store.save_config(self.cfg)

    def wtest(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["test", *argv])
        return code, err.getvalue()

    def test_allowed_prefix_runs_in_the_worktree_and_passes_the_exit_code(self):
        tid = self.add()
        wt = self.to_baking(tid)
        code, _ = self.wtest(tid, "--", *self.py,
                             "import os,sys; open('cwd.txt','w').write(os.getcwd()); sys.exit(3)")
        self.assertEqual(code, 3)
        self.assertTrue((wt / "cwd.txt").exists())
        self.assertFalse((self.repo / "cwd.txt").exists())

    def test_open_prefix_set_by_hand_is_refused(self):
        store.save_config({**self.cfg, "test_commands": {"app": ["python3 -c"]}})
        tid = self.add()
        wt = self.to_baking(tid)
        code, err = self.wtest(tid, "--", "python3", "-c", "open('ran.txt','w')")
        self.assertEqual(code, 1)
        self.assertIn("refusing to run it", err)
        self.assertFalse((wt / "ran.txt").exists())

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
            code, err = self.wtest(tid, "--", *self.py, "open('ran.txt','w')")
            waited = time.monotonic() - start
        self.assertEqual(code, 75)
        self.assertIn("tests busy: WONKA-00012 has held the lock since 14:03, run it again", err)
        self.assertGreaterEqual(waited, 0.5)
        self.assertFalse((wt / "ran.txt").exists())

    def test_holder_is_recorded_and_lock_released(self):
        tid = self.add()
        wt = self.to_baking(tid)
        lp = testrun.lock_path("app")
        code, _ = self.wtest(tid, "--dispatch", "4", "--", *self.py,
                             "import sys; open('holder.json','w').write(open(sys.argv[1]).read())", str(lp))
        self.assertEqual(code, 0)
        holder = json.loads((wt / "holder.json").read_text())
        self.assertEqual((holder["id"], holder["dispatch"]), (tid, 4))
        self.assertEqual(lp.read_text(), "")
        with open(lp) as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_holder_is_cleared_when_the_command_is_missing(self):
        tid = self.add()
        self.to_baking(tid)
        with mock.patch.object(testrun.subprocess, "run", side_effect=FileNotFoundError):
            code, _ = self.wtest(tid, "--", "./t.sh")
        self.assertEqual(code, 1)
        self.assertEqual(testrun.lock_path("app").read_text(), "")

    def test_waiter_sees_no_stale_holder_after_a_run(self):
        tid = self.add()
        self.to_baking(tid)
        lp = testrun.lock_path("app")
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps({"id": "WONKA-00012", "dispatch": 1, "started": "2026-10-19T14:03:00"}))
        self.assertEqual(self.wtest(tid, "--", *self.py, "pass")[0], 0)
        with open(lp, "a+") as held:  # a new holder that has not written its info yet
            fcntl.flock(held, fcntl.LOCK_EX)
            code, err = self.wtest(tid, "--", *self.py, "pass")
        self.assertEqual(code, 75)
        self.assertIn("tests busy: another run holds the lock, run it again", err)
        self.assertNotIn("WONKA-00012", err)

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
            self.assertEqual(self.wtest(tid, "--", *self.py, "import sys; sys.exit(5)")[0], 5)

    def test_lock_lives_in_the_worktrees_folder(self):
        """The sandbox may write the worktrees folder only, never the rest of ~/.wonka."""
        self.assertEqual(testrun.lock_path("app"), store.worktrees_dir() / ".locks" / "app-tests.lock")

    def test_runs_when_the_wonka_home_is_read_only(self):
        """As inside the sandbox: only the worktrees folder is writable. A busy lock still exits 75, unlogged."""
        tid = self.add()
        wt = self.to_baking(tid)
        home, log = store.home(), store.home() / "factory.log"
        log.touch()
        for path, mode in ((home, 0o555), (log, 0o444)):
            path.chmod(mode)
            self.addCleanup(path.chmod, 0o755)
        code, _ = self.wtest(tid, "--", *self.py, "open('ran.txt','w')")
        self.assertEqual(code, 0)
        self.assertTrue((wt / "ran.txt").exists())
        with open(testrun.lock_path("app")) as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, err = self.wtest(tid, "--", *self.py, "pass")
        self.assertEqual(code, 75)
        self.assertIn("tests busy", err)
        code, err = self.wtest("WONKA-00099", "--", "./t.sh")  # an error is reported, its log line skipped
        self.assertEqual(code, 1)
        self.assertIn("No ticket WONKA-00099", err)
        self.assertNotIn("WONKA-00099", log.read_text())

    def test_planted_links_in_the_locks_folder_are_refused(self):
        """Sandboxed test code can write the locks folder: wonka test never writes through a link it planted there."""
        tid = self.add()
        self.to_baking(tid)
        victim = self.tmp / "victim.txt"
        lp = testrun.lock_path("app")
        lp.parent.mkdir(parents=True, exist_ok=True)
        for plant in (lambda: lp.symlink_to(victim), lambda: lp.hardlink_to(victim)):
            victim.write_text("keep me")
            lp.unlink(missing_ok=True)
            plant()
            with self.subTest(plant=plant), mock.patch.object(testrun.subprocess, "run") as run:
                code, err = self.wtest(tid, "--", "./t.sh")
            self.assertEqual(code, 1)
            self.assertIn("not a plain lock file", err)
            run.assert_not_called()
            self.assertEqual(victim.read_text(), "keep me")
        lp.unlink()
        lp.parent.rmdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        lp.parent.symlink_to(elsewhere)
        code, err = self.wtest(tid, "--", "./t.sh")
        self.assertEqual(code, 1)
        self.assertIn("not a plain lock file", err)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_signal_exit_maps_to_128_plus_n(self):
        tid = self.add()
        self.to_baking(tid)
        with mock.patch.object(testrun.subprocess, "run", return_value=mock.Mock(returncode=-9)):
            self.assertEqual(self.wtest(tid, "--", "./t.sh")[0], 137)


class RunnerTablesTest(unittest.TestCase):
    """Each runner, launcher subcommand and option value in testrun's tables, spelled out, so none is dropped."""

    def test_every_runner_is_open(self):
        for p in ["sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "python3", "pypy3", "ruby", "perl",
                  "node", "deno", "bun", "php", "lua", "Rscript", "osascript", "pwsh", "powershell", "tclsh",
                  "env", "exec", "command", "xargs", "nice", "nohup", "timeout 60", "time", "sudo", "doas",
                  "npx", "pnpx", "bunx", "uvx", "Rscript -e", "osascript -e", "pwsh -Command",
                  "powershell -Command"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))

    def test_every_launcher_subcommand_is_open(self):
        for p in ["uv run", "poetry run", "pipenv run", "npm exec", "pnpm exec", "pnpm dlx", "yarn exec", "yarn dlx",
                  "bundle exec", "hatch run", "pdm run", "pnpm dlx python3 -c", "hatch run bash", "pdm run sh -c"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))
        for p in ["pnpm dlx vitest", "hatch run pytest", "pdm run pytest", "hatch test", "pdm test"]:
            with self.subTest(p):
                self.assertFalse(testrun.open_prefix(p))

    def test_more_interpreters_and_wrappers_are_open(self):
        for p in ["awk", "gawk -v x=1", "mawk -F ,", "nawk", "gawk -e", "busybox", "busybox sh", "busybox sh -c",
                  "java", "java -jar", "java -cp lib", "julia -e", "elixir -e",
                  "setsid", "setsid -w bash", "stdbuf -oL", "stdbuf -o L sh -c", "flock /tmp/l", "flock -w 5 /tmp/l",
                  "flock /tmp/l -c", "flock /tmp/l --command", "flock /tmp/l bash", "unshare", "unshare -r",
                  "unshare --wd /tmp sh -c", "nsenter -t 1", "nice", "ionice -c 3", "ionice -c 3 python3 -c",
                  "chroot /", "chroot --userspec u:g / bash", "taskset 0x1", "chrt 10", "strace -o log",
                  "ltrace", "valgrind", "fakeroot", "eatmydata", "firejail", "systemd-run --user", "setpriv",
                  "xargs", "nohup", "sudo", "doas", "timeout 60 busybox sh"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))
        for p in ["awk -f t.awk", "gawk '{print}'", "busybox make test", "java -jar t.jar", "java -cp lib org.T",
                  "setsid ./test.sh", "stdbuf -oL ./test.sh", "flock /tmp/l ./test.sh", "flock /tmp/l -c ./test.sh",
                  "unshare -r ./test.sh", "ionice -c 3 make test", "chroot / ./test.sh", "taskset 0x1 pytest",
                  "strace -o log ./test.sh", "systemd-run --user ./test.sh", "nohup ./test.sh"]:
            with self.subTest(p):
                self.assertFalse(testrun.open_prefix(p))

    def test_programs_that_run_a_command_anywhere_are_always_open(self):
        """find -exec, watch (joins its arguments into sh -c), script -c, parallel, gdb -ex, su -c take a command
        wherever it comes, so no operand pins them."""
        for p in ["find", "find . -name '*_test.py'", "watch", "watch ./test.sh", "script", "script -q -c ./t.sh",
                  "parallel", "parallel ./t.sh", "gdb --args ./t", "su", "runuser -u ci", "env FOO=1 find .",
                  "timeout 60 watch ./t.sh", "docker compose run tests", "docker run --rm img", "podman run img",
                  "nerdctl run img"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))

    def test_more_launcher_subcommands_are_open(self):
        for p in ["go run", "go run -tags x", "cargo run", "cargo +nightly run", "cargo run --release",
                  "deno run", "deno run -A", "deno eval", "bun run", "bun x", "bun exec", "npm x", "yarn node",
                  "rye run", "pixi run", "conda run -n env", "mamba run", "mix run", "npm --prefix x exec",
                  "uv --directory x run", "uv --directory x run bash"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))
        for p in ["go run ./cmd/test", "go run .", "cargo run --bin tests", "deno run -A ./test.ts", "deno eval 'Deno.test()'",
                  "bun run test", "conda run -n env pytest", "mix run test.exs", "go test ./...", "cargo test",
                  "deno test", "bun test", "npm run test", "yarn test", "uv run pytest"]:
            with self.subTest(p):
                self.assertFalse(testrun.open_prefix(p))

    def test_a_launcher_with_no_subcommand_is_open(self):
        """The caller could append run or exec: uv run bash."""
        for p in ["uv", "npm", "pnpm", "yarn", "go", "cargo", "poetry", "pipenv", "bundle", "hatch", "pdm", "rye",
                  "pixi", "conda", "mamba", "mix", "uv --project x", "cargo +nightly", "env go"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))
        # build tools that run the repo's own files, like ./test.sh does, are not open, even bare
        for p in ["make", "make -j4", "make test", "pytest", "cargo test", "npm test", "npm run", "tox"]:
            with self.subTest(p):
                self.assertFalse(testrun.open_prefix(p))

    def test_env_long_options_take_a_value(self):
        for p in ["env --unset HOME", "env --unset HOME FOO=1", "env --chdir /tmp", "env --chdir /tmp FOO=1",
                  "env --unset HOME python3 -c", "env --chdir /tmp bash"]:
            with self.subTest(p):
                self.assertTrue(testrun.open_prefix(p))
        for p in ["env --unset HOME pytest", "env --chdir /tmp ./test.sh"]:
            with self.subTest(p):
                self.assertFalse(testrun.open_prefix(p))


class RunStdinTest(RepoFixture, unittest.TestCase):
    def test_the_command_gets_no_stdin(self):
        """An agent's terminal or pipe never reaches the test command: it reads EOF."""
        store.save_config({**self.cfg, "test_commands": {"app": ["./t.sh"]}})
        tid = self.add()
        self.to_baking(tid)
        with mock.patch.object(testrun.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["test", tid, "--", "./t.sh"]), 0)
        self.assertIs(run.call_args.kwargs["stdin"], testrun.subprocess.DEVNULL)
