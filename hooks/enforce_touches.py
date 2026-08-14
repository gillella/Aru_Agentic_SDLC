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

  * a detected file write targets a governed repo while HEAD is protected, OR
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

# Canonical opt-in headings emitted by init_project.py and used by this
# framework's own AGENTS.md. A prose mention or explicit rejection of the law
# is not governance and must not make a globally installed hook block edits.
GOVERNANCE_MARKER = re.compile(
    r"^\s*#{1,6}\s+(?:🚨\s*)?Core Governance(?: Directive)?\s*:\s*"
    r"The Issue-First Law\s*$",
    re.IGNORECASE | re.MULTILINE,
)

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
    return bool(GOVERNANCE_MARKER.search(text))


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


def _nearest_existing_dir(path):
    """Walks up to the first directory that exists.

    A write may create its parent directories, so the target's own directory
    need not exist yet. Resolving git state from a nonexistent path fails, and
    treating that failure as "not a repository" would let every new file in a
    new package escape governance entirely.
    """
    current = os.path.dirname(os.path.abspath(path)) or os.sep
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current


def _resolve_checkout(path, session_root):
    """Git-backed half of ``owning_checkout``; None when git cannot answer."""
    anchor = _nearest_existing_dir(path)
    if not anchor:
        return None
    root = repo_root(anchor)
    if not root:
        return None

    # Sibling worktrees of one repository share a git common directory. That
    # is what separates them from an unrelated checkout sitting nearby - this
    # hook is installed globally and must not govern someone else's project.
    session_common = _git_common_dir(session_root)
    target_common = _git_common_dir(root)
    if not session_common or not target_common:
        return None
    if os.path.realpath(session_common) != os.path.realpath(target_common):
        return None

    return root, current_branch(root)


def owning_checkout(path, session_root, session_branch):
    """The (root, branch) of the checkout that actually contains ``path``.

    Governance follows the file, not the shell. Every agent works inside
    ``.worktrees/<branch>`` while its shell may sit anywhere, so deciding from
    the cwd asked the wrong repository in both directions: it refused
    legitimate edits inside an issue worktree, and - the dangerous half - it
    let writes into the ``main`` checkout and into other agents' worktrees past
    both the protected-branch guard and ``touches:``, because those paths look
    "outside the repo" when measured from a sibling worktree's root.

    Returns None when the path is not part of this repository, which the
    caller allows: a neighbouring project, or no repository at all. Sibling
    worktrees of one repository share a git common directory, and that is what
    separates them from an unrelated checkout that merely sits nearby - this
    hook is installed globally and must not govern someone else's project.

    When git cannot answer - no git on PATH, a path under no repository - fall
    back to the session's own view for anything sitting inside the session
    root. Without that fallback a resolution failure would silently ungovern
    a path the shell can plainly see is its own, turning an unknown into a
    permission. Anything genuinely elsewhere still returns None and is allowed.
    """
    if not path:
        return None

    resolved = _resolve_checkout(path, session_root)
    if resolved is not None:
        return resolved

    if _norm(path, session_root) is not None:
        return session_root, session_branch
    return None


def _cache_path(root, issue):
    common = _git_common_dir(root)
    if not common:
        return None
    return os.path.join(common, f"aru-touches-{issue}.json")


def _read_cache(root, issue, force_refresh=False):
    if force_refresh:
        return None
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


