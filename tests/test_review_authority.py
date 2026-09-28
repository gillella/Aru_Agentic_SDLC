"""The retired reviewer declaration cannot add a Kernel approval gate."""

from __future__ import annotations

import policy
import review_authority


def test_legacy_review_declaration_is_inert(tmp_path):
    legacy = tmp_path / ".aru" / "review.json"
    legacy.parent.mkdir()
    legacy.write_text('{"authority": "human", "reviewers": ["owner"]}', encoding="utf-8")
    assert review_authority.read_policy_text(cwd=tmp_path) is None
    legacy.write_text("malformed", encoding="utf-8")
    assert review_authority.read_policy_text(cwd=tmp_path) is None
    legacy.unlink()
    assert review_authority.read_policy_text(cwd=tmp_path) is None


def test_policy_register_declares_only_the_cross_account_review_gate():
    review_gates = {gate["id"] for gate in policy.gates()
                    if gate["id"].startswith("approval-") or "posture" in gate["id"]}
    assert review_gates == {"approval-by-another-account"}
    assert policy.ruleset_parameters()["required_reviewers"] == []
