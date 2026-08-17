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
import queue
import re
import socket
import ssl
import sys
import threading
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
    r"(?:doi\.org/|doi:\s*)(?P<id>10\.\d{4,9}/[-._;()/:<>A-Z0-9]+)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+", re.IGNORECASE)
MD_LINK_START_RE = re.compile(r"\[[^\]]*\]\(", re.IGNORECASE)
FINDING_SCOPE_RE = re.compile(
    r"^(?:\d+\.|[-*])\s+\[(?P<scope>external|repo\s+verified\s*:\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2}))\]\s+\S+",
    re.IGNORECASE,
)
REPO_CLAIM_RE = re.compile(
    r"^[-*]\s*(?:path|file|code)\s*:\s*(?P<path>\S+)\s*[—\-–]\s*"
    r"verified\s*:\s*(?P<date>\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE | re.MULTILINE,
)
PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?"
    r"(?![A-Za-z0-9_.-])"
)
SOURCE_SUFFIXES = {
    ".bash",
    ".c",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
    ".zsh",
}
SOURCE_ROOTS = {
    ".devcontainer",
    ".github",
    "app",
    "apps",
    "aru",
    "cmd",
    "config",
    "configs",
    "docs",
    "hooks",
    "include",
    "internal",
    "lib",
    "packages",
    "prompts",
    "resources",
    "scripts",
    "skills",
    "src",
    "templates",
    "tests",
}
SOURCE_BASENAMES = {
    ".dockerignore",
    ".gitignore",
    "AGENTS.md",
    "BUILD",
    "CMakeLists.txt",
    "Dockerfile",
    "Gemfile",
    "LICENSE",
    "Makefile",
    "Podfile",
    "README",
    "WORKSPACE",
    "go.mod",
    "go.sum",
    "package-lock.json",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
}
SOURCE_BASENAMES_CASEFOLD = {name.casefold() for name in SOURCE_BASENAMES}
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


def extract_markdown_link_urls(text: str) -> List[str]:  # noqa: C901, PLR0912, PLR0915
    """Extract Markdown destinations, excluding an optional link title."""
    urls: List[str] = []
    for match in MD_LINK_START_RE.finditer(text):
        i = match.end()
        depth = 1
        quote: Optional[str] = None
        top_level_space = False
        while i < len(text) and depth:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if quote:
                if ch == quote:
                    quote = None
            elif top_level_space and ch in {'"', "'"}:
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            elif ch in "\n\r":
                break
            elif ch.isspace() and depth == 1:
                top_level_space = True
            i += 1
        if depth != 0:
            continue
        content = text[match.end():i].strip()
        if content.startswith("<"):
            close = content.find(">")
            if close < 0:
                continue
            dest = content[1:close].strip()
        else:
            nested = 0
            end = len(content)
            escaped = False
            for offset, ch in enumerate(content):
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                elif ch == "(":
                    nested += 1
                elif ch == ")" and nested:
                    nested -= 1
                elif ch.isspace() and nested == 0:
                    end = offset
                    break
            dest = content[:end].strip()
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
    """Return every non-empty claim line under a Findings section."""
    lines = text.splitlines()
    in_findings = False
    findings_level = 0
    claims: List[str] = []
    heading_re = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
    for line in lines:
        stripped = line.strip()
        heading = heading_re.match(stripped)
        if heading:
            level = len(heading.group("marks"))
            title = heading.group("title")
            if in_findings:
                if level <= findings_level:
                    break
                # Nested headings are content inside Findings. Treat them as
                # claims so their titles cannot hide facts from validation.
                claims.append(stripped)
                continue
            if re.fullmatch(r"findings?", title, re.IGNORECASE):
                in_findings = True
                findings_level = level
                continue
        if not in_findings:
            continue
        if stripped and not stripped.startswith("<!--"):
            claims.append(stripped)
    return claims


