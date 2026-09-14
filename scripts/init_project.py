#!/usr/bin/env python3
"""Bootstrap the files and optional GitHub state for the minimal kernel."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import manifest
import merge_authority
import policy
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
}

# Consumer runner profiles. The check name, exact-head binding, read-only
# permissions, event provenance and touches enforcement are identical for every
# profile; only the compute target and its diagnostics differ.
RUNNER_PROFILES = {
    "self-hosted-mac": {
        "runs_on": "[self-hosted, macOS, ARM64, aru-ci]",
        "trust_step": "Validate self-hosted runner trust boundary",
        "trust_message": (
            "Only verified pull_request events from this repository may execute "
            "on persistent self-hosted runners."
        ),
        "target": "operator-owned `[self-hosted, macOS, ARM64, aru-ci]` Macs",
        "rule": (
            "Never fall back to a GitHub-hosted runner; an offline pool leaves "
            "merge blocked."
        ),
    },
    "github-hosted": {
        "runs_on": "ubuntu-latest",
        "trust_step": "Validate hosted runner trust boundary",
        "trust_message": (
            "Only verified pull_request events from this repository may execute "
            "the governed check."
        ),
        "target": "GitHub-hosted `ubuntu-latest` Actions runners",
        "rule": (
            "Never dispatch this repository to a self-hosted runner; hosted "
            "verification must never reach a personal machine."
        ),
    },
}

# Account assignments are declared in scripts/policy.toml, not compiled in here,
# so adopting Aru under a new account is a data change rather than a source patch.
# An account absent from the table still has no default: it must pass
# --runner-profile explicitly. There is no fallback and no cross-profile reroute.
ACCOUNT_RUNNER_PROFILES = policy.account_runner_profiles()

SCAFFOLD_TOKEN = re.compile(r"__ARU_[A-Z0-9_]+__")


class BootstrapError(RuntimeError):
    pass


def profile_spec(profile: object) -> dict[str, str]:
    if not isinstance(profile, str) or profile not in RUNNER_PROFILES:
        raise BootstrapError(f"unknown runner profile: {profile!r}")
    return RUNNER_PROFILES[profile]


def account_runner_profile(owner: str) -> str | None:
    """The account's declared profile, or None when it has no assignment."""
    return ACCOUNT_RUNNER_PROFILES.get(owner.casefold())


def resolve_runner_profile(owner: str | None, declared: str | None) -> str:
    """Select exactly one profile from the account policy and any declaration."""
    if declared is not None:
        profile_spec(declared)
    if owner is None:
        if declared is None:
            raise BootstrapError("--owner or --runner-profile must select a runner profile")
        return declared
    assigned = account_runner_profile(owner)
    if assigned is None:
        # No assignment is not a refusal by name: the account declares its own
        # profile. Declaring nothing is still refused, because there is no default.
        if declared is None:
            raise BootstrapError(
                f"account {owner} has no declared runner profile; pass --runner-profile"
            )
        return declared
    if declared is not None and declared != assigned:
        raise BootstrapError(
            f"account {owner} is assigned the {assigned} runner profile, not {declared}"
        )
    return assigned


def review_declaration(reviewers: list[str] | None) -> str:
    """The repository's review posture, seeded so a new repository is not deadlocked.

    The strict posture with nobody authorized refuses every merge including the first, so
    the account creating the repository is seeded as its first authorized reviewer.
    """
    return json.dumps(
        {"authority": "human", "reviewers": list(reviewers or [])}, indent=2
    ) + "\n"


