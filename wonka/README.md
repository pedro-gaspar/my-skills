# 🎩 Wonka

*Come with me, and you'll be in a world of pure imagination.* 🍫

A slim software factory for Claude Code. Every idea is a **golden ticket**, a note on an Obsidian Bases kanban.
🎩 **Wonka** (the `/wonka` skill) runs the floor, and his **Oompa-Loompas** (triage, analyst, builder, fixer) draft and bake each
ticket, the Nut Room **squirrels** test it (good nut or bad nut?), then Wonka ships it as a PR. You steer by ticking checkboxes.

## Install

Needs Python 3.11+ (stdlib only), git, `gh` (logged in) and Obsidian 1.14+ (Bases kanban view).

```bash
claude plugin marketplace add ~/Work/my-skills
claude plugin install wonka@my-skills
```

Then run `/wonka init`. It asks for your vault and how many tickets to draft and bake at once. It writes
`~/.wonka/config.json`, creates `<vault>/wonka/{golden-tickets,plans,archive}` and `Wonka.base`, links `wonka` into
`~/.local/bin`, and runs `wonka doctor`. The doctor prints the permission snippet that lets background agents run
unattended (`Bash(wonka *)`, plus the worktree and board folders as `additionalDirectories`). Agents run git only
through `wonka git <id> <subcommand>`, which pins it to the ticket's worktree and allows only status, diff, log, show,
rev-parse, ls-files, blame, add and commit (plus the fixer's `merge --no-edit <ref>` and `merge --abort`; no `-c`,
config, push or options that run programs), so no git permission rule is needed; the doctor warns if an old
`Bash(git *)` rule is still there.

## Use

| | |
|---|---|
| `/wonka add "idea"` | drop a ticket in the Inbox (run it from the target repo; `--pr-repo owner/name`, `--jira`) |
| `/wonka tick` | one turn of the factory: fix the board, act on your ticks, merge checks, dispatch agents |
| `/loop /wonka tick` | keep the factory running |
| `/wonka status` | one line per ticket |
| `wonka config [key value]` | show or change settings: `wip.drafting`, `wip.baking` (see [WIP limits](#wip-limits)), `archive_after_days`, `stale_hours`, `max_review_rounds` |
| `wonka git <id> <sub> ...` | what agents use instead of git: runs in the ticket's worktree with a fixed allowlist |
| `wonka doctor` | check git, gh, npx (markdownlint), board, state, Claude Code permissions and notes with stray ## sections |

## The floor

| Column | What happens |
|---|---|
| ✉️-Inbox | 📏 **oompa-loompa-triage** (Sonnet) sizes it. xs/s with a clear idea → 🌟 Done. m/l/xl or unclear → 🙋, tick **Approve** and answer the questions |
| 🧪-Drafting | 🧪 **oompa-loompa-analyst** (Opus) writes `## 📜 Spec` and a detailed plan in `wonka/plans/`, asks `## ❓ Questions` if needed; answered rounds move to `## ✅ Answers`, splits l/xl into new Inbox tickets (`## 🫐 Too big`, then Wont-Do) |
| 🎫-Ready | waits for a Baking slot |
| 🍫-Baking | 🍫 **oompa-loompa-builder** (Opus) builds in `~/.wonka/worktrees/<project>/<id>` and commits |
| 🐿️-Reviewing | 🐿️ **squirrel-architect / -backend / -frontend / -security / -spark / -airflow** (Opus, picked by the analyst). Issues → back to Baking; follow-ups → new Inbox tickets; decisions → `## 🐿️ Review Questions` (incl. close the ticket / back to Drafting); a clean round ships once every answer is the recommended one, otherwise rebakes. Max `max_review_rounds` (default 3) rounds with issues, then Wont-Do; a clean round that rebakes adds an extra round |
| 🛗-Shipped | the CLI pushes and opens the PR → 🙋 (click the desktop ping to open the PR) approve and merge it → 🌟 Done, worktree and local branch removed. Or tick **🔁 Request changes** (write what to change under it and/or leave PR review comments): back to Baking for a fix round with a fresh 3 review rounds; the fixes are pushed to the same PR. Only comments by the repo's owners, members and collaborators are copied to the builder (others could slip it instructions on a public repo); the number skipped shows in the factory log. Each tick an open PR with merge conflicts gets 🔧 **oompa-loompa-fixer** (Sonnet): it merges the base in, resolves, commits, and the CLI pushes (`wonka resolved`, never a force push); no new review, the PR is back to 🙋 |
| 🫐-Wont-Do | killed, split or closed (the local branch is kept, it may hold work). You can drag any card here |

Triage and the analyst read a throwaway read-only checkout of the latest `origin/<base>` in
`~/.wonka/worktrees/<project>/<id>-scout`, made fresh for each job and removed once the ticket leaves triage or
drafting. Your own checkout is never touched, so they never plan against stale code.

Phases: 🕰️-Pending · 🙋-Awaiting-Human · 🎩-In-Progress · 🌟-Done. Done tickets and their plans move to
`wonka/archive/` after 7 days.

## WIP limits

`wip.drafting` and `wip.baking` cap how many tickets an Oompa-Loompa is working on, not how many cards sit in a
column. A ticket counts toward its column's limit only while it is 🕰️-Pending or 🎩-In-Progress there, and the limit
is checked only when a ticket is pulled in (✉️ Inbox to 🧪 Drafting, 🎫 Ready to 🍫 Baking). This is on purpose:
waiting for a human should never block the line.

- 🧪 Drafting: a ticket waiting on your answers (🙋) frees its slot. When you answer, drafting resumes at once,
  even if that briefly puts Drafting over the limit.
- 🍫 Baking: tickets in 🐿️-Reviewing and 🛗-Shipped don't count, and squirrel reviews run without a limit. A ticket
  sent back by review returns to Baking straight away, without waiting for a slot.
- So `wip.baking` is not a cap on worktrees: every ticket keeps its worktree from Baking until its PR is merged
  or it goes to Wont-Do.

## Reading a ticket

Every note opens with a banner that says where the ticket is, what happens next and, when it's 🙋 your turn, exactly
what to tick. It also lists size, project, plan, PR and review round. The board's **🙋 Your turn** view lists only
tickets that wait for you. At the bottom, a folded **🎩 Factory log** holds the history. A job (or opening the PR)
that fails 3 times shows **🎩 Stuck** with a Retry checkbox; details are in `~/.wonka/factory.log`.

The 💡 Idea and 📜 Spec are free text, so they are checked with markdownlint (all default rules, via `npx`) when
written. A section it flags is kept as raw text in a code block under a warning that lists the problems, so broken
markdown can't spill into the rest of the note. Clean sections are written as is. `WONKA_MDLINT` overrides the
linter command, `WONKA_MDLINT=off` turns the check off; if the linter is missing, offline or fails, the section is
written unchecked and the reason goes to `~/.wonka/factory.log`.

**The state machine** (`wonka/machine.py`) is the only way a ticket moves. Drag a card anywhere other than Wont-Do
and the next tick puts it back, with a note on the ticket. Notes keep only `id, title, status, phase, size,
jira`. Everything else (repo, PR repo, branch, worktree, PR, rounds, history) is in
`~/.wonka/golden_ticket_status.json` (the previous version is kept next to it as `.json.bak`).

## Develop

```bash
cd wonka && python3 -m unittest discover tests
```
