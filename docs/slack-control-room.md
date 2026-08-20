# Slack factory control room

GitHub remains the work queue. One local Slack bridge can route several Aru
projects, with one private channel bound to each project. Cursor, Claude,
Codex, and Antigravity do not join as separate Slack users; messages use one
bot and stamp the agent, model family, project, issue or PR, and time.

Credentials stay in `~/.aru/slack.env`. Project routing lives separately in
`~/.aru/projects.json`. The registry never stores bot tokens, app tokens,
signing secrets, or other credentials.

## Registry model

Every record has an immutable generated `project_id` and immutable GitHub
repository and ProjectV2 identities. The repository slug and canonical local
checkout path are recoverable pointers. The Slack team and channel form a
reserved route. A record's lifecycle is `active` or `closed`; a missing local
checkout is runtime health `degraded_unreachable`, not a lifecycle change.

The registry is versioned and enforces:

- exactly one active project for an inbound team and channel;
- no reuse of a channel after its record is closed;
- private `0700` parent directories and `0600` JSON and lock files; an
  owner-controlled legacy `0755` `~/.aru` directory is tightened automatically,
  while foreign-owned or group/world-writable paths fail closed;
- lock-protected read-modify-write and same-directory atomic replacement;
- fail-closed reads for corrupt, insecure, non-regular, or symlinked files.

Create and inspect records with the local registry CLI:

```bash
python3 scripts/slack_projects.py create \
  --local-path /absolute/path/to/repo \
  --team-id T01234567 --channel-id C01234567 \
  --operator aravind

python3 scripts/slack_projects.py list
python3 scripts/slack_projects.py resolve \
  --team-id T01234567 --channel-id C01234567
python3 scripts/slack_projects.py verify --project-id proj_...
```

`create` discovers and verifies the repository and governed ProjectV2 board
through GitHub. Save the generated `project_id`; outbound notifications and
local status checks require it explicitly.

If a checkout moves or a repository is renamed, recover only the mutable
pointers. Recovery refuses a checkout whose GitHub repository or ProjectV2
identity differs from the immutable record:

```bash
python3 scripts/slack_projects.py recover \
  --project-id proj_... --local-path /new/absolute/path \
  --repo-slug owner/new-name --operator aravind
```

Close an obsolete record locally. Closing preserves its channel reservation
and immediately disables inbound and outbound routing:

```bash
python3 scripts/slack_projects.py close \
  --project-id proj_... --operator aravind
```

## Migrating the legacy singleton

The explicit migration reads the old team and channel from
`~/.aru/slack.env`, verifies the checkout identity, and imports the binding
once. Re-running it returns the same project. It does not copy credentials.

```bash
python3 scripts/slack_projects.py migrate \
  --local-path /absolute/path/to/repo --operator aravind
```

After migration, `SLACK_CHANNEL_ID` is no longer a routing fallback. It may be
removed once all callers provide a project ID. `SLACK_TEAM_ID` still identifies
the workspace authorized for the single credential set in this first version.

## Outbound notifications

Every notification must name a registry project. The destination channel comes
only from its active record. Write the concise alert summary to an
operator-owned `0600` file using a non-shell file-writing mechanism and set
`ARU_ALERT_TEXT_FILE` to that path before invoking the helper:

```bash
python3 "$ARU_SDLC_HOME/scripts/slack_notify.py" \
  --project-id proj_... \
  --agent cursor-1 --family xai \
  --event blocked --issue 172 \
  --text-file "$ARU_ALERT_TEXT_FILE"
```

An unknown or closed project posts nothing. A required GitHub issue/PR comment
must succeed before Slack is attempted; partial GitHub delivery is tracked per
target and remains retryable. Slack downtime still returns a warning without
halting factory work, and writes a secret-safe structured retry record under
`~/.aru/slack-notify-audit.json`. A later successful retry records recovery.
Deduplication includes `project_id`, so identical events from different
projects do not suppress one another. Alert events persist delivery keys under
`~/.aru/slack-notify-dedupe.json` so the same completed blocker is not re-posted
after a process restart.

### Alert events (`blocked`, `waiting-on`, `hitl`)

Factory agents use three alert kinds. Each alert posts a durable GitHub
issue/PR comment with the same facts **before** the Slack message:

