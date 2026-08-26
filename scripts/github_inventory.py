"""Bounded, quota-safe GitHub inventory parsing for shared CLI helpers."""

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit


Runner = Callable[..., Tuple[int, str, str]]
IDENTITY_LABEL_PREFIXES = ("agent:", "reviewer:", "merger:", "author:")


def issue_details(run_json, slug: str, issue_id: int) -> Optional[Dict[str, Any]]:
    """Read and normalize one issue through REST."""
    raw = run_json(["gh", "api", f"repos/{slug}/issues/{issue_id}"])
    if (
        not isinstance(raw, dict) or raw.get("pull_request") is not None
        or not isinstance(raw.get("number"), int)
        or not isinstance(raw.get("title"), str)
        or not isinstance(raw.get("labels"), list)
        or not isinstance(raw.get("assignees"), list)
        or not isinstance(raw.get("state"), str)
        or not isinstance(raw.get("user"), dict)
        or not isinstance((raw.get("user") or {}).get("login"), str)
    ):
        return None
    return {
        "number": raw["number"], "title": raw["title"], "labels": raw["labels"],
        "assignees": raw["assignees"], "body": raw.get("body") or "",
        "state": raw["state"].upper(), "author": {"login": raw["user"]["login"]},
        "updatedAt": raw.get("updated_at"),
        "authorAssociation": raw.get("author_association"),
    }


def hydrate_renamed_files(records, owner, repo, rename_types, rest_loader):
    """Use REST only when GraphQL identifies a rename needing its old path."""
    for record in records:
        files = record.get("files") or []
        renamed = any(
            isinstance(item, dict)
            and str(item.get("changeType") or "").upper() in rename_types
            for item in files
        )
        if renamed:
            rest_files = rest_loader(owner, repo, record["number"])
            record["files"] = [] if rest_files is None else rest_files
    return records


def local_repo_slug(run: Runner) -> Optional[str]:
    """Return ``owner/repo`` from origin without a GitHub API call."""
    code, remote, _ = run(["git", "remote", "get-url", "origin"], check=False)
    if code != 0 or not remote:
        return None
    remote = remote.strip()
    if "://" in remote:
        try:
            path = urlsplit(remote).path
        except ValueError:
            return None
    else:
        _host, separator, path = remote.partition(":")
        if not separator:
            return None
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    valid = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not owner or not repo or not valid.fullmatch(owner) or not valid.fullmatch(repo):
        return None
    return f"{owner}/{repo}"


def json_lines(stdout: str) -> Optional[List[Any]]:
    """Parse the JSON-lines shape produced by ``gh api --paginate --jq``."""
    rows: List[Any] = []
    for raw in (stdout or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError:
            return None
    return rows


def board_agent_identities(
    run: Runner, slug: str
) -> Tuple[Optional[Dict[str, List[str]]], str]:
    """Read agent identity labels from paginated issue and pull REST lists."""
    holders: Dict[str, List[str]] = {}
    queries = (
        (["gh", "api", "--paginate", f"repos/{slug}/issues?state=open&per_page=100",
          "--jq", ".[] | select(.pull_request == null) | {number,labels}"], "issue"),
        (["gh", "api", "--paginate", f"repos/{slug}/pulls?state=open&per_page=100",
          "--jq", ".[] | {number,labels}"], "PR"),
    )
    for command, kind in queries:
        code, stdout, _ = run(command, check=False)
        items = json_lines(stdout) if code == 0 else None
        if items is None:
            return None, f"could not read open {kind}s from GitHub"
        for item in items:
            if not isinstance(item, dict):
                return None, f"could not read open {kind}s from GitHub"
            for label in item.get("labels") or []:
                name = str((label or {}).get("name") or "")
                for prefix in IDENTITY_LABEL_PREFIXES:
                    if name.startswith(prefix) and name[len(prefix):]:
                        agent = name[len(prefix):]
                        holders.setdefault(agent, []).append(
                            f"{kind} #{item.get('number')} ({name})"
                        )
    return holders, ""


def open_issues(run: Runner, slug: str) -> Optional[List[Dict[str, Any]]]:
    """Read and normalize a complete paginated REST open-issue snapshot."""
    command = [
        "gh", "api", "--paginate", f"repos/{slug}/issues?state=open&per_page=100",
        "--jq", ".[] | select(.pull_request == null)",
    ]
    code, stdout, _ = run(command, check=False)
    rows = json_lines(stdout) if code == 0 else None
    if rows is None:
        return None
    issues: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        number = row.get("number") if isinstance(row, dict) else None
        title = row.get("title") if isinstance(row, dict) else None
        labels = row.get("labels") if isinstance(row, dict) else None
        assignees = row.get("assignees") if isinstance(row, dict) else None
        state = row.get("state") if isinstance(row, dict) else None
        user = row.get("user") if isinstance(row, dict) else None
        if (
            not isinstance(number, int) or number in seen
            or not isinstance(title, str) or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or not isinstance(assignees, list) or not isinstance(state, str)
            or state.lower() != "open" or not isinstance(user, dict)
            or not isinstance(user.get("login"), str)
        ):
            return None
        seen.add(number)
        issues.append({
            "number": number, "title": title, "labels": labels,
            "assignees": assignees, "body": row.get("body") or "", "state": "OPEN",
            "updatedAt": row.get("updated_at"), "author": {"login": user["login"]},
            "authorAssociation": row.get("author_association"),
        })
    return issues


def open_pull_requests(run: Runner, slug: str) -> Optional[List[Dict[str, Any]]]:
    """Read a complete REST open-PR snapshot for status diagnostics.

    This intentionally contains only facts exposed by the list endpoint.
    Fleet status is diagnostic, not merge authority, so it must not perform
    per-PR review/thread/check queries merely to say that an open PR is waiting.
    """
    command = [
        "gh", "api", "--paginate", f"repos/{slug}/pulls?state=open&per_page=100",
        "--jq", ".[]",
    ]
    code, stdout, _ = run(command, check=False)
    rows = json_lines(stdout) if code == 0 else None
    if rows is None:
        return None
    prs: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        number = row.get("number") if isinstance(row, dict) else None
        labels = row.get("labels") if isinstance(row, dict) else None
        head = row.get("head") if isinstance(row, dict) else None
        if (
            not isinstance(number, int) or number in seen
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels)
            or not isinstance(head, dict)
        ):
            return None
        seen.add(number)
        merge_state = str(row.get("mergeStateStatus") or row.get("mergeable_state") or "").upper()
        prs.append({
            "number": number,
            "title": row.get("title") or "",
            "isDraft": bool(row.get("draft")),
            "labels": labels,
            "reviews": row.get("reviews") if isinstance(row.get("reviews"), list) else [],
            "statusCheckRollup": row.get("statusCheckRollup") if isinstance(row.get("statusCheckRollup"), list) else [],
            "updatedAt": row.get("updated_at") or row.get("updatedAt"),
            "createdAt": row.get("created_at") or row.get("createdAt"),
            "headRefName": head.get("ref") or row.get("headRefName") or "",
            "headRefOid": head.get("sha") or row.get("headRefOid") or "",
            "body": row.get("body") or "",
            "comments": row.get("comments") if isinstance(row.get("comments"), list) else [],
            "reviewDecision": row.get("reviewDecision") or "",
            "mergeStateStatus": merge_state,
            "state": "OPEN",
        })
    return prs
