#!/usr/bin/env python3
# line-ceiling: 135
"""Review-authority permission checks and owner-bound reassignment leases."""

from contextlib import contextmanager
import json
import re
import secrets
import sys
import tempfile
import time

from common import get_repo_slug, run_cmd

LOCK_REF_PREFIX = "refs/tags/aru-locks/review-reassignment-"
LOCK_LEASE_S = 300
OID_RE = re.compile(r"[0-9a-fA-F]{40}")
WRITE_PERMISSIONS = {"admin", "maintain", "write"}


def review_trigger_authorized(login: str):
    """Return whether a GitHub actor can write to the repository, or None."""
    slug = get_repo_slug()
    if not slug:
        return None
    code, out, _ = run_cmd(
        ["gh", "api", f"repos/{slug}/collaborators/{login}/permission",
         "--jq", ".permission"], check=False)
    return None if code != 0 else out.strip().casefold() in WRITE_PERMISSIONS


def _new_lock_blob(pr_id: int, head: str):
    payload = json.dumps({"expires_at": time.time() + LOCK_LEASE_S,
                          "head": head, "owner": secrets.token_hex(16), "pr": pr_id},
                         sort_keys=True, separators=(",", ":"))
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            code, out, err = run_cmd(["git", "hash-object", "-w", handle.name], check=False)
    except OSError as exc:
        return None, str(exc)
    sha = out.strip()
    if code != 0 or OID_RE.fullmatch(sha) is None:
        return None, err.strip() or "git did not create a lock object"
    return sha, None


def _push_lock(ref: str, source: str, expected: str):
    spec = f"{source}:{ref}" if source else f":{ref}"
    return run_cmd(["git", "push", "--porcelain", f"--force-with-lease={ref}:{expected}",
                    "origin", spec], check=False)


def _remote_lock(ref: str, pr_id: int):
    """Return the exact remote lock object, with unknown shapes failing closed."""
    code, out, err = run_cmd(["git", "ls-remote", "origin", ref], check=False)
    if code != 0:
        return None, err.strip() or "could not read the remote lock"
    if not out.strip():
        return None, None
    fields = out.split()
    if len(fields) != 2 or fields[1] != ref or OID_RE.fullmatch(fields[0]) is None:
        return None, "remote lock ref is malformed"
    sha = fields[0]
    code, _, err = run_cmd(["git", "fetch", "--no-tags", "origin", ref], check=False)
    if code != 0:
        return None, err.strip() or "could not fetch the remote lock object"
    code, out, err = run_cmd(["git", "cat-file", "-p", sha], check=False)
    try:
        payload = json.loads(out) if code == 0 else None
    except json.JSONDecodeError:
        payload = None
    if (not isinstance(payload, dict) or payload.get("pr") != pr_id
            or not isinstance(payload.get("owner"), str)
            or not isinstance(payload.get("expires_at"), (int, float))):
        return None, err.strip() or "remote lock has no valid owner/expiry lease"
    return (sha, payload), None


@contextmanager
def remote_reassignment_lock(pr_id: int, head: str):
    """Acquire, recover, and owner-safely release one remote reassignment lease."""
    lock_ref = f"{LOCK_REF_PREFIX}{pr_id}"
    owner_sha, problem = _new_lock_blob(pr_id, head)
    if problem:
        print(f"[CONFLICT] Could not create reassignment lease for PR #{pr_id}: {problem}.",
              file=sys.stderr)
        yield False
        return
    code, _, err = _push_lock(lock_ref, owner_sha, "")
    if code != 0:
        observed, problem = _remote_lock(lock_ref, pr_id)
        if problem or (observed and observed[1]["expires_at"] > time.time()):
            print(f"[CONFLICT] Could not acquire reassignment lock for PR #{pr_id}; "
                  f"{problem or 'another operator holds an active lease'}.", file=sys.stderr)
            yield False
            return
        if observed:
            released, _, release_err = _push_lock(lock_ref, "", observed[0])
            if released != 0:
                print(f"[CONFLICT] Expired lock recovery lost its compare-and-swap: "
                      f"{release_err.strip()}.", file=sys.stderr)
                yield False
                return
        code, _, err = _push_lock(lock_ref, owner_sha, "")
        if code != 0:
            print(f"[CONFLICT] Reassignment lock changed during recovery: {err.strip()}.",
                  file=sys.stderr)
            yield False
            return
    try:
        yield True
    finally:
        released, _, release_err = _push_lock(lock_ref, "", owner_sha)
        if released != 0:
            print("[ERROR] Reassignment lease release failed safely without deleting another "
                  f"owner: {release_err.strip()}.", file=sys.stderr)
