#!/usr/bin/env python3
# line-ceiling: 559
"""Translate a production signal into a governed GitHub issue.

Firing opens or updates one board issue. Resolved comments, closes unclaimed
issues, and prunes leftover worktrees when Done. There is no second incident
process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from common import _redact_embedded_secrets, ensure_label, run_cmd


IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
INCIDENT_MARKER = "<!-- aru-incident:v1 fingerprint={fingerprint} -->"
EVENT_MARKER = (
    "<!-- aru-incident-event:v1 fingerprint={fingerprint} occurrence={occurrence} -->"
)
RESOLVED_MARKER = "<!-- aru-incident-resolved:v1 fingerprint={fingerprint} -->"
STATUS_LABELS = {
    "status:backlog": "Backlog",
    "status:ready": "Ready",
    "status:in-progress": "In Progress",
    "status:in-review": "In Review",
    "status:done": "Done",
}
UNCLAIMED_STATUSES = {"Backlog", "Ready"}
VALID_SEVERITIES = ("p0", "p1", "p2", "p3")


@dataclass(frozen=True)
class IncidentMatch:
    number: int
    status: Optional[str]
    body: str
    state: str


def incident_fingerprint(source: str, component: str, alert_name: str) -> str:
    """Return a stable 16-hex identity for source|component|alert_name."""
    canonical = "|".join(part.strip().lower() for part in (source, component, alert_name))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def incident_marker(fingerprint: str) -> str:
    return INCIDENT_MARKER.format(fingerprint=fingerprint)


def occurrence_id(evidence: str) -> str:
    return hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:12]


def event_marker(fingerprint: str, evidence: str) -> str:
    return EVENT_MARKER.format(fingerprint=fingerprint, occurrence=occurrence_id(evidence))


def resolved_marker(fingerprint: str) -> str:
    return RESOLVED_MARKER.format(fingerprint=fingerprint)


def sanitize_evidence(value: str) -> str:
    """Strip controls, markup, and credential-shaped tokens from evidence."""
    summary = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "").strip()
    summary = _redact_embedded_secrets(summary)
    summary = re.sub(r"(?:AKIA|ASIA)[A-Z0-9]{16}", "[redacted]", summary)
    summary = re.sub(
        r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
        r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
        "[redacted]",
        summary,
        flags=re.IGNORECASE | re.DOTALL,
    )
    summary = re.sub(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "[redacted]",
        summary,
    )
    summary = summary.replace("<redacted>", "[redacted]")
    summary = re.sub(r"[`<>]", "", summary)
    return summary[:2000] or "No additional diagnostic summary was available."


def validate_identity(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not IDENTITY_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid {field}: use 1-80 characters [A-Za-z0-9._:/-].")
    return cleaned


def _status_from_labels(labels: Any) -> Optional[str]:
    if not isinstance(labels, list):
        return None
    names = {
        label.get("name", "").lower()
        for label in labels
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }
    statuses = [status for key, status in STATUS_LABELS.items() if key in names]
    if len(statuses) != 1:
        return None
    return statuses[0]


def _matches_for_marker(issues: list[Any], marker: str) -> Optional[list[IncidentMatch]]:
    matches: list[IncidentMatch] = []
    for issue in issues:
        if not isinstance(issue, dict):
            return None
        body = issue.get("body", "")
        number = issue.get("number")
        state = issue.get("state", "OPEN")
        if not isinstance(body, str) or not isinstance(number, int):
            return None
        if marker not in body:
            continue
        status = _status_from_labels(issue.get("labels", []))
        if not isinstance(state, str):
            return None
        matches.append(IncidentMatch(number, status, body, state.upper()))
    return matches


def _list_incident_matches(
    fingerprint: str, include_closed: bool = False
) -> tuple[bool, Optional[list[IncidentMatch]]]:
    """Return query success and every marker match in the searched states."""
    marker = incident_marker(fingerprint)
    query = f"in:body fingerprint={fingerprint}"
    states = ("open", "closed") if include_closed else ("open",)
    matches: list[IncidentMatch] = []
    seen: set[int] = set()
    for state in states:
        code, out, _ = run_cmd(
            [
                "gh", "issue", "list", "--state", state, "--limit", "100",
                "--search", query, "--json", "number,title,body,labels,state",
            ],
            check=False,
        )
        if code != 0:
            return False, None
        try:
            issues = json.loads(out or "[]")
        except json.JSONDecodeError:
            return False, None
        if not isinstance(issues, list):
            return False, None
        parsed = _matches_for_marker(issues, marker)
        if parsed is None:
            return False, None
        for match in parsed:
            if match.number not in seen:
                seen.add(match.number)
                matches.append(match)
        if matches and state == "open":
            break
    return True, matches


def find_existing_incident(
    fingerprint: str, include_closed: bool = False
) -> tuple[bool, Optional[IncidentMatch]]:
    """Return query success and the unique marker match, if any."""
    query_ok, matches = _list_incident_matches(fingerprint, include_closed)
    if not query_ok or matches is None:
        return False, None
    if len(matches) > 1:
        return True, min(matches, key=lambda item: item.number)
    return True, matches[0] if matches else None


def _sdlc_home() -> Path:
    default = Path(__file__).resolve().parent.parent
    return Path(os.environ.get("ARU_SDLC_HOME") or default).resolve()


def attach_board(issue_id: int, status: str) -> bool:
    """Attach or move the issue on the Project Board; fail closed."""
    cmd = [
        sys.executable,
        str(_sdlc_home() / "scripts" / "update_issue_status.py"),
        "--issue", str(issue_id), "--status", status, "--require-board",
    ]
    code, _, err = run_cmd(cmd, check=False)
    if code != 0:
        print(
            f"[ERROR] Failed to attach issue #{issue_id} to Project Board: {err}",
            file=sys.stderr,
        )
        return False
    return True


def comment_on(issue_id: int, body: str) -> bool:
    code, _, err = run_cmd(
        ["gh", "issue", "comment", str(issue_id), "--body", body], check=False
    )
    if code != 0:
        print(f"[ERROR] Failed to comment on issue #{issue_id}: {err}", file=sys.stderr)
        return False
    return True


def _marker_already_recorded(existing: IncidentMatch, marker: str) -> Optional[bool]:
    if marker in existing.body:
        return True
    code, comments, _ = run_cmd(
        [
            "gh", "issue", "view", str(existing.number),
            "--json", "comments", "-q", ".comments[].body",
        ],
        check=False,
    )
    if code != 0:
        print(
            f"[ERROR] Could not read comments on issue #{existing.number}.",
            file=sys.stderr,
        )
        return None
    return marker in comments


def ensure_intake_labels(_severity: str) -> bool:
    return ensure_label(
        "origin:incident", "5319e7", "Opened from a production signal"
    )


def _issue_body(
    fingerprint: str,
    source: str,
    component: str,
    alert_name: str,
    severity: str,
    evidence: str,
) -> str:
    marker = incident_marker(fingerprint)
    event = event_marker(fingerprint, evidence)
    return f"""## Problem Description
