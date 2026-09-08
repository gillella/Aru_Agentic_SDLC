# Degraded Mode

GitHub is the only coordination authority. If issue, board, pull-request,
exact-head verification evidence, review, identity, or merge state cannot be
read completely:

1. do not claim, reassign, promote, merge, or close work;
2. keep already-claimed local work in its existing worktree;
3. record the failed command and error locally or on the linked issue when
   GitHub returns;
4. retry with bounded backoff;
5. never create a private queue, JSON ledger, lock service, or handoff file.

A local implementation may continue only when its claim and `touches:` budget
were established before the outage. It may not publish or transition state
until GitHub is authoritative again.

An external Driver must cancel or defer any scheduled continuation whose PR
head or authority cannot be reread. Consumer deployment and incident policy
remains consumer-owned; degraded Kernel state never authorizes production
action.

The separately installed Hermes adapter may retain operational wake receipts,
Stop gates and account locks in its own home. These are not lifecycle evidence.
A scheduler failure must not be acknowledged as delivery. Missing routes,
changed source heads or incomplete dependency proof leave the handoff visibly
blocked. A later event or ten-minute recovery heartbeat rereads GitHub; only
valid current evidence permits another bounded action. No degraded state
authorizes new scope, account takeover, review bypass or deployment.

If every `aru-ci` self-hosted Mac is offline, a `self-hosted-mac` repository
leaves `aru-governed-pr` queued while runner availability is restored. Do not
switch it to the `github-hosted` profile, change the workflow to a hosted
label, waive the required check, or treat an ad hoc terminal run as server
evidence. A personal-pool outage is not a hosted-capacity event, and the
reverse is equally forbidden: a `github-hosted` repository whose Actions
capacity is degraded waits for GitHub, and is never dispatched to a personal
Mac. Restoring a profile means fixing that profile's compute, never
re-scaffolding the repository onto the other one to get a green check.

## Optional Hermes Driver recovery

