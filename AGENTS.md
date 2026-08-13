# AGENTS.md - Master AI Engineering Playbook & Operating Directives

Welcome to **Aru_Agentic_SDLC**. This repository serves as a vendor-neutral, tool-agnostic template and operating playbook for AI coding agents (e.g., Gemini, Claude, Codex, Cursor, Antigravity, AutoGen, CrewAI, etc.).

All AI agents operating within this repository MUST follow the directives, skills, and tools defined herein.

---

## Core Governance Directive: The Issue-First Law

**No code change, refactor, or feature implementation may begin without first originating from a tracked issue on the GitHub Project Board.**

```
[ New Requirement / Bug ] ──► [ Issue Filed & Triaged ] ──► [ Claimed via Skill ] ──► [ Worktree Code ] ──► [ PR Closes #X & CI ] ──► [ Merged ]
```

---

## Process Ownership and Merge Authority

**Aru_Agentic_SDLC owns the complete issue-to-merge lifecycle in this
repository.** Other installed frameworks and tools may assist inside the
current Aru step, but they do not start a second lifecycle, replace the Project
Board, claim work independently, create an ungoverned branch, or merge around
the Definition-of-Done gate.

- GSD lifecycle/resume hooks and `.planning/HANDOFF.json` are disabled or
  non-authoritative in Aru-governed repositories.
- Brainstorming workflows such as Superpowers supply input to Aru's plan gate;
  they do not run a parallel implementation process.
- Memory tools provide context only. PR bots and review tools are reviewers,
  not workflow owners or merge authorities.
- If lifecycle instructions conflict, follow Aru. Higher-priority explicit
  system, developer, or user instructions still take precedence.

Routine merge execution is mechanical and may be performed by any factory
agent, including the implementation author, only through
`python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID>`, after a distinct agent
has completed the independent review and every enforced gate passes. An author
must never review their own PR. No agent or human may bypass the merge helper
with a direct push or an ad-hoc merge.

Money, PII, security, schema, migration, irreversible behavior, large diffs,
and repeated review rounds increase the required planning, testing, and review
depth; none of them alone creates a mandatory human gate. Human intervention
is exceptional and is needed only when a severe merge conflict or merge/
close-out failure remains unsafe or impossible for agents to resolve through
the governed remediation path. Record the exact failure and attempted
remediation when that exception occurs.

---

## Primary Directives for AI Agents

### 1. Execute via SkillsMP Skills

Perform tasks by following the declarative procedures in `skills/` (or the
Cursor-installed symlinks of the same names). Prefer `$ARU_SDLC_HOME/skills/`
when working from another repository.

- **Router**: [`skills/aru-agentic-sdlc/SKILL.md`](skills/aru-agentic-sdlc/SKILL.md)
- **Initialization**: [`skills/init-agent-project/SKILL.md`](skills/init-agent-project/SKILL.md)
- **Primary execution**: [`skills/implement-next-issue/SKILL.md`](skills/implement-next-issue/SKILL.md)
  - Session recovery → next unblocked issue → worktree → implement → test →
    commit → push → PR (`Closes #N`) → CI → remediation → review
- **Issue creation**: [`skills/create-github-issue/SKILL.md`](skills/create-github-issue/SKILL.md)
- **Backlog triage**: [`skills/triage-backlog/SKILL.md`](skills/triage-backlog/SKILL.md)
  - Promotes `Backlog` → `Ready` so the picker has work to hand out
- **Code review**: [`skills/code-review/SKILL.md`](skills/code-review/SKILL.md)
- **CI remediation**: [`skills/remediate-ci-failure/SKILL.md`](skills/remediate-ci-failure/SKILL.md)
- **PR feedback**: [`skills/address-pr-feedback/SKILL.md`](skills/address-pr-feedback/SKILL.md)

### 2. Offload Concrete Actions to Helper Scripts

Skills are declarative. Git / worktree / GitHub actions MUST use
`$ARU_SDLC_HOME/scripts/*.py` (they shell out to the configured `gh` CLI).
**Do not use GitHub MCP** (`user-github` or any other MCP GitHub server) for
lifecycle mutations — claims, labels, board status, PRs, reviews, or merges.
MCP is a second credential store and a second API client; writes through it
bypass `author:` / `reviewer:` stamps, board status, and the merge gate.
`gh auth status` is the GitHub identity check. MCP GitHub is optional and
non-authoritative. Never copy a PAT into MCP to "fix" it.

Direct `gh` is allowed only when no helper exists. The sanctioned exception
is implementation-plan comments via `gh issue comment`.

Helper inventory:

