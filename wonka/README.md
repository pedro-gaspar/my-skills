# 🎩 Wonka

*Come with me, and you'll be in a world of pure imagination.* 🍫

A slim software factory for Claude Code. Every idea is a **golden ticket**, a note on an Obsidian Bases kanban.
🎩 **Wonka** (the `/wonka` skill) runs the floor, and his **Oompa-Loompas** (triage, analyst, builder, fixer) draft and bake each
ticket, the Nut Room **squirrels** test it (good nut or bad nut?), then Wonka ships it as a PR. You steer by ticking checkboxes.

## Install

Needs Python 3.11+ (stdlib only), git, `gh` (logged in) and Obsidian 1.14+ (Bases kanban view). Desktop pings use
`notify-send` (and `xdg-open` for clicks) on Linux. On macOS they use `terminal-notifier` if installed (`brew install
terminal-notifier`, a click opens the PR), else the built-in `osascript` (a click opens nothing).

```bash
claude plugin marketplace add ~/Work/my-skills
claude plugin install wonka@my-skills
```

Then run `/wonka init`. It asks for your vault and how many tickets to draft and bake at once. It writes
`~/.wonka/config.json`, creates `<vault>/wonka/{golden-tickets,plans,archive}` and `🍫 Factory.base` (shows as
"🍫 Factory" in Obsidian), links `wonka` into `~/.local/bin`, and runs `wonka doctor`. Init never overwrites an
existing `🍫 Factory.base`; an older vault's `Wonka.base` is renamed to `🍫 Factory.base` by `wonka init` (keeping
any view changes), and the doctor warns until it is. The doctor prints the permission snippet that lets background
agents run unattended (`Bash(wonka *)`, plus the worktree and factory folders as `additionalDirectories`). Agents
run git only through `wonka git <id> [--dispatch <n>] <subcommand>`, which pins it to the ticket's worktree and that worktree's
registered git dir (a rewritten `.git` file is ignored) and allows only status, diff, log, show, rev-parse,
ls-files, blame, add and commit (plus the fixer's `rebase <ref>`, `rebase --continue` and `rebase --abort`; no `-c`, config,
push or options that run programs), so no git permission rule is needed; the doctor warns if an old `Bash(git *)`
rule is still there. Writes (add, commit, rebase) must carry the agent's `--dispatch` and are refused unless it
is the ticket's current dispatch (every agent command, from `wonka triage` to `wonka report`, requires `--dispatch`
too, and a result from an earlier dispatch is ignored), so a builder or fixer left over from an earlier dispatch cannot commit next to its
replacement. Wonka only ever commits and pushes the ticket's own `wonka/` branch: `wonka git` writes, the PR
push and `wonka resolved` refuse main, master, the base branch, or a worktree that is no longer on its branch. On
GitHub it only changes PRs it opened itself (it recognises them by a hidden `<!-- wonka:<id> -->` marker at the top of the description, or the older `🎫 Golden ticket` line): before every push it checks the open PRs on the branch, and one opened
by anyone else (by hand, or another tool) makes the ticket 🎩 Stuck instead of being pushed to or taken over.

## Sandbox boundary

`wonka git` is a guardrail against model mistakes, not a sandbox. Builders, fixers and squirrels run the plan's
test commands, which is arbitrary code, and builders and fixers also have Edit and Write. Both reach whatever the
`/loop` session reaches: its working directory and the `additionalDirectories` in your settings. Run the loop from
inside a repo (say `~/Work/my-skills`) and that includes the main checkout, its uncommitted work and its
`.git/config`. For a real boundary, do both:

1. **Run the loop from an empty, neutral directory**, such as `~/.wonka/floor`
   (`mkdir -p ~/.wonka/floor && cd ~/.wonka/floor && claude`), not `~/.wonka` itself, which holds Wonka's state
   and config. Add tickets with `--repo` (`/wonka add --repo ~/Work/my-skills "idea"`), or from another session
   opened in the repo: only the loop session's directory matters. Edit and Write then reach only the worktrees and
   the factory folder.
