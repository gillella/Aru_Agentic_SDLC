# Operations

## Start one unit

```bash
python3 scripts/fetch_next_work.py --agent <id> --json
python3 scripts/claim_issue.py --issue <n> --agent <id>
python3 scripts/create_branch.py --issue <n> --type <feat|fix|docs> --agent <id>
```

Work only in the reported worktree. Run the focused commands named by the
issue, then:

```bash
python3 scripts/create_pr.py --issue <n> --title "<title>" --body "<summary>"
python3 scripts/check_ci.py --pr <pr> --wait
python3 scripts/fetch_pr_feedback.py --pr <pr>
python3 scripts/merge_pr.py --pr <pr> --expected-head <sha>
```

## Triage

`triage_backlog.py` promotes only open issues with acceptance criteria,
safe `touches:` paths, and closed dependencies. It promotes one issue by
default; use `--all` only for deliberate backlog preparation.

## Manual reviewer replacement

After a concrete external-service failure, replace the single
`review:<service>` label with one other supported label and add a PR comment
stating the service failure, old label, new label, operator, and timestamp.
There is no automated reassignment command.

## Cleanup

`cleanup_worktrees.py` removes only clean worktrees under `.worktrees/`
whose GitHub PR is closed or merged. Dirty, unregistered, open, and
user-created directories are reported and retained.

## Recovery

The pre-reset baseline is tagged `pre-v0.2.0-2026-08-27`. Revert a merged PR
through `revert_merge.py`, which creates a new reviewed pull request; never
rewrite default-branch history.

## v0.2 freeze

Through 2026-09-26, accept only security and correctness fixes. Afterward, a
new capability still requires evidence from three consumer repositories and
must pass the admission rule in the kernel contract.
