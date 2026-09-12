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


def test_register_describes_the_rules_that_are_actually_live():
    # Two separate claims have drifted from ruleset 20802441 already: first the
    # approval rule was described as unapplied, then the merge-authority check
    # as absent while it was required. Both are asserted against here.
    content = " ".join(policy.REGISTER_PATH.read_text(encoding="utf-8").split())
    assert "Until the approval rule is added" not in content
    assert "one approving review" in content
    assert "`aru-merge-authorized` is absent" not in content
    assert "The merge-authority App gate is enabled here" in content


# --- rendered agent rules and bootstrap ruleset (#695) ----------------------

def test_agents_kernel_path_matches_the_policy():
    current = policy.AGENTS_PATH.read_text(encoding="utf-8")
    assert current == policy.render_agents(current=current), (
        "AGENTS.md generated block has drifted from scripts/policy.toml.\n"
        "Edit the policy, then run:\n"
        "    python3 -c \"import sys; sys.path.insert(0,'scripts'); import policy; policy.write_agents()\""
    )


def test_rendering_agents_touches_only_the_generated_block():
    current = policy.AGENTS_PATH.read_text(encoding="utf-8")
    head = current.split(policy.BEGIN_MARK)[0]
    tail = current.split(policy.END_MARK)[1]
    rendered = policy.render_agents(current=current)
    assert rendered.split(policy.BEGIN_MARK)[0] == head
    assert rendered.split(policy.END_MARK)[1] == tail


def test_agents_without_markers_refuses(tmp_path):
    with pytest.raises(policy.PolicyError, match="marker pair"):
        policy.render_agents(current="# no markers here\n")
    with pytest.raises(policy.PolicyError, match="marker pair"):
        policy.render_agents(current=f"{policy.BEGIN_MARK}\nx\n{policy.BEGIN_MARK}\n{policy.END_MARK}\n")


def test_bootstrap_ruleset_is_built_from_the_policy():
    import init_project
    declared = policy.ruleset_parameters()
    payload = init_project.ruleset_payload()
    pull = next(r for r in payload["rules"] if r["type"] == "pull_request")["parameters"]
    assert payload["name"] == declared["name"]
    assert payload["bypass_actors"] == []
    assert pull["allowed_merge_methods"] == declared["allowed_merge_methods"] == ["merge"]
    assert pull["required_approving_review_count"] == 1
    assert pull["dismiss_stale_reviews_on_push"] is True
    assert pull["require_last_push_approval"] is False  # deliberate; see the policy rationale
    checks = next(r for r in payload["rules"] if r["type"] == "required_status_checks")["parameters"]
    contexts = [c["context"] for c in checks["required_status_checks"]]
    assert contexts == declared["required_checks"] == ["aru-governed-pr", "aru-merge-policy"]


def test_the_boundary_cannot_be_softened_by_a_policy_edit(tmp_path):
    # Every declared value, not only the three that used to be checked. A policy
    # edit that flipped any of these would scaffold a weaker rule into every
    # consumer repository with nothing refusing.
    base = policy.load()
    for key, value, match in (
        ("bypass_actors", [{"actor_id": 1}], "bypass_actors must stay empty"),
        ("required_approving_review_count", 0, "at least one approving review"),
        ("required_checks", [], "required_checks must include"),
        ("required_checks", ["aru-governed-pr"], "aru-merge-policy"),
        ("required_checks", ["lint"], "required_checks must include"),
        ("allowed_merge_methods", ["squash", "merge"], "merge method only"),
        ("allowed_merge_methods", ["rebase"], "merge method only"),
        ("dismiss_stale_reviews_on_push", False, "dismiss_stale_reviews_on_push must stay true"),
        ("required_review_thread_resolution", False, "required_review_thread_resolution must stay true"),
        ("require_extra_approval_for_unattributed_changes", False,
         "require_extra_approval_for_unattributed_changes must stay true"),
        ("require_last_push_approval", "no", "must be an explicit boolean"),
    ):
        weakened = {**base, "ruleset": {**base["ruleset"], key: value}}
        with pytest.raises(policy.PolicyError, match=match):
            policy.ruleset_parameters(weakened)


def test_every_declared_key_is_required_to_be_present():
    base = policy.load()
    for key in policy.RULESET_KEYS:
        without = {k: v for k, v in base["ruleset"].items() if k != key}
        with pytest.raises(policy.PolicyError, match=r"\[ruleset\] is missing"):
            policy.ruleset_parameters({**base, "ruleset": without})


