"""Pure prompt and fingerprint helpers for the durable fleet runner."""

import hashlib
import json
from typing import Any


def state_fingerprint(fleet: dict[str, Any], work: dict[str, Any]) -> str:
    work_type = str(work.get("type") or "idle")
    raw_number = work.get("issue") if work_type == "issue" else work.get("pr")
    stable = {
        "fleet_state": fleet.get("state"), "summary": fleet.get("summary"),
        "open_issues": fleet.get("open_issues_count"),
        "open_prs": fleet.get("open_prs_count"),
        "active_claims": fleet.get("active_claims"), "work_type": work_type,
        "work_number": raw_number if isinstance(raw_number, int) else None,
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def build_prompt(config: Any, work: dict[str, Any]) -> str:
    work_type = str(work.get("type") or "idle")
    number = work.get("issue") if work_type == "issue" else work.get("pr")
    subject = work_type if not isinstance(number, int) else f"{work_type} #{number}"
    return (
        f"You are {config.agent}, model family {config.family}, in the trusted "
        f"repository {config.repo}. The durable Aru runner observed eligible "
        f"work ({subject}). Read AGENTS.md and the run-aru-factory skill, then "
        "execute exactly one governed Aru Code next unit. The durable runner "
        "used the canonical picker with your stable identity and promoted a "
        "qualified Backlog item when necessary; recover that identity's "
        "current work through the framework helpers before taking any new "
        "item. Complete or safely hand off that one unit, then exit this child "
        "session. Do not start a second unit and do not bypass Issue-First, "
        "worktrees, tests, CI, independent review, or the merge helper. Recover "
        "existing work for this identity before claiming anything new."
    )
