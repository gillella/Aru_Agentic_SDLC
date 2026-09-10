"""Bound unchanged author attempts using existing receipts, never lifecycle state."""
from __future__ import annotations

import hashlib
import json
import math
import re

from .config import DriverError
from .state import write_json

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 60

def validate_policy(project):
    epoch = project.get("worker_retry_epoch", "initial")
    if not isinstance(epoch, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", epoch):
        raise DriverError("worker_retry_epoch must name an explicit operator recovery revision")

def context(config, repo, work):
    """Only a new kernel action/PR/head or explicit operator epoch renews work."""
    return hashlib.sha256(json.dumps({
        "repo": repo, "issue": work["issue"], "action": work["type"],
        "pr": work.get("pr"), "head": work.get("head"),
        "epoch": config.project(repo).get("worker_retry_epoch", "initial"),
    }, sort_keys=True).encode()).hexdigest()

def stamp(config, record, work_type):
    if record["kind"] != "review":
        record["retry_context"] = context(config, record["repo"], {
            "issue": record["issue"], "type": work_type,
            "pr": record.get("pr"), "head": record.get("head"),
        })

def validate(record):
    if "retry_context" in record and not _fingerprint(record["retry_context"]):
        raise DriverError("worker retry context is invalid")
    observation = record.get("retry_observation")
    if observation is None:
        return
    if (not isinstance(observation, dict)
            or set(observation) != {"context", "attempt", "observed_at", "retry_at"}
            or not _fingerprint(observation["context"])
            or type(observation["attempt"]) is not int
            or not 1 <= observation["attempt"] <= MAX_ATTEMPTS
            or any(type(observation[k]) not in (int, float)
                   or not math.isfinite(observation[k]) or observation[k] < 0
                   for k in ("observed_at", "retry_at"))):
        raise DriverError("worker retry observation is invalid")

def _fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None

def latest_attempt(receipts):
    return max(receipts, key=lambda r: (
        r["started_at"], r["state"] not in {"claiming", "prepared"}, r["id"],
    ))

def gate(config, state, repo, work, receipts, now):
    """A released reservation with no governed progress consumes one attempt.

    Exit zero is not progress. A missing terminal receipt also counts, recovering
    lost workers without endlessly relaunching them. Claim/branch preparation and
    Stop-fenced children which never ran are recoverable without this penalty.
    Structured permission and quota policies retain their stricter recovery rules.
    """
    latest = latest_attempt(receipts)
    if (latest["state"] in {"claiming", "prepared"} or latest.get("retry_blocked") is True
            or ("admission_stop" in latest and not latest.get("child_pid")
                and latest["admission_stop"] != state.stop_nonce(repo))):
        return None
    current = context(config, repo, work)
    previous = latest.get("retry_observation")
    baseline = latest.get("retry_context") or (previous or {}).get("context")
    if baseline is not None and baseline != current:
        return None
    if previous is None:
        attempts = [r["retry_observation"]["attempt"] for r in receipts
                    if r.get("retry_observation", {}).get("context") == current]
        attempt = min(MAX_ATTEMPTS, max(attempts, default=0) + 1)
        finished = latest.get("finished_at", now)
        if type(finished) not in (int, float) or not math.isfinite(finished) or not 0 <= finished <= now:
            raise DriverError("worker completion time is invalid for retry")
        previous = {"context": current, "attempt": attempt, "observed_at": now,
                    "retry_at": finished + BACKOFF_SECONDS * 2 ** (attempt - 1)}
        latest["retry_observation"] = previous
        write_json(state.worker_path(latest["id"]), latest)
    if previous["attempt"] >= MAX_ATTEMPTS:
        return {"type": "worker_blocked", "execution": "blocked",
                "reason": "unchanged worker attempt limit reached; inspect retained work and advance worker_retry_epoch only after correcting the cause",
                "owner": "operator", "worker_id": latest["id"], "retry_exhausted": True}
    if previous["retry_at"] > now:
        return {"type": "worker_retry_wait", "execution": "waiting",
                "reason": "worker ended without governed task progress; bounded retry backoff",
                "retry_at": previous["retry_at"], "owner": "Hermes Driver recovery heartbeat",
                "worker_id": latest["id"]}
    return None
