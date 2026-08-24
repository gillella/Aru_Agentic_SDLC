#!/usr/bin/env python3
"""Audit provider-neutral review-service capacity snapshots without mutation."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from create_pr import REVIEW_SERVICES


SNAPSHOT_SCHEMA = "aru.review-service-capacity-snapshot.v1"
AUDIT_SCHEMA = "aru.review-service-capacity-audit.v1"
ACCOUNT_STATES = ("active", "suspended", "closed")
PLAN_KINDS = ("trial", "free", "paid")
QUOTA_KINDS = ("metered", "unlimited")
SNAPSHOT_KEYS = {"schema", "observed_at", "configured_services", "services"}
SERVICE_KEYS = {"service", "account_state", "plan", "quota"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def parse_snapshot(raw):
    """Parse one strict JSON object, rejecting duplicates and non-finite values."""
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid capacity snapshot JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("capacity snapshot must be a JSON object")
    return value


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def _as_of(value):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("as-of timestamp must include a timezone")
        return value.astimezone(timezone.utc)
    return _parse_timestamp(value)


def _mismatch(report, code, message, service=None):
    item = {"code": code, "message": message}
    if service is not None:
        item["service"] = service
    report["mismatches"].append(item)
    return item


def _base_report(as_of, max_age_seconds):
    return {
        "schema": AUDIT_SCHEMA,
        "ok": False,
        "as_of": _iso(as_of),
        "max_age_seconds": max_age_seconds,
        "snapshot": {
            "schema": None,
            "observed_at": None,
            "age_seconds": None,
        },
        "configured_services": [],
        "services": [],
        "mismatches": [],
    }


def _service_base(service):
    return {
        "service": service,
        "available": False,
        "account_state": None,
        "plan_kind": None,
        "trial_state": "unknown",
        "expiry_state": "unknown",
        "expires_at": None,
        "quota_kind": None,
        "quota_state": "unknown",
        "limit": None,
        "used": None,
        "remaining": None,
        "resets_at": None,
        "mismatches": [],
    }


def _service_mismatch(report, item, code, message):
    mismatch = _mismatch(report, code, message, item["service"])
    item["mismatches"].append(mismatch)


def _classify_plan(raw, item, report, as_of):
    if not isinstance(raw, dict):
        _service_mismatch(report, item, "plan_invalid", "Plan must be an object.")
        return
    kind = raw.get("kind")
    item["plan_kind"] = kind if isinstance(kind, str) else None
    expected_keys = {"kind", "expires_at"} if kind == "trial" else {"kind"}
    if kind not in PLAN_KINDS or set(raw) != expected_keys:
        _service_mismatch(
            report, item, "plan_invalid", "Plan kind or fields are missing or ambiguous.",
        )
        return
    if kind != "trial":
        item["trial_state"] = "not_applicable"
        item["expiry_state"] = "not_applicable"
        return
    try:
        expiry = _parse_timestamp(raw["expires_at"])
    except (KeyError, ValueError):
        _service_mismatch(
            report, item, "plan_invalid", "Trial expiry is missing or malformed.",
        )
        return
    item["expires_at"] = _iso(expiry)
    if expiry <= as_of:
        item["trial_state"] = "expired"
        item["expiry_state"] = "expired"
        _service_mismatch(report, item, "trial_expired", "Trial has expired.")
    else:
        item["trial_state"] = "active"
        item["expiry_state"] = "future"


def _count(value, *, positive=False):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value > 0 if positive else value >= 0)
    )


def _classify_quota(raw, item, report, as_of):
    if not isinstance(raw, dict):
        _service_mismatch(report, item, "quota_invalid", "Quota must be an object.")
        return
    kind = raw.get("kind")
    item["quota_kind"] = kind if isinstance(kind, str) else None
    if kind == "unlimited" and set(raw) == {"kind"}:
        item["quota_state"] = "unlimited"
        return
    expected = {"kind", "limit", "used", "remaining", "resets_at"}
    if kind != "metered" or set(raw) != expected:
        _service_mismatch(
            report, item, "quota_invalid", "Quota kind or fields are missing or ambiguous.",
        )
        return
    limit = raw["limit"]
    used = raw["used"]
    remaining = raw["remaining"]
    if not (_count(limit, positive=True) and _count(used) and _count(remaining)):
        _service_mismatch(report, item, "quota_invalid", "Quota counters must be integers.")
        return
    item.update({"limit": limit, "used": used, "remaining": remaining})
    if used > limit or remaining > limit or used + remaining != limit:
        _service_mismatch(report, item, "quota_invalid", "Quota counters are inconsistent.")
        return
    try:
        reset = _parse_timestamp(raw["resets_at"])
    except ValueError:
        _service_mismatch(report, item, "quota_invalid", "Quota reset is malformed.")
        return
    item["resets_at"] = _iso(reset)
    if reset <= as_of:
        item["quota_state"] = "unknown"
        _service_mismatch(
            report, item, "quota_window_elapsed", "Quota window has already reset.",
        )
    elif remaining == 0:
        item["quota_state"] = "exhausted"
        _service_mismatch(report, item, "quota_exhausted", "Quota is exhausted.")
    else:
        item["quota_state"] = "available"


def _classify_service(service, raw, report, as_of, snapshot_current):
    item = _service_base(service)
    if not isinstance(raw, dict) or set(raw) != SERVICE_KEYS or raw.get("service") != service:
        _service_mismatch(
            report,
            item,
            "service_record_invalid",
            "Service record fields are missing, unexpected, or mismatched.",
        )
        return item
    account_state = raw.get("account_state")
    item["account_state"] = account_state if isinstance(account_state, str) else None
    if account_state not in ACCOUNT_STATES:
        _service_mismatch(
            report, item, "account_state_invalid", "Account state is not recognized.",
        )
    elif account_state != "active":
        _service_mismatch(
            report, item, "account_unavailable", f"Account is {account_state}.",
        )
    _classify_plan(raw["plan"], item, report, as_of)
    _classify_quota(raw["quota"], item, report, as_of)
    item["available"] = snapshot_current and not item["mismatches"]
    return item


def _configured_services(payload, report):
    raw = payload.get("configured_services")
    valid = (
        isinstance(raw, list)
        and bool(raw)
        and all(isinstance(value, str) and value in REVIEW_SERVICES for value in raw)
        and len(raw) == len(set(raw))
    )
    if not valid:
        _mismatch(
            report,
            "configured_services_invalid",
            "Configured services must be a non-empty unique review-pool subset.",
        )
    present = {
        value for value in raw if isinstance(value, str)
    } if isinstance(raw, list) else set()
    configured = [service for service in REVIEW_SERVICES if service in present]
    report["configured_services"] = configured
    return configured


def _service_records(payload, configured, report):
    raw = payload.get("services")
    if not isinstance(raw, list):
        _mismatch(report, "services_invalid", "Services must be a list.")
        return {}
    records = {}
    for record in raw:
        name = record.get("service") if isinstance(record, dict) else None
        if not isinstance(name, str):
            _mismatch(report, "service_record_invalid", "A service record has no name.")
        elif name in records:
            _mismatch(report, "service_duplicate", "Service record is duplicated.", name)
        elif name not in configured:
            _mismatch(report, "service_unconfigured", "Service record is not configured.", name)
        else:
            records[name] = record
    for service in configured:
        if service not in records:
            _mismatch(report, "service_missing", "Configured service has no record.", service)
    return records


def audit_capacity(payload, *, as_of=None, max_age_seconds=3600):
    """Return a fail-closed capacity audit for one provider-neutral snapshot."""
    now = _as_of(as_of)
    report = _base_report(now, max_age_seconds)
    max_age_valid = _count(max_age_seconds, positive=True)
    if not max_age_valid:
        _mismatch(report, "max_age_invalid", "Maximum age must be a positive integer.")
    if not isinstance(payload, dict) or set(payload) != SNAPSHOT_KEYS:
        _mismatch(
            report,
            "snapshot_shape_invalid",
            "Snapshot fields are missing or unexpected.",
        )
    data = payload if isinstance(payload, dict) else {}
    report["snapshot"]["schema"] = data.get("schema")
    if data.get("schema") != SNAPSHOT_SCHEMA:
        _mismatch(report, "snapshot_schema_invalid", "Snapshot schema is unsupported.")

    observed = None
    try:
        observed = _parse_timestamp(data.get("observed_at"))
    except ValueError:
        _mismatch(report, "observed_at_invalid", "Observation time is missing or malformed.")
    snapshot_current = observed is not None and max_age_valid
    if observed is not None:
        age = (now - observed).total_seconds()
        report["snapshot"].update({"observed_at": _iso(observed), "age_seconds": age})
        if age < 0:
            snapshot_current = False
            _mismatch(report, "snapshot_from_future", "Snapshot is dated in the future.")
        elif max_age_valid and age > max_age_seconds:
            snapshot_current = False
            _mismatch(report, "snapshot_stale", "Snapshot exceeds the maximum age.")

    configured = _configured_services(data, report)
    records = _service_records(data, configured, report)
    report["services"] = [
        _classify_service(service, records[service], report, now, snapshot_current)
        for service in configured
        if service in records
    ]
    report["mismatches"].sort(
        key=lambda item: (item.get("service", ""), item["code"], item["message"]),
    )
    report["ok"] = (
        not report["mismatches"]
        and len(report["services"]) == len(configured)
        and all(item["available"] for item in report["services"])
    )
    return report


def _input_error(as_of, max_age_seconds, message):
    report = _base_report(as_of, max_age_seconds)
    _mismatch(report, "input_invalid", message)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="snapshot JSON path, or - for stdin")
    parser.add_argument("--as-of", help="ISO-8601 audit time; defaults to current UTC")
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        now = _as_of(args.as_of)
    except ValueError as exc:
        now = datetime.now(timezone.utc)
        report = _input_error(now, args.max_age_seconds, str(exc))
    else:
        try:
            raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(
                encoding="utf-8",
            )
            payload = parse_snapshot(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            report = _input_error(now, args.max_age_seconds, str(exc))
        else:
            report = audit_capacity(
                payload,
                as_of=now,
                max_age_seconds=args.max_age_seconds,
            )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
