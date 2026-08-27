"""Build and probe the stateless initial coding-reviewer pool."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from common import (
    CODING_REVIEWERS,
    REVIEW_BINDING_PREFIX,
    REVIEWER_CONFIG_ENV,
    KernelError,
    configured_coding_reviewers,
    gh_json,
    normalized_identity,
)

PROBE_PROMPT = "Reply exactly OK"
CodingCandidate = tuple[str, str, str, str | None]
ProbeRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def registered_coding_actors() -> dict[str, str]:
    records = gh_json(["label", "list", "--limit", "1000", "--json", "name"])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise KernelError("coding reviewer identity bindings are unavailable")
    bindings: dict[str, str] = {}
    for item in records:
        name = str(item.get("name") or "")
        if not name.startswith(REVIEW_BINDING_PREFIX):
            continue
        values = name[len(REVIEW_BINDING_PREFIX) :].split("=", 1)
        if len(values) != 2:
            raise KernelError("coding reviewer identity binding is malformed")
        identity, actor = values[0].lower(), values[1].lower()
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identity)
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?(?:\[bot\])?",
                actor,
            )
            or identity in bindings
        ):
            raise KernelError("coding reviewer identity binding is malformed or ambiguous")
        bindings[identity] = actor
    return bindings


def coding_reviewer_candidates(
    *,
    author_identity: str,
    author_actor: str = "",
    reviewer_actors: dict[str, str] | None = None,
) -> list[CodingCandidate]:
    if not os.environ.get(REVIEWER_CONFIG_ENV, "").strip():
        actors = (
            registered_coding_actors()
            if reviewer_actors is None
            else reviewer_actors
        )
        if actors:
            raise KernelError(
                f"{REVIEWER_CONFIG_ENV} is missing while reviewer bindings exist"
            )
        return []
    author_identity = normalized_identity(author_identity)
    author_actor = author_actor.lower()
    actors = registered_coding_actors() if reviewer_actors is None else reviewer_actors
    configured = configured_coding_reviewers()
    candidates: list[CodingCandidate] = []
    for family in CODING_REVIEWERS:
        for identity, subscription in configured.get(family, ()):
            actor = str(actors.get(identity) or "").lower()
            if identity == author_identity or not actor or actor == author_actor:
                continue
            candidates.append((family, identity, actor, subscription))
    return candidates


def _command(name: str) -> str | None:
    return shutil.which(name)


def _probe_ok(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 0 and result.stdout.strip() == "OK"


def probe_coding_candidate(candidate: CodingCandidate, runner: ProbeRunner) -> bool:
    family, _identity, _actor, subscription = candidate
    if family == "claude-code":
        executable = str(Path.home() / ".local" / "bin" / "claude-sub")
        return _probe_ok(runner([executable, str(subscription), "-p", PROBE_PROMPT]))
    command_names = {
        "openai-codex": "codex",
        "xai-cursor": "cursor-agent",
        "google-antigravity": "agy",
    }
    executable = _command(command_names[family])
    if executable is None:
        return False
    arguments = {
        "openai-codex": [executable, "exec", "--skip-git-repo-check", PROBE_PROMPT],
        "xai-cursor": [executable, "-p", PROBE_PROMPT],
        "google-antigravity": [executable, "-p", PROBE_PROMPT],
    }[family]
    return _probe_ok(runner(arguments))
