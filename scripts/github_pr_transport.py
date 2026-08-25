#!/usr/bin/env python3
# line-ceiling: 120
"""REST transport for opening a pull request when GraphQL is unavailable.

`gh pr create` is a GraphQL client. GraphQL and REST bill against separate
hourly quotas, so an exhausted GraphQL budget hard-fails pull-request creation
while the REST core budget can be nearly untouched -- observed 2026-08-25 with
graphql 0/5000 and core 4951/5000, which stranded a pushed branch that had
already passed verification.

This mirrors the read-side fallback in `github_inventory.py`: REST is a
transport for the same request, never a second lifecycle authority. The runner
is injected so callers keep one `run_cmd` policy and the module stays testable
without a live GitHub.
"""

import json
import re
import sys
from typing import Any, Callable, Dict, Optional, Tuple

from github_inventory import local_repo_slug

Runner = Callable[..., Tuple[int, str, str]]

# Deliberately narrow: only an explicitly GraphQL-attributed availability
# failure may reroute. A validation error (duplicate PR, empty diff, protected
# base) must keep its original message rather than be retried on a transport
# that would reject it again with a less useful one.
_GRAPHQL_UNAVAILABLE_RE = re.compile(
    r"graphql.*(?:rate limit|timeout|timed out|502|503|bad gateway"
    r"|service unavailable|temporarily unavailable)",
    re.IGNORECASE | re.DOTALL,
)


def graphql_unavailable(stderr: str) -> bool:
    """True when stderr blames GraphQL availability rather than the request."""
    return bool(_GRAPHQL_UNAVAILABLE_RE.search(stderr or ""))


def _default_base_branch(run: Runner, slug: str) -> Optional[str]:
    """The repository's default branch, read over REST."""
    code, out, _err = run(
        ["gh", "api", f"repos/{slug}", "--jq", ".default_branch"], check=False)
    base = (out or "").strip()
    return base if code == 0 and base else None


def _pull_request_url(payload: Any) -> Optional[str]:
    """The html_url of a REST pull-request response, when it has a usable one."""
    if not isinstance(payload, dict):
        return None
    url = payload.get("html_url")
    return url.strip() if isinstance(url, str) and url.strip() else None


def create_pull_request_over_rest(
    run: Runner, title: str, body: str, head: str,
) -> Tuple[bool, str, str]:
    """Open a draft pull request through REST. Returns (ok, html_url, error).

    The caller consumes the URL exactly as it consumes `gh pr create` stdout,
    so a rerouted creation is indistinguishable to every downstream step.
    """
    slug = local_repo_slug(run)
    if not slug:
        return False, "", "could not resolve owner/repo from the origin remote"
    base = _default_base_branch(run, slug)
    if not base:
        return False, "", "could not read the repository default branch over REST"
    code, out, err = run([
        "gh", "api", "--method", "POST", f"repos/{slug}/pulls",
        "-f", f"title={title}", "-f", f"body={body}",
        "-f", f"head={head}", "-f", f"base={base}",
        "-F", "draft=true",
    ], check=False)
    if code != 0:
        return False, "", (err or "").strip() or "REST pull-request creation failed"
    try:
        payload: Dict[str, Any] = json.loads(out or "")
    except (TypeError, json.JSONDecodeError):
        return False, "", "REST returned a response that is not valid JSON"
    url = _pull_request_url(payload)
    if url is None:
        # A 2xx we cannot address is not a usable pull request: every later
        # step (identity stamp, review label, ready) needs a reference.
        return False, "", "REST response contained no pull-request URL"
    return True, url, ""


def open_pull_request_with_fallback(
    run: Runner, code: int, out: str, err: str,
    title: str, body: str, head: str,
) -> Tuple[int, str, str]:
    """Pass through a `gh pr create` result, rerouting only a GraphQL outage.

    Returns the same (code, stdout, stderr) shape the caller already handles,
    so the reroute needs no new branch at the call site. A REST failure keeps
    the original GraphQL error and appends the fallback's own cause, because
    the first error is what an operator needs to see.
    """
    if code == 0 or not graphql_unavailable(err):
        return code, out, err
    print("[WARN] GraphQL is unavailable for PR creation; retrying over REST.",
          file=sys.stderr)
    ok, url, rest_err = create_pull_request_over_rest(run, title, body, head)
    if not ok:
        return code, out, f"{err}\n[REST fallback also failed] {rest_err}"
    return 0, url, ""
