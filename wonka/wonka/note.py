"""Golden ticket notes: frontmatter, ## sections and checkbox questions."""

import json
import re

from . import store

FIELDS = ("id", "title", "status", "phase", "size", "jira", "spec_review", "blocked_by")
OPTIONAL = ("spec_review", "blocked_by")  # owned fields written only when set: old notes get no blank line

HEADINGS = {
    "idea": "💡 Idea",
    "start": "💡 Start",
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
    "manual": "🕹️ Manual",
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
MANUAL_Q = "🕹️ Where to next?"
CHANGES_BOX = "- [ ] 🔁 Request changes"
START_BOX = "- [ ] Start (send it to triage)"

_Q = re.compile(r"^- \*\*(.+?)\*\*")
_OPT = re.compile(r"^\s+- \[([ xX])\] (.*)$")
_APPROVED = re.compile(r"^- \[[xX]\] Approve", re.M)
_STARTED = re.compile(r"^- \[[xX]\] Start", re.M)
_RETRY = re.compile(r"^- \[[xX]\] Retry", re.M)
_CHANGES = re.compile(r"^- \[([ xX])\] (?:🔁 )?Request changes.*$", re.M)


class Raw(str):
    """A frontmatter entry Wonka doesn't own (e.g. a tags list), kept byte for byte."""


def read(path):
    """(meta, body) of a note. Bytes that are not UTF-8 read as U+FFFD (what Obsidian shows) and a leading BOM is
    dropped, so a hand edit never stops the factory; frontmatter that still can't be read is a WonkaError naming the note."""
    text = path.read_bytes().decode("utf-8", errors="replace").removeprefix("﻿")
    try:
        return _parse(text)
    except ValueError as e:
        raise store.WonkaError(f"{path}: the frontmatter can't be read ({e})")


_CLOSE = re.compile(r"^---[ \t]*$", re.M)


def _parse(text):
    meta, body = {}, text
    if text.startswith("---\n"):
        end = _CLOSE.search(text, 4)
        if end:
            key, heads, cont = None, {}, {}
            # split on \n only: str.splitlines also splits on \x0c, \x85,   and others, cutting a value short
            for line in text[4:max(4, end.start() - 1)].split("\n"):
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
            body = text[end.end():].lstrip("\n")
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


def _uncomment(v):
    """The value without a trailing ' # comment', which YAML drops: after a quoted value's closing quote, or at a
    plain value's first ' #'."""
    if v[:1] in ("'", '"'):
        i = 1
        while i < len(v):
            if v[0] == '"' and v[i] == "\\":
                i += 2
                continue
            if v[i] == v[0]:
                if v[0] == "'" and v[i + 1:i + 2] == "'":
                    i += 2
                    continue
                rest = v[i + 1:]
                return v[:i + 1] if not rest.strip() or re.match(r"\s+#", rest) else v
            i += 1
        return v
    m = re.search(r"(^|\s)#", v)
    return v[:m.start()].rstrip() if m else v


def _unquote(v):
    v = _uncomment(v)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return _unescape(v[1:-1]) if v[0] == '"' else v[1:-1].replace("''", "'")
    return "" if v in ("null", "~") else v


_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
            "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ", "P": " "}
_ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", re.S)


def _unescape(s):
    """A YAML double-quoted value's escapes, a superset of JSON's (\\xe9, \\e, \\ and more); an unknown one is kept as written."""
    def one(m):
        e = m.group(1)
        if len(e) > 1:
            code = int(e[1:], 16)
            return chr(code) if code <= 0x10FFFF else m.group(0)
        return _ESCAPES.get(e, m.group(0))
    out = _ESCAPE.sub(one, s)
    try:
        return out.encode("utf-16", "surrogatepass").decode("utf-16")  # a JSON-style 🍫 pair is one character
    except UnicodeError:
        return out


# What YAML can't hold as is in a quoted value (controls, line and paragraph separators, surrogates): escaped on write.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  \ud800-\udfff￾￿]")


def _quote(v):
    v = "" if v is None else str(v)
    risky = v and (
        ": " in v or " #" in v or v.endswith(":")
        or v[0] in "[]{}&*!|>'\"%@`#,?-"
        or v != v.strip() or any(c in v for c in "\t\n\r") or _UNSAFE.search(v)
        or v.lower() in ("true", "false", "null", "yes", "no", "~")
    )
    if not risky:
        return v
    return _UNSAFE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", json.dumps(v, ensure_ascii=False))


