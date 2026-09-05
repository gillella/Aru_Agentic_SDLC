"""External, bounded adapter to the installed Aru kernel.

Kernel imports and GitHub credentials stay inside a fresh subprocess rooted in
the selected repository. The coordinator owns scheduling and shared capacity;
GitHub and the canonical helpers continue to own lifecycle transitions.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


SCHEMA = "aru.driver.snapshot/v1"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_PRS = 100
MAX_REFERENCES = 100
ACTIVE = {"In Progress", "In Review"}


class KernelAdapterError(RuntimeError):
    """An observation or transition could not be safely established."""


def _overlap(left: list[str], right: list[str]) -> bool:
    # Canonical touches semantics are exact paths and terminal '/**' only.
    # Declarations have already passed the kernel parser in the bridge.
    for a in left:
        a_tree = a.endswith("/**")
        a_root = PurePosixPath(a[:-3] if a_tree else a).as_posix()
        for b in right:
            b_tree = b.endswith("/**")
            b_root = PurePosixPath(b[:-3] if b_tree else b).as_posix()
            if a_root == b_root:
                return True
            if a_tree and b_root.startswith(a_root + "/"):
                return True
            if b_tree and a_root.startswith(b_root + "/"):
                return True
    return False


def _conflicts(snapshot: dict, record: dict, agent: str | None = None) -> list[str]:
    errors: list[str] = []
    number = record["number"]
    own_prs = [
        pr for pr in snapshot["prs"]
        if number in pr["issues"] and agent is not None
        and pr["author_agent"] == agent and record["agents"] == [agent]
        and record["status"] in ACTIVE
    ]
    effective_touches = sorted(set(record["touches"]).union(
        *(set(pr["touches"]) for pr in own_prs),
    ))
    for other in snapshot["issues"]:
        if other["number"] == number or other["status"] not in ACTIVE:
            continue
        if other.get("boundary_errors") or not other["touches"]:
            errors.append(f"active issue #{other['number']} has an unknown write boundary")
        elif _overlap(effective_touches, other["touches"]):
            errors.append(f"write boundary overlaps active issue #{other['number']}")
    for pr in snapshot["prs"]:
        if pr in own_prs:
            continue
        if number in pr["issues"]:
            errors.append(f"issue already has open PR #{pr['number']}")
        elif not pr["touches"]:
            errors.append(f"open PR #{pr['number']} has an unknown write boundary")
        elif _overlap(effective_touches, pr["touches"]):
            errors.append(f"write boundary overlaps open PR #{pr['number']}")
    return sorted(set(errors))


class KernelAdapter:
    def __init__(self, kernel_root: Path, repo_dir: Path, repo: str):
        self.kernel_root = Path(kernel_root).resolve()
        self.repo_dir = Path(repo_dir).resolve()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise KernelAdapterError("repository must be OWNER/NAME")
        self.repo = repo

    def _invoke(self, operation: str, **payload: Any) -> Any:
        command = [
            sys.executable, str(Path(__file__).resolve()), "--kernel-bridge",
            str(self.kernel_root), str(self.repo_dir), self.repo, operation,
        ]
        try:
            result = subprocess.run(
                command, input=json.dumps(payload), text=True, capture_output=True,
                check=False, timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KernelAdapterError(
                "kernel bridge did not complete; reread authority before retrying"
            ) from exc
        try:
            response = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise KernelAdapterError("kernel bridge returned malformed output") from exc
        if result.returncode or not isinstance(response, dict) or "result" not in response:
            message = response.get("error") if isinstance(response, dict) else None
            raise KernelAdapterError(message or "kernel bridge failed")
        return response["result"]

    def snapshot(self) -> dict:
        snapshot = self._invoke("snapshot")
        self._validate_snapshot(snapshot)
        return snapshot

    def _validate_snapshot(self, snapshot: dict) -> None:
        if (
            not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA
            or snapshot.get("complete") is not True or snapshot.get("repo") != self.repo
            or not isinstance(snapshot.get("issues"), list)
            or not isinstance(snapshot.get("prs"), list)
        ):
            raise KernelAdapterError("a complete matching repository snapshot is required")

    def blocked(self, snapshot: dict, status: str = "Ready") -> dict[str, list[str]]:
        self._validate_snapshot(snapshot)
        rejected: dict[str, list[str]] = {}
        for record in snapshot["issues"]:
            if record["status"] != status:
                continue
            errors = list(record["errors"])
            if record["agents"]:
                errors.append("issue already has an agent claim")
            errors.extend(_conflicts(snapshot, record))
            if errors:
                rejected[str(record["number"])] = sorted(set(errors))
        return rejected

    def candidates(self, snapshot: dict, status: str = "Ready") -> list[dict]:
        rejected = self.blocked(snapshot, status)
        return sorted(
            (record for record in snapshot["issues"]
             if record["status"] == status and str(record["number"]) not in rejected),
            key=lambda record: (record["priority"], record["number"]),
        )

    def revalidate(self, number: int, agent: str | None = None) -> dict:
        return self._invoke("revalidate", number=number, agent=agent)

    def promote(self, number: int) -> dict:
        return self._invoke("promote", number=number)

    def claim(self, number: int, agent: str) -> dict:
        return self._invoke("claim", number=number, agent=agent)

    def release(self, number: int, agent: str) -> dict:
        """Explicit caller operation; caller must first prove no live worker."""
        return self._invoke("release", number=number, agent=agent)

    def branch(self, number: int, agent: str) -> str:
        return self._invoke("branch", number=number, agent=agent)

    def next_work(self, agent: str) -> dict:
        return self._invoke("next_work", agent=agent)

    def reviewer_continuation(self, number: int) -> dict:
        return self._invoke("reviewer_continuation", number=number)

    def reviewer_status(self) -> dict:
        return self._invoke("reviewer_status")

    def issue_summary(self, number: int) -> dict:
        return self._invoke("issue_summary", number=number)

    def dependency_evidence(self, request: dict) -> dict:
        return self._invoke("dependency_evidence", request=request)

    def source_pr(self, number: int) -> dict:
        return self._invoke("source_pr", number=number)


class _Bridge:
    """One invocation, one repository, no persistent module/global state."""

    def __init__(self, kernel_root: Path, repo_dir: Path, repo: str):
        scripts = kernel_root.resolve() / "scripts"
        if not (scripts / "common.py").is_file():
            raise KernelAdapterError("kernel scripts are unavailable")
        self.repo_dir = repo_dir.resolve()
        self.repo = repo
        sys.path.insert(0, str(scripts))
        os.chdir(self.repo_dir)
        self.common = importlib.import_module("common")
        self.triage = importlib.import_module("triage_backlog")
        self.claims = importlib.import_module("claim_issue")
        self.branches = importlib.import_module("create_branch")
        self.picker = importlib.import_module("fetch_next_work")
        self.review = importlib.import_module("review_policy")
        self.merge_state = importlib.import_module("merge_state")
        self.identity()

    def identity(self, *, clean: bool = False) -> None:
        c = self.common
        if c.repo_root() != self.repo_dir:
            raise KernelAdapterError("configured repository directory is not its checkout root")
        if c.checkout_repository().casefold() != self.repo.casefold():
            raise KernelAdapterError("checkout remote does not match configured repository")
        if c.repo_slug().casefold() != self.repo.casefold():
            raise KernelAdapterError("live GitHub repository does not match configured repository")
        if clean and c.git(["status", "--porcelain", "--untracked-files=no"]):
            raise KernelAdapterError("tracked changes exist in the dispatch checkout")

    def pages(self, endpoint: str, *, key: str | None = None) -> list[dict]:
        records: list[dict] = []
        expected_count: int | None = None
        for page in range(1, MAX_PAGES + 1):
            separator = "&" if "?" in endpoint else "?"
            data = self.common.gh_json([
                "api", f"{endpoint}{separator}per_page={PAGE_SIZE}&page={page}",
            ])
            if key is not None:
                if not isinstance(data, dict) or type(data.get("total_count")) is not int:
                    raise KernelAdapterError("GitHub returned malformed counted inventory")
                count = data["total_count"]
                if count < 0 or (expected_count is not None and count != expected_count):
                    raise KernelAdapterError("GitHub inventory changed during pagination")
                expected_count = count
                data = data.get(key)
            if (
                not isinstance(data, list) or len(data) > PAGE_SIZE
                or any(not isinstance(item, dict) for item in data)
            ):
                raise KernelAdapterError("GitHub returned malformed inventory page")
            records.extend(data)
            if len(data) < PAGE_SIZE:
                if expected_count is not None and len(records) != expected_count:
                    raise KernelAdapterError("GitHub returned incomplete counted inventory")
                return records
        raise KernelAdapterError("GitHub inventory exceeds the bounded complete snapshot")

    def _issue(self, record: dict, dependency_states: dict[int, str]) -> dict:
        c = self.common
        if (
            type(record.get("number")) is not int or record["number"] <= 0
            or not isinstance(record.get("title"), str)
            or not isinstance(record.get("body"), (str, type(None)))
            or str(record.get("state", "")).upper() not in {"OPEN", "CLOSED"}
            or not isinstance(record.get("labels"), list)
        ):
            raise KernelAdapterError("GitHub returned a malformed issue")
        normalized = {**record, "state": record["state"].upper(), "body": record["body"] or ""}
        labels = c.label_names(normalized)
        status = c.status_of(normalized)
        agents = sorted(label.removeprefix("agent:") for label in labels if label.startswith("agent:"))
        boundary_errors: list[str] = []
        try:
            touches = c.parse_touches(normalized["body"])
        except c.KernelError as exc:
            touches = []
            boundary_errors.append(str(exc))
        errors = self.triage.evaluate_with_states(
            normalized, {number: state.lower() for number, state in dependency_states.items()},
        )
        try:
            priority = self.triage._priority(normalized)
        except c.KernelError:
            priority = 2
        try:
            dependencies = c.dependencies(normalized["body"])
        except c.KernelError:
            dependencies = []
        if len(agents) > 1:
            errors.append("issue has multiple agent claims")
        if "needs-design" in labels:
            errors.append("needs-design requires a resolved design decision before dispatch")
        return {
            "number": normalized["number"], "title": normalized["title"],
            "body": normalized["body"], "state": normalized["state"],
            "labels": labels, "status": status, "agents": agents, "touches": touches,
            "dependencies": {str(number): dependency_states.get(number, "UNKNOWN") for number in dependencies},
            "errors": sorted(set(errors)), "boundary_errors": boundary_errors,
            "priority": priority,
        }

    def _ci(self) -> dict:
        ci: dict[str, Any] = {
            "available": None, "online_runners": None, "free_runners": None,
            "queued": None, "reason": None,
        }
        try:
            runners = self.pages(f"repos/{self.repo}/actions/runners", key="runners")
            eligible = []
            for runner in runners:
                labels = runner.get("labels")
                if (
                    not isinstance(labels, list) or type(runner.get("busy")) is not bool
                    or runner.get("status") not in {"online", "offline"}
                    or any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels)
                ):
                    raise KernelAdapterError("GitHub returned malformed runner inventory")
                names = {label["name"].casefold() for label in labels}
                if {"self-hosted", "macos", "arm64", "aru-ci"} <= names:
                    eligible.append(runner)
            online = [runner for runner in eligible if runner["status"] == "online"]
            ci.update(available=bool(online), online_runners=len(online),
                      free_runners=sum(not runner["busy"] for runner in online))
        except (self.common.KernelError, KernelAdapterError) as exc:
            if str(exc) == getattr(self.common, "QUOTA_STOP_MESSAGE", None):
                raise
            ci["reason"] = str(exc)
        try:
            runs = self.pages(f"repos/{self.repo}/actions/runs?status=queued", key="workflow_runs")
            ci["queued"] = len(runs)
        except (self.common.KernelError, KernelAdapterError) as exc:
            if str(exc) == getattr(self.common, "QUOTA_STOP_MESSAGE", None):
                raise
            ci["available"] = None
            ci["reason"] = "; ".join(filter(None, [ci["reason"], str(exc)]))
        return ci

    def _raw_issues(self) -> dict[int, dict]:
        raw_issues = self.pages(f"repos/{self.repo}/issues?state=open")
        for status in ("in-progress", "in-review"):
            raw_issues.extend(self.pages(
                f"repos/{self.repo}/issues?state=closed&labels=status%3A{status}",
            ))
        indexed: dict[int, dict] = {}
        for record in raw_issues:
            if "pull_request" in record:
                continue
            number = record.get("number")
            if type(number) is not int or number in indexed:
                raise KernelAdapterError("issue inventory has malformed or duplicate identities")
            indexed[number] = record
        return indexed

    def _references(self, indexed: dict[int, dict], raw_prs: list[dict]) -> tuple[dict[int, str], dict[int, dict]]:
        references: set[int] = set()
        for record in indexed.values():
            try:
                references.update(self.common.dependencies(str(record.get("body") or "")))
            except self.common.KernelError:
                pass  # Candidate receives the canonical syntax error below.
        for pr in raw_prs:
            references.update(self.merge_state.linked_issues(str(pr.get("body") or "")))
        if len(references) > MAX_REFERENCES:
            raise KernelAdapterError("issue reference inventory exceeds the driver snapshot bound")
        dependency_states: dict[int, str] = {}
        linked_records = dict(indexed)
        for number in sorted(references):
            record = indexed.get(number) or self.common.issue(number)
            if str(record.get("state", "")).upper() not in {"OPEN", "CLOSED"}:
                raise KernelAdapterError("referenced issue state is unavailable")
            dependency_states[number] = record["state"].upper()
            linked_records[number] = record
        return dependency_states, linked_records

    def _pr_touches(self, number: int, linked: list[int], linked_records: dict[int, dict]) -> list[str]:
        touches: set[str] = set()
        for changed in self.pages(f"repos/{self.repo}/pulls/{number}/files"):
            paths = [changed.get("filename")]
            if "previous_filename" in changed:
                paths.append(changed["previous_filename"])
            for path in paths:
                if not isinstance(path, str) or not self.common.safe_declared_path(path):
                    raise KernelAdapterError("open PR file boundary is malformed")
                touches.add(path)
        for linked_number in linked:
            try:
                touches.update(self.common.parse_touches(str(linked_records[linked_number].get("body") or "")))
            except self.common.KernelError:
                # Exact changed files still reserve an ungoverned PR safely.
                pass
        return sorted(touches)

    def _pr(self, pr: dict, linked_records: dict[int, dict]) -> dict:
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha")
        if (
            type(number) is not int or number <= 0
            or pr.get("state") != "open" or not isinstance(pr.get("labels"), list)
            or not isinstance(head, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", head)
            or not isinstance((pr.get("user") or {}).get("login"), str)
        ):
            raise KernelAdapterError("GitHub returned malformed open PRs")
        labels = self.common.label_names(pr)
        authors = [name.removeprefix("author:") for name in labels if name.startswith("author:")]
        if len(authors) > 1:
            raise KernelAdapterError("open PR has contradictory author identities")
        linked = sorted(set(self.merge_state.linked_issues(str(pr.get("body") or ""))))
        touches = self._pr_touches(number, linked, linked_records)
        live_pr = self.common.gh_json(["api", f"repos/{self.repo}/pulls/{number}"])
        if (
            not isinstance(live_pr, dict) or live_pr.get("state") != "open"
            or (live_pr.get("head") or {}).get("sha") != head
            or self.common.label_names(live_pr) != labels
        ):
            raise KernelAdapterError("open PR authority changed during snapshot")
        return {
            "number": number, "head": head, "author_agent": authors[0] if authors else None,
            "author_actor": pr["user"]["login"], "labels": labels,
            "issue": linked[0] if len(linked) == 1 else None, "issues": linked,
            "touches": touches, "errors": [] if touches else ["unknown PR write boundary"],
        }

    def snapshot(self) -> dict:
        self.identity()
        indexed = self._raw_issues()
        raw_prs = self.pages(f"repos/{self.repo}/pulls?state=open")
        if len(raw_prs) > MAX_PRS:
            raise KernelAdapterError("open PR inventory exceeds the driver snapshot bound")
        dependency_states, linked_records = self._references(indexed, raw_prs)
        issues = [self._issue(record, dependency_states) for record in indexed.values()]
        prs = [self._pr(record, linked_records) for record in raw_prs]
        if len({pr["number"] for pr in prs}) != len(prs):
            raise KernelAdapterError("open PR inventory has duplicate identities")
        ci = self._ci()
        return {
            "schema": SCHEMA, "repo": self.repo, "complete": True,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status_source": "issue-label; target Project card reread before dispatch",
            "issues": sorted(issues, key=lambda item: item["number"]),
            "prs": sorted(prs, key=lambda item: item["number"]),
            "ci_available": ci["available"], "ci": ci,
        }

    def revalidate(self, number: int, agent: str | None = None) -> dict:
        if type(number) is not int or number <= 0:
            raise KernelAdapterError("issue number must be a positive integer")
        if agent is not None:
            self.claims.safe_agent(agent)
        self.identity(clean=True)
        snapshot = self.snapshot()
        matches = [record for record in snapshot["issues"] if record["number"] == number]
        if len(matches) != 1:
            raise KernelAdapterError("requested issue is absent from the complete snapshot")
        record = matches[0]
        errors = list(record["errors"])
        if agent is not None and record["status"] in ACTIVE:
            # Ready requires unfinished criteria; a claimed author can finish
            # them before opening a PR and In Review requires completed ones.
            # Keep a missing/malformed criteria section blocked on resumption.
            if self.common.acceptance_items(record["body"]):
                errors = [error for error in errors if error != "Acceptance Criteria must contain an unchecked item"]
            if record["agents"] != [agent]:
                errors.append("agent is not the exclusive issue claimant")
        elif record["status"] not in {"Ready", "Backlog"} or record["agents"]:
            errors.append("issue is not unclaimed Ready or Backlog")
        errors.extend(_conflicts(snapshot, record, agent))
        if self.common.project_item_status(number) != record["status"]:
            errors.append("issue status and linked Project card disagree")
        if errors:
            raise KernelAdapterError(f"issue #{number}: " + "; ".join(sorted(set(errors))))
        return record

    def _handoff_operation(self, operation: str, payload: dict) -> Any:
        # The bridge is executed as a file, with this directory on sys.path.
        import handoff_evidence
        if operation == "issue_summary":
            return handoff_evidence.issue_summary(self, payload["number"])
        if operation == "source_pr":
            return handoff_evidence.source_pr(self, payload["number"])
        return handoff_evidence.evidence(self, payload["request"])

    def dispatch(self, operation: str, payload: dict) -> Any:
        if operation in {"issue_summary", "dependency_evidence", "source_pr"}:
            return self._handoff_operation(operation, payload)
        if operation == "snapshot":
            return self.snapshot()
        if operation == "next_work":
            return self.picker.select(self.claims.safe_agent(payload["agent"]))
        if operation == "reviewer_continuation":
            return self.review.reviewer_continuation(payload["number"])
        if operation == "reviewer_status":
            return self.review.reviewer_status(probe=False)
        number = payload["number"]
        agent = payload.get("agent")
        if operation == "release":
            self.identity(clean=True)
            return self.claims.release(number, agent)
        if operation == "revalidate":
            return self.revalidate(number, agent)
        if operation == "promote":
            return self.promote(number)
        if operation == "claim":
            if self.revalidate(number)["status"] != "Ready":
                raise KernelAdapterError("claim requires live unclaimed Ready")
            result = self.claims.claim(number, agent)
            # Preserve the canonical claim if a concurrent writer appears; do
            # not launch, auto-release, or delete potentially recoverable work.
            self.revalidate(number, agent)
            return result
        if operation == "branch":
            return self.branch(number, agent)
        raise KernelAdapterError("unsupported kernel adapter operation")

    def promote(self, number: int) -> dict:
        def guard() -> None:
            if self.revalidate(number)["status"] != "Backlog":
                raise KernelAdapterError("promotion requires live unclaimed Backlog")
        guard()
        self.triage.promote_issue(number, pre_mutation_check=guard)
        return self.revalidate(number)

    def branch(self, number: int, agent: str) -> str:
        record = self.revalidate(number, agent)
        if record["status"] not in ACTIVE or record["agents"] != [agent]:
            raise KernelAdapterError("branch requires an exclusive active claimant")
        pattern = re.compile(rf"refs/heads/(?:feat|fix|docs)/issue-{number}-[^/]+$")
        matches = []
        for block in self.common.git(["worktree", "list", "--porcelain"]).split("\n\n"):
            values = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            if pattern.fullmatch(values.get("branch", "")):
                matches.append(values)
        if len(matches) > 1:
            raise KernelAdapterError("issue has ambiguous existing worktrees; preserve and reconcile")
        if matches:
            path = Path(matches[0].get("worktree", "")).resolve()
            primary = self.common.primary_worktree()
            if (
                not path.is_relative_to(primary / ".worktrees") or not path.is_dir()
                or self.common.repo_root(cwd=path) != path
                or self.common.checkout_repository(cwd=path).casefold() != self.repo.casefold()
                or self.common.git(["symbolic-ref", "HEAD"], cwd=path) != matches[0]["branch"]
            ):
                raise KernelAdapterError("existing issue worktree identity is unverified; preserve and reconcile")
            return str(path)
        if record["status"] != "In Progress":
            raise KernelAdapterError("In Review issue has no verified worktree; preserve and reconcile")
        return self.branches.create_worktree(number, "feat", agent)["path"]


def _main() -> int:
    try:
        if len(sys.argv) != 6 or sys.argv[1] != "--kernel-bridge":
            raise KernelAdapterError("kernel bridge requires an explicit repository operation")
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise KernelAdapterError("kernel bridge payload must be an object")
        bridge = _Bridge(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        result = bridge.dispatch(sys.argv[5], payload)
        print(json.dumps({"result": result}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
