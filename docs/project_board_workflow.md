# GitHub Project Board & Issue Lifecycle Standards

This document establishes the GitHub Project Board multi-view standards, issue state transitions, dependency management, worktree isolation, and parallel multi-agent rules for **Aru_Agentic_SDLC**.

---

## 🚨 Core Law: Issue-First Governance

No developer or AI agent may begin code modifications without first claiming an open issue from the GitHub Project Board. All work MUST originate from a tracked issue in `Backlog` / `Ready`.

---

## 📌 Project Board Views & Columns

When a repository is bootstrapped via `skills/init-agent-project/SKILL.md`, it provisions a GitHub Project v2 with 3 pre-configured views:

1. **Kanban View**: Visual lifecycle columns (`Backlog` $\rightarrow$ `Ready` $\rightarrow$ `In Progress` $\rightarrow$ `In Review` $\rightarrow$ `Done`).
2. **Jira-Style Backlog View**: Tabular list with fields (`Priority`, `Story Points`, `Assignee`, `depends-on`).
3. **Sprint View**: Tabular view with the `Phase` field available for sprint grouping and filtering.

### Lifecycle Column Definitions:

| Column | Description | Trigger / Action |
|---|---|---|
| **Backlog** | Un-triaged issues, feature proposals, and raw bug reports. | Created via GitHub issue templates. |
| **Ready** | Triage complete, acceptance criteria defined, dependencies specified. | Ready for agent/human to claim. |
| **In Progress** | Active development worktree created (`.worktrees/feat-issue-#-desc`). | Claimed via `claim_issue.py`. |
| **In Review** | Pull Request opened (`Closes #X`), CI pipeline green. | PR submitted via `create_pr.py`. |
| **Done** | PR merged into main, issue closed. | A factory agent runs the gated `merge_pr.py` close-out. |

Issues labeled `needs-human` are operator-only work. They remain visible in
Backlog, are never promoted by automated triage, and are excluded from both new
issue pickup and in-flight resume. Agents must not remove the label or claim the
issue to be helpful; only the operator may complete the work or remove the
label before the ordinary Ready lifecycle begins.

## Delivery Increments (user-facing: Sprints)

A Sprint is represented internally by one durable Delivery Increment. It does
not replace issue Status and is not assumed to be a two-week Scrum timebox.
The increment control issue and structured operator-decision comments are the
GitHub authority record; `~/.aru/delivery-increments.json` is the private,
lock-protected local materialization used by the control room.

New boards include `Delivery Increment` and `Increment State` fields for the
control issue. Ordinary stories continue through Backlog → Done unchanged.
Agents may propose a bounded issue set, but only the configured operator can
authorize, revise, start, accept, cancel, authorize deployment, or record
deployment through the project Slack channel. The Slack decision has no local
effect until its structured GitHub comment succeeds.

An authorized scope is frozen. Adding, removing, or replacing an issue needs a
new Slack `revise` decision, whose history preserves the prior exact scope.
One project has at most one active normal increment. Emergency increments are
separate records and never rewrite normal scope. Grooming remains independent,
and the release state is separate from the increment lifecycle: acceptance
does not authorize deployment. A second accepted-but-undeployed increment is
refused unless the operator explicitly records risk acceptance.

When an increment reaches the `accepted` state, `scripts/increment_release.py` tags the exact accepted default-branch commit (`ckpt/<project_id>/<increment_id>`) with structured metadata (increment ID, project identity, committed issue set, acceptance decision URL, timestamp, and concise message) and publishes a formal release record linking evidence, demo artifacts, and deployment state without triggering production deployment.

---

## 🌳 Git Worktree Isolation Guidelines

1. **Clean Workspace Isolation**:
   To prevent dirtying the main working directory during multi-agent or multi-branch development, all feature implementations and PR remediation work MUST be run inside dedicated worktrees under `.worktrees/`.
2. **Worktree Creation**:
   Execute `python3 "$ARU_SDLC_HOME/scripts/create_branch.py" --issue <ID> --worktree --agent <AGENT_ID>` to generate `.worktrees/feat-issue-<ID>-<slug>__<agent>`.
3. **Worktree Cleanup**:
   Keep active PR worktrees attached until governed close-out runs. Normal
   post-merge cleanup belongs to
   `python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID>`, and leftover
   retained/orphaned copies are reclaimed by the supported janitor sweep:
   `python3 "$ARU_SDLC_HOME/scripts/cleanup_worktrees.py" --repo <REPO_ROOT>`.

