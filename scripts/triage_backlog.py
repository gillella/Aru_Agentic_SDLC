#!/usr/bin/env python3
"""triage_backlog.py - promotes Backlog issues to Ready, and sizes the fleet.

Triage is the throughput cap nobody owns. The picker cannot hand out a Backlog
issue, so a fleet drains the Ready column and then idles while a human is
asleep. This script does the mechanical half of triage - verifying the Ready
contract - and reports exactly what each blocked issue is missing so the human
half is a two-minute job instead of an archaeology session.

  python3 triage_backlog.py                # report only
  python3 triage_backlog.py --promote      # promote everything that qualifies
  python3 triage_backlog.py --promote --issue 24 --issue 25
  python3 triage_backlog.py --capacity     # just the fleet-size answer

The Ready contract (all four required):
  * not an epic
  * acceptance criteria present, as checkboxes
  * a touches: declaration
  * every depends-on issue is closed

Oversized-scope recommendation (either signal is sufficient):
  * more than 8 acceptance-criteria checkboxes
  * touches: spans more than one top-level area
"""

import argparse
import os
import re
import sys
from typing import Any, Optional

from common import (
    claimed_by,
    get_repo_slug,
    label_names,
    list_open_issues,
    parse_touches,
    touches_conflict,
)
from fetch_next_issue import is_epic, parse_dependencies
from update_issue_status import update_status


