#!/usr/bin/env python3
"""Acquire or release the one issue writer claim."""

from __future__ import annotations

import argparse
import re

from common import (
    AGENT_PREFIX,
    KernelError,
    contract_errors,
    ensure_label,
    issue,
    label_names,
    run,
    set_status,
    status_of,
    unresolved_dependencies,
)


def safe_agent(agent: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", agent):
        raise KernelError("agent id must be 2-63 lowercase safe characters")
    return agent


def claim(number: int, agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    record = issue(number)
    if status_of(record) != "Ready":
        raise KernelError("issue is not Ready")
    errors = contract_errors(record)
    blocked = unresolved_dependencies(record)
    if errors or blocked:
        raise KernelError("; ".join(errors + [f"open dependencies: {blocked}"]))
    existing = [name for name in label_names(record) if name.startswith(AGENT_PREFIX)]
    if existing:
        raise KernelError(f"issue is already claimed by {existing[0][len(AGENT_PREFIX):]}")

    claim_label = AGENT_PREFIX + agent
    ensure_label(claim_label, color="1d76db", description=f"Claimed by {agent}")
    run(["gh", "issue", "edit", str(number), "--add-label", claim_label, "--add-assignee", "@me"])
    try:
        reread = issue(number)
        claimants = [name for name in label_names(reread) if name.startswith(AGENT_PREFIX)]
        if claimants != [claim_label]:
            raise KernelError("claim race detected; no exclusive winner")
        set_status(number, "In Progress")
        final = issue(number)
        if status_of(final) != "In Progress" or claim_label not in label_names(final):
            raise KernelError("claim did not settle")
    except KernelError as claim_error:
        try:
            run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(number),
                    "--remove-label",
                    claim_label,
                    "--remove-assignee",
                    "@me",
                ],
                check=False,
            )
        except KernelError as rollback_error:
            raise KernelError(
                f"{rollback_error}; original claim failure: {claim_error}"
            ) from rollback_error
        raise
    return {"issue": number, "agent": agent, "status": "In Progress"}


def release(number: int, agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    claim_label = AGENT_PREFIX + agent
    record = issue(number)
    claimants = [name for name in label_names(record) if name.startswith(AGENT_PREFIX)]
    if claimants != [claim_label]:
        raise KernelError("release refused: agent is not the exclusive claimant")
    run(
        ["gh", "issue", "edit", str(number), "--remove-label", claim_label, "--remove-assignee", "@me"]
    )
    set_status(number, "Ready")
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
