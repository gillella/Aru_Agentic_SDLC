#!/usr/bin/env python3
"""Mechanically resolve citations in a research findings artifact.

A research issue is Done only when every citation resolves. Unresolvable
citations fail closed. Network failures are reported as unresolved.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse

ARXIV_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)(?P<id>\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
DOI_RE = re.compile(
    r"(?:doi\.org/|doi:\s*)(?P<id>10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+", re.IGNORECASE)
MD_LINK_START_RE = re.compile(r"\[[^\]]*\]\(", re.IGNORECASE)
FINDING_LINE_RE = re.compile(r"^(?:\d+\.|[-*])\s+\S+")
REPO_CLAIM_RE = re.compile(
    r"^[-*]\s*(?:path|file|code)\s*:\s*(?P<path>\S+)\s*[—\-–]\s*"
    r"verified\s*:\s*(?P<date>\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE | re.MULTILINE,
)
MAX_ARXIV_BODY = 256 * 1024

Resolver = Callable[[str], Dict[str, Any]]


@dataclass
class CitationResult:
    identifier: str
    kind: str
    ok: bool
    detail: str = ""


def _clean_doi(raw: str) -> str:
    doi = raw.rstrip(".,;")
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    return doi


def extract_markdown_link_urls(text: str) -> List[str]:
    """Extract Markdown link destinations, allowing balanced parentheses in URLs."""
    urls: List[str] = []
    for match in MD_LINK_START_RE.finditer(text):
        i = match.end()
        depth = 1
        while i < len(text) and depth:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            elif ch in "\n\r":
                break
            i += 1
        if depth != 0:
            continue
        dest = text[match.end():i].strip()
        if dest.startswith("<") and dest.endswith(">"):
            dest = dest[1:-1].strip()
        if dest.lower().startswith(("http://", "https://")):
            urls.append(dest.rstrip(".,;"))
    return urls


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

    link_urls = extract_markdown_link_urls(text)
    for url in link_urls:
        arxiv = ARXIV_RE.search(url)
        doi = DOI_RE.search(url)
        if arxiv:
            add("arxiv", arxiv.group("id"))
        elif doi:
            add("doi", _clean_doi(doi.group("id")))
        else:
            add("url", url)

    for match in ARXIV_RE.finditer(text):
        add("arxiv", match.group("id"))

    for match in DOI_RE.finditer(text):
        add("doi", _clean_doi(match.group("id")))

    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;")
        if ARXIV_RE.search(url) or DOI_RE.search(url):
            continue
        add("url", url)

    return found


def extract_finding_lines(text: str) -> List[str]:
    """Return claim lines under a Findings section (numbered or bulleted)."""
    lines = text.splitlines()
    in_findings = False
    claims: List[str] = []
    section_re = re.compile(r"^#{1,6}\s*findings?\b", re.IGNORECASE)
    for line in lines:
        stripped = line.strip()
        if section_re.match(stripped):
            in_findings = True
            continue
        if in_findings and stripped.startswith("#"):
            break
        if not in_findings:
            continue
        if FINDING_LINE_RE.match(stripped):
            claims.append(stripped)
    return claims


def finding_has_citation(line: str) -> bool:
    return bool(
        extract_markdown_link_urls(line)
        or ARXIV_RE.search(line)
        or DOI_RE.search(line)
        or URL_RE.search(line)
    )


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
    invalid_dates = []
    for claim in dated:
        try:
            verified_date = date.fromisoformat(claim["verified"])
            if verified_date > date.today():
                invalid_dates.append(claim)
        except ValueError:
            invalid_dates.append(claim)
    return {
        "dated": dated,
        "missing_date": missing,
        "invalid_dates": invalid_dates,
    }


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(ip.is_global)


def resolve_public_addresses(host: str, port: int) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"dns:{exc}") from exc
    if not infos:
        raise ValueError("dns_empty")
    addresses: List[str] = []
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        if not is_public_ip(addr):
            raise ValueError(f"private_address:{addr}")
        if addr not in addresses:
            addresses.append(addr)
    return addresses


def assert_public_url(url: str) -> List[str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    host = parsed.hostname
    if not host:
        raise ValueError("missing_host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return resolve_public_addresses(host, port)


def _request_path(parsed) -> str:
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a validated IP with SNI for the original hostname."""

    def __init__(self, ip_address: str, *, server_hostname: str, **kwargs):
        self._server_hostname = server_hostname
        super().__init__(ip_address, **kwargs)

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            target = self.sock
        else:
            target = sock
        context = self._context or ssl.create_default_context()
        self.sock = context.wrap_socket(target, server_hostname=self._server_hostname)


