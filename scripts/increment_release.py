#!/usr/bin/env python3
"""increment_release.py — Tag accepted sprint checkpoints and publish release records for durably accepted Delivery Increments."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from delivery_increments import (
        DEFAULT_INCREMENT_PATH,
        DeliveryIncrementStore,
        IncrementError,
    )
except ImportError:
    # Fallback when imported in isolated test environments
    DEFAULT_INCREMENT_PATH = Path.home() / ".aru" / "delivery-increments.json"
    DeliveryIncrementStore = Any  # type: ignore[misc, assignment]
    IncrementError = Exception  # type: ignore[misc, assignment]


class IncrementReleaseError(Exception):
    """Raised when an increment checkpoint cannot be safely tagged or published."""


def run_git(args: List[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Execute a git command and return (exit_code, stdout, stderr)."""
    res = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd or ROOT),
    )
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def tag_name_for_increment(project_id: str, increment_id: str) -> str:
    """Derive deterministic checkpoint tag name from project and increment identity."""
    return f"ckpt/{project_id}/{increment_id}"


def get_tag_target_commit(tag_name: str, cwd: Optional[Path] = None) -> Optional[str]:
    """Return the 40-char commit SHA pointed to by the tag, or None if tag does not exist."""
    code, stdout, _ = run_git(["rev-parse", "-q", "--verify", f"refs/tags/{tag_name}^{{commit}}"], cwd=cwd)
    if code == 0 and re.fullmatch(r"^[0-9a-fA-F]{40}$", stdout):
        return stdout.lower()
    return None


