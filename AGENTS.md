# AGENTS.md - Master AI Engineering Playbook & Operating Directives

Welcome to **Aru_Agentic_SDLC**. This repository serves as a vendor-neutral, tool-agnostic template and operating playbook for AI coding agents (e.g., Gemini, Claude, Codex, Antigravity, AutoGen, CrewAI, etc.).

All AI agents operating within this repository MUST follow the directives, skills, and tools defined herein.

---

## 🎯 Primary Directives for AI Agents

### 1. Execute via SkillsMP Skills
When assigned to work on the repository, perform all tasks by following the declarative procedures defined in the `skills/` directory:
- **Primary Skill**: [`skills/implement-next-issue/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/implement-next-issue/SKILL.md)
  - Inspects past session state to determine where execution left off.
  - Selects the next unblocked issue in progressive order based on dependencies.
  - Spawns parallel subagents for independent issues when supported.
  - Executes the full issue lifecycle: claim -> branch -> implement -> test -> commit -> push -> PR -> CI check -> remediation -> review.
- **Code Review Skill**: [`skills/code-review/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/code-review/SKILL.md)
- **Issue Creation Skill**: [`skills/create-github-issue/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/create-github-issue/SKILL.md)

### 2. Offload Concrete Actions to Helper Scripts
The skills in `skills/` are 100% declarative and tool-agnostic. Concrete Git and GitHub API interactions MUST be executed using the Python automation scripts in `scripts/`:
* `python3 scripts/fetch_next_issue.py` - Identifies next actionable issue.
* `python3 scripts/claim_issue.py --issue <ID>` - Claims issue & updates status.
* `python3 scripts/create_branch.py --issue <ID> --type <feat|fix|docs>` - Creates standardized git branch.
* `python3 scripts/create_pr.py --issue <ID> --title "<Title>" --body "<body>"` - Opens PR pre-populated with `Closes #<ID>`.
* `python3 scripts/check_ci.py --pr <ID>` - Polls and returns CI run status.
* `python3 scripts/update_issue_status.py --issue <ID> --status "<Status>"` - Updates issue project board status.

---

## 🛡️ Repository Rules & Guardrails

1. **No Direct Pushes to Default Branches**: Never push directly to `main` or `master`. All changes MUST go through feature branches and Pull Requests.
2. **Mandatory Issue Closure Linking**: Every Pull Request MUST explicitly include `Closes #<issue_number>` in its description body.
3. **Local Test Verification First**: Never commit or push code without running local build and test suites to verify zero regressions.
4. **CI Green Gate**: A PR cannot be merged until all automated CI pipeline checks pass. If CI fails, the agent MUST fetch logs, fix the failure, and push updates before requesting review.
5. **Session State Memory**: Upon starting a session, always inspect recent git log, active branches, open PRs, and board status before claiming new work.

---

## 🔄 Issue & Project Board Lifecycle

```
[ Backlog ] ──► [ Ready ] ──► [ In Progress ] ──► [ In Review ] ──► [ Done ]
```

* **Backlog**: Triage pool for raw issues.
* **Ready**: Unblocked issues ready to be claimed.
* **In Progress**: Active work assigned to an agent or branch.
* **In Review**: Pull Request submitted, CI green, awaiting peer review.
* **Done**: PR merged and issue closed.

For detailed guidelines, see [`docs/project_board_workflow.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/docs/project_board_workflow.md) and [`docs/coding_standards.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/docs/coding_standards.md).
