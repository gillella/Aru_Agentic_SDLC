---
name: code-review
description: Refuses coding-agent PR review because CodeRabbit is the sole review authority.
---

# Code Review Policy

CodeRabbit is the sole PR code-review authority in every Factory-governed
project. Claude, Codex, Cursor, and Antigravity must never claim, perform, or be
dispatched for review, and their comments, approvals, `reviewed-by:` labels, or
head attestations cannot satisfy the merge gate.

When invoked, do not review or inspect the PR. Return it to the factory picker,
which routes CodeRabbit findings to the PR author or adopted
implementation/remediation agent. If CodeRabbit is missing, pending, failed,
rate-limited, stale, ambiguous, or unavailable, leave the PR blocked; there is
no coding-agent fallback.

Human operational authorization for money, production cutover, destructive
migration, credentials, or external-account mutations remains a separate gate
and is never supplied by CodeRabbit review.
