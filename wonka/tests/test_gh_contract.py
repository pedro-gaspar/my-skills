"""Contract test against the real `gh`: the fake gh in the other tests prints what Wonka expects, so this checks the
real one still prints it. It needs the network and a logged-in gh, so it is skipped unless WONKA_GH_CONTRACT=1:

    cd wonka && WONKA_GH_CONTRACT=1 python3 -m unittest tests.test_gh_contract -v

It reads a public, merged PR (cli/cli#9000, which has reviews, inline review comments and conversation comments) with
the same gh calls factory.py and prcomments.py make, and checks every field Wonka reads is there with the right type.
Run it after a gh upgrade, or when a test's fake gh output is changed. `gh run view --log-failed`, `gh pr create` and
`gh pr edit` print plain text, so they are not covered."""

import datetime as dt
import json
import os
import unittest
from unittest import mock

from wonka import factory, prcomments

REPO, NUMBER = "cli/cli", "9000"
URL = f"https://github.com/{REPO}/pull/{NUMBER}"
OPT_STR = (str, type(None))
OPT_INT = (int, type(None))


@unittest.skipUnless(os.environ.get("WONKA_GH_CONTRACT") == "1", "set WONKA_GH_CONTRACT=1 to run against the real gh")
class GhContractTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"WONKA_GH": "gh"})  # the real gh, whatever the other tests set
        patcher.start()
        self.addCleanup(patcher.stop)

    def fields(self, obj, spec, what):
        """Each key of spec is in obj with a value of the given type (a type or a tuple of types)."""
        self.assertIsInstance(obj, dict, what)
        for key, kind in spec.items():
            self.assertIn(key, obj, f"{what}: no {key!r}")
            self.assertIsInstance(obj[key], kind, f"{what}.{key} is {type(obj[key]).__name__}")

    def stamp(self, value, what):
        self.assertIsInstance(dt.datetime.fromisoformat(value), dt.datetime, what)

    def test_pr_state(self):
        """_pr_state: gh pr view --json state -q .state"""
        state = factory.gh("pr", "view", URL, "--json", "state", "-q", ".state").split()
        self.assertEqual(state, ["MERGED"])

    def test_pr_list_url_and_body(self):
        """_own_prs: gh pr list --json url,body (here merged PRs, as the head branch of an open one changes)"""
        prs = factory.gh_json("pr", "list", "--repo", REPO, "--state", "merged", "--limit", "3",
                              "--json", "url,body", default=[])
        self.assertTrue(prs)
        for pr in prs:
            self.fields(pr, {"url": str, "body": str}, "pr list item")

    def test_pr_view_comments_and_reviews(self):
        """_pr_feedback: gh pr view --json comments,reviews"""
        view = factory.gh_json("pr", "view", URL, "--json", "comments,reviews", default={})
        self.fields(view, {"comments": list, "reviews": list}, "pr view")
        self.assertTrue(view["comments"] and view["reviews"])
        for c in view["comments"]:
            self.fields(c, {"author": dict, "authorAssociation": str, "body": str, "createdAt": str}, "comment")
            self.fields(c["author"], {"login": str}, "comment.author")
            self.stamp(c["createdAt"], "comment.createdAt")
        for r in view["reviews"]:
            self.fields(r, {"author": dict, "authorAssociation": str, "body": str, "submittedAt": str, "state": str},
                        "review")
            self.fields(r["author"], {"login": str}, "review.author")
            self.stamp(r["submittedAt"], "review.submittedAt")

    def test_pr_view_merge_state_and_checks(self):
        """the awaiting-merge check: gh pr view --json state,mergeable,baseRefName,headRefOid,statusCheckRollup"""
        raw = factory.gh_json("pr", "view", URL, "--json", "state,mergeable,baseRefName,headRefOid,statusCheckRollup",
                              default={})
        self.fields(raw, {"state": str, "mergeable": str, "baseRefName": str, "headRefOid": str,
                          "statusCheckRollup": list}, "pr view")
        for item in raw["statusCheckRollup"]:
            self.assertIn(item.get("__typename"), ("CheckRun", "StatusContext"), item)
            if item["__typename"] == "StatusContext":
                self.fields(item, {"context": str, "state": str, "targetUrl": OPT_STR, "startedAt": OPT_STR},
                            "status context")
            else:
                self.fields(item, {"name": str, "status": str, "conclusion": OPT_STR, "detailsUrl": OPT_STR,
                                   "completedAt": OPT_STR, "startedAt": OPT_STR, "workflowName": OPT_STR}, "check run")
        out = factory.gh("pr", "view", URL, "--json", "state,mergeable,baseRefName,headRefOid,statusCheckRollup",
                         "-q", '.state + " " + .mergeable + " " + .baseRefName + " " + .headRefOid + " " '
                               '+ (.statusCheckRollup | tojson)')
        parts = out.split(maxsplit=4)
        self.assertEqual(parts[:4], [raw["state"], raw["mergeable"], raw["baseRefName"], raw["headRefOid"]])
        self.assertEqual(json.loads(parts[4]), raw["statusCheckRollup"])

    def test_api_pages(self):
        """_pr_feedback and wonka pr-comments: gh api --paginate on the PR's review comments, reviews and comments"""
        for kind, path, stamp in prcomments.SOURCES:
            items = factory._json_pages(factory.gh("api", "--paginate",
                                                   f"repos/{REPO}/{path.format(n=NUMBER)}?per_page=100"))
            self.assertTrue(items, path)
            for c in items:
                spec = {"user": dict, "author_association": str, "body": str, stamp: str, "html_url": str}
                if kind == "review":
                    spec["state"] = str
                if kind == "review_comment":
                    spec.update(path=str, line=OPT_INT, original_line=OPT_INT)
                self.fields(c, spec, path)
                self.fields(c["user"], {"login": str, "type": str}, f"{path} user")
                self.stamp(c[stamp], f"{path} {stamp}")
        rows = prcomments.pr_comments(REPO, NUMBER)
        self.assertTrue(rows, "PR 9000 has comments by cli/cli members")
        self.assertTrue(all(r["author_association"] in factory.TRUSTED for r in rows))


if __name__ == "__main__":
    unittest.main()
