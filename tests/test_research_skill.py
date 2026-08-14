import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_citations import (  # noqa: E402
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
3. DOI item https://doi.org/10.1234/example.item

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


if __name__ == "__main__":
    unittest.main()
