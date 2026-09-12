# Aru Code Factory

> A small, fail-closed rules-and-guidelines kernel that moves one approved
> GitHub issue to one safely merged pull request.

**Project status: v2.0.0 is the current released version
(released 2026-09-09).** Future kernel improvements should originate in evidence
from real governed consumer projects.

Aru helps a developer or coding agent answer four questions before changing a
software project:

1. **What work is approved?** — the GitHub issue and Project Board.
2. **What may this worker change?** — the exclusive claim and `touches:` paths.
3. **Is this exact revision safe enough to merge?** — the exact-head
   `aru-governed-pr` check, executed on the repository's one assigned runner
   profile, plus one approval of that head from a GitHub account other than
   the author.
4. **What is the governed merge path?** — `scripts/merge_pr.py` with the
   expected head; stronger GitHub-side exclusivity is a consumer deployment
   choice described below.

The Factory combines the Kernel, separately installed agent coordination, and
consumer-owned delivery policy. The implemented Kernel is intentionally a
governance layer, not an autonomous agent platform. It
does not schedule workers, run a private queue, deploy applications, manage
credentials, monitor production, or replace GitHub.

The [minimal kernel contract](docs/KERNEL-CONTRACT.md) is the one normative
summary. It separates the **Kernel** (issue to safe merge), an optional
**external Driver** (when to invoke the next bounded command), and
**consumer policy** (additional risk controls, broader engineering checks,
release, deploy, and production).

Aru is agent-independent. Any agent in any framework, a person or a scheduled
task can act as the optional external Driver by calling the same kernel commands
from outside this repository. The kernel and consumer bootstrap do not install
or run one.

## See the whole system in one minute

```mermaid
flowchart LR
    R[Requirement] --> B[Backlog issue]
    B -->|complete contract| RD[Ready]
    RD -->|exclusive claim| IP[In Progress]
    IP -->|isolated worktree| CODE[Small change + focused tests]
    CODE --> PR[Pull request]
    PR --> VERIFY{Exact-head governed verification green?}
    VERIFY -->|No or failed| FIX[Fix current head, runner, or consumer verify script]
    FIX --> VERIFY
    VERIFY -->|Yes| REV{Approved by another account?}
    REV -->|Findings| FIX
    REV -->|Approved| MERGE[merge_pr.py --expected-head]
    MERGE --> DONE[Done + safe cleanup]
```

The source of truth stays deliberately small:

| Concern | Authority |
| --- | --- |
| Approved work and lifecycle | GitHub issue plus linked Project Board |
| Write boundary | `touches:` declaration |
| Writer ownership | One `agent:<id>` claim |
| Isolation | One Git worktree per issue |
| Verification | `aru-governed-pr` on the exact PR head, using only the repository's one assigned runner profile |
| Review | One approval of the exact head from a GitHub account other than the author |
| Governed direct merge and confirmed-merge recovery | `scripts/merge_pr.py` |
| Deployment and production | The consumer repository and its operators |

## Is it usable for another project?

**Yes, with explicit prerequisites.** The current source contract can govern a new project or
be migrated into an existing project when all of these are true:

- Git, Python 3.11+, and an authenticated GitHub CLI are available.
- The repository has exactly one linked, open GitHub Project with the five
  statuses `Backlog`, `Ready`, `In Progress`, `In Review`, and `Done`.
- New governed issues are added to that Project Board.
- The repository's account is assigned a runner profile and can run the
  installed `aru-governed-pr` workflow and its consumer-owned `.aru/verify.sh`.
  On `self-hosted-mac` that also means at least one online repository-level
  self-hosted macOS arm64 runner carrying the `aru-ci` label; on
  `github-hosted` it means Actions is enabled and the governed workflow is
  active.
- At least one GitHub account other than the authoring accounts can approve
  pull requests: a person, CodeRabbit, or a coding agent on its own account.
- Developers and agents can read the canonical Aru directory through
  `ARU_SDLC_HOME`.

The bootstrap helper creates a minimal repository scaffold. It does **not**
install reviewers or review services, publish an initial default branch, or
merge conflicting files into an existing repository.
Those are deliberate operator-owned setup steps.

> **Important:** v2.0.0 installs only the six skills listed below. It has no
> command router or in-kernel loop; persistent continuation belongs to an
> external Driver.

## Choose an adoption path

