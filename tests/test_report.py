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


def test_successful_check_is_not_a_block():
    runs = [{"name": "aru-governed-pr", "conclusion": "success"}]
    assert report.blocks_by_gate([rec(check_runs=runs)]) == {}


def test_changes_requested_is_history_while_threads_map_to_findings_gate():
    r = rec(reviews=[{"state": "CHANGES_REQUESTED", "submitted_at": "2026-09-10T10:00:00Z"}],
            unresolved_threads=2)
    assert report.blocks_by_gate([r]) == {"unresolved-findings": 1}


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
    assert summary["post_first_review_commit_activity"]["share"] is None
    assert summary["claim_to_merge_hours"]["median"] is None


def test_summarize_shape_is_stable():
    summary = report.summarize([rec()])
    assert set(summary) == {
        "merged_pull_requests", "post_first_review_commit_activity", "claim_to_merge_hours",
        "blocks_by_gate", "unobservable_gates", "review_events", "current_unresolved_findings",
        "review_threads", "check_evidence",
    }


# --- evidence actually fetched from GitHub -----------------------------------
# These drive fetch_one itself. The pure-function tests above run on synthetic
# records, so they stayed green while the production path read the wrong commit
# and asserted a constant thread count.

HEAD = "a" * 40
PULL = {"number": 7, "created_at": "2026-09-10T08:00:00Z",
        "merged_at": "2026-09-10T12:00:00Z", "body": "",
        "merge_commit_sha": "b" * 40, "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "repo": {"full_name": "o/r"}}}


def thread(number, *, resolved=False, outdated=False, review_id=None):
    return {"id": f"thread-{number}", "isResolved": resolved, "isOutdated": outdated,
            "comments": {"nodes": [{"pullRequestReview": {"databaseId": review_id} if review_id else None}],
                         "pageInfo": {"hasNextPage": False}}}


def check(run_id=10, *, app_id=15368, head=HEAD, conclusion="failure"):
    return {"id": run_id + 1, "name": "aru-governed-pr", "head_sha": head,
            "app": {"id": app_id, "slug": "github-actions"},
            "details_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{run_id + 1}",
            "check_suite": {"id": run_id + 100}, "status": "completed", "conclusion": conclusion,
            "started_at": "2026-09-10T09:02:00Z", "completed_at": "2026-09-10T09:03:00Z"}


def run(run_id=10, *, attempt=1, conclusion="failure"):
    return {"id": run_id, "workflow_id": 42, "check_suite_id": run_id + 100,
            "run_attempt": attempt, "event": "pull_request", "path": ".github/workflows/governed-pr.yml",
            "head_sha": HEAD, "head_branch": "feature", "repository": {"full_name": "o/r"},
            "head_repository": {"full_name": "o/r"}, "pull_requests": [],
            "created_at": "2026-09-10T09:00:00Z", "run_started_at": "2026-09-10T09:01:00Z",
            "updated_at": "2026-09-10T09:04:00Z", "status": "completed", "conclusion": conclusion}


def _stub_github(monkeypatch, *, check_runs=None, runs=None, check_pages=None, run_pages=None,
                 threads=None, summary_pages=None,
                 check_total=None, run_total=None, commits=None):
    """Answer fetch_one's GitHub calls from fixtures, recording what it asked."""
    calls = {"urls": [], "threads": 0, "summaries": 0}

    def fake_json(args, **kw):
        if len(args) > 1 and args[1] == "graphql":
            query = next(a for a in args if a.startswith("query="))
            if "reviewThreads" in query:
                calls["threads"] += 1
                pages = threads if threads is not None else [[]]
                index = calls["threads"] - 1
                return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                    "nodes": pages[index], "pageInfo": {"hasNextPage": index + 1 < len(pages),
                                                   "endCursor": f"thread-cursor-{index}"},
                }}}}}
            calls["summaries"] += 1
            pages = summary_pages if summary_pages is not None else [[]]
            index = calls["summaries"] - 1
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": HEAD, "author": {"login": "writer"},
                "reviews": {"nodes": pages[index],
                            "pageInfo": {"hasNextPage": index + 1 < len(pages),
                                         "endCursor": f"review-cursor-{index}"}},
            }}}}
        url = args[-1]
        calls["urls"].append(url)
        if url.endswith("governed-pr.yml"):
            return {"id": 42, "path": ".github/workflows/governed-pr.yml", "state": "active"}
        if "/runs?" in url:
            served = runs if runs is not None else [run()]
            pages = run_pages if run_pages is not None else [served]
            total = run_total if run_total is not None else sum(map(len, pages))
            return [{"total_count": total, "workflow_runs": page} for page in pages]
        if "/check-runs?" in url:
            served = check_runs if check_runs is not None else [check()]
            pages = check_pages if check_pages is not None else [served]
            total = check_total if check_total is not None else sum(map(len, pages))
            return [{"total_count": total, "check_runs": page} for page in pages]
        raise AssertionError(url)

    def fake_paginated(endpoint, **kw):
        if "/commits?" in endpoint:
            return commits if commits is not None else [{"commit": {"committer": {"date": "2026-09-10T09:00:00Z"}}}]
        return []

    monkeypatch.setattr(report, "gh_paginated", fake_paginated)
    monkeypatch.setattr(report, "gh_json", fake_json)
    return calls


