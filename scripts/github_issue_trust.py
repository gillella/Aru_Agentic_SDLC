"""Conditional GraphQL lookup for issue editor identity unavailable in REST."""

from typing import Any, Callable, Dict, Optional


ISSUE_TRUST_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    issue(number:$number) { editor { login } authorAssociation }
  }
}
"""


def query_issue_trust_identity(
    issue_id: int,
    get_slug: Callable[[], Optional[str]],
    run_json: Callable[[list[str]], Any],
) -> Optional[Dict[str, Any]]:
    """Return last editor and association, or None on partial/invalid data."""
    slug = get_slug()
    if not slug or "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    payload = run_json([
        "gh", "api", "graphql", "-f", f"query={ISSUE_TRUST_QUERY}",
        "-F", f"owner={owner}", "-F", f"repo={repo}", "-F", f"number={issue_id}",
    ])
    if not isinstance(payload, dict) or payload.get("errors"):
        return None
    try:
        node = payload["data"]["repository"]["issue"]
    except (KeyError, TypeError):
        return None
    if not isinstance(node, dict):
        return None
    trust: Dict[str, Any] = {}
    if "editor" in node:
        trust["editor"] = node.get("editor")
    association = node.get("authorAssociation")
    if isinstance(association, str) and association:
        trust["authorAssociation"] = association
    return trust
