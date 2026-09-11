"""Optional merge-authority App: a required check that only merge_pr.py posts.

With ARU_MERGE_APP_RUNNER and ARU_MERGE_APP_ID both set, merge_pr.py posts an
``aru-merge-authorized`` check run as that App at the exact head once every gate
passes, and the ruleset requires the check pinned to the App's integration id,
so ``gh pr merge`` alone cannot satisfy it. Both unset leaves the gate off;
exactly one set is a misconfiguration and refuses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from common import KernelError, repo_slug, run

MERGE_AUTHORITY_CHECK = "aru-merge-authorized"
RUNNER_ENV = "ARU_MERGE_APP_RUNNER"
APP_ID_ENV = "ARU_MERGE_APP_ID"


def configured() -> tuple[str, int] | None:
    runner, app_id = os.environ.get(RUNNER_ENV, ""), os.environ.get(APP_ID_ENV, "")
    if not runner and not app_id:
        return None
    if not runner or not (app_id.isascii() and app_id.isdecimal()) or int(app_id) <= 0:
        raise KernelError(f"merge authority needs both {RUNNER_ENV} and a numeric {APP_ID_ENV}")
    path = Path(runner).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise KernelError("configured merge-authority runner is not executable")
    return str(path), int(app_id)


def _as_app(runner: str, slug: str, args: list[str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    argv = [runner, "--repo", slug, "--", "gh", "api", *args]
    if payload is not None:
        argv += ["--input", "-"]
    result = run(argv, input_text=None if payload is None else json.dumps(payload))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KernelError("merge-authority App returned malformed JSON") from exc
    if not isinstance(data, dict):
        raise KernelError("merge-authority App returned malformed JSON")
    return data


def post(head: str, conclusion: str, summary: str) -> int | None:
    """Record the verdict at ``head`` as the App; None when the gate is off."""
    config = configured()
    if config is None:
        return None
    runner, app_id = config
    slug = repo_slug()
    record = _as_app(runner, slug, ["--method", "POST", f"repos/{slug}/check-runs"], {
        "name": MERGE_AUTHORITY_CHECK, "head_sha": head, "status": "completed",
        "conclusion": conclusion, "output": {"title": summary, "summary": summary},
    })
    app = record.get("app")
    # A runner authenticated as another App (e.g. the factory's author App) posts
    # a check the ruleset ignores, so a merge would stall or bypass the pin.
    if (
        not isinstance(app, dict) or app.get("id") != app_id
        or record.get("head_sha") != head or record.get("name") != MERGE_AUTHORITY_CHECK
        or record.get("conclusion") != conclusion or type(record.get("id")) is not int
    ):
        raise KernelError("merge-authority check was not recorded by the configured App at the exact head")
    return record["id"]


def installed(slug: str) -> bool:
    """Whether the configured App can already act on ``slug``."""
    config = configured()
    if config is None:
        return False
    try:
        _as_app(config[0], slug, [f"repos/{slug}"])
    except KernelError:
        return False
    return True
