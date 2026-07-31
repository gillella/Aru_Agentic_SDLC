#!/usr/bin/env python3
"""
common.py - Shared GitHub and Git automation utilities for Aru_Agentic_SDLC scripts.
Provides robust execution of gh CLI commands, git worktree management, and API wrappers.
"""

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple


def run_cmd(cmd: List[str], check: bool = True, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Runs a system command and returns (returncode, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
        if check and res.returncode != 0:
            print(f"[ERROR] Command failed ({' '.join(cmd)}):\n{res.stderr.strip()}", file=sys.stderr)
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        if check:
            print(f"[EXCEPT] Exception running command ({' '.join(cmd)}): {e}", file=sys.stderr)
        return 1, "", str(e)


def run_gh_json(cmd: List[str]) -> Optional[Any]:
    """Runs a gh CLI command and parses JSON output."""
    code, stdout, stderr = run_cmd(cmd, check=False)
    if code != 0 or not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        print(f"[WARN] Failed to parse JSON from gh CLI: {stdout}", file=sys.stderr)
        return None


def get_current_branch() -> str:
    """Returns the current git branch name."""
    _, stdout, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    return stdout or "main"


def create_worktree(branch_name: str, path: str = None) -> str:
    """Creates a git worktree directory for isolated feature development/review."""
    if not path:
        path = os.path.join(".worktrees", branch_name.replace("/", "-"))
    os.makedirs(os.path.dirname(path), exist_ok=True)

    code, stdout, stderr = run_cmd(["git", "worktree", "add", "-b", branch_name, path], check=False)
    if code != 0:
        # Branch might already exist, checkout existing branch in worktree
        run_cmd(["git", "worktree", "add", path, branch_name], check=False)
    print(f"✅ Git worktree initialized at: '{path}'")
    return path


def list_open_issues() -> List[Dict[str, Any]]:
    """Fetches list of open issues via gh CLI."""
    cmd = ["gh", "issue", "list", "--state", "open", "--json", "number,title,labels,assignees,body,state"]
    res = run_gh_json(cmd)
    return res if isinstance(res, list) else []


def get_issue(issue_id: int) -> Optional[Dict[str, Any]]:
    """Fetches single issue details via gh CLI."""
    cmd = ["gh", "issue", "view", str(issue_id), "--json", "number,title,labels,assignees,body,state"]
    res = run_gh_json(cmd)
    return res if isinstance(res, dict) else None


def fetch_pr_comments(pr_id: int) -> List[Dict[str, Any]]:
    """Fetches inline review comments for a Pull Request."""
    cmd = ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_id}/comments"]
    res = run_gh_json(cmd)
    return res if isinstance(res, list) else []


if __name__ == "__main__":
    print("Aru_Agentic_SDLC Common Utilities Loaded Cleanly.")
