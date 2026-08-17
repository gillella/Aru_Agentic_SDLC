#!/usr/bin/env python3
"""Governed audit-only environment state promotion for merged checkpoints.

This helper records GitHub Environment/Deployment state and an issue trail. It
does not deploy, copy, rebuild, or prove movement of a runnable artifact.
"""

import argparse
import json
import re
import sys
import time
import uuid
from typing import Optional, Sequence
from urllib.parse import urlsplit

from common import run_cmd
from deploy_preview import (
    RunOutcome,
    _run_bounded,
    get_default_branch,
    get_repo_slug,
    verify_commit_merged,
    verify_run_correlation,
    wait_for_run,
)


FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
CHECKPOINT_RE = re.compile(r"^ckpt/([1-9]\d*)-([0-9a-f]{7})$")
ENVIRONMENTS = ("preview", "staging", "production")
ENVIRONMENT_RE = re.compile(r"^(preview|staging|production)$")
FORWARD_TRANSITIONS = {("preview", "staging"), ("staging", "production")}
REVERSE_TRANSITIONS = {("production", "staging"), ("staging", "preview")}
PROMOTION_MARKER = (
    "<!-- aru-promotion:v1 run_id={run_id} target={target} commit={commit} -->"
)
WORKFLOW_NAME = "promote.yml"
WORKFLOW_DISPLAY_NAME = "Record Governed Promotion"
RUN_CORRELATION_TIMEOUT_SECONDS = 60.0
AUDIT_ONLY_NOTICE = (
    "GitHub Environment/Deployment state only; no runnable build or hosting "
    "environment movement is claimed."
)


def valid_transition(source: str, target: str, direction: str = "forward") -> bool:
    transitions = (
        FORWARD_TRANSITIONS if direction == "forward" else REVERSE_TRANSITIONS
    )
    return direction in {"forward", "reverse"} and (source, target) in transitions


def _valid_run_url(url: str, repo_slug: str, run_id: int) -> bool:
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path == f"/{repo_slug}/actions/runs/{run_id}"
    )


def _read_run(run_id: int, repo_slug: str) -> Optional[dict]:
    code, out, _ = run_cmd(
        [
            "gh", "run", "view", str(run_id), "--json",
            "databaseId,conclusion,displayTitle,event,workflowName,url,headSha",
            "--repo", repo_slug,
        ],
        check=False,
    )
    if code != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    required = {
        "databaseId", "conclusion", "displayTitle", "event", "workflowName", "url",
        "headSha",
    }
    return data if isinstance(data, dict) and set(data) == required else None


def get_promotion_run_ids(
    workflow_name: str,
    branch: str,
    repo_slug: str,
    timeout_seconds: Optional[float] = None,
) -> Optional[set[int]]:
    """Read repository-dispatch promotion runs without conflating other events."""
    cmd = [
        "gh", "run", "list",
        "--workflow", workflow_name,
        "--event", "repository_dispatch",
        "--branch", branch,
        "--repo", repo_slug,
        "--json", "databaseId",
        "--limit", "30",
    ]
    if timeout_seconds is None:
        code, out, _ = run_cmd(cmd, check=False)
    else:
        code, out, _ = _run_bounded(cmd, timeout_seconds)
    if code != 0:
        return None
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"databaseId"}:
            return None
        run_id = row.get("databaseId")
        if not isinstance(run_id, int) or run_id <= 0:
            return None
        ids.add(run_id)
    return ids


def verify_prior_stage_evidence(
    run_id: int,
    environment: str,
    commit_sha: str,
    repo_slug: str,
) -> tuple[bool, str]:
    """Prove prior-stage evidence without overstating hosted deployment scope."""
    if run_id <= 0 or not ENVIRONMENT_RE.fullmatch(environment or ""):
        return False, "invalid prior-stage evidence request"
    data = _read_run(run_id, repo_slug)
    if data is None:
        return False, "prior-stage evidence run could not be read unambiguously"
    if data.get("databaseId") != run_id or data.get("conclusion") != "success":
        return False, "prior-stage evidence run is not successful"
    url = data.get("url")
    if not isinstance(url, str) or not _valid_run_url(url, repo_slug, run_id):
        return False, "prior-stage evidence run is not bound to this repository"
    title = data.get("displayTitle")
    workflow = data.get("workflowName")
    if not isinstance(title, str) or not isinstance(workflow, str):
        return False, "prior-stage evidence identity is malformed"
    event = data.get("event")
    if environment == "preview":
        expected = (
            workflow == "Deploy Preview"
            and event == "workflow_dispatch"
            and f"Deploy Preview for {commit_sha}" in title
        )
    else:
        expected = (
            workflow == WORKFLOW_DISPLAY_NAME
            and event == "repository_dispatch"
            and f"Audit-only promotion {environment} for {commit_sha}" in title
        )
    return (
        (True, url)
        if expected
        else (
            False,
            f"run does not prove {environment} prior-stage evidence for the exact commit",
        )
    )


