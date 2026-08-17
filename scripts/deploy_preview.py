#!/usr/bin/env python3
"""deploy_preview.py — Governed preview deployment helper.

Dispatches configured CD workflow for a specific merged commit, waits for completion,
records the preview URL on the originating issue, or files an attached remediation issue
on the Project Board if deployment fails.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set
from urllib.parse import urlsplit

from common import run_cmd


FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
REMEDIATION_MARKER = "<!-- aru-deploy-remediation commit_sha={commit_sha} -->"
REMEDIATION_EVENT_MARKER = (
    "<!-- aru-deploy-remediation-event commit_sha={commit_sha} "
    "stage={failure_stage} run_url={run_url} -->"
)
RUN_POLL_SECONDS = 5.0
RUN_TIMEOUT_SECONDS = 900.0
RUN_CORRELATION_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class RunOutcome:
    success: bool
    state: str
    run_url: str = ""


@dataclass(frozen=True)
class RemediationMatch:
    number: int
    status: str
    body: str


def get_default_branch() -> str:
    """Resolve the authoritative GitHub repository default branch."""
    code, out, _ = run_cmd(["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"], check=False)
    if code == 0 and out.strip():
        return out.strip()
    return ""


def get_repo_slug() -> str:
    """Resolve owner/name from the authenticated GitHub repository."""
    code, out, _ = run_cmd(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        check=False,
    )
    slug = out.strip() if code == 0 else ""
    return slug if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug) else ""


def _normalize_pages_url(url: str) -> str:
    if not isinstance(url, str) or not url or url != url.strip():
        return ""
    if re.search(r"[\s'\"<>\\\[\]()]", url):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    path = parsed.path.rstrip("/") + "/"
    return f"https://{parsed.hostname.lower()}{path}"


def _default_pages_url(repo_slug: str) -> str:
    owner, repository = repo_slug.split("/", 1)
    path = "/" if repository.lower() == f"{owner.lower()}.github.io" else f"/{repository}/"
    return f"https://{owner.lower()}.github.io{path}"


def _pages_url_from_response(raw: str) -> tuple[str, str]:
    try:
        pages = json.loads(raw)
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(pages, dict):
        return "", ""
    return str(pages.get("build_type") or ""), _normalize_pages_url(pages.get("html_url"))


def ensure_pages_enabled(repo_slug: str, dry_run: bool = False) -> Optional[str]:
    """Ensure workflow-mode Pages and return its authoritative base URL."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_slug or ""):
        return None
    if dry_run:
        print(f"[DRY-RUN] Would verify GitHub Pages workflow mode for {repo_slug}")
        return _default_pages_url(repo_slug)

    endpoint = f"repos/{repo_slug}/pages"
    code, out, err = run_cmd(["gh", "api", endpoint], check=False)
    if code == 0:
        build_type, pages_url = _pages_url_from_response(out)
        if build_type == "workflow":
            return pages_url or None
        update_code, _, _ = run_cmd(
            ["gh", "api", "--method", "PUT", endpoint, "-f", "build_type=workflow"],
            check=False,
        )
        if update_code != 0:
            return None
    else:
        if "HTTP 404" not in err:
            return None
        _post_code, _, _ = run_cmd(
            ["gh", "api", "--method", "POST", endpoint, "-f", "build_type=workflow"],
            check=False,
        )
        # Re-read even on failure: a concurrent operator may have enabled it.

    code, verify_out, _ = run_cmd(["gh", "api", endpoint], check=False)
    if code != 0:
        return None
    build_type, pages_url = _pages_url_from_response(verify_out)
    return pages_url if build_type == "workflow" and pages_url else None


def _valid_branch_name(branch: str) -> bool:
    return bool(
        branch
        and not branch.startswith("-")
        and not branch.endswith((".", "/"))
        and ".." not in branch
        and "@{" not in branch
        and not re.search(r"[\x00-\x20~^:?*\\\[]", branch)
    )


