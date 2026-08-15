---
name: idea-to-prd
description: Turn a raw product or software idea into an operator-approved PRD by wrapping the existing Grill-Aru interrogation, then publish the PRD as a governed Backlog epic. Use when the user wants to develop, refine, or formalize an idea before planning implementation work.
triggers:
  - "turn this idea into a PRD"
  - "develop this product idea"
  - "formalize this requirement"
  - "idea to PRD"
do_not_trigger_for:
  - "implementing an approved issue (use implement-next-issue instead)"
  - "turning an approved PRD into issues (use prd-to-issues instead)"
  - "filing an already-defined issue (use create-github-issue instead)"
---

# Idea-to-PRD Procedure

This skill owns orchestration only. It wraps the existing `grill-aru` skill,
converts the resulting decisions into a stable PRD contract, obtains explicit
operator approval, and publishes the approved artifact to the governed GitHub
Project Board.

It does not replace Grill-Aru, create implementation tasks, start a second
lifecycle, or make product decisions for the operator.

## Non-Negotiable Rules

1. Resolve `grill-aru` deterministically and read its complete `SKILL.md`
   before beginning. Prefer the exact path supplied by the active runtime's
   available-skills registry. If the registry has no entry, check these paths
   in order: `$ARU_SDLC_HOME/skills/grill-aru/SKILL.md`,
   `~/.agents/skills/grill-aru/SKILL.md`,
   `~/.codex/skills/grill-aru/SKILL.md`,
   `~/.cursor/skills/grill-aru/SKILL.md`, and
   `~/.claude/skills/grill-aru/SKILL.md`. Do not search arbitrary directories
   or infer availability from the skill name alone. If none exists, stop and
   report the missing dependency plus every checked path; never recreate its
   interrogation here.
2. During interrogation, follow Grill-Aru exactly, including its rule to ask
   exactly one question per turn. Do not draft the PRD, summarize prematurely,
   or start planning while the interrogation is active.
3. Recommendations are proposals until the operator accepts them. Never turn a
   suggested default, inference, or unanswered question into a confirmed
   requirement.
4. A missing required product decision makes the PRD `BLOCKED`; it must remain
   visible as an open question rather than being guessed.
5. Do not publish to GitHub until the operator explicitly approves the complete
   draft presented in the conversation.
6. Publish the PRD as a `type:epic` issue in `Backlog`. Do not promote it to
   `Ready`; implementation issues are created later through `prd-to-issues`.
7. GitHub and the Project Board remain authoritative. Spec-oriented sections
   are an artifact contract, not a competing lifecycle or tool requirement.

## Workflow

### 1. Establish Context

- Capture the raw idea in the operator's words.
- If a repository is in scope, inspect its current product and technical
  baseline before asking questions.
- Identify the governed repository root, its unambiguous `<owner>/<name>`
  identity, and its `ARU_SDLC_HOME` before any eventual publication. Fail
  closed if the root or repository identity cannot be resolved and verified.

### 2. Run Grill-Aru

- Invoke the existing `grill-aru` skill and complete its interrogation.
- Preserve the final Grill-Aru output categories: confirmed decisions, open
  items, and assumptions to validate.
- Keep traceability to the operator's answers. Do not silently resolve or omit
  disagreement, uncertainty, or deferred choices.

### 3. Draft the PRD

Transform only the confirmed interrogation results into the PRD structure at
`$ARU_SDLC_HOME/.github/ISSUE_TEMPLATE/prd.md`. Resolve it from the canonical
playbook rather than the current repository: consumer repositories are not
required to carry their own copy of this template.

Use stable identifiers:

- requirements: `REQ-001`, `REQ-002`, ...
- decisions: `DEC-001`, `DEC-002`, ...
- open questions: `OQ-001`, `OQ-002`, ...
- assumptions: `ASM-001`, `ASM-002`, ...

Every success metric must define a measurable outcome or explicitly remain an
open question. Every open question records whether it blocks planning, its
owner, and the decision needed. Every assumption records how it will be
validated.

Set artifact readiness as follows:

- `BLOCKED`: at least one open question marked `Blocking: yes` remains.
- `READY_FOR_PLANNING`: required product decisions are confirmed and no
  blocking open question remains.

`READY_FOR_PLANNING` means the PRD may enter the planning station; it does not
mean the epic or any implementation work is `Ready` on the board.