# The pull-request parameters ruleset 20802441 actually carried on 2026-09-12,
# read from the live ruleset. Update this only alongside the declaration it
# mirrors -- that is the point of the guard.
LIVE_PULL_REQUEST_PARAMETERS = (
    "allowed_merge_methods",
    "dismiss_stale_reviews_on_push",
    "require_code_owner_review",
    "require_extra_approval_for_unattributed_changes",
    "require_last_push_approval",
    "required_approving_review_count",
    "required_review_thread_resolution",
    "required_reviewers",
)


def test_every_live_pull_request_parameter_is_declared():
    # A live parameter the policy never declares is a boundary nobody reviewed.
    # require_extra_approval_for_unattributed_changes sat undeclared while
    # governing how many approvals every pull request needed.
    undeclared = set(LIVE_PULL_REQUEST_PARAMETERS) - set(policy.RULESET_PULL_REQUEST_KEYS)
    assert not undeclared, f"live parameters the policy does not declare: {sorted(undeclared)}"
    assert LIVE_PULL_REQUEST_PARAMETERS, "an empty fixture would pass vacuously"


def test_the_scaffolded_rule_carries_every_declared_parameter():
    import init_project
    declared = policy.ruleset_parameters()
    payload = init_project.ruleset_payload()
    pull = next(r for r in payload["rules"] if r["type"] == "pull_request")["parameters"]
    for key in policy.RULESET_PULL_REQUEST_KEYS:
        assert key in pull, f"{key} is declared but never scaffolded"
        assert pull[key] == declared[key], key


def test_last_push_approval_is_deliberately_off():
    # Recorded decision 2026-09-12, not drift. The App pushes and a person
    # approves, so this rule demanded a second approver on every pull request.
    assert policy.ruleset_parameters()["require_last_push_approval"] is False
    register = " ".join(policy.REGISTER_PATH.read_text(encoding="utf-8").split())
    assert "is not required" in register


def test_missing_ruleset_table_refuses():
    base = policy.load()
    with pytest.raises(policy.PolicyError, match="missing a \\[ruleset\\] table"):
        policy.ruleset_parameters({k: v for k, v in base.items() if k != "ruleset"})


def test_kernel_path_steps_are_numbered_in_order():
    rendered = policy.render_kernel_path()
    numbers = [int(line.split(".")[0]) for line in rendered.split("\n") if line[:1].isdigit()]
    assert numbers == list(range(1, len(numbers) + 1))
    assert len(numbers) >= 7


# --- refusals map to declared gates, both directions (#697) -----------------

import re as _re  # noqa: E402

import merge_pr  # noqa: E402


def _refused_gates() -> set[str]:
    source = (ROOT / "scripts" / "merge_pr.py").read_text(encoding="utf-8")
    return set(_re.findall(r'refuse\(\s*"([a-z0-9-]+)"', source))


def test_every_merge_refusal_names_a_declared_gate():
    undeclared = _refused_gates() - policy.gate_ids()
    assert not undeclared, f"merge_pr.py refuses for gates the policy does not declare: {sorted(undeclared)}"


def test_every_gate_merge_pr_claims_to_enforce_actually_refuses():
    claimed = policy.gates_enforced_by("merge_pr.py")
    unenforced = claimed - _refused_gates()
    assert not unenforced, f"policy says merge_pr.py enforces these, but it never refuses: {sorted(unenforced)}"


def test_the_mapping_is_not_vacuous():
    assert len(_refused_gates()) >= 6
    assert policy.gates_enforced_by("merge_pr.py")
    assert policy.gates_enforced_by("nonexistent_helper.py") == set()


def test_a_refusal_carries_its_gate_at_runtime():
    with pytest.raises(merge_pr.GateRefusal) as caught:
        merge_pr.refuse("base-head-race", "boom")
    assert caught.value.gate == "base-head-race"
    assert str(caught.value) == "boom"
    # Subclasses KernelError, so every existing handler still catches it.
    from common import KernelError
    assert isinstance(caught.value, KernelError)


def test_a_refusal_can_chain_a_cause():
    with pytest.raises(merge_pr.GateRefusal) as caught:
        try:
            raise ValueError("root")
        except ValueError as exc:
            merge_pr.refuse("issue-done-and-cleanup", "wrapped", cause=exc)
    assert isinstance(caught.value.__cause__, ValueError)


def test_malformed_enforcers_refuse():
    base = policy.load()
    broken = {**base, "gate": [{**base["gate"][0], "enforcers": "merge_pr.py"}]}
    with pytest.raises(policy.PolicyError, match="malformed enforcers"):
        policy.gates_enforced_by("merge_pr.py", broken)
