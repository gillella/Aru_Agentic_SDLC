from __future__ import annotations

import pytest

import common


def record(body: str, labels: list[str] | None = None, state: str = "OPEN") -> dict:
    return {
        "number": 7,
        "body": body,
        "state": state,
        "labels": [{"name": name} for name in labels or []],
    }


def test_issue_contract_accepts_one_safe_budget_and_unchecked_criterion():
    body = """
## Acceptance Criteria

- [ ] behavior is observable

touches: scripts/a.py, docs/**
"""
    assert common.contract_errors(record(body)) == []
    assert common.parse_touches(body) == ["scripts/a.py", "docs/**"]
    assert common.acceptance_items(body) == [(False, "behavior is observable")]


@pytest.mark.parametrize(
    "declaration",
    [
        "touches: ../secret",
        "touches: /tmp/file",
        "touches: -rf",
        "touches: ~user/file",
        "touches:",
    ],
)
def test_issue_contract_rejects_unsafe_budgets(declaration):
    body = f"## Acceptance Criteria\n\n- [ ] done\n\n{declaration}\n"
    assert common.contract_errors(record(body))


def test_path_budget_is_exact_or_recursive():
    declared = ["scripts/a.py", "docs/**"]
    assert common.path_allowed("scripts/a.py", declared)
    assert common.path_allowed("docs/guide/one.md", declared)
    assert not common.path_allowed("scripts/b.py", declared)
    assert not common.path_allowed("../docs/guide.md", declared)


def test_status_fails_closed_on_contradiction():
    issue = record("", ["status:ready", "status:in-progress"])
    with pytest.raises(common.KernelError, match="contradictory"):
        common.status_of(issue)


def test_dependencies_are_unique_and_ordered():
    body = "depends-on: #9\ndepends-on: #2\ndepends-on: #9\n"
    assert common.dependencies(body) == [2, 9]
