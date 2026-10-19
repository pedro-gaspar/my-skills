"""Every test run gets a private temp dir; the last test checks the suite left nothing in it."""

import atexit
import os
import shutil
import tempfile
import unittest

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
