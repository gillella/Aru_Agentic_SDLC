# Desktop factory continuity and optional headless runner

The primary Aru workflow runs inside the desktop coding task the operator
started after selecting a project in Codex, Claude, Cursor, or Antigravity.
`aru code loop`, `run the factory`, and `keep going` mean that this current
task repeatedly works the governed GitHub board until the operator stops it or
a specific human decision/approval is required.

## Primary desktop architecture

```text
operator selects a project and starts a task in a desktop app
  └─ current desktop task runs the Aru loop
       ├─ fetch_next_work.py --claim
       ├─ governed skill for one work unit
       ├─ worktree → tests → PR/review/merge
       └─ ask the picker again; wait/retry when temporarily idle

GitHub Project Board = durable queue and lifecycle state
desktop task         = execution and user interaction boundary
```

The loop does not send a final response merely because one unit completed, the
picker is idle, the board is Complete, work is waiting on CI/review/dependencies,
another agent won a race, or a tool/network/credit limit is temporary. It uses
dynamic backoff and continues in the same task while the desktop product keeps
that task runnable.

Context compaction is also a continuity event. GitHub claims, branches, and
worktrees contain the durable truth; after compaction the task recovers its
stable agent identity and asks the picker again.

## What Aru can and cannot guarantee

The repository can define loop behavior and install skills, commands, and thin
native adapters. It cannot override a vendor-enforced task termination, invent
credits, keep a powered-off Mac running, or resume an arbitrary GUI conversation
through a universal API that does not exist.

Never use AppleScript, accessibility clicking, keystroke injection, or screen
coordinates to keep desktop agents alive. Such UI automation can target the
wrong project, bypass an approval, or mutate the wrong worktree.

Capability levels must remain distinct:

| Capability | Meaning |
|---|---|
| Active-task loop | The currently running desktop task continues to pick work |
| Native wake | The application can schedule or trigger the same configured task/project |
| App restart recovery | Work resumes after the application relaunches |
| Machine restart recovery | Work resumes after reboot/login |

As of the current design, Codex documents background/thread automations and
Antigravity documents goals, workflows, and scheduled tasks. Their native
features may be configured in #46 as project-scoped wake adapters. Claude and
Cursor must use only capabilities documented by their installed desktop
versions; absence of a supported same-task wake is reported as a gap, not
silently replaced with a CLI process.

App-native schedules are optional wake mechanisms. They do not become a new
work queue: every wake recovers and selects work from GitHub.

## Intentional stop and human intervention

The desktop loop intentionally ends only when:

- the operator says stop, cancels the task, or disables its native wake; or
- an unresolved product, security, money, external-contract, approval, or
  hard-governance decision genuinely requires a human.

Routine idle, CI, review, dependency, transient error, context, and retry states
do not meet that bar.

## Optional headless CLI mode

`scripts/run_fleet.py` is retained for an operator who explicitly chooses a
headless local-agent deployment. It is not invoked by default from a desktop
task and cannot continue the conversation visible in a desktop application.

From a trusted isolated clone:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . --agent codex-1 --family openai --adapter codex
```

For Claude Code CLI:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . --agent claude-1 --family anthropic --adapter claude
```

Another CLI can be supplied as a JSON argv array. `{repo}` and `{prompt}` are
replaced as individual arguments, never evaluated by a shell:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . --agent local-1 --family other \
  --adapter-command-json '["my-agent","--cwd","{repo}","--prompt","{prompt}"]'
```

Headless operation provides `once`, `loop`, `status`, and drain-first `stop`.
It checks eligibility before launching a paid CLI child, isolates the child
from runner stop signals, retries recoverable failures with bounded backoff,
and stores process metadata—but not prompts, transcripts, tokens, credentials,
or a second work queue—under the platform state directory.

## Follow-on desktop adapters

Issue #46 installs and verifies the thinnest supported native continuity
adapter for each desktop application. It must preserve the project the operator
selected, persist explicit-stop intent, report capability gaps truthfully, and
never substitute an OS daemon that drives the GUI.
