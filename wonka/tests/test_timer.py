"""wonka timer: a systemd user timer on Linux, a launchd agent on macOS, both running `wonka tick --timer`."""

import contextlib
import io
import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import cli, launcher, store, timer

from .fixtures import RepoFixture

# records each call; `launchctl print` and `systemctl is-enabled` succeed only once something was installed
FAKE_CTL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CTL_CALLS"
case "$*" in
  print*|*is-enabled*) [ -e "$CTL_LOADED" ] ;;
  bootstrap*|*enable*) touch "$CTL_LOADED" ;;
  bootout*|*disable*) rm -f "$CTL_LOADED" ;;
esac
"""


class TimerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name in ("systemctl", "launchctl"):
            (bin_dir / name).write_text(FAKE_CTL)
            (bin_dir / name).chmod(0o755)
        self.calls = self.tmp / "calls"
        env = {"HOME": str(self.tmp / "user"), "WONKA_HOME": str(self.tmp / "wonka"), "CTL_CALLS": str(self.calls),
               "CTL_LOADED": str(self.tmp / "loaded"), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        for patcher in (mock.patch.dict(os.environ, env), mock.patch.object(timer, "SYSTEMCTL", "systemctl"),
                        mock.patch.object(timer, "LAUNCHCTL", "launchctl")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def platform(self, name):
        patcher = mock.patch.object(timer, "PLATFORM", name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def called(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_linux_installs_a_systemd_user_timer_and_removes_it(self):
        self.platform("linux")
        self.assertIn("every 15 minutes", timer.install(Path("/opt/w ka/wonka"), 15))
        units = self.tmp / "user/.config/systemd/user"
        service = (units / "wonka-tick.service").read_text()
        self.assertIn('ExecStart="/opt/w ka/wonka" tick --timer', service)
        self.assertIn(f'Environment="PATH={os.environ["PATH"]}"', service)
        self.assertIn(f'Environment="WONKA_HOME={self.tmp / "wonka"}"', service)
        self.assertIn(f"StandardOutput=append:{self.tmp / 'wonka/timer.log'}", service)
        self.assertIn("OnUnitActiveSec=15min", (units / "wonka-tick.timer").read_text())
        self.assertIn("systemctl --user enable --now wonka-tick.timer", self.called())
        self.assertTrue(timer.installed())
        self.assertEqual(timer.uninstall(), "⏰ timer removed")
        self.assertEqual(list(units.iterdir()), [])
        self.assertFalse(timer.installed())
        self.assertEqual(timer.uninstall(), "⏰ no timer installed")

    def test_macos_installs_a_launchd_agent_and_reinstalls_cleanly(self):
        self.platform("darwin")
        timer.install(Path("/opt/wonka"), 10)
        plist = plistlib.loads((self.tmp / "user/Library/LaunchAgents/dev.wonka.tick.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], ["/opt/wonka", "tick", "--timer"])
        self.assertEqual(plist["StartInterval"], 600)
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], os.environ["PATH"])
        timer.install(Path("/opt/wonka"), 20)  # a second install replaces the loaded agent
        domain = f"gui/{os.getuid()}"
        self.assertEqual([c for c in self.called() if not c.startswith("launchctl print")],
                         [f"launchctl bootstrap {domain} {self.tmp}/user/Library/LaunchAgents/dev.wonka.tick.plist",
                          f"launchctl bootout {domain}/dev.wonka.tick",
                          f"launchctl bootstrap {domain} {self.tmp}/user/Library/LaunchAgents/dev.wonka.tick.plist"])
        self.assertEqual(timer.uninstall(), "⏰ timer removed")
        self.assertFalse(timer.installed())

    def test_other_platforms_are_refused(self):
        self.platform("win32")
        with self.assertRaisesRegex(store.WonkaError, "Linux .* and macOS"):
            timer.install(Path("/opt/wonka"), 15)


class TimerTickTest(RepoFixture, unittest.TestCase):
    def tick(self, *args):
        out = io.StringIO()
        with mock.patch.object(cli, "LINK", self.tmp / "link" / "wonka"), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["tick", *args]), 0)
        return out.getvalue()

    def test_a_timer_tick_keeps_a_paused_factory_paused_and_a_manual_tick_resumes_it(self):
        self.cfg["launcher"] = "claude"
        store.save_config(self.cfg)
        launcher.pause()
        self.assertIn("⏸️ factory paused", self.tick("--timer"))
        self.assertTrue(launcher.paused())
        self.assertIn("▶️ factory resumed", self.tick())
        self.assertFalse(launcher.paused())


if __name__ == "__main__":
    unittest.main()
