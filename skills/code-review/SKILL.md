---
name: code-review
description: Refuses coding-agent PR review because the assigned review-pool service is the sole review authority.
---

# Code Review Policy

The assigned review-pool service is the sole PR code-review authority in every
Factory-governed project. `create_pr.py` assigns exactly one of
`review:coderabbit`, `review:sourcery`, or `review:codeant` to each ordinary
PR. Claude, Codex, Cursor, and Antigravity must never claim, perform, or be
dispatched for review, and their comments, approvals, `reviewed-by:` labels, or
head attestations cannot satisfy the merge gate.

When invoked, do not review or inspect the PR. Return it to the factory picker,
which routes assigned-service findings to the PR author or adopted
implementation/remediation agent. If assigned-service evidence is missing,
pending, failed, stale, ambiguous, or unavailable, leave the PR blocked; there
is no coding-agent fallback.

Human operational authorization for money, production cutover, destructive
migration, credentials, or external-account mutations remains a separate gate
and is never supplied by CodeRabbit review.
