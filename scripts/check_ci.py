#!/usr/bin/env python3
"""Read required exact-current-head GitHub check state for one pull request."""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone

from common import KernelError, gh_json, json_print, repo_slug
from merge_state import require_direct_merge_history


def parse_time(value: str, *, subject: str = "review assignment") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise KernelError(f"{subject} timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise KernelError(f"{subject} timestamp has no timezone")
    return parsed

DEFAULT_REQUIRED_CHECK = "aru-governed-pr"
GITHUB_ACTIONS_APP_ID = 15368


def check_name(record: dict) -> str:
    name = record.get("name") or record.get("context")
    if not isinstance(name, str) or not name:
        raise KernelError("CI returned a nameless check")
    return name


def check_state(record: dict) -> str:
    status = str(record.get("status") or "").upper()
    conclusion = str(record.get("conclusion") or record.get("state") or "").upper()
    if status and status != "COMPLETED":
        return "pending"
    if conclusion in {"PENDING", "EXPECTED", ""}:
        return "pending"
    if conclusion == "SUCCESS":
        return "success"
    return "failure"


def check_run_inventory(data: object, *, paginated: bool = False, key: str = "check_runs") -> list[dict]:
    """Validate complete direct or slurped inventories without dropping bad records."""
    pages = data if paginated else [data]
    if not isinstance(pages, list) or not pages or any(
        not isinstance(page, dict) or not isinstance(page.get(key), list) for page in pages
    ):
        raise KernelError("GitHub check/run inventory is malformed")
    records = [record for page in pages for record in page[key]]
    if (any(type(page.get("total_count")) is not int or page["total_count"] != len(records) for page in pages)
            or any(not isinstance(record, dict) for record in records)):
        raise KernelError("GitHub check/run inventory is incomplete")
    return records


def exact_head_check_runs(head: str) -> list[dict]:
    return check_run_inventory(gh_json(
        ["api", f"repos/{repo_slug()}/commits/{head}/check-runs?per_page=100&filter=latest"]
    ))


def _timestamp(value: object) -> datetime:
    parsed = parse_time(str(value), subject="governed CI creation")
    if parsed > datetime.now(timezone.utc):
        raise KernelError("governed CI creation timestamp is in the future")
    return parsed


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _execution_state(record: dict) -> str:
    status, conclusion = record.get("status"), record.get("conclusion")
    status = status.lower() if isinstance(status, str) else None
    if conclusion is not None and not isinstance(conclusion, str):
        raise KernelError("governed CI conclusion is malformed")
    conclusion = conclusion.lower() if isinstance(conclusion, str) else conclusion
    if status in {"queued", "in_progress", "waiting", "requested", "pending"} and conclusion in {None, ""}:
        return "pending"
    if status == "completed" and conclusion in {
        "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale",
    }:
        return "success" if conclusion == "success" else "failure"
    raise KernelError("governed CI execution state is malformed or conflicting")


def _workflow_runs(slug: str, head: str) -> list[dict]:
    endpoint = f"repos/{slug}/actions/workflows/governed-pr.yml"
    workflow = gh_json(["api", endpoint])
    if (not isinstance(workflow, dict) or not _positive(workflow.get("id"))
            or workflow.get("path") != ".github/workflows/governed-pr.yml"
            or workflow.get("state") != "active"):
        raise KernelError("governed workflow identity is unavailable")
    data = gh_json(["api", f"{endpoint}/runs?head_sha={head}&per_page=100"])
    runs = check_run_inventory(data, key="workflow_runs")
    if any(run.get("workflow_id") != workflow["id"] for run in runs):
        raise KernelError("governed workflow-run inventory is conflicting")
    return runs


def _run_binding(run: dict, pr: dict, slug: str, merged_at: datetime | None = None) -> bool:
    """Validate provenance before excluding history; PR associations are mutable."""
    if (not _positive(run.get("id")) or not _positive(run.get("check_suite_id"))
            or not _positive(run.get("run_attempt"))
            or run.get("event") != "pull_request"
            or run.get("path") != ".github/workflows/governed-pr.yml"
            or run.get("head_sha") != pr["headRefOid"]
            or run.get("head_branch") != pr["headRefName"]
            or any(not isinstance(run.get(key), dict) or run[key].get("full_name") != slug
                   for key in ("repository", "head_repository"))):
        raise KernelError("governed Actions run has incompatible provenance")
    current = _timestamp(run.get("created_at")) >= _timestamp(pr.get("createdAt"))
    associations = run.get("pull_requests")
    if associations == [] and merged_at is not None:
        # GitHub may clear associations after merge. A run before this PR is
        # history; only an in-lifetime run can use the merged PR's evidence.
        return current
    if not isinstance(associations, list) or len(associations) != 1:
        raise KernelError("governed Actions PR association is incomplete or conflicting")
    linked = associations[0]
    if (not isinstance(linked, dict) or not _positive(linked.get("number"))
            or (current and linked["number"] != pr["number"])):
        raise KernelError("governed Actions run is not bound to the current PR")
    for side, ref in (("head", "headRefName"), ("base", "baseRefName")):
        branch = linked.get(side)
        if (not isinstance(branch, dict) or branch.get("ref") != pr[ref]
                or not isinstance(branch.get("repo"), dict)
                or branch["repo"].get("url") != f"https://api.github.com/repos/{slug}"
                or (side == "head" and branch.get("sha") != pr["headRefOid"])):
            raise KernelError("governed Actions PR/head/repository binding conflicts")
    return current


def _check_binding(check: dict, head: str, slug: str) -> int:
    app = check.get("app")
    if (not isinstance(app, dict) or type(app.get("id")) is not int
            or app["id"] != GITHUB_ACTIONS_APP_ID or app.get("slug") != "github-actions"):
        raise KernelError("required GitHub check has an untrusted source")
    if check.get("head_sha") != head:
        raise KernelError("required GitHub check is not bound to the exact head")
    url = check.get("details_url")
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(slug)}/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)",
        url if isinstance(url, str) else "",
    )
    if not match or not _positive(check.get("id")) or int(match[2]) != check["id"]:
        raise KernelError("required GitHub check has malformed Actions job provenance")
    return int(match[1])


