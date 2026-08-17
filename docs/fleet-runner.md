# Desktop factory continuity and optional headless runner

The primary Aru workflow runs inside the desktop coding task the operator
started after selecting a project in Codex, Claude, Cursor, or Antigravity.
`aru code loop`, `run the factory`, and `keep going` mean that this current
task repeatedly works the governed GitHub board until the operator stops it or
a specific human decision/approval is required.

## Primary desktop architecture

```text
operator selects a project and starts a task in a desktop app
  └─ current desktop task runs the Aru loop
       ├─ fetch_next_work.py --claim
       ├─ governed skill for one work unit
       ├─ worktree → tests → PR/review/merge
       └─ ask the picker again; wait/retry when temporarily idle

GitHub Project Board = durable queue and lifecycle state
desktop task         = execution and user interaction boundary
```

The loop does not send a final response merely because one unit completed, the
picker is idle, the board is Complete, work is waiting on CI/review/dependencies,
another agent won a race, or a tool/network/credit limit is temporary. It uses
dynamic backoff and continues in the same task while the desktop product keeps
that task runnable.

Context compaction is also a continuity event. GitHub claims, branches, and
worktrees contain the durable truth; after compaction the task recovers its
stable agent identity and asks the picker again.

## What Aru can and cannot guarantee

The repository can define loop behavior and install skills, commands, and thin
native adapters. It cannot override a vendor-enforced task termination, invent
credits, keep a powered-off Mac running, or resume an arbitrary GUI conversation
through a universal API that does not exist.

Never use AppleScript, accessibility clicking, keystroke injection, or screen
coordinates to keep desktop agents alive. Such UI automation can target the
wrong project, bypass an approval, or mutate the wrong worktree.

Capability levels must remain distinct:

| Capability | Meaning |
|---|---|
| Active-task loop | The currently running desktop task continues to pick work |
| Native wake | The application can schedule or trigger the same configured task/project |
| App restart recovery | Work resumes after the application relaunches |
| Machine restart recovery | Work resumes after reboot/login |

As of the current design, Codex documents background/thread automations and
Antigravity documents goals, workflows, and scheduled tasks. Their native
features may be configured in #46 as project-scoped wake adapters. Claude and
Cursor must use only capabilities documented by their installed desktop
versions; absence of a supported same-task wake is reported as a gap, not
silently replaced with a CLI process.

App-native schedules are optional wake mechanisms. They do not become a new
work queue: every wake recovers and selects work from GitHub.

## Intentional stop and human intervention

The desktop loop intentionally ends only when:

- the operator says stop, cancels the task, or disables its native wake; or
- an unresolved product, security, money, external-contract, approval, or
  hard-governance decision genuinely requires a human.

Routine idle, CI, review, dependency, transient error, context, and retry states
do not meet that bar.

## Optional headless CLI mode

`scripts/run_fleet.py` is retained for an operator who explicitly chooses a
headless local-agent deployment. It is not invoked by default from a desktop
task and cannot continue the conversation visible in a desktop application.

From a trusted isolated clone:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . --agent codex-1 --family openai --adapter codex
```

For Claude Code CLI:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . --agent claude-1 --family anthropic --adapter claude
```

Another CLI can be supplied as a JSON argv array. `{repo}` and `{prompt}` are
replaced as individual arguments, never evaluated by a shell:

```bash
python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" loop \
  --repo . --agent local-1 --family other \
  --adapter-command-json '["my-agent","--cwd","{repo}","--prompt","{prompt}"]'
```

Headless operation provides `once`, `loop`, `status`, and drain-first `stop`.
It checks eligibility before launching a paid CLI child, isolates the child
from runner stop signals, retries recoverable failures with bounded backoff,
and stores process metadata—but not prompts, transcripts, tokens, credentials,
or a second work queue—under the platform state directory.

## Governed single-shot ephemeral workers

When a known PR is waiting only for a cross-family peer review or for its
stamped author to address review feedback, the active factory task may add
short-lived capacity with `scripts/spawn_ephemeral_worker.py`. This is an
opt-in CLI subprocess launcher, not an MCP call, daemon, picker, desktop-task
replacement, or alternate coordinator. It sends one task-scoped prompt to an
installed headless agent binary and exits with that process.

