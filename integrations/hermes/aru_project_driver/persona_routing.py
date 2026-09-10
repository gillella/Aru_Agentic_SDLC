"""Enforce approved persona routing, exact probes, and author/review launch boundaries."""
from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any, Iterable

try:
    from integrations import personas as _personas
    from integrations.personas.catalog import route as get_route
    from integrations.personas.plan import policy_source_digest
except ImportError:
    try:
        import personas as _personas
        from personas.catalog import route as get_route
        from personas.plan import policy_source_digest
    except ImportError:
        _personas = get_route = policy_source_digest = None

from .config import Config, DriverError
from .state import State, key, read_json, write_json

def is_available() -> bool:
    """Return True if the persona policy package is importable."""
    return _personas is not None

def require_package() -> None:
    if not is_available():
        raise DriverError("personas package is not available on PYTHONPATH")

def enabled(config: Config, repo: str) -> bool:
    """Explicit migration switch; package importability never enables dispatch."""
    value = config.project(repo).get("personas_required", config.raw.get("personas_required", False))
    if type(value) is not bool:
        raise DriverError("personas_required must be an explicit boolean")
    return value


def verify_policy_integrity(snapshot: Any = None, *, source_digest=None, policy_digest=None) -> None:
    """Compare configured integrity pins, rather than validating hash length alone."""
    require_package()
    policy = snapshot or _personas.default_snapshot()
    if source_digest is not None and source_digest != policy_source_digest():
        raise DriverError("persona source integrity mismatch")
    if policy_digest is not None and policy_digest != policy.digest:
        raise DriverError("persona policy integrity mismatch")

def get_policy_snapshot(config: Config, repo: str | None = None) -> Any:
    """Return the validated PolicySnapshot for the configuration."""
    require_package()
    pconf = config.project(repo) if repo and repo in config.projects else {}
    doc = pconf.get("personas_policy", config.raw.get("personas_policy"))
    if isinstance(doc, str):
        path = Path(doc)
        snapshot = _personas.load_policy_document(path if path.is_absolute() else (config.path.parent / path).resolve())
    elif isinstance(doc, dict):
        snapshot = _personas.from_document(doc, origin="Driver config personas_policy")
    elif doc is None:
        snapshot = _personas.default_snapshot()
    else:
        raise DriverError("personas_policy must be a file path or policy object")
    pins = {name: pconf.get(name, config.raw.get(name))
            for name in ("personas_source_digest", "personas_policy_digest")}
    if repo and enabled(config, repo) and any(not value for value in pins.values()):
        raise DriverError("persona-required dispatch needs source and policy digest pins")
    verify_policy_integrity(snapshot, source_digest=pins["personas_source_digest"],
                            policy_digest=pins["personas_policy_digest"])
    return snapshot

def load_evidence_store(config: Config, state: State, *, now: datetime | None = None) -> Any:
    """Load or construct the EvidenceStore from state or capability_evidence."""
    require_package()
    ev_spec = config.raw.get("capability_evidence")
    if isinstance(ev_spec, str):
        path = Path(ev_spec)
        p = path if path.is_absolute() else (config.path.parent / path).resolve()
        return _personas.EvidenceStore.load(p)
    elif isinstance(ev_spec, dict):
        return _personas.EvidenceStore.from_dict(ev_spec, origin="config capability_evidence")

    records_file = state.root / "probe_records.json"
    records = []
    if records_file.is_file():
        try:
            data = json.loads(records_file.read_text())
            raw_list = data if isinstance(data, list) else data["records"]
            for item in raw_list:
                records.append(_personas.ProbeRecord.from_dict(item))
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            raise DriverError("persona capability evidence is unreadable") from exc
    return _personas.EvidenceStore(records=tuple(records), origin="Hermes Driver live state")

def record_probe_result(state: State, record: Any) -> None:
    """Save an observation into the state's probe records."""
    records_file = state.root / "probe_records.json"
    existing = []
    if records_file.is_file():
        try:
            data = json.loads(records_file.read_text())
            existing = data if isinstance(data, list) else data["records"]
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            raise DriverError("existing persona probe records are unreadable; preserved") from exc
    if not isinstance(existing, list) or any(not isinstance(item, dict) for item in existing):
        raise DriverError("existing persona probe records must be an array of objects")
    key_tuple = (record.account_id, record.route, record.model_id, record.effort)
    filtered = [r for r in existing if (r.get("account_id"), r.get("route"), r.get("model_id"), r.get("effort")) != key_tuple]
    filtered.append(record.to_dict())
    write_json(records_file, {"records": filtered})

