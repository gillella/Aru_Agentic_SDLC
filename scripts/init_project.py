#!/usr/bin/env python3
"""
init_project.py - Automation script for bootstrapping a brand-new repository under
Aru_Agentic_SDLC governance, scaffolding AGENTS.md, CI workflows, issue/PR templates,
a private GitHub repo, governance labels, and a configured GitHub Project v2 board.

The board is not decorative: `fetch_next_issue.py` reads `depends-on: #N` from issue
bodies and `claim_issue.py` / `update_issue_status.py` move both the `status:*` label
and the board item. This script provisions the Status options and custom fields those
scripts expect, so the loop works on first use.
"""

import argparse
import json
import os
import sys
from typing import Optional, Tuple

from common import run_cmd, run_gh_json

# --- Board contract -------------------------------------------------------
# These five statuses are what fetch_next_issue.py / claim_issue.py assume.
# Changing them here means changing them there too.
BOARD_STATUSES = [
    ("Backlog", "GRAY", "Filed, not yet refined"),
    ("Ready", "BLUE", "Refined and unblocked; claimable"),
    ("In Progress", "YELLOW", "Claimed, branch open"),
    ("In Review", "ORANGE", "PR open, awaiting review or CI"),
    ("Done", "GREEN", "Merged and closed"),
]

GOVERNANCE_LABELS = [
    ("status:backlog", "ededed", "Board status: Backlog"),
    ("status:ready", "1d76db", "Board status: Ready"),
    ("status:in-progress", "fbca04", "Board status: In Progress"),
    ("status:in-review", "d93f0b", "Board status: In Review"),
    ("status:done", "0e8a16", "Board status: Done"),
    ("type:epic", "5319e7", "Phase-level epic; not directly implementable"),
    ("type:feat", "a2eeef", "New capability"),
    ("type:fix", "d73a4a", "Defect repair"),
    ("type:chore", "cfd3d7", "Tooling, CI, or maintenance"),
    ("type:docs", "0075ca", "Specification or documentation"),
    ("priority:p0", "b60205", "Blocking; drop everything"),
    ("priority:p1", "d93f0b", "Current phase critical path"),
    ("priority:p2", "fbca04", "Current phase, not critical path"),
    ("priority:p3", "c5def5", "Opportunistic"),
    ("parallel-eligible", "0e8a16", "No unresolved depends-on; safe for a parallel agent"),
]

DEFAULT_GITIGNORE = """# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class

# Environments & Dependencies
.venv/
env/
venv/
node_modules/
dist/
build/

# Worktrees & Temporary Logs
.worktrees/
*.log
.DS_Store

# Secrets
.env
.env.*
!.env.example
"""

DEFAULT_AGENTS_TEMPLATE = """# AGENTS.md - Master AI Engineering Playbook & Operating Directives

Welcome to **{project_name}**. This repository operates under **Aru_Agentic_SDLC** governance.

All AI agents operating within this repository MUST follow the directives, skills, and tools defined herein.

---

## 🚨 Core Governance: The Issue-First Law

**No code change, refactor, or feature implementation may begin without first originating from a tracked issue on the GitHub Project Board.**

---

## 🎯 Primary Directives for AI Agents

1. **Execute via SkillsMP Skills** (in `$ARU_SDLC_HOME/skills/`):
   - Primary Skill: `implement-next-issue/SKILL.md`
   - Code Review Skill: `code-review/SKILL.md`
   - CI Failure Remediation: `remediate-ci-failure/SKILL.md`
   - PR Review Feedback: `address-pr-feedback/SKILL.md`
2. **Worktree Isolation**:
   - Always run feature work inside `.worktrees/` directories to keep the main workspace clean.
3. **Local Test Verification First**:
   - Run `{test_runner}` and confirm all tests pass before committing.
4. **Mandatory Issue Linking**:
   - Every Pull Request MUST include `Closes #<issue_number>` in its body.

---

## 📋 Board Contract

Issue bodies drive the dependency engine. Every issue MUST carry:

```
depends-on: #12, #14        (omit or leave blank if none)
parallel-eligible: true     (only when it has no unresolved depends-on)
```

Board statuses, in order: `Backlog` → `Ready` → `In Progress` → `In Review` → `Done`.
Each is mirrored by a `status:*` label so the CLI and the board stay in sync.
"""

