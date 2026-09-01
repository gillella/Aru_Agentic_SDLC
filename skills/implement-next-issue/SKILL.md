---
name: implement-next-issue
description: Claim and implement one Ready issue in an isolated worktree, then open a governed PR.
---

# Implement one issue

1. Read current Git, worktree, PR, and board state.
2. Run `fetch_next_work.py --agent <id> --json`.
3. If it returns a Ready issue, claim that exact issue with `claim_issue.py`.
4. Create the isolated worktree with `create_branch.py` and work only there.
5. Make the smallest change inside the declared `touches:` paths.
6. Keep the consumer-owned `.aru/verify.sh` proportional to the issue's risk.
   Run it or narrower commands locally when useful as preflight; local output is
   optional audit evidence, not merge authority.
7. Commit and push the branch.
8. Open the PR with `create_pr.py`; its body must contain `Closes #N`.
9. Wait for the exact-head `aru-governed-pr` check on the dedicated `aru-ci`
   self-hosted macOS arm64 pool. Never change the workflow to a GitHub-hosted
   runner when that pool is offline. Tier 0 documentation and Tier 1 ordinary
   code do not wait for authoritative review; Tier 2
   sensitive/contract and Tier 3 production/destructive changes also wait for
   one assigned reviewer distinct from the author. Unrecognized or invalid
   paths fail upward.
   GitHub Actions supplies the check identity while `.aru/verify.sh` runs on an
   operator-owned Mac and validates the linked issue's `touches:` boundary
   against the actual diff. Ad hoc local runs remain preflight evidence.
   For Tier 2-3, do not hand-pick or hand-edit authority. The external Driver
   owns the one continuation event in `docs/KERNEL-CONTRACT.md`;
   `create_pr.py --refresh-reviewer <PR>` alone decides whether authority
   changes. If substantive coding review aborts or loses capacity, pass the
   truthful reason through
   `--coding-reviewer-unavailable <reason>`.
10. If feedback exists, use the feedback skill. If the server check fails, use
    the CI remediation skill.

A coding agent may review another agent's code. Never authoritatively review
your own PR. A coding-agent review must read the issue and acceptance criteria,
inspect the exact diff and surrounding code, run focused verification, and
submit a substantive full-current-head `APPROVE` or `REQUEST_CHANGES`
attestation with severity and `file:line` findings.

Stop after one claimed issue in this worktree. Another operator may invoke the
same single-agent picker independently, but do not start a scheduler, fleet,
background loop, private queue, or second lifecycle.
