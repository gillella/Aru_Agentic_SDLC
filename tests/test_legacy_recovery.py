from __future__ import annotations

import types

import pytest

import legacy_recovery
from common import KernelError

HEAD = "a" * 40
MERGE_COMMIT = "b" * 40
OTHER_HEAD = "c" * 40
ACTOR = "aru-code-factory-gillella[bot]"
REVIEWER_CONFIG = "claude-code:m1@1,openai-codex:m2"


def pr_record(**overrides):
    record = {
        "number": 207,
        "state": "MERGED",
        "createdAt": "2026-01-01T00:00:00Z",
        "mergedAt": "2026-01-02T00:00:00Z",
        "headRefOid": HEAD,
        "mergeCommit": {"oid": MERGE_COMMIT},
        "author": {"login": ACTOR},
        "body": "Legacy recovery work.\n\nCloses #206\n",
        "labels": [],
    }
    record.update(overrides)
    return record


def issue_record(**overrides):
    record = {
        "number": 206,
        "state": "CLOSED",
        "labels": [{"name": "agent:m1"}, {"name": "status:in-progress"}],
        "body": "## Acceptance Criteria\n\n- [ ] independent review\n- [x] shipped\n",
    }
    record.update(overrides)
    return record


class Harness:
    """Broker-free stand-in for every GitHub read and write the helper makes."""

    def __init__(self, pr, issue, commit_message):
        self.pr = pr
        self.issue = issue
        self.commit_message = commit_message
        self.commands: list[list[str]] = []
        self.ensured: list[str] = []
        self.statuses: list[tuple[int, str]] = []
        self.pr_reads = 0
        self.status_error: KernelError | None = None
        self.drift_after: int | None = None

    def pull_request(self, number):
        self.pr_reads += 1
        if self.drift_after is not None and self.pr_reads > self.drift_after:
            return {**self.pr, "headRefOid": OTHER_HEAD}
        return dict(self.pr)

    def issue_view(self, number):
        return dict(self.issue)

    def verdict(self, pr):
        return {"pr": pr["number"], "head": pr["headRefOid"], "state": "success"}

    def gh_json(self, args):
        if args[0] == "api":
            return {
                "sha": args[1].rsplit("/", 1)[-1],
                "commit": {
                    "message": self.commit_message,
                    "author": {"name": "Aravind Gillella", "email": "aru@jugaadagents.com"},
                    "committer": {"name": "GitHub", "email": "noreply@github.com"},
                },
            }
        return {"number": self.pr["number"], "labels": list(self.pr["labels"])}

    def ensure_label(self, name, color="", description=""):
        self.ensured.append(name)

    def run(self, args):
        self.commands.append(list(args))
        if args[:3] == ["gh", "pr", "edit"]:
            added = args[args.index("--add-label") + 1]
            self.pr["labels"] = self.pr["labels"] + [{"name": n} for n in added.split(",")]

    def set_status(self, number, status, expected_current=None, pre_mutation_check=None):
        if pre_mutation_check is not None:
            pre_mutation_check()
        if self.status_error is not None:
            raise self.status_error
        self.statuses.append((number, status))
        self.issue["labels"] = [
            label for label in self.issue["labels"] if not label["name"].startswith("status:")
        ] + [{"name": "status:in-review"}]


@pytest.fixture
def harness(monkeypatch):
    def build(pr=None, issue=None, commit_message="claude-code m1 lineage"):
        state = Harness(pr or pr_record(), issue or issue_record(), commit_message)
        monkeypatch.setenv("ARU_CODING_REVIEWERS", REVIEWER_CONFIG)
        monkeypatch.setattr(legacy_recovery, "pull_request", state.pull_request)
        monkeypatch.setattr(legacy_recovery, "issue", state.issue_view)
        monkeypatch.setattr(legacy_recovery, "finalization_verdict", state.verdict)
        monkeypatch.setattr(legacy_recovery, "gh_json", state.gh_json)
        monkeypatch.setattr(legacy_recovery, "repo_slug", lambda: "gillella/Aru_Agentic_SDLC")
        monkeypatch.setattr(legacy_recovery, "ensure_label", state.ensure_label)
        monkeypatch.setattr(legacy_recovery, "run", state.run)
        monkeypatch.setattr(legacy_recovery, "set_status", state.set_status)
        return state

    return build


def recover(**overrides):
    arguments = {
        "issue_number": 206,
        "expected_head": HEAD,
        "agent": "m1",
        "author_family": "claude-code",
        "author_actor": ACTOR,
    }
    arguments.update(overrides)
    number = arguments.pop("number", 207)
    return legacy_recovery.recover_legacy_provenance(number, **arguments)


def test_preview_is_the_default_and_mutates_nothing(harness):
    state = harness()
    result = recover()
    assert result["mode"] == "preview" and result["action"] == "preview"
    assert result["planned"] == ["author-metadata", "status"]
    assert result["author"] == "m1" and result["author_family"] == "claude-code"
    assert result["author_actor"] == ACTOR and result["claimant"] == "m1"
    assert result["head"] == HEAD and result["merge_commit"] == MERGE_COMMIT
    assert (state.commands, state.ensured, state.statuses) == ([], [], [])