def get_originating_issue(commit_sha: str) -> Optional[int]:
    """Inspects commit message and associated PR to find linked Closes #<ID> issue."""
    code, out, _ = run_cmd(["git", "log", "-1", "--format=%B", commit_sha], check=False)
    if code != 0 or not out:
        return None

    # 1. Direct Closes #ID in commit body
    match = re.search(r"\b(?:closes|fixes|resolves)\s*#(\d+)\b", out, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # 2. GitHub merge commit format: "Merge pull request #<PR_ID> from ..."
    pr_match = re.search(r"\bmerge\s+pull\s+request\s+#(\d+)\b", out, re.IGNORECASE)
    if pr_match:
        pr_id = int(pr_match.group(1))
        pr_code, pr_out, _ = run_cmd(["gh", "pr", "view", str(pr_id), "--json", "body"], check=False)
        if pr_code == 0 and pr_out.strip():
            try:
                pr_data = json.loads(pr_out)
                body = pr_data.get("body", "")
                issue_match = re.search(r"\b(?:closes|fixes|resolves)\s*#(\d+)\b", body, re.IGNORECASE)
                if issue_match:
                    return int(issue_match.group(1))
            except json.JSONDecodeError:
                pass

    # 3. Query GitHub API for PR associated with this commit SHA
    pr_code, pr_out, _ = run_cmd(["gh", "pr", "list", "--state", "all", "--search", commit_sha, "--json", "number,body", "--limit", "1"], check=False)
    if pr_code == 0 and pr_out.strip():
        try:
            prs = json.loads(pr_out)
            if prs and isinstance(prs, list):
                body = prs[0].get("body", "")
                issue_match = re.search(r"\b(?:closes|fixes|resolves)\s*#(\d+)\b", body, re.IGNORECASE)
                if issue_match:
                    return int(issue_match.group(1))
        except json.JSONDecodeError:
            pass

    return None


def verify_commit_merged(commit_sha: str, default_branch: Optional[str] = None) -> tuple[bool, str]:
    """Verifies that commit_sha exists and is merged into the default branch.

    Refreshes origin before validating to ensure local clone is not stale.
    Returns (is_valid, resolved_full_sha).
    """
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit_sha or ""):
        return False, ""

    if not default_branch:
        default_branch = get_default_branch()
    if not _valid_branch_name(default_branch):
        return False, ""

    remote_ref = f"refs/remotes/origin/{default_branch}"
    fetch_refspec = f"refs/heads/{default_branch}:{remote_ref}"
    code, _, _ = run_cmd(
        ["git", "fetch", "--no-tags", "origin", fetch_refspec],
        check=False,
    )
    if code != 0:
        return False, ""

    code, out, _ = run_cmd(["git", "rev-parse", "--verify", f"{commit_sha}^{{commit}}"], check=False)
    if code != 0 or not out.strip():
        return False, ""
    full_sha = out.strip()
    if not FULL_SHA_RE.fullmatch(full_sha):
        return False, ""

    code, _, _ = run_cmd(["git", "rev-parse", "--verify", f"{remote_ref}^{{commit}}"], check=False)
    if code != 0:
        return False, full_sha

    code, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", full_sha, remote_ref], check=False)
    if code != 0:
        return False, full_sha

    return True, full_sha


