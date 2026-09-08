from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import importlib.util
import importlib
import re
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver.kernel import (  # noqa: E402
    KernelAdapter, KernelAdapterError, SCHEMA, _Bridge, _overlap,
    runner_profile_for_account,
)


# A personal gillella repository, which the account policy keeps on the Macs.
REPO = "gillella/project"
HOSTED_REPO = "Unum-Inc/project"
ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("_driver_test_touches", ROOT / "scripts/touches.py")
touches_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(touches_module)


class FakeError(RuntimeError):
    pass


def issue(number, status="Ready", touches="src/a.py", *, agents=(), dependency=None):
    body = f"## Acceptance Criteria\n- [ ] Ships\n\ntouches: {touches}"
    if dependency:
        body += f"\ndepends-on: #{dependency}"
    return {
        "number": number, "title": f"Issue {number}", "body": body,
        "state": "open", "labels": ["status:" + status.lower().replace(" ", "-")]
        + ["agent:" + agent for agent in agents],
    }


def pr(number, linked=1, path="src/a.py", author="codex-a"):
    return {
        "number": number, "state": "open", "head": {"sha": "a" * 40},
        "body": f"Closes #{linked}", "labels": ["author:" + author],
        "user": {"login": "author-user"}, "files": [{"filename": path}],
    }


class Backend:
    def __init__(self, records=(), prs=()):
        self.records = {record["number"]: deepcopy(record) for record in records}
        self.prs = {record["number"]: deepcopy(record) for record in prs}
        self.calls = []
        self.board_override = None
        self.ci_error = None
        self.workflow_error = None
        self.runner_status = "online"
        self.workflows = [{"path": ".github/workflows/governed-pr.yml", "state": "active"}]

    def request(self, args):
        endpoint = args[1]
        self.calls.append(endpoint)
        parsed = urlsplit(endpoint)
        query = parse_qs(parsed.query)
        page = int(query.get("page", [1])[0])
        size = int(query.get("per_page", [100])[0])
        path = parsed.path
        if path == f"repos/{REPO}/issues":
            state = query["state"][0]
            records = [record for record in self.records.values() if record["state"] == state]
            if "labels" in query:
                records = [record for record in records if query["labels"][0] in record["labels"]]
        elif path == f"repos/{REPO}/pulls":
            records = list(self.prs.values())
        elif path.endswith("/files"):
            records = self.prs[int(path.split("/")[-2])]["files"]
        elif re.fullmatch(rf"repos/{REPO}/pulls/\d+", path):
            return deepcopy(self.prs[int(path.rsplit("/", 1)[1])])
        elif path.endswith("/actions/runners"):
            if self.ci_error:
                raise FakeError(self.ci_error)
            return {"total_count": 1, "runners": [{
                "status": self.runner_status, "busy": False,
                "labels": [{"name": name} for name in ("self-hosted", "macOS", "ARM64", "aru-ci")],
            }]}
        elif path.endswith("/actions/workflows"):
            if self.workflow_error:
                raise FakeError(self.workflow_error)
            return {"total_count": len(self.workflows), "workflows": deepcopy(self.workflows)}
        elif path.endswith("/actions/runs"):
            return {"total_count": 0, "workflow_runs": []}
        else:
            raise AssertionError(endpoint)
        return deepcopy(records[(page - 1) * size:page * size])


