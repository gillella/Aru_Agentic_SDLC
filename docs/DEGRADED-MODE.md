# Degraded Mode

GitHub is the only coordination authority. If issue, board, pull-request, CI,
review, identity, or merge state cannot be read completely:

1. do not claim, reassign, promote, merge, or close work;
2. keep already-claimed local work in its existing worktree;
3. record the failed command and error locally or on the linked issue when
   GitHub returns;
4. retry with bounded backoff;
5. never create a private queue, JSON ledger, lock service, or handoff file.

A local implementation may continue only when its claim and `touches:` budget
were established before the outage. It may not publish or transition state
until GitHub is authoritative again.
