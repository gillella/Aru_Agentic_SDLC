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
runner profile, and whether `.aru/verify-project.sh` still contains the generated
failing starter. Configured means inspected, not executed or proven sufficient.
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