def make_bridge(backend, tmp_path):  # noqa: C901 -- isolated fake kernel behaviors
    bridge = _Bridge.__new__(_Bridge)
    bridge.repo = REPO
    bridge.repo_dir = tmp_path

    def parse_touches(body):
        try:
            return touches_module.parse_touches(body)
        except touches_module.TouchesError as exc:
            raise FakeError(str(exc)) from exc

    def labels(record):
        values = record["labels"]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise FakeError("malformed labels")
        return values

    def status(record):
        found = [name[7:] for name in labels(record) if name.startswith("status:")]
        if len(found) > 1:
            raise FakeError("contradictory statuses")
        return found[0].replace("-", " ").title() if found else None

    def dependencies(body):
        return [int(number) for number in re.findall(r"(?m)^depends-on: #(\d+)$", body)]

    def evaluate(record, states):
        errors = []
        if record["state"] != "OPEN":
            errors.append("issue is not open")
        if "- [ ]" not in record["body"]:
            errors.append("Acceptance Criteria must contain an unchecked item")
        try:
            parse_touches(record["body"])
        except FakeError as exc:
            errors.append(str(exc))
        for number in dependencies(record["body"]):
            if states.get(number) != "closed":
                errors.append(f"open dependencies: #{number}")
        if "needs-human" in record["labels"]:
            errors.append("needs-human issues cannot enter Ready")
        return errors

    bridge.common = SimpleNamespace(
        KernelError=FakeError, QUOTA_STOP_MESSAGE="quota-stop",
        gh_json=backend.request, repo_root=lambda **kwargs: tmp_path,
        checkout_repository=lambda **kwargs: REPO, repo_slug=lambda: REPO,
        git=lambda args, **kwargs: "", issue=lambda number: deepcopy(backend.records[number]),
        label_names=labels, status_of=status, parse_touches=parse_touches,
        safe_declared_path=touches_module.safe_declared_path, dependencies=dependencies,
        acceptance_items=lambda body: re.findall(r"- \[([ x])\] (.+)", body),
        project_item_status=lambda number: backend.board_override or status(backend.records[number]),
    )
    bridge.triage = SimpleNamespace(evaluate_with_states=evaluate, _priority=lambda record: 2)
    bridge.claims = SimpleNamespace(safe_agent=lambda value: value)
    bridge.merge_state = SimpleNamespace(
        linked_issues=lambda body: [int(number) for number in re.findall(r"Closes #(\d+)", body)],
    )
    return bridge


def adapter(tmp_path):
    return KernelAdapter(ROOT, tmp_path, REPO)


def test_complete_snapshot_includes_closed_active_and_review_reservations(tmp_path):
    records = [issue(1, "In Progress", agents=["codex-a"]), issue(2), issue(3, "In Review", "lib/**")]
    records[2]["state"] = "closed"
    backend = Backend(records, [pr(9)])
    snapshot = make_bridge(backend, tmp_path).snapshot()
    assert snapshot["complete"] is True
    assert snapshot["schema"] == SCHEMA
    assert [record["number"] for record in snapshot["issues"]] == [1, 2, 3]
    assert snapshot["prs"][0]["author_actor"] == "author-user"
    assert snapshot["prs"][0]["labels"] == ["author:codex-a"]
    assert snapshot["ci_available"] is True
    assert snapshot["ci"]["free_runners"] == 1


