---
name: implement-next-issue
description: Claim and implement one Ready issue in an isolated worktree, then open a governed PR.
---

# Implement one issue

1. Read current Git, worktree, PR, and board state.
2. Run `python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" --agent <id> --json`.
3. If it returns a Ready issue, claim that exact issue with `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py"`.
4. Create the isolated worktree with `python3 "$ARU_SDLC_HOME/scripts/create_branch.py"` and work only there.
5. Make the smallest change inside the declared `touches:` paths.
6. Keep the consumer-owned `.aru/verify-project.sh` proportional to the issue's risk.
   Run it or narrower commands locally when useful as preflight; local output is
   optional audit evidence, not merge authority.
7. Commit and push the branch.
8. Open the PR with `python3 "$ARU_SDLC_HOME/scripts/create_pr.py"`; its body must contain `Closes #N`.
9. Wait for the exact-head `aru-governed-pr` check on the repository's one
   assigned runner profile: `self-hosted-mac` for `gillella` personal
   repositories, `github-hosted` for `Unum-Inc`. Never switch profiles or edit
   `runs-on:` to get a check to run — an offline `aru-ci` pool leaves the check
   queued, and a hosted repository is never sent to a personal Mac.
   GitHub Actions supplies the check identity while the Factory's verifier runs on
   that profile's runners and validates the linked issue's `touches:` boundary
   against the actual diff. Ad hoc local runs remain preflight evidence.
   Every PR also waits for one approval of its exact head from a GitHub account
   other than yours; `python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py"` reports that as `review` work, which
   the author cannot do. Do not poll for reviews.
10. Always read `python3 "$ARU_SDLC_HOME/scripts/fetch_pr_feedback.py" --pr <N>` before merge, even with an
    approval and no inline comments. If feedback exists, use the feedback skill.
    If the server check fails, use the CI remediation skill.

A coding agent may review another agent's code when it works under a different
GitHub account. Never approve your own PR. A review must read the issue and
acceptance criteria, inspect the exact diff and surrounding code, run focused
verification, and submit a GitHub `APPROVE` or `REQUEST_CHANGES` review of the
current head with severity, concrete consequence, evidence and `file:line`
findings. Put each blocking defect in an inline review thread; summary findings
must use explicit `[P0]`/`[P1]` labels when applicable. A COMMENT review may still
contain blocking findings and must never be treated as clearance because a
separate review approves the PR. The writer owns remediation; a reviewer who
pushes a fix cannot also approve it. Use the feedback skill's fix, evidence-backed disagreement,
advisory-only and accepted-follow-up dispositions. Inspect existing relevant
regression coverage before requesting new tests. One approval of the current
head is sufficient; no extra brands or ceremonial approval rounds are required.

Stop after one claimed issue in this worktree. Another operator may invoke the
same single-agent picker independently, but do not start a scheduler, fleet,
background loop, private queue, or second lifecycle.