2. **Turn on Claude Code's sandbox** ([docs](https://code.claude.com/docs/en/sandboxing)): `/sandbox`, or in
   `~/.claude/settings.json`; on Linux it needs `bubblewrap` and `socat`. The OS then confines every shell command
   and whatever it starts: writes only to the working directory, a temp dir and `additionalDirectories`, and network
   only to the domains you allow. It does not cover Edit and Write, which is why step 1 still matters. Wonka needs
   these settings to keep working under it:

   ```json
   {
     "sandbox": {
       "enabled": true,
       "allowUnsandboxedCommands": false,
       "excludedCommands": ["wonka *"],
       "filesystem": { "allowWrite": ["~/.wonka"] }
     }
   }
   ```

   - `wonka *` runs outside the sandbox: the CLI writes to the main repo's `.git` (worktrees, fetches, commit
     objects) and talks to GitHub and npm.
   - `allowWrite` is needed because a call that pipes text in with a heredoc (the analyst's `wonka plan` and
     `wonka section`) stays sandboxed, and every wonka call takes the lock in `~/.wonka`. Sandboxed test code
     can then edit Wonka's state, but not your checkouts.
   - Commands run through `wonka test` run outside the sandbox too (`wonka *` is excluded), and a builder can edit
     the script they run: only list a project's `test_commands` if you accept that.
   - `allowUnsandboxedCommands: false` stops an agent from retrying a blocked command outside the sandbox.

   Tests that need the network fail until you add their hosts to `sandbox.network.allowedDomains`.

What stays open: sandboxed commands can still read most of your home, `~/.ssh` included (close it with
`sandbox.filesystem.denyRead`), and git hooks run outside the sandbox: commit hooks with `wonka git commit`, and
pre-push hooks when Wonka pushes the PR branch (`wonka tick`, `wonka resolved`). In a repo whose hooks live in
tracked files (husky, `pre-commit` with `repo: local`), an agent can write a hook and it runs at the next commit or
push.

## Use

