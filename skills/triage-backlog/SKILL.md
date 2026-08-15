---
name: triage-backlog
description: Promotes Backlog issues to Ready by verifying the Ready contract, fills the gaps a script cannot, and reports how many parallel agents the board can actually keep busy. Use when the user says triage the backlog, promote issues, prepare the board, size the fleet, or before launching parallel agents.
triggers:
  - "triage the backlog"
  - "promote issues to ready"
  - "prepare the board"
  - "how many agents can I run"
  - "before launching the fleet"
do_not_trigger_for:
  - "creating a new issue (use create-github-issue instead)"
  - "implementing a Ready issue (use implement-next-issue instead)"
---

# Backlog Triage Procedure

Triage is where a human's judgment has the highest leverage per minute, and it
is the throughput cap on any fleet: `fetch_next_issue.py` cannot hand out a
Backlog issue, so agents idle the moment the Ready column empties.

The script verifies the mechanical half. This skill covers the half it cannot.

---

## Step 1: Read the board

```
python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py"
```

Output separates Backlog issues that satisfy the Ready contract from those
blocked, with the specific missing element per issue, and ends with a fleet
capacity number.

## Step 2: Fill the gaps the script reports

The script can only detect *absence*. For each blocked issue, supply what is
missing — and this is judgment work, not formatting work:

| Reported gap | What you actually have to decide |
|---|---|
| no acceptance criteria checkboxes | What observable change means this is done? Write criteria a reviewer can check without reading your mind. |
| no verification section | The exact command and expected result. If you cannot name one, the issue is not ready — say so on the issue. |
| no `touches:` declaration | Every path the work may modify. **Over-declare rather than under-declare**: an under-declaration lets the picker run two colliding issues in parallel, which is the worst failure this framework has. |
| depends-on still open | Is it a real dependency, or did the author link a related issue? Remove false dependencies; they serialize the fleet for nothing. |
| is an epic | Epics are containers. Split into implementable children instead of promoting. |

Issues that satisfy the Ready contract can still receive a `SPLIT`
recommendation. The script flags either visible oversize signal: more than 8
acceptance-criteria checkboxes **or** `touches:` paths spanning more than one
top-level area. A wildcard-bearing first component such as `**/*.py` or
`*/config.yml` is inherently wide because it can match multiple top-level
areas. Leading `./` is normalized, root-level files share one `<root>` area,
bare names such as `scripts` retain their possible directory-prefix meaning,
and an explicit whole-repository declaration (`.`, `./`, or `/`) is always
held for splitting. The output prints the observed checkbox count, area names,
or wildcard roots so the scope can be decomposed deliberately. Epics remain
blocked by the Ready contract rather than entering this overrideable
recommendation path.

## Step 3: Judge readiness beyond the contract

The contract is necessary, not sufficient. Before promoting, ask:

1. **Is the design settled?** If the hard part is *what to build* rather than
   *how to type it*, label it `needs-design` so the plan gate fires before any
   code is written. Promoting an unsettled issue converts a design question
   into a large PR nobody can review.
2. **Is it one issue?** If the acceptance criteria span two subsystems, the
   `touches:` declaration will be wide, and a wide declaration blocks other
   agents. Split it.
3. **Does it collide with in-flight work?** The capacity report shows this.
   Deliberately hold an issue back rather than let an agent claim work that
   will conflict at merge.
4. **Is the phase right?** An issue promoted out of phase order pulls the fleet
   away from the critical path.

## Step 4: Promote

```
python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" --promote
python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" --promote --issue 24 --issue 25
python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" --promote --force
```

`--promote` skips `SPLIT` recommendations. `--force` explicitly overrides only
that recommendation; it never overrides missing Ready-contract elements or
the rule that epics are unpromotable.

## Step 5: Size the fleet from the capacity number, not the Ready count

```
python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" --capacity
```

Two Ready issues whose `touches:` overlap cannot run at the same time. Launch
at most the reported concurrent count — extra agents will claim nothing, burn
tokens on session startup, and clutter the board with reap cycles.

---

## When to run this

- Before every fleet launch. Always.
- After a batch of issues is filed from a planning session.
- When the capacity report drops to zero or one and agents start idling.