def one_line(text):
    """text with its line breaks (all that str.splitlines splits on) folded into single spaces: a newline in a title
    or Jira key would break the banner's heading line."""
    return " ".join(part.strip() for part in str(text or "").splitlines() if part.strip())


_OPENER = re.compile(r"(`{3,}|~{3,})(.*)")


def fence_opener(s):
    """The fence marker (3+ backticks or tildes) when `s`, a line with its indent removed, opens a fenced code
    block, else None. A backtick fence's info string may not contain backticks (CommonMark), so ```x``` is inline
    code, not a fence."""
    m = _OPENER.match(s)
    if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
        return None
    return m.group(1)


def closes_fence(fence, s):
    """True when line `s` closes the block `fence` opened: the same character, at least as many, nothing else
    but surrounding whitespace."""
    return re.fullmatch(r"\s*" + re.escape(fence[0]) + "{%d,}\\s*" % len(fence), s) is not None


def _fences(lines):
    """{index of each line inside a closed code fence, its opening and closing lines included}. A fence that never
    closes is not one: its opener is plain text and the lines after it are read as usual."""
    inside, i = set(), 0
    while i < len(lines):
        fence = fence_opener(lines[i].strip())
        end = next((j for j in range(i + 1, len(lines)) if closes_fence(fence, lines[j].strip())), None) \
            if fence else None
        if end is None:
            i += 1
            continue
        inside.update(range(i, end + 1))
        i = end + 1
    return inside


def _h2s(body):
    """[(offset, line)] for each `## ` line of body outside closed code fences: the section headings. A `## ` line
    in a fenced example (a spec's markdown, a CI log) is code, not a heading."""
    lines = body.split("\n")
    fenced, out, at = _fences(lines), [], 0
    for i, line in enumerate(lines):
        if i not in fenced and line.startswith("## "):
            out.append((at, line))
        at += len(line) + 1
    return out


def _bounds(body, heading):
    heads = _h2s(body)
    for k, (at, line) in enumerate(heads):
        if line[3:].rstrip(" \t") == heading:
            return at, heads[k + 1][0] if k + 1 < len(heads) else len(body)
    return None


_HEADING = re.compile(r"^#{1,2}(?= )")


def _escape_fence(line):
    """An unclosed fence opener with a backslash before it: Markdown shows it as text, and it can no longer pair
    with a fence further down the note and hide the sections in between."""
    indent = len(line) - len(line.lstrip())
    return line[:indent] + "\\" + line[indent:]


def demote(content, fenced=False):
    """Section content can't hold # or ## headings: they would end the section early (see _bounds). Lines inside a
    closed code fence stay as written (`# install deps` or `## install` in a bash block), since _bounds skips
    fences. A fence that never closes is not one: the headings after its opener are demoted and the opener is
    escaped (see _escape_fence). With `fenced`, for text that goes into a code block of a file scanned without
    regard to fences (a CI log in the plan), ## lines inside fences are demoted too and nothing is escaped."""
    lines = content.split("\n")
    inside = _fences(lines)
    out = []
    for i, line in enumerate(lines):
        if i in inside:
            out.append(re.sub(r"^##(?= )", "###", line) if fenced else line)
        elif fence_opener(line.strip()) and not fenced:
            out.append(_escape_fence(line))
        else:
            out.append(_HEADING.sub("###", line))
    return "\n".join(out)


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
    """[(heading, heading above it or None)] for every ## heading (outside code fences) that isn't one of Wonka's.
    Notes written before _demote() existed may hold a section split in two by a ## line in its content."""
    known, out, prev = set(HEADINGS.values()), [], None
    for _, line in _h2s(body):
        heading = _H2.match(line).group(1)
        if heading not in known:
            out.append((heading, prev))
        prev = heading
    return out


def set_banner(body, callout):
    """The banner (title heading + callout) sits above the first section; only the old one is replaced."""
    heads = _h2s(body)
    first = heads[0][0] if heads else None
    head = body[:first] if heads else body
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
    return callout + "\n\n" + (kept + "\n\n" if kept else "") + (body[first:] if heads else "")


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


def manual_question(options):
    """The 🕹️ Manual section of a manual ticket: one question, one box per option, no recommendation."""
    return render_questions([{"q": MANUAL_Q, "options": list(options), "other": False}])


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


def started(section):
    return bool(section and _STARTED.search(section))


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
