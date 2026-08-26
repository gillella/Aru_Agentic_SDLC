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
- Memory tools provide context only. `create_pr.py` assigns exactly one of
  `review:coderabbit`, `review:sourcery`, or `review:codeant` using a
  deterministic least-loaded algorithm over the complete paginated open-PR
  inventory. Ties rotate by issue number in that fixed service order. The
  label is a reservation: it settles across consecutive fresh inventory reads,
  and the higher-numbered pull request releases and re-selects when a collision
  is observed. The existing assignment is immutable; after concrete observed
  unavailability an operator may make one audited external reassignment, never
  an automatic retry or repeated rotation.
  Coding agents never perform ordinary review. Only when all external reviewers
  are unavailable, busy, or waiting too long
  may the operator assign one independent coding agent with `review:agent`.
  That emergency path is per-PR and never creates a review queue, rotation,
  fleet, scheduler, or permission for an author to review their own work.
  Reviewers are not workflow owners or merge authorities.
- If lifecycle instructions conflict, follow Aru. Higher-priority explicit
  system, developer, or user instructions still take precedence.
- During GitHub outages, coordination stops gracefully; agents may continue local
  work in claimed worktrees but must never create fallback task queues or bypass
  gates (see [docs/degraded-mode.md](docs/degraded-mode.md)).

Routine merge execution is mechanical and may be performed by any factory
agent, including the implementation author, only through
`python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID>`, after the assigned
assigned reviewer has supplied authoritative exact-head evidence and every
enforced gate passes. CodeRabbit keeps its current exact-head contract.
Sourcery requires a successful head-bound `Sourcery review` check and zero
Sourcery unresolved threads. CodeAnt requires either an authoritative
exact-head `codeant-ai` review object or, when a clean run left no review
object to find, a trusted `codeant-ai` completed clean-review status record
bound unambiguously to the exact head - either way, plus zero CodeAnt
unresolved threads. A `codeant-ai` Review object bound to an earlier head is
historical audit evidence from a prior push, not current-head evidence, and
does not block the status-record fallback; a Review object that is itself
bound to the exact current head but unusable (pending, malformed, spoofed,
or ambiguous) still blocks. No agent or human may bypass the merge helper
with a direct push or an ad-hoc merge.
An emergency agent review requires exactly one different `author:` and
`reviewer:` identity, a valid reviewer model family, a substantive GitHub
review of the exact current head, and one matching completed
`aru-agent-review:v1` record. Authorization comes from the
`aru-agent-review-assignment:v1` record, which counts only when its author
holds repository write access - resolved from the collaborator roster, not the
comment's reported author association - and which names the single GitHub
account permitted to perform that review; both the review and the completion
record must come from that account. Missing, stale, duplicate, self-authored,
unauthorized, or malformed evidence from those accounts fails closed. A
marker-shaped comment from anyone else is ignored, so a drive-by comment
cannot block every later emergency merge.
Direct pushes to `main` are also blocked server-side by branch protection;
an ad-hoc merge (`gh pr merge` or the GitHub UI, run outside `merge_pr.py`)
is not - branch protection requires only a green CI status check, not
assigned-service review evidence - so that half of the rule is a governance
requirement agents and humans must follow, not a technical guarantee.

Legacy `reviewed-by:<coding-agent>`, `reviewer:<coding-agent>`, and
`aru-review-head:v1` evidence alone never satisfies the current review gate.
Assigned-reviewer identity, completed status, substantive review or check
evidence, current-head commit, and assigned-service thread disposition come
from authoritative GitHub data and fail closed on missing, pending, failed,
skipped, stale, ambiguous, duplicated, partial, or spoofed evidence.

Money, PII, security, schema, migration, irreversible behavior, large diffs,
and repeated review rounds increase the required planning, testing, and review
depth; none of them alone creates a mandatory human gate. Human intervention
is exceptional and is needed only when a severe merge conflict or merge/
close-out failure remains unsafe or impossible for agents to resolve through
the governed remediation path. Record the exact failure and attempted
remediation when that exception occurs.

Code review never supplies operational authorization. Existing human gates for
real-money execution, production cutover, destructive migration, credential
use, and external-account mutation remain separate and mandatory.

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
- **Code review**: external review by default; the emergency agent-review path
  is permitted only after explicit operator assignment. See
  [`skills/code-review/SKILL.md`](skills/code-review/SKILL.md). Authors use
  [`skills/address-pr-feedback/SKILL.md`](skills/address-pr-feedback/SKILL.md)
  to remediate findings.
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

