"""The typed decision and the validated command plan compiled from it.

A plan binds, in one frozen dataclass: the task, the classification and how it
was reached, the preferred persona, the persona actually selected and why, the
effective role, the exact model and effort, the harness, the account and its
shared capacity key, the project, the probe record that authorised it, the
argument array and the prompt.

The argument array is built element by element from validated registry data. The prompt contains quoted task context as a single argv element. Executable
basenames are route constrained; installation paths come from trusted operator
configuration. SHA-256 detects corruption; comparison with a separately resolved
trusted plan detects rehashed tampering. Neither a hash nor this object grants
execution permission.

This is a *plan*, not a launcher. The Driver keeps sole responsibility for
process lifecycle, capacity locks and supervision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from . import catalog
from .errors import HarnessBindingError, PlanTamperedError

PLAN_SCHEMA = "aru.personas.command-plan/v1"

#: Every file that can change a routing outcome. A plan records the digest of
#: this set so an audit can tell which policy source produced it, and so a plan
#: replayed against edited policy is visibly not the same decision.
POLICY_SOURCES: tuple[str, ...] = (
    "catalog.py", "classify.py", "errors.py", "plan.py", "policy.py", "prompt.py",
    "registry.py", "resolve.py", "review.py", "binding.py", "evidence.py",
    "lineage.py", "risk.py", "__init__.py", "__main__.py", "data/route-catalogs.json",
)


def policy_source_digest() -> str:
    """SHA-256 over the policy source files, in a stable order."""
    root = Path(__file__).resolve().parent
    running = hashlib.sha256()
    for name in POLICY_SOURCES:
        running.update(name.encode("utf-8"))
        running.update(b"\0")
        running.update((root / name).read_bytes())
        running.update(b"\0")
    return running.hexdigest()

#: The Driver's lane config marks the prompt slot with this token.
PROMPT_PLACEHOLDER = "{prompt}"


@dataclass(frozen=True)
class SkippedCandidate:
    """One approved candidate that was not selected, and exactly why."""

    persona: str
    account_id: str | None
    code: str
    reason: str
    #: "policy" (never eligible for this task) or "availability" (eligible but
    #: unusable right now). Only availability skips are what the amendment calls
    #: a bounded fallback trigger.
    kind: str

    def to_dict(self) -> dict:
        return {"persona": self.persona, "account_id": self.account_id,
                "code": self.code, "kind": self.kind, "reason": self.reason}


def _argv_element(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise HarnessBindingError(f"{what} must be a nonempty string")
    if "\x00" in value:
        raise HarnessBindingError(f"{what} contains a NUL byte")
    return value


def _base_args(spec, route_name: str, read_only: bool) -> list[str]:
    """Reviewer mode flips a documented per-route argument; it never adds a new one."""
    base = list(spec.base_args)
    if not read_only:
        return base
    if route_name == "codex":
        base[base.index("workspace-write")] = "read-only"
    elif route_name == "claude-code":
        base[base.index("acceptEdits")] = "plan"
    else:
        raise HarnessBindingError("route has no approved reviewer")
    return base


def _image_args(input_files: tuple[str, ...]) -> tuple[str, ...]:
    """Codex passes local references with ``--image``; each path stays literal and absolute."""
    argv: list[str] = []
    for path in input_files:
        if not Path(path).is_absolute() or "\x00" in path or ".." in Path(path).parts:
            raise HarnessBindingError("invalid input path")
        argv.extend(("--image", path))
    return tuple(argv)


def build_argv(route_name: str, executable: str, model_id: str, effort: str,
               workspace: str, prompt: str, *, read_only: bool = False,
               input_files: tuple[str, ...] = (), snapshot=None) -> tuple[str, ...]:
    """Compose one argument array from the route's own documented flag surface.

    Protocol, flag semantics and modality transport stay here, in reviewed source.
    Configuration decides *which* approved persona, model and effort reach this
    function; it never contributes an argument, an executable or a shell string.
    """
    from .binding import HarnessBinding
    from .policy import default_snapshot
    policy = snapshot or default_snapshot()
    HarnessBinding(route_name, executable, workspace)
    if not any(p.route == route_name and p.model_ids.get(effort) == model_id
               for p in policy.personas.values()):
        raise HarnessBindingError("command model/effort has no approved persona")
    policy.require_effort(route_name, model_id, effort)
    spec = catalog.route(route_name)
    argv: list[str] = [_argv_element(executable, "executable")]
    argv.extend(_base_args(spec, route_name, read_only))
    argv.extend((spec.model_flag, _argv_element(model_id, "model id")))
    if effort != "default":
        mechanism = spec.effort
        if mechanism.kind in {"flag", "config"}:
            argv.extend(part.replace("{effort}", effort) for part in mechanism.template)
        # `model_id` routes already carry the effort inside the identifier; a
        # second flag could contradict it, so none is emitted.
    if spec.workspace_flag:
        argv.extend((spec.workspace_flag, _argv_element(workspace, "workspace")))
    if route_name == "codex":
        argv.extend(_image_args(input_files))
    if spec.prompt_flag:
        argv.extend((spec.prompt_flag, _argv_element(prompt, "prompt")))
    else:
        argv.append(_argv_element(prompt, "prompt"))
    return tuple(argv)


def _digest(payload: Mapping) -> str:
    body = {k: v for k, v in payload.items() if k != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CommandPlan:
    """One fully validated, digest-bound decision. Fail-closed or absent."""

    # -- provenance ---------------------------------------------------------
    registry_version: str
    catalog_recorded_on: str
    source_digest: str
    created_at: str

    # -- what the work is ---------------------------------------------------
    project: str
    issue: int
    task_class: str
    risk_tier: int
    classification: Mapping
    touches: tuple[str, ...]

    # -- who does it, and why -----------------------------------------------
    preferred_persona: str
    persona: str
    effective_role: str
    acting: bool
    fallback_reason: str
    skipped: tuple[SkippedCandidate, ...]

    # -- exactly how --------------------------------------------------------
    route: str
    model_id: str
    effort: str
    effort_reason: str
    account_id: str
    capacity_key: str
    lineage: str
    executable: str
    workspace: str
    env: Mapping[str, str]
    argv: tuple[str, ...]
    prompt: str

    # -- what authorised it -------------------------------------------------
    evidence: Mapping
    expires_at: str

    author_history: tuple[Mapping, ...] = ()
    context: Mapping = field(default_factory=dict)
    input_files: tuple[str, ...] = ()
    review_assignment: Mapping = field(default_factory=dict)
    #: Which operator policy produced this decision. Work already running stays
    #: tied to the snapshot it was resolved against, even after publication.
    policy_version: str = ""
    policy_digest: str = ""
    policy_origin: str = ""
    digest: str = field(default="")

    def __post_init__(self) -> None:
        if not self.digest:
            object.__setattr__(self, "digest", _digest(self._body()))

    def _body(self) -> dict:
        return {
            "schema": PLAN_SCHEMA,
            "registry_version": self.registry_version,
            "catalog_recorded_on": self.catalog_recorded_on,
            "source_digest": self.source_digest,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "policy_origin": self.policy_origin,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "project": self.project,
            "issue": self.issue,
            "task_class": self.task_class,
            "risk_tier": self.risk_tier,
            "classification": dict(self.classification),
            "touches": list(self.touches),
            "preferred_persona": self.preferred_persona,
            "persona": self.persona,
            "effective_role": self.effective_role,
            "acting": self.acting,
            "fallback_reason": self.fallback_reason,
            "skipped": [s.to_dict() for s in self.skipped],
            "route": self.route,
            "model_id": self.model_id,
            "effort": self.effort,
            "effort_reason": self.effort_reason,
            "account_id": self.account_id,
            "capacity_key": self.capacity_key,
            "lineage": self.lineage,
            "executable": self.executable,
            "workspace": self.workspace,
            "env": dict(self.env),
            "argv": list(self.argv),
            "prompt": self.prompt,
            "evidence": dict(self.evidence),
            "author_history": [dict(a) for a in self.author_history],
            "context": dict(self.context), "input_files": list(self.input_files),
            "review_assignment": dict(self.review_assignment),
        }

    def to_dict(self) -> dict:
        return {**self._body(), "digest": self.digest}

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, ensure_ascii=False)

    def verify(self, expected: "CommandPlan", now: datetime | None = None) -> "CommandPlan":
        """Refuse a plan whose contents no longer match its own digest."""
        return verify_payload(self.to_dict(), expected=expected, now=now)

    def lane_template(self) -> dict:
        """This decision expressed in the external Driver's lane vocabulary.

        The Driver substitutes its own prompt token, so the argv it stores is
        identical to the compiled one except for the single prompt slot.
        """
        argv = list(self.argv)
        argv[argv.index(self.prompt)] = PROMPT_PLACEHOLDER
        return {
            "family": self.lineage,
            "capacity_key": self.capacity_key,
            "projects": [self.project],
            "command": argv,
            "max_sessions": 1,
            "cwd": self.workspace, "env": dict(self.env),
        }


def verify_payload(payload: Mapping, *, expected: CommandPlan, now: datetime | None = None) -> CommandPlan:
    """Compare against a freshly resolved trusted plan, including recomputed hashes.

    SHA-256 is integrity, not authentication. The expected plan must come from
    trusted live inputs, never from the payload being checked.
    """
    if payload.get("schema") != PLAN_SCHEMA:
        raise PlanTamperedError("plan payload has an unsupported schema")
    claimed = payload.get("digest")
    if not isinstance(claimed, str) or not claimed:
        raise PlanTamperedError("plan payload carries no digest")
    if _digest(payload) != claimed:
        raise PlanTamperedError(
            "plan digest does not match its contents; the command, prompt, persona, "
            "model, effort or account was altered after validation"
        )
    from .evidence import _instant
    moment = _instant(now or datetime.now(timezone.utc), "now")
    if not _instant(expected.created_at, "created_at") <= moment < _instant(expected.expires_at, "expires_at"):
        raise PlanTamperedError("plan expired or future-dated; re-resolve live inputs")
    if expected.source_digest != policy_source_digest():
        raise PlanTamperedError("policy source changed; re-resolve from trusted inputs")
    if _digest(expected._body()) != expected.digest:
        raise PlanTamperedError("trusted expected plan was mutated")
    if dict(payload) != expected.to_dict():
        raise PlanTamperedError("payload differs from the independently resolved trusted plan")
    return expected


def utc_now_iso(moment: datetime) -> str:
    return moment.isoformat()