@pytest.fixture
def review_bridge(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    common = importlib.import_module("common")
    merge = importlib.import_module("merge_pr")
    bridge = make_bridge(Backend(), tmp_path)
    opened = {"number": 9, "headRefOid": "a" * 40, "state": "OPEN", "isDraft": False,
              "body": "Closes #1", "author": {"login": "author-user"},
              "labels": ["author:writer", "author-family:openai-codex", "review:claude-code",
                         "reviewer:reviewer-one", "reviewer-actor:review-bot"]}
    bridge.merge_state.pull_request = lambda number: deepcopy(opened)
    bridge.revalidate = lambda number, agent: {"agents": [agent]} if number == 1 else pytest.fail("wrong issue")
    for name in ("AUTHOR_PREFIX", "CODING_REVIEWERS", "normalized_identity", "same_github_actor", "configured_reviewer_family"):
        setattr(bridge.common, name, getattr(common, name))
    bridge.common.registered_coding_actors = lambda: {"reviewer-one": "review-bot"}
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:reviewer-one@1")
    monkeypatch.setattr(merge, "pull_reviews", lambda number: [])
    return bridge, opened


def test_review_binding_uses_kernel_verdict_and_rejects_stale_expected_head(review_bridge, monkeypatch):
    bridge, opened = review_bridge
    binding = bridge.review_binding(9)
    assert binding["repo"] == REPO and binding["reviewer"] == "reviewer-one"
    assert binding["author"] == "writer" and binding["verdict"] is None
    opened["headRefOid"] = "b" * 40
    with pytest.raises(KernelAdapterError, match="changed"):
        bridge.review_binding(9, binding)
    merge = importlib.import_module("merge_pr")
    monkeypatch.setattr(merge, "coding_review_verdict", lambda *args: "REQUEST_CHANGES")
    assert bridge.review_binding(9)["verdict"] == "REQUEST_CHANGES"


@pytest.mark.parametrize("change", ["actor", "identity", "registration", "family", "multiple", "draft", "needs-human", "late-head"])
def test_review_binding_fails_closed_on_independence_or_authority_changes(review_bridge, monkeypatch, change):
    bridge, opened = review_bridge
    if change == "actor":
        opened["author"]["login"] = "review-bot"
    elif change == "identity":
        opened["labels"][0] = "author:reviewer-one"
    elif change == "registration":
        bridge.common.registered_coding_actors = lambda: {}
    elif change == "family":
        monkeypatch.setenv("ARU_CODING_REVIEWERS", "openai-codex:reviewer-one")
    elif change == "multiple":
        opened["labels"].append("review:coderabbit")
    elif change == "draft":
        opened["isDraft"] = True
    elif change == "needs-human":
        opened["labels"].append("needs-human")
    else:
        merge = importlib.import_module("merge_pr")
        def move_head(number):
            opened["headRefOid"] = "b" * 40
            return []
        monkeypatch.setattr(merge, "pull_reviews", move_head)
    with pytest.raises(RuntimeError):
        bridge.review_binding(9)


def test_review_worktree_is_detached_and_revalidates_after_fetch(review_bridge, tmp_path):
    bridge, _ = review_bridge
    binding = bridge.review_binding(9)
    calls = []
    directory = tmp_path / ".worktrees" / f"review-pr-9-{binding['head']}-reviewer-one"
    bridge.common.primary_worktree = lambda: tmp_path
    bridge.common.repo_root = lambda **kwargs: directory
    bridge.review_binding = lambda *args: binding
    def git(args, **kwargs):
        calls.append(args)
        if args[0] == "rev-parse":
            return binding["head"]
        return ""
    bridge.common.git = git
    assert bridge.review_worktree(binding) == str(directory)
    assert ["worktree", "add", "--detach", str(directory), binding["head"]] in calls
    bridge.common.git = lambda args, **kwargs: "b" * 40 if args[0] == "rev-parse" else ""
    with pytest.raises(KernelAdapterError, match="advanced"):
        bridge.review_worktree(binding)


def test_reviewer_recovery_calls_only_canonical_helper_after_binding_check(review_bridge, monkeypatch):
    bridge, opened = review_bridge
    binding = bridge.review_binding(9)
    create = importlib.import_module("create_pr")
    calls = []
    fallback = {"pr": 9, "authority": "openai-codex", "action": "fallback", "reason": "worker lost", "reviewer": "second"}
    monkeypatch.setattr(create, "recover_coding_authority", lambda *args: calls.append(args) or fallback)
    bridge.review = SimpleNamespace(reviewer_continuation=lambda number: {
        "authority": "openai-codex", "next_action": "await-authoritative-review", "retry_at": None})
    refreshed = bridge.refresh_reviewer(9, binding, "worker lost")
    assert refreshed["next_action"] == "await-authoritative-review" and refreshed["reviewer"] == "second"
    assert calls[0][:4] == (9, opened, "claude-code", "worker lost")
    opened["headRefOid"] = "b" * 40
    with pytest.raises(KernelAdapterError, match="changed"):
        bridge.refresh_reviewer(9, binding, "worker lost")
    assert len(calls) == 1


def test_recovery_rejects_observed_same_family_reassignment(review_bridge, monkeypatch):
    bridge, opened = review_bridge
    binding = bridge.review_binding(9)
    create = importlib.import_module("create_pr")
    monkeypatch.setattr(create, "recover_coding_authority", lambda *a: pytest.fail("stale recovery must not mutate"))
    def drift(number, expected):
        opened["labels"][-2:] = ["reviewer:second", "reviewer-actor:other-bot"]
        return binding
    bridge.review_binding = drift
    with pytest.raises(KernelAdapterError, match="assignment changed"):
        bridge.refresh_reviewer(9, binding, "worker lost")


def test_conflicting_first_candidate_does_not_starve_independent_issue(tmp_path):
    backend = Backend([
        issue(1, "In Progress", "src/**", agents=["peer"]),
        issue(2, touches="src/a.py"), issue(3, touches="docs/a.md"),
    ])
    snapshot = make_bridge(backend, tmp_path).snapshot()
    assert [record["number"] for record in adapter(tmp_path).candidates(snapshot)] == [3]
    assert "active issue #1" in adapter(tmp_path).blocked(snapshot)["2"][0]


@pytest.mark.parametrize("left,right,expected", [
    (["src/**"], ["src/a.py"], True),
    (["src/a/**"], ["src/**"], True),
    (["src/a.py"], ["src/ab.py"], False),
    (["src/a"], ["src/a/b.py"], False),
    (["./src/**"], ["src/a.py"], True),
])
def test_canonical_boundary_intersections(left, right, expected):
    assert _overlap(left, right) is expected


def test_unknown_active_boundary_blocks_dispatch(tmp_path):
    broken = issue(1, "In Progress", agents=["peer"])
    broken["body"] = "no write boundary"
    snapshot = make_bridge(Backend([broken, issue(2)]), tmp_path).snapshot()
    assert adapter(tmp_path).candidates(snapshot) == []
    assert "unknown write boundary" in adapter(tmp_path).blocked(snapshot)["2"][0]


def test_open_pr_rename_reserves_both_paths(tmp_path):
    opened = pr(9, path="new.py")
    opened["files"][0]["previous_filename"] = "old.py"
    records = [issue(1, "In Review", "new.py", agents=["codex-a"]), issue(2, touches="old.py")]
    snapshot = make_bridge(Backend(records, [opened]), tmp_path).snapshot()
    assert "old.py" in snapshot["prs"][0]["touches"]
    assert adapter(tmp_path).candidates(snapshot) == []


def test_dependencies_and_human_gate_are_not_promotable(tmp_path):
    records = [issue(1), issue(2, "Backlog", dependency=1), issue(3, "Backlog")]
    records[2]["labels"].append("needs-human")
    snapshot = make_bridge(Backend(records), tmp_path).snapshot()
    assert snapshot["issues"][1]["dependencies"] == {"1": "OPEN"}
    assert adapter(tmp_path).candidates(snapshot, "Backlog") == []


def test_truncated_or_malformed_inventory_is_not_idle(tmp_path, monkeypatch):
    bridge = make_bridge(Backend(), tmp_path)
    monkeypatch.setattr("aru_project_driver.kernel.MAX_PAGES", 2)
    monkeypatch.setattr("aru_project_driver.kernel.PAGE_SIZE", 1)
    bridge.common.gh_json = lambda args: [{"number": 1}]
    with pytest.raises(KernelAdapterError, match="bounded complete"):
        bridge.pages("issues")
    bridge.common.gh_json = lambda args: {"unexpected": []}
    with pytest.raises(KernelAdapterError, match="malformed inventory"):
        bridge.pages("issues")


def test_counted_inventory_must_be_complete(tmp_path):
    bridge = make_bridge(Backend(), tmp_path)
    bridge.common.gh_json = lambda args: {"total_count": 2, "runners": [{}]}
    with pytest.raises(KernelAdapterError, match="incomplete counted"):
        bridge.pages("runners", key="runners")


def test_duplicate_or_malformed_issue_is_rejected(tmp_path):
    bridge = make_bridge(Backend([issue(1)]), tmp_path)
    original = bridge.pages
    bridge.pages = lambda endpoint, **kwargs: [issue(1), issue(1)] if "state=open" in endpoint else original(endpoint, **kwargs)
    with pytest.raises(KernelAdapterError, match="duplicate identities"):
        bridge.snapshot()
    bridge = make_bridge(Backend([issue(1)]), tmp_path)
    bridge.common.gh_json = lambda args: [{"number": 1, "state": "open"}] if "state=open" in args[1] and "/issues?" in args[1] else []
    with pytest.raises(KernelAdapterError, match="malformed issue"):
        bridge.snapshot()


def test_pr_head_race_aborts_snapshot(tmp_path):
    backend = Backend([issue(1)], [pr(9)])
    bridge = make_bridge(backend, tmp_path)
    original = backend.request

    def request(args):
        result = original(args)
        if args[1] == f"repos/{REPO}/pulls/9":
            result["head"]["sha"] = "b" * 40
        return result

    bridge.common.gh_json = request
    with pytest.raises(KernelAdapterError, match="authority changed"):
        bridge.snapshot()


def test_ci_permission_unknown_and_quota_stops_reads(tmp_path):
    backend = Backend([issue(1)])
    backend.ci_error = "permission denied"
    bridge = make_bridge(backend, tmp_path)
    snapshot = bridge.snapshot()
    assert snapshot["ci_available"] is None
    assert "permission denied" in snapshot["ci"]["reason"]
    backend.calls.clear()
    backend.ci_error = "quota-stop"
    with pytest.raises(FakeError, match="quota-stop"):
        bridge.snapshot()
    assert not any("/actions/runs?" in call for call in backend.calls)


def test_online_runner_with_unknown_queue_cannot_admit_work(tmp_path):
    backend = Backend([issue(1)])
    bridge = make_bridge(backend, tmp_path)
    original = backend.request

    def request(args):
        if "/actions/runs?" in args[1]:
            raise FakeError("queue inventory unavailable")
        return original(args)

    bridge.common.gh_json = request
    snapshot = bridge.snapshot()
    assert snapshot["ci"]["online_runners"] == 1
    assert snapshot["ci"]["queued"] is None
    assert snapshot["ci"]["available"] is None
    assert snapshot["ci_available"] is None


def hosted_ci(backend, tmp_path):
    """Evaluate CI evidence for a Unum-Inc repository on the hosted profile."""
    bridge = make_bridge(backend, tmp_path)
    bridge.repo = HOSTED_REPO
    return bridge._ci()


def test_account_policy_selects_one_profile_without_fallback():
    assert runner_profile_for_account("gillella/Aru_Agentic_SDLC") == "self-hosted-mac"
    assert runner_profile_for_account("GILLELLA/other") == "self-hosted-mac"
    assert runner_profile_for_account("Unum-Inc/unumnow") == "github-hosted"
    assert runner_profile_for_account("unum-inc/other") == "github-hosted"
    assert runner_profile_for_account("someone-else/project") is None


def test_hosted_account_admits_on_bounded_workflow_and_queue_evidence(tmp_path):
    ci = hosted_ci(Backend([issue(1)]), tmp_path)
    assert ci["runner_profile"] == "github-hosted"
    assert ci["available"] is True
    assert ci["queued"] == 0
    assert ci["reason"] is None
    # Hosted capacity is never fabricated from a self-hosted inventory.
    assert ci["online_runners"] is None and ci["free_runners"] is None


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda backend: setattr(backend, "workflow_error", "permission denied"), "permission denied"),
        (lambda backend: backend.workflows.clear(), "no unique governed workflow"),
        (lambda backend: backend.workflows.__setitem__(0, {"path": ".github/workflows/governed-pr.yml", "state": "disabled_manually"}), "disabled_manually"),
        (lambda backend: backend.workflows.__setitem__(0, {"path": ".github/workflows/governed-pr.yml", "state": 7}), "malformed workflow inventory"),
    ],
)
def test_unknown_hosted_workflow_evidence_blocks_admission(tmp_path, mutate, expected):
    backend = Backend([issue(1)])
    mutate(backend)
    ci = hosted_ci(backend, tmp_path)
    assert ci["available"] is None
    assert expected in ci["reason"]


