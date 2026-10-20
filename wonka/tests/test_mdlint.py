import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wonka import mdlint

MD041 = "stdin:1 error MD041/first-line-heading/first-line-h1 First line in a file should be a top-level heading"

SAMPLES = [
    "Para\n\n- a\n- b\n\nPara",
    "- a\n\n- b\n\n- c",
    "- a\n- b",
    "1. a\n2. b\n\nend",
    "Para\n---\nmore",
    "some text\n2024. was a year",
    "text\n```\nx\n- a\n```\nmore",
    "Para\n1.\n2. b",
]


class SpaceListsTest(unittest.TestCase):
    def test_list_after_paragraph_gets_a_blank_line(self):
        self.assertEqual(mdlint.space_lists("Intro:\n- a\n- b"), "Intro:\n\n- a\n- b")
        self.assertEqual(mdlint.space_lists("Intro:\n1. a\n2. b"), "Intro:\n\n1. a\n2. b")

    def test_paragraph_after_list_gets_a_blank_line(self):
        self.assertEqual(mdlint.space_lists("- a\n- b\nAfter"), "- a\n- b\n\nAfter")
        self.assertEqual(mdlint.space_lists("- a\n### H"), "- a\n\n### H")
        self.assertEqual(mdlint.space_lists("- a\n> q"), "- a\n\n> q")

    def test_lists_inside_code_fences_are_untouched(self):
        for t in ["text\n```\nx\n- a\ny\n```", "text\n~~~\nx\n- a\n~~~", "text\n````\n```\n- a\ny\n````"]:
            with self.subTest(text=t):
                self.assertEqual(mdlint.space_lists(t), t)
        self.assertEqual(mdlint.space_lists("- a\n```\nb\n- c\n```"), "- a\n\n```\nb\n- c\n```")

    def test_nested_and_continued_items_stay_together(self):
        self.assertEqual(mdlint.space_lists("Intro:\n- a\n  more\n  - nested\n    - deeper\n- b"),
                         "Intro:\n\n- a\n  more\n  - nested\n    - deeper\n- b")
        t = "- a\n  ```\n  code\n  ```\n- b"
        self.assertEqual(mdlint.space_lists(t), t)

    def test_lazy_continuation_lines_keep_the_list_whole(self):
        for t in ["- a\nlazy text\n- b", "Intro:\n\n- a\nlazy\nmore lazy\n- b\n\nAfter", "1. a\nlazy\n2. b",
                  "- a\nlazy\n  indented\n- b"]:
            with self.subTest(text=t):
                self.assertEqual(mdlint.space_lists(t), t)
        self.assertEqual(mdlint.space_lists("- a\nAfter\n### H\n- b"), "- a\n\nAfter\n### H\n\n- b")
        self.assertEqual(mdlint.space_lists("- a\nAfter\n\n- b"), "- a\n\nAfter\n\n- b")

    def test_already_correct_markdown_is_unchanged(self):
        for t in SAMPLES:
            with self.subTest(text=t):
                self.assertEqual(mdlint.space_lists(t), t)

    def test_space_lists_is_idempotent(self):
        for t in SAMPLES + ["Intro:\n- a\n- b\nAfter", "- a\n```\nb\n```\nc"]:
            with self.subTest(text=t):
                once = mdlint.space_lists(t)
                self.assertEqual(mdlint.space_lists(once), once)

    def test_empty_ordered_item_does_not_interrupt_a_paragraph(self):
        for t in ["Para\n1.\n2. b", "Para\n1.   \n2. b", "Para\n\n1.\n2. b"]:
            with self.subTest(text=t):
                self.assertEqual(mdlint.space_lists(t), t)
        self.assertEqual(mdlint.space_lists("Para\n1. a\n2. b"), "Para\n\n1. a\n2. b")

    def test_indented_fence_marker_outside_a_list_is_not_a_fence(self):
        for mark in ["```", "~~~"]:
            with self.subTest(mark=mark):
                self.assertEqual(mdlint.space_lists(f"Text\n\n    {mark}\n\nIntro:\n- a\n- b"),
                                 f"Text\n\n    {mark}\n\nIntro:\n\n- a\n- b")
        self.assertEqual(mdlint.space_lists("Text\n\n\t```\n\nIntro:\n- a"), "Text\n\n\t```\n\nIntro:\n\n- a")
        t = "Text\n   ```\n- a\n```"
        self.assertEqual(mdlint.space_lists(t), t)

    def test_indented_fence_inside_a_list_is_still_a_fence(self):
        t = "- a\n\n      ```\n      x\n      ```\n- b"
        self.assertEqual(mdlint.space_lists(t), t)


