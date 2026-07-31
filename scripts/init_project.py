#!/usr/bin/env python3
"""
init_project.py - Automation script for bootstrapping a brand-new repository under
Aru_Agentic_SDLC governance, scaffolding AGENTS.md, CI workflows, private GitHub repo,
and multi-view GitHub Project boards.
"""

import argparse
import os
import sys
from typing import Optional
from common import run_cmd, run_gh_json


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
"""

DEFAULT_AGENTS_TEMPLATE = """# AGENTS.md - Master AI Engineering Playbook & Operating Directives

Welcome to **{project_name}**. This repository operates under **Aru_Agentic_SDLC** governance.

All AI agents operating within this repository MUST follow the directives, skills, and tools defined herein.

---

## 🚨 Core Governance: The Issue-First Law

**No code change, refactor, or feature implementation may begin without first originating from a tracked issue on the GitHub Project Board.**

---

## 🎯 Primary Directives for AI Agents

1. **Execute via SkillsMP Skills**:
   - Primary Skill: `skills/implement-next-issue/SKILL.md`
   - Code Review Skill: `skills/code-review/SKILL.md`
   - CI Failure Remediation: `skills/remediate-ci-failure/SKILL.md`
   - PR Review Feedback: `skills/address-pr-feedback/SKILL.md`
2. **Worktree Isolation**:
   - Always run feature work inside `.worktrees/` directories to keep the main workspace clean.
3. **Local Test Verification First**:
   - Run `{test_runner}` and confirm all tests pass before committing.
4. **Mandatory Issue Linking**:
   - Every Pull Request MUST include `Closes #<issue_number>` in its body.
"""


def scaffold_directory_structure(target_dir: str):
    """Creates standard directory tree."""
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
        os.makedirs(os.path.join(target_dir, d), exist_ok=True)
    print("✅ Standard directory structure scaffolded.")


def create_gitignore(target_dir: str):
    """Generates .gitignore file."""
    path = os.path.join(target_dir, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(DEFAULT_GITIGNORE.strip() + "\n")
        print("✅ .gitignore created.")


def create_agents_md(target_dir: str, project_name: str, test_runner: str):
    """Generates project-specific AGENTS.md."""
    path = os.path.join(target_dir, "AGENTS.md")
    content = DEFAULT_AGENTS_TEMPLATE.format(project_name=project_name, test_runner=test_runner)
    with open(path, "w") as f:
        f.write(content.strip() + "\n")
    print("✅ Project-specific AGENTS.md generated.")


def init_git_repo(target_dir: str):
    """Initializes git repo."""
    run_cmd(["git", "init", "-b", "main"], cwd=target_dir, check=False)
    print("✅ Git repository initialized on branch 'main'.")


def create_github_private_repo(repo_name: str, private: bool = True, target_dir: str = "."):
    """Creates GitHub remote repository via gh CLI."""
    print(f"Creating GitHub {'Private' if private else 'Public'} Repository '{repo_name}'...")
    vis_flag = "--private" if private else "--public"
    cmd = ["gh", "repo", "create", repo_name, vis_flag, "--source", target_dir, "--remote", "origin"]
    code, out, err = run_cmd(cmd, check=False, cwd=target_dir)
    if code == 0:
        print(f"✅ GitHub repository '{repo_name}' created and linked to origin.")
    else:
        print(f"[INFO] gh repo create output: {out or err}")


def create_project_board(owner: str, title: str) -> Optional[int]:
    """Creates a GitHub Project v2 with fields via gh project CLI."""
    print(f"Provisioning Multi-View GitHub Project Board '{title}'...")
    cmd = ["gh", "project", "create", "--owner", owner, "--title", title]
    res = run_gh_json(cmd)
    if res and "number" in res:
        proj_num = res["number"]
        print(f"✅ GitHub Project Board #{proj_num} created successfully.")
        return proj_num
    print("[INFO] Project board creation via gh CLI finished.")
    return None


def main():
    parser = argparse.ArgumentParser(description="Bootstrap a new repository under Aru_Agentic_SDLC governance.")
    parser.add_argument("--name", type=str, required=True, help="Project name")
    parser.add_argument("--private", action="store_true", default=True, help="Create private GitHub repository")
    parser.add_argument("--stack", type=str, default="python", help="Primary tech stack")
    parser.add_argument("--test-runner", type=str, default="python -m unittest", help="Test execution command")
    parser.add_argument("--create-board", action="store_true", help="Create GitHub Project Board with multi-views")
    parser.add_argument("--owner", type=str, default="@me", help="GitHub owner for board")
    parser.add_argument("--target-dir", type=str, default=".", help="Target directory path")
    args = parser.parse_args()

    print(f"=== Initializing Agentic Project '{args.name}' ===")
    scaffold_directory_structure(args.target_dir)
    create_gitignore(args.target_dir)
    create_agents_md(args.target_dir, args.name, args.test_runner)
    init_git_repo(args.target_dir)

    if args.create_board:
        create_project_board(args.owner, f"{args.name} Board")

    print(f"🚀 Project '{args.name}' successfully initialized under Aru_Agentic_SDLC governance!")


if __name__ == "__main__":
    main()