def test_hosted_account_never_reads_self_hosted_runner_inventory(tmp_path):
    backend = Backend([issue(1)])
    backend.ci_error = "self-hosted inventory must not be consulted"
    assert hosted_ci(backend, tmp_path)["available"] is True
    assert not any(call.endswith("/actions/runners") for call in backend.calls)


def test_offline_personal_pool_blocks_with_no_hosted_substitution(tmp_path):
    backend = Backend([issue(1)])
    backend.runner_status = "offline"
    snapshot = make_bridge(backend, tmp_path).snapshot()
    assert snapshot["ci"]["runner_profile"] == "self-hosted-mac"
    assert snapshot["ci_available"] is False
    assert snapshot["ci"]["online_runners"] == 0
    assert not any(call.endswith("/actions/workflows") for call in backend.calls)


def test_unassigned_account_is_blocked_before_any_capacity_read(tmp_path):
    backend = Backend([issue(1)])
    bridge = make_bridge(backend, tmp_path)
    bridge.repo = "someone-else/project"
    ci = bridge._ci()
    assert ci["runner_profile"] is None
    assert ci["available"] is None and ci["queued"] is None
    assert "no runner profile is assigned" in ci["reason"]
    assert backend.calls == []


@pytest.mark.parametrize("status", ["Ready", "Backlog"])
def test_needs_design_cannot_be_promoted_claimed_or_dispatched(tmp_path, status):
    record = issue(1, status)
    record["labels"].append("needs-design")
    backend = Backend([record])
    bridge = make_bridge(backend, tmp_path)
    snapshot = bridge.snapshot()
    assert adapter(tmp_path).candidates(snapshot, status) == []
    with pytest.raises(KernelAdapterError, match="needs-design"):
        bridge.revalidate(1)