class FenceTest(unittest.TestCase):
    def test_fence_opener(self):
        for s, want in (("```", "```"), ("````python", "````"), ("~~~ x `y`", "~~~"), ("```x```", None),
                        ("``` a`b", None), ("``", None), ("text", None), (" ```", None)):
            self.assertEqual(mdlint.fence_opener(s), want, s)

    def test_closes_fence(self):
        self.assertTrue(mdlint.closes_fence("```", "```"))
        self.assertTrue(mdlint.closes_fence("```", "  ````  "))
        self.assertFalse(mdlint.closes_fence("````", "```"))
        self.assertFalse(mdlint.closes_fence("```", "~~~"))
        self.assertFalse(mdlint.closes_fence("```", "``` x"))

    def test_space_lists_inline_triple_backticks_is_text(self):
        self.assertEqual(mdlint.space_lists("Intro:\n```x```\n- a"), "Intro:\n```x```\n\n- a")


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
        self.assertIn("````text\n```python\nopen\n```\n````", mdlint.guard("```python\nopen\n```"))
        self.assertIn("`````text\n````\nx\n````\n`````", mdlint.guard("````\nx\n````"))
        self.assertIn("````text\n\\```python\nopen\n````", mdlint.guard("```python\nopen"),
                      "an unclosed fence is escaped by note.demote before it is isolated")

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

    def test_exit_1_without_a_rule_is_a_linter_failure(self):
        self.linter("exit 1\n")
        self.assertEqual(mdlint.guard("x"), "x")
        self.assertIn("markdownlint skipped: exit code 1", (self.tmp / "home" / "factory.log").read_text())

    def test_npm_errors_leave_the_section_unchecked(self):
        for err in ("npm error code ECONNREFUSED\nnpm error errno ECONNREFUSED",
                    "npm error code E404\nnpm error 404 Not Found - GET https://registry.npmjs.org/markdownlint-cli",
                    "npm error code EACCES\nnpm error Your cache folder contains root-owned files"):
            with self.subTest(err=err):
                self.linter(f"cat > /dev/null\nprintf '%s\\n' '{err}' >&2\nexit 1\n")
                self.assertEqual(mdlint.guard("Some text"), "Some text")
                self.assertIn(f"markdownlint skipped: {err.splitlines()[0]}",
                              (self.tmp / "home" / "factory.log").read_text())

    def test_only_rule_lines_are_listed(self):
        self.linter(f'echo "npm warn exec The following package was not found and will be installed" >&2\n'
                    f'echo "{MD041}" >&2\nexit 1\n')
        out = mdlint.guard("Some text")
        self.assertIn("> - line 1: MD041/first-line-heading", out)
        self.assertNotIn("npm warn", out)

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

    def test_guard_lints_the_spaced_text(self):
        seen = self.tmp / "seen"
        self.linter(f'cat > "{seen}"\nexit 0\n')
        self.assertEqual(mdlint.guard("Intro:\n- a"), "Intro:\n\n- a")
        self.assertEqual(seen.read_text(), "Intro:\n\n- a\n")

    def test_other_failures_still_isolate_the_spaced_text(self):
        self.failing()
        out = mdlint.guard("Intro:\n- a")
        self.assertTrue(out.startswith(mdlint.MARK))
        self.assertIn("```text\nIntro:\n\n- a\n```", out)

    def test_unchecked_text_is_still_spaced(self):
        os.environ["WONKA_MDLINT"] = "off"
        self.assertEqual(mdlint.guard("Intro:\n- a"), "Intro:\n\n- a")

    def test_default_command_is_markdownlint_via_npx(self):
        os.environ.pop("WONKA_MDLINT", None)
        self.assertEqual(
            mdlint.command(),
            ["npx", "--yes", "markdownlint-cli@0.49.1", "--stdin", "--disable", *mdlint.RELAXED],
        )

    def test_cosmetic_rules_are_relaxed(self):
        for rule in ("MD038", "MD040", "MD034", "MD018", "MD060", "MD013", "MD041", "MD009"):
            self.assertIn(rule, mdlint.RELAXED)

    def test_structural_rules_stay_checked(self):
        for rule in ("MD005", "MD022", "MD023", "MD025", "MD028", "MD031", "MD032", "MD033", "MD056", "MD058"):
            self.assertNotIn(rule, mdlint.RELAXED)

    def test_relaxed_has_no_duplicates(self):
        self.assertEqual(len(set(mdlint.RELAXED)), len(mdlint.RELAXED))
        for rule in mdlint.RELAXED:
            self.assertRegex(rule, r"^MD\d{3}$")

    def test_override_command_is_used_as_is(self):
        os.environ["WONKA_MDLINT"] = "mylint --stdin"
        self.assertEqual(mdlint.command(), ["mylint", "--stdin"])


if __name__ == "__main__":
    unittest.main()