| | |
|---|---|
| `/wonka add "idea"` | drop a ticket on the board as a 💡 Idea (`--triage` to put it straight in Triage when you want it built now; run it from the target repo or pass `--repo <path>`; from a linked worktree the ticket is filed under the main checkout, so `.env` is there and it outlives the worktree; `--pr-repo owner/name`, `--jira`, `--needs-human-spec-review` to approve the spec yourself before baking; `--blocked-by WONKA-00001[,…]` to record tickets it waits on) |
| `/wonka tick` | one turn of the factory: fix the cards, act on your ticks, merge checks, dispatch agents |
| `/loop /wonka tick` | keep the factory running |
| `wonka block <id> <blocker>[,…]` / `wonka unblock <id> [<blocker>…]` | record or remove "this ticket waits on those", only while the ticket is an Idea or in Triage, Drafting or Planned. Refuses unknown ids, a ticket blocking itself, Won't-do blockers and cycles. `unblock` with no ids clears all. For now this only records and shows the dependency: the banner shows `⛓️ blocked by [[…]]` |
| `/wonka status` | one line per ticket, with ` ⛓️ <ids>` for blockers that have not merged yet |
| `wonka repos` | the repos the factory knows (project, local path, PR repo); the analyst uses it to file a ticket in another repo |
| `wonka config [key value]` | show or change settings: `wip.triage`, `wip.drafting`, `wip.baking` (see [WIP limits](#wip-limits)), `archive_after_days`, `stale_hours`, `max_review_rounds`, `max_followup_depth`, `worktree_files.<project>`, `test_commands.<project>`, `test_lock_wait_minutes` |
| `wonka git <id> [--dispatch <n>] <sub> ...` | what agents use instead of git: runs in the ticket's worktree with a fixed allowlist |
| `wonka test <id> [--dispatch <n>] -- <command> ...` | what agents use to run a project's configured test command: one run per project at a time, exit 75 when the lock stays busy |
| `wonka doctor` | check git, gh, npx (markdownlint), the factory folder, state, Claude Code permissions, open test_commands prefixes, `worktree_files` sources (present and gitignored in your checkout) and notes with stray ## sections |

## The floor

| Column | What happens |
|---|---|
| 💡-Idea | parked: no agent touches it and it uses no WIP slot. Refine the idea in the note, then tick **Start** in `## 💡 Start` or drag the card to 📏-Triage; drag it to 🗑️-Wont-Do to drop it. `wonka add` starts here unless `--triage`; squirrel follow-ups start here too |
| 📏-Triage | 📏 **oompa-loompa-triage** (Sonnet) sizes it. xs/s with a clear idea → 🌟 Ready. m/l/xl or unclear → 🙋, tick **Approve** and answer the questions. A duplicate of an open ticket → 🗑️ Wont-Do (`## 🗑️ Duplicate`) |
| 🧪-Drafting | 🧪 **oompa-loompa-analyst** (Opus) writes `## 📜 Spec` and a detailed plan in `wonka/plans/`, asks `## ❓ Questions` if needed; answered rounds move to `## ✅ Answers`, splits l/xl into new Triage tickets (`wonka add --triage`) (`## 🗑️ Too big`, then Wont-Do); an idea that spans several repos is split into one Triage ticket per repo (`## 🗑️ Split by repo`, then Wont-Do): a ticket always builds in one repo. A ticket with `spec_review: human` (set with `--needs-human-spec-review`, `wonka set <id> spec_review human`, or the note's property) waits in 🧪-Drafting 🙋 once the analyst is done: **Approve** sends it to 🎫-Planned, **Re-draft** back to the analyst with your notes, **Close** to Wont-Do. Children inherit it; anything but `agent` or empty counts as human; it only matters while the ticket is in Drafting |
| 🎫-Planned | waits for a Baking slot |
| 🍫-Baking | 🍫 **oompa-loompa-builder** (Sonnet) builds in `~/.wonka/worktrees/<project>/<id>` and commits; when the repo has a PR template (`.github/pull_request_template.md` and the other GitHub locations) the builder also fills it in after the tests pass, from the plan's decisions, the spec and what it verified, linking the Jira key (in the bake job JSON as `jira`) and never mentioning Wonka or AI, with `wonka pr-body` (saved as `wonka/plans/<id>-pr.md`; it refuses an AI attribution footer) and `wonka baked` refuses until it is written in that bake |
| 🐿️-Reviewing | 🐿️ **squirrel-architect / -backend / -frontend / -security / -spark / -airflow** (architect on Opus, the rest on Sonnet; picked by the analyst). Issues → back to Baking; follow-ups → new 💡 Idea tickets (not for a ticket already `max_followup_depth`, default 2, parents deep: those are only logged on the ticket, so follow-ups can't breed forever); decisions → `## 🐿️ Review Questions` (incl. close the ticket / back to Drafting); the answers are logged in the plan as `### Decisions` under that `## 🐿️ Review round N` (the log holds only squirrels that raised issues; a clean round with no answered questions logs nothing); a clean round ships once every answer is the recommended one, otherwise rebakes. Max `max_review_rounds` (default 3) rounds with issues, then Wont-Do; a clean round that rebakes adds an extra round |
| 🚢-Shipped | the CLI pushes and opens the PR as a draft, so no reviewer is pinged yet (titled `[KEY-1] title` when the ticket has a Jira key, else `[WONKA-00001] title`; the description is the builder's filled-in PR template when the repo has one (no Jira line: the template carries it), else the spec with a Jira line when there is a key; no Wonka branding, just the hidden marker) → 🙋 (click the desktop ping to open the PR) mark it ready for review, approve and merge it → 🌟 Ready, worktree and local branch removed (a worktree with local changes is kept and retried each tick: clean or delete it and Wonka removes it and the branch). Or tick **🔁 Request changes** (write what to change under it and/or leave PR review comments): back to Baking for a fix round with a fresh 3 review rounds; the fixes are pushed to the same PR, and its description is refreshed to the current spec too, unless someone edited it on GitHub since Wonka last wrote it (then it is left as is and the factory log says so; PRs opened before this version keep their description). A fix round starts from the PR branch on GitHub, so commits pushed there (an applied suggestion, "Update branch") are kept; if the push is still rejected the ticket goes stuck at once with what to do. Only comments by the repo's owners, members and collaborators are copied to the builder (others could slip it instructions on a public repo); the number skipped shows in the factory log. Each tick an open PR with merge conflicts gets 🔧 **oompa-loompa-fixer** (Sonnet): it rebases the PR branch onto the PR's current base on GitHub (never a merge commit), resolves, and the CLI pushes (`wonka resolved`, with `--force-with-lease` against the PR branch GitHub had when the fixer started, so a commit pushed there meanwhile is never overwritten); no new review, the PR is back to 🙋. Uncommitted leftovers in the worktree are stashed first, under `refs/wonka/stash/<id>/<time>` and not in your `git stash list` (the factory log names the ref; `git stash apply <ref>` gets them back, `git update-ref -d <ref>` deletes it). A fixer that cannot start (the fetch fails, the worktree is missing) is retried each tick and goes 🎩 Stuck after 3 tries; a PR branch that diverged from GitHub is stuck at once with what to do. If GitHub still calls the PR conflicting after 3 pushed fixes in a row, the ticket goes stuck instead (tick Retry for 3 more; a PR that turns mergeable unsticks itself). A PR closed while a fixer or a fix round was running sends the ticket to Wont-Do instead of opening a new PR; one merged during a fix round makes the ticket 🎩 Stuck (its new commits are only on the local branch: open a PR for them by hand, then drag the card to Wont-Do). An open PR whose checks all finished on its head with at least one failed (failure, timed out, action required, startup failure, error) goes back to Baking on its own, with the failing checks and the last 150 lines of each failed GitHub Actions run (up to 3) in the plan; Wonka waits while any check is pending, never reworks the same head twice, handles conflicts first, merges it with a ticked Request changes into one round, and stops after 3 CI fix rounds in a row (logged and notified: fix it by hand). A PR with no checks is left alone. |
| 🗑️-Wont-Do | killed, split or closed (the local branch is kept, it may hold work). You can drag any card here. A ticket killed while a builder, squirrel or fixer still works in its worktree keeps the worktree until that agent ends (`wonka finished`) or `stale_hours` pass, then Wonka removes it |

Triage and the analyst read a throwaway read-only checkout of the latest `origin/<base>` in
`~/.wonka/worktrees/<project>/<id>-scout`, made fresh for each job and removed once the ticket leaves triage or
drafting. Your own checkout is never touched, so they never plan against stale code.

Worktrees and scouts have no gitignored files. List the ones a project's tests need with
`wonka config worktree_files.<project> server/.env,other/.env` (`<project>` is the repo folder name; an empty value
removes it): the CLI copies them from your checkout into each worktree and scout, keeping their mode. Only files git
ignores there are copied (never one a builder could commit), an existing file is never overwritten, and a missing one is
noted in the ticket's factory log without blocking the job. They are copied, never symlinked, so a builder cannot change
your checkout's file. `wonka doctor` warns when a listed file is missing in your checkout or not gitignored there.

Agents run a project's tests with `wonka test <id> -- <command>`. List the allowed command prefixes with
`wonka config test_commands.<project> ./test.sh,make test` (a comma list; an empty value removes it). `wonka test`
runs only a command that starts with one of them, with no shell, in the ticket's scout or worktree (never your
checkout). One run per project holds `~/.wonka/locks/<project>-tests.lock`; the others wait up to
`test_lock_wait_minutes` (default 3, so wait plus run fit the agents' 10-minute Bash timeout), then exit 75 naming
the holder. The exit code is the command's. `wonka config` and `wonka doctor` warn about a prefix that leaves the
program or code to the caller (a shell, interpreter or launcher with nothing pinned after it, like `python3 -c`,
`sh -c`, `bash`, `env`, `npx`), since it allows any command outside the sandbox.

Every job JSON carries the project's `test_commands` (`[]` when none). The analyst, builder, fixer and squirrels run a
test command that starts with one of them through `wonka test <id> --dispatch <n> -- <command>` with a 10-minute Bash
timeout, run it again on exit 75 (up to 5 tries), and run anything else as before. So two tickets of one project never
run its tests at the same time.

Phases: 🕰️-Pending · 🙋-Awaiting-Human · 🎩-In-Progress · 🌟-Ready. Finished tickets (Shipped or Wont-Do) and their plans move to
`wonka/archive/` after 7 days. Notes, the state file and the board written with the old names (🌟-Done, 🎫-Ready) are
migrated automatically on the next tick.

## WIP limits

`wip.triage`, `wip.drafting` and `wip.baking` cap how many tickets an Oompa-Loompa is working on, not how many cards sit in a
column. A ticket counts toward its column's limit only while it is 🕰️-Pending or 🎩-In-Progress there, and the limit
is checked only when a ticket is pulled in (📏 Triage to 🧪 Drafting, 🎫 Planned to 🍫 Baking) or, for triage, when a
triage job is handed out. This is on purpose:
waiting for a human should never block the line.

- 💡 Idea: no limit and no agent; a ticket counts only once it reaches Triage.
- 📏 Triage: `wip.triage` (default 2) triage Oompa-Loompas run at once, so a burst of new tickets started at once is sized a few at a time.
- 🧪 Drafting: a ticket waiting on your answers (🙋) frees its slot. When you answer, drafting resumes at once,
  even if that briefly puts Drafting over the limit.
- 🍫 Baking: tickets in 🐿️-Reviewing and 🚢-Shipped don't count, and squirrel reviews run without a limit. A ticket
  sent back by review returns to Baking straight away, without waiting for a slot.
- So `wip.baking` is not a cap on worktrees: every ticket keeps its worktree from Baking until its PR is merged
  or it goes to Wont-Do.

## Reading a ticket

Every note opens with a banner that says where the ticket is, what happens next and, when it's 🙋 your turn, exactly
what to tick. It also lists size, project, plan, PR and review round. The factory's **🙋 Your turn** view lists only
tickets that wait for you. At the bottom, a folded **🎩 Factory log** holds the history. A job (or opening the PR)
that fails 3 times shows **🎩 Stuck** with a Retry checkbox; details are in `~/.wonka/factory.log`. A push
rejected because the PR branch has new commits on GitHub is stuck at once, not after 3 tries: rebase onto them in the
worktree (`git pull --rebase`), then tick Retry. An open PR that GitHub still calls conflicting after 3 pushed conflict fixes in a row
is stuck too: check its base branch on GitHub or fix the conflicts by hand, then tick Retry. A stuck open PR clears
itself when it is merged, closed or made mergeable.

The 💡 Idea and 📜 Spec are free text, so they are checked with markdownlint (via `npx`) when written, but only the rules that can change the note's structure (list
indentation, headings, blockquotes, fences, lists, inline HTML, tables); style-only rules (spaces in code spans, fence
languages, bare URLs, line length, table alignment and the like) are disabled. Missing blank lines around lists (MD032) are inserted automatically first, outside code fences. A section it flags is kept as raw text in a code block under a warning that lists the problems, so broken
markdown can't spill into the rest of the note. Clean sections are written as is. `WONKA_MDLINT` overrides the
linter command, `WONKA_MDLINT=off` turns the check off; if the linter is missing, offline or fails, the section is
written unchecked and the reason goes to `~/.wonka/factory.log`.

Wonka finds a ticket's note by its file name (`WONKA-00001.md`). A note renamed in Obsidian is renamed back on the
next tick (found by the `id` in its frontmatter); a deleted note drops its ticket to Wont-Do. Since that is final, a
tick refuses to run when the tickets folder is missing (an unmounted or moved vault, a changed `vault` setting) or
when every live ticket's note is gone at once; if you really deleted them all, run `wonka tick --drop-missing`.
Errors that stop the whole factory (that refusal, an unreadable state or config file, a lock held for 10 minutes)
also send an urgent desktop notification, so a `/loop` running in the background doesn't fail silently.

**The state machine** (`wonka/machine.py`) is the only way a ticket moves. Drag a card anywhere other than Wont-Do (or an Idea to Triage)
and the next tick puts it back, with a note on the ticket. Notes keep only `id, title, status, phase, size,
jira, spec_review, blocked_by`. `blocked_by` is a read-only mirror of the state (comma-separated, absent when none): edit it with `wonka block`/`unblock`, since a hand edit is overwritten on the next write. Everything else (repo, PR repo, branch, worktree, PR, rounds, history) is in
`~/.wonka/golden_ticket_status.json` (the previous version is kept next to it as `.json.bak`). It and the notes
are written atomically (a temp file, then a rename), and every command that changes state takes a lock in `~/.wonka`,
giving up after 10 minutes. `wonka git`, `wonka test`, `wonka status` and `wonka show` only read, so they never wait for it: an agent
is never stalled by a tick that is busy talking to GitHub.

## Develop

```bash
cd wonka && python3 -m unittest                  # from wonka/
python3 -m unittest discover -s wonka -t wonka   # from the repo root, no cd (what plans and squirrels use)
```

A bare `python3 -m unittest` from the repo root finds no tests (`NO TESTS RAN`, exit code 5). If a run reports 0
tests, the command is wrong, not passing.
