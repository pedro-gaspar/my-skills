"""The golden ticket state machine: which (status, phase) pairs exist and how they may change."""

STATUSES = {
    "inbox": "✉️-Inbox",
    "drafting": "🧪-Drafting",
    "planned": "🎫-Planned",
    "baking": "🍫-Baking",
    "reviewing": "🐿️-Reviewing",
    "shipped": "🚢-Shipped",
    "wontdo": "🗑️-Wont-Do",
}
PHASES = {
    "pending": "🕰️-Pending",
    "awaiting": "🙋-Awaiting-Human",
    "progress": "🎩-In-Progress",
    "ready": "🌟-Ready",
}
# Names before WONKA-00100 (old key -> new key); used only to read old notes, input and state files.
OLD_STATUS_KEYS = {"ready": "planned"}
OLD_PHASE_KEYS = {"done": "ready"}
OLD_STATUSES = {"ready": "🎫-Ready"}
OLD_PHASES = {"done": "🌟-Done"}
SIZES = ("xs", "s", "m", "l", "xl")
BUILDABLE = ("xs", "s", "m")
AUTO_READY = ("xs", "s")  # Inbox tickets this small need no human approval


def review_budget(t, max_rounds):
    """Review rounds this ticket may use: the max_review_rounds setting, plus one per clean round that rebaked."""
    return max_rounds + t.get("extra_rounds", 0)


VALID = {
    "inbox": {"pending", "awaiting", "ready"},
    "drafting": {"pending", "progress", "awaiting"},
    "planned": {"pending"},
    "baking": {"pending", "progress", "ready"},
    "reviewing": {"pending", "progress", "awaiting", "ready"},
    "shipped": {"pending", "progress", "awaiting", "ready"},
    "wontdo": {"ready"},
}

KILLED = ("wontdo", "ready")

_EDGES = [
    ("inbox/pending", "inbox/ready"),
    ("inbox/pending", "inbox/awaiting"),
    ("inbox/awaiting", "inbox/ready"),
    ("inbox/ready", "drafting/pending"),
    ("drafting/pending", "drafting/progress"),
    ("drafting/progress", "drafting/pending"),  # retry
    ("drafting/progress", "drafting/awaiting"),
    ("drafting/awaiting", "drafting/progress"),
    ("drafting/progress", "planned/pending"),
    ("planned/pending", "baking/pending"),
    ("baking/pending", "baking/progress"),
    ("baking/progress", "baking/pending"),  # retry
    ("baking/progress", "baking/ready"),
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
    return STATUSES[state[0]], PHASES[state[1]]


def _clean(value):
    return str(value or "").replace("️", "").strip().lower()


def _match(value, table):
    v = _clean(value)
    for key, text in table.items():
        if v in (key, _clean(text), _clean(text.split("-", 1)[1])):
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
        return OLD_STATUS_KEYS.get(status, status), OLD_PHASE_KEYS.get(phase, phase)

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
