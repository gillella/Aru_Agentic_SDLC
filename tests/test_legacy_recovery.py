from __future__ import annotations

import sys

import pytest

import common
import create_pr
import legacy_recovery
from common import KernelError

HEAD = "a" * 40
MERGE_COMMIT = "b" * 40
OTHER_HEAD = "c" * 40
ACTOR = "aru-code-factory-gillella[bot]"
REVIEWER_CONFIG = "claude-code:m1@1,openai-codex:m2"
ATTESTED = "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
CODEX_ATTESTED = "Co-Authored-By: Codex <noreply@openai.com>"
SIGNED_MESSAGE = f"Legacy work.\n\n{ATTESTED}"


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
        self.verified = True
        self.signer = ACTOR
        self.board = "In Progress"
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
                "author": {"login": self.signer},
                "commit": {
                    "message": self.commit_message,
                    "verification": {"verified": self.verified},
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

    def project_item_status(self, number):
        return self.board

    def set_status(self, number, status, expected_current=None, pre_mutation_check=None):
        if pre_mutation_check is not None:
            pre_mutation_check()
        if self.status_error is not None:
            raise self.status_error
        self.statuses.append((number, status))
        self.board = status
        self.issue["labels"] = [
            label for label in self.issue["labels"] if not label["name"].startswith("status:")
        ] + [{"name": "status:in-review"}]


@pytest.fixture
def harness(monkeypatch):
    def build(pr=None, issue=None, commit_message=SIGNED_MESSAGE):
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
        monkeypatch.setattr(legacy_recovery, "project_item_status", state.project_item_status)
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


def test_apply_preserves_every_governed_invariant(harness):
    """One apply: receipt posted, acceptance intact, claim and review authority untouched."""
    state = harness()
    result = recover(apply=True)
    assert result["acceptance"] == {"total": 2, "incomplete": 1}
    assert state.issue["body"] == issue_record()["body"]
    body = [a for a in state.commands if a[:3] == ["gh", "pr", "comment"]][0][-1]
    assert legacy_recovery.RECEIPT_MARKER in body and "not a review, an approval" in body
    assert {"name": "agent:m1"} in state.issue["labels"]
    assert all(status != "Done" for _number, status in state.statuses)
    joined = " ".join(part for a in state.commands for part in a)
    assert "reviewer" not in joined and "review:" not in joined
    assert not any("--remove-label" in a or a[:3] == ["gh", "issue", "edit"] for a in state.commands)


def test_replay_after_recovery_is_idempotent(harness):
    recovered = pr_record(
        labels=[{"name": "author:m1"}, {"name": "author-family:claude-code"}]
    )
    settled = issue_record(
        labels=[{"name": "agent:m1"}, {"name": "status:in-review"}]
    )
    state = harness(pr=recovered, issue=settled)
    state.board = "In Review"
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


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("routine maintenance", "canonical author-family attestation"),
        ("Authored by Claude Example <claude@example.com>", "canonical author-family"),
        ("claude-code anthropic claude", "canonical author-family attestation"),
        (f"Reverts a commit that said {ATTESTED!r} in prose", "canonical author-family"),
        (f"Work.\n\n{ATTESTED}\n{CODEX_ATTESTED}", "canonical author-family attestation"),
        (f"Work.\n\n{CODEX_ATTESTED}", "contradicts the attested historical family"),
    ],
)
def test_only_a_verified_canonical_trailer_proves_lineage(harness, message, error):
    """Display names, prose and quoted trailers carry no historical attestation."""
    harness(commit_message=message)
    with pytest.raises(KernelError, match=error):
        recover()


def test_current_configuration_alone_cannot_supply_lineage(harness):
    """ARU_CODING_REVIEWERS configures identity today; it is not historical proof."""
    harness(commit_message="routine maintenance")
    with pytest.raises(KernelError, match="canonical author-family attestation"):
        recover(author_family="claude-code")
def test_unverified_head_commit_signature_is_refused(harness):
    state = harness()
    state.verified = False
    with pytest.raises(KernelError, match="signature is not verified by GitHub"):
        recover()


def test_verified_commit_from_another_actor_is_refused(harness):
    state = harness()
    state.signer = "someone-else"
    with pytest.raises(KernelError, match="not attributed to the merged PR actor"):
        recover()


