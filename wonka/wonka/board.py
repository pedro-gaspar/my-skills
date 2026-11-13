"""What the board app calls: the whole board as JSON, dragging a card, and answering a ticket's form."""

from typing import Any

from . import banner, machine, mdlint, note, ticket
from .factory import JOB_AGENTS, Factory, fmt
from .store import WonkaError

Op = dict[str, Any]
# the Oompa-Loompa at work in each state; the review has several squirrels
WORKERS = {state: agent.removeprefix("oompa-loompa-") for agent, state in JOB_AGENTS.items()}
WORKERS[("reviewing", "progress")] = "squirrels"


def data(f: Factory) -> dict[str, Any]:
    return {
        "columns": [{"key": k, "label": v} for k, v in machine.STATUSES.items()],
        "phases": dict(machine.PHASES),
        "tickets": [_card(f, tid) for tid in f.by_priority()],
        "obsidian_synced_at": f.db.get("obsidian_synced_at"),
        "archivable": len(f.archivable()),
    }


def _card(f: Factory, tid: str) -> dict[str, Any]:
    t, (status, phase) = f.ticket(tid), f.state(tid)
    base = {"id": tid, "status": status, "phase": phase, "project": t.get("project"), "pr_url": t.get("pr_url"),
            "manual": bool(t.get("manual")), "stuck": bool(t.get("stuck")), "working": bool(t.get("job_started_at")),
            "worker": WORKERS.get((status, phase)) if t.get("job_started_at") else None,
            "blocked_by": f.waiting_on(tid), "history": t.get("history", [])[-30:]}
    try:
        content = ticket.load(tid)
    except (WonkaError, OSError, ValueError) as e:  # one broken ticket never hides the board
        return {**base, "error": str(e)}
    waiting, dead = f._blockers(tid)
    return {**base, "title": content["meta"].get("title", ""), "size": content["meta"].get("size") or None,
            "priority": machine.parse_priority(content["meta"].get("priority")),
            "spec_review": "human" if note.needs_human_review(content["meta"]) else "agent",
            "jira": content["meta"].get("jira") or None,
            "headline": banner.headline(tid, t, content["meta"], f.cfg, waiting=waiting, dead=dead),
            "drop_targets": drop_targets(f, tid), "sections": [_unwrapped(s) for s in content["sections"]],
            "plan": _plan(f, tid)}


def _plan(f: Factory, tid: str) -> str | None:
    path = f.plan_path(tid)
    return path.read_text() if path.exists() else None


def _unwrapped(section: dict[str, Any]) -> dict[str, Any]:
    """The board renders markdown safely, so it gets a flagged section as written, not the fence kept for Obsidian."""
    blocks = [{**b, "md": mdlint.unwrap(b["md"])} if b["type"] == "text" else b for b in section["blocks"]]
    return {**section, "blocks": blocks}


def drop_targets(f: Factory, tid: str) -> list[str]:
    """The columns a human may drag the card to: what the tick accepts from an Obsidian drag."""
    last, out = f.state(tid), []
    if last == machine.IDEA and not f.ticket(tid).get("manual"):
        out.append("triage")
    if last != machine.KILLED and machine.allowed(last, machine.KILLED):
        out.append("wontdo")
    return out


def drag(f: Factory, tid: str, status: str) -> str:
    if status not in drop_targets(f, tid):
        raise WonkaError(f"{tid} can't be dragged from {fmt(f.state(tid))} to {status}: "
                         f"only to {', '.join(drop_targets(f, tid)) or 'nowhere'}")
    if status == "triage":
        return f._start_idea(tid, "Dragged to Triage on the board")
    f.move(tid, machine.KILLED, "human", "Dragged to Wont Do on the board")
    return f"{tid}: killed by a human"


def answer(f: Factory, tid: str, ops: list[Op]) -> str:
    """Apply the board's form edits to the ticket and write it. The next tick acts on them, as on Obsidian ticks."""
    f.ticket(tid)
    t = ticket.load(tid)
    if not isinstance(ops, list):
        raise WonkaError("answer takes a JSON list of edits")
    for op in ops:
        _apply(t, op)
    f.write(tid, *ticket.to_note(t))
    return f.summary(tid)


def _apply(t: ticket.Ticket, op: Op) -> None:
    kind = op.get("op") if isinstance(op, dict) else None
    if kind == "idea":
        _section(t, note.HEADINGS["idea"])["blocks"] = [{"type": "text", "md": f"{_text(op, 'text').strip()}\n"}]
    elif kind == "check":
        block = _block(t, op)
        target = block if block["type"] == "box" else _option(block, op)
        if not isinstance(op.get("checked"), bool):
            raise WonkaError("check needs \"checked\": true or false")
        target["checked"] = op["checked"]
    elif kind == "notes":
        block = _block(t, op)
        if block["type"] != "question":
            raise WonkaError("notes go under a question")
        block["notes"] = [_escape(line) for line in _text(op, "text").splitlines() if line.strip()]
    else:
        raise WonkaError(f"unknown edit {kind!r}: idea, check or notes")


def _section(t: ticket.Ticket, heading: str) -> dict[str, Any]:
    for s in t["sections"]:
        if s["heading"] == heading:
            return s
    raise WonkaError(f"{t['id']} has no section {heading!r}")


def _block(t: ticket.Ticket, op: Op) -> ticket.Block:
    s = _section(t, str(op.get("section")))
    if s["heading"] not in ticket.INTERACTIVE:
        raise WonkaError(f"{s['heading']} is not a section a human answers")
    i = op.get("block")
    if not isinstance(i, int) or not 0 <= i < len(s["blocks"]) or s["blocks"][i]["type"] == "text":
        raise WonkaError(f"no question or checkbox at block {i} of {s['heading']}")
    return s["blocks"][i]


def _option(block: ticket.Block, op: Op) -> dict[str, Any]:
    j = op.get("option")
    if not isinstance(j, int) or not 0 <= j < len(block["options"]):
        raise WonkaError(f"no option {j} in {block['q']!r}")
    return block["options"][j]


def _text(op: Op, key: str) -> str:
    if not isinstance(op.get(key), str):
        raise WonkaError(f"{op.get('op')} needs \"{key}\" as a string")
    return op[key]


def _escape(line: str) -> str:
    """A note line must stay text: one that reads as a question, checkbox or heading would change the form."""
    line = line.strip()
    return "\\" + line if line.startswith(("-", "#", "*", "+")) else line