def get_acceptance_decision(record: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and validate the durable acceptance decision from an increment record."""
    if not isinstance(record, dict):
        raise IncrementReleaseError("invalid increment record: expected dictionary")

    lifecycle_state = record.get("lifecycle_state")
    if lifecycle_state != "accepted":
        raise IncrementReleaseError(
            f"refusing increment '{record.get('increment_id', 'unknown')}': "
            f"lifecycle_state is '{lifecycle_state}', only 'accepted' increments may be tagged as release checkpoints"
        )

    decisions = record.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise IncrementReleaseError("increment lacks decision history")

    accept_decisions = [
        d for d in decisions
        if isinstance(d, dict)
        and isinstance(d.get("decision"), dict)
        and d["decision"].get("action") == "accept"
    ]

    if not accept_decisions:
        raise IncrementReleaseError("no durable 'accept' decision found in increment history")

    latest_accept = accept_decisions[-1]
    evidence = latest_accept.get("evidence")
    if not isinstance(evidence, dict) or not evidence.get("authenticated"):
        raise IncrementReleaseError("acceptance decision lacks authenticated operator evidence")

    record_url = evidence.get("github_record_url")
    if not record_url or not isinstance(record_url, str) or not record_url.startswith("https://github.com/"):
        raise IncrementReleaseError("acceptance decision lacks durable GitHub record URL")

    return latest_accept


def build_tag_message(
    record: Dict[str, Any],
    commit_sha: str,
    acceptance_decision: Dict[str, Any],
    custom_message: Optional[str] = None,
) -> str:
    """Build the structured annotated tag message."""
    increment_id = record["increment_id"]
    project_id = record["project_id"]
    issue_scope = record.get("issue_scope", [])
    scope_str = ", ".join(f"#{issue}" for issue in issue_scope) if issue_scope else "none"
    evidence = acceptance_decision.get("evidence", {})
    record_url = evidence.get("github_record_url", "unknown")
    accepted_at = record.get("accepted_at") or evidence.get("recorded_at") or datetime.now(timezone.utc).isoformat()
    headline = custom_message or f"Sprint checkpoint for increment {increment_id} accepted by operator"

    lines = [
        headline,
        "",
        f"Increment: {increment_id}",
        f"Project: {project_id}",
        f"Target-Commit: {commit_sha}",
        f"Committed-Issues: {scope_str}",
        f"Decision-URL: {record_url}",
        f"Accepted-At: {accepted_at}",
    ]
    return "\n".join(lines)


def create_checkpoint_tag(
    record: Dict[str, Any],
    target_commit: str,
    repo_path: Optional[Path] = None,
    dry_run: bool = False,
    custom_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an annotated git tag for an accepted increment checkpoint.

    Idempotency: If tag exists pointing to exact same target_commit, returns existing tag info.
    Fail-closed: If tag exists pointing to a different commit, raises IncrementReleaseError.
    """
    cwd = repo_path or ROOT
    acceptance = get_acceptance_decision(record)
    increment_id = record["increment_id"]
    project_id = record["project_id"]
    tag_name = tag_name_for_increment(project_id, increment_id)

    # Validate target commit SHA
    if not re.fullmatch(r"^[0-9a-fA-F]{40}$", target_commit):
        raise IncrementReleaseError(f"invalid commit SHA: '{target_commit}'")
    target_commit_clean = target_commit.lower()

    # Check if target commit exists in repository
    code, _, _ = run_git(["cat-file", "-e", f"{target_commit_clean}^{{commit}}"], cwd=cwd)
    if code != 0:
        raise IncrementReleaseError(f"target commit '{target_commit_clean}' does not exist in repository")

    # Check existing tag
    existing_sha = get_tag_target_commit(tag_name, cwd=cwd)
    if existing_sha is not None:
        if existing_sha == target_commit_clean:
            return {
                "tag_name": tag_name,
                "commit_sha": target_commit_clean,
                "created": False,
                "idempotent": True,
                "increment_id": increment_id,
                "project_id": project_id,
            }
        raise IncrementReleaseError(
            f"tag '{tag_name}' already exists pointing to commit '{existing_sha}', "
            f"which differs from requested commit '{target_commit_clean}'"
        )

    tag_message = build_tag_message(record, target_commit_clean, acceptance, custom_message=custom_message)

    if dry_run:
        return {
            "tag_name": tag_name,
            "commit_sha": target_commit_clean,
            "created": False,
            "dry_run": True,
            "tag_message": tag_message,
            "increment_id": increment_id,
            "project_id": project_id,
        }

    tag_code, _, tag_err = run_git(
        ["tag", "-a", tag_name, target_commit_clean, "-m", tag_message],
        cwd=cwd,
    )
    if tag_code != 0:
        raise IncrementReleaseError(f"failed to create tag '{tag_name}': {tag_err}")

    return {
        "tag_name": tag_name,
        "commit_sha": target_commit_clean,
        "created": True,
        "idempotent": False,
        "tag_message": tag_message,
        "increment_id": increment_id,
        "project_id": project_id,
    }


def build_release_record(
    record: Dict[str, Any],
    tag_name: str,
    target_commit: str,
    demo_artifacts: Optional[List[Dict[str, Any]]] = None,
    limitations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Generate structured release record linking tag, evidence, demo artifacts, and deployment state."""
    acceptance = get_acceptance_decision(record)
    evidence = acceptance.get("evidence", {})
    decision = acceptance.get("decision", {})

    # Separation of deployment: release_state remains as recorded (e.g. unreleased), never force-promoted
    deployment_state = record.get("release_state", "unreleased")

    release_payload = {
        "schema_version": 1,
        "release_type": "sprint_checkpoint",
        "tag_name": tag_name,
        "target_commit": target_commit.lower(),
        "increment_id": record["increment_id"],
        "project_id": record["project_id"],
        "kind": record.get("kind", "normal"),
        "control_issue": record.get("control_issue"),
        "issue_scope": record.get("issue_scope", []),
        "lifecycle_state": record.get("lifecycle_state", "accepted"),
        "deployment_state": deployment_state,
        "accepted_at": record.get("accepted_at") or evidence.get("recorded_at"),
        "published_at": datetime.now(timezone.utc).isoformat(),
        "acceptance_evidence": {
            "slack_user_id": evidence.get("slack_user_id"),
            "slack_channel_id": evidence.get("slack_channel_id"),
            "slack_event_id": evidence.get("slack_event_id"),
            "github_record_url": evidence.get("github_record_url"),
            "recorded_at": evidence.get("recorded_at"),
            "risk_accepted": decision.get("risk_accepted", False),
        },
        "demo_artifacts": demo_artifacts or [],
        "limitations": limitations or [],
    }
    return release_payload


def render_release_markdown(release_record: Dict[str, Any]) -> str:
    """Render markdown release notes from structured release record."""
    inc_id = release_record["increment_id"]
    proj_id = release_record["project_id"]
    commit = release_record["target_commit"]
    tag = release_record["tag_name"]
    issues = release_record.get("issue_scope", [])
    evidence = release_record.get("acceptance_evidence", {})
    demo_artifacts = release_record.get("demo_artifacts", [])
    limitations = release_record.get("limitations", [])
    dep_state = release_record.get("deployment_state", "unreleased")

    lines = [
        f"# Sprint Release Checkpoint: {inc_id}",
        "",
        f"**Project**: `{proj_id}`  ",
        f"**Checkpoint Tag**: `{tag}`  ",
        f"**Accepted Commit**: `{commit}`  ",
        f"**Acceptance Decision**: [{evidence.get('github_record_url')}]({evidence.get('github_record_url')})  ",
        f"**Deployment State**: `{dep_state}` *(Production deployment remains separate & held until explicitly authorized)*  ",
        "",
        "## Included Issues",
        "",
    ]
    if issues:
        for iss in issues:
            lines.append(f"- Issue #{iss}")
    else:
        lines.append("- *(No specific issues scoped)*")

    lines.append("")
    lines.append("## Acceptance Evidence")
    lines.append("")
    lines.append(f"- **Operator**: `{evidence.get('slack_user_id')}`")
    lines.append(f"- **Channel**: `{evidence.get('slack_channel_id')}`")
    lines.append(f"- **Recorded At**: `{evidence.get('recorded_at')}`")
    lines.append(f"- **Risk Accepted**: `{evidence.get('risk_accepted', False)}`")

    lines.append("")
    lines.append("## Demo Artifacts & Preview Verification")
    lines.append("")
    if demo_artifacts:
        for art in demo_artifacts:
            name = art.get("name", "Artifact")
            url = art.get("url", "#")
            lines.append(f"- [{name}]({url})")
    else:
        lines.append("- No external demo preview recorded for this library or backend component.")

    lines.append("")
    lines.append("## Known Limitations")
    lines.append("")
    if limitations:
        for lim in limitations:
            lines.append(f"- {lim}")
    else:
        lines.append("- None noted at checkpoint acceptance.")

    lines.append("")
    return "\n".join(lines)


def publish_increment_release(
    record: Dict[str, Any],
    target_commit: str,
    repo_path: Optional[Path] = None,
    demo_artifacts: Optional[List[Dict[str, Any]]] = None,
    limitations: Optional[List[str]] = None,
    dry_run: bool = False,
    output_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Complete workflow: create checkpoint tag and publish release record."""
    tag_result = create_checkpoint_tag(
        record=record,
        target_commit=target_commit,
        repo_path=repo_path,
        dry_run=dry_run,
    )
    tag_name = tag_result["tag_name"]

    release_payload = build_release_record(
        record=record,
        tag_name=tag_name,
        target_commit=target_commit,
        demo_artifacts=demo_artifacts,
        limitations=limitations,
    )
    release_md = render_release_markdown(release_payload)
    release_payload["markdown_notes"] = release_md

    if output_file is not None and not dry_run:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(release_payload, indent=2), encoding="utf-8")

    return {
        "tag_result": tag_result,
        "release_record": release_payload,
        "markdown": release_md,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tag accepted sprint checkpoints and publish release records for durably accepted Delivery Increments.",
    )
    parser.add_argument("--increment", type=str, required=True, help="Increment ID (e.g. inc_0123456789abcdef0123)")
    parser.add_argument("--commit", type=str, default=None, help="Target default-branch commit SHA (defaults to HEAD)")
    parser.add_argument("--store", type=Path, default=DEFAULT_INCREMENT_PATH, help="Path to delivery increments store JSON")
    parser.add_argument("--output", type=Path, default=None, help="Output file path for release record JSON")
    parser.add_argument("--dry-run", action="store_true", help="Preview tag creation and release notes without committing")

    args = parser.parse_args(argv)

    try:
        from delivery_increments import DeliveryIncrementStore
        store = DeliveryIncrementStore(args.store)
        records = [rec for rec in store.list() if rec.get("increment_id") == args.increment]
        if not records:
            print(f"[ERROR] Increment '{args.increment}' not found in store {args.store}", file=sys.stderr)
            return 1
        record = records[0]
    except Exception as exc:
        print(f"[ERROR] Failed to load increment from store: {exc}", file=sys.stderr)
        return 1

    commit = args.commit
    if not commit:
        code, stdout, _ = run_git(["rev-parse", "HEAD"])
        if code != 0 or not stdout:
            print("[ERROR] Could not resolve HEAD commit SHA.", file=sys.stderr)
            return 1
        commit = stdout

    try:
        outcome = publish_increment_release(
            record=record,
            target_commit=commit,
            dry_run=args.dry_run,
            output_file=args.output,
        )
        tag_info = outcome["tag_result"]
        print(f"✅ Sprint Checkpoint Tag: {tag_info['tag_name']} -> {tag_info['commit_sha']}")
        if tag_info.get("idempotent"):
            print("ℹ️ Reused existing matching checkpoint tag (idempotent).")
        print("\n--- Release Notes ---")
        print(outcome["markdown"])
        return 0
    except IncrementReleaseError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
