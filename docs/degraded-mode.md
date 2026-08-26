# Degraded Mode Decision Record: Handling GitHub Outages

## Context & Decision

GitHub serves as the sole coordinator, work queue, state store, and gatekeeper for `Aru_Agentic_SDLC` (`docs/ARU-SOFTWARE-FACTORY.md` §3.7). When GitHub experiences an outage or API degradation, automated fleet coordination halts.

**The architectural decision of Aru_Agentic_SDLC is to accept this stoppage gracefully rather than building a complex secondary offline coordinator or fallback database.**

---

## 1. What Stops During a GitHub Outage

During an active GitHub outage or authentication failure:
- **Work Dispatch & Claiming**: `fetch_next_work.py` and `claim_issue.py` fail closed. No new issues or PR reviews can be claimed.
- **Board Synchronization**: Status updates between `Backlog`, `Ready`, `In Progress`, and `Done` cannot be committed.
- **Merge Gate Execution**: `merge_pr.py` refuses to merge, as it cannot verify live CI status, review attestations, or remote head bounds.
- **CI / Actions**: Remote pipeline verification and test execution on GitHub Actions cease.

### GraphQL quota exhaustion is a partial outage

GitHub REST and GraphQL have separate primary rate-limit buckets. The factory
uses local Git metadata for repository identity and paginated REST for ordinary
issue and identity-label inventories, preserving GraphQL for data that has no
equivalent authoritative batch read: Projects v2 state, review threads, and
rich pull-request gate evidence.

If the rich pull-request query fails, the picker performs one paginated REST
inventory read. That fallback is visibility only: it reports which open pull
requests exist and then fails closed because REST cannot batch-prove review
threads, CI rollups, and changed-file locks. It must not replace those missing
facts with one REST call per pull request or make a claim/merge decision from a
partial snapshot.

One normal picker cycle shares its issue and pull-request snapshots with the
stale-claim reapers. Per-pull-request review reads occur only for candidates
whose batched CI state is already green; ordinary file snapshots use REST only
when a rename requires the previous path. A cycle may refresh the snapshots
after a successful mutation, but unchanged polling must not re-read the full
queue.

---

## 2. What May Continue Locally

An agent operating inside an **already-claimed worktree** before the outage began MAY continue local engineering progress:
- Writing code and unit tests within its declared `touches:` path budget.
- Executing local test suites (`python3 -m unittest`, `pytest`, `npm test`, linters).
- Committing changes to the local branch in its isolated `.worktrees/<branch>` directory.

---

## 3. Strict Non-Goals & Forbidden Fallbacks

To preserve repository integrity and avoid state fragmentation, the following actions are **strictly forbidden**:
- **NO Local Task Queues**: Agents must NEVER create or fall back to local `tasks.md`, `TODO.txt`, or in-memory supervisor lock files.
- **NO Ungated Merges**: No agent or human may bypass `merge_pr.py` or force-push directly to `main`/`master`.
- **NO Alternative Coordinators**: Do not spawn local supervisor processes or MCP buses to replace the GitHub Project Board.

---

## 4. Recovery Runbook When GitHub Returns

Once GitHub connectivity and API health are restored:

1. **Verify Integration & Fleet Health**:
   ```bash
   python3 scripts/doctor_local_agent_integrations.py
   python3 scripts/fleet_status.py
   ```
2. **Reap Abandoned Claims**:
   If an agent crashed or lost state during the outage, stale leases are automatically reaped or can be released via:
   ```bash
   python3 scripts/fetch_next_work.py --agent <AGENT_ID> --family <FAMILY> --reap-after 4
   ```
3. **Resume Normal Factory Loop**:
   Agents resume polling via:
   ```bash
   python3 scripts/fetch_next_work.py --agent <AGENT_ID> --family <FAMILY> --claim
   ```
