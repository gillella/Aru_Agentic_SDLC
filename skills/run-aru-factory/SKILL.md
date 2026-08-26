---
name: run-aru-factory
description: Sole lifecycle router for the Aru_Agentic_SDLC client-work kernel. Offers adopt, status, next, loop, and doctor modes. Use when the user says "aru code" (or aru software/dev/sdlc), aru, please continue, continue, keep going, run the factory, work the project board, continue development, adopt this project, what is the factory doing, or check the factory setup.
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
  - "what is the factory doing"
  - "check the factory setup"
do_not_trigger_for:
  - "a specific lifecycle step the user named directly (use that skill)"
  - "filing an issue without doing the work (use create-github-issue)"
---

# Run Aru Factory

This is the only runtime lifecycle router. It delegates to the retained kernel
and never creates a second queue, local lifecycle ledger, or background
supervision process. GitHub Issues and the Project Board are authoritative.

Canonical home: `$ARU_SDLC_HOME`.

## Identity and the single picker

The picker derives a stable agent id when `--agent` is omitted. `ARU_AGENT_ID`
may pin a readable id, and `--family` records model-family attribution.

Every `next` or `loop` iteration starts with exactly one selection call:

```shell
python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" \
  [--agent <AGENT_ID>] [--family <FAMILY>] --claim --json
```

The returned top-level `agent` is authoritative for later helper calls. The
picker resumes work already held by that id before selecting anything new.

## Modes

| mode | what it does | delegates to |
|---|---|---|
| `adopt` | bootstrap an ungoverned repository | `init-agent-project` |
| `status` | report current state without mutation | `scripts/fleet_status.py` |
| `next` | perform one returned work item, then stop | matching kernel skill/helper |
| `loop` | keep the current desktop task processing picker results | the `next` routing table |
| `doctor` | inspect local integration health without mutation | doctor helper |

`please continue`, `continue`, and `keep going` mean `loop`. Default to `next`
only when the user requests one unit of work without naming a mode. Reject an
unknown mode rather than guessing.

### adopt

If the repository lacks an `AGENTS.md` carrying the Issue-First Law, follow
`init-agent-project`. Bootstrapping is its own outcome. If governance already
exists, continue as `next`.

### status

```shell
python3 "$ARU_SDLC_HOME/scripts/fleet_status.py" [--json]
```

This helper is compact and read-only. Report its complete, waiting, blocked,
or error result without claiming work.

### next

Call the single picker once and route its one returned item:

| type | action |
|---|---|
| `feedback` | follow `address-pr-feedback` |
| `review` | follow `code-review` only for this id's preassigned terminal `review:agent` fallback |
| `issue` | follow `implement-next-issue` |
| `merge` | invoke `merge_pr.py` as described under **Merging** |
| `idle` or `error` | report the returned state and stop |

Feedback and errors carry no new claim. Merge and non-resume issue results may
perform the needed claim mutation; resume results report work already held.

For `review`, require exactly `review:agent`, exactly
`reviewer:<AGENT_ID>`, and a different non-empty `author:<id>`. The picker only
recovers this operator-created emergency assignment. It never assigns ordinary
coding-agent review. If the assignment is malformed, do not inspect the diff;
release only this agent's own claim and report the problem.

A claim race ends that iteration, not the session. In loop mode, ask the single
picker again; never steal another agent's claim.

### loop

