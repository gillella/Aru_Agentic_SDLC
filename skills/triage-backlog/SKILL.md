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

An external caller seeking new work selects eligible Ready issues first with
`fetch_next_work.py`. When no eligible Ready work is available, it may promote
one Backlog issue only within explicitly authorized scope:

`python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" --issue 42 --issue 43 --json`

Repeat `--issue` to name that scope. Only those issues are read as candidates;
there is no fallback to unrelated Backlog work. The default promotes at most
one eligible issue by priority and number; `--all` with these flags stays inside
the named subset. Scope selection is the caller's authorization responsibility,
not permission inferred from a complete issue body. Human-gated, epic, closed,
claimed, non-Backlog and blocked work stays in place. Promotion rechecks the
evaluated contract and dependencies immediately before the existing issue/Project
transition; observed drift refuses the write.

Re-read work after promotion and claim through the normal helper. A competing
claim requires fresh selection, never overriding another writer. An empty
eligible subset, pending independent approval and unreadable authority are
different outcomes; only the first means no eligible work in that scope.
Scheduling, continuation and meaningful-change alerts belong to the external
caller, not this skill or the Kernel.
