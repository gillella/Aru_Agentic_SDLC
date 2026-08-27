# Enforcement Register

| Control | Blocks or authorizes | Mechanism |
| --- | --- | --- |
| issue contract | Backlog -> Ready | `triage_backlog.py` |
| five statuses | lifecycle transition | `update_issue_status.py` |
| exclusive claim | Ready -> In Progress | `claim_issue.py` |
| path budget | writes outside `touches:` | `hooks/enforce_touches.py` |
| default-branch protection | direct push to main/master | `hooks/pre-push` |
| worktree isolation | first implementation edit | `create_branch.py` |
| closure link | PR creation | `create_pr.py` |
| stable reviewer | review authority | `create_pr.py` |
| current-head CI | merge | `check_ci.py`, `merge_pr.py` |
| unresolved findings | merge | `fetch_pr_feedback.py`, `merge_pr.py` |
| current-head external verdict | merge | `merge_pr.py` |
| base/head race | merge | `merge_pr.py --expected-head` |
| issue Done and cleanup | close-out | `merge_pr.py`, `cleanup_worktrees.py` |
| reverse gear | unsafe merged change | `revert_merge.py` |
| anti-regrowth budgets | CI | `tests/test_surface.py` |

Anything not listed here is guidance, not an authorization mechanism.