def _find_lane_executable(config: Config, repo: str, pconf: dict, route_name: str) -> str | None:
    for ident in pconf.get("lanes", ()):
        lane = config.lane(repo, ident) if ident in config.lanes else {}
        fam = lane.get("family")
        if (fam == route_name or (fam == "claude-code" and route_name == "claude-code")
                or (fam == "openai-codex" and route_name == "codex")
                or (fam == "xai-cursor" and route_name == "cursor")
                or (fam == "google-antigravity" and route_name == "antigravity")):
            cmd = lane.get("command", [])
            if cmd and Path(cmd[0]).name in get_route(route_name).executables:
                return cmd[0]
    return None

def _build_harness_for_route(config: Config, repo: str, pconf: dict,
                             workspace: str, rname: str, hconf: dict) -> Any:
    if rname in hconf:
        spec = hconf[rname]
        return _personas.HarnessBinding(
            route=rname, executable=spec["executable"],
            workspace=spec.get("workspace", workspace),
            declared_modalities=frozenset(spec.get("declared_modalities", ())),
            modality_evidence=spec.get("modality_evidence", ""),
            version=spec.get("version", ""),
        )
    exe = _find_lane_executable(config, repo, pconf, rname) or f"/usr/local/bin/{sorted(get_route(rname).executables)[0]}"
    d_mod = frozenset({"image"}) if rname == "antigravity" and pconf.get("antigravity_image_support") else frozenset()
    return _personas.HarnessBinding(
        route=rname, executable=exe, workspace=workspace, declared_modalities=d_mod,
        modality_evidence="configured harness supports image input" if d_mod else "",
    )

def _build_account_binding(config: Config, state: State, repo: str,
                           policy: Any, acc_id: str, apol: Any) -> Any:
    owner = repo.split("/")[0] if "/" in repo else ""
    allowed = (repo,) if (not apol.required_owners or owner in apol.required_owners) else (f"{list(apol.required_owners)[0]}/placeholder",)
    env = {}
    if apol.route == "claude-code":
        num = acc_id.removeprefix("claude-subscription-") if acc_id.startswith("claude-subscription-") else "1"
        env["CLAUDE_CONFIG_DIR"] = str(config.hermes_home / f"profiles/claude-{num}")
    elif apol.route == "codex":
        env["CODEX_HOME"] = str(config.hermes_home / "profiles/codex")
    in_use = len(state.capacity_holders(apol.capacity_key, apol.concurrency or 1))
    cd = read_json(state.root / "cooldowns" / f"{key(apol.capacity_key)}.json", {"until": 0})
    unavail = cd.get("reason", "provider cooldown") if cd.get("until", 0) > time.time() else ""
    return _personas.AccountBinding(
        account_id=acc_id, allowed_projects=allowed, env=env,
        max_sessions=apol.concurrency or 1, sessions_in_use=in_use,
        unavailable_reason=unavail, snapshot=policy,
    )

def build_fleet_binding(config: Config, state: State, repo: str, *,
                        worktree: str | Path | None = None, snapshot: Any = None,
                        now: datetime | None = None, evidence: Any = None,
                        enabled_optional: Iterable[str] | None = None) -> Any:
    """Construct a FleetBinding from Driver configuration and recorded evidence."""
    require_package()
    policy = snapshot or get_policy_snapshot(config, repo)
    pconf = config.project(repo) if repo in config.projects else {}
    repo_dir = str(pconf.get("repo_dir", config.path.parent))
    workspace = str(Path(worktree).resolve() if worktree else Path(repo_dir).resolve())
    raw_binding = pconf.get("fleet_binding") or config.raw.get("fleet_binding")
    if isinstance(raw_binding, str):
        path = Path(raw_binding)
        return _personas.FleetBinding.load(path if path.is_absolute() else (config.path.parent / path).resolve())
    if isinstance(raw_binding, dict):
        return _personas.FleetBinding.from_dict(raw_binding, base=config.path.parent)

    hconf = pconf.get("harnesses") or config.raw.get("harnesses") or {}
    harnesses = {rn: _build_harness_for_route(config, repo, pconf, workspace, rn, hconf)
                 for rn in ("claude-code", "codex", "cursor", "antigravity")}
    accounts = [_build_account_binding(config, state, repo, policy, aid, ap)
                for aid, ap in policy.accounts.items()]
    if evidence is None:
        evidence = load_evidence_store(config, state, now=now)
    opt = enabled_optional if enabled_optional is not None else pconf.get("enabled_optional_personas", ("spark-pair",))
    return _personas.FleetBinding(
        harnesses=harnesses, accounts=tuple(accounts), evidence=evidence,
        enabled_optional=frozenset(opt) if isinstance(opt, (list, tuple, set, frozenset)) else frozenset(),
        snapshot=policy,
    )

