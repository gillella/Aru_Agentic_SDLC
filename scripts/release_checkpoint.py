#!/usr/bin/env python3
"""Fail-closed validation for phase and release full-suite checkpoints."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlsplit

from common import run_cmd
from deploy_preview import get_default_branch, get_repo_slug
from promote import _read_run, _valid_run_url


FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CHECKPOINT_WORKFLOW = "CI Pipeline"
CHECKPOINT_EVENTS = frozenset({"schedule", "workflow_dispatch"})


class ReleaseCheckpointError(ValueError):
    """Raised when release evidence cannot prove the exact current commit."""


def run_id_from_url(run_url: str, repo_slug: str) -> Optional[int]:
    """Return the run ID only for a canonical Actions URL in this repository."""
    if not isinstance(run_url, str) or not REPO_SLUG_RE.fullmatch(repo_slug or ""):
        return None
    try:
        parsed = urlsplit(run_url)
    except (TypeError, ValueError):
        return None
    prefix = f"/{repo_slug}/actions/runs/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
    ):
        return None
    raw_id = parsed.path.removeprefix(prefix)
    if not raw_id.isdigit() or str(int(raw_id)) != raw_id:
        return None
    run_id = int(raw_id)
    return run_id if run_id > 0 else None


def resolve_default_head(repo_slug: str) -> str:
    """Resolve the live GitHub default-branch head, or return an empty refusal."""
    branch = get_default_branch()
    if not branch:
        return ""
    code, out, _ = run_cmd(
        [
            "gh", "api", f"repos/{repo_slug}/commits/{quote(branch, safe='')}",
            "--jq", ".sha",
        ],
        check=False,
    )
    head = out.strip().lower() if code == 0 else ""
    return head if FULL_SHA_RE.fullmatch(head) else ""


def _run_evidence_error(
    data: dict,
    run_id: int,
    run_url: str,
    target: str,
    repo_slug: str,
) -> str:
    """Return the first refusal reason for an already-read Actions run."""
    if data.get("databaseId") != run_id:
        return "checkpoint run identity does not match its URL"
    if data.get("conclusion") != "success":
        return "checkpoint run is not successful"
    if data.get("workflowName") != CHECKPOINT_WORKFLOW:
        return "checkpoint run is not the CI Pipeline workflow"
    if data.get("event") not in CHECKPOINT_EVENTS:
        return "checkpoint run is not scheduled or manually dispatched"
    if data.get("headSha") != target:
        return "checkpoint run did not test the requested commit"
    if data.get("url") != run_url or not _valid_run_url(run_url, repo_slug, run_id):
        return "checkpoint run URL is not canonical for this repository"
    return ""


def validate_release_checkpoint(
    run_url: str,
    target_commit: str,
    repo_slug: Optional[str] = None,
) -> dict:
    """Validate successful full-suite evidence for the exact live default head."""
    target = target_commit.lower() if isinstance(target_commit, str) else ""
    if not FULL_SHA_RE.fullmatch(target):
        raise ReleaseCheckpointError("target commit must be a full 40-character SHA")

    slug = get_repo_slug() if repo_slug is None else repo_slug
    if not REPO_SLUG_RE.fullmatch(slug or ""):
        raise ReleaseCheckpointError("repository identity could not be resolved")

    run_id = run_id_from_url(run_url, slug)
    if run_id is None:
        raise ReleaseCheckpointError("checkpoint run URL is malformed or foreign")
    data = _read_run(run_id, slug)
    if data is None:
        raise ReleaseCheckpointError("checkpoint run could not be read unambiguously")
    run_error = _run_evidence_error(data, run_id, run_url, target, slug)
    if run_error:
        raise ReleaseCheckpointError(run_error)

    default_head = resolve_default_head(slug)
    if not default_head:
        raise ReleaseCheckpointError("live default-branch head could not be resolved")
    if default_head != target:
        raise ReleaseCheckpointError("requested commit is not the live default-branch head")

    return {
        "repository": slug,
        "run_id": run_id,
        "run_url": run_url,
        "commit_sha": target,
        "workflow": CHECKPOINT_WORKFLOW,
        "event": data["event"],
    }
