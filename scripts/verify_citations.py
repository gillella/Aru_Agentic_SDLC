#!/usr/bin/env python3
"""Mechanically resolve citations in a research findings artifact.

A research issue is Done only when every citation resolves. Unresolvable
citations fail closed. Network failures are reported as unresolved.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ARXIV_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
DOI_RE = re.compile(
    r"(?:doi\.org/|doi:\s*)(?P<id>10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+", re.IGNORECASE)
MD_LINK_RE = re.compile(r"\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)", re.IGNORECASE)
REPO_CLAIM_RE = re.compile(
    r"^[-*]\s*(?:path|file|code)\s*:\s*(?P<path>\S+)\s*[—\-–]\s*"
    r"verified\s*:\s*(?P<date>\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE | re.MULTILINE,
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Resolver = Callable[[str], Dict[str, Any]]


@dataclass
class CitationResult:
    identifier: str
    kind: str
    ok: bool
    detail: str = ""


def extract_citations(text: str) -> List[Dict[str, str]]:
    """Return unique citation dicts with kind + identifier (order preserved)."""
    found: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, identifier: str) -> None:
        key = f"{kind}:{identifier.lower()}"
        if key in seen:
            return
        seen.add(key)
        found.append({"kind": kind, "identifier": identifier})

    for match in ARXIV_RE.finditer(text):
        add("arxiv", match.group("id"))

    for match in DOI_RE.finditer(text):
        add("doi", match.group("id").rstrip("."))

    link_urls = [m.group("url").rstrip(".,;") for m in MD_LINK_RE.finditer(text)]
    bare_urls = [m.group(0).rstrip(".,;") for m in URL_RE.finditer(text)]
    for url in link_urls + bare_urls:
        if ARXIV_RE.search(url) or DOI_RE.search(url):
            continue
        add("url", url)

    return found


def extract_repo_claims(text: str) -> Dict[str, Any]:
    dated = [
        {"path": m.group("path"), "verified": m.group("date")}
        for m in REPO_CLAIM_RE.finditer(text)
    ]
    missing: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not re.match(r"^[-*]\s*(?:path|file|code)\s*:", stripped, re.IGNORECASE):
            continue
        if REPO_CLAIM_RE.search(stripped):
            continue
        missing.append(stripped)
    return {"dated": dated, "missing_date": missing}


def default_http_get(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    req = Request(url, method="GET", headers={"User-Agent": "aru-verify-citations/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return {
            "status": getattr(resp, "status", 200),
            "body": body.decode("utf-8", errors="replace"),
        }


def resolve_arxiv(arxiv_id: str, http_get: Resolver = default_http_get) -> CitationResult:
    api = (
        "https://export.arxiv.org/api/query?"
        f"id_list={arxiv_id}"
    )
    try:
        payload = http_get(api)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        return CitationResult(arxiv_id, "arxiv", False, f"network:{type(exc).__name__}")
    body = payload.get("body") or ""
    if f"arXiv:/{arxiv_id}" in body or f">{arxiv_id}" in body or f"<id>http://arxiv.org/abs/{arxiv_id}" in body:
        return CitationResult(arxiv_id, "arxiv", True, "resolved")
    if "<entry>" in body and arxiv_id in body:
        return CitationResult(arxiv_id, "arxiv", True, "resolved")
    return CitationResult(arxiv_id, "arxiv", False, "not_found")


def resolve_doi(doi: str, http_get: Resolver = default_http_get) -> CitationResult:
    url = f"https://doi.org/{doi}"
    try:
        payload = http_get(url)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        # doi.org often 302; urllib follows. Treat HTTPError 404 as miss.
        if isinstance(exc, HTTPError) and exc.code == 404:
            return CitationResult(doi, "doi", False, "not_found")
        return CitationResult(doi, "doi", False, f"network:{type(exc).__name__}")
    status = int(payload.get("status") or 0)
    if status and status >= 400:
        return CitationResult(doi, "doi", False, f"http_{status}")
    return CitationResult(doi, "doi", True, "resolved")


def resolve_url(url: str, http_get: Resolver = default_http_get) -> CitationResult:
    try:
        payload = http_get(url)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        if isinstance(exc, HTTPError):
            return CitationResult(url, "url", False, f"http_{exc.code}")
        return CitationResult(url, "url", False, f"network:{type(exc).__name__}")
    status = int(payload.get("status") or 0)
    if status and status >= 400:
        return CitationResult(url, "url", False, f"http_{status}")
    return CitationResult(url, "url", True, "resolved")


def resolve_citation(
    citation: Dict[str, str], http_get: Resolver = default_http_get
) -> CitationResult:
    kind = citation["kind"]
    ident = citation["identifier"]
    if kind == "arxiv":
        return resolve_arxiv(ident, http_get=http_get)
    if kind == "doi":
        return resolve_doi(ident, http_get=http_get)
    return resolve_url(ident, http_get=http_get)


def verify_findings(
    text: str, http_get: Optional[Resolver] = None
) -> Dict[str, Any]:
    getter = http_get or default_http_get
    citations = extract_citations(text)
    results = [resolve_citation(item, http_get=getter) for item in citations]
    repo = extract_repo_claims(text)
    invalid_dates = [
        claim
        for claim in repo["dated"]
        if not DATE_RE.match(claim["verified"])
    ]
    citation_ok = bool(citations) and all(item.ok for item in results)
    repo_ok = not repo["missing_date"] and not invalid_dates
    # Repo claims section is optional; only fail when present without dates.
    ok = citation_ok and repo_ok
    return {
        "ok": ok,
        "citation_ok": citation_ok,
        "repo_ok": repo_ok,
        "citations": [asdict(item) for item in results],
        "repo_claims": repo,
        "errors": [
            *(f"unresolved:{item.kind}:{item.identifier}:{item.detail}" for item in results if not item.ok),
            *(f"missing_verification_date:{line}" for line in repo["missing_date"]),
            *(f"invalid_verification_date:{c['path']}" for c in invalid_dates),
            *(["no_citations_found"] if not citations else []),
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify research citation identifiers.")
    parser.add_argument("path", type=Path, help="Path to findings markdown")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.path.is_file():
        print(f"[ERROR] findings file not found: {args.path}", file=sys.stderr)
        return 2
    text = args.path.read_text(encoding="utf-8")
    report = verify_findings(text)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for err in report["errors"]:
            print(f"[FAIL] {err}", file=sys.stderr)
        print(
            f"citations={len(report['citations'])} "
            f"ok={report['ok']} citation_ok={report['citation_ok']} "
            f"repo_ok={report['repo_ok']}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