---

## 📝 Pre-Edit Plan Gate

Issues labeled `type:feat` or `needs-design` require a durable implementation
plan comment before the first file edit. So does any issue whose acceptance
criteria, declared `touches:`, or intended implementation changes money
semantics, PII handling, schemas, migrations, or another irreversible
contract, regardless of its type labels. The comment records the approach,
files, schema/API or money-semantics changes, verification strategy, and
rejected alternatives. It must remain inside the issue so it survives agent
handoffs and context compaction.

The plan is always post-and-proceed once its durable issue comment is visible.
Money, PII, security, schema, migration, irreversible behavior, large diffs,
and repeated review rounds require proportionally stronger planning, tests,
and independent review, but none creates a mandatory human acknowledgement
gate. If the issue lacks a product decision needed to define acceptance, record
the options and block for clarification rather than inventing requirements.

---

## 🔗 Issue Dependencies & Progressive Claiming

1. **Dependency Syntax**:
   Issues specify prerequisite blockers in their body using `depends-on: #X, #Y`.
2. **Progressive Execution**:
   Agents executing `skills/implement-next-issue/SKILL.md` MUST evaluate dependency trees. An issue CANNOT be claimed until all issues listed in `depends-on` are merged.
3. **Issue ID Ordering**:
   When multiple unblocked issues exist, agents pick the lowest numerical Issue ID first to ensure steady progressive movement through the backlog.

---

## ⚡ Parallel Multi-Agent Execution Rules

1. **Parallel Eligibility**:
   Issues marked with `parallel-eligible: true` can be worked on concurrently only when their required `touches:` path declarations do not overlap in-flight work. **In Progress** reserves the issue's declared `touches:` list. **In Review** reserves the open PR's actual files when that list can be read completely, including both sides of a rename; if it cannot (lookup failure, truncated snapshot, malformed files, or a rename without a source path), the declared list is kept (fail closed). A declared path that the parked PR did not change does not block Ready work.
2. **Subagent Worktree Isolation**:
   Each parallel subagent MUST execute in its own isolated worktree (`.worktrees/issue-<ID>-<slug>`).
3. **Merge Conflict Resolution**:
   If parallel branches touch adjacent code paths, subagents must rebase on the latest `main` inside their worktree before opening PRs.

---

## 🏭 Process Ownership and Merge Authority

Aru_Agentic_SDLC owns the lifecycle from issue intake through claim, plan,
worktree, PR, review, merge, and cleanup. Other installed frameworks may help
with a step, but their session-resume files, brainstorming flows, memory, or PR
bots cannot replace board state or start a competing lifecycle.

After the assigned review-pool service supplies authoritative exact-head
evidence and all Definition-of-Done checks pass, any factory agent, including
the implementation author, may execute the mechanical merge only through
`python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID> --expected-head <HEAD_SHA>`
when the picker supplied `head_sha`. Direct pushes and
ad-hoc merge commands have no merge authority. Coding agents never review.
Human intervention is exceptional and applies only when a severe
merge conflict or merge/close-out failure remains unsafe or impossible for
agents to resolve through governed remediation; risk category, diff size, and
review-round count alone never require human participation.

Assigned-service review does not authorize real-money execution, production cutover,
destructive migrations, credential use, or external-account mutations. Those
existing human operational gates remain separate.

### Review-round scope reduction (issue #98)

`merge_pr.py` counts rework rounds and always surfaces them on `--dry-run`
(`review rounds` gate). Crossing the threshold (3) never fails Definition of
Done and never adds a human approval step. Authors run
`python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <N> --emit-review-split` to
post automated split guidance and file follow-up Backlog issues on the
governed Project Board. Each follow-up carries a `depends-on:` edge to the
original linked issue (first `Closes #N` in the PR body) — Aru's picker and
triage resolve depends-on as issue prerequisites, not PR numbers. Emission
is retry-safe via per-issue provenance markers. Triage oversized-scope SPLIT
warnings are the upstream half of the same policy. See
`skills/address-pr-feedback/SKILL.md`.

### Server-side protection of `main`

`hooks/pre-push` refuses a direct push in any clone that installed it. It is
not a gate: `--no-verify`, a missing install, or a fresh clone bypasses it.

Commit `46ca134` (`docs: add agentic software factory guide — deep research
synthesis`) reached `origin/main` with no pull request and no review. It is a
single-parent commit by `gillella@Aravinds-Mac-mini.local`. The hook already
blocks documentation-only pushes; this was a skipped or uninstalled client
hook, not a path-parser gap. No separate hook-fix issue was filed.

