---
name: remediate-ci-failure
description: Repair a failing exact-head aru-governed-pr server check on an authored PR.
---

# Remediate governed verification

1. Confirm the PR head and that you own the implementation branch.
2. Read the complete failing `aru-governed-pr` check logs. Identify whether
   the Factory's verifier, the actual-diff `touches:` check, or workflow setup failed.
   A queued check means the `aru-ci` self-hosted pool is offline or busy;
   restore that capacity and never switch to a GitHub-hosted runner as fallback.
3. Reproduce it locally with `$ARU_SDLC_HOME/scripts/verify_consumer.sh` when useful, but treat that run as
   preflight or audit evidence only; the exact-head server result is authority.
4. Apply the smallest fix inside the existing claimed worktree and `touches:`
   budget. Product commands live in `.aru/verify-project.sh` in new scaffolds;
   the Factory keeps the framework checks. Change either only when its required
   check is itself wrong or incomplete; preserve working checks in older consumers.
5. Commit and push. The workflow must run again on the new exact head.
6. Wait for the fresh server check and a fresh approval from another account;
   the push dismissed the earlier one.

Never dismiss a failing check, weaken `touches:`, or replace a risk-relevant
consumer check merely to obtain green status.
