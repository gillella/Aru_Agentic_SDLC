"""What the operator supplies at execution time: harnesses, accounts, capacity.

The registry says which persona is approved. A binding says which installed
binary, which billable account, which project allowlist and which live evidence
exist right now. Both must agree before a plan exists.

Nothing here reads the environment, discovers a binary on ``PATH`` or accepts a
shell string. An executable is an absolute path whose basename the route already
documents, and the environment handed to a worker is an explicit allowlist.

Since #637 a binding also carries the immutable :class:`PolicySnapshot` it was
built against, and refuses to mix two. Which subscriptions exist is operator
policy; which of them is authenticated, occupied or unusable right now is a
binding fact the Driver observes. The two are never conflated.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .catalog import MODALITIES, Route, route as get_route
from .errors import (
    AccountScopeError, AccountStateError, CapacityExhaustedError, HarnessBindingError,
    ModalityError,
)
from .evidence import EvidenceStore
from .policy import PolicySnapshot, default_snapshot, from_document, load as load_policy
from .registry import PROJECT_RE, AccountPolicy

BINDING_SCHEMA = "aru.personas.fleet-binding/v1"

_FORBIDDEN_IN_ARG = ("\x00", "\n", "\r")


def _plain(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise HarnessBindingError(f"{what} must be a nonempty string")
    if any(bad in value for bad in _FORBIDDEN_IN_ARG):
        raise HarnessBindingError(f"{what} contains a control character")
    return value


@dataclass(frozen=True)
class HarnessBinding:
    """One installed CLI the operator has authorised this package to describe."""

    route: str
    #: Absolute path. The basename must be one the route documents, so a plan can
    #: reject undocumented command basenames. The operator must authenticate the binary itself.
    executable: str
    #: The isolated worktree the worker will run in. Never a repository root.
    workspace: str
    #: Extra modalities the operator has proven for this exact installation,
    #: beyond what the route's own ``--help`` documents.
    declared_modalities: frozenset[str] = field(default=frozenset())
    #: Required whenever ``declared_modalities`` adds anything.
    modality_evidence: str = ""
    #: Installation metadata; validate it against the actual binary in the Driver.
    version: str = ""

    def __post_init__(self) -> None:
        spec = get_route(self.route)
        path = Path(_plain(self.executable, "executable"))
        if not path.is_absolute():
            raise HarnessBindingError("executable must be an absolute path")
        if path.name not in spec.executables:
            raise HarnessBindingError(
                f"{path.name!r} is not a documented {self.route} executable "
                f"(documented: {', '.join(sorted(spec.executables))})"
            )
        workspace = Path(_plain(self.workspace, "workspace"))
        if not workspace.is_absolute():
            raise HarnessBindingError("workspace must be an absolute path")
        extra = frozenset(self.declared_modalities) - spec.proven_modalities
        if extra and not self.modality_evidence:
            raise HarnessBindingError(
                f"{self.route} declares modalities {sorted(extra)} beyond its documented "
                "flag surface; modality_evidence must name the proof"
            )
        if not frozenset(self.declared_modalities) <= MODALITIES:
            raise HarnessBindingError(f"unknown modality in {sorted(self.declared_modalities)}")

    @property
    def spec(self) -> Route:
        return get_route(self.route)

    @property
    def modalities(self) -> frozenset[str]:
        return self.spec.proven_modalities | frozenset(self.declared_modalities)

    def require_modalities(self, required: frozenset[str]) -> None:
        missing = required - self.modalities
        if missing:
            raise ModalityError(
                f"the bound {self.route} harness has no proven support for "
                f"{', '.join(sorted(missing))} input; the task is refused rather than "
                "downgraded to a text-only approximation"
            )

    def to_dict(self) -> dict:
        return {"route": self.route, "executable": self.executable,
                "workspace": self.workspace, "version": self.version,
                "modalities": sorted(self.modalities),
                "modality_evidence": self.modality_evidence}


@dataclass(frozen=True)
class AccountBinding:
    """One billable identity, its explicit project allowlist and its session cap."""

    account_id: str
    allowed_projects: tuple[str, ...]
    #: Environment the worker inherits, restricted to the route's allowlist. This
    #: is how one machine addresses several subscriptions of the same CLI.
    env: Mapping[str, str] = field(default_factory=dict)
    max_sessions: int = 1
    #: Slots already reserved on this account's shared quota, observed by the
    #: Driver. Model switching inside one capacity key is never new capacity.
    sessions_in_use: int = 0
    #: Set when the provider has told the operator this account is unusable.
    unavailable_reason: str = ""
    #: The immutable policy this account was read from. Two bindings resolved
    #: against two snapshots never share state; there is no global to patch.
    snapshot: PolicySnapshot = field(default_factory=default_snapshot,
                                     repr=False, compare=False)

    def __post_init__(self) -> None:
        policy = self.policy
        if not self.allowed_projects:
            raise AccountScopeError(
                f"{self.account_id}: an explicit non-empty project allowlist is required"
            )
        if len(set(self.allowed_projects)) != len(self.allowed_projects):
            raise AccountScopeError(f"{self.account_id}: duplicate project in the allowlist")
        for project in self.allowed_projects:
            if not PROJECT_RE.fullmatch(project):
                raise AccountScopeError(
                    f"{self.account_id}: {project!r} is not a literal owner/repository"
                )
            if policy.required_owners is not None:
                owner = project.split("/", 1)[0]
                if owner not in policy.required_owners:
                    raise AccountScopeError(
                        f"{self.account_id}: {project!r} is outside this account's client "
                        "scope. " + policy.restriction
                    )
        self._validate_environment(policy)
        if type(self.max_sessions) is not int or not 1 <= self.max_sessions <= 8:
            raise AccountScopeError(f"{self.account_id}: max_sessions must be 1-8")
        if type(self.sessions_in_use) is not int or self.sessions_in_use < 0:
            raise AccountScopeError(f"{self.account_id}: sessions_in_use must be >= 0")

    def _validate_environment(self, policy: AccountPolicy) -> None:
        allowed_env = get_route(policy.route).env_allowlist
        for name, value in self.env.items():
            if name not in allowed_env:
                raise HarnessBindingError(
                    f"{self.account_id}: environment variable {name!r} is not on the "
                    f"{policy.route} allowlist ({', '.join(sorted(allowed_env)) or 'empty'})"
                )
            _plain(value, f"{self.account_id} {name}")
        for required in allowed_env:
            value = self.env.get(required, "")
            if not Path(value).is_absolute() or ".." in Path(value).parts:
                raise HarnessBindingError(f"{self.account_id}: absolute {required} profile required")

    @property
    def policy(self) -> AccountPolicy:
        return self.snapshot.account(self.account_id)

    @property
    def identity_digest(self) -> str:
        return hashlib.sha256(json.dumps({"account": self.account_id, "env": dict(self.env)},
                              sort_keys=True).encode()).hexdigest()

    @property
    def capacity_key(self) -> str:
        return self.policy.capacity_key

    def require_project(self, project: str) -> None:
        if project not in self.allowed_projects:
            raise AccountScopeError(
                f"{self.account_id} is not allowlisted for {project!r}"
                + (" " + self.policy.restriction if self.policy.restriction else "")
            )

    def require_state(self) -> None:
        """Refuse a new reservation on a draining or disabled subscription.

        This is a *reservation* refusal only. Work already running keeps this
        account, its policy snapshot and its cumulative lineage; nothing here
        stops a worker, erases audit history, cancels provider billing or
        revokes a credential. Actual drain execution belongs to #631/#636.
        """
        state = self.policy.state
        if state == "draining":
            raise AccountStateError(
                f"{self.account_id} is draining: no new reservation is made on it. Work "
                "already running keeps this account and its recorded lineage."
            )
        if state != "enabled":
            raise AccountStateError(
                f"{self.account_id} is {state} in the current policy and takes no "
                "reservation. Removing it from configuration is not a provider "
                "cancellation or a credential revocation."
            )

    def require_capacity(self) -> None:
        self.require_state()
        if self.unavailable_reason:
            raise CapacityExhaustedError(
                f"{self.account_id} is unusable: {self.unavailable_reason}"
            )
        if self.sessions_in_use >= self.max_sessions:
            raise CapacityExhaustedError(
                f"every managed session slot on capacity key {self.capacity_key} is "
                f"reserved ({self.sessions_in_use}/{self.max_sessions}); switching model "
                "inside one subscription is not new capacity"
            )

    def to_dict(self) -> dict:
        return {"account_id": self.account_id, "capacity_key": self.capacity_key,
                "allowed_projects": list(self.allowed_projects),
                "max_sessions": self.max_sessions,
                "sessions_in_use": self.sessions_in_use,
                "state": self.policy.state,
                "env_names": sorted(self.env)}


@dataclass(frozen=True)
class FleetBinding:
    """Everything execution-time that the registry deliberately does not know."""

    harnesses: Mapping[str, HarnessBinding]
    #: Ordered. Account preference within a route is the operator's declaration,
    #: never inferred from a name or a previous run.
    accounts: tuple[AccountBinding, ...]
    evidence: EvidenceStore
    #: Optional specialists stay refused unless named here *and* freshly probed.
    enabled_optional: frozenset[str] = field(default=frozenset())
    #: The one policy this whole binding was resolved against.
    snapshot: PolicySnapshot = field(default_factory=default_snapshot, repr=False)

    def __post_init__(self) -> None:
        for name, harness in self.harnesses.items():
            if name != harness.route:
                raise HarnessBindingError(
                    f"harness keyed {name!r} declares route {harness.route!r}"
                )
        for bound in self.accounts:
            if bound.snapshot.digest != self.snapshot.digest:
                raise AccountScopeError(
                    f"{bound.account_id} was built against policy "
                    f"{bound.snapshot.digest[:12]} but the fleet binding carries "
                    f"{self.snapshot.digest[:12]}; one binding reads exactly one snapshot"
                )
        profiles = [str(Path(v).resolve()) for a in self.accounts for v in a.env.values()]
        if len(profiles) != len(set(profiles)):
            raise AccountScopeError("account profiles must be distinct; aliases are not capacity")
        seen = [a.account_id for a in self.accounts]
        if len(seen) != len(set(seen)):
            raise AccountScopeError("an account is bound more than once")

    def require_capacity(self, bound: AccountBinding) -> None:
        """Every reservation on one capacity key counts, whichever alias asked.

        Two account identifiers may name one subscription. The shared quota is
        the sum of what is reserved across all of them, and the ceiling is the
        lowest one any of them observed, so an extra alias can never raise it.
        """
        bound.require_capacity()
        siblings = [a for a in self.accounts if a.capacity_key == bound.capacity_key]
        if len(siblings) < 2:
            return
        used = sum(a.sessions_in_use for a in siblings)
        ceiling = min(a.max_sessions for a in siblings)
        declared = self.snapshot.account(bound.account_id).concurrency
        if declared is not None:
            ceiling = min(ceiling, declared)
        if used >= ceiling:
            raise CapacityExhaustedError(
                f"capacity key {bound.capacity_key} is fully reserved across "
                f"{', '.join(sorted(a.account_id for a in siblings))} ({used}/{ceiling}); "
                "aliasing one subscription under a second identifier is not new capacity"
            )

    def harness(self, route_name: str) -> HarnessBinding:
        try:
            return self.harnesses[route_name]
        except KeyError as exc:
            raise HarnessBindingError(
                f"no {route_name} harness is bound; this route cannot be planned"
            ) from exc

    def accounts_for(self, route_name: str) -> tuple[AccountBinding, ...]:
        """Bound accounts for one route, in the operator's declared preference order."""
        found = [a for a in self.accounts if a.policy.route == route_name]
        found.sort(key=lambda a: a.policy.priority)
        return tuple(found)

    def require(self, account_id: str) -> AccountBinding:
        for candidate in self.accounts:
            if candidate.account_id == account_id:
                return candidate
        raise AccountScopeError(f"{account_id} is not bound in this fleet binding")

    def to_dict(self) -> dict:
        return {
            "schema": BINDING_SCHEMA,
            "policy": {"version": self.snapshot.version, "digest": self.snapshot.digest,
                       "origin": self.snapshot.origin},
            "harnesses": {k: v.to_dict() for k, v in sorted(self.harnesses.items())},
            "accounts": [a.to_dict() for a in self.accounts],
            "enabled_optional": sorted(self.enabled_optional),
            "evidence": {"origin": self.evidence.origin,
                         "authorizes_execution": self.evidence.authorizes_execution,
                         "records": len(self.evidence.records)},
        }

    @classmethod
    def from_dict(cls, raw: dict, base: Path | None = None) -> "FleetBinding":
        if raw.get("schema") != BINDING_SCHEMA:
            raise HarnessBindingError("fleet binding has an unsupported schema")
        snapshot = _snapshot_from(raw.get("policy"), base)
        harnesses = {
            name: HarnessBinding(
                route=name, executable=spec["executable"], workspace=spec["workspace"],
                declared_modalities=frozenset(spec.get("declared_modalities", ())),
                modality_evidence=spec.get("modality_evidence", ""),
                version=spec.get("version", ""),
            )
            for name, spec in (raw.get("harnesses") or {}).items()
        }
        accounts = tuple(
            AccountBinding(
                account_id=spec["account_id"],
                allowed_projects=tuple(spec["allowed_projects"]),
                env=dict(spec.get("env", {})),
                max_sessions=spec.get("max_sessions", 1),
                sessions_in_use=spec.get("sessions_in_use", 0),
                unavailable_reason=spec.get("unavailable_reason", ""),
                snapshot=snapshot,
            )
            for spec in (raw.get("accounts") or ())
        )
        evidence_spec = raw.get("evidence")
        if isinstance(evidence_spec, str):
            location = Path(evidence_spec)
            if not location.is_absolute() and base is not None:
                location = base / location
            store = EvidenceStore.load(location)
        elif isinstance(evidence_spec, dict):
            store = EvidenceStore.from_dict(evidence_spec, origin="inline binding")
        else:
            raise HarnessBindingError(
                "fleet binding must reference a capability-evidence file or embed one"
            )
        return cls(harnesses=harnesses, accounts=accounts, evidence=store,
                   enabled_optional=frozenset(raw.get("enabled_optional", ())),
                   snapshot=snapshot)

    @classmethod
    def load(cls, path: str | Path) -> "FleetBinding":
        location = Path(path)
        try:
            raw = json.loads(location.read_text())
        except (OSError, ValueError) as exc:
            raise HarnessBindingError(f"fleet binding is unreadable: {exc}") from exc
        return cls.from_dict(raw, base=location.resolve().parent)


def _snapshot_from(spec: object, base: Path | None) -> PolicySnapshot:
    """A binding references an operator policy document, embeds one, or uses defaults."""
    if spec is None:
        return default_snapshot()
    if isinstance(spec, str):
        location = Path(spec)
        if not location.is_absolute() and base is not None:
            location = base / location
        return load_policy(location)
    if isinstance(spec, dict):
        return from_document(spec, origin="inline binding policy")
    raise HarnessBindingError(
        "fleet binding policy must reference a policy document path or embed one"
    )


def now_utc() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)
