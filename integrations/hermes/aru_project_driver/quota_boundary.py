"""Quota wiring for the existing controller; GitHub helpers remain authority."""
from __future__ import annotations

import time
import uuid
import re
from pathlib import Path

from . import permissions, quota, quota_admission as admission
from .config import DriverError
from .state import key, read_json, write_json


def approved(controller, repo, adapter, identity, task, kind):
    return not quota.enabled(controller.config, repo) or bool(decide(controller, repo, adapter, identity, task, kind))


def recover_results(controller, repo):
    """Migrate only a retained, validated native result; never parse receipt prose."""
    if not quota.enabled(controller.config, repo):
        return
    for record in controller.state.workers(repo):
        if (record.get("outcome") != "worker_error" or record["state"] != "exited"
                or record.get("permission_snapshot", {}).get("result_format") != "claude-json"
                or not re.fullmatch(r"[a-f0-9]{32}", record["id"])
                or not quota.number(record.get("finished_at"), 0, time.time()) or not record.get("child_pid")
                or controller._holds_reservation(record)):
            continue
        lane = controller.config.lane(repo, record["agent"])
        expected = controller.state.root / "logs" / (record["id"] + ".result.json")
        if (lane["family"] != "claude-code" or lane["capacity_key"] != record["capacity_key"]
                or not isinstance(record.get("result_path"), str)
                or Path(record["result_path"]).resolve() != expected or expected.is_symlink()):
            continue
        observed = permissions.observe_result(expected, record.get("exit_code", 1), quota_errors=True)
        if observed["outcome"] != "quota_exhausted":
            continue
        record.update(observed, quota_reclassified_from="worker_error")
        until = record["finished_at"] + controller.config.project(repo)["quota_admission"]["cooldown_seconds"]
        path = controller.state.root / "cooldowns" / (key(record["capacity_key"]) + ".json")
        cooldown = read_json(path, {"until": 0})
        if not quota.number(cooldown.get("until")):
            raise DriverError("quota cooldown evidence invalid")
        if until > cooldown["until"]:
            write_json(path, {"until": until, "reset_at": None, "reason": "quota-exhausted",
                              "pool": lane["quota"]["pool"], "continuation_owner": record["id"]})
        write_json(controller.state.worker_path(record["id"]), record)


def decide(controller, repo, adapter, identity, task, kind="implementation", review=None):
    if not quota.enabled(controller.config, repo):
        return None
    try:
        if any(r["issue"] == task["number"] and r.get("quota_transfer_attempted") and not r.get("quota_transfer_completed")
               for r in controller.state.workers(repo)):
            raise DriverError("quota claim transition interrupted; canonical owner reconciliation required")
        decision = admission.evaluate(controller.config, controller.state, repo, identity, task, kind, adapter, review=review)
        reason = "bounded-checkpoint" if decision["checkpoint_seconds"] else "sufficient-observed-budget"
        accepted = True
    except DriverError as exc:
        decision, accepted, reason = None, False, str(exc)
    path = controller.state.root / "quota-decisions" / (key(repo) + ".json")
    data = read_json(path, {"decisions": []})
    data["decisions"] = (data["decisions"] + [{"candidate": identity, "issue": task["number"],
        "accepted": accepted, "reason": reason, "observed_at": time.time(), "decision": decision,
        "continuation_owner": "Hermes Driver completion/heartbeat"}])[-32:]
    write_json(path, data)
    return decision


def ranked(controller, repo, adapter, identities):
    if not quota.enabled(controller.config, repo):
        return identities
    candidates = adapter.candidates(adapter.snapshot(), status="Ready")
    if not candidates and controller.config.project(repo).get("auto_triage"):
        candidates = adapter.candidates(adapter.snapshot(), status="Backlog")
    if not candidates:
        return identities
    scored = []
    for identity in identities:
        decision = decide(controller, repo, adapter, identity, candidates[0])
        if decision:
            scored.append((not bool(decision["checkpoint_seconds"]), decision["margin"], identity))
    return [i for _, _, i in sorted(scored, reverse=True)]


