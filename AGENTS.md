# Aru Minimal Kernel Rules

The canonical contract is [docs/KERNEL-CONTRACT.md](docs/KERNEL-CONTRACT.md).
This file is its short operating summary for Aru itself and governed consumer
repositories. Do not infer additional gates from older audits, vision files, or
agent-global instructions.

## Non-negotiable kernel path

1. Use the GitHub issue and linked Project Board as lifecycle authority.
2. Require the Ready contract: unchecked acceptance criteria, one safe
   `touches:` declaration, and no unresolved dependency.
3. Claim before editing and work only in the issue's isolated worktree.
4. Keep the change inside `touches:` and run useful local preflight as needed.
5. Open the PR through `create_pr.py` with `Closes #N`. Require the exact-head
   `aru-governed-pr` check: GitHub Actions orchestrates it on the one runner
   profile the repository's account is assigned, where it runs the consumer's
   `.aru/verify.sh` and validates `touches:` against the actual diff. Never
   fall back to another profile's runners.
6. Resolve every finding and thread. Tier 0 documentation and Tier 1 ordinary
   code do not wait for authoritative review; Tier 2 sensitive/contract and
   Tier 3 production/destructive changes require exactly one current-head
   authoritative reviewer distinct from the author.
7. Submit only through `merge_pr.py --expected-head`. A merge-queue submission
   is still in flight; verify GitHub actually merged before Done and cleanup.

Any missing, stale, partial, contradictory, or unreadable authority blocks the
transition. Never push directly to `main` or `master`, review your own PR,
hand-edit review authority, create a second lifecycle store, or guess during a
GitHub outage.

## Consumer runner profiles

A governed repository verifies on exactly one profile, selected by its account:

| Profile | `runs-on` | Account |
| --- | --- | --- |
| `self-hosted-mac` | `[self-hosted, macOS, ARM64, aru-ci]` | `gillella` personal repositories, including Aru itself |
| `github-hosted` | `ubuntu-latest` | `Unum-Inc` repositories |

There is no default: an account outside that table has no profile and stays
blocked. The scaffolded workflow declares its profile in an
`# aru-runner-profile:` marker, and `.aru/verify.sh` refuses a workflow whose
marker and `runs-on:` disagree, an unknown marker, or a `github-hosted`
workflow that mentions `self-hosted`. A `self-hosted-mac` outage never falls
back to hosted runners, and a `github-hosted` repository never reaches a
personal Mac. The profile chooses compute only; the check name, exact-head
checkout, read-only permissions, event provenance and actual-diff `touches:`
enforcement are identical for both. Verification is not deployment.

## Workflow routing

Use only the six versioned skills installed by this repository:

- `init-agent-project`
- `create-github-issue`
- `triage-backlog`
- `implement-next-issue`
- `remediate-ci-failure`
- `address-pr-feedback`

Use `fetch_next_work.py`, not an unversioned picker name. Free-form trigger
words do not create a persistent loop or route to an unlisted skill.

## Layer boundary

- **Kernel:** the issue-to-safe-merge rules and helpers in this repository.
- **External Driver:** optional human, Hermes, or scheduled/event-driven
  continuation. For a Tier 2-3 review it rereads authority and owns the one
  pending-review event defined by the canonical contract; it owns no lifecycle
  state.
- **Consumer policy:** product acceptance, additional risk controls, wider
  engineering or release checks, human/domain approvals, deployment,
  production, and SRE.

Do not turn consumer policy into universal kernel ceremony. Do not put a
scheduler, daemon, queue, handoff system, dashboard, release/deploy system,
provider fleet, or repository-owned runtime in this project.

## Review policy

For Tier 2-3, CodeRabbit is the sole preferred external provider. Sourcery and
CodeAnt are retired: registration and historical evidence never make them
eligible for new assignments. Use one bounded authenticated check for usable
CodeRabbit access to the current repository/head. If access is denied, errored,
rate-limited, unavailable or unproven, immediately select an available distinct
coding reviewer; never wait through retired providers. Generic green checks,
cached installation inventory and empty/skipped reviews are not approval.
The optional `review-policy:timeout=<seconds>` is a completion deadline only
for an accepted review (default 900 seconds, informed by the observed 11-minute
CodeRabbit review). Explicit unavailability bypasses it. Ranked declarations
remain invalid. Use `create_pr.py --refresh-reviewer <PR>` to migrate a retired
assignment; do not hand-edit authority or erase prior findings/history.

The v0.2 feature freeze lasts through 2026-09-26. During it, accept only
security and correctness fixes.
