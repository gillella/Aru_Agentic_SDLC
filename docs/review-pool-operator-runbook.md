# Review-Pool Operator Runbook

## Purpose & Scope

Operational procedure for the deterministic three-service review pool
(CodeRabbit, Sourcery, CodeAnt) introduced by the review-pool cutover
(`scripts/create_pr.py`, `scripts/merge_pr.py`). It covers what a factory
agent or human operator does around PR assignment, review triggering, exact-head
evidence, remediation, governed dry-runs, and billing/trial boundaries.

This runbook does not grant review, merge, or account authority. Assigned
review-pool services are the sole PR code-review authority
(`AGENTS.md` §Process Ownership and Merge Authority); coding agents implement
and remediate only. Real-money execution, production cutover, destructive
migration, credential use, and external-account mutation stay separate,
mandatory human gates that no review evidence satisfies.

## 0. Preflight

Before assigning or editing anything, confirm the claimed issue is open and
inspect git log, branches, open PRs, and board status for related work
already in flight (`AGENTS.md` §1 Strict Issue-First Execution, §8 Session
State Memory). Execute the review from a PR-branch worktree under
`.worktrees/` (`AGENTS.md` §4 Mandatory Worktree Isolation). For `type:feat`,
`needs-design`, money, PII, schema, migration, or other irreversible work,
obtain the implementation plan required by `implement-next-issue` before the
first edit (`AGENTS.md` §9 Plan Before Editing). These gates apply to
remediation edits in §4 as well as initial assignment.

## 1. Assignment

`create_pr.py` assigns exactly one review service per issue, deterministically:

```text
REVIEW_SERVICES[(issue_id - 1) % 3]  ->  coderabbit | sourcery | codeant
```

The label (`review:coderabbit`, `review:sourcery`, or `review:codeant`) is
applied to the PR **while it is still draft**, before the PR is marked ready.
Only one review-pool label may ever be present; `merge_pr.py` refuses to
resolve a service when zero or more than one is set
(`check_reviews` in `scripts/merge_pr.py`). Never add or swap a review-pool
label by hand. `merge_pr.py` recomputes the assigned service from every
linked issue and rejects a mismatched label, making the PR unmergeable
instead of changing accepted review authority.

## 2. Trigger

The operator invokes the helper once:

```shell
python3 "$ARU_SDLC_HOME/scripts/create_pr.py" --issue <ID> --agent <AGENT_ID> \
  [--model-family <family>] --verify-command "<cmd>" [--verify-command "<cmd>" ...]
```

Each `--verify-command` becomes part of the committed verification evidence
(§3), so it must never contain a literal token, password, PII, or
secret-bearing URL. Commands that need a secret must read it from the
process environment themselves; do not pass secret values as command-line
arguments. `run_cmd` in `scripts/common.py` invokes each command as an
argv list without a shell, so shell metacharacters and variable expansion
are not interpreted, and `sanitize_command` redacts recognized secret
patterns (known option names, headers, URL credentials, opaque `-c`/`-e`
values) before the evidence is recorded — but it cannot redact an
unrecognized literal, so keeping secrets out of the command line is the
operator's responsibility, not the sanitizer's.

Everything below is performed internally by `create_pr()` and
`finalize_review_assignment()` in `scripts/create_pr.py` — the operator does
not type these `gh` commands directly:

1. `gh pr create --draft` opens the PR; the PR body must include
   `Closes #<issue_number>` (`check_issue_link` in `scripts/merge_pr.py`).
2. The assigned `review:<service>` label is added.
3. The PR is marked ready (`gh pr ready`).
4. For `review:codeant` only, a `@codeant-ai: review` comment fires the
   manual trigger; on failure, the function attempts to restore draft state so
   the operator can retry. If that rollback also fails, the PR can remain ready.

CodeRabbit's auto-review is centrally scoped to non-draft PRs carrying
`review:coderabbit` (`.coderabbit.yaml` `reviews.auto_review`), so step 3
alone triggers CodeRabbit — no manual comment is needed. Sourcery reviews
head-bound pushes on its own trigger once assigned. Re-review after a
remediation push is service-specific: CodeRabbit needs an explicit
`@coderabbitai full review` comment (`CODERABBIT_FULL_REVIEW_REQUEST` in
`scripts/merge_pr.py`); Sourcery and CodeAnt re-evaluate the new head without
one.

## 3. Exact-head evidence

`merge_pr.py` never accepts review evidence bound to a stale commit. Any push
after a review invalidates it — re-review the new head before merging
(`docs/project_board_workflow.md`). Per service:

- **CodeRabbit** — one completed, substantive review in PR history, not
  `CHANGES_REQUESTED`, plus a successful authoritative CodeRabbit status tied
  to the current head (`has_authoritative_coderabbit_review`). Status must
  come from the recognized CodeRabbit app/login and be `COMPLETED`/`SUCCESS`;
  missing, pending, failed, rate-limited, stale, ambiguous, or spoofed
  evidence blocks merge.
- **Sourcery** — one successful, head-bound `Sourcery review` check
  (`_sourcery_check`).
- **CodeAnt** — one authoritative exact-head `codeant-ai` review object, not
  `CHANGES_REQUESTED` (`_codeant_latest_review`).

