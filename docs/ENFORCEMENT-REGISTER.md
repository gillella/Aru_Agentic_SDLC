# Enforcement Register

| Control | Blocks or authorizes | Mechanism |
| --- | --- | --- |
| issue contract | Backlog -> Ready | `triage_backlog.py` |
| Ready contract pin | merge after criteria or `touches:` changed since promotion | `triage_backlog.py` stamps `ready:<digest>`; `merge_state.issue_gate` recomputes it |
| five statuses | invariant-bearing lifecycle transitions | `triage_backlog.py`, `claim_issue.py`, `create_pr.py`, `merge_pr.py` |
| exclusive claim | Ready -> In Progress | `claim_issue.py` |
| path budget | writes outside `touches:` | `hooks/enforce_touches.py`, `aru-governed-pr` actual-diff check |
| path budget, unrewritable | writes outside `touches:` when the pull request edits its own workflow | `aru-merge-policy` runs the base branch copy of itself on `pull_request_target`, never reads the head, and posts its verdict as a required status on the head |
| default-branch protection | direct push to the resolved remote default branch | `hooks/pre-push` |
| worktree isolation | first implementation edit | `create_branch.py` |
| closure link | PR creation | `create_pr.py` |
| exact-head consumer verification | merge | `aru-governed-pr`, `.aru/verify.sh`, `check_ci.py`, `merge_pr.py` |
| one account-assigned runner profile | required verification dispatch | `init_project.py` account policy, workflow `# aru-runner-profile:` marker, `.aru/verify.sh` marker/`runs-on:` agreement |
| no cross-profile runner fallback | required verification dispatch | `.aru/verify.sh` per-profile forbidden patterns: `self-hosted-mac` rejects hosted images, `github-hosted` rejects `self-hosted` |
| current board and dependencies | merge submission and direct-merge finalization | `merge_state.py` fresh Project/card/status-field identity, label agreement and closed dependencies |
| configured product verification | consumer check success | `.aru/verify.sh` requires executable `.aru/verify-project.sh`; generated starter fails |
| unresolved findings | merge | `fetch_pr_feedback.py`, `merge_pr.py` |
| approval by an authorized reviewer | merge, under the `human` posture | `.aru/review.json` on the default branch names the accounts that may approve; `review_authority.py` refuses an unlisted account and refuses any GitHub App outright, and both `merge_pr.py` and `aru-merge-policy` apply the same refusal |
| posture resolution | merge, when the declaration is unusable | absent, malformed or unknown resolves to `human`, so a repository cannot lose the gate by omission; a repository that declared nothing authorizes its owner rather than nobody |
| approval by another account | merge | ruleset: one approval, stale approvals dismissed, last-push approval; `merge_pr.py` `approved_at_head` rereads the reviews before submission |
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
Until the approval rule is added to that ruleset, only `merge_pr.py` enforces
the approval.
Without the merge-authority App, nothing server-side makes `merge_pr.py` the
only merge path. With it, only a holder of that App's key can satisfy the
pinned check, and a repository administrator can still edit the ruleset; that
residual boundary remains consumer-owned.
