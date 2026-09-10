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

def verify_policy_integrity(snapshot: Any = None) -> None:
    """Validate policy source digest and snapshot invariants."""
    require_package()
    policy = snapshot or _personas.default_snapshot()
    source_digest = policy_source_digest()
    if not source_digest or len(source_digest) != 64:
        raise DriverError("invalid policy source digest")
    if policy.digest and hasattr(policy, "validate"):
        policy.validate()

def get_policy_snapshot(config: Config, repo: str | None = None) -> Any:
    """Return the validated PolicySnapshot for the configuration."""
    require_package()
    pconf = config.project(repo) if repo and repo in config.projects else {}
    doc = pconf.get("personas_policy") or config.raw.get("personas_policy")
    if isinstance(doc, str):
        path = Path(doc)
        snapshot = _personas.load_policy_document(path if path.is_absolute() else (config.path.parent / path).resolve())
    elif isinstance(doc, dict):
        snapshot = _personas.from_document(doc, origin="Driver config personas_policy")
    else:
        snapshot = _personas.default_snapshot()
    verify_policy_integrity(snapshot)
    return snapshot

def load_evidence_store(config: Config, state: State, *, now: datetime | None = None) -> Any:
    """Load or construct the EvidenceStore from state or capability_evidence."""
    require_package()
    ev_spec = config.raw.get("capability_evidence")
    if isinstance(ev_spec, str):
        path = Path(ev_spec)
        p = path if path.is_absolute() else (config.path.parent / path).resolve()
        if p.is_file():
            return _personas.EvidenceStore.load(p)
    elif isinstance(ev_spec, dict):
        return _personas.EvidenceStore.from_dict(ev_spec, origin="config capability_evidence")

    records_file = state.root / "probe_records.json"
    records = []
    if records_file.is_file():
        try:
            data = json.loads(records_file.read_text())
            raw_list = data if isinstance(data, list) else data.get("records", [])
            for item in raw_list:
                records.append(_personas.ProbeRecord.from_dict(item))
        except Exception:
            pass
    return _personas.EvidenceStore(records=tuple(records), origin="Hermes Driver live state")

def record_probe_result(state: State, record: Any) -> None:
    """Save an observation into the state's probe records."""
    records_file = state.root / "probe_records.json"
    existing = []
    if records_file.is_file():
        try:
            data = json.loads(records_file.read_text())
            existing = data if isinstance(data, list) else data.get("records", [])
        except Exception:
            pass
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
    """Preserve cumulative author lineage across persona/account switches and resumed workers."""
    require_package()
    history, seen = [], set()
    for r in receipts:
        if r.get("kind") == "review":
            continue
        persona_id, actor = r.get("persona"), r.get("agent", "")
        if not persona_id:
            if "codex" in actor or "astra" in actor:
                persona_id = "astra-implementer"
            elif "sol" in actor:
                persona_id = "sol-implementer"
            elif "haiku" in actor:
                persona_id = "haiku-triage"
            elif "sonnet" in actor:
                persona_id = "sonnet-reviewer"
            else:
                persona_id = "opus-implementer"
        account_id = r.get("account_id") or r.get("capacity_key") or (
            "openai-codex" if any(k in persona_id for k in ("codex", "astra", "sol")) else "claude-subscription-1"
        )
        ident_key = (persona_id, account_id, actor)
        if ident_key not in seen:
            seen.add(ident_key)
            try:
                history.append(_personas.AuthorIdentity(persona_id, account_id, actor, snapshot=snapshot))
            except Exception:
                pass
    return tuple(history)

def _classify_task(task: dict, touches: tuple[str, ...], labels: tuple[str, ...], kind: str) -> str:
    for label in labels:
        if label.startswith("aru-task:"):
            return label.removeprefix("aru-task:")
    title_body = (task.get("title", "") + " " + task.get("body", "")).lower()
    if "architecture" in title_body:
        return "architecture_decision"
    if "needs-design" in labels:
        return "design_evidence_analysis"
    if kind == "remediation":
        return "bounded_implementation"
    if touches and all("docs" in t or t.endswith(".md") for t in touches) and not any(t.endswith((".py", ".sh", ".ts", ".go")) for t in touches):
        return "triage_documentation"
    if any(t.startswith("integrations/hermes") or "kernel" in t for t in touches):
        return "security_implementation"
    return "bounded_implementation"