def finding_has_citation(line: str) -> bool:
    return bool(
        extract_markdown_link_urls(line)
        or ARXIV_RE.search(line)
        or DOI_RE.search(line)
        or URL_RE.search(line)
    )


def _strip_markdown_links(text: str) -> str:
    """Remove complete Markdown links so labels are not treated as evidence."""
    chars = list(text)
    for match in MD_LINK_START_RE.finditer(text):
        index = match.end()
        depth = 1
        quote: Optional[str] = None
        while index < len(text) and depth:
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if quote:
                if char == quote:
                    quote = None
            elif char in {'"', "'"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char in "\n\r":
                break
            index += 1
        if depth == 0:
            for offset in range(match.start(), index):
                chars[offset] = " "
    return "".join(chars)


def _find_repo_root(start: Path) -> Optional[Path]:
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _repository_path_exists(path: str, repo_root: Optional[Path]) -> bool:
    if repo_root is None:
        return False
    parts = Path(path).parts
    if not parts or ".." in parts:
        return False
    return repo_root.joinpath(*parts).exists()


def source_path_references(
    line: str, *, repo_root: Optional[Path] = None
) -> List[str]:
    """Return repository-like source paths, excluding citation destinations."""
    scrubbed = _strip_markdown_links(line)
    scrubbed = URL_RE.sub("", scrubbed)
    scrubbed = ARXIV_RE.sub("", scrubbed)
    scrubbed = DOI_RE.sub("", scrubbed)
    root = repo_root if repo_root is not None else _find_repo_root(Path.cwd())
    references: List[str] = []
    for match in PATH_TOKEN_RE.finditer(scrubbed):
        path = match.group(0).strip("`'\"()[]{} ,;:").rstrip(".")
        while path.startswith("./"):
            path = path[2:]
        candidate = path.rstrip("/")
        if not candidate:
            continue
        parts = candidate.split("/")
        basename = parts[-1]
        suffix = Path(basename).suffix.lower()
        looks_like_source = (
            basename.casefold() in SOURCE_BASENAMES_CASEFOLD
            or (
                basename.casefold().startswith("requirements")
                and suffix == ".txt"
            )
            or suffix in SOURCE_SUFFIXES
            or (path.endswith("/") and parts[0].lower() in SOURCE_ROOTS)
            or _repository_path_exists(candidate, root)
        )
        if looks_like_source and candidate not in references:
            path = candidate
            references.append(path)
    return references


def _extract_sections(text: str, title_pattern: str) -> List[str]:
    heading_re = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
    lines = text.splitlines()
    sections: List[str] = []
    starts: List[tuple[int, int]] = []
    for index, line in enumerate(lines):
        heading = heading_re.match(line.strip())
        if heading and re.fullmatch(title_pattern, heading.group("title"), re.I):
            starts.append((index + 1, len(heading.group("marks"))))
    for start, level in starts:
        end = len(lines)
        for index in range(start, len(lines)):
            heading = heading_re.match(lines[index].strip())
            if heading and len(heading.group("marks")) <= level:
                end = index
                break
        sections.append("\n".join(lines[start:end]))
    return sections


def extract_repo_claims(text: str) -> Dict[str, Any]:
    sections = _extract_sections(text, r"repo(?:sitory)?\s+code\s+claims?")
    section = "\n".join(sections)
    dated = [
        {
            "path": m.group("path").strip("`'\".,;:"),
            "verified": m.group("date"),
        }
        for m in REPO_CLAIM_RE.finditer(section)
    ]
    missing: List[str] = []
    for line in section.splitlines():
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
    explicit_none = any(
        line.strip().lower() in {"none", "- none", "* none"}
        for line in section.splitlines()
    )
    mixed_none_and_entries = explicit_none and bool(dated or missing)
    duplicate_sections = len(sections) > 1
    missing_acknowledgement = not sections or (
        not dated and not missing and not explicit_none
    )
    return {
        "dated": dated,
        "missing_date": missing,
        "invalid_dates": invalid_dates,
        "section_present": bool(sections),
        "section_count": len(sections),
        "duplicate_sections": duplicate_sections,
        "explicit_none": explicit_none,
        "mixed_none_and_entries": mixed_none_and_entries,
        "missing_acknowledgement": missing_acknowledgement,
    }


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(ip.is_global)


def resolve_public_addresses(  # noqa: C901
    host: str, port: int, *, deadline: Optional[float] = None
) -> List[str]:
    def lookup() -> Any:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)

    try:
        if deadline is None:
            infos = lookup()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request_deadline")
            result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

            def run_lookup() -> None:
                try:
                    result.put((True, lookup()))
                except BaseException as exc:  # transported to the caller
                    result.put((False, exc))

            worker = threading.Thread(target=run_lookup, daemon=True)
            worker.start()
            worker.join(remaining)
            if worker.is_alive():
                raise TimeoutError("request_deadline")
            succeeded, value = result.get_nowait()
            if not succeeded:
                raise value
            infos = value
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


