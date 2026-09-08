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

Repository Setup/Create uses the canonical bootstrap path; Update refreshes
approved integration source without starting the Loop. Installing this
adapter does not authorize runtime activation or change the configured model.

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
The precheck also registers or retires the canonical pending-review deadline
timer before deciding whether to wake you, including while CI is pending or
an external review is quietly waiting.

Use returned actions to converge active PRs through the canonical CI,
feedback, review, and merge helpers. Keep a waiting PR in its own author's
lane. A worker's existence or a green check does not establish acceptance,
authoritative approval, or deployment. Treat a live worker's intermediate
push as unsettled until its owning process has finished.

For a returned PR action, inspect the named PR and its current head, then use
the matching canonical workflow: `check_ci.py` and `remediate-ci-failure` for
failed CI; `fetch_pr_feedback.py` and `address-pr-feedback` for unresolved
findings; `create_pr.py --reviewer-status --json` for current review authority;
and `merge_pr.py --pr N --expected-head SHA` only when its gates are satisfied.
Use the live helper's documented finalize path for already-merged PR close-out.
The action is a request to inspect and converge, not evidence that a gate has
passed. A PR closed without merging authorizes no merge finalization or
capacity refill by itself. Do not invent helper switches from old examples.

For `await-authoritative-review`, an external provider owns its review while
its verified deadline continuation is pending. `reconcile` launches an assigned
coding reviewer through the existing supervisor, in a detached exact-head
review worktree. It returns `execution:queued` with a worker receipt, or an
explicit `execution:blocked` owner, reason and next step. The supervisor records
running and exited states; its existing completion event wakes this Driver.
No separate generic reviewer or author worker may substitute for the assignment.

For a due `refresh-reviewer` action, reread PR/head/sole authority and, when
provided, every field of `review_binding`. Cancel a stale action. Invoke the
canonical `create_pr.py --refresh-reviewer N` once; include
`--coding-reviewer-unavailable REASON` only for the returned observed failure.
Only that helper selects authority: CodeRabbit first when usable, otherwise
an available distinct coding reviewer; retired providers are never candidates.
Then run one fresh reconcile immediately so a new coding assignment executes.
Do not overlap refresh commands or retry an exhausted assignment yourself.
If recovery fails, report its precise blocker with this Driver as next owner.
Capacity/identity/permission blockers retain the heartbeat or explicit operator
action as their next step. If this callback cannot execute an authorized
action, explicitly name the required operator and action; a stopped Driver
stays stopped. A denied launch never authorizes activation or another launcher.

Worker exit is not approval. Reconcile reads the kernel's current-head verdict;
valid approval returns to normal CI/merge/finalization, substantive defects to
the implementation owner, and an exit/lost reservation without a verdict to
governed reviewer recovery. A reviewer must never edit the code and approve it.

The adapter may refill multiple verified free lanes in one activation,
rechecking capacity and reservations between assignments. Promote only
already-approved, eligible Backlog work through canonical helpers. Do not
invent new scope to consume quota. Unknown capacity, unreadable authority,
rate limiting, and unresolved dependencies are blockers, not free resources.
Avoid repeated full-board scans or unconditional model smoke tests. Use the
available bounded snapshot and run substantive probes only when justified.

Track coding and review workers through verified process/session completion.
Send completion events through the same Driver event entrypoint. A review for
a sensitive change follows the current kernel's provider and actor separation
policy; this skill does not maintain a parallel review queue or authority.
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

## Reporting

Keep healthy unchanged activations silent (`[SILENT]`). Notify only on useful
progress, completion, a new failure, or a decision requiring the user. Lead
with actual running/waiting/stopped state and the verified next continuation.
Never say the Loop is working solely because source tests pass or a local
webhook health endpoint responds. Subscription utilization is a means to
complete useful approved work, not a reason to bypass review or manufacture
unnecessary tasks.
