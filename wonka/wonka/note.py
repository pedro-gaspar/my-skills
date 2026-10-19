"""Golden ticket notes: frontmatter, ## sections and checkbox questions."""

import json
import re

from . import store

FIELDS = ("id", "title", "status", "phase", "size", "jira", "spec_review")
OPTIONAL = ("spec_review",)  # owned fields written only when set: old notes get no blank line

HEADINGS = {
    "idea": "💡 Idea",
    "approval": "🎫 Approval",
    "questions": "❓ Questions",
    "answers": "✅ Answers",
    "spec": "📜 Spec",
    "toobig": "🗑️ Too big",
    "split": "🗑️ Split by repo",
    "duplicate": "🗑️ Duplicate",
    "review-questions": "🐿️ Review Questions",
    "stuck": "🎩 Stuck",
    "changes": "🔁 Request changes",
    "log": "🎩 Factory log",
}

CLOSE_Q = "🗑️ Close this ticket?"
DRAFT_Q = "🧪 Send it back to Drafting?"
SPEC_Q = "📜 Approve the spec and plan?"
SPEC_OPTIONS = (
    "Approve: send it to baking",
    "Re-draft: back to the analyst (write what to change under this question)",
    "Close: drop the ticket (Wont-Do)",
)
CHANGES_BOX = "- [ ] 🔁 Request changes"

_Q = re.compile(r"^- \*\*(.+?)\*\*")
_OPT = re.compile(r"^\s+- \[([ xX])\] (.*)$")
_APPROVED = re.compile(r"^- \[[xX]\] Approve", re.M)
_RETRY = re.compile(r"^- \[[xX]\] Retry", re.M)
_CHANGES = re.compile(r"^- \[([ xX])\] (?:🔁 )?Request changes.*$", re.M)


class Raw(str):
    """A frontmatter entry Wonka doesn't own (e.g. a tags list), kept byte for byte."""


def read(path):
    text = path.read_text()
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            key, heads, cont = None, {}, {}
            for line in text[4:end].splitlines():
                k, sep, value = line.partition(":")
                if sep and not line.startswith((" ", "\t", "-", "#")):
                    key = k.strip()
                    if key in FIELDS:
                        meta[key], heads[key], cont[key] = "", value.strip(), []
                    else:
                        meta[key] = Raw(line)
                elif key in FIELDS:
                    cont[key].append(line)
                elif key:
                    meta[key] = Raw(meta[key] + "\n" + line)
            for key, head in heads.items():
                meta[key] = _scalar(head, cont[key])
            body = text[end + 4:].lstrip("\n")
    return meta, body


_BLOCK = re.compile(r"^[|>][0-9+-]*(\s+#.*)?$")


def _scalar(head, lines):
    """An owned field's value from its key line's text and the indented continuation lines below it (column-0 ignored)."""
    lines = [line for line in lines if not line.strip() or line[:1] in (" ", "\t")]
    if not _BLOCK.match(head):
        rest = [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]
        return _unquote(" ".join([head, *rest]).strip() if rest else head)
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    first = next((line for line in lines if line.strip()), "")
    indent = len(first) - len(first.lstrip(" \t"))
    lines = [line[indent:] if line.strip() else "" for line in lines]
    if head[0] == "|":
        return "\n".join(lines)
    out, prev_blank = "", True
    for line in lines:
        if not line:
            out += "\n"
        else:
            out += ("" if prev_blank else " ") + line
        prev_blank = not line
    # Chomping (+/-) and indent digits are ignored: owned fields are single-line, so trailing newlines always go.
    return out.strip("\n")


def write(path, meta, body):
    lines = [f"{k}: {_quote(meta.get(k))}".rstrip() for k in FIELDS if k not in OPTIONAL or meta.get(k)]
    lines += [v if isinstance(v, Raw) else f"{k}: {_quote(v)}".rstrip() for k, v in meta.items() if k not in FIELDS]
    store.write_atomic(path, "---\n" + "\n".join(lines) + "\n---\n" + body.rstrip("\n") + "\n")


