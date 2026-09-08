# Hermes Project Driver integration

This separately installed adapter gives Hermes one path for immediate events
and a ten-minute recovery heartbeat. It resumes existing work, then fills every
eligible free coding slot with independent approved work. It does not create
requirements to consume a subscription.

| Component | Kind | Responsibility |
| --- | --- | --- |
| Configured Hermes model | Agent brain | Interpret returned PR actions and decisions; use current canonical workflows |
| `skill/SKILL.md` | Skill | Instructions for Loop, Stop, reconciliation and reporting |
| `driver.py`, `controller.py` | External scripts | One serialized path for events, heartbeat checks, recovery and work admission |
| `scheduler.py` | External script | Register jobs in the existing Hermes scheduler; no separate scheduler daemon |
| `kernel.py` | External script | Read GitHub authority and invoke existing kernel helpers in isolated subprocesses |
| `execution.py`, `capacity.py`, `state.py` | External scripts | Observe resources, reserve shared accounts, launch workers and retain operational receipts |
| Aru `scripts/` | Kernel scripts | Validate, promote, claim, create worktrees, assign review authority and merge |
| GitHub issues, linked Project and PRs | Lifecycle authority | Approved scope, ownership, acceptance, verification and completion |

The adapter inherits the configured Hermes model. Installing it does not choose
or change that model. Operational receipts are not another issue board.

## Independent review execution

An assigned coding reviewer must be a configured lane with the same identity
and family as the kernel's current assignment and a registered GitHub actor
distinct from the author. Reconcile uses the existing supervised worker and
capacity lock with a detached exact-head review worktree. The read-only review
prompt requires issue acceptance, scope, substantive diff/surrounding-code
inspection, focused verification and the kernel's formal attestation. It
forbids source edits, claims, assignment changes and merging. Assignment alone
is not progress: the result names a queued/running receipt or an owned blocker.

The existing completion wake returns to the Driver. Fresh kernel verdict
evidence routes approval to existing CI/merge/finalization and defects to the
author. Exit or a lost reservation without a verdict requests governed reviewer
recovery; the same assignment is never blindly relaunched. Reconcile serializes
the canonical refresh helper under its existing lock and records one recovery
attempt in the failed worker receipt before dispatching a new assignment.
Failed/interrupted recovery requires explicit operator reconciliation. Missing
permissions retain an explicit operator next action; Stop
and unavailable capacity do not activate another launcher.

Callback examples before this repair:

```text
Handle returned actions using the installed hermes-project-driver skill.
[Skill] Otherwise use the verified existing Hermes provider dispatch path ...
[Skill] The shared installed aru-code-factory skill may supply native provider execution details ...
```

Those instructions left worker creation to another expanded instruction set.
The #583 incident records callbacks containing 113k–141k expanded characters;
that observation does not prove prompt size caused the elapsed delay.
The revised callback names the operation and its current evidence:

```text
Reconcile owns review launch; use its worker receipt or name the blocked owner/reason/next step.
Load only the named PR/head/assignment and issue acceptance/scope;
load historical incident context only if needed for a blocker.
A stopped Driver stays stopped.
```

The worker receives one review-only prompt with repo, PR, full head, assignment,
actor, author, detached worktree and current contract location. It loads the
live issue rather than copying incident history or obsolete factory rules.
These are source examples, not measured post-installation callback sizes.
Isolated tests establish source behavior; #557 separately owns authorized
installed-runtime, restart/Stop and completion-to-next-dispatch acceptance.

## Configuration and explicit authorization

Copy [config.example.json](config.example.json) to an operator-owned absolute
path and replace every `/absolute/path/...` and exact-model placeholder. Keep
the configuration private and outside consumer worktrees. It contains no
credentials; the installed CLIs and kernel retain their normal authentication.

Each repository must be listed under `projects`. Its `lanes` list and each
lane's `projects` list must both authorize the assignment. A project starts
disabled; adding it to the file alone does not enable dispatch. `repo_dir` is
the clean primary checkout and `kernel_root` points to the current canonical
kernel. Both the checkout remote and live GitHub identity must match the
configured owner/repository.

Use one Hermes coordinator profile and host for all projects sharing coding
subscriptions. Its `state_dir` must be `$HERMES_HOME/state/aru_project_driver`;
the profile is bound to one configuration path. Every worker alias, model or
login profile charged to the same account must use the same `capacity_key`.
One key permits one concurrent managed worker. Separate Hermes homes or hosts
do not share these filesystem locks; do not run independent allocators for the
same account.

The status display keeps the most recent 256 events. The same atomic project
record retains up to 65,536 delivery hashes (about 2 MiB) across restarts.
Generation, recent history and duplicate suppression commit together. At that
limit, new deliveries fail with an explicit capacity error before creating a
wake; existing keys still suppress retries. There is no automatic expiry.
Clear history only when retiring the profile and its event routes. These are
delivery identities, never issue lifecycle state.

