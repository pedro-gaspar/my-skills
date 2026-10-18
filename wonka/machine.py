"""The golden ticket state machine: which (status, sub-status) pairs exist and how they may change."""

STATUSES = {
    "inbox": "✉️-Inbox",
    "drafting": "🧪-Drafting",
    "ready": "🎫-Ready",
    "baking": "🍫-Baking",
    "reviewing": "🐿️-Reviewing",
    "shipped": "🛗-Shipped",
    "wontdo": "🫐-Wont-Do",
}
SUBS = {
    "pending": "🕰️-Pending",
    "awaiting": "🙋-Awaiting-Human",
    "progress": "🎩-In-Progress",
    "done": "🌟-Done",
}
SIZES = ("xs", "s", "m", "l", "xl")
BUILDABLE = ("xs", "s", "m")
AUTO_DONE = ("xs", "s")  # Inbox tickets this small need no human approval

VALID = {
    "inbox": {"pending", "awaiting", "done"},
    "drafting": {"pending", "progress", "awaiting"},
    "ready": {"pending"},
    "baking": {"pending", "progress", "done"},
    "reviewing": {"pending", "progress", "awaiting", "done"},
    "shipped": {"pending", "progress", "awaiting", "done"},
    "wontdo": {"done"},
}

KILLED = ("wontdo", "done")

_EDGES = [
    ("inbox/pending", "inbox/done"),
    ("inbox/pending", "inbox/awaiting"),
    ("inbox/awaiting", "inbox/done"),
    ("inbox/done", "drafting/pending"),
    ("drafting/pending", "drafting/progress"),
    ("drafting/progress", "drafting/pending"),  # retry
    ("drafting/progress", "drafting/awaiting"),
    ("drafting/awaiting", "drafting/progress"),
    ("drafting/progress", "ready/pending"),
    ("ready/pending", "baking/pending"),
    ("baking/pending", "baking/progress"),
    ("baking/progress", "baking/pending"),  # retry
    ("baking/progress", "baking/done"),
    ("baking/done", "reviewing/pending"),
    ("reviewing/pending", "reviewing/progress"),
    ("reviewing/progress", "reviewing/pending"),  # retry
    ("reviewing/progress", "reviewing/awaiting"),
    ("reviewing/progress", "reviewing/done"),
    ("reviewing/progress", "baking/pending"),
    ("reviewing/awaiting", "baking/pending"),
    ("reviewing/awaiting", "drafting/pending"),
    ("reviewing/done", "shipped/pending"),
    ("shipped/pending", "shipped/progress"),
    ("shipped/progress", "shipped/pending"),  # retry
    ("shipped/progress", "shipped/awaiting"),
    ("shipped/awaiting", "shipped/done"),
]
EDGES = {tuple(tuple(side.split("/")) for side in edge) for edge in _EDGES}


def allowed(src, dst):
    """Any live ticket may be killed; everything else must be an edge in the table."""
    if dst == KILLED:
        return src != KILLED
    return (tuple(src), tuple(dst)) in EDGES


def valid(state):
    status, sub = state
    return sub in VALID.get(status, ())


def label(state):
    return STATUSES[state[0]], SUBS[state[1]]


def _clean(value):
    return str(value or "").replace("️", "").strip().lower()


def parse(value, table):
    """Map a board value (with or without its emoji) back to its key, or None."""
    v = _clean(value)
    for key, text in table.items():
        if v in (key, _clean(text), _clean(text.split("-", 1)[1])):
            return key
    return None
