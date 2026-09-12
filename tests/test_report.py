from __future__ import annotations

from datetime import datetime, timezone

import pytest

import policy
import report


def rec(**over):
    base = {
        "pr": 1,
        "merged_at": "2026-09-10T12:00:00Z",
        "commits": [{"committed_at": "2026-09-10T09:00:00Z"}],
        "reviews": [],
        "check_runs": [],
        "unresolved_threads": 0,
        "claimed_at": "2026-09-10T08:00:00Z",
    }
    base.update(over)
    return base


# --- window parsing ---------------------------------------------------------

def test_since_accepts_days_weeks_hours():
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    assert report.parse_since("2d", now=now) == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert report.parse_since("1w", now=now) == datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert report.parse_since("12h", now=now) == datetime(2026, 9, 11, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["", "30", "d", "0d", "-5d", "30y", "thirty days"])
def test_malformed_window_refuses(bad):
    with pytest.raises(report.ReportError):
        report.parse_since(bad)


# --- rework -----------------------------------------------------------------

def test_no_review_is_not_rework():
    assert report.reworked(rec(reviews=[])) is False


def test_commit_after_first_review_is_rework():
    r = rec(
        reviews=[{"state": "CHANGES_REQUESTED", "submitted_at": "2026-09-10T10:00:00Z"}],
        commits=[{"committed_at": "2026-09-10T09:00:00Z"}, {"committed_at": "2026-09-10T11:00:00Z"}],
    )
    assert report.reworked(r) is True


def test_commit_before_the_only_review_is_not_rework():
    r = rec(
        reviews=[{"state": "APPROVED", "submitted_at": "2026-09-10T10:00:00Z"}],
        commits=[{"committed_at": "2026-09-10T09:00:00Z"}],
    )
    assert report.reworked(r) is False


def test_rework_measures_against_the_FIRST_review_not_the_last():
    # Two reviews; the push sits between them. That is still rework.
    r = rec(
        reviews=[{"state": "CHANGES_REQUESTED", "submitted_at": "2026-09-10T10:00:00Z"},
                 {"state": "APPROVED", "submitted_at": "2026-09-10T13:00:00Z"}],
        commits=[{"committed_at": "2026-09-10T11:00:00Z"}],
    )
    assert report.reworked(r) is True


# --- claim to merge ---------------------------------------------------------

def test_claim_to_merge_hours():
    assert report.claim_to_merge_hours(rec()) == pytest.approx(4.0)


def test_unclaimed_is_excluded_not_counted_zero():
    assert report.claim_to_merge_hours(rec(claimed_at=None)) is None
    summary = report.summarize([rec(claimed_at=None), rec()])
    assert summary["claim_to_merge_hours"]["measured"] == 1
    assert summary["claim_to_merge_hours"]["unclaimed"] == 1
    assert summary["claim_to_merge_hours"]["median"] == pytest.approx(4.0)


# --- blocks by gate ---------------------------------------------------------

def test_failed_governed_check_counts_against_its_declared_gate():
    r = rec(check_runs=[{"name": "aru-governed-pr", "conclusion": "failure"}])
    assert report.blocks_by_gate([r]) == {"exact-head-consumer-verification": 1}


def test_successful_and_skipped_checks_are_not_blocks():
    runs = [{"name": "aru-governed-pr", "conclusion": c} for c in ("success", "neutral", "skipped", None)]
    assert report.blocks_by_gate([rec(check_runs=runs)]) == {}


def test_changes_requested_and_threads_map_to_their_gates():
    r = rec(reviews=[{"state": "CHANGES_REQUESTED", "submitted_at": "2026-09-10T10:00:00Z"}],
            unresolved_threads=2)
    assert report.blocks_by_gate([r]) == {"approval-by-another-account": 1, "unresolved-findings": 1}


def test_unknown_check_names_are_ignored():
    assert report.blocks_by_gate([rec(check_runs=[{"name": "some-other-ci", "conclusion": "failure"}])]) == {}


# --- the honest limit -------------------------------------------------------

def test_every_mapped_gate_is_actually_declared_by_the_policy():
    report.validate_gate_map()  # raises if a mapping names a retired gate


def test_a_mapping_to_an_undeclared_gate_refuses():
    with pytest.raises(report.ReportError, match="unknown gate ids"):
        report.validate_gate_map(declared={"only-this-one"})


def test_unobservable_gates_are_named_not_hidden():
    names = report.unobservable_gates()
    # merge_pr.py refusals leave no GitHub record, so most gates cannot be counted.
    assert names, "the report must admit which gates it cannot observe"
    assert set(names).issubset(policy.gate_ids())
    assert "exact-head-consumer-verification" not in names  # this one IS observable
    assert "base-head-race" in names                        # this one is not


# --- fail closed on bad evidence --------------------------------------------

@pytest.mark.parametrize("field,value", [("merged_at", None), ("merged_at", "not-a-date"), ("claimed_at", "")])
def test_unreadable_timestamps_refuse_rather_than_defaulting(field, value):
    with pytest.raises(report.ReportError):
        report.claim_to_merge_hours(rec(**{field: value}))