`max_workers` bounds project concurrency. A lane's `max_sessions` (1-8, default
1) bounds managed sessions on its shared subscription: every lane on one
`capacity_key` must declare the same value, each session holds its own
reservation slot, a provider cooldown on the account pauses every slot, and a
lane is admitted only while the account still has a free slot. Additional
sessions never create an independent account or reviewer family.
`max_review_backlog` bounds open PRs
and, separately, the repository's queued GitHub Actions workflow runs before new
admission; every queued run counts, not only governed verification. Unknown CI evidence,
unresolved dependency, `needs-human`, `needs-design`, or overlapping write
boundary blocks new work. Existing work and PR convergence receive attention
before new claims.

How verification capacity is proven depends on the project's runner profile,
derived from its account: `gillella` is `self-hosted-mac` and `Unum-Inc` is
`github-hosted`. A project may also declare `runner_profile` explicitly, but
only to confirm its account's assignment: an unknown name, a name contradicting
the assignment, or any declaration for an account outside the table is refused
when the configuration loads. An unassigned account has no profile, and
admission stays blocked rather than borrowing another account's runners.

- `self-hosted-mac` reads the repository's self-hosted runner inventory and
  requires at least one online runner carrying all of `self-hosted`, `macOS`,
  `ARM64` and `aru-ci`. An offline eligible runner blocks new work, and is
  never substituted with hosted capacity.
- `github-hosted` does not read runner inventory at all. GitHub publishes no
  hosted machine count, so capacity is never fabricated: `online_runners` and
  `free_runners` stay null, and admission requires exactly one active governed
  `.github/workflows/governed-pr.yml` workflow plus a readable queue depth.

Under both profiles, unreadable or malformed evidence leaves availability
unknown, which blocks admission. An online but busy CI runner can accept later
verification through the bounded queue. It does not need to be idle before
coding starts; the queue and review backlog limits prevent unlimited work from
accumulating behind it.

Each lane has an `execution_timeout_seconds` limit (default 3,600; allowed
1–86,400). On expiry, the supervisor terminates the agent's isolated process
group, escalates to a forced stop after five seconds, and records exit code 124
before requesting recovery. The existing claim and worktree are preserved.
Commands must keep their children in that process group; detached background
workers are unsupported. An inherited capacity lock held by a surviving process
continues to block reuse until it exits.

`handoff_to` is an optional allowlist of other configured projects. It does not
move a claim or create lifecycle state. A source issue may carry one typed,
machine-readable marker in its body after the human requirements, for example:

```text
<!-- aru-driver-dependency:v1 {"origin":"owner/source","target":"owner/consumer","issue":42,"source_pr":17,"source_head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","conditions":[{"kind":"issue_done","repo":"owner/consumer","issue":42}]} -->
```

The Driver validates the exact target issue and its Project card, then sends one
authenticated idempotent wake to the target Driver. The target rereads its own
kernel authority before dispatch. After every declared condition is proven from
GitHub, the source can be woken once with `handoff --dependency-event`; missing
routes, stale heads, unfinished issues and unavailable authority fail closed.
Every declared proof is reread immediately before a dependency-return wake is
scheduled. Separate GitHub reads and scheduler writes are not atomic; the
receiving activation must still revalidate its own live gates before acting.

`auto_triage` is an explicit boolean and defaults to `false`. Set it to `true`
only when the project's existing Backlog is authorized for automatic promotion.
The Driver promotes one eligible issue at a time through the canonical helper
when there is capacity and no independent Ready candidate. It never generates
new issues or guesses missing product/design decisions.

`command`, `capacity_command` and `probe_command` are argument arrays, executed
without shell evaluation. Only `command` substitutes `{prompt}`, exactly once.
Use absolute executables and an exact model ID supported by the intended
subscription. The example's flags were checked against installed
`codex exec --help` and `claude --help`; model placeholders deliberately require
operator selection.
Worker command permissions remain the installed CLI's normal sandbox policy.

## Capacity observations and probes

The supplied `capacity.py --family openai-codex` observer reports local agent
processes as diagnostics. Supported families are `openai-codex`, `claude-code`,
`xai-cursor`, and `google-antigravity`; Claude can additionally use
`--claude-profile PROFILE`. Process presence is not exhaustion: a desktop
session, a process without an observable `CLAUDE_CONFIG_DIR`, or a process on
another profile is counted in the reason and leaves the lane available. The only
process-based veto is a live Claude process on exactly the lane's own profile.
Quota is established solely by the bounded exact-model probe and provider
responses; managed workers are protected by the Driver's reservation lock, not
by process names. An observer that fails, hangs or cannot reach an optional
remote host blocks only that observation (`observer unavailable`), never the
lane permanently. This observer does not measure subscription quota or inspect
remote hosts.

