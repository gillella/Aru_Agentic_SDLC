"""Advisory PreToolUse hook for path boundary guidance.

Real enforcement lives in hooks/pre-push, hooks/enforce_touches.py,
aru-governed-pr and aru-merge-policy checks, and merge_pr.py; this hook adds none.
Emits advisory warning context when an edit falls outside the claimed issue's touches.
Always exits 0 and never returns an authorization decision.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _run(cmd: list[str], cwd: str | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _get_issue_body(issue_num: int, repo_root: str) -> str:
    cache_dir = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.gettempdir())
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = Path(repo_root).name
    cache_file = cache_dir / f"aru-touches-{slug}-{issue_num}.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("time", 0) < 600:
                return cached.get("body", "")
        except Exception:
            pass
    body = json.loads(_run(["gh", "issue", "view", str(issue_num), "--json", "body"], cwd=repo_root)).get("body", "")
    try:
        cache_file.write_text(json.dumps({"time": now, "body": body}), encoding="utf-8")
    except Exception:
        pass
    return body


def _load_touches_module():
    for candidate in [
        os.environ.get("ARU_SDLC_HOME", "") + "/scripts/touches.py",
        str(Path(__file__).resolve().parents[2] / "scripts" / "touches.py"),
    ]:
        if candidate and Path(candidate).is_file():
            spec = importlib.util.spec_from_file_location("touches", candidate)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
    import touches
    return touches


def main() -> None:
    try:
        raw_input = sys.stdin.read()
        if not raw_input:
            sys.exit(0)
        payload = json.loads(raw_input)
        file_path = payload.get("tool_input", {}).get("file_path")
        if not file_path:
            sys.exit(0)
        cwd = payload.get("cwd") or os.getcwd()
        repo_root = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
        branch = _run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"])
        match = re.search(r"issue-(\d+)-", branch)
        if not match:
            sys.exit(0)
        issue_num = int(match.group(1))
        body = _get_issue_body(issue_num, repo_root)
        touches_mod = _load_touches_module()
        declaration = touches_mod.parse_touches(body)
        target = Path(file_path)
        try:
            rel = str(target.relative_to(repo_root)) if target.is_absolute() else file_path
        except ValueError:
            rel = file_path
        if not touches_mod.path_allowed(rel, declaration):
            warning = (
                f"Aru advisory (not enforcement): {rel} is outside issue #{issue_num}'s "
                f"declared paths {sorted(declaration)}. The pre-push hook and the "
                "aru-governed-pr check will refuse it; correct the declaration in the "
                "issue or move the change."
            )
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": warning}}))
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
