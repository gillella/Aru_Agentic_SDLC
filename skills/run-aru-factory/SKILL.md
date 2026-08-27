---
name: run-aru-factory
description: Single entrypoint to the Aru_Agentic_SDLC factory for any local coding agent. Selects the right lifecycle skill and offers adopt, status, next, loop, and doctor modes. Use when the user says "aru code" (or aru software/dev/sdlc), aru, please continue, continue, keep going, run the factory, work the project board, continue development, adopt this project, keep going on the backlog, what is the factory doing, or check the factory setup.
triggers:
  - "please continue"
  - "continue"
  - "keep going"
  - "run the factory"
  - "aru code"
  - "aru software"
  - "aru dev"
  - "aru sdlc"
  - "aru"
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

Canonical home: `$ARU_SDLC_HOME`.
## Identity

The picker auto-assigns an **agent id** when `--agent` is omitted, and its
model family is optional:

```text
[--agent <AGENT_ID>] [--family <FAMILY>]
```

Every agent authenticates as the same GitHub user, so these labels provide
durable author/remediator routing and audit attribution.

Omit `--agent` and the picker derives a stable id from where
this agent runs — `<product>-<fingerprint>`, e.g. `claude-a3f19c`. The same
machine, checkout, and family always resolve to the same id, so a restarted
session reclaims its own board work, and two machines can never be issued one
id. `ARU_AGENT_ID` pins an id explicitly; pass `--agent <AGENT_ID>` when a
fixed readable name such as `claude-1` or `codex-1` is preferred.

```shell
python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" [--agent <AGENT_ID>] [--family <FAMILY>] --claim --json
```

## Modes

| mode | what it does | delegates to |
|---|---|---|
| `adopt` | make an ungoverned repo governed, then start work | `init-agent-project` |
| `status` | report factory state without changing anything | `scripts/fleet_status.py` |
| `next` | do exactly one unit of work, then stop | picker + the matching skill |
| `loop` | keep this desktop task working until stop/intervention | `prompts/fleet-worker.md` |
| `doctor` | check the local setup, read-only | see **doctor** below |

`please continue`, `continue`, and `keep going` are **`loop`**, not `next` and
not `implement-next-issue`. In Codex, Claude, Cursor, or Antigravity desktop, the
task the operator started owns the loop; do not replace it with a separately
launched CLI agent. The board is the session store — recover in-flight work
before anything new. Default to `next` only when the user named no mode and
wants one unit of work. An unrecognised mode is an error — say so and list the
five. Never silently fall through to `next`: guessing wrong starts real work
the user did not ask for.

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

### next

**Recover before you claim.** Ask the picker with your id — it returns work
you already hold, marked `resuming`, before offering anything new. Finishing
beats starting, and an abandoned claim blocks the board for everyone else.

```shell
python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" [--agent <AGENT_ID>] [--family <FAMILY>] --claim --json
```

It returns one item. `feedback`, `error`, and `idle` are returned without
claims. `merge` and non-resume issue paths perform the claim mutations;
resume output reports already-held work instead of claiming it again. Follow
the skill for its type:

| type | skill |
|---|---|
| `feedback` | `address-pr-feedback` |
| `review` | `code-review`, only when the picker is resuming this exact agent's preassigned `review:agent` emergency fallback |
| `issue` with `skill: research` | `research` |
| any other `issue` | `implement-next-issue` |
| `merge` | `merge_pr.py` only — see **merging** |
| `idle` | `next` stops; `loop` waits and asks again |

The picker's top-level `agent` field is the resolved identity; use that exact
value as `<AGENT_ID>` for later `claim_issue.py` and `create_pr.py` calls,
including unnamed workers. The picker's `work.skill` field is authoritative — a research issue has its own
close-out contract in `$ARU_SDLC_HOME/skills/research/SKILL.md`, so do not route
every issue through implementation.

If `work.type=review` lacks exactly `review:agent` plus
`reviewer:<AGENT_ID>`, do not inspect the PR; release only this agent's malformed
claim with `claim_issue.py --release` and return to the picker. A valid item follows `code-review`. The picker
never selects or claims normal coding-agent review work; it only recovers an
operator-created emergency assignment.

**If the claim conflicts**, another agent won the race. That ends the
iteration, not the session — ask the picker again. Treating a lost race as an
error strands the agent while the board still has work.

### loop

Read `prompts/fleet-worker.md`, then run its loop **inside the current desktop
task**. Each tick starts with **exactly one authoritative picker call**, the `fetch_next_work.py --claim --json` command under **next**; its result is the tick snapshot and the only routine GitHub-bearing entrypoint.
Do not preflight or enrich it with `fleet_status.py`, `triage_backlog.py`, direct `gh issue` / `gh pr` views, `check_ci.py`, or `merge_pr.py --dry-run`.
Pace dynamically: after a successful mutation invalidates the prior snapshot, ask the picker exactly once again immediately.
For unchanged, `idle`, a returned `error` work item, Complete, review/CI/dependency wait, rate limit, exhausted credits, or any usable picker result carrying a transient helper/degraded warning, make zero follow-up GitHub reads.
Build the **Status Card** only from the picker result, mark unavailable fields honestly, and use app-native wait or background primitives with a long fallback heartbeat, not a fixed interval.
A returned `error` work item is a usable snapshot and transient loop state, not a concrete failure: report its reason, wait, and read nothing further. A picker/helper failure that yields no usable snapshot also ends the routine tick with zero further reads by default. Full diagnostics are a separately declared attempt—replacing, not enriching, a routine tick—for an explicit operator `status` / `doctor` request or that concrete failure. Do not emit a final response for a recoverable state.
Loop mode ends intentionally only when the operator explicitly stops it or a
decision needs human intervention. If `$HOME/.aru/factory-loop.stop` applies
here, stop immediately and do not arm native wakes. Ambiguous board identity,
unresolved `touches:` conflicts, money semantics, security posture, or hard
rules can require intervention; explain the exact decision needed on the linked
issue or PR, then notify Slack with
`$ARU_SDLC_HOME/scripts/slack_notify.py` with the linked issue or PR and every
flag below.
`waiting-on` covers dependency and claim waits, `blocked` other blocks, `hitl`
an operator mention; idle ticks and heartbeats never notify. Examples:
`prompts/fleet-worker.md`.

