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


# --- GitHub Project v2 board helpers --------------------------------------
# The board is the monitoring surface; the status:* labels are what the CLI
# reads. Both must move together or they drift. These helpers exist so
# claim_issue.py and update_issue_status.py can move the board item too.


def get_repo_slug() -> Optional[str]:
    """Returns 'owner/repo' for the current working directory's repo."""
    cmd = ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    code, stdout, _ = run_cmd(cmd, check=False)
    return stdout or None


def get_issue_project_items(issue_number: int) -> List[Dict[str, Any]]:
    """Returns every project item for an issue, with the project's Status field
    and its available options resolved in one round trip."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return []
    owner, repo = slug.split("/", 1)

    query = """
    query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) {
        issue(number:$number) {
          id
          url
          projectItems(first:10) {
            nodes {
              id
              project {
                id
                number
                title
                field(name:"Status") {
                  ... on ProjectV2SingleSelectField {
                    id
                    options { id name }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"repo={repo}",
        "-F", f"number={issue_number}",
    ]
    res = run_gh_json(cmd)
    if not res:
        return []
    try:
        return res["data"]["repository"]["issue"]["projectItems"]["nodes"]
    except (KeyError, TypeError):
        return []


def set_board_status(issue_number: int, status: str) -> bool:
    """Moves an issue's board item(s) to the named Status option.

    Returns True only if at least one board item actually moved, so callers can
    tell the difference between 'moved' and 'issue is not on any board'.
    """
    items = get_issue_project_items(issue_number)
    if not items:
        return False

    moved = False
    for item in items:
        project = item.get("project") or {}
        field = project.get("field") or {}
        field_id = field.get("id")
        if not field_id:
            continue
        option = next(
            (o for o in field.get("options", []) if o.get("name", "").lower() == status.lower()),
            None,
        )
        if not option:
            print(
                f"[WARN] Project '{project.get('title')}' has no Status option "
                f"'{status}'. Available: {[o['name'] for o in field.get('options', [])]}",
                file=sys.stderr,
            )
            continue

        mutation = """
        mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
          updateProjectV2ItemFieldValue(input:{
            projectId:$project, itemId:$item, fieldId:$field,
            value:{ singleSelectOptionId:$option }
          }) { projectV2Item { id } }
        }
        """
        cmd = [
            "gh", "api", "graphql",
            "-f", f"query={mutation}",
            "-F", f"project={project['id']}",
            "-F", f"item={item['id']}",
            "-F", f"field={field_id}",
            "-F", f"option={option['id']}",
        ]
        code, _, err = run_cmd(cmd, check=False)
        if code == 0:
            moved = True
        else:
            print(f"[WARN] Board move failed for project '{project.get('title')}': {err}", file=sys.stderr)
    return moved


def add_issue_to_project(issue_number: int, project_number: int, owner: str = "@me") -> bool:
    """Adds an issue to a project board. Idempotent - re-adding is a no-op."""
    slug = get_repo_slug()
    if not slug:
        return False
    url = f"https://github.com/{slug}/issues/{issue_number}"
    code, out, err = run_cmd(
        ["gh", "project", "item-add", str(project_number), "--owner", owner, "--url", url],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not add issue #{issue_number} to project #{project_number}: {err or out}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    print("Aru_Agentic_SDLC Common Utilities Loaded Cleanly.")
