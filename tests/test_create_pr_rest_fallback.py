# line-ceiling: 300
"""GraphQL exhaustion must not strand a finished unit of work.

`gh pr create` speaks GraphQL. GraphQL and REST bill against separate hourly
quotas, so the factory can hold a pushed branch, green verification, and a dead
GraphQL budget while REST is nearly untouched. These tests pin both halves of
the contract: an availability failure reroutes to REST, and a request failure
does not.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import create_pr  # noqa: E402
import github_pr_transport as transport  # noqa: E402

RATE_LIMITED = "GraphQL: API rate limit already exceeded for user ID 1961078."
CREATED = {"html_url": "https://github.com/o/r/pull/451", "number": 451}


def fake_runner(slug="o/r", base=(0, "main"), post=(0, CREATED, "")):
    """A run_cmd stand-in routing the calls the REST path can make."""
    def run(cmd, check=True, **_kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return (0, f"https://github.com/{slug}.git", "") if slug else (1, "", "no remote")
        if cmd[:2] == ["gh", "api"] and "--jq" in cmd:
            return base[0], base[1], ""
        if "--method" in cmd:
            code, payload, err = post
            body = payload if isinstance(payload, str) else json.dumps(payload)
            return code, ("" if payload is None else body), err
        raise AssertionError(f"unexpected command: {cmd}")
    return run


class GraphqlUnavailableTests(unittest.TestCase):
    def test_the_observed_rate_limit_message_is_recognized(self):
        self.assertTrue(transport.graphql_unavailable(RATE_LIMITED))

    def test_transient_graphql_faults_are_recognized(self):
        for message in ("GraphQL: 502 Bad Gateway", "graphql: service unavailable",
                        "GraphQL request timed out"):
            with self.subTest(message=message):
                self.assertTrue(transport.graphql_unavailable(message))

    def test_request_errors_never_reroute(self):
        """REST would reject these the same way, with a worse message."""
        for message in (
            "GraphQL: A pull request already exists for gillella:my-branch.",
            "GraphQL: No commits between main and my-branch",
            "pull request create failed: invalid base branch",
            "",
        ):
            with self.subTest(message=message):
                self.assertFalse(transport.graphql_unavailable(message))


class CreateOverRestTests(unittest.TestCase):
    def test_success_returns_the_html_url(self):
        ok, url, err = transport.create_pull_request_over_rest(
            fake_runner(), "t", "b", "branch")
        self.assertTrue(ok)
        self.assertEqual(url, CREATED["html_url"])
        self.assertEqual(err, "")

    def test_posts_a_draft_against_the_default_base(self):
        seen = {}
        inner = fake_runner(base=(0, "trunk"))

        def capture(cmd, check=True, **kwargs):
            if "--method" in cmd:
                seen["cmd"] = cmd
            return inner(cmd, check=check, **kwargs)

        ok, _url, _err = transport.create_pull_request_over_rest(
            capture, "t", "b", "branch")
        self.assertTrue(ok)
        self.assertIn("repos/o/r/pulls", seen["cmd"])
        self.assertIn("base=trunk", seen["cmd"])
        self.assertIn("head=branch", seen["cmd"])
        self.assertIn("draft=true", seen["cmd"])

    def test_unresolvable_slug_fails_closed(self):
        ok, _url, err = transport.create_pull_request_over_rest(
            fake_runner(slug=""), "t", "b", "branch")
        self.assertFalse(ok)
        self.assertIn("origin remote", err)

    def test_unreadable_default_branch_fails_closed(self):
        ok, _url, err = transport.create_pull_request_over_rest(
            fake_runner(base=(1, "")), "t", "b", "branch")
        self.assertFalse(ok)
        self.assertIn("default branch", err)

    def test_success_without_a_url_fails_closed(self):
        """A 2xx we cannot address is not a usable pull request."""
        ok, _url, err = transport.create_pull_request_over_rest(
            fake_runner(post=(0, {"number": 4}, "")), "t", "b", "branch")
        self.assertFalse(ok)
        self.assertIn("no pull-request URL", err)

    def test_non_json_response_fails_closed(self):
        ok, _url, err = transport.create_pull_request_over_rest(
            fake_runner(post=(0, "<html>gateway</html>", "")), "t", "b", "branch")
        self.assertFalse(ok)
        self.assertIn("not valid JSON", err)

    def test_rest_rejection_is_reported(self):
        ok, _url, err = transport.create_pull_request_over_rest(
            fake_runner(post=(1, None, "422 Validation Failed")), "t", "b", "branch")
        self.assertFalse(ok)
        self.assertIn("422", err)


class FallbackDecisionTests(unittest.TestCase):
    def test_success_passes_straight_through(self):
        def explode(*_a, **_k):
            raise AssertionError("a successful create must not call REST")

        code, out, err = transport.open_pull_request_with_fallback(
            explode, 0, "url", "", "t", "b", "h")
        self.assertEqual((code, out, err), (0, "url", ""))

    def test_rate_limit_reroutes(self):
        code, out, err = transport.open_pull_request_with_fallback(
            fake_runner(), 1, "", RATE_LIMITED, "t", "b", "h")
        self.assertEqual(code, 0)
        self.assertEqual(out, CREATED["html_url"])
        self.assertEqual(err, "")

    def test_request_error_is_left_untouched(self):
        def explode(*_a, **_k):
            raise AssertionError("a request error must not call REST")

        code, _out, err = transport.open_pull_request_with_fallback(
            explode, 1, "", "GraphQL: A pull request already exists.", "t", "b", "h")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_failed_fallback_keeps_the_original_cause(self):
        code, _out, err = transport.open_pull_request_with_fallback(
            fake_runner(slug=""), 1, "", RATE_LIMITED, "t", "b", "h")
        self.assertEqual(code, 1)
        self.assertIn("rate limit", err.lower())
        self.assertIn("REST fallback also failed", err)


class CreatePrWiringTests(unittest.TestCase):
    """The reroute must be reached from create_pr(), not just unit-tested."""

    def _stubs(self):
        return (
            patch.object(create_pr, "get_current_branch", lambda: "my-branch"),
            patch.object(create_pr, "get_issue", lambda _n: {"title": "t"}),
            patch.object(create_pr, "get_current_commit", lambda: "a" * 40),
            patch.object(create_pr, "collect_verification_evidence", lambda *_a, **_k: []),
            patch.object(create_pr, "render_verification_evidence", lambda *_a, **_k: ""),
        )

    def _run_create(self, create_stderr):
        """Drive create_pr() with one fake runner serving gh and REST alike."""
        rest = fake_runner()
        seen = {}

        def run_cmd(cmd, check=True, **kwargs):
            if cmd[:3] == ["gh", "pr", "create"]:
                return 1, "", create_stderr
            if cmd[:3] == ["git", "remote", "get-url"] or cmd[:2] == ["gh", "api"]:
                return rest(cmd, check=check, **kwargs)
            return 0, "", ""

        stubs = self._stubs()
        with stubs[0], stubs[1], stubs[2], stubs[3], stubs[4], \
                patch.object(create_pr, "run_cmd", run_cmd), \
                patch.object(create_pr, "apply_identity",
                             lambda ref, *_a: seen.setdefault("ref", ref) or True), \
                patch.object(create_pr, "finalize_review_assignment", lambda *_a: True):
            result = create_pr.create_pr(436, title="t", body="b", agent="x",
                                         family="anthropic")
        return result, seen

    def test_rate_limited_creation_opens_the_pr_over_rest(self):
        result, seen = self._run_create(RATE_LIMITED)
        self.assertTrue(result)
        # Downstream must address the PR REST opened, not the bare branch.
        self.assertEqual(seen["ref"], CREATED["html_url"])

    def test_request_error_still_fails(self):
        result, seen = self._run_create("GraphQL: A pull request already exists.")
        self.assertFalse(result)
        self.assertNotIn("ref", seen)


if __name__ == "__main__":
    unittest.main()
