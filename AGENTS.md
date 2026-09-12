# Aru Minimal Kernel Rules

The canonical contract is [docs/KERNEL-CONTRACT.md](docs/KERNEL-CONTRACT.md).
This file is its short operating summary for Aru itself and governed consumer
repositories. Do not infer additional gates from older audits, vision files, or
agent-global instructions.

## Non-negotiable kernel path

<!-- BEGIN GENERATED: kernel-path -->
1. Use the GitHub issue and linked Project Board as lifecycle authority.
2. Require the Ready contract: unchecked acceptance criteria, one safe
   `touches:` declaration, and no unresolved dependency. Scope is not frozen at
   promotion: the merge gate reads the declaration that is live at merge time,
   so a claimant who needs an omitted path corrects the declaration and the
   gate judges the corrected one.
3. Claim before editing and work only in the issue's isolated worktree.
4. Keep the change inside `touches:` and run useful local preflight as needed.
5. Open the PR through `create_pr.py` with `Closes #N`. Require the exact-head
   `aru-governed-pr` check: GitHub Actions orchestrates it on the one runner
   profile the repository's account is assigned, where it runs the consumer's
   `.aru/verify.sh` and validates `touches:` against the actual diff. Never
   fall back to another profile's runners.
6. Resolve every finding and thread, and obtain one approval of the exact head
   from a GitHub account other than the PR author. Every PR needs it,
   documentation included; a push dismisses earlier approvals.
7. Submit only through `merge_pr.py --expected-head`. Configured merge queues
   and pending queue/auto-merge requests are unsupported and refused. Verify
   GitHub actually merged before Done and cleanup.
<!-- END GENERATED: kernel-path -->

Any missing, stale, partial, contradictory, or unreadable authority blocks the
transition. Never push directly to `main` or `master`, approve your own PR,
create a second lifecycle store, or guess during a GitHub outage.

## Consumer runner profiles

A governed repository verifies on exactly one profile, selected by its account:

| Profile | `runs-on` | Account |
| --- | --- | --- |
| `self-hosted-mac` | `[self-hosted, macOS, ARM64, aru-ci]` | `gillella` personal repositories, including Aru itself |
| `github-hosted` | `ubuntu-latest` | `Unum-Inc` repositories |

There is no default and bootstrap never guesses. An account in that table is
pinned to its assigned profile and cannot be scaffolded onto another one. An
account outside it has no assigned profile and must name one explicitly with
`--runner-profile`; without that flag the bootstrap refuses. The scaffolded
workflow declares its profile in an
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
- **External Driver:** optional human, any agent, or scheduled/event-driven
  continuation. It may surface PRs waiting for an approval or start a reviewer
  under another account; it owns no lifecycle state.
- **Consumer policy:** product acceptance, additional risk controls, wider
  engineering or release checks, human/domain approvals, deployment,
  production, and SRE.

Do not turn consumer policy into universal kernel ceremony. Do not put a
scheduler, daemon, queue, handoff system, dashboard, release/deploy system,
provider fleet, or repository-owned runtime in this project.

## Review policy

Every PR needs one approval of its current head from a GitHub account other
than the author: a person, CodeRabbit, or a coding agent on a different account.
There are no review tiers, provider rankings, reviewer labels, probes or refresh
helpers, and agents sharing one GitHub account cannot approve each other.

The v0.2 feature freeze has concluded. Normal feature work is accepted;
compatible v2.x changes retain the canonical safety and public API contracts.
