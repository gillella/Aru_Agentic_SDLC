"""Exact-model, exact-effort, account-scoped, expiring capability evidence.

Nothing in this package treats a catalog listing as capacity. A plan is only
compiled when a probe record exists for *exactly* this account, route, model and
effort, is younger than the freshness window, and actually succeeded.

Three separate refusals are kept distinct because they mean different things to
an operator:

* **missing** - nobody has ever probed this pair;
* **mismatched** - evidence exists, but for another account, model or effort;
* **stale** - evidence existed and has expired. It proves nothing now.

A failed probe is never rounded up to a success, and a success is never
synthesised from a catalog entry, a vendor announcement or another effort level.
Evidence shipped inside this repository is an archive: it records what was
observed on a date and is explicitly marked as unable to authorise execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .catalog import EFFORT_ORDER, ROUTES
from .errors import (
    CapabilityError, CapacityExhaustedError, CatalogError, CreditsRequiredError,
    MismatchedProbeError, MissingProbeError, ProbeFailedError, StaleProbeError,
)

EVIDENCE_SCHEMA = "aru.personas.capability-evidence/v1"

#: The one outcome that can authorise execution.
OK = "ok"

#: Every recognised failure outcome, and the refusal each one raises. An outcome
#: outside this table is rejected at load time rather than treated as success.
FAILURE_OUTCOMES: dict[str, type[CapabilityError]] = {
    "credits_required": CreditsRequiredError,
    "credits_exhausted": CreditsRequiredError,
    "session_limited": CapacityExhaustedError,
    "rate_limited": CapacityExhaustedError,
    "quota_exhausted": CapacityExhaustedError,
    "unauthorized": ProbeFailedError,
    "entitlement_missing": ProbeFailedError,
    "provider_unavailable": ProbeFailedError,
    "error": ProbeFailedError,
}

OUTCOMES = frozenset({OK, *FAILURE_OUTCOMES})

#: An authenticated bounded probe is cheap; treating a half-day-old one as proof
#: of current entitlement is not. The Driver may narrow this, never widen it.
DEFAULT_MAX_AGE = timedelta(hours=6)
MAX_ALLOWED_AGE = DEFAULT_MAX_AGE


def _instant(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CatalogError(f"{field} is not an ISO-8601 instant: {value!r}") from exc
    else:
        raise CatalogError(f"{field} must be an ISO-8601 instant")
    if moment.tzinfo is None:
        raise CatalogError(f"{field} must carry an explicit timezone offset")
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProbeRecord:
    """One bounded authenticated observation of one exact route pair."""

    account_id: str
    route: str
    model_id: str
    effort: str
    observed_at: datetime
    outcome: str
    detail: str = ""
    source: str = ""
    authenticated: bool = False
    identity_digest: str = ""
    modalities: frozenset[str] = frozenset({"text"})

    def __post_init__(self) -> None:
        if type(self.authenticated) is not bool:
            raise CatalogError("probe authenticated must be a boolean")
        if not self.modalities or not self.modalities <= {"text", "image"}:
            raise CatalogError("invalid probe modalities")
        if self.route not in ROUTES:
            raise CatalogError(f"probe names an unknown route: {self.route!r}")
        if self.effort not in EFFORT_ORDER:
            raise CatalogError(f"probe names an unassignable effort: {self.effort!r}")
        if self.outcome not in OUTCOMES:
            raise CatalogError(
                f"probe outcome {self.outcome!r} is not recognised; an unrecognised "
                "outcome is never read as success"
            )
        object.__setattr__(self, "observed_at", _instant(self.observed_at, "observed_at"))

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.account_id, self.route, self.model_id, self.effort)

    @property
    def succeeded(self) -> bool:
        return self.outcome == OK

    def age(self, now: datetime) -> timedelta:
        return _instant(now, "now") - self.observed_at

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id, "route": self.route,
            "model_id": self.model_id, "effort": self.effort,
            "observed_at": self.observed_at.isoformat(),
            "outcome": self.outcome, "detail": self.detail, "source": self.source,
            "authenticated": self.authenticated, "identity_digest": self.identity_digest,
            "modalities": sorted(self.modalities),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ProbeRecord":
        try:
            return cls(
                account_id=raw["account_id"], route=raw["route"],
                model_id=raw["model_id"], effort=raw["effort"],
                observed_at=raw["observed_at"], outcome=raw["outcome"],
                detail=raw.get("detail", ""), source=raw.get("source", ""),
                authenticated=raw.get("authenticated", False),
                identity_digest=raw.get("identity_digest", ""),
                modalities=frozenset(raw.get("modalities", ["text"])),
            )
        except KeyError as exc:
            raise CatalogError(f"probe record is missing {exc.args[0]!r}") from exc


@dataclass(frozen=True)
class EvidenceStore:
    """An immutable set of probe records plus the policy for reading them."""

    records: tuple[ProbeRecord, ...] = ()
    #: An archive shipped with the source cannot authorise a live execution, no
    #: matter how recent its timestamps look.
    authorizes_execution: bool = True
    max_age: timedelta = DEFAULT_MAX_AGE
    origin: str = "in-memory"
    note: str = ""

    def __post_init__(self) -> None:
        if type(self.authorizes_execution) is not bool:
            raise CatalogError("authorizes_execution must be boolean")
        for record in self.records:
            if not isinstance(record, ProbeRecord):
                raise CatalogError("invalid probe record")
        if self.max_age <= timedelta(0) or self.max_age > MAX_ALLOWED_AGE:
            raise CatalogError(
                f"probe freshness window must be within (0, {MAX_ALLOWED_AGE}]"
            )

    def with_records(self, *records: ProbeRecord) -> "EvidenceStore":
        return replace(self, records=self.records + tuple(records))

    def latest(self, account_id: str, route: str, model_id: str,
               effort: str) -> ProbeRecord | None:
        matches = [r for r in self.records
                   if r.key == (account_id, route, model_id, effort)]
        if not matches:
            return None
        newest = max(r.observed_at for r in matches)
        latest = [r for r in matches if r.observed_at == newest]
        if any(r != latest[0] for r in latest):
            raise ProbeFailedError("contradictory observations at the same instant")
        return latest[0]

    def history(self, model_id: str) -> tuple[ProbeRecord, ...]:
        return tuple(sorted((r for r in self.records if r.model_id == model_id),
                            key=lambda r: r.observed_at, reverse=True))

    def require(self, account_id: str, route: str, model_id: str, effort: str,
                now: datetime, *, identity_digest: str,
                modalities: frozenset[str] = frozenset({"text"})) -> ProbeRecord:
        """Return the one record that authorises this exact pair, or refuse."""
        if not self.authorizes_execution:
            raise MissingProbeError(
                f"{self.origin} is a recorded archive and never authorises execution; "
                "supply a live probe record for "
                f"{account_id}/{route}/{model_id}/{effort}"
            )
        found = self.latest(account_id, route, model_id, effort)
        if found is None:
            self._explain_absence(account_id, route, model_id, effort)
        if not found.authenticated or not found.source:
            raise ProbeFailedError("probe lacks authenticated provenance")
        if not identity_digest or found.identity_digest != identity_digest:
            raise MismatchedProbeError("probe belongs to a different authentication profile")
        if not modalities <= found.modalities:
            raise ProbeFailedError("exact model/effort probe lacks required modalities")
        # A later account-wide quota failure invalidates older successes for
        # sibling models on that same subscription. Model-specific entitlement
        # or credits-required failures do not imply all sibling models failed.
        capacity_failures = [r for r in self.records
            if r.account_id == account_id and r.route == route and r.authenticated
            and r.identity_digest == identity_digest and r.source
            and r.outcome in {"session_limited", "rate_limited", "quota_exhausted", "unauthorized"}
            and r.observed_at >= found.observed_at]
        if capacity_failures:
            raise CapacityExhaustedError("authenticated account-wide failure blocks this shared quota")
        age = found.age(now)
        if age >= self.max_age:
            raise StaleProbeError(
                f"the {model_id} {effort} probe on {account_id} is {age} old and the "
                f"freshness window is {self.max_age}; expired evidence proves nothing"
            )
        if age < timedelta(0):
            raise StaleProbeError(
                f"the {model_id} {effort} probe on {account_id} is timestamped in the "
                "future; refusing rather than trusting an unordered clock"
            )
        if not found.succeeded:
            raise FAILURE_OUTCOMES[found.outcome](
                f"the {model_id} {effort} probe on {account_id} did not succeed "
                f"({found.outcome}): {found.detail or 'no detail recorded'}"
            )
        return found

    def _explain_absence(self, account_id: str, route: str, model_id: str,
                         effort: str) -> None:
        same_model = [r for r in self.records
                      if (r.account_id, r.route, r.model_id) == (account_id, route, model_id)]
        if same_model:
            raise MismatchedProbeError(
                f"{account_id} has {model_id} evidence only for effort "
                f"{', '.join(sorted({r.effort for r in same_model}))}, not {effort!r}; "
                "one effort is never evidence for another"
            )
        other_accounts = [r for r in self.records
                          if (r.route, r.model_id, r.effort) == (route, model_id, effort)]
        if other_accounts:
            raise MismatchedProbeError(
                f"{model_id} {effort} evidence exists only for account "
                f"{', '.join(sorted({r.account_id for r in other_accounts}))}, "
                f"not {account_id}; entitlement is per account"
            )
        raise MissingProbeError(
            f"no probe has ever recorded {model_id} at effort {effort} on {account_id}"
        )

    def to_dict(self) -> dict:
        return {
            "schema": EVIDENCE_SCHEMA,
            "authorizes_execution": self.authorizes_execution,
            "max_age_seconds": int(self.max_age.total_seconds()),
            "origin": self.origin,
            "note": self.note,
            "observations": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, raw: dict, origin: str = "in-memory") -> "EvidenceStore":
        if raw.get("schema") != EVIDENCE_SCHEMA:
            raise CatalogError("capability evidence has an unsupported schema")
        if "authorizes_execution" not in raw:
            raise CatalogError(
                "capability evidence must state authorizes_execution explicitly; "
                "an unstated intent is never read as authorisation"
            )
        if type(raw["authorizes_execution"]) is not bool:
            raise CatalogError("authorizes_execution must be a boolean")
        seconds = raw.get("max_age_seconds", int(DEFAULT_MAX_AGE.total_seconds()))
        if type(seconds) is not int:
            raise CatalogError("max_age_seconds must be an integer")
        records = raw.get("observations")
        if not isinstance(records, list):
            raise CatalogError("capability evidence must carry an observations list")
        return cls(
            records=tuple(ProbeRecord.from_dict(item) for item in records),
            authorizes_execution=raw["authorizes_execution"],
            max_age=timedelta(seconds=seconds),
            origin=raw.get("origin", origin),
            note=raw.get("note", ""),
        )

    @classmethod
    def load(cls, path: str | Path) -> "EvidenceStore":
        location = Path(path)
        try:
            raw = json.loads(location.read_text())
        except (OSError, ValueError) as exc:
            raise CatalogError(f"capability evidence is unreadable: {exc}") from exc
        return cls.from_dict(raw, origin=str(location))


ARCHIVE_DIR = Path(__file__).resolve().parent / "evidence"


def archived() -> EvidenceStore:
    """Every dated observation shipped with this source, as a non-authorising set."""
    records: list[ProbeRecord] = []
    origins: list[str] = []
    for path in sorted(ARCHIVE_DIR.glob("*.json")):
        store = EvidenceStore.load(path)
        if store.authorizes_execution:
            raise CatalogError(f"{path.name}: shipped evidence must not claim authority")
        records.extend(store.records)
        origins.append(path.name)
    return EvidenceStore(
        records=tuple(records), authorizes_execution=False,
        origin="packaged archive (" + ", ".join(origins) + ")",
        note=("Dated observations only. They explain what was seen on a date and never "
              "authorise a run; the Driver supplies live probe records at execution time."),
    )
