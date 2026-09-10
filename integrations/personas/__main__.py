"""Offline inspection CLI. Never launches a provider or establishes authority."""
import argparse
from dataclasses import asdict, fields
from datetime import datetime
import json
from pathlib import Path
import sys

from . import (AuthorIdentity, FleetBinding, TaskRequest, PromptContext,
               ReviewAssignment, PersonaPolicyError, resolve, plan_review, validate_registry)
from .policy import (KERNEL_INVARIANTS, default_snapshot, load_policy_document,
                     preview, publish)
from .plan import policy_source_digest


def construct(cls, raw, converters=None):
    if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
        raise ValueError(f"unknown or malformed {cls.__name__} fields")
    values = dict(raw)
    for key, convert in (converters or {}).items():
        if key in values:
            values[key] = convert(values[key])
    return cls(**values)


def authors(items, snapshot=None):
    return tuple(construct(AuthorIdentity, {**item, "snapshot": snapshot or default_snapshot()})
                 for item in items)


def explain_packet(raw, base, now=None):
    if not isinstance(raw, dict) or raw.get("schema") != "aru.personas.request/v1":
        raise ValueError("expected aru.personas.request/v1")
    if set(raw) - {"schema", "request", "binding", "context", "assignment", "current_assignment", "synthetic"}:
        raise ValueError("unknown request envelope field")
    binding_raw = raw["binding"]
    binding = (FleetBinding.load(base / binding_raw) if isinstance(binding_raw, str)
               else FleetBinding.from_dict(binding_raw, base))
    context = construct(PromptContext, raw["context"])

    def convert_authors(items):
        return authors(items, binding.snapshot)

    if "assignment" in raw:
        if "request" in raw:
            raise ValueError("review assignment and ordinary request are mutually exclusive")
        conversions = {"authors": convert_authors, "touches": tuple}
        assignment = construct(ReviewAssignment, raw["assignment"], conversions)
        snapshot = construct(ReviewAssignment, raw["current_assignment"], conversions)
        plan = plan_review(assignment, context.head, binding, context, now,
                           current_assignment=snapshot)
    else:
        request = construct(TaskRequest, raw["request"], {
            "labels": tuple, "touches": tuple, "modalities": frozenset,
            "author_history": convert_authors, "input_files": tuple})
        plan = resolve(request, binding, context, now)
    return {"selected": plan.persona, "plan": plan.to_dict()}


def read_document(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("policy document must be a JSON object")
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="show the personas, roles, efforts and task families")
    listing.add_argument("--policy", type=Path, help="operator policy document to read instead of the defaults")
    validate = commands.add_parser("validate", help="validate registry/catalog/archive/policy consistency")
    validate.add_argument("--policy", type=Path, help="operator policy document to validate")
    explain = commands.add_parser("explain", help="compile an offline diagnostic plan or explain refusal")
    explain.add_argument("--input", type=Path, required=True)
    explain.add_argument("--at", help="ISO instant for synthetic replay; never live authority")
    export = commands.add_parser("policy-export",
                                 help="write the current policy out as a document, for editing")
    export.add_argument("--policy", type=Path, help="operator policy document to export instead of the defaults")
    diff = commands.add_parser("policy-preview",
                               help="explain what publishing a document would change; changes nothing")
    diff.add_argument("--policy", type=Path, required=True)
    publication = commands.add_parser(
        "policy-publish", help="validate a document and replace the live one atomically")
    publication.add_argument("--policy", type=Path, required=True)
    publication.add_argument("--to", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        validate_registry()
        snapshot = (load_policy_document(args.policy)
                    if getattr(args, "policy", None) and args.command in
                    {"list", "validate", "policy-export"} else default_snapshot())
        if args.command == "list":
            result = {"policy": snapshot.summary(),
                      "personas": [asdict(p) for p in snapshot.personas.values()],
                      "roles": [asdict(r) for r in snapshot.roles.values()],
                      "tasks": [asdict(t) for t in snapshot.task_classes.values()]}
        elif args.command == "validate":
            from .evidence import archived
            archive = archived()
            result = {"valid": True, "personas": len(snapshot.personas),
                      "source_digest": policy_source_digest(),
                      "policy": snapshot.validate().summary(),
                      "kernel_invariants": list(KERNEL_INVARIANTS),
                      "archived_observations": len(archive.records)}
        elif args.command == "policy-export":
            result = {"policy": snapshot.summary(), "document": snapshot.to_document()}
        elif args.command == "policy-preview":
            result = {"preview": preview(read_document(args.policy))}
        elif args.command == "policy-publish":
            document = read_document(args.policy)
            result = {"published": True, "path": str(args.to),
                      "digest": publish(document, args.to),
                      "rollback": "republish the previous document; the digest names the live policy"}
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