Read `prompts/fleet-worker.md` as the dispatched coding worker's lifecycle authority; Hermes does not execute that worker loop inline. The Hermes orchestration boundary is a short `snapshot → reconcile → decide → dispatch → report` control tick that returns to app-native waiting after dispatch/report and never waits synchronously for coding-worker, CI, or external-review completion. Worker-local CI waits and post-mutation picker transitions remain inside the dispatched worker task and neither extend the Hermes tick nor count as picker calls by that tick. This slice defines that boundary only: durable worker-state persistence belongs to #479 and the worker prompt protocol belongs to #480, so it does not rewrite either subsystem here.
Each Hermes tick starts with **exactly one authoritative picker call**, the `fetch_next_work.py --claim --json` command under **next**; its result is the tick snapshot and the only routine GitHub-bearing entrypoint for that tick.
Do not preflight or enrich it with `fleet_status.py`, `triage_backlog.py`, direct `gh issue` / `gh pr` views, `check_ci.py`, or `merge_pr.py --dry-run`.
Pace dynamically: after a successful mutation invalidates the prior snapshot, finish `report`; that report ends the current tick, and exactly one fresh picker call occurs only at the start of the next tick.
For unchanged, `idle`, a returned `error` work item, Complete, review/CI/dependency wait, rate limit, exhausted credits, or any usable picker result carrying a transient helper/degraded warning, make zero follow-up GitHub reads.
Build the **Status Card** only from the picker result, mark unavailable fields honestly, and use app-native wait or background primitives with a long fallback heartbeat, not a fixed interval. Enforce same-job single-flight: a later tick recovers durable claims, worktrees, and PR state instead of duplicating dispatch, and creates no repository-local daemon, private task queue, or competing scheduler. Routing preserves test ownership: consumer repositories follow their own `AGENTS.md` testing policy, while Aru's focused-predicate exception remains local to `Aru_Agentic_SDLC`.
A returned `error` work item is a usable snapshot and transient loop state, not a concrete failure: report its reason, wait, and read nothing further. A picker/helper failure that yields no usable snapshot also ends the routine tick with zero further reads by default. Full diagnostics are a separately declared attempt—replacing, not enriching, a routine tick—for an explicit operator `status` / `doctor` request or that concrete failure. Do not emit a final response for a recoverable state.

Loop mode ends only when the operator stops it or an actual product/security/
scope decision is required. Record the blocker on the linked issue or PR and
stop with the exact decision needed. Do not create another notification path,
task queue, or process to own continuity.

### doctor

Doctor is read-only. It must never claim, label, branch, commit, or open work.

```shell
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py"
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py" --json \
  --project /absolute/path/to/repo
```

It checks integration links, governance markers, repository identity,
worktrees, `git`, and `gh auth status` without printing credentials. Exit `0`
is healthy, `2` degraded, and `1` invalid.

## Rules that hold in every mode

1. **Issue first.** No code change without a claimed, open, tracked issue.
2. **GitHub through helpers.** Lifecycle mutations use
   `$ARU_SDLC_HOME/scripts/*.py`; direct `gh` is limited to operations without
   a helper, such as the implementation-plan comment. GitHub MCP is not an
   authority.
3. **Worktree isolation.** Feature and remediation work stays under
   `.worktrees/` and uses the exact stable agent id.
4. **Stay inside `touches:`.** Widen the issue declaration before any necessary
   out-of-budget edit.
5. **Focused verification.** Run every issue predicate and directly affected
   lint, syntax, documentation, and build check before pushing.
6. **Linked PRs.** Every implementation PR contains `Closes #<issue>` and is
   created through `create_pr.py --agent <id> --model-family <family>`.
7. **Degraded coordination stops safely.** Never invent a secondary task queue
   or bypass the merge gate.
8. **Exactly one ordinary review authority.** `create_pr.py` assigns exactly
   one of `review:coderabbit`, `review:sourcery`, or `review:codeant` using the
   deterministic least-loaded complete open-PR inventory rule. The assignment
   is immutable unless an operator records one audited external reassignment.
   Only after external exhaustion or an operator-declared excessive wait may
   one independent `review:agent` become the terminal fallback. Authors never
   review their own work.

## Merging

After the assigned authority supplies completed exact-current-head evidence
and every Definition-of-Done gate passes, any factory agent may execute:

```shell
python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" \
  --pr <N> --expected-head <HEAD_SHA>
```

Use the picker-supplied head. Do not add a separate dry-run or claim read. On a
blocked or head-mismatch result, release only the merger claim through
`claim_issue.py` and return to the picker. Direct pushes and ad-hoc GitHub
merges have no authority. A finding closes only through a later fix commit or
an authorized `Withdrawn:` reply.

## References

- Board lifecycle: `$ARU_SDLC_HOME/docs/project_board_workflow.md`
- Commit and test standards: `$ARU_SDLC_HOME/docs/coding_standards.md`
