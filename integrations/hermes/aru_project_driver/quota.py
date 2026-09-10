"""Versioned quota evidence and explicit opt-in policy; no inferred balances."""
from __future__ import annotations

import hashlib
import math
import re

from .config import DriverError

SCHEMA = "aru.quota/v1"
FAMILIES = {"openai-codex", "claude-code", "xai-cursor", "google-antigravity"}


def number(value, low=0, high=float("inf")):
    return type(value) in {int, float} and math.isfinite(value) and low <= value <= high


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def enabled(config, repo):
    return config.project(repo).get("quota_admission") is not None


def validate_config(config):
    accounts, keys = {}, {}
    for identity, lane in config.lanes.items():
        q = lane.get("quota")
        keys.setdefault(lane["capacity_key"], []).append(q)
        if q is None:
            continue
        validate_lane(lane, q)
        account = (lane["family"], q["account_sha256"])
        if account in accounts and accounts[account] != lane["capacity_key"]:
            raise DriverError("same quota account must share one capacity_key across pools and projects")
        accounts[account] = lane["capacity_key"]
    for values in keys.values():
        present = [q for q in values if q is not None]
        if present and (len(present) != len(values) or len({q["account_sha256"] for q in present}) != 1):
            raise DriverError("every shared-account alias must declare the same quota identity")
    for capacity_key in keys:
        lanes = [lane for lane in config.lanes.values() if lane["capacity_key"] == capacity_key and lane.get("quota")]
        if len({lane["family"] for lane in lanes}) > 1:
            raise DriverError("shared quota capacity cannot represent different providers")
        for lane in lanes:
            if any(other["quota"]["pool"] == lane["quota"]["pool"] and other["quota"]["windows"] != lane["quota"]["windows"] for other in lanes):
                raise DriverError("quota aliases disagree on applicable windows")
    for repo, project in config.projects.items():
        policy = project.get("quota_admission")
        if policy is None:
            continue
        validate_project(config, repo, project, policy)


def validate_lane(lane, q):
    if (not isinstance(q, dict) or set(q) != {"account_sha256", "pool", "model", "effort", "windows"}
            or lane["family"] not in FAMILIES
            or not isinstance(q["account_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", q["account_sha256"])
            or any(not isinstance(q[f], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", q[f])
                   for f in ("pool", "model", "effort"))):
        raise DriverError("quota lane requires a hashed account and exact pool/model/effort")
    windows = q["windows"]
    if (not isinstance(windows, dict) or set(windows) != {"primary", "secondary"}
            or any(type(v) is not int or not 1 <= v <= 525600 for v in windows.values())
            or windows["primary"] >= windows["secondary"]):
        raise DriverError("quota requires both short and long window durations in minutes")
    for argv in (lane["command"], lane["probe_command"]):
        if argv.count("--model") != 1 or argv[argv.index("--model") + 1:][:1] != [q["model"]]:
            raise DriverError("quota model must equal both worker and probe exact --model")
    if lane["family"] == "openai-codex" and "exec" not in lane["command"]:
        raise DriverError("Codex quota requires the native exec harness")
    if lane["command"][-1] != "{prompt}":
        raise DriverError("quota harness requires a final literal prompt argument")
    if q["effort"] != "default":
        argv = lane["command"]
        exact = f'model_reasoning_effort="{q["effort"]}"'
        if lane["family"] == "openai-codex":
            values = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] in {"-c", "--config"}
                      and argv[i + 1].split("=", 1)[0].strip() == "model_reasoning_effort"]
            matched = values == [exact]
        else:
            matched = argv.count("--effort") == 1 and argv[argv.index("--effort") + 1:][:1] == [q["effort"]]
        if not matched:
            raise DriverError("quota effort must match an explicit native worker setting or default")