def resume(controller, repo, work, receipt, available):
    if not quota.enabled(controller.config, repo) or receipt.get("outcome") not in {"quota_exhausted", "quota_checkpoint"}:
        return None
    policy, config = controller.config.project(repo)["quota_admission"], controller.config
    attempts = sum(r.get("outcome") == "quota_exhausted" for r in controller.state.workers(repo)
                   if r["issue"] == work["issue"] and r.get("kind") != "review")
    if attempts > policy["max_recoveries"]:
        raise DriverError("quota recovery bound reached; owner must inspect retained checkpoint")
    if receipt.get("quota_transfer_attempted") and not receipt.get("quota_transfer_completed"):
        raise DriverError("quota claim transition interrupted; canonical owner reconciliation required")
    if any(controller._holds_reservation(r) for r in controller.state.workers(repo) if r["issue"] == work["issue"]):
        raise DriverError("quota recovery excluded by live owner")
    lane = config.lane(repo, work["agent"])
    if work["agent"] in available:
        return {**work, "worktree": receipt.get("worktree")}
    # A family change would require kernel-persistent cumulative authorship.
    # Existing canonical authority has one family; retain it during automatic recovery.
    for identity in available:
        other = config.lane(repo, identity)
        if (other["family"] == lane["family"] and other["capacity_key"] != lane["capacity_key"]
                and all(other["quota"][f] == lane["quota"][f] for f in ("model", "effort"))):
            if work.get("pr"):
                raise DriverError("quota fallback cannot transfer an authored PR claim; preserve owner until reset")
            task = controller.adapter(repo).revalidate(work["issue"], agent=work["agent"])
            if decide(controller, repo, controller.adapter(repo), identity, task, "remediation"):
                return {**work, "quota_transfer": identity, "worktree": receipt.get("worktree")}
    raise DriverError("quota all eligible accounts exhausted or unknown; heartbeat owns cooldown recovery")


def transfer(controller, repo, adapter, work):
    identity = work.get("quota_transfer")
    if not identity:
        return work
    if not controller.state.project(repo)["enabled"]:
        raise DriverError("project stopped before quota claim transition")
    if any(controller._holds_reservation(r) for r in controller.state.workers(repo) if r["issue"] == work["issue"]):
        raise DriverError("quota transfer excluded by live owner")
    adapter.revalidate(work["issue"], agent=work["agent"])
    # Canonical release refuses an open PR and unsafe lifecycle state. No labels
    # are hand-edited, and no worktree/partial diff is moved or removed.
    receipt = max((r for r in controller.state.workers(repo) if r["issue"] == work["issue"]), key=lambda r: r["started_at"])
    if receipt.get("quota_transfer_attempted"):
        raise DriverError("quota claim transition already attempted; canonical owner reconciliation required")
    receipt.update(quota_transfer_attempted=True, quota_transfer_target=identity)
    write_json(controller.state.worker_path(receipt["id"]), receipt)
    adapter.release(work["issue"], work["agent"])
    if not controller.state.project(repo)["enabled"]:
        raise DriverError("project stopped after canonical release; preserved checkpoint needs owner reconciliation")
    intent = {"id": uuid.uuid4().hex, "repo": repo, "agent": identity, "issue": work["issue"],
              "capacity_key": controller.config.lane(repo, identity)["capacity_key"], "started_at": time.time(),
              "state": "claiming", "worktree": None, "quota_previous_agent": work["agent"]}
    write_json(controller.state.worker_path(intent["id"]), intent)
    adapter.claim(work["issue"], identity)
    intent.update(state="prepared", worktree=receipt.get("worktree"))
    write_json(controller.state.worker_path(intent["id"]), intent)
    receipt["quota_transfer_completed"] = True
    write_json(controller.state.worker_path(receipt["id"]), receipt)
    return {**work, "agent": identity}


def settle(controller, repo, adapter):
    if not quota.enabled(controller.config, repo):
        return
    issues = {r["issue"] for r in controller.state.workers(repo)
              if r.get("quota_decision") and not r.get("quota_review_released")}
    for number in issues:
        summary = adapter.issue_summary(number)
        authors = [r for r in controller.state.workers(repo) if r["issue"] == number and r.get("kind") != "review"]
        latest = max(authors, key=lambda r: r["started_at"], default={})
        blocked = latest.get("retry_blocked") and not any(controller._holds_reservation(r) for r in authors)
        has_pr = any(number in p.get("issues", []) for p in adapter.snapshot()["prs"])
        if summary.get("state") == "CLOSED" or (blocked and not has_pr):
            admission.release_review(controller.state, repo, number)


def refusal(controller, repo):
    decisions = read_json(controller.state.root / "quota-decisions" / (key(repo) + ".json"))["decisions"]
    return decisions[-1]["reason"]


def review_failure(controller, repo, binding):
    reason = refusal(controller, repo)
    if not reason.startswith("quota exhausted;"):
        raise DriverError(reason + "; assigned authority retained; completion/heartbeat owns revalidation")
    lane = controller.config.lane(repo, binding["reviewer"])
    receipt = {"id": uuid.uuid4().hex, "repo": repo, "agent": binding["reviewer"], "issue": binding["issue"],
               "kind": "review", "pr": binding["pr"], "head": binding["head"], "review": binding,
               "capacity_key": lane["capacity_key"], "state": "launch_failed", "started_at": time.time(),
               "reason": "assigned reviewer quota/independence admission unavailable", "worktree": None}
    write_json(controller.state.worker_path(receipt["id"]), receipt)
    return receipt