def _unquote(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return json.loads(v) if v[0] == '"' else v[1:-1].replace("''", "'")
    return "" if v in ("null", "~") else v


def _quote(v):
    v = "" if v is None else str(v)
    risky = v and (
        ": " in v or " #" in v or v.endswith(":")
        or v[0] in "[]{}&*!|>'\"%@`#,?-"
        or v != v.strip() or any(c in v for c in "\t\n\r")
        or v.lower() in ("true", "false", "null", "yes", "no", "~")
    )
    return json.dumps(v, ensure_ascii=False) if risky else v


def _bounds(body, heading):
    m = re.search(rf"^## {re.escape(heading)}[ \t]*$", body, re.M)
    if not m:
        return None
    nxt = re.search(r"^## ", body[m.end():], re.M)
    return m.start(), m.end() + nxt.start() if nxt else len(body)


_HEADING = re.compile(r"^#{1,2}(?= )", re.M)


def demote(content):
    """Section content can't hold # or ## headings: they would end the section early (see _bounds)."""
    return _HEADING.sub("###", content)


def get_section(body, key):
    b = _bounds(body, HEADINGS.get(key, key))
    if not b:
        return None
    return body[b[0]:b[1]].split("\n", 1)[1].strip("\n") if "\n" in body[b[0]:b[1]] else ""


def set_section(body, key, content):
    """Replace a section in place, or add it at the end (but always above the factory log)."""
    heading = HEADINGS.get(key, key)
    block = f"## {heading}\n{demote(content.strip())}\n\n"
    b = _bounds(body, heading)
    if b:
        return body[:b[0]] + block + body[b[1]:].lstrip("\n")
    log = _bounds(body, HEADINGS["log"])
    if log:
        return body[:log[0]] + block + body[log[0]:]
    return body.rstrip("\n") + "\n\n" + block


def drop_section(body, key):
    b = _bounds(body, HEADINGS.get(key, key))
    return body[:b[0]] + body[b[1]:].lstrip("\n") if b else body


_H2 = re.compile(r"^## (.*?)[ \t]*$", re.M)


def unknown_sections(body):
    """[(heading, heading above it or None)] for every ## heading that isn't one of Wonka's.
    Notes written before _demote() existed may hold a section split in two by a ## line in its content."""
    known, out, prev = set(HEADINGS.values()), [], None
    for m in _H2.finditer(body):
        heading = m.group(1)
        if heading not in known:
            out.append((heading, prev))
        prev = heading
    return out


def set_banner(body, callout):
    """The banner (title heading + callout) sits above the first section; only the old one is replaced."""
    first = re.search(r"^## ", body, re.M)
    head = body[:first.start()] if first else body
    lines = head.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].startswith("# "):
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
    if i < len(lines) and lines[i].startswith("> [!"):
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            i += 1
    kept = "\n".join(lines[i:]).strip()
    return callout + "\n\n" + (kept + "\n\n" if kept else "") + (body[first.start():] if first else "")


def render_questions(items):
    """items: [{"q": str, "options": [str], "recommended": int, "why": str}]"""
    out = []
    for i, item in enumerate(items, 1):
        out.append(f"- **Q{i}. {item['q']}**")
        for j, opt in enumerate(item.get("options", [])):
            star = ""
            if j == item.get("recommended"):
                star = " ⭐ recommended" + (f" — {item['why']}" if item.get("why") else "")
            out.append(f"  - [ ] {opt}{star}")
        if item.get("other", True):
            out.append("  - [ ] Other (write below)")
    return "\n".join(out)


def archive_questions(body):
    """Move an answered questions round (any tick or any free text) to the Answers history and drop it;
    an unanswered round, or none, is left as is."""
    old = get_section(body, "questions")
    if old and (any(c for q in questions(old) for c, _ in q["options"]) or _free_text(old)):
        history = get_section(body, "answers") or ""
        n = len(re.findall(r"^### Round \d+", history, re.M)) + 1
        entry = f"### Round {n}\n{old.strip()}"
        body = set_section(drop_section(body, "questions"), "answers", f"{history}\n\n{entry}".strip())
    return body


