#!/usr/bin/env python3
"""Pure, stable identity for one Aru coding-agent checkout.

Identity is derived only from machine, checkout, and model family.  It has no
presence-registry, board, network, or mutable-state dependency, so a restarted
desktop task deterministically recovers the same GitHub claim identity.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
from typing import Mapping, Optional

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
AGENT_ID_ENV_VAR = "ARU_AGENT_ID"
FINGERPRINT_LENGTH = 6

FAMILY_TO_PRODUCT = {
    "openai": "codex",
    "anthropic": "claude",
    "google": "antigravity",
    "cursor": "cursor",
}


def _machine_identifier() -> str:
    """Return a machine-local value that is stable across task restarts."""
    return platform.node() or "unknown-host"


def _checkout_root(repo_root: Optional[str] = None) -> str:
    """Resolve the checkout root instead of fingerprinting the launch cwd."""
    if repo_root:
        return os.path.realpath(repo_root)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return os.path.realpath(result.stdout.strip())
    except OSError:
        pass
    return os.path.realpath(os.getcwd())


def worker_fingerprint(
    repo_root: Optional[str] = None,
    family: str = "",
    machine: Optional[str] = None,
) -> str:
    """Hash machine, checkout, and family without exposing the checkout path."""
    parts = "\x00".join(
        [
            machine or _machine_identifier(),
            _checkout_root(repo_root),
            (family or "").strip().lower(),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def product_for_family(family: str) -> str:
    """Return the readable product prefix for a model family."""
    return FAMILY_TO_PRODUCT.get((family or "").strip().lower()) or "agent"


def fingerprint_agent_id(
    family: str = "",
    repo_root: Optional[str] = None,
    machine: Optional[str] = None,
) -> str:
    """Return the stable worker id, for example ``claude-a3f19c``."""
    return f"{product_for_family(family)}-{worker_fingerprint(repo_root, family, machine)}"


def configured_agent_id(env: Optional[Mapping[str, str]] = None) -> str:
    """Return an operator-pinned id, or an empty string when it is unset."""
    source = env if env is not None else os.environ
    return (source.get(AGENT_ID_ENV_VAR) or "").strip()


def resolve_agent_id(
    explicit: Optional[str] = None,
    *,
    family: str = "",
    env: Optional[Mapping[str, str]] = None,
    repo_root: Optional[str] = None,
    machine: Optional[str] = None,
) -> str:
    """Resolve explicit id, then environment override, then fingerprint."""
    if explicit is not None:
        return explicit.strip()
    pinned = configured_agent_id(env)
    if pinned:
        return pinned
    return fingerprint_agent_id(family, repo_root=repo_root, machine=machine)
