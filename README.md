# Aru Code Factory

Aru is a small issue-to-safe-merge governance kernel for private software
projects. It is intentionally not a scheduler, fleet manager, dashboard,
deployment system, or second project-management database.

The kernel moves one approved GitHub issue through:

1. a complete issue contract,
2. Ready status,
3. an exclusive claim,
4. an isolated worktree,
5. focused implementation and tests,
6. a pull request with exact-head CI and one external reviewer,
7. mechanical merge through `scripts/merge_pr.py`, and
8. Done status plus safe worktree cleanup.

GitHub Issues and the linked Project Board are the only lifecycle state.
Hermes or a human may call the commands, but orchestration stays outside this
repository. Consumer repositories own deployment, releases, credentials,
money, and production authorization.

## Install

```bash
./scripts/install_agent_integration.sh
./scripts/install_hooks.sh
```

## Commands

```bash
python3 scripts/init_project.py --name example --directory /path/to/example
python3 scripts/triage_backlog.py
python3 scripts/fetch_next_work.py --agent codex-1 --json
python3 scripts/claim_issue.py --issue 42 --agent codex-1
python3 scripts/create_branch.py --issue 42 --type feat --agent codex-1
python3 scripts/create_pr.py --issue 42 --title "feat: example" --body "Summary"
python3 scripts/check_ci.py --pr 123
python3 scripts/fetch_pr_feedback.py --pr 123
python3 scripts/merge_pr.py --pr 123 --expected-head <sha>
python3 scripts/cleanup_worktrees.py
```

The complete operating contract is in [AGENTS.md](AGENTS.md). Enforcement is
listed in [docs/ENFORCEMENT-REGISTER.md](docs/ENFORCEMENT-REGISTER.md).