def _run_bounded(cmd: list[str], timeout_seconds: float) -> tuple[int, str, str]:
    """Run a query within the caller's remaining wall-clock budget."""
    if timeout_seconds <= 0:
        return 124, "", "deadline exceeded"
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "deadline exceeded"
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_existing_run_ids(
    workflow_name: str,
    branch: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> Optional[Set[int]]:
    """Fetches currently indexed run IDs for workflow_dispatch on target branch.

    Returns Set[int] on success, or None on query/parsing failure.
    """
    list_cmd = [
        "gh", "run", "list",
        "--workflow", workflow_name,
        "--event", "workflow_dispatch",
        "--json", "databaseId",
        "--limit", "30",
    ]
    if branch:
        list_cmd.extend(["--branch", branch])
    if timeout_seconds is None:
        code, out, _ = run_cmd(list_cmd, check=False)
    else:
        code, out, _ = _run_bounded(list_cmd, timeout_seconds)
    if code != 0:
        return None
    if not out.strip():
        return set()
    try:
        runs = json.loads(out)
        return {r["databaseId"] for r in runs if isinstance(r, dict) and "databaseId" in r}
    except (json.JSONDecodeError, KeyError):
        return None


def verify_run_correlation(
    run_id: int,
    commit_sha: str,
    run_token: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> bool:
    """Verifies that a candidate workflow run belongs to the specific dispatch (by commit_sha and optional run_token)."""
    cmd = ["gh", "run", "view", str(run_id), "--json", "displayTitle,name,headSha"]
    if timeout_seconds is None:
        code, out, _ = run_cmd(cmd, check=False)
    else:
        code, out, _ = _run_bounded(cmd, timeout_seconds)
    if code != 0 or not out.strip():
        return False
    try:
        data = json.loads(out)
        title = data.get("displayTitle", "") + " " + data.get("name", "")
        short_sha = commit_sha[:7]
        sha_matched = (commit_sha in title) or (short_sha in title) or (data.get("headSha") == commit_sha)
        if run_token:
            token_matched = run_token in title
            return token_matched or (sha_matched and not title)
        return sha_matched
    except (json.JSONDecodeError, KeyError):
        return False


def dispatch_cd_workflow(  # noqa: C901, PLR0912
    commit_sha: str,
    workflow_name: str = "deploy-preview.yml",
    pre_existing_run_ids: Optional[Set[int]] = None,
    default_branch: Optional[str] = None,
    run_token: Optional[str] = None,
    max_poll_attempts: int = 10,
    poll_interval: float = 3.0,
    correlation_timeout_seconds: float = RUN_CORRELATION_TIMEOUT_SECONDS,
    dry_run: bool = False,
) -> Optional[int]:
    """Dispatches preview deployment workflow on default branch with commit_sha and run_token inputs, correlating the exact new run ID."""
    if not default_branch:
        default_branch = get_default_branch()
    if not _valid_branch_name(default_branch) or not FULL_SHA_RE.fullmatch(commit_sha):
        print("[ERROR] Dispatch requires an exact merged commit and valid default branch.", file=sys.stderr)
        return None

    if dry_run:
        print(f"[DRY-RUN] Would dispatch workflow '{workflow_name}' at ref '{default_branch}' for commit '{commit_sha}'")
        return 12345

    if not run_token:
        run_token = uuid.uuid4().hex

    deadline = time.monotonic() + max(0.0, correlation_timeout_seconds)

    if pre_existing_run_ids is None:
        initial_ids = get_existing_run_ids(
            workflow_name,
            branch=default_branch,
            timeout_seconds=deadline - time.monotonic(),
        )
        if initial_ids is None:
            print(f"[ERROR] Failed to query existing runs for workflow '{workflow_name}'.", file=sys.stderr)
            return None
        pre_existing_run_ids = initial_ids

    cmd = [
        "gh", "workflow", "run", workflow_name,
        "--ref", default_branch,
        "-f", f"commit_sha={commit_sha}",
        "-f", f"run_token={run_token}",
    ]
    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to dispatch workflow '{workflow_name}': {err}", file=sys.stderr)
        return None

    # Poll for the newly created run ID (must be strictly in new_runs and match commit/token)
    for attempt in range(max_poll_attempts):
        if attempt > 0 and poll_interval > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        current_runs = get_existing_run_ids(
            workflow_name,
            branch=default_branch,
            timeout_seconds=remaining,
        )
        if current_runs is not None:
            new_runs = current_runs - pre_existing_run_ids
            if new_runs:
                for candidate_id in sorted(new_runs, reverse=True):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if verify_run_correlation(
                        candidate_id,
                        commit_sha,
                        run_token,
                        timeout_seconds=remaining,
                    ):
                        return candidate_id

    print(f"[ERROR] Timed out waiting for correlated run of workflow '{workflow_name}' on branch '{default_branch}'.", file=sys.stderr)
    return None


def wait_for_run(
    run_id: int,
    timeout_seconds: float = RUN_TIMEOUT_SECONDS,
    poll_interval: float = RUN_POLL_SECONDS,
    dry_run: bool = False,
) -> RunOutcome:
    """Poll a workflow run until terminal state or a hard deadline."""
    if dry_run:
        print(f"[DRY-RUN] Would poll run {run_id} for at most {timeout_seconds:g}s")
        return RunOutcome(True, "dry-run", f"https://github.com/dry-run/actions/runs/{run_id}")

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_url = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RunOutcome(False, "timed-out", last_url)
        try:
            result = subprocess.run(
                ["gh", "run", "view", str(run_id), "--json", "status,conclusion,url"],
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RunOutcome(False, "timed-out", last_url)
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return RunOutcome(False, "query-failed", last_url)
        if result.returncode != 0 or not result.stdout.strip():
            return RunOutcome(False, "query-failed", last_url)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return RunOutcome(False, "query-failed", last_url)
        if not isinstance(data, dict):
            return RunOutcome(False, "query-failed", last_url)
        last_url = data.get("url") if isinstance(data.get("url"), str) else ""
        status = data.get("status")
        conclusion = data.get("conclusion")
        if status == "completed":
            state = conclusion if isinstance(conclusion, str) and conclusion else "unknown"
            return RunOutcome(state == "success", state, last_url)
        if not isinstance(status, str) or status not in {"queued", "in_progress", "pending", "waiting", "requested"}:
            return RunOutcome(False, "unknown-state", last_url)
        if poll_interval > 0:
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


def is_valid_preview_url(url: str, pages_url: str) -> bool:
    """Validate a preview URL against the authoritative Pages site base URL."""
    normalized = _normalize_pages_url(url)
    expected = _normalize_pages_url(pages_url)
    return bool(normalized and expected and normalized == expected)


def _strict_json_object(raw: str) -> Optional[dict]:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs_hook)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def extract_preview_url_from_run(
    run_id: int,
    commit_sha: str,
    repo_slug: str,
    pages_url: str,
    dry_run: bool = False,
) -> Optional[str]:
    """Read and validate metadata uploaded by this exact deployment run."""
    if dry_run:
        return _normalize_pages_url(pages_url) or None

    with tempfile.TemporaryDirectory(prefix="aru-preview-metadata-") as temp_dir:
        code, _, _ = run_cmd(
            [
                "gh", "run", "download", str(run_id),
                "--name", "preview-metadata",
                "--dir", temp_dir,
            ],
            check=False,
        )
        metadata_path = Path(temp_dir) / "preview-metadata.json"
        if code != 0 or not metadata_path.is_file() or metadata_path.is_symlink():
            return None
        try:
            if metadata_path.stat().st_size > 4096:
                return None
            data = _strict_json_object(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None
        if not data or not {"run_id", "commit_sha", "repository", "preview_url"}.issubset(set(data)):
            return None
        if data.get("run_id") != str(run_id):
            return None
        if data.get("commit_sha") != commit_sha or data.get("repository") != repo_slug:
            return None
        preview_url = data.get("preview_url")
        if preview_url == "skipped" or data.get("is_library") is True or data.get("status") == "skipped":
            return "skipped"
        if isinstance(preview_url, str) and is_valid_preview_url(preview_url, pages_url):
            return _normalize_pages_url(preview_url)
    return None


def post_preview_comment(
    issue_id: int,
    preview_url: str,
    commit_sha: str,
    pages_url: str,
    dry_run: bool = False,
) -> bool:
    """Posts a preview URL comment to the originating issue."""
    if preview_url == "skipped":
        body = (
            f"⏭️ **Preview Deployment: Visibly Skipped (Library)**\n\n"
            f"- **Commit**: `{commit_sha[:7]}`\n"
            f"- **Status**: Product is a library with no runnable web preview surface; preview and smoke stages skipped visibly.\n"
        )
    else:
        if not is_valid_preview_url(preview_url, pages_url):
            print(f"[ERROR] Rejecting invalid/untrusted preview URL: {preview_url}", file=sys.stderr)
            return False

        body = (
            f"🚀 **Preview Environment Deployed**\n\n"
            f"- **Commit**: `{commit_sha[:7]}`\n"
            f"- **Preview URL**: <{preview_url}>\n"
        )
    if dry_run:
        print(f"[DRY-RUN] Would comment on issue #{issue_id}:\n{body}")
        return True

    cmd = ["gh", "issue", "comment", str(issue_id), "--body", body]
    code, _, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to post comment to issue #{issue_id}: {err}", file=sys.stderr)
        return False
    return True


def find_existing_remediation_issue(commit_sha: str) -> tuple[bool, Optional[RemediationMatch]]:  # noqa: C901
    """Return query success and an exact-marker remediation issue match."""
    if not FULL_SHA_RE.fullmatch(commit_sha):
        return False, None
    marker = REMEDIATION_MARKER.format(commit_sha=commit_sha)
    cmd = [
        "gh", "issue", "list", "--state", "open", "--label", "type:fix",
        "--json", "number,title,body,labels", "--limit", "10000",
    ]
    code, out, _ = run_cmd(cmd, check=False)
    if code != 0:
        return False, None
    try:
        issues = json.loads(out or "[]")
    except json.JSONDecodeError:
        return False, None
    if not isinstance(issues, list):
        return False, None
    matches = []
    for issue in issues:
        if not isinstance(issue, dict):
            return False, None
        body = issue.get("body", "")
        number = issue.get("number")
        if not isinstance(body, str):
            return False, None
        if marker not in body:
            continue
        if not isinstance(number, int):
            return False, None
        labels = issue.get("labels", [])
        if not isinstance(labels, list):
            return False, None
        label_names = {
            label.get("name", "").lower()
            for label in labels
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        }
        status_map = {
            "status:backlog": "Backlog",
            "status:ready": "Ready",
            "status:in-progress": "In Progress",
            "status:in-review": "In Review",
            "status:done": "Done",
        }
        statuses = [status for label, status in status_map.items() if label in label_names]
        if len(statuses) > 1:
            return False, None
        matches.append(RemediationMatch(number, statuses[0] if statuses else "Ready", body))
    if len(matches) > 1:
        return False, None
    return True, matches[0] if matches else None


def _remediation_event_marker(commit_sha: str, failure_stage: str, run_url: str) -> str:
    return REMEDIATION_EVENT_MARKER.format(
        commit_sha=commit_sha,
        failure_stage=failure_stage,
        run_url=run_url or "none",
    )


def _record_reused_remediation_event(
    existing: RemediationMatch,
    commit_sha: str,
    failure_stage: str,
    run_url: str,
    error_details: str,
) -> bool:
    """Append one durable, idempotent event when an open remediation is reused."""
    marker = _remediation_event_marker(commit_sha, failure_stage, run_url)
    if marker in existing.body:
        return True

    code, comments, _ = run_cmd(
        ["gh", "issue", "view", str(existing.number), "--json", "comments", "-q", ".comments[].body"],
        check=False,
    )
    if code != 0:
        return False
    if marker in comments:
        return True

    run_evidence = run_url if run_url else "No workflow run was created."
    event_body = (
        "## Preview recovery event\n\n"
        f"{marker}\n"
        f"- Stage: `{failure_stage}`\n"
        f"- Exact workflow run: {run_evidence}\n"
        f"- Diagnostic summary: {_safe_failure_summary(error_details)}"
    )
    code, _, _ = run_cmd(
        ["gh", "issue", "comment", str(existing.number), "--body", event_body],
        check=False,
    )
    return code == 0


def _safe_failure_summary(value: str) -> str:
    summary = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "").strip()
    summary = re.sub(r"[`<>]", "", summary)
    return summary[:500] or "No additional diagnostic summary was available."


def _valid_run_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/\d+", parsed.path)
    )


def file_remediation_issue(  # noqa: C901
    issue_id: int,
    commit_sha: str,
    error_details: str,
    failure_stage: str = "deployment",
    run_url: str = "",
    dry_run: bool = False,
) -> Optional[int]:
    """Files a governed fix(deploy) issue attached to the Project Board (idempotent)."""
    if dry_run:
        print(f"[DRY-RUN] Would create or reuse remediation issue for commit {commit_sha[:7]}")
        return 9999

    if not FULL_SHA_RE.fullmatch(commit_sha):
        print("[ERROR] Remediation requires the exact 40-character commit SHA.", file=sys.stderr)
        return None
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", failure_stage):
        print("[ERROR] Invalid remediation failure stage.", file=sys.stderr)
        return None
    if run_url and not _valid_run_url(run_url):
        print("[ERROR] Invalid workflow run URL for remediation evidence.", file=sys.stderr)
        return None

    sdlc_home = os.environ.get("ARU_SDLC_HOME", ".")
    query_ok, existing = find_existing_remediation_issue(commit_sha)
    if not query_ok:
        print("[ERROR] Could not safely query existing deployment remediation issues.", file=sys.stderr)
        return None
    if existing is not None:
        if not _record_reused_remediation_event(
            existing,
            commit_sha,
            failure_stage,
            run_url,
            error_details,
        ):
            print(
                f"[ERROR] Failed to record recovery evidence on existing issue #{existing.number}.",
                file=sys.stderr,
            )
            return None
        # Re-attach at its current lifecycle state; never regress active work to Ready.
        attach_cmd = [
            sys.executable,
            str(Path(sdlc_home) / "scripts" / "update_issue_status.py"),
            "--issue", str(existing.number),
            "--status", existing.status,
            "--require-board",
        ]
        code, _, err = run_cmd(attach_cmd, check=False)
        if code != 0:
            print(f"[ERROR] Failed to attach existing remediation issue #{existing.number} to Project Board: {err}", file=sys.stderr)
            return None
        print(f"ℹ️ Reusing existing open remediation issue #{existing.number} for commit {commit_sha[:7]}")
        return existing.number

    title = f"fix(deploy): preview deployment failed for commit {commit_sha[:7]}"
    marker = REMEDIATION_MARKER.format(commit_sha=commit_sha)
    event_marker = _remediation_event_marker(commit_sha, failure_stage, run_url)
    run_evidence = run_url if run_url else "No workflow run was created."
    safe_details = _safe_failure_summary(error_details)
    body = f"""## Problem Description
Preview deployment failed for merged commit `{commit_sha}` (originating from issue #{issue_id}).

{marker}
{event_marker}

## Failure Evidence
- Stage: `{failure_stage}`
- Exact workflow run: {run_evidence}
- Diagnostic summary: {safe_details}

## Acceptance Criteria
- [ ] Preview deployment succeeds for commit `{commit_sha[:7]}` (verify: `python3 scripts/deploy_preview.py --commit {commit_sha} --issue {issue_id}`)

## Decision Boundaries
- Target: preview environment CD pipeline
- Error handling: surface pipeline failure to originating issue

## Non-Goals
- Production release configuration

## Verification
`python3 scripts/deploy_preview.py --commit {commit_sha} --issue {issue_id}` exits 0.

## Dependencies
depends-on: none
touches: `**`
parallel-eligible: false
"""

    cmd = [
        "gh", "issue", "create",
        "--title", title,
        "--body", body,
        "--label", "type:fix,priority:p1",
    ]
    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to create remediation issue: {err}", file=sys.stderr)
        return None

    # Parse issue number from URL output
    match = re.search(r"/issues/(\d+)", out)
    if not match:
        print(f"[ERROR] Could not parse issue number from: {out}", file=sys.stderr)
        return None

    new_issue_id = int(match.group(1))
    print(f"✅ Created remediation issue #{new_issue_id}")

    # Attach to project board as Ready; fail closed if attachment fails
    attach_cmd = [
        sys.executable,
        str(Path(sdlc_home) / "scripts" / "update_issue_status.py"),
        "--issue", str(new_issue_id),
        "--status", "Ready",
        "--require-board",
    ]
    code, _, err = run_cmd(attach_cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to attach remediation issue #{new_issue_id} to Project Board: {err}", file=sys.stderr)
        return None

    # Notify originating issue
    notify_body = (
        f"⚠️ **Preview Deployment Failed**\n\n"
        f"Preview deployment failed for commit `{commit_sha[:7]}`. "
        f"Created governed remediation issue #{new_issue_id} on the Project Board."
    )
    code, _, err = run_cmd(["gh", "issue", "comment", str(issue_id), "--body", notify_body], check=False)
    if code != 0:
        print(f"[WARN] Created remediation issue #{new_issue_id} on board but failed to notify originating issue #{issue_id}: {err}", file=sys.stderr)

    return new_issue_id


def deploy_preview(  # noqa: C901, PLR0912
    commit_sha: str,
    issue_id: Optional[int] = None,
    workflow_name: str = "deploy-preview.yml",
    preview_url: Optional[str] = None,
    wait: bool = True,
    dry_run: bool = False,
) -> int:
    """Executes full preview deployment procedure."""
    if not wait:
        print("[ERROR] Preview deployment must wait for exact-run completion evidence.", file=sys.stderr)
        return 1

    default_branch = get_default_branch()
    repo_slug = get_repo_slug()
    if not _valid_branch_name(default_branch) or not repo_slug:
        print("[ERROR] Could not resolve authoritative GitHub repository identity.", file=sys.stderr)
        return 1

    # 1. Enforce merged commit invariant against the same branch used for dispatch.
    is_merged, resolved_sha = verify_commit_merged(commit_sha, default_branch=default_branch)
    if not is_merged:
        print(f"[ERROR] Commit '{commit_sha}' is not merged into the default branch.", file=sys.stderr)
        return 1

    commit_sha = resolved_sha

    if not issue_id:
        issue_id = get_originating_issue(commit_sha)

    if not issue_id:
        print(f"[ERROR] Originating issue ID not provided and could not be inferred for commit {commit_sha}.", file=sys.stderr)
        return 1

    pages_url = ensure_pages_enabled(repo_slug, dry_run=dry_run)
    if not pages_url:
        print("[ERROR] GitHub Pages could not be verified in workflow mode.", file=sys.stderr)
        if not dry_run:
            file_remediation_issue(
                issue_id,
                commit_sha,
                "GitHub Pages is absent or could not be configured for workflow deployments.",
                failure_stage="pages-configuration",
            )
        return 1

    print(f"Deploying preview for commit {commit_sha[:7]} (originating issue #{issue_id})...")

    run_id = dispatch_cd_workflow(
        commit_sha,
        workflow_name=workflow_name,
        pre_existing_run_ids=set() if dry_run else None,
        default_branch=default_branch,
        dry_run=dry_run,
    )
    if run_id is None and not dry_run:
        file_remediation_issue(
            issue_id,
            commit_sha,
            f"Could not dispatch workflow {workflow_name} for the exact merged commit.",
            failure_stage="dispatch",
            dry_run=dry_run,
        )
        return 1

    run_outcome = RunOutcome(True, "dry-run")
    if run_id:
        run_outcome = wait_for_run(run_id, dry_run=dry_run)
        if not run_outcome.success and not dry_run:
            file_remediation_issue(
                issue_id,
                commit_sha,
                f"Workflow run {run_id} ended in state {run_outcome.state}.",
                failure_stage="workflow-run",
                run_url=run_outcome.run_url,
                dry_run=dry_run,
            )
            return 1

    resolved_url = None
    if run_id:
        resolved_url = extract_preview_url_from_run(
            run_id,
            commit_sha,
            repo_slug,
            pages_url,
            dry_run=dry_run,
        )

    if preview_url and resolved_url:
        normalized_supplied_url = _normalize_pages_url(preview_url)
        if not normalized_supplied_url or normalized_supplied_url != resolved_url:
            print("[ERROR] Supplied preview URL does not match exact-run deployment metadata.", file=sys.stderr)
            return 1

    if not resolved_url and not dry_run:
        print(f"[ERROR] Exact-run preview metadata was unavailable for deployment run {run_id}.", file=sys.stderr)
        file_remediation_issue(
            issue_id,
            commit_sha,
            f"Deployment run {run_id} did not provide valid exact-run preview metadata.",
            failure_stage="preview-evidence",
            run_url=run_outcome.run_url,
            dry_run=dry_run,
        )
        return 1

    final_url = resolved_url or extract_preview_url_from_run(
        run_id or 12345,
        commit_sha,
        repo_slug,
        pages_url,
        dry_run=True,
    )
    if final_url != "skipped" and (not final_url or not is_valid_preview_url(final_url, pages_url)):
        print("[ERROR] Preview URL is not the canonical repository Pages URL.", file=sys.stderr)
        return 1
    comment_ok = post_preview_comment(
        issue_id,
        final_url,
        commit_sha,
        pages_url,
        dry_run=dry_run,
    )
    if not comment_ok and not dry_run:
        print(f"[ERROR] Failed to record preview URL on issue #{issue_id}.", file=sys.stderr)
        file_remediation_issue(
            issue_id,
            commit_sha,
            f"Deployment succeeded but the preview URL comment on issue {issue_id} failed.",
            failure_stage="origin-comment",
            run_url=run_outcome.run_url,
        )
        return 1

    if final_url == "skipped":
        print(f"✅ Preview skipped visibly for library commit {commit_sha[:7]}")
    else:
        print(f"✅ Preview deployed successfully: {final_url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy preview environment for a merged commit.")
    parser.add_argument("--commit", required=True, help="Merged commit SHA to deploy")
    parser.add_argument("--issue", type=int, help="Originating issue ID (inferred from commit message if omitted)")
    parser.add_argument("--workflow", default="deploy-preview.yml", help="CD workflow filename (default: deploy-preview.yml)")
    parser.add_argument("--url", help="Preview URL (extracted from workflow run if omitted)")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Rejected compatibility flag; exact-run completion evidence is mandatory",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without mutations")

    args = parser.parse_args()
    return deploy_preview(
        commit_sha=args.commit,
        issue_id=args.issue,
        workflow_name=args.workflow,
        preview_url=args.url,
        wait=not args.no_wait,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
