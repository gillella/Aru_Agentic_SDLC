# AGENTS.md - Master AI Engineering Playbook & Operating Directives

Welcome to **Aru_Agentic_SDLC**. This repository serves as a vendor-neutral, tool-agnostic template and operating playbook for AI coding agents (e.g., Gemini, Claude, Codex, Antigravity, AutoGen, CrewAI, etc.).

All AI agents operating within this repository MUST follow the directives, skills, and tools defined herein.

---

## 🚨 Core Governance Directive: The Issue-First Law

**No code change, refactor, or feature implementation may begin without first originating from a tracked issue on the GitHub Project Board.**

```
[ New Requirement / Bug ] ──► [ Issue Filed & Triaged ] ──► [ Claimed via Skill ] ──► [ Worktree Code ] ──► [ PR Closes #X & CI ] ──► [ Merged ]
```

---

## 🎯 Primary Directives for AI Agents

### 1. Execute via SkillsMP Skills
When assigned to work on the repository, perform all tasks by following the declarative procedures defined in the `skills/` directory:
- **Initialization Skill**: [`skills/init-agent-project/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/init-agent-project/SKILL.md)
  - Bootstraps new repos, AGENTS.md governance, CI workflows, private GitHub repos, and multi-view project boards.
- **Primary Execution Skill**: [`skills/implement-next-issue/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/implement-next-issue/SKILL.md)
  - Inspects past session state to determine where execution left off.
  - Selects the next unblocked issue in progressive order based on dependencies.
  - Uses `git worktree` isolation for feature work to keep the main working tree clean.
  - Spawns parallel subagents for independent issues when supported.
  - Executes full lifecycle: claim -> worktree -> implement -> test -> commit -> push -> PR -> CI check -> remediation -> review.
- **Code Review Skill**: [`skills/code-review/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/code-review/SKILL.md) (with worktree isolation)
- **CI Failure Remediation Skill**: [`skills/remediate-ci-failure/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/remediate-ci-failure/SKILL.md)
- **PR Review Feedback Skill**: [`skills/address-pr-feedback/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/address-pr-feedback/SKILL.md)
- **Issue Creation Skill**: [`skills/create-github-issue/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/create-github-issue/SKILL.md)

### 2. Offload Concrete Actions to Helper Scripts
The skills in `skills/` are 100% declarative and tool-agnostic. Concrete Git, Worktree, and GitHub API interactions MUST be executed using the Python automation scripts in `scripts/`:
* `python3 scripts/init_project.py --name <NAME> [--private] [--create-board]` - Bootstraps project, CI, private repo & multi-view project board.
* `python3 scripts/fetch_next_issue.py` - Identifies next actionable issue.
* `python3 scripts/claim_issue.py --issue <ID> --agent <AGENT_ID>` - Claims issue & updates status.
* `python3 scripts/create_branch.py --issue <ID> --type <feat|fix|docs> [--worktree]` - Creates standardized git branch or worktree.
* `python3 scripts/create_pr.py --issue <ID> --title "<Title>" --body "<body>"` - Opens PR pre-populated with `Closes #<ID>`.
* `python3 scripts/check_ci.py --pr <ID>` - Polls and returns CI run status.
* `python3 scripts/fetch_pr_feedback.py --pr <ID>` - Fetches inline reviewer comments as a Markdown checklist.
* `python3 scripts/update_issue_status.py --issue <ID> --status "<Status>"` - Updates project board status.

---

## 🛡️ Repository Rules & Guardrails

1. **Strict Issue-First Execution**: Never code without a claimed open issue on the project board.
2. **No Direct Pushes to Default Branches**: Never push directly to `main` or `master`. All changes MUST go through feature branches/worktrees and Pull Requests.
3. **Mandatory Worktree Isolation**: Use `git worktree` for feature development and PR code reviews to prevent dirtying the main working directory.
4. **Mandatory Issue Closure Linking**: Every Pull Request MUST explicitly include `Closes #<issue_number>` in its description body.
5. **Local Test Verification First**: Never commit or push code without running local build and test suites to verify zero regressions.
6. **CI Green Gate**: A PR cannot be merged until all automated CI pipeline checks pass. If CI fails, invoke `remediate-ci-failure`.
7. **Session State Memory**: Upon starting a session, always inspect recent git log, active branches, open PRs, and board status before claiming new work.

---

## 🔄 Issue & Project Board Lifecycle

```
[ Backlog ] ──► [ Ready ] ──► [ In Progress ] ──► [ In Review ] ──► [ Done ]
```

For detailed guidelines, see [`docs/project_board_workflow.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/docs/project_board_workflow.md) and [`docs/coding_standards.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/docs/coding_standards.md).