def test_check_runs_are_read_from_the_head_not_the_merge_commit(monkeypatch):
    calls = _stub_github(monkeypatch)
    report.fetch_one("o/r", dict(PULL))
    assert any(f"commits/{HEAD}/check-runs" in url for url in calls["urls"])
    assert not any(PULL["merge_commit_sha"] in url for url in calls["urls"])


def test_a_merged_pull_request_reports_its_check_runs(monkeypatch):
    # The defect: every merged PR reported zero runs, because the merge commit
    # on the base branch carries none of the pull request's checks.
    _stub_github(monkeypatch)
    record = report.fetch_one("o/r", dict(PULL))
    assert record["check_runs"] == [{"name": "aru-governed-pr", "conclusion": "failure",
                                     "run_id": 10, "run_attempt": 1, "head": HEAD,
                                     "workflow_id": 42, "check_id": 11, "check_suite_id": 110,
                                     "event": "pull_request", "app": "github-actions", "app_id": 15368}]
    assert report.blocks_by_gate([record]) == {"exact-head-consumer-verification": 1}
    assert report.summarize([record])["check_evidence"] == [{"pr": 7, "final_head": HEAD,
                                                               "runs": record["check_runs"]}]


def test_unresolved_threads_come_from_github_not_a_constant(monkeypatch):
    _stub_github(monkeypatch, threads=[[thread(1), thread(2)]])
    assert report.fetch_one("o/r", dict(PULL))["unresolved_threads"] == 2


def test_resolved_threads_are_not_counted_but_outdated_unresolved_are(monkeypatch):
    _stub_github(monkeypatch, threads=[[thread(1, resolved=True), thread(2, outdated=True), thread(3)]])
    assert report.unresolved_threads("o/r", 7) == 2


def test_report_breaks_out_resolved_and_outdated_thread_counts(monkeypatch):
    _stub_github(monkeypatch, threads=[[thread(1, resolved=True), thread(2, outdated=True), thread(3)]])
    record = report.fetch_one("o/r", dict(PULL))
    summary = report.summarize([record])
    assert summary["review_threads"] == {"unresolved": 2, "unresolved_outdated": 1, "resolved": 1}


def test_the_thread_count_paginates_past_the_first_hundred(monkeypatch):
    _stub_github(monkeypatch, threads=[[thread(i) for i in range(100)],
                                        [thread(i) for i in range(100, 103)]])
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


def test_outdated_unresolved_thread_is_a_current_finding(monkeypatch):
    _stub_github(monkeypatch, threads=[[thread(1, outdated=True), thread(2, resolved=True)]])
    assert report.unresolved_threads("o/r", 7) == 1


def test_historical_changes_requested_is_not_a_current_gate_block():
    record = rec(reviews=[{"state": "CHANGES_REQUESTED", "submitted_at": "2026-09-10T10:00:00Z"},
                          {"state": "APPROVED", "submitted_at": "2026-09-10T11:00:00Z"}])
    assert "approval-by-another-account" not in report.blocks_by_gate([record])
    assert report.summarize([record])["review_events"]["changes_requested"] == 1


def summary_review(number, body="[P1] Fix this", *, state="CHANGES_REQUESTED",
                   submitted="2026-09-10T10:00:00Z"):
    return {"databaseId": number, "author": {"login": "reviewer"}, "state": state,
            "body": body, "url": f"https://github.com/o/r/pull/7#pullrequestreview-{number}",
            "submittedAt": submitted, "lastEditedAt": None, "commit": {"oid": HEAD}}


def test_blocking_summary_resolution_and_thread_overlap_are_not_duplicated(monkeypatch):
    findings = [summary_review(1), summary_review(2), summary_review(3),
                summary_review(4, "Resolves review: 3\nAddressed", state="COMMENTED",
                               submitted="2026-09-10T11:00:00Z")]
    _stub_github(monkeypatch, threads=[[thread(1, review_id=1)]], summary_pages=[findings])
    assert report.review_findings("o/r", 7, HEAD) == (1, 1, 0, 0)


