# 🎩 Wonka

*Come with me, and you'll be in a world of pure imagination.* 🍫

A slim software factory for Claude Code. Every idea is a **golden ticket**, a note on an Obsidian Bases kanban.
Film characters draft, bake, review and ship it as a PR. You steer by ticking checkboxes.

## Install

Needs Python 3.11+ (stdlib only), git, `gh` (logged in) and Obsidian 1.14+ (Bases kanban view).

```bash
claude plugin marketplace add ~/Work/wonka
claude plugin install wonka@wonka
```

Then run `/wonka init --vault ~/Work/CaptainsLog` once. It writes `~/.wonka/config.json`, creates
`<vault>/wonka/{tickets,plans,archive}` and `Wonka.base`, and links `wonka` into `~/.local/bin`. To let background
agents call the CLI without prompting, allow `Bash(wonka:*)` in your Claude Code settings.

## Use

| | |
|---|---|
| `/wonka add "idea"` | drop a ticket in the Inbox (run it from the target repo; `--pr-repo owner/name`, `--jira`) |
| `/wonka tick` | one turn of the factory: fix the board, act on your ticks, merge checks, dispatch agents |
| `/loop /wonka tick` | keep the factory running |
| `/wonka status` | one line per ticket |

## The floor

| Column | What happens |
|---|---|
| ✉️-Inbox | 👴 **grandpa-joe** (Sonnet) sizes it. xs/s with a clear idea → 🌟 Done. m/l/xl or unclear → 🙋, tick **Approve** and answer the questions |
| 🧪-Drafting | 🧪 **charlie** (Opus) writes `## 📜 Spec` and a detailed plan in `wonka/plans/`, asks `## ❓ Questions` if needed, splits l/xl into new Inbox tickets (`## 🫐 Too big`, then Wont-Do) |
| 🎫-Ready | waits for a Baking slot |
| 🍫-Baking | 🍫 **oompa-loompa** (Opus) builds in `~/.wonka/worktrees/<project>/<id>` and commits |
| 🐿️-Reviewing | 🐿️ **squirrel-architect / -backend / -frontend / -security** (Opus, picked by Charlie). Issues → back to Baking; follow-ups → new Inbox tickets; decisions → `## 🐿️ Review Questions` (incl. close the ticket / back to Drafting). Max 3 rounds, then Wont-Do |
| 🛗-Shipped | the CLI pushes and opens the PR → 🙋 approve and merge it → 🌟 Done, worktree removed |
| 🫐-Wont-Do | killed, split or closed. You can drag any card here |

Sub-statuses: 🕰️-Pending · 🙋-Awaiting-Human · 🎩-In-Progress · 🌟-Done. Done tickets move to `wonka/archive/`
after 7 days. WIP: 1 Drafting + 2 Baking (`~/.wonka/config.json`).

**The state machine** (`wonka/machine.py`) is the only way a ticket moves. Drag a card anywhere other than Wont-Do
and the next tick puts it back, with a note on the ticket. Notes keep only `id, title, status, sub-status, size,
jira`. Everything else (repo, PR repo, branch, worktree, PR, rounds, history) is in
`~/.wonka/golden_ticket_status.json`.

## Develop

```bash
python3 -m unittest discover tests
```