* `"$ARU_SDLC_HOME/scripts/install_agent_integration.sh"` — wire skills, commands, env for Cursor/Codex/Claude/Antigravity
* `python3 "$ARU_SDLC_HOME/scripts/init_project.py" --name <NAME> [--private] [--create-board]`
* `"$ARU_SDLC_HOME/scripts/install_hooks.sh"` — pre-push + PreToolUse enforcement (run once per repo)
* `python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" --agent <AGENT_ID>` — one picker for all three work types; prefer over `fetch_next_issue.py`
* `python3 "$ARU_SDLC_HOME/scripts/fetch_next_issue.py" --agent <AGENT_ID>` — issues only
* `python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" [--capacity]`
* `python3 "$ARU_SDLC_HOME/scripts/reassign_review.py" --pr <ID> --to <coderabbit|sourcery|codeant|agent> --reason <WHY> [...]`
* `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --issue <ID> --agent <AGENT_ID>`
* `python3 "$ARU_SDLC_HOME/scripts/create_branch.py" --issue <ID> --type <feat|fix|docs> [--worktree] [--agent <id>]`
* `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --pr <ID> --adopt --agent <id> --model-family <family>` — take over an abandoned PR

## Agent identity

An agent's id is derived from where it runs: `<product>-<fingerprint>`, e.g.
`claude-a3f19c`, where the fingerprint hashes machine, checkout, and model
family. It is therefore **stable across restarts** — a restarted session
recomputes the same id and reclaims its own board work — and **distinct across
machines**, so two agents can never be issued the same id.

Override with `ARU_AGENT_ID`, or name one explicitly with `--agent`, including
when an operator prefers a fixed readable name such as `claude-1` or `codex-1`.

## When an agent disappears

Claims are not permanent. An issue whose record, branch, and pull request have
all been quiet longer than `--reap-after` (default 4h) is released back to
Ready, with an audit comment naming the branch and PR that were left behind.

A successor **adopts** the abandoned PR rather than restarting it: `author:` and
`family:` move to the adopting agent, `adopted-from:<previous>` is recorded, and
the commits, CI history, and review threads are preserved. Adoption refuses a PR
updated inside the abandonment window, so live work cannot be taken.
* `python3 "$ARU_SDLC_HOME/scripts/create_pr.py" --issue <ID> --agent <AGENT_ID> [--model-family <family>] --title "<Title>" --body "<body>"`
  — `--agent` is required for author/remediator routing and audit attribution.
* `python3 "$ARU_SDLC_HOME/scripts/check_ci.py" --pr <ID>`
* `python3 "$ARU_SDLC_HOME/scripts/fetch_pr_feedback.py" --pr <ID>`
* `python3 "$ARU_SDLC_HOME/scripts/update_issue_status.py" --issue <ID> --status "<Status>"`
* `python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID> [--dry-run]` — the Definition-of-Done gate; the only sanctioned way to merge
* `python3 "$ARU_SDLC_HOME/scripts/revert_merge.py" --pr <ID> --agent <AGENT_ID> --family <family> --revert-issue <ID>` — governed reverse gear: creates a revert PR linking Closes #<ID>, reopens affected issues to Ready status, and enforces clean revert checks
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
[`docs/coding_standards.md`](docs/coding_standards.md) (including the
[Trust Boundary](docs/coding_standards.md#trust-boundary-for-issue-pr-and-review-text)
for untrusted issue, PR, and review text).

---

## Cursor Integration (cross-project)

To use this playbook from **any** Cursor workspace on this machine:

1. Run `scripts/install_agent_integration.sh` once (sets `ARU_SDLC_HOME`,
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

---

## Slack War Room & Escalation

Slack is the **escalation channel only** — never a work queue and never a second
chat home. The GitHub Project Board is the work queue; Telegram is Hermes'
home. Escalation is low-volume by design: routine issue work proceeds
autonomously (escalate on Slack, not silently — consult Aru only for important
new decisions and complex merge/conflict resolution).

**Single-channel decision (Aru, 2026-08-20):** all projects share ONE war
room. Escalation volume is near-zero for a solo operator, so one watched
channel beats per-project fragmentation. Per-project channels are reserved for
when a second human operator joins — do not create them now.

- Workspace: `anguliyam.slack.com` (team `T07L1SZCQEM`)
- War room: `#project-aru-code` (private, channel ID `C0BPZMRR1RC`)
- Bot: `@aru_code_app` (the Aru-CODE app)

Every alert is self-identifying — `slack_notify.py` stamps agent, model
family, `project_id`, issue/PR, and time — so a single channel stays navigable
without per-project fan-out. Disambiguation is metadata, not channel count.

**Two Slack layers (do not confuse them):**

1. **Hermes war room (live).** Bot `@aru_code_app` reads agent chatter in
   `#project-aru-code` and posts escalation pings + resolutions. Tokens live in
   `~/.hermes/.env` (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`).
2. **Factory bridge (documented, not yet deployed).** `scripts/slack_notify.py`
   + `scripts/slack_control_room.py` (Socket Mode, one bot, project registry in
   `~/.aru/projects.json`) emit the `blocked` / `waiting-on` / `hitl` alert
   events. As of 2026-08-20 the `~/.aru/` credential directory is absent, so
   this bridge is not wired; when it is, it MUST target the same
   `#project-aru-code` channel (single-channel decision), not per-project
   channels.

Details: [`docs/slack-control-room.md`](docs/slack-control-room.md).
