#!/usr/bin/env python3
"""Acquire or release the one issue writer claim."""

from __future__ import annotations

import argparse
import re

from common import (
    AGENT_PREFIX,
    REPOSITORY_AUTH,
    KernelError,
    contract_errors,
    ensure_label,
    gh_json,
    gh_paginated,
    issue,
    label_names,
    repo_slug,
    run,
    set_status,
    status_of,
    unresolved_dependencies,
)


_OPEN_PR_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    issue(number:$number){
      closedByPullRequestsReferences(first:100){
        nodes{number state}
        pageInfo{hasNextPage}
      }
    }
  }
}
"""


def safe_agent(agent: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", agent):
        raise KernelError("agent id must be 2-63 lowercase safe characters")
    return agent


def claimants(record: dict[str, object]) -> list[str]:
    return [name for name in label_names(record) if name.startswith(AGENT_PREFIX)]


def require_claim_state(number: int, claim_label: str, status: str) -> None:
    live = issue(number)
    if status_of(live) != status or claimants(live) != [claim_label]:
        raise KernelError(f"issue ownership changed before transition from {status}")


def other_active_claims(number: int, agent: str) -> list[int]:
    records = gh_paginated(
        f"repos/{repo_slug()}/issues?state=all&labels=agent%3A{agent}&per_page=100"
    )
    claims: list[int] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("number"), int):
            raise KernelError("active claim inventory is malformed")
        labels = label_names(record)
        lifecycle_active = any(
            name in {"status:in-progress", "status:in-review"} for name in labels
        )
        if (
            "pull_request" not in record
            and record["number"] != number
            and lifecycle_active
        ):
            claims.append(int(record["number"]))
    return sorted(claims)


def rollback_claim(number: int, claim_label: str) -> None:
    live = issue(number)
    owners = claimants(live)
    current = status_of(live)
    if claim_label not in owners:
        return
    if current == "In Progress" and owners == [claim_label]:
        set_status(number, "Ready", expected_current="In Progress")
    elif current not in {"Ready", "In Progress"}:
        raise KernelError("claim rollback found an unsafe lifecycle state")
    run(["gh", "issue", "edit", str(number), "--remove-label", claim_label])
    settled = issue(number)
    if claim_label in claimants(settled):
        raise KernelError("claim rollback did not settle")


def claim(number: int, agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    record = issue(number)
    if status_of(record) != "Ready":
        raise KernelError("issue is not Ready")
    errors = contract_errors(record)
    blocked = unresolved_dependencies(record)
    if errors or blocked:
        raise KernelError("; ".join(errors + [f"open dependencies: {blocked}"]))
    existing = claimants(record)
    if existing:
        raise KernelError(f"issue is already claimed by {existing[0][len(AGENT_PREFIX):]}")
    active = other_active_claims(number, agent)
    if active:
        raise KernelError(f"agent already has active issue claims: {active}")

    claim_label = AGENT_PREFIX + agent
    ensure_label(claim_label, color="1d76db", description=f"Claimed by {agent}")
    try:
        run(["gh", "issue", "edit", str(number), "--add-label", claim_label])
        reread = issue(number)
        owners = claimants(reread)
        if owners != [claim_label]:
            raise KernelError("claim race detected; no exclusive winner")
        set_status(
            number,
            "In Progress",
            expected_current="Ready",
            pre_mutation_check=lambda: require_claim_state(number, claim_label, "Ready"),
        )
        final = issue(number)
        if status_of(final) != "In Progress" or claimants(final) != [claim_label]:
            raise KernelError("claim did not settle")
    except KernelError as claim_error:
        try:
            rollback_claim(number, claim_label)
        except KernelError as rollback_error:
            raise KernelError(
                f"{rollback_error}; original claim failure: {claim_error}"
            ) from rollback_error
        raise
    return {"issue": number, "agent": agent, "status": "In Progress"}


def linked_open_prs(number: int) -> list[int]:
    owner, name = repo_slug().split("/", 1)
    data = gh_json(
        [
            "api",
            "graphql",
            "-f",
            f"query={_OPEN_PR_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ],
        auth=REPOSITORY_AUTH,
    )
    issue_node = ((data.get("data") or {}).get("repository") or {}).get("issue")
    connection = (issue_node or {}).get("closedByPullRequestsReferences")
    if (
        not isinstance(connection, dict)
        or not isinstance(connection.get("nodes"), list)
        or not isinstance(connection.get("pageInfo"), dict)
        or connection["pageInfo"].get("hasNextPage") is not False
    ):
        raise KernelError("linked pull-request inventory is incomplete")
    opened: list[int] = []
    for record in connection["nodes"]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("number"), int)
            or record.get("state") not in {"OPEN", "CLOSED", "MERGED"}
        ):
            raise KernelError("linked pull-request inventory is malformed")
        if record["state"] == "OPEN":
            opened.append(int(record["number"]))
    return sorted(opened)


def require_releasable(number: int, claim_label: str) -> None:
    require_claim_state(number, claim_label, "In Progress")
    opened = linked_open_prs(number)
    if opened:
        raise KernelError(f"release refused: linked open pull requests: {opened}")


def restore_released_claim(number: int, claim_label: str) -> None:
    record = issue(number)
    if status_of(record) != "Ready" or claimants(record):
        raise KernelError("release race could not safely restore the claim")
    try:
        run(["gh", "issue", "edit", str(number), "--add-label", claim_label])
        set_status(
            number, "In Progress", expected_current="Ready",
            pre_mutation_check=lambda: require_claim_state(number, claim_label, "Ready"),
        )
        settled = issue(number)
        if status_of(settled) != "In Progress" or claimants(settled) != [claim_label]:
            raise KernelError("release race claim restoration did not settle")
    except KernelError as restore_error:
        try:
            rollback_claim(number, claim_label)
        except KernelError as rollback_error:
            raise KernelError(
                f"{rollback_error}; original restoration failure: {restore_error}"
            ) from rollback_error
        raise


def release(number: int, agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    claim_label = AGENT_PREFIX + agent
    record = issue(number)
    owners = claimants(record)
    if owners != [claim_label]:
        raise KernelError("release refused: agent is not the exclusive claimant")
    if status_of(record) != "In Progress":
        raise KernelError("release refused: issue is not In Progress")
    opened = linked_open_prs(number)
    if opened:
        raise KernelError(f"release refused: linked open pull requests: {opened}")
    set_status(
        number,
        "Ready",
        expected_current="In Progress",
        pre_mutation_check=lambda: require_releasable(number, claim_label),
    )
    try:
        run(["gh", "issue", "edit", str(number), "--remove-label", claim_label])
    except KernelError as release_error:
        try:
            set_status(number, "In Progress", expected_current="Ready")
        except KernelError as rollback_error:
            raise KernelError(
                f"{rollback_error}; original release failure: {release_error}"
            ) from rollback_error
        raise
    settled = issue(number)
    if status_of(settled) != "Ready" or claimants(settled):
        raise KernelError("release did not settle at unclaimed Ready")
    appeared = linked_open_prs(number)
    if appeared:
        race_error = KernelError(f"release raced with linked open pull requests: {appeared}")
        try:
            restore_released_claim(number, claim_label)
        except KernelError as rollback_error:
            raise KernelError(
                f"{rollback_error}; original release race: {race_error}"
            ) from rollback_error
        raise race_error
    return {"issue": number, "agent": agent, "status": "Ready"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    try:
        result = release(args.issue, args.agent) if args.release else claim(args.issue, args.agent)
    except KernelError as exc:
        parser.error(str(exc))
    print(f"#{result['issue']} {result['status']} ({result['agent']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
