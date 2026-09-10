"""Synthetic contract/demand evidence; genuine probes are outside the repository."""
from copy import deepcopy
import json
import time

import pytest

from test_controller import Harness, REPO, issue
from aru_project_driver import quota, quota_admission as admission, quota_collect
from aru_project_driver.config import Config, DriverError
from aru_project_driver.state import key, read_json, write_json


def observation(lane, remaining=90, state="known"):
    data = quota.unknown(lane, time.time(), "fixture-unknown")
    if state == "unknown":
        return data
    data.update(state=state, confidence="provider", identity_verified=True, source="codex-app-server",
                reason="provider-observed" if state == "known" else "window-exhausted")
    data["windows"] = {name: {"unit": "percent", "remaining": remaining, "reset_at": time.time() + mins * 60,
                             "duration_minutes": mins} for name, mins in lane["quota"]["windows"].items()}
    return data


@pytest.fixture
def qh(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    for identity, lane in h.config.lanes.items():
        lane["quota"] = {"account_sha256": quota.digest(identity), "pool": "codex", "model": identity + "-model",
                         "effort": "default", "windows": {"primary": 300, "secondary": 10080}}
        lane["command"] = ["/synthetic/cli", "exec", "--model", lane["quota"]["model"], "{prompt}"]
        lane["probe_command"] = ["/synthetic/cli", "--model", lane["quota"]["model"], "OK"]
    policy = {"version": 1, "max_age_seconds": 120, "headroom_percent": 10, "unknown_checkpoint_seconds": 0,
              "max_recoveries": 2, "cooldown_seconds": 600, "author_actor": "test-author", "cold_start": []}
    for lane in h.config.lanes.values():
        for kind in ("implementation", "remediation", "review", "checkpoint"):
            for risk in (0, 3):
                policy["cold_start"].append({"task_class": kind, "risk": risk, "model": lane["quota"]["model"],
                    "effort": "default", "percent": {"primary": 10 if kind == "review" else 30,
                                                       "secondary": 10 if kind == "review" else 30}})
    h.config.project(REPO)["quota_admission"] = policy
    quota.validate_config(h.config)
    h.kernel.reviewer_status = lambda: {"schema": "aru.reviewer-status/v3", "valid": True, "coding_reviewers": [
        {"identity": i, "family": lane["family"], "reviewer_actor": i + "-actor", "eligible": True}
        for i, lane in h.config.lanes.items()]}
    h.kernel.issue_summary = lambda n: h.kernel.record(n)
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i)))
    yield h
    h.close()


@pytest.mark.parametrize("field,value", [("account_sha256", "f" * 64), ("provider", "wrong"), ("model", "other"),
    ("pool", "wrong"), ("capacity_key", "alias"), ("observed_at", -1), ("observed_at", float("nan")),
    ("observed_at", float("inf")), ("observed_at", time.time() + 10000), ("confidence", "guess"),
    ("identity_verified", False), ("reason", "secret=abc")])
def test_observation_refuses_identity_time_and_confidence(qh, field, value):
    lane = qh.config.lane(REPO, "codex-one")
    data = observation(lane)
    data[field] = value
    with pytest.raises(DriverError):
        quota.validate(data, lane, time.time(), 120)


@pytest.mark.parametrize("field,value", [("remaining", -1), ("remaining", 101), ("remaining", True),
    ("remaining", float("nan")), ("remaining", float("inf")), ("unit", "tokens"), ("unit", "usd"),
    ("reset_at", 0), ("duration_minutes", 10080), ("duration_minutes", True)])
def test_windows_refuse_bad_numbers_units_and_resets(qh, field, value):
    lane = qh.config.lane(REPO, "codex-one")
    data = observation(lane)
    data["windows"]["primary"][field] = value
    with pytest.raises(DriverError):
        quota.validate(data, lane, time.time(), 120)