def acceptance_criteria(body: str) -> list[str]:
    """Returns checkbox lines under the Acceptance Criteria heading.

    Requires the heading. A bare list of checkboxes elsewhere in the body is
    not acceptance criteria - it is usually a task list the author left behind,
    and treating it as criteria would promote under-specified issues.
    """
    if not body:
        return []
    parts = re.split(
        r"^\s*#{1,4}\s*(?:acceptance\s+criteria(?:\s*[/:]\s*expected\s+behavior)?|expected\s+behavior(?:\s*[/:]\s*predicates)?)\s*$",
        body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(parts) < 2:
        return []
    tail = re.split(r"^\s*#{1,4}\s+", parts[1], flags=re.MULTILINE)[0]
    return [ln.strip() for ln in tail.splitlines() if re.match(r"^\s*[-*]\s*\[[ xX]\]", ln)]


VERIFY_PLACEHOLDERS = frozenset({
    "command to verify",
    "command",
    "cmd",
    "commands",
    "<command>",
    "<command to verify>",
    "<command_to_verify>",
    "todo",
    "tbd",
    "none",
    "null",
    "n/a",
    "na",
    "...",
    "test",
    "test command",
    "your command here",
    "placeholder",
})


def _is_valid_fenced_verify_command(cmd: str) -> bool:
    cleaned = cmd.strip("`'\" \t\r\n").strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    if lower in VERIFY_PLACEHOLDERS:
        return False
    if re.match(r"^(?:<.*>|\.{3,}|todo|tbd|none|n/a)$", lower):
        return False
    return True


def _is_criterion_machine_checkable(criterion: str) -> bool:
    """Checks if an individual criterion has a structural backticked verify command, assertion, or opt-out."""
    # 1. Structural backticked verify command: (verify: `cmd`) or verify: `cmd`
    fenced_verify_pattern = re.compile(
        r"\((?:verify|verify_cmd):\s*`([^`]+)`\)"
        r"|\b(?:verify|verify_cmd)\s*:\s*`([^`]+)`",
        re.IGNORECASE,
    )
    for m in fenced_verify_pattern.finditer(criterion):
        cmd = m.group(1) or m.group(2)
        if cmd and _is_valid_fenced_verify_command(cmd):
            return True

    # 2. Explicit machine-readable opt-out: (verify: manual - reason) or [manual: reason]
    opt_out_pattern = re.compile(
        r"\((?:verify|verify_cmd):\s*(?:manual|opt-out|exempt|non-executable)\s*[:\-]\s*([^)]+)\)"
        r"|\[(?:manual|opt-out|exempt|non-executable)\s*[:\-]\s*([^\]]+)\]",
        re.IGNORECASE,
    )
    if opt_out_pattern.search(criterion):
        return True

    # 3. Structured assertions / invariants / exit codes / exceptions
    assertion_pattern = re.compile(
        r"\bexits?\s+(?:with\s+code\s+)?(?:0|1|non-zero)\b"
        r"|\breturns?\s+(?:code\s+)?(?:0|1|true|false)\b"
        r"|\bassert(?:s|ions?)?\s+(?:that\s+)?[`'\"]?[a-zA-Z0-9_.\s]+?\s*(?:==|!=|is|<=|>=|<|>|=|equals)\s*[`'\"]?(?:0|1|true|false|empty|non-empty|none|null|\d+)[`'\"]?"
        r"|\b(?:raises|throws)\s+(?:error|exception|[A-Z][a-zA-Z0-9_]*(?:Error|Exception))\b",
        re.IGNORECASE,
    )
    if assertion_pattern.search(criterion):
        return True

    return False


def has_machine_checkable_predicates(criteria: list[str]) -> bool:
    """Returns True if EVERY acceptance criterion contains an executable verify command, checkable assertion, or valid opt-out."""
    if not criteria:
        return False
    return all(_is_criterion_machine_checkable(c) for c in criteria)


def has_verification(body: str) -> bool:
    """A Verification section is what makes an acceptance criterion checkable."""
    if not body:
        return False
    parts = re.split(
        r"^\s*#{1,4}\s*verification\s*$", body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(parts) < 2:
        return False
    tail = re.split(r"^\s*#{1,4}\s+", parts[1], flags=re.MULTILINE)[0]
    return bool(tail.strip())


ARU_SDLC_REPO_SLUG = "gillella/Aru_Agentic_SDLC"
LEGACY_ISSUE_CUTOFF_NUMBER = 158
SPLIT_ACCEPTANCE_CRITERIA_THRESHOLD = 8

EXAMPLE_CONFORMING_ISSUE_BODY = """## Feature Description
Describe the problem and intended change.

## Acceptance Criteria
- [ ] Predicate 1 (verify: `python3 -m unittest tests.test_foo`)
- [ ] Predicate 2 (verify: `python3 scripts/foo.py --check`)

## Decision Boundaries
- Default: return 0 on success
- Error handling: exit 1 on failure
- Edge cases: handle empty inputs safely

## Non-Goals
- Modifying third-party dependencies

## Verification
`python3 -m unittest discover tests` exits 0.

## Dependencies
depends-on: none
touches: scripts/foo.py, tests/test_foo.py
parallel-eligible: true
"""

EXAMPLE_CONFORMING_ISSUE = f"""
Example of a conforming issue with machine-checkable criteria:

{EXAMPLE_CONFORMING_ISSUE_BODY}
"""


def _extract_section(body: str, heading_pattern: str) -> str:
    """Extracts markdown text under a given heading until the next heading, stripping HTML comments."""
    if not body:
        return ""
    parts = re.split(
        rf"^\s*#{{1,4}}\s*{heading_pattern}\b.*$", body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(parts) < 2:
        return ""
    tail = re.split(r"^\s*#{1,4}\s+", parts[1], flags=re.MULTILINE)[0]
    tail = re.sub(r"<!--.*?-->", "", tail, flags=re.DOTALL)
    return tail.strip()


def has_decision_boundaries(body: str) -> bool:
    """Checks for a substantive Decision Boundaries section in the issue body.

    Rejects missing sections, empty sections, and untouched template placeholders
    such as bare '- Default:', '- Edge cases:', '- Error handling:'.
    """
    content = _extract_section(body, r"decision\s+boundaries")
    if not content:
        return False
    placeholder_pattern = re.compile(
        r"^[-*]?\s*(default|edge\s*cases?|error\s*handling|thresholds?)\s*:\s*$",
        re.IGNORECASE,
    )
    substantive_lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line in ("-", "*", "+"):
            continue
        if placeholder_pattern.match(line):
            continue
        substantive_lines.append(line)
    return len(substantive_lines) > 0


def has_non_goals(body: str) -> bool:
    """Checks for a substantive Non-Goals section in the issue body.

    Rejects missing sections, empty sections, and untouched template placeholders
    such as a bare '-' or '*'.
    """
    content = _extract_section(body, r"non[- ]goals")
    if not content:
        return False
    substantive_lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line in ("-", "*", "+"):
            continue
        substantive_lines.append(line)
    return len(substantive_lines) > 0


def is_feat_or_fix(issue: dict[str, Any]) -> bool:
    """Checks if an issue represents a feature or bug fix."""
    labels = {lbl.get("name", "").lower() for lbl in (issue.get("labels") or [])}
    title = (issue.get("title") or "").lower()
    return (
        any(lbl in labels for lbl in ("type:feat", "type:fix", "feature", "bug"))
        or title.startswith("feat:")
        or title.startswith("fix:")
    )


def is_legacy_issue(num: int, repo_slug: Optional[str] = None) -> bool:
    """Grandfathering only applies to pre-existing issues in Aru_Agentic_SDLC itself.

    Downstream repositories enforce machine-checkable criteria from issue #1 onwards.
    """
    if num <= 0 or num > LEGACY_ISSUE_CUTOFF_NUMBER:
        return False
    if repo_slug is None:
        repo_slug = get_repo_slug()
    return bool(repo_slug and repo_slug.strip().lower() == ARU_SDLC_REPO_SLUG.lower())


def ready_gaps(issue: dict[str, Any], open_numbers: set, repo_slug: Optional[str] = None) -> list[str]:
    """Returns the list of unmet Ready-contract elements. Empty means ready."""
    body = issue.get("body") or ""
    num = issue.get("number", 0)
    gaps = []

    if is_epic(issue.get("labels", [])):
        gaps.append("is an epic (never directly implementable)")
        return gaps

    criteria = acceptance_criteria(body)
    if not criteria:
        gaps.append("no acceptance criteria checkboxes")
    elif is_feat_or_fix(issue) and not has_machine_checkable_predicates(criteria):
        if is_legacy_issue(num, repo_slug):
            print(
                f"  [WARN] Pre-existing legacy issue #{num} lacks machine-checkable verification predicates in acceptance criteria; warning only.",
                file=sys.stderr,
            )
        else:
            gaps.append("acceptance criteria lack machine-checkable predicate (e.g., '(verify: `cmd`)' or test assertion)")

    if not has_verification(body):
        gaps.append("no verification section")
    if not parse_touches(body):
        gaps.append("no touches: declaration")

    if is_feat_or_fix(issue):
        missing_db = not has_decision_boundaries(body)
        missing_ng = not has_non_goals(body)
        if missing_db or missing_ng:
            if is_legacy_issue(num, repo_slug):
                print(
                    f"  [WARN] Pre-existing legacy issue #{num} is missing machine-checkable criteria sections "
                    f"({'Decision Boundaries' if missing_db else ''}{' and ' if missing_db and missing_ng else ''}{'Non-Goals' if missing_ng else ''}); warning only.",
                    file=sys.stderr,
                )
            else:
                if missing_db:
                    gaps.append("missing section: ## Decision Boundaries")
                if missing_ng:
                    gaps.append("missing section: ## Non-Goals")

    unresolved = [d for d in parse_dependencies(body) if d in open_numbers]
    if unresolved:
        gaps.append("depends-on still open: " + ", ".join(f"#{d}" for d in unresolved))

    return gaps


def split_reasons(issue: dict[str, Any]) -> list[str]:
    """Returns concrete reasons a Ready-contract issue should be split.

    This is deliberately advisory: each visible oversize signal is enough to
    hold automatic promotion, while ``--force`` lets a triager record an
    explicit exception.
    """
    body = issue.get("body") or ""
    criteria_count = len(acceptance_criteria(body))
    touches_match = re.search(
        r"^[ \t]*[*_`]{0,2}touches[*_`]{0,2}[ \t]*:[ \t]*([^\n]*)",
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    declared_paths = []
    if touches_match:
        declared_paths = [
            path.strip().strip("`")
            for path in touches_match.group(1).split(",")
            if path.strip().strip("`")
        ]
    area_roots = []
    repository_wide = False
    for declared_path in declared_paths:
        path = declared_path.strip()
        while path.startswith("./"):
            path = path[2:]
        if path in {"", ".", "/"}:
            repository_wide = True
            continue
        path = path.lstrip("/")
        if not path or path == ".":
            repository_wide = True
            continue
        # Root-level files share one repository-root area. Treating each
        # conventional filename as an area makes README.md + pyproject.toml
        # look cross-cutting. A bare name without a filename suffix is
        # ambiguous, though: the touches enforcement grammar accepts it as a
        # directory prefix (for example ``scripts``). Preserve that name as an
        # area so triage cannot promote a cross-directory scope by mistaking
        # both prefixes for root files.
        if "/" in path:
            area_roots.append(path.split("/", 1)[0])
        elif any(char in path for char in "*?["):
            area_roots.append(path)
        elif "." not in path:
            area_roots.append(path)
        else:
            area_roots.append("<root>")
    wildcard_roots = sorted({
        root for root in area_roots if any(char in root for char in "*?[")
    })
    areas = sorted({
        root for root in area_roots if not any(char in root for char in "*?[")
    })
    reasons = []
    if criteria_count > SPLIT_ACCEPTANCE_CRITERIA_THRESHOLD:
        reasons.append(
            f"{criteria_count} acceptance criteria exceed the threshold of "
            f"{SPLIT_ACCEPTANCE_CRITERIA_THRESHOLD}"
        )
    if repository_wide:
        reasons.append("touches include the whole repository root")
    if len(areas) > 1:
        reasons.append(f"touches span {len(areas)} top-level areas: {', '.join(areas)}")
    if wildcard_roots:
        reasons.append(
            "touches use wildcard top-level area patterns: "
            + ", ".join(wildcard_roots)
        )
    return reasons


def partition(issues: list[dict[str, Any]]) -> tuple[list, list, list]:
    """Splits open issues into (backlog, ready, held)."""
    backlog, ready, held = [], [], []
    for issue in issues:
        names = {n.lower() for n in label_names(issue)}
        if claimed_by(issue) or "status:in-review" in names:
            held.append(issue)
        elif "status:ready" in names:
            ready.append(issue)
        elif "status:backlog" in names:
            backlog.append(issue)
    return backlog, ready, held


def capacity(ready: list[dict[str, Any]], held: list[dict[str, Any]]) -> dict[str, Any]:
    """How many agents this board can actually keep busy right now.

    Ready count alone overstates it: two Ready issues whose touches overlap
    cannot run at the same time, and neither can one that collides with work
    already in flight. This walks the list greedily the way the picker would.
    """
    in_flight_paths: list[str] = []
    for issue in held:
        in_flight_paths.extend(parse_touches(issue.get("body") or ""))

    concurrent, deferred = [], []
    for issue in sorted(ready, key=lambda i: i["number"]):
        paths = parse_touches(issue.get("body") or "")
        if not paths:
            deferred.append((issue["number"], "no touches declaration"))
            continue
        clash = touches_conflict(paths, in_flight_paths)
        if clash:
            deferred.append((issue["number"], f"path conflict on {clash[0]}"))
            continue
        concurrent.append(issue["number"])
        in_flight_paths.extend(paths)

    return {"concurrent": concurrent, "deferred": deferred, "ready_total": len(ready)}


def print_capacity(
    cap: dict[str, Any],
    held: list[dict[str, Any]],
    ready_target: Optional[int] = None,
) -> None:
    n = len(cap["concurrent"])
    print("\n=== Fleet capacity ===")
    print(f"  Ready issues:            {cap['ready_total']}")
    if ready_target is not None:
        print(f"  Ready target:            {ready_target}")
    print(f"  Claimable simultaneously: {n}  {cap['concurrent']}")
    if cap["deferred"]:
        print("  Deferred this moment:")
        for num, why in cap["deferred"]:
            print(f"    #{num}: {why}")
    if held:
        holders = ", ".join(
            f"#{i['number']}({claimed_by(i) or 'parked'})" for i in held
        )
        print(f"  In flight:               {holders}")
    if ready_target is not None and cap["ready_total"] < ready_target:
        print(f"\n  ⚠️  Ready depth ({cap['ready_total']}) is below target ({ready_target}) — fleet is starved.")
    if n == 0:
        print("\n  → Launch nothing. Triage first.")
    else:
        print(f"\n  → Launch at most {n} agent(s). More will idle.")


def main():
    parser = argparse.ArgumentParser(description="Verify the Ready contract and promote Backlog issues.")
    parser.add_argument("--promote", action="store_true", help="Promote qualifying issues to Ready")
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --promote, override SPLIT recommendations (never Ready-contract gaps or epics)",
    )
    parser.add_argument("--issue", type=int, action="append", default=[],
                        help="Restrict to specific issue numbers (repeatable)")
    parser.add_argument("--capacity", action="store_true", help="Print only the fleet-capacity summary")
    parser.add_argument("--ready-target", type=int, default=None,
                        help="Configured Ready depth target (or set ARU_READY_TARGET)")
    args = parser.parse_args()

    issues = list_open_issues()
    if not issues:
        print("[ERROR] No open issues returned; is gh authenticated in this repo?", file=sys.stderr)
        return 1
    open_numbers = {i["number"] for i in issues}
    backlog, ready, held = partition(issues)

    target = args.ready_target
    if target is None:
        raw = os.environ.get("ARU_READY_TARGET", "").strip()
        if raw:
            try:
                target = int(raw)
            except ValueError:
                pass

    if args.capacity:
        print_capacity(capacity(ready, held), held, ready_target=target)
        return 0

    if args.issue:
        backlog = [i for i in backlog if i["number"] in args.issue]

    slug = get_repo_slug()
    qualified, split_recommended, blocked = [], [], []
    for issue in sorted(backlog, key=lambda i: i["number"]):
        gaps = ready_gaps(issue, open_numbers, repo_slug=slug)
        if gaps:
            blocked.append((issue, gaps))
            continue
        reasons = split_reasons(issue)
        (split_recommended if reasons else qualified).append((issue, reasons))

    print(f"=== Backlog triage — {len(backlog)} issue(s) examined ===\n")
    if qualified:
        print("Meets the Ready contract:")
        for issue, _ in qualified:
            print(f"  ✅ #{issue['number']:<4} {issue['title']}")
    if split_recommended:
        print("\nSPLIT — Ready contract met, but scope looks oversized:")
        for issue, reasons in split_recommended:
            print(f"  ⚠️  #{issue['number']:<4} {issue['title']}")
            for reason in reasons:
                print(f"        · {reason}")
    if blocked:
        print("\nBlocked — Ready contract incomplete:")
        has_criteria_gaps = False
        for issue, gaps in blocked:
            print(f"  ❌ #{issue['number']:<4} {issue['title']}")
            for gap in gaps:
                print(f"        · {gap}")
                if any(k in gap.lower() for k in ("missing section:", "acceptance criteria lack", "machine-checkable", "decision boundaries", "non-goals")):
                    has_criteria_gaps = True
        if has_criteria_gaps:
            print(f"\n{EXAMPLE_CONFORMING_ISSUE.strip()}\n")

    promoted = 0
    promotable = qualified + (split_recommended if args.force else [])
    if args.promote and promotable:
        print()
        for issue, _ in promotable:
            if update_status(issue["number"], "Ready"):
                print(f"  ⬆️  #{issue['number']} → Ready")
                promoted += 1
            else:
                print(f"  [WARN] #{issue['number']} could not be promoted", file=sys.stderr)
        # Re-read so capacity reflects the promotions we just made.
        ready = partition(list_open_issues())[1]
    elif qualified:
        print(f"\n  {len(qualified)} issue(s) would be promoted. Re-run with --promote.")

    if split_recommended and not (args.promote and args.force):
        print(
            f"\n  {len(split_recommended)} issue(s) held for splitting. "
            "Use --promote --force to override the recommendation."
        )

    print_capacity(capacity(ready, held), held, ready_target=target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
