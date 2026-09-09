"""Every refusal this package can raise.

The package is fail-closed: it either returns one fully validated plan or
raises. There is no partial plan, no silent substitution of a different
persona, model, effort or account, and no undocumented fallback.
"""

from __future__ import annotations


class PersonaPolicyError(RuntimeError):
    """Base class: a persona decision cannot safely proceed."""

    code = "persona-policy"


# -- classification -----------------------------------------------------------

class ClassificationError(PersonaPolicyError):
    code = "classification"


class UnknownTaskError(ClassificationError):
    """No trusted metadata names a supported task class."""

    code = "unknown-task"


class ContradictoryTaskError(ClassificationError):
    """Trusted metadata names more than one incompatible classification."""

    code = "contradictory-task"


class UnsafeScopeError(ClassificationError):
    """Declared touches are empty, malformed or unsafe."""

    code = "unsafe-scope"


class RiskEvidenceError(ClassificationError):
    """Declared risk tier contradicts the tier derived from declared paths."""

    code = "risk-evidence"


# -- routing ------------------------------------------------------------------

class RoutingError(PersonaPolicyError):
    code = "routing"


class UnknownPersonaError(RoutingError):
    code = "unknown-persona"


class UnknownRoleError(RoutingError):
    code = "unknown-role"


class RoleQualificationError(RoutingError):
    """The persona is not allowlisted to perform the role the task requires."""

    code = "role-qualification"


class RiskFloorError(RoutingError):
    """The requested persona may not carry work at this risk tier."""

    code = "risk-floor"


class IncompatibleOverrideError(RoutingError):
    """An explicit override contradicts the persona or the risk floor."""

    code = "incompatible-override"


class ModalityError(RoutingError):
    """The task needs a modality the route has not been proven to support."""

    code = "modality"


class OptionalPersonaError(RoutingError):
    """An optional specialist was not explicitly enabled by the operator."""

    code = "optional-persona"


class NoEligibleCandidateError(RoutingError):
    """Every approved role-qualified candidate was skipped for a recorded reason.

    This is the amendment's blocking condition: work stops only when the whole
    approved chain is unusable, and the reason for each skipped candidate is
    carried on the exception rather than summarised away.
    """

    code = "no-eligible-candidate"

    def __init__(self, message: str, skipped: tuple = ()):
        super().__init__(message)
        self.skipped = tuple(skipped)


# -- catalog and harness ------------------------------------------------------

class CatalogError(PersonaPolicyError):
    code = "catalog"


class UnsupportedModelError(CatalogError):
    code = "unsupported-model"


class UnsupportedEffortError(CatalogError):
    code = "unsupported-effort"


class HarnessBindingError(PersonaPolicyError):
    """The supplied executable, account arguments or environment are not usable."""

    code = "harness-binding"


# -- accounts and capacity ----------------------------------------------------

class AccountError(PersonaPolicyError):
    code = "account"


class AccountScopeError(AccountError):
    """The account is not allowed to work on this project."""

    code = "account-scope"


class CapabilityError(PersonaPolicyError):
    code = "capability"


class MissingProbeError(CapabilityError):
    code = "missing-probe"


class StaleProbeError(CapabilityError):
    code = "stale-probe"


class MismatchedProbeError(CapabilityError):
    """Evidence exists but not for this exact account/route/model/effort."""

    code = "mismatched-probe"


class ProbeFailedError(CapabilityError):
    code = "failed-probe"


class CreditsRequiredError(CapabilityError):
    """The route is reachable only by spending credits. Never a fallback."""

    code = "credits-required"


class CapacityExhaustedError(CapabilityError):
    code = "capacity-exhausted"


# -- plans --------------------------------------------------------------------

class PlanError(PersonaPolicyError):
    code = "plan"


class PlanTamperedError(PlanError):
    """A plan's recorded digest does not match its own contents."""

    code = "plan-tampered"


# -- review -------------------------------------------------------------------

class ReviewError(PersonaPolicyError):
    code = "review"


class ExternalFirstError(ReviewError):
    """External-first authority is the kernel's; it has not released this head."""

    code = "external-first"


class ReviewAuthorityError(ReviewError):
    """The bound reviewer persona holds no review authority at this tier."""

    code = "review-authority"


class ReviewIndependenceError(ReviewError):
    """Reviewer and author share lineage, account, actor or identity."""

    code = "review-independence"


class HeadMismatchError(ReviewError):
    """The review context is not bound to the current head."""

    code = "head-mismatch"


class AuthorLineageError(ReviewError):
    """Resumption or remediation changed the author's persona lineage."""

    code = "author-lineage"
