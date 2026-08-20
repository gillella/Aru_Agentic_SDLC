---
name: grill-aru
description: Run the one-question-at-a-time product interrogation that turns a raw idea into a confirmed decision inventory (DEC-*, OQ-*, ASM-*). Use when the operator wants to grill a product idea into explicit, non-guessed decisions before any PRD is drafted.
triggers:
  - "grill the idea"
  - "interrogate this product idea"
  - "ask me the product questions"
  - "run grill-aru"
do_not_trigger_for:
  - "turning an idea into a PRD (use idea-to-prd instead; it wraps this skill)"
  - "decomposing an approved PRD into issues (use prd-to-issues instead)"
  - "filing an already-defined issue (use create-github-issue instead)"
---

# Grill-Aru Interrogation Procedure

This skill owns the product interrogation only. It asks the operator exactly one
question per turn and never invents a missing product decision. Its output is a
stable decision inventory that `idea-to-prd` step 3 consumes to draft the PRD.

It does not draft the PRD, create issues, branches, worktrees, or code, and it
does not make product decisions on the operator's behalf.

## Non-Negotiable Rules

1. Ask exactly one question per turn. Never bundle a second question into a
   confirmation, a follow-up, or a multi-part prompt.
2. Never guess a missing or ambiguous product decision. Record it as a blocking
   open question (`OQ-*`) with an owner and the exact decision needed; do not
   substitute a default, an inference, or a placeholder answer.
3. Every suggested answer is a proposal until the operator accepts it. A default
   is never promoted to a confirmed requirement without an explicit operator
   acceptance.
4. A partial or ambiguous operator answer triggers a re-ask of the same single
   question; do not move on or interpret it.
5. If no operator is present (headless run), stop and report the inventory so
   far plus every unanswered question; never fabricate decisions to finish.

## Workflow

### 1. Establish the Interrogation Target

- Capture the raw idea in the operator's own words.
- If a repository is in scope, note its `<owner>/<name>` identity and governed
  root for downstream use, but do not require it to ask questions.
- Seed the inventory with an assumption record only when the operator states
  the assumption; do not invent one.

### 2. Ask One Question per Turn

Ask a single, concrete question. Wait for the operator's answer before asking
the next one. Do not ask the next question in the same turn as presenting a
result.

When the answer is unambiguous, record it as a decision. When the answer is
partial, ambiguous, or deferred, re-ask the same question (ambiguity) or record
it as a blocking open question with an owner (deferral).

Stop asking when the operator signals the interrogation is complete, or when
every question the PRD needs has either a confirmed decision or a blocking open
question. Then emit the inventory.

### 3. Emit the Decision Inventory

Use exactly these stable identifier families so `idea-to-prd` can consume the
output directly:

- `DEC-001`, `DEC-002`, ... — confirmed decisions.
- `OQ-001`, `OQ-002`, ... — blocking open questions.
- `ASM-001`, `ASM-002`, ... — assumptions to validate.

Each record is one line with a label and its detail:

```text
DEC-001: <the confirmed decision>
OQ-001: <the missing decision> | Blocking: yes | Owner: <role or person> | Decision needed: <exact choice>
ASM-001: <the assumption> | Validate by: <how it will be checked>
```

An open question must record whether it blocks planning, its owner, and the
decision needed. An assumption must record how it will be validated.

## Decision Boundaries

- A decision is confirmed only when the operator explicitly accepts it in
  answer to a question. A proposal the operator has not yet accepted stays out
  of `DEC-*`.
- A question the operator defers is recorded as a blocking `OQ-*`, never as a
  guess.
- A headless run stops and reports rather than fabricating answers.

## Non-Goals

- Does not draft the PRD (`idea-to-prd` does that).
- Does not create issues, branches, worktrees, or code.
- Does not make product decisions on the operator's behalf.

## Fail-Closed Example

Operator proposes a Slack intake bot but has not decided whether direct
messages are accepted. Ask one question:

```text
Does the intake bot accept direct messages?
```

If the operator defers, record it and do not choose for them:

```text
OQ-001: Does the intake bot accept direct messages? | Blocking: yes | Owner: Product owner | Decision needed: channel-only or channel-and-DM intake
```

## Hand Off

After emitting the inventory, hand the operator to `idea-to-prd` to draft the
PRD from the confirmed `DEC-*` records. Do not draft the PRD here.
