# 0003 - Merge evidence is compared, not locked

Date: 2026-09-12. Status: accepted.

The contract states the rule: two full gate evaluations, one bounded final
revalidation, and the fact that GitHub metadata reads and the merge API are
non-atomic. This record keeps the detail of *which* evidence each pass compares
and which races remain, which is implementation explanation rather than a gate.

## Detail, as the contract stated it

The PR scope hook binds its final head reread to the same single parsed closing
issue directive, then rereads the issue and compares its parsed touches scope.
Benign issue prose and declaration formatting or ordering remain compatible.
Merge submission compares two full gate evaluations, including
the actual changed paths, declared touches, claimant, linked Project card
identity/status, resolved dependency identities/states, and each parsed acceptance
item's text and completion state. Criterion counts alone are not authorization
evidence. After CI and review reads, one bounded final PR/issue validation checks
the open, ready PR, exact head and base, closing issue, and current issue gate
against that evidence. One final head/base-bound queue snapshot refuses observed
queue configuration or pending queue-entry/auto-merge request drift before the
merge command; it does not rerun the full gates or cancel an existing request.
After those rereads, one bounded review validation rereads the reviews and
unresolved threads and again requires a non-author approval of the exact head;
a withdrawn, dismissed or superseded approval refuses submission. Missing,
unreadable, invalid, or changed authorization blocks submission. Description or
verification prose outside these semantic fields may change without refusal.

Separate GitHub metadata reads and the merge API are **non-atomic**. A bounded
reread catches observed drift; it does not lock issue metadata or make the
operations a transaction. Changes after their last read can still race submission,
including queue configuration changes. The expected-head argument binds the submitted commit,
not all external metadata. Existing head, CI, review, thread, scope, and queue-state
provenance gates remain required. Post-merge close-out revalidates current
authority before Done; it cannot prevent or undo a merge already submitted.