def verify_reverse_reference(
    run_id: int,
    source: str,
    target: str,
    commit_sha: str,
    repo_slug: str,
) -> tuple[bool, str]:
    """Validate the earlier successful promotion that this run reverses."""
    if (
        run_id <= 0
        or not FULL_SHA_RE.fullmatch(commit_sha or "")
        or (source, target) not in REVERSE_TRANSITIONS
    ):
        return False, "reverse-of run must be a positive integer"
    data = _read_run(run_id, repo_slug)
    if data is None:
        return False, "reverse-of run could not be read unambiguously"
    title = data.get("displayTitle")
    url = data.get("url")
    expected_title_prefix = (
        f"Audit-only promotion {source} for {commit_sha} "
        f"from {target} [forward] ("
    )
    valid = (
        data.get("databaseId") == run_id
        and data.get("conclusion") == "success"
        and data.get("workflowName") == WORKFLOW_DISPLAY_NAME
        and data.get("event") == "repository_dispatch"
        and isinstance(title, str)
        and title.startswith(expected_title_prefix)
        and title.endswith(")")
        and isinstance(url, str)
        and _valid_run_url(url, repo_slug, run_id)
    )
    return (
        (True, url)
        if valid
        else (
            False,
            "reverse-of run does not match the exact commit and inverse transition",
        )
    )


def _remote_checkpoint_ref(checkpoint: str) -> Optional[tuple[str, str]]:
    code, out, _ = run_cmd(
        [
            "git", "--no-replace-objects", "ls-remote", "--tags", "origin",
            f"refs/tags/{checkpoint}", f"refs/tags/{checkpoint}^{{}}",
        ],
        check=False,
    )
    if code != 0:
        return None
    tag_objects = []
    peeled = []
    for line in out.splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2:
            continue
        if fields[1] == f"refs/tags/{checkpoint}":
            tag_objects.append(fields[0])
        elif fields[1] == f"refs/tags/{checkpoint}^{{}}":
            peeled.append(fields[0])
    if (
        len(tag_objects) != 1
        or len(peeled) != 1
        or not FULL_SHA_RE.fullmatch(tag_objects[0])
        or not FULL_SHA_RE.fullmatch(peeled[0])
    ):
        return None
    return tag_objects[0], peeled[0]


def verify_checkpoint(
    checkpoint: str,
    commit_sha: str,
    issues: Sequence[int],
) -> tuple[bool, str]:
    """Bind one published annotated checkpoint to its commit and included issues."""
    if not CHECKPOINT_RE.fullmatch(checkpoint or ""):
        return False, "checkpoint must use ckpt/<pr>-<sha7>"
    if checkpoint.rsplit("-", 1)[-1].lower() != commit_sha[:7].lower():
        return False, "checkpoint name does not match the promotion commit"
    remote_ref = _remote_checkpoint_ref(checkpoint)
    if remote_ref is None or remote_ref[1] != commit_sha:
        return False, "checkpoint does not resolve to the exact promotion commit"
    code, object_type, _ = run_cmd(
        ["git", "--no-replace-objects", "cat-file", "-t", commit_sha],
        check=False,
    )
    if code != 0 or object_type.strip() != "commit":
        return False, "checkpoint target is not an exact commit object"

    code, local_tag_object, _ = run_cmd(
        [
            "git", "--no-replace-objects", "rev-parse", "--verify",
            f"refs/tags/{checkpoint}^{{tag}}",
        ],
        check=False,
    )
    if code != 0:
        code, _, _ = run_cmd(
            [
                "git", "--no-replace-objects", "fetch", "--no-tags", "origin",
                f"refs/tags/{checkpoint}:refs/tags/{checkpoint}",
            ],
            check=False,
        )
        if code != 0:
            return False, "annotated checkpoint could not be fetched"
        code, local_tag_object, _ = run_cmd(
            [
                "git", "--no-replace-objects", "rev-parse", "--verify",
                f"refs/tags/{checkpoint}^{{tag}}",
            ],
            check=False,
        )
    if code != 0 or local_tag_object.strip() != remote_ref[0]:
        return False, "local checkpoint annotation does not match the published tag"

    code, message, _ = run_cmd(
        [
            "git", "--no-replace-objects", "for-each-ref",
            f"refs/tags/{checkpoint}", "--format=%(contents)",
        ],
        check=False,
    )
    if code != 0 or not message.strip():
        return False, "checkpoint annotation could not be read"
    merge_match = re.search(r"(?m)^merged as:\s+([0-9a-f]{40})$", message)
    if not merge_match or merge_match.group(1) != commit_sha:
        return False, "checkpoint annotation names a different merge commit"
    issue_match = re.search(r"(?m)^issues:\s+(.+)$", message)
    if not issue_match:
        return False, "checkpoint annotation has no issue trail"
    recorded = {int(value) for value in re.findall(r"#([1-9]\d*)", issue_match.group(1))}
    requested = set(issues)
    if not requested or requested != recorded:
        return False, "included issues do not exactly match the checkpoint issue set"
    return True, message


