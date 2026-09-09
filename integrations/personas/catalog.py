"""Recorded route catalogs and the *validated* harness flag surfaces.

Two independent facts live here and are never conflated:

* **Catalog visibility** — an identifier the provider's own catalog listed on
  2026-09-09 (``data/route-catalogs.json``). It proves the identifier exists.
  It never proves the account may run it; that is capability evidence.
* **Flag surface** — the arguments each installed CLI actually documents. Every
  flag below was read from ``--help`` of the installed binary on 2026-09-09 and
  is recorded with the version it was read from. No flag is invented, and no
  requested effort is silently translated into a different one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import CatalogError, UnsupportedEffortError, UnsupportedModelError

DATA = Path(__file__).resolve().parent / "data" / "route-catalogs.json"

#: Efforts this package will ever assign, weakest first. ``default`` means
#: "emit no effort selection at all". ``max`` and ``ultra`` appear in provider
#: catalogs but are deliberately never assignable: the approved scope forbids
#: recursive max/ultra worker fan-out.
EFFORT_ORDER: tuple[str, ...] = ("default", "low", "medium", "high", "xhigh")
NEVER_ASSIGNABLE_EFFORTS: frozenset[str] = frozenset({"max", "ultra"})

#: Catalog identifiers that exist but must never receive a persona.
EXCLUDED_MODEL_IDS: frozenset[str] = frozenset({
    "auto",             # dynamic router, not a fixed-model assignment
    "gpt-reserve",      # hidden internal catalog entry
    "codex-auto-review",  # hidden approval component, not governed PR review
})

MODALITIES: frozenset[str] = frozenset({"text", "image"})


def effort_rank(effort: str) -> int:
    try:
        return EFFORT_ORDER.index(effort)
    except ValueError as exc:  # pragma: no cover - guarded by validation
        raise UnsupportedEffortError(f"unknown effort: {effort!r}") from exc


@dataclass(frozen=True)
class EffortMechanism:
    """How one route expresses reasoning effort, as its CLI actually documents."""

    kind: str          # "flag" | "config" | "model_id"
    template: tuple[str, ...] = ()   # argv fragment; "{effort}" is substituted
    accepts: tuple[str, ...] = ()    # levels the CLI documents for `kind=flag`


@dataclass(frozen=True)
class Route:
    """One installed coding harness."""

    name: str
    cli: str
    version_read: str
    executables: frozenset[str]      # allowed argv[0] basenames
    model_flag: str
    effort: EffortMechanism
    base_args: tuple[str, ...]       # non-interactive, no-shell invocation
    #: ``None`` when the CLI takes the prompt as its final positional argument;
    #: otherwise the flag whose *value* is the prompt. Both forms were read from
    #: the installed binary's own argument-arity error, not assumed.
    prompt_flag: str | None
    workspace_flag: str | None
    env_allowlist: frozenset[str]
    proven_modalities: frozenset[str]
    help_evidence: str


#: ``claude --help`` (2.1.266) documents ``--model <model>`` and
#: ``--effort <level>`` with exactly these five levels.
CLAUDE_CODE = Route(
    name="claude-code",
    cli="claude",
    version_read="2.1.266",
    executables=frozenset({"claude"}),
    model_flag="--model",
    effort=EffortMechanism(kind="flag", template=("--effort", "{effort}"),
                           accepts=("low", "medium", "high", "xhigh", "max")),
    base_args=("--print", "--permission-mode", "acceptEdits", "--permission-prompts", "none"),
    prompt_flag=None,
    workspace_flag="--add-dir",
    env_allowlist=frozenset({"CLAUDE_CONFIG_DIR"}),
    # `--help` documents no local image/attachment input for this CLI.
    proven_modalities=frozenset({"text"}),
    help_evidence=("claude --help: --print, --permission-mode, --permission-prompts, "
                   "--add-dir, --model, --effort <level> (low, medium, high, xhigh, max)"),
)

#: ``codex exec --help`` (codex-cli 0.153.4) documents ``-m/--model`` and
#: ``-i/--image``, and no ``--effort`` flag. Effort is a config override; the
#: key ``model_reasoning_effort`` is present in the installed binary and in the
#: operator's own ``~/.codex/config.toml``.
CODEX = Route(
    name="codex",
    cli="codex",
    version_read="codex-cli 0.153.4",
    executables=frozenset({"codex"}),
    model_flag="--model",
    effort=EffortMechanism(kind="config",
                           template=("-c", "model_reasoning_effort={effort}")),
    base_args=("exec", "--sandbox", "workspace-write", "--color", "never"),
    prompt_flag=None,
    workspace_flag="--cd",
    env_allowlist=frozenset({"CODEX_HOME"}),
    proven_modalities=frozenset({"text", "image"}),
    help_evidence=("codex exec --help: -m/--model, -i/--image, -C/--cd, --sandbox, "
                   "--color; -c model_reasoning_effort=<level>"),
)

#: ``cursor-agent --help`` (2026.09.08-6caf4ff) documents ``--model`` only;
#: effort is encoded in the catalog identifier itself.
CURSOR = Route(
    name="cursor",
    cli="cursor-agent",
    version_read="2026.09.08-6caf4ff",
    executables=frozenset({"cursor-agent"}),
    model_flag="--model",
    effort=EffortMechanism(kind="model_id"),
    base_args=("--print", "--output-format", "text"),
    prompt_flag=None,
    workspace_flag="--workspace",
    env_allowlist=frozenset(),
    proven_modalities=frozenset({"text"}),
    help_evidence=("cursor-agent --help: --print, --output-format, --workspace, --model "
                   "(effort encoded in the model id); `cursor-agent --print` errors "
                   "'No prompt provided for print mode', so the prompt is positional"),
)

#: ``agy --help`` (1.1.28) documents ``--model``, ``--mode accept-edits`` and
#: ``--effort (low|medium|high)``. The catalog identifiers already encode effort,
#: so this package selects the encoded identifier and never emits a second,
#: possibly contradicting, flag. ``agy --print`` reports "flag needs an argument",
#: so the prompt is this flag's value rather than a positional argument.
ANTIGRAVITY = Route(
    name="antigravity",
    cli="agy",
    version_read="1.1.28",
    executables=frozenset({"agy"}),
    model_flag="--model",
    effort=EffortMechanism(kind="model_id", accepts=("low", "medium", "high")),
    base_args=("--mode", "accept-edits", "--output-format", "text"),
    prompt_flag="--print",
    workspace_flag="--add-dir",
    env_allowlist=frozenset(),
    proven_modalities=frozenset({"text"}),
    help_evidence=("agy --help: --model, --mode accept-edits, --output-format, "
                   "--effort (low|medium|high); catalog ids encode effort; "
                   "`agy --print` errors 'flag needs an argument'"),
)

ROUTES: Mapping[str, Route] = {r.name: r for r in (CLAUDE_CODE, CODEX, CURSOR, ANTIGRAVITY)}


def route(name: str) -> Route:
    try:
        return ROUTES[name]
    except KeyError as exc:
        raise CatalogError(f"unknown route: {name!r}") from exc


@dataclass(frozen=True)
class CatalogEntry:
    route: str
    id: str
    name: str
    category: str
    visibility: str
    efforts: tuple[str, ...] | None   # provider-declared levels, when published

    @property
    def is_fast_variant(self) -> bool:
        """``-fast`` is a speed/billing option, never a distinct assignment."""
        return self.id.endswith("-fast")

    @property
    def assignable(self) -> bool:
        return (self.visibility == "list" and not self.is_fast_variant
                and self.id not in EXCLUDED_MODEL_IDS)


def _catalog() -> tuple[dict, Mapping[tuple[str, str], CatalogEntry]]:
    try:
        raw = json.loads(DATA.read_text())
    except (OSError, ValueError) as exc:
        raise CatalogError(f"recorded route catalog is unreadable: {exc}") from exc
    if raw.get("schema") != "aru.personas.route-catalog/v1":
        raise CatalogError("recorded route catalog has an unsupported schema")
    index: dict[tuple[str, str], CatalogEntry] = {}
    for item in raw["entries"]:
        efforts = item.get("efforts")
        entry = CatalogEntry(
            route=item["route"], id=item["id"], name=item.get("name", item["id"]),
            category=item.get("category", "unmapped"),
            visibility=item.get("visibility", "list"),
            efforts=tuple(efforts) if efforts else None,
        )
        index[(entry.route, entry.id)] = entry
    return raw, index


def catalog_metadata() -> dict:
    meta, _ = _catalog()
    return {k: v for k, v in meta.items() if k != "entries"}


def entries() -> tuple[CatalogEntry, ...]:
    _, index = _catalog()
    return tuple(index.values())


def entry(route_name: str, model_id: str) -> CatalogEntry:
    _, index = _catalog()
    try:
        return index[(route_name, model_id)]
    except KeyError as exc:
        raise UnsupportedModelError(
            f"{model_id!r} is not a recorded {route_name} catalog identifier"
        ) from exc


def require_assignable(route_name: str, model_id: str) -> CatalogEntry:
    """Refuse hidden, router, legacy-excluded and ``-fast`` billing identifiers."""
    found = entry(route_name, model_id)
    if found.id in EXCLUDED_MODEL_IDS:
        raise UnsupportedModelError(f"{model_id!r} is an excluded catalog entry, not a persona")
    if found.is_fast_variant:
        raise UnsupportedModelError(
            f"{model_id!r} is a fast speed/billing variant and is never assignable"
        )
    if found.visibility != "list":
        raise UnsupportedModelError(f"{model_id!r} is a hidden catalog entry")
    return found


def require_effort(route_name: str, model_id: str, effort: str) -> None:
    """Prove the route and, when published, the model itself accept this effort."""
    if effort in NEVER_ASSIGNABLE_EFFORTS:
        raise UnsupportedEffortError(
            f"effort {effort!r} is never assigned by this package (no max/ultra fan-out)"
        )
    if effort not in EFFORT_ORDER:
        raise UnsupportedEffortError(f"unknown effort: {effort!r}")
    found = require_assignable(route_name, model_id)
    mechanism = route(route_name).effort
    if mechanism.kind == "model_id":
        encoded = model_id.rsplit("-", 1)[-1]
        if (model_id == "composer-2.5" and effort != "default") or (
                model_id != "composer-2.5" and encoded != effort):
            raise UnsupportedEffortError("effort contradicts the exact route model identifier")
    if effort == "default":
        if model_id not in {"composer-2.5", "claude-haiku-4-5-20251001"}:
            raise UnsupportedEffortError("this model requires an explicit effort")
        return
    if found.efforts is not None and effort not in found.efforts:
        raise UnsupportedEffortError(
            f"{route_name}:{model_id} publishes no {effort!r} reasoning level "
            f"(published: {', '.join(found.efforts)})"
        )
    if mechanism.kind == "flag" and effort not in mechanism.accepts:
        raise UnsupportedEffortError(
            f"the {route_name} CLI does not document effort {effort!r} "
            f"(documented: {', '.join(mechanism.accepts)})"
        )