All three additionally require every blocker enforced by `check_reviews`
(`scripts/merge_pr.py`) to be clear:

- The assigned-service thread gate: zero unresolved and zero outdated-unfixed
  threads raised by the assigned service itself.
- Aggregate unresolved, outdated-unfixed, and "unfixed" counts across the
  whole PR are zero. "Unfixed" means a thread resolved with no commit,
  size-waiver, or verification-refresh after the finding was raised — see
  "Closing out a review finding" in `docs/project_board_workflow.md`.
- No non-advisory human reviewer's latest verdict is `CHANGES_REQUESTED`.
  This applies regardless of which service is assigned — a Sourcery- or
  CodeAnt-assigned PR still blocks on an unaddressed human
  `CHANGES_REQUESTED`.

## 4. Remediation

Follow `skills/address-pr-feedback/SKILL.md`:

1. `python3 scripts/fetch_pr_feedback.py --pr <PR_ID>` lists unresolved
   thread text and location, but not outdated-unfixed/unfixed counts or a
   human `CHANGES_REQUESTED` verdict. Confirm the full blocker set with the
   governed dry-run (§5, `--expected-head <SHA>`) — every blocker listed in
   §3 must clear, not just unresolved threads.
2. Implement fixes per §0 in the PR branch worktree under `.worktrees/`; for
   `type:feat`, `needs-design`, money, PII, schema, migration, or other
   irreversible-work findings, confirm a plan is in place before editing.
   Run the local suite.
3. Commit and push. This invalidates the prior review by design — the PR
   returns to `review`, not `ready-to-merge`.
4. Refresh verification evidence for the new head:
   `python3 scripts/create_pr.py --refresh-pr <PR_ID> --verify-command "<cmd>" ...`
5. Reply on each addressed thread with the fix commit hash and resolve it, or
   reply `Withdrawn: <reason>` for a finding argued down instead of fixed.
6. Re-trigger review at the new head per §2 (CodeRabbit: comment
   `@coderabbitai full review`; Sourcery/CodeAnt: the new push is sufficient).

A finding is never closed by resolving its thread alone — `merge_pr.py`
checks for a commit, a matching size-waiver/evidence edit, or a `Withdrawn:`
reply after the finding was raised.

## 5. Governed dry-run

```shell
python3 scripts/merge_pr.py --pr <PR_ID> --dry-run [--expected-head <SHA>] [--json]
```

Runs every Definition-of-Done gate (open, issue link, verification, CI,
review, rebased, size, tests, spec-sync, review rounds, and per-linked-issue
acceptance) against the live PR and merges nothing. `--expected-head`
refuses the run if the live head differs from what the caller expected —
use it whenever the head SHA is already known, to catch a race against a
concurrent push. Exit `0` means every gate passed;
exit `3` means Definition of Done is unmet and names the first failing gate;
exit `1` is a tooling/API error, not a gate verdict. If the `ci` gate fails,
invoke `remediate-ci-failure` (`skills/remediate-ci-failure/SKILL.md`) before
rerunning the dry-run — do not rerun against an unfixed CI failure. A passing
dry-run is evidence the PR is mergeable, not a merge — `merge_pr.py --pr
<PR_ID>` (without `--dry-run`) performs the actual merge and close-out.

## 6. Billing boundaries

- No script in this repository reads, writes, or otherwise touches billing
  or trial-conversion state for CodeRabbit, Sourcery, or CodeAnt. Assignment,
  triggering, and merge gating operate entirely on public PR/review/check
  data.
- Triggering a review can consume included vendor quota and may incur
  vendor-side usage-based charges when the account is eligible, even though
  no repository script touches billing state. Confirm account and plan
  eligibility before triggering a review.
- CodeRabbit's configuration is inherited from the organization-level
  `gillella/coderabbit` config (`.coderabbit.yaml` header); this repo does
  not manage its account or plan.
- Sourcery and CodeAnt trial-expiry and paid-conversion dates are tracked as
  an external account read-back, not a repository change (see issue #384).
  An operator may record what they observe on the vendor dashboard; nothing
  here or in the vendor dashboard should be changed to enable billing as part
  of running a pilot.
- Dashboard-side settings called out during the review-pool cutover (Sourcery
  Labels allowlist, CodeAnt automatic-review disable/manual-trigger) remain
  external follow-ups; this runbook does not authorize applying them.

## 7. Operator checklist

- [ ] Preflight complete: issue confirmed open, session state inspected,
      running from `.worktrees/`, and (for high-risk work per §0) plan
      obtained before editing.
- [ ] Exactly one `review:<service>` label, applied before `gh pr ready`.
- [ ] Assigned service's exact-head evidence present per §3.
- [ ] The assigned-service thread gate passes and aggregate unresolved,
      outdated-unfixed, and unfixed counts are zero; every resolved finding
      is fixed, explicitly withdrawn, or supported by the required
      size-waiver/verification-refresh evidence.
- [ ] No non-advisory human reviewer's latest verdict is `CHANGES_REQUESTED`.
- [ ] `merge_pr.py --pr <PR_ID> --dry-run --expected-head <SHA>` exits `0`
      before requesting merge, using the recorded exact head.
- [ ] No repository script changed billing, trial, or account-plan state,
      and vendor account billing status/review eligibility was confirmed
      before triggering review.