For example, an OpenAI parent can ask an installed Claude Code CLI to review
one PR:

```bash
python3 "$ARU_SDLC_HOME/scripts/spawn_ephemeral_worker.py" \
  --repo . --pr <PR> --skill code-review \
  --parent-agent codex-1 --parent-family openai \
  --worker-agent claude-ephemeral-<PR> --worker-family anthropic \
  --adapter claude
```

If Claude is unavailable, an OpenAI parent can use the installed Gemini CLI as
independently attested Google-family review capacity:

```bash
python3 "$ARU_SDLC_HOME/scripts/spawn_ephemeral_worker.py" \
  --repo . --pr <PR> --skill code-review \
  --parent-agent codex-1 --parent-family openai \
  --worker-agent gemini-ephemeral-<PR> --worker-family google \
  --adapter gemini
```

The `gemini` adapter invokes the headless Gemini CLI in sandboxed,
non-interactive auto-approval mode inside the isolated review worktree. The
ephemeral launcher accepts only same-repository PRs; fork PRs require a
persistent or manual review path because their heads are not trusted with the
worker's GitHub authority. Cursor and Antigravity remain governed desktop
participants, but this launcher does not drive their UI or infer the model
family selected inside Cursor. Antigravity review capacity uses the
independently identifiable Google-family Gemini CLI path.

An author-feedback task uses the PR's existing author stamps instead. The
worker id and family must exactly match `author:<id>` and `family:<family>`:

```bash
python3 "$ARU_SDLC_HOME/scripts/spawn_ephemeral_worker.py" \
  --repo . --pr <PR> --skill address-pr-feedback \
  --parent-agent codex-1 --parent-family openai \
  --worker-agent <AUTHOR_ID> --worker-family <AUTHOR_FAMILY> \
  --adapter claude
```

The command validates live PR identity before launch. Review workers must
differ from both the parent family and the PR author's family; feedback workers
must be the stamped author. A machine-global locked registry permits at most
two ephemeral workers, rejects a duplicate worker identity, and reaps dead
launcher PIDs. Every child receives `ARU_CAN_SPAWN=0`, so it cannot recursively
spawn more workers.

Each invocation fetches the exact live PR head into
`.worktrees/ephemeral-<review|feedback>-<pid>`. Review worktrees are detached;
feedback worktrees use a launcher-owned temporary branch and push
fast-forward to the existing PR branch. The default hard timeout is 300
seconds (override with `--timeout`), after which the whole child process group
is terminated. Non-zero exits, missing binaries, unavailable provider
credentials, and setup errors fail closed, release a held review claim, clean
an unchanged launcher-owned worktree/temporary branch, unregister, and return
non-zero. Dirty or uncertain worktrees are never force-removed; they are
retained visibly for diagnosis and make the launcher return non-zero.

The child still follows `code-review` or `address-pr-feedback` in full. It must
submit the review or feedback evidence, complete/release the normal GitHub
claim, and never merge. The parent returns to the canonical picker after the
single-shot command exits. Do not use this command for general idle capacity,
to impersonate an unrelated author, or when a suitable persistent agent is
already handling the same PR.

## Follow-on desktop adapters

Issue #46 installs and verifies the thinnest supported native continuity
adapter for each desktop application. It must preserve the project the operator
selected, persist explicit-stop intent, report capability gaps truthfully, and
never substitute an OS daemon that drives the GUI.

## Unattended-completion acceptance test

The default end-to-end acceptance scenario is hermetic:

```bash
python3 -m unittest tests.e2e.test_unattended_board_completion
```

It injects fake local-agent adapters and an in-memory GitHub transport while
running the production picker, optimistic issue/review/merge claim helpers,
merge Definition-of-Done evaluator, and runner. It spends no model credits,
uses no GitHub credentials, and does not read or modify Codex, Claude, Cursor,
Antigravity, or developer configuration. The scenario covers two model
families, concurrent non-overlapping claims, dependency and path serialization,
author handoff, cross-family review feedback, CI remediation, crash recovery,
guarded merge, an unresolved high-risk product decision and acknowledgement,
idle waiting, final board and workspace audit, and explicit runner shutdown.

