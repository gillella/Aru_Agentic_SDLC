#!/usr/bin/env python3
"""
common.py - Shared GitHub and Git automation utilities for Aru_Agentic_SDLC scripts.
Provides robust execution of gh CLI commands and fallback git commands.
"""

import json
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple


def run_cmd(cmd: List[str], check: bool = True) -> Tuple[int, str, str]:
    """Runs a system command and returns (returncode, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


def get_git_status() -> str:
    """Returns brief git status."""
    _, stdout, _ = run_cmd(["git", "status", "--porcelain"], check=False)
    return stdout


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


if __name__ == "__main__":
    print("Aru_Agentic_SDLC Common Utilities Loaded Cleanly.")
