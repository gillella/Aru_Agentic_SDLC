"""Supervised quota checkpoints, terminal evidence and bounded calibration."""
from __future__ import annotations

import json
import re
import time

from . import permissions, quota, quota_admission as admission, quota_collect
from .config import DriverError
from .state import key, write_json


def recheck(config, state, record, adapter):
    if not record.get("quota_decision"):
        return
    old = record["quota_decision"]
    prepare(config, state, record, adapter, exclude=record["id"])
    if old["checkpoint_seconds"] != record["quota_decision"]["checkpoint_seconds"]:
        raise DriverError("quota checkpoint mode changed before child execution")


def prepare(config, state, record, adapter, *, exclude=None):
    if not quota.enabled(config, record["repo"]):
        return
    for other in state.workers(record["repo"]):
        lane = config.lanes.get(other["agent"], {})
        if (other["issue"] == record["issue"] and other["id"] != exclude
                and other["id"] in state.capacity_holders(other["capacity_key"], lane.get("max_sessions", 1))):
            raise DriverError("quota launch excluded by a live task owner")
    author = record["review"]["author"] if record["kind"] == "review" else record["agent"]
    task = adapter.revalidate(record["issue"], agent=author)
    record["quota_decision"] = admission.evaluate(config, state, record["repo"], record["agent"],
        task, record["kind"], adapter, exclude=exclude, review=record.get("review"))
    record["policy_fingerprint"] = permissions.fingerprint(config, record["repo"], record["agent"])
    seconds = record["quota_decision"]["checkpoint_seconds"]
    if seconds:
        checkpoint = state.root / "checkpoints" / (key(record["repo"] + str(record["issue"])) + ".md")
        checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        record["quota_checkpoint"] = str(checkpoint)
        record["prompt"] += (f"\nQuota is unknown. This is only a {seconds}-second checkpoint, not a full task. "
            f"Finish a small reversible step and save actual progress, tests and next step at {checkpoint}. "
            "Stop before this bound; preserve claim, worktree and partial work. No completion guarantee.")


def argv(lane, record):
    command = [p.replace("{prompt}", record["prompt"]) for p in lane["command"]]
    if lane["family"] == "claude-code" and "--output-format" not in command:
        command[-1:-1] = ["--output-format", "json"]
    elif lane["family"] == "openai-codex" and "--json" not in command:
        command.insert(command.index("exec") + 1, "--json")
    return command


