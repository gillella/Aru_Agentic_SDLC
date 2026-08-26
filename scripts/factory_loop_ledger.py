#!/usr/bin/env python3
# line-ceiling: 540
"""factory_loop_ledger.py - project-scoped run health and lane utilization ledger (#471).

Maintains a crash-consistent, bounded, append-only JSONL audit ledger for
factory loop execution ticks. Records stage durations, distinct outcome taxonomy,
idle causes, and explicit pause reasons without capturing credentials or secrets.

Observational only: does not act as a task queue, scheduler, or claim authority.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import stat
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX host
    fcntl = None

SCHEMA_VERSION = "aru.factory_loop_ledger.v1"
DEFAULT_LEDGER_DIR = Path.home() / ".aru" / "ledger"
MAX_LEDGER_BYTES = 5 * 1024 * 1024  # 5 MB per ledger before rotation

OUTCOMES = (
    "success", "failure", "skipped-single-flight", "missed-fire",
    "stale-recovery", "waiting", "paused", "error",
)
IDLE_REASONS = (
    "no-ready-work", "dependency-blocked", "touches-contention",
    "review-wait", "ci-wait", "needs-human", "needs-design",
    "quota-limited", "none",
)
PAUSE_REASONS = (
    "operator-requested", "factory-complete", "human-intervention",
    "quota-exhausted", "maintenance", "error-threshold",
)

AVOIDABLE_IDLE_REASONS = frozenset({"touches-contention", "quota-limited"})
DEPENDENCY_CONTENTION_REASONS = frozenset({"dependency-blocked", "touches-contention"})
REVIEW_CI_WAIT_REASONS = frozenset({"review-wait", "ci-wait"})

ALLOWED_RECORD_KEYS = frozenset({
    "schema_version", "run_id", "project_slug", "scheduled_at", "started_at",
    "finished_at", "stage_durations", "outcome", "snapshot_hash", "actions_selected",
    "workers_launched", "workers_adopted", "prs_progressed", "merges_completed",
    "delivery_status", "assignment_latency_ms", "idle_reason", "pause_reason",
    "linked_issue", "linked_pr",
})


class LedgerError(Exception):
    """Base exception for factory loop ledger operations."""


class LedgerValidationError(LedgerError):
    """Raised when a tick record fails schema or boundary validation."""


class LedgerCorruptionError(LedgerError):
    """Raised when an interior ledger line is corrupted or unparsable."""


@dataclass(frozen=True)
class TickRecord:
    schema_version: str
    run_id: str
    project_slug: str
    scheduled_at: str
    started_at: str
    finished_at: str
    stage_durations: dict[str, int | float]
    outcome: str
    snapshot_hash: str
    actions_selected: list[str]
    workers_launched: int
    workers_adopted: int
    prs_progressed: list[int]
    merges_completed: list[int]
    delivery_status: str
    assignment_latency_ms: int | float
    idle_reason: str
    pause_reason: str | None = None
    linked_issue: int | None = None
    linked_pr: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SLUG_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _is_finite_non_negative_number(val: Any) -> bool:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    if not math.isfinite(val):
        return False
    return val >= 0


def _is_non_negative_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and val >= 0


def _is_positive_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and val > 0


def _parse_iso(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError(f"'{name}' must be a non-empty ISO timestamp.")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        datetime.datetime.fromisoformat(text)
    except (ValueError, TypeError) as exc:
        raise LedgerValidationError(f"'{name}' invalid ISO timestamp: {exc}") from exc


def _validate_envelope(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise LedgerValidationError("Tick record must be a JSON object.")
    disallowed = set(data.keys()) - ALLOWED_RECORD_KEYS
    if disallowed:
        raise LedgerValidationError(f"Disallowed fields in tick record: {sorted(disallowed)}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LedgerValidationError(f"Invalid schema_version: expected '{SCHEMA_VERSION}'")

    for key in ("run_id", "snapshot_hash", "delivery_status"):
        val = data.get(key)
        if not isinstance(val, str) or not val.strip():
            raise LedgerValidationError(f"'{key}' must be a non-empty string.")

    slug_val = data.get("project_slug")
    sanitize_slug(slug_val)

    for key in ("scheduled_at", "started_at", "finished_at"):
        _parse_iso(data.get(key), key)


def _validate_timings_and_lanes(data: dict[str, Any]) -> None:
    stage_durations = data.get("stage_durations")
    if not isinstance(stage_durations, dict):
        raise LedgerValidationError("'stage_durations' must be an object.")
    for k, v in stage_durations.items():
        if not _is_finite_non_negative_number(v):
            raise LedgerValidationError(f"Duration '{k}' must be non-negative finite number.")
    if not _is_finite_non_negative_number(stage_durations.get("total_ms")):
        raise LedgerValidationError(
            "'stage_durations.total_ms' is required and must be a non-negative finite number."
        )

    lat = data.get("assignment_latency_ms")
    if not _is_finite_non_negative_number(lat):
        raise LedgerValidationError("'assignment_latency_ms' must be non-negative finite number.")

    actions = data.get("actions_selected")
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise LedgerValidationError("'actions_selected' must be a list of strings.")

    for key in ("workers_launched", "workers_adopted"):
        val = data.get(key)
        if not _is_non_negative_int(val):
            raise LedgerValidationError(f"'{key}' must be a non-negative integer.")

    for key in ("prs_progressed", "merges_completed"):
        val = data.get(key)
        if not isinstance(val, list) or not all(_is_positive_int(p) for p in val):
            raise LedgerValidationError(f"'{key}' must be a list of positive integer IDs.")


def _validate_reasons(data: dict[str, Any], outcome: str) -> None:
    if outcome not in OUTCOMES:
        raise LedgerValidationError(f"Invalid outcome '{outcome}'; must be one of {OUTCOMES}")

    idle = data.get("idle_reason")
    if idle not in IDLE_REASONS:
        raise LedgerValidationError(f"Invalid idle_reason '{idle}'; must be one of {IDLE_REASONS}")

    pause = data.get("pause_reason")
    if outcome == "paused":
        if pause not in PAUSE_REASONS:
            raise LedgerValidationError(f"Paused outcome requires pause_reason in {PAUSE_REASONS}")
    elif pause is not None:
        raise LedgerValidationError(f"Non-paused outcome '{outcome}' cannot have pause_reason.")

    for key in ("linked_issue", "linked_pr"):
        val = data.get(key)
        if val is not None and not _is_positive_int(val):
            raise LedgerValidationError(f"'{key}' must be positive integer or null.")


def validate_tick_record(data: dict[str, Any]) -> TickRecord:
    """Validate data against aru.factory_loop_ledger.v1 schema and safety boundaries."""
    _validate_envelope(data)
    _validate_timings_and_lanes(data)
    outcome = data.get("outcome", "")
    _validate_reasons(data, outcome)

    return TickRecord(
        schema_version=SCHEMA_VERSION,
        run_id=data["run_id"],
        project_slug=data["project_slug"],
        scheduled_at=data["scheduled_at"],
        started_at=data["started_at"],
        finished_at=data["finished_at"],
        stage_durations=data["stage_durations"],
        outcome=outcome,
        snapshot_hash=data["snapshot_hash"],
        actions_selected=list(data["actions_selected"]),
        workers_launched=data["workers_launched"],
        workers_adopted=data["workers_adopted"],
        prs_progressed=list(data["prs_progressed"]),
        merges_completed=list(data["merges_completed"]),
        delivery_status=data["delivery_status"],
        assignment_latency_ms=data["assignment_latency_ms"],
        idle_reason=data["idle_reason"],
        pause_reason=data.get("pause_reason"),
        linked_issue=data.get("linked_issue"),
        linked_pr=data.get("linked_pr"),
    )


def sanitize_slug(slug: Any) -> str:
    """Validate and sanitize a project slug for filesystem use."""
    if not isinstance(slug, str) or not slug.strip():
        raise LedgerValidationError("Project slug must be a non-empty string.")
    cleaned = slug.strip()
    if cleaned.startswith("/") or cleaned.endswith("/") or "\\" in cleaned:
        raise LedgerValidationError(f"Invalid project slug '{slug}'.")
    parts = cleaned.split("/")
    for part in parts:
        if not part or not _SLUG_PART_RE.fullmatch(part) or part in {".", ".."}:
            raise LedgerValidationError(f"Invalid project slug '{slug}'.")
    return "__".join(parts)


def _contained_ledger_path(slug: str, extension: str, base_dir: Path | None = None) -> Path:
    sanitized = sanitize_slug(slug)
    target_dir = (base_dir or DEFAULT_LEDGER_DIR).resolve()
    target_path = (target_dir / f"{sanitized}{extension}").resolve()
    try:
        target_path.relative_to(target_dir)
    except ValueError as exc:
        raise LedgerValidationError(f"Path traversal detected for project slug '{slug}'.") from exc
    if target_path.parent != target_dir:
        raise LedgerValidationError(f"Path traversal detected for project slug '{slug}'.")
    return target_path


def ledger_file_for_slug(slug: str, base_dir: Path | None = None) -> Path:
    return _contained_ledger_path(slug, ".jsonl", base_dir=base_dir)


def rotated_file_for_slug(slug: str, base_dir: Path | None = None) -> Path:
    return _contained_ledger_path(slug, ".jsonl.1", base_dir=base_dir)


def _ensure_dir_secure(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dir_path, stat.S_IRWXU)  # 0700
    except OSError:
        pass


def _lock_file_path(ledger_file: Path) -> Path:
    return ledger_file.parent / f"{ledger_file.name}.lock"


def _rotate_unlocked(ledger_file: Path, rotated_file: Path) -> None:
    if ledger_file.exists():
        os.replace(ledger_file, rotated_file)


def _truncate_torn_tail(ledger_file: Path) -> None:
    try:
        with open(ledger_file, "rb+") as check_handle:
            content = check_handle.read()
            if content and not content.endswith(b"\n"):
                last_newline = content.rfind(b"\n")
                check_handle.seek(last_newline + 1 if last_newline != -1 else 0)
                check_handle.truncate()
    except OSError:
        pass


def _apply_permissions(fh_fileno: int, ledger_file: Path) -> None:
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fh_fileno, 0o600)
        else:
            os.chmod(ledger_file, 0o600)
    except OSError:
        pass


def rotate_ledger(slug: str, base_dir: Path | None = None) -> bool:
    """Explicitly rotate the project ledger file to its single-generation backup."""
    target_dir = base_dir or DEFAULT_LEDGER_DIR
    _ensure_dir_secure(target_dir)
    ledger_file = ledger_file_for_slug(slug, base_dir=target_dir)
    rotated_file = rotated_file_for_slug(slug, base_dir=target_dir)
    lock_file = _lock_file_path(ledger_file)

    with open(lock_file, "a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            if not ledger_file.exists():
                return False
            _rotate_unlocked(ledger_file, rotated_file)
            return True
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)


def append_tick_record(
    data_or_record: dict[str, Any] | TickRecord,
    base_dir: Path | None = None,
) -> TickRecord:
    """Append a validated tick record to the project ledger with crash consistency."""
    record = (
        data_or_record
        if isinstance(data_or_record, TickRecord)
        else validate_tick_record(data_or_record)
    )
    target_dir = base_dir or DEFAULT_LEDGER_DIR
    _ensure_dir_secure(target_dir)

    ledger_file = ledger_file_for_slug(record.project_slug, base_dir=target_dir)
    rotated_file = rotated_file_for_slug(record.project_slug, base_dir=target_dir)
    lock_file = _lock_file_path(ledger_file)
    line = json.dumps(record.to_dict(), separators=(",", ":"), allow_nan=False) + "\n"

    with open(lock_file, "a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            if ledger_file.exists():
                if ledger_file.stat().st_size >= MAX_LEDGER_BYTES:
                    _rotate_unlocked(ledger_file, rotated_file)
                else:
                    _truncate_torn_tail(ledger_file)
            fd = os.open(ledger_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fh = os.fdopen(fd, "a", encoding="utf-8")
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            with fh:
                _apply_permissions(fh.fileno(), ledger_file)
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
    return record


def read_tick_records(slug: str, base_dir: Path | None = None) -> list[TickRecord]:
    """Read and validate all records for a project, safely ignoring torn tail on EOF."""
    ledger_file = ledger_file_for_slug(slug, base_dir=base_dir)
    if not ledger_file.exists():
        return []
    try:
        with open(ledger_file, "r", encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.readlines()
    except OSError as exc:
        raise LedgerError(f"Could not read ledger file '{ledger_file}': {exc}") from exc

    records: list[TickRecord] = []
    total = len(raw_lines)
    for idx, raw_line in enumerate(raw_lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            records.append(validate_tick_record(json.loads(line)))
        except Exception as exc:
            if idx == total and not raw_line.endswith("\n"):
                break  # Ignore torn tail at EOF
            raise LedgerCorruptionError(f"Corrupted line {idx} in '{ledger_file}': {exc}") from exc
    return records


def _nearest_rank_p90(values: list[float | int]) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = math.ceil(0.90 * len(sorted_vals))
    idx = min(max(0, k - 1), len(sorted_vals) - 1)
    return float(sorted_vals[idx])


def summarize_ledger(records: list[TickRecord]) -> dict[str, Any]:
    """Compute observational summary metrics across a sequence of tick records."""
    if not records:
        return {
            "total_ticks": 0, "median_tick_duration_ms": 0.0, "p90_tick_duration_ms": 0.0,
            "median_assignment_latency_ms": 0.0, "p90_assignment_latency_ms": 0.0,
            "outcomes": {o: 0 for o in OUTCOMES}, "skipped_fires": 0,
            "idle_reasons": {r: 0 for r in IDLE_REASONS}, "avoidable_idle_count": 0,
            "dependency_contention_count": 0, "review_ci_wait_count": 0,
            "active_lanes": 0, "total_workers_launched": 0, "total_workers_adopted": 0,
            "total_prs_progressed": 0, "total_merges_completed": 0,
            "last_material_progress_at": None, "last_tick_at": None,
            "last_outcome": None, "last_pause_reason": None,
        }

    durations = [
        r.stage_durations["total_ms"]
        for r in records
        if "total_ms" in r.stage_durations
        and _is_finite_non_negative_number(r.stage_durations["total_ms"])
    ]
    latencies = [r.assignment_latency_ms for r in records]
    outcomes: dict[str, int] = {o: 0 for o in OUTCOMES}
    idle_reasons: dict[str, int] = {r: 0 for r in IDLE_REASONS}
    launched = adopted = 0
    prs_set: set[int] = set()
    merges_set: set[int] = set()
    last_progress_at: str | None = None

    for r in records:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        idle_reasons[r.idle_reason] = idle_reasons.get(r.idle_reason, 0) + 1
        launched += r.workers_launched
        adopted += r.workers_adopted
        prs_set.update(r.prs_progressed)
        merges_set.update(r.merges_completed)
        if r.outcome == "success" or r.prs_progressed or r.merges_completed or (r.workers_launched + r.workers_adopted) > 0:
            last_progress_at = r.finished_at

    last = records[-1]
    return {
        "total_ticks": len(records),
        "median_tick_duration_ms": float(statistics.median(durations)) if durations else 0.0,
        "p90_tick_duration_ms": _nearest_rank_p90(durations),
        "median_assignment_latency_ms": float(statistics.median(latencies)) if latencies else 0.0,
        "p90_assignment_latency_ms": _nearest_rank_p90(latencies),
        "outcomes": outcomes,
        "skipped_fires": outcomes.get("skipped-single-flight", 0) + outcomes.get("missed-fire", 0),
        "idle_reasons": idle_reasons,
        "avoidable_idle_count": sum(idle_reasons.get(k, 0) for k in AVOIDABLE_IDLE_REASONS),
        "dependency_contention_count": sum(idle_reasons.get(k, 0) for k in DEPENDENCY_CONTENTION_REASONS),
        "review_ci_wait_count": sum(idle_reasons.get(k, 0) for k in REVIEW_CI_WAIT_REASONS),
        "active_lanes": last.workers_launched + last.workers_adopted,
        "total_workers_launched": launched,
        "total_workers_adopted": adopted,
        "total_prs_progressed": len(prs_set),
        "total_merges_completed": len(merges_set),
        "last_material_progress_at": last_progress_at,
        "last_tick_at": last.finished_at,
        "last_outcome": last.outcome,
        "last_pause_reason": last.pause_reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record and summarize factory loop run health and lane utilization."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sum_p = subparsers.add_parser("summary", help="Summarize project ledger records")
    sum_p.add_argument("--project-slug", required=True, help="GitHub repository slug")
    sum_p.add_argument("--dir", default=None, help="Base ledger directory override")
    sum_p.add_argument("--json", action="store_true", help="Emit summary as JSON")

    list_p = subparsers.add_parser("list", help="List project ledger records")
    list_p.add_argument("--project-slug", required=True, help="GitHub repository slug")
    list_p.add_argument("--dir", default=None, help="Base ledger directory override")
    list_p.add_argument("--limit", type=int, default=50, help="Maximum records to list")
    list_p.add_argument("--json", action="store_true", help="Emit records as JSON")

    rot_p = subparsers.add_parser("rotate", help="Rotate project ledger file")
    rot_p.add_argument("--project-slug", required=True, help="GitHub repository slug")
    rot_p.add_argument("--dir", default=None, help="Base ledger directory override")

    args = parser.parse_args()
    base_dir = Path(args.dir) if args.dir else None

    if args.command == "summary":
        records = read_tick_records(args.project_slug, base_dir=base_dir)
        summary = summarize_ledger(records)
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"=== Factory Loop Ledger Summary ({args.project_slug}) ===")
            print(f"Total Ticks: {summary['total_ticks']}")
            print(f"Duration: median {summary['median_tick_duration_ms']:.1f}ms | p90 {summary['p90_tick_duration_ms']:.1f}ms")
            print(f"Latency: median {summary['median_assignment_latency_ms']:.1f}ms | p90 {summary['p90_assignment_latency_ms']:.1f}ms")
            print(f"Skipped / Missed: {summary['skipped_fires']} | Active Lanes: {summary['active_lanes']}")
            print(f"Idle Breakdown: avoidable={summary['avoidable_idle_count']}, contention={summary['dependency_contention_count']}, review/ci-wait={summary['review_ci_wait_count']}")
            print(f"Last Material Progress: {summary['last_material_progress_at'] or 'none'}")
    elif args.command == "list":
        records = read_tick_records(args.project_slug, base_dir=base_dir)
        slice_records = records[-args.limit:] if args.limit > 0 else records
        if args.json:
            print(json.dumps([r.to_dict() for r in slice_records], indent=2))
        else:
            for r in slice_records:
                print(f"[{r.finished_at}] {r.run_id} outcome={r.outcome} idle={r.idle_reason}")
    elif args.command == "rotate":
        rotated = rotate_ledger(args.project_slug, base_dir=base_dir)
        print("Rotated" if rotated else "No ledger file to rotate")


if __name__ == "__main__":
    main()
