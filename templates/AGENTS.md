<!-- BEGIN ARU_SDLC_GOVERNANCE -->
# Aru Minimal SDLC Governance

For governed software changes, read
`$ARU_SDLC_HOME/docs/KERNEL-CONTRACT.md` and use the current helpers under
`$ARU_SDLC_HOME/scripts`. GitHub Issues and the linked Project Board are the
only lifecycle state.

Require a valid Ready issue, exclusive claim, declared `touches:`, isolated
worktree, smallest acceptable change, a PR with `Closes #N`, the exact-head
`aru-governed-pr` check, one approval of that head from a GitHub account other
than the author, and merge through `merge_pr.py --expected-head`.

This repository is scaffolded for the `__ARU_RUNNER_PROFILE__` runner profile,
so GitHub Actions orchestrates the check and __ARU_RUNNER_TARGET__ supply the
compute. It runs this repository's `.aru/verify.sh` and validates `touches:`
against the actual diff. __ARU_RUNNER_RULE__ `.aru/verify.sh` refuses a workflow
whose declared profile and `runs-on:` disagree. The framework verifier invokes
the required executable `.aru/verify-project.sh`; configure its failing starter
with meaningful product checks and preserve existing consumer verification on updates.

Configured merge queues and pending queue/auto-merge requests are unsupported
and refused before submission. Verification accepts same-repository PR heads
only. Keep the issue In Review until GitHub confirms the exact head merged;
`--finalize` recovers direct merges but refuses historical queue work. Only then
mark Done and clean the worktree.

Every PR, documentation included, needs one approval of its current head from a
GitHub account other than the author: a person, CodeRabbit, or a coding agent on
a different account. A push dismisses earlier approvals. There are no review
tiers, provider rankings, reviewer labels or refresh helpers. Never approve your
own PR; agents sharing one GitHub account cannot approve each other.

The installed skills are exactly `init-agent-project`, `create-github-issue`,
`triage-backlog`, `implement-next-issue`, `remediate-ci-failure`, and
`address-pr-feedback`. Use `fetch_next_work.py` to select work. Do not route to
an unlisted skill or infer a persistent loop from free-form trigger words.

Keep the layers separate: the Kernel governs issue to merge; an optional
external Driver decides when to invoke the next bounded command; this consumer
owns additional risk controls, engineering or release checks, deployment, and
production. The Driver may surface PRs waiting for an approval and must not
create another lifecycle store.

Do not add a scheduler, private queue, handoff file, dashboard, deployment
system, or repository-owned runtime. Missing or stale authority blocks the
transition; never invent fallback state or self-review.
<!-- END ARU_SDLC_GOVERNANCE -->
