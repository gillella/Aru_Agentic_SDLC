"""Deterministic resolution from a classified task to one validated plan.

The walk is ordered, pre-approved data, never a search:

1. classify the task from trusted metadata alone;
2. take the task family's ordered candidate list - or exactly the pinned
   persona, if the caller supplied an override;
3. for each candidate, in the operator's declared account order, apply every
   gate: role qualification, risk ceiling, author lineage, modality, optional
   enablement, effort support, project allowlist, shared capacity and exact
   expiring probe evidence;
4. return the first candidate that clears all of them, recording why each
   earlier candidate was skipped;
5. block only when the whole approved chain is unusable.

Step 4 is what Aru's amendment asks for: a Fable outage moves architecture work
to Astra and then to Opus, with the Chief Architect contract intact, instead of
stopping the work. Step 3 is what keeps that safe: a fallback candidate has to
pass exactly the same exact-model, exact-effort, account and capacity gates as
the preferred one. Nothing here creates review authority, buys credits, changes
an account, retries a provider or starts a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .lineage import extend_history

from . import catalog
from .binding import AccountBinding, FleetBinding
from .classify import Classification, TaskRequest, classify
from .errors import (
    AccountScopeError, CapabilityError, HarnessBindingError, IncompatibleOverrideError,
    ModalityError, NoEligibleCandidateError, OptionalPersonaError, PersonaPolicyError,
    ReviewAuthorityError, RiskFloorError, RoleQualificationError,
)
from .evidence import ProbeRecord
from .plan import CommandPlan, SkippedCandidate, build_argv, policy_source_digest
from .prompt import PromptContext, render
from .registry import REGISTRY_VERSION, Persona

#: Refusals that mean "eligible identity, unusable right now". These are the
#: bounded-fallback triggers the amendment names.
AVAILABILITY_ERRORS = (CapabilityError,)


def _kind(error: PersonaPolicyError) -> str:
    return "availability" if isinstance(error, AVAILABILITY_ERRORS) else "policy"


@dataclass(frozen=True)
class Selection:
    """One candidate that cleared every gate, before the plan is rendered."""

    persona: Persona
    role_id: str
    account: AccountBinding
    model_id: str
    effort: str
    effort_reason: str
    probe: ProbeRecord


def _effort_for(item: Persona, role_id: str, classification: Classification,
                request: TaskRequest) -> tuple[str, str]:
    """Choose the effort, and say in one sentence why it is that one."""
    if request.effort_override is not None:
        if request.effort_override not in item.allowed_efforts:
            raise IncompatibleOverrideError(
                f"effort {request.effort_override!r} is not an approved variant for "
                f"{item.id} (approved: {', '.join(item.allowed_efforts)}); an override is "
                "refused, never translated to a different level"
            )
        return request.effort_override, "explicit effort override, validated against the route"
    effort = item.canonical_effort
    reason = f"{item.id} canonical effort"
    escalate = (request.major_unresolved_decision if role_id == "chief_architect" else
                (request.nontrivial or classification.risk_tier >= 3))
    if escalate and item.escalated_effort:
        effort = item.escalated_effort
        reason = ("major unresolved architectural decision" if role_id == "chief_architect"
                  else "explicit nontrivial work or tier 3 escalation")
    if classification.risk_tier >= 2 and role_id != "chief_architect" and item.id == "flash-qa":
        effort, reason = "high", "sensitive verification requires high effort"
    return effort, reason


def _gate(item: Persona, role_id: str, account: AccountBinding, request: TaskRequest,
          classification: Classification, binding: FleetBinding,
          now: datetime) -> Selection:
    """Every check that must pass before this candidate may carry the work."""
    policy = binding.snapshot
    if not item.performs(role_id):
        raise RoleQualificationError(
            f"{item.id} is not allowlisted to perform the {role_id} role"
        )
    if classification.risk_tier > item.max_risk_tier:
        raise RiskFloorError(
            f"risk tier {classification.risk_tier} is above the tier "
            f"{item.max_risk_tier} ceiling for {item.id}"
        )
    if role_id != "code_reviewer":
        extend_history(request.author_history, item.id, account.account_id, request.actor,
                       request.handoff_reason or ("approved architect availability fallback"
                                                 if role_id == "chief_architect" else ""),
                       snapshot=policy)
    if item.optional and (item.id not in binding.enabled_optional or not request.allow_optional):
        raise OptionalPersonaError("optional persona requires binding and task enablement")
    harness = binding.harness(item.route)
    harness.require_modalities(classification.required_modalities | item.required_modalities)
    effort, effort_reason = _effort_for(item, role_id, classification, request)
    if classification.risk_tier >= 2 and catalog.effort_rank(effort) < catalog.effort_rank("high"):
        raise RiskFloorError("sensitive tasks require at least high effort")
    if role_id == "chief_architect" and effort == "xhigh" and not request.major_unresolved_decision:
        raise IncompatibleOverrideError("architect xhigh requires a major unresolved decision")
    model_id = item.model_id(effort)
    if request.model_override is not None and request.model_override != model_id:
        raise IncompatibleOverrideError("model override disagrees with persona and effort")
    catalog.require_effort(item.route, model_id, effort,
                           effort_selection=policy.model_rule(item.route,
                                                              model_id).effort_selection)
    if account.policy.route != item.route:
        raise AccountScopeError(f"{account.account_id} does not serve the {item.route} route")
    if account.policy.lineage != item.lineage:
        raise AccountScopeError(
            f"{account.account_id} lineage {account.policy.lineage} does not match "
            f"{item.id} lineage {item.lineage}"
        )
    account.require_project(request.project)
    binding.require_capacity(account)
    probe = binding.evidence.require(account.account_id, item.route, model_id, effort, now,
                                     identity_digest=account.identity_digest,
                                     modalities=classification.required_modalities | item.required_modalities)
    return Selection(item, role_id, account, model_id, effort, effort_reason, probe)


def _candidates(classification: Classification, request: TaskRequest,
                policy) -> tuple[str, ...]:
    family = policy.task_class(classification.task_class)
    if request.persona_override is not None:
        pinned = policy.persona(request.persona_override)
        if pinned.id not in (*family.candidates, *family.override_candidates):
            raise IncompatibleOverrideError(
                f"{pinned.id} is not an approved candidate for {family.name} "
                f"(approved: {', '.join(family.candidates)}); an override selects among "
                "approved candidates and never adds authority a persona does not hold"
            )
        # A pin is a pin: it is validated in full, and it never opens a fallback
        # to some other persona the caller did not name.
        return (pinned.id,)
    if not family.availability_fallback:
        # Reviewer-style families never advance to another identity on their own:
        # that authority belongs to the kernel, not to this library.
        return family.candidates[:1]
    return family.candidates


def _resolve(request: TaskRequest, binding: FleetBinding, context: PromptContext,
            now: datetime | None = None) -> CommandPlan:
    """Return the one plan this request authorises, or refuse with every reason."""
    moment = now or datetime.now(timezone.utc)
    policy = binding.snapshot
    classification = classify(request, policy)
    family = policy.task_class(classification.task_class)
    candidates = _candidates(classification, request, policy)
    preferred = family.candidates[0]
    if not context.branch or not context.head or len(context.head) != 40 or any(c not in "0123456789abcdef" for c in context.head):
        raise HarnessBindingError("context requires branch and full lowercase commit head")
    if not Path(context.worktree).is_absolute():
        raise HarnessBindingError("context requires an absolute isolated worktree")
    for path in request.input_files:
        if not Path(path).is_absolute() or "\x00" in path or ".." in Path(path).parts:
            raise ModalityError("input files must be absolute literal paths")
    if "image" in classification.required_modalities and not request.input_files:
        raise ModalityError("image tasks require explicit input files")
    skipped: list[SkippedCandidate] = []
    selection: Selection | None = None

    for persona_id in candidates:
        item = policy.persona(persona_id)
        role_id = family.role or ("senior_implementer" if item.id == "sonnet-reviewer"
                                  else item.native_role)
        accounts = binding.accounts_for(item.route)
        if not accounts:
            skipped.append(SkippedCandidate(
                persona_id, None, "account-scope",
                f"no {item.route} account is bound in this fleet binding", "policy"))
            continue
        for account in accounts:
            try:
                selection = _gate(item, role_id, account, request, classification,
                                  binding, moment)
            except PersonaPolicyError as exc:
                skipped.append(SkippedCandidate(
                    persona_id, account.account_id, getattr(exc, "code", "persona-policy"),
                    str(exc), _kind(exc)))
                continue
            break
        if selection is not None:
            break

    if selection is None:
        raise NoEligibleCandidateError(
            f"every approved candidate for {family.name} was skipped: "
            + "; ".join(f"{s.persona}/{s.account_id or '-'} [{s.code}] {s.reason}"
                        for s in skipped),
            tuple(skipped),
        )

    return _compile(request, classification, selection, binding, context,
                    preferred, tuple(skipped), moment)


def _compile(request: TaskRequest, classification: Classification, selection: Selection,
             binding: FleetBinding, context: PromptContext, preferred: str,
             skipped: tuple[SkippedCandidate, ...], moment: datetime) -> CommandPlan:
    item, account = selection.persona, selection.account
    policy = binding.snapshot
    role = policy.role(selection.role_id)
    harness = binding.harness(item.route)
    if Path(harness.workspace).resolve() != Path(context.worktree).resolve():
        raise HarnessBindingError("command workspace disagrees with prompt context")
    fallback_reason = ""
    if item.id != preferred:
        earlier = [s for s in skipped if s.persona == preferred]
        fallback_reason = (earlier[-1].reason if earlier
                           else f"explicit approved assignment to {item.id}")
    prompt = render(
        item, role, request, classification, context,
        model_id=selection.model_id, effort=selection.effort,
        account_id=account.account_id, capacity_key=account.capacity_key,
        preferred_persona=preferred, fallback_reason=fallback_reason,
    )
    argv = build_argv(item.route, harness.executable, selection.model_id,
                      selection.effort, harness.workspace, prompt,
                      read_only=role.id == "code_reviewer", input_files=request.input_files,
                      snapshot=policy)
    return CommandPlan(
        registry_version=REGISTRY_VERSION,
        catalog_recorded_on=str(catalog.catalog_metadata().get("recorded_on", "")),
        source_digest=policy_source_digest(),
        policy_version=policy.version, policy_digest=policy.digest,
        policy_origin=policy.origin,
        created_at=moment.isoformat(),
        expires_at=min(moment + timedelta(seconds=60),
                       selection.probe.observed_at + binding.evidence.max_age).isoformat(),
        project=request.project, issue=request.issue,
        task_class=classification.task_class, risk_tier=classification.risk_tier,
        classification=classification.to_dict(), touches=tuple(request.touches),
        preferred_persona=preferred, persona=item.id,
        effective_role=role.id, acting=role.id != item.native_role,
        fallback_reason=fallback_reason, skipped=skipped,
        route=item.route, model_id=selection.model_id, effort=selection.effort,
        effort_reason=selection.effort_reason,
        account_id=account.account_id, capacity_key=account.capacity_key,
        lineage=item.lineage, executable=harness.executable,
        workspace=harness.workspace, env=dict(account.env),
        argv=argv, prompt=prompt,
        author_history=tuple(a.to_dict() for a in (
            request.author_history if role.id == "code_reviewer" else
            extend_history(request.author_history, item.id, account.account_id, request.actor,
                           request.handoff_reason or ("approved architect availability fallback"
                                                     if role.id == "chief_architect" else ""),
                           snapshot=policy))),
        context={"worktree": context.worktree, "branch": context.branch, "head": context.head,
                 "pr": context.pr},
        input_files=request.input_files,
        evidence={
            "route": selection.probe.route,
            "identity_digest": selection.probe.identity_digest,
            "authenticated": selection.probe.authenticated,
            "modalities": sorted(selection.probe.modalities),
            "account_id": selection.probe.account_id,
            "model_id": selection.probe.model_id,
            "effort": selection.probe.effort,
            "outcome": selection.probe.outcome,
            "observed_at": selection.probe.observed_at.isoformat(),
            "age_seconds": int(selection.probe.age(moment).total_seconds()),
            "source": selection.probe.source,
        },
    )


def resolve(request: TaskRequest, binding: FleetBinding, context: PromptContext,
            now: datetime | None = None) -> CommandPlan:
    family = classify(request, binding.snapshot).task_class
    if binding.snapshot.task_class(family).role == "code_reviewer":
        raise ReviewAuthorityError("review requires plan_review and a current kernel assignment")
    return _resolve(request, binding, context, now)


def dry_run(request: TaskRequest, binding: FleetBinding, context: PromptContext,
            now: datetime | None = None) -> dict:
    """Explain the walk without raising, for `explain` and operator diagnosis."""
    try:
        plan = resolve(request, binding, context, now)
    except NoEligibleCandidateError as exc:
        return {"selected": None, "blocked": str(exc),
                "skipped": [s.to_dict() for s in exc.skipped]}
    except PersonaPolicyError as exc:
        return {"selected": None, "blocked": str(exc),
                "code": getattr(exc, "code", "persona-policy"), "skipped": []}
    return {"selected": plan.persona, "plan": plan.to_dict(),
            "skipped": [s.to_dict() for s in plan.skipped]}