def test_apply_recovers_author_metadata_and_the_single_status_step(harness):
    state = harness()
    result = recover(apply=True)
    assert result["action"] == "recovered"
    assert result["applied"] == ["author-metadata", "status"]
    assert state.ensured == ["author:m1", "author-family:claude-code"]
    assert state.statuses == [(206, "In Review")]
    edits = [args for args in state.commands if args[:3] == ["gh", "pr", "edit"]]
    assert edits[0][-1] == "author:m1,author-family:claude-code"


def test_apply_posts_a_receipt_that_claims_no_review_authority(harness):
    state = harness()
    recover(apply=True)
    comments = [args for args in state.commands if args[:3] == ["gh", "pr", "comment"]]
    assert len(comments) == 1
    body = comments[0][-1]
    assert legacy_recovery.RECEIPT_MARKER in body
    assert "not a review, an approval" in body


def test_replay_after_recovery_is_idempotent(harness):
    recovered = pr_record(
        labels=[{"name": "author:m1"}, {"name": "author-family:claude-code"}]
    )
    settled = issue_record(
        labels=[{"name": "agent:m1"}, {"name": "status:in-review"}]
    )
    state = harness(pr=recovered, issue=settled)
    result = recover(apply=True)
    assert result["action"] == "already-recovered"
    assert result["planned"] == [] and result["status_transition"] is None
    assert (state.commands, state.ensured, state.statuses) == ([], [], [])


def test_replay_over_conflicting_author_metadata_fails_closed(harness):
    conflicting = pr_record(
        labels=[{"name": "author:m2"}, {"name": "author-family:openai-codex"}]
    )
    harness(pr=conflicting)
    with pytest.raises(KernelError, match="conflicts with the recovered provenance"):
        recover(apply=True)


@pytest.mark.parametrize("head", ["", "abc123", HEAD.upper(), "z" * 40])
def test_recovery_requires_an_exact_full_head(harness, head):
    harness()
    with pytest.raises(KernelError, match="exact full 40-character historical head"):
        recover(expected_head=head)


def test_head_that_does_not_match_the_merged_pr_is_refused(harness):
    harness()
    with pytest.raises(KernelError, match="expected head does not match"):
        recover(expected_head=OTHER_HEAD)


@pytest.mark.parametrize(
    "overrides",
    [{"state": "OPEN", "mergedAt": None}, {"state": "CLOSED", "mergedAt": None}],
)
def test_open_or_unmerged_pull_requests_are_refused(harness, overrides):
    harness(pr=pr_record(**overrides))
    with pytest.raises(KernelError, match="only applies to a confirmed merged PR"):
        recover()


def test_unreadable_merge_commit_is_refused(harness):
    harness(pr=pr_record(mergeCommit={"oid": "not-a-sha"}))
    with pytest.raises(KernelError, match="missing a readable merge commit"):
        recover()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("no directive at all", "exactly one closing issue directive"),
        ("Closes #206\nCloses #210\n", "exactly one closing issue directive"),
        ("Closes #999\n", "does not close the expected issue"),
    ],
)
def test_linked_issue_directive_must_be_exact(harness, body, message):
    harness(pr=pr_record(body=body))
    with pytest.raises(KernelError, match=message):
        recover()


def test_open_linked_issue_is_refused(harness):
    harness(issue=issue_record(state="OPEN"))
    with pytest.raises(KernelError, match="only applies to a closed linked issue"):
        recover()


def test_missing_historical_ci_proof_is_refused(harness, monkeypatch):
    state = harness()
    monkeypatch.setattr(
        legacy_recovery,
        "finalization_verdict",
        lambda pr: {"head": pr["headRefOid"], "state": "failure"},
    )
    with pytest.raises(KernelError, match="historical governed CI is not proven"):
        recover()
    assert state.commands == []


def test_ambiguous_claim_is_refused(harness):
    ambiguous = issue_record(
        labels=[{"name": "agent:m1"}, {"name": "agent:m2"}, {"name": "status:in-progress"}]
    )
    harness(issue=ambiguous)
    with pytest.raises(KernelError, match="one exclusive claimant"):
        recover()


def test_declared_author_must_equal_the_recorded_claimant(harness):
    harness()
    with pytest.raises(KernelError, match="does not match the linked issue's recorded claimant"):
        recover(agent="m2", author_family="openai-codex")


def test_a_git_name_alone_is_not_model_lineage_proof(harness, monkeypatch):
    """Without configured lineage, a human Git author resolves to no coding family."""
    harness(commit_message="Aravind Gillella authored this")
    monkeypatch.delenv("ARU_CODING_REVIEWERS", raising=False)
    with pytest.raises(KernelError, match="unknown or not canonical"):
        recover()


def test_head_evidence_contradicting_the_declared_family_is_refused(harness):
    harness(commit_message="openai-codex produced this change")
    with pytest.raises(KernelError, match="contradicts the declared family"):
        recover()


