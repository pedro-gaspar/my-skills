"""The top-of-note callout that says, in plain words, where a golden ticket is and what happens next."""

import datetime as dt

from . import machine

ARCHIVE_DAYS_KEY = "archive_after_days"


QUIPS = {
    ("inbox", "pending"): "🎫 “I've got a golden ticket!”",
    ("inbox", "awaiting"): "🎩 “The suspense is terrible. I hope it'll last.”",
    ("inbox", "done"): "🎶 “We are the music makers, and we are the dreamers of dreams.”",
    ("drafting", "pending"): "🚪 “Little surprises around every corner, but nothing dangerous.”",
    ("drafting", "progress"): "🧪 “Invention is 93% perspiration, 6% electricity, 4% evaporation and 2% butterscotch ripple.”",
    ("drafting", "awaiting"): "🤔 “Strike that. Reverse it.” …or don't. Your call.",
    ("ready", "pending"): "🍫 “Everything in this room is eatable.” Almost.",
    ("baking", "pending"): "⏳ “Patience, my dear. A little nonsense now and then is relished by the wisest men.”",
    ("baking", "progress"): "🎶 Oompa-Loompa, doompadee-doo…",
    ("baking", "done"): "🎶 …I've got another puzzle for you.",
    ("reviewing", "pending"): "🐿️ The squirrels are cracking their knuckles.",
    ("reviewing", "progress"): "🐿️ Tap, tap, tap. Good nut or bad nut?",
    ("reviewing", "awaiting"): "🐿️ “Don't forget what happened to the man who suddenly got everything he wanted.”",
    ("reviewing", "done"): "🥜 A good nut!",
    ("shipped", "pending"): "🛗 “Up and out!”",
    ("shipped", "progress"): "🛗 Pressing the button marked UP AND OUT…",
    ("shipped", "awaiting"): "🛗 “We have so much time and so little to see.” Strike that, reverse it: merge it.",
    ("shipped", "done"): "✨ “So shines a good deed in a weary world.”",
    ("wontdo", "done"): "🫐 “Violet, you're turning violet, Violet!”",
}
STUCK_QUIP = "🌊 Augustus is stuck in the chocolate pipe again."


def _round(t):
    return f"review round {t['review_round']}/3" if t.get("review_round") else ""


def _squirrels(t):
    return ", ".join(t.get("reviewers") or ["architect"])


def _archive_day(t, cfg):
    if not t.get("entered_done_at"):
        return ""
    day = dt.datetime.fromisoformat(t["entered_done_at"]) + dt.timedelta(days=cfg[ARCHIVE_DAYS_KEY])
    return f" Moves to the archive on {day:%Y-%m-%d}."


def _last_why(t):
    return next((h["why"] for h in reversed(t["history"]) if h.get("why")), "")


