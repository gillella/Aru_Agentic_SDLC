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

BASE_GITIGNORE = """# Worktrees & Temporary Logs
.worktrees/
*.log
.DS_Store

# Secrets
.env
.env.*
!.env.example
"""

STACK_GITIGNORE = {
    "python": """# Python
__pycache__/
*.py[cod]
*$py.class
.venv/
env/
venv/
dist/
build/
""",
    "node": """# Node.js / TypeScript / React
node_modules/
dist/
build/
coverage/
.next/
""",
    "go": """# Go
bin/
*.test
coverage.out
""",
}


def render_gitignore(stack: str) -> str:
    normalized = stack.strip().lower()
    family = "node" if normalized in {"node", "nodejs", "typescript", "react"} else normalized
    return STACK_GITIGNORE[family].strip() + "\n\n" + BASE_GITIGNORE.strip() + "\n"

DEFAULT_AGENTS_TEMPLATE = """# AGENTS.md - Master AI Engineering Playbook & Operating Directives

Welcome to **{project_name}**. This repository operates under **Aru_Agentic_SDLC** governance.

Primary technology stack: **{stack}**.

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
touches: src/**, tests/**    (all paths this issue may modify)
```

Board statuses, in order: `Backlog` → `Ready` → `In Progress` → `In Review` → `Done`.
Each is mirrored by a `status:*` label so the CLI and the board stay in sync.
"""

CI_WORKFLOW_HEADER = """name: CI

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
"""

PYTHON_CI_STEPS = """

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
          if [ -f pyproject.toml ]; then pip install -e .; fi

      - name: Lint
        run: |
          pip install ruff
          ruff check .

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
          if find tests -type f ! -name '.gitkeep' -print -quit 2>/dev/null | grep -q .; then
            pip install pytest
            {test_runner}
          else
            echo "No tests present yet; skipping. This step becomes mandatory at first source commit."
          fi
"""

NODE_CI_STEPS = """

      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '22'

      - name: Install dependencies
        run: |
          if [ -f package-lock.json ]; then
            npm ci
          elif [ -f package.json ]; then
            npm install
          else
            echo "No package.json present yet; skipping dependency installation."
          fi

      - name: Lint
        run: |
          if [ -f package.json ]; then npm run lint --if-present; fi

      - name: Tests
        run: |
          if [ -f package.json ]; then
            {test_runner}
          else
            echo "No package.json present yet; skipping. This step becomes mandatory at first source commit."
          fi
"""

GO_CI_STEPS = """

      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version: 'stable'

      - name: Lint and compile
        run: |
          if [ -f go.mod ]; then go vet ./...; fi

      - name: Tests
        run: |
          if [ -f go.mod ]; then
            {test_runner}
          else
            echo "No go.mod present yet; skipping. This step becomes mandatory at first source commit."
          fi
"""

DEFAULT_TEST_RUNNERS = {
    "python": "pytest -q",
    "node": "npm test",
    "nodejs": "npm test",
    "typescript": "npm test",
    "react": "npm test",
    "go": "go test ./...",
}


def render_ci_workflow(stack: str, test_runner: str) -> str:
    """Renders a CI workflow whose setup and test gate match the chosen stack."""
    normalized = stack.strip().lower()
    if normalized not in DEFAULT_TEST_RUNNERS:
        supported = ", ".join(sorted(DEFAULT_TEST_RUNNERS))
        raise ValueError(f"Unsupported stack '{stack}'. Expected one of: {supported}")
    if not test_runner.strip() or "\n" in test_runner or "\r" in test_runner:
        raise ValueError("--test-runner must be one non-empty shell command")

    if normalized == "python":
        steps = PYTHON_CI_STEPS
    elif normalized in {"node", "nodejs", "typescript", "react"}:
        steps = NODE_CI_STEPS
    else:
        steps = GO_CI_STEPS
    return CI_WORKFLOW_HEADER + steps.format(test_runner=test_runner.strip())

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


