---
name: run-aru-factory
description: Single entrypoint to the Aru_Agentic_SDLC factory for any local coding agent. Selects the right lifecycle skill and offers adopt, status, next, loop, and doctor modes. Use when the user says please continue, continue, keep going, run the factory, work the project board, continue development, adopt this project, keep going on the backlog, what is the factory doing, or check the factory setup.
triggers:
  - "please continue"
  - "continue"
  - "keep going"
  - "run the factory"
  - "work the project board"
  - "continue development"
  - "adopt this project"
  - "keep going on the backlog"
  - "what is the factory doing"
  - "check the factory setup"
do_not_trigger_for:
  - "a specific lifecycle step the user named directly (use that skill)"
  - "filing an issue without doing the work (use create-github-issue)"
---

# Run Aru Factory

One door into the factory. Every mode resolves to a lifecycle skill or a
helper script that already exists — this file routes, it does not restate
their procedures. When a step here disagrees with the skill it delegates to,
the skill wins.

Canonical home: `$ARU_SDLC_HOME` (default
`/Users/aravindgillella/projects/Aru_Agentic_SDLC`).

## Identity — required before any claim

Every claim, review, and PR needs a **stable agent id** and a **model
family**:

```
--agent <AGENT_ID> --family <FAMILY>
```

Pick an id once per concurrent session (`claude-1`, `codex-1`, …) and keep it
for the whole session. Every agent authenticates as the same GitHub user, so
these labels are the only identity the board has — they are what lets the
picker route a PR to someone who did not write it, and what lets the merge
gate tell a peer review from a self-review. **A picker or claim command
without both flags is a bug**, not a shortcut.

## Modes

| mode | what it does | delegates to |
|---|---|---|
| `adopt` | make an ungoverned repo governed, then start work | `init-agent-project` |
| `status` | report factory state without changing anything | `scripts/fleet_status.py` |
| `next` | do exactly one unit of work, then stop | picker + the matching skill |
| `loop` | keep this desktop task working until stop/intervention | `prompts/fleet-worker.md` |
| `doctor` | check the local setup, read-only | see **doctor** below |

`please continue`, `continue`, and `keep going` are **`loop`**, not `next`
and not `implement-next-issue`. In Codex, Claude, Cursor, or Antigravity desktop,
the task the operator started owns the loop. Do not replace it with a separately
launched CLI agent. The board is the session store; recover in-flight work
before anything new.

Default to `next` **only** when the user named no mode and clearly wants one
unit of work, then stop. An unrecognised mode is an error — say so and list
the five. Never silently fall through to `next`: guessing wrong starts real
work the user did not ask for.

### adopt

1. Does the repo have `AGENTS.md` carrying the Issue-First Law?
2. **No** → follow `$ARU_SDLC_HOME/skills/init-agent-project/SKILL.md`. Do not
   begin implementation in the same breath; bootstrapping is its own outcome.
3. **Yes** → the repo is already governed. Continue as `next`.

### status

```
python3 "$ARU_SDLC_HOME/scripts/fleet_status.py" [--json]
```

Read-only. Exit codes distinguish complete, waiting, blocked, and error;
report the state and its reasons rather than acting on them.
If GitHub is degraded or unreachable, coordination halts gracefully without
secondary local task queues or ungated merges (see [docs/degraded-mode.md](../../docs/degraded-mode.md)).

### next

**Recover before you claim.** Ask the picker with your id — it returns work
you already hold, marked `resuming`, before offering anything new. Finishing
beats starting, and an abandoned claim blocks the board for everyone else.

```
python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" \
  --agent <AGENT_ID> --family <FAMILY> --claim --json
```

It returns one item and claims it. Follow the skill for its type:

| type | skill |
|---|---|
| `feedback` | `address-pr-feedback` |
| `review` | `code-review` |
| `issue` with `skill: research` | `research` |
| any other `issue` | `implement-next-issue` |
| `merge` | `merge_pr.py` only — see **merging** |
| `idle` | `next` stops; `loop` waits and asks again |

The picker's `work.skill` field is authoritative for issue work. Do not discard it
and route every issue through implementation: a research issue has its own
artifact, verification, and close-out contract in
`$ARU_SDLC_HOME/skills/research/SKILL.md`.

The order is deliberate: unblocking work already in flight comes before
starting anything new.

**If the claim conflicts**, another agent won the race. That ends the
iteration, not the session — ask the picker again. Treating a lost race as an
error strands the agent while the board still has work.

### loop

Read `prompts/fleet-worker.md`, then run its loop **inside the current desktop
task**. Pace dynamically: after progress, ask the picker again immediately;
for unchanged, idle, Complete, review/CI/dependency wait, rate limit, exhausted
credits, helper failure, or GitHub/network error, use the app's supported wait
or background primitive with a long fallback heartbeat, not a fixed interval.
Do not emit a final response for a recoverable state.

