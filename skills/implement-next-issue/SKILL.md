---
name: implement-next-issue
description: Claim and implement one Ready issue in an isolated worktree, then open a reviewed PR.
---

# Implement one issue

1. Read current Git, worktree, PR, and board state.
2. Run `fetch_next_work.py --agent <id> --json`.
3. If it returns a Ready issue for that lane, claim it with `claim_issue.py`.
4. Create the isolated worktree with `create_branch.py` and work only there.
5. Make the smallest change inside the declared `touches:` paths.
6. Run only focused verification for the issue: acceptance predicates,
   changed-path lint/type/compile checks, directly affected tests, and
   invariant or secret gates. Do not run a repository-wide suite.
7. Commit and push the branch.
8. Open the PR with `create_pr.py`; its body must contain `Closes #N` and a
   `## Verification` section listing only those focused commands.
9. After each push or edit to the verification section, refresh the exact-head
   PR-body evidence with `create_pr.py --refresh-verification <PR>`.
10. Wait for that exact-head local verification and the single assigned
    authoritative reviewer.
   Initial authority rotates deterministically across registered external
   services and locally configured, bound coding identities while excluding the
   author. If the assigned external service explicitly fails or remains pending
   for 15 minutes, an external event or timer must invoke
   `create_pr.py --refresh-reviewer <PR>` (the kernel itself has no scheduler);
   it smoke-tests capacity (verifying liveness only) and may assign a coding agent
   other than the author. If substantive review execution hits quota, recover
   immediately with `create_pr.py --refresh-reviewer <PR> --coding-reviewer-unavailable <reason>`.
   Only `reviewer-registered:<service>` external providers and coding identities with
   `reviewer-binding:<identity>=<github-login>` are eligible. Before coding
   fallback, require a strict local `ARU_CODING_REVIEWERS` allowlist using
   `family:identity` and `claude-code:identity@subscription` entries. Missing,
   malformed, duplicate, or unbound configuration fails closed; subscriptions
   may be added or removed without changing code.
11. If feedback exists, use the feedback skill. If local verification is stale
    or invalid, use the CI skill.

A coding agent may review another agent's code. Never authoritatively review
your own PR. A coding-agent review must read the issue and acceptance criteria,
inspect the exact diff and surrounding code, run focused verification, and
submit a substantive full-current-head `APPROVE` or `REQUEST_CHANGES`
attestation with severity and `file:line` findings.

Stop after one claimed issue in this worktree. Another free operator-approved
lane may independently run the same bounded picker flow in parallel, but do not
start a scheduler, fleet, background loop, private queue, or second lifecycle.
