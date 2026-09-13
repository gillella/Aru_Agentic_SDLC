#!/usr/bin/env python3
"""Bringing a consumer repository into the kernel, and keeping it current.

Three lifecycles meet here. `init_project.py` creates a governed repository from
nothing. This module handles the two that act on a repository which already
exists: adoption, for one that has code and history but no governance, and sync,
for one already governed whose copied files have fallen behind the framework.

Both render from `init_project.framework_files()` rather than from templates of
their own. A file rendered in one place and compared in another is how a
consumer ends up running a gate the framework no longer ships.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import merge_authority
from common import KernelError, checkout_repository
from init_project import (
    CONSUMER_OWNED,
    EXECUTABLE,
    BootstrapError,
    command,
    contained_git_hooks,
    framework_files,
    profile_spec,
    provision_github,
    reprovision_ruleset,
    resolve_runner_profile,
    safe_destination_path,
    write,
)

# Both must exist before a directory is treated as governed, so a sync refuses
# an unrelated repository instead of half-converting it.
GOVERNED_MARKERS = (".aru/verify.sh", ".github/workflows/governed-pr.yml")

RUNNER_PROFILE_MARKER = "# aru-runner-profile: "

# Where a repository's own copy of a framework-owned file is kept when adoption
# replaces it. Adoption never discards work: the operator merges and deletes.
PRE_ADOPTION_SUFFIX = ".pre-aru"


def detect_runner_profile(destination: Path) -> str:
    """The profile this repository already declares, read from its own workflow.

    Taken from the workflow marker rather than from the account policy: a sync
    re-renders what this repository runs. Resolving the account again would
    silently move a repository onto a different profile, which is a change of
    where its code executes, not a refresh.
    """
    workflow = safe_destination_path(destination, ".github/workflows/governed-pr.yml")
    declared = [
        line[len(RUNNER_PROFILE_MARKER):].strip()
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if line.startswith(RUNNER_PROFILE_MARKER)
    ]
    if len(declared) != 1:
        raise BootstrapError(
            "governed workflow must declare exactly one '# aru-runner-profile:' line"
        )
    profile_spec(declared[0])
    return declared[0]


def require_governed(directory: Path) -> Path:
    destination = directory.expanduser().resolve()
    for marker in GOVERNED_MARKERS:
        if not safe_destination_path(destination, marker).is_file():
            raise BootstrapError(
                f"{destination} is not a governed repository ({marker} is absent); "
                "scaffold it before syncing"
            )
    return destination


def sync_report(directory: Path) -> dict[str, Any]:
    """Compare a governed repository against the current framework. Reads only.

    Consumer-owned files are reported as preserved rather than compared: they
    are expected to differ, and flagging them would train the operator to ignore
    the output.
    """
    destination = require_governed(directory)
    profile = detect_runner_profile(destination)
    current: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    for relative, content in framework_files(profile).items():
        if relative in CONSUMER_OWNED:
            continue
        path = safe_destination_path(destination, relative)
        if not path.is_file():
            missing.append(relative)
        elif path.read_text(encoding="utf-8") != content:
            stale.append(relative)
        else:
            current.append(relative)
    return {
        "directory": str(destination),
        "runner_profile": profile,
        "current": sorted(current),
        "stale": sorted(stale),
        "missing": sorted(missing),
        "preserved": sorted(CONSUMER_OWNED),
        "in_sync": not stale and not missing,
    }


def sync_apply(directory: Path) -> dict[str, Any]:
    """Re-render every framework-owned file that diverged, and nothing else."""
    report = sync_report(directory)
    destination = Path(report["directory"])
    rewritten = sorted([*report["stale"], *report["missing"]])
    if rewritten:
        # Re-rendered from the profile the repository declares, so a sync never
        # relocates where its verification runs.
        files = framework_files(report["runner_profile"])
        for relative in rewritten:
            write(destination, relative, files[relative],
                  executable=relative in EXECUTABLE, replace=True)
    return {**report, "rewritten": rewritten, "in_sync": True}


def adopt_plan(directory: Path, runner_profile: str) -> dict[str, Any]:
    """What adopting this repository would do. Reads only.

    Adoption is not scaffolding. A repository that already has code owns files
    the framework also writes: its `.gitignore` carries language and toolchain
    entries, and its `AGENTS.md` may carry instructions of its own. Nothing
    existing is discarded. Files the consumer owns are left exactly as they
    stand; a framework-owned file that already exists is kept beside the new one
    under a `.pre-aru` suffix, so the operator can merge and the change is
    reversible.
    """
    destination = directory.expanduser().resolve()
    if not (destination / ".git").exists():
        raise BootstrapError(f"{destination} is not a git repository; adoption needs one that exists")
    if all(safe_destination_path(destination, marker).is_file() for marker in GOVERNED_MARKERS):
        raise BootstrapError(f"{destination} is already governed; use --sync to bring it up to date")
    fresh: list[str] = []
    preserve: list[str] = []
    keep: list[str] = []
    for relative, content in framework_files(runner_profile).items():
        path = safe_destination_path(destination, relative)
        if not path.is_file():
            fresh.append(relative)
        elif relative in CONSUMER_OWNED or path.read_text(encoding="utf-8") == content:
            keep.append(relative)
        else:
            preserve.append(relative)
    return {
        "directory": str(destination),
        "runner_profile": runner_profile,
        "write": sorted(fresh),
        "preserve_existing_as": sorted(preserve),
        "keep_untouched": sorted(keep),
    }


def adopt(directory: Path, runner_profile: str) -> dict[str, Any]:
    """Bring an ungoverned repository under the kernel without discarding work."""
    plan = adopt_plan(directory, runner_profile)
    destination = Path(plan["directory"])
    files = framework_files(runner_profile)
    for relative in plan["preserve_existing_as"]:
        existing = safe_destination_path(destination, relative).read_text(encoding="utf-8")
        write(destination, f"{relative}{PRE_ADOPTION_SUFFIX}", existing,
              executable=relative in EXECUTABLE, replace=True)
        write(destination, relative, files[relative],
              executable=relative in EXECUTABLE, replace=True)
    for relative in plan["write"]:
        write(destination, relative, files[relative], executable=relative in EXECUTABLE)
    contained_git_hooks(destination)
    command([str(Path(__file__).resolve().parents[1] / "scripts" / "install_hooks.sh")],
            cwd=destination)
    return {**plan, "adopted": True, "commit": commit_adoption(destination, plan)}


ADOPTION_MESSAGE = """chore(governance): adopt the Aru kernel