Production signal `{alert_name}` fired on `{component}` (source: `{source}`).

incident-fingerprint: {fingerprint}
{marker}
{event}

## Failure Evidence
- Source: `{source}`
- Component: `{component}`
- Alert: `{alert_name}`
- Severity: `{severity}`
- Diagnostic summary: {evidence}

## Acceptance Criteria
- [ ] The production signal is cleared or the failing component is repaired (verify: `python3 scripts/incident_intake.py --signal resolved --source {source} --component {component} --alert-name {alert_name}`)

## Decision Boundaries
- This issue entered through incident intake. Do not open a parallel incident process.
- Triage must replace `touches: pending-ops-triage` with the real write set before Ready.

## Non-Goals
- A second incident board or pager rotation
- Changing merge, review, or claim mechanics

## Verification
`python3 -m unittest tests.test_incident_intake`

## Dependencies
depends-on: none
touches: pending-ops-triage
parallel-eligible: false
"""


def create_incident_issue(
    fingerprint: str,
    source: str,
    component: str,
    alert_name: str,
    severity: str,
    evidence: str,
) -> Optional[int]:
    """Create a skill-shaped issue and attach it to Backlog."""
    if not ensure_intake_labels(severity):
        print("[ERROR] Could not ensure intake labels.", file=sys.stderr)
        return None
    title = f"fix(ops): {alert_name} on {component}"
    cmd = [
        "gh", "issue", "create",
        "--title", title,
        "--body", _issue_body(fingerprint, source, component, alert_name, severity, evidence),
        "--label", f"type:fix,priority:{severity},origin:incident",
    ]
    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to create incident issue: {err}", file=sys.stderr)
        return None
    match = re.search(r"/issues/(\d+)", out)
    if not match:
        print(f"[ERROR] Could not parse issue number from: {out}", file=sys.stderr)
        return None
    new_id = int(match.group(1))
    if not attach_board(new_id, "Backlog"):
        return None
    return reconcile_created(new_id, fingerprint, evidence)


def record_recurrence(existing: IncidentMatch, fingerprint: str, evidence: str) -> Optional[int]:
    """Comment on the open issue; never regress its board status."""
    marker = event_marker(fingerprint, evidence)
    recorded = _marker_already_recorded(existing, marker)
    if recorded is None:
        return None
    if not recorded:
        body = (
            "## Recurring production signal\n\n"
            f"{marker}\n"
            f"- Diagnostic summary: {evidence}"
        )
        if not comment_on(existing.number, body):
            return None
    if existing.status and not attach_board(existing.number, existing.status):
        return None
    print(f"ℹ️ Recurring alert: updated issue #{existing.number}")
    return existing.number


def reconcile_created(
    new_id: int, fingerprint: str, evidence: str
) -> Optional[int]:
    """Keep the oldest open match if two firing calls raced."""
    query_ok, matches = _list_incident_matches(fingerprint)
    if not query_ok or matches is None:
        return None
    if not matches:
        print(f"✅ Created incident issue #{new_id}")
        return new_id
    canonical = min(matches, key=lambda item: item.number)
    for extra in matches:
        if extra.number == canonical.number:
            continue
        if extra.number == new_id and extra.status in UNCLAIMED_STATUSES:
            if not close_unclaimed(extra.number):
                return None
    if canonical.number != new_id:
        print(f"ℹ️ Reconciled duplicate to issue #{canonical.number}")
        return record_recurrence(canonical, fingerprint, evidence)
    print(f"✅ Created incident issue #{new_id}")
    return new_id


def repository_root() -> Optional[str]:
    """Return the primary worktree root even when invoked from a linked one."""
    code, common_dir, _ = run_cmd(
        ["git", "rev-parse", "--git-common-dir"],
        check=False,
    )
    if code != 0 or not common_dir:
        return None
    common_dir = os.path.abspath(common_dir.strip())
    return os.path.dirname(common_dir) if os.path.basename(common_dir) == ".git" else None


def _worktree_blocks(porcelain: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for chunk in porcelain.split("\n\n"):
        fields: dict[str, str] = {}
        for line in chunk.splitlines():
            key, _, value = line.partition(" ")
            if value:
                fields[key] = value
        if fields.get("worktree"):
            blocks.append(fields)
    return blocks


def prune_issue_worktrees(issue_id: int) -> bool:
    """Remove leftover local worktrees for a Done incident issue."""
    repo_root = repository_root()
    if not repo_root:
        print("[ERROR] Could not resolve repository root.", file=sys.stderr)
        return False
    code, out, _ = run_cmd(
        ["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root
    )
    if code != 0:
        print("[ERROR] Could not list worktrees.", file=sys.stderr)
        return False
    needle = f"issue-{issue_id}-"
    for fields in _worktree_blocks(out):
        path = fields.get("worktree", "")
        branch = fields.get("branch", "")
        if needle not in branch:
            continue
        if os.path.abspath(path) == os.path.abspath(repo_root):
            print("[ERROR] Refusing to remove the primary worktree.", file=sys.stderr)
            return False
        status_code, status, _ = run_cmd(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            check=False, cwd=path,
        )
        if status_code != 0 or status.strip():
            print(f"ℹ️ Skipping dirty or unreadable worktree {path}")
            continue
        rm_code, _, err = run_cmd(
            ["git", "worktree", "remove", path], check=False, cwd=repo_root
        )
        if rm_code != 0:
            print(f"[ERROR] Could not prune worktree {path}: {err}", file=sys.stderr)
            return False
    return True


def close_unclaimed(issue_id: int) -> bool:
    if not attach_board(issue_id, "Done"):
        return False
    code, _, err = run_cmd(
        ["gh", "issue", "close", str(issue_id), "--reason", "completed"], check=False
    )
    if code != 0:
        print(f"[ERROR] Failed to close issue #{issue_id}: {err}", file=sys.stderr)
        return False
    return True


def note_resolved(existing: IncidentMatch, fingerprint: str, evidence: str) -> Optional[bool]:
    marker = resolved_marker(fingerprint)
    recorded = _marker_already_recorded(existing, marker)
    if recorded is None:
        return None
    if recorded:
        return True
    body = (
        "## Production signal resolved\n\n"
        f"{marker}\n"
        f"- Diagnostic summary: {evidence}"
    )
    return comment_on(existing.number, body)


def intake_firing(
    source: str,
    component: str,
    alert_name: str,
    severity: str,
    evidence: str,
) -> Optional[int]:
    fingerprint = incident_fingerprint(source, component, alert_name)
    query_ok, existing = find_existing_incident(fingerprint)
    if not query_ok:
        print("[ERROR] Could not safely query existing incident issues.", file=sys.stderr)
        return None
    if existing is not None:
        return record_recurrence(existing, fingerprint, evidence)
    return create_incident_issue(
        fingerprint, source, component, alert_name, severity, evidence
    )


def intake_resolved(
    source: str,
    component: str,
    alert_name: str,
    evidence: str,
) -> Optional[int]:
    fingerprint = incident_fingerprint(source, component, alert_name)
    query_ok, existing = find_existing_incident(fingerprint, include_closed=True)
    if not query_ok:
        print("[ERROR] Could not safely query existing incident issues.", file=sys.stderr)
        return None
    if existing is None:
        print("ℹ️ No matching incident issue for resolved signal")
        return 0
    if existing.state != "CLOSED" and existing.status == "Done":
        if not close_unclaimed(existing.number):
            return None
        if not prune_issue_worktrees(existing.number):
            return None
        print(f"✅ Resolved alert: closed leftover Done issue #{existing.number}")
        return existing.number
    if existing.state == "CLOSED" and existing.status == "Done":
        if not prune_issue_worktrees(existing.number):
            return None
        print(f"✅ Resolved alert: cleaned leftover worktrees for issue #{existing.number}")
        return existing.number
    noted = note_resolved(existing, fingerprint, evidence)
    if noted is None or noted is False:
        return None
    if existing.status in UNCLAIMED_STATUSES:
        if not close_unclaimed(existing.number):
            return None
        if not prune_issue_worktrees(existing.number):
            return None
        print(f"✅ Resolved alert: closed issue #{existing.number}")
        return existing.number
    print(f"ℹ️ Resolved alert: noted on in-flight issue #{existing.number}")
    return existing.number


EVIDENCE_READ_LIMIT = 8192


def _read_evidence(text: str, evidence_file: Optional[str]) -> str:
    if evidence_file:
        with Path(evidence_file).open(encoding="utf-8", errors="replace") as handle:
            return handle.read(EVIDENCE_READ_LIMIT)
    return text


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open or update a governed issue from a production signal."
    )
    parser.add_argument("--signal", required=True, choices=("firing", "resolved"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--alert-name", required=True)
    parser.add_argument("--severity", default="p2", choices=VALID_SEVERITIES)
    parser.add_argument("--evidence", default="")
    parser.add_argument("--evidence-file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        source = validate_identity(args.source, "source")
        component = validate_identity(args.component, "component")
        alert_name = validate_identity(args.alert_name, "alert-name")
        evidence = sanitize_evidence(_read_evidence(args.evidence, args.evidence_file))
    except (ValueError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        fingerprint = incident_fingerprint(source, component, alert_name)
        print(
            f"[DRY-RUN] Would {args.signal} signal {alert_name} on {component} "
            f"(fingerprint {fingerprint})"
        )
        return 0

    if args.signal == "firing":
        result = intake_firing(source, component, alert_name, args.severity, evidence)
    else:
        result = intake_resolved(source, component, alert_name, evidence)
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