def test_duplicate_review_summary_across_pages_refuses(monkeypatch):
    _stub_github(monkeypatch, summary_pages=[[summary_review(1)], [summary_review(1)]])
    with pytest.raises(report.ReportError, match="duplicate review"):
        report.review_findings("o/r", 7, HEAD)


def test_review_thread_with_truncated_comments_refuses(monkeypatch):
    item = thread(1)
    item["comments"]["pageInfo"]["hasNextPage"] = True
    _stub_github(monkeypatch, threads=[[item]])
    with pytest.raises(report.ReportError, match="truncated"):
        report.unresolved_threads("o/r", 7)


def test_check_runs_across_pages_and_repeated_runs_count_one_pr(monkeypatch):
    _stub_github(monkeypatch, run_pages=[[run(10)], [run(20, attempt=2)]],
                 check_pages=[[check(10)], [check(20)]])
    record = report.fetch_one("o/r", dict(PULL))
    assert [item["run_attempt"] for item in record["check_runs"]] == [1, 2]
    assert report.blocks_by_gate([record]) == {"exact-head-consumer-verification": 1}


@pytest.mark.parametrize("changed", [{"app_id": 999}, {"head": "b" * 40}])
def test_unrelated_same_name_check_cannot_impersonate_governed_run(monkeypatch, changed):
    _stub_github(monkeypatch, check_runs=[check(**changed)])
    with pytest.raises(report.KernelError):
        report.fetch_one("o/r", dict(PULL))


def test_missing_run_or_incomplete_check_pagination_refuses(monkeypatch):
    _stub_github(monkeypatch, runs=[], check_runs=[])
    with pytest.raises(report.ReportError, match="no verified"):
        report.fetch_one("o/r", dict(PULL))
    _stub_github(monkeypatch, check_total=2)
    with pytest.raises(report.ReportError, match="truncated"):
        report.fetch_one("o/r", dict(PULL))


def test_duplicate_check_page_cannot_mask_missing_evidence(monkeypatch):
    item = check()
    _stub_github(monkeypatch, check_pages=[[item], [item]])
    with pytest.raises(report.ReportError, match="duplicate"):
        report.fetch_one("o/r", dict(PULL))


def test_nameless_check_in_inventory_is_not_silently_ignored(monkeypatch):
    _stub_github(monkeypatch, check_runs=[check(), {"id": 999}])
    with pytest.raises(report.KernelError, match="nameless"):
        report.fetch_one("o/r", dict(PULL))


def test_missing_head_refuses_before_zero_check_total(monkeypatch):
    _stub_github(monkeypatch)
    bad = {**PULL, "head": {"sha": None, "ref": "feature"}}
    with pytest.raises(report.ReportError, match="final head"):
        report.fetch_one("o/r", bad)


def test_unrelated_pr_head_repository_refuses(monkeypatch):
    _stub_github(monkeypatch)
    bad = {**PULL, "head": {**PULL["head"], "repo": {"full_name": "other/r"}}}
    with pytest.raises(report.ReportError, match="final head"):
        report.fetch_one("o/r", bad)


def test_missing_commit_inventory_does_not_become_no_post_review_activity(monkeypatch):
    _stub_github(monkeypatch, commits=[])
    with pytest.raises(report.ReportError, match="commit evidence"):
        report.fetch_one("o/r", dict(PULL))


def test_merged_only_sample_exposes_truncation(monkeypatch):
    pulls = [{**PULL, "number": n, "merged_at": f"2026-09-1{n}T12:00:00Z"}
             for n in (1, 2, 3)] + [{**PULL, "number": 4, "merged_at": None}]
    monkeypatch.setattr(report, "gh_paginated", lambda *a, **k: pulls)
    monkeypatch.setattr(report, "fetch_one", lambda repo, pull: {"pr": pull["number"]})
    records, truncated = report.fetch_sample("o/r", datetime(2026, 9, 1, tzinfo=timezone.utc), 2)
    assert [record["pr"] for record in records] == [3, 2]
    assert truncated is True


def test_sample_excludes_merges_after_its_window_end(monkeypatch):
    pulls = [{**PULL, "number": 1, "merged_at": "2026-09-10T12:00:00Z"},
             {**PULL, "number": 2, "merged_at": "2026-09-12T12:00:00Z"}]
    monkeypatch.setattr(report, "gh_paginated", lambda *a, **k: pulls)
    monkeypatch.setattr(report, "fetch_one", lambda repo, pull: {"pr": pull["number"]})
    records, truncated = report.fetch_sample("o/r", datetime(2026, 9, 1, tzinfo=timezone.utc), 2,
                                              until=datetime(2026, 9, 11, tzinfo=timezone.utc))
    assert [record["pr"] for record in records] == [1]
    assert truncated is False