def test_configured_identity_contradicting_the_attestation_is_refused(harness):
    """Configuration is not proof, but it may not silently disagree with proof."""
    harness(commit_message=f"Work.\n\n{CODEX_ATTESTED}")
    with pytest.raises(KernelError, match="configured identity family contradicts"):
        recover(author_family="openai-codex")


def test_unreadable_head_commit_evidence_is_refused(harness, monkeypatch):
    harness()
    monkeypatch.setattr(legacy_recovery, "gh_json", lambda args: {"sha": OTHER_HEAD})
    with pytest.raises(KernelError, match="head commit evidence is unreadable"):
        recover()


def test_declared_actor_must_match_the_merged_pr_author(harness):
    harness()
    with pytest.raises(KernelError, match="declared GitHub actor does not match"):
        recover(author_actor="someone-else")


@pytest.mark.parametrize(
    ("kind", "overrides", "message"),
    [
        ("pr", {"mergeCommit": {"oid": "not-a-sha"}}, "missing a readable merge commit"),
        ("pr", {"author": {}}, "no readable GitHub author actor"),
        ("pr", {"labels": [{"name": "author:m1"}, {"name": "author:m2"}]},
         "contradictory author metadata"),
        ("issue", {"state": "OPEN"}, "only applies to a closed linked issue"),
    ],
)
def test_malformed_records_are_refused(harness, kind, overrides, message):
    harness(**{kind: (pr_record if kind == "pr" else issue_record)(**overrides)})
    with pytest.raises(KernelError, match=message):
        recover()


@pytest.mark.parametrize(("label", "status"), [("done", "Done"), ("backlog", "Backlog")])
def test_only_in_progress_advances_to_in_review(harness, label, status):
    state = harness(issue=issue_record(labels=[{"name": "agent:m1"}, {"name": f"status:{label}"}]))
    state.board = status
    with pytest.raises(KernelError, match="cannot move a .* issue to In Review"):
        recover()


def test_observed_drift_before_a_write_refuses_further_mutation(harness):
    state = harness()
    state.drift_after = 1
    with pytest.raises(KernelError, match="precondition drift; no further write"):
        recover(apply=True)
    assert state.commands == [] and state.statuses == []
def failing(real, message, predicate):
    """Wrap a harness call so selected invocations raise instead of succeeding."""
    def call(*args):
        if predicate(*args):
            raise KernelError(message)
        return real(*args)

    return call


def test_observed_closing_issue_drift_stops_every_write(harness, monkeypatch):
    state = harness()

    def drift(pr):
        state.pr["body"] = "Closes #999\n"
        return state.verdict(pr)

    monkeypatch.setattr(legacy_recovery, "finalization_verdict", drift)
    with pytest.raises(KernelError, match="closing issue directive drift"):
        recover(apply=True)
    assert (state.commands, state.statuses) == ([], [])


@pytest.mark.parametrize("board", ["In Progress", "Done", None])
def test_project_disagreement_refuses_including_the_replay_path(harness, board):
    """A failed set_status rollback leaves the label ahead of the card; never report success."""
    state = harness(issue=issue_record(labels=[{"name": "agent:m1"}, {"name": "status:in-review"}]))
    state.board = board
    with pytest.raises(KernelError, match="lifecycle authorities disagree"):
        recover(apply=True)
    assert (state.commands, state.statuses) == ([], [])


def test_unreadable_project_card_refuses(harness, monkeypatch):
    state = harness()
    monkeypatch.setattr(legacy_recovery, "project_item_status",
                        failing(state.project_item_status, "board unavailable", lambda n: True))
    with pytest.raises(KernelError, match="Project card is unreadable"):
        recover()
    assert state.commands == []


@pytest.mark.parametrize("mode", ["raises", "disagrees"])
def test_landed_labels_report_attempted_when_readback_fails(harness, monkeypatch, mode):
    """The edit returned success, so labels landed. Unreadable state is never zero mutation."""
    state = harness()
    stub = (failing(state.gh_json, "readback unavailable", lambda a: a[0] != "api")
            if mode == "raises"
            else lambda a: state.gh_json(a) if a[0] == "api" else {"number": 207, "labels": []})
    monkeypatch.setattr(legacy_recovery, "gh_json", stub)
    with pytest.raises(legacy_recovery.LegacyRecoveryError) as caught:
        recover(apply=True)
    assert state.pr["labels"] and caught.value.receipt["action"] == "partial"
    assert caught.value.receipt["attempted"] == ["author-metadata"]
    assert caught.value.receipt["applied"] == []


