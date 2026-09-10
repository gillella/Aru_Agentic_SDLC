# Quota admission and recoverable exhaustion (#645)

Quota admission is an optional external Driver policy. It changes no kernel
authority, project activation, claim contract, reviewer assignment or merge gate.
Existing projects without `quota_admission` (or with it set to `null`) retain
their existing dispatch behavior. The supplied example explicitly demonstrates
the opt-in; it is not usable until its account/model/actor placeholders are replaced.
Source installation and live rollout remain separate operator actions.

## Configure and validate before rollout

Use one coordinator home for every account shared across projects. Preserve the
existing two-way project/lane permissions. Every model/login alias billed to the
same provider account must share `capacity_key`, `max_sessions` and
`quota.account_sha256`. Different providers cannot share that key. Aliases of
one pool must agree on its window durations. Subscription numbers and process
names are not verified account identity or independent reviewer families.

The lane's `quota` object identifies the provider account by SHA-256 of the
supported account identifier, the exact provider `pool`, exact CLI `model`,
`effort`, and both applicable short/long window durations in minutes. The
provider remains the lane's existing `family`. Both command and liveness probe
must name that exact model. An explicit effort must match the native worker
setting: Codex `-c 'model_reasoning_effort="high"'`, or Claude `--effort high`.
`default` truthfully means the harness's unspecified effort; it always uses
cold-start demand and never reuses history as comparable measured precision.
The existing Factory permission compiler uses its supported harness shape;
do not append unsupported permission flags to make an effort setting fit.

`projects[REPO].quota_admission` version 1 requires all these fields:

| Field | Meaning |
| --- | --- |
| `max_age_seconds` | Freshness bound, 1–300 seconds; future observations fail |
| `headroom_percent` | Reserve 1–99 percentage points in each applicable pool window |
| `unknown_checkpoint_seconds` | 0 blocks unknown authors; 1–86400 permits bounded author work, capped by the lane execution timeout |
| `unknown_review_seconds` | Optional; default 0 blocks unknown reviews; 1–86400 explicitly accepts bounded unknown review work |
| `unknown_max_attempts` | Optional 1–32 child attempts per issue and role across identities/heads; default `max_recoveries + 1` |
| `unknown_total_seconds` | Optional 1–86400 total seconds per issue and role; default per-attempt seconds times attempt bound |
| `review_escrow_seconds` | Optional 60–3600 second review lease; default 900 |
| `max_recoveries` | 0–3 automatic continuations after genuine quota exhaustion per issue; separate from unknown work |
| `cooldown_seconds` | 60–3600 seconds before another attempt when no genuine reset exists |
| `author_actor` | Operator-declared GitHub actor of author workers; canonical PR actor checks still apply |
| `cold_start` | Explicit rows keyed by task class, conservative risk, exact model and effort |

Each cold-start row has `percent.primary` and `percent.secondary` in quota
percentage points, from 1 to 100. Classes are `implementation`, `remediation`,
`review` and `checkpoint`. The estimator uses risk 0 only when the canonical path classifier reports tier
0 on the declared scope. Every other tier, or missing risk evidence, uses the
conservative risk-3 demand bucket; sensitive Markdown contracts never get a
suffix-based downgrade. This demand assumption does not replace the kernel's
actual-diff risk tier.
Unknown mode is explicit risk acceptance, not measured headroom or a promise of
completion. The example author/review allowance is 1800 seconds, eight attempts
and 14400 total seconds for each role; choose these against real task duration
and the lane execution timeout. Each child consumes its entire admitted allowance,
even if it exits early, so rapid exits and missing completion times cannot defeat
the bound. At the limit the Driver retains ownership and reports that the operator
must inspect progress and task scope. It does not demand a permission epoch change
or automatically reset the allowance. Known capacity can still admit ordinary work.
Old opt-in configs remain readable; omitted unknown-review policy blocks reviews
locally. Non-opted projects retain legacy behavior.

Missing demand rows block that candidate. The example's amounts are deliberately
operator-configurable assumptions, not measured capacity or promises of completion.

Read the proposed config with `Config(PATH, bind=False)` from the reviewed
package before installation. This validates policy without writing the profile
binding. Preview `install.py --config PATH`; an authorized `--apply` copies all
quota modules plus this runbook into the installed skill references and preserves
normal backups. Incomplete quota source is refused. Installation does not collect
quota, start projects, create accounts, or alter their activation.

## Supported collection and honest unknowns

