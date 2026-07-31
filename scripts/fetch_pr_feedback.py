#!/usr/bin/env python3
"""
fetch_pr_feedback.py - Fetches inline reviewer comments for a Pull Request
and outputs a structured Markdown task list for remediation.
"""

import argparse
import json
import sys
from common import fetch_pr_comments


def main():
    parser = argparse.ArgumentParser(description="Fetch inline PR review comments.")
    parser.add_argument("--pr", type=int, required=True, help="Pull Request Number")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    comments = fetch_pr_comments(args.pr)

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
