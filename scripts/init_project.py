#!/usr/bin/env python3
"""Bootstrap the files and optional GitHub state for the minimal kernel."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from common import PROJECT_AUTH, REPOSITORY_AUTH, KernelError, run

STATUSES = ("Backlog", "Ready", "In Progress", "In Review", "Done")
GITHUB_ACTIONS_APP_ID = 15368
LABELS = {
    "status:backlog": ("ededed", "Board status: Backlog"),
    "status:ready": ("1d76db", "Board status: Ready"),
    "status:in-progress": ("fbca04", "Board status: In Progress"),
    "status:in-review": ("d93f0b", "Board status: In Review"),
    "status:done": ("0e8a16", "Board status: Done"),
    "type:feat": ("a2eeef", "New capability"),
    "type:fix": ("d73a4a", "Defect repair"),
    "type:docs": ("0075ca", "Documentation"),
    "priority:p0": ("b60205", "Blocking; drop everything"),
    "priority:p1": ("d93f0b", "Current phase critical path"),
    "priority:p2": ("fbca04", "Current phase, not critical path"),
    "priority:p3": ("c5def5", "Opportunistic"),
    "review:coderabbit": ("0e8a16", "External review: CodeRabbit"),
    "review:sourcery": ("0e8a16", "External review: Sourcery"),
    "review:codeant": ("0e8a16", "External review: CodeAnt"),
    "review:claude-code": ("5319e7", "Coding-agent review: Claude Code"),
    "review:openai-codex": ("5319e7", "Coding-agent review: OpenAI Codex"),
    "review:xai-cursor": ("5319e7", "Coding-agent review: xAI Cursor"),
    "review:google-antigravity": (
        "5319e7",
        "Coding-agent review: Google Antigravity",
    ),
}


class BootstrapError(RuntimeError):
    pass


def command(
    argv: list[str],
    *,
    cwd: Path,
    json_output: bool = False,
    auth: str | None = None,
):
    try:
        result = run(argv, cwd=cwd, auth=auth)
    except KernelError as exc:
        raise BootstrapError(str(exc)) from exc
    if not json_output:
        return result.stdout.strip()
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError("command returned malformed JSON") from exc


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,99}", value):
        raise BootstrapError("project name must be 2-100 safe characters")
    return value


def safe_destination_path(destination: Path, relative: str) -> Path:
    """Return one contained path, rejecting every existing symlink component."""
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise BootstrapError(f"unsafe scaffold path: {relative}")
    current = destination
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            raise BootstrapError(f"refusing symbolic-link scaffold path: {current}")
    try:
        current.resolve(strict=False).relative_to(destination)
    except ValueError as exc:
        raise BootstrapError(f"scaffold path escapes destination: {current}") from exc
    return current


def write(
    destination: Path,
    relative: str,
    content: str,
    *,
    executable: bool = False,
) -> None:
    path = safe_destination_path(destination, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after directory creation so a pre-existing nested link cannot be
    # hidden behind a missing parent during the first inspection.
    path = safe_destination_path(destination, relative)
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise BootstrapError(f"refusing to overwrite existing file: {path}")
    else:
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise BootstrapError(f"refusing concurrent path replacement: {path}") from exc
    if executable:
        path.chmod(path.stat().st_mode | 0o111)


def contained_git_hooks(destination: Path) -> Path:
    """Validate that Git's canonical hooks directory stays inside destination."""
    common_raw = Path(command(["git", "rev-parse", "--git-common-dir"], cwd=destination))
    common = common_raw if common_raw.is_absolute() else destination / common_raw
    common = common.resolve(strict=True)
    try:
        common.relative_to(destination)
    except ValueError as exc:
        raise BootstrapError("Git common directory escapes scaffold destination") from exc

    canonical = (common / "hooks").resolve(strict=False)
    configured_raw = Path(
        command(["git", "rev-parse", "--git-path", "hooks"], cwd=destination)
    )
    configured = (
        configured_raw if configured_raw.is_absolute() else destination / configured_raw
    ).resolve(strict=False)
    if configured != canonical:
        raise BootstrapError("refusing non-canonical Git hooks path during bootstrap")
    try:
        configured.relative_to(destination)
    except ValueError as exc:
        raise BootstrapError("Git hooks directory escapes scaffold destination") from exc
    return configured