* `"$ARU_SDLC_HOME/scripts/install_cursor_integration.sh"` — wire Cursor skills, commands, env
* `python3 "$ARU_SDLC_HOME/scripts/init_project.py" --name <NAME> [--private] [--create-board]`
* `"$ARU_SDLC_HOME/scripts/install_hooks.sh"` — pre-push + PreToolUse enforcement (run once per repo)
* `python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" --agent <AGENT_ID>` — one picker for all three work types; prefer over `fetch_next_issue.py`
* `python3 "$ARU_SDLC_HOME/scripts/fetch_next_issue.py" --agent <AGENT_ID>` — issues only
* `python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" [--capacity]`
* `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --issue <ID> --agent <AGENT_ID>`
* `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --pr <ID> --agent <AGENT_ID>` — claim a PR for review
* `python3 "$ARU_SDLC_HOME/scripts/create_branch.py" --issue <ID> --type <feat|fix|docs> [--worktree]`
* `python3 "$ARU_SDLC_HOME/scripts/create_pr.py" --issue <ID> --agent <AGENT_ID> [--model-family <family>] --title "<Title>" --body "<body>"`
  — `--agent` is required. It stamps `author:<id>`, which is the only thing
  that lets the merge gate tell a peer review from a self-review, since every
  agent authenticates as the same GitHub user.
* `python3 "$ARU_SDLC_HOME/scripts/check_ci.py" --pr <ID>`
* `python3 "$ARU_SDLC_HOME/scripts/fetch_pr_feedback.py" --pr <ID>`
* `python3 "$ARU_SDLC_HOME/scripts/update_issue_status.py" --issue <ID> --status "<Status>"`
* `python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID> [--dry-run]` — the Definition-of-Done gate; the only sanctioned way to merge
* `python3 "$ARU_SDLC_HOME/scripts/revert_merge.py" --pr <ID> [--agent <ID>] [--family <family>]` — governed reverse gear: creates a revert PR, reopens affected issues to Ready status, and enforces clean revert checks
* `"$ARU_SDLC_HOME/scripts/launch_fleet.sh" -n <N>` — prepare N isolated clones + per-agent prompts

Inside this playbook repo itself, `$ARU_SDLC_HOME` may be `.` / the repo root.

---

## Repository Rules & Guardrails

1. **Strict Issue-First Execution**: Never code without a claimed open issue on the project board.
2. **GitHub access is `gh` plus helpers, not MCP**: All GitHub lifecycle
   mutations go through the configured `gh` CLI via
   `python3 "$ARU_SDLC_HOME/scripts/<tool>.py"`. Do not use GitHub MCP for
   those writes. Direct `gh` only when no helper exists (`gh issue comment`
   for implementation plans). Do not make MCP GitHub a required setup step.
3. **No Direct Pushes to Default Branches**: Never push directly to `main` or `master`.
4. **Mandatory Worktree Isolation**: Feature work and PR reviews run under `.worktrees/`.
5. **Mandatory Issue Closure Linking**: Every PR body includes `Closes #<issue_number>`.
6. **Local Test Verification First**: Never commit or push without a green local suite.
7. **CI Green Gate**: If CI fails, invoke `remediate-ci-failure`.
8. **Session State Memory**: Inspect git log, branches, open PRs, and board status before claiming new work.
9. **Plan Before Editing**: For `type:feat`, `needs-design`, money, PII,
   schema, migration, or other irreversible work, post the implementation plan
   required by `implement-next-issue` before the first edit. High-risk scope
   triggers the gate regardless of the issue's type labels. The plan is always
   post-and-proceed unless the issue lacks a product decision required to
   define its acceptance criteria; risk category alone does not require human
   acknowledgement.

---

## Issue & Project Board Lifecycle

```
[ Backlog ] ──► [ Ready ] ──► [ In Progress ] ──► [ In Review ] ──► [ Done ]
```

See [`docs/project_board_workflow.md`](docs/project_board_workflow.md) and
[`docs/coding_standards.md`](docs/coding_standards.md).

---

## Cursor Integration (cross-project)

To use this playbook from **any** Cursor workspace on this machine:

1. Run `scripts/install_cursor_integration.sh` once (sets `ARU_SDLC_HOME`,
   symlinks skills into `~/.cursor/skills/` and `~/.agents/skills/`, installs
   slash commands under `~/.cursor/commands/`).
2. Paste `templates/cursor/user-rules-aru-agentic-sdlc.md` into
   **Cursor → Customize → Rules → User Rules**.
3. In each app repo keep a thin `AGENTS.md` that points at `$ARU_SDLC_HOME`
   — do not vendor a second copy of `skills/` or `scripts/`.

Full notes: [`docs/cursor-integration.md`](docs/cursor-integration.md).

Slash commands after install: `/implement-next-issue`, `/init-agent-project`,
`/create-github-issue`, `/code-review`, `/remediate-ci-failure`,
`/address-pr-feedback`.
