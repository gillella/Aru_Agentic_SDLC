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

---

## 🌳 Git Worktree Isolation Guidelines

1. **Clean Workspace Isolation**:
   To prevent dirtying the main working directory during multi-agent or multi-branch development, all feature implementations and PR reviews MUST be run inside dedicated worktrees under `.worktrees/`.
2. **Worktree Creation**:
   Execute `python3 "$ARU_SDLC_HOME/scripts/create_branch.py" --issue <ID> --worktree` to generate `.worktrees/feat-issue-<ID>-<slug>`.
3. **Worktree Cleanup**:
   Upon PR merge or review completion, remove temporary worktree directories with `git worktree remove .worktrees/<dir>`.

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
   Issues marked with `parallel-eligible: true` can be worked on concurrently only when their required `touches:` path declarations do not overlap in-flight work.
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

After a distinct agent completes an independent review and all
Definition-of-Done checks pass, any factory agent, including the implementation
author, may execute the mechanical merge only through
`python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID>`. Direct pushes and
ad-hoc merge commands have no merge authority, and authors may never
self-review.

### Server-side protection of `main`

`hooks/pre-push` refuses a direct push in any clone that installed it. It is
not a gate: `--no-verify`, a missing install, or a fresh clone bypasses it.

Commit `46ca134` (`docs: add agentic software factory guide — deep research
synthesis`) reached `origin/main` with no pull request and no review. It is a
single-parent commit by `gillella@Aravinds-Mac-mini.local`. The hook already
blocks documentation-only pushes; this was a skipped or uninstalled client
hook, not a path-parser gap. No separate hook-fix issue was filed.

`scripts/enable_main_ruleset.py` creates ruleset `aru-protect-main`: pull
requests required, status check `Lint, Verify & Test` required, force-push and
deletion blocked. **Required approving review is off** — GitHub will not let
the fleet account approve its own PRs, so that rule would deadlock every
fleet-authored PR until #123. **Required linear history is off** — it would
forbid merge commits and fight #89.

On this private repository the Rulesets API currently returns HTTP 403
(GitHub Pro or public visibility required). `--apply` exits 3 in that case.
Live enable and the scratch-clone push test are #133. Human intervention is exceptional and applies only when a severe
merge conflict or merge/close-out failure remains unsafe or impossible for
agents to resolve through governed remediation; risk category, diff size, and
review-round count alone never require human participation.

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

- **Pushing after a review invalidates it.** A review attests to the commit it
  was submitted against, so once head moves nobody has reviewed what would
  merge. Re-review the current commit.
- **The audit line records which signal let the PR through** — reviewed at
  head, no unresolved threads, and how many findings were withdrawn rather
  than fixed — so a later reader can reconstruct why.

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
| **`blocked`** | `3` | Issues/PRs carrying human escalation labels, `needs-design`, unresolvable project board identity, or exhausted review/remediation rounds. |
| **`error`** | `1` | GitHub API/auth failures; fails closed. |


