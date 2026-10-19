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

    def test_non_object_permissions_skips_the_file(self):
        good = self.write("good.json", {"permissions": {"allow": ["Bash(wonka *)"]}})
        paths = (self.write("list.json", {"permissions": ["Bash(x)"]}),
                 self.write("str.json", {"permissions": "x"}),
                 self.write("null.json", {"permissions": None}), good)
        self.assertEqual(doctor._permissions(paths), (["Bash(wonka *)"], []))

    def test_string_allow_is_skipped(self):
        a = self.write("a.json", {"permissions": {"allow": "Bash(git *)", "additionalDirectories": ["~/x"]}})
        self.assertEqual(doctor._permissions((a,)), ([], [str(Path("~/x").expanduser())]))

    def test_string_additional_directories_is_skipped(self):
        a = self.write("a.json", {"permissions": {"allow": ["Bash(wonka *)"], "additionalDirectories": "~/x"}})
        self.assertEqual(doctor._permissions((a,)), (["Bash(wonka *)"], []))

    def test_non_string_items_are_dropped(self):
        a = self.write("a.json", {"permissions": {"allow": ["Bash(wonka *)", 1, None],
                                                  "additionalDirectories": ["~/x", 2, None]}})
        self.assertEqual(doctor._permissions((a,)), (["Bash(wonka *)"], [str(Path("~/x").expanduser())]))

    def test_default_paths_are_read_at_call_time(self):
        a = self.write("settings.json", {"permissions": {"allow": ["Bash(git *)"]}})
        with mock.patch.object(doctor, "SETTINGS", (a, self.tmp / "nope.json")):
            self.assertEqual(doctor._permissions(), (["Bash(git *)"], []))


class OrphansTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"vault": str(self.tmp), "folder": "wonka"}
        self.board = self.tmp / "wonka"

    def write(self, folder, name, body):
        path = self.board / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nid: WONKA-00001\n---\n" + body)
        return path

    def test_clean_note_is_ok(self):
        self.write("golden-tickets", "WONKA-00001.md", "# 🎫 T\n\n## 💡 Idea\nx\n\n## 🎩 Factory log\n> y\n")
        result = list(doctor._orphans(self.cfg))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "ok")
        self.assertIn("1 notes", result[0][1])

    def test_orphans_in_tickets_and_archive_warn(self):
        body = "## 📜 Spec\nspec\n## Changes\nmore\n\n## 🎩 Factory log\n> y\n"
        a = self.write("golden-tickets", "WONKA-00001.md", body)
        b = self.write("archive", "WONKA-00002.md", body)
        result = list(doctor._orphans(self.cfg))
        self.assertEqual(len(result), 1)
        level, msg = result[0]
        self.assertEqual(level, "warn")
        self.assertIn(str(a), msg)
        self.assertIn(str(b), msg)
        self.assertIn("## Changes (below ## 📜 Spec)", msg)

    def test_plans_are_skipped(self):
        self.write("archive", "WONKA-00001-plan.md", "# Plan\n\n## Goal\nx\n\n## Changes\ny\n")
        self.assertEqual(list(doctor._orphans(self.cfg)), [("ok", "notes: no unknown ## sections (0 notes)")])

    def test_missing_folders_are_ok(self):
        self.assertEqual(list(doctor._orphans(self.cfg)), [("ok", "notes: no unknown ## sections (0 notes)")])

    def test_undecodable_note_warns_unreadable(self):
        path = self.board / "golden-tickets" / "WONKA-00001.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"---\nid: WONKA-00001\n---\n# \xff\xfe bad\n")
        try:
            path.read_text()
        except UnicodeDecodeError:
            pass
        else:
            self.skipTest("default encoding decodes any bytes")
        self.write("golden-tickets", "WONKA-00002.md", "# 🎫 T\n\n## 💡 Idea\nx\n\n## 🎩 Factory log\n> y\n")
        result = list(doctor._orphans(self.cfg))
        self.assertEqual(len(result), 1)
        level, msg = result[0]
        self.assertEqual(level, "warn")
        self.assertIn(f"{path}: unreadable (", msg)
        self.assertNotIn("WONKA-00002.md", msg)

    def test_unreadable_path_warns_unreadable(self):
        path = self.board / "archive" / "WONKA-00003.md"
        path.mkdir(parents=True)
        result = list(doctor._orphans(self.cfg))
        self.assertEqual(len(result), 1)
        level, msg = result[0]
        self.assertEqual(level, "warn")
        self.assertIn(f"{path}: unreadable (", msg)



class NpxCheckTest(unittest.TestCase):
    def npx_lines(self):
        with mock.patch.object(doctor.store, "load_config", side_effect=doctor.store.WonkaError("no config")):
            return [c for c in doctor.checks() if c[1].startswith("npx")]

    def test_missing_npx_warns(self):
        with mock.patch.dict(doctor.os.environ), mock.patch.object(doctor.shutil, "which", return_value=None), \
                mock.patch.object(doctor, "_ok", return_value=True):
            doctor.os.environ.pop("WONKA_MDLINT", None)
            self.assertEqual([lv for lv, _ in self.npx_lines()], ["warn"])

    def test_no_npx_line_with_a_custom_linter(self):
        with mock.patch.dict(doctor.os.environ, {"WONKA_MDLINT": "off"}), mock.patch.object(doctor, "_ok", return_value=True):
            self.assertEqual(self.npx_lines(), [])


if __name__ == "__main__":
    unittest.main()
