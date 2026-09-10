"""Demand and reservations under the existing Driver coordination lock."""
from __future__ import annotations

import time

from . import quota, quota_collect
from .config import DriverError
from .state import key, read_json, write_json


def lineage(config, state, repo, issue, identity):
    families = {config.lane(repo, identity)["family"]}
    for r in state.workers(repo):
        if r["issue"] == issue and r.get("kind") != "review":
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


def reservations(config, state, account, pool, exclude=None, consume_review=None):
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
            held = live if allocation["role"] == "worker" else not r.get("quota_review_released", False)
            if allocation["role"] == "review" and (r["repo"], r["issue"]) == consume_review:
                held = False
            if held and allocation["account"] == account and allocation["pool"] != pool:
                raise DriverError("quota cross-pool reservation is not comparable; wait for the shared account")
            if held and allocation["account"] == account and allocation["pool"] == pool:
                for name, value in allocation["percent"].items():
                    result[name] += value
    return result


def capacity(config, state, repo, identity, estimate, *, exclude=None, consume_review=None):
    lane, now = config.lane(repo, identity), time.time()
    q, policy = lane["quota"], config.project(repo)["quota_admission"]
    cooldown = read_json(state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json"), {"until": 0})
    if not quota.number(cooldown.get("until")):
        raise DriverError("quota cooldown evidence invalid")
    if cooldown["until"] > now:
        raise DriverError("quota account cooldown; heartbeat owns bounded retry")
    obs = quota.validate(quota_collect.collect(config, repo, identity), lane, now=time.time(), max_age=policy["max_age_seconds"])
    held = reservations(config, state, q["account_sha256"], q["pool"], exclude, consume_review)
    if obs["state"] == "exhausted":
        resets = [w["reset_at"] for w in obs["windows"].values() if w["remaining"] == 0]
        write_json(state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json"), {
            "until": max(resets) if resets else now + policy["cooldown_seconds"],
            "reset_at": max(resets) if resets else None, "reason": "quota-exhausted", "pool": q["pool"]})
        raise DriverError("quota exhausted; shared-account cooldown recorded")
    checkpoint = obs["state"] in {"unknown", "unavailable"}
    if checkpoint and (not policy["unknown_checkpoint_seconds"] or estimate["task_class"] != "checkpoint"):
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


def evaluate(config, state, repo, identity, task, kind, adapter, *, exclude=None, review=None):
    if not quota.enabled(config, repo):
        return None
    lane, policy = config.lane(repo, identity), config.project(repo)["quota_admission"]
    families = lineage(config, state, repo, task["number"], review["author"] if review else identity)
    if review:
        if not isinstance(review.get("author_family"), str) or review["author_family"] not in quota.FAMILIES:
            raise DriverError("quota canonical author family is unproven")
        families = sorted(set(families) | {review["author_family"]})
    if review and (lane["family"] in families or review["author_actor"].lower() == review["reviewer_actor"].lower()):
        raise DriverError("quota reviewer is not independent of cumulative author family/actor")
    estimate = demand(config, state, repo, identity, task, kind)
    try:
        obs, margin, checkpoint = capacity(config, state, repo, identity, estimate, exclude=exclude,
                                           consume_review=(repo, task["number"]))
    except DriverError as exc:
        if "quota unknown;" not in str(exc) or not policy["unknown_checkpoint_seconds"] or review:
            raise
        estimate = demand(config, state, repo, identity, task, "checkpoint")
        obs, margin, checkpoint = capacity(config, state, repo, identity, estimate, exclude=exclude)
    q = lane["quota"]
    allocation = {"role": "worker", "account": q["account_sha256"], "pool": q["pool"], "percent": estimate["percent"]}
    result = {"schema": "aru.quota-admission/v1", "observation": obs, "demand": estimate,
              "margin": margin, "checkpoint_seconds": policy["unknown_checkpoint_seconds"] if checkpoint else 0,
              "reservations": [allocation], "author_families": families,
              "continuation_owner": "Hermes Driver completion/heartbeat", "selected": identity,
              "review_candidates": [], "policy_hash": quota.digest(str(sorted(policy.items())))}
    if review:
        return result
    return review_budget(config, state, repo, task, adapter, result, exclude)


def review_budget(config, state, repo, task, adapter, result, exclude):
    policy = config.project(repo)["quota_admission"]
    families, checkpoint = result["author_families"], result["checkpoint_seconds"]
    status = adapter.reviewer_status()
    if status.get("schema") != "aru.reviewer-status/v3" or status.get("valid") is not True:
        raise DriverError("quota independent reviewer eligibility is unproven")
    candidates = status.get("coding_reviewers")
    if (not isinstance(candidates, list) or len(candidates) > 128 or any(
        not isinstance(c, dict) or not isinstance(c.get("identity"), str)
        or not isinstance(c.get("family"), str) or c["family"] not in quota.FAMILIES
        or not isinstance(c.get("reviewer_actor"), str) or not c["reviewer_actor"]
        or type(c.get("eligible")) is not bool for c in candidates
    )):
        raise DriverError("quota independent reviewer eligibility invalid")
    options = []
    snapshot = adapter.snapshot()
    author_actors = {policy["author_actor"].lower()} | {p["author_actor"].lower() for p in snapshot["prs"]
                    if task["number"] in p.get("issues", []) and isinstance(p.get("author_actor"), str)}
    owners = {a for item in snapshot["issues"] if item["status"] in {"In Progress", "In Review"} for a in item["agents"]}
    for candidate in candidates:
        other = candidate.get("identity")
        reason = "independent-review-eligibility"
        if (candidate.get("eligible") is True and candidate.get("family") not in families
                and candidate.get("reviewer_actor", "").lower() not in author_actors
                and other in config.project(repo)["lanes"] and other not in owners):
            try:
                other_lane = config.lane(repo, other)
                if other_lane["family"] != candidate["family"] or state.capacity_busy(other_lane["capacity_key"], other_lane.get("max_sessions", 1)):
                    raise DriverError("review account unavailable")
                review_demand = demand(config, state, repo, other, task, "review")
                if checkpoint:
                    result["review_budget"] = {"identity": other, "demand": review_demand,
                                               "state": "unknown-full-task-forbidden", "authority": "eligibility-only"}
                    return result
                review_obs, review_margin, _ = capacity(config, state, repo, other, review_demand,
                    exclude=exclude, consume_review=(repo, task["number"]))
                options.append((review_margin, other, other_lane, review_demand, review_obs))
                reason = "eligible-budget"
            except DriverError as exc:
                reason = str(exc)
        result["review_candidates"].append({"identity": other, "reason": reason})
    if not options:
        raise DriverError("quota no eligible independent reviewer with known budget")
    _, other, other_lane, review_demand, review_obs = max(options, key=lambda x: (x[0], x[1]))
    result["review_budget"] = {"identity": other, "observation": review_obs, "demand": review_demand,
                               "authority": "budget-only-canonical-helper-selects-reviewer"}
    result["reservations"].append({"role": "review", "account": other_lane["quota"]["account_sha256"],
                                   "pool": other_lane["quota"]["pool"], "percent": review_demand["percent"]})
    return result


def release_review(state, repo, issue):
    for receipt in state.workers(repo):
        if receipt["issue"] == issue and receipt.get("quota_decision"):
            receipt["quota_review_released"] = True
            write_json(state.worker_path(receipt["id"]), receipt)
