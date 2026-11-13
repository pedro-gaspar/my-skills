"""Golden tickets as JSON in ~/.wonka/tickets: the source of truth. Obsidian notes are rendered from them.

A ticket is the note split into data: owned frontmatter fields, raw frontmatter lines Wonka doesn't own, the text
above the first section, and the ## sections. Sections a human answers hold typed blocks (questions, checkboxes,
text) so the board can render them as a form; the others hold their markdown as one text block."""

import json
import re
import time
from pathlib import Path
from typing import Any

from . import note, store

INTERACTIVE = {note.HEADINGS[k] for k in ("start", "approval", "questions", "toobig", "split", "duplicate",
                                          "review-questions", "stuck", "changes", "manual")}
STAR = " ⭐ recommended"
_BOX = re.compile(r"^- \[([ xX])\] (.*)$")
_Q = re.compile(r"^- \*\*(.+?)\*\*$")
_OPT = re.compile(r"^\s+- \[([ xX])\] (.*)$")
Block = dict[str, Any]
Ticket = dict[str, Any]


def tickets_dir() -> Path:
    return store.home() / "tickets"


def path(tid: str) -> Path:
    return tickets_dir() / f"{tid}.json"


def exists(tid: str) -> bool:
    return path(tid).exists()


def load(tid: str) -> Ticket:
    try:
        return json.loads(path(tid).read_text())
    except FileNotFoundError:
        raise store.WonkaError(f"{tid} has no ticket JSON at {path(tid)}: import its note with "
                               "wonka import-notes") from None


def save(t: Ticket) -> None:
    tickets_dir().mkdir(parents=True, exist_ok=True)
    store.write_atomic(path(t["id"]), json.dumps(t, ensure_ascii=False, indent=1) + "\n")
    bump_version()


def bump_version() -> None:
    """The board watches this file and refetches when it changes."""
    store.write_atomic(tickets_dir() / ".version", f"{time.time_ns()}\n")


def from_note(tid: str, meta: dict[str, Any], body: str) -> Ticket:
    owned = {k: v for k, v in meta.items() if not isinstance(v, note.Raw)}
    extra = {k: str(v) for k, v in meta.items() if isinstance(v, note.Raw)}
    heads = note._h2s(body)
    preamble = body[:heads[0][0]] if heads else body
    sections: list[dict[str, Any]] = []
    for k, (at, line) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(body)
        heading = line[3:].rstrip(" \t")
        content = body[at + len(line) + 1:end] if at + len(line) < end else ""
        sections.append({"heading": heading, "blocks": parse_blocks(content) if heading in INTERACTIVE
                         else [{"type": "text", "md": content}]})
    return {"id": tid, "meta": owned, "extra": extra, "preamble": preamble, "sections": sections}


def to_note(t: Ticket) -> tuple[dict[str, Any], str]:
    meta = {**t["meta"], **{k: note.Raw(v) for k, v in t.get("extra", {}).items()}}
    body = t["preamble"] + "".join(f"## {s['heading']}\n{render_blocks(s['blocks'])}" for s in t["sections"])
    return meta, body


def parse_blocks(content: str) -> list[Block]:
    blocks: list[Block] = []
    for line in content.split("\n"):
        q, opt, box = _Q.match(line), _OPT.match(line), _BOX.match(line)
        last = blocks[-1] if blocks else None
        if q:
            blocks.append({"type": "question", "q": q.group(1), "lead": [], "options": [], "notes": []})
        elif opt and last and last["type"] == "question":
            _settle_notes(last)
            last["options"].append(_option(opt.group(1) != " ", opt.group(2)))
        elif box:
            blocks.append({"type": "box", "text": box.group(2), "checked": box.group(1) != " "})
        elif last and last["type"] == "question" and line.strip():
            last["notes"].append(line)
        elif last and last["type"] == "text":
            last["md"] += "\n" + line
        else:
            blocks.append({"type": "text", "md": line})
    return blocks


def _settle_notes(q: Block) -> None:
    """Text written above an option belongs to the option before it (or above the first): a question's own notes
    are the lines after its last option, where the board writes them."""
    if q["notes"]:
        (q["options"][-1]["after"] if q["options"] else q["lead"]).extend(q["notes"])
        q["notes"] = []


def _option(checked: bool, text: str) -> dict[str, Any]:
    label, star, why = text.partition(STAR)
    if star and (not why or why.startswith(" — ")):
        return {"text": label, "checked": checked, "recommended": True, "why": why.removeprefix(" — "), "after": []}
    return {"text": text, "checked": checked, "recommended": False, "why": "", "after": []}


def render_blocks(blocks: list[Block]) -> str:
    return "\n".join(_render(b) for b in blocks)


def _render(b: Block) -> str:
    if b["type"] == "text":
        return b["md"]
    if b["type"] == "box":
        return f"- [{'x' if b['checked'] else ' '}] {b['text']}"
    lines = [f"- **{b['q']}**", *b["lead"]]
    for o in b["options"]:
        star = (STAR + (f" — {o['why']}" if o["why"] else "")) if o["recommended"] else ""
        lines += [f"  - [{'x' if o['checked'] else ' '}] {o['text']}{star}", *o["after"]]
    return "\n".join(lines + b["notes"])
