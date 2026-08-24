# Downstream Factory checkpoints

Which merged Factory commit each governed repository is running, and what
evidence proves it. Every value below was read back live and read-only at the
observation time given in each section. Nothing here was produced by mutating a
downstream repository, a worktree, a service account, or billing state.

This document records propagation; it does not perform it. It is not a merge
verdict and not a review. For what a review-pool pilot must capture, see
[`docs/review-pool-pilots.md`](review-pool-pilots.md); for operating the pool,
see [`docs/review-pool-operator-runbook.md`](review-pool-operator-runbook.md).

## Merged Factory checkpoint

Observed 2026-08-24T18:43:56Z.

| Field | Value |
|---|---|
| Checkpoint SHA | `5b9b6ace50d0e13d0a2bcabf41975b03b5659cd3` |
| Origin | Merge commit of PR [#390](https://github.com/gillella/Aru_Agentic_SDLC/pull/390) (merged), `chore(review): add CodeAnt pilot assignment audit` |
| Closes | #384 (closed) — pilot and propagate the three-service review pool |
| Merged at | 2026-08-24T18:31:11Z |
| Position | Current `main` head of `gillella/Aru_Agentic_SDLC` |
| Assigned review label on #390 | `review:codeant` |

Read back with:

```bash
gh api repos/gillella/Aru_Agentic_SDLC/commits/main --jq '.sha'
gh pr view 390 --repo gillella/Aru_Agentic_SDLC --json mergeCommit,mergedAt,state,labels
```

## How a checkpoint reaches a downstream repository

Downstream repositories do not vendor or pin the factory. Each keeps a thin
`AGENTS.md` that delegates to `$ARU_SDLC_HOME`, and the shell profile exports
`ARU_SDLC_HOME` to a single shared checkout of `Aru_Agentic_SDLC`. A checkpoint
therefore propagates by that one checkout moving, not by three independent
pins, and there is no per-repository version file to update.

Two consequences worth stating plainly:

- Propagation is complete for every repository at once when the shared checkout
  reaches the checkpoint, and incomplete for all of them when it has not.
- A separate clone — a fleet clone, a review clone, a temporary checkout — is
  its own consumer at its own commit, and can lag. Lagging clones are recorded
  below rather than silently refreshed.

## Local-consumption evidence

Observed 2026-08-24T18:43:56Z. All heads read with `git rev-parse HEAD`; all
`dirty` counts from `git status --porcelain`.

| Checkout | Role | Head | Branch | Dirty | At checkpoint |
|---|---|---|---|---|---|
| `~/Projects/Aru_Agentic_SDLC` | Shared `$ARU_SDLC_HOME` resolved by the shell profile | `5b9b6ace50d0e13d0a2bcabf41975b03b5659cd3` | `main` | 0 | yes |
| `~/Projects/jaji-mission-control` | Governed downstream repo | `fdee41ee8ad3ea05a70ad138b8c9a419558712f3` | `main` | 0 | consumes via `$ARU_SDLC_HOME` |
| `~/Projects/hermes-trading-automation` | Governed downstream repo | `f3a9da50cb4024e2845861b17763d9df9502454f` | `main` | 0 | consumes via `$ARU_SDLC_HOME` |
| `~/.aru-sdlc/fresh-20260824144159-83833` | Fleet clone | `5b9b6ace50d0e13d0a2bcabf41975b03b5659cd3` | `main` | 0 | yes |
| `~/.aru-sdlc/fresh-20260824142847-78308` | Fleet clone | `4e2c66d71f5b272acbe7b1eadeeedd4865c5b323` | `main` | 0 | no — pre-checkpoint |
| `~/.aru-sdlc/fresh-20260824142902-78440` | Fleet clone | `4e2c66d71f5b272acbe7b1eadeeedd4865c5b323` | `main` | 0 | no — pre-checkpoint |
| `~/.aru-sdlc/review81-bjCHX8` | Fleet clone | `37e3b1a79d7a4dd6626f679a642169f0fe22799c` | `main` | 0 | no — pre-checkpoint |

The downstream repository heads are their own product commits; they are listed
so the read-back is reproducible, not because a product commit tracks the
Factory checkpoint. The lagging fleet clones were left exactly as found.

## Deterministic review assignment

`create_pr.py` is the sole assigner. It picks the service by issue number and
applies exactly one label while the pull request is still a draft, then marks
it ready:

- `scripts/create_pr.py:37` — the pool, in order: `coderabbit`, `sourcery`, `codeant`.
- `scripts/create_pr.py:46-48` — the rule: index `(issue_id - 1) % 3`.
- `scripts/create_pr.py:267-287` — label-then-ready ordering, plus the CodeAnt trigger comment.

No agent selects or overrides the service. Two live confirmations of the rule,
read back 2026-08-24T18:43:56Z:

| Repository | Issue | PR | Predicted | Label observed |
|---|---|---|---|---|
| Aru_Agentic_SDLC | 384 | 390 | codeant | `review:codeant` |
| hermes-trading-automation | 129 | 142 | codeant | `review:codeant` |

Review-pool label inventory at the same time, from `gh label list`:

| Repository | `review:*` labels present |
|---|---|
| Aru_Agentic_SDLC | `review:coderabbit`, `review:codeant` |
| jaji-mission-control | `review:coderabbit` |
| hermes-trading-automation | `review:coderabbit`, `review:codeant` |

A missing label is not a propagation defect. `create_pr.py` calls
`ensure_label` before applying the assignment, so the label is created on
demand at PR-creation time. No label was created, renamed, or removed to
produce this table.

## Downstream next-PR assignment evidence — pending

**Status: not yet observable. This criterion is not satisfied.**

As of 2026-08-24T18:43:56Z, zero pull requests have been created in any of the
three repositories since the checkpoint merged at 2026-08-24T18:31:11Z. The
most recent pull request in each repository predates the checkpoint:

| Repository | Most recent PR | Created | State | `review:*` label |
|---|---|---|---|---|
| Aru_Agentic_SDLC | 397 | 2026-08-24T17:42:31Z | MERGED | `review:codeant` |
| hermes-trading-automation | 142 | 2026-08-24T14:55:41Z | OPEN | `review:codeant` |
| jaji-mission-control | 125 | 2026-08-24T12:23:37Z | MERGED | `review:coderabbit` |

None of these is the "next governed PR created from the propagated
checkpoint", so none of them is evidence for this criterion. They are listed to
show what was checked, not to stand in for the missing evidence.

Counted with:

```bash
gh pr list --repo gillella/jaji-mission-control --state all --limit 30 \
  --json number,createdAt,labels
```

### Evidence contract to close this out

For each of the three repositories, once the first governed pull request is
created from a checkout at `5b9b6ace50d0e13d0a2bcabf41975b03b5659cd3`, record:

1. repository, PR number and URL, linked `Closes #N` issue, and exact head SHA;
2. the deterministic prediction `(issue_number - 1) % 3` and the single
   `review:*` label actually present, which must match and must be the only
   review-pool label on the PR;
3. timeline evidence that the label was applied while the PR was a draft and
   before the ready transition, and — for a CodeAnt assignment — the URL and
   timestamp of the `@codeant-ai: review` comment posted by the helper;
4. the full JSON from the read-only audit, run from inside that repository:

```bash
python3 "$ARU_SDLC_HOME/scripts/audit_review_assignment.py" --pr 123 \
  --expected-head 0123456789abcdef0123456789abcdef01234567
```

Exit `0` with `"ok": true` means the snapshot is coherent — one service across
linked issues, exactly one matching label, matching heads, and no other review
service holding review or thread evidence. It is not a merge verdict, and zero
assigned-service reviews is a truthful pre-review snapshot rather than proof of
completion.

The audit is read-only. It never edits labels, readiness, comments, reviews,
threads, checks, service configuration, or billing. Completing this record
requires no service invocation by a coding agent: the assigned service runs on
its own, and the merge verdict comes only from the governed gate:

```bash
python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr 123 --dry-run \
  --expected-head 0123456789abcdef0123456789abcdef01234567
```

Until all three records exist and agree, this criterion stays open. Do not
close it by creating a pull request whose purpose is to generate the evidence.

## Preserved state and no-bypass boundary

Observed 2026-08-24T18:43:56Z, before and after this document was written. All
counts from `git worktree list`, `git branch`, and `git status --porcelain`.

| Repository | Worktrees | Local branches | Primary checkout dirty |
|---|---|---|---|
| Aru_Agentic_SDLC | 10 | 7 | 0 |
| jaji-mission-control | 21 | 17 | 0 |
| hermes-trading-automation | 33 | 10 | 0 |

Every one of those worktrees and branches — including in-flight review
worktrees under `.worktrees/` and detached review checkouts under `/private/tmp`
— was left exactly as found. Nothing was pruned, reset, checked out, pulled,
rebased, or deleted.

Boundaries observed while producing this record, and required of anyone
extending it:

- **No lifecycle bypass.** No branch was pushed to a default branch, no pull
  request was merged, and no gate in `merge_pr.py` was skipped or simulated.
- **No coding-agent review.** Coding agents implement and remediate only. The
  assigned review-pool service is the sole review authority for a given PR.
- **No assignment tampering.** The deterministic service was neither chosen
  nor changed by hand, and no `review:*` label was added or removed.
- **No downstream mutation.** No downstream repository, worktree, branch,
  artifact, or production runtime was modified, deployed, or restarted.
- **No billing or account mutation.** Read-only observation only: no trial
  started or extended, no plan selected, no payment details entered, no seat or
  usage limit changed, and no credential read into this document.
- **No fabricated evidence.** A missing observation is recorded as pending, and
  a stale or ambiguous source is recorded as such rather than assumed clean.
