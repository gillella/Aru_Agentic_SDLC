from __future__ import annotations

from pathlib import Path

import pytest

import policy


ROOT = Path(__file__).resolve().parents[1]


def test_register_is_byte_identical_to_the_rendered_policy():
    committed = policy.REGISTER_PATH.read_text(encoding="utf-8")
    assert committed == policy.render_register(), (
        "docs/ENFORCEMENT-REGISTER.md has drifted from scripts/policy.toml.\n"
        "The register is generated; do not hand-edit it. Edit the policy, then run:\n"
        f"    {policy.REGEN_COMMAND}"
    )


def test_every_gate_declares_the_required_fields():
    for gate in policy.gates():
        for field in policy.GATE_FIELDS:
            assert isinstance(gate.get(field), str) and gate[field].strip(), (gate, field)


def test_gate_ids_are_unique_and_slug_shaped():
    ids = [gate["id"] for gate in policy.gates()]
    assert len(ids) == len(set(ids))
    assert all(id_ == id_.lower() and " " not in id_ for id_ in ids)


def test_rendering_is_deterministic():
    loaded = policy.load()
    assert policy.render_register(loaded) == policy.render_register(loaded)


def test_policy_module_is_a_library_not_a_supported_command():
    # tests/test_surface.py caps supported commands at 14 and all 14 are in use,
    # so this module must never grow an entry point.
    assert 'if __name__ == "__main__"' not in (ROOT / "scripts" / "policy.py").read_text(encoding="utf-8")


def test_policy_reads_without_a_third_party_dependency():
    source = (ROOT / "scripts" / "policy.py").read_text(encoding="utf-8")
    assert "import tomllib" in source
    assert "import yaml" not in source


@pytest.mark.parametrize(
    "body",
    [
        "",                                              # no register, no gates
        '[register]\ntitle = "T"\ncolumns = ["a","b","c"]\n',  # declares no gates
        '[register]\ntitle = ""\ncolumns = ["a","b","c"]\n[[gate]]\nid="x"\ncontrol="c"\nblocks="b"\nmechanism="m"\n',
        'this is not toml [[[',                          # unparseable
    ],
)
def test_malformed_policy_refuses_rather_than_rendering_an_empty_register(body, tmp_path):
    bad = tmp_path / "policy.toml"
    bad.write_text(body, encoding="utf-8")
    with pytest.raises(policy.PolicyError):
        policy.load(bad)


def test_duplicate_gate_ids_are_refused(tmp_path):
    bad = tmp_path / "policy.toml"
    bad.write_text(
        '[register]\ntitle = "T"\ncolumns = ["a","b","c"]\n'
        '[[gate]]\nid="x"\ncontrol="c"\nblocks="b"\nmechanism="m"\n'
        '[[gate]]\nid="x"\ncontrol="c2"\nblocks="b2"\nmechanism="m2"\n',
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyError, match="duplicate gate id"):
        policy.load(bad)


def test_a_pipe_in_a_gate_field_is_refused(tmp_path):
    # A pipe would silently split the markdown row it renders into.
    bad = tmp_path / "policy.toml"
    bad.write_text(
        '[register]\ntitle = "T"\ncolumns = ["a","b","c"]\n'
        '[[gate]]\nid="x"\ncontrol="c | injected"\nblocks="b"\nmechanism="m"\n',
        encoding="utf-8",
    )
    with pytest.raises(policy.PolicyError, match="newline or pipe"):
        policy.load(bad)


def test_register_does_not_claim_the_approval_rule_is_unapplied():
    # The rule is live on ruleset 20802441; the register used to say it was not.
    content = " ".join(policy.REGISTER_PATH.read_text(encoding="utf-8").split())
    assert "Until the approval rule is added" not in content
    assert "one approving review" in content
    assert "`aru-merge-authorized` is absent" in content