Codex collection starts a bounded local `app-server --stdio` process using the
same executable/wrapper and profile/config arguments as the worker. It generates
schemas with that installed binary, validates both account reads and the quota
response using `jsonschema` Draft7, and closes the process. The selected Hermes
Python environment must already provide `jsonschema`; otherwise collection is
explicitly unavailable. No package installation or auth change occurs implicitly.
The read path is `initialize`, `account/read` with `refreshToken:false`,
`account/rateLimits/read`, then a matching second `account/read`.
No inference, reset redemption, new login or account creation is requested.
See the [official app-server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md).

Bind the configured hash to the response's `accountId`, not a local subscription
nickname. Discovery should run the supported read in an operator-owned artifact
directory and export only `sha256(accountId)`, pool IDs, model mapping and numeric
window fields. Never save raw account responses, email, tokens, OAuth files,
credits/upsell payloads or unrelated identities. A missing account ID is unknown;
a wrong account or account switch during a read is invalid and blocks admission.
The CLI/account read can perform its own normal provider bookkeeping.

The normalized `aru.quota/v1` contract includes account/capacity identity,
provider/model/pool, observation time/source, `identity_verified`, confidence,
state, a bounded reason code and windows with `unit`, `remaining`, `reset_at`
and `duration_minutes`. States distinguish `known`, `unknown`, `unavailable`
and `exhausted`. Only provider percentages are supported in this version;
tokens, context size, dollars and liveness never become quota percentages.

Codex window **duration**, rather than the backend's primary/secondary field
position, determines short versus long. A weekly-only response leaves the short
window unknown. An additional spend-control window is unsupported and produces
unknown instead of ignoring a constraint. The explicit model/pool mapping is
operator policy; a provider `normalModelSlug` mismatch is refused. Unknown model
alias mapping must not be guessed during rollout. The genuine `codex_bengalfox`
pool exposed both windows but no `normalModelSlug`; that is not evidence that it
serves `gpt-6-astra`. Do not configure that mapping without supported model evidence. Provider prohibition, including
ordinary-usage or spend-control denial, overrides positive percentages. Missing
ordinary-usage permission is unknown, not inferred recovery. Duplicate/conflicting
pools, malformed/nonfinite/negative/out-of-range values, expired resets and stale
observations fail closed. Window expiry requires a fresh read, not assumed refill.

Claude's [official statusline](https://code.claude.com/docs/en/statusline) exposes
subscription `rate_limits` in the interactive interface. The actual unattended
`--print` probe performed for this implementation returned a valid result without
invoking a process-local statusline callback or returning `rate_limits`. This
adapter therefore returns `claude-statusline-unavailable-in-print`. It does not
claim unattended Claude balance integration. Cursor and Antigravity likewise
return explicit unknown for unavailable supported surfaces. There are no browser
scrapers, OAuth credential readers, undocumented endpoint calls or generic quota
executable hooks. The legacy `capacity_command` remains a separate availability
gate and `probe_command` remains liveness only.

## Admission, reservations and recovery

Reconcile ranks eligible authors by known capacity ahead of checkpoint capacity,
then by the smallest remaining window margin after estimated demand, reservations
and headroom. It considers aliases before applying worker limits. Existing work
retains priority. Actual author/resume and assigned-review boundaries recheck;
launch checks again under the shared account slot lock, and the supervisor checks
under coordination before crossing the existing short Stop/spawn barrier.
Provider/GitHub calls never run inside that Stop barrier.

Existing worker receipts hold per-window worker reservations across all configured
projects and aliases. Unknown legacy reservations block quota admission. Distinct
pools on a shared account are not numerically interchangeable: outstanding
cross-pool reservations conservatively block until comparable capacity is free.
An author also reserves a budget on an eligible independent coding-review lane,
using fresh canonical reviewer inventory and a distinct actor/family. This is a
budget estimate, never an authority assignment. The canonical helper can assign
another reviewer, whose actual launch must pass its own gates. Review escrow is
replaced on continuation, consumed at review dispatch, released on failed work,
any live CLOSED issue, or a blocked author without an open PR. Every shared-account
reservation scan also expires leases and releases stopped projects' escrow, even
when those projects never reconcile again. Missing older leases expire 900 seconds
after the receipt start. Expiry relinquishes advisory capacity only: later author
and reviewer launches must revalidate all observations, budgets and authority.
A stopped child's actual worker reservation lasts while its capacity lock is held.
These leases cannot authorize lifecycle progress.

Unknown capacity permits explicitly configured bounded author and reviewer work.
Both legs retain demand estimates, any actually observed window constraints,
shared-account exclusion, and independent reviewer eligibility. An unknown review
leg can accompany a known author; the author still ranks ahead of unknown authors.
The decision records the unknown observation and time allowance, never fabricated
percentage headroom. A full authorized task may finish within that allowance;
GitHub remains the only completion and review authority.

