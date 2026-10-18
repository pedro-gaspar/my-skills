import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import doctor


class PermissionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_rules_from_every_settings_file_are_merged(self):
        a = self.write("settings.json", {"permissions": {"allow": ["Bash(wonka *)"]}})
        b = self.write("settings.local.json", {"permissions": {"allow": ["Bash(git:*)"], "additionalDirectories": ["~/x"]}})
        self.assertEqual(doctor._permissions((a, b)),
                         (["Bash(wonka *)", "Bash(git:*)"], [str(Path("~/x").expanduser())]))

    def test_missing_and_broken_files_are_skipped(self):
        good = self.write("good.json", {"permissions": {"allow": ["Bash(wonka *)"]}})
        paths = (self.tmp / "missing.json", self.write("list.json", []), self.write("bad.json", "not json"), good)
        self.assertEqual(doctor._permissions(paths), (["Bash(wonka *)"], []))

    def test_default_paths_are_read_at_call_time(self):
        a = self.write("settings.json", {"permissions": {"allow": ["Bash(git *)"]}})
        with mock.patch.object(doctor, "SETTINGS", (a, self.tmp / "nope.json")):
            self.assertEqual(doctor._permissions(), (["Bash(git *)"], []))


if __name__ == "__main__":
    unittest.main()
