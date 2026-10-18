"""Golden ticket notes: frontmatter, ## sections and checkbox questions."""

import json
import re

FIELDS = ("id", "title", "status", "sub-status", "size", "jira")

HEADINGS = {
    "idea": "💡 Idea",
    "approval": "🎫 Approval",
    "questions": "❓ Questions",
    "spec": "📜 Spec",
    "toobig": "🫐 Too big",
    "review-questions": "🐿️ Review Questions",
}

CLOSE_Q = "🫐 Close this ticket?"
DRAFT_Q = "🧪 Send it back to Drafting?"

_Q = re.compile(r"^- \*\*(.+?)\*\*")
_OPT = re.compile(r"^\s+- \[([ xX])\] (.*)$")
_APPROVED = re.compile(r"^- \[[xX]\] Approve", re.M)


def read(path):
    text = path.read_text()
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, sep, value = line.partition(":")
                if sep and not line.startswith((" ", "-", "#")):
                    meta[key.strip()] = _unquote(value.strip())
            body = text[end + 4:].lstrip("\n")
    return meta, body


def write(path, meta, body):
    keys = list(FIELDS) + [k for k in meta if k not in FIELDS]
    lines = [f"{k}: {_quote(meta.get(k))}".rstrip() for k in keys if k in meta or k in FIELDS]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n" + body.rstrip("\n") + "\n")


def _unquote(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return json.loads(v) if v[0] == '"' else v[1:-1].replace("''", "'")
    return "" if v in ("null", "~") else v


def _quote(v):
    v = "" if v is None else str(v)
    if v and (": " in v or " #" in v or v[0] in "[]{}&*!|>'\"%@`#,?-" or v.lower() in ("true", "false", "null", "yes", "no")):
        return json.dumps(v, ensure_ascii=False)
    return v


def _bounds(body, heading):
    m = re.search(rf"^## {re.escape(heading)}[ \t]*$", body, re.M)
    if not m:
        return None
    nxt = re.search(r"^## ", body[m.end():], re.M)
    return m.start(), m.end() + nxt.start() if nxt else len(body)


def get_section(body, key):
    b = _bounds(body, HEADINGS.get(key, key))
    if not b:
        return None
    return body[b[0]:b[1]].split("\n", 1)[1].strip("\n") if "\n" in body[b[0]:b[1]] else ""


def set_section(body, key, content):
    heading = HEADINGS.get(key, key)
    block = f"## {heading}\n{content.strip()}\n\n"
    b = _bounds(body, heading)
    if b:
        return body[:b[0]] + block + body[b[1]:].lstrip("\n")
    return body.rstrip("\n") + "\n\n" + block


def drop_section(body, key):
    b = _bounds(body, HEADINGS.get(key, key))
    return body[:b[0]] + body[b[1]:].lstrip("\n") if b else body


def add_note(body, line):
    """One-line factory notes sit in a blockquote above the first section."""
    return f"> 🎩 {line}\n\n" + body


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


def questions(section):
    qs = []
    for line in (section or "").splitlines():
        if m := _Q.match(line):
            qs.append({"q": m.group(1), "options": []})
        elif (m := _OPT.match(line)) and qs:
            qs[-1]["options"].append((m.group(1) != " ", m.group(2)))
    return qs


def all_answered(section):
    qs = questions(section)
    return bool(qs) and all(any(c for c, _ in q["options"]) for q in qs)


def picked_yes(section, question):
    for q in questions(section):
        if question in q["q"]:
            return any(c and text.startswith("Yes") for c, text in q["options"])
    return False


def approved(section):
    return bool(section and _APPROVED.search(section))
