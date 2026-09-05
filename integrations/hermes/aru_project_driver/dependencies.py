"""Recover dependency continuation from GitHub contracts, without a local queue."""

import json

from . import handoff_contract
from .config import DriverError
from .kernel import KernelAdapterError


def _quota_error(exc: Exception) -> bool:
    return any(term in str(exc).lower() for term in ("rate limit", "rate-limit", "quota"))


def proofs(controller, contract: dict, *, cache: dict | None = None) -> list[dict]:
    result = []
    for condition in contract["conditions"]:
        key = json.dumps(condition, sort_keys=True)
        try:
            proof = cache.get(key) if cache is not None else None
            if proof is None:
                proof = controller.adapter(condition["repo"]).dependency_evidence(condition)
            if not isinstance(proof, dict) or type(proof.get("satisfied")) is not bool:
                raise DriverError("dependency evidence lacks an explicit result")
        except (KernelAdapterError, DriverError, ValueError) as exc:
            if _quota_error(exc):
                raise DriverError(str(exc)) from exc
            proof = {"satisfied": False, "reason": str(exc)}
        if cache is not None:
            cache[key] = proof
        result.append({"condition": condition, "proof": proof})
    return result


def _action(controller, repo: str, number: int, snapshots: dict,
            readiness_cache: dict, proof_cache: dict) -> tuple[dict, bool]:
    contract, _ = controller._dependency_contract(repo, number)
    digest = handoff_contract.digest(contract)
    verified = proofs(controller, contract, cache=proof_cache)
    satisfied = all(item["proof"]["satisfied"] for item in verified)
    action = {"type": "dependency", "issue": number, "target": contract["target"],
              "target_issue": contract["issue"], "contract_digest": digest,
              "satisfied": satisfied, "next_action": "wait"}
    if satisfied:
        event_id = f"dependency:{repo}:{number}:{digest}"
        if not controller.state.has_event(repo, event_id):
            action["next_action"] = "dependency-satisfied"
        return action, False
    action["blockers"] = [item["proof"].get("reason", "dependency condition is not satisfied")
                          for item in verified if not item["proof"]["satisfied"]]
    target, issue = contract["target"], contract["issue"]
    event_id = f"handoff:{repo}:{number}:{digest}"
    if controller.state.has_event(target, event_id):
        return action, True
    if (target, issue) not in readiness_cache:
        if target not in snapshots:
            snapshots[target] = controller.adapter(target).snapshot()
        readiness_cache[target, issue] = controller._target_readiness(
            target, issue, snapshot=snapshots[target],
        )
    readiness = readiness_cache[target, issue]
    action["receiver"] = {key: value for key, value in readiness.items() if key != "snapshot_at"}
    if not readiness["blockers"]:
        action["next_action"] = "handoff"
    return action, True


def actions(controller, repo: str, snapshot: dict) -> tuple[list[dict], set[int]]:
    sources = [item for item in snapshot["issues"]
               if item["status"] in {"In Progress", "In Review"}
               and handoff_contract.MARKER in item.get("body", "")]
    if len(sources) > 32:
        raise DriverError("active dependency inventory exceeds the bounded limit")
    result, held = [], set()
    # These caches last only for this read-only plan. Delivery always rereads
    # its contract, receiver and proofs; no cached result authorizes a mutation.
    snapshots, readiness_cache, proof_cache = {}, {}, {}
    for source in sources:
        number = source["number"]
        try:
            action, blocked = _action(controller, repo, number, snapshots, readiness_cache, proof_cache)
        except (DriverError, KernelAdapterError) as exc:
            if _quota_error(exc):
                raise
            action = {"type": "dependency", "issue": number, "next_action": "wait",
                      "satisfied": False, "blockers": [str(exc)]}
            blocked = True
        if blocked:
            held.add(number)
        result.append(action)
    return result, held