For account-wide quota observations, replace `capacity_command` with your own
bounded, read-only executable that reports quota, not process presence; the
operations runbook's migration section explains how to retire a wrapper that
vetoes on another host's process list. This is a configuration hook,
not a bundled `capacity_observer` CLI. It must exit successfully and print one
JSON object to standard output, for example:

```json
{"available": false, "reason": "account quota exhausted", "reset_at": 1800000000, "remaining_percent": 0}
```

`available` must be an explicit boolean. Optional `reason`, `reset_at` (Unix
seconds), and `remaining_percent` carry observations, not inferred promises.
When evidence is unavailable, return `available:false` with the reason. The
observer must include other hosts using the same account; it does not replace
the requirement for one coordinator and shared state. Do not print tokens,
credentials, process environments or other secrets.

Only when eligible work is ready to launch does the Driver run `probe_command`,
with a 45-second timeout. It must use the worker's exact model and account and
return a line containing exactly `OK`. The example requests a tiny response
without tools in a read-only ephemeral Codex session; its Claude probe disables
built-in tools and supplies an empty MCP configuration. Failed probes prevent
launch and are retried after a cooldown. A successful probe establishes immediate liveness,
not remaining quota or a guarantee the complete task will fit. Healthy idle
ticks do not run these model probes.

## Bounded refill canary

