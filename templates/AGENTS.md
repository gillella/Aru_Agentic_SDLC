<!-- BEGIN ARU_SDLC_GOVERNANCE -->
# Aru Minimal SDLC Governance

For governed software changes, read
`$ARU_SDLC_HOME/docs/KERNEL-CONTRACT.md` and use the current helpers under
`$ARU_SDLC_HOME/scripts`. GitHub Issues and the linked Project Board are the
only lifecycle state.

Require a valid Ready issue, exclusive claim, declared `touches:`, isolated
worktree, smallest acceptable change, a PR with `Closes #N`, the exact-head
`aru-governed-pr` server check, one authoritative reviewer distinct from the
author when risk requires one, and merge through
`merge_pr.py --expected-head`. The server check runs this repository's
`.aru/verify.sh` and validates `touches:` against the actual diff. Local
verification is optional preflight or audit evidence.

A `merge_pr.py` merge-queue or auto-merge result is still in flight. Keep the
issue In Review until GitHub confirms the exact head merged; only then mark Done
and clean the worktree.

Review is risk-tiered from the actual changed paths: Tier 0 documentation and
Tier 1 ordinary code do not wait for authoritative review; Tier 2
sensitive/contract and Tier 3 production/destructive changes require one
distinct authoritative review. Unrecognized safe paths fail upward to Tier 2;
empty, malformed, or unsafe paths fail to Tier 3. Consumer policy may add
controls but must not downgrade the Kernel tier.

For Tier 2-3, follow the validated repository
`review-policy:primary=<authority>`, contiguous
`review-policy:fallback-N=<authority>`, and
`review-policy:timeout=<seconds>` label definitions. Without them, use the first
registered external provider, then Claude Code, OpenAI Codex, xAI Cursor, and
Google Antigravity with a 120-second timeout. Inspect the effective policy with
`create_pr.py --reviewer-status --json`; invalid declarations fail closed.
Remove `reviewer-registered:<service>` when external access ends.

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
