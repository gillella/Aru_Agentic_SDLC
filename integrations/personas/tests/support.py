"""SYNTHETIC ONLY: all success probes here are invented test fixtures."""
from dataclasses import replace
from datetime import datetime, timezone
from integrations.personas import *  # noqa: F403
from integrations.personas.policy import default_snapshot, from_document

NOW = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
PROJECT = "gillella/Aru_Agentic_SDLC"
WORKTREE = "/synthetic/worktrees/issue-630"
HEAD = "a" * 40
CONTEXT = PromptContext(WORKTREE, branch="feat/synthetic", head=HEAD, pr=700)

#: Auth profiles are per account; distinct absolute directories, never shared.
PROFILES = {"claude-code": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def snapshot_of(policy=None):
    """A snapshot from a document, an already built snapshot, or the defaults."""
    if policy is None:
        return default_snapshot()
    if isinstance(policy, PolicySnapshot):
        return policy
    return from_document(policy, origin="SYNTHETIC operator document")


def env_for(account):
    name = PROFILES.get(account.route)
    return {name: f"/synthetic/profiles/{account.id}"} if name else {}


def fleet(policy=None, probe_accounts=None):
    policy = snapshot_of(policy)
    accounts = tuple(AccountBinding(
        a.id, ("Unum-Inc/example",) if a.required_owners else (PROJECT,),
        env=env_for(a), snapshot=policy) for a in policy.accounts.values())
    harnesses = {route: HarnessBinding(route, f"/synthetic/bin/{exe}", WORKTREE,
                  declared_modalities=frozenset({"image"}) if route == "antigravity" else frozenset(),
                  modality_evidence="SYNTHETIC installation reads attached reference files")
                 for route, exe in [("claude-code", "claude"), ("codex", "codex"),
                                    ("cursor", "cursor-agent"), ("antigravity", "agy")]}
    records = tuple(ProbeRecord(a.account_id, p.route, model, effort, NOW, "ok",
                    source="SYNTHETIC test fixture; NOT live access", authenticated=True,
                    identity_digest=a.identity_digest, modalities=frozenset({"text", "image"}))
                    for p in policy.personas.values() for effort, model in p.model_ids.items()
                    for a in accounts if a.policy.route == p.route
                    if probe_accounts is None or a.account_id in probe_accounts)
    return FleetBinding(harnesses, accounts, EvidenceStore(records, origin="SYNTHETIC TESTS ONLY"),
                        frozenset({"spark-pair"}), snapshot=policy)


def request(persona_id="astra-implementer", policy=None, **changes):
    p = snapshot_of(policy).persona(persona_id)
    values = dict(project=PROJECT, issue=630, task_class=p.primary_task,
                  touches=("docs/note.md",) if p.id == "haiku-triage" else ("src/main.py",),
                  persona_override=p.id, actor="synthetic-author", allow_optional=True)
    if p.id == "pro-design":
        values["input_files"] = ("/synthetic/reference.png",)
    values.update(changes)
    return TaskRequest(**values)


def assignment(reviewer="sonnet-reviewer", author="astra-implementer", policy=None, **changes):
    snapshot = snapshot_of(policy)
    p = snapshot.persona(reviewer)
    a = next(a for a in snapshot.accounts.values() if a.route == p.route)
    author_p = snapshot.persona(author)
    author_a = next(a for a in snapshot.accounts.values() if a.route == author_p.route)
    values = dict(repo=PROJECT, pr=700, head=HEAD, issue=630, risk_tier=1,
                  authority=p.lineage, reviewer_persona=reviewer, reviewer_actor="synthetic-reviewer",
                  reviewer_account=a.id,
                  authors=(AuthorIdentity(author, author_a.id, "synthetic-author",
                                          snapshot=snapshot),),
                  touches=("src/main.py",), authority_source="SYNTHETIC kernel snapshot",
                  external_first_released=True, external_first_reason="SYNTHETIC provider unavailable")
    values.update(changes)
    return ReviewAssignment(**values)


def change_probes(binding, predicate, **changes):
    return replace(binding, evidence=replace(binding.evidence, records=tuple(
        replace(r, **changes) if predicate(r) else r for r in binding.evidence.records)))


#: Fields that carry a live policy snapshot rather than JSON. A wire packet
#: names its policy once, in the binding, and never repeats it per entry.
NOT_ON_THE_WIRE = ("snapshot",)


def wire(item):
    """One dataclass as the JSON envelope carries it, without the live snapshot."""
    from dataclasses import fields, is_dataclass
    raw = {}
    for spec in fields(item):
        if spec.name in NOT_ON_THE_WIRE:
            continue
        value = getattr(item, spec.name)
        if isinstance(value, tuple) and value and is_dataclass(value[0]):
            value = [wire(entry) for entry in value]
        raw[spec.name] = value
    return raw


def packet(persona_id, policy=None):
    from dataclasses import asdict
    b = fleet(policy)
    binding = {"schema": "aru.personas.fleet-binding/v1",
               "harnesses": {key: {"executable": h.executable, "workspace": h.workspace,
                           "declared_modalities": sorted(h.declared_modalities),
                           "modality_evidence": h.modality_evidence} for key, h in b.harnesses.items()},
               "accounts": [wire(a) for a in b.accounts],
               "evidence": b.evidence.to_dict(), "enabled_optional": sorted(b.enabled_optional)}
    if policy is not None:
        binding["policy"] = policy
    raw = {"schema": "aru.personas.request/v1", "synthetic": True,
           "binding": binding, "context": asdict(CONTEXT)}
    if persona_id == "sonnet-reviewer":
        a = assignment().to_dict()
        for author in a["authors"]:
            author.pop("family")
            author.pop("vendor")
        raw.update(assignment=a, current_assignment=a)
    else:
        raw["request"] = wire(request(persona_id, policy=policy))
        raw["request"]["modalities"] = sorted(raw["request"]["modalities"])
    return raw
