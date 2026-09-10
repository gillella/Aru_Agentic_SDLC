"""Project inventory describes local candidates, never reviewer authority."""
from pathlib import Path
import re

from .config import DriverError

ENV = "ARU_CODING_REVIEWERS"
EXECUTABLES = {"claude-code": "claude-sub", "openai-codex": "codex",
               "xai-cursor": "cursor-agent", "google-antigravity": "agy"}

def inventory(config, repo: str) -> str | None:
    project = config.project(repo)
    if "coding_reviewers" not in project:
        return None
    identities = project["coding_reviewers"]
    if (not isinstance(identities, list) or not identities
            or any(not isinstance(i, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", i) for i in identities)
            or len(identities) != len(set(identities))):
        raise DriverError("coding_reviewers must select a nonempty unique list of canonical lane identities")
    entries, accounts, candidates = [], set(), set()
    for identity in sorted(identities):
        lane = config.lane(repo, identity)
        family = lane["family"]
        worker, probe = lane["command"], lane["probe_command"]
        if (family not in EXECUTABLES or Path(worker[0]).name != EXECUTABLES[family]
                or worker[0] != probe[0]):
            raise DriverError("coding_reviewers requires matching native worker/probe executables and family")
        subscription = None
        if family == "claude-code":
            if (len(worker) < 2 or not re.fullmatch(r"[1-9][0-9]*", worker[1])
                    or worker[1:2] != probe[1:2]):
                raise DriverError("coding_reviewers Claude subscription must agree in worker and probe")
            subscription = worker[1]
        account = lane["capacity_key"]
        candidate = (family, subscription)
        if account in accounts or candidate in candidates:
            raise DriverError("coding_reviewers repeats an account/subscription or ambiguous family")
        accounts.add(account)
        candidates.add(candidate)
        entries.append(f"{family}:{identity}" + (f"@{subscription}" if subscription else ""))
    return ",".join(entries)

def environment(config, repo: str, base: dict[str, str]) -> dict[str, str]:
    result = dict(base)
    value = inventory(config, repo)
    if value is not None:
        result[ENV] = value
    return result
