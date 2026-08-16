#!/usr/bin/env python3
"""smoke_preview.py — Governed smoke and E2E preview validation helper.

Executes post-deployment smoke and scenario verification against a running preview
environment. Validates HTTP reachability, response integrity, and acceptance
criteria scenarios. Visibly skips library projects with no runnable surface.
Fails closed to block promotion on broken deployments.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_FACTOR = 2.0


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    details: str


@dataclass(frozen=True)
class SmokeOutcome:
    success: bool
    skipped: bool
    status_code: int
    url: str
    message: str
    scenario_results: List[ScenarioResult]


def is_truthy(val: Optional[str]) -> bool:
    """Return True if string represents a truthy boolean value."""
    if val is None:
        return False
    return str(val).strip().lower() in ("true", "1", "yes", "y")


def is_falsy(val: Optional[str]) -> bool:
    """Return True if string represents a falsy boolean value."""
    if val is None:
        return False
    return str(val).strip().lower() in ("false", "0", "no", "n")


def validate_preview_url(url: str) -> bool:
    """Validate that preview URL is a well-formed HTTP/HTTPS URL."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if re.search(r"[\s'\"<>\\\[\]()]", url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def _write_step_summary(content: str) -> None:
    """Append report content to GitHub Actions step summary if available."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    try:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(content + "\n")
    except OSError as exc:
        print(f"[WARN] Could not write to GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)


DEFAULT_SCENARIO_PATHS = [
    ".github/scenarios/smoke.json",
    "control-plane/.github/scenarios/smoke.json",
    "scenarios/smoke.json",
]


def load_and_validate_scenarios(scenarios_file: Optional[str] = None) -> Tuple[bool, Optional[List[Dict[str, Any]]], str]:
    """Load and validate scenario definitions. Fails closed if file is requested but invalid."""
    target_file = scenarios_file
    if not target_file:
        for default_path in DEFAULT_SCENARIO_PATHS:
            if Path(default_path).is_file():
                target_file = default_path
                break

    if not target_file:
        return True, None, ""

    path_obj = Path(target_file)
    if not path_obj.is_file():
        return False, None, f"Scenarios file '{target_file}' does not exist or is not a file."

    try:
        with open(path_obj, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return False, None, f"Failed to parse scenarios JSON from '{target_file}': {exc}"

    if not isinstance(data, list):
        return False, None, f"Scenarios file '{target_file}' must contain a JSON array of scenario objects."

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            return False, None, f"Scenario at index {idx} in '{target_file}' must be an object."
        name = item.get("name")
        if not name or not isinstance(name, str) or not name.strip():
            return False, None, f"Scenario at index {idx} in '{target_file}' is missing a valid 'name' string."

        has_assertion = False
        for pattern_key in ("contains", "not_contains"):
            pat = item.get(pattern_key)
            if pat is not None:
                if not isinstance(pat, str) or not pat.strip():
                    return False, None, f"Scenario '{name}' {pattern_key} pattern must be a non-empty string."
                try:
                    re.compile(pat)
                except re.error as exc:
                    return False, None, f"Scenario '{name}' has invalid regex for {pattern_key}: {exc}"
                has_assertion = True

        if "min_length" in item:
            val = item["min_length"]
            if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                return False, None, f"Scenario '{name}' min_length must be a positive integer."
            has_assertion = True

        if not has_assertion:
            return False, None, f"Scenario '{name}' must define at least one valid assertion (contains, not_contains, or positive min_length)."

    return True, data, ""


def evaluate_html_scenarios(html_content: str, scenarios: Optional[List[Dict[str, Any]]] = None) -> List[ScenarioResult]:
    """Evaluate acceptance criteria scenarios against preview HTML content."""
    results: List[ScenarioResult] = []

    # 1. Base structural integrity scenario
    has_html_tag = bool(re.search(r"<!DOCTYPE\s+html|<html[\s>]", html_content, re.IGNORECASE))
    results.append(
        ScenarioResult(
            name="HTML Document Structure",
            passed=has_html_tag,
            details="Found valid HTML document declaration/root tag" if has_html_tag else "Missing <!DOCTYPE html> or <html> tag",
        )
    )

    # 2. Non-empty title / heading scenario
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_content, re.IGNORECASE | re.DOTALL)
    title_text = title_match.group(1).strip() if title_match else ""
    has_title = bool(title_text)
    results.append(
        ScenarioResult(
            name="Page Title Declaration",
            passed=has_title,
            details=f"Page title resolved: '{title_text}'" if has_title else "Missing or empty <title> tag",
        )
    )

    # 3. Absence of Unhandled Runtime Errors
    error_match = re.search(
        r"(Unhandled Runtime Error|Traceback\s+\(most recent call last\)|500\s+Internal\s+Server\s+Error)",
        html_content,
        re.IGNORECASE,
    )
    no_runtime_errors = not bool(error_match)
    results.append(
        ScenarioResult(
            name="Absence of Runtime Errors",
            passed=no_runtime_errors,
            details="No unhandled runtime errors detected" if no_runtime_errors else f"Detected runtime error pattern: '{error_match.group(1)}'",
        )
    )

    # 4. Custom acceptance scenario evaluations if provided
    if scenarios:
        for item in scenarios:
            name = str(item.get("name", "Custom Scenario"))
            contains_pattern = item.get("contains")
            not_contains_pattern = item.get("not_contains")
            min_length = item.get("min_length", 0)

            passed = True
            details_list = []

            if contains_pattern:
                if re.search(contains_pattern, html_content, re.IGNORECASE):
                    details_list.append(f"Matched pattern '{contains_pattern}'")
                else:
                    passed = False
                    details_list.append(f"Missing expected pattern '{contains_pattern}'")

            if not_contains_pattern:
                if re.search(not_contains_pattern, html_content, re.IGNORECASE):
                    passed = False
                    details_list.append(f"Found forbidden pattern '{not_contains_pattern}'")
                else:
                    details_list.append(f"Forbidden pattern '{not_contains_pattern}' absent")

            if isinstance(min_length, int) and not isinstance(min_length, bool) and min_length > 0:
                if len(html_content) >= min_length:
                    details_list.append(f"Body length {len(html_content)} >= {min_length}")
                else:
                    passed = False
                    details_list.append(f"Body length {len(html_content)} < {min_length}")

            results.append(ScenarioResult(name=name, passed=passed, details="; ".join(details_list)))

    return results


def fetch_preview_with_retry(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
) -> Tuple[int, Dict[str, str], str, str]:
    """Fetch URL with retries to account for CDN propagation delay.

    Returns (status_code, headers_dict, body_text, error_message).
    """
    attempt = 0
    current_delay = 1.0
    last_error = ""

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Aru-Agentic-SDLC-SmokeRunner/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )

    while attempt <= max_retries:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = getattr(response, "status", 200)
                headers = dict(response.headers.items())
                body_bytes = response.read()
                body_text = body_bytes.decode("utf-8", errors="replace")
                return status_code, headers, body_text, ""
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            last_error = f"HTTP {exc.code} {exc.reason}"
            if status_code in (404, 502, 503, 504) and attempt < max_retries:
                time.sleep(current_delay)
                current_delay *= backoff_factor
                attempt += 1
                continue
            return status_code, {}, "", last_error
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(current_delay)
                current_delay *= backoff_factor
                attempt += 1
                continue
            return 0, {}, "", last_error

    return 0, {}, "", last_error or "Exhausted retries without response"


def run_smoke_check(
    url: Optional[str] = None,
    has_preview: Optional[str] = None,
    is_library: Optional[str] = None,
    commit_sha: Optional[str] = None,
    scenarios_file: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> SmokeOutcome:
    """Run full smoke and scenario verification against preview URL or visibly skip."""
    # 1. Check if product is a library or has no preview surface
    if is_truthy(is_library) or is_falsy(has_preview):
        msg = "Product is a library or has no runnable preview surface. Skipping smoke/E2E test stage visibly."
        print(f"[INFO] {msg}")
        summary = (
            "### ⏭️ Smoke & E2E Preview Stage: Visibly Skipped\n\n"
            "- **Classification**: Library / No runnable web preview surface\n"
            f"- **Commit**: `{commit_sha or 'N/A'}`\n"
            "- **Status**: Skipped visibly per governance contract\n"
        )
        _write_step_summary(summary)
        return SmokeOutcome(
            success=True,
            skipped=True,
            status_code=0,
            url=url or "",
            message=msg,
            scenario_results=[],
        )

    # 2. Enforce valid preview URL for runnable products
    if not url or not validate_preview_url(url):
        msg = f"Invalid or missing preview URL for runnable product: '{url}'"
        print(f"[ERROR] {msg}", file=sys.stderr)
        summary = (
            "### ❌ Smoke & E2E Preview Stage: FAILED\n\n"
            f"- **Error**: {msg}\n"
            f"- **Commit**: `{commit_sha or 'N/A'}`\n"
            "- **Status**: Failing smoke stage (blocks promotion)\n"
        )
        _write_step_summary(summary)
        return SmokeOutcome(
            success=False,
            skipped=False,
            status_code=0,
            url=url or "",
            message=msg,
            scenario_results=[],
        )

    # 3. Load and validate scenario definitions (fail closed if invalid)
    valid_scenarios, custom_scenarios, scenario_err = load_and_validate_scenarios(scenarios_file)
    if not valid_scenarios:
        msg = f"Scenario validation failed: {scenario_err}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        summary = (
            "### ❌ Smoke & E2E Preview Stage: FAILED\n\n"
            f"- **Error**: {msg}\n"
            f"- **Commit**: `{commit_sha or 'N/A'}`\n"
            "- **Status**: Failing smoke stage (blocks promotion due to invalid scenarios configuration)\n"
        )
        _write_step_summary(summary)
        return SmokeOutcome(
            success=False,
            skipped=False,
            status_code=0,
            url=url or "",
            message=msg,
            scenario_results=[],
        )

    print(f"[INFO] Executing smoke & E2E verification against deployed preview: {url}")

    # 4. Fetch preview URL
    status_code, headers, body, err = fetch_preview_with_retry(
        url,
        timeout=timeout,
        max_retries=max_retries,
    )

    if status_code != 200 or err:
        msg = f"HTTP request failed for '{url}' (status={status_code}): {err}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        summary = (
            "### ❌ Smoke & E2E Preview Stage: FAILED\n\n"
            f"- **Preview URL**: [{url}]({url})\n"
            f"- **HTTP Status**: `{status_code}`\n"
            f"- **Error**: {err or 'Non-200 HTTP status code'}\n"
            f"- **Commit**: `{commit_sha or 'N/A'}`\n"
            "- **Result**: Deployed preview is unreachable or unhealthy; promotion blocked.\n"
        )
        _write_step_summary(summary)
        return SmokeOutcome(
            success=False,
            skipped=False,
            status_code=status_code,
            url=url,
            message=msg,
            scenario_results=[],
        )

    # 5. Evaluate scenario tests derived from acceptance criteria
    scenario_results = evaluate_html_scenarios(body, scenarios=custom_scenarios)
    failed_scenarios = [r for r in scenario_results if not r.passed]

    scenario_rows = "\n".join(
        f"| {'✅ Pass' if r.passed else '❌ Fail'} | **{r.name}** | {r.details} |"
        for r in scenario_results
    )

    if failed_scenarios:
        failed_names = ", ".join(r.name for r in failed_scenarios)
        msg = f"Smoke scenarios failed: {failed_names}"
        print(f"[ERROR] {msg}", file=sys.stderr)
        summary = (
            "### ❌ Smoke & E2E Preview Stage: FAILED\n\n"
            f"- **Preview URL**: [{url}]({url})\n"
            f"- **HTTP Status**: `200 OK`\n"
            f"- **Commit**: `{commit_sha or 'N/A'}`\n\n"
            "| Result | Scenario | Details |\n"
            "|---|---|---|\n"
            f"{scenario_rows}\n\n"
            "**Result**: Acceptance criteria scenarios failed on running surface; promotion blocked.\n"
        )
        _write_step_summary(summary)
        return SmokeOutcome(
            success=False,
            skipped=False,
            status_code=status_code,
            url=url,
            message=msg,
            scenario_results=scenario_results,
        )

    # 6. All checks passed
    msg = f"All smoke and E2E scenarios passed for preview: {url}"
    print(f"✅ {msg}")
    summary = (
        "### ✅ Smoke & E2E Preview Stage: Passed\n\n"
        f"- **Preview URL**: [{url}]({url})\n"
        f"- **HTTP Status**: `200 OK`\n"
        f"- **Commit**: `{commit_sha or 'N/A'}`\n\n"
        "| Result | Scenario | Details |\n"
        "|---|---|---|\n"
        f"{scenario_rows}\n"
    )
    _write_step_summary(summary)
    return SmokeOutcome(
        success=True,
        skipped=False,
        status_code=status_code,
        url=url,
        message=msg,
        scenario_results=scenario_results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run smoke and E2E validation against deployed preview URL.")
    parser.add_argument("--url", help="Deployed preview URL to validate")
    parser.add_argument("--has-preview", help="Whether preview deployment exists (true/false)")
    parser.add_argument("--is-library", help="Whether product is classified as a library (true/false)")
    parser.add_argument("--commit-sha", help="Exact merged commit SHA")
    parser.add_argument("--scenarios-file", help="Path to JSON scenario definition file")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP request timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Maximum fetch retry attempts")

    args = parser.parse_args()
    outcome = run_smoke_check(
        url=args.url,
        has_preview=args.has_preview,
        is_library=args.is_library,
        commit_sha=args.commit_sha,
        scenarios_file=args.scenarios_file,
        timeout=args.timeout,
        max_retries=args.max_retries,
    )
    return 0 if outcome.success else 1


if __name__ == "__main__":
    sys.exit(main())
