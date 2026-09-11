"""Demand and reservations under the existing Driver coordination lock."""
from __future__ import annotations

import time

from . import quota, quota_collect, quota_checkpoint
from .config import DriverError
from .state import key, read_json

def lineage(config, state, repo, issue, identity):
    families = {config.lane(repo, identity)["family"]}
    for r in state.workers(repo):
        if r["issue"] == issue:
            lane = config.lanes.get(r["agent"])
            if lane is None:
                raise DriverError("quota author lineage has an unconfigured historical identity")
            families.add(lane["family"])
            families.update(r.get("quota_decision", {}).get("author_families", []))
    return sorted(families)

def demand(config, state, repo, identity, task, kind):
    q, policy = config.lane(repo, identity)["quota"], config.project(repo)["quota_admission"]
    # Only canonical tier-0 scope gets the documentation estimate. Every other
    # tier, or missing evidence, uses the conservative tier-3 demand bucket.
    risk = 0 if type(task.get("quota_risk")) is int and task["quota_risk"] == 0 else 3
    signature = {"task_class": kind, "risk": risk, "model": q["model"], "effort": q["effort"]}
    rows = [r for r in policy["cold_start"] if all(r[k] == v for k, v in signature.items())]
    if len(rows) != 1:
        raise DriverError("quota has no comparable cold-start demand for task/model/effort/risk")
    history = measured_history(state, q["account_sha256"])
    samples = [s for s in history["samples"] if s["signature"] == signature and s["pool"] == q["pool"]
               and s["durations"] == q["windows"] and q["effort"] != "default"][-32:]
    percent = {n: max([v] + [s["percent"][n] * 1.25 for s in samples]) for n, v in rows[0]["percent"].items()}
    return {**signature, "percent": percent, "unit": "percent", "samples": len(samples),
            "confidence": "conservative-observed" if samples else "cold-start",
            "uncertainty": "account-delta-upper-bound-includes-external-use" if samples else "no-comparable-measurements"}

def measured_history(state, account):
    history = read_json(state.root / "quota-history" / (key(account) + ".json"), {"samples": []})
    if (not isinstance(history.get("samples"), list) or len(history["samples"]) > 128
            or any(not isinstance(s, dict) or not isinstance(s.get("signature"), dict)
                   or not isinstance(s.get("durations"), dict) or not isinstance(s.get("pool"), str)
                   or s.get("unit") != "percent" or not isinstance(s.get("percent"), dict)
                   or set(s["percent"]) != {"primary", "secondary"}
                   or any(not quota.number(v, 0, 100) for v in s["percent"].values()) for s in history["samples"])):
        raise DriverError("quota measured history invalid")
    return history

def reservations(config, state, account, pool, exclude=None):
    result = {"primary": 0, "secondary": 0}
    for r in state.workers():
        if r["id"] == exclude:
            continue
        lane = config.lanes.get(r["agent"], {})
        live = r["id"] in state.capacity_holders(r["capacity_key"], lane.get("max_sessions", 1))
        decision = r.get("quota_decision", {})
        if live and not decision and lane.get("quota", {}).get("account_sha256") == account:
            raise DriverError("quota shared account has an unmeasured legacy reservation")
        for allocation in decision.get("reservations", []):
            # Historical review allocations never reserve current capacity.
            held = live and allocation["role"] == "worker"
            if held and allocation["account"] == account and allocation["pool"] != pool:
                raise DriverError("quota cross-pool reservation is not comparable; wait for the shared account")
            if held and allocation["account"] == account and allocation["pool"] == pool:
                for name, value in allocation["percent"].items():
                    result[name] += value
    return result

def capacity(config, state, repo, identity, estimate, *, exclude=None, unknown_seconds=0):
    lane, now = config.lane(repo, identity), time.time()
    q, policy = lane["quota"], config.project(repo)["quota_admission"]
    cooldown = read_json(state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json"), {"until": 0})
    if not quota.number(cooldown.get("until")):
        raise DriverError("quota cooldown evidence invalid")
    if cooldown["until"] > now:
        raise DriverError("quota account cooldown; heartbeat owns bounded retry")
    obs = quota.validate(quota_collect.collect(config, repo, identity), lane, now=time.time(), max_age=policy["max_age_seconds"])
    held = reservations(config, state, q["account_sha256"], q["pool"], exclude)
    if obs["state"] == "exhausted":
        resets = [w["reset_at"] for w in obs["windows"].values() if w["remaining"] == 0]
        quota_checkpoint.cooldown(state, lane, max(resets) if resets else now + policy["cooldown_seconds"],
                                  max(resets) if resets else None)
        raise DriverError("quota exhausted; shared-account cooldown recorded")
    checkpoint = obs["state"] in {"unknown", "unavailable"}
    if checkpoint and not unknown_seconds:
        raise DriverError("quota unknown; only explicit bounded checkpoint work is eligible")
    margins = []
    for name, window in obs["windows"].items():
        margin = window["remaining"] - held[name] - estimate["percent"][name] - policy["headroom_percent"]
        if margin < 0:
            raise DriverError("quota insufficient in " + name + " window including reservations/headroom")
        margins.append(margin)
    if checkpoint and any(held.values()):
        raise DriverError("quota unknown account already has a reservation")
    return obs, min(margins, default=0), checkpoint

def evaluate(config, state, repo, identity, task, kind, adapter, *, exclude=None):
    if not quota.enabled(config, repo):
        return None
    lane, policy = config.lane(repo, identity), config.project(repo)["quota_admission"]
    families = lineage(config, state, repo, task["number"], identity)
    estimate = demand(config, state, repo, identity, task, kind)
    seconds = policy["unknown_checkpoint_seconds"]
    obs, margin, checkpoint = capacity(config, state, repo, identity, estimate, exclude=exclude,
        unknown_seconds=seconds)
    if checkpoint:
        seconds = quota_checkpoint.allowance(config, state, repo, identity, task["number"], exclude)
        estimate = {**estimate, "confidence": "unknown", "uncertainty": "operator-bounded-risk-acceptance-not-measured-headroom"}
    q = lane["quota"]
    allocation = {"role": "worker", "account": q["account_sha256"], "pool": q["pool"], "percent": estimate["percent"]}
    return {"schema": "aru.quota-admission/v1", "observation": obs, "demand": estimate,
              "margin": margin, "checkpoint_seconds": seconds if checkpoint else 0,
              "reservations": [allocation], "author_families": families,
              "continuation_owner": "Hermes Driver completion/heartbeat", "selected": identity,
              "policy_hash": quota.digest(str(sorted(policy.items())))}
