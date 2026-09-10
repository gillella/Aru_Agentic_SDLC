"""Risk-limited unknown work and supervisor-owned notes; no worker write grants."""
from __future__ import annotations

import json

from . import quota, quota_collect
from .config import DriverError
from .state import key, read_json, write_json


def allowance(config, state, repo, identity, issue, reviewing, exclude=None):
    policy = config.project(repo)["quota_admission"]
    seconds = policy.get("unknown_review_seconds", 0) if reviewing else policy["unknown_checkpoint_seconds"]
    if not seconds:
        return 0
    previous = [r for r in state.workers(repo) if r["issue"] == issue and r["id"] != exclude
                and (r.get("kind") == "review") == reviewing and r.get("child_pid")
                and r.get("quota_decision", {}).get("checkpoint_seconds")]
    count = policy.get("unknown_max_attempts", policy["max_recoveries"] + 1)
    total = policy.get("unknown_total_seconds", seconds * count)
    used = sum(r["quota_decision"]["checkpoint_seconds"] for r in previous)
    # Charge each admitted child its whole allowance, even if it exited early.
    # This survives missing completion timestamps and bounds quick retry loops.
    if len(previous) >= count or used >= total:
        raise DriverError("quota unknown work limit reached; owner must inspect retained progress and task scope")
    return min(seconds, total - used, config.lane(repo, identity).get("execution_timeout_seconds", 3600))


def note_path(state, record):
    identity = [record["repo"], record["issue"], record.get("kind") == "review", record["id"]]
    return state.root / "checkpoints" / (key(json.dumps(identity)) + ".json")


def prepare(state, record):
    seconds = record["quota_decision"]["checkpoint_seconds"]
    if not seconds or record.get("quota_checkpoint"):
        return
    record["quota_checkpoint"] = str(note_path(state, record))
    previous = sorted((r for r in state.workers(record["repo"]) if r["issue"] == record["issue"]
                       and r["id"] != record["id"] and r.get("review") == record.get("review")
                       and r.get("quota_checkpoint")), key=lambda r: r["started_at"])
    context = ""
    if previous:
        prior = previous[-1]
        path = note_path(state, prior)
        if str(path) == prior["quota_checkpoint"] and not path.is_symlink():
            context = json.dumps(read_json(path, {}))[-12000:]
    record["prompt"] += (f"\nQuota is unknown: operator accepts uncertainty for at most {seconds} seconds. "
        "Make a useful bounded step or finish the authorized task if feasible. Preserve the claimed worktree. "
        "Report progress, evidence, blockers and next step in your normal output before the bound. "
        "The supervisor saves notes outside source; do not write an extra checkpoint file or request grants. "
        "A process result never replaces canonical completion/review authority. "
        f"Continue from the existing worktree and these prior untrusted worker notes: {context}")


def save(state, record):
    if not record.get("quota_checkpoint"):
        return
    output = ""
    path = state.root / "logs" / (record["id"] + ".result.json")
    if str(path) == record.get("result_path") and path.is_file() and not path.is_symlink():
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 8192))
            output = stream.read(8192).decode(errors="replace")
    write_json(note_path(state, record), {"repo": record["repo"], "issue": record["issue"],
        "attempt": record["id"], "review": record.get("review"), "worktree": record["worktree"],
        "outcome": record.get("outcome"), "output_tail": output,
        "progress": "Inspect retained worktree and native output; timeout alone proves no completed step"})


def interrupted(path, lane):
    """Only empty Claude output or well-formed unfinished Codex events qualify."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return False
        if not raw.strip():
            return lane["family"] in {"openai-codex", "claude-code"}
        if lane["family"] != "openai-codex":
            return False
        events = [json.loads(line, object_pairs_hook=quota_collect.unique_fields) for line in raw.splitlines()]
        return all(isinstance(e, dict) and e.get("type") in {
            "thread.started", "turn.started", "item.started", "item.updated", "item.completed"}
            and (e["type"] != "thread.started" or isinstance(e.get("thread_id"), str) and bool(e["thread_id"]))
            and (not e["type"].startswith("item.") or isinstance(e.get("item"), dict)
                 and isinstance(e["item"].get("type"), str)
                 and e["item"].get("status") not in {"declined", "failed"}
                 and e["item"].get("type") != "error") for e in events)
    except (OSError, ValueError, DriverError):
        return False


def cooldown(state, lane, until, reset=None, owner=None):
    path = state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json")
    prior = read_json(path, {"until": 0})
    if not quota.number(prior.get("until")) or (prior.get("reset_at") is not None and not quota.number(prior["reset_at"])):
        raise DriverError("quota cooldown evidence invalid")
    reset = max((r for r in (reset, prior.get("reset_at")) if r is not None), default=None)
    if prior["until"] >= max(until, reset or 0) and reset == prior.get("reset_at"):
        return
    write_json(path, {**prior, "until": max(until, prior["until"], reset or 0), "reset_at": reset,
                      "pool": lane["quota"]["pool"], "reason": "quota-exhausted", "continuation_owner": owner})