def render_profile(content: str, profile: str) -> str:
    spec = profile_spec(profile)
    for token, value in (
        ("__ARU_RUNNER_PROFILE__", profile),
        ("__ARU_RUNS_ON__", spec["runs_on"]),
        ("__ARU_TRUST_STEP__", spec["trust_step"]),
        ("__ARU_TRUST_MESSAGE__", spec["trust_message"]),
        ("__ARU_RUNNER_TARGET__", spec["target"]),
        ("__ARU_RUNNER_RULE__", spec["rule"]),
    ):
        content = content.replace(token, value)
    unresolved = SCAFFOLD_TOKEN.search(content)
    if unresolved:
        raise BootstrapError(f"unresolved scaffold token: {unresolved.group(0)}")
    return content


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
    replace: bool = False,
) -> None:
    path = safe_destination_path(destination, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after directory creation so a pre-existing nested link cannot be
    # hidden behind a missing parent during the first inspection.
    path = safe_destination_path(destination, relative)
    if path.exists():
        # Anything that is not a regular file is refused even under replace: the
        # point of the check is that a symlink or directory planted at this path
        # must never be written through, and a sync is no more entitled to that
        # than a bootstrap is.
        if not path.is_file():
            raise BootstrapError(f"refusing to overwrite existing file: {path}")
        if path.read_text(encoding="utf-8") != content:
            if not replace:
                raise BootstrapError(f"refusing to overwrite existing file: {path}")
            path.write_text(content, encoding="utf-8")
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


# Files the consumer owns once bootstrap has run. A sync never rewrites these:
# the reviewer list and the project's own verification are decisions the
# repository made, not framework content to be replaced underneath it.
CONSUMER_OWNED = (".aru/review.json", ".aru/verify-project.sh", ".gitignore")

# Written with the executable bit; everything else is plain.
EXECUTABLE = (".aru/verify.sh", ".aru/verify-project.sh",
              ".aru/hooks/pre-push", ".aru/hooks/enforce_touches.py",
              ".aru/hooks/check_manifest.py")

# Both must exist before a directory is treated as governed, so a sync refuses
# an unrelated repository instead of half-converting it.

# The exact-head verification context. Every generation of the kernel has
# required it, which is what makes it a durable way to recognise the kernel's
# own ruleset after the declared name has changed. A test holds it to the
# policy's own list.
GOVERNED_CONTEXT = "aru-governed-pr"

# Where a repository's own copy of a framework-owned file is kept when adoption
# replaces it. Adoption never discards work: the operator merges and deletes.


def framework_files(
    runner_profile: str, reviewers: list[str] | None = None, *, canonical: bool = True,
) -> dict[str, str]:
    """Every file bootstrap writes, as repository-relative path -> content.

    Scaffold and sync both render from here on purpose. The moment one of them
    renders a file the other does not, a consumer starts running a gate the
    framework no longer ships -- which is the drift this function exists to make
    impossible.

    `.aru/manifest.json` is COPIED from the Factory's committed
    `templates/manifests/<profile>.json`, never computed here, so a consumer's
    manifest is byte-identical to the one a test pins against these same files.
    `canonical=False` omits it, which is how the renderer hashes the other files
    without reading the manifest it is about to produce.
    """
    profile_spec(runner_profile)
    framework = Path(__file__).resolve().parents[1]

    def template(name: str) -> str:
        return (framework / "templates" / name).read_text(encoding="utf-8")

    files = {
        "AGENTS.md": render_profile(template("AGENTS.md"), runner_profile),
        ".github/ISSUE_TEMPLATE/governed-task.yml": template("issue.yml"),
        ".github/PULL_REQUEST_TEMPLATE.md": template("pull_request.md"),
        ".github/workflows/governed-pr.yml": render_profile(
            template("governed-pr.yml"), runner_profile),
        ".github/workflows/merge-policy.yml": render_profile(
            template("merge-policy.yml"), runner_profile),
        ".aru/review.json": review_declaration(reviewers),
        ".aru/verify.sh": template("verify.sh"),
        ".aru/verify-project.sh": template("verify-project.sh"),
        ".aru/lib/touches.py": (framework / "scripts" / "touches.py").read_text(encoding="utf-8"),
        ".gitignore": "__pycache__/\n*.py[cod]\n.venv/\n.env\n.worktrees/\n",
    }
    for hook in ("pre-push", "enforce_touches.py", "check_manifest.py"):
        files[f".aru/hooks/{hook}"] = (framework / "hooks" / hook).read_text(encoding="utf-8")
    files[manifest.VERSION_PATH] = policy.version() + "\n"
    if canonical:
        files[manifest.MANIFEST_PATH] = manifest.canonical_path(runner_profile).read_text(
            encoding="utf-8")
    return files


def scaffold(
    name: str, directory: Path, *, runner_profile: str,
    reviewers: list[str] | None = None,
) -> list[str]:
    safe_name(name)
    destination = directory.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    framework = Path(__file__).resolve().parents[1]
    written: list[str] = []
    for relative, content in framework_files(runner_profile, reviewers).items():
        write(destination, relative, content, executable=relative in EXECUTABLE)
        written.append(relative)
    git_entry = safe_destination_path(destination, ".git")
    if not git_entry.exists():
        command(["git", "init", "-b", "main"], cwd=destination)
    contained_git_hooks(destination)
    command([str(framework / "scripts" / "install_hooks.sh")], cwd=destination)
    return written



def ruleset_contexts(slug: str, ruleset_id: int, directory: Path) -> set[str]:
    """The status contexts one ruleset requires. The list endpoint omits rules."""
    detail = command(
        ["gh", "api", f"repos/{slug}/rulesets/{ruleset_id}"],
        cwd=directory, json_output=True, auth=REPOSITORY_AUTH,
    )
    if not isinstance(detail, dict) or not isinstance(detail.get("rules"), list):
        raise BootstrapError(f"{slug}: ruleset {ruleset_id} is unreadable")
    contexts: set[str] = set()
    for rule in detail["rules"]:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
            if isinstance(check, dict) and isinstance(check.get("context"), str):
                contexts.add(check["context"])
    return contexts


def governed_ruleset_id(slug: str, directory: Path) -> int | None:
    """The id of the kernel's own ruleset on this repository, or None.

    Identified by the exact-head verification context it requires rather than by
    its declared name. Matching on the name missed every repository scaffolded
    before that name last changed, and the create path then added a second
    ruleset enforcing alongside the first, with no way to tell which one refused
    a merge. A name can be renamed; the context this kernel posts cannot be
    anything else.
    """
    inventory = command(
        ["gh", "api", f"repos/{slug}/rulesets"],
        cwd=directory, json_output=True, auth=REPOSITORY_AUTH,
    )
    if not isinstance(inventory, list):
        raise BootstrapError(f"{slug}: ruleset inventory is unreadable")
    owned: list[int] = []
    for entry in inventory:
        if not isinstance(entry, dict) or entry.get("target") not in (None, "branch"):
            continue
        ruleset_id = entry.get("id")
        if not isinstance(ruleset_id, int):
            raise BootstrapError(f"{slug}: a ruleset has no readable id")
        if GOVERNED_CONTEXT in ruleset_contexts(slug, ruleset_id, directory):
            owned.append(ruleset_id)
    if len(owned) > 1:
        raise BootstrapError(
            f"{slug} carries {len(owned)} rulesets requiring {GOVERNED_CONTEXT!r} "
            f"(ids {sorted(owned)}); resolve by hand"
        )
    return owned[0] if owned else None


def reprovision_ruleset(
    slug: str, directory: Path, merge_app_id: int | None = None
) -> dict[str, object] | str:
    """Replace the kernel's ruleset on this repository, or create it when absent.

    A plain re-POST would leave two rulesets both enforcing, so an existing one
    is updated in place. Updating also renames it, which is how a repository
    scaffolded under an earlier declared name catches up.
    """
    ruleset_id = governed_ruleset_id(slug, directory)
    if ruleset_id is None:
        return provision_ruleset(slug, directory, merge_app_id)
    return _ruleset_request("PUT", f"repos/{slug}/rulesets/{ruleset_id}", merge_app_id, directory)


def _ruleset_request(
    method: str, path: str, merge_app_id: int | None, directory: Path
) -> dict[str, object] | str:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(ruleset_payload(merge_app_id), handle, sort_keys=True)
            temporary = Path(handle.name)
        return command(
            ["gh", "api", path, "--method", method, "--input", str(temporary)],
            cwd=directory, json_output=True, auth=REPOSITORY_AUTH,
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ruleset_payload(merge_app_id: int | None = None) -> dict[str, object]:
    """Return the minimal server-enforced merge boundary for consumers.

    Built from the declaration in scripts/policy.toml so the boundary a consumer
    is scaffolded with and the boundary docs/ENFORCEMENT-REGISTER.md documents
    cannot drift apart.
    """
    declared = policy.ruleset_parameters()
    checks = [
        {"context": context, "integration_id": GITHUB_ACTIONS_APP_ID}
        for context in declared["required_checks"]
    ]
    if merge_app_id is not None:
        checks.append({"context": declared["merge_authority_check"], "integration_id": merge_app_id})
    return {
        "name": declared["name"],
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": list(declared["bypass_actors"]),
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": list(declared["allowed_merge_methods"]),
                    "dismiss_stale_reviews_on_push": declared["dismiss_stale_reviews_on_push"],
                    "require_code_owner_review": declared["require_code_owner_review"],
                    "require_extra_approval_for_unattributed_changes":
                        declared["require_extra_approval_for_unattributed_changes"],
                    "require_last_push_approval": declared["require_last_push_approval"],
                    "required_approving_review_count": declared["required_approving_review_count"],
                    "required_review_thread_resolution": declared["required_review_thread_resolution"],
                    "required_reviewers": list(declared["required_reviewers"]),
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "do_not_enforce_on_create": True,
                    "required_status_checks": checks,
                    "strict_required_status_checks_policy": True,
                },
            },
        ],
    }


