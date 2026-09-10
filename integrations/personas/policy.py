"""Operator policy documents: experts, subscriptions and model rules as data.

Aru's clarification for #637 is that adding an expert, or adding and removing a
fleet subscription, must be a configuration change rather than a Python edit.
This module is that seam, and it is deliberately narrow.

* A **policy document** is JSON. It upserts roles, task families, personas,
  accounts and model rules onto a base, and may remove entries by identifier.
  It contains no command, no environment, no credential and no approval: those
  words are refused anywhere in the document.
* A **snapshot** is the immutable, fully validated result. It carries an explicit
  schema, a version and a content digest, and every resolution reads the snapshot
  it was handed. Nothing here mutates a module-level dictionary, so two callers
  holding two snapshots cannot leak policy into each other.
* Everything a configuration may *never* do stays in source, under review:
  :data:`KERNEL_INVARIANTS` names each one and :meth:`PolicySnapshot.validate`
  enforces it. Configuration cannot permit self-review, weaken exact-head sole
  authority, lower a task's risk tier, cross a client boundary, invent a model,
  buy capacity, or turn a harness into an independent authorship family.

Publication is atomic and reversible: :func:`publish` validates the document,
writes it through a temporary file in the destination directory and renames it
into place, and returns the digest an operator can record. Rolling back is
republishing the previous document; the digest says which one is live.

Nothing in this module authenticates an account, proves usable quota, drains a
running worker or installs anything. Those remain #631/#636 responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from . import catalog
from .errors import (
    AccountScopeError, PolicyDocumentError, PolicyPrivilegeError, UnknownPersonaError,
    UnknownRoleError, UnknownTaskError,
)
from .registry import (
    ACCOUNTS, ACCOUNT_STATES, APPROVED_ARCHITECT_CHAIN, AccountPolicy, MANDATORY_STOP_CRITERIA,
    MODEL_RULES, ModelRule, PERSONAS, Persona, ROLES, REGISTRY_VERSION, Role, TASK_CLASSES,
    TaskClass,
)

POLICY_SCHEMA = "aru.personas.policy/v1"

DEFAULT_ORIGIN = "built-in default policy"

#: What no policy document may do, whatever it declares. Each line is enforced by
#: :meth:`PolicySnapshot.validate` and covered by a negative test.
KERNEL_INVARIANTS: tuple[str, ...] = (
    "Every role keeps the mandatory fleet stop criteria; a specialization may only add.",
    "A persona holding review scope must perform the code_reviewer role, and routine "
    "review scope may not carry work above risk tier 1.",
    "A review task family never advances to another identity on an availability skip; "
    "reviewer selection is the kernel's authority, not configuration's.",
    "The approved architect availability chain stays an exact ordered prefix.",
    "A model must already be a recorded, assignable catalog identifier and must still "
    "pass an exact-model, exact-effort capability probe; configuration never proves access.",
    "Model authorship (vendor) is independent of the access harness, so reaching a "
    "vendor's model through another fleet never creates independent authorship.",
    "Accounts sharing one capacity key share route, lineage, client restriction and "
    "concurrency ceiling; an alias is never additional quota.",
    "A document carries no command, executable, environment, credential, purchase or "
    "review-authority field, and no such key may appear anywhere inside it.",
)

#: Keys refused anywhere in a document, at any depth. Configuration is data.
FORBIDDEN_DOCUMENT_KEYS: frozenset[str] = frozenset({
    "argv", "authority", "authority_source", "command", "credential", "credentials",
    "credits", "env", "environment", "exec", "executable", "external_first_released",
    "hook", "import", "plugin", "purchase", "review_authority", "shell", "token",
})

_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_OWNER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_CONTROL = ("\x00", "\n", "\r")

_DOCUMENT_KEYS = frozenset({"schema", "version", "base", "note", "roles", "task_classes",
                            "personas", "accounts", "models", "remove"})
_REMOVE_KEYS = frozenset({"roles", "task_classes", "personas", "accounts", "models"})
_ROLE_KEYS = frozenset({"id", "base", "title", "scope", "output_contract", "escalation",
                        "stop_criteria"})
_TASK_KEYS = frozenset({"name", "summary", "candidates", "role", "availability_fallback",
                        "override_candidates", "required_modalities"})
_PERSONA_KEYS = frozenset({"id", "display", "route", "vendor", "model_ids", "canonical_effort",
                           "allowed_efforts", "escalated_effort", "escalate_at_tier",
                           "max_risk_tier", "native_role", "acting_roles", "primary_task",
                           "also_eligible", "review_scope", "lineage", "authority_boundary",
                           "optional", "required_modalities", "notes"})
_ACCOUNT_KEYS = frozenset({"id", "route", "capacity_key", "lineage", "description",
                           "required_owners", "restriction", "state", "priority",
                           "concurrency"})
_MODEL_KEYS = frozenset({"route", "id", "vendor", "effort_selection"})

REVIEW_SCOPES: frozenset[str] = frozenset({"none", "routine", "high_risk"})

#: Roles whose shipped contract is a safety floor. A document may extend one of
#: these, never publish a thinner replacement under the same identifier.
PROTECTED_ROLES: tuple[str, ...] = ("code_reviewer", "chief_architect", "qa_automation",
                                    "triage_assistant", "design_analyst")


# --------------------------------------------------------------------------- #
# Primitive readers - every one refuses rather than coerces
# --------------------------------------------------------------------------- #

def _keys(raw: object, allowed: frozenset[str], what: str) -> dict:
    if not isinstance(raw, dict):
        raise PolicyDocumentError(f"{what} must be a JSON object")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PolicyDocumentError(f"{what}: unknown field(s) {', '.join(unknown)}")
    return raw


def _text(raw: dict, key: str, what: str, *, required: bool = True, default: str = "") -> str:
    value = raw.get(key, default)
    if not required and value == default:
        return default
    if not isinstance(value, str) or not value:
        raise PolicyDocumentError(f"{what}: {key} must be a nonempty string")
    if any(bad in value for bad in _CONTROL):
        raise PolicyDocumentError(f"{what}: {key} contains a control character")
    return value


def _identifier(raw: dict, key: str, what: str) -> str:
    value = _text(raw, key, what)
    if not _ID_RE.fullmatch(value):
        raise PolicyDocumentError(
            f"{what}: {key} {value!r} is not a lowercase literal identifier"
        )
    return value


def _lines(raw: dict, key: str, what: str, *, required: bool = True) -> tuple[str, ...]:
    value = raw.get(key, ())
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise PolicyDocumentError(f"{what}: {key} must be a list of strings")
    items = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise PolicyDocumentError(f"{what}: {key} must contain nonempty strings")
        if any(bad in entry for bad in ("\x00", "\r")):
            raise PolicyDocumentError(f"{what}: {key} contains a control character")
        items.append(entry)
    if required and not items:
        raise PolicyDocumentError(f"{what}: {key} must not be empty")
    if len(set(items)) != len(items):
        raise PolicyDocumentError(f"{what}: {key} repeats an entry")
    return tuple(items)


def _flag(raw: dict, key: str, what: str, default: bool) -> bool:
    value = raw.get(key, default)
    if type(value) is not bool:
        raise PolicyDocumentError(f"{what}: {key} must be a boolean")
    return value


def _whole(raw: dict, key: str, what: str, low: int, high: int, default: int) -> int:
    value = raw.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise PolicyDocumentError(f"{what}: {key} must be an integer {low}-{high}")
    return value


def _modalities(raw: dict, what: str) -> frozenset[str]:
    value = raw.get("required_modalities", ["text"])
    if not isinstance(value, (list, tuple)) or not value:
        raise PolicyDocumentError(f"{what}: required_modalities must be a nonempty list")
    found = frozenset(value)
    if not found <= catalog.MODALITIES:
        raise PolicyDocumentError(
            f"{what}: unknown modality in {sorted(found)}; known: {sorted(catalog.MODALITIES)}"
        )
    return found


def _scan_forbidden(raw: object, where: str = "document") -> None:
    """Refuse a command, credential, purchase or authority key at any depth."""
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str):
                raise PolicyDocumentError(f"{where}: object keys must be strings")
            if key.casefold() in FORBIDDEN_DOCUMENT_KEYS:
                raise PolicyPrivilegeError(
                    f"{where}: {key!r} is not a policy field. Configuration cannot inject "
                    "an executable, shell command, environment, credential, purchase or "
                    "review authority; those stay in reviewed adapter code and the kernel."
                )
            _scan_forbidden(value, f"{where}.{key}")
    elif isinstance(raw, (list, tuple)):
        for index, value in enumerate(raw):
            _scan_forbidden(value, f"{where}[{index}]")


# --------------------------------------------------------------------------- #
# Document entries -> typed policy objects
# --------------------------------------------------------------------------- #

def _role_from(raw: dict, base_roles: Mapping[str, Role]) -> Role:
    entry = _keys(raw, _ROLE_KEYS, "role")
    role_id = _identifier(entry, "id", "role")
    what = f"role {role_id}"
    base_id = _text(entry, "base", what, required=False)
    inherited: Role | None = None
    if base_id:
        if base_id not in base_roles:
            raise PolicyDocumentError(f"{what}: base role {base_id!r} does not exist")
        if base_id == role_id:
            raise PolicyDocumentError(f"{what}: a role cannot specialize itself")
        inherited = base_roles[base_id]
    defaults = inherited or base_roles.get(role_id)
    return Role(
        id=role_id,
        title=_text(entry, "title", what, required=defaults is None,
                    default=defaults.title if defaults else ""),
        scope=_text(entry, "scope", what, required=defaults is None,
                    default=defaults.scope if defaults else ""),
        output_contract=(_lines(entry, "output_contract", what)
                         if "output_contract" in entry or defaults is None
                         else defaults.output_contract),
        escalation=(_lines(entry, "escalation", what)
                    if "escalation" in entry or defaults is None else defaults.escalation),
        stop_criteria=(_lines(entry, "stop_criteria", what)
                       if "stop_criteria" in entry
                       else (defaults.stop_criteria if defaults else MANDATORY_STOP_CRITERIA)),
        base=base_id,
    )


def _task_from(raw: dict) -> TaskClass:
    entry = _keys(raw, _TASK_KEYS, "task class")
    name = _identifier(entry, "name", "task class")
    what = f"task class {name}"
    role_id = entry.get("role")
    if role_id is not None and not isinstance(role_id, str):
        raise PolicyDocumentError(f"{what}: role must be a string or null")
    return TaskClass(
        name=name,
        summary=_text(entry, "summary", what),
        candidates=_lines(entry, "candidates", what),
        role=role_id or None,
        availability_fallback=_flag(entry, "availability_fallback", what, True),
        override_candidates=_lines(entry, "override_candidates", what, required=False),
        required_modalities=_modalities(entry, what),
    )


def _persona_from(raw: dict) -> Persona:
    entry = _keys(raw, _PERSONA_KEYS, "persona")
    persona_id = _identifier(entry, "id", "persona")
    what = f"persona {persona_id}"
    model_ids = entry.get("model_ids")
    if not isinstance(model_ids, dict) or not model_ids:
        raise PolicyDocumentError(f"{what}: model_ids must be a nonempty effort->model map")
    for effort, model in model_ids.items():
        if not isinstance(effort, str) or not isinstance(model, str) or not model:
            raise PolicyDocumentError(f"{what}: model_ids must map effort names to identifiers")
    escalated = entry.get("escalated_effort")
    if escalated is not None and (not isinstance(escalated, str) or not escalated):
        raise PolicyDocumentError(f"{what}: escalated_effort must be a string or null")
    return Persona(
        id=persona_id,
        display=_text(entry, "display", what),
        route=_text(entry, "route", what),
        model_ids=dict(model_ids),
        canonical_effort=_text(entry, "canonical_effort", what),
        allowed_efforts=_lines(entry, "allowed_efforts", what),
        escalated_effort=escalated,
        escalate_at_tier=_whole(entry, "escalate_at_tier", what, 0, 3, 2),
        max_risk_tier=_whole(entry, "max_risk_tier", what, 0, 3, 0),
        native_role=_text(entry, "native_role", what),
        acting_roles=_lines(entry, "acting_roles", what, required=False),
        primary_task=_text(entry, "primary_task", what),
        also_eligible=_lines(entry, "also_eligible", what, required=False),
        review_scope=_text(entry, "review_scope", what, required=False, default="none"),
        lineage=_text(entry, "lineage", what),
        authority_boundary=_lines(entry, "authority_boundary", what),
        optional=_flag(entry, "optional", what, False),
        required_modalities=_modalities(entry, what),
        notes=_text(entry, "notes", what, required=False),
        vendor=_text(entry, "vendor", what, required=False),
    )


def _account_from(raw: dict) -> AccountPolicy:
    entry = _keys(raw, _ACCOUNT_KEYS, "account")
    account_id = _identifier(entry, "id", "account")
    what = f"account {account_id}"
    owners = entry.get("required_owners")
    if owners is not None:
        if not isinstance(owners, (list, tuple)) or not owners:
            raise PolicyDocumentError(f"{what}: required_owners must be null or a nonempty list")
        for owner in owners:
            if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
                raise PolicyDocumentError(f"{what}: {owner!r} is not a literal repository owner")
        owners = frozenset(owners)
    concurrency = entry.get("concurrency")
    if concurrency is not None and (type(concurrency) is not int or not 1 <= concurrency <= 8):
        raise PolicyDocumentError(f"{what}: concurrency must be null or an integer 1-8")
    state = _text(entry, "state", what, required=False, default="enabled")
    if state not in ACCOUNT_STATES:
        raise PolicyDocumentError(
            f"{what}: state must be one of {', '.join(ACCOUNT_STATES)}"
        )
    restriction = _text(entry, "restriction", what, required=False)
    if owners is not None and not restriction:
        raise PolicyDocumentError(
            f"{what}: a client-scoped account must state its restriction in words, so a "
            "refusal explains itself to the operator"
        )
    return AccountPolicy(
        id=account_id,
        route=_text(entry, "route", what),
        capacity_key=_identifier(entry, "capacity_key", what),
        lineage=_text(entry, "lineage", what),
        description=_text(entry, "description", what),
        required_owners=owners,
        restriction=restriction,
        state=state,
        priority=_whole(entry, "priority", what, 0, 99, 0),
        concurrency=concurrency,
    )


def _model_from(raw: dict) -> ModelRule:
    entry = _keys(raw, _MODEL_KEYS, "model")
    model_id = _text(entry, "id", "model")
    what = f"model {model_id}"
    selection = _text(entry, "effort_selection", what, required=False, default="explicit")
    if selection not in catalog.EFFORT_SELECTIONS:
        raise PolicyDocumentError(
            f"{what}: effort_selection must be one of {sorted(catalog.EFFORT_SELECTIONS)}"
        )
    return ModelRule(route=_text(entry, "route", what), model_id=model_id,
                     vendor=_text(entry, "vendor", what), effort_selection=selection)


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #

def _canonical(payload: Mapping) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class PolicySnapshot:
    """One immutable, fully validated fleet policy.

    A snapshot is handed explicitly to every resolution. It is never installed in
    a module global, so a second operator document cannot leak into a request or
    a profile that was resolved against the first.
    """

    version: str
    roles: Mapping[str, Role]
    task_classes: Mapping[str, TaskClass]
    personas: Mapping[str, Persona]
    accounts: Mapping[str, AccountPolicy]
    models: Mapping[tuple[str, str], ModelRule]
    origin: str = DEFAULT_ORIGIN
    note: str = ""
    digest: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        # A snapshot is immutable all the way down: the caller cannot reach in
        # and change a persona, an account or a model rule after publication.
        for name in ("roles", "task_classes", "personas", "accounts", "models"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        if not self.digest:
            object.__setattr__(self, "digest", self.compute_digest())

    # -- lookups ------------------------------------------------------------ #

    def role(self, role_id: str) -> Role:
        try:
            return self.roles[role_id]
        except KeyError as exc:
            raise UnknownRoleError(f"unknown role: {role_id!r}") from exc

    def task_class(self, name: str) -> TaskClass:
        try:
            return self.task_classes[name]
        except KeyError as exc:
            raise UnknownTaskError(f"unknown task class: {name!r}") from exc

    def persona(self, persona_id: str) -> Persona:
        try:
            return self.personas[persona_id]
        except KeyError as exc:
            raise UnknownPersonaError(f"unknown persona: {persona_id!r}") from exc

    def account(self, account_id: str) -> AccountPolicy:
        try:
            return self.accounts[account_id]
        except KeyError as exc:
            raise AccountScopeError(
                f"unknown account: {account_id!r}; removing an account from policy removes "
                "its future reservations, never its recorded history"
            ) from exc

    def accounts_for_route(self, route_name: str) -> tuple[str, ...]:
        found = [a for a in self.accounts.values() if a.route == route_name]
        found.sort(key=lambda a: a.priority)
        return tuple(a.id for a in found)

    def personas_for_capacity(self, capacity_key: str) -> tuple[str, ...]:
        routes = {a.route for a in self.accounts.values() if a.capacity_key == capacity_key}
        return tuple(p.id for p in self.personas.values() if p.route in routes)

    def accounts_for_capacity(self, capacity_key: str) -> tuple[AccountPolicy, ...]:
        return tuple(a for a in self.accounts.values() if a.capacity_key == capacity_key)

    def model_rule(self, route_name: str, model_id: str) -> ModelRule:
        try:
            return self.models[(route_name, model_id)]
        except KeyError as exc:
            raise PolicyDocumentError(
                f"{model_id!r} is not an enabled {route_name} model in this policy; a model "
                "becomes usable only when the policy declares it and a recorded catalog "
                "entry plus a fresh exact capability probe agree"
            ) from exc

    def require_effort(self, route_name: str, model_id: str, effort: str) -> None:
        catalog.require_effort(route_name, model_id, effort,
                               effort_selection=self.model_rule(route_name, model_id)
                               .effort_selection)

    def vendor_of(self, persona_id: str) -> str:
        return self.persona(persona_id).author_vendor

    # -- serialization ------------------------------------------------------ #

    def to_document(self) -> dict:
        """The complete document this snapshot is, with ``base: empty``."""
        return {
            "schema": POLICY_SCHEMA,
            "version": self.version,
            "base": "empty",
            "note": self.note,
            "roles": [_role_document(r) for r in _ordered(self.roles)],
            "task_classes": [_task_document(t) for t in _ordered(self.task_classes)],
            "models": [_model_document(m) for m in
                       sorted(self.models.values(), key=lambda m: m.key)],
            "personas": [_persona_document(p) for p in _ordered(self.personas)],
            "accounts": [_account_document(a) for a in _ordered(self.accounts)],
        }

    def compute_digest(self) -> str:
        return hashlib.sha256(_canonical(self.to_document()).encode("utf-8")).hexdigest()

    def summary(self) -> dict:
        return {
            "schema": POLICY_SCHEMA, "version": self.version, "origin": self.origin,
            "digest": self.digest, "roles": len(self.roles),
            "task_classes": len(self.task_classes), "personas": len(self.personas),
            "accounts": len(self.accounts), "models": len(self.models),
            "account_states": {a.id: a.state for a in _ordered(self.accounts)},
        }

    # -- validation --------------------------------------------------------- #

    def validate(self) -> "PolicySnapshot":
        """Fail closed on anything malformed, unproven or privilege-widening."""
        self._validate_roles()
        self._validate_models()
        self._validate_accounts()
        self._validate_personas()
        self._validate_task_classes()
        if self.digest != self.compute_digest():
            raise PolicyDocumentError("policy snapshot digest does not match its contents")
        return self

    def _validate_roles(self) -> None:
        for role in self.roles.values():
            what = f"role {role.id}"
            missing = [line for line in MANDATORY_STOP_CRITERIA if line not in role.stop_criteria]
            if missing:
                raise PolicyPrivilegeError(
                    f"{what}: a role may add stop criteria, never drop the mandatory fleet "
                    f"ones; missing: {missing[0]!r}"
                )
            if role.base:
                inherited = self.role(role.base)
                for name in ("output_contract", "escalation", "stop_criteria"):
                    dropped = [line for line in getattr(inherited, name)
                               if line not in getattr(role, name)]
                    if dropped:
                        raise PolicyPrivilegeError(
                            f"{what}: a specialization of {role.base} may only add to its "
                            f"{name}; it dropped {dropped[0]!r}"
                        )
                if self._base_chain(role.id) is None:
                    raise PolicyDocumentError(f"{what}: role inheritance forms a cycle")
            shipped = ROLES.get(role.id)
            if role.id in PROTECTED_ROLES and shipped is not None:
                for name in ("output_contract", "escalation", "stop_criteria"):
                    dropped = [line for line in getattr(shipped, name)
                               if line not in getattr(role, name)]
                    if dropped:
                        raise PolicyPrivilegeError(
                            f"{what}: this contract is a safety floor; a document may extend "
                            f"it but not publish a thinner {name} ({dropped[0]!r} is missing)"
                        )

    def _base_chain(self, role_id: str) -> tuple[str, ...] | None:
        seen: list[str] = []
        current = role_id
        while current:
            if current in seen:
                return None
            seen.append(current)
            if len(seen) > len(self.roles):
                return None
            current = self.roles[current].base if current in self.roles else ""
        return tuple(seen)

    def _validate_models(self) -> None:
        for key, rule in self.models.items():
            if key != rule.key:
                raise PolicyDocumentError(f"model rule keyed {key} declares {rule.key}")
            catalog.route(rule.route)
            # A configured model must already be a recorded, assignable catalog
            # identifier: appearing in a document never makes a model exist.
            catalog.require_assignable(rule.route, rule.model_id)
            if rule.effort_selection not in catalog.EFFORT_SELECTIONS:
                raise PolicyDocumentError(
                    f"model {rule.model_id}: unknown effort selection {rule.effort_selection!r}"
                )
            if not rule.vendor:
                raise PolicyDocumentError(f"model {rule.model_id}: a vendor must be declared")

    def _validate_accounts(self) -> None:
        by_capacity: dict[str, list[AccountPolicy]] = {}
        by_route: dict[str, set[str]] = {}
        for item in self.accounts.values():
            catalog.route(item.route)
            if item.state not in ACCOUNT_STATES:
                raise PolicyDocumentError(f"account {item.id}: unknown state {item.state!r}")
            by_capacity.setdefault(item.capacity_key, []).append(item)
            by_route.setdefault(item.route, set()).add(item.lineage)
        for route_name, lineages in by_route.items():
            if len(lineages) > 1:
                raise PolicyDocumentError(
                    f"route {route_name}: accounts disagree on lineage "
                    f"({', '.join(sorted(lineages))}); one harness is one author family"
                )
        for capacity_key, siblings in by_capacity.items():
            first = siblings[0]
            for other in siblings[1:]:
                if (other.route, other.lineage, other.required_owners, other.concurrency) != (
                        first.route, first.lineage, first.required_owners, first.concurrency):
                    raise PolicyPrivilegeError(
                        f"capacity key {capacity_key}: {other.id} and {first.id} name one "
                        "billable identity but disagree on route, lineage, client scope or "
                        "concurrency. An alias is never new capacity and never escapes a "
                        "client restriction."
                    )

    def _reference(self, kind: str, identifier: str, what: str) -> None:
        """A document names an entry that does not exist: refuse the document."""
        known = {"role": self.roles, "task class": self.task_classes,
                 "persona": self.personas, "account": self.accounts}[kind]
        if identifier not in known:
            raise PolicyDocumentError(
                f"{what}: {kind} {identifier!r} does not exist in this policy "
                f"(declared: {', '.join(sorted(known)) or 'none'})"
            )

    def _validate_personas(self) -> None:
        for item in self.personas.values():
            what = f"persona {item.id}"
            catalog.route(item.route)
            self._reference("role", item.native_role, what)
            for acting in item.acting_roles:
                self._reference("role", acting, what)
            self._validate_persona_efforts(item, what)
            self._validate_persona_route(item, what)
            self._validate_persona_authorship(item, what)
            self._validate_persona_scope(item, what)

    def _validate_persona_efforts(self, item: Persona, what: str) -> None:
        if item.canonical_effort not in item.allowed_efforts:
            raise PolicyDocumentError(f"{what}: canonical effort is not allowed")
        if item.escalated_effort and item.escalated_effort not in item.allowed_efforts:
            raise PolicyDocumentError(f"{what}: escalated effort is not allowed")
        if set(item.model_ids) != set(item.allowed_efforts):
            raise PolicyDocumentError(f"{what}: model identifiers and efforts disagree")

    def _validate_persona_route(self, item: Persona, what: str) -> None:
        route_accounts = self.accounts_for_route(item.route)
        if not route_accounts:
            raise PolicyDocumentError(
                f"{what}: the {item.route} route has no account in this policy"
            )
        if item.lineage != self.account(route_accounts[0]).lineage:
            raise PolicyDocumentError(f"{what}: lineage disagrees with its route accounts")

    def _validate_persona_authorship(self, item: Persona, what: str) -> None:
        """Model authorship is the vendor's, whatever harness the persona reaches it through."""
        vendors = set()
        for effort, model in item.model_ids.items():
            rule = self.model_rule(item.route, model)
            catalog.require_effort(item.route, model, effort,
                                   effort_selection=rule.effort_selection)
            vendors.add(rule.vendor)
        if len(vendors) > 1:
            raise PolicyDocumentError(
                f"{what}: one identity cannot span vendors {sorted(vendors)}"
            )
        if item.author_vendor != next(iter(vendors)):
            raise PolicyPrivilegeError(
                f"{what}: declared vendor {item.author_vendor!r} disagrees with the "
                f"{next(iter(vendors))!r} model it runs. Reaching a vendor's model "
                "through another harness never creates independent authorship."
            )

    def _validate_persona_scope(self, item: Persona, what: str) -> None:
        for name in (item.primary_task, *item.also_eligible):
            self._reference("task class", name, what)
        if item.id not in self.task_class(item.primary_task).candidates:
            raise PolicyDocumentError(f"{what}: not listed by its own primary task class")
        if item.review_scope not in REVIEW_SCOPES:
            raise PolicyDocumentError(f"{what}: unknown review scope")
        if item.review_scope != "none" and "code_reviewer" not in item.role_ids:
            raise PolicyPrivilegeError(
                f"{what}: holds review scope without the code_reviewer role"
            )
        if item.review_scope == "routine" and item.max_risk_tier > 1:
            raise PolicyPrivilegeError(
                f"{what}: routine review scope may not carry work above risk tier 1"
            )
        if not item.required_modalities <= catalog.MODALITIES:
            raise PolicyDocumentError(f"{what}: unknown required modality")

    def _validate_task_classes(self) -> None:
        for family in self.task_classes.values():
            what = f"task class {family.name}"
            if not family.candidates:
                raise PolicyDocumentError(f"{what}: no approved candidate")
            if len(set(family.candidates)) != len(family.candidates):
                raise PolicyDocumentError(
                    f"{what}: a candidate list repeats an identity, which is a fallback cycle"
                )
            overlap = set(family.candidates) & set(family.override_candidates)
            if overlap:
                raise PolicyDocumentError(
                    f"{what}: {sorted(overlap)} is both an ordinary and an override candidate"
                )
            if family.role is not None:
                self._reference("role", family.role, what)
            if family.role == "code_reviewer" and family.availability_fallback:
                raise PolicyPrivilegeError(
                    f"{what}: a review family never advances to another identity on an "
                    "availability skip; reviewer selection is the kernel's authority"
                )
            for persona_id in (*family.candidates, *family.override_candidates):
                self._reference("persona", persona_id, what)
                candidate = self.persona(persona_id)
                if family.role is not None and not candidate.performs(family.role):
                    raise PolicyDocumentError(
                        f"{what}: {persona_id} is not role-qualified for {family.role}"
                    )
                if family.name not in (candidate.primary_task, *candidate.also_eligible):
                    raise PolicyDocumentError(
                        f"{what}: {persona_id} does not declare this family"
                    )
        chain = self.task_classes.get("architecture_decision")
        if chain is not None and chain.candidates[:len(APPROVED_ARCHITECT_CHAIN)] != \
                APPROVED_ARCHITECT_CHAIN:
            raise PolicyPrivilegeError(
                "the approved architect availability chain "
                f"({' -> '.join(APPROVED_ARCHITECT_CHAIN)}) must stay an exact ordered "
                "prefix; a document may append further qualified candidates"
            )


