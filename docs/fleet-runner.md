# Durable factory runner

`scripts/run_fleet.py` supplies the process boundary that a chat session cannot.
It keeps one stable factory identity alive, checks GitHub for eligible work,
and launches a fresh local coding-agent session for one governed lifecycle
unit. Context exhaustion ends that child session; it does not end the worker.

## Architecture

```text
OS / fleet supervisor (#46, restart after death or reboot)
  └─ run_fleet.py loop (wait, retry, explicit-stop ownership)
       ├─ fleet_status.py (authoritative state)
       ├─ fetch_next_work.py without --claim (credit-free eligibility check)
       └─ finite local-agent child
            └─ picker --claim → skill → worktree → tests → PR/review/merge

GitHub Project Board = the only durable work queue
local state file      = process metadata only
```

The foreground runner does not claim, implement, review, or merge. Those
actions remain inside the governed child session. This separation lets a child
return on low context, a CLI error, rate limiting, or a temporary service
failure without losing the outer loop.

## Start one worker

Run from a trusted isolated clone, not a checkout containing unrelated user
work:

```bash
export ARU_SDLC_HOME=/absolute/path/to/Aru_Agentic_SDLC

python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . \
  --agent codex-1 \
  --family openai \
  --adapter codex
```

For Claude Code:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . \
  --agent claude-1 \
  --family anthropic \
  --adapter claude
```

The built-in Codex adapter uses `codex exec --full-auto`. The built-in Claude
adapter uses non-interactive print mode with automatic permissions. Repository
governance and local CLI permission settings still apply; the runner does not
weaken either one.

Another local CLI can be supplied as a JSON argv array. `{repo}` and
`{prompt}` are replaced as individual arguments, never evaluated by a shell:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . --agent local-1 --family other \
  --adapter-command-json '["my-agent","--cwd","{repo}","--prompt","{prompt}"]'
```

## Operate it

```bash
# One eligibility check and at most one child session
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" once \
  --repo . --agent codex-1 --family openai --adapter codex

# Process metadata; add --json for machine-readable output
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" status \
  --repo . --agent codex-1

# Drain the active child, then stop the foreground runner
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" stop \
  --repo . --agent codex-1
```

`SIGINT` and `SIGTERM` also request drain-first shutdown. A stop request does
not kill an active child because that could abandon a claim between GitHub and
local state changes.

## Liveness and credits

Loop mode intentionally remains alive in every authoritative factory state:

| State or event | Runner behavior |
|---|---|
| Eligible work | Launch one child immediately |
| Child succeeds | Re-read GitHub immediately |
| Same work remains visible | Wait before spending another child session |
| Idle / dependency / review / CI wait | Exponential backoff with jitter |
| Board complete | Park and watch for newly added work |
| Blocked product or merge state | Park and keep checking |
| GitHub/network error | Retry with bounded backoff |
| Stuck status or picker helper | Terminate after 120 seconds, then retry |
| CLI failure, rate limit, or unavailable credits | Keep the process alive and retry |
| Explicit stop or signal | Drain the child and exit |

The runner cannot query a vendor-neutral “credits remaining” API. Instead, a
non-zero local-agent exit is treated as temporary unavailability. It waits and
retries, so renewed subscription credits or a cleared rate limit are picked up
without an operator restart. The maximum wait defaults to 15 minutes and can be
changed with `--initial-wait` and `--max-wait`.
The helper timeout can be changed with `--helper-timeout`.

Runtime JSON lives under the platform state directory (normally
`~/.local/state/aru-factory/`). It stores only PID/control metadata, identity,
cycle counters, a state fingerprint, and retry timing. Agent prompts,
transcripts, tokens, and credentials are not stored.

## What is still needed for literal 24/7 operation

This issue provides a long-lived foreground worker. A terminal can still be
closed, the process can still be killed, and a laptop can sleep or reboot.
Issue #46 adds the second layer: launch and supervise several isolated workers,
restart dead processes, elect one janitor, preserve explicit stops, and install
an OS service where appropriate. Until #46 lands, run the foreground worker in
a persistent terminal multiplexer or service manager and keep the machine
awake and online.