`scripts/enable_main_ruleset.py` creates ruleset `aru-protect-main`: pull
requests required, and the CI job name from `.github/workflows/ci.yml` as
the required status check. Default enforcement is `active` (#133) so direct
pushes to `main` are refused by GitHub. The ruleset also requires every review
thread to be resolved and prevents protected-branch deletion and force pushes
(#208). `--disable` and `--delete` are the inverse.
**Required approving review is off** — GitHub will not let the fleet account
approve its own PRs, so that rule would deadlock every fleet-authored PR
until #123. **Required linear history is off** — it would forbid merge
commits and fight #89.

On public repositories or private repositories owned by a GitHub Pro account,
`--apply` exits 0 and activates the `aru-protect-main` ruleset with its default
`active` enforcement. Explicit `--enforcement evaluate` or `--enforcement
disabled` arguments override that default. `--apply` exits 3 if GitHub returns
HTTP 403.

### Closing out a review finding

The merge gate does not accept a resolved thread as evidence that its finding
was addressed. Resolving a thread is a UI toggle with no relationship to the
diff, and treating it as proof let PR #62 merge with five blocking findings
intact. Every finding must be disposed of in one of two ways:

- **Fixed** — push a commit after the thread was raised. The gate checks that
  some commit followed the finding, not that a particular one addressed it;
  ordering is the weaker claim it can actually verify, and it is only ever
  used to refuse.
- **Withdrawn** — reply to the thread starting with `Withdrawn:` and say why.
  A finding can be legitimately retracted or argued down, and without this the
  commit rule would push agents to manufacture no-op commits, producing an
  audit trail that lies.

Two consequences worth knowing before you hit them:

- **Pushing after assigned-service review invalidates it.** The review or check
  attests to the commit it
  was submitted against, so once head moves nobody has reviewed what would
  merge. Re-review the current commit.
- **The audit line records which signal let the PR through** — reviewed at
  head, no unresolved threads, and how many findings were withdrawn rather
  than fixed — so a later reader can reconstruct why.

The assigned review-pool service is the sole code-review authority for a given
PR. CodeRabbit keeps its current exact-head contract. Sourcery requires a
successful head-bound `Sourcery review` check and zero Sourcery unresolved
threads. CodeAnt accepts either of two evidence shapes bound unambiguously to
the exact current head, plus zero CodeAnt unresolved threads: an authoritative
exact-head `codeant-ai` review object, or - when CodeAnt found nothing to
flag and therefore created no review object - a trusted, provider-owned
`codeant-review-status` marker on CodeAnt's own rolling status comment,
recording a completed (`done: true`) run for the exact head (#394). The
status-marker path only ever proves the run finished; it grants nothing about
findings or verdict, so it never overrides the aggregate unresolved-thread
count or a human's `CHANGES_REQUESTED`. Identity, completed status,
current-head binding, and assigned-service thread state are read from
authoritative GitHub data. Missing, pending, failed, skipped, stale, ambiguous,
duplicated, or spoofed evidence blocks - including a status marker that is
malformed, unfinished, bound to a different commit, or posted from more than
one trusted comment. Legacy `reviewer:` / `reviewed-by:` coding-agent state is
non-authoritative and must not be re-dispatched.

---

## 📊 Factory Fleet Status & Completion Evaluation

The factory supervisor reads `scripts/fleet_status.py` for authoritative, deterministic evaluation of factory completion:

```
python3 "$ARU_SDLC_HOME/scripts/fleet_status.py" [--json]
```

### Factory States & Exit Codes:

| State | Exit Code | Description |
|---|---|---|
| **`complete`** | `0` | Every governed issue is Done/closed, zero open PRs, zero active claims (`agent:*`, `reviewer:*`), no board drift, no orphan worktrees. |
| **`waiting`** | `2` | Active work in flight, Ready/In Progress/In Review/Backlog issues, pending CI, pending reviews, or worktree cleanup. |
| **`blocked`** | `3` | Issues/PRs carrying operator-only labels (`needs-human`), `needs-design` awaiting a product decision, or an unresolvable project board identity. High review-round count is **not** a blocked/human state — it triggers automated scope-reduction guidance (`merge_pr.py --emit-review-split`) while merge authority stays with `merge_pr.py`. |
| **`error`** | `1` | GitHub API/auth failures; fails closed. |
