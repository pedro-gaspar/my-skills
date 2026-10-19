"""Every test run gets a private temp dir; the last test checks the suite left nothing in it.

The suite is also hermetic against the developer's git config: before any test runs, git is pointed at a private
global config (only an identity in it) and the system config is skipped, so a global commit.gpgsign, core.hooksPath,
core.editor or init.defaultBranch never reaches a test. `wonka git` drops every GIT_CONFIG_* and pins HOME to the passwd
home (so an agent cannot pick its config), so for it the passwd home is faked to a private home holding that config."""

import atexit
import os
import shutil
import tempfile
import unittest

from wonka import gitwrap

GIT_HOME = tempfile.mkdtemp(prefix="wonka-tests-git-")  # outside RUN_TMP, which must end up empty
atexit.register(shutil.rmtree, GIT_HOME, ignore_errors=True)
GIT_CONFIG = os.path.join(GIT_HOME, ".gitconfig")
with open(GIT_CONFIG, "w") as fh:
    fh.write("[user]\n\tname = t\n\temail = t@t\n")
os.environ.update(GIT_CONFIG_GLOBAL=GIT_CONFIG, GIT_CONFIG_NOSYSTEM="1", GIT_EDITOR="true", GIT_PAGER="cat",
                  GIT_TERMINAL_PROMPT="0")
os.environ.pop("XDG_CONFIG_HOME", None)
for _k in [k for k in os.environ if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))] + [
        "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_SYSTEM", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]:
    os.environ.pop(_k, None)
REAL_PASSWD_HOME = gitwrap._passwd_home  # for the tests of _passwd_home itself
gitwrap._passwd_home = lambda: GIT_HOME

RUN_TMP = tempfile.mkdtemp(prefix="wonka-tests-")
atexit.register(shutil.rmtree, RUN_TMP, ignore_errors=True)
tempfile.tempdir = RUN_TMP


class NoLeftoversTest(unittest.TestCase):
    def test_tests_leave_no_temp_dirs(self):
        self.assertEqual(sorted(os.listdir(RUN_TMP)), [], "a test did not remove its tempfile.mkdtemp() folder")


def load_tests(loader, standard_tests, pattern):
    standard_tests.addTests(loader.discover(start_dir=os.path.dirname(__file__), pattern=pattern))
    standard_tests.addTest(NoLeftoversTest("test_tests_leave_no_temp_dirs"))
    return standard_tests
