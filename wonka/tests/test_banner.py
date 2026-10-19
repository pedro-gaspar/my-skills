import unittest

from wonka import banner, machine, store


class BannerTest(unittest.TestCase):
    def test_every_legal_state_tells_a_story(self):
        cfg = dict(store.DEFAULT_CONFIG)
        for status, subs in machine.VALID.items():
            for sub in subs:
                t = {"last_valid": {"status": status, "phase": sub}, "project": "app", "history": [],
                     "review_round": 1, "reviewers": ["architect"], "entered_done_at": "2026-10-18T10:00:00+00:00"}
                text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
                self.assertTrue(text.startswith("# 🎫 WONKA-00001\n\n> [!"), (status, sub))
                self.assertIn(banner.QUIPS[(status, sub)], text)

    def test_blocked_by_fact(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "triage", "phase": "pending"}, "project": "app", "history": []}
        self.assertNotIn("⛓️", banner.render("WONKA-00002", t, {"size": "s"}, cfg))
        t["blocked_by"] = ["WONKA-00001"]
        self.assertIn("⛓️ blocked by [[WONKA-00001]]", banner.render("WONKA-00002", t, {"size": "s"}, cfg))

    def test_blocked_headline(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "triage", "phase": "ready"}, "project": "app", "history": []}
        w = ["WONKA-00001"]
        text = banner.render("WONKA-00002", t, {"size": "s"}, cfg, waiting=w)
        self.assertIn("> [!info] ⛓️ Blocked by WONKA-00001", text)
        text = banner.render("WONKA-00002", t, {"size": "s"}, cfg, waiting=w, dead=w)
        self.assertIn("[!warning]", text)
        self.assertIn("Won't do", text)
        t["last_valid"] = {"status": "baking", "phase": "progress"}
        t["worktree"] = "/w"
        self.assertEqual(banner.headline("WONKA-00002", t, {}, cfg, waiting=w), "A builder Oompa-Loompa is baking")
        t["last_valid"] = {"status": "triage", "phase": "ready"}
        self.assertEqual(banner.headline("WONKA-00002", t, {}, cfg), "Ready for the Inventing Room")

    def test_log_is_newest_first_and_folded(self):
        t = {"history": [
            {"at": "2026-10-18T10:00:00+00:00", "from": "triage/pending", "to": "triage/ready", "by": "triage"},
            {"at": "2026-10-18T11:00:00+00:00", "by": "wonka", "why": "Reverted"},
        ]}
        lines = banner.log(t).splitlines()
        self.assertEqual(lines[0], "> [!note]- 2 events")
        self.assertIn("Reverted", lines[1])
        self.assertIn("📏-Triage 🕰️-Pending → 📏-Triage 🌟-Ready", lines[2])

    def test_review_round_uses_the_max_rounds_setting(self):
        cfg = {**store.DEFAULT_CONFIG, "max_review_rounds": 5}
        t = {"last_valid": {"status": "reviewing", "phase": "progress"}, "project": "app", "history": [],
             "review_round": 2, "reviewers": ["architect"], "entered_done_at": "2026-10-18T10:00:00+00:00"}
        text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("review round 2/5", text)

    def test_extra_rounds_extend_the_budget(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "reviewing", "phase": "progress"}, "project": "app", "history": [],
             "review_round": 4, "extra_rounds": 1, "reviewers": ["architect"]}
        self.assertIn("review round 4/4 (1 extra)", banner.render("WONKA-00001", t, {"size": "s"}, cfg))
        t["last_valid"] = {"status": "baking", "phase": "pending"}
        self.assertIn("A fix round after review round 4/4 (1 extra)", banner.render("WONKA-00001", t, {"size": "s"}, cfg))


    def test_fix_round_for_requested_changes(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "baking", "phase": "pending"}, "project": "app", "history": [],
             "review_round": 0, "fixing_changes": True, "reviewers": ["architect"]}
        self.assertIn("changes you requested", banner.render("WONKA-00001", t, {"size": "s"}, cfg))
        t["fixing_changes"] = False
        self.assertNotIn("changes you requested", banner.render("WONKA-00001", t, {"size": "s"}, cfg))

    def test_fix_round_for_failing_ci(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "baking", "phase": "pending"}, "project": "app", "history": [],
             "review_round": 0, "fixing_ci": True, "reviewers": ["architect"]}
        out = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("failing CI checks", out)
        self.assertNotIn("changes you requested", out)
        t["fixing_changes"] = True
        out = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("changes you requested and the failing CI checks", out)

    def test_awaiting_merge_mentions_request_changes(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "shipped", "phase": "awaiting"}, "project": "app", "history": [],
             "review_round": 1, "reviewers": ["architect"], "pr_url": "https://github.com/acme/app/pull/7"}
        self.assertIn("🔁 Request changes", banner.render("WONKA-00001", t, {"size": "s"}, cfg))

    def test_conflict_fix_has_its_own_story(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "shipped", "phase": "progress"}, "project": "app", "history": [],
             "review_round": 1, "conflicts": True, "merge_ref": "origin/main", "branch": "wonka/x", "worktree": "/wt"}
        text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("fixing merge conflicts", text)
        self.assertIn("Rebasing `wonka/x` onto origin/main", text)
        self.assertIn(banner.QUIPS[("shipped", "progress")], text)
        t["last_valid"]["phase"] = "awaiting"
        text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("The PR has merge conflicts", text)
        self.assertIn("picks it up on this tick", text)
        t["conflict_error"] = "try 2/3: boom"
        text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("[!warning]", text)
        self.assertIn("could not start", text)
        self.assertIn("try 2/3: boom", text)
        t.pop("conflict_error")
        t.pop("conflicts")
        self.assertIn("merge the PR", banner.render("WONKA-00001", t, {"size": "s"}, cfg))

    def test_spec_review_story(self):
        cfg = dict(store.DEFAULT_CONFIG)
        t = {"last_valid": {"status": "drafting", "phase": "awaiting"}, "project": "app", "history": [],
             "review_round": 0, "reviewers": [], "spec_review_pending": True}
        text = banner.render("WONKA-00001", t, {"size": "s"}, cfg)
        self.assertIn("review the spec", text)
        self.assertIn("[[WONKA-00001-plan]]", text)
        del t["spec_review_pending"]
        self.assertIn("the analyst has questions", banner.render("WONKA-00001", t, {"size": "s"}, cfg))


if __name__ == "__main__":
    unittest.main()
