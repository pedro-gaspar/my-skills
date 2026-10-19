"""markdownlint for note sections: a section it flags is kept as raw text so it can't break the rest of the note."""

import os
import re
import shlex
import subprocess

from . import note, store

DEFAULT = "npx --yes markdownlint-cli@0.49.1 --stdin"
TIMEOUT = 60
MARK = "> [!warning] markdownlint flagged this section"
HEADER = f"{MARK}, so it is kept as raw text below"
MAX_SHOWN = 10
_ISOLATED = re.compile(r"\A" + re.escape(HEADER) + r"\n(?:> - [^\n]*\n)*\n(`{3,})text\n(.*)\n\1\Z", re.S)


def command():
    """The linter argv from WONKA_MDLINT (default: markdownlint-cli via npx), or None when disabled."""
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


def guard(text):
    """The section content to write: as is when clean, unchecked or already isolated, isolated when flagged."""
    body = note.demote(text.strip())
    if not body or isolated(body):
        return body
    found = problems(body)
    if not found:
        return body
    store.log(f"markdownlint flagged {len(found)} problems; section kept as raw text")
    return isolate(body, found)