def test_target_board_mismatch_prevents_claim(tmp_path):
    backend = Backend([issue(1)])
    backend.board_override = "Backlog"
    bridge = make_bridge(backend, tmp_path)
    calls = []
    bridge.claims.claim = lambda *args: calls.append(args)
    with pytest.raises(KernelAdapterError, match="Project card disagree"):
        bridge.dispatch("claim", {"number": 1, "agent": "codex-a"})
    assert calls == []


def test_fresh_claim_guard_and_post_claim_conflict_preserve_claim(tmp_path):
    backend = Backend([issue(1)])
    bridge = make_bridge(backend, tmp_path)

    def claim(number, agent):
        backend.records[number]["labels"] = ["status:in-progress", "agent:" + agent]
        backend.records[2] = issue(2, "In Progress", agents=["peer"])
        return {"issue": number, "agent": agent, "status": "In Progress"}

    bridge.claims.claim = claim
    with pytest.raises(KernelAdapterError, match="overlaps active issue #2"):
        bridge.dispatch("claim", {"number": 1, "agent": "codex-a"})
    assert "agent:codex-a" in backend.records[1]["labels"]


def test_precise_promotion_checks_conflicts_at_mutation_boundary(tmp_path):
    backend = Backend([issue(1, "Backlog")])
    bridge = make_bridge(backend, tmp_path)
    promoted = []

    def promote(number, *, pre_mutation_check):
        backend.records[2] = issue(2, "In Progress", agents=["peer"])
        pre_mutation_check()
        promoted.append(number)

    bridge.triage.promote_issue = promote
    with pytest.raises(KernelAdapterError, match="overlaps active issue #2"):
        bridge.dispatch("promote", {"number": 1})
    assert promoted == []
    assert backend.records[1]["labels"] == ["status:backlog"]