def scaffold(name: str, directory: Path) -> list[str]:
    safe_name(name)
    destination = directory.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    framework = Path(__file__).resolve().parents[1]
    outputs = {
        "AGENTS.md": (framework / "templates" / "AGENTS.md").read_text(encoding="utf-8"),
        ".github/ISSUE_TEMPLATE/governed-task.yml": (
            framework / "templates" / "issue.yml"
        ).read_text(encoding="utf-8"),
        ".github/PULL_REQUEST_TEMPLATE.md": (
            framework / "templates" / "pull_request.md"
        ).read_text(encoding="utf-8"),
        ".github/workflows/governed-pr.yml": (
            framework / "templates" / "governed-pr.yml"
        ).read_text(encoding="utf-8"),
        ".aru/verify.sh": (framework / "templates" / "verify.sh").read_text(
            encoding="utf-8"
        ),
        ".aru/lib/touches.py": (framework / "scripts" / "touches.py").read_text(
            encoding="utf-8"
        ),
        ".gitignore": "__pycache__/\n*.py[cod]\n.venv/\n.env\n.worktrees/\n",
    }
    written: list[str] = []
    for relative, content in outputs.items():
        write(
            destination,
            relative,
            content,
            executable=relative == ".aru/verify.sh",
        )
        written.append(relative)
    for name_in_repo in ("pre-push", "enforce_touches.py"):
        source = framework / "hooks" / name_in_repo
        relative = f".aru/hooks/{name_in_repo}"
        write(destination, relative, source.read_text(encoding="utf-8"), executable=True)
        written.append(relative)
    git_entry = safe_destination_path(destination, ".git")
    if not git_entry.exists():
        command(["git", "init", "-b", "main"], cwd=destination)
    contained_git_hooks(destination)
    command([str(framework / "scripts" / "install_hooks.sh")], cwd=destination)
    return written


def ruleset_payload() -> dict[str, object]:
    """Return the minimal server-enforced merge boundary for consumers."""
    return {
        "name": "aru-protect-default",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_approving_review_count": 0,
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "do_not_enforce_on_create": True,
                    "required_status_checks": [
                        {
                            "context": "aru-governed-pr",
                            "integration_id": GITHUB_ACTIONS_APP_ID,
                        }
                    ],
                    "strict_required_status_checks_policy": True,
                },
            },
        ],
    }


def provision_ruleset(slug: str, directory: Path) -> dict[str, object] | str:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(ruleset_payload(), handle, sort_keys=True)
            temporary = Path(handle.name)
        return command(
            [
                "gh",
                "api",
                f"repos/{slug}/rulesets",
                "--method",
                "POST",
                "--input",
                str(temporary),
            ],
            cwd=directory,
            json_output=True,
            auth=REPOSITORY_AUTH,
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def github_setup(name: str, directory: Path, private: bool) -> dict[str, object]:
    visibility = "--private" if private else "--public"
    command(
        [
            "gh",
            "repo",
            "create",
            name,
            visibility,
            "--source",
            str(directory),
            "--remote",
            "origin",
        ],
        cwd=directory,
    )
    view = command(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        cwd=directory,
        json_output=True,
    )
    slug = view["nameWithOwner"]
    for label, (color, description) in LABELS.items():
        command(
            [
                "gh",
                "label",
                "create",
                label,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            cwd=directory,
        )
    owner = slug.split("/", 1)[0]
    project = command(
        [
            "gh",
            "project",
            "create",
            "--owner",
            owner,
            "--title",
            f"{name} Delivery",
            "--format",
            "json",
        ],
        cwd=directory,
        json_output=True,
    )
    number = str(project["number"])
    command(
        ["gh", "project", "link", number, "--owner", owner, "--repo", slug],
        cwd=directory,
    )
    fields = command(
        ["gh", "project", "field-list", number, "--owner", owner, "--format", "json"],
        cwd=directory,
        json_output=True,
    )
    status_fields = [field for field in fields.get("fields", []) if field.get("name") == "Status"]
    if len(status_fields) != 1:
        raise BootstrapError("new Project Board has no unique Status field")
    mutation = """
    mutation($field:ID!){
      updateProjectV2Field(input:{
        projectV2FieldId:$field
        singleSelectOptions:[
          {name:"Backlog",color:GRAY,description:""}
          {name:"Ready",color:BLUE,description:""}
          {name:"In Progress",color:YELLOW,description:""}
          {name:"In Review",color:ORANGE,description:""}
          {name:"Done",color:GREEN,description:""}
        ]
      }){
        projectV2Field{... on ProjectV2SingleSelectField{id}}
      }
    }
    """
    command(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-F",
            f"field={status_fields[0]['id']}",
        ],
        cwd=directory,
        auth=PROJECT_AUTH,
    )
    ruleset = provision_ruleset(slug, directory)
    return {
        "repository": slug,
        "project": project.get("url"),
        "ruleset": ruleset.get("_links", {}).get("html", {}).get("href")
        if isinstance(ruleset, dict)
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args()
    try:
        written = scaffold(args.name, args.directory)
        remote = (
            github_setup(args.name, args.directory.resolve(), args.private)
            if args.github
            else None
        )
    except BootstrapError as exc:
        parser.error(str(exc))
    print(f"bootstrapped {args.directory.resolve()} ({len(written)} files)")
    if remote:
        print(remote)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
