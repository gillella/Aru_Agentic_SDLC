#!/usr/bin/env python3
"""PreToolUse hook: enforces the write budget an agent declared on its issue.

Why this exists as a hook rather than a rule in AGENTS.md: prompt-level rules
are followed probabilistically. The parallel-agent design depends on every
agent staying inside its issue's ``touches:`` declaration, because that is the
only thing fetch_next_issue.py used to decide two issues were safe to run
concurrently. One agent that writes outside its budget silently corrupts
another agent's work, and the damage surfaces at merge time.

Contract with Claude Code:
  * stdin  - JSON with tool_name, tool_input, cwd
  * exit 0 - allow the call
  * exit 2 - block the call; stderr is shown to the model

Fail-open by design. This hook runs on every tool call, so a GitHub outage, an
unparseable issue body, or work in an ungoverned repo must never halt the
session. It blocks only when it can positively prove a violation:

  * the branch names an issue, AND
  * that issue declares touches, AND
  * the target path is outside the declaration.

Anything less and the call is allowed with a note on stderr.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
import time

# Tools whose input names a file we can check.
PATH_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

PROTECTED_BRANCHES = {"main", "master"}

# Re-reading the issue from GitHub on every keystroke-level tool call would add
# a network round trip to each edit. The declaration changes rarely, so a short
# TTL cache keyed by issue number is enough.
CACHE_TTL_S = 900

EXIT_ALLOW = 0
EXIT_BLOCK = 2


def _run(cmd, cwd=None, timeout=15):
    """Runs a command, returning (rc, stdout). Never raises."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # Includes the timeout case. Caller treats any failure as "unknown",
        # which fails open.
        return 1, ""


def repo_root(cwd):
    rc, out = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return out if rc == 0 and out else None


def current_branch(cwd):
    rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return out if rc == 0 else ""