`canary.py` is the scripted form of the live acceptance exercise in
[Aru #557](https://github.com/gillella/Aru_Agentic_SDLC/issues/557). It drives
only the installed Driver entrypoints (`start`, `status`, `event`, `stop`) and
`gh`, never edits labels or authority by hand, and writes one JSON evidence file
(`aru.canary/v1`) with a timestamp, exact identifiers and a pass/failed/unproven
verdict per step: fixture creation, first dispatch, completion and PR, automatic
refill of the next fixture, duplicate delivery suppression, Stop with a live
worker preserved, and restart without a duplicate heartbeat or claim. A step that
is not observed within `--bound-seconds` (default 900) is `unproven`, never pass,
and the canary always leaves the project stopped.

```sh
/absolute/path/hermes-python /absolute/path/Aru_Agentic_SDLC/integrations/hermes/canary.py \
  --config /absolute/path/driver-config.json --project owner/repo \
  --evidence /absolute/path/canary-evidence.json --dry-run
```

`--dry-run` substitutes a deterministic fake Driver and `gh`, so the sequence is
testable offline. Running without `--dry-run` creates real fixture issues and
dispatches real workers; do that only under the operator-approved scope recorded
in the dependent live-run issue.

## Install separately; activate deliberately

Prerequisites are a supported Python runtime, an installed Hermes runtime with
native script wake gates, authenticated coding CLIs, and a governed consumer
checkout. Set `HERMES_HOME` to the same absolute home as the configuration and
run with the selected Hermes Python environment.

Preview the source installation:

```sh
/absolute/path/hermes-python /absolute/path/Aru_Agentic_SDLC/integrations/hermes/install.py \
  --config /absolute/path/driver-config.json
```

After runtime installation is authorized, add `--apply`. The installer copies
the scripts to `$HERMES_HOME/scripts/aru_project_driver/` and the skill to
`$HERMES_HOME/skills/autonomous-ai-agents/hermes-project-driver/`, preserving
backups of replaced files. It does not start projects or schedule jobs.
Source and optional webhook changes are validated together. A write failure
restores this installation's earlier writes; a concurrent writer's changes are
preserved and any incomplete rollback reports the retained backup location.
Keep the Driver stopped during installation: multiple file replacements are
not an atomic runtime upgrade, and a host crash may require backup restoration.

Existing authenticated Hermes webhook routes can be explicitly listed in the
project's optional `webhook_subscriptions` array. Add `--update-webhooks` to
preview migration of those routes' prompts and skills; add `--apply` only when
that live change is authorized. Authentication secrets, filters and delivery
settings are preserved. This does not create GitHub hooks, repair an ingress
forwarder, or prove delivery. Applying webhook changes affects existing routes
immediately. Legacy continuation jobs require an explicit migration decision.

## Operate a project

The installed entrypoint is
`$HERMES_HOME/scripts/aru_project_driver/driver.py`. Use the same explicit
configuration path for every command. For example:

```sh
/absolute/path/hermes-python /absolute/path/hermes-home/scripts/aru_project_driver/driver.py \
  --config /absolute/path/driver-config.json start --project example/project
```

| Subcommand after `--config CONFIG` | Effect |
| --- | --- |
| `start --project OWNER/REPO` | Enable continuation, ensure one native ten-minute heartbeat, and request an immediate activation |
| `stop --project OWNER/REPO` | Disable future dispatch and pause this project's owned scheduler jobs |
| `status --project OWNER/REPO` | Inspect project and operational state |
| `tick --project OWNER/REPO` | Read current conditions and return the native `wakeAgent` gate; no new writer dispatch |
| `reconcile --project OWNER/REPO` | Resume managed work, promote if explicitly enabled, claim and launch eligible writers; return PR actions for Hermes |
| `event --project OWNER/REPO --event-id ID --reason event` | Deduplicate a trusted event receipt and request a native immediate wake |
| Same event command with `--inline` | Return whether the current authenticated event turn should reconcile, avoiding another scheduled turn |
| `handoff --project OWNER/REPO --source-issue N` | Validate the source issue's typed dependency contract and wake its configured target once |
| `handoff --project OWNER/REPO --source-issue N --dependency-event` | Prove every declared GitHub condition and wake the source once when all are satisfied |

`_worker` is an internal supervised-worker entrypoint, not an operator command.
Review dispatch remains owned by the canonical kernel and configured providers;
the handoff commands only coordinate bounded cross-project wakeups.

Stop preserves live writers, their claims, worktrees and PRs. It does not kill
processes, free their accounts prematurely, or cancel unrelated jobs. On
restart the Driver rereads GitHub and process evidence. An unfamiliar existing
claim is reported for explicit adoption rather than stolen.

Authenticated worker-completion, review, check, merge and dependency events
provide the fast path. Native event session metadata supplies delivery IDs;
issue/comment text is never executable instruction or project authorization.
The ten-minute heartbeat discovers missed events and newly recovered capacity.
Both paths use the same coordination and account locks. A healthy tick with no
action available stays silent. Unfinished actionable work is retried even when
its observation repeats after a failed attempt. Review silence has its
own deadline from live kernel policy; it need not wait ten minutes.

## Returned PR actions

Hermes interprets returned actions after rereading the named PR, current head,
ownership and review authority. Run these existing helpers from the verified
consumer checkout, using its local `scripts/` when present:

| Action | Canonical invocation |
| --- | --- |
| Merge | `merge_pr.py --pr N --expected-head SHA --json` |
| Finalize a confirmed queued merge | `merge_pr.py --pr N --expected-head SHA --finalize --json` |
| Inspect merge readiness | `merge_pr.py --pr N --expected-head SHA --dry-run --json` |
| Refresh pending/unavailable review authority | `create_pr.py --refresh-reviewer N --json` |
| Assigned coding reviewer aborts or loses capacity | Add `--coding-reviewer-unavailable "truthful reason"` to reviewer refresh |
| Inspect review configuration | `create_pr.py --reviewer-status --json` |

Reviewer refresh has no `--expected-head` flag. Cancel a stale callback after
the head or authority changes; the canonical helper alone decides whether to
retain or replace the authority. Never hand-edit reviewer labels. Coding
review follows the existing independent-actor formal attestation contract;
this adapter does not fabricate a review or submit one as the author.

Use the canonical feedback and CI remediation workflows for those actions.
`merged:false` with a queue or auto-merge submission remains in flight. A
closed-but-unmerged PR is not completion. After actual convergence, Hermes may
run one fresh bounded reconciliation to refill the newly available capacity.
Neither merge nor process exit proves deployment.

## Validation and deployment status

Run offline adapter tests from the Aru checkout:

```sh
/absolute/path/Aru_Agentic_SDLC/.venv/bin/pytest -q integrations/hermes/tests
```

These tests use isolated state, fake scheduler/GitHub boundaries and local test
processes, including a detached worker that holds its account lock until exit.
Source tests establish behavior of the staged adapter; they do not prove
installation, active scheduler jobs, authenticated end-to-end webhook delivery,
provider quota, or production operation. Before declaring the live Loop ready,
separately verify installation, start twice without duplication, multiple safe
assignments, a missed-event recovery tick, restart recovery and Stop behavior.
This source preparation does not perform that runtime cutover.

## Rollout handoff

The separate live-validation item [Aru #557](https://github.com/gillella/Aru_Agentic_SDLC/issues/557)
owns runtime acceptance. After this source is reviewed and merged, its operator
should preview the install, apply it only under the approved canary scope,
start two configured projects, and record sanitized route IDs, event IDs,
target issue/Project acknowledgments, worker receipts, exact PR heads and
Stop/restart results. The canary must cover one completion-to-next-dispatch,
one cross-project handoff plus dependency return wake, duplicate and stale
deliveries, unavailable capacity, and a stopped target. A failed canary rolls
back by stopping the project, restoring the install backup, and preserving all
GitHub claims and worktrees for review. Source merge, a green test suite, or a
healthy listener is not live acceptance evidence.