def verify_issues(issues: Sequence[int], repo_slug: str) -> tuple[bool, str]:
    """Confirm every audit target is a real issue in the selected repository."""
    for issue_id in issues:
        code, out, _ = run_cmd(
            [
                "gh", "api", f"repos/{repo_slug}/issues/{issue_id}",
                "--jq", '{number,state,url:.html_url,is_pr:has("pull_request")}',
            ],
            check=False,
        )
        if code != 0:
            return False, f"included issue #{issue_id} could not be read"
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return False, f"included issue #{issue_id} returned malformed data"
        expected_url = f"https://github.com/{repo_slug}/issues/{issue_id}"
        state = data.get("state") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or set(data) != {"number", "state", "url", "is_pr"}
            or data.get("number") != issue_id
            or not isinstance(state, str)
            or state.upper() not in {"OPEN", "CLOSED"}
            or data.get("url") != expected_url
            or data.get("is_pr") is not False
        ):
            return False, f"included issue #{issue_id} is not repository-bound"
    return True, "all included issues are repository-bound"


def dispatch_promotion(
    commit_sha: str,
    checkpoint: str,
    issues: Sequence[int],
    source: str,
    target: str,
    evidence_run: int,
    default_branch: str,
    repo_slug: str,
    reverse_of: Optional[int] = None,
    workflow_name: str = WORKFLOW_NAME,
    dry_run: bool = False,
) -> Optional[int]:
    """Dispatch the workflow and correlate the exact new run by random token."""
    direction = "reverse" if reverse_of is not None else "forward"
    if not valid_transition(source, target, direction):
        return None
    token = uuid.uuid4().hex
    if dry_run:
        print(
            f"[DRY-RUN] Would record audit-only {direction} promotion state for "
            f"{commit_sha[:7]} from {source} to {target} using {checkpoint}; "
            "no runnable build movement would be claimed."
        )
        return 12345
    deadline = time.monotonic() + RUN_CORRELATION_TIMEOUT_SECONDS
    existing = get_promotion_run_ids(
        workflow_name,
        default_branch,
        repo_slug,
        timeout_seconds=deadline - time.monotonic(),
    )
    if existing is None:
        return None
    cmd = [
        "gh", "api", "--method", "POST", f"repos/{repo_slug}/dispatches",
        "-f", "event_type=aru-promotion",
        "-f", f"client_payload[commit_sha]={commit_sha}",
        "-f", f"client_payload[checkpoint]={checkpoint}",
        "-f", f"client_payload[issues]={','.join(str(issue) for issue in issues)}",
        "-f", f"client_payload[from_environment]={source}",
        "-f", f"client_payload[to_environment]={target}",
        "-f", f"client_payload[evidence_run]={evidence_run}",
        "-f", f"client_payload[direction]={direction}",
        "-f", f"client_payload[reverse_of_run]={reverse_of or ''}",
        "-f", f"client_payload[run_token]={token}",
    ]
    code, _, _ = run_cmd(cmd, check=False)
    if code != 0:
        return None
    for attempt in range(10):
        if attempt:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(3.0, remaining))
        runs = get_promotion_run_ids(
            workflow_name,
            default_branch,
            repo_slug,
            timeout_seconds=deadline - time.monotonic(),
        )
        if runs is None:
            return None
        candidates = sorted(runs - existing, reverse=True)
        for run_id in candidates:
            if verify_run_correlation(
                run_id,
                commit_sha,
                run_token=token,
                timeout_seconds=deadline - time.monotonic(),
            ):
                return run_id
    return None


def _promotion_marker(run_id: int, target: str, commit_sha: str) -> str:
    return PROMOTION_MARKER.format(run_id=run_id, target=target, commit=commit_sha)


