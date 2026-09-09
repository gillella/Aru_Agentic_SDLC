"""Deterministic classification from explicit, trusted task metadata.

Routing never reads free prose. A task is classified from an explicit task
class or from trusted kernel-issued labels, and its risk tier must agree with
the tier derived from the declared ``touches`` paths. Anything missing,
ambiguous or contradictory is refused rather than guessed. The advisory
``title`` is recorded for humans and is never an input to any decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from .catalog import MODALITIES
from .errors import (
    ContradictoryTaskError, RiskEvidenceError, UnknownTaskError, UnsafeScopeError,
)
from .registry import PROJECT_RE, TASK_CLASSES, task_class

TASK_LABEL_PREFIX = "aru-task:"
RISK_LABEL_PREFIX = "aru-risk:"

from .risk import review_risk_tier
from .lineage import AuthorIdentity


def tier_for_path(path: str) -> int:
    if (not isinstance(path, str) or not path or path.startswith(("/", "~", "-"))
            or ".." in path.split("/") or "//" in path
            or not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9._/-]*", path)
            or any(part == "." for part in path.rstrip("/").split("/"))):
        raise UnsafeScopeError(f"unsafe declared path: {path!r}")
    return review_risk_tier((path,))


def derive_risk_tier(touches: tuple[str, ...]) -> int:
    if not touches:
        raise UnsafeScopeError("nonempty declared touches required")
    return max(tier_for_path(path) for path in touches)


@dataclass(frozen=True)
class TaskRequest:
    """Trusted metadata about one unit of work. Prose is never routed on."""

    project: str
    issue: int
    #: The explicit approved task family, when the caller knows it.
    task_class: str | None = None
    #: Trusted kernel labels. Only ``aru-task:`` and ``aru-risk:`` are read.
    labels: tuple[str, ...] = ()
    #: Kernel path-derived tier, when the caller has already computed it.
    risk_tier: int | None = None
    touches: tuple[str, ...] = ()
    modalities: frozenset[str] = field(default=frozenset({"text"}))
    #: A pin, not a hint: an override is validated in full or refused. It never
    #: enables an availability fallback to a different persona.
    persona_override: str | None = None
    effort_override: str | None = None
    #: Architect-role escalation trigger. Effort still has to be supported by the
    #: exact selected route; it is recorded, never silently translated.
    major_unresolved_decision: bool = False
    #: Optional specialists (currently Spark) stay refused unless enabled here.
    allow_optional: bool = False
    author_history: tuple[AuthorIdentity, ...] = ()
    actor: str = ""
    handoff_reason: str = ""
    model_override: str | None = None
    nontrivial: bool = False
    input_files: tuple[str, ...] = ()
    #: Advisory only. Recorded in the plan for humans; never used for routing.
    title: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.project, str) or not PROJECT_RE.fullmatch(self.project):
            raise UnsafeScopeError(f"project must be a literal owner/repository: {self.project!r}")
        if type(self.issue) is not int or self.issue < 1:
            raise UnsafeScopeError("issue must be a positive integer")


@dataclass(frozen=True)
class Classification:
    task_class: str
    risk_tier: int
    #: Field name -> how it was established. Every routing input is explainable.
    sources: Mapping[str, str]
    derived_tier: int | None
    required_modalities: frozenset[str]

    def explain(self) -> tuple[str, ...]:
        return tuple(f"{name}: {reason}" for name, reason in sorted(self.sources.items()))

    def to_dict(self) -> dict:
        return {
            "task_class": self.task_class,
            "risk_tier": self.risk_tier,
            "derived_tier": self.derived_tier,
            "required_modalities": sorted(self.required_modalities),
            "sources": dict(sorted(self.sources.items())),
        }


def _labelled(labels: tuple[str, ...], prefix: str) -> tuple[str, ...]:
    seen = [label[len(prefix):] for label in labels if label.startswith(prefix)]
    return tuple(dict.fromkeys(seen))


def _classify_family(request: TaskRequest) -> tuple[str, str]:
    labelled = _labelled(request.labels, TASK_LABEL_PREFIX)
    if len(labelled) > 1:
        raise ContradictoryTaskError(
            "trusted labels name more than one task class: " + ", ".join(sorted(labelled))
        )
    if request.task_class is not None:
        if request.task_class not in TASK_CLASSES:
            raise UnknownTaskError(
                f"{request.task_class!r} is not an approved task class "
                f"(approved: {', '.join(sorted(TASK_CLASSES))})"
            )
        if labelled and labelled[0] != request.task_class:
            raise ContradictoryTaskError(
                f"explicit task class {request.task_class!r} contradicts trusted label "
                f"{TASK_LABEL_PREFIX}{labelled[0]}"
            )
        return request.task_class, "explicit request field"
    if labelled:
        if labelled[0] not in TASK_CLASSES:
            raise UnknownTaskError(f"{TASK_LABEL_PREFIX}{labelled[0]} is not an approved task class")
        return labelled[0], f"trusted label {TASK_LABEL_PREFIX}{labelled[0]}"
    raise UnknownTaskError(
        "no explicit task class and no trusted aru-task label; prose is never classified"
    )


def _classify_risk(request: TaskRequest) -> tuple[int, str, int | None]:
    labelled = _labelled(request.labels, RISK_LABEL_PREFIX)
    if len(labelled) > 1:
        raise ContradictoryTaskError(
            "trusted labels name more than one risk tier: " + ", ".join(sorted(labelled))
        )
    declared: int | None = request.risk_tier
    source = "explicit request field"
    if declared is not None and (type(declared) is not int or not 0 <= declared <= 3):
        raise RiskEvidenceError(f"risk tier must be 0-3, got {declared!r}")
    if declared is None and labelled:
        try:
            declared = int(labelled[0])
        except ValueError as exc:
            raise RiskEvidenceError(
                f"{RISK_LABEL_PREFIX}{labelled[0]} is not a risk tier"
            ) from exc
        if not 0 <= declared <= 3:
            raise RiskEvidenceError(f"risk tier must be 0-3, got {declared!r}")
        source = f"trusted label {RISK_LABEL_PREFIX}{labelled[0]}"
    elif declared is not None and labelled and str(declared) != labelled[0]:
        raise ContradictoryTaskError(
            f"explicit risk tier {declared} contradicts trusted label "
            f"{RISK_LABEL_PREFIX}{labelled[0]}"
        )
    derived = derive_risk_tier(request.touches)
    if declared is None:
        if derived is None:
            raise RiskEvidenceError(
                "risk tier is neither declared nor derivable; declare a tier or touches"
            )
        return derived, "derived from declared touches", derived
    if derived is not None and declared < derived:
        raise RiskEvidenceError(
            f"declared risk tier {declared} is below the tier {derived} derived from the "
            "declared touches; evidence fails upward and is never routed down"
        )
    return declared, source, derived


def classify(request: TaskRequest) -> Classification:
    """Refuse anything that is not explainable from trusted metadata."""
    for flag in (request.major_unresolved_decision, request.allow_optional, request.nontrivial):
        if type(flag) is not bool:
            raise ContradictoryTaskError("task switches must be booleans")
    if request.major_unresolved_decision and request.task_class not in (None, "architecture_decision"):
        raise ContradictoryTaskError("major unresolved decision applies only to architecture")
    family, family_source = _classify_family(request)
    if request.major_unresolved_decision and family != "architecture_decision":
        raise ContradictoryTaskError("major unresolved decision applies only to architecture")
    tier, tier_source, derived = _classify_risk(request)
    if not request.modalities or not set(request.modalities) <= MODALITIES:
        raise UnknownTaskError(
            f"task modalities must be a non-empty subset of {sorted(MODALITIES)}"
        )
    return Classification(
        task_class=family,
        risk_tier=tier,
        sources={
            "task_class": family_source,
            "risk_tier": tier_source,
            "touches": ("declared: " + ", ".join(request.touches)) if request.touches
                       else "not declared",
            "modalities": ", ".join(sorted(request.modalities)),
        },
        derived_tier=derived,
        required_modalities=frozenset(request.modalities)
                            | task_class(family).required_modalities,
    )