def test_head_evidence_naming_two_families_is_refused(harness):
    harness(commit_message="claude-code and openai-codex both touched this")
    with pytest.raises(KernelError, match="contradicts the declared family"):
        recover()


def test_absent_head_evidence_is_refused(harness):
    harness(commit_message="routine maintenance commit")
    with pytest.raises(KernelError, match="evidence is missing"):
        recover()


def test_unreadable_head_commit_evidence_is_refused(harness, monkeypatch):
    harness()
    monkeypatch.setattr(legacy_recovery, "gh_json", lambda args: {"sha": OTHER_HEAD})
    with pytest.raises(KernelError, match="head commit evidence is unreadable"):
        recover()


def test_incomplete_head_commit_authorship_is_refused(harness, monkeypatch):
    harness()
    monkeypatch.setattr(
        legacy_recovery,
        "gh_json",
        lambda args: {
            "sha": HEAD,
            "commit": {"message": "claude-code", "author": {"name": ""}, "committer": {}},
        },
    )
    with pytest.raises(KernelError, match="head commit authorship is incomplete"):
        recover()


def test_declared_actor_must_match_the_merged_pr_author(harness):
    harness()
    with pytest.raises(KernelError, match="declared GitHub actor does not match"):
        recover(author_actor="someone-else")


def test_missing_pr_actor_is_refused(harness):
    harness(pr=pr_record(author={}))
    with pytest.raises(KernelError, match="no readable GitHub author actor"):
        recover()


def test_contradictory_existing_author_labels_are_refused(harness):
    harness(pr=pr_record(labels=[{"name": "author:m1"}, {"name": "author:m2"}]))
    with pytest.raises(KernelError, match="contradictory author metadata"):
        recover()


@pytest.mark.parametrize("status", ["status:done", "status:backlog", "status:ready"])
def test_only_in_progress_advances_to_in_review(harness, status):
    harness(issue=issue_record(labels=[{"name": "agent:m1"}, {"name": status}]))
    with pytest.raises(KernelError, match="cannot move a .* issue to In Review"):
        recover()


def test_observed_drift_before_a_write_refuses_further_mutation(harness):
    state = harness()
    state.drift_after = 1
    with pytest.raises(KernelError, match="precondition drift; no further write"):
        recover(apply=True)
    assert state.commands == [] and state.statuses == []


def test_partial_write_is_reported_with_what_already_landed(harness):
    state = harness()
    state.status_error = KernelError("project readback disagreed")
    with pytest.raises(legacy_recovery.LegacyRecoveryError) as caught:
        recover(apply=True)
    receipt = caught.value.receipt
    assert receipt["action"] == "partial"
    assert receipt["applied"] == ["author-metadata"] and receipt["skipped"] == []
    assert state.statuses == []
    assert not [args for args in state.commands if args[:3] == ["gh", "pr", "comment"]]


def test_author_metadata_that_does_not_settle_is_refused(harness, monkeypatch):
    state = harness()
    real = state.gh_json
    monkeypatch.setattr(
        legacy_recovery,
        "gh_json",
        lambda args: real(args) if args[0] == "api" else {"number": 207, "labels": []},
    )
    with pytest.raises(legacy_recovery.LegacyRecoveryError, match="did not settle"):
        recover(apply=True)


def test_original_acceptance_is_reported_but_never_ticked(harness):
    state = harness()
    result = recover(apply=True)
    assert result["acceptance"] == {"total": 2, "incomplete": 1}
    assert state.issue["body"] == issue_record()["body"]
    assert not [args for args in state.commands if args[:3] == ["gh", "issue", "edit"]]


def test_recovery_never_removes_the_claim_or_sets_done(harness):
    state = harness()
    recover(apply=True)
    assert {"name": "agent:m1"} in state.issue["labels"]
    assert all(status != "Done" for _number, status in state.statuses)
    assert not any("--remove-label" in args for args in state.commands)


def test_recovery_never_assigns_a_reviewer_or_an_approval(harness):
    state = harness()
    recover(apply=True)
    joined = " ".join(part for args in state.commands for part in args)
    assert "reviewer" not in joined and "review:" not in joined
    assert not any(args[:3] == ["gh", "pr", "review"] for args in state.commands)


def test_operator_arguments_require_the_linked_issue(harness):
    harness()
    args = types.SimpleNamespace(
        recover_legacy=207,
        issue=None,
        expected_head=HEAD,
        agent="m1",
        author_family="claude-code",
        author_github_login=ACTOR,
        apply=False,
    )
    with pytest.raises(KernelError, match="requires the linked --issue"):
        legacy_recovery.recover_from_args(args)


def test_operator_arguments_default_to_preview(harness):
    state = harness()
    args = types.SimpleNamespace(
        recover_legacy=207,
        issue=206,
        expected_head=HEAD,
        agent="m1",
        author_family="claude-code",
        author_github_login=ACTOR,
        apply=False,
    )
    assert legacy_recovery.recover_from_args(args)["action"] == "preview"
    assert (state.commands, state.statuses) == ([], [])
