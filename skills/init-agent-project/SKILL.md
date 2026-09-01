---
name: init-agent-project
description: Bootstrap a repository with the Aru minimal issue-to-safe-merge kernel.
---

# Initialize a governed repository

1. Confirm the destination and whether an existing repository may be modified.
2. Run:

   ```bash
   python3 "$ARU_SDLC_HOME/scripts/init_project.py" --name <name> --directory <path>
   ```

3. Inspect the generated AGENTS.md, issue form, PR template,
   `.github/workflows/governed-pr.yml`, `.aru/verify.sh`, and hooks. Replace the
   fail-closed verify placeholder with risk-appropriate consumer commands.
4. If GitHub setup was requested, create or attach one Project Board with the
   five statuses: Backlog, Ready, In Progress, In Review, Done.
5. With `--github`, confirm the minimal ruleset has no configured bypass actors
   and requires `aru-governed-pr` from GitHub Actions. Do not claim that this
   portable rule makes `merge_pr.py` technically exclusive; keep stronger
   security, engineering, release, and deployment controls in the consumer.
6. Report generated files and any GitHub step that could not be completed.

Do not install a daemon, scheduler, dashboard, deploy stack, release stack,
private queue, or repository-owned runtime.