def test_unreadable_review_timestamp_refuses():
    with pytest.raises(report.ReportError):
        report.reworked(rec(reviews=[{"state": "APPROVED", "submitted_at": "yesterday"}]))


def test_empty_input_reports_zero_not_a_crash():
    summary = report.summarize([])
    assert summary["merged_pull_requests"] == 0
    assert summary["rework"]["rate"] is None
    assert summary["claim_to_merge_hours"]["median"] is None


def test_summarize_shape_is_stable():
    summary = report.summarize([rec()])
    assert set(summary) == {
        "merged_pull_requests", "rework", "claim_to_merge_hours",
        "blocks_by_gate", "unobservable_gates",
    }


# --- evidence actually fetched from GitHub -----------------------------------
# These drive fetch_one itself. The pure-function tests above run on synthetic
# records, so they stayed green while the production path read the wrong commit
# and asserted a constant thread count.

PULL = {"number": 7, "merged_at": "2026-09-10T12:00:00Z", "body": "",
        "merge_commit_sha": "MERGECOMMIT", "head": {"sha": "HEADSHA"}}


def _stub_github(monkeypatch, *, check_runs=None, threads=None):
    """Answer fetch_one's GitHub calls from fixtures, recording what it asked."""
    calls = {"check_run_urls": [], "graphql": 0}

    def fake_json(args, **kw):
        if len(args) > 1 and args[1] == "graphql":
            calls["graphql"] += 1
            pages = threads if threads is not None else [[]]
            page = pages[calls["graphql"] - 1]
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": page,
                "pageInfo": {"hasNextPage": calls["graphql"] < len(pages), "endCursor": "c"},
            }}}}}
        url = args[1]
        calls["check_run_urls"].append(url)
        # Model GitHub honestly: the merge commit on the base branch carries
        # none of the pull request's checks. Answering any commit with the same
        # runs would let a test pass against code reading the wrong one.
        served = check_runs if (check_runs is not None and "HEADSHA" in url) else []
        return {"check_runs": served}

    monkeypatch.setattr(report, "gh_paginated", lambda *a, **k: [])
    monkeypatch.setattr(report, "gh_json", fake_json)
    return calls


def test_check_runs_are_read_from_the_head_not_the_merge_commit(monkeypatch):
    calls = _stub_github(monkeypatch)
    report.fetch_one("o/r", dict(PULL))
    assert len(calls["check_run_urls"]) == 1
    url = calls["check_run_urls"][0]
    assert "HEADSHA" in url, url
    assert "MERGECOMMIT" not in url, "governed checks never run on the merge commit"


def test_a_merged_pull_request_reports_its_check_runs(monkeypatch):
    # The defect: every merged PR reported zero runs, because the merge commit
    # on the base branch carries none of the pull request's checks.
    _stub_github(monkeypatch, check_runs=[{"name": "aru-governed-pr", "conclusion": "failure"}])
    record = report.fetch_one("o/r", dict(PULL))
    assert record["check_runs"] == [{"name": "aru-governed-pr", "conclusion": "failure"}]
    assert report.blocks_by_gate([record]) == {"exact-head-consumer-verification": 1}


def test_unresolved_threads_come_from_github_not_a_constant(monkeypatch):
    _stub_github(monkeypatch, threads=[[{"isResolved": False, "isOutdated": False},
                                        {"isResolved": False, "isOutdated": False}]])
    assert report.fetch_one("o/r", dict(PULL))["unresolved_threads"] == 2


def test_resolved_and_outdated_threads_are_not_counted(monkeypatch):
    _stub_github(monkeypatch, threads=[[{"isResolved": True, "isOutdated": False},
                                        {"isResolved": False, "isOutdated": True},
                                        {"isResolved": False, "isOutdated": False}]])
    assert report.unresolved_threads("o/r", 7) == 1


def test_the_thread_count_paginates_past_the_first_hundred(monkeypatch):
    open_thread = {"isResolved": False, "isOutdated": False}
    _stub_github(monkeypatch, threads=[[open_thread] * 100, [open_thread] * 3])
    assert report.unresolved_threads("o/r", 7) == 103


def test_unreadable_thread_evidence_refuses_rather_than_counting_zero(monkeypatch):
    monkeypatch.setattr(report, "gh_json", lambda *a, **k: {"data": {"repository": None}})
    with pytest.raises(report.ReportError, match="review-thread evidence"):
        report.unresolved_threads("o/r", 7)


def test_a_malformed_repository_refuses(monkeypatch):
    with pytest.raises(report.ReportError, match="owner/name"):
        report.unresolved_threads("not-a-slug", 7)


def test_the_thread_count_is_not_a_literal_in_the_production_path():
    from pathlib import Path
    source = Path(report.__file__).read_text(encoding="utf-8")
    assert '"unresolved_threads": 0' not in source, "the count must be read, not asserted"


# --- read-only ---------------------------------------------------------------

def test_report_performs_no_write(tmp_path):
    from pathlib import Path
    source = (Path(report.__file__)).read_text(encoding="utf-8")
    for forbidden in ("write_text(", "open(", "--method POST", "--method PUT", "--method PATCH",
                      "gh issue edit", "gh pr ", "mkdir", "unlink"):
        assert forbidden not in source, forbidden
