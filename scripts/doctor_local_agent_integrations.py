#!/usr/bin/env python3
# line-ceiling: 935
"""Read-only diagnosis for local coding-agent integrations.

Reports desktop continuity adapters plus install-link checks (canonical home,
skill links, invocation surfaces, governance blocks, git/gh prerequisites,
worktree isolation, the local stop file, and target-repo governance). It never
prints credential values and never mutates configuration, GitHub state, or
agent files.

It reads no presence registry and no factory-metrics state: GitHub remains the
authority on who holds what, and the doctor only reports what is installed and
readable on this machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

SECRET_ENV = re.compile(r"(TOKEN|SECRET|KEY|PASSWORD|PAT|CREDENTIAL|AUTH)", re.I)
VERSION_SAFE = re.compile(r"^[A-Za-z0-9._+ -]{1,80}$")
MANAGED_CODEX_ID = re.compile(r'^id = "aru-code-loop(-[0-9a-f]+)?"\s*$', re.M)
CODEX_STATUS = re.compile(r'^status = "([^"]+)"\s*$', re.M)
GITHUB_TOKEN_SHAPE = re.compile(r"gh[pousr]_|github_pat_")
GITHUB_CREDENTIAL_ENV = (
    "GH_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from loop_control import invalid_stop_marker, resolve_desktop_stop_marker  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_DEGRADED = 2

GOVERNANCE_BEGIN = "<!-- BEGIN ARU_SDLC_GOVERNANCE -->"
GOVERNANCE_END = "<!-- END ARU_SDLC_GOVERNANCE -->"
ISSUE_FIRST = "Issue-First"
ARU_HOOK_MARK = "Aru_Agentic_SDLC pre-push"

AGENT_LAYOUT = {
    "codex": {
        "skill_dirs": [".codex/skills"],
        "governance": [".codex/instructions.md"],
        "surfaces": [".codex/instructions.md"],
    },
    "claude": {
        "skill_dirs": [".claude/skills"],
        "governance": [".claude/CLAUDE.md"],
        "surfaces": [".claude/commands/run-aru-factory.md"],
    },
    "cursor": {
        "skill_dirs": [".cursor/skills"],
        "governance": [".cursor/user-rules-aru-agentic-sdlc.md"],
        "required_files": [".cursor/rules/aru-agentic-sdlc.mdc"],
        "surfaces": [".cursor/commands/run-aru-factory.md"],
    },
    "antigravity": {
        "skill_dirs": [".gemini/antigravity/skills", ".antigravity/skills"],
        "governance": [
            ".gemini/antigravity/AGENTS.md",
            ".antigravity/AGENTS.md",
        ],
        "surfaces": [
            ".gemini/antigravity/workflows/aru-code-loop.md",
            ".antigravity/workflows/aru-code-loop.md",
        ],
    },
}


def load_catalog(aru_home: Path) -> dict:
    path = aru_home / "templates" / "integrations" / "continuity.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def probe_version(binary: str) -> str | None:
    found = shutil.which(binary)
    if not found:
        return None
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
    }
    try:
        proc = subprocess.run(
            [found, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unreadable"
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not text:
        return "unknown"
    first = text[0].strip()
    if SECRET_ENV.search(first):
        return "redacted"
    if not VERSION_SAFE.search(first):
        return "unparsed"
    return first


def config_detected(target_home: Path, spec: dict) -> bool:
    for token in spec.get("detect", []):
        if token.startswith(".") and (target_home / token).exists():
            return True
    return False


def cli_name(spec: dict) -> str | None:
    for token in spec.get("detect", []):
        if not token.startswith("."):
            return token
    return None


def application_roots(target_home: Path, live: bool = True) -> list[Path]:
    roots = [target_home / "Applications"]
    if live:
        roots.extend([Path("/Applications"), Path.home() / "Applications"])
    unique = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def sanitize_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if SECRET_ENV.search(text):
        return "redacted"
    if not VERSION_SAFE.search(text):
        return "unparsed"
    return text


def read_bundle_meta(app_path: Path) -> dict | None:
    plist = app_path / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    try:
        data = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ident = data.get("CFBundleIdentifier")
    version = sanitize_version(
        data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
    )
    return {
        "path": str(app_path),
        "bundle_id": ident if isinstance(ident, str) else None,
        "version": version,
    }


def find_macos_app(spec: dict, roots: list[Path]) -> dict | None:
    names = spec.get("macos_app_names") or []
    bundle_ids = set(spec.get("macos_bundle_ids") or [])
    for name in names:
        for root in roots:
            meta = read_bundle_meta(root / name)
            if meta:
                return meta
    if not bundle_ids:
        return None
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.suffix != ".app":
                continue
            meta = read_bundle_meta(child)
            if meta and meta.get("bundle_id") in bundle_ids:
                return meta
    return None


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def project_automation_id(project: str) -> str:
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    return f"aru-code-loop-{digest}"


def read_managed_codex_status(toml: Path) -> str | None:
    if not toml.is_file():
        return None
    try:
        text = toml.read_text(encoding="utf-8")
    except OSError:
        return None
    if not MANAGED_CODEX_ID.search(text):
        return None
    match = CODEX_STATUS.search(text)
    return match.group(1) if match else "unknown"


def native_wake_state(name: str, spec: dict, target_home: Path,
                      project: str | None, wake_entry: dict) -> dict:
    """Split operator opt-in from verified per-app configured/active wake."""
    same = spec["same_task_native_wake"]
    requested = bool(wake_entry.get("enabled")) if same.startswith("opt_in") else False
    if name == "codex" and project:
        auto_id = wake_entry.get("automation_id") or project_automation_id(project)
        auto_dir = target_home / ".codex" / "automations" / auto_id
        prompt = (auto_dir / "PROMPT.md").is_file()
        status = read_managed_codex_status(auto_dir / "automation.toml")
        prepared = requested or prompt
        configured = status is not None
        enabled = status == "ACTIVE"
        if enabled:
            evidence = "automation_active"
        elif status == "PAUSED":
            evidence = "automation_paused"
        elif prepared:
            evidence = "prompt_only"
        else:
            evidence = "none"
    elif same.startswith("opt_in"):
        prepared, configured, enabled = requested, False, False
        evidence = "requested" if requested else "none"
    else:
        prepared, configured, enabled = False, False, False
        evidence = "unsupported"
    return {
        "native_wake_prepared": prepared,
        "native_wake_configured": configured,
        "native_wake_enabled": enabled,
        "native_wake_evidence": evidence,
    }


def check(check_id: str, ok: bool, severity: str, repair: str, **extra) -> dict:
    item = {"id": check_id, "ok": ok, "severity": severity, "repair": repair}
    item.update(extra)
    return item


def canonical_skill_names(aru_home: Path) -> list[str]:
    root = aru_home / "skills"
    if not root.is_dir():
        return []
    names = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            names.append(child.name)
    return names


def classify_skill_link(dest: Path, expected: Path) -> str:
    if not dest.exists() and not dest.is_symlink():
        return "missing"
    if dest.is_symlink():
        try:
            if dest.exists() and dest.resolve() == expected.resolve():
                return "ok"
        except OSError:
            return "stale"
        return "stale"
    return "conflict"


def _skill_dir_active(target_home: Path, rel: str) -> bool:
    """Skip an antigravity alternate root that is not installed."""
    ag = target_home / ".antigravity"
    gemini = target_home / ".gemini" / "antigravity"
    if rel.startswith(".antigravity/"):
        return ag.exists()
    if rel.startswith(".gemini/antigravity/"):
        return gemini.exists() or not ag.exists()
    return True


def inspect_skill_dirs(aru_home: Path, target_home: Path, rel_dirs: list[str],
                       skills: list[str]) -> list[dict]:
    rows = []
    for rel in rel_dirs:
        if not _skill_dir_active(target_home, rel):
            continue
        parent = target_home / rel
        for name in skills:
            dest = parent / name
            expected = aru_home / "skills" / name
            state = classify_skill_link(dest, expected)
            rows.append({
                "path": str(dest),
                "skill": name,
                "expected": str(expected),
                "state": state,
            })
    return rows


def governance_state(path: Path) -> str:
    if not path.is_file():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unreadable"
    if GOVERNANCE_BEGIN not in text or GOVERNANCE_END not in text:
        return "missing"
    begin = text.find(GOVERNANCE_BEGIN)
    end = text.find(GOVERNANCE_END)
    if begin == -1 or end == -1 or end < begin:
        return "malformed"
    block = text[begin:end + len(GOVERNANCE_END)]
    if "factory-loop.stop" not in block:
        return "incomplete"
    return "ok"


def surface_ok(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "run-aru-factory" in text


def redact_text(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        secret = SECRET_ENV.search(line) or GITHUB_TOKEN_SHAPE.search(line)
        lines.append("redacted" if secret else line)
    return "\n".join(lines)


def run_quiet(cmd: list[str], cwd: str | None = None,
              redact: bool = True, home: Path | None = None) -> tuple[int, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home) if home is not None else os.environ.get("HOME", ""),
        "LANG": "C",
    }
    if home is None:
        for key in GITHUB_CREDENTIAL_ENV:
            value = os.environ.get(key, "")
            if value:
                env[key] = value
    env = {key: value for key, value in env.items() if value}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=8,
            cwd=cwd, env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    text = proc.stdout or proc.stderr or ""
    return proc.returncode, redact_text(text) if redact else text


def git_on_path() -> bool:
    return shutil.which("git") is not None


def gh_on_path() -> bool:
    return shutil.which("gh") is not None


def gh_logged_in(home: Path | None = None) -> bool:
    if not gh_on_path():
        return False
    code, _text = run_quiet(["gh", "auth", "status"], home=home)
    return code == 0


def _is_git_repo(project: str, home: Path | None = None) -> bool:
    code, text = run_quiet(
        ["git", "-C", project, "rev-parse", "--is-inside-work-tree"],
        home=home,
    )
    return code == 0 and "true" in text.lower()


def redact_remote(url: str | None) -> str | None:
    """Strip URL userinfo so a PAT in origin never reaches doctor output."""
    if not url:
        return None
    text = url.strip()
    if "://" in text:
        scheme, rest = text.split("://", 1)
        if "@" in rest:
            _creds, hostpart = rest.rsplit("@", 1)
            text = f"{scheme}://redacted@{hostpart}"
    if SECRET_ENV.search(text) or GITHUB_TOKEN_SHAPE.search(text):
        return "redacted"
    return text


def git_remote(project: str, home: Path | None = None) -> str | None:
    code, text = run_quiet(
        ["git", "-C", project, "remote", "get-url", "origin"],
        redact=False, home=home,
    )
    if code != 0:
        return None
    line = text.strip().splitlines()
    return redact_remote(line[0] if line else None)


def agents_md_ok(project: str) -> bool:
    path = Path(project) / "AGENTS.md"
    if not path.is_file():
        return False
    try:
        return ISSUE_FIRST in path.read_text(encoding="utf-8")
    except OSError:
        return False


def aru_pre_push_installed(project: str, home: Path | None = None) -> bool:
    code, git_dir = run_quiet(
        ["git", "-C", project, "rev-parse", "--git-common-dir"],
        home=home,
    )
    if code != 0 or not git_dir.strip():
        return False
    hook_root = Path(git_dir.strip())
    if not hook_root.is_absolute():
        hook_root = Path(project) / hook_root
    hook = hook_root / "hooks" / "pre-push"
    if not hook.is_file() or not os.access(hook, os.X_OK):
        return False
    try:
        return ARU_HOOK_MARK in hook.read_text(encoding="utf-8")
    except OSError:
        return False


def worktree_status(project: str) -> dict:
    root = Path(project) / ".worktrees"
    present = root.is_dir()
    inflight = []
    if present:
        try:
            inflight = sorted(
                child.name for child in root.iterdir()
                if child.is_dir() and child.name != ".retained"
            )
        except OSError:
            inflight = []
    return {"directory": str(root), "present": present, "in_flight": inflight}


def repo_slug_from_remote(remote: str | None) -> str | None:
    if not remote:
        return None
    text = remote.strip()
    if text.endswith(".git"):
        text = text[:-4]
    if "github.com" in text:
        part = text.split("github.com", 1)[1].lstrip(":/")
        pieces = [p for p in part.split("/") if p]
        if len(pieces) >= 2:
            return f"{pieces[0]}/{pieces[1]}"
    return None


@contextmanager
def isolated_github_env(home: Path | None):
    if home is None:
        yield
        return
    import common as common_mod
    previous = common_mod.run_cmd

    def run_cmd_isolated(cmd, check=True, cwd=None, evidence=None):
        del check, evidence
        env = {
            key: value for key, value in os.environ.items()
            if key not in GITHUB_CREDENTIAL_ENV
        }
        env["HOME"] = str(home)
        env.setdefault("PATH", os.environ.get("PATH", ""))
        env.setdefault("LANG", "C")
        try:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=cwd, env=env, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", str(exc)
        return res.returncode, (res.stdout or "").strip(), (res.stderr or "").strip()

    common_mod.run_cmd = run_cmd_isolated
    try:
        yield
    finally:
        common_mod.run_cmd = previous


def project_board_identity(
    project: str, auth_ok: bool, remote: str | None,
    home: Path | None = None,
) -> dict:
    if not auth_ok:
        return {"ok": False, "state": "unauthenticated", "projects": []}
    slug = repo_slug_from_remote(remote)
    if not slug:
        code, text = run_quiet(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=project, home=home,
        )
        slug = text.strip() if code == 0 and text.strip() else None
    if not slug:
        return {"ok": False, "state": "unknown_repo", "projects": []}
    try:
        from common import get_repo_projects, select_governed_projects
        with isolated_github_env(home):
            boards = get_repo_projects(slug)
    except Exception:
        return {"ok": False, "state": "unreadable", "projects": [], "repo": slug}
    if boards is None:
        return {"ok": False, "state": "unreadable", "projects": [], "repo": slug}
    governed = select_governed_projects(boards, slug)
    if not governed:
        return {"ok": False, "state": "absent", "projects": [], "repo": slug}
    titles = [
        b.get("title") or b.get("id") for b in governed if isinstance(b, dict)
    ]
    return {"ok": True, "state": "ok", "projects": titles, "repo": slug}


def append_link_checks(checks: list[dict], rows: list[dict]) -> None:
    for row in rows:
        state = row["state"]
        if state == "ok":
            continue
        if state == "conflict":
            checks.append(check(
                f"skill_conflict:{row['path']}", False, "invalid",
                f"Move or rename {row['path']} then run "
                "scripts/install_local_agent_integrations.sh --repair",
                path=row["path"], skill=row["skill"],
            ))
        elif state == "stale":
            checks.append(check(
                f"skill_stale:{row['path']}", False, "degraded",
                f"Relink {row['path']} with "
                "scripts/install_local_agent_integrations.sh --repair",
                path=row["path"], skill=row["skill"],
            ))
        else:
            checks.append(check(
                f"skill_missing:{row['path']}", False, "degraded",
                f"Install the {row['skill']} skill link with "
                "scripts/install_local_agent_integrations.sh",
                path=row["path"], skill=row["skill"],
            ))


def diagnose_install(aru_home: Path, target_home: Path, agents: dict,  # noqa: C901, PLR0912
                     home: Path | None = None) -> dict:
    skills = canonical_skill_names(aru_home)
    checks: list[dict] = []
    catalog_ok = (aru_home / "templates" / "integrations" / "continuity.json").is_file()
    home_ok = bool(skills) and catalog_ok
    checks.append(check(
        "canonical_home", home_ok,
        "ok" if home_ok else "invalid",
        "Point ARU_SDLC_HOME at the Aru_Agentic_SDLC checkout "
        "(must contain skills/ and templates/integrations/continuity.json).",
        path=str(aru_home),
    ))
    detected = [name for name, agent in agents.items() if agent.get("detected")]
    if not detected:
        checks.append(check(
            "agents_detected", False, "degraded",
            "Install at least one supported agent, then run "
            "scripts/install_local_agent_integrations.sh",
        ))
    shared_rows = inspect_skill_dirs(
        aru_home, target_home, [".agents/skills"], skills
    ) if detected else []
    if detected:
        append_link_checks(checks, shared_rows)
    per_agent = {}
    for name in AGENT_LAYOUT:
        layout = AGENT_LAYOUT[name]
        present = name in detected
        rows = inspect_skill_dirs(
            aru_home, target_home, layout["skill_dirs"], skills
        ) if present else []
        if present:
            append_link_checks(checks, rows)
        gov_paths = []
        if present:
            for rel in layout["governance"]:
                if not _skill_dir_active(target_home, rel):
                    continue
                path = target_home / rel
                state = governance_state(path)
                gov_paths.append({"path": str(path), "state": state})
                if state != "ok":
                    checks.append(check(
                        f"governance:{path}", False, "degraded",
                        "Restore the managed ARU_SDLC_GOVERNANCE block with "
                        "scripts/install_local_agent_integrations.sh "
                        "(unrelated file content is left alone).",
                        path=str(path),
                    ))
            for rel in layout.get("required_files") or []:
                path = target_home / rel
                if not path.is_file():
                    checks.append(check(
                        f"required_file:{path}", False, "degraded",
                        f"Install {rel} with "
                        "scripts/install_local_agent_integrations.sh",
                        path=str(path),
                    ))
        surfaces = []
        if present:
            found = False
            for rel in layout["surfaces"]:
                path = target_home / rel
                ok = surface_ok(path)
                surfaces.append({"path": str(path), "ok": ok})
                found = found or ok
            if not found:
                checks.append(check(
                    f"surface:{name}", False, "degraded",
                    f"Install the run-aru-factory invocation surface for {name} "
                    "with scripts/install_local_agent_integrations.sh",
                ))
        per_agent[name] = {
            "detected": present,
            "skills": rows,
            "governance": gov_paths,
            "surfaces": surfaces,
        }
    git_ok = git_on_path()
    gh_ok = gh_on_path()
    auth_ok = gh_logged_in(home=home)
    checks.append(check(
        "git", git_ok, "ok" if git_ok else "degraded",
        "Install git and ensure it is on PATH.",
    ))
    checks.append(check(
        "gh", gh_ok, "ok" if gh_ok else "degraded",
        "Install the GitHub CLI (gh) and keep it on PATH.",
    ))
    checks.append(check(
        "gh_auth", auth_ok,
        "ok" if auth_ok else "degraded",
        "Run gh auth login. Doctor never prints token values.",
        logged_in=auth_ok,
    ))
    return {
        "skills": skills,
        "shared_skills": shared_rows,
        "agents": per_agent,
        "prerequisites": {
            "git": git_ok,
            "gh": gh_ok,
            "gh_logged_in": auth_ok,
        },
        "checks": checks,
    }


def diagnose_repo(project: str, auth_ok: bool, home: Path | None = None) -> dict:
    checks: list[dict] = []
    remote = git_remote(project, home=home)
    checks.append(check(
        "git_remote", bool(remote), "degraded" if not remote else "ok",
        f"Add a GitHub origin remote in {project}.",
        remote=remote,
    ))
    agents_ok = agents_md_ok(project)
    checks.append(check(
        "agents_md", agents_ok, "degraded" if not agents_ok else "ok",
        "Add AGENTS.md carrying the Issue-First Law, or run init-agent-project.",
    ))
    hooks_ok = aru_pre_push_installed(project, home=home)
    checks.append(check(
        "hooks", hooks_ok, "degraded" if not hooks_ok else "ok",
        "Run scripts/install_hooks.sh in the target repository.",
    ))
    trees = worktree_status(project)
    checks.append(check(
        "worktrees", trees["present"], "degraded" if not trees["present"] else "ok",
        "Create .worktrees/ for isolated feature and review checkouts.",
        in_flight=trees["in_flight"],
    ))
    if not auth_ok:
        board = {"ok": False, "state": "unauthenticated", "projects": []}
        checks.append(check(
            "board", False, "invalid",
            "Authenticate gh (gh auth login) so the doctor can resolve the "
            "Project Board. Token values are never printed.",
        ))
    else:
        board = project_board_identity(project, auth_ok, remote, home=home)
        if board["state"] == "absent":
            checks.append(check(
                "board", False, "degraded",
                "Link a governed GitHub Project board to this repository.",
                repo=board.get("repo"),
            ))
        elif not board["ok"]:
            checks.append(check(
                "board", False, "degraded",
                "Could not read Project Board identity; check gh access.",
                repo=board.get("repo"),
            ))
        else:
            checks.append(check(
                "board", True, "ok", "",
                repo=board.get("repo"), projects=board.get("projects"),
            ))
    return {
        "remote": remote,
        "agents_md": agents_ok,
        "hooks": hooks_ok,
        "worktrees": trees,
        "board": board,
        "checks": checks,
    }


def overall_status(checks: list[dict], payload: dict) -> str:
    if any(item["severity"] == "invalid" and not item["ok"] for item in checks):
        return "invalid"
    if any(item["severity"] == "degraded" and not item["ok"] for item in checks):
        return "degraded"
    if payload.get("loop_stopped"):
        return "degraded"
    agents = payload.get("agents") or {}
    if any(a.get("detected") and a.get("capability_gap") for a in agents.values()):
        return "degraded"
    return "healthy"


def report(aru_home: Path, target_home: Path, project: str | None,
           isolate: bool = False) -> dict:
    catalog = load_catalog(aru_home)
    stop_doc = load_json(target_home / ".aru" / "factory-loop.stop")
    wake_doc = load_json(target_home / ".aru" / "native-wake.json") or {"projects": {}}
    wake_entry = (wake_doc.get("projects") or {}).get(project or "", {})
    roots = application_roots(target_home, live=not isolate)
    home = target_home if isolate else None
    agents = {}
    for name, spec in catalog["agents"].items():
        app = find_macos_app(spec, roots)
        config = config_detected(target_home, spec)
        binary = cli_name(spec)
        cli_version = probe_version(binary) if binary else None
        cli_present = bool(binary and shutil.which(binary))
        detected = bool(app or config or cli_present)
        version = (app or {}).get("version") or cli_version
        same = spec["same_task_native_wake"]
        gap = same in {"session_loop_only", "unsupported"}
        wake = native_wake_state(name, spec, target_home, project, wake_entry)
        agents[name] = {
            "detected": detected,
            "version": version,
            "app_path": (app or {}).get("path"),
            "app_version": (app or {}).get("version"),
            "cli_version": cli_version,
            "config_detected": config,
            "active_task_loop": spec["active_task_loop"],
            "same_task_native_wake": same,
            "same_task_native_wake_notes": spec["same_task_native_wake_notes"],
            "app_restart_recovery": spec["app_restart_recovery"],
            "machine_restart_recovery": spec["machine_restart_recovery"],
            "capability_gap": gap,
            **wake,
        }
    marker_path = target_home / ".aru" / "factory-loop.stop"
    try:
        marker_info = resolve_desktop_stop_marker(target_home, project)
        marker_error = None
    except ValueError as exc:
        marker_error = str(exc)
        marker_info = invalid_stop_marker(marker_path, marker_error)
        # `load_json` swallows the parse error, so the legacy `stop` field would
        # otherwise be None -- identical to an absent marker. Report the corrupt
        # file explicitly instead of erasing it.
        if stop_doc is None and marker_info["present"]:
            stop_doc = {"valid": False, "error": marker_error, "path": str(marker_path)}
    stopped = marker_info["applies"]
    install = diagnose_install(aru_home, target_home, agents, home=home)
    repo = None
    checks = list(install["checks"])
    if marker_error:
        checks.append(check(
            "stop_marker", False, "invalid",
            f"Malformed stop marker: {marker_error}",
            path=str(marker_path),
        ))
    if project and _is_git_repo(project, home=home):
        repo = diagnose_repo(project, install["prerequisites"]["gh_logged_in"], home=home)
        checks.extend(repo["checks"])
    elif project:
        checks.append(check(
            "project_repo", False, "degraded",
            f"--project {project} is not a Git work tree; pass a repository root.",
            path=project,
        ))
    payload = {
        "install_diagnosis": "complete",
        "issue_34": False,
        "status": "healthy",
        "canonical_home": str(aru_home),
        "project": project,
        "stop_file": str(marker_path),
        "desktop_stop_marker": marker_info,
        "loop_stopped": stopped,
        "stop": stop_doc,
        "native_wake": wake_entry or None,
        "non_guarantees": catalog["non_guarantees"],
        "forbidden": catalog["forbidden"],
        "agents": agents,
        "install": install,
        "repository": repo,
        "checks": checks,
    }
    payload["status"] = overall_status(checks, payload)
    return payload


def render_human(payload: dict) -> str:
    lines = [
        f"Aru local-agent doctor  status={payload['status']}",
        f"canonical_home: {payload['canonical_home']}",
        f"loop_stopped: {payload['loop_stopped']}",
        f"project: {payload['project'] or '(none)'}",
    ]
    for name, agent in payload["agents"].items():
        mark = "detected" if agent["detected"] else "absent"
        gap = " gap" if agent["capability_gap"] else ""
        app = f" app={agent['app_path']}" if agent.get("app_path") else ""
        wake = (
            f" prepared={agent['native_wake_prepared']}"
            f" enabled={agent['native_wake_enabled']}"
            f" evidence={agent['native_wake_evidence']}"
        )
        lines.append(
            f"  {name}: {mark} version={agent['version']}{app} "
            f"same_task_wake={agent['same_task_native_wake']}{gap}{wake}"
        )
    for note in payload.get("non_guarantees") or []:
        lines.append(f"wake_limitation: {note}")
    failed = [item for item in payload.get("checks") or [] if not item["ok"]]
    if failed:
        lines.append("failed checks:")
        for item in failed:
            repair = item.get("repair") or ""
            lines.append(f"  [{item['severity']}] {item['id']}: {repair}")
    lines.append("Never claims app-quit, sleep, power-off, or credit recovery.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only local-agent integration doctor.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--aru-home")
    parser.add_argument("--target-home")
    parser.add_argument("--project")
    args = parser.parse_args()
    aru_home = Path(args.aru_home or os.environ.get("ARU_SDLC_HOME") or Path(__file__).resolve().parents[1])
    isolate = args.target_home is not None
    target_home = Path(args.target_home or Path.home())
    if args.project and not args.project.startswith("/"):
        print("error: --project must be an absolute path", file=sys.stderr)
        return EXIT_INVALID
    if not (aru_home / "templates" / "integrations" / "continuity.json").is_file():
        print("error: continuity catalog missing under --aru-home", file=sys.stderr)
        return EXIT_INVALID
    payload = report(aru_home, target_home, args.project, isolate=isolate)
    if args.json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human(payload))
    if payload["status"] == "invalid":
        return EXIT_INVALID
    if payload["status"] == "degraded":
        return EXIT_DEGRADED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
