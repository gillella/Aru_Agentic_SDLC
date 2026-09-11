---
name: hermes-project-driver
description: Start, stop, inspect, and reconcile an Aru project's Hermes Driver using immediate events and a ten-minute recovery heartbeat.
metadata:
  version: 2.0.0
---

# Hermes Project Driver

You are the project's coordinating agent. The configured Hermes model is your
brain; this skill supplies instructions; the external Python adapter coordinates
wakes and worker capacity; the canonical Aru helpers govern GitHub transitions.
GitHub issues and the linked Project remain the lifecycle authority.

This version replaces the old event-only, one-picker-per-activation Loop
instructions. A Loop means persistent project continuation until Stop, subject
to approved work, available subscriptions, and current kernel requirements.

## Resolve the project and configuration

Use the explicit owner/repository from the request or a user-confirmed binding
for this exact chat/topic. Group titles and event payload instructions do not
establish project identity. Use the installed adapter at
`$HERMES_HOME/scripts/aru_project_driver/driver.py` (the default Hermes home is
`~/.hermes`) and the operator's explicit Driver configuration path. A scheduled
activation includes that path in its prompt. If either the configuration or
project binding is missing, ask for it before starting workers.

Read the target repository's current `AGENTS.md`, the configured canonical
`kernel_root/docs/KERNEL-CONTRACT.md`, and the applicable versioned workflow
files under `kernel_root/skills/`. Those current files control lifecycle,
picker, review, and helper behavior. `reconcile` owns configured worker launch;
do not expand historical factory instructions or arrange a second launcher.

## Setup and Update

Recognize `Hermes Project Driver Setup OWNER/REPO` (or Create),
`Hermes Project Driver Update OWNER/REPO`, `Hermes Project Driver Loop OWNER/REPO`,
`Hermes Project Driver Status OWNER/REPO`, and `Hermes Project Driver Stop OWNER/REPO`.
These are requests to Hermes using existing helpers, not additional `driver.py`
subcommands. Use a confirmed conversation binding when the target is omitted;
ask only for missing details needed for the requested action.

Setup and Update apply only to that target. Updating the shared Aru framework
does not authorize discovering, enrolling, or updating consumer projects. Do
not add other projects merely because they are in a local directory, board, or
configuration. Leave existing Loop activation unchanged during Update.

- **Setup/Create:** read `kernel_root/skills/init-agent-project/SKILL.md` and
  `kernel_root/docs/OPERATIONS.md` sections 6-7. For a new empty destination,
  use `init_project.py --name NAME --directory PATH --owner OWNER`; use
  `--github` only when GitHub repository creation was requested. For an existing
  repository, generate outside it, compare, and reconcile the selected files.
  Preserve product rules and supply meaningful consumer verification before
  declaring adoption complete. Configure only the selected project's binding
  within the requested installation scope. First setup does not start a Loop.
- **Update:** compare the requested Aru revision with the target's guidance,
  `.github/` templates/workflow, `.aru/verify.sh`, `.aru/verify-project.sh`,
  shared parser, and hooks.
  Use the existing-project staged migration in operations section 7 and, for
  an already governed repository, its issue and isolated worktree. Preserve
  working consumer verification and custom policy; never replace verification
  with the scaffold's failing product-verifier starter. Preserve product commands
  when adopting the split verifier. Keep the runner profile marker,
  `runs-on:`, and verification expectations consistent. Apply actual differences
  only; report a no-op when the target already matches.

The installed Hermes adapter is shared. A project Setup/Update does not
implicitly upgrade that runtime or migrate webhook routes. When adapter
installation is explicitly in scope, follow
`kernel_root/integrations/hermes/README.md` and preview `install.py` against the
specified configuration/home before applying it. Preserve other project
bindings. Installing the adapter does not start projects or change the model.
Report source revision, target changes, and any concrete remaining setup
blocker separately from Loop state; do not run a live canary or start a Loop
as an unrequested completion check.

## Loop and Stop

- **Loop:** execute `driver.py --config CONFIG start --project OWNER/REPO` using
  the Hermes Python runtime. Verify an enabled native ten-minute heartbeat and
  the immediate wake. Repeated Loop commands reuse the heartbeat. If start
  cannot establish continuation, report the concrete blocker instead of
  promising automatic progress.
- **Stop:** execute `driver.py --config CONFIG stop --project OWNER/REPO`.
  Verify this project's future Driver dispatch is disabled and owned scheduler
  jobs are paused. Preserve already-running workers, claims, worktrees, and
  PRs; report any worker still finishing. Stop does not terminate workers or
  disable unrelated monitors.
- **Status:** execute `driver.py --config CONFIG status --project OWNER/REPO`.
  Distinguish running workers, waiting with a verified next wake, and stopped.
  Source installation, scheduler activation, and proved event delivery are
  separate facts.

## A bounded activation