The supervisor writes a private, per-project/issue/role/attempt checkpoint note
outside source from bounded native output. Workers report notes through normal
output, requiring no extra sandbox grants or out-of-worktree file writes. Review
notes use the same channel and grant no source edits. Continuations receive the
previous note inline and keep the claimed worktree. A timeout may leave only a
partial diff when the CLI emits nothing until exit; no completed step is inferred.
Supervisor-proven expiry with empty Claude output or well-formed unfinished Codex
events becomes `quota_checkpoint`, with bounded same-owner continuation. Successful
bounded steps also continue on the same assignment. Actual denials, malformed
nonempty output and unsupported error envelopes remain blocked. Unknown budget
limits and checkpoints never require a permission-policy revision.

A validated Claude error result containing the native session/weekly-limit
message and no permission denial or authentication error is `quota_exhausted`,
not permission-policy failure. Codex's native JSONL terminal error message is
also recognized; model-authored prose is not a provider error. Denial, malformed
evidence and other errors remain fail closed. Ambiguous text such as
`resets 11:30am (America/New_York)` never yields a fabricated reset epoch.
All aliases/pools share account cooldown. Fresh numeric exhausted-window resets
can set its deadline; otherwise `until` is a bounded retry time with `reset_at:null`.
A generic alias exhaustion never shortens a longer existing cooldown or erases a
genuine reset. An opted-in project may reclassify an older `worker_error` only by rereading its
retained, supervised Claude JSON result at the exact private result path. It
preserves the previous outcome marker and anchors cooldown to the original finish
time, not each heartbeat. Text-only, missing, malformed, denied or still-live
results are not migrated. No receipt prose supplies quota evidence.

Recovery preserves claim, worktree, partial work and author-family history. A
live task reservation excludes another writer. The same identity can resume after
cooldown. An eligible different account may recover only within the same author
family and exact model/effort, before an authored PR, through canonical
`release` then `claim`; historical author receipts are retained. Canonical release
may refuse. An interrupted transition is recorded and requires owner reconciliation,
not automatic repair. Cross-family transfers are withheld because current canonical
PR authority has one author-family field; operational receipts cannot silently
replace it. Unknown/insufficient admission retains the assigned reviewer and waits locally;
checkpoints resume that assignment until its configured bound. Genuine exhaustion
or execution unavailability uses the existing owned canonical recovery event.
Invalid results and real denials retain their fail-closed gate. Stop, completion wake and heartbeat ownership remain unchanged.

## Calibration, limits, rollback and #631 handoff

Quota decisions retain the latest 32 candidates per project. Worker receipts
include observation, demand/confidence, reservations, continuation owner, result
and post-run observation; status exposes these without raw provider responses.
Calibration stores at most 128 samples per account and compares the latest 32
with matching pool, durations, class/risk/model/explicit effort. Only reported
successful full tasks with two known observations and unchanged reset epochs
produce samples. Actual account percentage deltas include external/concurrent
usage and are upper bounds, not exclusive task metering. Estimates use the greater
of configured cold-start demand and the largest comparable delta plus 25%.
Quantization, unknown external use and changing work make completion uncertain.
No token/cost conversion, learned confidence score, or admission guarantee is made.
These bounded private additions use the existing operational state directory;
they are not a second issue lifecycle store. Existing worker/result retention
rules still apply, including the 8 MiB completed-result limit.

For rollout, first independently review and merge source through exact-head
governed CI. Back up source/config and preserve all receipts. Use the documented
quiescent installation window for affected projects. Opt in only the authorized
Factory project, verify supported account/pool evidence and cold-start bounds,
and separately authorize activation; other stopped projects must stay stopped.
Genuine provider probes and fixture tests are separate evidence. Source testing
does not establish installed runtime, unattended completion, or #557 acceptance.

For rollback, use the existing authorized Stop procedure, preserve active children
and their claims, and quiesce old coordinators/supervisors before restoring source
and its matching config backup. Retain worker receipts, cooldowns, checkpoints,
Stop intents and calibration. Setting `quota_admission:null` disables new quota
admission; it does not erase denial/recovery evidence or authorize reopening an
exhausted account. Do not delete receipts or increase recovery limits as a retry
shortcut. No rollback or live installation is performed by this source change.

#631 must integrate this merged boundary rather than replace it with process
counts or an `OK` probe. Preserve account mappings, locks, Stop fence, compiled
permissions, bounded unknown mode, cumulative family exclusion and canonical
transfers/review recovery. Add any future supported collector through this validated
contract with genuine unattended evidence. Wider provider support, cross-family
kernel lineage authority and multi-host allocation require separately approved
work; #637's preserved persona source and claim are untouched.