def _ordered(mapping: Mapping) -> tuple:
    return tuple(mapping[key] for key in sorted(mapping))


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

def _role_document(role: Role) -> dict:
    return {"id": role.id, "base": role.base, "title": role.title, "scope": role.scope,
            "output_contract": list(role.output_contract),
            "escalation": list(role.escalation),
            "stop_criteria": list(role.stop_criteria)}


def _task_document(family: TaskClass) -> dict:
    return {"name": family.name, "summary": family.summary,
            "candidates": list(family.candidates), "role": family.role,
            "availability_fallback": family.availability_fallback,
            "override_candidates": list(family.override_candidates),
            "required_modalities": sorted(family.required_modalities)}


def _persona_document(item: Persona) -> dict:
    return {"id": item.id, "display": item.display, "route": item.route,
            "vendor": item.vendor, "model_ids": dict(sorted(item.model_ids.items())),
            "canonical_effort": item.canonical_effort,
            "allowed_efforts": list(item.allowed_efforts),
            "escalated_effort": item.escalated_effort,
            "escalate_at_tier": item.escalate_at_tier, "max_risk_tier": item.max_risk_tier,
            "native_role": item.native_role, "acting_roles": list(item.acting_roles),
            "primary_task": item.primary_task, "also_eligible": list(item.also_eligible),
            "review_scope": item.review_scope, "lineage": item.lineage,
            "authority_boundary": list(item.authority_boundary), "optional": item.optional,
            "required_modalities": sorted(item.required_modalities), "notes": item.notes}


