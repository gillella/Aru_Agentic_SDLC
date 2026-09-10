"""Regression probes for live board and dependency authorization at merge."""

from copy import deepcopy
import subprocess

import pytest

import common
import merge_pr
import merge_state
from review_risk import review_risk_tier
from test_governed_merge import BASE, HEAD, board_evidence, issue_record, ready_pr


def install_authority_world(monkeypatch):
    world = {
        "pr": ready_pr(),
        "issue": issue_record("src/example.py"),
        "board": board_evidence(),
        "dependencies": {99: {"number": 99, "state": "CLOSED"}},
        "commands": [],
    }
    world["issue"]["body"] += "\n## Dependencies\ndepends-on: #99\n"
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("external call"))
    monkeypatch.setattr(merge_pr, "pull_request", lambda _n: deepcopy(world["pr"]))
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _n: ["src/example.py"])
    monkeypatch.setattr(merge_state, "issue", lambda n: deepcopy(
        world["issue"] if n == 7 else world["dependencies"][n]))

    def board(_number):
        if world["board"] is None:
            raise common.KernelError("linked Project card unavailable or missing")
        return deepcopy(world["board"])

    monkeypatch.setattr(merge_state, "project_item_evidence", board)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _n: [])
    monkeypatch.setattr(merge_pr, "merge_queue_snapshot", lambda *_a: {
        "configured": False, "entry": None, "auto_merge": None,
    })
    monkeypatch.setattr(merge_pr, "ci_verdict", lambda _n: {
        "head": HEAD, "state": "success", "checks": ["aru-governed-pr"],
    })

    def submit(command):
        world["commands"].append(command)
        raise RuntimeError("submission boundary reached")

    monkeypatch.setattr(merge_pr, "run", submit)
    return world


@pytest.mark.parametrize("mutation", [
    "board-backlog", "board-empty", "board-missing", "project-identity",
    "card-identity", "field-identity", "dependency-reopened", "dependency-unreadable",
    "dependency-added", "dependency-removed", "dependency-malformed", "stable",
])
@pytest.mark.parametrize("ci_read", [1, 2])
def test_live_authority_drift_blocks_before_submission(monkeypatch, mutation, ci_read):
    world = install_authority_world(monkeypatch)
    reads = 0

    def ci(_number):
        nonlocal reads
        reads += 1
        if reads == ci_read:
            if mutation in {"board-backlog", "board-empty"}:
                world["board"]["status"] = "Backlog" if mutation == "board-backlog" else None
            elif mutation == "board-missing":
                world["board"] = None
            elif mutation in {"project-identity", "card-identity", "field-identity"}:
                key = {"project-identity": "project_id", "card-identity": "item_id",
                       "field-identity": "status_field_id"}[mutation]
                world["board"][key] += "-replacement"
            elif mutation in {"dependency-reopened", "dependency-unreadable"}:
                world["dependencies"][99]["state"] = (
                    "OPEN" if mutation == "dependency-reopened" else None)
            elif mutation == "dependency-added":
                world["dependencies"][100] = {"number": 100, "state": "CLOSED"}
                world["issue"]["body"] += "depends-on: #100\n"
            elif mutation == "dependency-removed":
                world["issue"]["body"] = world["issue"]["body"].replace("depends-on: #99", "")
            elif mutation == "dependency-malformed":
                world["issue"]["body"] += "depends-on: malformed\n"
        return {"head": HEAD, "state": "success", "checks": ["aru-governed-pr"]}

    monkeypatch.setattr(merge_pr, "ci_verdict", ci)
    if mutation == "stable":
        with pytest.raises(RuntimeError, match="submission boundary reached"):
            merge_pr.merge(10, HEAD)
        assert len(world["commands"]) == 1
    else:
        with pytest.raises(common.KernelError):
            merge_pr.merge(10, HEAD)
        assert world["commands"] == []
        assert ci_read <= reads <= 2


@pytest.mark.parametrize("status", ["In Review", "Done"])
def test_valid_historical_direct_merge_finalizes_with_current_board(monkeypatch, status):
    world = install_authority_world(monkeypatch)
    world["pr"].update(state="MERGED", mergedAt="2026-09-10T12:00:00Z",
                       mergeCommit={"oid": BASE})
    world["issue"]["state"] = "CLOSED"
    world["issue"]["labels"][0]["name"] = common.status_label(status)
    world["board"]["status"] = status
    monkeypatch.setattr(merge_pr, "finalization_verdict", lambda _pr: {
        "head": HEAD, "state": "success", "checks": ["aru-governed-pr"],
    })

    def move(_number, target, *, expected_current):
        assert expected_current == world["board"]["status"] == "In Review"
        world["board"]["status"] = target
        world["issue"]["labels"][0]["name"] = common.status_label(target)

    monkeypatch.setattr(merge_state, "set_status", move)
    result = merge_pr.finalize_queued(10, HEAD)
    assert result["finalized"] is True
    assert world["board"]["status"] == "Done"
    assert world["commands"] == []


def test_finalization_refuses_missing_board_before_mutation(monkeypatch):
    world = install_authority_world(monkeypatch)
    world["pr"].update(state="MERGED", mergedAt="2026-09-10T12:00:00Z")
    world["board"] = None
    monkeypatch.setattr(merge_state, "set_status", lambda *_a, **_k: pytest.fail("status mutation"))
    with pytest.raises(common.KernelError, match="Project card"):
        merge_pr.finalize_queued(10, HEAD)
    assert world["commands"] == []


@pytest.mark.parametrize("path,tier", [
    ("src/README.py", 1), ("src/CHANGELOG.sh", 1), ("src/LICENSE.js", 1),
    ("README", 0), ("README.md", 0), ("CHANGELOG.rst", 0), ("LICENSE", 0),
    ("README-unknown", 2), ("scripts/README.py", 2),
])
def test_documentation_names_do_not_hide_executable_code(path, tier):
    from integrations.personas.risk import review_risk_tier as persona_risk

    assert review_risk_tier([path]) == tier
    assert persona_risk([path]) == tier


def test_every_kernel_helper_requires_independent_review():
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    helpers = [path for path in scripts.rglob("*") if path.suffix in {".py", ".sh"}]
    assert helpers
    assert all(review_risk_tier([path.relative_to(scripts.parent).as_posix()]) >= 2
               for path in helpers)
