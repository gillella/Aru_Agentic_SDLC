"""Opt-in Claude task grants; CLI permissions are not an operating-system sandbox."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path

from .config import DriverError

FACTORY = "gillella/Aru_Agentic_SDLC"
COMPILER = "factory-claude/v1"
RESULT_BYTES = 8 * 1024 * 1024
RECEIPT_DETAILS_BYTES = 8192


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def safe_path(value: object) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
            or any(p in {".", ".."} for p in value.split("/"))
            or Path.home().is_relative_to(Path(value).resolve())):
        raise DriverError("worker_permissions requires literal absolute paths, never home or shell patterns")
    return value


def validate(config, repo: str, project: dict) -> None:
    policy = project.get("worker_permissions")
    if policy is None:
        return
    required = {"version", "lanes", "python", "test_python", "app_runner", "recovery_epoch"}
    if (repo != FACTORY or not isinstance(policy, dict) or set(policy) != required
            or type(policy.get("version")) is not int or policy["version"] != 1):
        raise DriverError("worker_permissions v1 requires an explicit Factory-only policy")
    if not isinstance(policy["recovery_epoch"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", policy["recovery_epoch"]):
        raise DriverError("worker_permissions recovery_epoch must name an authorized policy revision")
    lanes = policy["lanes"]
    if (not isinstance(lanes, list) or not lanes or any(not isinstance(i, str) for i in lanes)
            or len(lanes) != len(set(lanes)) or any(i not in project["lanes"] for i in lanes)):
        raise DriverError("worker_permissions lanes must explicitly select unique project lanes")
    for value in (policy["python"], policy["test_python"], policy["app_runner"], project["repo_dir"], str(config.kernel_root)):
        safe_path(value)
    for identity in lanes:
        lane = config.lanes[identity]
        argv = lane.get("command", [])
        # Accept only the installed, documented unattended harness shape. Do not
        # merge opaque operator permission flags into the compiled policy.
        if (lane.get("family") != "claude-code" or len(argv) != 10
                or Path(argv[0]).name != "claude-sub" or argv[1] not in {"1", "2", "3", "4"}
                or argv[2] != "--model" or not re.fullmatch(r"[a-zA-Z0-9_.-]+", argv[3])
                or argv[4:] != ["--print", "--permission-mode", "acceptEdits",
                                "--permission-prompts", "none", "{prompt}"]):
            raise DriverError("worker_permissions requires claude-sub N --model MODEL --print --permission-mode acceptEdits --permission-prompts none {prompt}")


def enabled(config, repo: str, identity: str) -> bool:
    policy = config.project(repo).get("worker_permissions")
    return bool(policy and identity in policy["lanes"])


def fingerprint(config, repo: str, identity: str) -> str:
    from .reviewers import inventory
    reviewers = inventory(config, repo)
    return digest({"compiler": COMPILER, "policy": config.project(repo).get("worker_permissions"),
                   **({"coding_reviewers": reviewers} if reviewers is not None else {}),
                   "repo_dir": config.project(repo)["repo_dir"], "kernel": str(config.kernel_root),
                   "lane": config.lane(repo, identity)})


def retry_blocker(config, receipt: dict, work: dict) -> str | None:
    if not receipt.get("retry_blocked"):
        return None
    if (receipt.get("policy_fingerprint") == fingerprint(config, receipt["repo"], receipt["agent"])
            and receipt.get("head") == work.get("head") and receipt.get("pr") == work.get("pr")):
        return (receipt.get("reason") or "worker result unavailable") + (
            "; operator must approve a worker_permissions revision/recovery_epoch or changed PR task; unchanged retries blocked")
    return None


def _task(config, record: dict, adapter, run) -> dict:
    repo, identity, issue = record["repo"], record["agent"], record["issue"]
    directory = Path(safe_path(record["worktree"]))
    root = Path(safe_path(config.project(repo)["repo_dir"]))
    if not directory.is_dir() or not directory.is_relative_to(root / ".worktrees"):
        raise DriverError("worker permission scope requires the claimed isolated worktree; restore it before retry")
    reviewing = record.get("kind") == "review"
    if reviewing:
        binding = adapter.review_binding(record["pr"], record["review"])
        if (binding.get("reviewer") != identity or binding.get("author") == identity
                or binding.get("author_actor") == binding.get("reviewer_actor")
                or adapter.review_worktree(binding) != str(directory)):
            raise DriverError("worker permission scope requires a distinct current-head reviewer")
        scope = []
    else:
        live = adapter.revalidate(issue, agent=identity)
        scope = live["touches"]
        if not scope:
            raise DriverError("worker permission scope requires live touches")
    context = run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"], directory)
    expected = [str(directory), str(root / ".git")]
    if context.returncode or context.stdout.splitlines() != expected:
        raise DriverError("worker permission scope requires verified Git worktree and canonical common directory")
    branch_result = run(["git", "branch", "--show-current"], directory)
    if branch_result.returncode:
        raise DriverError("worker branch context is unreadable")
    branch = branch_result.stdout.strip()
    if not reviewing and (not re.fullmatch(r"(?:fix|feat|chore|docs|refactor|test)/issue-" + str(issue) + r"-[a-z0-9-]+", branch)):
        raise DriverError("worker permission scope requires the claimed issue feature branch; never main/master")
    for path in scope:
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*(?:/\*\*)?", path)
                or ".." in Path(path).parts):
            raise DriverError("worker permission scope contains unsafe touches")
    return {"branch": branch, "touches": scope, "worktree": str(directory)}


def compile_policy(config, record: dict, adapter, run, *, preflight: bool = False) -> dict:
    repo, identity, issue = record["repo"], record["agent"], record["issue"]
    if not enabled(config, repo, identity):
        raise DriverError("operator must explicitly opt this Factory lane into worker_permissions")
    task = _task(config, record, adapter, run)
    policy = config.project(repo)["worker_permissions"]
    directory, kernel = task["worktree"], str(config.kernel_root)
    py, test_py = policy["python"], policy["test_python"]
    reviewing = record.get("kind") == "review"
    gh = ["gh"] if reviewing and not record["review"]["reviewer_actor"].endswith("[bot]") else [
        policy["app_runner"], "--repo", repo, "--", "gh"]
    reads = [[py, "--version"], [test_py, "--version"], ["git", "config", "user.name"], ["git", "config", "user.email"],
             ["git", "branch", "--show-current"], ["git", "rev-parse", "HEAD"],
             ["git", "status", "--short"], ["git", "diff"], ["git", "diff", "--stat"],
             [*gh, "issue", "view", str(issue), "--repo", repo, "--json", "number,body,labels,state"]]
    if record.get("pr"):
        reads += [[*gh, "pr", op, str(record["pr"]), "--repo", repo] for op in ("view", "diff", "checks")]
    if reviewing:
        reads += [[*gh, "api", "user", "--jq", ".login"]]
    commands = list(reads)
    # Scratch body stays untracked in the worktree; stage only declared touches.
    body = directory + "/.aru-worker-body.md"
    if not preflight:
        commands += [[test_py, "-m", "pytest", "-q"], [test_py, "-m", "ruff", "check", "."]]
        if reviewing:
            commands += [[*gh, "pr", "review", str(record["pr"]), "--repo", repo,
                          verdict, "--body-file", body] for verdict in ("--approve", "--request-changes")]
        else:
            commands += [["git", "fetch", "origin"],
                         ["git", "add", "--", *[p.removesuffix("/**") for p in task["touches"]]],
                         ["git", "commit", "-m", f"Fix #{issue}"],
                         ["git", "push", "origin", f"HEAD:refs/heads/{task['branch']}"],
                         [py, kernel + "/scripts/create_pr.py", "--issue", str(issue), "--agent", identity,
                          "--author-family", "claude-code", "--title", f"Fix #{issue}", "--body-file", body]]
    allow = [f"Bash({shlex.join(c)})" for c in commands]
    allow += [f"Read(/{directory}/**)", f"Read(/{kernel}/AGENTS.md)",
              f"Read(/{kernel}/docs/KERNEL-CONTRACT.md)", f"Read(/{kernel}/skills/**)",
              f"Read(/{kernel}/scripts/**)"]
    if not preflight:
        allow += [f"Edit(/{body})"]
        if not reviewing:
            allow += [f"Edit(/{directory}/{p})" for p in task["touches"]]
    lane = config.lane(repo, identity)
    argv = lane["command"][:5] + ["--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--setting-sources", "", "--strict-mcp-config", "--tools", "Bash,Read,Glob,Grep,Edit,Write",
            "--add-dir", kernel, "--allowedTools", *allow,
            "--output-format", "json", "--no-session-persistence"]
    prompt = record["prompt"] + "\nUse only these literal shell commands (no extra flags or shell composition):\n" + "\n".join(shlex.join(c) for c in commands)
    prompt += (f"\nUse Read for canonical rules. Body scratch path: {body}; never stage it. "
               'Create a missing body with Edit using old_string="" and new_string containing the body; '
               "Read an existing body before editing it. "
               "Canonical access is for reading only. No author review/merge authority. "
               "CLI grants are not OS isolation; tests/helpers execute trusted project code.")
    if preflight:
        prompt = ("No-write capability preflight only. Do not implement, test, submit, or change anything. "
                  f"Read {kernel}/AGENTS.md and {kernel}/docs/KERNEL-CONTRACT.md using Read. "
                  "Execute EACH of these exact read commands and report actual outputs and failures; a bare OK is insufficient:\n"
                  + "\n".join(shlex.join(c) for c in reads))
        argv += ["--disallowedTools", "Edit", "Write", "--verbose"]
        argv[argv.index("--output-format") + 1] = "stream-json"
    # Append after either task prompt so preflight cannot drop the shared contract.
    prompt += ("\nBash.command must equal one listed command byte-for-byte: copy it literally, "
               "one command per Bash invocation; no extra flags or alternate spellings; "
               "no shell wrappers, composition, substitutions, pipelines or suffixes. "
               "Do not append echo/printf or exit-code reporting (including ; echo or ; printf). "
               "Use native tool output and result metadata for outputs, exit status and failures. "
               "Wait for each tool result before the next call. "
               "Stop on the first permission denial and report the exact blocker; "
               "do not retry with alternate tools (including Glob, Grep or Read), rewritten commands "
               "or permission changes, and do not execute any remaining commands.")
    return {"argv": [*argv, prompt], "task": task, "commands": commands,
            "policy_fingerprint": fingerprint(config, repo, identity), "result_format": "claude-json"}


def observe_result(path: Path, exit_code: int, *, quota_errors: bool = False) -> dict:
    observation = _observe_result(path, exit_code, quota_errors=quota_errors)
    # Reconciliation scans receipts repeatedly; keep full model/tool payloads
    # only in the private result log, never amplified into receipt/status text.
    if len(json.dumps(observation).encode()) > RECEIPT_DETAILS_BYTES:
        observation = {key: observation[key] for key in (
            "outcome", "retry_blocked", "governed_completion") if key in observation}
        observation.update(details_omitted=True,
                           reason=f"Claude {observation['outcome']}; full details in worker result_path log")
    elif len(observation["reason"].encode()) > 2048:
        observation["reason"] = "Claude " + observation["outcome"] + "; full details in worker result_path log"
    return observation


def _observe_result(path: Path, exit_code: int, *, quota_errors: bool = False) -> dict:
    """Claude SDK result envelope; reported artifacts still require GitHub reread."""
    try:
        with path.open("r+b") as stream:
            payload = stream.read(RESULT_BYTES + 1)
            if len(payload) > RESULT_BYTES:
                stream.truncate(RESULT_BYTES)
                raise ValueError("result too large; retained first 8 MiB in result log")
        result = json.loads(payload)
        if (not isinstance(result, dict) or result.get("type") != "result"
                or type(result.get("is_error")) is not bool
                or not isinstance(result.get("permission_denials"), list)
                or not isinstance(result.get("subtype"), str)):
            raise ValueError("missing result envelope fields")
        denials = result["permission_denials"]
        if any(not isinstance(d, dict) or not isinstance(d.get("tool_name"), str)
               or not isinstance(d.get("tool_input"), dict) for d in denials):
            raise ValueError("malformed permission denials")
    except (OSError, ValueError) as exc:
        return {"outcome": "result_unavailable", "retry_blocked": True,
                "reason": f"structured worker result unavailable: {exc}"}
    observation = {"result_subtype": result["subtype"], "permission_denials": denials,
                   "reported_result": result.get("result"), "result_errors": result.get("errors"),
                   "governed_completion": False}
    if denials:
        return {**observation, "outcome": "permission_denied", "retry_blocked": True,
                "reason": "Claude permission denial: " + json.dumps(denials, sort_keys=True)}
    # Only a valid native result envelope, with no denial, can prove quota.
    # Ambiguous clock text is deliberately not converted to a reset epoch.
    if quota_errors and result["is_error"] and not result.get("errors") and result.get("api_error_status") not in (401, 403) and isinstance(result.get("result"), str) and re.fullmatch(
        r"You've hit your (?:session|weekly) limit(?: · resets [^\r\n]{1,100})?", result["result"]
    ):
        return {"outcome": "quota_exhausted", "retry_blocked": False,
                "governed_completion": False, "reason": "authenticated Claude subscription quota exhausted",
                "quota_reset_at": None}
    if result["is_error"] or result["subtype"] != "success" or exit_code:
        return {**observation, "outcome": "worker_error", "retry_blocked": True,
                "reason": "Claude worker error: " + json.dumps(result.get("errors") or result.get("result") or result["subtype"])}
    return {**observation, "outcome": "reported_success", "retry_blocked": True,
            "reason": "worker reported success; unchanged task still requires governed progress evidence"}
