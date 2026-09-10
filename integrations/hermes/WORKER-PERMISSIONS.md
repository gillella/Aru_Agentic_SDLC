# Factory source-worker permission repair (#638)

This is an opt-in correction to the existing supervisor. Installing source does
not enable it. Only `gillella/Aru_Agentic_SDLC` and explicitly selected Claude
lanes can use version 1. Other projects, providers, and unselected lanes retain
their command and result behavior. Factory must stay stopped throughout source
review and operator rollout; preserve #637's m2 claim and worktree.

## Permission and evidence boundaries

The compiler accepts the existing `claude-sub N --model MODEL --print
--permission-mode acceptEdits --permission-prompts none {prompt}` command shape.
It preserves subscription/model selection, compiles `dontAsk` with exact Bash
commands and scoped file rules, adds the canonical directory, disables user /
project / local settings sources, limits built-in tools, and excludes inherited
MCP configuration. It never enables bypass, a broad Bash rule, `python -c`, a
helper wildcard, global permissions, another repository, or author review/merge.
Malformed policy, unsupported harness, nonliteral paths, missing live touches,
wrong Git common directory, and a wrong issue branch fail closed.

Author grants cover the App-bound issue read, optional current PR reads, Git
identity/status/diff, the configured Python versions, exact pytest and Ruff
commands, `git fetch origin`, staging only live touches, a fixed issue commit
message, pushing only `HEAD:refs/heads/<claimed-branch>`, and one canonical
`create_pr.py --issue ... --author-family claude-code` invocation. The PR body
scratch file `.aru-worker-body.md` stays untracked and contains no closing line;
the helper appends it. No issue claiming, reviewer assignment, merge, release,
deployment or arbitrary helper execution is granted. Exact commands appear in
the worker prompt; extra flags/alternate spellings may be denied deliberately.

For a missing scratch body, use `Edit` with `old_string=""` and the body in
`new_string`; read an existing body before editing it. The exact scratch-path
Edit grant applies to both roles and does not require a broader grant. The
installed 2.1.266 source supports creation through Edit; see the
[bounded review evidence](REMEDIATION-638.md). Live harness behavior remains an
operator gate.

Reviewer grants substitute exact PR review submission for author edits,
commit/push and PR creation. Reviewers may write the attestation scratch file,
but have no source-edit grant. Their authenticated actor is checked using
`gh api user --jq .login`: personal reviewer bindings drop the author App runner,
while App reviewer bindings use the configured runner. A mismatched actor must
stop. Assignment/head/author separation remains the existing kernel review
binding and prompt contract; a CLI PR-review command is not an atomic head lock.

**CLI permissions are not OS isolation.** `--add-dir` enables canonical directory
access; it does not mount it read-only. Built-in read-only commands and file
access defaults still exist. Canonical-read-only, no credentials/other projects,
reviewer non-authorship, and no production/merge/release are prompt/kernel policy,
not a complete filesystem or subprocess sandbox. Exact tests and the canonical
helper execute trusted code and subprocesses; hooks still run on Git commit/push.
These grants do not make hostile test code safe. Managed Claude settings still
apply and may deny access. This repair does not modify OS, global Claude, Git,
credential, branch-rule, or other project permissions.

