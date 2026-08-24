# Review-pool pilot evidence

The three-service review pool assigns exactly one authority to each pull
request: CodeRabbit, Sourcery, or CodeAnt. A live pilot proves the integration
with GitHub evidence; it is not a coding-agent review and it does not authorize
a merge. Coding agents implement and remediate findings only.

This document defines what to record. It intentionally contains no pilot
outcomes. Add a result only after reading it back from the live pull request
and service account.

## Read-only assignment audit

Run the audit against the full commit SHA being examined:

```bash
python3 scripts/audit_review_assignment.py --pr 123 --expected-head 0123456789abcdef0123456789abcdef01234567
```

The command reads GitHub through the same pull-request and review-evidence
loaders as `merge_pr.py`. It never edits labels, readiness, comments, reviews,
threads, checks, account configuration, or billing. Output is JSON using schema
`aru.review-assignment-audit.v1`.

Exit `0` means the snapshot is internally coherent: linked issues all map to
one deterministic service, exactly one matching `review:*` label exists, the
requested/PR/evidence heads match exactly, the final assignment snapshot is
unchanged, evidence fields are well formed, and no other known review service
submits a review or owns attributable thread evidence. Exit `1` means at least
one mismatch or unavailable evidence source was reported. Argument errors exit
`2`.

An audit with `"ok": true` is not a merge verdict. In particular, zero
assigned-service reviews is a truthful pre-review snapshot, not evidence of
review completion. Use the governed `merge_pr.py --dry-run --expected-head`
gate after the assigned service completes.

### Evidence fields

| Field | Meaning |
|---|---|
| `pr` | Requested and returned PR numbers plus current open/draft state. |
| `assignment.linked_issues` | Every `Closes #N` link and its deterministic service. |
| `assignment.expected_service` | The one service shared by all linked issues; `null` when it cannot be determined safely. |
| `assignment.review_labels` | Current authoritative review-pool labels on the PR. |
| `assignment.label_service` | Service named by exactly one current review label. |
| `assignment.validated_service` | Result of the merge gate's label-plus-linked-issue validation. |
| `heads.expected` | Full SHA supplied by the operator, when present. |
| `heads.pr` | Head from the PR snapshot. Check rollup entries describe this snapshot. |
| `heads.review_evidence` | Head repeated across the paginated review and thread reads. A concurrent push makes evidence unavailable instead of mixing heads. |
| `heads.final_pr` | Head re-read after review/thread pagination. A missing or moved final head fails closed, as does a same-head change to the PR body or authoritative review labels. |
| `checks` | Current check total, normalized entries, and counts for recognized review-service checks. Status and conclusion remain separate. |
| `reviews` | Raw total/exact-head review-object counts plus substantive counts. Assigned/all-service authority counts exclude pending, dismissed, and empty commented records; spoofed or unknown automation actors fail closed. |
| `threads.aggregate` | Unresolved, unfixed, outdated-unfixed, outdated-addressed, body-addressed, and withdrawn counts used by the merge evidence loader. |
| `threads.assigned_service` | Unresolved, unfixed, and outdated-unfixed counts attributable to the assigned service. |
| `threads.by_service` | The same attributable counts for every review-pool service, used to detect an unassigned service. |
| `mismatches` | Stable code/message objects explaining every fail-closed inconsistency found in the snapshot. |

The audit reports evidence; it does not manufacture missing evidence. An empty
check or review list is recorded as zero. A missing, malformed, ambiguous, or
stale source is a mismatch rather than an assumed zero.

An unassigned service may emit a successful status while its label filter
records that review was skipped. The audit reports that check but does not call
the status alone a review run. An unassigned review object or attributable
thread count is a mismatch; confirm skip/run meaning from the service comment
or dashboard when recording the live pilot.

## Live-pilot record

Record these fields for each service only after its pilot exists:

- repository, PR URL/number, linked issue, and exact head SHA;
- Factory base/checkpoint SHA used to create the pilot;
- PR creation evidence showing the assigned `review:*` label was applied while
  the PR was draft and before the ready transition;
- assigned-service trigger evidence, if the service requires a trigger;
- the complete JSON from `audit_review_assignment.py --expected-head`;
- assigned-service review/check identity, state, conclusion, submitted time,
  and exact reviewed/check head;
- finding URL and later fix SHA when a validated finding exists;
- exact-head re-review/check evidence after every fix push;
- assigned-service and aggregate unresolved/unfixed thread counts;
- `merge_pr.py --dry-run --expected-head` output for the candidate head and,
  after merge elsewhere, the merged checkpoint when the issue requires both;
- dashboard or central-configuration read-back proving other services did not
  run; and
- observation time and source for trial expiry and paid-conversion boundaries.

The audit is a current snapshot and cannot by itself prove event ordering.
Use the GitHub timeline or helper output for label-before-ready evidence. For a
CodeAnt assignment, `create_pr.py` posts the exact `@codeant-ai: review`
trigger after marking the already-labelled draft ready. Record that comment's
URL and timestamp. Do not post a duplicate manual trigger when the helper's
comment is present.

Service completion remains service-specific:

- CodeRabbit: one completed substantive exact-head review plus its authoritative
  exact-head status and zero unresolved/unfixed assigned-service threads.
- Sourcery: one successful producer-validated, PR-linked, exact-head
  `Sourcery review` check and zero unresolved/unfixed assigned-service threads.
- CodeAnt: one substantive exact-head `codeant-ai` review object that is not
  left at `CHANGES_REQUESTED`, plus zero unresolved/unfixed assigned-service
  threads.

## Billing boundary

Pilot authorization is read-only. It permits reading service dashboards,
installation/configuration state, trial expiry, and displayed plan boundaries.
It does not permit starting or extending a trial, selecting a paid plan,
entering payment details, accepting a paid conversion, increasing seats or
usage limits, or changing organization/repository billing settings.

Record only the displayed expiry date, conversion condition, currency/price if
already shown, source URL or dashboard name, and observation timestamp. Do not
copy payment-card data, invoices containing personal information, tokens, or
other credentials into GitHub. If the account requires a mutation to reveal a
price or expiry, stop and report that the read-back is unavailable; do not
enable billing to complete the pilot.