Brings the repository under the minimal issue-to-safe-merge kernel: the governed
pull request and merge policy workflows, the consumer verifier, the touches
helper, the review declaration, and the agent contract.

From here every change needs a Ready issue, an exclusive claim, an isolated
worktree, a pull request carrying its closing directive, an exact-head
verification check, one approval from an account other than the author, and a
merge performed only by the kernel's merge helper.
"""


def commit_adoption(destination: Path, plan: dict[str, Any]) -> str | None:
    """Commit exactly what adoption wrote. None when there was nothing to write.

    Only the adopted paths are staged, never `--all`: the repository being
    adopted may have unrelated work in progress, and sweeping it into the
    governance commit would be a surprising thing to do to someone's tree.
    """
    written = [*plan["write"], *plan["preserve_existing_as"]]
    if not written:
        return None
    staged = [*written, *(f"{r}{PRE_ADOPTION_SUFFIX}" for r in plan["preserve_existing_as"])]
    command(["git", "add", "--", *staged], cwd=destination)
    command(["git", "commit", "-m", ADOPTION_MESSAGE], cwd=destination)
    return command(["git", "rev-parse", "HEAD"], cwd=destination)


def push_adoption(destination: Path) -> str:
    """Put the adoption commit on the default branch, before anything enforces.

    Ordering is the whole point. `aru-merge-policy` runs the *base branch's*
    copy of a workflow that adoption is itself installing, so a ruleset
    provisioned first leaves the repository unable to accept the very commit
    that would govern it -- including a pull request that would fix it.

    `--no-verify` is deliberate and specific to this one commit: adoption has
    just installed the pre-push hook that refuses direct pushes to the default
    branch, and this is the commit installing it. Every later change goes
    through a pull request, where that hook is not in the way.
    """
    branch = command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=destination)
    command(["git", "push", "--no-verify", "origin", f"HEAD:{branch}"], cwd=destination)
    return branch


def run_sync(args: argparse.Namespace) -> int:
    """Bring an already-governed repository up to the current framework."""
    if args.name or args.github or args.owner or args.runner_profile:
        raise BootstrapError("--sync takes --directory, and optionally --check or --ruleset")
    report = sync_report(args.directory) if args.check else sync_apply(args.directory)
    if args.ruleset and not args.check:
        destination = Path(report["directory"])
        report["ruleset"] = reprovision_ruleset(
            checkout_repository(destination), destination, args.merge_app_id
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    changed = report.get("rewritten", [*report["stale"], *report["missing"]])
    print(f"{report['directory']} ({report['runner_profile']})")
    if not changed:
        print(f"  in sync: {len(report['current'])} framework files current, nothing to do")
    else:
        verb = "would rewrite" if args.check else "rewrote"
        for relative in changed:
            print(f"  {verb}: {relative}")
    print(f"  preserved (consumer-owned): {', '.join(report['preserved'])}")
    return 0


def run_adopt(args: argparse.Namespace) -> int:
    """Bring an existing, ungoverned repository under the kernel."""
    if args.name or args.sync:
        raise BootstrapError("--adopt takes --directory, and optionally --check, --owner and --github")
    profile = resolve_runner_profile(args.owner, args.runner_profile)
    report = adopt_plan(args.directory, profile) if args.check else adopt(args.directory, profile)
    if args.github and not args.check:
        destination = Path(report["directory"])
        try:
            merge_app = merge_authority.configured()
        except KernelError as exc:
            raise BootstrapError(str(exc)) from exc
        slug = checkout_repository(destination)
        # The commit goes first. Provisioning a ruleset before the governing
        # workflows are on the default branch seals the repository against the
        # very commit that would govern it.
        report["pushed_to"] = push_adoption(destination)
        try:
            report["github"] = provision_github(
                slug, destination.name, destination, runner_profile=profile, merge_app=merge_app,
            )
        except (BootstrapError, KernelError) as exc:
            # The adoption commit is already on the default branch, so --adopt
            # will now refuse this repository as governed. Name the command that
            # does finish the job rather than leaving it to be worked out.
            raise BootstrapError(
                f"{exc}\n\nThe adoption commit reached {slug}; only provisioning failed. "
                f"The repository is governed but has no ruleset. Finish with:\n"
                f"    init_project.py --sync --ruleset --directory {destination}"
            ) from exc
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    verb = "would write" if args.check else "wrote"
    print(f"{report['directory']} ({report['runner_profile']})")
    for relative in report["write"]:
        print(f"  {verb}: {relative}")
    for relative in report["preserve_existing_as"]:
        print(f"  {verb}: {relative}  (yours kept as {relative}{PRE_ADOPTION_SUFFIX} -- merge it)")
    print(f"  left untouched: {len(report['keep_untouched'])} file(s) the repository already owns")
    if report.get("commit"):
        print(f"  committed: {report['commit'][:7]}")
    elif not args.check:
        print("  committed: nothing to commit; the framework files were already present")
    if report.get("pushed_to"):
        print(f"  pushed to: {report['pushed_to']}")
    if report.get("github"):
        print(f"  provisioned: {report['github']['repository']}")
    return 0

