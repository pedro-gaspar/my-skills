"""wonka timer: run `wonka tick --timer` every few minutes with no Claude session, so ticked checkboxes and merged
PRs are picked up. A systemd user timer on Linux, a launchd agent on macOS."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import store
from .store import WonkaError

PLATFORM = sys.platform
NAME = "wonka-tick"
LABEL = "dev.wonka.tick"
SYSTEMCTL = "systemctl"
LAUNCHCTL = "launchctl"


def _systemd_dir():
    return Path.home() / ".config" / "systemd" / "user"


def _plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path():
    return store.home() / "timer.log"


def _env():
    """A service gets no login shell, so it keeps the PATH (claude, gh, git) and WONKA_HOME of the installer."""
    env = {"PATH": os.environ.get("PATH", "")}
    if "WONKA_HOME" in os.environ:
        env["WONKA_HOME"] = os.environ["WONKA_HOME"]
    return env


def systemd_units(wonka, minutes):
    env = "\n".join(f'Environment="{k}={v}"' for k, v in _env().items())
    log = log_path()
    service = (f"[Unit]\nDescription=Wonka factory tick\n\n[Service]\nType=oneshot\n{env}\n"
               f'ExecStart="{wonka}" tick --timer\nStandardOutput=append:{log}\nStandardError=append:{log}\n')
    timer = (f"[Unit]\nDescription=Run the Wonka factory tick every {minutes} minutes\n\n[Timer]\n"
             f"OnBootSec=2min\nOnUnitActiveSec={minutes}min\nUnit={NAME}.service\n\n[Install]\nWantedBy=timers.target\n")
    return {_systemd_dir() / f"{NAME}.service": service, _systemd_dir() / f"{NAME}.timer": timer}


def launchd_plist(wonka, minutes):
    log = str(log_path())
    return plistlib.dumps({"Label": LABEL, "ProgramArguments": [str(wonka), "tick", "--timer"],
                           "StartInterval": minutes * 60, "EnvironmentVariables": _env(),
                           "StandardOutPath": log, "StandardErrorPath": log}).decode()


def _run(*argv, check=True):
    try:
        p = subprocess.run(argv, capture_output=True, text=True)
    except OSError as e:
        raise WonkaError(f"could not run {argv[0]}: {e}")
    if check and p.returncode:
        raise WonkaError(f"{' '.join(argv)} failed: {(p.stderr or p.stdout).strip()}")
    return p


def _domain():
    return f"gui/{os.getuid()}"


def _loaded():
    return _run(LAUNCHCTL, "print", f"{_domain()}/{LABEL}", check=False).returncode == 0


def _platform():
    if PLATFORM.startswith("linux"):
        return "linux"
    if PLATFORM == "darwin":
        return "mac"
    raise WonkaError(f"wonka timer runs on Linux (systemd) and macOS (launchd), not {PLATFORM}")


def install(wonka, minutes):
    store.home().mkdir(parents=True, exist_ok=True)
    if _platform() == "linux":
        for path, text in systemd_units(wonka, minutes).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            store.write_atomic(path, text)
        _run(SYSTEMCTL, "--user", "daemon-reload")
        _run(SYSTEMCTL, "--user", "enable", "--now", f"{NAME}.timer")
        where = f"systemd user timer {NAME}.timer (it runs while you are logged in, unless loginctl enable-linger)"
    else:
        if _loaded():
            _run(LAUNCHCTL, "bootout", f"{_domain()}/{LABEL}")
        _plist_path().parent.mkdir(parents=True, exist_ok=True)
        store.write_atomic(_plist_path(), launchd_plist(wonka, minutes))
        _run(LAUNCHCTL, "bootstrap", _domain(), str(_plist_path()))
        where = f"launchd agent {LABEL}"
    return f"⏰ wonka tick runs every {minutes} minutes: {where}; output in {log_path()}"


def uninstall():
    if _platform() == "linux":
        units = list(systemd_units("wonka", 0))
        if not any(p.exists() for p in units):
            return "⏰ no timer installed"
        _run(SYSTEMCTL, "--user", "disable", "--now", f"{NAME}.timer")
        for p in units:
            p.unlink(missing_ok=True)
        _run(SYSTEMCTL, "--user", "daemon-reload")
    else:
        if not _plist_path().exists() and not _loaded():
            return "⏰ no timer installed"
        if _loaded():
            _run(LAUNCHCTL, "bootout", f"{_domain()}/{LABEL}")
        _plist_path().unlink(missing_ok=True)
    return "⏰ timer removed"


def installed():
    if _platform() == "linux":
        return _run(SYSTEMCTL, "--user", "is-enabled", f"{NAME}.timer", check=False).returncode == 0
    return _loaded()


def status():
    if not installed():
        return "⏰ no timer installed: wonka timer install"
    return f"⏰ timer installed; output in {log_path()}"
