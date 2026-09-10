"""Changed-path risk classification for proportional Aru review gates."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable

_DOC_SUFFIXES = {".adoc", ".md", ".rst", ".txt"}
_CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".java",
    ".js", ".jsx", ".kt", ".kts", ".m", ".mm", ".php", ".py", ".pyi",
    ".rb", ".rs", ".scss", ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue",
}
_TIER_2_RE = re.compile(
    r"(?:^|[./_-])(?:auth(?:entication|orization)?|security|permissions?|secrets?|"
    r"migrations?|trading|payments?|billing|infra(?:structure)?|terraform|k8s|"
    r"kubernetes|dependencies?|config(?:uration)?)(?:[./_-]|$)",
    re.IGNORECASE,
)
_TIER_3_RE = re.compile(
    r"(?:^|/)(?:prod(?:uction)?|destructive|deploy(?:ment)?|rollback|revert)"
    r"(?:[./_-]|$)|(?:^|[._-])(?:prod|production)(?:[._-]|$)",
    re.IGNORECASE,
)
_DEPENDENCY_FILES = {
    "cargo.lock", "cargo.toml", "gemfile", "gemfile.lock", "go.mod", "go.sum",
    "package-lock.json", "package.json", "pnpm-lock.yaml", "poetry.lock",
    "pyproject.toml", "yarn.lock",
}
_KERNEL_POLICY_DOCS = {
    "docs/degraded-mode.md",
    "docs/enforcement-register.md",
    "docs/kernel-contract.md",
    "docs/operations.md",
}


def _dependency_file(name: str) -> bool:
    return name in _DEPENDENCY_FILES or (
        name.startswith("requirements") and name.endswith(".txt")
    )


def _sensitive_contract_path(normalized: str, name: str) -> bool:
    return bool(
        name in {"agents.md", "claude.md", "copilot-instructions.md", "codeowners"}
        or normalized == ".github/pull_request_template.md"
        or normalized == "integrations/hermes/install.py"
        or normalized in _KERNEL_POLICY_DOCS
        or normalized.startswith(
            (
                ".agents/", ".aru/", ".codex/", ".cursor/rules/",
                ".github/issue_template/", ".github/pull_request_template/",
                ".github/workflows/", "hooks/", "skills/", "templates/", "scripts/",
                "integrations/hermes/aru_project_driver/", "integrations/hermes/skill/",
            )
        )
        or "/skills/" in normalized
        or _TIER_2_RE.search(normalized)
        or _dependency_file(name)
    )


def review_risk_tier(paths: Iterable[str]) -> int:
    """Classify changed paths from docs-only (0) through destructive (3)."""
    tiers: list[int] = []
    for value in paths:
        raw = str(value).strip()
        path = PurePosixPath(raw)
        if not raw or "\\" in raw or raw.startswith(("/", "~", "-")) or ".." in path.parts:
            tiers.append(3)
            continue
        normalized = path.as_posix().lower()
        name = path.name.lower()
        if _TIER_3_RE.search(normalized):
            tiers.append(3)
        elif _sensitive_contract_path(normalized, name):
            tiers.append(2)
        elif path.suffix.lower() in _DOC_SUFFIXES or name in {"readme", "changelog", "license"}:
            tiers.append(0)
        elif path.suffix.lower() in _CODE_SUFFIXES:
            tiers.append(1)
        else:
            tiers.append(2)
    return max(tiers, default=3)
