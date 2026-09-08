<!-- BEGIN ARU_SDLC_GOVERNANCE -->
# Aru Minimal SDLC Governance

For governed software changes, read
`$ARU_SDLC_HOME/docs/KERNEL-CONTRACT.md` and use the current helpers under
`$ARU_SDLC_HOME/scripts`. GitHub Issues and the linked Project Board are the
only lifecycle state.

Require a valid Ready issue, exclusive claim, declared `touches:`, isolated
worktree, smallest acceptable change, a PR with `Closes #N`, the exact-head
`aru-governed-pr` check, one authoritative reviewer distinct from the author
when risk requires one, and merge through `merge_pr.py --expected-head`.

This repository is scaffolded for the `__ARU_RUNNER_PROFILE__` runner profile,
so GitHub Actions orchestrates the check and __ARU_RUNNER_TARGET__ supply the
compute. It runs this repository's `.aru/verify.sh` and validates `touches:`
against the actual diff. __ARU_RUNNER_RULE__ `.aru/verify.sh` refuses a workflow
whose declared profile and `runs-on:` disagree.

Configured merge queues and pending queue/auto-merge requests are unsupported
and refused before submission. Verification accepts same-repository PR heads
only. Keep the issue In Review until GitHub confirms the exact head merged;
`--finalize` recovers direct merges but refuses historical queue work. Only then
mark Done and clean the worktree.

Review is risk-tiered from the actual changed paths: Tier 0 documentation and
Tier 1 ordinary code do not wait for authoritative review; Tier 2
sensitive/contract and Tier 3 production/destructive changes require one
distinct authoritative review. Unrecognized safe paths fail upward to Tier 2;
empty, malformed, or unsafe paths fail to Tier 3. Consumer policy may add
controls but must not downgrade the Kernel tier.

For Tier 2-3, CodeRabbit is the sole preferred external provider. Sourcery and
CodeAnt are retired: registration and historical evidence never make them
eligible for new assignments. Use one bounded authenticated check for usable
CodeRabbit access to the current repository/head. If access is denied, errored,
rate-limited, unavailable or unproven, immediately select an available distinct
coding reviewer; never wait through retired providers. Generic green checks,
cached installation inventory and empty/skipped reviews are not approval.
The optional `review-policy:timeout=<seconds>` is a completion deadline only
for an accepted review (default 900 seconds, informed by the observed 11-minute
CodeRabbit review). Explicit unavailability bypasses it. Ranked declarations
remain invalid. Use `create_pr.py --refresh-reviewer <PR>` to migrate a retired
assignment; do not hand-edit authority or erase prior findings/history.

The installed skills are exactly `init-agent-project`, `create-github-issue`,
`triage-backlog`, `implement-next-issue`, `remediate-ci-failure`, and
`address-pr-feedback`. Use `fetch_next_work.py` to select work. Do not route to
an unlisted skill or infer a persistent loop from free-form trigger words.

Keep the layers separate: the Kernel governs issue to merge; an optional
external Driver decides when to invoke the next bounded command; this consumer
owns additional risk controls, engineering or release checks, deployment, and
production. For a Tier 2-3 review, the Driver owns the single pending-review
continuation event defined in the canonical contract and must not create
another lifecycle store.

Do not add a scheduler, private queue, handoff file, dashboard, deployment
system, or repository-owned runtime. Missing or stale authority blocks the
transition; never invent fallback state or self-review.
<!-- END ARU_SDLC_GOVERNANCE -->
