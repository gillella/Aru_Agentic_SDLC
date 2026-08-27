---
name: implement-next-issue
description: Claim and implement one Ready issue in an isolated worktree, then open a reviewed PR.
---

# Implement one issue

1. Read current Git, worktree, PR, and board state.
2. Run `fetch_next_work.py --agent <id> --json`.
3. If it returns a Ready issue, claim it with `claim_issue.py`.
4. Create the isolated worktree with `create_branch.py` and work only there.
5. Make the smallest change inside the declared `touches:` paths.
6. Run the focused verification named by the issue.
7. Commit and push the branch.
8. Open the PR with `create_pr.py`; its body must contain `Closes #N`.
9. Wait for current-head CI and the single assigned authoritative reviewer.
   Prefer CodeRabbit, Sourcery, then CodeAnt. If the assigned external service
   explicitly fails or remains pending for 15 minutes, run
   `create_pr.py --refresh-reviewer <PR>`; it smoke-tests capacity and may assign
   a coding agent other than the author.
10. If feedback exists, use the feedback skill. If CI fails, use the CI skill.

A coding agent may review another agent's code. Never authoritatively review
your own PR. A coding-agent review must read the issue and acceptance criteria,
inspect the exact diff and surrounding code, run focused verification, and
submit a substantive full-current-head `APPROVE` or `REQUEST_CHANGES`
attestation with severity and `file:line` findings.

Stop after one unit. Do not start a scheduler, fleet, background loop, private
queue, or second lifecycle.