def validate_project(config, repo, project, policy):
    required = {"version", "max_age_seconds", "headroom_percent", "unknown_checkpoint_seconds",
                "max_recoveries", "cooldown_seconds", "author_actor", "cold_start"}
    optional = {"unknown_review_seconds", "unknown_max_attempts", "unknown_total_seconds", "review_escrow_seconds"}
    if (not isinstance(policy, dict) or not required <= set(policy) or set(policy) - required - optional or type(policy["version"]) is not int
            or policy["version"] != 1 or not isinstance(policy["author_actor"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[bot\])?", policy["author_actor"])):
        raise DriverError("quota_admission requires explicit v1 policy and author actor")
    for field, low, high in (("max_age_seconds", 1, 300), ("headroom_percent", 1, 99),
                             ("unknown_checkpoint_seconds", 0, 86400), ("max_recoveries", 0, 3),
                             ("cooldown_seconds", 60, 3600), ("unknown_review_seconds", 0, 86400),
                             ("unknown_max_attempts", 1, 32), ("unknown_total_seconds", 1, 86400),
                             ("review_escrow_seconds", 60, 3600)):
        if field not in policy:
            continue
        if not number(policy[field], low, high):
            raise DriverError("quota policy bound invalid: " + field)
        if field != "headroom_percent" and type(policy[field]) is not int:
            raise DriverError("quota policy time/count must be an integer: " + field)
    if any(config.lane(repo, i).get("quota") is None for i in project["lanes"]):
        raise DriverError("quota opt-in requires evidence configuration for every authorized lane")
    rows = policy["cold_start"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise DriverError("quota cold_start requires bounded explicit demand rows")
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"task_class", "risk", "model", "effort", "percent"}
                or row["task_class"] not in {"implementation", "remediation", "review", "checkpoint"}
                or type(row["risk"]) is not int or row["risk"] not in range(4)
                or not isinstance(row["model"], str) or not isinstance(row["effort"], str)
                or not isinstance(row["percent"], dict) or set(row["percent"]) != {"primary", "secondary"}
                or any(not number(v, 1, 100) for v in row["percent"].values())):
            raise DriverError("quota cold_start row invalid; quota percentages only")
        signature = tuple(row[f] for f in ("task_class", "risk", "model", "effort"))
        if signature in seen:
            raise DriverError("duplicate quota demand row")
        seen.add(signature)


def unknown(lane, now, reason, *, unavailable=False):
    q = lane["quota"]
    return {"schema": SCHEMA, "provider": lane["family"], "account_sha256": q["account_sha256"],
            "capacity_key": lane["capacity_key"], "model": q["model"], "pool": q["pool"],
            "observed_at": now, "source": "supported-cli", "confidence": "unknown",
            "state": "unavailable" if unavailable else "unknown", "reason": reason,
            "windows": {}, "identity_verified": False}


def validate(observation, lane, now, max_age):
    q = lane["quota"]
    expected = {"schema": SCHEMA, "provider": lane["family"], "account_sha256": q["account_sha256"],
                "capacity_key": lane["capacity_key"], "model": q["model"], "pool": q["pool"]}
    allowed = set(expected) | {"observed_at", "source", "confidence", "state", "reason", "windows", "identity_verified"}
    if (not isinstance(observation, dict) or set(observation) != allowed
            or any(observation.get(k) != v for k, v in expected.items())
            or not number(observation["observed_at"], max(0, now - max_age), now)
            or observation["source"] not in {"supported-cli", "codex-app-server"}
            or observation["confidence"] not in {"provider", "unknown"}
            or observation["state"] not in {"known", "unknown", "unavailable", "exhausted"}
            or type(observation["identity_verified"]) is not bool
            or not isinstance(observation["reason"], str)
            or not re.fullmatch(r"[a-z0-9-]{1,96}", observation["reason"])):
        raise DriverError("invalid quota evidence: identity, freshness or envelope")
    windows = observation["windows"]
    if not isinstance(windows, dict) or set(windows) - set(q["windows"]):
        raise DriverError("invalid quota windows")
    for name, w in windows.items():
        if (not isinstance(w, dict) or set(w) != {"unit", "remaining", "reset_at", "duration_minutes"}
                or w["unit"] != "percent" or not number(w["remaining"], 0, 100)
                or not number(w["reset_at"], now + .001, observation["observed_at"] + q["windows"][name] * 60 + 1)
                or type(w["duration_minutes"]) is not int or w["duration_minutes"] != q["windows"][name]):
            raise DriverError("invalid quota evidence: window units, allowance or reset")
    state = observation["state"]
    if state in {"known", "exhausted"}:
        if not observation["identity_verified"] or observation["confidence"] != "provider":
            raise DriverError("quota balance has no verified provider identity")
        if state == "known" and (set(windows) != set(q["windows"]) or any(w["remaining"] == 0 for w in windows.values())):
            raise DriverError("contradictory quota balance or missing windows")
        if state == "exhausted" and observation["reason"] != "provider-blocked" and not any(w["remaining"] == 0 for w in windows.values()):
            raise DriverError("quota exhaustion has no provider evidence")
    elif observation["confidence"] != "unknown":
        raise DriverError("unknown quota must declare uncertainty")
    if state in {"unknown", "unavailable"} and any(w["remaining"] == 0 for w in windows.values()):
        raise DriverError("unknown quota contradicts an exhausted window")
    return observation
