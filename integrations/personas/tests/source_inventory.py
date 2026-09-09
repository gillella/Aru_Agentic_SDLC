"""Exact static JSON inventory used by the repository's no-runtime-ledger guard.

Only the schema, catalog and six synthetic example inputs are source assets.
Pins bind the reviewed contents, including nested fields: a synthetic marker or
schema name alone cannot turn execution history into an allowed fixture.
Changing an asset requires reviewing its semantics and updating its pin here;
never generate this inventory during verification. Real observations and
verification receipts belong outside Git, regardless of their filename.
"""
from hashlib import sha256
import json
from pathlib import Path


STATIC_PERSONA_JSON = {
    "data/request.schema.json":
        "f198e462b4bae5c9c5b9eedaaffab98977b08c41fde379daa0ad96453e03f8f3",
    "data/route-catalogs.json":
        "12e8040aa8b080d78122ebe931e75fb0c3146954dbb38b13835cbabb548757fb",
    "examples/architecture-fallback.json":
        "30066d35e4ba4c1e09a29442342ff1c9df806c6b345e79bbf2abb8195bcc25c8",
    "examples/astra.json":
        "e3b3144ffe98d0899ff1b412e840b5cda2d77c5c369f2976eb81ef721bebe445",
    "examples/design.json":
        "e03c92630d8d1ecd87a08268e24eb18507f186953299e3c20be6b1e6f72e9185",
    "examples/refused-model.json":
        "1c81e993d809102569157e7a210ce5d5bcb3ed5cc200ac2e331c1bdc70495d04",
    "examples/review.json":
        "23091e5dc39cfd9fb0ce4c8f1f86fd04958864ee1e04b671dfce36a6986a0459",
    "examples/synthetic-binding.json":
        "5cf51e3c41cfe11c43bb3e14469be134f30f53b175f1780f306f83ca8573dcd5",
}


def assert_static_persona_json(path: Path, root: Path) -> None:
    """Refuse unknown paths, malformed assets and any changed fixture content."""
    relative = path.relative_to(root).as_posix()
    prefix = "integrations/personas/"
    name = relative.removeprefix(prefix)
    assert relative.startswith(prefix) and name in STATIC_PERSONA_JSON, relative
    assert not path.is_symlink(), relative
    content = path.read_bytes()
    raw = json.loads(content)
    assert isinstance(raw, dict), relative
    if name == "data/request.schema.json":
        assert raw["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert raw["type"] == "object" and raw["additionalProperties"] is False
        assert raw["properties"]["schema"] == {"const": "aru.personas.request/v1"}
    elif name == "data/route-catalogs.json":
        assert raw["schema"] == "aru.personas.route-catalog/v1"
        assert set(raw) == {"schema", "recorded_on", "source_report", "note", "routes", "entries"}
        assert raw["entries"] and all("id" in entry for entry in raw["entries"])
    elif name == "examples/synthetic-binding.json":
        assert_synthetic_binding(raw)
    else:
        assert raw["schema"] == "aru.personas.request/v1" and raw["synthetic"] is True
        assert raw["binding"] == "synthetic-binding.json"
        assert raw["context"]["worktree"] == "/synthetic/worktrees/issue-630"
        assert raw["context"]["head"] == "a" * 40
    assert sha256(content).hexdigest() == STATIC_PERSONA_JSON[name], f"changed static asset: {relative}"


def assert_synthetic_binding(raw: dict) -> None:
    assert raw["schema"] == "aru.personas.fleet-binding/v1"
    assert raw["harnesses"] and raw["accounts"]
    for harness in raw["harnesses"].values():
        assert harness["executable"].startswith("/synthetic/bin/")
        assert harness["workspace"] == "/synthetic/worktrees/issue-630"
    for account in raw["accounts"]:
        assert account["sessions_in_use"] == 0
        assert all(value.startswith("/synthetic/") for value in account["env"].values())
    evidence = raw["evidence"]
    assert evidence["origin"] == "SYNTHETIC TESTS ONLY"
    assert evidence["observations"]
    for observation in evidence["observations"]:
        assert observation["source"] == "SYNTHETIC test fixture; NOT live access"
        assert observation["observed_at"] == "2026-09-09T18:00:00+00:00"