def assert_public_url(
    url: str, *, deadline: Optional[float] = None
) -> List[str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    host = parsed.hostname
    if not host:
        raise ValueError("missing_host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return resolve_public_addresses(host, port, deadline=deadline)


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


def default_http_get(  # noqa: C901, PLR0912, PLR0915
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
        addresses = assert_public_url(current, deadline=deadline)
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
                        current = urljoin(current, location)
                        redirected = True
                        break
                    if not read_body:
                        # Status is enough for URL/DOI checks. Closing the
                        # connection avoids consuming an untrusted body.
                        return {"status": status, "body": ""}
                    chunks: List[bytes] = []
                    total = 0
                    while total < max_body:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("request_deadline")
                        sock = getattr(conn, "sock", None)
                        if sock is not None:
                            sock.settimeout(remaining)
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
    status = int(payload.get("status") or 0)
    if not 200 <= status < 300:
        return CitationResult(arxiv_id, "arxiv", False, f"http_{status}")
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
    if not 200 <= status < 300:
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
    if not 200 <= status < 300:
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
    text: str,
    http_get: Optional[Resolver] = None,
    *,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    getter = http_get or default_http_get
    citations = extract_citations(text)
    results = [resolve_citation(item, http_get=getter) for item in citations]
    repo = extract_repo_claims(text)
    findings = extract_finding_lines(text)
    findings_section_count = len(_extract_sections(text, r"findings?"))
    duplicate_findings_sections = findings_section_count > 1
    resolved_repo_root = (
        _find_repo_root(repo_root)
        if repo_root is not None
        else _find_repo_root(Path.cwd())
    )
    repo_identity_unknown = resolved_repo_root is None
    uncited = [line for line in findings if not finding_has_citation(line)]
    unclassified = []
    repo_findings = []
    invalid_repo_finding_dates = []
    external_source_findings = []
    repo_path_binding_errors = []
    dated_repo_paths = {
        (claim["path"].removeprefix("./"), claim["verified"])
        for claim in repo["dated"]
    }
    for line in findings:
        match = FINDING_SCOPE_RE.match(line)
        if not match:
            unclassified.append(line)
            continue
        references = source_path_references(line, repo_root=resolved_repo_root)
        if match.group("scope").lower() == "external":
            if references:
                external_source_findings.append(
                    {"finding": line, "paths": references}
                )
            continue
        repo_findings.append(line)
        verified_text = match.group("date")
        try:
            verified = date.fromisoformat(verified_text)
            if verified > date.today():
                invalid_repo_finding_dates.append(line)
        except ValueError:
            invalid_repo_finding_dates.append(line)
        if not references:
            repo_path_binding_errors.append(f"missing_path:{line}")
        for path in references:
            if (path, verified_text) not in dated_repo_paths:
                repo_path_binding_errors.append(
                    f"unmatched_path_or_date:{path}:{verified_text}:{line}"
                )
    repo_section_conflict = (bool(repo_findings) and repo["explicit_none"]) or repo[
        "mixed_none_and_entries"
    ]
    citation_results_ok = bool(citations) and all(item.ok for item in results)
    findings_ok = (
        findings_section_count == 1
        and bool(findings)
        and not uncited
        and not unclassified
    )
    citation_ok = citation_results_ok and findings_ok
    repo_ok = not (
        repo["missing_date"]
        or repo["invalid_dates"]
        or repo["missing_acknowledgement"]
        or repo["duplicate_sections"]
        or invalid_repo_finding_dates
        or repo_section_conflict
        or unclassified
        or external_source_findings
        or repo_path_binding_errors
        or repo_identity_unknown
    )
    ok = citation_ok and repo_ok
    return {
        "ok": ok,
        "citation_ok": citation_ok,
        "repo_ok": repo_ok,
        "citations": [asdict(item) for item in results],
        "repo_claims": repo,
        "findings": findings,
        "findings_section_count": findings_section_count,
        "duplicate_findings_sections": duplicate_findings_sections,
        "repo_identity_known": not repo_identity_unknown,
        "uncited_findings": uncited,
        "unclassified_findings": unclassified,
        "repo_findings": repo_findings,
        "invalid_repo_finding_dates": invalid_repo_finding_dates,
        "external_source_findings": external_source_findings,
        "repo_path_binding_errors": repo_path_binding_errors,
        "errors": [
            *(f"unresolved:{item.kind}:{item.identifier}:{item.detail}" for item in results if not item.ok),
            *(f"missing_verification_date:{line}" for line in repo["missing_date"]),
            *(f"invalid_verification_date:{c['path']}:{c['verified']}" for c in repo["invalid_dates"]),
            *(
                ["missing_repo_code_claims_acknowledgement"]
                if repo["missing_acknowledgement"]
                else []
            ),
            *(
                ["duplicate_repo_code_claims_sections"]
                if repo["duplicate_sections"]
                else []
            ),
            *(
                ["duplicate_findings_sections"]
                if duplicate_findings_sections
                else []
            ),
            *(
                ["repository_identity_unavailable"]
                if repo_identity_unknown
                else []
            ),
            *(f"unclassified_finding:{line}" for line in unclassified),
            *(
                f"invalid_repo_finding_date:{line}"
                for line in invalid_repo_finding_dates
            ),
            *(
                ["repo_finding_requires_dated_path_section"]
                if repo_section_conflict
                else []
            ),
            *(
                "external_finding_references_source_path:"
                f"{','.join(item['paths'])}:{item['finding']}"
                for item in external_source_findings
            ),
            *(
                f"repo_finding_path_binding:{error}"
                for error in repo_path_binding_errors
            ),
            *(["no_citations_found"] if not citations else []),
            *(["no_findings_section"] if findings_section_count == 0 else []),
            *(
                ["no_findings_found"]
                if findings_section_count == 1 and not findings
                else []
            ),
            *(f"uncited_finding:{line}" for line in uncited),
        ],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify research citation identifiers.")
    parser.add_argument("path", type=Path, help="Path to findings markdown")
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Governed repository whose code claims are being verified",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.path.is_file():
        print(f"[ERROR] findings file not found: {args.path}", file=sys.stderr)
        return 2
    text = args.path.read_text(encoding="utf-8")
    repo_root = None
    if args.repo_root is not None:
        repo_root = _find_repo_root(args.repo_root)
        if repo_root is None:
            print(
                f"[ERROR] repository root not found: {args.repo_root}",
                file=sys.stderr,
            )
            return 2
    else:
        repo_root = _find_repo_root(args.path.parent)
        if repo_root is None:
            repo_root = _find_repo_root(Path.cwd())
    report = verify_findings(text, repo_root=repo_root)
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
