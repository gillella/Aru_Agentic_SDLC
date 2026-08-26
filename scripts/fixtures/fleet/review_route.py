"""Small emergency-review helpers for the hermetic fleet scenario."""

from typing import Any


def different_family_reviewer(workers: dict[str, str], author: str) -> str:
    return next(
        agent for agent, family in workers.items()
        if agent != author and family != workers[author]
    )


def completion_evidence(
    pull_requests: dict[int, Any], number: int, workers: dict[str, str],
) -> dict[str, Any]:
    pr = pull_requests[number]
    head = f"{pr.head:040x}"
    reviewer = pr.reviewed_by
    family = workers.get(reviewer or "", "")
    return {
        "head_oid": head,
        "head_commit_committed_at": "2026-08-14T09:00:00Z",
        "agent_review_attestations": ([{
            "agent": reviewer, "family": family, "head": head,
            "disposition": "findings-resolved", "github_login": "fixture-account",
            "completed_at": "2026-08-14T09:03:00Z",
        }] if reviewer else []),
        "agent_review_assignments": ([{
            "reviewer": reviewer, "family": family, "head": head,
            "assigned_at": "2026-08-14T09:01:00Z",
        }] if reviewer else []),
        "reviews": ([{
            "state": "COMMENTED", "body": "Substantive fixture review.",
            "commit": {"oid": head},
            "author": {"login": "fixture-account", "__typename": "User"},
            "submittedAt": "2026-08-14T09:02:00Z",
        }] if reviewer else []),
        "unresolved": 0, "outdated_unfixed": 0, "unfixed": 0,
        "agent_review_marker_errors": [],
        "agent_review_assignment_errors": [],
    }