def default_http_get(
    url: str,
    timeout: float = 20.0,
    *,
    read_body: bool = True,
    max_body: int = MAX_ARXIV_BODY,
    max_redirects: int = 5,
) -> Dict[str, Any]:
    """GET a public URL by connecting to a validated IP (DNS-rebinding safe)."""
    deadline = time.monotonic() + timeout
    current = url
    for _ in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request_deadline")
        addresses = assert_public_url(current)
        parsed = urlparse(current)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = _request_path(parsed)
        headers = {
            "User-Agent": "aru-verify-citations/1.0",
            "Host": host if parsed.port is None else f"{host}:{parsed.port}",
            "Accept": "*/*",
        }
        last_error: Optional[BaseException] = None
        redirected = False
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request_deadline")
            try:
                if parsed.scheme == "https":
                    conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                        address,
                        server_hostname=host,
                        port=port,
                        timeout=remaining,
                        context=ssl.create_default_context(),
                    )
                else:
                    conn = http.client.HTTPConnection(
                        address, port=port, timeout=remaining
                    )
                try:
                    conn.request("GET", path, headers=headers)
                    resp = conn.getresponse()
                    status = resp.status
                    location = resp.getheader("Location")
                    if status in {301, 302, 303, 307, 308} and location:
                        resp.read()
                        current = urljoin(current, location)
                        redirected = True
                        break
                    if not read_body:
                        # Drain nothing useful; status is enough for URL/DOI checks.
                        while resp.read(8192):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("request_deadline")
                            break
                        return {"status": status, "body": ""}
                    chunks: List[bytes] = []
                    total = 0
                    while total < max_body:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("request_deadline")
                        chunk = resp.read(min(8192, max_body - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    return {
                        "status": status,
                        "body": b"".join(chunks).decode("utf-8", errors="replace"),
                    }
                finally:
                    conn.close()
            except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError, ValueError) as exc:
                last_error = exc
                continue
        if redirected:
            continue
        if last_error is not None:
            raise last_error
        raise URLError("no_public_address")
    raise URLError("too_many_redirects")


def resolve_arxiv(arxiv_id: str, http_get: Optional[Resolver] = None) -> CitationResult:
    getter = http_get or default_http_get
    api = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        payload = getter(api)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        return CitationResult(arxiv_id, "arxiv", False, f"network:{type(exc).__name__}")
    body = payload.get("body") or ""
    if f"<id>http://arxiv.org/abs/{arxiv_id}" in body or (
        "<entry>" in body and arxiv_id in body
    ):
        return CitationResult(arxiv_id, "arxiv", True, "resolved")
    return CitationResult(arxiv_id, "arxiv", False, "not_found")


def resolve_doi(doi: str, http_get: Optional[Resolver] = None) -> CitationResult:
    url = f"https://doi.org/{doi}"
    try:
        if http_get is None:
            payload = default_http_get(url, read_body=False)
        else:
            payload = http_get(url)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        if isinstance(exc, HTTPError) and exc.code == 404:
            return CitationResult(doi, "doi", False, "not_found")
        if isinstance(exc, ValueError):
            return CitationResult(doi, "doi", False, str(exc))
        return CitationResult(doi, "doi", False, f"network:{type(exc).__name__}")
    status = int(payload.get("status") or 0)
    if status and status >= 400:
        return CitationResult(doi, "doi", False, f"http_{status}")
    return CitationResult(doi, "doi", True, "resolved")


def resolve_url(url: str, http_get: Optional[Resolver] = None) -> CitationResult:
    try:
        if http_get is None:
            payload = default_http_get(url, read_body=False)
        else:
            payload = http_get(url)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        if isinstance(exc, ValueError):
            return CitationResult(url, "url", False, str(exc))
        if isinstance(exc, HTTPError):
            return CitationResult(url, "url", False, f"http_{exc.code}")
        return CitationResult(url, "url", False, f"network:{type(exc).__name__}")
    status = int(payload.get("status") or 0)
    if status and status >= 400:
        return CitationResult(url, "url", False, f"http_{status}")
    return CitationResult(url, "url", True, "resolved")


def resolve_citation(
    citation: Dict[str, str], http_get: Optional[Resolver] = None
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
    findings = extract_finding_lines(text)
    uncited = [line for line in findings if not finding_has_citation(line)]
    citation_results_ok = bool(citations) and all(item.ok for item in results)
    findings_ok = bool(findings) and not uncited
    citation_ok = citation_results_ok and findings_ok
    repo_ok = not repo["missing_date"] and not repo["invalid_dates"]
    ok = citation_ok and repo_ok
    return {
        "ok": ok,
        "citation_ok": citation_ok,
        "repo_ok": repo_ok,
        "citations": [asdict(item) for item in results],
        "repo_claims": repo,
        "findings": findings,
        "uncited_findings": uncited,
        "errors": [
            *(f"unresolved:{item.kind}:{item.identifier}:{item.detail}" for item in results if not item.ok),
            *(f"missing_verification_date:{line}" for line in repo["missing_date"]),
            *(f"invalid_verification_date:{c['path']}:{c['verified']}" for c in repo["invalid_dates"]),
            *(["no_citations_found"] if not citations else []),
            *(["no_findings_section"] if not findings else []),
            *(f"uncited_finding:{line}" for line in uncited),
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