| Event | When | Slack extras |
|---|---|---|
| `blocked` | unresolved `depends-on`, missing product decision, merge/close-out stuck | stamped identity + reason |
| `waiting-on` | peer holds a claim, review, or overlapping `touches:` path | names `--waiting-on-agent` and the peer issue/PR; never steals the claim |
| `hitl` | severe merge/close-out failure, exhausted credits, or an unresolvable decision | mentions the validated operator (`SLACK_OPERATOR_USER_ID`) and, when `SLACK_ESCALATION_USER_ID` is set, Hermes' war-room bot; agents still stop per `AGENTS.md` |

## Escalation to Hermes (mention the war-room bot)

The factory posts through one bot (`@aru_code_app`), and Hermes is a *separate*
bot (`@hermes-war-room`, see `templates/slack/manifest-hermes-war-room.yaml`).
A Slack bot cannot wake itself, so material events must mention Hermes' bot,
never the factory bot.

Configure `SLACK_ESCALATION_USER_ID` (Hermes' bot user id, e.g. `U0BRH53KR51`)
in `~/.aru/slack.env`. `blocked` and `hitl` events then prepend a runtime
mention of that id — `<@U0BRH53KR51> BLOCKED — escalate to Hermes` — so Hermes
wakes and escalates the decision to Telegram. `waiting-on` stays mention-free
(routine peer-claim chatter). Leave the var unset to keep legacy behavior (no
escalation mention).

For the examples below, prepare `ARU_ALERT_TEXT_FILE` or
`ARU_ALERT_DECISION_FILE` with the same secure file procedure. The registry
record is authoritative for both repository slug and checkout path; `--repo`
and `--repo-dir` cannot redirect an alert. `--project-id` is optional: when
omitted, `slack_notify.py` resolves the binding from `--repo-dir` via
`~/.aru/projects.json`, so any factory agent can notify from a bound checkout
with no per-agent Slack setup. An unbound checkout fails closed with a clear
warning (bind it once with `slack_projects.py migrate --local-path <repo>
--operator <you>`).

```bash
python3 "$ARU_SDLC_HOME/scripts/slack_notify.py" \
  --project-id proj_... \
  --agent cursor-1 --family other \
  --event waiting-on --repo gillella/Aru_Agentic_SDLC --issue 181 \
  --waiting-on-agent claude-1 --waiting-on-issue 163 \
  --repo-dir . \
  --text-file "$ARU_ALERT_TEXT_FILE"

python3 "$ARU_SDLC_HOME/scripts/slack_notify.py" \
  --project-id proj_... \
  --agent cursor-1 --family other \
  --event hitl --repo gillella/Aru_Agentic_SDLC --pr 170 \
  --repo-dir . \
  --decision-file "$ARU_ALERT_DECISION_FILE"
```

Do **not** post heartbeats, diffs, prompts, tokens, or test logs. The helper
rejects those event types. A Slack reply cannot claim, review, or merge —
inbound `claim` / `merge` / `review` verbs are refused; only GitHub helpers
mutate work state.

## Bridge process

```bash
pip install -r requirements-slack.txt   # live Socket Mode only
python3 scripts/slack_control_room.py doctor
python3 scripts/slack_control_room.py start
python3 scripts/slack_control_room.py status --project-id proj_...
python3 scripts/slack_control_room.py stop
```

`start` runs a single Socket Mode bridge that serves every active registry
record. `stop` stops only the bridge process, never a factory loop.

For each inbound mention, the bridge resolves exactly one active project
before authorization, deduplication, command parsing, filesystem mutation,
GitHub access, or Slack acknowledgement. Unknown, ambiguous, and closed routes
produce no Slack reply and no remote or project side effect. They create only
a throttled local audit entry in `~/.aru/slack-audit.json`.

Before a resolved command can inspect or mutate a checkout, the bridge also
re-verifies its immutable GitHub repository and ProjectV2 identities. A path
that has been reused for another checkout is treated as degraded until an
operator recovers or closes the record.

## Operator commands

Mention the bot and put the verb first:

- `status` reports only the resolved project. A missing checkout reports
  `degraded_unreachable` without attempting repository or GitHub work.
- `stop` records the resolved project's canonical checkout path in
  `~/.aru/factory-loop.stop`.
- `resume` removes only the resolved project's path and agent tokens.
- `intervention issue #172 <decision>` or `intervention PR #123 <decision>`
  copies the decision to the resolved repository through the sanctioned GitHub
  comment path.

### Sprint decisions

Agents can recommend scope, but these commands are accepted only from the
configured operator in the resolved project channel:

```text
sprint authorize control #300 issues #180,#205 baseline <full-commit-sha>
sprint revise <increment-id> issues #180,#205
sprint start <increment-id>
sprint accept <increment-id>
sprint accept <increment-id> risk-accepted
sprint authorize-deployment <increment-id>
sprint deployed <increment-id>
sprint cancel <increment-id>
```

Append `emergency` to `sprint authorize` to create a separate emergency
increment. The control issue is the durable GitHub anchor. Every decision is
posted there as structured `aru.delivery-decision.v1` JSON before the private
increment registry changes. If that post fails, the transition remains paused
and the same Slack event can be retried. Duplicate successful events are
idempotent across bridge restarts. Authorization also verifies that the full
baseline SHA resolves to a commit in the project checkout before it records
anything.

`accept` records sprint acceptance only. It does not tag, release, authorize
deployment, or deploy. `authorize-deployment` and `deployed` are distinct
operator decisions; this implementation records those decisions but does not
run a deployment command.

Slack does not expose global or agent-scoped factory commands in V1. `stop all`,
`resume all`, and agent targets are rejected. The existing `projects: ["*"]`
contract is preserved for local operator control, and Slack cannot clear it. A
degraded project permits status only; use local `verify`, `recover`, or `close`
for recovery.

## Epic splits and Slack brainstorms

Slack is the conversation. The Project Board is the resulting work. Any factory
agent may start a thread on an open `type:epic` to propose a split. That thread
is **not** a claim, a `depends-on` resolution, or merge authority — including a
thumbs-up.

File agreed slices only through a structured 0600 JSON document and the helper.
Do not paste secrets into the document or into Slack.

```bash
python3 "$ARU_SDLC_HOME/scripts/slack_control_room.py" file-split \
  --epic 178 \
  --from-file /path/to/split.json \
  --repo-dir /absolute/path/to/repo \
  --dry-run
```

Omit `--dry-run` to create GitHub issues. Each child must declare `touches`,
`parallel-eligible`, and `depends-on` for **other** issues only — never the
still-open parent epic (that deadlocks the picker). Link the parent with
`Epic: #<epic>`. The helper attaches them with
`update_issue_status.py --require-board` (Ready only when the rendered body
meets the Ready contract: machine-checkable criteria plus Verification,
Decision Boundaries, and Non-Goals; otherwise Backlog), comments the issue
URLs on the epic, and posts them back to the Slack thread when `--thread-ts`
is set.

Brainstorm in Slack when the epic still needs slicing or a missing product
decision. If the decision cannot be derived from the epic, `@` the operator
(`hitl`) and stop. If the slices are already obvious, skip Slack and file with
`create-github-issue` / this helper.

Agents still pick work only through `fetch_next_work.py`.

## Setup and security

1. Create the app from `templates/slack/manifest.yaml` and enable Socket Mode.
2. Create an app-level token with `connections:write` and install the app.
3. Invite the bot into each private project channel.
4. Store `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`,
   `SLACK_TEAM_ID`, `SLACK_OPERATOR_USER_ID`, and (for Hermes escalation)
   `SLACK_ESCALATION_USER_ID` in `~/.aru/slack.env` with mode `0600`.
5. Create or migrate one registry record per project and run `doctor`.

The bridge authorizes only the configured operator user in the record's Slack
workspace and channel. Outbound text redacts credential-shaped data and the
HTTP client refuses redirects. Duplicate inbound deliveries persist as
`project_id|team|channel|event` identities in
`~/.aru/slack-control-room-seen.json`, so restarts do not replay commands and
the same Slack event ID cannot collide across projects.

If Slack or the bridge is unavailable, GitHub-governed factory work continues.
Revoke credentials in the Slack app dashboard before deleting
`~/.aru/slack.env`.
