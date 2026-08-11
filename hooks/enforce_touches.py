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

  * a file tool targets a governed repo while HEAD is protected, OR
  * the branch names an issue, that issue declares touches, and the target
    path is outside the declaration.

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


def governed_repo(root):
    """True when the repository opted into the Aru Issue-First Law.

    ``False`` means the marker is positively absent. ``None`` means detection
    failed, which the caller treats as unknown and therefore allows. This hook
    is installed globally, so an ordinary repository with its own AGENTS.md
    must never be mistaken for an Aru-governed one.
    """
    marker = os.path.join(root, "AGENTS.md")
    if not os.path.isfile(marker):
        return False
    try:
        with open(marker, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeError):
        return None
    return bool(re.search(r"\bIssue-First Law\b", text, re.IGNORECASE))


def _git_common_dir(root):
    """The shared .git directory, resolved from inside a worktree.

    In a worktree `<root>/.git` is a *file* pointing elsewhere, so joining a
    cache path onto it raises ENOTDIR. Since the framework mandates that all
    development happens in worktrees, the naive path meant the cache never
    worked anywhere real: every Edit and Bash call re-queried GitHub, paying up
    to the full timeout per tool call during an outage.
    """
    rc, out = _run(["git", "rev-parse", "--git-common-dir"], cwd=root)
    if rc != 0 or not out:
        return None
    return out if os.path.isabs(out) else os.path.join(root, out)


def _cache_path(root, issue):
    common = _git_common_dir(root)
    if not common:
        return None
    return os.path.join(common, f"aru-touches-{issue}.json")


def _read_cache(root, issue):
    path = _cache_path(root, issue)
    if not path:
        return None
    try:
        with open(path) as fh:
            blob = json.load(fh)
        if time.time() - blob.get("fetched_at", 0) < CACHE_TTL_S:
            return blob.get("touches")
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_cache(root, issue, touches):
    path = _cache_path(root, issue)
    if not path:
        return
    try:
        with open(path, "w") as fh:
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


# `>&` is ambiguous and cannot be classified without looking at its target:
# `2>&1` and `>&-` rebind or close a descriptor and write nothing, but
# `>&out.txt` creates or truncates a file exactly like `>`. Resolved in
# _redirect_targets by inspecting what follows; see _IS_DESCRIPTOR.
_AMBIGUOUS_DUP_OPS = frozenset({">&", ">>&"})
# A descriptor target is a bare number (`2`), `-` (close), or a descriptor
# move such as `3-`, which duplicates the descriptor and closes the source.
# Anything else is a filename, and treating it as a descriptor is the
# dangerous direction: a real write goes undetected.
_IS_DESCRIPTOR = re.compile(r"\A(?:\d+-?|-)\Z")
# An unresolved expansion after `>&` cannot be classified either way:
# `echo hi >&$fd` duplicates a descriptor when $fd holds a number and writes a
# file when it holds a path, and this scanner resolves quoting but never
# expansion. Reporting it as a filename blocks a governed branch without
# positive proof of a write. Scoped to `>&` deliberately - after a plain `>`
# an expansion is unambiguously a file, and `echo hi > $HOME/out.txt` must
# still be caught.
_HAS_EXPANSION = re.compile(r"[$`]")


