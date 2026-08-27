#!/usr/bin/env python3
"""Persist and validate merge gate evidence across a resumed close-out."""

import json
import os
import tempfile

from common import run_cmd


SCHEMA_VERSION = 1
TOP_LEVEL_KEYS = {"schema_version", "pr", "gated_head", "gates"}
ROW_KEYS = {"name", "passed", "message"}


def gate_verdict_path(repo_root, pr_num, run_cmd_fn=run_cmd):
    code, out, _ = run_cmd_fn(["git", "rev-parse", "--git-common-dir"], check=False, cwd=repo_root)
    if code != 0 or not out.strip():
        return ""
    common = out.strip()
    if not os.path.isabs(common):
        common = os.path.join(repo_root, common)
    return os.path.join(common, f"aru-gates-{pr_num}.json")


def save_gate_verdicts(repo_root, pr_num, gates, gated_head, run_cmd_fn=run_cmd):
    path = gate_verdict_path(repo_root, pr_num, run_cmd_fn=run_cmd_fn)
    if not path or not gated_head:
        return False
    payload = {
        "schema_version": SCHEMA_VERSION,
        "pr": pr_num,
        "gated_head": gated_head,
        "gates": [{"name": name, "passed": passed, "message": message}
                  for name, passed, message in gates],
    }
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".aru-gates-", suffix=".tmp", delete=False) as handle:
            temp_path = handle.name
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return True
    except (OSError, TypeError, ValueError):
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False


def load_gate_verdicts(repo_root, pr_num, run_cmd_fn=run_cmd):
    path = gate_verdict_path(repo_root, pr_num, run_cmd_fn=run_cmd_fn)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def validate_gate_verdicts(record, pr_num, gated_head):
    if not isinstance(record, dict) or set(record) != TOP_LEVEL_KEYS:
        return False, None, "gate evidence is absent or malformed."
    if record.get("schema_version") != SCHEMA_VERSION:
        return False, None, "gate evidence schema version is unsupported."
    if record.get("pr") != pr_num:
        return False, None, f"gate evidence is for PR #{record.get('pr')}, not #{pr_num}."
    if record.get("gated_head") != gated_head:
        return False, None, (f"gate evidence is bound to head {record.get('gated_head') or 'unknown'}, "
                             f"not {gated_head}.")
    rows = record.get("gates")
    if not isinstance(rows, list) or not rows:
        return False, None, "gate evidence has no evaluated gate rows."
    gates = []
    failed = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            return False, None, "gate evidence has a malformed gate row."
        name = row.get("name")
        passed = row.get("passed")
        message = row.get("message")
        if not isinstance(name, str) or not isinstance(passed, bool) or not isinstance(message, str):
            return False, None, "gate evidence row types are malformed."
        gates.append((name, passed, message))
        if not passed:
            failed.append(name)
    if failed:
        return False, None, f"gate evidence records failing gates: {', '.join(failed)}."
    return True, gates, "gate evidence is exact-head and all-passing."


def discard_gate_verdicts(repo_root, pr_num, run_cmd_fn=run_cmd):
    path = gate_verdict_path(repo_root, pr_num, run_cmd_fn=run_cmd_fn)
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
