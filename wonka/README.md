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
git clone https://github.com/pedro-gaspar/my-skills ~/Work/my-skills   # any path; Wonka's retro files ideas in it
claude plugin marketplace add ~/Work/my-skills
claude plugin install wonka@my-skills
```

Then run `/wonka init`. It asks for your vault and how many tickets to draft and bake at once. It writes
`~/.wonka/config.json`, creates `<vault>/wonka/{golden-tickets,plans,archive}` and `🍫 Factory.base` (shows as
"🍫 Factory" in Obsidian), links `wonka` into `~/.local/bin`, and runs `wonka doctor`. Init never overwrites an
existing `🍫 Factory.base`; an older vault's `Wonka.base` is renamed to `🍫 Factory.base` by `wonka init` (keeping
any view changes), and the doctor warns until it is. The doctor prints the permission snippet that lets background
agents run unattended (one `Bash(wonka <command> *)` rule per command agents and the loop run, plus the worktree and
factory folders as `additionalDirectories`; never `~/.wonka` itself or a folder above it like `~`, which would let
Edit and Write change the config and state: the doctor warns). The human's commands (`init`, `config`, `set`, `move`,
`block`, `unblock`, `doctor`, `session`) are left out on purpose: an agent that read a hostile diff or CI log could otherwise switch the
sandbox off (`wonka config test_commands.<project> bash`) or pass its own gates (`wonka move`, `wonka set <id>
spec_review agent`). They ask you instead; never allow them, or `Bash(wonka *)`, in your settings (the doctor warns).
`wonka section` only writes the analyst's 📜 Spec, while the ticket is being drafted, so no agent ticks a box for you. Agents
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
   these settings to keep working under it (`wonka doctor` prints them):

   ```json
   {
     "sandbox": {
       "enabled": true,
       "allowUnsandboxedCommands": false,
       "excludedCommands": [
         "wonka git *", "wonka ci-logs *", "wonka triage *", "wonka ask *", "wonka section *", "wonka plan *",
         "wonka drafted *", "wonka pr-body *", "wonka baked *", "wonka resolved *", "wonka report *",
         "wonka add *", "wonka show *", "wonka status *", "wonka repos *", "wonka metrics *",
         "wonka pr-comments *", "wonka tick *", "wonka finished *", "wonka retry *", "wonka worktree *",
         "wonka init *", "wonka config *", "wonka set *", "wonka move *", "wonka block *", "wonka unblock *",
         "wonka doctor *", "wonka session *"
       ]
     }
   }
   ```

   - Every wonka command but `wonka test` runs outside the sandbox: the CLI writes Wonka's state in `~/.wonka` and
     the main repo's `.git` (worktrees, fetches, commit objects), and talks to GitHub and npm.
   - `wonka test` is left out on purpose, so it runs inside the sandbox and the test command it starts (a script the
     builder can edit: `./test.sh`, a `conftest.py`, a Makefile) is confined with it: it writes only the worktree, a
     temp dir and the `additionalDirectories`, and reaches only the domains you allow. `wonka test` writes nothing
     but its lock, which is in the worktrees folder (`~/.wonka/worktrees/.locks`). Never exclude `wonka *` or
     `wonka test *`: test commands would then run unconfined (the doctor warns, and warns when the sandbox is off).
     With the sandbox off, `wonka test` works as before, unconfined. A shell or interpreter with nothing pinned after
     it (`bash`, `python3 -c`) would run anything, so `wonka config` refuses it as a test command and `wonka test`
     refuses to run one set by hand.
   - Never add `~/.wonka` to `sandbox.filesystem.allowWrite`: it holds Wonka's config and state, and sandboxed test
     code that can edit them can loosen the factory's checks. Agents hand markdown over with `--file`
     (`wonka plan|section|pr-body <id> --file <drafts>/...`, a file they write in `~/.wonka/worktrees/.drafts`), so
     no other wonka call runs sandboxed; a heredoc piped into wonka would. The doctor warns about an old `allowWrite`.
   - `allowUnsandboxedCommands: false` stops an agent from retrying a blocked command outside the sandbox.

   Tests that need the network fail until you add their hosts to `sandbox.network.allowedDomains`.

Projects whose CI is not GitHub Actions configure their log tool and CI host, e.g.
`wonka config ci_log_command.beyondpricing "beyond ci logs"` and
`wonka config ci_hosts.beyondpricing <concourse host>`. Builders then fetch a failed log with `wonka ci-logs <id> --dispatch <n> <url>` (only in a CI fix round, from the current dispatch),
which refuses any URL that is not https on one of those hosts (a check URL on a PR can be set by anyone who can post a
commit status). It runs under `Bash(wonka ci-logs *)` and outside the sandbox with `wonka ci-logs *`, so no `Bash(beyond ci logs *)`
rule or sandbox exclusion is needed: remove them if you added them, they let an agent run the tool on any URL (the
doctor warns).

The log tool must be a program on PATH or an absolute path: `wonka config` and `wonka ci-logs` refuse a path relative
to the worktree (`./scripts/ci.sh`), which the builder could edit.

`wonka git` never looks inside a submodule (`diff.ignoreSubmodules=all`): a nested repo brings its own config, whose
filters would run outside the sandbox. `wonka git add` refuses to run while the worktree holds a submodule or a nested
repo, since git add looks inside them anyway, so repos that use submodules need a human to commit.

Agents cannot point wonka at a config they wrote: a command that starts with a variable (`WONKA_HOME=<dir> wonka git
...`) does not match the per-command allow rules, so Claude Code asks you instead of running it for a background
agent; never approve one. `WONKA_HOME` itself (for the tests, or a second factory) must be an absolute path, and not
inside `~/.wonka` (whose worktrees and drafts agents write). gh is always the `gh` on PATH: the old `WONKA_GH`
override is gone.

What stays open: sandboxed commands can still read most of your home, `~/.ssh` included (close it with
`sandbox.filesystem.denyRead`), and git hooks run outside the sandbox: commit hooks with `wonka git commit`, and
pre-push hooks when Wonka pushes the PR branch (`wonka tick`, `wonka resolved`). In a repo whose hooks live in
tracked files (husky, `pre-commit` with `repo: local`), an agent can write a hook and it runs at the next commit or
push.

## Use

| | |
|---|---|
| `/wonka add "idea"` | drop a ticket on the board as a 💡 Idea (`--triage` to put it straight in Triage when you want it built now; run it from the target repo or pass `--repo <path>`; from a linked worktree the ticket is filed under the main checkout, so `.env` is there and it outlives the worktree; `--pr-repo owner/name`, `--jira`, `--review-spec` to approve the spec yourself before baking; `--blocked-by WONKA-00001[,…]` to record tickets it waits on; `--manual` to work it by hand from the start: it is created 🕹️ manual, as `wonka session` would take it, and `wonka session <id>` opens it). An empty idea is refused; a line break in a title or Jira key (here, in `wonka set` or `wonka triage --title`) becomes a space |
| `/wonka tick` | one turn of the factory: fix the cards, act on your ticks, merge checks, dispatch agents |
| `/loop /wonka tick` | keep the factory running |
| `wonka block <id> <blocker>[,…]` / `wonka unblock <id> [<blocker>…]` | record or remove "this ticket waits on those", only while the ticket is an Idea or in Triage, Drafting or Planned. Refuses unknown ids, a ticket blocking itself, Won't-do blockers and cycles. `unblock` with no ids clears all. A ticket with unmerged blockers waits in its column until every blocker has merged (see The floor); the banner shows `⛓️ blocked by [[…]]` |
| `/wonka session <id or Jira key>` | take a ticket into a Claude session: it turns 🕹️ manual (see [Manual tickets](#manual-tickets)) and Claude opens in its worktree with a brief (note, plan, stage, branch, PR). `wonka session KEY` in a terminal runs `claude` there (`--terminal`) or opens an Orca tab in the worktree (`--orca`; the default comes from `wonka config session_launcher terminal\|orca`); `--print` only prints the brief, which is what `/wonka session` uses inside Claude. Refused while an agent works on the ticket unless `--force` (its results are then ignored). `--from <branch>` starts the ticket's branch from work you began by hand in another checkout, only while the branch has no commits of its own |
| `/wonka worktree <id or Jira key>` | the ticket's worktree path (`cd "$(wonka worktree DATA-3806)"`); `--info` adds the branch and base, commits ahead, uncommitted changes, state and PR. Only reads |
| `/wonka status` | one line per ticket, with ` ⛓️ <ids>` for blockers that have not merged yet; a ticket whose note is gone shows `⚠️` and why in place of its title |
| `wonka repos` | the repos the factory knows (project, local path, PR repo); the analyst uses it to file a ticket in another repo |
| `wonka metrics [--since YYYY-MM-DD]` | one JSON line per ticket, archived ones too: `id, project, size, created_at, reviewers, outcome` (merged / bad nut / duplicate / split / closed by human / PR closed, null while live), `hours_in` per status, `hours_awaiting_human` (time in 🙋), `rounds`, `stats`, `followups_filed`, `pr_url`; `--since` filters on the creation date. Reads a snapshot, never the lock |
| `wonka pr-comments <id>` | one JSON line per human comment on the ticket's PR, oldest first: review comments, reviews and conversation comments (all pages), only by the repo's owners, members and collaborators, bots skipped. The inspector reads PR comments through it, so it needs no `gh` permission. Reads a snapshot, never the lock |
| `/wonka retro [YYYY-MM-DD]` | look back over the tickets created since the date (default 7 days ago): 🔍 **oompa-loompa-inspector** (Opus, read-only) reads `wonka metrics`, the plans' review logs and decisions, the notes' answers, `~/.wonka/factory.log` and the human review comments on merged PRs (`wonka pr-comments`), and reports flow (hours per status, time waiting on you, gates that fire most), recurring issues (3+ tickets), squirrel quality per reviewer and planning quality. Each proposal (cheapest fix first: a CLI check, an agent prompt, the squirrel focus or reviewer choice, then the repo's AGENTS.md) is yours to Apply (a 💡 Idea ticket in the my-skills clone or the product repo), Apply and start, or Skip; nothing is edited directly, never the plugin cache in ~/.claude/plugins |
| `wonka config [key value]` | show or change settings (it also shows the factory folder and log paths): `wip.triage`, `wip.drafting`, `wip.baking` (see [WIP limits](#wip-limits)), `archive_after_days`, `stale_hours`, `max_review_rounds`, `max_followup_depth`, `worktree_files.<project>`, `test_commands.<project>`, `ci_hosts.<project>`, `ci_log_command.<project>`, `test_lock_wait_minutes`, `session_launcher` (`terminal` or `orca`: how `wonka session` opens Claude) (numbers must be finite: `inf` and `nan` are refused). A hand-edited `config.json` with a value of the wrong type (a null `vault` or `wip`, a string where a list goes) stops every command with an error naming the key |
| `wonka git <id> [--dispatch <n>] <sub> ...` | what agents use instead of git: runs in the ticket's worktree with a fixed allowlist |
| `wonka test <id> [--dispatch <n>] -- <command> ...` | what agents use to run a project's configured test command: one run per project at a time, exit 75 when the lock stays busy |
| `wonka ci-logs <id> --dispatch <n> <url>` | what builders use to fetch a failed CI log, only in a CI fix round and from the current dispatch: runs the project's `ci_log_command` on the URL, only when it is https on one of the project's `ci_hosts` |
| `wonka doctor` | check git, gh, npx (markdownlint), the factory folder, the config and state files (valid JSON of the right shape), Claude Code permissions, the sandbox (on, with every wonka command but `test` excluded), `additionalDirectories` that would expose `~/.wonka`, open test_commands prefixes, CI log tool allow rules that bypass the host check, `worktree_files` sources (present and gitignored in your checkout) and notes with stray ## sections |

## The floor

| Column | What happens |
|---|---|
| 💡 Idea | parked in 🙋 Awaiting Human (so it shows in 🙋 Your turn): no agent touches it and it uses no WIP slot. Refine the idea in the note, then tick **Start** in `## 💡 Start` or drag the card to 📏 Triage; drag it to 🗑️ Wont Do to drop it. `wonka add` starts here unless `--triage`; squirrel follow-ups start here too |
| 📏 Triage | 📏 **oompa-loompa-triage** (Sonnet) sizes it. xs/s with a clear idea → 🍭 Ready. m/l/xl or unclear → 🙋, tick **Approve** and answer the questions. A duplicate of an open ticket → 🗑️ Wont Do (`## 🗑️ Duplicate`) |
| 🧪 Drafting | 🧪 **oompa-loompa-analyst** (Opus) writes `## 📜 Spec` and a detailed plan in `wonka/plans/`, asks `## ❓ Questions` if needed; answered rounds move to `## ✅ Answers`, splits l/xl into new Triage tickets (`wonka add --triage`) (`## 🗑️ Too big`, then Wont Do); an idea that spans several repos is split into one Triage ticket per repo (`## 🗑️ Split by repo`, then Wont Do): a ticket always builds in one repo. A ticket with `spec_review: human` (set with `--review-spec`, `wonka set <id> spec_review human`, or the note's property) waits in 🧪 Drafting 🙋 once the analyst is done: **Approve** sends it to 🎫 Planned, **Re-draft** back to the analyst with your notes, **Close** to Wont Do. Children inherit it; anything but `agent` or empty counts as human; it only matters while the ticket is in Drafting |
| 🎫 Planned | waits for a Baking slot |
| 🍫 Baking | 🍫 **oompa-loompa-builder** (Sonnet) builds in `~/.wonka/worktrees/<project>/<id>` and commits; each bake that is not a fix round on an open PR first fast-forwards the worktree to the latest `origin/<base>` when its branch has no commits of its own and the tree is clean (no changes, no untracked files), so a retried bake sees a dependency that merged meanwhile; a branch with its own commits or local changes is left as is, and the factory log says when it moves; when the repo has a PR template (`.github/pull_request_template.md` and the other GitHub locations) the builder also fills it in after the tests pass, from the plan's decisions, the spec and what it verified, linking the Jira key (in the bake job JSON as `jira`) and never mentioning Wonka or AI, with `wonka pr-body` (saved as `wonka/plans/<id>-pr.md`; it refuses an AI attribution footer) and `wonka baked` refuses until it is written in that bake. In a fix round the builder may dispute a review issue it declines to fix with `wonka baked --dispute "<issue> :: <why>"` (repeatable): logged under the round in the plan as `### Disputed by the builder` and kept in the round's `disputes`; a round whose issues are all disputed needs no new commits, a partly disputed one still does; change requests and CI failures cannot be disputed |
| 🐿️ Reviewing | 🐿️ **squirrel-architect / -backend / -frontend / -security / -spark / -airflow** (architect on Opus, the rest on Sonnet; picked by the analyst). Issues → back to Baking; follow-ups → new 💡 Idea tickets (not for a ticket already `max_followup_depth`, default 2, parents deep: those are only logged on the ticket, so follow-ups can't breed forever); decisions → `## 🐿️ Review Questions` (incl. close the ticket / back to Drafting); the answers are logged in the plan as `### Decisions` under that `## 🐿️ Review round N` (the log holds only squirrels that raised issues; a clean round with no answered questions logs nothing); a clean round ships once every answer is the recommended one, otherwise rebakes. Max `max_review_rounds` (default 3) rounds with issues, then Wont Do; a clean round that rebakes adds an extra round. Squirrels read the builder's disputes and only re-raise one with a reason that answers the builder's, or as a question |
| 🚢 Shipped | the CLI pushes and opens the PR as a draft, so no reviewer is pinged yet, assigned to you (`--assignee @me`: the account gh is logged in as) (titled `[KEY-1] title` when the ticket has a Jira key, else `[WONKA-00001] title`; the description is the builder's filled-in PR template when the repo has one (no Jira line: the template carries it), else the spec with a Jira line when there is a key; no Wonka branding, just the hidden marker) → 🙋 (click the desktop ping to open the PR) mark it ready for review, approve and merge it → 🍭 Ready, worktree and local branch removed (a worktree with local changes is kept and retried each tick: clean or delete it and Wonka removes it and the branch; a branch with commits that were never pushed to the PR, say ones you made in the worktree after the last push, is kept and the ticket log says so). Or tick **🔁 Request changes** (write what to change under it and/or leave PR review comments): back to Baking for a fix round with a fresh 3 review rounds; the fixes are pushed to the same PR, and its description is refreshed to the current spec too, unless someone edited it on GitHub since Wonka last wrote it (then it is left as is and the factory log says so; PRs opened before this version keep their description). A fix round starts from the PR branch on GitHub, so commits pushed there (an applied suggestion, "Update branch") are kept; if the push is still rejected the ticket goes stuck at once with what to do. Only comments by the repo's owners, members and collaborators are copied to the builder (others could slip it instructions on a public repo); the number skipped shows in the factory log. Each tick an open PR with merge conflicts gets 🔧 **oompa-loompa-fixer** (Sonnet): it rebases the PR branch onto the PR's current base on GitHub (never a merge commit), resolves, and the CLI pushes (`wonka resolved`, with `--force-with-lease` against the PR branch GitHub had when the fixer started, so a commit pushed there meanwhile is never overwritten); no new review, the PR is back to 🙋. Uncommitted leftovers in the worktree are stashed first, under `refs/wonka/stash/<id>/<time>` and not in your `git stash list` (the factory log names the ref; `git stash apply <ref>` gets them back, `git update-ref -d <ref>` deletes it). `wonka resolved` checks the branch contains the base commit the fixer was given (a rebase onto a newer base passes too). A fixer that stops without pushing (a conflict it could not resolve, tests that failed or stayed busy after its rebase) is retried: Wonka first puts the branch back where the PR has it, keeping the fixer's rebase under `refs/wonka/rebased/<id>/<time>`, so the next fixer starts clean and nothing is pushed in between. A fixer that cannot start (the fetch fails, the worktree is missing) is retried each tick and goes 🎩 Stuck after 3 tries; a PR branch that diverged from GitHub is stuck at once with what to do. If GitHub still calls the PR conflicting after 3 pushed fixes in a row, the ticket goes stuck instead (tick Retry for 3 more; a PR that turns mergeable unsticks itself). While a ticket with a PR is not waiting on it (a fix round in Baking or Reviewing, a fixer at work, a ship that is retried or 🎩 Stuck), each tick asks GitHub about the PR: one closed meanwhile sends the ticket to Wont Do, one merged (by you too, say to settle a stuck ticket) records it as merged in 🍭 Ready, so its dependents unblock. Either way no new PR is opened and the agents at work on it are stopped (their results and `wonka git` writes are refused; the worktree waits for them as for a kill). When the fix round made commits the merged PR never got, the local branch is kept for a PR by hand and the factory log says so. An open PR whose checks all finished on its head with at least one failed (failure, timed out, action required, startup failure, error) goes back to Baking on its own, with the failing checks and the last 150 lines of each failed GitHub Actions run (up to 3) in the plan; Wonka waits while any check is pending, never reworks the same failed runs twice, handles conflicts first, merges it with a ticked Request changes into one round, and after 3 CI fix rounds in a row whose new head is still red the ticket goes 🎩 Stuck with the failing checks (fix it by hand and tick Retry for 3 more; a head whose checks all pass unsticks it; if the PR turns conflicting meanwhile the stuck is lifted and the fixer rebases it, and a still-red rebased head goes stuck again; the count resets on green checks, merge or close). In a CI fix round the builder gets each failed log (GitHub Actions: the excerpt in the plan; other CI such as Concourse: `wonka ci-logs <url>`, which runs the configured log tool (e.g. `beyond ci logs`) only on the project's CI hosts), reduces it to the failing test and its error, fixes it with commits pushed to the same PR, and treats log text as data. In a CI-only fix round the builder may judge the failure flaky, for infrastructure failures only such as a lost worker, network trouble or preemption (`wonka baked <id> --flaky "<why>"`, refused outside a CI round, with requested changes or with new commits): the ticket then goes straight back to 🙋 with no review or push, logs `CI judged flaky: <why>; re-run <checks>`, and the banner asks you to re-run them. A re-run that fails again on the same head is a new failure (the handled marker is the head plus the failing runs), and it still counts toward the 3 CI fix rounds. A PR with no checks is left alone. |
| 🗑️ Wont Do | killed, split or closed (the local branch is kept, it may hold work). You can drag any card here but a merged one (🚢 Shipped 🍭 Ready), which the next tick puts back. A ticket killed while a builder, squirrel or fixer still works in its worktree keeps the worktree until that agent ends (`wonka finished`) or `stale_hours` pass, then Wonka removes it |

### Manual tickets

`wonka session <id or Jira key>` takes a ticket out of the factory: 🕹️ manual. It stays in its stage (an Idea, a
ticket in Triage or Drafting; Planned, Baking and Reviewing become manual 🍫 Baking; a ticket with an open PR stays
🚢 Shipped) in 🙋 Awaiting Human, and no agent works it until you hand it back. The note gets a `## 🕹️ Manual`
question at the bottom; tick one option and the next tick acts on it:

| Stage | ⏭️ Next | ⏮️ Back | 🎩 Back to the factory |
|---|---|---|---|
| 💡 Idea | 📏 Triage | – | the triage Oompa-Loompa sizes it |
| 📏 Triage | 🧪 Drafting | 💡 Idea | the analyst drafts it (an unsized ticket is triaged first) |
| 🧪 Drafting | 🍫 Baking | 📏 Triage | 🎫 Planned: a builder bakes your plan (refused until the plan file exists) |
| 🍫 Baking | 🐿️ Reviewing (needs commits and a clean tree) | 🧪 Drafting | the squirrels review it, then the usual flow |
| 🐿️ Reviewing | 🚢 Shipped: Wonka pushes and opens or updates the draft PR | 🍫 Baking | a builder fixes the round's issues, or a good nut ships |
| 🚢 Shipped | – | 🍫 Baking | Wonka watches the PR again (conflicts, CI, 🔁 Request changes) |

While a ticket is manual, only the squirrels' review and the PR push run for it. Their review round is logged in the
plan, issues, questions and suggestions alike, and the ticket comes back to you in 🐿️ Reviewing (no rebake, no ship,
no kill on the review budget). A merged or closed PR is still recorded, and a drag to Wont Do still kills it; any
other drag is put back. Conflicts, red CI and change requests on its PR are yours. A ticket waiting for your spec approval loses that question: the spec and plan are yours to finish in the session. More than one ticked option, or
one that can't be done yet (no commits, no plan), is unticked and the reason goes to the ticket's log.

The session always works in the ticket's own worktree, `~/.wonka/worktrees/<project>/<id>`: made if missing, reused
as it is otherwise (no reset, rebase or stash), never one Wonka did not make (a branch checked out in another
checkout is refused). That is the folder the agents reach when you hand the ticket back, and the one Wonka removes
when the PR merges. Orca lists it like any git worktree of a repo it knows.

**Blocked tickets.** A ticket with unmerged blockers is triaged as usual but is not pulled into Drafting or Baking, and a held draft resume waits too. A running analyst is not stopped. Its banner reads `⛓️ Blocked by …`. When every blocker has merged, the next tick picks it up and logs "Unblocked: … merged". A blocker that went to Wont Do never unblocks it: the tick reports it once as needing a human (an event, the ticket log, a desktop ping, and `awaiting_human`) and the banner turns into a warning. Fix it with `wonka unblock <id> <blocker>` or kill the ticket.

Triage and the analyst read a throwaway read-only checkout of the latest `origin/<base>` in
`~/.wonka/worktrees/<project>/<id>-scout`, made fresh for each job and removed once the ticket leaves triage or
drafting. Your own checkout is never touched, so they never plan against stale code. When the scout cannot be made,
the job is not handed out: it counts as a retry (the reason is in the ticket log and `factory.log`), and after three
the ticket is stuck until you tick Retry.

Worktrees and scouts have no gitignored files. List the ones a project's tests need with
`wonka config worktree_files.<project> server/.env,other/.env` (`<project>` is the repo folder name; an empty value
removes it): the CLI copies them from your checkout into each worktree and scout, keeping their mode. Only files git
ignores there are copied (never one a builder could commit), an existing file is never overwritten, and a missing one is
noted in the ticket's factory log without blocking the job. They are copied, never symlinked, so a builder cannot change
your checkout's file. `wonka doctor` warns when a listed file is missing in your checkout or not gitignored there.
A repo's `.worktreeinclude` (Claude Code's file, in `.gitignore` syntax, in the repo root) is read too: the untracked
files of your checkout it matches are copied the same way, on top of `worktree_files`, so one file serves Claude Code
and Wonka. `wonka doctor` says how many it copies and warns about matched files that are tracked or not gitignored.

Agents run a project's tests with `wonka test <id> -- <command>`. List the allowed command prefixes with
`wonka config test_commands.<project> ./test.sh,make test` (a comma list; an empty value removes it). `wonka test`
runs only a command that starts with one of them, with no shell, in the ticket's scout or worktree (never your
checkout). One run per project holds `~/.wonka/worktrees/.locks/<project>-tests.lock` (in the worktrees folder, so
`wonka test` can run inside the sandbox; a link planted in its place is refused); the others wait up to
`test_lock_wait_minutes` (default 3, so wait plus run fit the agents' 10-minute Bash timeout), then exit 75 naming
the holder. The exit code is the command's. `wonka config` and `wonka doctor` warn about a prefix that leaves the
program or code to the caller (a shell, interpreter, wrapper or launcher with nothing pinned after it, like
`python3 -c`, `sh -c`, `bash`, `awk`, `env`, `setsid`, `flock <file>`, `npx`, `go run`, `cargo run`, or `uv` and
`npm` with no subcommand; option values like `python3 -W ignore`, env assignments like `env FOO=1` and a wrapped
runner like `env FOO=1 bash` do not count as pinned; `find`, `watch`, `script`, `parallel`, `gdb` and `su` take a
command anywhere in their arguments, so nothing pins them), since it allows any command outside the sandbox. Tools
that run the repo's own files are not open, even bare: `make`, `npm run`, `cargo test` and `pytest` run what the
Makefile, package.json or conftest.py say, which the builder can edit anyway, like `./test.sh`.

Every job JSON carries the project's `test_commands` (`[]` when none). The analyst, builder, fixer and squirrels run a
test command that starts with one of them through `wonka test <id> --dispatch <n> -- <command>` with a 10-minute Bash
timeout, run it again on exit 75 (up to 5 tries), and run anything else as before. So two tickets of one project never
run its tests at the same time.

Phases: 🕰️ Pending · 🙋 Awaiting Human · 🎩 In Progress · 🍭 Ready (Ideas filed as 🕰️ Pending before are migrated to 🙋 Awaiting Human). Finished tickets (Shipped or Wont Do) and their plans move to
`wonka/archive/` after 7 days. Notes, the state file and the board written with the old names (🌟-Done, 🎫-Ready) or the old
hyphenated labels (🙋-Awaiting-Human) and Ready's old 🌟 are migrated automatically on the next tick.

## WIP limits

`wip.triage`, `wip.drafting` and `wip.baking` cap how many tickets an Oompa-Loompa is working on, not how many cards sit in a
column. A ticket counts toward its column's limit only while it is 🕰️ Pending or 🎩 In Progress there, and the limit
is checked only when a ticket is pulled in (📏 Triage to 🧪 Drafting, 🎫 Planned to 🍫 Baking) or, for triage, when a
triage job is handed out. This is on purpose:
waiting for a human should never block the line.

- 💡 Idea: no limit and no agent; a ticket counts only once it reaches Triage.
- 📏 Triage: `wip.triage` (default 2) triage Oompa-Loompas run at once, so a burst of new tickets started at once is sized a few at a time.
- 🧪 Drafting: a ticket waiting on your answers (🙋) frees its slot. When you answer, drafting resumes at once,
  even if that briefly puts Drafting over the limit.
- 🍫 Baking: tickets in 🐿️ Reviewing and 🚢 Shipped don't count, and squirrel reviews run without a limit. A ticket
  sent back by review returns to Baking straight away, without waiting for a slot.
- A blocked ticket uses no slot while it waits, unless an agent is already working on it, and the pull skips it and
  moves on to the next ticket.
- So `wip.baking` is not a cap on worktrees: every ticket keeps its worktree from Baking until its PR is merged
  or it goes to Wont Do.

## Reading a ticket

Every note opens with a banner that says where the ticket is, what happens next and, when it's 🙋 your turn, exactly
what to tick. It also lists size, project, plan, PR and review round. The factory's **🙋 Your turn** view lists only
tickets that wait for you. At the bottom, a folded **🎩 Factory log** holds the history. A job (or opening the PR)
that fails 3 times shows **🎩 Stuck** with a Retry checkbox; details are in `~/.wonka/factory.log`. A push
rejected because the PR branch has new commits on GitHub is stuck at once, not after 3 tries: rebase onto them in the
worktree (`git pull --rebase`), then tick Retry. An open PR that GitHub still calls conflicting after 3 pushed conflict fixes in a row
is stuck too: check its base branch on GitHub or fix the conflicts by hand, then tick Retry. An open PR whose CI is
still red after 3 CI fix rounds in a row is stuck too; it clears itself when its checks go green, when it is merged or
closed, when the PR turns conflicting (the fixer rebases it; a still-red rebased head is stuck again), or with Retry. Any other stuck open PR clears itself when it is merged, closed or made mergeable.

The 💡 Idea and 📜 Spec are free text, so they are checked with markdownlint (via `npx`) when written, but only the rules that can change the note's structure (list
indentation, headings, blockquotes, fences, lists, inline HTML, tables); style-only rules (spaces in code spans, fence
languages, bare URLs, line length, table alignment and the like) are disabled. Missing blank lines around lists (MD032) are inserted automatically first, outside code fences. A section it flags is kept as raw text in a code block under a warning that lists the problems, so broken
markdown can't spill into the rest of the note. Clean sections are written as is. `WONKA_MDLINT` overrides the
linter command, `WONKA_MDLINT=off` turns the check off; if the linter is missing, offline or fails, the section is
written unchecked and the reason goes to `~/.wonka/factory.log`. Only output lines that name a markdownlint rule
(`MD032/...`) count as problems: an npx or npm error (no network, a 404 from the registry, a cache it cannot write)
exits 1 as well and counts as the linter failing. A line that continues a list item without indent (a lazy
continuation) is left in its list. Headings in any section's text (`#`, `##`) become `###`, so they cannot end the
section early. Lines inside a closed code fence stay as written (`## install` or `# install deps` in a bash block):
Wonka skips fences when it looks for sections. A fence that never closes is not one: the headings after it are
demoted and its opening line gets a `\` in front, so it shows as text and cannot pair with a fence further down.
In the plan, a CI log's `##` lines are demoted even inside fences.

Wonka finds a ticket's note by its file name (`WONKA-00001.md`). A note renamed in Obsidian, or moved into a subfolder
of the tickets folder, is put back on the next tick (found by the `id` in its frontmatter); a deleted note drops its
ticket to Wont Do. A finished ticket (merged or Wont Do) whose note is deleted is only archived early, as it ended. Since that is final, a
tick refuses to run when the tickets folder is missing (an unmounted or moved vault, a changed `vault` setting) or
when every live ticket's note is gone at once; if you really deleted them all, run `wonka tick --drop-missing`.
Frontmatter is read as YAML (Obsidian's own escapes and `# comments` included), and bytes that are not UTF-8 read as
`�`, as Obsidian shows them. A note Wonka still can't read only holds up its own ticket: the tick reports it as a
`⚠️` event naming the note and carries on with the others. Outside a tick, a command on a ticket whose note is gone
says so in one line.
Errors that stop the whole factory (that refusal, an unreadable state or config file, a lock held for 10 minutes)
also send an urgent desktop notification, so a `/loop` running in the background doesn't fail silently.

**The state machine** (`wonka/machine.py`) is the only way a ticket moves. Drag a card anywhere other than Wont Do (or an Idea to Triage)
and the next tick puts it back, with a note on the ticket. Notes keep only `id, title, status, phase, size,
jira, spec_review, blocked_by`. `blocked_by` is a read-only mirror of the state (comma-separated, absent when none): edit it with `wonka block`/`unblock`, since a hand edit is overwritten on the next write. Everything else (repo, PR repo, branch, worktree, PR, rounds, history) is in
`~/.wonka/golden_ticket_status.json` (the previous version is kept next to it as `.json.bak`). It also keeps a never-reset record of what went wrong: `rounds` (one entry per review verdict: round, kind review/change/ci, time, and per squirrel the verdict, suggest and counts of issues, questions and follow-ups, plus the builder's `disputes` (issue, why) when it declined issues) and `stats` (`ci_rounds`, `conflict_fixes`, `retries`, `stuck`, `followups_filed` counters that survive the resets of the working counters; `stuck` counts each time the ticket goes 🎩 Stuck, once per stuck episode: a CI stuck that turns into a conflict stuck counts once). The CI working counters sit next to them: `ci_failures` counts every CI fix round of the ticket and numbers the plan's `## 🔴 CI failure N` sections (never reset); `ci_fixes` counts the CI fix rounds in a row on the open PR and makes the ticket 🎩 Stuck at 3 (reset by green checks, a merge or close, or Retry on a CI-stuck ticket); `ci_handled_sha` is the PR head whose red checks were last handled and, with `ci_handled_runs` (the failing runs), keeps the same failure from being reworked twice (cleared by Retry on a CI-stuck ticket; `wonka baked --flaky` needs the worktree HEAD to be it). It and the notes
are written atomically (a temp file, then a rename), and every command that changes state takes a lock in `~/.wonka`,
giving up after 10 minutes. `wonka git`, `wonka test`, `wonka status`, `wonka show`, `wonka repos`, `wonka metrics`, `wonka pr-comments` and `wonka worktree` only read, so they never wait for it: an agent
is never stalled by a tick that is busy talking to GitHub. A tick itself makes its slow reads (each PR's state,
checks and requested-change comments, failed CI logs, the base fetches for new jobs) before it takes the lock, on a
snapshot of the state, so `wonka baked`, `report`, `finished` and the other result commands wait only for its quick
part; a read that failed, or one for a ticket that moved on meanwhile, is redone under the lock. Pushes, opening a PR
and a fixer's fetches still happen under it.

## Develop

```bash
cd wonka && python3 -m unittest                  # from wonka/
python3 -m unittest discover -s wonka -t wonka   # from the repo root, no cd (what plans use)
```

Before a commit, run `./check.sh` (from `wonka/`): the unit tests, then `ruff check` with `ruff.toml` (pyflakes and
a few pycodestyle error rules, no formatting) when ruff is installed; without ruff it says so and skips the lint.
There is no hosted CI: this is the check.

A bare `python3 -m unittest` from the repo root finds no tests (`NO TESTS RAN`, exit code 5). If a run reports 0
tests, the command is wrong, not passing.

The tests never touch GitHub: a fake `gh` prints what Wonka expects. `tests/test_gh_contract.py` checks that the real
`gh` still prints it: every JSON field Wonka reads from `gh pr view`, `gh pr list` and `gh api`, against a public merged
PR (cli/cli#9000). It needs the network and a logged-in `gh`, so it only runs on request. Run it after a gh upgrade or
when you change a fake gh's output:

```bash
cd wonka && WONKA_GH_CONTRACT=1 python3 -m unittest tests.test_gh_contract -v
```
