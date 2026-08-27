#!/usr/bin/env python3
"""Fetch every unresolved current review thread, failing closed on truncation."""

from __future__ import annotations

import argparse

from common import KernelError, gh_json, json_print, repo_slug

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


def fetch_feedback(number: int) -> list[dict[str, object]]:
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
        data = gh_json(args)
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
            if thread.get("isResolved") or thread.get("isOutdated"):
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
