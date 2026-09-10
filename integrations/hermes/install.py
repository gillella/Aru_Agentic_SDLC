#!/usr/bin/env python3
"""Stage the external Hermes adapter. Preview by default; never activate jobs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


class InstallError(RuntimeError):
    """Installation cannot preserve the requested isolated destination."""


def _inside(path: Path, root: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise InstallError(f"Destination resolves outside Hermes home: {path}")


def _payload(source_root: Path, hermes_home: Path) -> list[tuple[Path, Path]]:
    package = source_root / "aru_project_driver"
    skill = source_root / "skill"
    if not (package / "driver.py").is_file() or not (skill / "SKILL.md").is_file():
        raise InstallError("Source package is incomplete: driver.py and skill/SKILL.md are required")
    quota_modules = [package / (name + ".py") for name in (
        "quota", "quota_collect", "quota_admission", "quota_boundary", "quota_worker", "quota_checkpoint")]
    if any(path.exists() for path in quota_modules) and not all(path.is_file() for path in quota_modules):
        raise InstallError("Source quota package is incomplete")
    result = []
    for source in sorted(package.rglob("*.py")):
        if "tests" in source.relative_to(package).parts or "__pycache__" in source.parts:
            continue
        result.append((source, hermes_home / "scripts" / "aru_project_driver" / source.relative_to(package)))
    for source in sorted(skill.rglob("*.md")):
        result.append((source, hermes_home / "skills" / "autonomous-ai-agents" / "hermes-project-driver" / source.relative_to(skill)))
    if (source_root / "QUOTA.md").is_file():
        result.append((source_root / "QUOTA.md", hermes_home / "skills" / "autonomous-ai-agents" / "hermes-project-driver" / "references" / "quota.md"))
    for source, destination in result:
        if source.is_symlink() or not source.resolve().is_relative_to(source_root.resolve()):
            raise InstallError(f"Refusing linked source outside the reviewed package: {source}")
        _inside(destination, hermes_home)
        if destination.exists() and not destination.is_file():
            raise InstallError(f"Destination is not a regular file: {destination}")
    return result


def _configured_home(config_path: Path, hermes_home: Path | None) -> Path:
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError) as exc:
        raise InstallError("An explicit readable JSON Driver configuration is required") from exc
    if not isinstance(config, dict):
        raise InstallError("Driver configuration must be a JSON object")
    kernel_root = config.get("kernel_root")
    if not isinstance(kernel_root, str) or not Path(kernel_root).is_absolute():
        raise InstallError("Configuration kernel_root must be an absolute path")
    if not Path(kernel_root).is_dir():
        raise InstallError("Configuration kernel_root must exist on the installation host")
    configured_home = config.get("hermes_home")
    if not isinstance(configured_home, str) or not Path(configured_home).is_absolute():
        raise InstallError("Configuration hermes_home must be an absolute path")
    destination_home = Path(hermes_home or configured_home).expanduser().resolve()
    if destination_home != Path(configured_home).resolve():
        raise InstallError("Installation home must match the explicit Driver configuration")
    return destination_home


def _webhook_mappings(config: dict) -> dict[str, str]:
    projects = config.get("projects")
    if not isinstance(projects, dict):
        raise InstallError("Webhook update requires explicitly configured projects")
    mappings = {}
    for project, values in projects.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", project) or not isinstance(values, dict):
            raise InstallError("Invalid project identity in webhook configuration")
        routes = values.get("webhook_subscriptions", [])
        if not isinstance(routes, list):
            raise InstallError("webhook_subscriptions must explicitly list native route IDs")
        for route in routes:
            if not isinstance(route, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", route):
                raise InstallError("Invalid native webhook subscription ID")
            if route in mappings:
                raise InstallError(f"Webhook subscription is multiply mapped: {route}")
            mappings[route] = project
    if not mappings:
        raise InstallError("No explicit project webhook_subscriptions are configured")
    return mappings


def _webhook_plan(config_path: Path, hermes_home: Path) -> dict:
    mappings = _webhook_mappings(json.loads(config_path.read_text()))
    destination = hermes_home / "webhook_subscriptions.json"
    _inside(destination, hermes_home)
    try:
        original = destination.read_bytes()
        subscriptions = json.loads(original)
    except (OSError, ValueError) as exc:
        raise InstallError("Cannot read existing native webhook subscriptions") from exc
    if not isinstance(subscriptions, dict):
        raise InstallError("Native webhook subscriptions must be an object keyed by route ID")
    spec = importlib.util.spec_from_file_location(
        "aru_install_scheduler", Path(__file__).parent / "aru_project_driver" / "scheduler.py"
    )
    scheduler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scheduler)
    updated = copy.deepcopy(subscriptions)
    changes = []
    for route, project in mappings.items():
        current = subscriptions.get(route)
        if not isinstance(current, dict):
            raise InstallError(f"Unknown native webhook subscription: {route}")
        secret = current.get("secret")
        if not isinstance(secret, str) or not secret.strip() or secret == "INSECURE_NO_AUTH":
            raise InstallError(f"Webhook subscription requires existing HMAC authentication: {route}")
        if current.get("deliver_only"):
            raise InstallError(f"Webhook subscription is delivery-only, not an agent trigger: {route}")
        driver_path = hermes_home / "scripts" / "aru_project_driver" / "driver.py"
        updated[route]["prompt"] = scheduler.webhook_prompt(project, config_path, driver_path, route)
        updated[route]["skills"] = ["hermes-project-driver"]
        changes.append({"subscription": route, "project": project, "changed": updated[route] != current})
    return {
        "destination": destination, "original": original,
        "content": (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode(),
        "changes": changes,
    }


def _current(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _rollback(written: list, hermes_home: Path) -> list[str]:
    failed = []
    for destination, before, after, mode in reversed(written):
        try:
            _inside(destination, hermes_home)
            if _current(destination) != after:
                raise InstallError("another writer changed an installed file")
            if before is None:
                destination.unlink()
            else:
                _atomic_write(destination, before)
                destination.chmod(mode)
        except (OSError, InstallError):
            failed.append(str(destination.relative_to(hermes_home)))
    return failed


def _apply_changes(plan: list, hermes_home: Path, backup_root: Path) -> list[str]:
    backups, written = [], []
    try:
        # Validate the complete source/webhook transaction before its first write.
        for destination, before, _after, _mode in plan:
            _inside(destination, hermes_home)
            if _current(destination) != before:
                raise InstallError("Destination changed during installation; preview again")
        for destination, before, after, mode in plan:
            _inside(destination, hermes_home)
            if _current(destination) != before:
                raise InstallError("Destination changed during installation; preview again")
            if before is not None:
                backup = backup_root / destination.relative_to(hermes_home)
                _atomic_write(backup, before)
                backups.append(str(backup))
            _atomic_write(destination, after)
            written.append((destination, before, after, mode))
    except (OSError, InstallError) as exc:
        failed = _rollback(written, hermes_home)
        recovery = ("rollback needs manual recovery: " + ", ".join(failed)) if failed else "prior files restored"
        raise InstallError(f"Installation failed; {recovery}; backups: {backup_root}; {exc}") from exc
    return backups


def _atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".aru-install-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(
    source_root: Path, config_path: Path, *, hermes_home: Path | None = None,
    apply: bool = False, update_webhooks: bool = False,
) -> dict:
    """Preview or copy reviewed source; retain a backup for every replaced file."""
    source_root = Path(source_root).expanduser().resolve()
    config_path = Path(config_path).expanduser().resolve()
    destination_home = _configured_home(config_path, hermes_home)
    payload = _payload(source_root, destination_home)
    webhook_plan = _webhook_plan(config_path, destination_home) if update_webhooks else None
    changes, plan = [], []
    for source, destination in payload:
        content = source.read_bytes()
        before = _current(destination)
        changed = before != content
        if changed:
            plan.append((destination, before, content, destination.stat().st_mode & 0o777 if before is not None else None))
        changes.append({
            "source": str(source), "destination": str(destination),
            "sha256": hashlib.sha256(content).hexdigest(), "changed": changed,
        })
    if webhook_plan and any(change["changed"] for change in webhook_plan["changes"]):
        destination = webhook_plan["destination"]
        plan.append((destination, webhook_plan["original"], webhook_plan["content"],
                     destination.stat().st_mode & 0o777))
    backup_root = destination_home / "state" / "aru_project_driver" / "install-backups" / uuid.uuid4().hex
    _inside(backup_root, destination_home)
    backups = _apply_changes(plan, destination_home, backup_root) if apply else []
    return {
        "applied": apply, "activated": False, "hermes_home": str(destination_home),
        "config_path": str(config_path), "files": changes, "backups": backups,
        "webhook_changes": webhook_plan["changes"] if webhook_plan else [],
        "next_step": "Review configuration and run Driver start only when runtime activation is authorized.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--apply", action="store_true", help="Copy source after reviewing the default preview")
    parser.add_argument("--update-webhooks", action="store_true", help="Update only explicitly mapped existing webhook prompts and skills")
    args = parser.parse_args()
    try:
        result = install(
            Path(__file__).resolve().parent, args.config, hermes_home=args.hermes_home,
            apply=args.apply, update_webhooks=args.update_webhooks,
        )
    except InstallError as exc:
        parser.exit(2, f"Installation blocked: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
