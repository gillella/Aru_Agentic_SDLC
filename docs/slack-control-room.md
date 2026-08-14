# Slack factory control room

GitHub is the work queue. Slack is the discussion and alert plane for one
project. Cursor, Claude, Codex, and Antigravity do **not** join as Slack
users. They share **one** bot (Aru Code App / Aru Factory) in a single
allowlisted channel. Messages stamp `agent`, `family`, repo, issue/PR, and
time.

Live operator channel for this repo: private `#project-aru-code` on Anguliyam
(`C0BPZMRR1RC`). Tokens stay in `~/.aru/slack.env`, never in git.

## What each desktop agent does

After a work unit, an agent may post (never instead of GitHub helpers):

```bash
python3 "$ARU_SDLC_HOME/scripts/slack_notify.py" \
  --agent cursor-1 --family xai \
  --event blocked --issue 172 \
  --text "waiting on depends-on #110"
```

Exit code is 0 even when Slack is down. The factory loop continues.

## Bridge process

```bash
pip install -r requirements-slack.txt   # optional; live Socket Mode only
python3 scripts/slack_control_room.py doctor
python3 scripts/slack_control_room.py start --repo-dir /path/to/repo
python3 scripts/slack_control_room.py status --repo-dir /path/to/repo
python3 scripts/slack_control_room.py stop
```

`start` uses Slack Socket Mode (no public HTTP URL). `stop` stops the **bridge**,
not the factory. Factory stop/resume are Slack **commands**.

## Operator commands (allowlisted user, allowlisted channel)

Mention the bot:

- `status` — read-only `fleet_status.py`
- `stop` / `stop all` — write `~/.aru/factory-loop.stop` (drain-first)
- `resume` / `resume all` — clear that operator stop only
- `intervention #172 approved` — copy the decision onto the GitHub issue

Events from other workspaces, channels, users, or bots are ignored.

## Setup

1. Create the app from `templates/slack/manifest.yaml` (or add the listed
   bot scopes and reinstall).
2. Enable Socket Mode. Create an app-level token with `connections:write`.
3. Install to the workspace. `/invite` the bot into the private channel.
4. Store `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`,
   `SLACK_TEAM_ID`, `SLACK_CHANNEL_ID`, and optionally
   `SLACK_OPERATOR_USER_ID` in `~/.aru/slack.env` (`chmod 600`).

Revoke tokens in the Slack app dashboard, then delete `~/.aru/slack.env`.

## Threat model

- Tokens never logged or posted. Outbound text is redacted for `xoxb-` / `xapp-`.
- Slack cannot claim, review, or merge. Intervention is a GitHub comment.
- Duplicate Slack deliveries are ignored (`client_msg_id` / `ts`).
- If Slack is down, notify returns a warning and the GitHub loop continues.
- On reconnect, the bridge does not replay stop/resume; it handles new events.

## Recovery

`doctor` reports missing env, missing bolt, and whether the bridge pid is live.
If the bridge dies, factory work is unaffected. Restart `start`.
