#!/usr/bin/env python3
"""Create a governed revert PR without rewriting default-branch history."""

from __future__ import annotations

import argparse
import contextlib

from common import KernelError, gh_json, git, run
from create_branch import create_worktree
from create_pr import create


def merged_pr(number: int) -> dict:
    data = gh_json(
        [
            "pr",
            "view",
            str(number),
            "--json",
            "number,title,mergedAt,mergeCommit,body",
        ]
    )
    merge = data.get("mergeCommit") if isinstance(data, dict) else None
    if not data.get("mergedAt") or not isinstance(merge, dict) or not merge.get("oid"):
        raise KernelError(f"PR #{number} is not a merged pull request")
    return data


def create_revert(original: int, revert_issue: int, agent: str) -> dict[str, object]:
    source = merged_pr(original)
    worktree = create_worktree(revert_issue, "fix", agent)
    path = worktree["path"]
    merge_sha = str(source["mergeCommit"]["oid"])
    parents = git(["show", "-s", "--format=%P", merge_sha], cwd=path).split()
    command = ["git", "-c", "core.fsmonitor=false", "revert", "--no-edit"]
    if len(parents) > 1:
        command.extend(["-m", "1"])
    command.append(merge_sha)
    result = run(command, cwd=path, check=False)
    if result.returncode:
        raise KernelError(
            "revert has conflicts; resolve them in the preserved worktree before continuing"
        )
    git(["push", "--set-upstream", "origin", worktree["branch"]], cwd=path)
    title = f"revert: PR #{original} {source['title']}"
    body = (
        f"Reverts merged PR #{original} at {merge_sha}.\n\n"
        "This is the governed reverse gear; the original history is preserved."
    )
    with contextlib.chdir(path):  # create() reads the branch and diff from the working directory
        result_pr = create(revert_issue, title, body, agent)
    return {
        "original_pr": original,
        "revert_issue": revert_issue,
        "worktree": path,
        "revert_pr": result_pr["pr"],
        "head": result_pr["head"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--revert-issue", type=int, required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    try:
        result = create_revert(args.pr, args.revert_issue, args.agent)
    except KernelError as exc:
        parser.error(str(exc))
    print(f"opened revert PR #{result['revert_pr']} from {result['worktree']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