def provision_ruleset(slug: str, directory: Path, merge_app_id: int | None = None) -> dict[str, object] | str:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(ruleset_payload(merge_app_id), handle, sort_keys=True)
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


def github_setup(
    name: str, directory: Path, private: bool, *, owner: str, runner_profile: str
) -> dict[str, object]:
    profile_spec(runner_profile)
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", owner):
        raise BootstrapError(f"unsafe GitHub owner: {owner!r}")
    try:
        merge_app = merge_authority.configured()
    except KernelError as exc:
        raise BootstrapError(str(exc)) from exc
    visibility = "--private" if private else "--public"
    command(
        [
            "gh",
            "repo",
            "create",
            f"{owner}/{name}",
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
    return provision_github(
        slug, name, directory, runner_profile=runner_profile,
        merge_app=merge_app, expected_owner=owner,
    )


def provision_github(
    slug: str, name: str, directory: Path, *, runner_profile: str,
    merge_app: tuple[str, int] | None, expected_owner: str | None = None,
) -> dict[str, object]:
    """Labels, linked Project Board and ruleset for a repository that exists.

    Shared by bootstrap and adoption. A repository that already has code needs
    exactly this provisioning and nothing else; duplicating it for adoption is
    how the two would drift.
    """
    # Labels, the Project and the ruleset must land in the requested account, not
    # wherever the authenticated login happened to create the repository.
    created_owner = slug.split("/", 1)[0]
    if expected_owner is not None and created_owner.casefold() != expected_owner.casefold():
        raise BootstrapError(
            f"{slug} was created outside the requested account {expected_owner}; provisioning stopped"
        )
    # The scaffolded workflow is already bound to one profile. Refuse the rest of
    # provisioning when the account it actually landed in is assigned another.
    created_profile = account_runner_profile(created_owner)
    if created_profile is not None and created_profile != runner_profile:
        raise BootstrapError(
            f"{slug} belongs to {created_owner}, which is not assigned the "
            f"{runner_profile} runner profile of the scaffolded workflow"
        )
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
        fieldId:$field
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
    # Pin the merge-authority check only once its App can act on the new repository:
    # a required check that nothing can post would deadlock the first pull request.
    pinned = merge_app is not None and merge_authority.installed(slug)
    ruleset = provision_ruleset(slug, directory, merge_app[1] if pinned else None)
    return {
        "repository": slug,
        "project": project.get("url"),
        "ruleset": ruleset.get("_links", {}).get("html", {}).get("href")
        if isinstance(ruleset, dict)
        else None,
        "merge_authority": "required" if pinned else "not-installed" if merge_app else "off",
    }



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--owner", help="GitHub account that will own the repository")
    parser.add_argument("--runner-profile", choices=sorted(RUNNER_PROFILES))
    parser.add_argument("--sync", action="store_true",
                        help="update an existing governed repository instead of creating one")
    parser.add_argument("--adopt", action="store_true",
                        help="bring an existing, ungoverned repository under the kernel")
    parser.add_argument("--check", action="store_true",
                        help="with --sync, report divergence and write nothing")
    parser.add_argument("--ruleset", action="store_true",
                        help="with --sync, also re-provision the branch ruleset from the policy")
    parser.add_argument("--merge-app-id", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.sync or args.adopt:
        try:
            # Imported here rather than at module scope: consumer.py renders from
            # this module's framework_files(), so a top-level import would cycle.
            import consumer

            return consumer.run_adopt(args) if args.adopt else consumer.run_sync(args)
        except (BootstrapError, KernelError) as exc:
            parser.error(str(exc))
    if args.name is None:
        parser.error("--name is required unless --sync is given")
    try:
        if args.github and args.owner is None:
            raise BootstrapError("--github requires --owner to name the account that owns the repository")
        profile = resolve_runner_profile(args.owner, args.runner_profile)
        # The creating account is the first authorized reviewer; without one the strict
        # posture would refuse the repository's own first merge.
        written = scaffold(
            args.name, args.directory, runner_profile=profile,
            reviewers=[args.owner] if args.owner else [],
        )
        remote = (
            github_setup(
                args.name, args.directory.resolve(), args.private,
                owner=args.owner, runner_profile=profile,
            )
            if args.github
            else None
        )
    except BootstrapError as exc:
        parser.error(str(exc))
    print(f"bootstrapped {args.directory.resolve()} ({len(written)} files, {profile})")
    if remote:
        print(remote)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
