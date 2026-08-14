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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VERIFICATION_EVIDENCE_SCHEMA = "aru.verification.v1"
VERIFICATION_EVIDENCE_START = "<!-- aru-verification-evidence:v1 -->"
VERIFICATION_EVIDENCE_END = "<!-- /aru-verification-evidence -->"

_SENSITIVE_ARGUMENT_NAMES = {
    "api-key", "apikey", "auth", "credential", "credentials", "key",
    "password", "passwd", "secret", "token",
}


def _looks_sensitive(name: str) -> bool:
    normalized = name.lstrip("-").replace("_", "-").lower()
    return any(part in _SENSITIVE_ARGUMENT_NAMES for part in normalized.split("-"))


def _redact_local_path(value: str) -> str:
    """Removes absolute filesystem locations while keeping a useful basename."""
    if not value or "://" in value:
        return value
    if Path(value).is_absolute():
        return f"<local-path>/{Path(value).name}" if Path(value).name else "<local-path>"
    for separator in ("=", ":"):
        prefix, found, suffix = value.partition(separator)
        if found and Path(suffix).is_absolute():
            name = Path(suffix).name
            replacement = f"<local-path>/{name}" if name else "<local-path>"
            return f"{prefix}{separator}{replacement}"
    if value.startswith("-I/"):
        return f"-I<local-path>/{Path(value[2:]).name}"
    return value


def sanitize_command(cmd: List[str]) -> List[str]:
    """Redacts common secret arguments and absolute local paths from evidence."""
    sanitized = []
    redact_next = False
    for raw_arg in cmd:
        arg = str(raw_arg)
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        name, separator, _value = arg.partition("=")
        if separator and _looks_sensitive(name):
            sanitized.append(f"{name}=<redacted>")
            continue
        if arg.startswith("-") and _looks_sensitive(arg):
            sanitized.append(arg)
            redact_next = True
            continue
        sanitized.append(_redact_local_path(arg))
    return sanitized


