# Desktop-native continuity adapters

This is the capability model for keeping an Aru factory loop alive inside the
desktop coding application the operator started. GitHub remains the work queue.
The current desktop task remains the execution owner.

Install or repair adapters from the playbook:

```bash
"$ARU_SDLC_HOME/scripts/install_local_agent_integrations.sh"
"$ARU_SDLC_HOME/scripts/install_local_agent_integrations.sh" \
  --enable-native-wake --project /absolute/path/to/repo
"$ARU_SDLC_HOME/scripts/install_local_agent_integrations.sh" \
  --stop-loop --project /absolute/path/to/repo
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py" --json \
  --project /absolute/path/to/repo
```

Machine-readable catalog: `templates/integrations/continuity.json`.

## Capability levels

| Level | Meaning |
|---|---|
| Active-task loop | The currently running desktop task continues to pick work |
| Same-task native wake | The application can schedule a return to **this** conversation |
| GitHub-recovery wake | A **new** session/agent recovers from the Project Board |
| App restart recovery | Work resumes after the application relaunches |
| Machine restart recovery | Work resumes after reboot/login |

App quit, logout, machine sleep, power-off, exhausted credits, and
vendor-enforced termination are **not** software guarantees.

Never use AppleScript, accessibility clicks, keystroke injection, or screen
coordinates. Never replace the desktop task with `scripts/run_fleet.py`.

## Per application

| App | Active-task loop | Same-task native wake | App/machine restart |
|---|---|---|---|
| Codex | yes | opt-in **thread** automation (`kind=heartbeat`) bound to the current task | no |
| Antigravity | yes | `/goal` in the current task; `/schedule` is a new project-scoped agent | no |
| Claude | yes | `/loop` while the session lives. Desktop scheduled tasks start a **fresh** session | no |
| Cursor | yes | Cursor `loop` skill while the session lives. Cursor Automations start a **new** agent | no |

Codex standalone automations and Claude Desktop scheduled tasks may recover
from GitHub in a new session. They must not be labelled same-task wake.

`--enable-native-wake --project <abs-path>` records that opt-in for **that
project only** and writes a Codex thread-automation prompt under
`~/.codex/automations/aru-code-loop-<sha256(project)[:12]>/`. It does not
invent a `target_thread_id`. Bind the heartbeat from the current Codex
task. Sibling automations under `~/.codex/automations/` are never modified.
`--disable-native-wake` pauses that project's managed heartbeat before
dropping the JSON entry. `--dry-run` prints the same actions and writes
nothing.

## Explicit stop

`$HOME/.aru/factory-loop.stop` is durable operator intent. `/stop-aru-loop`
writes it; `/resume-aru-loop` clears it. A stop without `--project` stores
`*`. Project-scoped `--resume-loop --project` refuses while `*` applies —
clear the global stop without `--project`. `--resume-loop` reactivates a
managed Codex heartbeat only when that project's `native-wake.json` entry is
still `enabled`; `--disable-native-wake` therefore survives a later resume.
While a stop applies to a project, loop mode must not continue and must not
arm native wakes. If a managed Codex heartbeat (`id = "aru-code-loop"` or
`id = "aru-code-loop-<12 hex>"`) exists for that project, stop pauses
**that file only**.

## Doctor

`doctor_local_agent_integrations.py` reports continuity adapters, macOS
`.app` bundle versions (Info.plist only, no credentials), CLI/config evidence
separately, stop state, and capability gaps. Full install-link diagnosis
remains issue #34.
