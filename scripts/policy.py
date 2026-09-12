"""Single source for the kernel's declared gates, and the register renderer.

`docs/ENFORCEMENT-REGISTER.md` is generated from `scripts/policy.toml`; the two
used to be maintained by hand and drifted. `tests/test_policy.py` fails when
they disagree.

This is a library module on purpose. It defines no ``__main__`` block, because
``tests/test_surface.py`` caps supported commands at 14 and all 14 are in use.
Regenerate with :data:`REGEN_COMMAND`.

Reads TOML with the standard library, so the kernel gains no runtime dependency.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
POLICY_PATH = SCRIPTS / "policy.toml"
REGISTER_PATH = SCRIPTS.parent / "docs" / "ENFORCEMENT-REGISTER.md"
AGENTS_PATH = SCRIPTS.parent / "AGENTS.md"

# The generated block in AGENTS.md. Prose outside these markers is hand-written
# and never touched by the renderer.
BEGIN_MARK = "<!-- BEGIN GENERATED: kernel-path -->"
END_MARK = "<!-- END GENERATED: kernel-path -->"
REGEN_COMMAND = "python3 -c \"import sys; sys.path.insert(0, 'scripts'); import policy; policy.write_register()\""

GATE_FIELDS = ("id", "control", "blocks", "mechanism")


class PolicyError(ValueError):
    """The gate declaration is missing, malformed, or ambiguous."""


def _validate_register(register: object) -> None:
    if not isinstance(register, dict):
        raise PolicyError("policy is missing a [register] table")
    if not isinstance(register.get("title"), str) or not register["title"].strip():
        raise PolicyError("[register] needs a non-empty title")
    columns = register.get("columns")
    if not isinstance(columns, list) or len(columns) != len(GATE_FIELDS) - 1:
        raise PolicyError(f"[register] needs {len(GATE_FIELDS) - 1} column headings")
    if not all(isinstance(c, str) and c.strip() for c in columns):
        raise PolicyError("[register] column headings must be non-empty strings")
    notes = register.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise PolicyError("[register] notes must be a list of strings")


def _validate_gates(declared: object) -> None:
    if not isinstance(declared, list) or not declared:
        raise PolicyError("policy declares no gates")
    seen: set[str] = set()
    for gate in declared:
        if not isinstance(gate, dict):
            raise PolicyError("each gate must be a table")
        for field in GATE_FIELDS:
            value = gate.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PolicyError(f"gate {gate.get('id', '<unnamed>')!r} needs a non-empty {field}")
            # A newline or pipe would silently break the markdown row it renders into.
            if "\n" in value or "|" in value:
                raise PolicyError(f"gate {gate['id']!r} field {field} may not contain a newline or pipe")
        if gate["id"] in seen:
            raise PolicyError(f"duplicate gate id: {gate['id']}")
        seen.add(gate["id"])


def load(path: Path | None = None) -> dict[str, Any]:
    """Parse and validate the declaration. A malformed file refuses; it never
    degrades to an empty gate set, which would render an empty register."""
    source = path or POLICY_PATH
    try:
        policy = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"gate declaration is unreadable: {exc}") from exc
    _validate_register(policy.get("register"))
    _validate_gates(policy.get("gate"))
    return policy


def gates(policy: dict[str, Any] | None = None) -> list[dict[str, str]]:
    return list((policy or load())["gate"])


def gate_ids(policy: dict[str, Any] | None = None) -> set[str]:
    return {gate["id"] for gate in gates(policy)}


def render_register(policy: dict[str, Any] | None = None) -> str:
    """Render the whole register. Output is the file, byte for byte."""
    policy = policy or load()
    register = policy["register"]
    columns = register["columns"]
    parts = [
        f"# {register['title']}",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    parts += [
        "| " + " | ".join((gate["control"], gate["blocks"], gate["mechanism"])) + " |"
        for gate in policy["gate"]
    ]
    for note in register.get("notes", []):
        parts += ["", note.strip("\n")]
    return "\n".join(parts) + "\n"


def write_register(policy: dict[str, Any] | None = None, path: Path | None = None) -> Path:
    target = path or REGISTER_PATH
    target.write_text(render_register(policy), encoding="utf-8")
    return target


def ruleset_parameters(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """The declared server-enforced merge boundary.

    `init_project.py` builds its ruleset payload from this, so the boundary a
    consumer gets and the boundary the register documents cannot disagree.
    """
    policy = policy or load()
    ruleset = policy.get("ruleset")
    if not isinstance(ruleset, dict):
        raise PolicyError("policy is missing a [ruleset] table")
    required = (
        "name", "allowed_merge_methods", "required_approving_review_count",
        "dismiss_stale_reviews_on_push", "require_last_push_approval",
        "required_review_thread_resolution", "require_code_owner_review",
        "bypass_actors", "required_checks",
    )
    missing = [key for key in required if key not in ruleset]
    if missing:
        raise PolicyError(f"[ruleset] is missing: {missing}")
    # These three are the boundary. A policy edit must not be able to soften them
    # silently, so the renderer refuses rather than scaffolding a weaker rule.
    if ruleset["bypass_actors"]:
        raise PolicyError("[ruleset] bypass_actors must stay empty")
    if int(ruleset["required_approving_review_count"]) < 1:
        raise PolicyError("[ruleset] must require at least one approving review")
    if not ruleset["required_checks"]:
        raise PolicyError("[ruleset] must require at least one status check")
    return dict(ruleset)


def render_kernel_path(policy: dict[str, Any] | None = None) -> str:
    """The numbered non-negotiable list, as it appears inside the markers."""
    policy = policy or load()
    path = policy.get("kernel_path")
    if not isinstance(path, dict) or not isinstance(path.get("steps"), list) or not path["steps"]:
        raise PolicyError("policy is missing [kernel_path] steps")
    steps = path["steps"]
    if not all(isinstance(s, str) and s.strip() for s in steps):
        raise PolicyError("[kernel_path] steps must be non-empty strings")
    return "\n".join(s.strip("\n") for s in steps)


def render_agents(policy: dict[str, Any] | None = None, current: str | None = None) -> str:
    """Replace only the generated block, leaving hand-written prose untouched."""
    text = current if current is not None else AGENTS_PATH.read_text(encoding="utf-8")
    if text.count(BEGIN_MARK) != 1 or text.count(END_MARK) != 1:
        raise PolicyError("AGENTS.md needs exactly one generated-block marker pair")
    head, rest = text.split(BEGIN_MARK, 1)
    _stale, tail = rest.split(END_MARK, 1)
    if not head.endswith("\n"):
        raise PolicyError("AGENTS.md generated block must start on its own line")
    return f"{head}{BEGIN_MARK}\n{render_kernel_path(policy)}\n{END_MARK}{tail}"


def write_agents(policy: dict[str, Any] | None = None) -> Path:
    AGENTS_PATH.write_text(render_agents(policy), encoding="utf-8")
    return AGENTS_PATH


def gates_enforced_by(helper: str, policy: dict[str, Any] | None = None) -> set[str]:
    """Gate ids the policy says this helper enforces.

    Makes the register's Mechanism column machine-checkable: a helper must refuse
    for every gate it claims, and must not refuse for a gate it does not declare.
    """
    found = set()
    for gate in gates(policy):
        enforcers = gate.get("enforcers", [])
        if not isinstance(enforcers, list) or not all(isinstance(e, str) for e in enforcers):
            raise PolicyError(f"gate {gate['id']!r} has a malformed enforcers list")
        if helper in enforcers:
            found.add(gate["id"])
    return found
