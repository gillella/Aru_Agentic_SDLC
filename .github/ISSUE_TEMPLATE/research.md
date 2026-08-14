---
name: Research
about: Bounded research question that produces a cited findings artifact
title: 'research: '
labels: 'type:research'
assignees: ''
---

## Research Question
<!-- One bounded question. Not a vague "look into X". -->

## Scope Bounds
<!-- What is in scope / out of scope. Timebox if known. -->
- In scope:
- Out of scope:

## Acceptance Criteria
- [ ] Findings artifact attached to this issue (verify: artifact comment or `docs/research/` path present)
- [ ] Every citation carries a resolvable identifier (verify: `python3 scripts/verify_citations.py <artifact>`)
- [ ] Citation resolution passes mechanically (verify: same command exits 0)
- [ ] Claims about this repository's code include a verification date (verify: verifier repo-claim check)
- [ ] Follow-on issues proposed when findings warrant them (`depends-on: #<this>`)

## Dependencies
depends-on: <!-- none, or #N -->
touches: docs/research/**, <!-- or state: issue comment attachment only -->
parallel-eligible: true

## Notes
Research is Done when the artifact exists and `verify_citations.py` exits 0 — not when the agent believes the question is answered.
