# Continuation diagnosis and source acceptance

Read-only operational inspection on 2026-09-05, approximately 09:00–09:08
America/New_York, observed Factory source `44da258dd547dbbbf9e7d78e092c360b07ab96f8`.
These are historical observations, not a claim about the deployed state after
this source change.

- The installed Project Driver was a Markdown skill directory outside Git.
  Its Loop instructions ended on `idle`/`wait` and required a new activation
  after the first merge. They prohibited the requested standing heartbeat.
- None of the seven enabled main-profile Hermes jobs was a recurring Project
  Driver check. Recent Factory continuations were completed one-shots.
- The current kernel picker accepted one agent and selected Ready issues.
  Backlog promotion was a separate helper. Installed Driver instructions still
  referred to older batch and review-policy behavior.
- The Factory webhook endpoint accepted events, but the loaded forwarding
  service had no running process and ended with a connection-closed error.
  That confirms a forwarding gap; it does not prove every event route failed.
- Worker processes existed in Factory and Trading worktrees. The finding was
  missing reliable continuation and refill, not a claim that all agents were idle.
- No authenticated end-to-end canary was sent. Receiver/listener presence did
  not establish delivery, worker dispatch, subscription quota or live recovery.

Aravind approved the hybrid design and consolidated implementation. Source
ownership is now this repository's separately installed `integrations/hermes/`
adapter, tracked by #555 and #556. Generic cron and HMAC ingress stay in Hermes;
the kernel helpers retain all lifecycle and merge authority.

The regression suite covers multiple independent coding accounts, reserved
paths, opt-in Backlog promotion, quota/CI admission, recovery after a claim or
worker failure, identical actionable retries, Stop, duplicate events, review
deadlines, and detached worker lock ownership. Handoff tests additionally prove
source PR/issue/head binding, route authorization, target board/capacity checks,
delivery failure and restart deduplication, all-of dependency proofs, immutable
artifact comparison, and heartbeat discovery of both handoff and return actions.
Independent work remains eligible while a source issue is held by its dependency.

The [rollout handoff](README.md#rollout-handoff) maps this source to #557. That
issue remains open until approved live installation, canary dispatch, actual
authenticated delivery, restart and Stop evidence have been recorded. Neither
this audit nor passing source tests establishes deployed continuous operation.