def run_cmd(
    cmd: List[str],
    check: bool = True,
    cwd: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[int, str, str]:
    """Runs a command and optionally appends sanitized verification evidence."""
    started = time.monotonic()
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
        if check and res.returncode != 0:
            print(f"[ERROR] Command failed ({' '.join(cmd)}):\n{res.stderr.strip()}", file=sys.stderr)
        code, stdout, stderr = res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        if check:
            print(f"[EXCEPT] Exception running command ({' '.join(cmd)}): {e}", file=sys.stderr)
        code, stdout, stderr = 1, "", str(e)
    if evidence is not None:
        evidence.append({
            "command": sanitize_command(cmd),
            "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
            "exit_code": code,
            "status": "passed" if code == 0 else "failed",
        })
    return code, stdout, stderr


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


def query_open_issues() -> Optional[List[Dict[str, Any]]]:
    """Fetches open issues, preserving a query failure as ``None``.

    --limit is explicit: gh defaults to 30, which silently truncates any board
    with more issues than that and makes the dependency graph wrong.
    """
    cmd = ["gh", "issue", "list", "--state", "open", "--limit", "500",
           "--json", "number,title,labels,assignees,body,state,updatedAt"]
    res = run_gh_json(cmd)
    return res if isinstance(res, list) else None


def list_open_issues() -> List[Dict[str, Any]]:
    """Compatibility wrapper for issue pickers that historically consume a list.

    Authoritative callers that must distinguish an empty repository from an
    infrastructure failure use :func:`query_open_issues` directly.
    """
    return query_open_issues() or []


# --- Concurrency primitives -----------------------------------------------
# GitHub offers no compare-and-swap on issue assignment, so claiming is
# optimistic: write, read back, and resolve any race with a deterministic
# tie-break on agent id. See claim_issue.py for the protocol.

AGENT_LABEL_PREFIX = "agent:"
ACTIVE_STATUS_LABELS = {"status:in-progress", "status:in-review"}


def label_names(issue: Dict[str, Any]) -> List[str]:
    return [label.get("name", "") for label in issue.get("labels", [])]


def agent_labels(issue: Dict[str, Any]) -> List[str]:
    """Returns every agent:* label on an issue. More than one means a race."""
    return sorted(n for n in label_names(issue) if n.startswith(AGENT_LABEL_PREFIX))


def claimed_by(issue: Dict[str, Any]) -> Optional[str]:
    """Returns the winning agent id for an issue, or None if unclaimed.

    When two agents raced, the lowest-sorting label wins. Both agents compute
    the same winner from the same data, so no coordinator is required.

    An issue claim (agent:<id>) represents active In Progress implementation
    only. In Review ignores the label as a claim while retaining it as a legacy
    authorship backstop; Done removes it during close-out.
    """
    names = set(label_names(issue))
    if {"status:in-review", "status:done"} & names:
        return None
    labels = agent_labels(issue)
    if labels:
        return labels[0][len(AGENT_LABEL_PREFIX):]
    if "status:in-progress" in names:
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
    # Treat this as issue metadata, not prose.  An unanchored search would
    # parse the first sentence containing ``touches:`` (including Markdown
    # code spans) and silently ignore the real declaration later in the body.
    #
    # [^\n]* rather than a lazy match with a trailing \s*: \s matches newlines,
    # so an *empty* declaration used to run past the line ending and adopt the
    # next line. "touches:\nparallel-eligible: true" reported
    # ['parallel-eligible: true'] as a declared path, which made an issue with
    # no path budget look claimable to build_candidates() while the enforcement
    # hook's stricter parser saw nothing and failed open - so two agents could
    # be handed overlapping files.
    match = re.search(
        r"^[ \t]*[*_`]{0,2}touches[*_`]{0,2}[ \t]*:[ \t]*([^\n]*)",
        body, re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return []
    raw = match.group(1).strip().strip("*_").strip()
    # "(github settings only)" and similar prose mean the issue changes nothing
    # in the tree - not that it declared a directory called "(github".
    if raw.startswith("("):
        return []
    return [p.strip().strip("`") for p in raw.split(",") if p.strip()]


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
    """Returns the first conflicting pair, or None.

    Empty declarations have no pair to compare; the issue picker rejects them
    before calling this primitive.
    """
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


def fetch_issue_comments(issue_id: int) -> List[Dict[str, Any]]:
    """Fetches all comments for an Issue, retrieving all pages."""
    cmd = ["gh", "api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{issue_id}/comments"]
    code, stdout, _ = run_cmd(cmd, check=False)
    if code != 0 or not stdout:
        return []
    comments: List[Dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(stdout):
        while pos < len(stdout) and stdout[pos].isspace():
            pos += 1
        if pos >= len(stdout):
            break
        try:
            doc, end = decoder.raw_decode(stdout, idx=pos)
            if isinstance(doc, list):
                comments.extend(doc)
            elif isinstance(doc, dict):
                comments.append(doc)
            pos = end
        except json.JSONDecodeError:
            break
    return comments


# --- GitHub Project v2 board helpers --------------------------------------
# The board is the monitoring surface; the status:* labels are what the CLI
# reads. Both must move together or they drift. These helpers exist so
# claim_issue.py and update_issue_status.py can move the board item too.


def get_repo_slug() -> Optional[str]:
    """Returns 'owner/repo' for the current working directory's repo."""
    cmd = ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    code, stdout, _ = run_cmd(cmd, check=False)
    return stdout or None


def query_issue_project_items(
    issue_number: int,
) -> Optional[List[Dict[str, Any]]]:
    """Returns project items while preserving GraphQL failures as ``None``."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
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
              status: fieldValueByName(name:"Status") {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  optionId
                  name
                }
              }
              project {
                id
                number
                title
                repositories(first:100) {
                  nodes { nameWithOwner }
                }
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
    if not isinstance(res, dict) or res.get("errors"):
        return None
    try:
        return res["data"]["repository"]["issue"]["projectItems"]["nodes"]
    except (KeyError, TypeError):
        return None


def get_issue_project_items(issue_number: int) -> List[Dict[str, Any]]:
    """Compatibility wrapper for board mutation helpers expecting a list."""
    return query_issue_project_items(issue_number) or []


def get_repo_projects(repo_slug: str) -> Optional[List[Dict[str, Any]]]:
    """Returns Project v2 boards linked to ``owner/repo``.

    Issue creation cannot discover its destination from project items because
    a newly-created issue has none yet.  Resolve from the repository's linked
    projects instead, using the same title/linkage contract as status moves.
    """
    if not repo_slug or "/" not in repo_slug:
        return None
    owner, repo = repo_slug.split("/", 1)
    query = """
    query($owner:String!, $repo:String!) {
      repository(owner:$owner, name:$repo) {
        projectsV2(first:100) {
          nodes {
            id
            number
            title
            owner {
              ... on User { login }
              ... on Organization { login }
            }
            repositories(first:100) {
              nodes { nameWithOwner }
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
    ]
    res = run_gh_json(cmd)
    if not isinstance(res, dict) or res.get("errors"):
        return None
    try:
        return res["data"]["repository"]["projectsV2"]["nodes"]
    except (KeyError, TypeError):
        return None


def select_governed_project_items(
    items: List[Dict[str, Any]],
    repo_slug: str,
) -> List[Dict[str, Any]]:
    """Selects only the repository's Aru_Agentic_SDLC project item.

    Issues can appear on several roadmaps.  A status transition must not move
    all of them just because they expose an identically named Status option.
    Prefer the board name created by ``init_project.py``; fall back only when
    exactly one project is linked to this repository.  Ambiguity fails closed.
    """
    repo_name = repo_slug.split("/", 1)[-1]
    expected_title = f"{repo_name} Board".lower()
    linked = []
    for item in items:
        project = item.get("project") or {}
        repositories = (project.get("repositories") or {}).get("nodes") or []
        linked_slugs = {repo.get("nameWithOwner") for repo in repositories}
        if repo_slug in linked_slugs:
            linked.append(item)

    named = [
        item
        for item in linked
        if (item.get("project") or {}).get("title", "").lower() == expected_title
    ]
    if len(named) == 1:
        return named
    if not named and len(linked) == 1:
        return linked
    return []


def select_governed_projects(
    projects: List[Dict[str, Any]],
    repo_slug: str,
) -> List[Dict[str, Any]]:
    """Applies the governed-board selector before an issue has project items."""
    wrapped = [{"project": project} for project in projects]
    return [
        item["project"]
        for item in select_governed_project_items(wrapped, repo_slug)
    ]


def resolve_governed_project(repo_slug: str) -> Optional[Dict[str, Any]]:
    """Resolves the exact ``<repo> Board`` or sole linked project."""
    available = get_repo_projects(repo_slug)
    if available is None:
        print(
            f"[WARN] Could not query project boards for '{repo_slug}'.",
            file=sys.stderr,
        )
        return None
    projects = select_governed_projects(available, repo_slug)
    if len(projects) == 1:
        return projects[0]
    print(
        f"[WARN] Could not identify one governed project board for '{repo_slug}'.",
        file=sys.stderr,
    )
    return None


def attach_issue_to_governed_project(issue_number: int) -> bool:
    """Idempotently attaches an issue to its repository's governed board."""
    slug = get_repo_slug()
    if not slug:
        print(
            f"[WARN] Could not resolve the repository for issue #{issue_number}.",
            file=sys.stderr,
        )
        print(
            "[WARN] Manual remedy: gh project item-add <PROJECT_NUMBER> "
            "--owner <OWNER> --url <ISSUE_URL>",
            file=sys.stderr,
        )
        return False

    project = resolve_governed_project(slug)
    if not project:
        print(
            f"[WARN] Manual remedy: gh project item-add <PROJECT_NUMBER> "
            f"--owner <OWNER> --url https://github.com/{slug}/issues/{issue_number}",
            file=sys.stderr,
        )
        return False

    project_id = project.get("id")
    existing = get_issue_project_items(issue_number)
    if project_id and any(
        (item.get("project") or {}).get("id") == project_id
        for item in existing
    ):
        return True

    project_number = project.get("number")
    owner = (project.get("owner") or {}).get("login")
    if project_number is None or not owner:
        print(
            f"[WARN] Governed project metadata is incomplete for '{slug}'.",
            file=sys.stderr,
        )
        print(
            f"[WARN] Manual remedy: gh project item-add <PROJECT_NUMBER> "
            f"--owner <OWNER> --url https://github.com/{slug}/issues/{issue_number}",
            file=sys.stderr,
        )
        return False
    return add_issue_to_project(issue_number, int(project_number), owner)


def set_board_status(issue_number: int, status: str) -> bool:
    """Moves an issue's board item(s) to the named Status option.

    Returns True only if at least one board item actually moved, so callers can
    tell the difference between 'moved' and 'issue is not on any board'.
    """
    slug = get_repo_slug()
    if not slug:
        return False
    items = get_issue_project_items(issue_number)
    items = select_governed_project_items(items, slug)
    if not items:
        if not attach_issue_to_governed_project(issue_number):
            return False
        items = select_governed_project_items(
            get_issue_project_items(issue_number), slug
        )
    if not items:
        print(
            f"[WARN] Could not identify one governed project board for '{slug}'.",
            file=sys.stderr,
        )
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
        print(
            f"[WARN] Manual remedy: gh project item-add {project_number} "
            f"--owner {owner} --url {url}",
            file=sys.stderr,
        )
        return False
    return True


def parse_semver_major(version_str: Optional[str]) -> Optional[int]:
    """Extracts the MAJOR version number from a SemVer string (e.g., 'v1.2.3' -> 1, 'v0.1.0' -> 0)."""
    if not version_str:
        return None
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", str(version_str).strip())
    if match:
        return int(match.group(1))
    return None


def get_framework_root() -> str:
    """Returns the absolute path to the Aru_Agentic_SDLC framework repository root."""
    env_home = os.environ.get("ARU_SDLC_HOME")
    if env_home and os.path.isdir(env_home):
        return os.path.abspath(env_home)
    # Fallback to the repository root containing this module (parent directory of scripts/)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def get_current_framework_version(repo_root: Optional[str] = None) -> str:
    """Returns the current framework version from git tags or fallback."""
    cwd = repo_root or get_framework_root()
    code, stdout, _ = run_cmd(["git", "describe", "--tags", "--abbrev=0", "--match", "v*"], check=False, cwd=cwd)
    if code == 0 and stdout:
        return stdout.strip()
    return "v0.1.0"


def check_version_compatibility(
    expected_ref: Optional[str] = None,
    current_version: Optional[str] = None,
) -> bool:
    """Warns (never hard-fails) on MAJOR SemVer mismatch between expected ref and current version.

    Always returns True (never raises or exits non-zero).
    """
    ref = expected_ref or os.environ.get("ARU_SDLC_REF")
    if not ref:
        return True

    expected_major = parse_semver_major(ref)
    if expected_major is None:
        return True

    curr_ver = current_version or get_current_framework_version()
    current_major = parse_semver_major(curr_ver)
    if current_major is None:
        return True

    if expected_major != current_major:
        print(
            f"[WARN] Framework version mismatch: ARU_SDLC_REF specifies '{ref}' "
            f"(MAJOR {expected_major}), but current repository ref is '{curr_ver}' "
            f"(MAJOR {current_major}). Continuing execution.",
            file=sys.stderr,
        )
    return True


if os.environ.get("ARU_SDLC_REF"):
    check_version_compatibility()


if __name__ == "__main__":
    print("Aru_Agentic_SDLC Common Utilities Loaded Cleanly.")
