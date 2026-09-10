"""The 13 approved persona identities, their roles, task families and accounts.

This module is data. It encodes exactly the fleet Aru approved on 2026-09-09
and nothing else: no alias, no experimental pilot model, no legacy standby.
Every model identifier and effort here is checked against the recorded route
catalog by :mod:`personas.catalog` before a plan is compiled.

Two separable ideas live here:

* a **role** is the work contract - scope, output deliverables and escalation
  rules - and it belongs to the task, not to a vendor;
* a **persona** is one stable identity bound to one route, one canonical model
  and a bounded set of efforts, which performs its own native role and, where
  Aru explicitly allowlisted it, one named secondary role.

The architect secondary role has an explicitly approved availability chain:
availability chain Fable 5.1 -> GPT-6 Astra -> Claude Opus 5 keeps the Chief
Architect contract intact when the preferred model is unusable. That is an
explicit, ordered, role-qualified fallback. Other task families declare their
own qualified candidates; review remains pinned to kernel assignment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from . import catalog
from .errors import (
    AccountScopeError, PersonaPolicyError, UnknownPersonaError, UnknownRoleError,
    UnknownTaskError,
)

REGISTRY_VERSION = "1.0.0"

PROJECT_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

_COMMON_STOP = (
    "Stop and report when the live issue, claim, head, scope or acceptance criteria changed.",
    "Stop rather than widen the declared touches boundary or start a second writer.",
    "A process exiting zero is not evidence of completed work; report actual artefacts.",
)

_NO_LIFECYCLE = (
    "Never merge, deploy, release, change credentials, or operate production systems.",
    "Never edit lifecycle or review labels, or manufacture review approval.",
)


@dataclass(frozen=True)
class Role:
    """One work contract: what to do, what to emit, when to escalate, when to stop.

    A role is stable across whichever approved persona performs it. That is what
    makes the architect fallback safe: the deliverables do not change when the
    acting model does.
    """

    id: str
    title: str
    scope: str
    output_contract: tuple[str, ...]
    escalation: tuple[str, ...]
    stop_criteria: tuple[str, ...] = _COMMON_STOP + _NO_LIFECYCLE

    def sections(self) -> Mapping[str, tuple[str, ...]]:
        return {
            "OUTPUT CONTRACT": self.output_contract,
            "ESCALATION": self.escalation,
            "STOP CRITERIA": self.stop_criteria,
        }


ROLES: Mapping[str, Role] = {r.id: r for r in (
    Role(
        id="chief_architect", title="Chief Architect / Strategic Orchestrator",
        scope=("Decide architecture, system boundaries, invariants, trade-offs and task "
               "decomposition. Produce decision records and implementation contracts. "
               "This is not routine status work and not ordinary implementation."),
        output_contract=(
            "A decision record: problem, alternatives considered, choice and rationale.",
            "The invariants that must hold, and why each one holds.",
            "An implementation contract per work package: scope, interface, acceptance "
            "criteria and the evidence that package must produce.",
            "Explicit escalation triggers for the work packages you hand off.",
        ),
        escalation=(
            "Use xhigh effort only for a major unresolved decision, and only where the "
            "exact selected route documents that level.",
            "Escalate to the operator when the decision needs new spend, scope or approval.",
        ),
        stop_criteria=_COMMON_STOP + _NO_LIFECYCLE + (
            "Stop if asked to approve code, a merge, or a release: strategic approval is "
            "not code-review approval and never substitutes for it.",
        ),
    ),
    Role(
        id="principal_implementer", title="Principal Implementation Engineer",
        scope=("Implement complex full-stack features, brownfield refactoring and multi-file "
               "integration inside one declared touches boundary."),
        output_contract=(
            "A minimal diff, the focused verification actually run, and its real output.",
            "Remaining risks, and the exact branch and head the evidence is bound to.",
        ),
        escalation=(
            "Escalate to xhigh only for tier 3 work or a recorded hard-debugging blocker.",
            "Escalate to an architecture decision when an invariant is unresolved.",
        ),
    ),
    Role(
        id="lead_systems_implementer", title="Lead Systems Implementer",
        scope=("Implement security and authentication, transactions and trading, migrations, "
               "concurrency, shared interfaces and difficult debugging inside one declared "
               "touches boundary."),
        output_contract=(
            "A minimal diff, the focused verification actually run, and its real output.",
            "Explicit threat or failure reasoning for the security or concurrency change.",
            "Remaining risks, and the exact branch and head the evidence is bound to.",
        ),
        escalation=(
            "Escalate to xhigh for tier 3 work or a recorded hard-debugging blocker.",
            "Escalate to an architecture decision when a shared interface must change.",
        ),
    ),
    Role(
        id="senior_implementer", title="Senior Implementation Engineer",
        scope=("Implement ordinary bounded features, API and backend integrations, and "
               "test-backed fixes with a clear specification."),
        output_contract=(
            "A minimal diff, the focused tests actually run, and their real output.",
            "Remaining risks, and the exact branch and head the evidence is bound to.",
        ),
        escalation=(
            "Escalate to the Lead Systems Implementer when the work turns out to be "
            "security, migration or concurrency sensitive, or reaches risk tier 3.",
        ),
    ),
    Role(
        id="maintenance_engineer", title="Maintenance Engineer",
        scope="Apply small, clearly specified changes, routine tests, maintenance and narrow refactors.",
        output_contract=(
            "A minimal diff, the focused tests actually run, and their real output.",
            "An explicit statement when the specification was ambiguous.",
        ),
        escalation=(
            "Escalate to high effort for a nontrivial fix inside the same risk tier.",
            "Escalate to an implementer role as soon as the change reaches risk tier 2.",
        ),
    ),
    Role(
        id="repository_scout", title="Repository Scout / Task Assistant",
        scope=("Locate code, summarise logs, collect evidence, map dependencies and prepare "
               "handoffs for another persona."),
        output_contract=(
            "Exact file paths and line ranges for every claim, with quoted evidence.",
            "A handoff note naming what is known, what is unknown and what to read next.",
        ),
        escalation=(
            "Escalate to medium effort for a wide or ambiguous search.",
            "Escalate to an implementer role as soon as a code change is required.",
        ),
    ),
    Role(
        id="code_reviewer", title="Code Reviewer / Quality Engineer",
        scope=("Perform one independent review bound to one exact head: acceptance gaps, "
               "regressions and concrete findings."),
        output_contract=(
            "Concrete findings with file and line, each with the defect and its consequence.",
            "One verdict bound to the exact head reviewed, or an explicit blocker.",
        ),
        escalation=(
            "Escalate to an independent frontier reviewer when the change is risk tier 2-3.",
            "Escalate rather than approve when evidence is missing, stale or contradictory.",
        ),
        stop_criteria=_COMMON_STOP + _NO_LIFECYCLE + (
            "A reviewer never edits the source under review, fixes findings, or pushes.",
            "Stop if the head advanced: never attest to a head you did not read.",
        ),
    ),
    Role(
        id="triage_assistant", title="Triage / Documentation Assistant",
        scope=("Classify issues, summarise, extract structured facts and write small "
               "documentation. Documentation-tier work only."),
        output_contract=(
            "The requested classification, summary or extraction, with its source quoted.",
            "An explicit 'unknown' rather than a guess when the source does not say.",
        ),
        escalation=(
            "Escalate any task that turns out to touch code, risk or acceptance criteria.",
        ),
    ),
    Role(
        id="qa_automation", title="QA Automation / Integration Engineer",
        scope=("Reproduce defects, implement tests, verify acceptance criteria, inspect "
               "integration paths and carry bounded coding for verification."),
        output_contract=(
            "A reproducer, the tests added, and the observed results before and after.",
            "An explicit statement when a criterion could not be verified, and why.",
        ),
        escalation=(
            "Escalate to high effort for difficult verification inside the same risk tier.",
            "Escalate to a systems implementer for tier 3 or production-path work.",
        ),
        stop_criteria=_COMMON_STOP + _NO_LIFECYCLE + (
            "QA evidence is not review authority and never becomes an approval.",
        ),
    ),
    Role(
        id="product_frontend", title="Product / Frontend Implementer",
        scope=("Build interactive apps, visual prototypes, product experiments and longer "
               "frontend implementation."),
        output_contract=(
            "The implemented UI or prototype, and how it was exercised locally.",
            "Explicit notes on anything mocked, stubbed or left unwired.",
        ),
        escalation=(
            "Escalate to an implementer role when backend or contract code is required.",
        ),
    ),
    Role(
        id="rapid_fix", title="Rapid Fix / UI Engineer",
        scope="Small UI changes, tightly scoped bug fixes and repetitive coding.",
        output_contract=(
            "The single scoped change and the check that exercised it.",
        ),
        escalation=(
            "Escalate to a product or implementer role when the change grows.",
        ),
    ),
    Role(
        id="realtime_pair", title="Realtime Pair Programmer",
        scope="Small interactive edits in a tight operator feedback loop.",
        output_contract=(
            "The single edit made and the immediate check that exercised it.",
        ),
        escalation=(
            "Escalate to an implementer or maintenance role as soon as the change leaves "
            "one small edit.",
        ),
    ),
    Role(
        id="design_analyst", title="Multimodal / Design Analyst",
        scope=("Analyse reference designs, diagrams and long documents, and translate that "
               "evidence into an implementation specification."),
        output_contract=(
            "A specification citing each visual or document source it was derived from.",
            "An explicit list of what the evidence does not determine.",
        ),
        escalation=(
            "Escalate to the operator when required design evidence is missing or ambiguous.",
        ),
        stop_criteria=_COMMON_STOP + _NO_LIFECYCLE + (
            "Produce specifications only; hold no implementation authority.",
        ),
    ),
)}


def role(role_id: str) -> Role:
    try:
        return ROLES[role_id]
    except KeyError as exc:
        raise UnknownRoleError(f"unknown role: {role_id!r}") from exc


# --------------------------------------------------------------------------- #
# Task families
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TaskClass:
    """One approved family of work and the ordered personas that may carry it."""

    name: str
    summary: str
    #: Ordered, pre-approved candidates. ``candidates[0]`` is the preferred
    #: persona; later entries are consulted only when an earlier one is skipped
    #: for a recorded reason. The order is a policy declaration, never inferred.
    candidates: tuple[str, ...]
    #: When set, every candidate performs *this* role and must be role-qualified
    #: for it. When unset each candidate performs its own native role.
    role: str | None = None
    #: Whether an availability skip (quota, credits, entitlement, occupied
    #: capacity, failed probe) may advance to the next candidate. Reviewer
    #: assignment never does: its authority is the kernel's.
    availability_fallback: bool = True
    override_candidates: tuple[str, ...] = ()
    required_modalities: frozenset[str] = frozenset({"text"})


TASK_CLASSES: Mapping[str, TaskClass] = {t.name: t for t in (
    TaskClass("architecture_decision",
              "System boundaries, invariants, trade-offs, decomposition, decision records.",
              ("fable-architect", "astra-implementer", "opus-implementer"),
              role="chief_architect"),
    TaskClass("complex_implementation",
              "Complex full-stack features, brownfield refactoring, multi-file integration.",
              ("opus-implementer", "astra-implementer"), role="principal_implementer"),
    TaskClass("security_implementation",
              "Security/auth, transactions/trading, migrations, concurrency, shared interfaces.",
              ("astra-implementer", "opus-implementer"), role="lead_systems_implementer"),
    TaskClass("bounded_implementation",
              "Ordinary bounded features, API/backend integration, test-backed fixes.",
              ("sol-implementer", "opus-implementer", "astra-implementer"),
              override_candidates=("sonnet-reviewer",), role="senior_implementer"),
    TaskClass("maintenance_fix",
              "Small clearly specified changes, routine tests, narrow refactors.",
              ("terra-maintainer", "sol-implementer"), role="maintenance_engineer"),
    TaskClass("repository_scout",
              "Locate code, summarise logs, collect evidence, map dependencies, prepare handoffs.",
              ("luna-scout",)),
    TaskClass("triage_documentation",
              "Issue classification, summaries, structured extraction, small documentation.",
              ("haiku-triage",)),
    TaskClass("code_review",
              "Independent review of an exact head: acceptance gaps, regressions, findings.",
              ("sonnet-reviewer", "opus-implementer", "astra-implementer"),
              role="code_reviewer", availability_fallback=False),
    TaskClass("qa_verification",
              "Reproduce defects, implement tests, verify acceptance, inspect integration paths.",
              ("flash-qa", "astra-implementer"), role="qa_automation"),
    TaskClass("frontend_implementation",
              "Interactive apps, visual prototypes, product experiments, frontend work.",
              ("grok-frontend",)),
    TaskClass("rapid_ui_fix",
              "Small UI changes, scoped bug fixes, repetitive coding.",
              ("composer-fixer",)),
    TaskClass("interactive_pair_edit",
              "Interactive small edits in a tight feedback loop.",
              ("spark-pair",)),
    TaskClass("design_evidence_analysis",
              "Reference designs, diagrams and long documents translated into specs.",
              ("pro-design",), required_modalities=frozenset({"text", "image"})),
)}


def task_class(name: str) -> TaskClass:
    try:
        return TASK_CLASSES[name]
    except KeyError as exc:
        raise UnknownTaskError(f"unknown task class: {name!r}") from exc


# --------------------------------------------------------------------------- #
# Personas
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Persona:
    """One stable fleet identity bound to one route, model family and effort set."""

    id: str
    display: str
    route: str
    #: Effort -> canonical route model identifier. Routes whose effort lives in
    #: a flag or config key map every allowed effort onto one identifier; cursor
    #: and antigravity encode effort in the identifier itself.
    model_ids: Mapping[str, str]
    canonical_effort: str
    allowed_efforts: tuple[str, ...]
    escalated_effort: str | None
    escalate_at_tier: int
    #: Highest kernel risk tier this persona may carry. Tier 2-3 work can never
    #: reach a scout, maintenance, triage, UI or pair-programming persona.
    max_risk_tier: int
    native_role: str
    #: Roles Aru explicitly allowlisted this persona to perform in addition to
    #: its own. Includes explicitly qualified architect, reviewer and bounded implementation roles.
    acting_roles: tuple[str, ...]
    primary_task: str
    also_eligible: tuple[str, ...]
    #: "none" | "routine" (tier <= 1) | "high_risk" (tier <= 3)
    review_scope: str
    #: Shared quota/independence lineage. Two personas on the same lineage can
    #: never review each other, whatever account or display name is used.
    lineage: str
    authority_boundary: tuple[str, ...]
    #: Optional specialists stay refused unless the operator enables them *and*
    #: a fresh successful exact probe exists.
    optional: bool = False
    required_modalities: frozenset[str] = field(default=frozenset({"text"}))
    notes: str = ""

    @property
    def title(self) -> str:
        return role(self.native_role).title

    @property
    def role_ids(self) -> tuple[str, ...]:
        return (self.native_role, *self.acting_roles)

    def performs(self, role_id: str) -> bool:
        return role_id in self.role_ids

    def model_id(self, effort: str) -> str:
        try:
            return self.model_ids[effort]
        except KeyError as exc:
            raise PersonaPolicyError(
                f"{self.id} has no {effort!r} route identifier"
            ) from exc

    def reviews_at(self, risk_tier: int) -> bool:
        if self.review_scope == "high_risk":
            return 0 <= risk_tier <= 3
        if self.review_scope == "routine":
            return 0 <= risk_tier <= 1
        return False


def _same(model: str, efforts: tuple[str, ...]) -> Mapping[str, str]:
    """Routes whose effort is a flag or config key keep one model identifier."""
    return {effort: model for effort in efforts}


_ARCHITECT_FALLBACK_NOTE = (
    "Explicitly allowlisted acting Chief Architect in the approved availability chain "
    "Fable 5.1 -> Astra -> Opus 5. Acting means performing the full Chief Architect "
    "contract, not narrowing it to implementation."
)

PERSONAS: Mapping[str, Persona] = {p.id: p for p in (
    Persona(
        id="fable-architect", display="Claude Fable 5.1", route="claude-code",
        model_ids=_same("claude-fable-5-1", ("high", "xhigh")),
        canonical_effort="high", allowed_efforts=("high", "xhigh"),
        escalated_effort="xhigh", escalate_at_tier=2, max_risk_tier=3,
        native_role="chief_architect", acting_roles=(),
        primary_task="architecture_decision", also_eligible=(),
        review_scope="none", lineage="anthropic-claude",
        authority_boundary=(
            "Strategic approval is not code-review approval and never substitutes for it.",
            "Cannot override failed exact-head evidence, risk tiers or operator approvals.",
            "Holds no merge, review-verdict or lifecycle authority.",
        ),
        notes="Invoke at architecture checkpoints for material new uncertainty, not per PR.",
    ),
    Persona(
        id="opus-implementer", display="Claude Opus 5", route="claude-code",
        model_ids=_same("claude-opus-5", ("high", "xhigh")),
        canonical_effort="high", allowed_efforts=("high", "xhigh"),
        escalated_effort="xhigh", escalate_at_tier=3, max_risk_tier=3,
        native_role="principal_implementer", acting_roles=("chief_architect", "code_reviewer", "lead_systems_implementer", "senior_implementer"),
        primary_task="complex_implementation",
        also_eligible=("architecture_decision", "security_implementation",
                       "bounded_implementation", "code_review"),
        review_scope="high_risk", lineage="anthropic-claude",
        authority_boundary=(
            "Writes only inside the issue's declared touches and its isolated worktree.",
            "Owns remediation of its own findings; becoming an author forfeits review authority.",
            "Holds no merge or lifecycle authority; the kernel helpers own transitions.",
        ),
        notes=_ARCHITECT_FALLBACK_NOTE,
    ),
    Persona(
        id="sonnet-reviewer", display="Claude Sonnet 5", route="claude-code",
        model_ids=_same("claude-sonnet-5", ("high",)),
        canonical_effort="high", allowed_efforts=("high",),
        escalated_effort=None, escalate_at_tier=2, max_risk_tier=1,
        native_role="code_reviewer", acting_roles=("senior_implementer",),
        primary_task="code_review", also_eligible=("bounded_implementation",),
        review_scope="routine", lineage="anthropic-claude",
        authority_boundary=(
            "Routine review only: tier 2-3 review requires an independent frontier reviewer.",
            "A reviewer never edits the source under review, fixes findings, or pushes.",
            "Cannot select, assign or mutate kernel reviewer labels or authority.",
        ),
    ),
    Persona(
        id="haiku-triage", display="Claude Haiku 4.5", route="claude-code",
        model_ids=_same("claude-haiku-4-5-20251001", ("default",)),
        canonical_effort="default", allowed_efforts=("default",),
        escalated_effort=None, escalate_at_tier=2, max_risk_tier=0,
        native_role="triage_assistant", acting_roles=(),
        primary_task="triage_documentation", also_eligible=(),
        review_scope="none", lineage="anthropic-claude",
        authority_boundary=(
            "No architecture, review-verdict, approval or merge authority of any kind.",
            "No override, label, role name or account change can grant that authority.",
            "Never edits code paths outside documentation and issue metadata drafts.",
        ),
        notes="No effort flag is emitted for this persona; it runs at the route default.",
    ),
    Persona(
        id="astra-implementer", display="GPT-6 Astra", route="codex",
        model_ids=_same("gpt-6-astra", ("high", "xhigh")),
        canonical_effort="high", allowed_efforts=("high", "xhigh"),
        escalated_effort="xhigh", escalate_at_tier=3, max_risk_tier=3,
        native_role="lead_systems_implementer", acting_roles=("chief_architect", "code_reviewer", "principal_implementer", "senior_implementer", "qa_automation"),
        primary_task="security_implementation",
        also_eligible=("architecture_decision", "complex_implementation",
                       "bounded_implementation", "qa_verification", "code_review"),
        review_scope="high_risk", lineage="openai-codex",
        authority_boundary=(
            "XHigh is the same model with more reasoning: it is not a second worker or lane.",
            "Shares the Codex quota and lineage with Sol, Terra, Luna and Spark.",
            "Writes only inside the issue's declared touches and its isolated worktree.",
            "Holds no merge or lifecycle authority; never buys credits or changes auth.",
        ),
        notes=_ARCHITECT_FALLBACK_NOTE,
    ),
    Persona(
        id="sol-implementer", display="GPT-5.6 Sol", route="codex",
        model_ids=_same("gpt-5.6-sol", ("high",)),
        canonical_effort="high", allowed_efforts=("high",),
        escalated_effort=None, escalate_at_tier=2, max_risk_tier=2,
        native_role="senior_implementer", acting_roles=("maintenance_engineer",),
        primary_task="bounded_implementation", also_eligible=("maintenance_fix",),
        review_scope="none", lineage="openai-codex",
        authority_boundary=(
            "Shares the Codex quota and lineage with Astra, Terra, Luna and Spark.",
            "Holds no review-verdict, merge or lifecycle authority.",
            "Writes only inside the issue's declared touches and its isolated worktree.",
        ),
    ),
    Persona(
        id="terra-maintainer", display="GPT-5.6 Terra", route="codex",
        model_ids=_same("gpt-5.6-terra", ("medium", "high")),
        canonical_effort="medium", allowed_efforts=("medium", "high"),
        escalated_effort="high", escalate_at_tier=1, max_risk_tier=1,
        native_role="maintenance_engineer", acting_roles=(),
        primary_task="maintenance_fix", also_eligible=(),
        review_scope="none", lineage="openai-codex",
        authority_boundary=(
            "Never carries sensitive/contract or production work: risk tier 0-1 only.",
            "Shares the Codex quota and lineage with Astra, Sol, Luna and Spark.",
            "Holds no review-verdict, merge or lifecycle authority.",
        ),
    ),
    Persona(
        id="luna-scout", display="GPT-5.6 Luna", route="codex",
        model_ids=_same("gpt-5.6-luna", ("low", "medium")),
        canonical_effort="low", allowed_efforts=("low", "medium"),
        escalated_effort="medium", escalate_at_tier=1, max_risk_tier=1,
        native_role="repository_scout", acting_roles=(),
        primary_task="repository_scout", also_eligible=(),
        review_scope="none", lineage="openai-codex",
        authority_boundary=(
            "Investigation and handoff preparation only; no design or approval authority.",
            "Never carries sensitive/contract or production work: risk tier 0-1 only.",
            "Shares the Codex quota and lineage with Astra, Sol, Terra and Spark.",
        ),
    ),
    Persona(
        id="spark-pair", display="GPT-5.3-Codex-Spark", route="codex",
        model_ids=_same("gpt-5.3-codex-spark", ("high",)),
        canonical_effort="high", allowed_efforts=("high",),
        escalated_effort=None, escalate_at_tier=2, max_risk_tier=1,
        native_role="realtime_pair", acting_roles=(),
        primary_task="interactive_pair_edit", also_eligible=(),
        review_scope="none", lineage="openai-codex",
        authority_boundary=(
            "Optional specialist: refused unless the operator enables it and a fresh "
            "successful exact-model probe exists.",
            "Never carries sensitive/contract or production work: risk tier 0-1 only.",
            "Holds no review-verdict, merge or lifecycle authority.",
        ),
        optional=True,
        notes="Catalog-visible and never live-probed in the recorded report.",
    ),
    Persona(
        id="grok-frontend", display="Cursor Grok 4.6", route="cursor",
        model_ids={"medium": "cursor-grok-4.6-medium", "high": "cursor-grok-4.6-high"},
        canonical_effort="high", allowed_efforts=("medium", "high"),
        escalated_effort=None, escalate_at_tier=2, max_risk_tier=1,
        native_role="product_frontend", acting_roles=(),
        primary_task="frontend_implementation", also_eligible=(),
        review_scope="none", lineage="cursor-fleet",
        authority_boundary=(
            "Shares the Cursor account quota with Composer; a model switch is not new capacity.",
            "Only high and medium are eligible variants; low and xhigh are not assigned.",
            "Never carries sensitive/contract or production work: risk tier 0-1 only.",
            "Holds no review-verdict, merge or lifecycle authority.",
        ),
    ),
    Persona(
        id="composer-fixer", display="Composer 2.5", route="cursor",
        model_ids={"default": "composer-2.5"},
        canonical_effort="default", allowed_efforts=("default",),
        escalated_effort=None, escalate_at_tier=2, max_risk_tier=1,
        native_role="rapid_fix", acting_roles=(),
        primary_task="rapid_ui_fix", also_eligible=(),
        review_scope="none", lineage="cursor-fleet",
        authority_boundary=(
            "Shares the Cursor account quota with Grok; a model switch is not new capacity.",
            "Never carries sensitive/contract or production work: risk tier 0-1 only.",
            "Holds no review-verdict, merge or lifecycle authority.",
        ),
        notes="The route publishes no effort variant; no effort selection is emitted.",
    ),
    Persona(
        id="flash-qa", display="Gemini 3.8 Flash", route="antigravity",
        model_ids={"medium": "gemini-3.8-flash-medium", "high": "gemini-3.8-flash-high"},
        canonical_effort="medium", allowed_efforts=("medium", "high"),
        escalated_effort="high", escalate_at_tier=2, max_risk_tier=2,
        native_role="qa_automation", acting_roles=(),
        primary_task="qa_verification", also_eligible=(),
        review_scope="none", lineage="google-antigravity",
        authority_boundary=(
            "Shares the Antigravity account quota with Gemini 3.1 Pro.",
            "QA evidence is not review authority and never becomes an approval.",
            "Holds no review-verdict, merge or lifecycle authority.",
        ),
    ),
    Persona(
        id="pro-design", display="Gemini 3.1 Pro", route="antigravity",
        model_ids={"high": "gemini-3.1-pro-high"},
        canonical_effort="high", allowed_efforts=("high",),
        escalated_effort=None, escalate_at_tier=2, max_risk_tier=1,
        native_role="design_analyst", acting_roles=(),
        primary_task="design_evidence_analysis", also_eligible=(),
        review_scope="none", lineage="google-antigravity",
        authority_boundary=(
            "Requires a harness with proven image input; refused otherwise, never downgraded.",
            "Shares the Antigravity account quota with Gemini 3.8 Flash.",
            "Produces specifications only; holds no implementation or merge authority.",
        ),
        required_modalities=frozenset({"text", "image"}),
        notes=("The installed agy CLI documents no local image attachment, so multimodal "
               "work stays refused until a harness binding declares proven image input."),
    ),
)}

assert len(PERSONAS) == 13, "the approved fleet is exactly 13 personas"


def persona(persona_id: str) -> Persona:
    try:
        return PERSONAS[persona_id]
    except KeyError as exc:
        raise UnknownPersonaError(f"unknown persona: {persona_id!r}") from exc


#: The one approved acting-role chain, kept here so it is auditable as data.
ARCHITECT_FALLBACK_CHAIN: tuple[str, ...] = TASK_CLASSES["architecture_decision"].candidates


# --------------------------------------------------------------------------- #
# Accounts, shared capacity and project scope
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AccountPolicy:
    """One billable identity and the rule that constrains what it may work on."""

    id: str
    route: str
    #: Every persona on this account draws from this one quota. Switching model
    #: inside a capacity key is never new capacity.
    capacity_key: str
    lineage: str
    description: str
    #: ``None`` means the operator declares the allowlist freely. A frozenset
    #: means the allowlist may contain only repositories under these owners.
    required_owners: frozenset[str] | None = None
    restriction: str = ""


UNUM_OWNER = "Unum-Inc"

ACCOUNTS: Mapping[str, AccountPolicy] = {a.id: a for a in (
    AccountPolicy("claude-subscription-1", "claude-code", "claude-subscription-1",
                  "anthropic-claude", "Personal Claude subscription 1."),
    AccountPolicy("claude-subscription-2", "claude-code", "claude-subscription-2",
                  "anthropic-claude", "Personal Claude subscription 2."),
    AccountPolicy("claude-subscription-3", "claude-code", "claude-subscription-3",
                  "anthropic-claude", "Personal Claude subscription 3."),
    AccountPolicy("claude-subscription-4", "claude-code", "claude-subscription-4",
                  "anthropic-claude", "Unum client Claude subscription.",
                  required_owners=frozenset({UNUM_OWNER}),
                  restriction=("Subscription 4 is the Unum client identity and is restricted "
                               f"to {UNUM_OWNER} repositories. Any other project is refused.")),
    AccountPolicy("openai-codex", "codex", "openai-codex", "openai-codex",
                  "One Codex subscription shared by Astra, Sol, Terra, Luna and Spark."),
    AccountPolicy("cursor-account", "cursor", "cursor-account", "cursor-fleet",
                  "One Cursor subscription shared by Grok 4.6 and Composer 2.5."),
    AccountPolicy("antigravity-account", "antigravity", "antigravity-account",
                  "google-antigravity",
                  "One Antigravity subscription shared by Gemini 3.8 Flash and 3.1 Pro."),
)}


def account(account_id: str) -> AccountPolicy:
    try:
        return ACCOUNTS[account_id]
    except KeyError as exc:
        raise AccountScopeError(f"unknown account: {account_id!r}") from exc


def accounts_for_route(route_name: str) -> tuple[str, ...]:
    return tuple(a.id for a in ACCOUNTS.values() if a.route == route_name)


def personas_for_capacity(capacity_key: str) -> tuple[str, ...]:
    """Every persona that draws on one shared quota. Used to explain contention."""
    routes = {a.route for a in ACCOUNTS.values() if a.capacity_key == capacity_key}
    return tuple(p.id for p in PERSONAS.values() if p.route in routes)


# --------------------------------------------------------------------------- #
# Self-validation
# --------------------------------------------------------------------------- #

def _validate_persona(item: Persona) -> None:
    catalog.route(item.route)
    role(item.native_role)
    for acting in item.acting_roles:
        role(acting)
    if item.canonical_effort not in item.allowed_efforts:
        raise PersonaPolicyError(f"{item.id}: canonical effort is not allowed")
    if item.escalated_effort and item.escalated_effort not in item.allowed_efforts:
        raise PersonaPolicyError(f"{item.id}: escalated effort is not allowed")
    if set(item.model_ids) != set(item.allowed_efforts):
        raise PersonaPolicyError(f"{item.id}: model identifiers and efforts disagree")
    if item.lineage != account(accounts_for_route(item.route)[0]).lineage:
        raise PersonaPolicyError(f"{item.id}: lineage disagrees with its route accounts")
    for effort, model in item.model_ids.items():
        catalog.require_effort(item.route, model, effort)
    for name in (item.primary_task, *item.also_eligible):
        task_class(name)
    if item.id not in task_class(item.primary_task).candidates:
        raise PersonaPolicyError(f"{item.id}: not listed by its own primary task class")
    if item.review_scope not in {"none", "routine", "high_risk"}:
        raise PersonaPolicyError(f"{item.id}: unknown review scope")
    if item.review_scope != "none" and "code_reviewer" not in item.role_ids:
        raise PersonaPolicyError(f"{item.id}: holds review scope without the reviewer role")


def validate_registry() -> None:
    """Prove every shipped persona is a real, assignable, self-consistent identity."""
    for item in PERSONAS.values():
        _validate_persona(item)
    for family in TASK_CLASSES.values():
        if not family.candidates:
            raise PersonaPolicyError(f"{family.name}: no approved candidate")
        for persona_id in (*family.candidates, *family.override_candidates):
            candidate = persona(persona_id)
            if family.role is not None and not candidate.performs(family.role):
                raise PersonaPolicyError(
                    f"{family.name}: {persona_id} is not role-qualified for {family.role}"
                )
            if family.name not in (candidate.primary_task, *candidate.also_eligible):
                raise PersonaPolicyError(
                    f"{family.name}: {persona_id} does not declare this family"
                )
    if ARCHITECT_FALLBACK_CHAIN != ("fable-architect", "astra-implementer", "opus-implementer"):
        raise PersonaPolicyError("the approved architect fallback chain was altered")