def test_partial_snapshot_is_rejected(tmp_path):
    with pytest.raises(KernelAdapterError, match="complete matching"):
        adapter(tmp_path).candidates({"schema": SCHEMA, "repo": REPO, "complete": False, "issues": [], "prs": []})


def test_identity_and_clean_checkout_are_required(tmp_path):
    bridge = make_bridge(Backend([issue(1)]), tmp_path)
    bridge.common.repo_slug = lambda: "different/project"
    with pytest.raises(KernelAdapterError, match="live GitHub repository"):
        bridge.snapshot()
    bridge.common.repo_slug = lambda: REPO
    bridge.common.git = lambda *args: " M user-file.py"
    with pytest.raises(KernelAdapterError, match="tracked changes"):
        bridge.revalidate(1)


@pytest.mark.parametrize("number", [0, -1, True, 1.5, None, "1", [], {}])
def test_release_rejects_invalid_issue_before_identity_or_github_reads(tmp_path, number):
    bridge = make_bridge(Backend(), tmp_path)
    bridge.identity = lambda **kwargs: pytest.fail("invalid release must not read GitHub")
    bridge.claims.release = lambda *args: pytest.fail("invalid release must not mutate")
    with pytest.raises(KernelAdapterError, match="positive integer"):
        bridge.dispatch("release", {"number": number, "agent": "codex-a"})


