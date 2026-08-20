#!/usr/bin/env python3
# line-ceiling: 554
"""Fail when documentation contradicts the code it describes.

Three mechanical checks over ``docs/**/*.md``:

1. **Referenced paths exist.** A repo path named in an inline code span
   resolves, and a ``path:LINE`` citation points inside the file.
2. **Referenced issue states match.** ``#123 (closed)`` is really closed.
3. **Quoted excerpts match source.** A fenced block marked
   ``<!-- doc-check: excerpt scripts/x.py L10-L20 -->`` matches those lines.

Every check is deliberately narrower than what documentation contains. A
check that guesses produces false positives, and a noisy gate gets disabled -
which is worse than no gate, because the disabled gate still looks like one.
The narrowing rules are recorded at each check; loosen one only with a
document that the looser rule reads correctly.

Exit 0 when documentation and code agree, 1 when they contradict, 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
FENCE_OPEN_RE = re.compile(r"^\s*(?:```|~~~)\s*(?P<info>.*)$")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# A trailing `:104` or `:148-195` is a line citation, not part of the path.
LINE_CITATION_RE = re.compile(r":(\d+)(?:-(\d+))?$")

# Anything a documentation author uses to mean "substitute something here",
# plus shell globs. A token carrying one of these is a template, not a path.
PLACEHOLDER_CHARS = frozenset("<>*?{}|\\\"'$ \t")

# `.git` contents differ between a normal clone and a linked worktree (there
# `.git` is a file), so a reference into it is not verifiable from the tree -
# and `.git/config` carries the checkout's authentication material, which
# this checker must never quote into a job log.
UNVERIFIABLE_TOP_LEVEL = frozenset({".git"})

# `gh` talks to the network. Without a bound, a stall hangs the job until the
# workflow timeout instead of producing a finding the fail-closed design can
# report.
GH_TIMEOUT_SECONDS = 30

# Paths are commonly written relative to the framework home rather than the
# repo root; both spellings mean the same file.
PATH_PREFIXES = ("$ARU_SDLC_HOME/", "./")

ISSUE_STATE_RE = re.compile(r"#(\d+)\s*\(\s*(open|closed|merged)\b", re.IGNORECASE)
ISSUE_ANNOTATION_RE = re.compile(
    r"<!--\s*doc-check:\s*issue\s+#?(\d+)\s+(open|closed|merged)\s*-->",
    re.IGNORECASE,
)
EXCERPT_ANNOTATION_RE = re.compile(
    r"<!--\s*doc-check:\s*excerpt\s+(?P<path>[^\s>]+)"
    r"(?:\s+L(?P<start>\d+)(?:\s*-\s*L?(?P<end>\d+))?)?\s*-->",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """One place where a document and the repository disagree."""

    file: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.file}:{self.line}: {self.message}"


@dataclass
class Fence:
    """A fenced code block: where it starts, and what it contains."""

    open_line: int  # 1-indexed line of the opening fence
    info: str
    body: List[str]


def resolve_within_root(root: Path, relative: str) -> Optional[Path]:
    """Resolve ``relative`` under ``root``; ``None`` if it leaves the tree.

    On a ``pull_request`` run the documentation being checked is
    attacker-controlled, and this checker prints source lines into the job
    log. Three ways out of the tree are refused here: an absolute operand
    (joining one discards ``root`` entirely), a ``..`` climb, and a symlink
    that points outward - which is why the comparison is made after
    ``resolve()``. Anything under ``.git`` is refused for the same reason,
    its config holding the checkout credential.
    """
    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    resolved_root = root.resolve()
    target = (resolved_root / candidate).resolve()
    try:
        inside = target.relative_to(resolved_root)
    except ValueError:
        return None
    if inside.parts and inside.parts[0] in UNVERIFIABLE_TOP_LEVEL:
        return None
    return target


def iter_markdown(root: Path, subdir: str = "docs") -> List[Path]:
    base = root / subdir
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())


def split_fences(lines: Sequence[str]) -> Tuple[List[Tuple[int, str]], List[Fence]]:
    """Separate prose lines from fenced code blocks.

    Fence tracking is a simple toggle. Markdown allows nesting a longer fence
    around a shorter one; docs here do not use it, and guessing at nesting
    would make the parse depend on a rule authors are not following anyway.
    """
    prose: List[Tuple[int, str]] = []
    fences: List[Fence] = []
    current: Optional[Fence] = None
    for lineno, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            if current is None:
                match = FENCE_OPEN_RE.match(line)
                info = match.group("info").strip() if match else ""
                current = Fence(open_line=lineno, info=info, body=[])
            else:
                fences.append(current)
                current = None
            continue
        if current is None:
            prose.append((lineno, line))
        else:
            current.body.append(line)
    if current is not None:
        # An unterminated fence still carries whatever it collected.
        fences.append(current)
    return prose, fences


# --------------------------------------------------------------------------
# Check 1 - referenced paths exist
# --------------------------------------------------------------------------


def normalise_path_token(token: str, top_level: Iterable[str]) -> Optional[
    Tuple[str, Optional[int], Optional[int]]
]:
    """Return ``(path, start_line, end_line)`` when a token is a repo path.

    ``None`` means "not something this check can verify". The qualifying
    rules, and why each one is here:

    * no placeholder or glob characters - those are templates, not paths;
    * contains a ``/`` - a bare word is far more often prose than a path;
    * first segment is an existing top-level entry - otherwise `path/to/file`
      style illustrations qualify;
    * last segment has an extension, or the token ends in ``/`` - without
      this, a branch name like ``docs/30-current-state-gap-analysis`` reads
      as a missing file. That is a real string in these docs, and it is
      correct.
    """
    token = token.strip()
    for prefix in PATH_PREFIXES:
        if token.startswith(prefix):
            token = token[len(prefix):]
    if not token or PLACEHOLDER_CHARS & set(token):
        return None

    start = end = None
    citation = LINE_CITATION_RE.search(token)
    if citation:
        token = token[: citation.start()]
        start = int(citation.group(1))
        end = int(citation.group(2)) if citation.group(2) else None

    if "/" not in token.rstrip("/"):
        return None
    head = token.split("/", 1)[0]
    if head not in top_level or head in UNVERIFIABLE_TOP_LEVEL:
        return None

    is_dir_ref = token.endswith("/")
    last = token.rstrip("/").rsplit("/", 1)[-1]
    if not is_dir_ref and "." not in last:
        return None
    return token, start, end


def check_paths(root: Path, doc: Path, prose: Sequence[Tuple[int, str]]) -> List[Finding]:
    """Verify repo paths named in inline code spans outside fenced blocks.

    Fenced blocks are excluded on purpose: they hold commands and templates,
    where an unresolvable path is usually an example rather than a claim.
    """
    top_level = {entry.name for entry in root.iterdir()}
    rel = doc.relative_to(root).as_posix()
    findings: List[Finding] = []
    for lineno, line in prose:
        for span in INLINE_CODE_RE.findall(line):
            parsed = normalise_path_token(span, top_level)
            if parsed is None:
                continue
            path, start, end = parsed
            target = resolve_within_root(root, path.rstrip("/"))
            if target is None:
                findings.append(
                    Finding(rel, lineno, f"references `{path}`, which is outside the repository")
                )
                continue
            if path.endswith("/"):
                if not target.is_dir():
                    findings.append(
                        Finding(rel, lineno, f"references directory `{path}`, which does not exist")
                    )
                continue
            if not target.is_file():
                findings.append(
                    Finding(rel, lineno, f"references `{path}`, which does not exist")
                )
                continue
            if start is None:
                continue
            total = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            cited = f"{path}:{start}" + (f"-{end}" if end else "")
            # A reversed or zero-based citation names no source range at all;
            # comparing only the upper bound would let `:900-1` through.
            if start < 1 or (end is not None and end < start):
                findings.append(
                    Finding(rel, lineno, f"cites `{cited}`, which is not a valid line range")
                )
            elif max(start, end or start) > total:
                findings.append(
                    Finding(rel, lineno, f"cites `{cited}`, but that file has {total} lines")
                )
    return findings


# --------------------------------------------------------------------------
# Check 2 - referenced issue states match
# --------------------------------------------------------------------------


class IssueStateResolver:
    """Resolve issue/PR state through `gh`, once per number.

    Lookup failures are findings rather than warnings. A state check that
    quietly turns itself off when the network hiccups is the decay this
    check exists to prevent; `--offline` is the explicit way to skip it.
    """

    def __init__(self, root: Path, repo: Optional[str] = None):
        self.root = root
        self._repo = repo
        self._cache: Dict[int, Tuple[Optional[str], Optional[str]]] = {}

    def _run(self, args: Sequence[str]) -> Tuple[int, str, str]:
        # A missing or stalled `gh` becomes a reportable failure rather than a
        # traceback or an indefinite hang.
        try:
            proc = subprocess.run(
                list(args),
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            return 127, "", "gh is not installed"
        except subprocess.TimeoutExpired:
            return 124, "", f"gh timed out after {GH_TIMEOUT_SECONDS}s"
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def repo(self) -> Tuple[Optional[str], Optional[str]]:
        if self._repo:
            return self._repo, None
        code, out, err = self._run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
        )
        if code != 0 or not out:
            return None, err or "gh repo view failed"
        self._repo = out
        return self._repo, None

    def state(self, number: int) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(state, error)``; state is open, closed, or merged."""
        if number in self._cache:
            return self._cache[number]
        repo, err = self.repo()
        if repo is None:
            result: Tuple[Optional[str], Optional[str]] = (None, err)
            self._cache[number] = result
            return result
        code, out, err = self._run(["gh", "api", f"repos/{repo}/issues/{number}"])
        if code != 0:
            result = (None, err.splitlines()[-1] if err else f"gh api exited {code}")
            self._cache[number] = result
            return result
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            result = (None, "gh api returned unparseable JSON")
            self._cache[number] = result
            return result
        # A merged PR is also "closed" upstream; report the stronger fact so a
        # document may claim either.
        pull = payload.get("pull_request") or {}
        state = "merged" if pull.get("merged_at") else str(payload.get("state", "")).lower()
        result = (state or None, None if state else "response carried no state")
        self._cache[number] = result
        return result


