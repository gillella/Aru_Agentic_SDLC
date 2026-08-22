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
│   ├── aru-agentic-sdlc/            # Cursor router skill — pick the right procedure
│   ├── init-agent-project/          # Bootstrap repo, AGENTS.md, CI & Project Board
│   ├── implement-next-issue/        # Primary: claim → worktree → implement → PR → CI
│   ├── code-review/                 # PR review in an isolated worktree
│   ├── create-github-issue/         # Structured issue creation
│   ├── remediate-ci-failure/        # CI log parse & fix loop
│   └── address-pr-feedback/         # Resolve review threads
├── scripts/                         # Reusable GitHub helper automation tools
│   ├── install_agent_integration.sh  # Wire skills/adapters for Cursor/Codex/Claude/Antigravity
│   ├── common.py                    # Shared Git / Worktree / GitHub API / gh CLI helpers
│   ├── init_project.py              # Bootstraps AGENTS.md, CI, private repo & board
│   ├── fetch_next_issue.py          # Next unblocked issue respecting dependencies
│   ├── fetch_pr_feedback.py         # Inline PR comments → checklist
│   ├── claim_issue.py               # Assigns issue & updates board status
│   ├── create_branch.py             # Feature branch or worktree
│   ├── create_pr.py                 # Opens PR linking 'Closes #X'
│   ├── check_ci.py                  # Polls CI status
│   └── update_issue_status.py       # Board status transitions
├── templates/cursor/                # Cursor User Rules, project rules, slash commands
└── docs/
    ├── ARU-SOFTWARE-FACTORY.md      # The working build plan — start here
    ├── AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md  # Evidence and citations; not the roadmap
    ├── PROJECT-BOARD-FOR-AGENTIC-FACTORY.md        # GitHub vs Linear vs Jira for agents
    ├── cursor-integration.md        # Cross-project Cursor setup
    ├── project_board_workflow.md    # Board, dependencies, worktrees
    ├── coding_standards.md          # Commit hygiene & testing standards
    └── golden-path-demo.md          # Companion demo: idea through preview
```

**Where the project is going:** [`docs/ARU-SOFTWARE-FACTORY.md`](docs/ARU-SOFTWARE-FACTORY.md) is the narrative plan. [Roadmap epic #335](https://github.com/gillella/Aru_Agentic_SDLC/issues/335) and the governed GitHub Project Board are the live authority for phases, issue state, and delivery order. Read both before proposing structural changes.

**Strategy & industry research:** [`docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md`](docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md) maps a classical idea→research→requirements→sprint SDLC onto an agentic software factory (harness engineering, Spec Kit, board-as-orchestrator) and positions Aru against that landscape.

**Which project board?** [`docs/PROJECT-BOARD-FOR-AGENTIC-FACTORY.md`](docs/PROJECT-BOARD-FOR-AGENTIC-FACTORY.md) compares GitHub Issues, Linear, Jira, Azure Boards, and others for agentic coding factories — and recommends keeping GitHub as Aru’s coordinator.

**Explore the lifecycle:** open the no-build [interactive SDLC factory flow visualizer](sdlc_flow_visualizer/index.html) locally to inspect claim, worktree, review, merge, and remediation mechanics. Issue #342 is the Current status-legend correction; until it merges, the visualizer may lag this vocabulary, so use roadmap epic #335 and the Project Board for live status.

> **Research authority.** `AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md` is the
> single research and evidence guide. `ARU-SOFTWARE-FACTORY.md` is the
> canonical narrative plan; roadmap epic #335 and the Project Board are the
> live authority for sequencing and implementation status.

## Lifecycle status vocabulary

These six terms have one meaning across the README and both canonical factory
guides. A document never infers live issue state from prose; the board wins.

| Status | Meaning |
|---|---|
| **Shipped** | Present in the repository with linked implementation evidence. |
| **Current** | The live roadmap slice represented by open board work; consult the board for item state. |
| **Deferred** | Intentionally sequenced after an unmet phase entry gate; not available now. |
| **Blocked** | Cannot start or finish until an explicit dependency or operator decision is satisfied. |
| **Historical** | Dated evidence about an earlier state; never a current capability claim. |
| **Audit-only** | Records governance evidence but does not prove a runnable artifact or environment. |

Current delivery truth is deliberately narrower than “idea to production”:

- **Shipped:** governed intake, issue-to-merge execution, telemetry, and the
  GitHub Pages runnable preview path in `scripts/deploy_preview.py`.
- **Audit-only:** `scripts/promote.py` records GitHub environment/deployment
  evidence; it does not move or prove a runnable artifact.
- **Deferred:** real provider delivery remains issue #345 until an immutable
  deployment identity, authoritative URLs, smoke evidence, promotion without a
  rebuild, and rollback are demonstrated.

`scripts/merge_pr.py` defaults to a merge commit. Squash is an explicit opt-in,
not the shipped default (issue #89).

---

## 🚀 Quick Start for AI Agents

1. **Bootstrap New Project**: Run `skills/init-agent-project/SKILL.md` or `python3 scripts/init_project.py --name <NAME> --create-board`.
2. **Read `AGENTS.md`**: Understand repository guardrails, worktree isolation rules, and the Issue-First Law.
3. **Execute Primary Skill**: Follow [`skills/implement-next-issue/SKILL.md`](skills/implement-next-issue/SKILL.md) to inspect session state, pick the next actionable issue, create a clean worktree, and execute the implementation lifecycle.
4. **Use Helper Tools**: Execute GitHub operations via `python3 "$ARU_SDLC_HOME/scripts/<script_name>.py"` (they use the configured `gh` CLI). Do **not** use GitHub MCP for lifecycle mutations. `gh auth status` is the identity check; MCP GitHub is optional and non-authoritative — do not copy a PAT into it.

---

## 🖥️ Use from Cursor across all projects

One-time machine setup:

```bash
./scripts/install_agent_integration.sh
```

Then paste `templates/cursor/user-rules-aru-agentic-sdlc.md` into
**Cursor → Customize → Rules → User Rules**, and open a new Agent chat.

That installs:

- `ARU_SDLC_HOME` in your shell profile
- Symlinked Agent Skills for every SDLC procedure
- Slash commands (`/implement-next-issue`, `/init-agent-project`, …)

Details: [`docs/cursor-integration.md`](docs/cursor-integration.md).

GitHub access for the factory is the configured **`gh` CLI** via
`$ARU_SDLC_HOME/scripts/*.py`. GitHub MCP is not a required setup step.

---

## 🛤️ Golden-path demo

Walk idea → Ready → PR → distinct review → `merge_pr.py` → deploy-preview on
the public companion repo
[gillella/aru-golden-path-demo](https://github.com/gillella/aru-golden-path-demo)
without copying this playbook's `skills/` or factory helpers.

Runbook: [`docs/golden-path-demo.md`](docs/golden-path-demo.md).

**Playbook pin (S1.4):** set `ARU_SDLC_REF` to an immutable commit SHA or
`ckpt/<PR>-<sha7>` tag before installing. The demo currently pins `8d0513b`.
Do not pin the moving branch name `main` if you want a frozen walk. Details:
[`docs/cursor-integration.md`](docs/cursor-integration.md).

---

## 📄 License
MIT License. Free to use as a template for any AI-assisted coding repository.