def commit_verdict(
    head: str, *, pr: dict | None = None, merged_at: datetime | None = None,
) -> dict[str, object]:
    """Conservatively combine all governing executions, never select newest green.

    A complete bounded inventory binds each Actions check to its canonical workflow
    run and suite. A run created before this PR cannot be its original event even
    if GitHub now associates it with the replacement PR. Only such proven history
    is excluded. Every current run contributes: failures dominate pending, pending
    dominates success, and a queued run without a job still blocks. A completed
    run without its required check, or contradictory evidence, is unreadable.
    Reruns are observed through their current run state and latest suite checks;
    run creation (not attempt start/completion) determines the historical boundary.
    """
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise KernelError("exact-head GitHub check state is incomplete")
    if (not isinstance(pr, dict) or pr.get("headRefOid") != head
            or not _positive(pr.get("number"))
            or any(not isinstance(pr.get(key), str) or not pr[key] for key in ("headRefName", "baseRefName"))):
        raise KernelError("current PR provenance is required for governed CI")
    _timestamp(pr.get("createdAt"))
    records = exact_head_check_runs(head)
    slug = repo_slug()
    checks = {}
    for record in records:
        if check_name(record) != DEFAULT_REQUIRED_CHECK:
            continue
        run_id = _check_binding(record, head, slug)
        if run_id in checks:
            raise KernelError("required GitHub check has ambiguous job evidence")
        _execution_state(record)
        checks[run_id] = record
    states, seen, suites = [], set(), set()
    for run in _workflow_runs(slug, head):
        current = _run_binding(run, pr, slug, merged_at)
        if run["id"] in seen or run["check_suite_id"] in suites:
            raise KernelError("governed workflow-run inventory is ambiguous")
        seen.add(run["id"])
        suites.add(run["check_suite_id"])
        check = checks.get(run["id"])
        state = _bound_state(run, check)
        if merged_at is not None and current:
            if state != "success" or check is None or not (
                _timestamp(pr["createdAt"]) <= _timestamp(run["created_at"])
                <= _timestamp(run.get("run_started_at")) <= _timestamp(check.get("started_at"))
                <= _timestamp(check.get("completed_at")) <= _timestamp(run.get("updated_at")) <= merged_at
            ):
                raise KernelError("governed CI did not succeed within the pre-merge lifetime")
        if current:
            states.append(state)
    if set(checks) - seen:
        raise KernelError("required check is missing from the governed workflow inventory")
    state = "failure" if "failure" in states else "pending" if not states or "pending" in states else "success"
    return {"head": head, "state": state, "checks": [DEFAULT_REQUIRED_CHECK]}


