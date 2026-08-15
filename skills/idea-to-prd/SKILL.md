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

1. Discover `grill-aru` from the active skill registry and read its complete
   `SKILL.md` before beginning. If it is unavailable, stop and report the
   missing dependency; never recreate its interrogation here.
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
- Identify the governed repository and its `ARU_SDLC_HOME` before any eventual
  publication.

### 2. Run Grill-Aru

- Invoke the existing `grill-aru` skill and complete its interrogation.
- Preserve the final Grill-Aru output categories: confirmed decisions, open
  items, and assumptions to validate.
- Keep traceability to the operator's answers. Do not silently resolve or omit
  disagreement, uncertainty, or deferred choices.

### 3. Draft the PRD

Transform only the confirmed interrogation results into the PRD structure in
`.github/ISSUE_TEMPLATE/prd.md`.

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

### 5. Publish the Approved PRD

Create a GitHub issue from the approved PRD. Direct `gh` is permitted here
because the framework has no dedicated PRD publication helper:

```bash
gh issue create \
  --title "prd: <product or capability name>" \
  --label "type:epic" \
  --body-file <approved-prd-file>
```

Capture the created issue number, then attach it to the governed board and set
its initial status using the framework helper:

```bash
python3 "$ARU_SDLC_HOME/scripts/update_issue_status.py" \
  --issue <ID> \
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