def test_subprocess_bridge_imports_only_in_child_and_preserves_cwd(tmp_path):
    scripts = tmp_path / "kernel/scripts"
    scripts.mkdir(parents=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (scripts / "common.py").write_text(
        "from pathlib import Path\n"
        "def repo_root(): return Path.cwd()\n"
        f"def checkout_repository(): return {REPO!r}\n"
        f"def repo_slug(): return {REPO!r}\n"
    )
    (scripts / "claim_issue.py").write_text("def safe_agent(agent): return agent\n")
    (scripts / "fetch_next_work.py").write_text(
        "import os\ndef select(agent): return {'type':'idle','agent':agent,'cwd':os.getcwd()}\n"
    )
    for name in ("triage_backlog", "create_branch", "review_policy", "merge_state"):
        (scripts / f"{name}.py").write_text("")
    previous = Path.cwd()
    result = KernelAdapter(scripts.parent, repo_dir, REPO).next_work("codex-a")
    assert result == {"type": "idle", "agent": "codex-a", "cwd": str(repo_dir)}
    assert Path.cwd() == previous


def test_existing_verified_worktree_is_reused_and_ambiguity_rejected(tmp_path):
    backend = Backend([issue(1, "In Progress", agents=["codex-a"])])
    bridge = make_bridge(backend, tmp_path)
    worktree = tmp_path / ".worktrees/feat-issue-1-example"
    worktree.mkdir(parents=True)
    branch = "refs/heads/feat/issue-1-example"
    listing = f"worktree {tmp_path}\nbranch refs/heads/main\n\nworktree {worktree}\nbranch {branch}\n"
    bridge.common.git = lambda args, **kwargs: listing if args[:2] == ["worktree", "list"] else branch if args[0] == "symbolic-ref" else ""
    bridge.common.repo_root = lambda **kwargs: kwargs.get("cwd", tmp_path)
    bridge.common.primary_worktree = lambda: tmp_path
    assert bridge.dispatch("branch", {"number": 1, "agent": "codex-a"}) == str(worktree)
    listing += f"\nworktree {worktree}-other\nbranch refs/heads/fix/issue-1-another\n"
    with pytest.raises(KernelAdapterError, match="ambiguous existing"):
        bridge.dispatch("branch", {"number": 1, "agent": "codex-a"})


def test_completed_criteria_can_resume_existing_claim_but_missing_criteria_cannot(tmp_path):
    record = issue(1, "In Review", agents=["codex-a"])
    record["body"] = record["body"].replace("- [ ]", "- [x]")
    backend = Backend([record])
    bridge = make_bridge(backend, tmp_path)
    assert bridge.revalidate(1, "codex-a")["status"] == "In Review"
    backend.records[1]["body"] = "touches: src/a.py"
    with pytest.raises(KernelAdapterError, match="Acceptance Criteria"):
        bridge.revalidate(1, "codex-a")


def test_resume_checks_actual_own_pr_paths_against_other_writers(tmp_path):
    records = [
        issue(1, "In Review", "declared.py", agents=["codex-a"]),
        issue(2, "In Progress", "outside.py", agents=["peer"]),
    ]
    opened = pr(9, linked=1, path="outside.py")
    bridge = make_bridge(Backend(records, [opened]), tmp_path)
    with pytest.raises(KernelAdapterError, match="overlaps active issue #2"):
        bridge.revalidate(1, "codex-a")