def _bound_state(run: dict, check: dict | None) -> str:
    state = _execution_state(run)
    if check is None:
        if state != "pending":
            raise KernelError("completed governed workflow is missing its required check")
        return state
    suite = check.get("check_suite")
    if not isinstance(suite, dict) or not _positive(suite.get("id")) or suite["id"] != run["check_suite_id"]:
        raise KernelError("governed check suite conflicts with Actions run")
    check_status = _execution_state(check)
    if state == "success" and check_status != "success":
        raise KernelError("successful governed workflow conflicts with required check")
    return "failure" if "failure" in (state, check_status) else "pending" if "pending" in (state, check_status) else "success"


def finalization_verdict(pr: dict) -> dict[str, object]:
    """Read-only historical proof, used explicitly by confirmed-merge close-out.

    The merged REST PR records the historical base; never query a branch tip or
    require the head branch to survive. Bind its ordered parents to that base and
    the exact head, and retain the independent, bounded queue-history refusal.
    """
    number, head = pr["number"], pr["headRefOid"]
    slug = repo_slug()
    endpoint = f"repos/{slug}/pulls/{number}"
    merged = gh_json(["api", endpoint])
    commit = pr.get("mergeCommit")
    if (pr.get("state") != "MERGED" or not isinstance(commit, dict)
            or not isinstance(commit.get("oid"), str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", commit["oid"])
            or not isinstance(merged, dict) or merged.get("number") != number
            or merged.get("state") != "closed" or merged.get("merged") is not True
            or merged.get("merge_commit_sha") != commit["oid"]
            or merged.get("merged_at") != pr.get("mergedAt")
            or merged.get("created_at") != pr.get("createdAt")):
        raise KernelError("confirmed merged PR provenance is incomplete or conflicting")
    merged_at = _timestamp(merged.get("merged_at"))
    if _timestamp(pr.get("createdAt")) >= merged_at:
        raise KernelError("merged PR lifetime is conflicting")
    for side, ref in (("head", "headRefName"), ("base", "baseRefName")):
        branch = merged.get(side)
        if (not isinstance(branch, dict) or branch.get("ref") != pr.get(ref)
                or not isinstance(branch.get("repo"), dict) or branch["repo"].get("full_name") != slug
                or not isinstance(branch.get("sha"), str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", branch["sha"])
                or (side == "head" and branch["sha"] != head)):
            raise KernelError("merged PR repository/head/base provenance conflicts")
    obj = gh_json(["api", f"repos/{slug}/commits/{commit['oid']}"])
    parents = obj.get("parents") if isinstance(obj, dict) else None
    if (not isinstance(obj, dict) or obj.get("sha") != commit["oid"]
            or not isinstance(parents, list) or len(parents) != 2
            or any(not isinstance(parent, dict) for parent in parents)
            or [parent.get("sha") for parent in parents] != [merged["base"]["sha"], head]
            or merged["base"]["sha"] == head):
        raise KernelError("confirmed direct merge commit parents conflict")
    require_direct_merge_history(number, head, commit["oid"])
    result = commit_verdict(head, pr=pr, merged_at=merged_at)
    if result["state"] != "success":
        raise KernelError("governed CI has no current in-lifetime successful run")
    if gh_json(["api", endpoint]) != merged:
        raise KernelError("merged PR provenance changed during CI inspection")
    return {"pr": number, **result}


def ci_verdict(number: int) -> dict[str, object]:
    fields = "number,headRefOid,createdAt,headRefName,baseRefName"
    pr = gh_json(["pr", "view", str(number), "--json", fields])
    if not isinstance(pr, dict) or pr.get("number") != number:
        raise KernelError("exact-head GitHub check state is incomplete")
    result = commit_verdict(pr.get("headRefOid"), pr=pr)
    if gh_json(["pr", "view", str(number), "--json", fields]) != pr:
        raise KernelError("PR provenance changed during CI inspection")
    return {"pr": number, **result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = ci_verdict(args.pr)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        print(f"PR #{args.pr} {result['head']}: {result['state']}")
    return 0 if result["state"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