def _account_document(item: AccountPolicy) -> dict:
    return {"id": item.id, "route": item.route, "capacity_key": item.capacity_key,
            "lineage": item.lineage, "description": item.description,
            "required_owners": (None if item.required_owners is None
                                else sorted(item.required_owners)),
            "restriction": item.restriction, "state": item.state,
            "priority": item.priority, "concurrency": item.concurrency}


def _model_document(rule: ModelRule) -> dict:
    return {"route": rule.route, "id": rule.model_id, "vendor": rule.vendor,
            "effort_selection": rule.effort_selection}


def default_document() -> dict:
    """The shipped fleet expressed as a policy document, for diffing and export."""
    return _base_snapshot().to_document()


_DEFAULT: list[PolicySnapshot] = []


def _base_snapshot() -> PolicySnapshot:
    if not _DEFAULT:
        _DEFAULT.append(PolicySnapshot(
            version=REGISTRY_VERSION, roles=dict(ROLES), task_classes=dict(TASK_CLASSES),
            personas=dict(PERSONAS), accounts=dict(ACCOUNTS), models=dict(MODEL_RULES),
            origin=DEFAULT_ORIGIN,
            note="The 13 personas Aru approved on 2026-09-09 and their subscriptions.",
        ))
    return _DEFAULT[0]