def observe(path, lane, exit_code):
    if lane["family"] == "claude-code":
        return permissions.observe_result(path, exit_code, quota_errors=True)
    if lane["family"] == "openai-codex":
        try:
            with path.open("r+b") as stream:
                raw = stream.read(permissions.RESULT_BYTES + 1)
                if len(raw) > permissions.RESULT_BYTES:
                    stream.truncate(permissions.RESULT_BYTES)
                    raise ValueError("oversized")
            events = [json.loads(line) for line in raw.splitlines()]
            if not events or any(not isinstance(e, dict) or not isinstance(e.get("type"), str)
                                 or ("item" in e and not isinstance(e["item"], dict)) for e in events):
                raise ValueError("invalid")
            failure = events[-1]
            error = failure.get("error")
            if (failure["type"] == "turn.failed" and isinstance(error, dict)
                    and isinstance(error.get("message"), str)
                    and re.fullmatch(r"You've hit your usage limit(?:\.[^\r\n]{0,400})?", error["message"])
                    and not any(e.get("item", {}).get("status") == "declined" for e in events)
                    and all(e.get("message") == error["message"] for e in events if e["type"] == "error")):
                return {"outcome": "quota_exhausted", "retry_blocked": False, "quota_reset_at": None,
                        "reason": "authenticated Codex usage limit exhausted", "governed_completion": False}
            if (failure["type"] == "turn.completed" and not exit_code and isinstance(failure.get("usage"), dict)
                    and all(quota.number(failure["usage"].get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens"))):
                return {"outcome": "reported_success", "retry_blocked": True,
                        "reason": "worker reported success; unchanged task requires governed progress", "governed_completion": False}
        except (OSError, ValueError):
            pass
    return {"outcome": "result_unavailable", "retry_blocked": True,
            "reason": "quota worker result invalid or unsupported; unchanged retries blocked"}


def finish(config, state, record):
    decision = record.get("quota_decision")
    if not decision:
        return
    lane, now = config.lane(record["repo"], record["agent"]), time.time()
    record["quota_continuation"] = {"owner": "Hermes Driver completion/heartbeat", "issue": record["issue"],
                                    "worktree": record["worktree"], "checkpoint": record.get("quota_checkpoint")}
    record["quota_measurement"] = {"state": "unknown", "reason": "no-comparable-full-completion-observations"}
    # Release a speculative review budget for a failed/checkpoint author; a
    # reported full task retains its budget until review dispatch or live Done.
    if record.get("outcome") != "reported_success":
        record["quota_review_released"] = True
    if record.get("outcome") == "reported_success" and decision["checkpoint_seconds"]:
        record.update(outcome="quota_checkpoint", retry_blocked=False, quota_review_released=True,
                      reason="bounded quota checkpoint ended; completion/heartbeat owns limited continuation")
    if record.get("outcome") == "quota_exhausted":
        until = now + config.project(record["repo"])["quota_admission"]["cooldown_seconds"]
        write_json(state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json"), {
            "until": until, "reset_at": None, "pool": lane["quota"]["pool"], "reason": "quota-exhausted",
            "continuation_owner": record["id"]})
    if record.get("child_pid") is None:
        return
    try:
        after = quota.validate(quota_collect.collect(config, record["repo"], record["agent"]), lane,
                               time.time(), config.project(record["repo"])["quota_admission"]["max_age_seconds"])
        record["quota_after"] = after
        if after["state"] == "exhausted":
            resets = [w["reset_at"] for w in after["windows"].values() if w["remaining"] == 0]
            if resets:
                write_json(state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json"), {
                    "until": max(resets), "reset_at": max(resets), "pool": lane["quota"]["pool"],
                    "reason": "quota-exhausted", "continuation_owner": record["id"]})
        before = decision["observation"]
        if (record.get("outcome") != "reported_success" or decision["checkpoint_seconds"]
                or before["state"] != "known" or after["state"] != "known"
                or any(before["windows"][n]["reset_at"] != w["reset_at"] for n, w in after["windows"].items())):
            return
        percent = {n: before["windows"][n]["remaining"] - w["remaining"] for n, w in after["windows"].items()}
        if any(not quota.number(v, 0, 100) for v in percent.values()):
            return
        estimate = decision["demand"]
        sample = {"signature": {k: estimate[k] for k in ("task_class", "risk", "model", "effort")},
                  "pool": lane["quota"]["pool"], "durations": lane["quota"]["windows"], "percent": percent,
                  "outcome": "reported-completion", "observed_at": now, "unit": "percent",
                  "measurement": "account-delta-upper-bound-not-exclusive-task-usage"}
        path = state.root / "quota-history" / (key(lane["quota"]["account_sha256"]) + ".json")
        history = admission.measured_history(state, lane["quota"]["account_sha256"])
        history["samples"] = (history["samples"] + [sample])[-128:]
        write_json(path, history)
        record["quota_measurement"] = sample
    except DriverError:
        record["quota_measurement"] = {"state": "unknown", "reason": "completion-observation-unavailable"}


def validate_record(record):
    decision = record.get("quota_decision")
    if decision is None:
        return
    if (not isinstance(decision, dict) or decision.get("schema") != "aru.quota-admission/v1"
            or not quota.number(decision.get("checkpoint_seconds"), 0, 300)
            or not isinstance(decision.get("author_families"), list)
            or not decision["author_families"] or any(not isinstance(f, str) or f not in quota.FAMILIES for f in decision["author_families"])
            or not isinstance(decision.get("reservations"), list) or not 1 <= len(decision["reservations"]) <= 2):
        raise DriverError("invalid quota worker receipt")
    for allocation in decision["reservations"]:
        if (not isinstance(allocation, dict) or allocation.get("role") not in {"worker", "review"}
                or not isinstance(allocation.get("account"), str) or len(allocation["account"]) != 64
                or not isinstance(allocation.get("pool"), str) or not isinstance(allocation.get("percent"), dict)
                or set(allocation["percent"]) != {"primary", "secondary"}
                or any(not quota.number(v, 0, 10000) for v in allocation["percent"].values())):
            raise DriverError("invalid quota reservation")
