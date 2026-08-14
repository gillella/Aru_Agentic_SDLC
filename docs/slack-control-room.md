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

Mention the bot, then put the verb first (`<@bot> status`, not a sentence
that happens to contain `stop`):

- `status` — read-only fleet state plus capacity, open review work, active
  claims, and each configured desktop loop's last heartbeat (unknown when
  the continuity doctor has no timestamp)
- `stop` / `stop all` — write `~/.aru/factory-loop.stop` with `projects: ["*"]`
- `stop <agent>` — record `agents: ["<project>::<agent>"]` so a peer agent in
  the same project keeps running
- `resume` / `resume all` — clear that operator stop only
- `resume <agent>` — clear only that agent's token; refused while `*` or a
  project-wide stop is in effect
- `intervention #172 approved` — copy the decision onto the GitHub issue
  in `--repo-dir` with `gh issue comment` / `gh pr comment` (no comment
  helper exists; that is the governed direct-comment path)

A scoped `resume <target>` is rejected while a global `*` stop is in effect.
Events from other workspaces, channels, users, or bots are ignored. The
bridge refuses to start, and commands fail closed, unless
`SLACK_OPERATOR_USER_ID` is set.

## Setup

1. Create the app from `templates/slack/manifest.yaml` (or add the listed
   bot scopes and reinstall).
2. Enable Socket Mode. Create an app-level token with `connections:write`.
3. Install to the workspace. `/invite` the bot into the private channel.
4. Store `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`,
   `SLACK_TEAM_ID`, `SLACK_CHANNEL_ID`, and `SLACK_OPERATOR_USER_ID`
   in `~/.aru/slack.env` (`chmod 600`). The operator id is required for
   the bridge; notify-only posting can omit it.

Revoke tokens in the Slack app dashboard, then delete `~/.aru/slack.env`.

## Threat model

- Tokens never logged or posted. Outbound text is redacted for `xoxb-` / `xapp-`.
- Notify does not follow HTTP redirects, so the bot token cannot leave Slack.
- The runtime manifest requests only `app_mentions:read` and `chat:write`.
- Slack cannot claim, review, or merge. Intervention is a GitHub comment.
- Duplicate Slack deliveries are ignored (`client_msg_id` / `ts`) and the
  processed ids persist in `~/.aru/slack-control-room-seen.json` so a bridge
  restart cannot replay `stop` / `resume` / `intervention`.
- `stop` of the bridge process signals only a live PID whose command line
  contains `slack_control_room`; a stale file pointing at another process is
  refused.
- If Slack is down, notify returns a warning and the GitHub loop continues.
- On reconnect, the bridge does not replay stop/resume; it handles new events.

## Recovery

`doctor` reports missing env, missing bolt, and whether the bridge pid is live.
If the bridge dies, factory work is unaffected. Restart `start`. A loop should
stop when `factory-loop.stop` contains `*`, this project path, or
`<project>::<agent-id>`.
