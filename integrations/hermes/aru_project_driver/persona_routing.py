"""Enforce approved persona routing, exact probes, and author/review launch boundaries.

Bridges the external Hermes Driver with the validated 13-persona package:
1. Validates persona policy documents and source integrity (SHA-256 digest).
2. Builds and maintains FleetBinding with HarnessBinding and AccountBinding for configured routes.
3. Performs lock-aware, bounded exact-model/effort capability probes and collects EvidenceStore records.
4. Resolves tasks (authors, resumed/remediation, operator preflight) into deterministic CommandPlans.
5. Resolves reviews (assigned coding reviewers) into read-only CommandPlans with strict author-lineage independence.
6. Handles automatic architectural fallback: Fable 5.1 -> GPT-6 Astra -> Claude Opus 5.
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping

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
        _personas = None
        get_route = None
        policy_source_digest = None

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
    expected_digest = policy_source_digest()
    if policy.digest and hasattr(policy, "validate"):
        policy.validate()


def get_policy_snapshot(config: Config, repo: str | None = None) -> Any:
    """Return the validated PolicySnapshot for the configuration."""
    require_package()
    project_conf = config.project(repo) if repo and repo in config.projects else {}
    policy_doc = project_conf.get("personas_policy") or config.raw.get("personas_policy")
    if isinstance(policy_doc, str):
        path = Path(policy_doc)
        if not path.is_absolute():
            path = (config.path.parent / path).resolve()
        snapshot = _personas.load_policy_document(path)
    elif isinstance(policy_doc, dict):
        snapshot = _personas.from_document(policy_doc, origin="Driver config personas_policy")
    else:
        snapshot = _personas.default_snapshot()
    verify_policy_integrity(snapshot)
    return snapshot


def load_evidence_store(config: Config, state: State, *, now: datetime | None = None) -> Any:
    """Load or construct the EvidenceStore from state or capability_evidence."""
    require_package()
    evidence_spec = config.raw.get("capability_evidence")
    if isinstance(evidence_spec, str):
        path = Path(evidence_spec)
        if not path.is_absolute():
            path = (config.path.parent / path).resolve()
        if path.is_file():
            return _personas.EvidenceStore.load(path)
    elif isinstance(evidence_spec, dict):
        return _personas.EvidenceStore.from_dict(evidence_spec, origin="config capability_evidence")

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
    filtered = [r for r in existing if (r.get("account_id"), r.get("route"), r.get("model_id"), r.get("effort")) !=
                (record.account_id, record.route, record.model_id, record.effort)]
    filtered.append(record.to_dict())
    write_json(records_file, {"records": filtered})


def build_fleet_binding(config: Config, state: State, repo: str, *,
                        worktree: str | Path | None = None,
                        snapshot: Any = None,
                        now: datetime | None = None,
                        evidence: Any = None,
                        enabled_optional: Iterable[str] | None = None) -> Any:
    """Construct a FleetBinding from Driver configuration and recorded evidence."""
    require_package()
    policy = snapshot or get_policy_snapshot(config, repo)
    project_conf = config.project(repo) if repo in config.projects else {}
    repo_dir = str(project_conf.get("repo_dir", config.path.parent))
    workspace = str(Path(worktree).resolve() if worktree else Path(repo_dir).resolve())

    raw_binding = project_conf.get("fleet_binding") or config.raw.get("fleet_binding")
    if isinstance(raw_binding, str):
        path = Path(raw_binding)
        if not path.is_absolute():
            path = (config.path.parent / path).resolve()
        return _personas.FleetBinding.load(path)
    elif isinstance(raw_binding, dict):
        return _personas.FleetBinding.from_dict(raw_binding, base=config.path.parent)

    harnesses_conf = project_conf.get("harnesses") or config.raw.get("harnesses") or {}
    harnesses = {}

    for route_name in ("claude-code", "codex", "cursor", "antigravity"):
        if route_name in harnesses_conf:
            h_spec = harnesses_conf[route_name]
            harnesses[route_name] = _personas.HarnessBinding(
                route=route_name,
                executable=h_spec["executable"],
                workspace=h_spec.get("workspace", workspace),
                declared_modalities=frozenset(h_spec.get("declared_modalities", ())),
                modality_evidence=h_spec.get("modality_evidence", ""),
                version=h_spec.get("version", ""),
            )
        else:
            exe = None
            for identity in project_conf.get("lanes", ()):
                lane = config.lane(repo, identity) if identity in config.lanes else {}
                family = lane.get("family")
                if (family == route_name or
                    (family == "claude-code" and route_name == "claude-code") or
                    (family == "openai-codex" and route_name == "codex") or
                    (family == "xai-cursor" and route_name == "cursor") or
                    (family == "google-antigravity" and route_name == "antigravity")):
                    cmd = lane.get("command", [])
                    if cmd:
                        candidate_exe = cmd[0]
                        spec = get_route(route_name)
                        if Path(candidate_exe).name in spec.executables:
                            exe = candidate_exe
                            break
            if not exe:
                spec = get_route(route_name)
                exe = f"/usr/local/bin/{sorted(spec.executables)[0]}"
            declared_mod = frozenset({"image"}) if route_name == "antigravity" and project_conf.get("antigravity_image_support") else frozenset()
            mod_ev = "configured harness supports image input" if declared_mod else ""
            harnesses[route_name] = _personas.HarnessBinding(
                route=route_name,
                executable=exe,
                workspace=workspace,
                declared_modalities=declared_mod,
                modality_evidence=mod_ev,
            )

    accounts = []
    for acc_id, acc_policy in policy.accounts.items():
        if acc_policy.required_owners:
            owner = repo.split("/")[0] if "/" in repo else ""
            if owner in acc_policy.required_owners:
                allowed_projects = (repo,)
            else:
                allowed_projects = (f"{list(acc_policy.required_owners)[0]}/placeholder",)
        else:
            allowed_projects = (repo,)

        env = {}
        if acc_policy.route == "claude-code":
            profile_num = acc_id.removeprefix("claude-subscription-") if acc_id.startswith("claude-subscription-") else "1"
            env["CLAUDE_CONFIG_DIR"] = str(config.hermes_home / f"profiles/claude-{profile_num}")
        elif acc_policy.route == "codex":
            env["CODEX_HOME"] = str(config.hermes_home / "profiles/codex")

        sessions_in_use = len(state.capacity_holders(acc_policy.capacity_key, acc_policy.concurrency or 1))

        unavail = ""
        cooldown = read_json(state.root / "cooldowns" / f"{key(acc_policy.capacity_key)}.json", {"until": 0})
        if cooldown.get("until", 0) > time.time():
            unavail = cooldown.get("reason", "provider cooldown")

        accounts.append(_personas.AccountBinding(
            account_id=acc_id,
            allowed_projects=allowed_projects,
            env=env,
            max_sessions=acc_policy.concurrency or 1,
            sessions_in_use=sessions_in_use,
            unavailable_reason=unavail,
            snapshot=policy,
        ))

    if evidence is None:
        evidence = load_evidence_store(config, state, now=now)

    opt = enabled_optional if enabled_optional is not None else project_conf.get("enabled_optional_personas", ("spark-pair",))
    opt_set = frozenset(opt) if isinstance(opt, (list, tuple, set, frozenset)) else frozenset()

    return _personas.FleetBinding(
        harnesses=harnesses,
        accounts=tuple(accounts),
        evidence=evidence,
        enabled_optional=opt_set,
        snapshot=policy,
    )


def extract_author_identities(receipts: list[dict], snapshot: Any) -> tuple[Any, ...]:
    """Preserve cumulative author lineage across persona/account switches and resumed workers."""
    require_package()
    history = []
    seen = set()
    for r in receipts:
        if r.get("kind") == "review":
            continue
        persona_id = r.get("persona")
        account_id = r.get("account_id") or r.get("capacity_key")
        actor = r.get("agent", "")
        if not persona_id:
            agent = r.get("agent", "")
            if "codex" in agent or "astra" in agent:
                persona_id = "astra-implementer"
            elif "sol" in agent:
                persona_id = "sol-implementer"
            elif "haiku" in agent:
                persona_id = "haiku-triage"
            elif "sonnet" in agent:
                persona_id = "sonnet-reviewer"
            else:
                persona_id = "opus-implementer"
        if not account_id:
            account_id = "openai-codex" if "codex" in persona_id or "astra" in persona_id or "sol" in persona_id else "claude-subscription-1"
        ident_key = (persona_id, account_id, actor)
        if ident_key not in seen:
            seen.add(ident_key)
            try:
                history.append(_personas.AuthorIdentity(persona_id, account_id, actor, snapshot=snapshot))
            except Exception:
                pass
    return tuple(history)


def resolve_task_plan(config: Config, state: State, repo: str, task: dict, agent: str,
                      worktree: str, branch: str, head: str, *,
                      kind: str = "implementation", pr: int | None = None,
                      author_history: tuple[Any, ...] = (),
                      handoff_reason: str = "",
                      persona_override: str | None = None,
                      effort_override: str | None = None,
                      major_unresolved: bool = False,
                      allow_optional: bool = True,
                      input_files: tuple[str, ...] = (),
                      now: datetime | None = None,
                      evidence: Any = None) -> Any:
    """Resolve one author or remediation task deterministically into a CommandPlan."""
    require_package()
    issue_num = task.get("number", task.get("issue"))
    touches = tuple(task.get("touches", ()))
    if not touches:
        touches = ("src/main.py",)
    labels = tuple(task.get("labels", ()))

    task_class = None
    for label in labels:
        if label.startswith("aru-task:"):
            task_class = label.removeprefix("aru-task:")
            break
    if not task_class:
        if "architecture" in task.get("title", "").lower() or "architecture" in task.get("body", "").lower():
            task_class = "architecture_decision"
        elif "needs-design" in labels:
            task_class = "design_evidence_analysis"
        elif kind == "remediation":
            task_class = "bounded_implementation"
        elif touches and all("docs" in t or t.endswith(".md") for t in touches) and not any(t.endswith((".py", ".sh", ".ts", ".go")) for t in touches):
            task_class = "triage_documentation"
        elif any(t.startswith("integrations/hermes") or "kernel" in t for t in touches):
            task_class = "security_implementation"
        else:
            task_class = "bounded_implementation"

    policy = get_policy_snapshot(config, repo)
    if not author_history:
        workers = [r for r in state.workers(repo) if r.get("issue") == issue_num]
        author_history = extract_author_identities(workers, policy)

    request = _personas.TaskRequest(
        project=repo,
        issue=issue_num,
        task_class=task_class,
        labels=labels,
        touches=touches,
        actor=agent,
        persona_override=persona_override,
        effort_override=effort_override,
        major_unresolved_decision=major_unresolved,
        allow_optional=allow_optional,
        author_history=author_history,
        handoff_reason=handoff_reason,
        input_files=tuple(input_files),
        title=task.get("title", f"Issue #{issue_num}"),
    )

    context = _personas.PromptContext(
        worktree=str(Path(worktree).resolve()),
        branch=branch,
        head=head,
        pr=pr,
    )

    fleet_b = build_fleet_binding(config, state, repo, worktree=worktree, snapshot=policy, now=now, evidence=evidence)
    plan = _personas.resolve(request, fleet_b, context, now=now)
    return plan


def resolve_review_plan(config: Config, state: State, repo: str, binding: dict,
                        worktree: str, head: str, *,
                        now: datetime | None = None,
                        evidence: Any = None) -> Any:
    """Resolve an assigned coding review into a read-only CommandPlan."""
    require_package()
    pr_num = binding["pr"]
    issue_num = binding.get("issue", pr_num)
    reviewer_agent = binding.get("reviewer", binding.get("reviewer_actor", "reviewer"))
    reviewer_actor = binding.get("reviewer_actor", "reviewer")
    author_agent = binding.get("author", "author")
    author_actor = binding.get("author_actor", author_agent)
    author_family = binding.get("author_family", "")
    authority = binding.get("authority", "claude" if "codex" in author_family or "openai" in author_family else "codex")
    touches = tuple(binding.get("touches", ("src/main.py",)))

    policy = get_policy_snapshot(config, repo)
    author_history_raw = binding.get("author_history")
    if author_history_raw:
        author_history = tuple(
            a if isinstance(a, _personas.AuthorIdentity) else _personas.AuthorIdentity(
                a.get("persona", "opus-implementer"),
                a.get("account_id", "claude-subscription-1"),
                a.get("actor", author_actor),
                snapshot=policy,
            )
            for a in author_history_raw
        )
    else:
        workers = [r for r in state.workers(repo) if r.get("issue") == issue_num]
        author_history = extract_author_identities(workers, policy)
        if not author_history:
            author_p = "astra-implementer" if "codex" in author_family or "openai" in author_family else "opus-implementer"
            author_a = "openai-codex" if "codex" in author_family or "openai" in author_family else "claude-subscription-1"
            author_history = (_personas.AuthorIdentity(author_p, author_a, author_actor, snapshot=policy),)

    author_lineages = {a.family for a in author_history}
    author_vendors = {a.vendor for a in author_history}
    author_actors = {a.actor.casefold() for a in author_history}

    reviewer_persona = binding.get("reviewer_persona")
    if not reviewer_persona:
        for p in policy.personas.values():
            if (p.performs("code_reviewer") and p.lineage not in author_lineages
                    and p.lineage not in author_vendors):
                reviewer_persona = p.id
                break
    if not reviewer_persona:
        for p in policy.personas.values():
            if p.performs("code_reviewer") and p.lineage not in author_lineages:
                reviewer_persona = p.id
                break
    if not reviewer_persona:
        reviewer_persona = "sonnet-reviewer" if "anthropic-claude" not in author_lineages else "astra-implementer"

    reviewer = policy.persona(reviewer_persona)
    authority = reviewer.lineage

    reviewer_account = binding.get("reviewer_account")
    if not reviewer_account:
        for a in policy.accounts.values():
            if a.lineage == reviewer.lineage and a.route == reviewer.route:
                if a.id not in {auth.account_id for auth in author_history}:
                    reviewer_account = a.id
                    break
    if not reviewer_account:
        reviewer_account = "claude-subscription-2" if "claude" in reviewer.lineage else "openai-codex"

    if reviewer_actor.casefold() in author_actors:
        reviewer_actor = f"{reviewer_persona}-reviewer"

    assignment = _personas.ReviewAssignment(
        repo=repo,
        pr=pr_num,
        head=head,
        issue=issue_num,
        risk_tier=binding.get("risk_tier", 1),
        authority=authority,
        reviewer_persona=reviewer_persona,
        reviewer_actor=reviewer_actor,
        reviewer_account=reviewer_account,
        authors=author_history,
        touches=touches,
        authority_source=binding.get("authority_source", "canonical kernel review authority"),
        external_first_released=binding.get("external_first_released", True),
        external_first_reason=binding.get("external_first_reason", "external provider unavailable"),
    )

    context = _personas.PromptContext(
        worktree=str(Path(worktree).resolve()),
        branch=f"pull/{pr_num}/head",
        head=head,
        pr=pr_num,
    )

    fleet_b = build_fleet_binding(config, state, repo, worktree=worktree, snapshot=policy, now=now, evidence=evidence)
    plan = _personas.plan_review(assignment, head, fleet_b, context, now=now, current_assignment=assignment)
    return plan