def _story(tid, t, meta, cfg):
    """(callout type, headline, what is going on / what to do)"""
    s = (t["last_valid"]["status"], t["last_valid"]["phase"])
    size = meta.get("size") or "?"
    wip = cfg["wip"]
    if t.get("stuck"):
        return "warning", "🙋 Your turn: the factory is stuck", (
            "The last jobs for this ticket did not finish (details in `~/.wonka/factory.log`). "
            "Fix the cause, then tick **Retry** in 🎩 Stuck below.")
    return {
        ("inbox", "pending"): ("abstract", "Waiting for Grandpa Joe", "👴 He will size this ticket on the next tick."),
        ("inbox", "awaiting"): ("question", "🙋 Your turn: approve it", (
            f"Grandpa Joe sized it **{size}**. Tick **Approve** and answer every question (⭐ is the recommendation) "
            "to send it to Drafting." if size not in machine.AUTO_DONE else
            "Grandpa Joe needs answers first: tick one option per question (⭐ is the recommendation).")),
        ("inbox", "done"): ("info", "Ready for the Inventing Room",
                            f"🧪 Charlie picks it up as soon as Drafting has room ({wip['drafting']} at a time)."),
        ("drafting", "pending"): ("info", "Next in line for Charlie", "🧪 Drafting starts on the next tick."),
        ("drafting", "progress"): ("info", "Charlie is drafting",
                                   "🧪 Studying the code and writing the 📜 Spec and the build plan. Nothing to do."),
        ("drafting", "awaiting"): ("question", "🙋 Your turn: Charlie has questions", (
            "Tick one option per question in ❓ Questions (⭐ is the recommendation). Write under a question to add "
            "detail. Drafting resumes once every question has a tick.")),
        ("ready", "pending"): ("info", "Spec done, waiting for an oven",
                               f"🍫 Baking starts when an Oompa-Loompa is free ({wip['baking']} at a time)."),
        ("baking", "pending"): ("info", "Queued for baking", (
            f"🍫 A fix round after {_round(t)}." if t.get("review_round") else "🍫 Baking starts on the next tick.")),
        ("baking", "progress"): ("info", "An Oompa-Loompa is baking",
                                 f"🍫 Building in `{t.get('worktree')}` on `{t.get('branch')}`. Nothing to do."),
        ("baking", "done"): ("info", "Baked", "🐿️ Off to the squirrels."),
        ("reviewing", "pending"): ("info", "Waiting for the squirrels", f"🐿️ Reviewers: {_squirrels(t)}."),
        ("reviewing", "progress"): ("info", "The squirrels are reviewing",
                                    f"🐿️ {_squirrels(t)} are checking the code ({_round(t)}). Nothing to do."),
        ("reviewing", "awaiting"): ("question", "🙋 Your turn: the squirrels have questions", (
            f"Tick one option per question in 🐿️ Review Questions ({_round(t)}). Then it goes back to baking, "
            "back to drafting, or is closed, as you choose.")),
        ("reviewing", "done"): ("success", "A good nut", "🛗 Off to ship."),
        ("shipped", "pending"): ("info", "Opening the pull request", "🛗 On the next tick."),
        ("shipped", "progress"): ("info", "Opening the pull request", "🛗 Pushing the branch and opening the PR."),
        ("shipped", "awaiting"): ("question", "🙋 Your turn: merge the PR", (
            f"Review and merge {t.get('pr_url')}. Wonka sees the merge on the next tick and cleans up the worktree.")),
        ("shipped", "done"): ("success", "Shipped 🎉", f"Merged: {t.get('pr_url')}.{_archive_day(t, cfg)}"),
        ("wontdo", "done"): ("failure", "Rolled away 🫐", f"{_last_why(t) or 'Will not be done.'}{_archive_day(t, cfg)}"),
    }[s]


def headline(tid, t, meta, cfg):
    return _story(tid, t, meta, cfg)[1]


def render(tid, t, meta, cfg):
    kind, headline, text = _story(tid, t, meta, cfg)
    s = (t["last_valid"]["status"], t["last_valid"]["phase"])
    facts = [f"{' · '.join(machine.label(s))}", f"size **{meta.get('size') or '?'}**", f"`{t['project']}`"]
    if t.get("parent"):
        facts.append(f"parent [[{t['parent']}]]")
    if t.get("plan"):
        facts.append(f"plan [[{tid}-plan]]")
    if t.get("pr_url"):
        facts.append(f"[PR]({t['pr_url']})")
    if meta.get("jira"):
        facts.append(f"Jira {meta['jira']}")
    quip = STUCK_QUIP if t.get("stuck") else QUIPS[s]
    title = f"# 🎫 {meta.get('title') or tid}"
    return f"{title}\n\n> [!{kind}] {headline}\n> {text}\n> *{quip}*\n> \n> {' · '.join(facts)}"


def log(t):
    """The ticket's history, newest first, folded away at the bottom of the note."""
    lines = []
    for h in reversed(t["history"]):
        at = dt.datetime.fromisoformat(h["at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        if "to" in h:
            src, dst = (tuple(x.split("/")) for x in (h["from"], h["to"]))
            what = f"{' '.join(machine.label(src))} → {' '.join(machine.label(dst))}"
        else:
            what = ""
        why = h.get("why", "")
        lines.append(f"> - {at} · {h['by']} · {what}{': ' if what and why else ''}{why}")
    return f"> [!note]- {len(lines)} event{'' if len(lines) == 1 else 's'}\n" + "\n".join(lines)
