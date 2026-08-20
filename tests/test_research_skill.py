# line-ceiling: 729
import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_citations import (  # noqa: E402
    assert_public_url,
    default_http_get,
    extract_citations,
    extract_repo_claims,
    main,
    resolve_arxiv,
    resolve_doi,
    resolve_url,
    verify_findings,
)


SAMPLE = """# Research findings: citation checks

## Question
Do the identifiers resolve?

## Findings
1. [external] Agentic PRs are studied ([paper](https://arxiv.org/abs/2605.22534)).
2. [external] External note at https://example.com/factory-note
3. [external] DOI item ([source](https://doi.org/10.1234/example.item))

## Citations
- arXiv:2605.22534
- https://example.com/factory-note
- doi:10.1234/example.item

## Repo code claims
- path: scripts/verify_citations.py — verified: 2026-08-14
"""


class FakeHttp:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, url: str, timeout: float = 20.0):
        self.calls.append(url)
        if url not in self.mapping:
            raise HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return value


class ResearchSkillTests(unittest.TestCase):
    def test_skill_creates_worktree_before_repository_artifact_write(self):
        skill = (ROOT / "skills" / "research" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            skill.index("create_branch.py"),
            skill.index("### 3. Investigate and write findings"),
        )
        self.assertIn("before the first repository write", skill)
        self.assertIn("comment-only artifact", skill)
        self.assertIn("outside the checkout", skill)
        self.assertIn("--repo-root <consumer-repo-root>", skill)

    def test_extract_citations_dedupes_kinds(self):
        cites = extract_citations(SAMPLE)
        kinds = {(c["kind"], c["identifier"]) for c in cites}
        self.assertIn(("arxiv", "2605.22534"), kinds)
        self.assertIn(("url", "https://example.com/factory-note"), kinds)
        self.assertIn(("doi", "10.1234/example.item"), kinds)

    def test_doi_from_markdown_link_strips_delimiter(self):
        text = "See [source](https://doi.org/10.1234/example)"
        cites = extract_citations(text)
        self.assertEqual(cites[0]["identifier"], "10.1234/example")

    def test_verify_findings_passes_with_resolvable_citations(self):
        http = FakeHttp(
            {
                "https://export.arxiv.org/api/query?id_list=2605.22534": {
                    "status": 200,
                    "body": "<feed><entry><id>http://arxiv.org/abs/2605.22534</id></entry></feed>",
                },
                "https://example.com/factory-note": {"status": 200, "body": "ok"},
                "https://doi.org/10.1234/example.item": {"status": 200, "body": "ok"},
            }
        )
        report = verify_findings(SAMPLE, http_get=http)
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["citation_ok"])
        self.assertTrue(report["repo_ok"])

    def test_unresolvable_citation_fails(self):
        http = FakeHttp(
            {
                "https://export.arxiv.org/api/query?id_list=2605.22534": {
                    "status": 200,
                    "body": "<feed></feed>",
                },
                "https://example.com/factory-note": {"status": 200, "body": "ok"},
                "https://doi.org/10.1234/example.item": {"status": 200, "body": "ok"},
            }
        )
        report = verify_findings(SAMPLE, http_get=http)
        self.assertFalse(report["ok"])
        self.assertTrue(any("unresolved:arxiv" in err for err in report["errors"]))

    def test_only_final_2xx_status_resolves(self):
        matching_error = FakeHttp(
            {
                "https://export.arxiv.org/api/query?id_list=2605.22534": {
                    "status": 500,
                    "body": "<entry><id>http://arxiv.org/abs/2605.22534</id></entry>",
                }
            }
        )
        self.assertEqual(
            resolve_arxiv("2605.22534", http_get=matching_error).detail,
            "http_500",
        )
        redirect = FakeHttp(
            {
                "https://example.com/a": {"status": 302, "body": ""},
                "https://doi.org/10.1234/example": {"status": 302, "body": ""},
            }
        )
        self.assertEqual(
            resolve_url("https://example.com/a", http_get=redirect).detail,
            "http_302",
        )
        self.assertEqual(
            resolve_doi("10.1234/example", http_get=redirect).detail,
            "http_302",
        )

    def test_missing_citation_identifier_fails(self):
        text = "# Findings\n\nNo sources here.\n"
        report = verify_findings(text, http_get=FakeHttp({}))
        self.assertFalse(report["ok"])
        self.assertIn("no_citations_found", report["errors"])

    def test_uncited_finding_fails(self):
        text = """## Findings
1. [external] Unsupported claim with no citation.
2. [external] Supported ([ok](https://example.com/a))

## Citations
- https://example.com/a
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": "ok"}})
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["ok"])
        self.assertTrue(any("uncited_finding:" in err for err in report["errors"]))

    def test_private_url_is_rejected(self):
        with self.assertRaises(ValueError):
            assert_public_url("http://127.0.0.1/secret")
        report = verify_findings(
            "## Findings\n1. [external] local ([x](http://127.0.0.1/x))\n\n## Citations\n- http://127.0.0.1/x\n",
            http_get=default_rejecting_http,
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any("private_address" in err or "ValueError" in err or "unresolved:url" in err for err in report["errors"]))

    def test_repo_claim_requires_verification_date(self):
        text = SAMPLE.replace(
            "- path: scripts/verify_citations.py — verified: 2026-08-14",
            "- path: scripts/verify_citations.py",
        )
        http = FakeHttp(
            {
                "https://export.arxiv.org/api/query?id_list=2605.22534": {
                    "status": 200,
                    "body": "<feed><entry><id>http://arxiv.org/abs/2605.22534</id></entry></feed>",
                },
                "https://example.com/factory-note": {"status": 200, "body": "ok"},
                "https://doi.org/10.1234/example.item": {"status": 200, "body": "ok"},
            }
        )
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["repo_ok"])
        self.assertFalse(report["ok"])
        claims = extract_repo_claims(text)
        self.assertTrue(claims["missing_date"])

    def test_invalid_calendar_date_fails(self):
        text = SAMPLE.replace("2026-08-14", "2026-99-99")
        http = FakeHttp(
            {
                "https://export.arxiv.org/api/query?id_list=2605.22534": {
                    "status": 200,
                    "body": "<feed><entry><id>http://arxiv.org/abs/2605.22534</id></entry></feed>",
                },
                "https://example.com/factory-note": {"status": 200, "body": "ok"},
                "https://doi.org/10.1234/example.item": {"status": 200, "body": "ok"},
            }
        )
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["repo_ok"])
        self.assertTrue(any("invalid_verification_date" in err for err in report["errors"]))

    def test_cli_exit_codes(self):
        http_ok = FakeHttp(
            {
                "https://export.arxiv.org/api/query?id_list=2605.22534": {
                    "status": 200,
                    "body": "<feed><entry><id>http://arxiv.org/abs/2605.22534</id></entry></feed>",
                },
                "https://example.com/factory-note": {"status": 200, "body": "ok"},
                "https://doi.org/10.1234/example.item": {"status": 200, "body": "ok"},
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "findings.md"
            path.write_text(SAMPLE, encoding="utf-8")
            with patch("verify_citations.default_http_get", http_ok):
                self.assertEqual(main([str(path)]), 0)
                bad = Path(raw) / "bad.md"
                bad.write_text("# empty\n", encoding="utf-8")
                self.assertEqual(main([str(bad)]), 1)
            missing = Path(raw) / "missing.md"
            self.assertEqual(main([str(missing)]), 2)

    def test_cli_json_output(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "findings.md"
            path.write_text(
                "## Findings\n1. [external] note ([a](https://example.com/a))\n\n"
                "## Citations\n- https://example.com/a\n\n"
                "## Repo code claims\nnone\n",
                encoding="utf-8",
            )
            from io import StringIO
            from contextlib import redirect_stdout

            buf = StringIO()
            with patch(
                "verify_citations.default_http_get",
                FakeHttp({"https://example.com/a": {"status": 200, "body": "ok"}}),
            ), redirect_stdout(buf):
                code = main([str(path), "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["ok"])

    def test_markdown_doi_keeps_balanced_parentheses(self):
        text = "See [paper](https://doi.org/10.1000/example(part-a))"
        cites = extract_citations(text)
        self.assertEqual(cites[0]["kind"], "doi")
        self.assertEqual(cites[0]["identifier"], "10.1000/example(part-a)")

    def test_markdown_title_is_not_part_of_resolved_url(self):
        text = """## Findings
