#!/usr/bin/env python3
"""Fetch unresolved threads and blocking review summaries from GitHub."""

from __future__ import annotations

import argparse
import re
from datetime import datetime

from common import REPOSITORY_AUTH, KernelError, canonical_github_actor, gh_json, json_print, repo_slug

QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100,after:$after){
        nodes{
          isResolved
          isOutdated
          path
          line
          comments(first:50){
            nodes{author{login} body url createdAt}
            pageInfo{hasNextPage}
          }
        }
        pageInfo{hasNextPage endCursor}
      }
    }
  }
}
"""


def fetch_threads(number: int) -> list[dict[str, object]]:
    owner, name = repo_slug().split("/", 1)
    cursor: str | None = None
    feedback: list[dict[str, object]] = []
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor:
            args.extend(["-F", f"after={cursor}"])
        data = gh_json(args, auth=REPOSITORY_AUTH)
        pull = ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
        connection = (pull or {}).get("reviewThreads")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise KernelError("review-thread state is incomplete")
        for thread in connection["nodes"]:
            if not isinstance(thread, dict):
                raise KernelError("review-thread state is malformed")
            comments = thread.get("comments")
            if not isinstance(comments, dict) or comments.get("pageInfo", {}).get("hasNextPage"):
                raise KernelError("review comments are truncated")
            if thread.get("isResolved"):
                continue
            nodes = comments.get("nodes")
            if not isinstance(nodes, list) or not nodes:
                raise KernelError("unresolved review thread has no comment")
            latest = nodes[-1]
            feedback.append(
                {
                    "path": thread.get("path"),
                    "line": thread.get("line"),
                    "author": (latest.get("author") or {}).get("login"),
                    "body": latest.get("body"),
                    "url": latest.get("url"),
                }
            )
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise KernelError("review-thread pagination is incomplete")
    return feedback


SUMMARY_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      headRefOid author{login}
      reviews(first:100,after:$after){
        nodes{databaseId author{login} state body url submittedAt commit{oid}}
        pageInfo{hasNextPage endCursor}
      }
    }
  }
}
"""
# Severity labels, not natural-language judgement. Reviewers must use these labels
# or an unresolved inline thread; prose such as "no P1 findings" is not a label.
BLOCKING_LABEL = re.compile(r"\[P[01](?:\]|\s+Badge\])|(?m:^)[ \t]*(?:#{1,6}\s*)?(?:[-*]|\d+[.)])?\s*\*{0,2}P[01]\s*:", re.I)
RESOLUTION = re.compile(r"^Resolves review: ([1-9][0-9]*)$", re.M)
STATES = {"COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED", "PENDING"}


def _identity(value: object) -> str:
    login = value.get("login") if isinstance(value, dict) else None
    if not isinstance(login, str) or not login.strip():
        raise KernelError("review summary identity is unreadable")
    return canonical_github_actor(login)


def _submitted_review(review: object) -> dict | None:
    if not isinstance(review, dict) or review.get("state") not in STATES:
        raise KernelError("review summary state is malformed")
    if review["state"] == "PENDING":
        return None  # Unsubmitted drafts are not review evidence.
    number = review.get("databaseId")
    body, url, stamp = (review.get(key) for key in ("body", "url", "submittedAt"))
    commit = review.get("commit")
    head = commit.get("oid") if isinstance(commit, dict) else None
    if (type(number) is not int or number <= 0 or not isinstance(body, str)
            or not isinstance(url, str) or not url.startswith("https://")
            or not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head)):
        raise KernelError("review summary evidence is malformed")
    try:
        submitted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if submitted.tzinfo is None:
            raise ValueError("missing timezone")
    except (AttributeError, TypeError, ValueError) as exc:
        raise KernelError("review summary submission time is malformed") from exc
    return {**review, "reviewer": _identity(review.get("author")), "submitted": submitted}


def _summary_page(number: int, cursor: str | None) -> tuple[dict, dict]:
    owner, name = repo_slug().split("/", 1)
    args = ["api", "graphql", "-f", f"query={SUMMARY_QUERY}", "-F", f"owner={owner}",
            "-F", f"name={name}", "-F", f"number={number}"]
    if cursor:
        args.extend(["-F", f"after={cursor}"])
    data = gh_json(args, auth=REPOSITORY_AUTH)
    if not isinstance(data, dict) or data.get("errors"):
        raise KernelError("review summary inventory is incomplete")
    root = data.get("data")
    repo = root.get("repository") if isinstance(root, dict) else None
    pr = repo.get("pullRequest") if isinstance(repo, dict) else None
    connection = pr.get("reviews") if isinstance(pr, dict) else None
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        raise KernelError("review summary inventory is incomplete")
    return pr, connection


def _next_cursor(connection: dict, seen: set[str]) -> str | None:
    page = connection.get("pageInfo")
    if not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool:
        raise KernelError("review summary pagination is incomplete")
    if not page["hasNextPage"]:
        return None
    cursor = page.get("endCursor")
    if not isinstance(cursor, str) or not cursor or cursor in seen:
        raise KernelError("review summary pagination is incomplete or repeated")
    seen.add(cursor)
    return cursor


def _summary_inventory(number: int) -> tuple[str, str, list[dict]]:
    cursor, identity = None, None
    seen_cursors, seen_ids = set(), set()
    reviews = []
    while True:
        pr, connection = _summary_page(number, cursor)
        head = pr.get("headRefOid")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
            raise KernelError("review summary head is unreadable")
        current = (head, _identity(pr.get("author")))
        if identity is not None and identity != current:
            raise KernelError("review summary head or author changed during pagination")
        identity = current
        for node in connection["nodes"]:
            review = _submitted_review(node)
            if review is None:
                continue
            if review["databaseId"] in seen_ids:
                raise KernelError("review summary inventory contains duplicate reviews")
            seen_ids.add(review["databaseId"])
            reviews.append(review)
        cursor = _next_cursor(connection, seen_cursors)
        if cursor is None:
            return *identity, reviews


def _resolves(candidate: dict, finding: dict, head: str, author: str) -> bool:
    body = candidate["body"]
    return (
        candidate["state"] in {"COMMENTED", "APPROVED"}
        and candidate["commit"]["oid"] == head
        and candidate["reviewer"] == finding["reviewer"]
        and candidate["reviewer"] != author
        and candidate["submitted"] > finding["submitted"]
        and str(finding["databaseId"]) in RESOLUTION.findall(body)
        and bool(RESOLUTION.sub("", body).strip())
        and not BLOCKING_LABEL.search(body)
        # A quoted example is not an explicit resolution instruction.
        and "```" not in body and "~~~" not in body
    )


def fetch_summary_feedback(number: int) -> list[dict[str, object]]:
    head, author, reviews = _summary_inventory(number)
    feedback = []
    for review in reviews:
        if not BLOCKING_LABEL.search(review["body"]):
            continue
        if any(_resolves(candidate, review, head, author) for candidate in reviews):
            continue
        feedback.append({
            "path": "(review summary)", "line": None,
            "author": review["author"]["login"], "body": review["body"],
            "url": review["url"], "review_id": review["databaseId"],
        })
    return feedback


def fetch_feedback(number: int) -> list[dict[str, object]]:
    return fetch_threads(number) + fetch_summary_feedback(number)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        feedback = fetch_feedback(args.pr)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print({"pr": args.pr, "feedback": feedback})
    elif not feedback:
        print("No unresolved review feedback.")
    else:
        for item in feedback:
            print(f"{item['path']}:{item['line'] or '-'} {item['author']}: {item['url']}")
    return 1 if feedback else 0


if __name__ == "__main__":
    raise SystemExit(main())