def default_snapshot() -> PolicySnapshot:
    """The immutable baseline. Callers receive the same frozen object, never a global dict."""
    return _base_snapshot()


def from_document(raw: Mapping, *, base: PolicySnapshot | None = None,
                  origin: str = "operator document") -> PolicySnapshot:
    """Validate one document against a base and return the resulting snapshot."""
    entry = _keys(raw, _DOCUMENT_KEYS, "policy document")
    _scan_forbidden(entry)
    if entry.get("schema") != POLICY_SCHEMA:
        raise PolicyDocumentError(
            f"policy document must declare schema {POLICY_SCHEMA!r}; an unstated schema is "
            "never assumed"
        )
    version = _text(entry, "version", "policy document")
    base_name = _text(entry, "base", "policy document", required=False, default="default")
    if base_name not in {"default", "empty"}:
        raise PolicyDocumentError("policy document base must be 'default' or 'empty'")
    baseline = (base if base is not None else default_snapshot()) if base_name == "default" else None

    roles = dict(baseline.roles) if baseline else {}
    task_classes = dict(baseline.task_classes) if baseline else {}
    personas = dict(baseline.personas) if baseline else {}
    accounts = dict(baseline.accounts) if baseline else {}
    models = dict(baseline.models) if baseline else {}

    for item in _section(entry, "roles"):
        role = _role_from(item, roles)
        roles[role.id] = role
    for item in _section(entry, "task_classes"):
        family = _task_from(item)
        task_classes[family.name] = family
    for item in _section(entry, "models"):
        rule = _model_from(item)
        models[rule.key] = rule
    for item in _section(entry, "personas"):
        person = _persona_from(item)
        personas[person.id] = person
    for item in _section(entry, "accounts"):
        account = _account_from(item)
        accounts[account.id] = account

    removals = _keys(entry.get("remove", {}), _REMOVE_KEYS, "policy document remove")
    _remove(roles, removals.get("roles"), "role")
    _remove(task_classes, removals.get("task_classes"), "task class")
    _remove(personas, removals.get("personas"), "persona")
    _remove(accounts, removals.get("accounts"), "account")
    for key in _identifiers(removals.get("models"), "model"):
        route_name, _, model_id = key.partition(":")
        if (route_name, model_id) not in models:
            raise PolicyDocumentError(f"cannot remove unknown model {key!r}")
        del models[(route_name, model_id)]

    return PolicySnapshot(
        version=version, roles=roles, task_classes=task_classes, personas=personas,
        accounts=accounts, models=models, origin=origin,
        note=_text(entry, "note", "policy document", required=False),
    ).validate()


