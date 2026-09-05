"""Read-only GitHub proof primitives for typed cross-project handoffs."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import quote


def issue_summary(bridge, number: int) -> dict:
    """Return one issue and its linked Project status from the kernel identity."""
    common = bridge.common
    record = common.issue(number)
    status = common.status_of(record)
    if status != common.project_item_status(number):
        raise ValueError("issue and Project status disagree")
    return {
        "number": number,
        "state": str(record["state"]).upper(),
        "status": status,
        "body": record.get("body") or "",
        "url": record["url"],
        "agents": sorted(
            label[6:] for label in common.label_names(record) if label.startswith("agent:")
        ),
    }


def _read(bridge, repo: str, suffix: str) -> dict:
    if not isinstance(repo, str) or "/" not in repo:
        raise ValueError("dependency evidence repository is invalid")
    value = bridge.common.gh_json(["api", f"repos/{repo}/{suffix}"])
    if not isinstance(value, dict):
        raise ValueError("dependency evidence is unreadable")
    return value


def _commit(bridge, repo: str, ref: str) -> str:
    record = _read(bridge, repo, "commits/" + quote(ref, safe=""))
    sha = record.get("sha")
    if not isinstance(sha, str) or len(sha) != 40:
        raise ValueError("dependency ref did not resolve to a full commit")
    return sha.lower()


def _published(bridge, repo: str, tag: str) -> dict:
    release = _read(bridge, repo, "releases/tags/" + quote(tag, safe=""))
    if (release.get("tag_name") != tag or release.get("draft") is not False
            or release.get("prerelease") is not False or not release.get("published_at")):
        raise ValueError("required release is not published and stable")
    return {
        "tag": tag,
        "commit": _commit(bridge, repo, "refs/tags/" + tag),
        "url": release.get("html_url"),
    }


def _merged(bridge, repo: str, pr: int, head: str) -> dict:
    record = _read(bridge, repo, f"pulls/{pr}")
    if record.get("number") != pr or (record.get("head") or {}).get("sha") != head:
        raise ValueError("dependency PR head differs from the declared contract")
    merged = record.get("merged") is True and bool(record.get("merged_at"))
    return {
        "satisfied": merged,
        "pr": pr,
        "head": head,
        "merge_commit": record.get("merge_commit_sha") if merged else None,
    }


def _artifact(bridge, repo: str, ref: str, path: str) -> tuple[str, str]:
    commit = _commit(bridge, repo, ref)
    record = _read(bridge, repo, "contents/" + quote(path, safe="/") + "?ref=" + commit)
    if record.get("type") != "file" or record.get("encoding") != "base64":
        raise ValueError("dependency artifact is not a bounded regular GitHub file")
    content = record.get("content")
    if not isinstance(content, str):
        raise ValueError("dependency artifact content is malformed")
    try:
        raw = base64.b64decode("".join(content.split()), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("dependency artifact content is malformed") from exc
    if len(raw) > 2_000_000:
        raise ValueError("dependency artifact exceeds the bounded comparison size")
    return commit, hashlib.sha256(raw).hexdigest()


def evidence(bridge, request: dict) -> dict:
    """Evaluate one already-validated condition; never executes issue text."""
    kind = request["kind"]
    repo = request["repo"]
    if repo != bridge.repo and kind != "artifact_matches_release":
        raise ValueError("dependency evidence repository mismatch")
    if kind == "issue_done":
        record = issue_summary(bridge, request["issue"])
        return {
            "satisfied": record["state"] == "CLOSED" and record["status"] == "Done",
            "issue": record["number"],
            "url": record["url"],
        }
    if kind == "pr_merged":
        return _merged(bridge, repo, request["pr"], request["head"])
    if kind == "release_contains_pr":
        pr = _merged(bridge, repo, request["pr"], request["head"])
        if not pr["satisfied"]:
            return pr
        release = _published(bridge, repo, request["tag"])
        comparison = _read(
            bridge, repo, f"compare/{pr['merge_commit']}...{release['commit']}"
        )
        return {
            **release,
            "satisfied": comparison.get("status") in {"ahead", "identical"},
            "pr": request["pr"],
            "head": request["head"],
        }
    if kind == "artifact_matches_release":
        source_commit, source_sha = _artifact(bridge, repo, request["ref"], request["path"])
        release = _published(bridge, request["release_repo"], request["tag"])
        release_commit, release_sha = _artifact(
            bridge, request["release_repo"], release["commit"], request["release_path"]
        )
        return {
            **release,
            "satisfied": source_sha == release_sha,
            "source_commit": source_commit,
            "release_commit": release_commit,
            "source_sha256": source_sha,
            "release_sha256": release_sha,
        }
    raise ValueError("unsupported dependency proof")