# A heredoc and its body: `<<EOF`, `<<'EOF'`, `<<-EOF`, terminated by a line
# holding just the delimiter (or by end of input, for a truncated command).
# `rest` is whatever follows the opener on the same line and is deliberately
# kept: real redirects live there, as in `cat <<'EOF' > out.txt`. The body
# only begins at the newline.
# The delimiter is any run of characters a shell would read as one word, not
# just \w+. `END-MSG` is a valid delimiter and a natural thing to write; with
# \w+ the opener did not match, the body was never stripped, and a '>' inside
# it - the closing angle of a Co-Authored-By trailer, say - lexed as a real
# redirect whose target was the terminator. Shell metacharacters, quotes and
# whitespace are excluded because they would end the word.
_HEREDOC = re.compile(
    r"<<-?[ \t]*(?P<q>['\"]?)(?P<delim>[^\s'\"<>|&;()\\]+)(?P=q)(?P<rest>[^\n]*)\n"
    r"(?P<body>.*?)(?:^[ \t]*(?P=delim)[ \t]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)


def _strip_heredocs(command):
    """Removes heredoc bodies, which are data rather than shell syntax.

    shlex has no notion of a heredoc, so the body is lexed as if it were part
    of the command. A commit message passed this way is ordinary prose, and
    the '>' closing the address in a Co-Authored-By trailer would otherwise
    lex as a real redirect operator whose target is the heredoc terminator.

    The remainder of the opener line survives, so a genuine redirect sitting
    beside the heredoc is still seen. Dropping it would trade this module's
    false positives for a false negative, which is the worse failure.
    """
    return _HEREDOC.sub(lambda match: " " + match.group("rest") + " ", command or "")


# Output redirection operators, longest first so the scanner matches greedily
# and `&>>` is never read as `&>` followed by a stray `>`.
_REDIR_OPS = ("&>>", ">>&", "&>", ">>", ">&", ">")


def _shell_tokens(command):
    """Splits a command into ('word' | 'op', text) pairs, or None if malformed.

    Hand-written rather than delegated to shlex, because neither shlex mode
    answers the question this module actually asks - *was this operator
    quoted?* - and each fails in a different direction:

    * ``posix=True`` resolves quoting correctly but discards it, so a quoted
      ``">"`` and a real redirect both arrive as a bare '>' token. That
      blocked prose such as ``echo ">" file.txt``.
    * ``posix=False`` keeps the quotes but stops processing escapes, so
      ``-m "use \\">\\" here"`` terminates the string at the escaped quote and
      manufactures a redirect that was never there.

    Tracking quote state directly costs about thirty lines and gets both:
    quoting is resolved the way bash resolves it, and an operator is reported
    only when it is genuinely unquoted and unescaped. Words come back already
    unquoted, so callers compare values rather than spellings.

    Returns None on unbalanced quotes or a trailing escape. That is
    deliberately fail-open, matching this module's contract: block only on
    positive proof of a write. The pre-push hook and review remain the
    backstop for anything this misses.
    """
    if not command:
        return None
    text = _strip_heredocs(command)

    tokens = []
    word = []
    quoted = False  # this word contained quotes, so it is never an operator
    index = 0
    length = len(text)

    def flush():
        if word or quoted:
            tokens.append(("word", "".join(word)))
        del word[:]

    while index < length:
        char = text[index]

        if char == "\\":
            # An escape makes the next character literal wherever it appears
            # outside single quotes, which is exactly what stops `\>` from
            # redirecting. A trailing backslash means the command is truncated.
            if index + 1 >= length:
                return None
            word.append(text[index + 1])
            index += 2
            continue

        if char == "'":
            end = text.find("'", index + 1)
            if end == -1:
                return None
            word.append(text[index + 1:end])
            quoted = True
            index = end + 1
            continue

        if char == '"':
            index += 1
            while index < length and text[index] != '"':
                if text[index] == "\\" and index + 1 < length:
                    # Inside double quotes bash only treats a backslash as an
                    # escape before these; elsewhere it stays literal.
                    if text[index + 1] in '"\\$`':
                        word.append(text[index + 1])
                        index += 2
                        continue
                word.append(text[index])
                index += 1
            if index >= length:
                return None
            quoted = True
            index += 1
            continue

        if char.isspace():
            flush()
            quoted = False
            index += 1
            continue

        # `#` opens a comment only at the start of a word - bash reads
        # `echo hi#not-comment` as a single word, `#` and all. Once a comment
        # opens, the rest of the line is not executable, so continuing to lex
        # it manufactured redirects out of prose: `echo hi # > out.txt` was
        # reported as writing out.txt. A following line still lexes normally,
        # which matters for the multi-line commands agents actually send.
        if char == "#" and not word and not quoted:
            newline = text.find("\n", index)
            if newline == -1:
                break
            index = newline + 1
            continue

        # `>(cmd)` is process substitution: bash hands the command a pipe path
        # such as /dev/fd/63 and creates no file named `cmd`. This has to be
        # matched before the redirect operators below, which would otherwise
        # read the `>` as a write and report the first word inside the parens
        # as its target. The body is still scanned, so a genuine redirect
        # nested inside it - `echo >(cat > inside.txt)` - is not lost.
        if text.startswith(">(", index):
            flush()
            quoted = False
            index += 2
            continue

        operator = next((op for op in _REDIR_OPS if text.startswith(op, index)), None)
        if operator:
            flush()
            quoted = False
            tokens.append(("op", operator))
            index += len(operator)
            continue

        # Any other shell metacharacter ends the current word. Their meaning
        # does not matter here; only that they are not part of a filename.
        if char in "<|;&()":
            flush()
            quoted = False
            index += 1
            continue

        word.append(char)
        index += 1

    flush()
    return tokens


def _redirect_targets(command):
    """Best-effort extraction of shell writes: redirects, tee, sed -i.

    Intentionally incomplete - a shell can write a file in ways no lexer will
    catch. This covers the honest-mistake cases; adversarial evasion is out of
    scope and is handled by review and by the pre-push hook.
    """
    tokens = _shell_tokens(command)
    if not tokens:
        return []

    found = []
    for index, (kind, text) in enumerate(tokens):
        # Only an 'op' token is a real operator. Quoted and escaped angle
        # brackets are folded into words by the scanner, so `echo ">" file.txt`
        # and `-m "> fix parser"` never reach here.
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        target = following[1] if following and following[0] == "word" else None

        if kind == "op":
            if text in _AMBIGUOUS_DUP_OPS:
                # `>&` writes a file unless its target is a descriptor.
                # Classifying it as duplication unconditionally hid real writes
                # such as `echo hi >&out.txt`, the dangerous failure direction.
                if (
                    target is not None
                    and not _IS_DESCRIPTOR.match(target)
                    and not _HAS_EXPANSION.search(target)
                ):
                    found.append(target)
            elif target is not None:
                found.append(target)
            continue

        if text == "tee":
            # Skip tee's own flags to reach the first path argument.
            for kind_after, candidate in tokens[index + 1:]:
                if kind_after != "word" or candidate.startswith("-"):
                    continue
                found.append(candidate)
                break

    # sed -i rewrites its last argument. The script itself may contain spaces
    # and quotes, so pick the final token of the segment rather than trying to
    # parse sed's own grammar.
    for segment in re.split(r"[;|&]+", command or ""):
        if re.search(r"\bsed\b[^\n]*\s-i(\.\S+)?\b", segment):
            segment_tokens = segment.split()
            if segment_tokens:
                found.append(segment_tokens[-1].strip("'\""))

    # The scanner already resolved quoting, so these are values, not spellings.
    return [value for value in found if value and not value.startswith("-")]


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

    target = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("path")
    )
    rel = _norm(target, root)
    if rel is None:
        return EXIT_ALLOW  # Outside the repo; not governed.

    if branch in PROTECTED_BRANCHES:
        governed = governed_repo(root)
        if governed:
            return deny(
                f"write to '{rel}' on protected branch '{branch}'.",
                "Claim an issue, then create an isolated issue branch with "
                "scripts/create_branch.py --worktree before editing.",
            )
        # False is an ordinary ungoverned repository. None is a detection
        # failure. Both deliberately fail open for a globally installed hook.
        return EXIT_ALLOW

    if issue is None:
        # A non-protected scratch branch remains usable without an issue.
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