def extract_author_identities(receipts: list[dict], snapshot: Any) -> tuple[Any, ...]:
    """Require explicit cumulative identities; never infer a model from an agent name."""
    require_package()
    history = []
    for receipt in receipts:
        if receipt.get("kind") == "review":
            continue
        entries = receipt.get("author_history") or [receipt]
        for entry in entries:
            persona = entry.get("persona_id", entry.get("persona"))
            account, actor = entry.get("account_id"), entry.get("actor")
            if not all((persona, account, actor)):
                raise DriverError("incomplete author history requires explicit reconciliation")
            identity = _personas.AuthorIdentity(persona, account, actor, snapshot=snapshot)
            if identity not in history:
                history.append(identity)
    return tuple(history)

def resolve_task_plan(config: Config, state: State, repo: str, task: dict, agent: str,
                      worktree: str, branch: str, head: str, *,
                      kind: str = "implementation", pr: int | None = None,
                      author_history: tuple[Any, ...] = (), handoff_reason: str = "",
                      persona_override: str | None = None, effort_override: str | None = None,
                      major_unresolved: bool = False, allow_optional: bool = False,
                      input_files: tuple[str, ...] = (), now: datetime | None = None,
                      evidence: Any = None) -> Any:
    """Resolve one author or remediation task deterministically into a CommandPlan."""
    require_package()
    issue_num = task.get("number", task.get("issue"))
    touches = tuple(task.get("touches", ()))
    labels = tuple(task.get("labels", ()))
    task_class = task.get("task_class")  # classify() validates every structured label; no prose inference.
    policy = get_policy_snapshot(config, repo)
    if not author_history:
        workers = [r for r in state.workers(repo) if r.get("issue") == issue_num]
        author_history = extract_author_identities(workers, policy)

    request = _personas.TaskRequest(
        project=repo, issue=issue_num, task_class=task_class, labels=labels,
        touches=touches, actor=agent, persona_override=persona_override,
        effort_override=effort_override, major_unresolved_decision=major_unresolved,
        allow_optional=allow_optional, author_history=author_history,
        handoff_reason=handoff_reason, input_files=tuple(input_files),
        title=task.get("title", f"Issue #{issue_num}"),
    )
    context = _personas.PromptContext(
        worktree=str(Path(worktree).resolve()), branch=branch, head=head, pr=pr,
    )
    fleet_b = build_fleet_binding(config, state, repo, worktree=worktree, snapshot=policy, now=now, evidence=evidence)
    return _personas.resolve(request, fleet_b, context, now=now)

def resolve_review_plan(config: Config, state: State, repo: str, binding: dict,
                        worktree: str, head: str, *, current_binding: dict | None = None,
                        now: datetime | None = None, evidence: Any = None) -> Any:
    """Compile only an explicit assignment matching separately reread authority."""
    require_package()
    if not isinstance(binding, dict) or current_binding is None or binding != current_binding:
        raise DriverError("persona review requires separately reread matching kernel authority")
    required = ("pr", "issue", "reviewer_actor", "reviewer_persona", "reviewer_account",
                "authority", "authority_source", "external_first_reason", "touches", "author_history")
    if any(not binding.get(name) for name in required) or binding.get("external_first_released") is not True:
        raise DriverError("persona review assignment is incomplete")
    if binding.get("repo") != repo or binding.get("head") != head:
        raise DriverError("persona review project or current head mismatch")
    policy = get_policy_snapshot(config, repo)
    authors = extract_author_identities(binding["author_history"], policy)
    assignment = _personas.ReviewAssignment(
        repo=repo, pr=binding["pr"], head=head, issue=binding["issue"],
        risk_tier=binding["risk_tier"], authority=binding["authority"],
        reviewer_persona=binding["reviewer_persona"], reviewer_actor=binding["reviewer_actor"],
        reviewer_account=binding["reviewer_account"], authors=authors, touches=tuple(binding["touches"]),
        authority_source=binding["authority_source"], external_first_released=True,
        external_first_reason=binding["external_first_reason"],
    )
    context = _personas.PromptContext(worktree=str(Path(worktree).resolve()),
                                    branch=f"pull/{binding['pr']}/head", head=head, pr=binding["pr"])
    fleet = build_fleet_binding(config, state, repo, worktree=worktree, snapshot=policy,
                               now=now, evidence=evidence)
    # The equality above compares separate bridge observations before conversion.
    return _personas.plan_review(assignment, head, fleet, context, now=now,
                                current_assignment=assignment)
