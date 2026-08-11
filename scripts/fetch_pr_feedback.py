#!/usr/bin/env python3
"""Fetch active inline review feedback for a pull request.

GitHub's REST review-comment endpoint includes resolved and outdated comments,
while ``reviewDecision`` is blank for same-account COMMENTED reviews.  The
worker loop and this checklist therefore share the GraphQL review-thread state:
only unresolved, non-outdated threads are actionable.
"""

import argparse
import json
import sys

from common import get_repo_slug, run_gh_json


def fetch_active_review_feedback(pr_id: int) -> list[dict] | None:
    """Returns root comments for active review threads, or None on uncertainty.

    A plain issue comment is intentionally outside this query and cannot route
    feedback.  Resolved or outdated threads are equally excluded.  Any API,
    schema, pagination, or partial-data failure returns ``None`` so callers
    fail closed instead of treating an unreadable review queue as empty.
    """
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, name = slug.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          reviewThreads(first:100, after:$cursor) {
            nodes {
              isResolved
              isOutdated
              comments(first:1) {
                nodes {
                  databaseId body path line originalLine url
                  author { login }
                  commit { oid }
                }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    cursor = None
    seen_cursors = set()
    expected_head_oid = None
    feedback = []

    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = run_gh_json(args)
        if not data or not isinstance(data, dict) or data.get("errors"):
            return None
        try:
            pull_request = data["data"]["repository"]["pullRequest"]
            head_oid = pull_request["headRefOid"]
            connection = pull_request["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if (not isinstance(head_oid, str) or not head_oid
                or not isinstance(nodes, list)
                or not isinstance(page_info, dict)
                or not isinstance(has_next, bool)):
            return None
        if expected_head_oid is None:
            expected_head_oid = head_oid
        elif head_oid != expected_head_oid:
            # Pagination is not an atomic GitHub snapshot.  A push between
            # pages makes both the thread set and isOutdated flags ambiguous,
            # so the only safe result is unknown and a later retry.
            return None

        for thread in nodes:
            if not isinstance(thread, dict):
                return None
            resolved = thread.get("isResolved")
            outdated = thread.get("isOutdated")
            if not isinstance(resolved, bool) or not isinstance(outdated, bool):
                return None
            if resolved or outdated:
                continue
            try:
                comments = thread["comments"]["nodes"]
            except (KeyError, TypeError):
                return None
            if not isinstance(comments, list) or len(comments) != 1:
                return None
            comment = comments[0]
            if not isinstance(comment, dict):
                return None
            author = comment.get("author")
            commit = comment.get("commit")
            if author is not None and not isinstance(author, dict):
                return None
            if commit is not None and not isinstance(commit, dict):
                return None
            feedback.append({
                "id": comment.get("databaseId"),
                "body": comment.get("body") or "",
                "path": comment.get("path") or "file",
                "line": comment.get("line"),
                "original_line": comment.get("originalLine"),
                "url": comment.get("url"),
                "user": {"login": (author or {}).get("login") or "reviewer"},
                "commit_oid": (commit or {}).get("oid"),
                "head_oid": expected_head_oid,
            })

        if not has_next:
            return feedback
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def main():
    parser = argparse.ArgumentParser(description="Fetch inline PR review comments.")
    parser.add_argument("--pr", type=int, required=True, help="Pull Request Number")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    comments = fetch_active_review_feedback(args.pr)
    if comments is None:
        print(f"[ERROR] Could not read active review feedback for PR #{args.pr}.",
              file=sys.stderr)
        raise SystemExit(1)

    if args.json:
        print(json.dumps(comments, indent=2))
        return

    print(f"=== PR #{args.pr} Review Feedback Checklist ===")
    if not comments:
        print("✨ No unresolved review comments found.")
        return

    for idx, c in enumerate(comments, 1):
        path = c.get("path", "file")
        line = c.get("line") or c.get("original_line") or "N/A"
        user = c.get("user", {}).get("login", "reviewer")
        body = c.get("body", "").strip()
        print(f"{idx}. [ ] [{path}:L{line}] (@{user}): {body}")


if __name__ == "__main__":
    main()