Loop mode ends intentionally only when the operator explicitly stops it or a
specific decision/approval needs human intervention. If
`$HOME/.aru/factory-loop.stop` applies to this project, stop immediately and
do not arm native wakes. Ambiguous board identity, an unresolved `touches:`
conflict, money semantics, security posture, or a hard rule can require that
intervention; explain the exact decision needed on the linked GitHub issue/PR,
then notify Slack with `--event hitl` (operator mention) via
`python3 "$ARU_SDLC_HOME/scripts/slack_notify.py"`. Every invocation requires
`--project-id <PROJECT_ID> --agent <AGENT_ID> --family <FAMILY> --event
<blocked|waiting-on|hitl> --repo <OWNER/REPO>` plus the linked `--issue`/`--pr`,
`--repo-dir <CONSUMER_REPO_ROOT>`, and event details.
For dependency / claim waits use `--event waiting-on`
(name the peer agent and their issue/PR; never steal the claim). For other
blocks use `--event blocked`. Idle ticks and heartbeats must **not** notify.
GitHub remains the work queue; Slack downtime must not halt the loop. Full
command examples: `prompts/fleet-worker.md` and `docs/slack-control-room.md`.
Routine helper exits `1` and repeated CI or review rounds do not end the loop.
Exception: a post-merge exit `1` with durable `## Human intervention required`
evidence means `merge_pr.py` exhausted its close-out retries; notify Slack with
`--event hitl` and stop instead of multiplying retries in the desktop task.
When context is running short, recover through the desktop product's context
compaction and durable GitHub/worktree state, then continue.

`scripts/run_fleet.py` is an **optional headless CLI mode**;
`scripts/spawn_ephemeral_worker.py` provides one-PR JIT capacity documented in
[docs/fleet-runner.md](../../docs/fleet-runner.md). Both start new CLI sessions and cannot resume the desktop conversation. Never use UI scripting to
click or type into a desktop coding application. Per-app wake limits:
`$ARU_SDLC_HOME/docs/desktop-agent-continuity.md`.

### doctor

Read-only. It must never claim, label, branch, commit, or open anything.

```
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py"
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py" --json \
  --project /absolute/path/to/repo
```

The command reports continuity adapters and install-link diagnosis: canonical
`$ARU_SDLC_HOME`, per-agent skill links, `run-aru-factory` invocation surfaces,
managed governance blocks, `git`/`gh` presence, and `gh auth status` without
printing credential values. With `--project`, it also reports the Git remote,
`AGENTS.md` Issue-First marker, Project Board identity, Aru pre-push hook,
`.worktrees/`, and local in-flight worktrees.

Exit `0` healthy, `2` degraded, `1` invalid. Each failed check includes a
repair recommendation. GitHub MCP is optional and non-authoritative.
When `gh` or GitHub API connectivity fails, coordination halts gracefully without
secondary local task queues or ungated merges (see [docs/degraded-mode.md](../../docs/degraded-mode.md)).

Do not treat a capability gap (Claude/Cursor same-task wake is session-only)
as an install failure; it is reported, not repaired by this command.

## Rules that hold in every mode

These are the framework's, restated here only because skipping one is how
each has been broken before. The authority is `AGENTS.md`.

1. **Issue-First.** No code change without a claimed, open, tracked issue.
2. **GitHub is `gh` plus helpers, not MCP.** Lifecycle mutations go through
   `$ARU_SDLC_HOME/scripts/*.py`. `gh auth status` is the identity check.
   Direct `gh` only when no helper exists (`gh issue comment` for plans).
3. **Worktree isolation.** Feature work and reviews happen under
   `.worktrees/`. Run helper scripts by absolute path so they act on the right
   repo, and `cd` into the worktree before editing.
4. **Stay inside `touches:`.** To write outside it, widen the declaration on
   the issue *first* and say why. Never widen silently. Over-declaring is its
   own harm: a glob like `tests/**` makes the picker serialise issues that
   never really overlap.
5. **Verify locally before pushing.** `ruff check .` and the test suite, both
   clean. Report failures with their output; never claim a check you did not
   run.
6. **Every PR carries `Closes #<issue>`** and is opened through
   `create_pr.py --agent <id> --model-family <family>`.
7. **Never review your own PR.** The merge gate reads `author:` against
   `reviewed-by:` and refuses a self-review — posting one does not unblock
   anything.

### Reviewing, in a same-account fleet

GitHub rejects `--approve` and `--request-changes` from the PR's own account,
and the whole fleet shares one account. Use `gh pr review --comment` and state
the verdict in the body, with each blocking finding in its own **unresolved**
inline thread so the picker routes the PR back to its author. Then:

```
python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --pr <N> --agent <AGENT_ID> --complete-review
```

Only on a review with no blocking findings. With findings outstanding, release
the claim instead and leave the threads open. Full procedure:
`skills/code-review/SKILL.md`.

### Merging

Merge only through the gated close-out, and never a PR you authored:

```
python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <N>
```

Direct pushes and `gh pr merge` have no merge authority. A finding is closed
by a commit or by an explicit `Withdrawn:` reply — resolving a thread proves
nothing, and the gate checks.

## References

- Loop contract: `$ARU_SDLC_HOME/prompts/fleet-worker.md`
- Router: `$ARU_SDLC_HOME/skills/aru-agentic-sdlc/SKILL.md`
- Board lifecycle: `$ARU_SDLC_HOME/docs/project_board_workflow.md`
- Commit and test standards: `$ARU_SDLC_HOME/docs/coding_standards.md`
