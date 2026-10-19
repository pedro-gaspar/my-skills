"""markdownlint for note sections: a section it flags is kept as raw text so it can't break the rest of the note.
Only rules that can change the note's structure are checked.

Blank lines around lists (MD032) are inserted first, so the common "list right after a paragraph" is fixed, not flagged.
"""

import os
import re
import shlex
import subprocess

from . import note, store

# Style-only rules, disabled because they cannot change the note's block structure. The rules still checked (list
# indent, headings, blockquotes, fences, lists, inline HTML, tables) can make a section spill into the rest of the note.
RELAXED = (
    "MD001", "MD003", "MD004", "MD007", "MD009", "MD010", "MD011", "MD012",
    "MD013", "MD014", "MD018", "MD019", "MD020", "MD021", "MD024", "MD026",
    "MD027", "MD029", "MD030", "MD034", "MD035", "MD036", "MD037", "MD038",
    "MD039", "MD040", "MD041", "MD042", "MD045", "MD046", "MD047", "MD048",
    "MD049", "MD050", "MD051", "MD052", "MD053", "MD054", "MD055", "MD059",
    "MD060",
)
DEFAULT = "npx --yes markdownlint-cli@0.49.1 --stdin --disable " + " ".join(RELAXED)
TIMEOUT = 60
MARK = "> [!warning] markdownlint flagged this section"
HEADER = f"{MARK}, so it is kept as raw text below"
MAX_SHOWN = 10
_ISOLATED = re.compile(r"\A" + re.escape(HEADER) + r"\n(?:> - [^\n]*\n)*\n(`{3,})text\n(.*)\n\1\Z", re.S)


def command():
    """The linter argv from WONKA_MDLINT (default: markdownlint-cli via npx, skipping the style-only rules in RELAXED), or None when disabled."""
    value = os.environ.get("WONKA_MDLINT", DEFAULT)
    if not value.strip() or value.strip().lower() == "off":
        return None
    return shlex.split(value)


def _problem(line):
    line = line.strip()
    if line.startswith("stdin:"):
        line = line[len("stdin:"):]
    m = re.match(r"(\d+)(?::\d+)?\s+(?:error\s+|warning\s+)?(.*)", line)
    return f"line {m.group(1)}: {m.group(2)}" if m else line


def problems(text):
    """The violations markdownlint reports ([] when clean), or None when it could not check."""
    cmd = command()
    if cmd is None:
        return None
    try:
        out = subprocess.run(cmd, input=text if text.endswith("\n") else text + "\n",
                             capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:  # FileNotFoundError is an OSError
        store.log(f"markdownlint skipped: {e}")
        return None
    if out.returncode == 0:
        return []
    if out.returncode == 1:
        lines = [_problem(l) for l in (out.stderr + "\n" + out.stdout).splitlines() if l.strip()]
        return lines or ["markdownlint failed without details"]
    first = next((l for l in out.stderr.splitlines() if l.strip()), f"exit code {out.returncode}")
    store.log(f"markdownlint skipped: {first.strip()}")
    return None


def isolate(text, found):
    """A warning callout listing the problems, then the text in a fence no backtick run inside it can close."""
    shown = [re.sub(r"[\r\n]+", " ", p) for p in found[:MAX_SHOWN]]
    lines = [HEADER, *(f"> - {p}" for p in shown)]
    if len(found) > MAX_SHOWN:
        lines.append(f"> - ... and {len(found) - MAX_SHOWN} more")
    fence = "`" * max(3, max((len(r) for r in re.findall(r"`+", text)), default=0) + 1)
    return "\n".join(lines) + f"\n\n{fence}text\n{text}\n{fence}"


def isolated(text):
    """True only for the exact shape isolate() writes: the callout, then one fence closed on the last line."""
    m = _ISOLATED.match(text)
    return bool(m) and all(len(r) < len(m.group(1)) for r in re.findall(r"`+", m.group(2)))


_ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$")
_BREAK = re.compile(r"^ {0,3}([-*_])([ \t]*\1){2,}[ \t]*$")
_FENCE = re.compile(r" {0,3}")  # outside a list: at most 3 spaces, else indented code or text
_LIST_FENCE = re.compile(r"\s*")  # inside a list item: indented relative to the item


def _item(line):
    """(indent, marker, content) when the line starts a list item, else None (thematic breaks are not items)."""
    m = _ITEM.match(line)
    if not m or _BREAK.match(line):
        return None
    return len(m.group(1)), m.group(2), m.group(3) or ""


# fenced code blocks are told apart the same way everywhere a note is read
fence_opener, closes_fence = note.fence_opener, note.closes_fence


def _fence_end(lines, start, fence):
    """Index of the line closing the fence opened at `start` (the last line when it never closes)."""
    for i in range(start + 1, len(lines)):
        if closes_fence(fence, lines[i]):
            return i
    return len(lines) - 1


def space_lists(text):
    """The text with the blank lines MD032 wants before and after lists; code fences and nested items are left alone."""
    lines = text.split("\n")
    out = []
    in_list, prev, i = False, None, 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            out.append(line)
            prev = "blank"
            i += 1
            continue
        item = _item(line)
        if in_list:
            if line[0] in " \t" or item:
                prev = "list"
            else:
                if prev == "list":
                    out.append("")
                in_list, prev = False, "text"
        elif item and item[0] <= 3:
            number = item[1][:-1]
            interrupts = bool(item[2]) and (item[1] in "-*+" or number == "1")
            if prev in (None, "blank") or interrupts:
                if prev == "text":
                    out.append("")
                in_list, prev = True, "list"
            else:
                prev = "text"
        else:
            prev = "text"
        out.append(line)
        indent = (_LIST_FENCE if in_list else _FENCE).match(line)
        fence = fence_opener(line[indent.end():])
        if fence:
            end = _fence_end(lines, i, fence)
            out.extend(lines[i + 1:end + 1])
            i = end
        i += 1
    return "\n".join(out)


def guard(text):
    """The section content to write: list spacing fixed first, then as is when clean, unchecked or already isolated,
    isolated when flagged."""
    body = note.demote(text.strip())
    if not body or isolated(body):
        return body
    body = space_lists(body)
    found = problems(body)
    if not found:
        return body
    store.log(f"markdownlint flagged {len(found)} problems; section kept as raw text")
    return isolate(body, found)