def create_gitignore(target_dir: str, stack: str = "python"):
    path = os.path.join(target_dir, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(render_gitignore(stack))
        print("✅ .gitignore created.")
    else:
        print("[INFO] .gitignore already exists; left untouched.")


def create_agents_md(
    target_dir: str,
    project_name: str,
    test_runner: str,
    stack: str = "python",
):
    path = os.path.join(target_dir, "AGENTS.md")
    if os.path.exists(path):
        print("[INFO] AGENTS.md already exists; left untouched.")
        return
    content = DEFAULT_AGENTS_TEMPLATE.format(
        project_name=project_name,
        stack=stack,
        test_runner=test_runner,
    )
    with open(path, "w") as f:
        f.write(content.strip() + "\n")
    print("✅ Project-specific AGENTS.md generated.")


def write_ci_workflow(target_dir: str, test_runner: str, stack: str = "python"):
    """SKILL.md step 4. Was previously unimplemented."""
    path = os.path.join(target_dir, ".github", "workflows", "ci.yml")
    with open(path, "w") as f:
        f.write(render_ci_workflow(stack, test_runner))
    print("✅ CI workflow written to .github/workflows/ci.yml")


def render_issue_form(
    name: str,
    description: str,
    title: str,
    labels: list[str],
    fields: list[tuple[str, str]],
    project_ref: Optional[str] = None,
) -> str:
    """Renders a structured issue form with the concurrency metadata contract."""
    project_line = f'projects: ["{project_ref}"]' if project_ref else "projects: []"
    lines = [
        f"name: {name}",
        f"description: {description}",
        f"title: '{title}'",
        "labels: [" + ", ".join(f'"{label}"' for label in labels) + "]",
        project_line,
        "body:",
    ]
    for field_id, label in fields:
        lines.extend([
            "  - type: textarea",
            f"    id: {field_id}",
            "    attributes:",
            f"      label: {label}",
            "    validations:",
            "      required: true",
        ])
    lines.extend([
        "  - type: textarea",
        "    id: workflow-metadata",
        "    attributes:",
        "      label: Workflow metadata",
        "      description: Declare dependencies and every path this issue may modify.",
        "      value: |",
        "        depends-on:",
        "        touches:",
        "        parallel-eligible: false",
        "    validations:",
        "      required: true",
    ])
    return "\n".join(lines) + "\n"


def write_templates(target_dir: str, project_ref: Optional[str] = None):
    """Writes issue forms and a PR template.

    ``projects`` on an issue form automatically adds issues created from the
    form to the configured Project v2 board for users with write access.
    """
    tpl_dir = os.path.join(target_dir, ".github", "ISSUE_TEMPLATE")
    for filename, body in [
        ("feature.yml", render_issue_form(
            "Feature", "A new capability originating from the project plan", "feat: ",
            ["type:feat", "status:backlog"],
            [("summary", "Summary"), ("background", "Background"),
             ("acceptance", "Acceptance criteria"), ("verification", "Verification")],
            project_ref,
        )),
        ("task.yml", render_issue_form(
            "Task", "Tooling, CI, specification, or maintenance work", "chore: ",
            ["type:chore", "status:backlog"],
            [("summary", "Summary"), ("acceptance", "Acceptance criteria"),
             ("verification", "Verification")],
            project_ref,
        )),
        ("bug.yml", render_issue_form(
            "Bug", "Something behaves incorrectly", "fix: ",
            ["type:fix", "status:backlog"],
            [("observed", "Observed behavior"), ("expected", "Expected behavior"),
             ("reproduction", "Reproduction"), ("verification", "Verification")],
            project_ref,
        )),
        ("epic.yml", render_issue_form(
            "Epic", "A phase-level container; not directly implementable", "epic: ",
            ["type:epic", "status:backlog"],
            [("phase", "Phase"), ("goal", "Goal"),
             ("gating", "Gating contract"), ("children", "Child issues")],
            project_ref,
        )),
    ]:
        with open(os.path.join(tpl_dir, filename), "w") as f:
            f.write(body)
    with open(os.path.join(target_dir, ".github", "pull_request_template.md"), "w") as f:
        f.write(PR_TEMPLATE)
    print("✅ Issue templates and PR template written.")


def init_git_repo(target_dir: str) -> bool:
    if os.path.isdir(os.path.join(target_dir, ".git")):
        print("[INFO] Git repository already initialized.")
        return True
    code, _, err = run_cmd(["git", "init", "-b", "main"], cwd=target_dir, check=False)
    if code != 0:
        print(f"[ERROR] Git initialization failed: {err}", file=sys.stderr)
        return False
    print("✅ Git repository initialized on branch 'main'.")
    return True


def initial_commit(target_dir: str, project_name: str) -> bool:
    """SKILL.md step 7. Previously unimplemented."""
    code, _, err = run_cmd(["git", "add", "-A"], cwd=target_dir, check=False)
    if code != 0:
        print(f"[ERROR] Could not stage bootstrap files: {err}", file=sys.stderr)
        return False
    code, out, err = run_cmd(["git", "status", "--porcelain"], cwd=target_dir, check=False)
    if code != 0:
        print(f"[ERROR] Could not inspect repository status: {err}", file=sys.stderr)
        return False
    if not out:
        print("[INFO] Nothing to commit.")
        return True
    msg = (
        f"feat: initialize {project_name} under Aru_Agentic_SDLC governance\n\n"
        "Scaffolds directory layout, AGENTS.md governance, CI pipeline, and\n"
        "issue/PR templates. Baseline commit prior to any tracked issue work."
    )
    code, _, err = run_cmd(["git", "commit", "-m", msg], cwd=target_dir, check=False)
    if code == 0:
        print("✅ Initial commit created.")
        return True
    print(f"[WARN] Initial commit failed: {err}")
    return False


def commit_project_template_link(target_dir: str, project_ref: str) -> bool:
    """Commits and pushes the board reference added to generated issue forms."""
    write_templates(target_dir, project_ref)
    code, _, err = run_cmd(
        ["git", "add", ".github/ISSUE_TEMPLATE", ".github/pull_request_template.md"],
        cwd=target_dir,
        check=False,
    )
    if code != 0:
        print(f"[ERROR] Could not stage project-linked templates: {err}", file=sys.stderr)
        return False
    code, _, err = run_cmd(
        ["git", "commit", "-m", "chore: link issue forms to project board"],
        cwd=target_dir,
        check=False,
    )
    if code != 0:
        print(f"[ERROR] Could not commit project-linked templates: {err}", file=sys.stderr)
        return False
    code, out, err = run_cmd(
        ["git", "push", "origin", "main"],
        cwd=target_dir,
        check=False,
    )
    if code != 0:
        print(f"[ERROR] Could not push project-linked templates: {err or out}", file=sys.stderr)
        return False
    print(f"✅ Issue forms linked to project '{project_ref}' and pushed.")
    return True


def create_github_private_repo(repo_name: str, private: bool, target_dir: str) -> bool:
    """SKILL.md step 5. Defined but never called in the previous version."""
    code, existing, _ = run_cmd(["git", "remote", "get-url", "origin"], cwd=target_dir, check=False)
    if code == 0 and existing:
        print(f"[INFO] Remote 'origin' already set to {existing}; pushing bootstrap commit.")
        push_code, out, err = run_cmd(
            ["git", "push", "-u", "origin", "main"],
            cwd=target_dir,
            check=False,
        )
        if push_code == 0:
            print("✅ Bootstrap commit pushed to existing origin.")
            return True
        print(f"[ERROR] Initial push failed: {err or out}", file=sys.stderr)
        return False

    vis_flag = "--private" if private else "--public"
    print(f"Creating GitHub {'private' if private else 'public'} repository '{repo_name}'...")
    cmd = ["gh", "repo", "create", repo_name, vis_flag, "--source", ".", "--remote", "origin", "--push"]
    code, out, err = run_cmd(cmd, cwd=target_dir, check=False)
    if code == 0:
        print(f"✅ GitHub repository '{repo_name}' created, linked to origin, and pushed.")
        return True
    print(f"[ERROR] gh repo create failed: {err or out}", file=sys.stderr)
    return False


def create_labels(target_dir: str) -> bool:
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
    if created != len(GOVERNANCE_LABELS):
        print(
            f"[ERROR] Governance label provisioning incomplete ({created}/{len(GOVERNANCE_LABELS)}).",
            file=sys.stderr,
        )
        return False
    print(f"✅ Governance labels provisioned ({created}/{len(GOVERNANCE_LABELS)}).")
    return True


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


def configure_project_views(project_id: str) -> bool:
    """Creates the three views promised by the bootstrap contract.

    GitHub creates one table view by default.  Reuse it for Kanban so repeated
    runs do not leave a stray ``View 1``, then create or repair the Backlog and
    Sprint views through the ProjectV2 view mutations.
    """
    if not project_id:
        print("[ERROR] Project node id is required to configure views.", file=sys.stderr)
        return False

    query = """
    query($project:ID!) {
      node(id:$project) {
        ... on ProjectV2 { views(first:20) { nodes { id name layout } } }
      }
    }
    """
    views_res = run_gh_json([
        "gh", "api", "graphql", "-f", f"query={query}", "-F", f"project={project_id}",
    ])
    try:
        views = views_res["data"]["node"]["views"]["nodes"]
    except (KeyError, TypeError):
        print("[ERROR] Could not list project views.", file=sys.stderr)
        return False

    desired = [
        ("Kanban", "BOARD_LAYOUT"),
        ("Jira-Style Backlog", "TABLE_LAYOUT"),
        ("Sprint", "TABLE_LAYOUT"),
    ]
    ok = True
    for index, (name, layout) in enumerate(desired):
        existing = next((v for v in views if v.get("name", "").lower() == name.lower()), None)
        if not existing and index == 0 and views:
            existing = views[0]

        if existing:
            mutation = f"""
            mutation($view:ID!) {{
              updateProjectV2View(input:{{viewId:$view, name:\"{name}\", layout:{layout}}}) {{
                projectV2View {{ id name layout }}
              }}
            }}
            """
            cmd = [
                "gh", "api", "graphql", "-f", f"query={mutation}",
                "-F", f"view={existing['id']}",
            ]
        else:
            mutation = f"""
            mutation($project:ID!) {{
              createProjectV2View(input:{{projectId:$project, name:\"{name}\", layout:{layout}}}) {{
                projectV2View {{ id name layout }}
              }}
            }}
            """
            cmd = [
                "gh", "api", "graphql", "-f", f"query={mutation}",
                "-F", f"project={project_id}",
            ]

        code, out, err = run_cmd(cmd, check=False)
        if code == 0:
            print(f"✅ Project view '{name}' configured ({layout}).")
        else:
            print(f"[ERROR] Project view '{name}' failed: {err or out}", file=sys.stderr)
            ok = False
    return ok


def configure_board(number: int, owner: str, project_id: str = "") -> bool:
    """Renames the built-in Status options to the five-status contract and adds
    Priority / Story Points / Phase fields. Without this the board ships GitHub's
    default Todo/In Progress/Done and three of the five statuses the workflow
    scripts use do not exist."""
    if not project_id:
        project = run_gh_json([
            "gh", "project", "view", str(number), "--owner", owner, "--format", "json",
        ])
        project_id = project.get("id", "") if isinstance(project, dict) else ""

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
    custom_ok = True
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
            custom_ok = False
    return custom_ok and configure_project_views(project_id)


def link_project_to_repo(number: int, owner: str, repo_slug: str, target_dir: str) -> bool:
    code, out, err = run_cmd(
        ["gh", "project", "link", str(number), "--owner", owner, "--repo", repo_slug],
        cwd=target_dir, check=False,
    )
    if code == 0:
        print(f"✅ Project #{number} linked to {repo_slug}.")
        return True
    else:
        print(f"[ERROR] Could not link project to repo: {err or out}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Bootstrap a new repository under Aru_Agentic_SDLC governance.")
    parser.add_argument("--name", type=str, required=True, help="Project name (also the GitHub repo name)")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument(
        "--private", dest="private", action="store_true",
        help="Create a private repository (default)",
    )
    visibility.add_argument(
        "--public", dest="private", action="store_false",
        help="Create a public repository",
    )
    parser.set_defaults(private=True)
    parser.add_argument(
        "--stack", type=str, default="python", choices=sorted(DEFAULT_TEST_RUNNERS),
        help="Primary technology stack",
    )
    parser.add_argument(
        "--test-runner", type=str, default=None,
        help="Test command (defaults to the selected stack's standard runner)",
    )
    parser.add_argument("--create-board", action="store_true", help="Create and configure the GitHub Project board")
    parser.add_argument("--owner", type=str, default="@me", help="GitHub owner for the board")
    parser.add_argument("--target-dir", type=str, default=".", help="Target directory path")
    parser.add_argument("--no-remote", action="store_true", help="Scaffold and commit locally; skip GitHub repo creation")
    args = parser.parse_args()

    private = args.private
    target = args.target_dir
    test_runner = args.test_runner or DEFAULT_TEST_RUNNERS[args.stack]

    try:
        render_ci_workflow(args.stack, test_runner)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"=== Initializing Agentic Project '{args.name}' ===")
    scaffold_directory_structure(target)
    create_gitignore(target, args.stack)
    create_agents_md(target, args.name, test_runner, args.stack)
    write_ci_workflow(target, test_runner, args.stack)
    write_templates(target)
    if not init_git_repo(target) or not initial_commit(target, args.name):
        print("[FATAL] Local repository bootstrap failed.", file=sys.stderr)
        sys.exit(1)

    if args.no_remote:
        print("🚀 Local scaffold complete (--no-remote). Skipped GitHub repo, labels, and board.")
        return

    if not create_github_private_repo(args.name, private, target):
        print("[FATAL] Repository creation failed; skipping labels and board.", file=sys.stderr)
        sys.exit(1)

    if not create_labels(target):
        print("[FATAL] Governance labels could not be provisioned.", file=sys.stderr)
        sys.exit(1)

    if args.create_board:
        board = create_project_board(args.owner, f"{args.name} Board")
        if not board:
            print("[FATAL] Project board creation failed.", file=sys.stderr)
            sys.exit(1)
        number, project_id = board
        if not configure_board(number, args.owner, project_id):
            print(f"[FATAL] Project board #{number} configuration failed.", file=sys.stderr)
            sys.exit(1)
        code, slug, err = run_cmd(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=target, check=False,
        )
        if code != 0 or not slug or not link_project_to_repo(number, args.owner, slug, target):
            print(f"[FATAL] Project board #{number} could not be linked: {err}", file=sys.stderr)
            sys.exit(1)
        project_info = run_gh_json([
            "gh", "project", "view", str(number), "--owner", args.owner, "--format", "json",
        ])
        try:
            project_owner = project_info["owner"]["login"]
        except (KeyError, TypeError):
            print("[FATAL] Could not resolve the project owner for issue forms.", file=sys.stderr)
            sys.exit(1)
        if not commit_project_template_link(target, f"{project_owner}/{number}"):
            print("[FATAL] Issue forms could not be linked to the project.", file=sys.stderr)
            sys.exit(1)

    print(f"🚀 Project '{args.name}' successfully initialized under Aru_Agentic_SDLC governance!")


if __name__ == "__main__":
    main()
