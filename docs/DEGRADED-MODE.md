# Degraded Mode

GitHub is the only coordination authority. If issue, board, pull-request,
exact-head verification evidence, review, identity, or merge state cannot be
read completely:

1. do not claim, reassign, promote, merge, or close work;
2. keep already-claimed local work in its existing worktree;
3. record the failed command and error locally or on the linked issue when
   GitHub returns;
4. retry with bounded backoff;
5. never create a private queue, JSON ledger, lock service, or handoff file.

A local implementation may continue only when its claim and `touches:` budget
were established before the outage. It may not publish or transition state
until GitHub is authoritative again.

An external Driver must cancel or defer any scheduled continuation whose PR
head or authority cannot be reread. Consumer deployment and incident policy
remains consumer-owned; degraded Kernel state never authorizes production
action.

If every `aru-ci` self-hosted Mac is offline, leave `aru-governed-pr` queued and
restore runner availability. Do not change the workflow to a GitHub-hosted
label, waive the required check, or treat an ad hoc terminal run as server
evidence.
