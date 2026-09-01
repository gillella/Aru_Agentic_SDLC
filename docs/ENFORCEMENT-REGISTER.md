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
| one current-head review authority plus governed refresh | Tier 2-3 review authority | `create_pr.py` |
| exact-head consumer verification | merge | self-hosted `aru-governed-pr`, `.aru/verify.sh`, `check_ci.py`, `merge_pr.py` |
| zero GitHub-hosted runner minutes | required verification dispatch | workflow/template `[self-hosted, macOS, ARM64, aru-ci]` labels and no fallback |
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
