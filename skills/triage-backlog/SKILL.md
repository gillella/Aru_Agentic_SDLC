---
name: triage-backlog
description: Validate Backlog issues and promote only complete, unblocked work to Ready.
---

# Triage

Run `python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py"`.

The command promotes at most one issue by default. It must refuse an issue
unless:

- `## Acceptance Criteria` contains an unchecked item;
- exactly one safe `touches:` declaration exists;
- every `depends-on: #N` issue is closed;
- GitHub and Project Board state are readable.

Use `--all` only when an operator deliberately wants multiple Ready issues.
Never infer that unavailable board state means an empty backlog.
