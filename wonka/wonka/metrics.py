"""Per-ticket history for `wonka metrics`: pure functions over a ticket's state dict."""
import datetime as dt

DONE = ("shipped/ready", "wontdo/ready")
KEYS = ("id", "project", "size", "created_at", "reviewers", "outcome", "hours_in", "hours_awaiting_human",
        "rounds", "stats", "followups_filed", "pr_url")


def _t(stamp):
    d = dt.datetime.fromisoformat(stamp)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _lv(t):
    lv = t.get("last_valid")
    if isinstance(lv, dict):
        return f"{lv.get('status')}/{lv.get('phase')}"
    return lv


def _moves(t):
    return [h for h in t.get("history", []) if h.get("to")]


def hours(t, now):
    """(hours_in per status, hours waiting on a human, parked Ideas excluded); the final shipped/wontdo state accrues nothing."""
    secs, waiting = {}, 0.0
    hist = t.get("history", [])
    stamp = t.get("created_at") or (hist[0]["at"] if hist else None)
    if not stamp:
        return {}, 0.0
    start = _t(stamp)

    def add(state, a, b):
        nonlocal waiting
        status, _, phase = (state or "").partition("/")
        if not status:
            return
        d = max(0.0, (b - a).total_seconds())
        secs[status] = secs.get(status, 0.0) + d
        if phase == "awaiting" and status != "idea":
            waiting += d

    moves = _moves(t)
    for m in moves:
        at = _t(m["at"])
        add(m.get("from"), start, at)
        start = at
    last = moves[-1]["to"] if moves else _lv(t)
    if last and last not in DONE:
        add(last, start, now)
    return {k: round(v / 3600, 2) for k, v in secs.items()}, round(waiting / 3600, 2)


def outcome(t):
    if _lv(t) == "shipped/ready":
        return "merged"
    kills = [m for m in _moves(t) if m["to"] == "wontdo/ready"]
    if not kills:
        return None
    k = kills[-1]
    why, by = k.get("why") or "", k.get("by")
    if why.startswith("Duplicate of"):
        return "duplicate"
    if why.startswith(("Split by repo", "Too big")):
        return "split"
    if why.startswith("Bad nut"):
        return "bad nut"
    if by == "github":
        return "PR closed"
    if by == "human":
        return "closed by human"
    return why or "killed"


def followups_filed(t, tickets):
    stats = t.get("stats") or {}
    if "followups_filed" in stats:
        return stats["followups_filed"]
    first = next((m["at"] for m in _moves(t) if m["to"].startswith("reviewing/")), None)
    if not first:
        return 0
    return sum(1 for c in t.get("children", [])
               if c in tickets and (tickets[c].get("created_at") or "") >= first)


def ticket_metrics(tid, t, size, tickets, now):
    hin, wait = hours(t, now)
    vals = {"id": tid, "project": t.get("project"), "size": size or None, "created_at": t.get("created_at"),
            "reviewers": t.get("reviewers", []), "outcome": outcome(t), "hours_in": hin,
            "hours_awaiting_human": wait, "rounds": t.get("rounds", []), "stats": t.get("stats", {}),
            "followups_filed": followups_filed(t, tickets), "pr_url": t.get("pr_url")}
    return {k: vals[k] for k in KEYS}
