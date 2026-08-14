import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_citations import (  # noqa: E402
    assert_public_url,
    extract_citations,
    extract_repo_claims,
    main,
    verify_findings,
)


SAMPLE = """# Research findings: citation checks

## Question
Do the identifiers resolve?

## Findings
1. Agentic PRs are studied ([paper](https://arxiv.org/abs/2605.22534)).
2. External note at https://example.com/factory-note
3. DOI item ([source](https://doi.org/10.1234/example.item))

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

    def test_missing_citation_identifier_fails(self):
        text = "# Findings\n\nNo sources here.\n"
        report = verify_findings(text, http_get=FakeHttp({}))
        self.assertFalse(report["ok"])
        self.assertIn("no_citations_found", report["errors"])

    def test_uncited_finding_fails(self):
        text = """## Findings
1. Unsupported claim with no citation.
2. Supported ([ok](https://example.com/a))

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
            "## Findings\n1. local ([x](http://127.0.0.1/x))\n\n## Citations\n- http://127.0.0.1/x\n",
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
                "## Findings\n1. note ([a](https://example.com/a))\n\n"
                "## Citations\n- https://example.com/a\n",
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
        from verify_citations import default_http_get

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


def default_rejecting_http(url: str, timeout: float = 20.0):
    assert_public_url(url)
    raise AssertionError("should not connect")


if __name__ == "__main__":
    unittest.main()