def test_real_set_status_partial_failure_is_reported_and_blocks_replay(harness, monkeypatch):
    """Actual common.set_status: the board write fails and the label rollback also fails."""
    state = harness()
    monkeypatch.setattr(legacy_recovery, "set_status", common.set_status)
    monkeypatch.setattr(common, "issue", lambda number, **kw: state.issue_view(number))
    monkeypatch.setattr(common, "project_item_status", lambda number, **kw: state.board)
    monkeypatch.setattr(common, "board_edit", lambda *a, **kw: ["project", "item-edit", "--id", "X"])
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: None)

    def gh(args, **kw):
        if args[:3] == ["gh", "project", "item-edit"]:
            raise KernelError("simulated board write failed")
        if args[:3] == ["gh", "issue", "edit"]:
            added = args[args.index("--add-label") + 1]
            if added == "status:in-progress":
                raise KernelError("simulated label rollback failed")
            state.issue["labels"] = [x for x in state.issue["labels"]
                                     if not x["name"].startswith("status:")] + [{"name": added}]

    monkeypatch.setattr(common, "run", gh)
    with pytest.raises(legacy_recovery.LegacyRecoveryError) as caught:
        recover(apply=True)
    assert caught.value.receipt["attempted"] == ["status"]
    assert common.status_of(state.issue) == "In Review" and state.board == "In Progress"
    with pytest.raises(KernelError, match="lifecycle authorities disagree"):
        recover(apply=True)


def test_receipt_comment_failure_keeps_the_completed_write_report(harness, monkeypatch):
    state = harness()
    monkeypatch.setattr(legacy_recovery, "run",
                        failing(state.run, "receipt unavailable",
                                lambda a: a[:3] == ["gh", "pr", "comment"]))
    with pytest.raises(legacy_recovery.LegacyRecoveryError) as caught:
        recover(apply=True)
    assert caught.value.receipt["applied"] == ["author-metadata", "status"]


RECOVERY = ["--recover-legacy", "207", "--issue", "206", "--expected-head", HEAD]


@pytest.mark.parametrize(
    "argv",
    [
        [*RECOVERY, "--refresh-reviewer", "999", "--apply"],
        ["--reviewer-status", "--recover-legacy", "207"],
        ["--refresh-reviewer", "9", "--expected-head", HEAD],
        ["--issue", "640", "--title", "t", "--body", "b", "--apply"],
        [*RECOVERY, "--title", "t"],
    ],
)
def test_incompatible_public_modes_are_refused_before_any_work(harness, monkeypatch, argv):
    state = harness()
    monkeypatch.setattr(sys, "argv", ["create_pr.py", *argv])
    with pytest.raises(SystemExit) as caught:
        create_pr.main()
    assert caught.value.code == 2
    assert (state.commands, state.statuses, state.ensured) == ([], [], [])


@pytest.mark.parametrize(
    ("extra", "needle"), [(["--json"], '"author-metadata"'), ([], "confirmed writes")]
)
def test_cli_reports_partial_receipts_on_both_output_paths(monkeypatch, capsys, extra, needle):
    error = legacy_recovery.LegacyRecoveryError(
        "simulated Project unavailable",
        {"action": "partial", "applied": ["author-metadata"], "attempted": [], "skipped": []},
    )
    def refuse(args):
        raise error

    monkeypatch.setattr(sys, "argv", ["create_pr.py", *RECOVERY, *extra])
    monkeypatch.setattr(create_pr, "recover_from_args", refuse)
    assert create_pr.main() == 1
    assert needle in capsys.readouterr().err


def test_cli_preview_succeeds_through_the_real_parser(harness, monkeypatch, capsys):
    state = harness()
    monkeypatch.setattr(sys, "argv", ["create_pr.py", *RECOVERY, "--agent", "m1",
                                      "--author-family", "claude-code"])
    assert create_pr.main() == 0
    assert "legacy recovery preview" in capsys.readouterr().out
    assert (state.commands, state.statuses) == ([], [])
