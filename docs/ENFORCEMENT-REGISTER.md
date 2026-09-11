# Enforcement Register

| Control | Blocks or authorizes | Mechanism |
| --- | --- | --- |
| issue contract | Backlog -> Ready | `triage_backlog.py` |
| Ready contract pin | merge after criteria or `touches:` changed since promotion | `triage_backlog.py` stamps `ready:<digest>`; `merge_state.issue_gate` recomputes it |
| five statuses | invariant-bearing lifecycle transitions | `triage_backlog.py`, `claim_issue.py`, `create_pr.py`, `merge_pr.py` |
| exclusive claim | Ready -> In Progress | `claim_issue.py` |
| path budget | writes outside `touches:` | `hooks/enforce_touches.py`, `aru-governed-pr` actual-diff check |
| default-branch protection | direct push to the resolved remote default branch | `hooks/pre-push` |
| worktree isolation | first implementation edit | `create_branch.py` |
| closure link | PR creation | `create_pr.py` |
| policy-ordered current-head review authority plus configured-timeout refresh | Tier 2-3 review authority | `create_pr.py` |
| no reviewer available | Tier 2-3 merge (not PR creation) | `create_pr.py` opens the PR as `needs-reviewer`; `merge_pr.py` refuses without a `review:*` authority |
| exact-head consumer verification | merge | `aru-governed-pr`, `.aru/verify.sh`, `check_ci.py`, `merge_pr.py` |
| one account-assigned runner profile | required verification dispatch | `init_project.py` account policy, workflow `# aru-runner-profile:` marker, `.aru/verify.sh` marker/`runs-on:` agreement |
| no cross-profile runner fallback | required verification dispatch | `.aru/verify.sh` per-profile forbidden patterns: `self-hosted-mac` rejects hosted images, `github-hosted` rejects `self-hosted` |
| unproven verification capacity | Driver work admission | `aru_project_driver/kernel.py` `_ci()`: self-hosted runner inventory, hosted active-workflow plus queue evidence, unassigned account blocked |
| changed-path risk tier | whether authoritative review is required; all Kernel scripts are sensitive | `review_risk.py`, `merge_pr.py` |
| current board and dependencies | merge submission and direct-merge finalization | `merge_state.py` fresh Project/card/status-field identity, label agreement and closed dependencies |
| configured product verification | consumer check success | `.aru/verify.sh` requires executable `.aru/verify-project.sh`; generated starter fails |
| unresolved findings | merge | `fetch_pr_feedback.py`, `merge_pr.py` |
| current-head external or coding-agent verdict | Tier 2-3 merge | `merge_pr.py` |
| base/head race | merge | `merge_pr.py --expected-head` |
| helper-only merge (optional) | merge submission by any other path | `merge_pr.py` posts `aru-merge-authorized` as the configured merge-authority App; the ruleset requires it pinned by `integration_id` |
| issue Done and cleanup | close-out | `merge_pr.py`, `cleanup_worktrees.py` |
| reverse gear | unsafe merged change | `revert_merge.py` |
| anti-regrowth budgets | CI | `tests/test_surface.py` |

Anything not listed here is guidance, not an authorization mechanism.
External Driver timing and consumer risk policy are therefore coordination and
product policy, respectively; neither is a second Kernel gate. Their boundary
is defined once in `docs/KERNEL-CONTRACT.md`.

For this repository, the live default-branch ruleset requires the
`aru-governed-pr` context produced by the authenticated GitHub Actions App.
Without the merge-authority App, nothing server-side makes `merge_pr.py` the
only merge path. With it, only a holder of that App's key can satisfy the
pinned check, and a repository administrator can still edit the ruleset; that
residual boundary remains consumer-owned.
