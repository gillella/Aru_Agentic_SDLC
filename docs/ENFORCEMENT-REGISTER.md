# Enforcement Register

| Control | Blocks or authorizes | Mechanism |
| --- | --- | --- |
| issue contract | Backlog -> Ready | `triage_backlog.py` |
| five statuses | invariant-bearing lifecycle transitions | `triage_backlog.py`, `claim_issue.py`, `create_pr.py`, `merge_pr.py` |
| exclusive claim | Ready -> In Progress | `claim_issue.py` |
| path budget | writes outside `touches:` | `hooks/enforce_touches.py`, `aru-governed-pr` actual-diff check |
| path budget, unrewritable | writes outside `touches:` when the pull request edits its own workflow | `aru-merge-policy` runs the base branch copy of itself on `pull_request_target`, never reads the head, and posts its verdict as a required status on the head |
| managed file integrity | merging a head whose Factory-managed files or managed `AGENTS.md` block differ from `.aru/manifest.json` | `aru-merge-policy` runs the Factory's `hooks/check_manifest.py` from the base branch, comparing head bytes read through the contents API to the base branch's manifest |
| workflow stubs call this Factory | merging a head whose workflow stub calls another repository, another action, another runner, or grants itself write permissions | `scripts/verify_consumer.sh` checks the head's stubs; `aru-merge-policy` runs the Factory's `hooks/check_stubs.py` from the base branch, which allows only the pinned tag to differ |
| default-branch protection | direct push to the resolved remote default branch | `hooks/pre-push` |
| worktree isolation | first implementation edit | `create_branch.py` |
| closure link | PR creation | `create_pr.py` |
| exact-head consumer verification | merge | `aru-governed-pr` calling the Factory's `governed-pr` action, which runs `scripts/verify_consumer.sh` against the exact head; `check_ci.py`, `merge_pr.py` |
| one account-assigned runner profile | required verification dispatch | `init_project.py` account policy, stub `# aru-runner-profile:` marker, `scripts/verify_consumer.sh` marker/`runs-on:` agreement, and the profile the stub passes to the action |
| no cross-profile runner fallback | required verification dispatch | `scripts/verify_consumer.sh` per-profile forbidden patterns: `self-hosted-mac` rejects hosted images, `github-hosted` rejects `self-hosted` |
| current board and dependencies | merge submission and direct-merge finalization | `merge_state.py` fresh Project/card/status-field identity, label agreement and closed dependencies |
| configured product verification | consumer check success | `scripts/verify_consumer.sh` requires executable `.aru/verify-project.sh`; generated starter fails |
| unresolved findings | author feedback routing, merge, final revalidation and close-out | `fetch_pr_feedback.py` reads submitted P0/P1-labelled summaries and unresolved threads, including outdated ones; `fetch_next_work.py` routes feedback to the author; `merge_pr.py` refuses until resolved. Summary clearance needs explicit original-reviewer evidence on the current head; approval alone is insufficient. |
| approval by an authorized reviewer | merge, under the `human` posture | `.aru/review.json` on the default branch names the accounts that may approve; `review_authority.py` refuses an unlisted account and refuses any GitHub App outright, and requires the approval to carry a written body. Applied by `merge_pr.py`, the only path that submits a merge; `aru-merge-policy` does not apply it, because the trigger that would re-evaluate after an approval runs the pull request's own copy of the workflow |
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
`aru-governed-pr` and `aru-merge-policy` contexts produced by the
authenticated GitHub Actions App, and one approving review with stale
approvals dismissed. GitHub enforces the approval rule; `merge_pr.py` rereads
the same evidence before submission.
The merge-authority App gate is enabled here: `aru-merge-authorized` is
required, pinned by `integration_id` to the merge-authority App, so only a
holder of that App's key can satisfy it and `merge_pr.py` is the only path that
completes a merge. A consumer scaffolded without `--merge-app-id` has no App to
pin and therefore does not get this check; there, any account with write access
can merge once the other contexts pass.
Approval of the most recent push by an account other than its pusher is not
required. The App pushes on the factory's behalf and a person approves, so that
rule demanded a second approver on every pull request and nothing could merge;
it is declared false rather than silently removed. Stale-approval dismissal
stays on, so a new push still voids every approval.
A repository administrator can still edit the ruleset; that residual boundary
remains consumer-owned.

A consumer carries no verification logic. Its two workflows are stubs that call this
repository's composite actions at a pinned release, so the script that judges a pull
request is never a file that pull request can rewrite. Managed-file integrity and
stub origin are checked from the base branch, which the head cannot change. An upgrade
head legitimately moves the pinned tag and is judged against its own manifest; an
administrator can still edit the stubs together with the manifest, or edit the ruleset,
and a fork that repoints `FACTORY_REPOSITORY` changes whose checks run.
