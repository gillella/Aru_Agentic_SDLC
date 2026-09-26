# Consumer compatibility inspection

This optional read-only tool compares a consumer's copied Aru files with the
canonical source. It never installs files, edits a board, executes the consumer's
tests, probes a paid provider, or changes repository protections.

```bash
python3 "$ARU_SDLC_HOME/integrations/adoption/check.py" \
  --repo /absolute/path/to/consumer --owner Unum-Inc
```

Add `--github` to read the actual repository identity and whether the default
branch's rules require one fresh approval of the last push (`approval_rule`:
`enforced`, `not-enforced` or `unknown`). This does not prove that a reviewer
account exists, runner health, Project correctness, application correctness, or
completed adoption. Run the normal project-scoped setup checks and a governed
pilot for that evidence.

The JSON report includes canonical and consumer revisions, whether canonical
source is dirty, hashes and comparison status for managed files, the assigned
runner profile, a `manifest` field reporting whether the consumer's
`.aru/manifest.json` is `current`, `stale`, `malformed` or `missing` against the
Factory's committed manifest for that profile, and whether
`.aru/verify-project.sh` still contains the generated failing starter. Configured means inspected, not executed or proven sufficient.
Differences may be intentional stricter consumer policy; reconcile them manually.
Missing/unreadable/symlinked paths, special files, and files above 2 MiB require inspection. No source content or
credentials are printed. Exit 0 means local files match and a configured executable
consumer verifier is present; exit 1 requests reconciliation; exit 2 is invalid
input. No exit status is deployment or runtime-readiness evidence.

For a new project, replace the failing starter with meaningful product checks.
For an existing consumer, retain its verification commands and product rules.
When adopting the split verifier, move those commands into `verify-project.sh`
and merge the framework changes deliberately. An update never means copying a
starter over working product checks. Re-run the real product checks after review.

Measure improvement from existing GitHub timestamps, CI runs and Driver receipts:
Ready-to-merge duration, review wait, unchanged attempts per completion and human
interventions. Keep unknown account quota/cost unknown. For multiple projects,
measure coordination-lock duration and API calls before changing reservation
locking. These measurements are diagnostic evidence, not new lifecycle state.

## Planning and review practice

This section is the working guide for the adoption pilot. It adds no gate, file,
approval, lifecycle state or skill; the Kernel contract still decides every
transition.

**Idea versus Ready.** An accepted idea is an open issue in Backlog. It becomes
implementable Ready work only when `triage_backlog.py` accepts it (an unchecked
acceptance criterion, exactly one safe `touches:` line, no unresolved
`depends-on: #N`) and no open decision would change its scope. Aru does not decide
product design: the consumer's product owner resolves each unresolved decision,
and the issue names that owner. A minimal issue with only criteria and `touches:`
stays valid.

**Optional prompts, proportional plans.** The issue forms offer optional sections
for problem and users, outcome, constraints and non-goals, unresolved decisions, a
plan and acceptance evidence. Use only what the change needs:

- *Tiny change* — "Fix the README typo": one criterion, `touches: README.md`, no
  plan.
- *Complex change* — "Exports survive a restart": a short Plan section recording
  the decision (persist the queue in the existing table), the rejected alternative
  (an in-memory retry loses work on crash), the risk (migration lock time) and the
  verification approach (a restart test in the product suite), or a link to a
  consumer-owned design document.

Start no line outside the `touches:` field with `touches:` or `depends-on`; the
contract reads those anywhere in the issue. List dependencies one per line.

The scaffolded issue form is consumer-owned. `init_project.py --sync` never
rewrites it, so a consumer's custom form and stricter product rules survive
updates; copy new prompts in deliberately if wanted.

**Review.** Reviewers follow `plugin/agents/aru-reviewer.md`; authors answer with
the dispositions in `skills/address-pr-feedback/SKILL.md`. A green check, a bot
summary or a review comment is not approval: approval is a GitHub review of the
exact head by an account other than the author that the default branch's
`.aru/review.json` authorizes; under `human` that is a listed named account, never
an App. A reviewer who pushes a fix becomes the last pusher, so someone else must
approve.

**Maintained lessons.** When the same mistake recurs, record it once as a
maintained rule (the consumer's `AGENTS.md`, product checks or runbook, or a Kernel
issue if the Kernel is wrong) plus a deterministic regression test or an external
agent-eval case. Name the rule's owner and link the source evidence: the PRs,
review threads or failures that showed the pattern. Publish only general lessons
that have been reviewed through a governed PR. Never bulk-copy personal agent
memories, customer information, credentials or stale private instructions.