The installed Claude 2.1.266 `--help` was inspected, including `--add-dir`,
`--allowedTools`, `--permission-mode`, `--permission-prompts`, `--setting-sources`,
`--strict-mcp-config`, `--tools`, `--output-format`, and `--no-session-persistence`.
See the [official CLI reference](https://code.claude.com/docs/en/cli-reference),
[permission rules](https://code.claude.com/docs/en/permissions), and
[SDK result envelope](https://code.claude.com/docs/en/agent-sdk/typescript).
The failed #637 log is evidence of permission failure, not guidance to broadly
allow `gh pr *` or `python scripts/*.py`.

## Durable outcomes and retries

The worker receipt captures its argv, task scope and effective config fingerprint
before supervisor launch. A config change before child execution refuses the old
snapshot. The supervisor writes stdout to a separate `logs/<id>.result.json`,
keeps stderr in the existing log, and records process exit independently from the
Claude `result` envelope (`subtype`, `is_error`, `permission_denials`). A denial
retains its tool/input, including at exit zero. Invalid, absent, oversized or
error results block rather than becoming success or subscription quota evidence.

Receipts retain at most 8 KiB of serialized result observation details; larger
observations retain only outcome/blocker flags and a pointer to `result_path`.
Result-derived reasons are at most 2 KiB, keeping repeated receipt reads and
status reports small. Full accepted envelopes, including denial inputs, stay in
the private 0600 result log up to 8 MiB. On completion, an oversized log is
trimmed to its first 8 MiB and classified unavailable, never successful. This is
a completed-log limit, not a streaming disk quota while the child runs. These
logs can contain sensitive model/tool input; inspect locally and publish only
redacted evidence. Existing receipts are retained without retroactive rewriting.

The receipt's argv is trusted operator-owned execution input. Its config
fingerprint detects configuration drift, not tampering with the receipt that
also contains the fingerprint. The existing private state directory and local
account are the trust boundary; this is not cryptographic receipt authentication.

Even a clean reported success is only a report. The controller rereads GitHub;
if the task still requires the same author work, its terminal receipt blocks a
repeat. Changed PR/head or an explicitly approved lane/policy revision (including
`recovery_epoch`) allows reevaluation, followed by normal live claim/worktree
validation. Events, elapsed time and Stop/Start do not erase the blocker. Keep
all receipts. Do not repeatedly bump the epoch to conceal an unchanged failure.
Existing review recovery retains its single bounded authority-refresh event;
only a changed policy permits a new attempt at an unchanged reviewer assignment.
Process success never supplies independent review or governed completion.

A worker fenced by Stop before child creation has no result to classify. It
retains its Stop/admission reason and releases its reservation without a result
retry blocker. Start can revalidate and resume it once through normal admission;
an old supervisor still cannot cross the Stop nonce. If a child did run, its
denial/error result remains blocking even when Stop occurs before completion.

Legacy text receipts are retained unchanged: this repair does not retroactively
classify #637 by parsing prose. The first authorized opted-in launch after
rollout gets the new snapshot/result contract. A bare provider `OK` probe remains
liveness evidence only, not proof of task permissions.

## Operator-only proposal after independent review and governed merge

These commands are a proposal, not actions performed by the source author.
Hermes must approve/apply the project policy, verify installed source, and own
all live evidence. No launch of #637 is part of source verification.

At `/Users/gillella/.hermes/aru-project-driver.json`, set **only**
`projects["gillella/Aru_Agentic_SDLC"].worker_permissions` to:

```json
{
  "version": 1,
  "lanes": ["m1", "m2", "m3"],
  "python": "/Users/gillella/.hermes/hermes-agent/venv/bin/python",
  "test_python": "/tmp/aru-ci633-venv/bin/python",
  "app_runner": "/Users/gillella/.local/bin/aru-code-factory-app-run",
  "recovery_epoch": "approved-driver638-v1"
}
```

The test interpreter above is the existing operator-provided pytest/Ruff venv;
verify it still exists before rollout. Its `/tmp` location is not durable across
host cleanup. Hermes should approve a persistent replacement path separately if
needed; a source change does not install interpreters or packages. Keep all lane
commands and every other project entry unchanged. This policy requires those
selected lanes to retain the documented command shape. Keep the test venv's
`bin` first in the operator/Driver PATH so Git hooks find its dependencies.

Use a fresh backup directory and the merged canonical source. Preserve the
prior installed package and skill as well as the entire original config, because
rollback must restore source/config together without deleting worker history:

```sh
export PATH=/tmp/aru-ci633-venv/bin:$PATH
DRIVER638_BACKUP=/tmp/factory-driver638-rollout-backup
mkdir "$DRIVER638_BACKUP"
cp /Users/gillella/.hermes/aru-project-driver.json "$DRIVER638_BACKUP/config.json"
cp -R /Users/gillella/.hermes/scripts/aru_project_driver "$DRIVER638_BACKUP/package"
cp -R /Users/gillella/.hermes/skills/hermes-project-driver "$DRIVER638_BACKUP/skill"
/Users/gillella/.hermes/hermes-agent/venv/bin/python /Users/gillella/Projects/Aru_Agentic_SDLC/integrations/hermes/install.py --config /Users/gillella/.hermes/aru-project-driver.json
# After reviewing the preview and approving install:
/Users/gillella/.hermes/hermes-agent/venv/bin/python /Users/gillella/Projects/Aru_Agentic_SDLC/integrations/hermes/install.py --config /Users/gillella/.hermes/aru-project-driver.json --apply
# After separately approving the exact project-only JSON edit above:
/Users/gillella/.hermes/hermes-agent/venv/bin/python /Users/gillella/.hermes/scripts/aru_project_driver/driver.py --config /Users/gillella/.hermes/aru-project-driver.json status --project gillella/Aru_Agentic_SDLC
```

Require `enabled: false`. Do not Start merely to validate installation. The
installer creates its own source backups and never activates the project.

## No-write capability preflight

The installed `preflight` operation performs bounded read-only kernel/Git context
validation and prints a probe plan; it never executes Claude, claims, creates a
worktree, writes a binding, enables Factory, or reserves a worker. Use an existing
claimed task. For #637, the path below is the preserved worktree from receipt
`f5c3e9ae000f48c68edcf9952d4d191e`; reread that authority before use:

```sh
ARU_GITHUB_APP_RUNNER=/Users/gillella/.local/bin/aru-code-factory-app-run \
/Users/gillella/.hermes/hermes-agent/venv/bin/python /Users/gillella/.hermes/scripts/aru_project_driver/driver.py \
  --config /Users/gillella/.hermes/aru-project-driver.json preflight \
  --project gillella/Aru_Agentic_SDLC --agent m2 --issue 637 \
  --worktree /Users/gillella/Projects/Aru_Agentic_SDLC/.worktrees/feat-issue-637-feat-personas-make-expert-and-subscription > /tmp/driver638-preflight-plan.json
```

Hermes reviews `cwd`, `task`, `policy_fingerprint`, commands and full argv. The
probe uses the launch harness/account/model and the same compiled read grants,
with write/submission/test grants removed and Edit/Write explicitly denied. It
requests actual App-bound issue content, both Python versions, canonical rules,
Git identity, branch, HEAD/status/diff. Its stream JSON includes tool calls and
results. It is deliberately **not proof of edit/test/push capability**. Only the
subsequent separately authorized source worker and its artifacts can prove that.

After approval, execute that one generated probe argv from its recorded cwd;
this shell command prints it for inspection and manual execution, without eval:

```sh
jq -r '.argv | @sh' /tmp/driver638-preflight-plan.json
```

Capture stdout/stderr outside the repository. Inspect actual tool-use/result
pairs and the terminal result: every required read must have succeeded, with no
permission denials or errors. A bare OK, missing tool execution, missing/malformed
result, or exit zero alone does not pass. A failure leaves Factory stopped and
requires a specific corrected policy/harness/task plus a new reviewed plan.
Do not execute implementation prompts as a preflight. A live probe can still
write Claude's own operational logs; “no-write” refers to task/GitHub operations,
not an OS guarantee against provider bookkeeping.

Only after that read proof, installed-source verification and separate rollout
authorization may Hermes resume the preserved claim through the existing
controller. Observe a bounded worker, its permissions/result evidence, actual
branch/PR artifacts and exact-head CI. Never check issue live acceptance items
based solely on the source tests. Review, merge, release and installation remain
separate operator actions. #557 acceptance stays open until actually exercised.

## Rollback proposal

Keep Factory stopped. If it was activated after rollout, invoke the existing
Stop command and require the verified Stop response before restoring files.
Stop preserves admitted workers: confirm none still holds a reservation before
restoring the installed package. Then restore the saved config and source, keep
all `state/aru_project_driver` receipts and Stop intents, and verify stopped
status. With no concurrent installer/config writer:

```sh
/Users/gillella/.hermes/hermes-agent/venv/bin/python /Users/gillella/.hermes/scripts/aru_project_driver/driver.py --config /Users/gillella/.hermes/aru-project-driver.json stop --project gillella/Aru_Agentic_SDLC
mv /Users/gillella/.hermes/scripts/aru_project_driver "$DRIVER638_BACKUP/withdrawn-package"
cp -R "$DRIVER638_BACKUP/package" /Users/gillella/.hermes/scripts/aru_project_driver
mv /Users/gillella/.hermes/skills/hermes-project-driver "$DRIVER638_BACKUP/withdrawn-skill"
cp -R "$DRIVER638_BACKUP/skill" /Users/gillella/.hermes/skills/hermes-project-driver
cp "$DRIVER638_BACKUP/config.json" /Users/gillella/.hermes/aru-project-driver.json
/Users/gillella/.hermes/hermes-agent/venv/bin/python /Users/gillella/.hermes/scripts/aru_project_driver/driver.py --config /Users/gillella/.hermes/aru-project-driver.json status --project gillella/Aru_Agentic_SDLC
```

Rollback does not authorize retrying the old known-denied #637 command. Leave
Factory stopped and report the exact failure and retained receipt/log paths.
