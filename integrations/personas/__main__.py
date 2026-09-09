"""Offline inspection CLI. Never launches a provider or establishes authority."""
import argparse
from dataclasses import asdict, fields
from datetime import datetime
import json
from pathlib import Path
import sys

from . import (AuthorIdentity, FleetBinding, TaskRequest, PromptContext,
               ReviewAssignment, PersonaPolicyError, resolve, plan_review, validate_registry)
from .registry import PERSONAS, ROLES, TASK_CLASSES
from .plan import policy_source_digest


def construct(cls, raw, converters=None):
    if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
        raise ValueError(f"unknown or malformed {cls.__name__} fields")
    values = dict(raw)
    for key, convert in (converters or {}).items():
        if key in values:
            values[key] = convert(values[key])
    return cls(**values)


def authors(items):
    return tuple(construct(AuthorIdentity, item) for item in items)


def explain_packet(raw, base, now=None):
    if not isinstance(raw, dict) or raw.get("schema") != "aru.personas.request/v1":
        raise ValueError("expected aru.personas.request/v1")
    if set(raw) - {"schema", "request", "binding", "context", "assignment", "current_assignment", "synthetic"}:
        raise ValueError("unknown request envelope field")
    binding_raw = raw["binding"]
    binding = (FleetBinding.load(base / binding_raw) if isinstance(binding_raw, str)
               else FleetBinding.from_dict(binding_raw, base))
    context = construct(PromptContext, raw["context"])
    if "assignment" in raw:
        if "request" in raw:
            raise ValueError("review assignment and ordinary request are mutually exclusive")
        conversions = {"authors": authors, "touches": tuple}
        assignment = construct(ReviewAssignment, raw["assignment"], conversions)
        snapshot = construct(ReviewAssignment, raw["current_assignment"], conversions)
        plan = plan_review(assignment, context.head, binding, context, now,
                           current_assignment=snapshot)
    else:
        request = construct(TaskRequest, raw["request"], {
            "labels": tuple, "touches": tuple, "modalities": frozenset,
            "author_history": authors, "input_files": tuple})
        plan = resolve(request, binding, context, now)
    return {"selected": plan.persona, "plan": plan.to_dict()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show the 13 personas, roles, efforts and task families")
    commands.add_parser("validate", help="validate shipped registry/catalog/archive consistency")
    explain = commands.add_parser("explain", help="compile an offline diagnostic plan or explain refusal")
    explain.add_argument("--input", type=Path, required=True)
    explain.add_argument("--at", help="ISO instant for synthetic replay; never live authority")
    args = parser.parse_args(argv)
    try:
        validate_registry()
        if args.command == "list":
            result = {"personas": [asdict(p) for p in PERSONAS.values()],
                      "roles": [asdict(r) for r in ROLES.values()],
                      "tasks": [asdict(t) for t in TASK_CLASSES.values()]}
        elif args.command == "validate":
            from .evidence import archived
            archive = archived()
            result = {"valid": True, "personas": len(PERSONAS),
                      "source_digest": policy_source_digest(),
                      "archived_observations": len(archive.records)}
        else:
            raw = json.loads(args.input.read_text())
            result = explain_packet(raw, args.input.resolve().parent,
                                    datetime.fromisoformat(args.at) if args.at else None)
        print(json.dumps({"execution_authority": False, **result}, indent=2, sort_keys=True,
                         default=lambda value: sorted(value) if isinstance(value, (set, frozenset)) else str(value)))
        return 0
    except (PersonaPolicyError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        result = {"execution_authority": False, "selected": None, "blocked": str(exc),
                  "code": getattr(exc, "code", "invalid-input")}
        if hasattr(exc, "skipped"):
            result["skipped"] = [s.to_dict() for s in exc.skipped]
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
