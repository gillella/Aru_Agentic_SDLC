#!/usr/bin/env python3
"""Bootstrap the files and optional GitHub state for the minimal kernel."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from common import PROJECT_AUTH, KernelError, run

STATUSES = ("Backlog", "Ready", "In Progress", "In Review", "Done")
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


def write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise BootstrapError(f"refusing to overwrite existing file: {path}")
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o111)


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
        ".github/workflows/ci.yml": (
            framework / "templates" / "ci.yml"
        ).read_text(encoding="utf-8"),
        ".gitignore": "__pycache__/\n*.py[cod]\n.venv/\n.env\n.worktrees/\n",
    }
    written: list[str] = []
    for relative, content in outputs.items():
        write(destination / relative, content)
        written.append(relative)
    hook_dir = destination / ".aru" / "hooks"
    for name_in_repo in ("pre-push", "enforce_touches.py"):
        source = framework / "hooks" / name_in_repo
        target = hook_dir / name_in_repo
        write(target, source.read_text(encoding="utf-8"), executable=True)
        written.append(str(target.relative_to(destination)))
    if not (destination / ".git").exists():
        command(["git", "init", "-b", "main"], cwd=destination)
    git_hooks = Path(command(["git", "rev-parse", "--git-path", "hooks"], cwd=destination))
    if not git_hooks.is_absolute():
        git_hooks = destination / git_hooks
    git_hooks.mkdir(parents=True, exist_ok=True)
    for name_in_repo in ("pre-push", "enforce_touches.py"):
        shutil.copy2(hook_dir / name_in_repo, git_hooks / name_in_repo)
        (git_hooks / name_in_repo).chmod((git_hooks / name_in_repo).stat().st_mode | 0o111)
    return written


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
    return {"repository": slug, "project": project.get("url")}


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