def verify_deployment_record(
    run_id: int,
    run_url: str,
    commit_sha: str,
    checkpoint: str,
    issues: Sequence[int],
    target: str,
    direction: str,
    repo_slug: str,
) -> tuple[bool, str]:
    """Bind the successful workflow to its exact audit-only deployment record."""
    endpoint = (
        f"repos/{repo_slug}/deployments?ref={commit_sha}"
        f"&environment={target}&per_page=100"
    )
    code, out, _ = run_cmd(["gh", "api", endpoint], check=False)
    if code != 0:
        return False, "deployment records could not be read"
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return False, "deployment records returned malformed data"
    if not isinstance(rows, list):
        return False, "deployment records returned an ambiguous shape"
    expected_payload = {
        "audit_only": True,
        "checkpoint": checkpoint,
        "included_issues": list(issues),
        "direction": direction,
        "workflow_run": run_id,
        "scope": "github-state-only",
    }
    expected_description = (
        f"Aru audit-only {direction} promotion {checkpoint}; run {run_id}"
    )
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), int)
        and row.get("id") > 0
        and row.get("ref") == commit_sha
        and row.get("sha") == commit_sha
        and row.get("environment") == target
        and row.get("description") == expected_description
        and row.get("payload") == expected_payload
    ]
    if len(matches) != 1:
        return False, "exact audit-only deployment record was not unique"
    deployment_id = matches[0]["id"]
    code, out, _ = run_cmd(
        [
            "gh", "api",
            f"repos/{repo_slug}/deployments/{deployment_id}/statuses?per_page=100",
        ],
        check=False,
    )
    if code != 0:
        return False, "deployment statuses could not be read"
    try:
        statuses = json.loads(out)
    except json.JSONDecodeError:
        return False, "deployment statuses returned malformed data"
    if not isinstance(statuses, list):
        return False, "deployment statuses returned an ambiguous shape"
    valid_status = any(
        isinstance(status, dict)
        and status.get("state") == "success"
        and status.get("environment") == target
        and status.get("log_url") == run_url
        and status.get("description")
        == "Audit-only GitHub state; no runnable build movement claimed"
        for status in statuses
    )
    if not valid_status:
        return False, "deployment record has no trusted success status"
    return True, str(deployment_id)


def record_promotion(
    issue_id: int,
    run_id: int,
    run_url: str,
    source: str,
    target: str,
    commit_sha: str,
    checkpoint: str,
    issues: Sequence[int],
    evidence_run: int,
    repo_slug: str,
    deployment_id: str,
    reverse_of: Optional[int] = None,
    dry_run: bool = False,
) -> bool:
    """Append one idempotent structured promotion record to a governed issue."""
    marker = _promotion_marker(run_id, target, commit_sha)
    direction = "reverse" if reverse_of else "forward"
    body = (
        "## Governed promotion record (audit-only)\n\n"
        f"{marker}\n"
        f"- Scope: {AUDIT_ONLY_NOTICE}\n"
        f"- Direction: `{direction}`\n"
        f"- Transition: `{source} -> {target}`\n"
        f"- Commit: `{commit_sha}`\n"
        f"- Checkpoint: `{checkpoint}`\n"
        f"- Included issues: {', '.join(f'#{number}' for number in issues)}\n"
        f"- Prior-stage evidence: {evidence_run}\n"
        f"- GitHub deployment record: `{deployment_id}`\n"
        f"- Promotion workflow run: {run_url}\n"
    )
    if reverse_of:
        body += f"- Reverses promotion run: {reverse_of}\n"
    if dry_run:
        print(f"[DRY-RUN] Would record audit-only promotion on issue #{issue_id}:\n{body}")
        return True
    code, comments, _ = run_cmd(
        [
            "gh", "api", "--paginate",
            f"repos/{repo_slug}/issues/{issue_id}/comments",
            "--jq", ".[].body",
        ],
        check=False,
    )
    if code != 0:
        return False
    if marker in comments:
        return True
    code, _, _ = run_cmd(
        ["gh", "issue", "comment", str(issue_id), "--repo", repo_slug, "--body", body],
        check=False,
    )
    return code == 0