Use the [installed operations guide](OPERATIONS.md#18-installed-hermes-driver-operations)
and the released [adapter contract](../integrations/hermes/README.md) when
recovering the separately installed Driver. Its observations and delivery
receipts help diagnose a stop; GitHub still owns every lifecycle transition.

### Account availability and worker permissions

A live account with unknown ownership is unavailable. Include every host using
that subscription in the operator-owned capacity observation; do not infer
which account a process uses from its display name, inspect credentials, or
reuse an account merely because a local lock is free. Preserve active workers
and their account reservations until their exit is verified.

Process liveness is not remaining quota. Before launch, the Driver needs both
a successful availability observation and a bounded probe using the worker's
exact model and account. The probe has a 45-second timeout and requires an
`OK` response; it runs only for eligible work. Even a successful probe does not
prove enough quota for the whole task, working GitHub credentials, or permission
to execute the required tools. Unknown capacity, failed probes and live account
use block the affected lane; another independently verified free lane may work.

A worker may exit with code zero after reporting denied commands. Verify the
actual issue, worktree and PR before calling that completion. Repeated tool
permission failures warrant stopping the affected Driver and preserving the
claim for recovery. An operator may authorize the specific required permissions
or take over through the canonical release/claim helpers after the worker exits.
Do not disable hooks, broaden write scope or bypass CLI protections to make a
live test pass. Manual recovery does not establish automatic continuation.

### Which operations remain blocked

| Missing or conflicting evidence | Affected operation and recovery |
| --- | --- |
| No explicit configured route | Do not acknowledge a cross-project handoff. Obtain the intended route from the operator and revalidate it before retrying. |
| Source PR head changed | Discard the stale callback. Rebind the contract to an approved current head, then reread source and target authority. |
| Any typed condition lacks proof | Do not send the dependency return. Require every declared issue/merge/release/artifact condition, not a comment claiming completion. |
| Review backlog or verification queue exceeds configured limits | Defer new coding admission. Existing PR convergence and valid dependency returns can still be considered against their own gates. |
| Eligible CI runners are offline, or runner/queue authority is unknown | Defer new admission and retain the required server check. Restore or reread the existing infrastructure; a local test is not a replacement. |
| Unreadable ownership, dependency or write boundary | Preserve GitHub ownership and the isolated worktree. Do not steal a claim or guess which files are safe. |

A scheduled wake is an acknowledgment of scheduling, not evidence that the
receiving project claimed or completed work. A merge proves the merged change;
it does not prove deployment, authenticated ingress, automatic refill or a
successful return to the source. Retain those observations separately in the
linked live acceptance issue.

### Events, recovery heartbeat and Stop

Authenticated events provide immediate continuation. Exactly one native
ten-minute heartbeat per enabled project recovers missed events and freed
capacity through the same bounded decision path. Healthy unchanged idle checks
stay quiet without a model probe or new agent turn. Actionable work may be
retried after a failed attempt; quiet mode must not hide an unfinished task.

Inspect the timestamped status, native next-run metadata and the returned
reconciliation reasons, including `blocked_lanes`, to identify the relevant
limit. A failed authority read returns a degraded reason and a cooldown: the
released adapter uses ten minutes for ordinary failures and one hour for
rate-limit/quota errors. The first changed error can wake the coordinator;
repeated unchanged errors do not justify unbounded polling or repeated claims.

Stop closes new dispatch before pausing this project's owned future jobs.
Verify command success and that no owned jobs remain enabled; a scheduler
failure can leave jobs enabled while the persistent dispatch gate is closed.
Inspect the failure and retry Stop before assuming the jobs are paused. Existing
workers, claims, worktrees and PRs survive. Restart only within the approved
scope after checking current evidence; repeated Loop must not create a second
heartbeat or reuse an occupied account. A test of Stop with no live worker
cannot establish that an active worker was preserved. Record the observed case
and leave any untested live acceptance criteria open.

### Live evidence: recovery heartbeat honesty (2026-09-08)

Two incidents on the operator's Mac Mini established that a degraded Driver is
reported as degraded and that the ten-minute recovery heartbeat is honest.

- **[#574](https://github.com/gillella/Aru_Agentic_SDLC/issues/574), fixed by
  [PR #575](https://github.com/gillella/Aru_Agentic_SDLC/pull/575).** JMC
  heartbeat `c14ac8ff8830` ran under launchd's minimal `PATH`; the kernel bridge
  failed with `[Errno 2] No such file or directory: 'gh'`. The Driver recorded that
  `last_error`, but the native scheduler recorded `last_status: ok` because the
  wrapper exited zero. After the fix the wrapper resolves `gh` deterministically,
  exits non-zero on a degraded precheck, and a cooling-down tick still reports the
  recorded failure instead of laundering it to healthy.
- **[#579](https://github.com/gillella/Aru_Agentic_SDLC/issues/579), fixed by
  [PR #580](https://github.com/gillella/Aru_Agentic_SDLC/pull/580).** Hermes
  self-updated to 0.21.1 and moved `_parse_wake_gate`; every Driver `start` and
  `stop` failed closed with "Installed Hermes lacks script wake gates". `stop`
  disables Driver state before the scheduler call, so both projects were left
  disabled with heartbeats still enabled (ticks answered `project stopped`).
  The probe now recognizes the gate by AST in any `cron/*.py` module.
- **Recovery sequence.** Immediate wake `d55a8c131a03` ran the agent, which
  reconciled at 17:53:36Z and cleared `last_error`. Native heartbeats
  `c14ac8ff8830` (JMC) and `5810faaa7409` (Aru) then fired under the scheduler's
  own environment with `wakeAgent=false`, exit 0 and `last_error: null`.

What this proves: missing routing or capacity produces an explicit degraded
state, the recovery heartbeat cannot report `ok` over a Driver that could not
observe GitHub, and quiet idle checks stay quiet. What it does not prove:
completion-to-next-dispatch refill, or Stop and restart with an active worker.
Those remain open on
[#557](https://github.com/gillella/Aru_Agentic_SDLC/issues/557); the full
sequence with exact outputs is recorded in
[this comment](https://github.com/gillella/Aru_Agentic_SDLC/issues/557#issuecomment-5589865436).