### 4. Obtain Explicit Approval

- Present the complete draft to the operator.
- Ask one direct question: whether this exact PRD should be published.
- If the operator requests changes, update the draft and ask again.
- If the operator approves, record that approval in the PRD's Artifact Status.
- If approval is absent or ambiguous, stop before publication.

After approval, bind it to the exact file bytes that the operator reviewed.
Preserve both variables in the same shell session used for publication. Any
content change invalidates the digest and requires renewed approval:

```bash
set -euo pipefail
ARU_APPROVED_PRD_FILE="/absolute/path/to/approved-prd.md"
test -f "$ARU_APPROVED_PRD_FILE"
ARU_APPROVED_PRD_SHA256="$(shasum -a 256 "$ARU_APPROVED_PRD_FILE" | awk '{print $1}')"
test -n "$ARU_APPROVED_PRD_SHA256"
```

### 5. Publish the Approved PRD

Create a GitHub issue from the approved PRD. Direct `gh` is permitted here
because the framework has no dedicated PRD publication helper. First change to
the governed repository root and verify that the repository identity resolved
there is the same identity captured in Step 1. The template may live in the
playbook clone; the epic must live in the governed consumer repository.

Use task-specific variables rather than relying on whichever repository `gh`
happens to infer from the current directory. Set both values from the verified
Step 1 context before running the commands:

```bash
set -euo pipefail
ARU_GOVERNED_REPO_ROOT="/absolute/path/to/governed/repository"
ARU_GOVERNED_REPO="owner/name"
test "$(git -C "$ARU_GOVERNED_REPO_ROOT" rev-parse --show-toplevel)" = \
  "$ARU_GOVERNED_REPO_ROOT"
test -f "$ARU_APPROVED_PRD_FILE"
test "$(shasum -a 256 "$ARU_APPROVED_PRD_FILE" | awk '{print $1}')" = \
  "$ARU_APPROVED_PRD_SHA256"
cd "$ARU_GOVERNED_REPO_ROOT"
test "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" = \
  "$ARU_GOVERNED_REPO"
ARU_PRD_ISSUE_URL="$(gh issue create \
  --repo "$ARU_GOVERNED_REPO" \
  --title "prd: <product or capability name>" \
  --label "type:epic" \
  --body-file "$ARU_APPROVED_PRD_FILE")"
ARU_PRD_ISSUE_ID="${ARU_PRD_ISSUE_URL##*/}"
test -n "$ARU_PRD_ISSUE_ID"
test "$ARU_PRD_ISSUE_ID" -eq "$ARU_PRD_ISSUE_ID" 2>/dev/null
```

Capture the created issue number, remain in `$ARU_GOVERNED_REPO_ROOT`, and
confirm `gh repo view --json nameWithOwner --jq .nameWithOwner` still matches
`$ARU_GOVERNED_REPO`. Then attach the issue to that repository's governed board
and set its initial status using the framework helper, which resolves its
repository from the current working directory:

```bash
set -euo pipefail
cd "$ARU_GOVERNED_REPO_ROOT"
test "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" = \
  "$ARU_GOVERNED_REPO"
python3 "$ARU_SDLC_HOME/scripts/update_issue_status.py" \
  --issue "$ARU_PRD_ISSUE_ID" \
  --status Backlog \
  --require-board
```

Treat publication as incomplete if issue creation, the `type:epic` label, board
attachment, or Backlog status cannot be verified. A blocked PRD may still be
published for visibility, but its `BLOCKED` readiness and blocking questions
must remain explicit.

### 6. Hand Off to Planning

- For a `READY_FOR_PLANNING` PRD, name `prd-to-issues` as the next station.
- For a `BLOCKED` PRD, name the blocking question owners and wait for their
  decisions.
- Do not create `tasks.md`, implementation issues, branches, worktrees, code,
  or pull requests in this skill.

The downstream sequence is:

```text
approved PRD -> specify -> plan -> tasks via prd-to-issues -> governed issue lifecycle
```

## Fail-Closed Example

If an idea proposes a Slack intake bot but the operator has not decided whether
direct messages are accepted, record it as:

```text
OQ-001: Does the intake bot accept direct messages?
Blocking: yes
Owner: Product owner
Decision needed: channel-only or channel-and-DM intake
```

Set readiness to `BLOCKED`. Do not choose channel-only or channel-and-DM on the
operator's behalf.
