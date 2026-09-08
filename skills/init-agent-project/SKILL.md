---
name: init-agent-project
description: Bootstrap a repository with the Aru minimal issue-to-safe-merge kernel.
---

# Initialize a governed repository

1. Confirm the destination and whether an existing repository may be modified.
2. Run:

   ```bash
   python3 "$ARU_SDLC_HOME/scripts/init_project.py" --name <name> --directory <path> \
     --owner <github-account>
   ```

   `--owner` selects the runner profile: `gillella` gets `self-hosted-mac`
   (`[self-hosted, macOS, ARM64, aru-ci]`), `Unum-Inc` gets `github-hosted`
   (`ubuntu-latest`). There is no default, so an unassigned account is refused;
   pass `--runner-profile` to state one explicitly. Passing both asserts they
   agree and refuses a contradiction. Never work around a refusal by choosing
   the other account's profile.

3. Inspect the generated AGENTS.md, issue form, PR template,
   `.github/workflows/governed-pr.yml`, `.aru/verify.sh`, and hooks. Confirm the
   workflow's `# aru-runner-profile:` marker matches its `runs-on:` and the
   intended account. Replace the fail-closed verify placeholder with
   risk-appropriate consumer commands.
4. If GitHub setup was requested, create or attach one Project Board with the
   five statuses: Backlog, Ready, In Progress, In Review, Done.
5. With `--github`, confirm the minimal ruleset has no configured bypass actors
   and requires `aru-governed-pr` from GitHub Actions; the helper also refuses
   provisioning if the created repository's owner is not assigned the
   scaffolded profile. On `self-hosted-mac`, confirm at least one
   repository-level macOS arm64 runner with label `aru-ci` is online before
   admitting work, and that Python 3.11+, pip, and `gh` are installed on every
   labeled runner. On `github-hosted`, confirm Actions is enabled and the
   governed workflow is active. Under both profiles the workflow has no
   cross-profile fallback and rejects cross-repository fork PRs before
   checkout. Do not claim that this portable rule makes `merge_pr.py`
   technically exclusive; keep stronger security, engineering, release, and
   deployment controls in the consumer.
6. Report generated files and any GitHub step that could not be completed.

Do not install a daemon, scheduler, dashboard, deploy stack, release stack,
private queue, or repository-owned runtime.
