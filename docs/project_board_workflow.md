# GitHub Project Board & Issue Lifecycle Standards

This document establishes the GitHub Project Board columns, issue state transitions, dependency management, worktree isolation, and parallel multi-agent rules for **Aru_Agentic_SDLC**.

---

## 📌 Project Board Columns

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
   Execute `python3 scripts/create_branch.py --issue <ID> --worktree` to generate `.worktrees/feat-issue-<ID>-<slug>`.
3. **Worktree Cleanup**:
   Upon PR merge or review completion, remove temporary worktree directories with `git worktree remove .worktrees/<dir>`.

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
   Issues marked with `parallel-eligible: true` or possessing zero shared file dependencies can be worked on concurrently by separate subagents.
2. **Subagent Worktree Isolation**:
   Each parallel subagent MUST execute in its own isolated worktree (`.worktrees/issue-<ID>-<slug>`).
3. **Merge Conflict Resolution**:
   If parallel branches touch adjacent code paths, subagents must rebase on the latest `main` inside their worktree before opening PRs.
