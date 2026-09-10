"""SYNTHETIC ONLY: all success probes here are invented test fixtures."""
from dataclasses import replace
from datetime import datetime, timezone
from integrations.personas import (
    AccountBinding, AuthorIdentity, EvidenceStore, FleetBinding, HarnessBinding, PERSONAS,
    ProbeRecord, PromptContext, ReviewAssignment, TaskRequest,
)
from integrations.personas.registry import ACCOUNTS

NOW = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
PROJECT = "gillella/Aru_Agentic_SDLC"
WORKTREE = "/synthetic/worktrees/issue-630"
HEAD = "a" * 40
CONTEXT = PromptContext(WORKTREE, branch="feat/synthetic", head=HEAD, pr=700)


def fleet():
    accounts = tuple(AccountBinding(a.id, ("Unum-Inc/example",) if a.id.endswith("-4") else (PROJECT,),
                    env=({"CLAUDE_CONFIG_DIR": f"/synthetic/profiles/{a.id}"} if a.route == "claude-code"
                         else {"CODEX_HOME": "/synthetic/profiles/codex"} if a.route == "codex" else {}))
                     for a in ACCOUNTS.values())
    harnesses = {route: HarnessBinding(route, f"/synthetic/bin/{exe}", WORKTREE,
                  declared_modalities=frozenset({"image"}) if route == "antigravity" else frozenset(),
                  modality_evidence="SYNTHETIC installation reads attached reference files")
                 for route, exe in [("claude-code", "claude"), ("codex", "codex"),
                                    ("cursor", "cursor-agent"), ("antigravity", "agy")]}
    records = tuple(ProbeRecord(a.account_id, p.route, model, effort, NOW, "ok",
                    source="SYNTHETIC test fixture; NOT live access", authenticated=True,
                    identity_digest=a.identity_digest, modalities=frozenset({"text", "image"}))
                    for p in PERSONAS.values() for effort, model in p.model_ids.items()
                    for a in accounts if a.policy.route == p.route)
    return FleetBinding(harnesses, accounts, EvidenceStore(records, origin="SYNTHETIC TESTS ONLY"),
                        frozenset({"spark-pair"}))


def request(persona_id="astra-implementer", **changes):
    p = PERSONAS[persona_id]
    values = dict(project=PROJECT, issue=630, task_class=p.primary_task,
                  touches=("docs/note.md",) if p.id == "haiku-triage" else ("src/main.py",),
                  persona_override=p.id, actor="synthetic-author", allow_optional=True)
    if p.id == "pro-design":
        values["input_files"] = ("/synthetic/reference.png",)
    values.update(changes)
    return TaskRequest(**values)


def assignment(reviewer="sonnet-reviewer", author="astra-implementer", **changes):
    p = PERSONAS[reviewer]
    a = next(a for a in ACCOUNTS.values() if a.route == p.route)
    author_p = PERSONAS[author]
    author_a = next(a for a in ACCOUNTS.values() if a.route == author_p.route)
    values = dict(repo=PROJECT, pr=700, head=HEAD, issue=630, risk_tier=1,
                  authority=p.lineage, reviewer_persona=reviewer, reviewer_actor="synthetic-reviewer",
                  reviewer_account=a.id,
                  authors=(AuthorIdentity(author, author_a.id, "synthetic-author"),),
                  touches=("src/main.py",), authority_source="SYNTHETIC kernel snapshot",
                  external_first_released=True, external_first_reason="SYNTHETIC provider unavailable")
    values.update(changes)
    return ReviewAssignment(**values)


def change_probes(binding, predicate, **changes):
    return replace(binding, evidence=replace(binding.evidence, records=tuple(
        replace(r, **changes) if predicate(r) else r for r in binding.evidence.records)))


def packet(persona_id):
    from dataclasses import asdict
    b = fleet()
    binding = {"schema": "aru.personas.fleet-binding/v1",
               "harnesses": {key: {"executable": h.executable, "workspace": h.workspace,
                           "declared_modalities": sorted(h.declared_modalities),
                           "modality_evidence": h.modality_evidence} for key, h in b.harnesses.items()},
               "accounts": [asdict(a) for a in b.accounts],
               "evidence": b.evidence.to_dict(), "enabled_optional": sorted(b.enabled_optional)}
    raw = {"schema": "aru.personas.request/v1", "synthetic": True,
           "binding": binding, "context": asdict(CONTEXT)}
    if persona_id == "sonnet-reviewer":
        a = assignment().to_dict()
        for author in a["authors"]:
            author.pop("family")
        raw.update(assignment=a, current_assignment=a)
    else:
        raw["request"] = asdict(request(persona_id))
        raw["request"]["modalities"] = sorted(raw["request"]["modalities"])
    return raw