### Opt-in live disposable-repository smoke test

The hermetic scenario is the CI gate. A live smoke test is a separate,
operator-authorized exercise and must never target this repository or an
existing project board:

1. Create the repository and Project only through the bootstrap helper, from a
   directory that is not an existing checkout:

   ```bash
   python3 "$ARU_SDLC_HOME/scripts/init_project.py" \
     --name <DISPOSABLE_REPO> --private --create-board \
     --owner <TEST_OWNER> --target-dir <NEW_CHECKOUT>
   ```

   In the new checkout, enable the real required check when the test owner and
   repository plan support rulesets:

   ```bash
   python3 "$ARU_SDLC_HOME/scripts/enable_main_ruleset.py" \
     --apply --enforcement active
   ```

   If active rulesets are unavailable, stop rather than claiming that the live
   guarded-merge case was exercised.
2. Before any paid child starts, run `gh auth status` without redirecting its
   output, verify the expected test-account login, and verify the exact target
   with `gh repo view --json nameWithOwner -q .nameWithOwner`. Confirm the sole
   disposable Project with `gh project list --owner <TEST_OWNER>` and run
   `python3 "$ARU_SDLC_HOME/scripts/fleet_status.py" --repo-dir . --json`.
   Abort on any credential, repository, or Project mismatch. Never place a
   token in an argument, environment dump, state file, or log.
3. File two low-risk issues through the `create-github-issue` skill, then move
   each one with the board helper:

   ```bash
   python3 "$ARU_SDLC_HOME/scripts/update_issue_status.py" \
     --issue <ID> --status Ready --require-board
   ```

   Their bodies must use exact machine-readable metadata:

   ```text
   depends-on: #12, #14
   touches: src/a.py, tests/test_a.py
   parallel-eligible: true
   ```

   Give the first two issues non-overlapping `touches:` values and make a third
   issue depend on the first. Do not use secrets, production data, billing
   paths, or a real application repository.
4. Use distinct agent ids from at least two model families. Distinct ids are
   required for `reviewed-by:<agent_id>` attribution even when both workers use
   one GitHub account; a separate GitHub App or account may instead provide a
   server-side approval. In two isolated clones, use only the governed
   lifecycle commands:

   ```bash
   python3 "$ARU_SDLC_HOME/scripts/fetch_next_work.py" \
     --agent <ID> --family <FAMILY> --claim --json
   python3 "$ARU_SDLC_HOME/scripts/create_branch.py" \
     --issue <ISSUE> --worktree
   python3 "$ARU_SDLC_HOME/scripts/create_pr.py" \
     --issue <ISSUE> --agent <ID> --model-family <FAMILY> \
     --title "<TITLE>" --body "Closes #<ISSUE>"
   python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" \
     --pr <PR> --agent <REVIEWER>
   python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" \
     --pr <PR> --agent <REVIEWER> --complete-review
   python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" \
     --pr <PR> --expected-head <SHA>
   ```

   Do not substitute direct `gh` or API lifecycle writes for a helper that
   exists.
5. Observe claim, worktree, PR, CI, independent review, guarded merge, Done
   reconciliation, and helper-driven worktree/branch cleanup. Record only
   issue/PR URLs and gate outcomes. Request explicit runner stop for each
   worker, then run the authoritative final audit:

   ```bash
   python3 "$ARU_SDLC_HOME/scripts/run_fleet.py" stop \
     --repo . --agent <ID>
   python3 "$ARU_SDLC_HOME/scripts/fleet_status.py" --repo-dir . --json
   ```

   Verify zero open issues, PRs, claims, or dirty worktrees. After that audit,
   the operator may delete the disposable Project and repository in the GitHub
   UI; no local agent performs that destructive account-level cleanup.

This live smoke test is intentionally not part of default CI because it uses
paid local-agent sessions and mutates a real GitHub repository. Run it only
when an operator deliberately supplies that disposable scope and accepts the
cost.
