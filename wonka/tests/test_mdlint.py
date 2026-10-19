import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import mdlint

MD041 = "stdin:1 error MD041/first-line-heading/first-line-h1 First line in a file should be a top-level heading"


class MdlintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"WONKA_HOME": str(self.tmp / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def linter(self, script):
        path = self.tmp / "lint"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        os.environ["WONKA_MDLINT"] = str(path)
        return path

    def failing(self):
        self.calls = self.tmp / "calls"
        return self.linter(f'echo x >> "{self.calls}"\necho "{MD041}" >&2\nexit 1\n')

    def test_clean_text_is_unchanged(self):
        self.linter("cat > /dev/null\nexit 0\n")
        self.assertEqual(mdlint.guard("Some *text*\n"), "Some *text*")

    def test_flagged_text_is_isolated(self):
        self.failing()
        out = mdlint.guard("Some text")
        self.assertTrue(out.startswith(mdlint.MARK))
        self.assertIn("> - line 1: MD041/first-line-heading", out)
        self.assertIn("```text\nSome text\n```", out)
        self.assertIn("markdownlint flagged 1 problems", (self.tmp / "home" / "factory.log").read_text())

    def test_fence_is_longer_than_any_backtick_run_inside(self):
        self.failing()
        self.assertIn("````text\n```python\nopen\n````", mdlint.guard("```python\nopen"))
        self.assertIn("`````text\n````\nx\n`````", mdlint.guard("````\nx"))

    def test_isolated_text_is_not_wrapped_twice(self):
        self.failing()
        once = mdlint.guard("Some text")
        self.calls.unlink()
        self.assertEqual(mdlint.guard(once), once)
        self.assertFalse(self.calls.exists(), "the linter is not called again")
        self.assertTrue(mdlint.isolated(once))

    def test_isolate_output_is_recognised(self):
        many = [f"p{i}" for i in range(mdlint.MAX_SHOWN + 2)]
        for text in ["x", "```python\nopen", "````\nx", "a\n\nb\n```"]:
            for found in [["p"], [], many]:
                with self.subTest(text=text, found=len(found)):
                    self.assertTrue(mdlint.isolated(mdlint.isolate(text, found)))

    def test_text_that_only_starts_with_the_mark_is_linted(self):
        self.failing()
        cases = [
            mdlint.MARK,
            mdlint.MARK + "\nsome text",
            mdlint.isolate("x", ["p"]) + "\nmore text",
            f"{mdlint.HEADER}\n> - p\n\n```text\nopen",
            f"{mdlint.HEADER}\n> - p\n\n```text\n```\nx\n```",
        ]
        for t in cases:
            with self.subTest(text=t):
                self.assertFalse(mdlint.isolated(t))
                if self.calls.exists():
                    self.calls.unlink()
                out = mdlint.guard(t)
                self.assertTrue(self.calls.exists(), "the linter is called")
                self.assertTrue(mdlint.isolated(out))
                self.assertNotEqual(out, t)

    def test_unchecked_when_the_linter_cannot_run(self):
        text = "Some text"
        os.environ["WONKA_MDLINT"] = "off"
        self.assertEqual(mdlint.guard(text), text)
        os.environ["WONKA_MDLINT"] = " "
        self.assertEqual(mdlint.guard(text), text)
        os.environ["WONKA_MDLINT"] = str(self.tmp / "missing-linter")
        self.assertEqual(mdlint.guard(text), text)
        self.linter("echo 'bad config' >&2\nexit 2\n")
        self.assertEqual(mdlint.guard(text), text)
        self.assertIn("markdownlint skipped: bad config", (self.tmp / "home" / "factory.log").read_text())
        self.linter("sleep 5\n")
        with mock.patch.object(mdlint, "TIMEOUT", 0.1):
            self.assertEqual(mdlint.guard(text), text)

    def test_exit_1_without_output_still_isolates(self):
        self.linter("exit 1\n")
        self.assertIn("> - markdownlint failed without details", mdlint.guard("x"))

    def test_long_problem_lists_are_cut(self):
        n = mdlint.MAX_SHOWN + 3
        self.linter(f"for i in $(seq 1 {n}); do echo \"stdin:$i:2 error MD009/no-trailing-spaces Trailing\" >&2; done\nexit 1\n")
        out = mdlint.guard("x")
        self.assertEqual(out.count("MD009"), mdlint.MAX_SHOWN)
        self.assertIn("line 1: MD009/no-trailing-spaces Trailing", out)
        self.assertIn("> - ... and 3 more", out)

    def test_problem_lines_cannot_break_the_callout(self):
        out = mdlint.isolate("x", ["one\r\ntwo"])
        self.assertIn("> - one two\n", out)

    def test_linter_gets_the_demoted_text(self):
        seen = self.tmp / "seen"
        self.linter(f'cat > "{seen}"\nexit 0\n')
        mdlint.guard("## Heading\ntext")
        self.assertEqual(seen.read_text(), "### Heading\ntext\n")

    def test_default_command_is_markdownlint_via_npx(self):
        os.environ.pop("WONKA_MDLINT", None)
        self.assertEqual(
            mdlint.command(),
            ["npx", "--yes", "markdownlint-cli@0.49.1", "--stdin", "--disable", "MD013", "MD041"],
        )

    def test_override_command_is_used_as_is(self):
        os.environ["WONKA_MDLINT"] = "mylint --stdin"
        self.assertEqual(mdlint.command(), ["mylint", "--stdin"])


if __name__ == "__main__":
    unittest.main()
