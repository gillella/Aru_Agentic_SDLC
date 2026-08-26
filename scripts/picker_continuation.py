"""Keep the Ready queue moving after a governed merge close-out."""

import json
import sys
from pathlib import Path

from common import run_cmd


def ensure_ready_after_closeout(repo_root, run_cmd_fn=None):
    """Run one promote-only picker pass without assigning work to the merger."""
    if not repo_root or not Path(repo_root).is_dir():
        return False, "Picker could not ensure Ready work: repository root is unavailable."
    root_picker = Path(repo_root).resolve() / "scripts" / "fetch_next_work.py"
    picker = root_picker if root_picker.is_file() else Path(__file__).resolve().with_name(
        "fetch_next_work.py"
    )
    code, out, err = (run_cmd_fn or run_cmd)(
        [sys.executable, str(picker), "--agent", "post-merge-promoter", "--promote-idle",
         "--reap-after", "0", "--json"],
        check=False, cwd=repo_root,
    )
    if code != 0:
        detail = (err or out).strip() or "unknown error"
        return False, f"Picker could not ensure Ready work: {detail}"
    try:
        payload = json.loads(out)
    except (TypeError, json.JSONDecodeError):
        return False, "Picker returned malformed continuation state."
    promoted = payload.get("auto_promoted_issue")
    if promoted:
        return True, f"Promoted qualified Backlog issue #{promoted} to Ready."
    work = payload.get("work") or {}
    if work.get("type") == "error":
        reason = work.get("reason") or "unknown error"
        return False, f"Picker could not ensure Ready work: {reason}"
    if work.get("type") == "issue":
        return True, f"Ready work is already available at issue #{work.get('issue')}."
    if work.get("type") in {"feedback", "merge"}:
        return True, f"Higher-priority {work.get('type')} work is already available."
    return True, "No qualified Backlog issue is currently promotable."
