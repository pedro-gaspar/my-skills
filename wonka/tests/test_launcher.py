"""launcher: wonka tick starts each agent as a detached headless claude, and the worker reports back by itself."""

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from wonka import cli, factory, launcher, store

from .fixtures import RepoFixture

# records its arguments and stdin, then answers as `claude -p --output-format json` does, leaving no result in Wonka
FAKE_CLAUDE = """#!/bin/sh
out="$FAKE_CLAUDE_CALLS/$$"
{ printf '%s\\n' "$@"; echo "--- stdin"; cat; } > "$out.tmp"
mv "$out.tmp" "$out.call"
printf '%s\\n' '{"result": "sized it\\nas small", "total_cost_usd": 0.1234, "is_error": false, "session_id": "s-1"}'
"""


class LauncherTest(RepoFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        bin_dir, self.calls = self.tmp / "bin", self.tmp / "calls"
        for folder in (bin_dir, self.calls):
            folder.mkdir()
        for name, script in (("claude", FAKE_CLAUDE), ("gh", "#!/bin/sh\nexit 1\n")):
            (bin_dir / name).write_text(script)
            (bin_dir / name).chmod(0o755)
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_CLAUDE_CALLS": str(self.calls)}
        for patcher in (mock.patch.dict(os.environ, env), mock.patch.object(launcher, "CLAUDE", "claude"),
                        mock.patch.object(cli, "LINK", self.tmp / "link" / "wonka")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg["launcher"] = "claude"
        store.save_config(self.cfg)
        self.addCleanup(self.wait, self.idle, "the workers never ended")  # before the temp dir is removed

    def wait(self, done, what):
        deadline = time.monotonic() + 60
        while not done():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.05)

    def records(self):
        return sorted(launcher.runs_dir().glob("*.run.json"))

    def idle(self):
        """No record left and no worker of this test's factory still running (a worker ticks after its record goes)."""
        mark = os.fsencode(str(launcher.runs_dir()))
        for pid in (p for p in os.listdir("/proc") if p.isdigit()):
            try:
                if mark in Path(f"/proc/{pid}/cmdline").read_bytes():
                    return False
            except OSError:
                pass
        return not self.records()

    def claimed(self):
        """A ticket whose triage job has a run record but no worker (the launch stopped before the spawn)."""
        tid = self.add()
        with mock.patch.object(launcher, "spawn"):
            launcher.tick(self.cfg)
        return tid, self.records()[0]

    def set_pid(self, run, pid):
        run.write_text(json.dumps({**json.loads(run.read_text()), "pid": pid}))

    def ticket(self, tid):
        return store.snapshot()["tickets"][tid]

    def test_a_tick_runs_the_agent_in_a_headless_claude_and_the_worker_reports_back(self):
        tid = self.add()
        with warnings.catch_warnings():  # the worker outlives this process's handle on it, as it does `wonka tick`
            warnings.simplefilter("ignore", ResourceWarning)
            out = launcher.tick(self.cfg)
        self.assertEqual(out["jobs"], [])
        self.assertIn(f"{tid}: 🚀 triage launched (dispatch 1)", out["events"])
        # the fake claude leaves no result, so each worker's report retries the job and its tick launches the next
        self.wait(lambda: self.ticket(tid).get("stuck") and self.idle(), "the ticket never got stuck")
        calls = [p.read_text().split("--- stdin\n") for p in self.calls.glob("*.call")]
        self.assertEqual(len(calls), factory.MAX_RETRIES)
        dispatches = set()
        for argv, stdin in calls:
            argv = argv.splitlines()
            self.assertEqual(argv[argv.index("--agent") + 1], "wonka:oompa-loompa-triage")
            self.assertEqual(argv[argv.index("--settings") + 1], '{"disableAllHooks":true}')
            self.assertIn("-p", argv)
            job, line = stdin.rsplit("\n", 2)[:2]
            self.assertEqual(line, f"Work golden ticket {tid} (triage).")
            self.assertNotIn(line, argv)
            self.assertEqual((json.loads(job)["id"], json.loads(job)["job"]), (tid, "triage"))
            dispatches.add(json.loads(job)["dispatch"])
        self.assertEqual(dispatches, {1, 2, 3})
        log = (store.home() / "factory.log").read_text()
        self.assertIn(f"{tid}: wonka:oompa-loompa-triage left no result, back in the queue · $0.12 · sized it as small",
                      log)
        self.assertIn(f"{tid}: 🚀 triage launched (dispatch 3)", log)
        self.assertEqual(len(list(launcher.runs_dir().glob("*.log.json"))), 3)

    def test_a_running_worker_is_left_alone_and_reported_once_it_is_gone(self):
        tid, run = self.claimed()
        self.assertEqual(launcher.reap_now(self.cfg), [], "a record still waiting for its pid counts as running")
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", str(run)])
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        self.set_pid(run, worker.pid)
        self.assertEqual(launcher.reap_now(self.cfg), [])
        worker.kill()
        worker.wait()
        self.assertEqual(launcher.reap_now(self.cfg),
                         [f"{tid}: wonka:oompa-loompa-triage left no result, back in the queue · no output"])
        self.assertEqual((self.records(), self.ticket(tid)["retries"]), ([], 1))

    def test_a_pid_that_is_now_another_program_is_reaped(self):
        tid, run = self.claimed()
        self.set_pid(run, os.getpid())  # alive, but its command line does not name the run file
        self.assertEqual(len(launcher.reap_now(self.cfg)), 1)
        self.assertEqual((self.records(), self.ticket(tid)["retries"]), ([], 1))

    def test_two_reaps_at_once_report_a_worker_once(self):
        tid, run = self.claimed()
        self.set_pid(run, os.getpid())
        events = []
        threads = [threading.Thread(target=lambda: events.extend(launcher.reap_now(self.cfg))) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(events), 1)
        self.assertEqual(self.ticket(tid)["retries"], 1)

    def test_a_paused_factory_only_reports_finished_workers_until_the_next_tick(self):
        tid, run = self.claimed()
        launcher.pause()
        launcher.work(run)
        self.assertEqual(len(list(self.calls.glob("*.call"))), 1)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.ticket(tid)["retries"], 1)
        with mock.patch.object(launcher, "spawn"):
            out = launcher.tick(self.cfg)
        self.assertIn("▶️ factory resumed", out["events"])
        self.assertIn(f"{tid}: 🚀 triage launched (dispatch 2)", out["events"])
        self.assertFalse(launcher.paused())
        for record in self.records():  # its worker was never spawned
            record.unlink()

    def test_a_stale_job_is_retried_only_when_no_worker_runs_it(self):
        tid, run = self.claimed()
        with store.state() as db:
            db["tickets"][tid]["job_started_at"] = factory._iso(factory.now() - dt.timedelta(hours=3))
        out = launcher.tick(self.cfg)
        self.assertEqual((out["stale"], self.ticket(tid).get("retries", 0)), ([tid], 0))
        run.unlink()  # the worker was lost with its record
        out = launcher.tick(self.cfg)
        self.assertIn(f"{tid}: back in the queue", out["events"])
        self.assertEqual(self.ticket(tid)["retries"], 1)

    def test_a_missing_claude_is_reported_and_the_job_retried(self):
        tid = self.add()
        with mock.patch.object(launcher, "CLAUDE", "wonka-tests-no-claude"):
            out = launcher.tick(self.cfg)
        self.assertIn(f"{tid}: ⚠️ claude is not on PATH, no agent launched: back in the queue", out["events"])
        self.assertEqual((out["jobs"], self.records(), self.ticket(tid)["retries"]), ([], [], 1))

    def test_the_session_launcher_hands_the_jobs_to_the_manager(self):
        tid = self.add()
        self.cfg["launcher"] = "session"
        out = launcher.tick(self.cfg)
        self.assertEqual([(j["id"], j["agents"]) for j in out["jobs"]], [(tid, ["wonka:oompa-loompa-triage"])])
        self.assertEqual(out["launcher"], "session")
        self.assertEqual((self.records(), list(self.calls.iterdir())), ([], []))

    def test_launcher_setting(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["config", "launcher", "session"]), 0)
            self.assertEqual(cli.main(["config", "launcher", "tmux"]), 1)
        self.assertIn("🚀 launcher         : session", out.getvalue())
        self.assertIn("launcher must be claude or session", err.getvalue())
        self.assertEqual(store.load_config()["launcher"], "session")


if __name__ == "__main__":
    unittest.main()