def resolve_task_plan(config: Config, state: State, repo: str, task: dict, agent: str,
                      worktree: str, branch: str, head: str, *,
                      kind: str = "implementation", pr: int | None = None,
                      author_history: tuple[Any, ...] = (), handoff_reason: str = "",
                      persona_override: str | None = None, effort_override: str | None = None,
                      major_unresolved: bool = False, allow_optional: bool = True,
                      input_files: tuple[str, ...] = (), now: datetime | None = None,
                      evidence: Any = None) -> Any:
    """Resolve one author or remediation task deterministically into a CommandPlan."""
    require_package()
    issue_num = task.get("number", task.get("issue"))
    touches = tuple(task.get("touches", ())) or ("src/main.py",)
    labels = tuple(task.get("labels", ()))
    task_class = _classify_task(task, touches, labels, kind)
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

def _resolve_review_authors(binding: dict, state: State, repo: str, issue_num: int,
                            policy: Any, author_actor: str, author_family: str) -> tuple[Any, ...]:
    author_raw = binding.get("author_history")
    if author_raw:
        return tuple(
            a if isinstance(a, _personas.AuthorIdentity) else _personas.AuthorIdentity(
                a.get("persona", "opus-implementer"), a.get("account_id", "claude-subscription-1"),
                a.get("actor", author_actor), snapshot=policy,
            ) for a in author_raw
        )
    workers = [r for r in state.workers(repo) if r.get("issue") == issue_num]
    history = extract_author_identities(workers, policy)
    if history:
        return history
    is_codex = "codex" in author_family or "openai" in author_family
    return (_personas.AuthorIdentity(
        "astra-implementer" if is_codex else "opus-implementer",
        "openai-codex" if is_codex else "claude-subscription-1",
        author_actor, snapshot=policy,
    ),)

def _select_reviewer_persona(binding: dict, policy: Any,
                             lineages: set[str], vendors: set[str]) -> str:
    r_persona = binding.get("reviewer_persona")
    if r_persona:
        return r_persona
    for p in policy.personas.values():
        if p.performs("code_reviewer") and p.lineage not in lineages and p.lineage not in vendors:
            return p.id
    for p in policy.personas.values():
        if p.performs("code_reviewer") and p.lineage not in lineages:
            return p.id
    return "sonnet-reviewer" if "anthropic-claude" not in lineages else "astra-implementer"

def _select_reviewer_account(binding: dict, policy: Any, reviewer: Any,
                             authors: tuple[Any, ...]) -> str:
    r_account = binding.get("reviewer_account")
    if r_account:
        return r_account
    auth_accs = {a.account_id for a in authors}
    for a in policy.accounts.values():
        if a.lineage == reviewer.lineage and a.route == reviewer.route and a.id not in auth_accs:
            return a.id
    return "claude-subscription-2" if "claude" in reviewer.lineage else "openai-codex"

def resolve_review_plan(config: Config, state: State, repo: str, binding: dict,
                        worktree: str, head: str, *,
                        now: datetime | None = None, evidence: Any = None) -> Any:
    """Resolve an assigned coding review into a read-only CommandPlan."""
    require_package()
    pr_num, issue_num = binding["pr"], binding.get("issue", binding["pr"])
    reviewer_actor = binding.get("reviewer_actor", "reviewer")
    author_actor = binding.get("author_actor", binding.get("author", "author"))
    author_family = binding.get("author_family", "")
    touches = tuple(binding.get("touches", ("src/main.py",)))

    policy = get_policy_snapshot(config, repo)
    authors = _resolve_review_authors(binding, state, repo, issue_num, policy, author_actor, author_family)
    lineages, vendors = {a.family for a in authors}, {a.vendor for a in authors}

    reviewer_persona = _select_reviewer_persona(binding, policy, lineages, vendors)
    reviewer = policy.persona(reviewer_persona)
    reviewer_account = _select_reviewer_account(binding, policy, reviewer, authors)

    if reviewer_actor.casefold() in {a.actor.casefold() for a in authors}:
        reviewer_actor = f"{reviewer_persona}-reviewer"

    assignment = _personas.ReviewAssignment(
        repo=repo, pr=pr_num, head=head, issue=issue_num,
        risk_tier=binding.get("risk_tier", 1), authority=reviewer.lineage,
        reviewer_persona=reviewer_persona, reviewer_actor=reviewer_actor,
        reviewer_account=reviewer_account, authors=authors, touches=touches,
        authority_source=binding.get("authority_source", "canonical kernel review authority"),
        external_first_released=binding.get("external_first_released", True),
        external_first_reason=binding.get("external_first_reason", "external provider unavailable"),
    )
    context = _personas.PromptContext(
        worktree=str(Path(worktree).resolve()), branch=f"pull/{pr_num}/head", head=head, pr=pr_num,
    )
    fleet_b = build_fleet_binding(config, state, repo, worktree=worktree, snapshot=policy, now=now, evidence=evidence)
    return _personas.plan_review(assignment, head, fleet_b, context, now=now, current_assignment=assignment)