CI_WORKFLOW = """name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
          if [ -f pyproject.toml ]; then pip install -e . || true; fi

      - name: Lint
        run: |
          pip install ruff
          ruff check . || echo "::warning::ruff reported findings"

      - name: Enforce module boundaries
        run: |
          if [ -f .importlinter ] || grep -q "importlinter" pyproject.toml 2>/dev/null; then
            pip install import-linter
            lint-imports
          else
            echo "No import-linter contracts configured yet; skipping."
          fi

      - name: Tests
        run: |
          if find tests -name 'test_*.py' -o -name '*_test.py' 2>/dev/null | grep -q .; then
            pip install pytest
            {test_runner}
          else
            echo "No tests present yet; skipping. This step becomes mandatory at first source commit."
          fi
"""

ISSUE_TEMPLATE_FEATURE = """---
name: Feature
about: A new capability originating from the project plan
title: 'feat: '
labels: ['type:feat', 'status:backlog']
---

## Summary

<!-- One sentence: what capability does this add? -->

## Background

<!-- Which phase and which section of PROJECT-PLAN.md does this come from? Link it. -->

## Acceptance Criteria

- [ ]
- [ ]

## Verification

<!-- The command, test, or observable output that proves this is done. Required. -->

## Dependencies

depends-on:
parallel-eligible: false
"""

ISSUE_TEMPLATE_TASK = """---
name: Task
about: Tooling, CI, specification, or maintenance work
title: 'chore: '
labels: ['type:chore', 'status:backlog']
---

## Summary

## Acceptance Criteria

- [ ]

## Verification

## Dependencies

depends-on:
parallel-eligible: false
"""

ISSUE_TEMPLATE_BUG = """---
name: Bug
about: Something behaves incorrectly
title: 'fix: '
labels: ['type:fix', 'status:backlog']
---

## Observed

## Expected

## Reproduction

## Verification

<!-- The failing test that must pass. -->

## Dependencies

depends-on:
parallel-eligible: false
"""

ISSUE_TEMPLATE_EPIC = """---
name: Epic
about: A phase-level container; not directly implementable
title: 'epic: '
labels: ['type:epic', 'status:backlog']
---

## Phase

## Goal

<!-- What is demo-able when this epic closes? -->

## Gating Contract

<!-- What must be decided or signed off before this epic can start? Name the owner. -->

## Child Issues

<!-- Populated as the phase is decomposed. -->

## Dependencies

depends-on:
parallel-eligible: false
"""

PR_TEMPLATE = """## What

<!-- One paragraph. -->

## Verification

<!-- The command you ran and its result. Required by the governance directive. -->

```
```

## Checklist

- [ ] Local test suite passes
- [ ] No cross-module boundary violations introduced
- [ ] Documentation updated if behaviour changed

Closes #
"""


def scaffold_directory_structure(target_dir: str):
    """Creates standard directory tree with .gitkeep so empty dirs survive git."""
    dirs = [
        "src",
        "tests",
        "skills",
        "scripts",
        "docs",
        ".github/workflows",
        ".github/ISSUE_TEMPLATE",
    ]
    for d in dirs:
        path = os.path.join(target_dir, d)
        os.makedirs(path, exist_ok=True)
        if d in ("src", "tests", "skills", "scripts"):
            keep = os.path.join(path, ".gitkeep")
            if not os.listdir(path):
                open(keep, "a").close()
    print("✅ Standard directory structure scaffolded.")


