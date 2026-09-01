"""Bounded, concurrent liveness probes for configured coding reviewers."""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from common import (
    CODING_REVIEWERS,
    PROBE_PROMPT,
    canonical_github_actor,
    configured_coding_reviewers,
    normalized_identity,
    registered_coding_actors,
    same_github_actor,
)

PROBE_COMMAND_TIMEOUT_SECONDS, PROBE_AGGREGATE_TIMEOUT_SECONDS = 10, 12
ProbeRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
CodingCandidate = tuple[str, str, str, str | None]


def _default_probe(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def _probe_ok(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 0 and result.stdout.strip() == "OK"


def _command(name: str) -> str | None:
    return shutil.which(name)


def _coding_candidates(
    *,
    author_identity: str,
    author_family: str,
    author_actor: str,
    reviewer_actors: dict[str, str] | None,
) -> list[CodingCandidate]:
    author_identity = normalized_identity(author_identity)
    author_actor = canonical_github_actor(author_actor)
    actors = registered_coding_actors() if reviewer_actors is None else reviewer_actors
    configured = configured_coding_reviewers()
    family_order = [family for family in CODING_REVIEWERS if family != author_family]
    if author_family in CODING_REVIEWERS:
        family_order.append(author_family)
    candidates: list[CodingCandidate] = []
    for family in family_order:
        for identity, subscription in configured.get(family, ()):
            actor = str(actors.get(identity) or "").lower()
            if identity != author_identity and actor and not same_github_actor(actor, author_actor):
                candidates.append((family, identity, actor, subscription))
    return candidates


def _probe_command(candidate: CodingCandidate) -> list[str] | None:
    family, _identity, _actor, subscription = candidate
    if family == "claude-code":
        return [
            str(Path.home() / ".local" / "bin" / "claude-sub"),
            str(subscription), "-p", PROBE_PROMPT,
        ]
    names = {
        "openai-codex": "codex",
        "xai-cursor": "cursor-agent",
        "google-antigravity": "agy",
    }
    executable = _command(names[family])
    if executable is None:
        return None
    arguments = {
        "openai-codex": ["exec", "--skip-git-repo-check", PROBE_PROMPT],
        "xai-cursor": ["-p", PROBE_PROMPT],
        "google-antigravity": ["-p", PROBE_PROMPT],
    }[family]
    return [executable, *arguments]


def available_coding_reviewers(
    candidates: list[CodingCandidate],
    *,
    runner: ProbeRunner = _default_probe,
    aggregate_timeout: float = PROBE_AGGREGATE_TIMEOUT_SECONDS,
) -> list[tuple[str, str, str]]:
    jobs = [
        (index, candidate, command)
        for index, candidate in enumerate(candidates)
        if (command := _probe_command(candidate)) is not None
    ]
    if not jobs:
        return []
    executor = ThreadPoolExecutor(max_workers=min(8, len(jobs)))
    futures = {
        executor.submit(runner, command): (index, candidate)
        for index, candidate, command in jobs
    }
    done, pending = wait(futures, timeout=aggregate_timeout)
    available: set[int] = set()
    for future in done:
        try:
            if _probe_ok(future.result()):
                available.add(futures[future][0])
        except Exception:
            continue
    for future in pending:
        future.cancel()
    executor.shutdown(wait=False, cancel_futures=True)
    return [
        (family, identity, actor)
        for index, (family, identity, actor, _subscription) in enumerate(candidates)
        if index in available
    ]


def probe_coding_reviewer(
    *,
    author_identity: str,
    author_family: str,
    author_actor: str = "",
    rotation_key: int,
    reviewer_actors: dict[str, str] | None = None,
    runner: ProbeRunner = _default_probe,
    aggregate_timeout: float = PROBE_AGGREGATE_TIMEOUT_SECONDS,
) -> tuple[str, str, str] | None:
    candidates = _coding_candidates(
        author_identity=author_identity,
        author_family=author_family,
        author_actor=author_actor,
        reviewer_actors=reviewer_actors,
    )
    available = available_coding_reviewers(
        candidates, runner=runner, aggregate_timeout=aggregate_timeout
    )
    return available[rotation_key % len(available)] if available else None


__all__ = [
    "CodingCandidate",
    "PROBE_AGGREGATE_TIMEOUT_SECONDS",
    "PROBE_COMMAND_TIMEOUT_SECONDS",
    "ProbeRunner",
    "_coding_candidates",
    "_command",
    "_default_probe",
    "available_coding_reviewers",
    "probe_coding_reviewer",
]
