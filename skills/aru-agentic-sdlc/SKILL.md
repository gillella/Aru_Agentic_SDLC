---
name: aru-agentic-sdlc
description: Router for Aru_Agentic_SDLC governance. Selects the correct skill for issue-first GitHub work, worktrees, PRs, CI, and reviews. Use when starting any software task under Aru Agentic SDLC, when AGENTS.md references Aru_Agentic_SDLC, or when the user mentions SDLC, issue-first, implement next issue, or bootstrap a governed repo.
---

# Aru Agentic SDLC Router

Canonical playbook home: `$ARU_SDLC_HOME` (default
`/Users/aravindgillella/projects/Aru_Agentic_SDLC`).

If `ARU_SDLC_HOME` is unset, resolve it once, export it for the shell session,
and refuse to invent a second copy of these skills inside the target repo.

## Preconditions

1. Confirm `echo "$ARU_SDLC_HOME"` points at the playbook repo.
2. Confirm the target project has `AGENTS.md` with the Issue-First Law.
   If not, run `init-agent-project` (or ask whether to bootstrap).
3. Confirm GitHub identity with `gh auth status` (never print credential
   values). Factory helpers under `"$ARU_SDLC_HOME/scripts/"` shell out to
   that `gh`. **GitHub MCP is optional and non-authoritative** — do not use
   it for claims, labels, board status, PRs, reviews, or merges, and do not
   copy a PAT into MCP. Direct `gh` only when no helper exists (`gh issue
   comment` for implementation plans).
4. Prefer helper scripts under `"$ARU_SDLC_HOME/scripts/"` for GitHub/git
   operations — do not reimplement them ad hoc.

## Route the request

| User intent | Skill to read and follow |
|---|---|
| Please continue / keep going / work the board / no specific step named | `run-aru-factory` (`loop`) |
| New governed repo / board / CI | `init-agent-project` |
| Implement a named issue, or `implement next issue` | `implement-next-issue` |
| File a bug/feature/task | `create-github-issue` |
| Research board item / `type:research` / `research:` title | `research` |
| Promote Backlog → Ready; board has no ready work | `triage-backlog` |
| Review someone else's PR | Refuse: CodeRabbit alone reviews; route findings with `address-pr-feedback` |
| Fix red CI on an open PR | `remediate-ci-failure` |
| Address review comments on your PR | `address-pr-feedback` |

Read the matching skill from `$ARU_SDLC_HOME/skills/<name>/SKILL.md` and
execute it exactly. Do not skip worktree isolation, local tests, or
`Closes #<issue>` on PRs.

## Hard rules (always)

- **Issue-First Law**: no code change without a tracked GitHub issue.
- **GitHub via `gh` + helpers, not MCP**: lifecycle mutations use
  `$ARU_SDLC_HOME/scripts/*.py`. `gh auth status` is the identity check.
  MCP GitHub is optional and non-authoritative.
- **No direct pushes** to `main` / `master`.
- **Worktrees** under `.worktrees/` for feature/remediation work.
- **Review authority**: CodeRabbit only. Coding agents never review PRs.
- **Local tests green** before commit/push.
- **CI green** before merge; remediate rather than weaken gates.
- **Session recovery first**: inspect git log, branches, open PRs, and board
  status before claiming new work.

## Agent identity

Every claim requires `--agent <AGENT_ID>` (for example `cursor-1`). Use a
stable id per concurrent agent. Resume in-flight work for that id before
claiming something new.

## References

- Board lifecycle: `$ARU_SDLC_HOME/docs/project_board_workflow.md`
- Commit/test standards: `$ARU_SDLC_HOME/docs/coding_standards.md`
- Cursor install notes: `$ARU_SDLC_HOME/docs/cursor-integration.md`
