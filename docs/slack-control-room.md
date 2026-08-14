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
only from its active record:

```bash
python3 "$ARU_SDLC_HOME/scripts/slack_notify.py" \
  --project-id proj_... \
  --agent cursor-1 --family xai \
  --event blocked --issue 172 \
  --text "waiting on depends-on #110"
```

An unknown or closed project posts nothing. Slack downtime still returns a
warning without halting factory work. Deduplication includes `project_id`, so
identical events from different projects do not suppress one another.

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
- `stop <agent>` records `<canonical-path>::<agent>` and leaves peer projects
  and agents running.
- `resume` removes only the resolved project's path and agent tokens.
- `resume <agent>` removes only that project's agent token.
- `intervention issue #172 <decision>` or `intervention PR #123 <decision>`
  copies the decision to the resolved repository through the sanctioned GitHub
  comment path.

Slack does not expose a global factory command. `stop all` and `resume all`
are rejected. The existing `projects: ["*"]` contract is preserved for local
operator control, and Slack cannot clear it. A degraded project permits status
only; use local `verify`, `recover`, or `close` for recovery.

## Setup and security

1. Create the app from `templates/slack/manifest.yaml` and enable Socket Mode.
2. Create an app-level token with `connections:write` and install the app.
3. Invite the bot into each private project channel.
4. Store `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`,
   `SLACK_TEAM_ID`, and `SLACK_OPERATOR_USER_ID` in `~/.aru/slack.env` with
   mode `0600`.
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