def _section(entry: Mapping, name: str) -> tuple[dict, ...]:
    value = entry.get(name, ())
    if not isinstance(value, (list, tuple)):
        raise PolicyDocumentError(f"policy document {name} must be a list")
    return tuple(value)


def _identifiers(value: object, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PolicyDocumentError(f"remove.{what} must be a list of identifiers")
    for item in value:
        if not isinstance(item, str) or not item:
            raise PolicyDocumentError(f"remove.{what} must contain nonempty identifiers")
    return tuple(value)


def _remove(target: dict, value: object, what: str) -> None:
    for identifier in _identifiers(value, what):
        if identifier not in target:
            raise PolicyDocumentError(
                f"cannot remove unknown {what} {identifier!r}; removal names an existing entry"
            )
        del target[identifier]


def load_policy_document(path: str | Path, *,
                         base: PolicySnapshot | None = None) -> PolicySnapshot:
    """Read and validate one operator policy document."""
    location = Path(path)
    if location.is_symlink():
        raise PolicyDocumentError(f"policy document must not be a symlink: {location}")
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyDocumentError(f"policy document is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyDocumentError("policy document must be a JSON object")
    return from_document(raw, base=base, origin=str(location))


#: Short alias kept for readability inside this package.
load = load_policy_document


def preview(raw: Mapping, *, base: PolicySnapshot | None = None) -> dict:
    """Explain what publishing this document would change, without publishing it."""
    baseline = base if base is not None else default_snapshot()
    candidate = from_document(raw, base=baseline)
    changes = {"base_digest": baseline.digest, **candidate.summary()}
    for name in ("roles", "task_classes", "personas", "accounts"):
        before, after = getattr(baseline, name), getattr(candidate, name)
        changes[name] = {
            "added": sorted(set(after) - set(before)),
            "changed": sorted(k for k in set(after) & set(before) if after[k] != before[k]),
            "removed": sorted(set(before) - set(after)),
        }
    changes["models"] = {
        "added": sorted(f"{r}:{m}" for r, m in set(candidate.models) - set(baseline.models)),
        "removed": sorted(f"{r}:{m}" for r, m in set(baseline.models) - set(candidate.models)),
    }
    changes["draining"] = sorted(a.id for a in candidate.accounts.values()
                                 if a.state == "draining")
    changes["disabled"] = sorted(a.id for a in candidate.accounts.values()
                                 if a.state == "disabled")
    changes["publishes"] = False
    return changes


def publish(raw: Mapping, path: str | Path, *, base: PolicySnapshot | None = None) -> str:
    """Validate, then replace the document at ``path`` atomically. Returns the digest.

    Publication is a single rename inside the destination directory, so a reader
    sees either the previous document or the new one and never a partial write.
    Rolling back is publishing the previous document again; its digest identifies
    which policy a recorded plan was resolved against.
    """
    snapshot = from_document(raw, base=base)
    location = Path(path)
    if location.is_symlink():
        raise PolicyDocumentError(f"policy document must not be a symlink: {location}")
    body = json.dumps(dict(raw), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    handle, temporary = tempfile.mkstemp(dir=str(location.parent), suffix=".policy-tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, location)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return snapshot.digest


def snapshots_agree(*snapshots: PolicySnapshot) -> bool:
    """True when every supplied snapshot is the same published policy."""
    digests = {s.digest for s in snapshots}
    return len(digests) <= 1


def require_same_policy(snapshots: Iterable[PolicySnapshot], what: str) -> PolicySnapshot:
    """Refuse a mixed-policy fleet: one resolution reads exactly one snapshot."""
    found = list(snapshots)
    if not found:
        raise PolicyDocumentError(f"{what}: no policy snapshot supplied")
    if not snapshots_agree(*found):
        raise PolicyDocumentError(
            f"{what}: parts were built against different policy snapshots "
            f"({', '.join(sorted({s.digest[:12] for s in found}))}); a request resolves "
            "against exactly one immutable policy"
        )
    return found[0]
