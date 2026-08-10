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
| **Done** | PR merged into main, issue closed. | PR merged by reviewer or automated merge bot. |

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
plan comment before the first file edit. The comment records the approach,
files, schema/API or money-semantics changes, verification strategy, and
rejected alternatives. It must remain inside the issue so it survives agent
handoffs and context compaction.

The normal mode is post-and-proceed. For money, PII, schema, migrations, other
irreversible paths, or an explicit operator escalation, invoke the workflow as
`implement-next-issue --require-plan-ack`. In that mode the claim remains held,
but implementation pauses until a repository owner or designated maintainer
comments `Plan approved` after the latest plan.

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

After an independent review and all Definition-of-Done checks pass, a factory
agent may execute a routine merge only through
`python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID>`. Direct pushes and
ad-hoc merge commands have no merge authority. Human acknowledgement is
required only for the high-risk and irreversible categories identified by the
plan or merge gate, plus explicit escalations.