def test_missing_stale_exhausted_and_extra_evidence(qh):
    lane = qh.config.lane(REPO, "codex-one")
    for mutate in (lambda d: d["windows"].pop("secondary"), lambda d: d.update(observed_at=time.time() - 121),
                   lambda d: d.update(state="exhausted"), lambda d: d.update(credentials="never-allowed")):
        data = observation(lane)
        mutate(data)
        with pytest.raises(DriverError):
            quota.validate(data, lane, time.time(), 120)
    assert quota.validate(observation(lane, 0, "exhausted"), lane, time.time(), 120)["state"] == "exhausted"


def test_codex_duration_mapping_and_missing_short_window(qh):
    lane = qh.config.lane(REPO, "codex-one")
    lane["quota"]["account_sha256"] = quota.digest("fixture-account")
    pool = {"limitId": "codex", "primary": {"usedPercent": 30, "windowDurationMins": 10080, "resetsAt": int(time.time()) + 10000}}
    limits = {"accountId": "fixture-account", "ordinaryUsageAllowed": True, "rateLimits": pool,
              "rateLimitsByLimitId": {"codex": pool}}
    data = quota_collect.normalize(lane, {"account": {"type": "chatgpt"}}, limits, time.time())
    assert data["state"] == "unknown" and set(data["windows"]) == {"secondary"}
    assert quota.validate(data, lane, time.time(), 120)
    limits["accountId"] = "wrong-account"
    with pytest.raises(DriverError, match="account-mismatch"):
        quota_collect.normalize(lane, {"account": {"type": "chatgpt"}}, limits, time.time())


def test_known_demand_reserves_independent_review_and_reports_uncertainty(qh):
    decision = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    assert decision["demand"]["confidence"] == "cold-start" and decision["demand"]["risk"] == 3
    assert decision["demand"]["samples"] == 0 and len(decision["reservations"]) == 2
    assert decision["review_budget"]["identity"] == "claude-one"
    assert decision["margin"] == 50


@pytest.mark.parametrize("risk", [0, 1])
def test_low_risk_author_does_not_need_or_reserve_a_reviewer(qh, risk):
    qh.kernel.reviewer_status = lambda: pytest.fail("low-risk work must not require reviewer inventory")
    task = {**issue(1), "quota_risk": risk}
    decision = admission.evaluate(qh.config, qh.state, REPO, "codex-one", task, "implementation", qh.kernel)
    assert [r["role"] for r in decision["reservations"]] == ["worker"]
    assert "review_budget" not in decision


@pytest.mark.parametrize("risk", [2, 3, None, "0", True, -1, 4])
def test_sensitive_or_unknown_risk_still_requires_independent_review(qh, risk):
    qh.kernel.reviewer_status = lambda: {"schema": "aru.reviewer-status/v3", "valid": True, "coding_reviewers": []}
    with pytest.raises(DriverError, match="no eligible independent reviewer"):
        admission.evaluate(qh.config, qh.state, REPO, "codex-one", {**issue(1), "quota_risk": risk}, "implementation", qh.kernel)


def test_consumer_can_explicitly_reserve_review_for_low_risk_tasks(qh):
    qh.config.project(REPO)["quota_admission"]["reserve_review_for_all_tasks"] = True
    quota.validate_config(qh.config)
    result = admission.evaluate(qh.config, qh.state, REPO, "codex-one", {**issue(1), "quota_risk": 0}, "implementation", qh.kernel)
    assert result["review_budget"]["identity"] == "claude-one"
    qh.config.project(REPO)["quota_admission"]["reserve_review_for_all_tasks"] = "false"
    with pytest.raises(DriverError, match="reserve_review_for_all_tasks"):
        quota.validate_config(qh.config)