def collect_issue_claims(
    prose: Sequence[Tuple[int, str]],
) -> List[Tuple[int, int, str]]:
    """Return ``(line, issue_number, claimed_state)`` for explicit claims.

    Only two spellings count: the parenthetical ``#78 (closed)`` and the
    explicit ``<!-- doc-check: issue 78 closed -->``. Inferring state from
    prose does not work here - "#113 merged the evidence" and "#18 was
    opened through the sanctioned path" are both about something other than
    issue state, and both appear in these documents.
    """
    claims: List[Tuple[int, int, str]] = []
    for lineno, line in prose:
        for pattern in (ISSUE_STATE_RE, ISSUE_ANNOTATION_RE):
            for match in pattern.finditer(line):
                claims.append((lineno, int(match.group(1)), match.group(2).lower()))
    return claims


def check_issue_states(
    root: Path,
    doc: Path,
    prose: Sequence[Tuple[int, str]],
    resolver: IssueStateResolver,
) -> List[Finding]:
    rel = doc.relative_to(root).as_posix()
    findings: List[Finding] = []
    for lineno, number, claimed in collect_issue_claims(prose):
        actual, error = resolver.state(number)
        if actual is None:
            findings.append(
                Finding(
                    rel,
                    lineno,
                    f"cites #{number} as {claimed} but its state could not be "
                    f"resolved ({error}); re-run with --offline to skip issue checks",
                )
            )
            continue
        if actual == claimed:
            continue
        if claimed == "closed" and actual == "merged":
            continue
        findings.append(
            Finding(rel, lineno, f"cites #{number} as {claimed}, but it is {actual}")
        )
    return findings