def create_gitignore(target_dir: str):
    path = os.path.join(target_dir, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(DEFAULT_GITIGNORE.strip() + "\n")
        print("✅ .gitignore created.")
    else:
        print("[INFO] .gitignore already exists; left untouched.")


def create_agents_md(target_dir: str, project_name: str, test_runner: str):
    path = os.path.join(target_dir, "AGENTS.md")
    if os.path.exists(path):
        print("[INFO] AGENTS.md already exists; left untouched.")
        return
    content = DEFAULT_AGENTS_TEMPLATE.format(project_name=project_name, test_runner=test_runner)
    with open(path, "w") as f:
        f.write(content.strip() + "\n")
    print("✅ Project-specific AGENTS.md generated.")


def write_ci_workflow(target_dir: str, test_runner: str):
    """SKILL.md step 4. Was previously unimplemented."""
    path = os.path.join(target_dir, ".github", "workflows", "ci.yml")
    with open(path, "w") as f:
        f.write(CI_WORKFLOW.format(test_runner=test_runner))
    print("✅ CI workflow written to .github/workflows/ci.yml")


def write_templates(target_dir: str):
    """SKILL.md step 6 (issue templates) + PR template. Previously unimplemented."""
    tpl_dir = os.path.join(target_dir, ".github", "ISSUE_TEMPLATE")
    for name, body in [
        ("feature.md", ISSUE_TEMPLATE_FEATURE),
        ("task.md", ISSUE_TEMPLATE_TASK),
        ("bug.md", ISSUE_TEMPLATE_BUG),
        ("epic.md", ISSUE_TEMPLATE_EPIC),
    ]:
        with open(os.path.join(tpl_dir, name), "w") as f:
            f.write(body)
    with open(os.path.join(target_dir, ".github", "pull_request_template.md"), "w") as f:
        f.write(PR_TEMPLATE)
    print("✅ Issue templates and PR template written.")


def init_git_repo(target_dir: str):
    if os.path.isdir(os.path.join(target_dir, ".git")):
        print("[INFO] Git repository already initialized.")
        return
    run_cmd(["git", "init", "-b", "main"], cwd=target_dir, check=False)
    print("✅ Git repository initialized on branch 'main'.")


def initial_commit(target_dir: str, project_name: str) -> bool:
    """SKILL.md step 7. Previously unimplemented."""
    run_cmd(["git", "add", "-A"], cwd=target_dir, check=False)
    code, out, _ = run_cmd(["git", "status", "--porcelain"], cwd=target_dir, check=False)
    if not out:
        print("[INFO] Nothing to commit.")
        return False
    msg = (
        f"feat: initialize {project_name} under Aru_Agentic_SDLC governance\n\n"
        "Scaffolds directory layout, AGENTS.md governance, CI pipeline, and\n"
        "issue/PR templates. Baseline commit prior to any tracked issue work.\n\n"
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
    )
    code, _, err = run_cmd(["git", "commit", "-m", msg], cwd=target_dir, check=False)
    if code == 0:
        print("✅ Initial commit created.")
        return True
    print(f"[WARN] Initial commit failed: {err}")
    return False


def create_github_private_repo(repo_name: str, private: bool, target_dir: str) -> bool:
    """SKILL.md step 5. Defined but never called in the previous version."""
    code, existing, _ = run_cmd(["git", "remote", "get-url", "origin"], cwd=target_dir, check=False)
    if code == 0 and existing:
        print(f"[INFO] Remote 'origin' already set to {existing}; skipping repo creation.")
        return True

    vis_flag = "--private" if private else "--public"
    print(f"Creating GitHub {'private' if private else 'public'} repository '{repo_name}'...")
    cmd = ["gh", "repo", "create", repo_name, vis_flag, "--source", ".", "--remote", "origin", "--push"]
    code, out, err = run_cmd(cmd, cwd=target_dir, check=False)
    if code == 0:
        print(f"✅ GitHub repository '{repo_name}' created, linked to origin, and pushed.")
        return True
    print(f"[ERROR] gh repo create failed: {err or out}", file=sys.stderr)
    return False


def create_labels(target_dir: str):
    """Governance labels the workflow scripts depend on. --force makes this idempotent."""
    created = 0
    for name, color, desc in GOVERNANCE_LABELS:
        code, _, err = run_cmd(
            ["gh", "label", "create", name, "--color", color, "--description", desc, "--force"],
            cwd=target_dir,
            check=False,
        )
        if code == 0:
            created += 1
    print(f"✅ Governance labels provisioned ({created}/{len(GOVERNANCE_LABELS)}).")


def create_project_board(owner: str, title: str) -> Optional[Tuple[int, str]]:
    """Creates a Project v2 and returns (number, node_id).

    The previous version omitted --format json, so run_gh_json always returned None
    and the project number was discarded.
    """
    print(f"Provisioning GitHub Project board '{title}'...")
    res = run_gh_json(["gh", "project", "create", "--owner", owner, "--title", title, "--format", "json"])
    if not res or "number" not in res:
        print("[ERROR] Project board creation failed or returned no number.", file=sys.stderr)
        return None
    print(f"✅ Project board #{res['number']} created.")
    return res["number"], res.get("id", "")


def configure_board(number: int, owner: str) -> bool:
    """Renames the built-in Status options to the five-status contract and adds
    Priority / Story Points / Phase fields. Without this the board ships GitHub's
    default Todo/In Progress/Done and three of the five statuses the workflow
    scripts use do not exist."""
    fields = run_gh_json(["gh", "project", "field-list", str(number), "--owner", owner, "--format", "json"])
    if not fields:
        print("[ERROR] Could not list project fields.", file=sys.stderr)
        return False

    field_list = fields.get("fields", fields if isinstance(fields, list) else [])
    status_field = next((f for f in field_list if f.get("name") == "Status"), None)
    if not status_field:
        print("[ERROR] No Status field found on the project.", file=sys.stderr)
        return False

    opts = ", ".join(
        f'{{name: "{n}", color: {c}, description: "{d}"}}' for n, c, d in BOARD_STATUSES
    )
    mutation = f"""
    mutation {{
      updateProjectV2Field(input: {{
        fieldId: "{status_field['id']}"
        singleSelectOptions: [{opts}]
      }}) {{
        projectV2Field {{
          ... on ProjectV2SingleSelectField {{ id name options {{ name }} }}
        }}
      }}
    }}
    """
    code, out, err = run_cmd(["gh", "api", "graphql", "-f", f"query={mutation}"], check=False)
    if code != 0:
        print(f"[ERROR] Failed to configure Status options: {err or out}", file=sys.stderr)
        return False
    print("✅ Status options set: " + " → ".join(n for n, _, _ in BOARD_STATUSES))

    existing_names = {f.get("name") for f in field_list}
    custom = [
        ("Priority", "SINGLE_SELECT", "P0,P1,P2,P3"),
        ("Story Points", "NUMBER", None),
        ("Phase", "SINGLE_SELECT", "Phase -1,Phase 0,Phase 1,Phase 2,Phase 3,Phase 4,Phase 5,Migration"),
    ]
    for name, dtype, options in custom:
        if name in existing_names:
            print(f"[INFO] Field '{name}' already exists; skipping.")
            continue
        cmd = ["gh", "project", "field-create", str(number), "--owner", owner,
               "--name", name, "--data-type", dtype]
        if options:
            cmd += ["--single-select-options", options]
        code, out, err = run_cmd(cmd, check=False)
        if code == 0:
            print(f"✅ Field '{name}' created.")
        else:
            print(f"[WARN] Field '{name}' not created: {err or out}")
    return True


def link_project_to_repo(number: int, owner: str, repo_slug: str, target_dir: str):
    code, out, err = run_cmd(
        ["gh", "project", "link", str(number), "--owner", owner, "--repo", repo_slug],
        cwd=target_dir, check=False,
    )
    if code == 0:
        print(f"✅ Project #{number} linked to {repo_slug}.")
    else:
        print(f"[WARN] Could not link project to repo: {err or out}")


def main():
    parser = argparse.ArgumentParser(description="Bootstrap a new repository under Aru_Agentic_SDLC governance.")
    parser.add_argument("--name", type=str, required=True, help="Project name (also the GitHub repo name)")
    parser.add_argument("--public", action="store_true", help="Create a PUBLIC repository (default is private)")
    parser.add_argument("--stack", type=str, default="python", help="Primary tech stack")
    parser.add_argument("--test-runner", type=str, default="pytest -q", help="Test execution command")
    parser.add_argument("--create-board", action="store_true", help="Create and configure the GitHub Project board")
    parser.add_argument("--owner", type=str, default="@me", help="GitHub owner for the board")
    parser.add_argument("--target-dir", type=str, default=".", help="Target directory path")
    parser.add_argument("--no-remote", action="store_true", help="Scaffold and commit locally; skip GitHub repo creation")
    args = parser.parse_args()

    private = not args.public
    target = args.target_dir

    print(f"=== Initializing Agentic Project '{args.name}' ===")
    scaffold_directory_structure(target)
    create_gitignore(target)
    create_agents_md(target, args.name, args.test_runner)
    write_ci_workflow(target, args.test_runner)
    write_templates(target)
    init_git_repo(target)
    initial_commit(target, args.name)

    if args.no_remote:
        print("🚀 Local scaffold complete (--no-remote). Skipped GitHub repo, labels, and board.")
        return

    if not create_github_private_repo(args.name, private, target):
        print("[FATAL] Repository creation failed; skipping labels and board.", file=sys.stderr)
        sys.exit(1)

    create_labels(target)

    if args.create_board:
        board = create_project_board(args.owner, f"{args.name} Board")
        if board:
            number, _ = board
            configure_board(number, args.owner)
            code, slug, _ = run_cmd(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                cwd=target, check=False,
            )
            if code == 0 and slug:
                link_project_to_repo(number, args.owner, slug, target)

    print(f"🚀 Project '{args.name}' successfully initialized under Aru_Agentic_SDLC governance!")


if __name__ == "__main__":
    main()
