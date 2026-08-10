#!/usr/bin/env python3
"""
create_pr.py - Opens a Pull Request pre-populated with issue linking ('Closes #X').

Also stamps who wrote it. Every agent authenticates as the same GitHub user, so
`github.actor` cannot distinguish them - the same reason claiming needs its own
`agent:<id>` label. Review eligibility depends on knowing the author, so the
identity has to be on the PR itself:

  author:<agent-id>   never review your own work
  family:<family>     prefer a reviewer whose model blind spots differ

Family, not tool: Cursor running Sonnet has the same blind spots as Claude Code
running Sonnet, so "a different tool" is not necessarily a different reviewer.
"""

import argparse
import sys

from common import ensure_label, get_current_branch, get_issue, run_cmd

# Kept explicit rather than free-form: a typo like "anthropc" would silently
# make every PR look cross-family to the picker, which is the one failure mode
# this label exists to prevent.
MODEL_FAMILIES = ("anthropic", "openai", "google", "meta", "mistral", "xai", "human")


def apply_identity(pr_ref: str, agent: str = "", family: str = "") -> None:
    """Labels the PR with its author agent and model family.

    Best-effort: a PR that opened successfully must not be reported as failed
    because a label did not stick. An unlabelled PR degrades to "unknown
    author", which the picker treats conservatively.
    """
    labels = []
    if agent:
        name = f"author:{agent}"
        ensure_label(name, "1d76db", f"PR authored by agent '{agent}'")
        labels.append(name)
    if family:
        name = f"family:{family}"
        ensure_label(name, "d4a27f", f"PR authored by a {family}-family model")
        labels.append(name)
    if not labels:
        return

    cmd = ["gh", "pr", "edit", pr_ref]
    for label in labels:
        cmd += ["--add-label", label]
    code, _, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[WARN] Could not stamp {', '.join(labels)}: {err.strip()}", file=sys.stderr)
        print("[WARN] Review routing will treat this PR as having an unknown author.",
              file=sys.stderr)
    else:
        print(f"🏷️  Stamped {', '.join(labels)}")


def create_pr(issue_id: int, title: str = "", body: str = "",
              agent: str = "", family: str = "") -> bool:
    current_branch = get_current_branch()
    issue = get_issue(issue_id)

    if not title:
        title = issue["title"] if issue else f"Fix issue #{issue_id}"

    closure_footer = f"\n\nCloses #{issue_id}"
    full_body = (body.strip() + closure_footer) if body else f"Implementation for issue #{issue_id}.{closure_footer}"

    print(f"Opening Pull Request for branch '{current_branch}' linking 'Closes #{issue_id}'...")
    cmd = ["gh", "pr", "create", "--title", title, "--body", full_body, "--head", current_branch]

    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to open PR: {err}", file=sys.stderr)
        return False

    print(f"✅ Pull Request created successfully:\n{out}")

    if agent or family:
        # `gh pr create` prints the URL, which gh accepts anywhere a PR number
        # would do. Falling back to the branch keeps this working if the output
        # format ever changes.
        pr_ref = out.strip().splitlines()[-1].strip() if out.strip() else current_branch
        apply_identity(pr_ref, agent, family)

    return True


def main():
    parser = argparse.ArgumentParser(description="Create Pull Request linking an issue.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--title", type=str, default="", help="Pull Request Title")
    parser.add_argument("--body", type=str, default="", help="Pull Request Description Body")
    parser.add_argument("--agent", type=str, default="",
                        help="Authoring agent id; stamped as author:<id> for review eligibility")
    parser.add_argument("--model-family", type=str, default="", dest="family",
                        help=f"Authoring model family, one of: {', '.join(MODEL_FAMILIES)}")
    args = parser.parse_args()

    if args.family and args.family.lower() not in MODEL_FAMILIES:
        print(f"[ERROR] Unknown model family '{args.family}'. Valid values: "
              f"{', '.join(MODEL_FAMILIES)}", file=sys.stderr)
        sys.exit(1)

    ok = create_pr(args.issue, args.title, args.body, args.agent, args.family.lower())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