def new_questions(body, rendered):
    """Open a new questions round; an answered previous round (any tick or any free text) moves to the
    Answers history first."""
    return set_section(archive_questions(body), "questions", rendered)


def questions(section):
    qs = []
    for line in (section or "").splitlines():
        if m := _Q.match(line):
            qs.append({"q": m.group(1), "options": []})
        elif (m := _OPT.match(line)) and qs:
            qs[-1]["options"].append((m.group(1) != " ", m.group(2)))
    return qs


def _free_text(section):
    """True when the section holds a non-blank line that is neither a question nor an option: human text."""
    return any(line.strip() and not _Q.match(line) and not _OPT.match(line)
               for line in (section or "").splitlines())


def all_recommended(section, skip=()):
    """True when every question (except those whose text contains a string in `skip`) has only its
    ⭐ recommended option ticked and no free text written under it."""
    found, cur = False, None  # cur: {"ticked": bool, "ok": bool} for the current non-skipped question
    for line in (section or "").splitlines():
        if m := _Q.match(line):
            if cur is not None and not (cur["ticked"] and cur["ok"]):
                return False
            found = True
            cur = None if any(s in m.group(1) for s in skip) else {"ticked": False, "ok": True}
        elif m := _OPT.match(line):
            if cur is not None and m.group(1) != " ":
                cur["ticked"] = True
                cur["ok"] = cur["ok"] and "⭐ recommended" in m.group(2)
        elif line.strip() and cur is not None:
            cur["ok"] = False
    if cur is not None and not (cur["ticked"] and cur["ok"]):
        return False
    return found


_QNUM = re.compile(r"^Q\d+\. ")


def decisions(section, skip_no=()):
    """Markdown bullets for an answered questions section: each question with its ticked option(s) and any text
    the human wrote under it. Questions whose text contains a string in skip_no are left out when "No" is picked."""
    qs = []  # [{"q": str, "picks": [str], "notes": [str]}]
    for line in (section or "").splitlines():
        if m := _Q.match(line):
            qs.append({"q": _QNUM.sub("", m.group(1)), "picks": [], "notes": []})
        elif m := _OPT.match(line):
            if qs and m.group(1) != " ":
                label, star, why = m.group(2).partition(" ⭐ recommended")
                qs[-1]["picks"].append(f"{label} (recommended{why})" if star else label)
        elif line.strip() and qs:
            qs[-1]["notes"].append(line.strip())
    out = []
    for q in qs:
        if not q["picks"]:
            continue
        if any(s in q["q"] for s in skip_no) and all(p.startswith("No") for p in q["picks"]):
            continue
        out.append(f"- {q['q']} → {'; '.join(q['picks'])}")
        out += [f"  > {demote(n)}" for n in q["notes"]]
    return demote("\n".join(out))


def all_answered(section):
    qs = questions(section)
    return bool(qs) and all(any(c for c, _ in q["options"]) for q in qs)


def picked(section, question):
    """The ticked option texts of the first question whose text contains `question`."""
    for q in questions(section):
        if question in q["q"]:
            return [text for c, text in q["options"] if c]
    return []


def needs_human_review(meta):
    """Fail-safe: only an empty value or "agent" means agent review; anything else (even a typo) means human."""
    return (meta.get("spec_review") or "").strip().lower() not in ("", "agent")


def picked_yes(section, question):
    for q in questions(section):
        if question in q["q"]:
            return any(c and text.startswith("Yes") for c, text in q["options"])
    return False


def approved(section):
    return bool(section and _APPROVED.search(section))


def approved_retry(section):
    return bool(section and _RETRY.search(section))


def requested_changes(section):
    """(ticked, notes) for the 🔁 Request changes section: notes are every line but the checkbox."""
    if not section:
        return False, ""
    m = _CHANGES.search(section)
    ticked = bool(m and m.group(1) != " ")
    lines = (section[:m.start()] + section[m.end():] if m else section).splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return ticked, "\n".join(lines)
