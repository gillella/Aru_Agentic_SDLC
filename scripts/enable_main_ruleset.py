#!/usr/bin/env python3
"""Create or update the Aru ruleset that blocks direct pushes to main.

Required approving review and required linear history are deliberately
absent. The former deadlocks a same-account fleet (#123 / S0.7a). The
latter forbids merge commits and fights #89 / S1.1.

A 403 from GitHub because the plan does not include rulesets is exit 3,
not a silent success. Live enable on this private repo is #133.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from common import get_repo_slug, run_cmd

RULESET_NAME = "aru-protect-main"
CI_CONTEXT = "Lint, Verify & Test"
EXIT_BLOCKED = 3
FORBIDDEN_RULE_TYPES = frozenset({"required_linear_history"})


def ruleset_payload() -> Dict[str, Any]:
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["refs/heads/main", "refs/heads/master"],
                "exclude": [],
            }
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_approving_review_count": 0,
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": CI_CONTEXT}],
                },
            },
        ],
    }


def assert_safe_payload(payload: Dict[str, Any]) -> None:
    """Refuse to ship the two rules this issue explicitly excludes."""
    for rule in payload.get("rules") or []:
        rtype = rule.get("type")
        if rtype in FORBIDDEN_RULE_TYPES:
            raise ValueError(f"forbidden ruleset rule: {rtype}")
        params = rule.get("parameters") or {}
        if int(params.get("required_approving_review_count") or 0) != 0:
            raise ValueError("required approving review would deadlock the fleet")


def _gh_api(method: str, path: str, body: Optional[Dict[str, Any]] = None):
    cmd = ["gh", "api", "--method", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    payload = json.dumps(body) if body is not None else None
    try:
        proc = __import__("subprocess").run(
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def is_plan_blocked(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}".lower()
    return "upgrade to github pro" in blob or (
        "403" in blob and "ruleset" in blob
    )


def find_existing_id(slug: str) -> Optional[int]:
    code, out, err = run_cmd(
        ["gh", "api", f"repos/{slug}/rulesets"], check=False
    )
    if code != 0:
        if is_plan_blocked(err, out):
            return None
        return None
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("name") == RULESET_NAME:
            return item.get("id")
    return None


def apply_ruleset(slug: str) -> int:
    payload = ruleset_payload()
    assert_safe_payload(payload)
    existing = find_existing_id(slug)
    if existing is None:
        # Distinguish "none exist" from "list 403'd": re-check with POST.
        path = f"repos/{slug}/rulesets"
        method = "POST"
    else:
        path = f"repos/{slug}/rulesets/{existing}"
        method = "PUT"
    code, out, err = _gh_api(method, path, payload)
    if code == 0:
        action = "updated" if existing else "created"
        print(f"✅ Ruleset '{RULESET_NAME}' {action} on {slug}.")
        return 0
    if is_plan_blocked(err, out):
        print(
            "[BLOCKED] GitHub refused rulesets on this repository (HTTP 403). "
            "Private repos need GitHub Pro, or the repo must be public. "
            "The local pre-push hook remains skippable until then. See #133.",
            file=sys.stderr,
        )
        detail = (err or out).strip()
        if detail:
            print(detail, file=sys.stderr)
        return EXIT_BLOCKED
    print(f"[ERROR] ruleset {method} {path} failed:\n{(err or out).strip()}",
          file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enable the Aru main-branch ruleset (no required reviews)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload and refuse to call GitHub.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create or update the ruleset on the current repository.",
    )
    args = parser.parse_args(argv)
    payload = ruleset_payload()
    assert_safe_payload(payload)
    if args.dry_run or not args.apply:
        print(json.dumps(payload, indent=2))
        if not args.apply:
            print(
                "Pass --apply to create or update the ruleset.",
                file=sys.stderr,
            )
        return 0
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        print("[ERROR] Could not resolve owner/repo via gh.", file=sys.stderr)
        return 1
    return apply_ruleset(slug)


if __name__ == "__main__":
    sys.exit(main())
