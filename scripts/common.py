#!/usr/bin/env python3
"""
common.py - Shared GitHub and Git automation utilities for Aru_Agentic_SDLC scripts.
Provides robust execution of gh CLI commands, git worktree management, and API wrappers.
"""

import fnmatch
import json
import os
import random
import re
import subprocess
import sys
import time
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


def create_worktree(branch_name: str, path: str = None, attempts: int = 5) -> str:
    """Creates a git worktree for isolated feature development/review.

    Retries with backoff: concurrent agents in one clone contend on
    .git/index.lock, and `git worktree add` fails transiently rather than
    waiting.
    """
    if not path:
        path = os.path.join(".worktrees", branch_name.replace("/", "-"))
    os.makedirs(os.path.dirname(path), exist_ok=True)

    for attempt in range(attempts):
        code, _, stderr = run_cmd(["git", "worktree", "add", "-b", branch_name, path], check=False)
        if code == 0:
            print(f"✅ Git worktree initialized at: '{path}'")
            return path

        # Branch already exists (resume) - attach the worktree to it instead.
        code2, _, _ = run_cmd(["git", "worktree", "add", path, branch_name], check=False)
        if code2 == 0:
            print(f"✅ Git worktree attached to existing branch at: '{path}'")
            return path

        if "lock" not in stderr.lower() or attempt == attempts - 1:
            break
        sleep_s = 0.5 * (2 ** attempt) + random.random() * 0.3
        print(f"[INFO] git lock contention; retrying in {sleep_s:.1f}s "
              f"({attempt + 1}/{attempts})", file=sys.stderr)
        time.sleep(sleep_s)

    print(f"[ERROR] Could not create worktree at '{path}': {stderr}", file=sys.stderr)
    return path


def list_open_issues() -> List[Dict[str, Any]]:
    """Fetches list of open issues via gh CLI.

    --limit is explicit: gh defaults to 30, which silently truncates any board
    with more issues than that and makes the dependency graph wrong.
    """
    cmd = ["gh", "issue", "list", "--state", "open", "--limit", "500",
           "--json", "number,title,labels,assignees,body,state,updatedAt"]
    res = run_gh_json(cmd)
    return res if isinstance(res, list) else []


# --- Concurrency primitives -----------------------------------------------
# GitHub offers no compare-and-swap on issue assignment, so claiming is
# optimistic: write, read back, and resolve any race with a deterministic
# tie-break on agent id. See claim_issue.py for the protocol.

AGENT_LABEL_PREFIX = "agent:"
ACTIVE_STATUS_LABELS = {"status:in-progress", "status:in-review"}


def label_names(issue: Dict[str, Any]) -> List[str]:
    return [l.get("name", "") for l in issue.get("labels", [])]


def agent_labels(issue: Dict[str, Any]) -> List[str]:
    """Returns every agent:* label on an issue. More than one means a race."""
    return sorted(n for n in label_names(issue) if n.startswith(AGENT_LABEL_PREFIX))


def claimed_by(issue: Dict[str, Any]) -> Optional[str]:
    """Returns the winning agent id for an issue, or None if unclaimed.

    When two agents raced, the lowest-sorting label wins. Both agents compute
    the same winner from the same data, so no coordinator is required.
    """
    labels = agent_labels(issue)
    if labels:
        return labels[0][len(AGENT_LABEL_PREFIX):]
    if ACTIVE_STATUS_LABELS & set(label_names(issue)):
        return "unknown"  # in flight but pre-dates agent labelling
    return None


def ensure_label(name: str, color: str = "5319e7", description: str = "") -> bool:
    """Creates a label if absent. gh issue edit --add-label fails on unknown
    labels, and agent:* labels are created on demand."""
    code, _, _ = run_cmd(
        ["gh", "label", "create", name, "--color", color, "--description", description, "--force"],
        check=False,
    )
    return code == 0


def parse_touches(body: str) -> List[str]:
    """Parses 'touches: src/a/*, docs/b.md' from an issue body.

    Declares which paths an issue will modify so the picker can refuse to hand
    two agents work that collides on the same files. `parallel-eligible` only
    means 'no unresolved depends-on'; it says nothing about file conflicts.
    """
    if not body:
        return []
    match = re.search(r"touches\s*:\s*([^\n]*)", body, re.IGNORECASE)
    if not match:
        return []
    return [p.strip() for p in match.group(1).split(",") if p.strip()]


def _norm_path(p: str) -> str:
    return p.strip().strip("/")


def paths_overlap(a: str, b: str) -> bool:
    """True if two path patterns could touch the same file.

    Deliberately errs toward declaring a conflict: a false positive costs
    serialisation, a false negative costs a merge conflict.
    """
    a, b = _norm_path(a), _norm_path(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    # Directory containment: "docs/contracts/*" vs "docs/contracts/X.md"
    ap = a.split("*")[0].rstrip("/")
    bp = b.split("*")[0].rstrip("/")
    if ap and bp and (ap == bp or ap.startswith(bp + "/") or bp.startswith(ap + "/")):
        return True
    return False


def touches_conflict(a_paths: List[str], b_paths: List[str]) -> Optional[Tuple[str, str]]:
    """Returns the first conflicting pair, or None. An issue that declares no
    touches is treated as conflicting with nothing - undeclared work is the
    author's responsibility, not the picker's."""
    for a in a_paths:
        for b in b_paths:
            if paths_overlap(a, b):
                return (a, b)
    return None


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
