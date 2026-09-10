"""One event/heartbeat path with sequential claims and account capacity locks."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import execution, scheduler, dependencies, permissions, quota, quota_boundary
from .config import Config, DriverError
from . import handoff_contract
from .kernel import KernelAdapter, KernelAdapterError
from .state import State, write_json

ACTIVE = {"In Progress", "In Review"}
RESUMABLE = {"claimed_issue", "feedback", "conflict", "verification"}
REVIEW_AUTHORITIES = {
    "coderabbit", "sourcery", "codeant", "claude-code", "openai-codex",
    "xai-cursor", "google-antigravity",
}
CODING_AUTHORITIES = REVIEW_AUTHORITIES - {"coderabbit", "sourcery", "codeant"}


class Controller:
    def __init__(self, config: Config, *, adapter_factory=KernelAdapter,
                 availability=execution.availability, probe=execution.probe,
                 launch=execution.launch, sync_reviews=None, now=time.time):
        self.config = config
        self.state = State(config.state_dir)
        self.adapter_factory = adapter_factory
        self.available = availability
        self.probe = probe
        self.launch = launch
        self.sync_reviews = sync_reviews or self._sync_reviews
        self.now = now

    def adapter(self, repo: str):
        return self.adapter_factory(self.config.kernel_root,
                                    Path(self.config.project(repo)["repo_dir"]), repo)

    @staticmethod
    def _owners(snapshot: dict) -> set[str]:
        return {agent for issue in snapshot["issues"] if issue["status"] in ACTIVE
                for agent in issue["agents"]} | {
                    pr["author_agent"] for pr in snapshot["prs"] if pr.get("author_agent")
                }

    def _sessions(self, identity: str) -> int:
        """Managed sessions allowed on the lane's subscription (1 unless configured)."""
        lane = self.config.lanes.get(identity) or {}
        return int(lane.get("max_sessions", 1)) if lane else 1

    def _lane_observations(self, repo: str, snapshot: dict) -> tuple[list, dict]:
        ready, blocked, seen = [], {}, {}
        owners = self._owners(snapshot)
        # A current claimant gets first use of its account, including across model aliases.
        identities = sorted(self.config.project(repo)["lanes"], key=lambda item: item not in owners)
        for identity in identities:
            lane = self.config.lane(repo, identity)
            # One free lane per remaining session slot on the shared subscription.
            if not quota.enabled(self.config, repo) and seen.get(lane["capacity_key"], 0) >= self._sessions(identity):
                blocked[identity] = {"available": False, "reason": "shared account already represented"}
                continue
            try:
                result = self.available(self.config, repo, identity, self.state)
            except DriverError as exc:
                result = {"available": False, "reason": str(exc)}
            if result.get("available") is True:
                seen[lane["capacity_key"]] = seen.get(lane["capacity_key"], 0) + 1
                ready.append(identity)
            else:
                blocked[identity] = result
        return ready, blocked

    def _existing(self, repo: str, snapshot: dict, adapter, available: list) -> tuple[list, list]:
        actions, resumes = [], []
        managed = self.state.workers(repo)
        for identity in sorted(self._owners(snapshot)):
            issues = [issue for issue in snapshot["issues"] if issue["status"] in ACTIVE
                      and identity in issue["agents"]]
            if len(issues) > 1:
                raise DriverError("multiple active claims for one identity")
            if identity not in self.config.project(repo)["lanes"] and not any(
                pr.get("author_agent") == identity for pr in snapshot["prs"]
            ):
                actions.append({"type": "external_owner", "agent": identity})
                continue
            receipts = [r for r in managed if r.get("agent") == identity and r.get("kind") != "review"]
            # Do not race a managed live writer. Unavailable quota alone must not
            # prevent read-only PR inspection, merge/finalize or reviewer refresh.
            if any(r.get("state") in {"launching", "running"}
                   and self._holds_reservation(r) for r in receipts):
                continue
            work = adapter.next_work(identity)
            work = {**work.get("work", work), "agent": identity}
            work = self._settle_live_review(work, managed)
            if not self._pr_matches_claim(work, snapshot, issues):
                actions.append({"type": "ownership_conflict", "agent": identity,
                                "pr": work["pr"],
                                "reason": "selected PR and sole active issue claim do not agree"})
                continue
            work = self._with_review_continuation(work, snapshot, adapter)
            if issues:
                work["issue"] = issues[0]["number"]
            if work["type"] in RESUMABLE:
                self._resume_action(repo, work, receipts, available, actions, resumes)
            elif work["type"] not in {"wait", "idle", "issue"} or work.get("next_action") or work.get("execution"):
                actions.append(work)
            elif not issues and work["type"] in {"idle", "issue"}:
                actions.append({"type": "ownership_conflict", "agent": identity,
                                "reason": "authored PR exists but picker did not resume it"})
        return actions, resumes

    def _resume_action(self, repo, work, receipts, available, actions, resumes):
        owned = [r for r in receipts if r.get("issue") == work.get("issue")]
        if not owned:
            actions.append({**work, "type": "unmanaged_claim",
                            "reason": "existing work needs explicit owner adoption"})
            return
        receipt = max(owned, key=lambda r: r["started_at"])
        blocker = next((reason for r in owned
                        if (reason := permissions.retry_blocker(self.config, r, work))), None)
        if blocker:
            actions.append({**work, "type": "worker_blocked", "execution": "blocked",
                            "reason": blocker, "worker_id": receipt["id"]})
        elif quota.enabled(self.config, repo) and receipt.get("outcome") in {"quota_exhausted", "quota_checkpoint"}:
            try:
                recovery = quota_boundary.resume(self, repo, work, receipt, available)
                if recovery:
                    resumes.append(recovery)
            except DriverError as exc:
                actions.append({**work, "type": "worker_blocked", "execution": "blocked",
                                "reason": str(exc), "worker_id": receipt["id"],
                                "owner": "Hermes Driver completion/heartbeat"})
        elif work["agent"] in available:
            resumes.append({**work, "worktree": receipt.get("worktree")})

    def _settle_live_review(self, work: dict, managed: list) -> dict:
        active = next((r for r in managed if r.get("kind") == "review" and r.get("pr") == work.get("pr")
                       and self._holds_reservation(r)), None)
        if active:
            # A substantive review settles before any author mutation.
            return {"type": "wait", "agent": work["agent"], "pr": work["pr"],
                    "head": work.get("head"), "execution": "running", "worker_id": active["id"],
                    "owner": "Hermes Driver", "next_step": "Worker completion or recovery heartbeat"}
        return work

    @staticmethod
    def _pr_matches_claim(work: dict, snapshot: dict, issues: list) -> bool:
        if not work.get("pr") or work["type"] == "finalize":
            return True
        prs = [pr for pr in snapshot["prs"] if pr["number"] == work["pr"]]
        return (len(prs) == 1 and len(issues) == 1
                and prs[0].get("issues") == [issues[0]["number"]])

    @staticmethod
    def _with_review_continuation(work: dict, snapshot: dict, adapter) -> dict:
        # CI and external review proceed concurrently. The single-agent picker
        # may return an explicit CI-blocked action. Observe an already assigned
        # authority without converting CI uncertainty into merge authorization.
        if (work["type"] not in {"wait", "blocked"} or work.get("next_action")
                or not work.get("pr") or work.get("execution") or work.get("review_error")):
            return work
        pr = next(item for item in snapshot["prs"] if item["number"] == work["pr"])
        authorities = [name[7:] for name in pr.get("labels", [])
                       if name.startswith("review:") and name[7:] in REVIEW_AUTHORITIES]
        if len(authorities) > 1:
            raise DriverError("PR has multiple authoritative reviewers")
        if authorities:
            head = work.get("head")
            if not isinstance(head, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", head):
                return {**work, "type": "blocked", "review_error": "current full PR head is unavailable"}
            try:
                return {**work, **adapter.reviewer_continuation(work["pr"], expected_head=head)}
            except KernelAdapterError as exc:
                return {**work, "type": "blocked", "review_error": str(exc)}
        return work

    def _admission_reasons(self, repo: str, snapshot: dict) -> list[str]:
        project = self.config.project(repo)
        if snapshot.get("complete") is not True or snapshot.get("repo") != repo:
            raise DriverError("complete current repository evidence is required")
        ci = snapshot.get("ci", {})
        reasons = []
        if snapshot.get("ci_available") is not True or type(ci.get("queued")) is not int:
            reasons.append("self-hosted verification capacity or queue depth is unproven")
        limit = project.get("max_review_backlog", 4)
        if len(snapshot["prs"]) >= limit:
            reasons.append("verification/review backlog limit reached")
        if type(ci.get("queued")) is int and ci["queued"] >= limit:
            reasons.append("queued verification limit reached")
        return reasons

    def _holds_reservation(self, receipt: dict) -> bool:
        holders = self.state.capacity_holders(receipt["capacity_key"], self._sessions(receipt.get("agent", "")))
        return receipt["id"] in holders

    def _worker_count(self, repo: str) -> int:
        # Count live reservations, not accounts: one subscription may hold several slots.
        return len({r["id"] for r in self.state.workers(repo)
                    if r.get("state") in {"launching", "running"} and self._holds_reservation(r)})

    def _action_due(self, action: dict) -> bool:
        if action.get("execution") == "running":
            return False
        if action.get("type") == "dependency":
            return action.get("next_action") in {"handoff", "dependency-satisfied"}
        if action.get("type") in {"external_owner", "unmanaged_claim", "ownership_conflict"}:
            return False
        if action.get("next_action") == "await-authoritative-review":
            # External services work independently. An assigned coding fallback
            # still needs the Hermes brain to arrange/check its review worker.
            return action.get("authority") in {
                "claude-code", "openai-codex", "xai-cursor", "google-antigravity",
            }
        if action.get("type") == "worker_blocked":
            return False
        retry = action.get("retry_at")
        if retry:
            try:
                deadline = datetime.fromisoformat(retry.replace("Z", "+00:00"))
                if deadline.tzinfo is None or deadline.utcoffset() is None:
                    raise ValueError("timezone required")
                return deadline.timestamp() <= self.now()
            except (ValueError, AttributeError, OverflowError, OSError):
                raise DriverError("review continuation has an unreadable deadline") from None
        return True

    def _plan(self, repo: str) -> dict:
        project, adapter = self.config.project(repo), self.adapter(repo)
        snapshot = adapter.snapshot()
        reasons = self._admission_reasons(repo, snapshot)
        quota_boundary.recover_results(self, repo)
        free, blocked = self._lane_observations(repo, snapshot)
        actions, resumes = self._existing(repo, snapshot, adapter, free)
        dependency_actions, held = dependencies.actions(self, repo, snapshot)
        actions = [a for a in actions if a.get("issue") not in held] + dependency_actions
        resumes = [a for a in resumes if a.get("issue") not in held]
        # Owners and assigned reviewers hold their account's session slots; a
        # subscription stays open for new work only while it has slots to spare.
        reserved_accounts: dict[str, int] = {}
        holders = [item for item in self._owners(snapshot) if item in project["lanes"]] + [
            name[9:] for pr in snapshot["prs"] for name in pr.get("labels", [])
            if name.startswith("reviewer:") and name[9:] in project["lanes"]
        ]
        for item in holders:
            account = self.config.lane(repo, item)["capacity_key"]
            reserved_accounts[account] = reserved_accounts.get(account, 0) + 1
        free = [item for item in free
                if reserved_accounts.get(self.config.lane(repo, item)["capacity_key"], 0)
                < self._sessions(item)]
        slots = max(0, project.get("max_workers", 4) - self._worker_count(repo))
        resumes = resumes[:slots]
        limit = len(free) if quota.enabled(self.config, repo) and slots > len(resumes) else max(0, slots - len(resumes))
        free = free[:limit] if not reasons else []
        candidates = adapter.candidates(snapshot, status="Ready") if free else []
        backlog = adapter.candidates(snapshot, status="Backlog") if (
            free and project.get("auto_triage", False)
        ) else []
        result = {
            "repo": repo, "actions": actions, "resumes": resumes, "free_lanes": free,
            "ready": [item["number"] for item in candidates],
            "backlog": [item["number"] for item in backlog],
            "blocked_lanes": blocked, "reasons": reasons,
            "blocked_issues": adapter.blocked(snapshot, status="Ready"),
            "worker_receipts": [{"id": r["id"], "state": r["state"]}
                                for r in self.state.workers(repo)[-32:]],
        }
        result["actionable"] = bool(resumes or any(self._action_due(a) for a in actions)
                                    or (free and (candidates or backlog)))
        result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        return result

    def tick(self, repo: str) -> dict:
        """Native cron precheck: cheap scripts stay quiet while no action is available."""
        self.config.project(repo)
        with self.state.lock():
            state = self.state.project(repo)
            if not state["enabled"]:
                return {"wakeAgent": False, "reason": "project stopped"}
            if max(state.get("cooldown_until", 0), state.get("wake_pending_until", 0)) > self.now():
                return {"wakeAgent": False, "reason": "cooldown or pending activation"}
            try:
                plan = self._plan(repo)
                if not self.state.project(repo)["enabled"]:
                    return {"wakeAgent": False, "reason": "project stopped"}
                self.sync_reviews(repo, plan["actions"])
            except (KernelAdapterError, DriverError, scheduler.SchedulerError) as exc:
                return self._failure(repo, state, exc, precheck=True)
            # An actionable observation is retried on the next heartbeat even
            # when its fingerprint repeats after a failed probe or missed event.
            wake = plan["actionable"]
            needs_attention = any(a.get("type") in {"unmanaged_claim", "ownership_conflict", "dependency"}
                                  for a in plan["actions"])
            wake |= needs_attention and plan["fingerprint"] != state.get("last_fingerprint")
            if wake:
                state["wake_pending_until"] = self.now() + 120
            blocked = [a for a in plan["actions"] if a.get("type") == "worker_blocked"]
            if blocked:
                state["last_error"] = "; ".join(a["reason"] for a in blocked)
            state["last_checked_at"] = self.now()
            state["last_observation"] = {"actionable": plan["actionable"], "reasons": plan["reasons"]}
            self.state.save(repo, state)
            if not self.state.project(repo)["enabled"]:
                return {"wakeAgent": False, "reason": "project stopped"}
            return {"wakeAgent": bool(wake), "project": repo, "plan": plan if wake else None,
                    **({"status": "degraded", "blockers": blocked} if blocked else {})}

    def _failure(self, repo: str, state: dict, exc: Exception, *, precheck: bool) -> dict:
        if not self.state.project(repo)["enabled"]:
            return {"wakeAgent": False, "status": "stopped", "reason": "project stopped"}
        message = str(exc)
        changed = state.get("last_error") != message
        delay = 3600 if any(s in message.lower() for s in ("rate limit", "rate-limit", "quota")) else 600
        state.update(last_error=message, cooldown_until=self.now() + delay,
                     wake_pending_until=0, last_checked_at=self.now())
        self.state.save(repo, state)
        wake = precheck and changed and self.state.project(repo)["enabled"]
        return {"wakeAgent": bool(wake), "status": "degraded", "reason": message}

    def _sync_reviews(self, repo: str, actions: list[dict]) -> dict:
        events = [{"pr": a.get("pr"), "head": a.get("head"),
                   "reviewer": a.get("authority", a.get("reviewer")), "retry_at": a["retry_at"]}
                  for a in actions if a.get("next_action") == "refresh-reviewer"
                  and a.get("retry_at") and not self._action_due(a)]
        return scheduler.sync_review_wakes(
            self.config.hermes_home, repo, self.config.path, Path(__file__).with_name("driver.py"),
            events=events, hermes_repo=self.config.hermes_repo,
        )

    def reconcile(self, repo: str) -> dict:
        """Only this locked script selects, claims and launches new writers."""
        self.config.project(repo)
        with self.state.lock():
            state = self.state.project(repo)
            if not state["enabled"]:
                return {"status": "stopped", "launched": []}
            if state.get("cooldown_until", 0) > self.now():
                return {"status": "cooldown", "launched": [], "until": state["cooldown_until"]}
            launched = []
            try:
                plan = self._plan(repo)
                if not self.state.project(repo)["enabled"]:
                    return {"status": "stopped", "launched": []}
                adapter = self.adapter(repo)
                quota_boundary.settle(self, repo, adapter)
                actions = [self._converge_review(repo, adapter, a, launched) for a in plan["actions"]]
                timers = self.sync_reviews(repo, actions)
                for work in plan["resumes"]:
                    self._resume_one(repo, adapter, work, launched)
                for identity in quota_boundary.ranked(self, repo, adapter, plan["free_lanes"]):
                    self._fill_one(repo, adapter, identity, launched)
                if not self.state.project(repo)["enabled"]:
                    return {"status": "stopped", "launched": launched,
                            "actions": [a for a in actions if a.get("execution") == "blocked"]}
                blocked = [a for a in actions if a.get("type") == "worker_blocked"]
                state.update(last_fingerprint=plan["fingerprint"], wake_pending_until=0,
                             last_reconciled_at=self.now(), last_error="; ".join(a["reason"] for a in blocked) or None,
                             handled_generation=state["generation"])
                self.state.save(repo, state)
                return {"status": "degraded" if blocked else "running" if launched or self._worker_count(repo) else "waiting",
                        "launched": launched, "actions": actions, "review_timers": timers,
                        "blocked_lanes": plan["blocked_lanes"], "reasons": plan["reasons"]}
            except (KernelAdapterError, DriverError, scheduler.SchedulerError) as exc:
                result = self._failure(repo, state, exc, precheck=False)
                return {**result, "launched": launched}

    def _resume_one(self, repo, adapter, work, launched):
        if not self.state.project(repo)["enabled"]:
            return
        if self._worker_count(repo) >= self.config.project(repo).get("max_workers", 4):
            return
        identity = work.get("quota_transfer", work["agent"])
        if not self.available(self.config, repo, identity, self.state).get("available"):
            return
        if not self.probe(self.config, repo, identity, self.state):
            return
        task = adapter.revalidate(work["issue"], agent=work["agent"])
        if not quota_boundary.approved(self, repo, adapter, identity, task, "remediation"):
            return
        work = quota_boundary.transfer(self, repo, adapter, work)
        # Idempotent canonical branch recovery also verifies any
        # recorded path; a receipt is never filesystem authority.
        worktree = adapter.branch(work["issue"], work["agent"])
        adapter.revalidate(work["issue"], agent=work["agent"])
        launched.append(self.launch(
            self.config, repo, work["agent"], work["issue"], str(worktree),
            kind="remediation", pr=work.get("pr"), head=work.get("head"),
        ))

    @staticmethod
    def _review_blocked(work: dict, reason: str) -> dict:
        return {**work, "execution": "blocked", "owner": "Hermes Driver",
                "reason": reason, "next_action": "review-blocked",
                "next_step": "Restore the stated gate, then reconcile; heartbeat owns retry"}

    def _converge_review(self, repo: str, adapter, work: dict, launched: list) -> dict:
        try:
            if work.get("next_action") == "refresh-reviewer" and self._action_due(work):
                if not self.state.project(repo)["enabled"]:
                    raise DriverError("project stopped before reviewer refresh")
                expected = {"repo": repo, "pr": work["pr"], "head": work["head"],
                            "authority": work.get("authority"), "author": work["agent"], "issue": work["issue"]}
                work = {**work, **adapter.refresh_reviewer(work["pr"], expected)}
            if (work.get("next_action") == "await-authoritative-review"
                    and work.get("authority") in CODING_AUTHORITIES):
                return self._dispatch_review(repo, adapter, work, launched)
            return work
        except (KernelAdapterError, DriverError) as exc:
            return self._review_blocked(work, str(exc))

    def _dispatch_review(self, repo: str, adapter, work: dict, launched: list, *, recover=True) -> dict:
        """One receipt per exact assignment; never infer a verdict from process exit."""
        try:
            binding = adapter.review_binding(work["pr"], {
                "repo": repo, "pr": work["pr"], "head": work["head"],
                "authority": work["authority"], "author": work["agent"], "issue": work["issue"],
            })
            work = {**work, "review_binding": binding}
            if binding.get("verdict"):
                next_work = adapter.next_work(binding["author"])
                return {**next_work.get("work", next_work), "agent": binding["author"],
                        "execution": "completed", "review_binding": binding,
                        "owner": "Hermes Driver", "next_step": "Use current kernel convergence action"}
            receipts = [r for r in self.state.workers(repo) if r.get("kind") == "review"
                        and r.get("pr") == binding["pr"]]
            if any(self._holds_reservation(r) for r in receipts):
                return {**work, "execution": "running", "owner": "Hermes Driver",
                        "next_step": "Existing worker completion event or recovery heartbeat"}
            matching = sorted((r for r in receipts if r.get("review") == binding),
                              key=lambda r: r["started_at"])
            if matching and matching[-1].get("retry_blocked") and not permissions.retry_blocker(self.config, matching[-1], work):
                return self._start_review(repo, adapter, work, binding, launched, recover)
            if matching:
                return self._recover_review(repo, adapter, work, matching[-1], launched, recover)
            return self._start_review(repo, adapter, work, binding, launched, recover)
        except (KernelAdapterError, DriverError) as exc:
            return self._review_blocked(work, str(exc))

    def _recover_review(self, repo: str, adapter, work: dict, receipt: dict, launched: list, recover: bool) -> dict:
        if not self.state.project(repo)["enabled"]:
            return self._review_blocked(work, "project stopped before reviewer recovery")
        reason = receipt.get("reason") or "assigned review worker ended or lost its reservation without a valid verdict"
        if not recover:
            return self._review_blocked(work, reason)
        if receipt.get("review_recovery_attempted"):
            return {**self._review_blocked(work, receipt.get("recovery_error") or reason),
                    "next_step": "Operator must reconcile the recorded recovery attempt before another helper call"}
        receipt["review_recovery_attempted"] = True
        write_json(self.state.worker_path(receipt["id"]), receipt)
        try:
            refreshed = adapter.refresh_reviewer(work["pr"], receipt["review"], reason)
        except (KernelAdapterError, DriverError) as exc:
            receipt["recovery_error"] = str(exc)
            write_json(self.state.worker_path(receipt["id"]), receipt)
            return {**self._review_blocked(work, str(exc)),
                    "next_step": "Operator must reconcile the recorded recovery attempt before another helper call"}
        next_work = {**work, **refreshed}
        next_work.pop("review_binding", None)
        if (next_work.get("next_action") == "await-authoritative-review"
                and next_work.get("authority") in CODING_AUTHORITIES):
            return self._dispatch_review(repo, adapter, next_work, launched, recover=False)
        return next_work

    def _start_review(self, repo: str, adapter, work: dict, binding: dict, launched: list, recover: bool) -> dict:
        identity = binding["reviewer"]
        lane = self.config.lane(repo, identity)
        if lane["family"] != binding["authority"]:
            raise DriverError("assigned reviewer family does not match the configured lane")
        snapshot = adapter.snapshot()
        if identity in self._owners(snapshot):
            raise DriverError("assigned reviewer has author work; independent review lane is unavailable")
        if self._worker_count(repo) >= self.config.project(repo).get("max_workers", 4):
            raise DriverError("project worker capacity is fully reserved")
        if quota.enabled(self.config, repo):
            task = adapter.revalidate(binding["issue"], agent=binding["author"])
            if not quota_boundary.decide(self, repo, adapter, identity, task, "review", binding):
                receipt = quota_boundary.review_failure(self, repo, binding)
                return self._recover_review(repo, adapter, work, receipt, launched, recover)
        capacity = self.available(self.config, repo, identity, self.state)
        if capacity.get("available") is not True:
            raise DriverError("review capacity unavailable or unknown: " + str(capacity.get("reason", "no observation")))
        if not self.probe(self.config, repo, identity, self.state):
            receipt = {"id": uuid.uuid4().hex, "repo": repo, "agent": identity, "issue": binding["issue"],
                       "kind": "review", "pr": binding["pr"], "head": binding["head"], "review": binding,
                       "capacity_key": lane["capacity_key"], "state": "launch_failed", "started_at": self.now(),
                       "reason": "assigned reviewer's bounded execution probe failed", "worktree": None}
            write_json(self.state.worker_path(receipt["id"]), receipt)
            return self._recover_review(repo, adapter, work, receipt, launched, recover)
        current = adapter.review_binding(work["pr"], binding)
        if current.get("verdict"):
            return self._dispatch_review(repo, adapter, work, launched, recover=recover)
        worktree = adapter.review_worktree(binding)
        adapter.review_binding(work["pr"], binding)
        if not self.state.project(repo)["enabled"]:
            raise DriverError("project stopped before review launch")
        receipt = self.launch(self.config, repo, identity, binding["issue"], worktree,
                              kind="review", pr=binding["pr"], head=binding["head"], review=binding)
        launched.append(receipt)
        return {**work, "execution": "queued", "worker_id": receipt["id"],
                "owner": "Hermes Driver", "next_step": "Supervised worker and completion wake"}

    def _fill_one(self, repo: str, adapter, identity: str, launched: list) -> None:
        if not self.state.project(repo)["enabled"]:
            return
        if not self.available(self.config, repo, identity, self.state).get("available"):
            return
        if self._worker_count(repo) >= self.config.project(repo).get("max_workers", 4):
            return
        snapshot = adapter.snapshot()
        if self._admission_reasons(repo, snapshot) or identity in self._owners(snapshot):
            return
        candidates = adapter.candidates(snapshot, status="Ready")
        promote = False
        if not candidates and self.config.project(repo).get("auto_triage", False):
            candidates = adapter.candidates(snapshot, status="Backlog")
            promote = True
        if not candidates or not self.probe(self.config, repo, identity, self.state):
            return
        # Probes can take time. Recheck the queue and candidate after they finish.
        snapshot = adapter.snapshot()
        if self._admission_reasons(repo, snapshot) or identity in self._owners(snapshot):
            return
        status = "Backlog" if promote else "Ready"
        eligible = {item["number"] for item in adapter.candidates(snapshot, status=status)}
        number = candidates[0]["number"]
        if number not in eligible or not self.state.project(repo)["enabled"]:
            return
        if quota.enabled(self.config, repo) and not quota_boundary.decide(self, repo, adapter, identity, candidates[0]):
            return
        if promote:
            adapter.promote(number)
        # Journal intent BEFORE the first claim. A crash after claim/branch can
        # recover our exclusive claim; this receipt never overrides GitHub.
        intent = {"id": uuid.uuid4().hex, "repo": repo, "agent": identity, "issue": number,
                  "capacity_key": self.config.lane(repo, identity)["capacity_key"],
                  "started_at": self.now(), "state": "claiming", "worktree": None}
        write_json(self.state.worker_path(intent["id"]), intent)
        adapter.claim(number, identity)
        worktree = adapter.branch(number, identity)
        intent.update(state="prepared", worktree=str(worktree))
        write_json(self.state.worker_path(intent["id"]), intent)
        adapter.revalidate(number, agent=identity)
        launched.append(self.launch(self.config, repo, identity, number, str(worktree)))

    def event(self, repo: str, event_id: str, reason: str, *, inline: bool = False) -> dict:
        self.config.project(repo)
        if not event_id or len(event_id) > 512:
            raise DriverError("event requires a bounded unique delivery id")
        with self.state.lock():
            return self._event_locked(repo, event_id, reason, inline=inline)

    def _event_locked(self, repo: str, event_id: str, reason: str, *, inline=False) -> dict:
        if not self.state.project(repo)["enabled"]:
            return {"accepted": False, "wakeAgent": False, "project": repo, "status": "stopped"}
        if self.state.has_event(repo, event_id):
            return {"accepted": False, "wakeAgent": False, "project": repo, "status": "duplicate"}
        self.state.check_event_capacity(repo, event_id)
        # Native scheduling is itself keyed. If it fails, do not acknowledge
        # delivery; a retry after a crash reuses the same native job.
        if not inline:
            scheduler.schedule_wake(
                self.config.hermes_home, repo, self.config.path,
                Path(__file__).with_name("driver.py"), reason=reason, event_key=event_id,
                hermes_repo=self.config.hermes_repo,
            )
        if not self.state.event(repo, event_id, reason):
            return {"accepted": False, "wakeAgent": False, "project": repo, "status": "stopped"}
        data = self.state.project(repo)
        data.update(last_fingerprint=None, wake_pending_until=0)
        self.state.save(repo, data)
        if not self.state.project(repo)["enabled"]:
            return {"accepted": False, "wakeAgent": False, "project": repo, "status": "stopped"}
        return {"accepted": True, "wakeAgent": True, "project": repo, "status": "delivered"}

    def _dependency_contract(self, repo: str, source_issue: int) -> tuple[dict, dict]:
        if not handoff_contract.number(source_issue):
            raise DriverError("source issue must be a positive integer")
        summary = self.adapter(repo).issue_summary(source_issue)
        if summary["state"] != "OPEN" or summary["status"] not in ACTIVE:
            raise DriverError("dependency source issue must be open and active")
        contract = handoff_contract.parse(summary["body"], repo)
        if contract is None:
            raise DriverError("source issue has no typed Driver dependency contract")
        if contract["target"] not in self.config.project(repo).get("handoff_to", []):
            raise DriverError("source project has no explicit route to the dependency target")
        pr = self.adapter(repo).source_pr(contract["source_pr"])
        if (pr.get("state") != "OPEN" or pr.get("head") != contract["source_head"]
                or pr.get("issues") != [source_issue]):
            raise DriverError("source PR head or linked issue differs from the dependency contract")
        return contract, summary

    def _target_readiness(self, target: str, issue_number: int, *, snapshot: dict | None = None) -> dict:
        target_adapter = self.adapter(target)
        snapshot = target_adapter.snapshot() if snapshot is None else snapshot
        matches = [record for record in snapshot["issues"] if record["number"] == issue_number]
        if len(matches) != 1:
            raise DriverError("target dependency issue is absent from the complete snapshot")
        record = matches[0]
        blockers = list(record.get("errors", []))
        summary = target_adapter.issue_summary(issue_number)
        if summary["status"] != record.get("status") or summary["state"] != record.get("state"):
            blockers.append("target issue and linked Project evidence changed")
        blockers.extend(self._admission_reasons(target, snapshot))
        enabled = self.state.project(target).get("enabled") is True
        if not enabled:
            blockers.append("target Driver is stopped")
        if record.get("agents"):
            blockers.append("target issue already has an agent claim")
        if record.get("state") != "OPEN":
            blockers.append("target issue is not open")
        if record.get("status") not in {"Backlog", "Ready"}:
            blockers.append(f"target issue is {record.get('status') or 'untracked'}")
        if record.get("status") == "Ready":
            blockers.extend(target_adapter.blocked(snapshot, status="Ready").get(str(issue_number), []))
        elif record.get("status") == "Backlog":
            if self.config.project(target).get("auto_triage", False):
                blockers.extend(target_adapter.blocked(snapshot, status="Backlog").get(str(issue_number), []))
            else:
                blockers.append("target issue requires explicit triage before dispatch")
        free_lanes, blocked_lanes = self._lane_observations(target, snapshot)
        owners = self._owners(snapshot)
        reserved = {self.config.lane(target, identity)["capacity_key"] for identity in owners
                    if identity in self.config.project(target)["lanes"]}
        free_lanes = [identity for identity in free_lanes
                      if self.config.lane(target, identity)["capacity_key"] not in reserved]
        if self._worker_count(target) >= self.config.project(target).get("max_workers", 4):
            free_lanes = []
        if not free_lanes:
            blockers.append("target has no currently available coding lane")
        return {
            "repo": target,
            "issue": issue_number,
            "status": record.get("status"),
            "priority": record.get("priority"),
            "touches": record.get("touches", []),
            "enabled": enabled,
            "free_lanes": free_lanes,
            "blocked_lanes": blocked_lanes,
            "blockers": sorted(set(blockers)),
            "snapshot_at": snapshot.get("observed_at"),
        }

    def handoff(self, repo: str, source_issue: int) -> dict:
        """Validate and deliver one authenticated, idempotent cross-project wake."""
        self.config.project(repo)
        with self.state.lock():
            if not self.state.project(repo)["enabled"]:
                return {"accepted": False, "delivered": False, "status": "stopped", "project": repo}
            return self._handoff_locked(repo, source_issue)

    def _handoff_locked(self, repo: str, source_issue: int) -> dict:
        contract, summary = self._dependency_contract(repo, source_issue)
        target = contract["target"]
        if target not in self.config.project(repo).get("handoff_to", []):
            raise DriverError("source project has no explicit route to the dependency target")
        readiness = self._target_readiness(target, contract["issue"])
        digest = handoff_contract.digest(contract)
        if readiness["blockers"]:
            return {
                "accepted": False,
                "delivered": False,
                "project": repo,
                "source_issue": source_issue,
                "source_url": summary["url"],
                "target": readiness,
                "contract_digest": digest,
            }
        event_id = f"handoff:{repo}:{source_issue}:{digest}"
        fresh, _ = self._dependency_contract(repo, source_issue)
        if fresh != contract:
            raise DriverError("dependency contract changed during target validation")
        # Acknowledge only an existing or newly scheduled native wake.
        delivered = self._event_locked(target, event_id, "dependency handoff")
        return {
            "accepted": delivered["status"] in {"delivered", "duplicate"},
            "delivered": delivered["status"] in {"delivered", "duplicate"},
            "duplicate": delivered["status"] == "duplicate",
            "wakeAgent": delivered["wakeAgent"],
            "project": repo,
            "source_issue": source_issue,
            "source_url": summary["url"],
            "target": readiness,
            "contract_digest": digest,
            "next_action": "reconcile target using current priority and admission gates",
            "source_pr": contract["source_pr"],
            "source_head": contract["source_head"],
        }

    def dependency_event(self, repo: str, source_issue: int) -> dict:
        """Wake the origin only after every typed dependency proof is satisfied."""
        self.config.project(repo)
        with self.state.lock():
            if not self.state.project(repo)["enabled"]:
                return {"accepted": False, "wakeAgent": False, "status": "stopped", "project": repo}
            return self._dependency_event_locked(repo, source_issue)

    def _dependency_event_locked(self, repo: str, source_issue: int) -> dict:
        contract, summary = self._dependency_contract(repo, source_issue)
        proofs = dependencies.proofs(self, contract)
        unsatisfied = [item for item in proofs if item["proof"].get("satisfied") is not True]
        digest = handoff_contract.digest(contract)
        if unsatisfied:
            return {
                "accepted": False,
                "wakeAgent": False,
                "project": repo,
                "source_issue": source_issue,
                "source_url": summary["url"],
                "contract_digest": digest,
                "proofs": proofs,
                "blockers": [item["proof"].get("reason", "dependency condition is not satisfied")
                             for item in unsatisfied],
            }
        fresh, _ = self._dependency_contract(repo, source_issue)
        if fresh != contract:
            raise DriverError("dependency contract changed during proof collection")
        proofs = dependencies.proofs(self, fresh)
        if any(item["proof"].get("satisfied") is not True for item in proofs):
            return {"accepted": False, "wakeAgent": False, "project": repo,
                    "source_issue": source_issue, "contract_digest": digest, "proofs": proofs,
                    "blockers": ["dependency proof changed before return delivery"]}
        result = self._event_locked(repo, f"dependency:{repo}:{source_issue}:{digest}", "dependency satisfied")
        return {
            **result,
            "source_issue": source_issue,
            "source_url": summary["url"],
            "contract_digest": digest,
            "proofs": proofs,
        }