1. [external] Standard link ([source](https://example.com/a "Primary source")).

## Citations
- [source](https://example.com/a "Primary source")

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(http.calls, ["https://example.com/a"])

    def test_complex_registered_doi_resolves_without_truncation(self):
        doi = "10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P"
        url = f"https://doi.org/{doi}"
        text = f"""## Findings
1. [external] DOI result ([source]({url})).

## Citations
- doi:{doi}

## Repo code claims
none
"""
        http = FakeHttp({url: {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["citations"][0]["identifier"], doi)
        self.assertEqual(http.calls, [url])

    def test_future_verification_date_fails(self):
        text = SAMPLE.replace("2026-08-14", "2099-01-01")
        http = FakeHttp(
            {
                "https://export.arxiv.org/api/query?id_list=2605.22534": {
                    "status": 200,
                    "body": "<feed><entry><id>http://arxiv.org/abs/2605.22534</id></entry></feed>",
                },
                "https://example.com/factory-note": {"status": 200, "body": "ok"},
                "https://doi.org/10.1234/example.item": {"status": 200, "body": "ok"},
            }
        )
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["repo_ok"])
        self.assertTrue(any("invalid_verification_date" in err for err in report["errors"]))

    def test_cgnat_and_documentation_addresses_rejected(self):
        from verify_citations import is_public_ip

        self.assertFalse(is_public_ip("100.64.0.1"))
        self.assertFalse(is_public_ip("192.0.2.1"))
        with self.assertRaises(ValueError):
            assert_public_url("http://100.64.0.1/secret")

    def test_dns_rebinding_connects_to_validated_address_only(self):
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        ]
        seen = []

        class FakeResp:
            status = 200

            def getheader(self, name, default=None):
                return default

            def read(self, n=-1):
                return b""

        class FakeConn:
            def __init__(self, host, *args, **kwargs):
                seen.append(host)

            def request(self, *args, **kwargs):
                return None

            def getresponse(self):
                return FakeResp()

            def close(self):
                return None

        with patch("verify_citations.socket.getaddrinfo", return_value=infos), patch(
            "verify_citations.http.client.HTTPConnection", FakeConn
        ):
            payload = default_http_get("http://evil.example/path", read_body=False)
        self.assertEqual(payload["status"], 200)
        self.assertEqual(seen, ["8.8.8.8"])

    def test_dns_resolution_obeys_total_deadline(self):
        def slow_lookup(*args, **kwargs):
            time.sleep(0.08)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

        started = time.monotonic()
        with patch("verify_citations.socket.getaddrinfo", side_effect=slow_lookup):
            with self.assertRaises(TimeoutError):
                default_http_get("http://slow.example/", timeout=0.01)
        # Stay below the stub's 0.08-second delay while allowing scheduler
        # overhead on a loaded CI runner.
        self.assertLess(time.monotonic() - started, 0.075)

    def test_redirect_body_is_never_read(self):
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
        responses = []

        class FakeResp:
            def __init__(self, status, location=None):
                self.status = status
                self.location = location
                self.read_called = False

            def getheader(self, name, default=None):
                return self.location if name == "Location" else default

            def read(self, n=-1):
                self.read_called = True
                raise AssertionError("redirect or status-only body was consumed")

        class FakeConn:
            def __init__(self, *args, **kwargs):
                return None

            def request(self, *args, **kwargs):
                return None

            def getresponse(self):
                response = (
                    FakeResp(302, "http://next.example/")
                    if not responses
                    else FakeResp(200)
                )
                responses.append(response)
                return response

            def close(self):
                return None

        with patch("verify_citations.socket.getaddrinfo", return_value=infos), patch(
            "verify_citations.http.client.HTTPConnection", FakeConn
        ):
            payload = default_http_get("http://start.example/", read_body=False)
        self.assertEqual(payload, {"status": 200, "body": ""})
        self.assertEqual(len(responses), 2)
        self.assertFalse(any(response.read_called for response in responses))

    def test_prose_repository_claim_without_date_fails_closed(self):
        text = """## Findings
1. This repository routes research through scripts/fetch_next_work.py. ([source](https://example.com/a))

## Citations
- https://example.com/a

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["repo_ok"])
        self.assertTrue(
            any("unclassified_finding" in err for err in report["errors"])
        )

    def test_root_arbitrary_path_and_continuation_claims_fail_closed(self):
        claims = (
            "1. AGENTS.md defines the factory rules ([source](https://example.com/a)).",
            "1. src/router.py routes work ([source](https://example.com/a)).",
            "1. lib/router.py routes work ([source](https://example.com/a)).",
            "1. aru/router.py routes work ([source](https://example.com/a)).",
            "1. [external] Cited statement ([source](https://example.com/a)).\n"
            "   AGENTS.md defines this factory.",
        )
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        for claim in claims:
            with self.subTest(claim=claim):
                text = f"""## Findings
{claim}

## Citations
- https://example.com/a

## Repo code claims
none
"""
                report = verify_findings(text, http_get=http)
                self.assertFalse(report["ok"])
                self.assertTrue(report["unclassified_findings"])

    def test_nested_heading_cannot_hide_unclassified_finding(self):
        text = """## Findings
1. [external] Supported fact ([source](https://example.com/a)).
### Additional findings
AGENTS.md defines the factory.

## Citations
- https://example.com/a

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["ok"])
        self.assertIn("### Additional findings", report["findings"])
        self.assertIn("AGENTS.md defines the factory.", report["findings"])
        self.assertTrue(report["unclassified_findings"])

    def test_nested_heading_title_cannot_hide_factual_claim(self):
        headings = (
            "### AGENTS.md defines the factory",
            "### Finding AGENTS.md defines the factory",
            "### Findings AGENTS.md defines the factory",
            "### Findings",
        )
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        for heading in headings:
            with self.subTest(heading=heading):
                text = f"""## Findings
1. [external] Supported fact ([source](https://example.com/a)).
{heading}

## Citations
- https://example.com/a

## Repo code claims
none
"""
                report = verify_findings(text, http_get=http)
                self.assertFalse(report["ok"])
                self.assertIn(heading, report["findings"])
                self.assertTrue(report["unclassified_findings"])

    def test_duplicate_findings_sections_fail_closed(self):
        text = """## Findings
1. [external] Supported fact ([source](https://example.com/a)).

## Findings
1. AGENTS.md defines the factory.

## Citations
- https://example.com/a

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["ok"])
        self.assertEqual(report["findings_section_count"], 2)
        self.assertIn("duplicate_findings_sections", report["errors"])

    def test_external_marker_cannot_relabel_source_path_claim(self):
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        for path in ("AGENTS.md", "src/router.py"):
            with self.subTest(path=path):
                text = f"""## Findings
1. [external] {path} defines routing ([source](https://example.com/a)).

## Citations
- https://example.com/a

## Repo code claims
none
"""
                report = verify_findings(text, http_get=http)
                self.assertFalse(report["ok"])
                self.assertTrue(report["external_source_findings"])
                self.assertTrue(
                    any(
                        "external_finding_references_source_path" in error
                        for error in report["errors"]
                    )
                )

    def test_external_doi_identifier_is_not_mistaken_for_source_path(self):
        text = """## Findings
1. [external] Published result doi:10.1234/example.item

## Citations
- doi:10.1234/example.item

## Repo code claims
none
"""
        http = FakeHttp(
            {"https://doi.org/10.1234/example.item": {"status": 200, "body": ""}}
        )
        report = verify_findings(text, http_get=http)
        self.assertTrue(report["ok"], report["errors"])

    def test_source_path_detection_covers_build_files_without_prose_false_positives(self):
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        source_paths = (
            "BUILD",
            "WORKSPACE",
            "Gemfile",
            "Podfile",
            "go.mod",
            "requirements.txt",
            ".gitignore",
            "CMakeLists.txt",
            "README",
            "src/",
            "requirements-dev.txt",
            "requirements-slack.txt",
            "sdlc_flow_visualizer/index.html",
            "styles.css",
        )
        prose = (
            "CI/CD",
            "input/output",
            "and/or",
            "client/server",
            "app/store",
            "2026/08/14",
        )
        for value in source_paths + prose:
            with self.subTest(value=value):
                text = f"""## Findings
1. [external] {value} observation ([source](https://example.com/a)).

## Citations
- https://example.com/a

## Repo code claims
none
"""
                report = verify_findings(text, http_get=http)
                self.assertEqual(
                    report["ok"],
                    value in prose,
                    report["errors"],
                )

    def test_markdown_citation_label_is_not_a_source_path(self):
        text = """## Findings
1. [external] External project documentation ([README](https://example.com/a)).

## Citations
- https://example.com/a

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertTrue(report["ok"], report["errors"])

    def test_outside_repo_requires_explicit_repository_identity(self):
        text = """## Findings
1. [external] requirements-dev.txt defines dependencies ([source](https://example.com/a)).

## Citations
- https://example.com/a

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        with tempfile.TemporaryDirectory() as raw, patch(
            "verify_citations.Path.cwd", return_value=Path(raw)
        ):
            unknown = verify_findings(text, http_get=http)
            self.assertFalse(unknown["ok"])
            self.assertFalse(unknown["repo_identity_known"])
            self.assertIn("repository_identity_unavailable", unknown["errors"])

            explicit = verify_findings(text, http_get=http, repo_root=ROOT)
            self.assertFalse(explicit["ok"])
            self.assertTrue(explicit["repo_identity_known"])
            self.assertTrue(explicit["external_source_findings"])

    def test_structured_repo_finding_with_date_and_path_passes(self):
        text = """## Findings
1. [repo verified: 2026-08-14] AGENTS.md defines the factory rules ([source](https://example.com/a)).

## Citations
- https://example.com/a

## Repo code claims
- path: AGENTS.md — verified: 2026-08-14
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertTrue(report["ok"], report["errors"])

    def test_repo_finding_requires_matching_section_path_and_date(self):
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        sections = (
            "- path: scripts/other.py — verified: 2026-08-14",
            "- path: AGENTS.md — verified: 2026-08-13",
        )
        for section in sections:
            with self.subTest(section=section):
                text = f"""## Findings
1. [repo verified: 2026-08-14] AGENTS.md defines the rules ([source](https://example.com/a)).

## Citations
- https://example.com/a

## Repo code claims
{section}
"""
                report = verify_findings(text, http_get=http)
                self.assertFalse(report["ok"])
                self.assertTrue(report["repo_path_binding_errors"])

    def test_repo_evidence_outside_section_does_not_satisfy_finding(self):
        text = """## Findings
1. [repo verified: 2026-08-14] AGENTS.md defines the rules ([source](https://example.com/a)).

## Citations
- https://example.com/a
- path: AGENTS.md — verified: 2026-08-14

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["ok"])
        self.assertEqual(report["repo_claims"]["dated"], [])
        self.assertTrue(report["repo_path_binding_errors"])

    def test_duplicate_repo_claim_sections_fail_closed(self):
        text = """## Findings
1. [repo verified: 2026-08-14] AGENTS.md defines the rules ([source](https://example.com/a)).

## Citations
- https://example.com/a

## Repo code claims
- path: AGENTS.md — verified: 2026-08-14

## Repo code claims
none
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["ok"])
        self.assertEqual(report["repo_claims"]["section_count"], 2)
        self.assertIn("duplicate_repo_code_claims_sections", report["errors"])

    def test_repo_claims_section_requires_dated_entries_or_none(self):
        text = """## Findings
1. [external] External fact ([source](https://example.com/a)).

## Citations
- https://example.com/a
"""
        http = FakeHttp({"https://example.com/a": {"status": 200, "body": ""}})
        report = verify_findings(text, http_get=http)
        self.assertFalse(report["repo_ok"])
        self.assertIn("missing_repo_code_claims_acknowledgement", report["errors"])


def default_rejecting_http(url: str, timeout: float = 20.0):
    assert_public_url(url)
    raise AssertionError("should not connect")


if __name__ == "__main__":
    unittest.main()