```mermaid
flowchart TD
    START[Adopt Aru] --> Q{Does the project already contain code?}
    Q -->|No| NEW[Generate a new minimal scaffold]
    Q -->|Yes| EXISTING[Generate a staging scaffold]
    NEW --> REVIEW[Inspect generated governance files]
    EXISTING --> MERGE[Manually reconcile files on a normal project branch]
    MERGE --> REVIEW
    REVIEW --> GH[Link one GitHub Project and configure reviewers]
    GH --> PILOT[Run one low-risk issue end to end]
    PILOT --> USE[Use Aru for normal development]
```

### 1. Install the canonical agent skills once

From this repository:

```bash
./scripts/install_agent_integration.sh
```

Then set the canonical location in the environment used by your agents:

```bash
export ARU_SDLC_HOME=/Users/aravindgillella/projects/Aru_Agentic_SDLC
```

The installer exposes exactly six skills to supported local coding agents:

- `init-agent-project`
- `create-github-issue`
- `triage-backlog`
- `implement-next-issue`
- `remediate-ci-failure`
- `address-pr-feedback`

It also maintains the delimited Aru block in `~/.codex/AGENTS.md`. Existing
non-Aru instructions are preserved. A recognized legacy all-Aru file is backed
up before replacement so stale skill and command routes do not survive an
upgrade.

### 2. Bootstrap a new local project

