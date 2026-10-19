"""The golden ticket state machine: which (status, phase) pairs exist and how they may change."""

STATUSES = {
    "idea": "💡 Idea",
    "triage": "📏 Triage",
    "drafting": "🧪 Drafting",
    "planned": "🎫 Planned",
    "baking": "🍫 Baking",
    "reviewing": "🐿️ Reviewing",
    "shipped": "🚢 Shipped",
    "wontdo": "🗑️ Wont Do",
}
PHASES = {
    "pending": "🕰️ Pending",
    "awaiting": "🙋 Awaiting Human",
    "progress": "🎩 In Progress",
    "ready": "🍭 Ready",
}
# Names before WONKA-00100 (ready) and WONKA-00140 (inbox) (old key -> new key); used only to read old notes, input and state files.
OLD_STATUS_KEYS = {"ready": "planned", "inbox": "triage"}
OLD_PHASE_KEYS = {"done": "ready"}
OLD_STATUSES = {"ready": "🎫-Ready", "inbox": "✉️-Inbox"}
OLD_PHASES = {"done": "🌟-Done"}
START = ("idea", "triage")  # where `Factory.add` may start a ticket
IDEA = ("idea", "awaiting")  # an Idea waits only on a human
OLD_STATES = {("idea", "pending"): IDEA}  # (before WONKA-00167) retired state -> its replacement
SIZES = ("xs", "s", "m", "l", "xl")
BUILDABLE = ("xs", "s", "m")
SPEC_REVIEWS = ("agent", "human")
AUTO_READY = ("xs", "s")  # Triage tickets this small need no human approval


def current(state):
    """Map a retired (status, phase) pair to its replacement; any other state comes back unchanged."""
    state = tuple(state)
    return OLD_STATES.get(state, state)


def review_budget(t, max_rounds):
    """Review rounds this ticket may use: the max_review_rounds setting, plus one per clean round that rebaked."""
    return max_rounds + t.get("extra_rounds", 0)


VALID = {
    "idea": {"awaiting"},
    "triage": {"pending", "awaiting", "ready"},
    "drafting": {"pending", "progress", "awaiting"},
    "planned": {"pending"},
    "baking": {"pending", "progress", "ready"},
    "reviewing": {"pending", "progress", "awaiting", "ready"},
    "shipped": {"pending", "progress", "awaiting", "ready"},
    "wontdo": {"ready"},
}

KILLED = ("wontdo", "ready")
MERGED = ("shipped", "ready")

_EDGES = [
    ("idea/awaiting", "triage/pending"),  # a human started the idea
    ("triage/pending", "triage/ready"),
    ("triage/pending", "triage/awaiting"),
    ("triage/awaiting", "triage/ready"),
    ("triage/ready", "drafting/pending"),
    ("drafting/pending", "drafting/progress"),
    ("drafting/progress", "drafting/pending"),  # retry
    ("drafting/progress", "drafting/awaiting"),
    ("drafting/awaiting", "drafting/progress"),
    ("drafting/progress", "planned/pending"),
    ("drafting/awaiting", "planned/pending"),  # a human approved the spec (spec_review: human)
    ("planned/pending", "baking/pending"),
    ("baking/pending", "baking/progress"),
    ("baking/progress", "baking/pending"),  # retry
    ("baking/progress", "baking/ready"),
    ("baking/progress", "shipped/awaiting"),  # the builder judged a red CI flaky: no new code, back to the open PR
    ("baking/ready", "reviewing/pending"),
    ("reviewing/pending", "reviewing/progress"),
    ("reviewing/progress", "reviewing/pending"),  # retry
    ("reviewing/progress", "reviewing/awaiting"),
    ("reviewing/progress", "reviewing/ready"),
    ("reviewing/progress", "baking/pending"),
    ("reviewing/awaiting", "baking/pending"),
    ("reviewing/awaiting", "drafting/pending"),
    ("reviewing/awaiting", "reviewing/ready"),
    ("reviewing/ready", "shipped/pending"),
    ("shipped/pending", "shipped/progress"),
    ("shipped/progress", "shipped/pending"),  # retry
    ("shipped/progress", "shipped/awaiting"),
    ("shipped/awaiting", "shipped/ready"),
    ("shipped/awaiting", "baking/pending"),  # the human requested changes on the PR
    ("shipped/awaiting", "shipped/progress"),  # fixing merge conflicts
]
# the PR was merged on GitHub while Wonka was not waiting on it: a fix round, a fixer, a retried or stuck ship
_EDGES += [(src, "shipped/ready") for src in ("baking/pending", "baking/progress", "baking/ready", "reviewing/pending",
                                              "reviewing/progress", "reviewing/awaiting", "reviewing/ready",
                                              "shipped/pending", "shipped/progress")]
EDGES = {tuple(tuple(side.split("/")) for side in edge) for edge in _EDGES}


def allowed(src, dst):
    """Any ticket may be killed except a finished one (killed or merged: a merged PR can't be undone); everything
    else must be an edge in the table."""
    if dst == KILLED:
        return tuple(src) not in (KILLED, MERGED)
    return (tuple(src), tuple(dst)) in EDGES


def valid(state):
    status, sub = state
    return sub in VALID.get(status, ())


def label(state):
    return STATUSES[state[0]], PHASES[state[1]]


def hyphenated(text):
    """A label as it was written before Wonka 1.18 ("🙋-Awaiting-Human"): the board and old notes may still hold it."""
    return text.replace(" ", "-")


def _clean(value):
    """Lowercase, no emoji variation selector, and hyphens read as spaces: old labels ("🗑️-Wont-Do") and hand-typed
    ones ("wont-do") match the new ones."""
    return " ".join(str(value or "").replace("️", "").replace("-", " ").split()).lower()


def _match(value, table):
    v = _clean(value)
    bare = v.split(" ", 1)[-1] if v[:1] and not v[:1].isalnum() else v  # an old emoji (🌟 Ready) still reads
    for key, text in table.items():
        if v == _clean(text) or bare in (key, _clean(text).split(" ", 1)[-1]):
            return key
    return None


def parse(value, table):
    """Map a board value (with or without its emoji) back to its key, or None. Old names still parse."""
    found = _match(value, table)
    if found is None and table is STATUSES:
        found = OLD_STATUS_KEYS.get(_match(value, OLD_STATUSES))
    elif found is None and table is PHASES:
        found = OLD_PHASE_KEYS.get(_match(value, OLD_PHASES))
    return found


def migrate(db):
    """Rewrite old state keys in a loaded state dict in place; True when anything changed. Idempotent."""
    changed = False

    def fix(status, phase):
        return current((OLD_STATUS_KEYS.get(status, status), OLD_PHASE_KEYS.get(phase, phase)))

    for t in db.get("tickets", {}).values():
        lv = t.get("last_valid")
        if isinstance(lv, dict):
            new = fix(lv.get("status"), lv.get("phase"))
            if new != (lv.get("status"), lv.get("phase")):
                lv["status"], lv["phase"] = new
                changed = True
        for h in t.get("history", []):
            for side in ("from", "to"):
                value = h.get(side)
                if isinstance(value, str) and "/" in value:
                    new = "/".join(fix(*value.split("/", 1)))
                    if new != value:
                        h[side] = new
                        changed = True
    return changed
