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
separately, stop state, and capability gaps. Opt-in `--enable-native-wake`
is **prepared/requested** only: it writes `native-wake.json` plus a Codex
prompt. `native_wake_enabled` is true only when that app has verified
configured/active vendor state (Codex `automation.toml` with
`status = "ACTIVE"`). Prompt-only preparation, a paused heartbeat, or an
Antigravity JSON flag without a bound `/goal` or `/schedule` is not
enabled. Full install-link diagnosis remains issue #34.

## Project-scoped presence (issue #191)

`scripts/agent_presence.py` records which agent **tasks** are present for a
project (`~/.aru/agent-presence.json`). Each registration binds one `--agent`
identity to exactly one project identity, with availability
(`available` / `busy` / `cooling-down` / `temporarily-offline` /
`unavailable` / `returned`), heartbeat, cooldown, capabilities, workload, and
supported wake evidence.

Presence is not ownership:

- GitHub `agent:` / claim labels remain authoritative.
- Heartbeat expiry moves availability to `temporarily-offline` without
  deleting the registration and without releasing or stealing a claim.
- The same desktop product may register separate tasks for different projects
  only by using **different** agent ids.
- Doctor reports presence and truthful vendor wake limitations read-only; it
  never launches agents or consumes paid wake usage.
- Presence falls back to a clone-independent `proj_repo_<hash>` derived from
  the GitHub repository node id and ProjectV2 board id when the checkout path
  is not yet listed in `projects.json`.
- Explicit `unregister` (or a distinct agent id) is required before the same
  agent id may bind to another project.
- Desktop tasks register and heartbeat via `scripts/agent_presence.py`
  (`register` / `heartbeat` / `set-availability`); `run_fleet.py` is optional
  headless only.
- Never post presence heartbeats to Slack.

## Credit cooldown, takeover, and return

Credit exhaustion, provider rate limits, provider outages, and failed child
sessions reduce only that task's project capacity. The task records
`cooling-down`, a classified `cooldown_reason`, its last heartbeat, and a local
next-probe time when known. `query_role_poll_agents()` excludes cooling tasks
from new non-claiming role polls, while available peers continue. The optional
headless runner caps a cooldown eligibility probe at five minutes.

Retry ticks and presence heartbeats stay local. A cooldown entry and a
successful return each produce at most one concise project-channel
`availability` transition; Slack remains discussion, not a task queue. Doctor
shows the cooldown reason and retry time from the same presence record.

Claim protection is configurable through the advisory warning and takeover
windows. Passing the takeover time is necessary but insufficient. A caller
must also provide affirmative evidence that no process is live, no branch has
recent activity, and the work is explicitly resumable. Missing or unknown
evidence fails closed. The evaluator never releases, transfers, or reclaims a
GitHub claim.

After a cooldown, the runner queries GitHub again before launching any child.
Only a successful child session marks the task returned; it cannot reuse the
old work number or reclaim work transferred while it was cooling. A provider's
unknown reset time remains unknown—the local five-minute probe is not a claim
that credits will recover then.

Unsupported same-task wake remains unavailable until the operator explicitly
reopens that task. A new scheduled session may recover board state, but must
not be presented as the original task returning. Aru does not purchase credits,
bypass provider limits, or guarantee recovery across app or machine restarts.
