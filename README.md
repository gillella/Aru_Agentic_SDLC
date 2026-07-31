# Aru_Agentic_SDLC - AI Engineering Playbook & Template

A vendor-neutral, GitHub-centric **Agentic Software Development Life Cycle (SDLC)** template and AI Engineering Playbook. Standardized for compliance with **SkillsMP (Agent Skills Marketplace)** specs.

---

## 🌟 Overview

`Aru_Agentic_SDLC` establishes standardized rules, declarative skill procedures, and automation scripts for AI coding agents (Gemini, Claude, Codex, Antigravity, AutoGen, CrewAI, etc.). It enables autonomous or semi-autonomous development teams to maintain high quality, strict git hygiene, and clear issue traceability.

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
│   │   └── SKILL.md                 # Primary workflow: Session recovery -> Next Issue -> Implementation -> PR -> CI
│   ├── code-review/
│   │   └── SKILL.md                 # Code review & security audit procedure
│   └── create-github-issue/
│       └── SKILL.md                 # Structured issue creation workflow
├── scripts/                         # Reusable GitHub helper automation tools
│   ├── common.py                    # Shared Git / GitHub API / gh CLI helpers
│   ├── fetch_next_issue.py          # Identifies next unblocked issue respecting dependencies
│   ├── claim_issue.py               # Assigns issue & updates board status
│   ├── create_branch.py             # Creates standardized feature branch
│   ├── create_pr.py                 # Opens PR linking 'Closes #X'
│   ├── check_ci.py                  # Polls and verifies CI build status
│   └── update_issue_status.py       # Manages board status transitions
└── docs/
    ├── project_board_workflow.md    # Issue lifecycle, dependencies & multi-agent rules
    └── coding_standards.md          # Commit conventions & testing standards
```

---

## 🚀 Quick Start for AI Agents

1. **Read `AGENTS.md`**: Understand repository guardrails and rules of engagement.
2. **Execute Primary Skill**: Follow [`skills/implement-next-issue/SKILL.md`](file:///Users/aravindgillella/projects/Aru_Agentic_SDLC/skills/implement-next-issue/SKILL.md) to inspect session state, pick the next actionable issue, and execute the implementation lifecycle.
3. **Use Helper Tools**: Execute GitHub operations via `python3 scripts/<script_name>.py`.

---

## 🛡️ Key Features

- **Tool-Agnostic Skills**: Skills contain pure declarative markdown instructions with zero hardcoded agent CLI syntax.
- **Session Continuity**: Agents automatically detect recent commits and active PRs to resume work seamlessly across sessions.
- **Progressive & Parallel Order**: Issues with dependencies are executed sequentially; independent issues are claimed in parallel by spawning subagents.
- **Automated CI Remediation**: Integrated loop for reading CI failure logs, fixing issues, and pushing updates.

---

## 📄 License
MIT License. Free to use as a template for any AI-assisted coding repository.
