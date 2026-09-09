# Driver wiring handoff (#631)

Keep this package next to the existing `aru_project_driver` source in the later
source rollout. Import it as `integrations.personas` from the checkout root, or
as `personas` with the checkout's `integrations` directory on `PYTHONPATH`.
No pip dependency, installation hook or runtime write is needed for #630.

1. At the existing bounded activation, reread the live issue/claim/scope and
   actual Git branch/worktree/head. Derive task family from trusted structured
   operator/issue metadata; do not turn arbitrary issue prose into routing flags.
2. Build `FleetBinding` from the existing account configuration and authenticated
   probe receipts. Keep one account/capacity identity across model variants.
   Reject the old non-Unum projects on Claude subscription 4. Verify executable
   paths and auth-profile ownership against operator configuration. Read help or
   version evidence after a CLI change; do not assume the recorded surface forever.
3. Under the existing shared capacity coordination lock, refresh occupied and
   unusable accounts, resolve one approved candidate, then reserve the returned
   capacity key atomically. An earlier unlocked plan is diagnostic only. Existing
   account-wide session-limit evidence skips that account without inference or
   repeated probe attempts. No independent picker or launcher is added.
4. Carry **all** prior author identities from trusted issue/PR/handoff history.
   On an authorized family change, append the selected contributor and preserve
   the previous contributors in operational evidence. This evidence is input to
   existing kernel authority, never a second lifecycle state. #630 history is
   Claude subscription 1 → Claude subscription 3 → Codex Astra; neither model
   family is an independent reviewer for it.
5. For review, the existing `kernel.py` seam rereads the canonical sole authority
   and its head. Map that assignment to `ReviewAssignment` and pass the freshly
   reread value separately as `current_assignment`. Both must match. Supply the
   complete authors and scope; bind `PromptContext.pr/head` to the same values.
   Do not call ordinary `resolve` or permit account/model fallback for review.
6. Compile immediately before dispatch. If transporting a payload, compare it
   with that separately resolved expected plan using `verify_payload`. Dispatch
   exactly `argv` as an argument array, `cwd=workspace`, and an explicit clean
   environment containing the plan's account env. Avoid inherited API keys,
   CLI model overrides, profiles or alternate account routes. The existing
   `execution.py` supervisor owns launch, receipts, exit handling and wakes.
7. Provider failure after launch produces a real failure receipt. Reread authority
   and capacity through the existing continuation path before resolving another
   plan. Do not edit effort/model in an old plan or assume its hash proves access.
   Only kernel helpers may change a review assignment. If all qualified candidates
   fail, report structured skips to the existing owner/continuation mechanism.

Existing seams inspected: `aru_project_driver/config.py` lane validation,
`kernel.py` authority reads, `capacity.py` shared reservations and `execution.py`
argument-array process execution. `lane_template()` omits required Driver-specific
probe/capacity commands and is not suitable for blind config replacement.

Later integration validation should exercise real reservation races, one bounded
fallback under actual unavailable-account receipts, mixed-family author handoff,
assigned-account review, changed-head cancellation, inherited environment removal
and image delivery. Those are #631/runtime responsibilities. Source-only unit
successes in #630 do not prove those deployed behaviors.
