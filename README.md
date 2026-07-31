# Aru_Agentic_SDLC - AI Engineering Playbook & Template

A vendor-neutral, GitHub-centric **Agentic Software Development Life Cycle (SDLC)** template and AI Engineering Playbook. Standardized for compliance with **SkillsMP (Agent Skills Marketplace)** specs and enforcing the **Issue-First Governance Law**.

---

## 🚨 Core Governance: The Issue-First Law

> **No code change, refactor, or feature implementation may begin without first originating from a tracked issue on the GitHub Project Board.**

---

## 🌟 Overview

`Aru_Agentic_SDLC` establishes standardized rules, declarative skill procedures (`SKILL.md`), git worktree isolation, automated CI/CD pipelines, and project bootstrap tools for AI coding agents (Gemini, Claude, Codex, Antigravity, AutoGen, CrewAI, etc.).

---

## 📂 Repository Structure

```
Aru_Agentic_SDLC/
├── AGENTS.md                        # Master playbook & operating directives for AI agents
├── README.md                        # Project documentation & guidelines
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md     # Standard PR template with mandatory issue linking
│   ├── ISSUE_TEMPLATE/              # Issue templates (feature, bug, task)
│   └── workflows/
│       └── ci.yml                   # Built-in GitHub Actions CI pipeline template
├── skills/                          # SkillsMP-compliant markdown skills
│   ├── init-agent-project/
│   │   └── SKILL.md                 # NEW: Bootstrap repo, AGENTS.md, CI workflow & Multi-view Project Board
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
│   ├── init_project.py              # NEW: Bootstraps AGENTS.md, .gitignore, CI workflow, private repo & multi-view project board
│   ├── fetch_next_issue.py          # Identifies next unblocked issue respecting dependencies
│   ├── fetch_pr_feedback.py         # Fetches inline PR comments into actionable task list
│   ├── claim_issue.py               # Assigns issue & updates board status
│   ├── create_branch.py             # Creates standardized feature branch or worktree
│   ├── create_pr.py                 # Opens PR linking 'Closes #X'
│   ├── check_ci.py                  # Polls and verifies CI build status
│   └── update_issue_status.py       # Manages board status transitions
└── docs/
    ├── project_board_workflow.md    # Issue lifecycle, dependencies, worktrees & multi-view board standards
    └── coding_standards.md          # Commit hygiene & testing standards
```

---

## 🚀 Quick Start for AI Agents

1. **Bootstrap New Project**: Run `skills/init-agent-project/SKILL.md` or `python3 scripts/init_project.py --name <NAME> --create-board`.
2. **Read `AGENTS.md`**: Understand repository guardrails, worktree isolation rules, and the Issue-First Law.
3. **Execute Primary Skill**: Follow [`skills/implement-next-issue/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/implement-next-issue/SKILL.md) to inspect session state, pick the next actionable issue, create a clean worktree, and execute the implementation lifecycle.
4. **Use Helper Tools**: Execute GitHub operations via `python3 scripts/<script_name>.py`.

---

## 📄 License
MIT License. Free to use as a template for any AI-assisted coding repository.