Bootstrap a new project as below. For an existing repository, follow the staged
migration in [section 7 of the operations guide](docs/OPERATIONS.md#7-adopt-aru-in-an-existing-project).
Adoption applies only to the named repository; updating this shared Aru source
does not update or enroll other projects.

```bash
python3 "$ARU_SDLC_HOME/scripts/init_project.py" \
  --name my-project \
  --directory /path/to/my-project \
  --owner gillella
```

`--owner` names the GitHub account that will own the repository and selects its
runner profile from the assignments declared in `scripts/policy.toml`:
`gillella` scaffolds `self-hosted-mac`, `Unum-Inc` scaffolds `github-hosted`.
Any other account simply has no assignment, so it passes `--runner-profile`
explicitly. There is still no default: declaring nothing is refused rather than
pointed at another account's machines, and a declaration that contradicts an
existing assignment is refused.

Add `--github --private` when you also want the helper to create a private
GitHub repository, labels, linked Project Board, and a minimal ruleset with no
configured bypass actors that requires `aru-governed-pr` from GitHub Actions.
`--github` requires `--owner`: the repository is created as `OWNER/NAME`, and
provisioning stops if it lands in any other account or the created owner is not
assigned the scaffolded profile. On
`self-hosted-mac` the generated workflow runs only on repository-level
Apple-silicon macOS runners labeled `aru-ci`; register one before admitting
work. The helper writes the governance scaffold but does not commit or push it.

That portable ruleset authenticates the required check producer, but it cannot
make the helper the only possible GitHub merge path or condition server-side
review on Aru's path-derived tier. The helper-only merge rule is therefore a
governed process rule. Organizations that need a tamper-resistant boundary can
add consumer-owned controls supported by their GitHub plan, such as a pinned
required workflow, a dedicated merge App identity, or team/file-pattern review
rules. Those controls are intentionally outside the portable Kernel.

### 3. Migrate an existing project safely

Do not point `init_project.py` directly at an existing repository with files
that may conflict. Generate into a temporary staging directory, inspect the
output, and reconcile it on a normal project branch:

```bash
staging_dir="$(mktemp -d)"
python3 "$ARU_SDLC_HOME/scripts/init_project.py" \
  --name my-project \
  --directory "$staging_dir" \
  --owner Unum-Inc
```

Review the staged `AGENTS.md`, `.github/`, `.aru/`, and `.gitignore` before
applying them. Bootstrap installs the consumer-owned `aru-governed-pr` workflow,
which checks out the exact PR head on the account's assigned runner profile,
runs `.aru/verify.sh`, and validates the linked issue's `touches:` boundary
against the actual diff. On `self-hosted-mac`, register the runner before
requiring the check. Customize the verification commands and branch rules for
the consumer's risk policy. The workflow deliberately has no cross-profile
fallback and does not upload artifacts or use Actions caches by default.

An existing governed consumer updates by restaging with the `--owner` its
repository actually lives under and reconciling changed Aru files on a normal
project branch. Preserve its working verification commands and product rules;
never replace working checks with generated starters. New scaffolds separate
framework checks in `.aru/verify.sh` from product checks in the required executable
`.aru/verify-project.sh`. Replace its failing starter with real product checks.
When adopting this split, preserve existing verification commands in that file
and reconcile the framework verifier deliberately.
Keep the workflow's `# aru-runner-profile:` marker, `runs-on:`, and verification
expectations consistent. Identical files need no replacement. Changing
`ARU_SDLC_HOME` alone does not refresh copied `.aru/`, `.github/`, or guidance
files. Follow the [existing-project procedure](docs/OPERATIONS.md#7-adopt-aru-in-an-existing-project).

### 4. Run one governed unit of work

```bash
python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" --json
python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" \
  --agent codex-local --json
```

`fetch_next_work.py` is read-only and accepts exactly one agent identity. It
first returns that author's oldest open PR as feedback, verification,
conflict, wait, or merge work. Only when there is no authored PR does it return
the highest-priority complete, unblocked Ready issue. It never claims,
promotes Backlog, repairs the board, allocates a batch, or starts another
activation.

If the result is an issue, claim that exact live issue explicitly:

```bash
python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" \
  --issue 42 --agent codex-local
```

The Ready snapshot is fully paginated. Dependencies are read once in a bounded
bulk query, and eligible issues are ordered by priority then issue number.
`needs-human`, consumer epic containers, unresolved dependencies, malformed
contracts, and contradictory priorities are skipped or reported. An idle
result is terminal for that activation; it does not authorize another picker
tick.

Work only in the worktree reported by `create_branch.py`. Local checks are
optional preflight or audit evidence. Publish the branch and open the PR through
`create_pr.py`; merge only after the exact-head `aru-governed-pr` check has run
on the repository's assigned runner profile and another account has approved
that head. On `self-hosted-mac`, if all registered Macs are offline the
check stays queued and merge remains blocked; it is never rerouted to
`github-hosted` runners.

## Review

Every pull request, documentation included, needs one approval of its exact
current head from a GitHub account other than the author: a person, CodeRabbit,
or a coding agent working under its own account. The bootstrap ruleset has
GitHub enforce it (one approval, stale approvals dismissed, approval of the
last push by someone other than its pusher), and `merge_pr.py` rereads it
before submission. A push dismisses earlier approvals; unresolved threads and a
`CHANGES_REQUESTED` decision still block. There are no review tiers, reviewer
labels, provider rankings, capability probes or refresh helpers, and agents that
share one GitHub account cannot approve each other.

The Kernel never waits or polls for review. `fetch_next_work.py` reports a PR
with a green check and no such approval as `review` work, which the author
cannot do; an operator or external Driver arranges a reviewer on another account.

Merge queues and pending auto-merge requests are unsupported in the v2.0.0 release: the helper refuses them before submission. The workflow verifies
same-repository PR heads only. `--finalize` recovers a confirmed direct merge;
historical queue work is refused because a PR-head check does not prove the
combined queue revision. Keep the issue In Review until merge and close-out
are confirmed; only then clean up.

Inspect copied consumer files without running them using the
[consumer compatibility tool](integrations/adoption/README.md). For shipping
software, use the [consumer deployment guide](integrations/deployment/README.md)
and its evidence template. These integrations do not add Kernel lifecycle state.

## The seven-document map

| Read this | When you need it |
| --- | --- |
| [README.md](README.md) | Orientation and fastest adoption path |
| [AGENTS.md](AGENTS.md) | Non-negotiable operating rules |
| [Kernel contract](docs/KERNEL-CONTRACT.md) | Authorities, limits, and admission rule |
| [Enforcement register](docs/ENFORCEMENT-REGISTER.md) | Which rules are mechanically enforced |
| [Operations guide](docs/OPERATIONS.md) | Complete setup, daily workflow, commands, and troubleshooting |
| [Degraded mode](docs/DEGRADED-MODE.md) | What to do when GitHub data is unavailable |
| [Changelog](CHANGELOG.md) | Version history and freeze policy |

## Where to go next

Read the **[complete developer use guide](docs/OPERATIONS.md)**. It covers:

- prerequisites and readiness checks;
- new and existing repository adoption;
- GitHub Project and reviewer setup;
- the issue contract and `touches:` examples;
- the full issue-to-merge procedure;
- every supported command;
- recovery, rollback, and troubleshooting;
- the boundary between Aru and the consumer project.

The v0.2 feature freeze period has concluded, and the repository again accepts
normal feature work. Compatible v2.x maintenance must retain
the public interfaces in the Kernel contract. Breaking contract changes require
a new major version.
