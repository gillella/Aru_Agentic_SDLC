# Enforcement Register

| Control | Blocks or authorizes | Mechanism |
| --- | --- | --- |
| issue contract | Backlog -> Ready | `triage_backlog.py` |
| five statuses | invariant-bearing lifecycle transitions | `triage_backlog.py`, `claim_issue.py`, `create_pr.py`, `merge_pr.py` |
| exclusive claim | Ready -> In Progress | `claim_issue.py` |
| path budget | writes outside `touches:` | `hooks/enforce_touches.py`, `aru-governed-pr` actual-diff check |
| default-branch protection | direct push to the resolved remote default branch | `hooks/pre-push` |
| worktree isolation | first implementation edit | `create_branch.py` |
| closure link | PR creation | `create_pr.py` |
| policy-ordered current-head review authority plus configured-timeout refresh | Tier 2-3 review authority | `create_pr.py` |
| exact-head consumer verification | merge | `aru-governed-pr`, `.aru/verify.sh`, `check_ci.py`, `merge_pr.py` |
| one account-assigned runner profile | required verification dispatch | `init_project.py` account policy, workflow `# aru-runner-profile:` marker, `.aru/verify.sh` marker/`runs-on:` agreement |
| no cross-profile runner fallback | required verification dispatch | `.aru/verify.sh` per-profile forbidden patterns: `self-hosted-mac` rejects hosted images, `github-hosted` rejects `self-hosted` |
| unproven verification capacity | Driver work admission | `aru_project_driver/kernel.py` `_ci()`: self-hosted runner inventory, hosted active-workflow plus queue evidence, unassigned account blocked |
| changed-path risk tier | whether authoritative review is required | `common.py`, `merge_pr.py` |
| unresolved findings | merge | `fetch_pr_feedback.py`, `merge_pr.py` |
| current-head external or coding-agent verdict | Tier 2-3 merge | `merge_pr.py` |
| base/head race | merge | `merge_pr.py --expected-head` |
| issue Done and cleanup | close-out | `merge_pr.py`, `cleanup_worktrees.py` |
| reverse gear | unsafe merged change | `revert_merge.py` |
| anti-regrowth budgets | CI | `tests/test_surface.py` |

Anything not listed here is guidance, not an authorization mechanism.
External Driver timing and consumer risk policy are therefore coordination and
product policy, respectively; neither is a second Kernel gate. Their boundary
is defined once in `docs/KERNEL-CONTRACT.md`.

For this repository, the live default-branch ruleset requires the
`aru-governed-pr` context produced by the authenticated GitHub Actions App.
The portable rule still cannot make `merge_pr.py` technically exclusive for a
repository administrator; that stronger boundary remains consumer-owned.
