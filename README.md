# Aru_Agentic_SDLC - AI Engineering Playbook & Template

A vendor-neutral, GitHub-centric **Agentic Software Development Life Cycle (SDLC)** template and AI Engineering Playbook. Standardized for compliance with **SkillsMP (Agent Skills Marketplace)** specs.

---

## 🌟 Overview

`Aru_Agentic_SDLC` establishes standardized rules, declarative skill procedures (`SKILL.md`), git worktree isolation, and automation scripts for AI coding agents (Gemini, Claude, Codex, Antigravity, AutoGen, CrewAI, etc.). It enables autonomous or semi-autonomous development teams to maintain high quality, strict git hygiene, and clear issue traceability.

---

## 📂 Repository Structure

```
Aru_Agentic_SDLC/
├── AGENTS.md                        # Master playbook & operating directives for AI agents
├── README.md                        # Project documentation & guidelines
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md     # Standard PR template with issue linking
│   └── ISSUE_TEMPLATE/              # Issue templates (feature, bug, task)
├── skills/                          # SkillsMP-compliant markdown skills
│   ├── implement-next-issue/
│   │   └── SKILL.md                 # Primary workflow: Session recovery -> Next Issue -> Worktree -> Implementation -> PR -> CI
│   ├── code-review/
│   │   └── SKILL.md                 # Code review & security audit procedure (with Worktree isolation)
│   ├── create-github-issue/
│   │   └── SKILL.md                 # Structured issue creation workflow
│   ├── remediate-ci-failure/
│   │   └── SKILL.md                 # Structured CI log parsing & targeted failure remediation loop
│   └── address-pr-feedback/
│       └── SKILL.md                 # Fetch inline PR review comments, build checklist, resolve & reply
├── scripts/                         # Reusable GitHub helper automation tools
│   ├── common.py                    # Shared Git / Worktree / GitHub API / gh CLI helpers
│   ├── fetch_next_issue.py          # Identifies next unblocked issue respecting dependencies
│   ├── fetch_pr_feedback.py         # Fetches inline PR comments into actionable task list
│   ├── claim_issue.py               # Assigns issue & updates board status
│   ├── create_branch.py             # Creates standardized feature branch or worktree
│   ├── create_pr.py                 # Opens PR linking 'Closes #X'
│   ├── check_ci.py                  # Polls and verifies CI build status
│   └── update_issue_status.py       # Manages board status transitions
└── docs/
    ├── project_board_workflow.md    # Issue lifecycle, dependencies, worktrees & parallel multi-agent rules
    └── coding_standards.md          # Commit hygiene & testing standards
```

---

## 🚀 Quick Start for AI Agents

1. **Read `AGENTS.md`**: Understand repository guardrails, worktree isolation rules, and operating directives.
2. **Execute Primary Skill**: Follow [`skills/implement-next-issue/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/implement-next-issue/SKILL.md) to inspect session state, pick the next actionable issue, create a clean worktree, and execute the implementation lifecycle.
3. **Use Helper Tools**: Execute GitHub operations via `python3 scripts/<script_name>.py`.

---

## 🛡️ Key Features

- **Tool-Agnostic Skills**: Skills contain pure declarative markdown instructions with zero hardcoded agent CLI syntax.
- **Git Worktree Isolation**: All feature development and code reviews run in clean worktree directories (`.worktrees/`).
- **Session Continuity**: Agents automatically detect recent commits and active PRs to resume work seamlessly across sessions.
- **Progressive & Parallel Order**: Issues with dependencies are executed sequentially; independent issues are claimed in parallel by spawning subagents.
- **Automated CI & PR Remediation**: Integrated skills for log-based CI failure remediation (`remediate-ci-failure`) and PR reviewer comment resolution (`address-pr-feedback`).

---

## 📄 License
MIT License. Free to use as a template for any AI-assisted coding repository.
