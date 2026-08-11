#!/usr/bin/env python3
"""Fetch active review feedback for a pull request.

GitHub's REST review-comment endpoint includes resolved and outdated comments,
while ``reviewDecision`` is blank for same-account COMMENTED reviews.  The
worker loop and this checklist therefore share one GraphQL snapshot containing
unresolved threads and body-only change requests on the current head.
"""

import argparse
import json
import sys

from common import get_repo_slug, run_gh_json
from merge_pr import is_advisory_review_account


def fetch_active_review_feedback(pr_id: int) -> list[dict] | None:
    """Returns current actionable review items, or None on uncertainty.

    A plain issue comment is intentionally outside this query and cannot route
    feedback. Resolved/outdated threads and reviews of older heads are excluded.
    A body-only change request is included; a change request with inline
    comments is represented by its still-active threads without duplication.
    Any API, schema, pagination, or partial-data failure returns ``None`` so
    callers fail closed instead of treating an unreadable queue as empty.
    """
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, name = slug.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $pr:Int!,
          $threadCursor:String, $reviewCursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          reviewThreads(first:100, after:$threadCursor) {
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
          reviews(first:100, after:$reviewCursor) {
            nodes {
              databaseId state body submittedAt url
              author { login }
              commit { oid }
              comments { totalCount }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    thread_cursor = None
    review_cursor = None
    seen_thread_cursors = set()
    seen_review_cursors = set()
    threads_done = False
    reviews_done = False
    expected_head_oid = None
    feedback = []
    reviews = []

    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"pr={pr_id}",
        ]
        if thread_cursor:
            args.extend(["-F", f"threadCursor={thread_cursor}"])
        if review_cursor:
            args.extend(["-F", f"reviewCursor={review_cursor}"])
        data = run_gh_json(args)
        if not data or not isinstance(data, dict) or data.get("errors"):
            return None
        try:
            pull_request = data["data"]["repository"]["pullRequest"]
            head_oid = pull_request["headRefOid"]
            thread_connection = pull_request["reviewThreads"]
            thread_nodes = thread_connection["nodes"]
            thread_page = thread_connection["pageInfo"]
            thread_has_next = thread_page["hasNextPage"]
            review_connection = pull_request["reviews"]
            review_nodes = review_connection["nodes"]
            review_page = review_connection["pageInfo"]
            review_has_next = review_page["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if (not isinstance(head_oid, str) or not head_oid
                or not isinstance(thread_nodes, list)
                or not isinstance(thread_page, dict)
                or not isinstance(thread_has_next, bool)
                or not isinstance(review_nodes, list)
                or not isinstance(review_page, dict)
                or not isinstance(review_has_next, bool)):
            return None
        if expected_head_oid is None:
            expected_head_oid = head_oid
        elif head_oid != expected_head_oid:
            # Pagination is not an atomic GitHub snapshot.  A push between
            # pages makes both the thread set and isOutdated flags ambiguous,
            # so the only safe result is unknown and a later retry.
            return None

        if not threads_done:
            for thread in thread_nodes:
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

        if not reviews_done:
            for review in review_nodes:
                if not isinstance(review, dict):
                    return None
                state = review.get("state")
                author = review.get("author")
                commit = review.get("commit")
                comments = review.get("comments")
                if not isinstance(state, str):
                    return None
                if state.upper() in {"PENDING", "COMMENTED"}:
                    # Neither state changes an earlier formal verdict.
                    continue
                if (author is not None and not isinstance(author, dict)):
                    return None
                if commit is not None and not isinstance(commit, dict):
                    return None
                if (not isinstance(comments, dict)
                        or not isinstance(comments.get("totalCount"), int)
                        or not isinstance(review.get("body"), str)
                        or not isinstance(review.get("submittedAt"), str)):
                    return None
                reviews.append(review)

        if not threads_done:
            if thread_has_next:
                next_cursor = thread_page.get("endCursor")
                if (not isinstance(next_cursor, str) or not next_cursor
                        or next_cursor in seen_thread_cursors):
                    return None
                seen_thread_cursors.add(next_cursor)
                thread_cursor = next_cursor
            else:
                threads_done = True
        if not reviews_done:
            if review_has_next:
                next_cursor = review_page.get("endCursor")
                if (not isinstance(next_cursor, str) or not next_cursor
                        or next_cursor in seen_review_cursors):
                    return None
                seen_review_cursors.add(next_cursor)
                review_cursor = next_cursor
            else:
                reviews_done = True
        if threads_done and reviews_done:
            break

    # GitHub's review connection is historical. Only each account's latest
    # submitted verdict can still block, matching the merge gate's semantics.
    latest_by_author = {}
    for review in reviews:
        author = review.get("author") or {}
        review_id = review.get("databaseId")
        login = author.get("login") or f"deleted-reviewer:{review_id}"
        order = (review["submittedAt"], review_id if isinstance(review_id, int) else -1)
        if login not in latest_by_author or order > latest_by_author[login][0]:
            latest_by_author[login] = (order, review)

    for _, review in latest_by_author.values():
        if review["state"].upper() != "CHANGES_REQUESTED":
            continue
        author = review.get("author") or {}
        if is_advisory_review_account(author.get("login")):
            continue
        commit_oid = (review.get("commit") or {}).get("oid")
        if commit_oid != expected_head_oid or review["comments"]["totalCount"] != 0:
            continue
        body = review["body"].strip()
        if not body:
            continue
        feedback.append({
            "id": review.get("databaseId"),
            "body": body,
            "path": "Pull request review",
            "line": None,
            "original_line": None,
            "url": review.get("url"),
            "user": {"login": author.get("login") or "reviewer"},
            "commit_oid": commit_oid,
            "head_oid": expected_head_oid,
        })
    return feedback


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
