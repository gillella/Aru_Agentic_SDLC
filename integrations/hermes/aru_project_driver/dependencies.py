"""Recover dependency continuation from GitHub contracts, without a local queue."""

from . import handoff_contract
from .config import DriverError
from .kernel import KernelAdapterError


def proofs(controller, contract: dict) -> list[dict]:
    result = []
    for condition in contract["conditions"]:
        try:
            proof = controller.adapter(condition["repo"]).dependency_evidence(condition)
            if not isinstance(proof, dict) or type(proof.get("satisfied")) is not bool:
                raise DriverError("dependency evidence lacks an explicit result")
        except (KernelAdapterError, DriverError, ValueError) as exc:
            proof = {"satisfied": False, "reason": str(exc)}
        result.append({"condition": condition, "proof": proof})
    return result


def _action(controller, repo: str, number: int) -> tuple[dict, bool]:
    contract, _ = controller._dependency_contract(repo, number)
    digest = handoff_contract.digest(contract)
    verified = proofs(controller, contract)
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
    readiness = controller._target_readiness(contract["target"], contract["issue"])
    action["receiver"] = {key: value for key, value in readiness.items() if key != "snapshot_at"}
    event_id = f"handoff:{repo}:{number}:{digest}"
    if not readiness["blockers"] and not controller.state.has_event(contract["target"], event_id):
        action["next_action"] = "handoff"
    return action, True


def actions(controller, repo: str, snapshot: dict) -> tuple[list[dict], set[int]]:
    sources = [item for item in snapshot["issues"]
               if item["status"] in {"In Progress", "In Review"}
               and handoff_contract.MARKER in item.get("body", "")]
    if len(sources) > 32:
        raise DriverError("active dependency inventory exceeds the bounded limit")
    result, held = [], set()
    for source in sources:
        number = source["number"]
        try:
            action, blocked = _action(controller, repo, number)
        except (DriverError, KernelAdapterError) as exc:
            action = {"type": "dependency", "issue": number, "next_action": "wait",
                      "satisfied": False, "blockers": [str(exc)]}
            blocked = True
        if blocked:
            held.add(number)
        result.append(action)
    return result, held