# --------------------------------------------------------------------------
# Check 3 - quoted excerpts match source
# --------------------------------------------------------------------------


def check_excerpts(
    root: Path,
    doc: Path,
    lines: Sequence[str],
    prose: Sequence[Tuple[int, str]],
    fences: Sequence[Fence],
) -> List[Finding]:
    """Compare each marked fenced block against the source it quotes.

    The marker is required. Guessing which fenced block quotes a file - from
    a nearby path mention, say - misfires on command examples, which is most
    of what fenced blocks in these documents are.

    Markers are read from prose only, so a document that shows the marker
    syntax inside a fenced block is describing it, not making a claim.
    """
    rel = doc.relative_to(root).as_posix()
    by_open = {fence.open_line: fence for fence in fences}
    prose_linenos = {lineno for lineno, _ in prose}
    findings: List[Finding] = []
    for lineno, line in enumerate(lines, 1):
        if lineno not in prose_linenos:
            continue
        match = EXCERPT_ANNOTATION_RE.search(line)
        if not match:
            continue
        fence = _next_fence(by_open, lines, lineno)
        if fence is None:
            findings.append(
                Finding(rel, lineno, "excerpt marker is not followed by a fenced code block")
            )
            continue
        findings.extend(_compare_excerpt(root, rel, match, fence))
    return findings


