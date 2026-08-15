---
name: Feature Request
about: Suggest an idea or new feature for the project
title: 'feat: '
labels: 'feature'
assignees: ''
---

## Feature Description
<!-- Clear and concise description of what the feature is and why it is needed -->

## Acceptance Criteria
<!-- `verify:` commands in backticks (or an indented `verify:` line) are executed
     at merge by scripts/acceptance_runner.py in the PR checkout. Only python3/python
     -m unittest, relative tests|scripts|docs `*.py` paths, and pytest are allowed.
     Shell metacharacters are refused. Criteria without a command still use the checkbox. -->
- [ ] Predicate 1 (verify: `command to verify`)
- [ ] Predicate 2 (verify: `command to verify`)

## Decision Boundaries
<!-- Explicit defaults, edge cases, error paths, and thresholds -->
- Default:
- Edge cases:
- Error handling:

## Non-Goals
<!-- Explicit non-goals to bound agent improvisation -->
-

## Verification
<!-- Commands and steps to verify this feature -->

## Dependencies
depends-on: <!-- Issue numbers if any, e.g., #12, #14 -->
touches: <!-- Paths or globs touched by this issue -->
parallel-eligible: true <!-- Set to true if this issue can be implemented independently in parallel -->

## Technical Considerations
<!-- Any specific design preferences, architecture notes, or API contracts -->