```text
--project-id <PROJECT_ID> --agent <AGENT_ID> --family <FAMILY>
--event <blocked|waiting-on|hitl> --repo <OWNER/REPO> --repo-dir <CONSUMER_REPO_ROOT>
```
Routine helper exits `1` and repeated CI/review rounds do not end the loop.
Exception: post-merge exit `1` with a durable `## Human intervention required`
means `merge_pr.py` exhausted retries; notify Slack (`hitl`) and stop. When
context is running short, recover through the desktop product's context
compaction and durable GitHub/worktree state, then continue.

`scripts/run_fleet.py` remains an **optional headless CLI mode**; the ephemeral
launcher attests Codex/OpenAI, Claude/Anthropic, and Gemini/Google —
never a Cursor-selected model or desktop UI. Neither resumes this task, and
never use UI scripting. Details: [fleet runner](../../docs/fleet-runner.md) and
`$ARU_SDLC_HOME/docs/desktop-agent-continuity.md`.

### doctor

Read-only. It must never claim, label, branch, commit, or open anything.

```shell
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py"
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py" --json \
  --project /absolute/path/to/repo
```

It diagnoses continuity adapters and install links — `$ARU_SDLC_HOME`, per-agent
skill links, invocation surfaces, governance blocks, `git`/`gh`, and `gh auth
status` without printing credentials. `--project` adds the repo's remote,
Issue-First marker, board identity, pre-push hook, and in-flight worktrees.

Exit `0` healthy, `2` degraded, `1` invalid. Each failed check includes a
repair recommendation. GitHub MCP is optional and non-authoritative.

Do not treat a capability gap (Claude/Cursor same-task wake is session-only)
as an install failure; it is reported, not repaired by this command.

## Rules that hold in every mode

Restated only because skipping one is how each has been broken before.
`AGENTS.md` is the authority.

1. **Issue-First.** No code change without a claimed, open, tracked issue.
2. **GitHub is `gh` plus helpers, not MCP.** Lifecycle mutations go through
   `$ARU_SDLC_HOME/scripts/*.py`. `gh auth status` is the identity check.
   Slack is not a queue; direct `gh` only when no helper exists (`gh issue comment`).
3. **Worktree isolation.** Feature and remediation work happens under `.worktrees/`,
   in a directory scoped to your agent id. Run helpers by absolute path and `cd`
   into the worktree before editing.
4. **Stay inside `touches:`.** To write outside it, widen the declaration on the
   issue *first* and say why — never silently. Over-declaring harms too: a glob
   like `tests/**` serialises issues that never really overlap.
5. **Verify locally before pushing.** `ruff check .` and the test suite, both
   clean. Report failures with their output; never claim a check you did not
   run.
6. **Every PR carries `Closes #<issue>`**. `create_pr.py` requires a non-empty
   `--agent <id>`; `--model-family <family>` is optional.
7. **Degraded GitHub halts coordination gracefully** — never a secondary local
   task queue or an ungated merge. See `docs/degraded-mode.md`.
8. **Agent review is emergency-only.** `create_pr.py` assigns exactly one immutable ordinary authority
   from CodeRabbit, Sourcery, or CodeAnt using the deterministic balanced policy;
   reassignment is never automatic. An operator may make at most one audited external reassignment
   with `reassign_review.py` after concrete unavailability or excessive wait, never automatic rotation
   or retry. `review:agent` remains a terminal fallback selected only after external exhaustion or an
   operator-declared excessive wait. The picker resumes it but never creates a coding-agent review
   queue. Self-review, stale heads, labels alone, and malformed or duplicate evidence fail closed.

### Merging

After the assigned reviewer has supplied exact-current-head evidence
and every DoD gate passes, any factory agent, including the implementation author,
may execute the merge
helper with the picker-supplied `head_sha` pinned as `--expected-head`:

```shell
python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <N> --expected-head <HEAD_SHA>
```

The picker already claimed and evaluated the merge: do not add a claim-confirmation read or separate `merge_pr.py --dry-run`; invoke the helper once, then make one fresh picker call only after a successful mutation. On exit `3` (blocked or head mismatch), release the merger claim with `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --pr <N> --agent <AGENT_ID> --merge --release`, then return to the picker.
Direct pushes and `gh pr merge` have no merge authority. A finding closes by a
commit or an explicit `Withdrawn:` reply — resolving a thread proves nothing.

## References

- Loop contract: `$ARU_SDLC_HOME/prompts/fleet-worker.md`
- Router: `$ARU_SDLC_HOME/skills/aru-agentic-sdlc/SKILL.md`
- Board lifecycle: `$ARU_SDLC_HOME/docs/project_board_workflow.md`
- Commit and test standards: `$ARU_SDLC_HOME/docs/coding_standards.md`
