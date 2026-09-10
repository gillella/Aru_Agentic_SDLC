"""Configuration extensibility (#637). Every capability success is SYNTHETIC.

These tests add a real extra expert and a real fifth subscription through
disposable documents built here, resolve them, then disable and remove them,
without editing any source module. What they prove is source behaviour only:
they do not prove authentication, usable quota, a drained running worker or a
live installation. Those remain #631/#636.
"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from integrations.personas import (
    AccountBinding, AuthorIdentity, resolve, TaskRequest, validate_assignment, verify_payload,
)
from integrations.personas.errors import (
    AccountScopeError, AccountStateError, NoEligibleCandidateError, PersonaPolicyError,
    PolicyDocumentError, PolicyPrivilegeError, ReviewIndependenceError, RiskEvidenceError,
    UnknownTaskError, UnsupportedEffortError,
)
from integrations.personas.policy import (
    KERNEL_INVARIANTS, POLICY_SCHEMA, default_snapshot, from_document,
    load_policy_document, preview, publish,
)
from integrations.personas.registry import MANDATORY_STOP_CRITERIA, PERSONAS
from .support import (
    CONTEXT, HEAD, NOW, PROJECT, assignment, change_probes, fleet, packet, request,
)

ROOT = Path(__file__).resolve().parents[3]


def base_document():
    return default_snapshot().to_document()


def entry_for(section, key, value):
    return next(item for item in base_document()[section] if item[key] == value)


def document(**sections):
    """One operator document over the shipped defaults."""
    raw = {"schema": POLICY_SCHEMA, "version": "1.1.0-synthetic", "base": "default",
           "note": "SYNTHETIC disposable operator policy for tests."}
    raw.update(sections)
    return raw


# --------------------------------------------------------------------------- #
# The configured expert and the configured subscription
# --------------------------------------------------------------------------- #

DATABASE_ROLE = {
    "id": "database_expert",
    "base": "lead_systems_implementer",
    "title": "Database Migration Expert",
    "scope": ("Plan, apply and reverse schema migrations and the data backfills that "
              "accompany them, inside one declared touches boundary."),
    "output_contract": entry_for("roles", "id", "lead_systems_implementer")["output_contract"]
                       + ["A reversible migration plan, its forward and backward steps, and "
                          "the state the database is left in if it stops half way."],
    "escalation": entry_for("roles", "id", "lead_systems_implementer")["escalation"]
                  + ["Escalate to the operator before any migration that drops or rewrites "
                     "existing rows."],
    "stop_criteria": entry_for("roles", "id", "lead_systems_implementer")["stop_criteria"]
                     + ["Never run a migration against a production or shared database."],
}

DATABASE_TASK = {
    "name": "database_migration",
    "summary": "Schema migrations, backfills and the reversibility evidence they need.",
    "candidates": ["atlas-database", "astra-implementer"],
    "role": "database_expert",
    "availability_fallback": True,
}

ATLAS = {
    "id": "atlas-database", "display": "GPT-5.6 Sol (database expert)", "route": "codex",
    "model_ids": {"high": "gpt-5.6-sol"}, "canonical_effort": "high",
    "allowed_efforts": ["high"], "escalated_effort": None, "escalate_at_tier": 3,
    "max_risk_tier": 3, "native_role": "database_expert", "acting_roles": [],
    "primary_task": "database_migration", "also_eligible": [], "review_scope": "none",
    "lineage": "openai-codex",
    "authority_boundary": [
        "Shares the Codex quota and lineage with Astra, Sol, Terra, Luna and Spark.",
        "Holds no review-verdict, merge or lifecycle authority.",
    ],
    "notes": "SYNTHETIC configured expert; added by policy document, not by source.",
}

SUBSCRIPTION_5 = {
    "id": "claude-subscription-5", "route": "claude-code",
    "capacity_key": "claude-subscription-5", "lineage": "anthropic-claude",
    "description": "SYNTHETIC fifth personal Claude subscription.",
    "required_owners": None, "state": "enabled", "priority": 9,
}


def qualified_astra():
    """Astra, re-declared so it is role-qualified for the configured family."""
    astra = entry_for("personas", "id", "astra-implementer")
    astra["acting_roles"] = [*astra["acting_roles"], "database_expert"]
    astra["also_eligible"] = [*astra["also_eligible"], "database_migration"]
    return astra


def expert_document():
    return document(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                    personas=[ATLAS, qualified_astra()])


def fifth_document(state="enabled"):
    return document(accounts=[{**SUBSCRIPTION_5, "state": state}])


def removal_document():
    """Removal names an entry of the policy it is published against, not of the defaults."""
    return document(remove={"accounts": ["claude-subscription-5"]})


def migration_request(**changes):
    values = dict(project=PROJECT, issue=637, task_class="database_migration",
                  touches=("src/main.py",), actor="synthetic-author")
    values.update(changes)
    return TaskRequest(**values)


class ConfiguredExpertTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = from_document(expert_document())
        self.binding = fleet(self.snapshot)

    def test_extra_expert_resolves_with_zero_python_changes(self):
        plan = resolve(migration_request(), self.binding, CONTEXT, NOW)
        self.assertEqual((plan.persona, plan.effective_role, plan.model_id, plan.effort),
                         ("atlas-database", "database_expert", "gpt-5.6-sol", "high"))
        self.assertIn("reversible migration plan", plan.prompt)
        self.assertIn("Database Migration Expert", plan.prompt)
        self.assertEqual(plan.policy_digest, self.snapshot.digest)
        self.assertIn("SYNTHETIC", plan.evidence["source"])

    def test_specialization_keeps_the_whole_base_contract(self):
        role = self.snapshot.role("database_expert")
        inherited = self.snapshot.role("lead_systems_implementer")
        for name in ("output_contract", "escalation", "stop_criteria"):
            self.assertTrue(set(getattr(inherited, name)) <= set(getattr(role, name)))
        self.assertTrue(set(MANDATORY_STOP_CRITERIA) <= set(role.stop_criteria))
        plan = resolve(migration_request(), self.binding, CONTEXT, NOW)
        for line in inherited.output_contract:
            self.assertIn(line, plan.prompt)

    def test_specialization_may_not_thin_the_contract_it_inherits(self):
        for name in ("output_contract", "escalation", "stop_criteria"):
            thin = dict(DATABASE_ROLE)
            thin[name] = list(DATABASE_ROLE[name])[1:]
            with self.subTest(section=name), self.assertRaises(PolicyPrivilegeError):
                from_document(document(roles=[thin], task_classes=[DATABASE_TASK],
                                       personas=[ATLAS, qualified_astra()]))

    def test_ordered_qualified_fallback_applies_every_gate(self):
        # Model-specific entitlement failure, not an account-wide quota failure:
        # the sibling Codex models on this subscription stay usable.
        b = change_probes(self.binding, lambda r: r.model_id == "gpt-5.6-sol",
                          outcome="entitlement_missing")
        plan = resolve(migration_request(), b, CONTEXT, NOW)
        self.assertEqual((plan.preferred_persona, plan.persona, plan.effective_role),
                         ("atlas-database", "astra-implementer", "database_expert"))
        self.assertTrue(plan.acting)
        b = change_probes(b, lambda r: r.route == "codex", outcome="quota_exhausted")
        with self.assertRaises(NoEligibleCandidateError):
            resolve(migration_request(), b, CONTEXT, NOW)

    def test_an_unqualified_candidate_is_refused_not_promoted(self):
        task = {**DATABASE_TASK, "candidates": ["atlas-database", "haiku-triage"]}
        with self.assertRaises(PolicyDocumentError):
            from_document(document(roles=[DATABASE_ROLE], task_classes=[task],
                                   personas=[ATLAS]))

    def test_defaults_are_untouched_by_a_configured_document(self):
        self.assertEqual(len(PERSONAS), 13)
        self.assertEqual(len(default_snapshot().personas), 13)
        self.assertNotIn("atlas-database", default_snapshot().personas)
        self.assertNotEqual(default_snapshot().digest, self.snapshot.digest)
        for family in default_snapshot().task_classes.values():
            self.assertEqual(self.snapshot.task_class(family.name).candidates[:1],
                             family.candidates[:1])


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #

class ConfiguredSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = from_document(fifth_document())
        self.binding = fleet(self.snapshot)

    def busy(self, binding, *account_ids):
        return replace(binding, accounts=tuple(
            replace(a, sessions_in_use=a.max_sessions) if a.account_id in account_ids else a
            for a in binding.accounts))

    def test_fifth_account_is_reached_only_after_the_declared_earlier_ones(self):
        plan = resolve(request("opus-implementer"), self.binding, CONTEXT, NOW)
        self.assertEqual(plan.account_id, "claude-subscription-1")
        busy = self.busy(self.binding, "claude-subscription-1", "claude-subscription-2",
                         "claude-subscription-3")
        plan = resolve(request("opus-implementer"), busy, CONTEXT, NOW)
        self.assertEqual((plan.account_id, plan.capacity_key),
                         ("claude-subscription-5", "claude-subscription-5"))

    def test_draining_then_disabled_then_removed(self):
        for state in ("draining", "disabled"):
            with self.subTest(state=state):
                snapshot = from_document(fifth_document(state))
                busy = self.busy(fleet(snapshot), "claude-subscription-1",
                                 "claude-subscription-2", "claude-subscription-3")
                with self.assertRaises(NoEligibleCandidateError) as caught:
                    resolve(request("opus-implementer"), busy, CONTEXT, NOW)
                reasons = " ".join(s.reason for s in caught.exception.skipped)
                self.assertIn(state, reasons)
                self.assertIn("account-state", {s.code for s in caught.exception.skipped})
                bound = fleet(snapshot).require("claude-subscription-5")
                self.assertEqual(bound.to_dict()["state"], state)
                with self.assertRaises(AccountStateError):
                    bound.require_capacity()
        removed = from_document(removal_document(), base=self.snapshot)
        with self.assertRaises(AccountScopeError):
            removed.account("claude-subscription-5")
        self.assertEqual(len(removed.accounts), len(default_snapshot().accounts))
        # Removal is fail-closed: it names an entry the live policy actually has,
        # so a typo or a doubled removal is refused rather than silently ignored.
        with self.assertRaises(PolicyDocumentError):
            from_document(removal_document(), base=removed)

    def test_draining_refusal_explains_that_running_work_is_untouched(self):
        snapshot = from_document(fifth_document("draining"))
        bound = fleet(snapshot).require("claude-subscription-5")
        with self.assertRaises(AccountStateError) as caught:
            bound.require_capacity()
        self.assertIn("already running", str(caught.exception))

    def test_running_work_stays_tied_to_the_policy_it_was_resolved_against(self):
        busy = self.busy(self.binding, "claude-subscription-1", "claude-subscription-2",
                         "claude-subscription-3")
        plan = resolve(request("opus-implementer"), busy, CONTEXT, NOW)
        removed = from_document(removal_document(), base=self.snapshot)
        self.assertEqual(plan.account_id, "claude-subscription-5")
        self.assertEqual(plan.policy_digest, self.snapshot.digest)
        self.assertNotEqual(plan.policy_digest, removed.digest)
        self.assertIs(verify_payload(plan.to_dict(), expected=plan, now=NOW), plan)
        self.assertTrue(plan.author_history)

    def test_alias_of_one_subscription_is_never_new_capacity(self):
        alias = {**SUBSCRIPTION_5, "id": "claude-subscription-5-alias", "priority": 10}
        snapshot = from_document(document(accounts=[SUBSCRIPTION_5, alias]))
        binding = fleet(snapshot)
        busy = replace(binding, accounts=tuple(
            replace(a, sessions_in_use=a.max_sessions)
            if a.account_id in {"claude-subscription-1", "claude-subscription-2",
                                "claude-subscription-3", "claude-subscription-5"} else a
            for a in binding.accounts))
        with self.assertRaises(NoEligibleCandidateError) as caught:
            resolve(request("opus-implementer"), busy, CONTEXT, NOW)
        self.assertIn("not new capacity", " ".join(s.reason for s in caught.exception.skipped))

    def test_reusing_one_credential_profile_is_refused(self):
        alias = {**SUBSCRIPTION_5, "id": "claude-subscription-5-alias", "priority": 10}
        snapshot = from_document(document(accounts=[SUBSCRIPTION_5, alias]))
        binding = fleet(snapshot)
        first = binding.require("claude-subscription-5")
        with self.assertRaises(AccountScopeError):
            replace(binding, accounts=tuple(
                replace(a, env=first.env) if a.account_id.endswith("-alias") else a
                for a in binding.accounts))

    def test_alias_cannot_escape_the_unum_client_restriction(self):
        alias = {"id": "claude-subscription-4-alias", "route": "claude-code",
                 "capacity_key": "claude-subscription-4", "lineage": "anthropic-claude",
                 "description": "SYNTHETIC alias of the Unum client identity.",
                 "required_owners": None}
        with self.assertRaises(PolicyPrivilegeError):
            from_document(document(accounts=[alias]))
        scoped = {**alias, "required_owners": ["Unum-Inc"],
                  "restriction": "SYNTHETIC alias inherits the Unum client scope."}
        snapshot = from_document(document(accounts=[scoped]))
        with self.assertRaises(AccountScopeError):
            AccountBinding("claude-subscription-4-alias", (PROJECT,),
                           env={"CLAUDE_CONFIG_DIR": "/synthetic/profiles/alias"},
                           snapshot=snapshot)

    def test_no_ordinal_limit_on_configured_subscriptions(self):
        extra = [{**SUBSCRIPTION_5, "id": f"claude-subscription-{n}",
                  "capacity_key": f"claude-subscription-{n}", "priority": n}
                 for n in range(5, 11)]
        snapshot = from_document(document(accounts=extra))
        self.assertEqual(len(snapshot.accounts_for_route("claude-code")), 10)
        self.assertEqual(len(fleet(snapshot).accounts_for("claude-code")), 10)


# --------------------------------------------------------------------------- #
# Models, harnesses and vendor lineage
# --------------------------------------------------------------------------- #

GROK_LOW = {"route": "cursor", "id": "cursor-grok-4.6-low", "vendor": "cursor-fleet",
            "effort_selection": "explicit"}

SONNET_VIA_ANTIGRAVITY = {"route": "antigravity", "id": "claude-sonnet-4-6",
                          "vendor": "anthropic-claude", "effort_selection": "none"}


def cursor_low_persona():
    grok = entry_for("personas", "id", "grok-frontend")
    return {**grok, "id": "grok-triage", "display": "Cursor Grok 4.6 (low)",
            "model_ids": {"low": "cursor-grok-4.6-low"}, "canonical_effort": "low",
            "allowed_efforts": ["low"], "escalated_effort": None,
            "primary_task": "rapid_ui_fix", "also_eligible": []}


def antigravity_claude_persona(vendor="anthropic-claude"):
    pro = entry_for("personas", "id", "pro-design")
    return {**pro, "id": "sonnet-via-antigravity", "display": "Claude Sonnet 4.6 via Antigravity",
            "vendor": vendor, "model_ids": {"default": "claude-sonnet-4-6"},
            "canonical_effort": "default", "allowed_efforts": ["default"],
            "native_role": "qa_automation", "acting_roles": ["code_reviewer"],
            "review_scope": "high_risk", "max_risk_tier": 3,
            "primary_task": "qa_verification", "also_eligible": ["code_review"],
            "required_modalities": ["text"]}


class ConfiguredModelTests(unittest.TestCase):
    def test_model_on_a_supported_surface_is_configuration_plus_evidence(self):
        task = {**entry_for("task_classes", "name", "rapid_ui_fix"),
                "candidates": ["grok-triage", "composer-fixer"]}
        composer = entry_for("personas", "id", "composer-fixer")
        snapshot = from_document(document(models=[GROK_LOW], task_classes=[task],
                                          personas=[cursor_low_persona(), composer]))
        plan = resolve(request("grok-triage", policy=snapshot), fleet(snapshot), CONTEXT, NOW)
        self.assertEqual((plan.model_id, plan.effort), ("cursor-grok-4.6-low", "low"))
        self.assertEqual(plan.argv[plan.argv.index("--model") + 1], "cursor-grok-4.6-low")
        self.assertNotIn("--effort", plan.argv)
        b = change_probes(fleet(snapshot), lambda r: r.model_id == "cursor-grok-4.6-low",
                          outcome="entitlement_missing")
        with self.assertRaises(NoEligibleCandidateError):
            resolve(request("grok-triage", policy=snapshot), b, CONTEXT, NOW)

    def test_a_model_absent_from_the_recorded_catalog_never_becomes_usable(self):
        for rule in ({"route": "cursor", "id": "grok-9-unreleased", "vendor": "cursor-fleet"},
                     {"route": "cursor", "id": "auto", "vendor": "cursor-fleet"},
                     {"route": "cursor", "id": "composer-2.5-fast", "vendor": "cursor-fleet"},
                     {"route": "codex", "id": "gpt-reserve", "vendor": "openai-codex"},
                     {"route": "codex", "id": "codex-auto-review", "vendor": "openai-codex"}):
            with self.subTest(model=rule["id"]), self.assertRaises(PersonaPolicyError):
                from_document(document(models=[rule]))

    def test_an_unsupported_harness_stays_reviewed_adapter_code(self):
        with self.assertRaises(PersonaPolicyError):
            from_document(document(models=[{"route": "aider", "id": "gpt-6-astra",
                                            "vendor": "openai-codex"}]))
        alien = {**ATLAS, "route": "aider"}
        with self.assertRaises(PersonaPolicyError):
            from_document(document(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                                   personas=[alien, qualified_astra()]))

    def test_effort_selection_is_data_not_a_source_enum(self):
        wrong = {**GROK_LOW, "effort_selection": "none"}
        with self.assertRaises(PersonaPolicyError):
            from_document(document(models=[wrong],
                                   task_classes=[{**entry_for("task_classes", "name",
                                                              "rapid_ui_fix"),
                                                  "candidates": ["grok-triage"]}],
                                   personas=[cursor_low_persona()]))
        for selection in ("ultra", "", "flag"):
            with self.subTest(selection=selection), self.assertRaises(PolicyDocumentError):
                from_document(document(models=[{**GROK_LOW, "effort_selection": selection}]))

    def test_a_vendor_model_reached_through_another_fleet_keeps_its_authorship(self):
        review = {**entry_for("task_classes", "name", "code_review"),
                  "candidates": ["sonnet-reviewer", "opus-implementer", "astra-implementer",
                                 "sonnet-via-antigravity"]}
        qa = {**entry_for("task_classes", "name", "qa_verification"),
              "candidates": ["flash-qa", "astra-implementer", "sonnet-via-antigravity"]}
        snapshot = from_document(document(
            models=[SONNET_VIA_ANTIGRAVITY], task_classes=[review, qa],
            personas=[antigravity_claude_persona()]))
        person = snapshot.persona("sonnet-via-antigravity")
        self.assertEqual((person.lineage, person.author_vendor),
                         ("google-antigravity", "anthropic-claude"))
        claude_author = AuthorIdentity("opus-implementer", "claude-subscription-1",
                                       "claude-author", snapshot=snapshot)
        a = assignment("sonnet-via-antigravity", policy=snapshot, authors=(claude_author,),
                       authority="google-antigravity",
                       reviewer_account="antigravity-account")
        with self.assertRaises(ReviewIndependenceError):
            validate_assignment(a, HEAD, current_assignment=a, snapshot=snapshot)
        codex_author = AuthorIdentity("astra-implementer", "openai-codex", "codex-author",
                                      snapshot=snapshot)
        ok = replace(a, authors=(codex_author,))
        self.assertIs(validate_assignment(ok, HEAD, current_assignment=ok, snapshot=snapshot), ok)

    def test_a_document_cannot_relabel_model_authorship(self):
        for vendor in ("google-antigravity", "independent-fleet"):
            with self.subTest(vendor=vendor), self.assertRaises(PolicyPrivilegeError):
                from_document(document(
                    models=[SONNET_VIA_ANTIGRAVITY],
                    task_classes=[{**entry_for("task_classes", "name", "qa_verification"),
                                   "candidates": ["flash-qa", "sonnet-via-antigravity"]}],
                    personas=[antigravity_claude_persona(vendor)]))


# --------------------------------------------------------------------------- #
# Fail-closed documents and kernel invariants
# --------------------------------------------------------------------------- #

class InvalidDocumentTests(unittest.TestCase):
    def bad(self, error=PolicyDocumentError, **sections):
        with self.assertRaises(error):
            from_document(document(**sections))

    def test_envelope_is_refused_unless_it_is_exactly_right(self):
        for raw in ({}, {"schema": "aru.personas.policy/v2", "version": "1"},
                    {"schema": POLICY_SCHEMA}, {"schema": POLICY_SCHEMA, "version": ""},
                    {"schema": POLICY_SCHEMA, "version": "1", "base": "production"},
                    {"schema": POLICY_SCHEMA, "version": "1", "experts": []},
                    {"schema": POLICY_SCHEMA, "version": "1", "personas": {}}):
            with self.subTest(raw=raw), self.assertRaises(PolicyDocumentError):
                from_document(raw)

    def test_configuration_can_never_carry_code_or_authority(self):
        for section, payload in (
                ("personas", [{**ATLAS, "command": ["/bin/sh", "-c", "id"]}]),
                ("personas", [{**ATLAS, "env": {"OPENAI_API_KEY": "x"}}]),
                ("accounts", [{**SUBSCRIPTION_5, "credentials": "/tmp/token"}]),
                ("accounts", [{**SUBSCRIPTION_5, "purchase": True}]),
                ("roles", [{**DATABASE_ROLE, "review_authority": "self"}])):
            with self.subTest(section=section), self.assertRaises(PolicyPrivilegeError):
                from_document(document(**{section: payload}))

    def test_malformed_references_and_duplicate_identities(self):
        self.bad(personas=[{**ATLAS, "native_role": "nonexistent_role"}])
        self.bad(personas=[{**ATLAS, "primary_task": "nonexistent_family"}])
        self.bad(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                 personas=[{**ATLAS, "model_ids": {"high": "gpt-9-imaginary"}},
                           qualified_astra()])
        self.bad(roles=[DATABASE_ROLE],
                 task_classes=[{**DATABASE_TASK,
                                "candidates": ["atlas-database", "atlas-database"]}],
                 personas=[ATLAS])
        self.bad(roles=[DATABASE_ROLE],
                 task_classes=[{**DATABASE_TASK, "candidates": ["atlas-database"],
                                "override_candidates": ["atlas-database"]}],
                 personas=[ATLAS])
        self.bad(roles=[{**DATABASE_ROLE, "base": "database_expert"}])
        with self.assertRaises(PolicyDocumentError):
            from_document(document(remove={"personas": ["never-existed"]}))

    def test_incompatible_effort_role_and_risk_are_refused(self):
        self.bad(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                 personas=[{**ATLAS, "canonical_effort": "low"}, qualified_astra()])
        self.bad(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                 personas=[{**ATLAS, "allowed_efforts": ["high", "xhigh"]}, qualified_astra()])
        self.bad(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                 personas=[{**ATLAS, "max_risk_tier": 9}, qualified_astra()])
        self.bad(error=UnsupportedEffortError, roles=[DATABASE_ROLE],
                 task_classes=[DATABASE_TASK],
                 personas=[{**ATLAS, "model_ids": {"max": "gpt-5.6-sol"},
                            "allowed_efforts": ["max"], "canonical_effort": "max"},
                           qualified_astra()])

    def test_configuration_cannot_grant_review_authority(self):
        with self.assertRaises(PolicyPrivilegeError):
            from_document(document(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                                   personas=[{**ATLAS, "review_scope": "high_risk"},
                                             qualified_astra()]))
        with self.assertRaises(PolicyPrivilegeError):
            from_document(document(personas=[{**entry_for("personas", "id", "sonnet-reviewer"),
                                              "max_risk_tier": 3}]))
        with self.assertRaises(PolicyPrivilegeError):
            from_document(document(task_classes=[
                {**entry_for("task_classes", "name", "code_review"),
                 "availability_fallback": True}]))
        thin = entry_for("roles", "id", "code_reviewer")
        thin["stop_criteria"] = list(MANDATORY_STOP_CRITERIA)
        with self.assertRaises(PolicyPrivilegeError):
            from_document(document(roles=[thin]))

    def test_the_approved_architect_chain_stays_an_exact_prefix(self):
        chain = entry_for("task_classes", "name", "architecture_decision")
        for candidates in (["astra-implementer", "fable-architect", "opus-implementer"],
                           ["fable-architect", "opus-implementer"],
                           ["opus-implementer", "astra-implementer", "fable-architect"]):
            with self.subTest(candidates=candidates), self.assertRaises(PolicyPrivilegeError):
                from_document(document(task_classes=[{**chain, "candidates": candidates}]))
        extended = from_document(document(task_classes=[
            {**chain, "candidates": [*chain["candidates"], "sol-implementer"]},
            {**entry_for("task_classes", "name", "bounded_implementation")}],
            personas=[{**entry_for("personas", "id", "sol-implementer"),
                       "acting_roles": ["maintenance_engineer", "chief_architect"],
                       "also_eligible": ["maintenance_fix", "architecture_decision"]}]))
        self.assertEqual(extended.task_class("architecture_decision").candidates[:3],
                         default_snapshot().task_class("architecture_decision").candidates)

    def test_a_document_cannot_lower_a_declared_risk_tier(self):
        snapshot = from_document(expert_document())
        binding = fleet(snapshot)
        with self.assertRaises(RiskEvidenceError):
            resolve(migration_request(risk_tier=0, touches=("src/auth.py",)),
                    binding, CONTEXT, NOW)


# --------------------------------------------------------------------------- #
# Snapshots, isolation, publication
# --------------------------------------------------------------------------- #

class SnapshotLifecycleTests(unittest.TestCase):
    def test_the_default_document_round_trips_to_the_same_digest(self):
        rebuilt = from_document(default_snapshot().to_document())
        self.assertEqual(rebuilt.digest, default_snapshot().digest)
        self.assertEqual(len(rebuilt.personas), 13)

    def test_two_snapshots_never_leak_into_each_other(self):
        first = from_document(expert_document())
        second = from_document(fifth_document())
        self.assertNotIn("atlas-database", second.personas)
        self.assertNotIn("claude-subscription-5", first.accounts)
        self.assertNotIn("atlas-database", PERSONAS)
        with self.assertRaises(UnknownTaskError):
            second.task_class("database_migration")
        with self.assertRaises(AccountScopeError):
            AccountBinding("claude-subscription-5", (PROJECT,),
                           env={"CLAUDE_CONFIG_DIR": "/synthetic/profiles/five"},
                           snapshot=first)

    def test_one_binding_reads_exactly_one_snapshot(self):
        first, second = from_document(expert_document()), from_document(fifth_document())
        binding = fleet(first)
        stranger = fleet(second).require("claude-subscription-1")
        with self.assertRaises(AccountScopeError):
            replace(binding, accounts=(stranger, *binding.accounts[1:]))

    def test_preview_explains_the_change_without_publishing_it(self):
        report = preview(expert_document())
        self.assertEqual(report["personas"]["added"], ["atlas-database"])
        self.assertEqual(report["personas"]["changed"], ["astra-implementer"])
        self.assertEqual(report["roles"]["added"], ["database_expert"])
        self.assertEqual(report["task_classes"]["added"], ["database_migration"])
        self.assertFalse(report["publishes"])
        self.assertEqual(report["base_digest"], default_snapshot().digest)
        removal = preview(document(remove={"personas": ["spark-pair"],
                                           "task_classes": ["interactive_pair_edit"]}))
        self.assertEqual(removal["personas"]["removed"], ["spark-pair"])
        self.assertEqual(len(default_snapshot().personas), 13)

    def test_publication_is_atomic_and_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "fleet-policy.json"
            first = publish(fifth_document(), live)
            self.assertEqual(load_policy_document(live).digest, first)
            second = publish(fifth_document("draining"), live)
            self.assertNotEqual(first, second)
            with self.assertRaises(PolicyDocumentError):
                publish(document(personas=[{**ATLAS, "native_role": "nope"}]), live)
            self.assertEqual(load_policy_document(live).digest, second)
            self.assertEqual(publish(fifth_document(), live), first)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["fleet-policy.json"])

    def test_a_snapshot_is_immutable_and_self_describing(self):
        snapshot = from_document(expert_document())
        summary = snapshot.summary()
        self.assertEqual(summary["schema"], POLICY_SCHEMA)
        self.assertEqual(summary["digest"], snapshot.digest)
        self.assertEqual(summary["version"], "1.1.0-synthetic")
        with self.assertRaises(Exception):
            snapshot.personas["atlas-database"] = None  # type: ignore[index]
        self.assertTrue(KERNEL_INVARIANTS)


# --------------------------------------------------------------------------- #
# The disposable end-to-end proof, through the real CLI
# --------------------------------------------------------------------------- #

class DisposableConfigurationTests(unittest.TestCase):
    def cli(self, *args, expect=0):
        result = subprocess.run([sys.executable, "-m", "integrations.personas", *args],
                                capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_add_resolve_then_remove_an_expert_and_a_subscription_without_source_edits(self):
        combined = document(roles=[DATABASE_ROLE], task_classes=[DATABASE_TASK],
                            personas=[ATLAS, qualified_astra()], accounts=[SUBSCRIPTION_5])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(combined))

            report = self.cli("policy-preview", "--policy", str(policy_path))["preview"]
            self.assertEqual(report["personas"]["added"], ["atlas-database"])
            self.assertEqual(report["accounts"]["added"], ["claude-subscription-5"])

            live = root / "live-policy.json"
            published = self.cli("policy-publish", "--policy", str(policy_path),
                                 "--to", str(live))
            self.assertIn("rollback", published)

            listed = self.cli("list", "--policy", str(live))
            self.assertEqual(listed["policy"]["digest"], published["digest"])
            self.assertIn("atlas-database", {p["id"] for p in listed["personas"]})
            self.assertEqual(listed["policy"]["account_states"]["claude-subscription-5"],
                             "enabled")

            checked = self.cli("validate", "--policy", str(live))
            self.assertTrue(checked["valid"])
            self.assertEqual(checked["personas"], 14)
            self.assertTrue(checked["kernel_invariants"])

            packet_path = root / "packet.json"
            raw = packet("atlas-database", policy=combined)
            raw["request"]["task_class"] = "database_migration"
            raw["request"]["touches"] = ["src/main.py"]
            packet_path.write_text(json.dumps(raw))
            explained = self.cli("explain", "--input", str(packet_path),
                                 "--at", NOW.isoformat())
            self.assertEqual(explained["selected"], "atlas-database")
            self.assertFalse(explained["execution_authority"])
            self.assertEqual(explained["plan"]["effective_role"], "database_expert")

            # Rollback is republishing the previous document; here that is the
            # shipped baseline, which removes the expert and the subscription.
            (root / "rollback.json").write_text(json.dumps(document()))
            after = self.cli("policy-publish", "--policy", str(root / "rollback.json"),
                             "--to", str(live))
            rolled_back = self.cli("validate", "--policy", str(live))
            self.assertEqual(rolled_back["personas"], 13)
            self.assertNotIn("claude-subscription-5",
                             rolled_back["policy"]["account_states"])
            self.assertNotEqual(after["digest"], published["digest"])

    def test_every_shipped_default_still_resolves_and_the_defaults_are_intact(self):
        listed = self.cli("list")
        self.assertEqual(len(listed["personas"]), 13)
        self.assertEqual(self.cli("validate")["personas"], 13)
        exported = self.cli("policy-export")["document"]
        self.assertEqual(exported["schema"], POLICY_SCHEMA)
        self.assertEqual(len(exported["personas"]), 13)
        self.assertEqual(from_document(exported).digest, default_snapshot().digest)

    def test_the_documented_boundary_is_actually_written_down(self):
        readme = (ROOT / "integrations/personas/README.md").read_text(encoding="utf-8")
        for token in (POLICY_SCHEMA, "policy-publish", "policy-preview", "policy-export",
                      "draining", "rollback"):
            with self.subTest(token=token):
                self.assertIn(token, readme)
        for line in KERNEL_INVARIANTS:
            self.assertTrue(line.strip().endswith("."))