def promote(
    commit_sha: str,
    checkpoint: str,
    issues: Sequence[int],
    source: str,
    target: str,
    evidence_run: int,
    reverse_of: Optional[int] = None,
    workflow_name: str = WORKFLOW_NAME,
    dry_run: bool = False,
) -> int:
    """Validate, dispatch, wait, and write the post-merge issue trail."""
    normalized_issues = sorted(set(issues))
    direction = "reverse" if reverse_of is not None else "forward"
    if (
        not FULL_SHA_RE.fullmatch(commit_sha or "")
        or not valid_transition(source, target, direction)
        or not normalized_issues
        or any(issue <= 0 for issue in normalized_issues)
        or evidence_run <= 0
        or (reverse_of is not None and reverse_of <= 0)
    ):
        print("[ERROR] Invalid promotion transition or evidence identifiers.", file=sys.stderr)
        return 1
    default_branch = get_default_branch()
    repo_slug = get_repo_slug()
    if not default_branch or not repo_slug:
        print("[ERROR] Could not resolve authoritative repository identity.", file=sys.stderr)
        return 1
    merged, exact_sha = verify_commit_merged(commit_sha, default_branch=default_branch)
    if not merged or not FULL_SHA_RE.fullmatch(exact_sha):
        print("[ERROR] Promotion commit is not merged into the refreshed default branch.", file=sys.stderr)
        return 1
    checkpoint_ok, checkpoint_reason = verify_checkpoint(
        checkpoint, exact_sha, normalized_issues
    )
    if not checkpoint_ok:
        print(f"[ERROR] {checkpoint_reason}", file=sys.stderr)
        return 1
    issues_ok, issues_reason = verify_issues(normalized_issues, repo_slug)
    if not issues_ok:
        print(f"[ERROR] {issues_reason}", file=sys.stderr)
        return 1
    evidence_environment = target if direction == "reverse" else source
    evidence_ok, evidence_value = verify_prior_stage_evidence(
        evidence_run, evidence_environment, exact_sha, repo_slug
    )
    if not evidence_ok:
        print(f"[ERROR] {evidence_value}", file=sys.stderr)
        return 1
    if reverse_of is not None:
        reverse_ok, reverse_reason = verify_reverse_reference(
            reverse_of,
            source,
            target,
            exact_sha,
            repo_slug,
        )
        if not reverse_ok:
            print(f"[ERROR] {reverse_reason}", file=sys.stderr)
            return 1
    run_id = dispatch_promotion(
        exact_sha,
        checkpoint,
        normalized_issues,
        source,
        target,
        evidence_run,
        default_branch,
        repo_slug,
        reverse_of=reverse_of,
        workflow_name=workflow_name,
        dry_run=dry_run,
    )
    if run_id is None:
        print("[ERROR] Promotion workflow could not be dispatched or correlated.", file=sys.stderr)
        return 1
    outcome = RunOutcome(True, "dry-run", f"https://github.com/{repo_slug}/actions/runs/{run_id}")
    if not dry_run:
        outcome = wait_for_run(run_id)
    if not outcome.success or not _valid_run_url(outcome.run_url, repo_slug, run_id):
        print(f"[ERROR] Promotion run ended without trusted success evidence: {outcome.state}.", file=sys.stderr)
        return 1
    deployment_ok, deployment_value = (True, "dry-run")
    if not dry_run:
        deployment_ok, deployment_value = verify_deployment_record(
            run_id,
            outcome.run_url,
            exact_sha,
            checkpoint,
            normalized_issues,
            target,
            direction,
            repo_slug,
        )
    if not deployment_ok:
        print(f"[ERROR] {deployment_value}", file=sys.stderr)
        return 1
    for issue_id in normalized_issues:
        if not record_promotion(
            issue_id,
            run_id,
            outcome.run_url,
            source,
            target,
            exact_sha,
            checkpoint,
            normalized_issues,
            evidence_run,
            repo_slug,
            deployment_value,
            reverse_of=reverse_of,
            dry_run=dry_run,
        ):
            print(f"[ERROR] Failed to record promotion on issue #{issue_id}.", file=sys.stderr)
            return 1
    print(
        f"✅ Recorded audit-only {'reversal' if reverse_of else 'promotion'} "
        f"for {exact_sha[:7]} from {source} to {target}; run {run_id}. "
        "No runnable build movement is claimed."
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Governed audit-only GitHub environment state promotion."
    )
    parser.add_argument("--commit", required=True, help="Exact merged commit SHA")
    parser.add_argument("--checkpoint", required=True, help="Published ckpt/* tag")
    parser.add_argument("--issue", type=int, action="append", required=True)
    parser.add_argument("--from-environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--to-environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--evidence-run", type=int, required=True)
    parser.add_argument("--reverse-of", type=int, help="Successful promotion run being reversed")
    parser.add_argument("--workflow", default=WORKFLOW_NAME)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return promote(
        args.commit,
        args.checkpoint,
        args.issue,
        args.from_environment,
        args.to_environment,
        args.evidence_run,
        reverse_of=args.reverse_of,
        workflow_name=args.workflow,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
