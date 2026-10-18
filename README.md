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

Then run `/wonka init`. It asks for your vault and how many tickets to draft and bake at once. It writes
`~/.wonka/config.json`, creates `<vault>/wonka/{golden-tickets,plans,archive}` and `Wonka.base`, links `wonka` into
`~/.local/bin`, and runs `wonka doctor`. The doctor prints the permission snippet that lets background agents run
unattended (`Bash(wonka *)`, `Bash(git *)`, plus the worktree and board folders as `additionalDirectories`).

## Use

| | |
|---|---|
| `/wonka add "idea"` | drop a ticket in the Inbox (run it from the target repo; `--pr-repo owner/name`, `--jira`) |
| `/wonka tick` | one turn of the factory: fix the board, act on your ticks, merge checks, dispatch agents |
| `/loop /wonka tick` | keep the factory running |
| `/wonka status` | one line per ticket |
| `wonka config [key value]` | show or change settings: `wip.drafting`, `wip.baking`, `archive_after_days`, `stale_hours` |
| `wonka doctor` | check git, gh, board, state and Claude Code permissions |

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

Phases: 🕰️-Pending · 🙋-Awaiting-Human · 🎩-In-Progress · 🌟-Done. Done tickets and their plans move to
`wonka/archive/` after 7 days.

## Reading a ticket

Every note opens with a banner that says where the ticket is, what happens next and, when it's 🙋 your turn, exactly
what to tick. It also lists size, project, plan, PR and review round. The board's **🙋 Your turn** view lists only
tickets that wait for you. At the bottom, a folded **🎩 Factory log** holds the history. A job that fails 3 times
shows **🎩 Stuck** with a Retry checkbox; details are in `~/.wonka/factory.log`.

**The state machine** (`wonka/machine.py`) is the only way a ticket moves. Drag a card anywhere other than Wont-Do
and the next tick puts it back, with a note on the ticket. Notes keep only `id, title, status, phase, size,
jira`. Everything else (repo, PR repo, branch, worktree, PR, rounds, history) is in
`~/.wonka/golden_ticket_status.json`.

## Develop

```bash
python3 -m unittest discover tests
```