def touches_for(root, issue, force_refresh=False):
    cached = _read_cache(root, issue, force_refresh=force_refresh)
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
    business, so they resolve to None and are always allowed. Containment is
    based on filesystem identity rather than string case: macOS commonly maps
    ``Repo/File`` and ``repo/file`` to the same inode even though commonpath()
    sees unrelated strings. Walking upward also preserves a nonexistent suffix
    for a new file while resolving every existing symlink component.
    """
    if not path:
        return None
    real_root = os.path.realpath(root)
    abs_path = os.path.abspath(os.path.join(real_root, os.path.expanduser(path)))
    real_path = os.path.realpath(abs_path)
    probe = real_path
    suffix = []
    while True:
        try:
            if os.path.samefile(probe, real_root):
                return "/".join(reversed(suffix)) or "."
        except (OSError, ValueError):
            # Nonexistent new files and unreadable ancestors are expected.
            # Keep walking; if identity can never be proven, fail open.
            pass
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        suffix.append(os.path.basename(probe))
        probe = parent


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


_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_WRAPPERS = frozenset({"env", "nohup", "time", "sudo", "exec", "builtin", "command"})

_SUDO_VAL_OPTS = frozenset({
    "-u", "--user",
    "-g", "--group",
    "-C", "--close-from",
    "-p", "--prompt",
    "-D", "--chdir",
    "-h", "--host",
    "-R", "--chroot",
    "-T", "--command-timeout",
    "-U", "--other-user",
})

_ENV_VAL_OPTS = frozenset({
    "-u", "--unset",
    "-C", "--chdir",
    "-S", "--split-string",
})

_TIME_VAL_OPTS = frozenset({
    "-f", "--format",
    "-o", "--output",
})


def _is_wrapper(token):
    if not token:
        return False
    base = os.path.basename(token)
    return base in _WRAPPERS


def _is_git_exe(token):
    if not token:
        return False
    base = os.path.basename(token.rstrip("/"))
    return base == "git"


def _unwrap_simple_command(words):
    """Strips leading environment variable assignments and command wrappers (env, sudo, etc.).

    Returns (executable, args_list) or (None, []) if empty.
    """
    if not words:
        return None, []

    words = list(words)
    i = 0
    while i < len(words):
        token = words[i]
        if _ENV_ASSIGNMENT.match(token):
            i += 1
            continue

        if _is_wrapper(token):
            wrapper_name = os.path.basename(token)
            i += 1
            while i < len(words):
                w_tok = words[i]
                if w_tok == "--":
                    i += 1
                    break
                if _ENV_ASSIGNMENT.match(w_tok):
                    i += 1
                    continue
                if not w_tok.startswith("-"):
                    break

                name, _, inline = w_tok.partition("=")
                if wrapper_name == "env":
                    if name in ("-S", "--split-string"):
                        if inline:
                            s_arg = inline
                            i += 1
                        elif i + 1 < len(words):
                            s_arg = words[i + 1]
                            i += 2
                        else:
                            i += 1
                            s_arg = ""
                        inner_tokens = _shell_tokens(s_arg)
                        inner_words = [t[1] for t in inner_tokens if t[0] == "word"]
                        if inner_words:
                            words = words[:i] + inner_words + words[i:]
                        continue
                    elif inline:
                        i += 1
                    elif name in _ENV_VAL_OPTS:
                        i += 2 if i + 1 < len(words) else 1
                    else:
                        i += 1
                elif wrapper_name == "sudo":
                    if inline:
                        i += 1
                    elif name in _SUDO_VAL_OPTS:
                        i += 2 if i + 1 < len(words) else 1
                    elif len(name) > 2 and name.startswith("-") and not name.startswith("--"):
                        i += 1
                    else:
                        i += 1
                elif wrapper_name == "time":
                    if inline:
                        i += 1
                    elif name in _TIME_VAL_OPTS:
                        i += 2 if i + 1 < len(words) else 1
                    else:
                        i += 1
                else:
                    if inline:
                        i += 1
                    else:
                        i += 1
            continue
        break

    if i >= len(words):
        return None, []
    return words[i], words[i + 1:]


def _git_write_to_protected(command, branch):
    """Detects commits on, or pushes to, a protected branch.

    Deliberately narrow. The git pre-push hook is the real backstop for pushes;
    this catches the common case early and gives the model a useful message
    instead of an opaque git failure.

    Judges `branch` as given. Callers must pass the branch of the checkout the
    command actually acts on - see `_git_write_violation`, which resolves it.
    """
    if not command:
        return None

    if isinstance(command, (list, tuple)):
        simple_cmds = [[text for text in command if isinstance(text, str)]]
    else:
        cmd_sans_heredoc = _strip_heredocs(command)
        tokens = _shell_tokens(cmd_sans_heredoc)
        if not tokens:
            return None
        simple_cmds = []
        current = []
        for kind, text in tokens:
            if kind == "control":
                if current:
                    simple_cmds.append(current)
                    current = []
            elif kind == "word":
                current.append(text)
        if current:
            simple_cmds.append(current)

    for words in simple_cmds:
        exe, args = _unwrap_simple_command(words)
        if not _is_git_exe(exe):
            continue

        index = 0
        subcommand = None
        while index < len(args):
            token = args[index]
            if not token.startswith("-"):
                subcommand = token
                index += 1
                break
            name, _, inline = token.partition("=")
            if inline:
                index += 1
            elif name in _GIT_VALUE_OPTS:
                index += 2
            else:
                index += 1

        if not subcommand or subcommand not in _GIT_WRITE_SUBCOMMANDS:
            continue

        if subcommand == "commit" and branch in PROTECTED_BRANCHES:
            return f"commit directly on '{branch}'"

        if subcommand == "push":
            push_opts_with_val = {"-o", "--push-option", "-r", "--repo", "--receive-pack", "--exec"}
            pos_args = []
            pushes_all_refs = False
            i = index
            while i < len(args):
                tok = args[i]
                if tok.startswith("-"):
                    name, _, inline = tok.partition("=")
                    if name in {"--all", "--mirror"}:
                        pushes_all_refs = True
                    if not inline and name in push_opts_with_val:
                        i += 2
                    else:
                        i += 1
                else:
                    pos_args.append(tok)
                    i += 1

            if pushes_all_refs:
                return "push may update protected branches"

            refspecs = pos_args[1:] if len(pos_args) > 1 else []
            if refspecs:
                for refspec in refspecs:
                    normalized_refspec = refspec.removeprefix("+")
                    if normalized_refspec.startswith("^") or any(
                        marker in normalized_refspec for marker in ("*", "?", "[")
                    ):
                        return "push refspec cannot be proven safe"
                    dest = normalized_refspec.split(":")[-1].replace("refs/heads/", "")
                    src = normalized_refspec.split(":")[0].replace("refs/heads/", "")
                    if dest in PROTECTED_BRANCHES:
                        return f"push to '{dest}'"
                    if (src == "HEAD" or dest == "HEAD") and branch in PROTECTED_BRANCHES:
                        return f"push '{branch}'"
            else:
                # With no explicit refspec, remote and branch configuration can
                # select refs other than the current branch. This static guard
                # cannot prove those refs exclude main/master, so fail closed.
                return "push has no explicit safe refspec"

    return None


# Subcommands that write to a branch. Anything else git does is a read as far
# as this guard is concerned.
_GIT_WRITE_SUBCOMMANDS = frozenset({"commit", "push"})
# Options git accepts *before* the subcommand that move where it acts. The
# previous pattern allowed only lowercase `-c <config>`, so `git -C <path>
# commit` did not read as a commit at all and skipped the guard entirely -
# the check was narrower than what git accepts.
_GIT_DIR_OPTS = frozenset({"-C", "--git-dir", "--work-tree"})
_GIT_VALUE_OPTS = _GIT_DIR_OPTS | {"-c", "--namespace", "--exec-path"}


def _resolve_dir(raw, base):
    """A `cd`/`-C` operand as an absolute path, or None if unknowable."""
    resolved = _resolve_target(raw)
    if resolved is None:
        return None
    return os.path.normpath(os.path.join(base, resolved))


def _git_write_violation(command, cwd):
    """Whether `command` writes to a protected branch, in whatever checkout it
    actually acts on.

    #70 moved path governance onto the checkout that owns the file, but a git
    subcommand has no path operand, so the protected-branch guard kept reading
    the shell's branch. That failed in both directions: `cd <worktree> && git
    commit` from a shell on `main` was refused, while `git -C <main> commit`
    from a worktree was allowed - the guard never consulted, which is the gap
    #29 closed reopened through a different door.

    Resolution order mirrors the shell: a `cd` earlier in the command moves the
    base directory, and `-C`/`--work-tree`/`--git-dir` on the invocation itself
    override it. A `cd` *after* the git command cannot retarget it, which is
    why word order is walked rather than the command being scanned as a bag of
    tokens.

    Fails **closed**. Shell parsing is not winnable by enumeration - `cd`,
    `pushd`, subshells and variables all retarget a write - so when a git write
    is present and its checkout cannot be determined, this refuses. A refusal
    costs one explicit command; a false permission costs an ungoverned commit
    on a protected branch.
    """
    tokens = _shell_tokens(command)
    if not tokens:
        return None  # Unlexable; matches this module's fail-open contract.

    simple_cmds = []
    current = []
    for kind, text in tokens:
        if kind == "control":
            if current:
                simple_cmds.append(current)
                current = []
        elif kind == "word":
            current.append(text)
    if current:
        simple_cmds.append(current)

    base = cwd
    base_unknown = False
    for words in simple_cmds:
        exe, args = _unwrap_simple_command(words)
        if exe in ("cd", "pushd"):
            operand = args[0] if args else None
            if operand is None or operand.startswith("-"):
                base_unknown = True
            else:
                moved = _resolve_dir(operand, base)
                if moved is None:
                    base_unknown = True
                else:
                    base, base_unknown = moved, False
            continue

        if not _is_git_exe(exe):
            continue

        target, unknown = base, base_unknown
        index = 0
        subcommand = None
        while index < len(args):
            token = args[index]
            if not token.startswith("-"):
                subcommand = token
                index += 1
                break
            name, _, inline = token.partition("=")
            if inline:
                value = inline
                index += 1
            elif name in _GIT_VALUE_OPTS:
                value = args[index + 1] if index + 1 < len(args) else None
                index += 2
            else:
                index += 1
                continue
            if name in _GIT_DIR_OPTS and value is not None:
                # --git-dir names the .git directory; the checkout is its parent.
                candidate = value[:-len("/.git")] if name == "--git-dir" and value.endswith("/.git") else value
                moved = _resolve_dir(candidate, base)
                target, unknown = (base, True) if moved is None else (moved, False)

        if subcommand not in _GIT_WRITE_SUBCOMMANDS:
            continue

        if unknown:
            return (
                f"run 'git {subcommand}' in a directory this hook cannot "
                "resolve, so it cannot prove the target branch is unprotected"
            )
        violation = _git_write_to_protected(["git"] + list(args), current_branch(target))
        if violation:
            return violation

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
# A whole leading segment that is exactly one variable, braced or not.
_LEADING_VAR = re.compile(r"\A\$\{?(\w+)\}?\Z")


def _resolve_target(target):
    """A redirect target's real destination, or None when it is unknowable.

    Only the **leading** segment decides which root a path belongs to, so it is
    the only place an expansion changes the answer. `dir/$name.txt` is
    repository-relative whatever `$name` holds and stays governed; `$TMP/out.txt`
    could be anywhere.

    Previously a leading expansion was read as a repository-relative path, so
    `echo x > $TMP/out.txt` was reported as a write to `<repo>/$TMP/out.txt` and
    refused against `touches:`. The refusal blamed a declaration for a path the
    hook could not locate, and widening that declaration to cover `$TMP` would
    have been meaningless.

    The hook runs inside the agent's process tree, so resolving from the
    environment is not guesswork: `$HOME` and `$ARU_SDLC_HOME` keep their
    current coverage and are judged against their real destination. Only a
    variable this process genuinely cannot see - one assigned inside the
    command, or a command substitution - is unknowable, and that falls back to
    the module's existing rule of allowing what it cannot prove.
    """
    if not target:
        return target
    head, slash, rest = target.partition("/")
    if not _HAS_EXPANSION.search(head):
        return target  # Ordinary relative or absolute path; unchanged.

    match = _LEADING_VAR.match(head)
    if not match:
        # Command substitution, arithmetic, or a partially expanded segment.
        # Out of scope by design; unknowable is the honest answer.
        return None
    value = os.environ.get(match.group(1))
    if not value:
        return None
    return os.path.join(value, rest) if slash else value


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
# Control operators that delimit simple commands in shell grammar.
_CONTROL_OPS = ("&&", "||", ";", "|", "&", "\n")


def _shell_tokens(command):
    """Splits a command into ('word' | 'op' | 'control', text) pairs, or None if malformed.

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

        if char.isspace() and char != "\n":
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
            flush()
            tokens.append(("control", "\n"))
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

        ctrl = next((op for op in _CONTROL_OPS if text.startswith(op, index)), None)
        if ctrl:
            flush()
            quoted = False
            tokens.append(("control", ctrl))
            index += len(ctrl)
            continue

        # Any other shell metacharacter ends the current word. Their meaning
        # does not matter here; only that they are not part of a filename.
        if char in "<()":
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


def deny_protected_write(rel, branch):
    return deny(
        f"write to '{rel}' on protected branch '{branch}'.",
        "Claim an issue, then create an isolated issue branch with "
        "scripts/create_branch.py --worktree before editing.",
    )


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
        # Judged by the checkout the command acts on, not the shell's. A `cd`
        # or `-C` moves the write somewhere the session's branch says nothing
        # about, in both the permissive and the restrictive direction.
        violation = _git_write_violation(command, cwd)
        if violation:
            return deny(
                f"attempted to {violation}.",
                "Protected branches are merged through scripts/merge_pr.py, never "
                "pushed to directly. Open a PR from your issue branch instead.\n"
                "If the target directory is correct, name it literally "
                "(git -C <path>) so the checkout can be resolved.",
            )
        targets = _redirect_targets(command)
        # A shell redirect can name a path in any checkout, so each target is
        # judged by the one that owns it - the same rule the write path uses.
        # Both checks have to key off the owner, not the shell: running them
        # against the session's root and issue let `echo x > <other-wt>/f.py`
        # slip through, because a sibling worktree is outside the session root
        # and `_norm` returned None. The equivalent Write was refused, so the
        # redirect became a way around the write path.
        touches_by_owner = {}
        for raw_target in targets:
            # A leading expansion means the destination is not knowable, and a
            # path the hook cannot locate must never be reported as a
            # `touches:` violation - the declaration is not what is wrong.
            target = _resolve_target(raw_target)
            if target is None:
                continue
            owned = owning_checkout(target, root, branch)
            if owned is None:
                continue
            owner_root, owner_branch = owned
            rel = _norm(target, owner_root)
            if rel is None:
                continue

            if owner_branch in PROTECTED_BRANCHES:
                if governed_repo(owner_root):
                    return deny_protected_write(rel, owner_branch)
                continue

            owner_issue = issue_from_branch(owner_branch)
            if owner_issue is None:
                continue  # Scratch worktree; usable without an issue.

            # One lookup per owning checkout, not per redirect target: a
            # command with several targets in one worktree must not pay a
            # GitHub round trip each.
            if owner_issue not in touches_by_owner:
                touches_by_owner[owner_issue] = touches_for(owner_root, owner_issue)
            touches = touches_by_owner[owner_issue]
            if not touches:
                continue

            if not path_allowed(rel, touches):
                # Re-check GitHub to verify if touches: declaration was widened
                fresh_touches = touches_for(owner_root, owner_issue, force_refresh=True)
                if fresh_touches is None:
                    print(
                        f"[aru] Could not re-read issue #{owner_issue} to verify widened touches; allowing.",
                        file=sys.stderr,
                    )
                    continue
                if not fresh_touches or path_allowed(rel, fresh_touches):
                    touches_by_owner[owner_issue] = fresh_touches
                    continue

                return deny(
                    f"shell write to '{rel}' is outside issue "
                    f"#{owner_issue}'s declared touches.",
                    f"Declared: {', '.join(fresh_touches)}\n"
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
    # The checkout that owns the file decides, not the one the shell is in.
    owned = owning_checkout(target, root, branch)
    if owned is None:
        return EXIT_ALLOW  # Not part of this repository; not governed.
    owner_root, branch = owned
    issue = issue_from_branch(branch)

    rel = _norm(target, owner_root)
    if rel is None:
        return EXIT_ALLOW  # Outside the repo; not governed.

    if branch in PROTECTED_BRANCHES:
        governed = governed_repo(owner_root)
        if governed:
            return deny_protected_write(rel, branch)
        # False is an ordinary ungoverned repository. None is a detection
        # failure. Both deliberately fail open for a globally installed hook.
        return EXIT_ALLOW

    if issue is None:
        # A non-protected scratch branch remains usable without an issue.
        return EXIT_ALLOW

    touches = touches_for(owner_root, issue)
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

    # The cached declaration refused the path. Re-query GitHub to check if touches: was widened.
    fresh_touches = touches_for(owner_root, issue, force_refresh=True)
    if fresh_touches is None:
        print(
            f"[aru] Could not re-read issue #{issue} to verify widened touches; allowing.",
            file=sys.stderr,
        )
        return EXIT_ALLOW
    if not fresh_touches:
        print(
            f"[aru] Issue #{issue} declares no touches; nothing to enforce.",
            file=sys.stderr,
        )
        return EXIT_ALLOW

    if path_allowed(rel, fresh_touches):
        return EXIT_ALLOW

    return deny(
        f"write to '{rel}' is outside issue #{issue}'s declared touches.",
        f"Declared: {', '.join(fresh_touches)}\n"
        "This declaration is what let the picker run your issue in parallel with "
        "other agents. Widen it on the issue, or file a follow-up issue and release "
        "your claim. Do not expand your footprint silently.",
    )


if __name__ == "__main__":
    sys.exit(main())