@pytest.mark.parametrize("case", ["short", "long", "stale", "no-reviewer", "same-family", "same-actor", "invalid", "unknown", "missing-demand"])
def test_admission_refusals(qh, monkeypatch, case):
    def collect(c, r, i):
        data = observation(c.lane(r, i))
        if case in {"short", "long"}:
            data["windows"]["primary" if case == "short" else "secondary"]["remaining"] = 39
        if case == "stale":
            data["observed_at"] -= 121
        if case == "invalid":
            data["windows"]["primary"]["remaining"] = float("nan")
        if case == "unknown":
            return observation(c.lane(r, i), state="unknown")
        return data
    monkeypatch.setattr(quota_collect, "collect", collect)
    if case == "no-reviewer":
        qh.kernel.reviewer_status = lambda: {"valid": False}
    if case == "same-family":
        qh.config.lanes["claude-one"]["family"] = "openai-codex"
    if case == "same-actor":
        qh.config.project(REPO)["quota_admission"]["author_actor"] = "claude-one-actor"
    if case == "missing-demand":
        qh.config.project(REPO)["quota_admission"]["cold_start"] = []
    with pytest.raises(DriverError):
        admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)


def test_unknown_is_only_bounded_checkpoint_and_invalid_never_is(qh, monkeypatch):
    qh.config.project(REPO)["quota_admission"]["unknown_checkpoint_seconds"] = 60
    qh.config.project(REPO)["quota_admission"]["unknown_review_seconds"] = 60
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), state="unknown"))
    decision = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    assert decision["checkpoint_seconds"] == 60 and decision["demand"]["confidence"] == "unknown"
    monkeypatch.setattr(quota_collect, "collect", lambda *args: {"state": "unknown"})
    with pytest.raises(DriverError, match="invalid quota"):
        admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)


def test_measured_history_is_comparable_percent_upper_bound(qh):
    lane = qh.config.lanes["codex-one"]
    lane["quota"]["effort"] = "high"
    for row in qh.config.project(REPO)["quota_admission"]["cold_start"]:
        if row["model"] == lane["quota"]["model"]:
            row["effort"] = "high"
    sample = {"signature": {"task_class": "implementation", "risk": 3, "model": lane["quota"]["model"], "effort": "high"},
              "pool": "codex", "durations": lane["quota"]["windows"], "percent": {"primary": 40, "secondary": 35}, "unit": "percent"}
    path = qh.state.root / "quota-history" / (key(lane["quota"]["account_sha256"]) + ".json")
    write_json(path, {"samples": [sample, {**sample, "signature": {**sample["signature"], "effort": "low"}}]})
    estimate = admission.demand(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation")
    assert estimate["samples"] == 1 and estimate["percent"]["primary"] == 50
    sample["unit"] = "tokens"
    write_json(path, {"samples": [sample]})
    with pytest.raises(DriverError, match="history invalid"):
        admission.demand(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation")


@pytest.mark.parametrize("case", ["account-alias", "unmapped-alias", "model", "effort", "windows", "headroom", "checkpoint", "duplicate-row"])
def test_config_refuses_unsafe_mapping_and_policy(qh, case):
    lane = qh.config.lanes["codex-one"]
    policy = qh.config.project(REPO)["quota_admission"]
    if case in {"account-alias", "unmapped-alias"}:
        qh.config.lanes["alias"] = deepcopy(lane)
        if case == "account-alias":
            qh.config.lanes["alias"]["capacity_key"] = "another-key"
        else:
            qh.config.lanes["alias"].pop("quota")
    elif case in {"model", "effort"}:
        lane["quota"][case] = "wrong"
    elif case == "windows":
        lane["quota"]["windows"].pop("secondary")
    elif case == "headroom":
        policy["headroom_percent"] = float("nan")
    elif case == "checkpoint":
        policy["unknown_checkpoint_seconds"] = 86401
    else:
        policy["cold_start"].append(policy["cold_start"][0])
    qh.config.path.write_text(json.dumps(qh.config.raw))
    with pytest.raises(DriverError):
        Config(qh.config.path)


def test_cooldown_has_real_reset_only_from_provider(qh, monkeypatch):
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 0, "exhausted"))
    with pytest.raises(DriverError, match="exhausted"):
        admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    cooldown = read_json(qh.state.root / "cooldowns" / (key(qh.config.lanes["codex-one"]["capacity_key"]) + ".json"))
    assert cooldown["reset_at"] == cooldown["until"] and cooldown["until"] > time.time() + 10000