def issue_from_branch(branch):
    """Extracts the issue number from a governed branch name.

    Governed names look like feat/issue-42-short-slug. A branch that does not
    carry an issue number is treated as ungoverned work, not as a violation -
    the Issue-First Law is enforced by review, not by this hook.
    """
    match = re.search(r"issue-(\d+)", branch or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def _cache_path(root, issue):
    # Lives in .git/ so it is never committed and is shared across worktrees.
    return os.path.join(root, ".git", f"aru-touches-{issue}.json")


def _read_cache(root, issue):
    try:
        with open(_cache_path(root, issue)) as fh:
            blob = json.load(fh)
        if time.time() - blob.get("fetched_at", 0) < CACHE_TTL_S:
            return blob.get("touches")
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_cache(root, issue, touches):
    try:
        with open(_cache_path(root, issue), "w") as fh:
            json.dump({"fetched_at": time.time(), "touches": touches}, fh)
    except OSError:
        # A read-only .git is unusual but must not break the hook.
        pass


def parse_touches(body):
    """Reads the 'touches: a/*, b.md' metadata line from an issue body.

    Only the first matching line counts, and Markdown emphasis around the key
    is tolerated because issue forms and hand-written bodies differ.
    """
    if not body:
        return []
    # [^\n]* rather than .*? with a trailing \s*: \s matches newlines, so a
    # lazy match on an empty declaration would run past the line ending and
    # adopt the *next* line as the declaration.
    match = re.search(
        r"^[ \t]*[*_`]{0,2}touches[*_`]{0,2}[ \t]*:[ \t]*([^\n]*)",
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return []
    raw = match.group(1).strip().strip("*_").strip()
    # A declaration of "(github settings only)" or similar prose means the
    # issue changes nothing in the tree; treat it as no declaration rather than
    # as a literal path, so the hook fails open instead of blocking everything.
    if raw.startswith("("):
        return []
    entries = [e.strip().strip("`") for e in re.split(r"[,\s]+", raw) if e.strip()]
    return [e for e in entries if not e.startswith("(")]


def touches_for(root, issue):
    cached = _read_cache(root, issue)
    if cached is not None:
        return cached
    rc, out = _run(
        ["gh", "issue", "view", str(issue), "--json", "body", "-q", ".body"], cwd=root
    )
    if rc != 0:
        return None  # Unknown - caller fails open.
    touches = parse_touches(out)
    _write_cache(root, issue, touches)
    return touches


def _norm(path, root):
    """Returns the repo-relative path, or None when the path is outside the repo.

    Paths outside the repository (a scratchpad, /tmp) are not the hook's
    business, so they resolve to None and are always allowed.
    """
    if not path:
        return None
    abs_path = os.path.abspath(os.path.join(root, os.path.expanduser(path)))
    try:
        rel = os.path.relpath(abs_path, root)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def path_allowed(rel_path, touches):
    """True when rel_path falls inside at least one declared entry.

    An entry is either a glob, a directory prefix, or an exact file. A bare
    directory name covers everything beneath it, which is how issue authors
    already write these declarations.
    """
    for entry in touches:
        entry = entry.rstrip("/").replace(os.sep, "/")
        if not entry:
            continue
        if any(ch in entry for ch in "*?["):
            if fnmatch.fnmatch(rel_path, entry):
                return True
            # "src/api/*" should also cover nested files under src/api.
            if fnmatch.fnmatch(rel_path, entry.rstrip("*").rstrip("/") + "/*"):
                return True
            continue
        if rel_path == entry or rel_path.startswith(entry + "/"):
            return True
    return False


def _git_write_to_protected(command, branch):
    """Detects commits on, or pushes to, a protected branch.

    Deliberately narrow. The git pre-push hook is the real backstop for pushes;
    this catches the common case early and gives the model a useful message
    instead of an opaque git failure.
    """
    if not command:
        return None
    # Strip quotes so `git push origin "main"` is seen the same as bare main.
    normalized = re.sub(r"[\"']", "", command)

    if re.search(r"\bgit\s+(-c\s+\S+\s+)*commit\b", normalized) and branch in PROTECTED_BRANCHES:
        return f"commit directly on '{branch}'"

    push = re.search(r"\bgit\s+(-c\s+\S+\s+)*push\b(?P<args>[^&|;]*)", normalized)
    if push:
        args = push.group("args") or ""
        targets = args.split()
        for tok in targets:
            # Handles `main`, `HEAD:main`, and `refs/heads/main`.
            ref = tok.split(":")[-1].replace("refs/heads/", "")
            if ref in PROTECTED_BRANCHES:
                return f"push to '{ref}'"
        # A bare `git push` on a protected branch pushes that branch.
        if not [t for t in targets if not t.startswith("-")] and branch in PROTECTED_BRANCHES:
            return f"push '{branch}'"
    return None


def _redirect_targets(command):
    """Best-effort extraction of shell writes: redirects, tee, sed -i.

    Intentionally incomplete - a shell can write a file in ways no regex will
    catch. This covers the honest-mistake cases; adversarial evasion is out of
    scope and is handled by review and by the pre-push hook.
    """
    if not command:
        return []
    found = []
    found += re.findall(r"(?<![0-9<>])>>?\s*([^\s;|&]+)", command)
    found += re.findall(r"\btee\s+(?:-a\s+)?([^\s;|&]+)", command)
    # sed -i rewrites its last argument. The script itself may contain spaces
    # and quotes, so pick the final token of the segment rather than trying to
    # parse sed's own grammar.
    for segment in re.split(r"[;|&]+", command):
        if re.search(r"\bsed\b[^\n]*\s-i(\.\S+)?\b", segment):
            tokens = segment.split()
            if tokens:
                found.append(tokens[-1])
    return [f.strip("'\"") for f in found if not f.startswith("-")]


def deny(reason, detail):
    print(f"BLOCKED by Aru_Agentic_SDLC: {reason}\n{detail}", file=sys.stderr)
    return EXIT_BLOCK


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return EXIT_ALLOW  # Not a shape we understand; do not interfere.

    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()

    root = repo_root(cwd)
    if not root:
        return EXIT_ALLOW  # Not a git repo.

    branch = current_branch(cwd)
    issue = issue_from_branch(branch)

    if tool == "Bash":
        command = tool_input.get("command", "")
        violation = _git_write_to_protected(command, branch)
        if violation:
            return deny(
                f"attempted to {violation}.",
                "Protected branches are merged through scripts/merge_pr.py, never "
                "pushed to directly. Open a PR from your issue branch instead.",
            )
        if issue is None:
            return EXIT_ALLOW
        touches = touches_for(root, issue)
        if not touches:
            return EXIT_ALLOW
        for target in _redirect_targets(command):
            rel = _norm(target, root)
            if rel and not path_allowed(rel, touches):
                return deny(
                    f"shell write to '{rel}' is outside issue #{issue}'s declared touches.",
                    f"Declared: {', '.join(touches)}\n"
                    "Widen the declaration on the issue, or file a follow-up issue. "
                    "Do not expand your footprint silently.",
                )
        return EXIT_ALLOW

    if tool not in PATH_TOOLS:
        return EXIT_ALLOW

    if issue is None:
        # Ungoverned branch. The Issue-First Law is a review concern, not
        # something to enforce on every keystroke - blocking here would make
        # scratch work and ungoverned repos unusable.
        return EXIT_ALLOW

    touches = touches_for(root, issue)
    if touches is None:
        print(
            f"[aru] Could not read issue #{issue} to verify touches; allowing.",
            file=sys.stderr,
        )
        return EXIT_ALLOW
    if not touches:
        print(
            f"[aru] Issue #{issue} declares no touches; nothing to enforce.",
            file=sys.stderr,
        )
        return EXIT_ALLOW

    target = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("path")
    )
    rel = _norm(target, root)
    if rel is None:
        return EXIT_ALLOW  # Outside the repo; not governed.

    if path_allowed(rel, touches):
        return EXIT_ALLOW

    return deny(
        f"write to '{rel}' is outside issue #{issue}'s declared touches.",
        f"Declared: {', '.join(touches)}\n"
        "This declaration is what let the picker run your issue in parallel with "
        "other agents. Widen it on the issue, or file a follow-up issue and release "
        "your claim. Do not expand your footprint silently.",
    )


if __name__ == "__main__":
    sys.exit(main())
