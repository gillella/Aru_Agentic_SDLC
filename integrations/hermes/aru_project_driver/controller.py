"""One event/heartbeat path with sequential claims and account capacity locks."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import execution, scheduler
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

    def _lane_observations(self, repo: str, snapshot: dict) -> tuple[list, dict]:
        ready, blocked, seen = [], {}, set()
        owners = self._owners(snapshot)
        # A current claimant gets first use of its account, including across model aliases.
        identities = sorted(self.config.project(repo)["lanes"], key=lambda item: item not in owners)
        for identity in identities:
            lane = self.config.lane(repo, identity)
            if lane["capacity_key"] in seen:
                blocked[identity] = {"available": False, "reason": "shared account already represented"}
                continue
            try:
                result = self.available(self.config, repo, identity, self.state)
            except DriverError as exc:
                result = {"available": False, "reason": str(exc)}
            if result.get("available") is True:
                seen.add(lane["capacity_key"])
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
            if identity not in self.config.project(repo)["lanes"]:
                actions.append({"type": "external_owner", "agent": identity})
                continue
            receipts = [r for r in managed if r.get("agent") == identity]
            # Do not race a managed live writer. Unavailable quota alone must not
            # prevent read-only PR inspection, merge/finalize or reviewer refresh.
            if any(r.get("state") in {"launching", "running"}
                   and self.state.capacity_holder(r["capacity_key"]) == r["id"] for r in receipts):
                continue
            work = adapter.next_work(identity)
            work = {**work.get("work", work), "agent": identity}
            if not self._pr_matches_claim(work, snapshot, issues):
                actions.append({"type": "ownership_conflict", "agent": identity,
                                "pr": work["pr"],
                                "reason": "selected PR and sole active issue claim do not agree"})
                continue
            work = self._with_review_continuation(work, snapshot, adapter)
            if issues:
                work["issue"] = issues[0]["number"]
            if work["type"] in RESUMABLE:
                owned = [r for r in receipts if r.get("issue") == work.get("issue")]
                if not owned:
                    actions.append({**work, "type": "unmanaged_claim",
                                    "reason": "existing work needs explicit owner adoption"})
                elif identity in available:
                    receipt = max(owned, key=lambda r: r["started_at"])
                    resumes.append({**work, "worktree": receipt.get("worktree")})
            elif work["type"] not in {"wait", "idle", "issue"} or work.get("next_action"):
                actions.append(work)
            elif not issues and work["type"] in {"idle", "issue"}:
                actions.append({"type": "ownership_conflict", "agent": identity,
                                "reason": "authored PR exists but picker did not resume it"})
        return actions, resumes

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
        # only exposes review continuation after green CI, so observe an
        # already assigned authority here without changing its policy.
        if work["type"] != "wait" or work.get("next_action") or not work.get("pr"):
            return work
        pr = next(item for item in snapshot["prs"] if item["number"] == work["pr"])
        authorities = [name[7:] for name in pr.get("labels", [])
                       if name.startswith("review:") and name[7:] in REVIEW_AUTHORITIES]
        if len(authorities) > 1:
            raise DriverError("PR has multiple authoritative reviewers")
        if authorities:
            return {**work, **adapter.reviewer_continuation(work["pr"])}
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

    def _worker_count(self, repo: str) -> int:
        return len({r["capacity_key"] for r in self.state.workers(repo)
                    if r.get("state") in {"launching", "running"}
                    and self.state.capacity_holder(r["capacity_key"]) == r["id"]})

    def _action_due(self, action: dict) -> bool:
        if action.get("type") in {"external_owner", "unmanaged_claim", "ownership_conflict"}:
            return False
        if action.get("next_action") == "await-authoritative-review":
            # External services work independently. An assigned coding fallback
            # still needs the Hermes brain to arrange/check its review worker.
            return action.get("authority") in {
                "claude-code", "openai-codex", "xai-cursor", "google-antigravity",
            }
        retry = action.get("retry_at")
        if retry:
            try:
                return datetime.fromisoformat(retry.replace("Z", "+00:00")).timestamp() <= self.now()
            except (ValueError, AttributeError):
                raise DriverError("review continuation has an unreadable deadline") from None
        return True

    def _plan(self, repo: str) -> dict:
        project, adapter = self.config.project(repo), self.adapter(repo)
        snapshot = adapter.snapshot()
        reasons = self._admission_reasons(repo, snapshot)
        free, blocked = self._lane_observations(repo, snapshot)
        actions, resumes = self._existing(repo, snapshot, adapter, free)
        reserved_accounts = {self.config.lane(repo, item)["capacity_key"]
                             for item in self._owners(snapshot) if item in project["lanes"]}
        free = [item for item in free
                if self.config.lane(repo, item)["capacity_key"] not in reserved_accounts]
        slots = max(0, project.get("max_workers", 4) - self._worker_count(repo))
        resumes = resumes[:slots]
        free = free[:max(0, slots - len(resumes))] if not reasons else []
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
                self.sync_reviews(repo, plan["actions"])
            except (KernelAdapterError, DriverError, scheduler.SchedulerError) as exc:
                return self._failure(repo, state, exc, precheck=True)
            # An actionable observation is retried on the next heartbeat even
            # when its fingerprint repeats after a failed probe or missed event.
            wake = plan["actionable"]
            needs_attention = any(a.get("type") in {"unmanaged_claim", "ownership_conflict"}
                                  for a in plan["actions"])
            wake |= needs_attention and plan["fingerprint"] != state.get("last_fingerprint")
            if wake:
                state["wake_pending_until"] = self.now() + 120
            state["last_checked_at"] = self.now()
            state["last_observation"] = {"actionable": plan["actionable"], "reasons": plan["reasons"]}
            self.state.save(repo, state)
            return {"wakeAgent": bool(wake), "project": repo, "plan": plan if wake else None}

    def _failure(self, repo: str, state: dict, exc: Exception, *, precheck: bool) -> dict:
        message = str(exc)
        changed = state.get("last_error") != message
        delay = 3600 if any(s in message.lower() for s in ("rate limit", "rate-limit", "quota")) else 600
        state.update(last_error=message, cooldown_until=self.now() + delay,
                     wake_pending_until=0, last_checked_at=self.now())
        self.state.save(repo, state)
        return {"wakeAgent": bool(precheck and changed), "status": "degraded", "reason": message}

    def _sync_reviews(self, repo: str, actions: list[dict]) -> dict:
        events = [{"pr": a["pr"], "head": a["head"],
                   "reviewer": a.get("authority", a.get("reviewer")), "retry_at": a["retry_at"]}
                  for a in actions if a.get("next_action") == "refresh-reviewer"
                  and a.get("retry_at") and a.get("pr") and a.get("head")
                  and a.get("authority", a.get("reviewer")) and not self._action_due(a)]
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
                timers = self.sync_reviews(repo, plan["actions"])
                adapter = self.adapter(repo)
                for work in plan["resumes"]:
                    if not self.available(self.config, repo, work["agent"], self.state).get("available"):
                        continue
                    if not self.probe(self.config, repo, work["agent"], self.state):
                        continue
                    adapter.revalidate(work["issue"], agent=work["agent"])
                    # Idempotent canonical branch recovery also verifies any
                    # recorded path; a receipt is never filesystem authority.
                    worktree = adapter.branch(work["issue"], work["agent"])
                    adapter.revalidate(work["issue"], agent=work["agent"])
                    launched.append(self.launch(
                        self.config, repo, work["agent"], work["issue"], str(worktree),
                        kind="remediation", pr=work.get("pr"), head=work.get("head"),
                    ))
                for identity in plan["free_lanes"]:
                    self._fill_one(repo, adapter, identity, launched)
                state.update(last_fingerprint=plan["fingerprint"], wake_pending_until=0,
                             last_reconciled_at=self.now(), last_error=None,
                             handled_generation=state["generation"])
                self.state.save(repo, state)
                return {"status": "running" if launched else "waiting",
                        "launched": launched, "actions": plan["actions"], "review_timers": timers,
                        "blocked_lanes": plan["blocked_lanes"], "reasons": plan["reasons"]}
            except (KernelAdapterError, DriverError, scheduler.SchedulerError) as exc:
                result = self._failure(repo, state, exc, precheck=False)
                return {**result, "launched": launched}

    def _fill_one(self, repo: str, adapter, identity: str, launched: list) -> None:
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
        if number not in eligible:
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
            accepted = self.state.event(repo, event_id, reason)
            if accepted:
                data = self.state.project(repo)
                data["last_fingerprint"] = None
                data["wake_pending_until"] = 0
                self.state.save(repo, data)
                if not inline:
                    scheduler.schedule_wake(
                        self.config.hermes_home, repo, self.config.path,
                        Path(__file__).with_name("driver.py"), reason=reason, event_key=event_id,
                        hermes_repo=self.config.hermes_repo,
                    )
        return {"accepted": accepted, "wakeAgent": accepted, "project": repo}

    def _dependency_contract(self, repo: str, source_issue: int) -> tuple[dict, dict]:
        summary = self.adapter(repo).issue_summary(source_issue)
        if summary["state"] != "OPEN" or summary["status"] not in ACTIVE:
            raise DriverError("dependency source issue must be open and active")
        contract = handoff_contract.parse(summary["body"], repo)
        if contract is None:
            raise DriverError("source issue has no typed Driver dependency contract")
        return contract, summary

    def _target_readiness(self, target: str, issue_number: int) -> dict:
        target_adapter = self.adapter(target)
        snapshot = target_adapter.snapshot()
        matches = [record for record in snapshot["issues"] if record["number"] == issue_number]
        if len(matches) != 1:
            raise DriverError("target dependency issue is absent from the complete snapshot")
        record = matches[0]
        blockers = list(record.get("errors", []))
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
        else:
            blockers.append("target issue requires explicit triage before dispatch")
        return {
            "repo": target,
            "issue": issue_number,
            "status": record.get("status"),
            "priority": record.get("priority"),
            "touches": record.get("touches", []),
            "enabled": enabled,
            "blockers": sorted(set(blockers)),
            "snapshot_at": snapshot.get("observed_at"),
        }

    def handoff(self, repo: str, source_issue: int) -> dict:
        """Validate and deliver one authenticated, idempotent cross-project wake."""
        self.config.project(repo)
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
        delivered = self.event(target, event_id, "dependency handoff")
        return {
            "accepted": True,
            "delivered": delivered["accepted"],
            "duplicate": not delivered["accepted"],
            "wakeAgent": delivered["wakeAgent"],
            "project": repo,
            "source_issue": source_issue,
            "source_url": summary["url"],
            "target": readiness,
            "contract_digest": digest,
        }

    def dependency_event(self, repo: str, source_issue: int) -> dict:
        """Wake the origin only after every typed dependency proof is satisfied."""
        self.config.project(repo)
        contract, summary = self._dependency_contract(repo, source_issue)
        proofs = []
        for condition in contract["conditions"]:
            bridge = self.adapter(condition["repo"])
            try:
                proof = bridge.dependency_evidence(condition)
            except (KernelAdapterError, ValueError) as exc:
                proof = {"satisfied": False, "reason": str(exc)}
            proofs.append({"condition": condition, "proof": proof})
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
        result = self.event(repo, f"dependency:{digest}", "dependency satisfied")
        return {
            **result,
            "source_issue": source_issue,
            "source_url": summary["url"],
            "contract_digest": digest,
            "proofs": proofs,
        }
