import datetime as dt
import unittest

from wonka import metrics

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def at(h):
    return (T0 + dt.timedelta(hours=h)).isoformat(timespec="seconds")


def mv(h, src, dst, by="wonka", why=None):
    e = {"at": at(h), "from": src, "to": dst, "by": by}
    if why:
        e["why"] = why
    return e


def ticket(history, **kw):
    return {"created_at": at(0), "history": history, "last_valid": {"status": "idea", "phase": "pending"}, **kw}


class MetricsTest(unittest.TestCase):
    def test_hours_from_a_fixed_history(self):
        t = ticket([
            mv(1, "triage/pending", "triage/awaiting"),
            {"at": at(2), "by": "wonka", "why": "noise"},
            mv(3, "triage/awaiting", "triage/ready"),
            mv(3, "triage/ready", "drafting/pending"),
            mv(4, "drafting/pending", "drafting/progress"),
            mv(5, "drafting/progress", "drafting/awaiting"),
            mv(6.5, "drafting/awaiting", "planned/pending"),
            mv(7, "planned/pending", "shipped/awaiting"),
            mv(10, "shipped/awaiting", "shipped/ready"),
        ])
        hin, wait = metrics.hours(t, T0 + dt.timedelta(days=30))
        self.assertEqual(hin, {"triage": 3.0, "drafting": 3.5, "planned": 0.5, "shipped": 3.0})
        self.assertEqual(wait, 2.0 + 1.5 + 3.0)

    def test_live_ticket_counts_up_to_now(self):
        t = ticket([mv(1, "baking/progress", "reviewing/awaiting")])
        hin, wait = metrics.hours(t, T0 + dt.timedelta(hours=3))
        self.assertEqual(hin["reviewing"], 2.0)
        self.assertEqual(wait, 2.0)
        self.assertIsNone(metrics.outcome(t))

    def test_no_moves_uses_last_valid(self):
        t = ticket([])
        self.assertEqual(metrics.hours(t, T0 + dt.timedelta(hours=1)), ({"idea": 1.0}, 0.0))

    def test_outcomes(self):
        def kill(by, why=None):
            return ticket([mv(1, "triage/ready", "wontdo/ready", by, why)], last_valid={"status": "wontdo", "phase": "ready"})
        cases = [
            (kill("triage", "Duplicate of WONKA-00002"), "duplicate"),
            (kill("analyst", "Split by repo: a, b"), "split"),
            (kill("analyst", "Too big (xl): ..."), "split"),
            (kill("wonka", "Bad nut: out of rounds"), "bad nut"),
            (kill("github", "PR closed"), "PR closed"),
            (kill("human", "dragged"), "closed by human"),
            (kill("human", "Closed at spec review"), "closed by human"),
            (kill("human"), "closed by human"),
            (kill("wonka", "weird"), "weird"),
            (kill("wonka"), "killed"),
            (ticket([mv(1, "reviewing/ready", "shipped/ready")], last_valid={"status": "shipped", "phase": "ready"}), "merged"),
        ]
        for t, want in cases:
            self.assertEqual(metrics.outcome(t), want)
        late = kill("github", "PR closed")
        late["history"].append({"at": at(2), "by": "wonka", "why": "finished"})
        self.assertEqual(metrics.outcome(late), "PR closed")

    def test_followups_filed_prefers_the_stat_then_falls_back(self):
        kids = {"A": {"created_at": at(1)}, "B": {"created_at": at(6)}}
        t = ticket([mv(5, "baking/progress", "reviewing/pending")], children=["A", "B", "gone"])
        self.assertEqual(metrics.followups_filed(t, kids), 1)
        t["stats"] = {"followups_filed": 2}
        self.assertEqual(metrics.followups_filed(t, kids), 2)
        self.assertEqual(metrics.followups_filed(ticket([], children=["B"]), kids), 0)

    def test_key_order_and_defaults(self):
        m = metrics.ticket_metrics("X-1", ticket([]), "", {}, T0)
        self.assertEqual(list(m), list(metrics.KEYS))
        self.assertEqual((m["rounds"], m["stats"], m["reviewers"], m["size"]), ([], {}, [], None))


if __name__ == "__main__":
    unittest.main()