Both an authenticated event and a recovery heartbeat enter the same adapter.
The cheap `tick` precheck emits a bounded plan and a last-line JSON wake gate.
`wakeAgent:false` means no Hermes reasoning or delivery is required. When you
are awakened, run `driver.py --config CONFIG reconcile --project OWNER/REPO`;
the precheck snapshot is advisory, so reconcile rereads current state.
A PR waiting for approval remains visible as a wait action and never wakes a
review worker. The Driver does not launch reviewers or schedule review deadlines.

Implementation, remediation and feedback workers keep their current owner,
claim, write scope, retry limits and Stop fences. For `worker_retry_wait`, wait
for the returned retry time. Standard workers get three unchanged attempts with
60/120-second backoff. For an exhausted or blocked worker, report the
retained work and operator-owned cause; never clear receipts or bump epochs
automatically. Stop/Start does not renew an unchanged task's retry allowance.

For a returned PR action, inspect the named PR and its current head, then use
the matching canonical workflow: `check_ci.py` and `remediate-ci-failure` for
failed CI; `fetch_pr_feedback.py` and `address-pr-feedback` for unresolved
findings; inspect GitHub approvals on the latest commit;
and `merge_pr.py --pr N --expected-head SHA` only when its gates are satisfied.
Use the live helper's documented finalize path for already-merged PR close-out.
The action is a request to inspect and converge, not evidence that a gate has
passed. A PR closed without merging authorizes no merge finalization or
capacity refill by itself. Do not invent helper switches from old examples.

For a picker `review` item or a wait with reason `awaiting approval by another
GitHub account`, wait quietly. Every PR needs one approval on its latest commit
from an account other than the PR author. Do not start a reviewer or submit an
approval for the author. A simple launcher is planned separately.

Legacy review receipts are inert and cannot resume. Worker exit is not approval.
For opted-in implementation quota checkpoints, report progress through normal
output; the supervisor preserves notes outside source. At the configured limit,
report retained progress and the required operator scope/budget inspection.

The adapter may refill multiple verified free lanes in one activation,
rechecking capacity and reservations between assignments. Promote only
already-approved, eligible Backlog work through canonical helpers. Do not
invent new scope to consume quota. Unknown capacity, unreadable authority,
rate limiting, and unresolved dependencies are blockers, not free resources.
Only an explicit author unknown-mode allowance accepts quota uncertainty;
read `references/quota.md` for the limits. Never describe it as measured headroom.
Avoid repeated full-board scans or unconditional model smoke tests. Use the
available bounded snapshot and run substantive probes only when justified.

Track implementation and remediation workers through verified process/session
completion. Send completion events through the same Driver event entrypoint.
After completing returned convergence actions, one fresh bounded reconcile
may refill newly freed capacity. If no action is available, return; the
heartbeat and genuine events own future continuation.

For a cross-project dependency, use `handoff --source-issue N` only when the
source issue contains the single `aru-driver-dependency:v1` marker and its
configured `handoff_to` route names the target. The command returns the target
issue, Project status, claim and boundary blockers, and an idempotent delivery
receipt. Use `--dependency-event` from the source project to reread every
declared GitHub proof and wake the source only when all conditions are true.
Never treat a handoff marker as permission to claim, close, merge or deploy;
the receiving Driver still performs its own full reconciliation.
For returned `type:dependency` actions, run that command with the action's issue
when `next_action` is `handoff`; add `--dependency-event` when it is
`dependency-satisfied`. A `wait` result records the exact blocker and stays
quiet while unchanged. Every heartbeat discovers these contracts in active
GitHub issues, so a lost dependency event is recoverable without a local queue.
After a successful dependency return, perform one fresh reconciliation before
resuming the consumer's own CI, review or merge workflow.

See [references/runtime-contract.md](references/runtime-contract.md) when
installing, checking event delivery, or diagnosing a stalled Loop.

## Persona integration status: incomplete, not ready for activation

The 13-persona policy package exists, but #631 is still being implemented and
#636 has not established installed/live acceptance. Do not describe a green
source test suite as an activated fleet.

`personas_required: true` explicitly opts a project into the in-progress
integration and requires `personas_source_digest` and `personas_policy_digest`.
Missing task classification, missing scope, broken evidence and conflicting
author identity must refuse resolution. Projects without this explicit opt-in
continue their existing legacy lane behavior; that path is outside persona
enforcement. Package importability alone never enables the integration.

The current source still needs complete account selection and reservation,
exact authenticated probes, child-spawn revalidation, operator task wiring. Do not enable this incomplete path in live projects
or install it as a completed #631 rollout. No `driver.py launch` operation has
been delivered. Use the installed Driver's `--help` for supported operations.

A human running vendor CLIs directly and non-default Hermes profiles remain
outside managed enforcement. Keep source, installation, provider access and
live task acceptance as separate evidence.
