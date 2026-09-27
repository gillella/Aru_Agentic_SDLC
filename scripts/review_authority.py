#!/usr/bin/env python3
"""Compatibility stub for the retired per-repository reviewer posture.

The Kernel's one-approval rule is enforced by ``merge_pr.py`` and the GitHub
ruleset. Legacy ``.aru/review.json`` files have no merge or PR-creation effect.
This module remains importable for older callers while they migrate.
"""

from __future__ import annotations

from pathlib import Path


def read_policy_text(*, cwd: str | Path | None = None) -> None:
    """Return no policy: legacy reviewer declarations carry no Kernel authority."""
    return None