def _next_fence(
    by_open: Dict[int, Fence], lines: Sequence[str], marker_line: int
) -> Optional[Fence]:
    """The fence opening on the next non-blank line after the marker."""
    probe = marker_line + 1
    while probe <= len(lines) and not lines[probe - 1].strip():
        probe += 1
    return by_open.get(probe)


def _compare_excerpt(root: Path, rel: str, match: re.Match, fence: Fence) -> List[Finding]:
    path = match.group("path").strip("`")
    source = resolve_within_root(root, path)
    if source is None:
        return [
            Finding(rel, fence.open_line, f"quotes `{path}`, which is outside the repository")
        ]
    if not source.is_file():
        return [Finding(rel, fence.open_line, f"quotes `{path}`, which does not exist")]

    source_lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    start = int(match.group("start")) if match.group("start") else 1
    end = int(match.group("end")) if match.group("end") else (
        int(match.group("start")) if match.group("start") else len(source_lines)
    )
    if start < 1 or end < start:
        return [Finding(rel, fence.open_line, f"quotes `{path}` with an invalid line range L{start}-L{end}")]
    if end > len(source_lines):
        return [
            Finding(
                rel,
                fence.open_line,
                f"quotes `{path}` lines {start}-{end}, but that file has "
                f"{len(source_lines)} lines",
            )
        ]

    expected = [line.rstrip() for line in source_lines[start - 1 : end]]
    quoted = [line.rstrip() for line in fence.body]
    while expected and not expected[-1]:
        expected.pop()
    while quoted and not quoted[-1]:
        quoted.pop()
    if expected == quoted:
        return []

    for offset, (want, got) in enumerate(zip(expected, quoted)):
        if want != got:
            return [
                Finding(
                    rel,
                    fence.open_line + 1 + offset,
                    f"quoted excerpt of `{path}` diverges at source line "
                    f"{start + offset}: expected {want.strip()!r}, found {got.strip()!r}",
                )
            ]
    return [
        Finding(
            rel,
            fence.open_line,
            f"quoted excerpt of `{path}` has {len(quoted)} lines, but source "
            f"lines {start}-{end} have {len(expected)}",
        )
    ]


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def check_documents(
    root: Path, subdir: str = "docs", offline: bool = False, repo: Optional[str] = None
) -> List[Finding]:
    resolver = IssueStateResolver(root, repo=repo)
    findings: List[Finding] = []
    for doc in iter_markdown(root, subdir):
        lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
        prose, fences = split_fences(lines)
        findings.extend(check_paths(root, doc, prose))
        if not offline:
            findings.extend(check_issue_states(root, doc, prose, resolver))
        findings.extend(check_excerpts(root, doc, lines, prose, fences))
    # Checks run in their own passes, so findings arrive grouped by check.
    # Reading order is by location.
    return sorted(findings, key=lambda f: (f.file, f.line))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    parser.add_argument(
        "--docs", default="docs", help="documentation directory, relative to root"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip the issue-state check instead of failing when gh cannot resolve one",
    )
    parser.add_argument("--repo", help="OWNER/REPO for issue lookups (default: gh infers it)")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: --root {root} is not a directory", file=sys.stderr)
        return 2

    findings = check_documents(root, args.docs, offline=args.offline, repo=args.repo)

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "findings": [
                        {"file": f.file, "line": f.line, "message": f.message}
                        for f in findings
                    ],
                },
                indent=2,
            )
        )
    elif findings:
        print(f"Documentation contradicts the code in {len(findings)} place(s):\n", file=sys.stderr)
        for finding in findings:
            print(finding.render(), file=sys.stderr)
    else:
        checked = len(iter_markdown(root, args.docs))
        skipped = " (issue-state check skipped)" if args.offline else ""
        print(f"✅ {checked} document(s) agree with the code they describe{skipped}.")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
