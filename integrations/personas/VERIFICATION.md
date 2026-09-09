# Issue #630 source verification

Verified 2026-09-09 in the existing isolated branch
`feat/issue-630-feat-integration-define-and-validate-the-1`, based on
`ebc7664a19ca0a2869b341e16fcbd3b09f8427e9`. Scope: `integrations/personas/` only.
This report is local author verification, **not independent review or merge authority**.
The exact resulting commit is supplied in the final handoff and Git history;
this report does not attempt to embed its own commit hash.

## Truthful author lineage

The package began with Claude Opus workers on subscriptions 1 and 3. Both
stopped after session exhaustion. The operator preserved their partial files
and transferred the sole claim canonically to `codex-astra-personas`, which
completed the source implementation and tests using GPT-6 Astra High.

**Author families: `anthropic-claude` AND `openai-codex`. Neither family may
provide authoritative review for this implementation.** Persona changes,
subscription changes and canonical claim transfer do not reset that history.
No new independent authorship or review approval is claimed.

Live issue #630 was read with the private Factory App wrapper. Its handoff
comments confirm both lineage and canonical release/claim; the live claim label
was `agent:codex-astra-personas`, with `status:in-progress`. Issue checkboxes and
statuses were not changed. The API returned an empty `projectItems` list; this
source handoff makes no assertion about Project Board authority. Hermes must
reread it before a governed PR/lifecycle transition.

Read `AGENTS.md`, `docs/KERNEL-CONTRACT.md`, the live issue, both `/tmp/persona-…`
briefs, the existing package, recorded reports and Driver source. This checkout
has no `.aru/AGENT-WORKFLOW.md`; no substitute authority was invented. The user's
explicit source implementation and push authorization controls this handoff;
PR authority selection, creation and merge remain with Hermes.

## Actual local verification

Runtime: `/Users/gillella/.hermes/hermes-agent/venv/bin/python` (Python 3.11.15).
Commands run from the repository root:

```sh
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m unittest discover -s integrations/personas/tests -t . -v
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m compileall -q integrations/personas
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m integrations.personas validate
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m integrations.personas list
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m integrations.personas.tests.check_examples
git diff --cached --check
```

Results: **49 unit tests passed**, including subtest matrices for all 13 persona
identities and all **20 approved model/effort variants**. Compile checks passed.
Registry validation passed. The tests launched the real source CLI once for
`list`, once for `validate`, and 13 positive `explain` calls plus an unsupported
model refusal. Five shipped examples passed: architecture fallback selects
Astra as Chief Architect; ordinary Astra, Sonnet review and Pro design select
their expected roles; unsupported model exits 2 with no plan. The available
`jsonschema` validator accepted the interface schema and all five expanded
example envelopes. It is optional for example validation, not a package runtime
dependency. The scoped staged diff passed whitespace checks.

| Requirement | Exercised evidence |
| --- | --- |
| Exact fleet and efforts | 13 identities, 20 variants; all native prompts and output sections; unsupported IDs/efforts/fast models refused |
| Deterministic classification | Every task family; labels versus explicit fields; title ignored; missing/contradictory/unknown risk/task/scope; kernel risk snapshot equivalence |
| Availability fallback | Every recognized Fable failure → Astra; Fable + Astra fail → Opus; all fail; occupied/forbidden accounts; missing image capability; unsupported route effort; pinned and explicit acting-model assignments |
| Retained role and authorship | Architect contract preserved; other fallback output contracts preserved; explicit mixed-family continuation appends rather than resets |
| Capability/account gates | Missing/archive/stale/future/mismatched/failed/unauthenticated probes; conflicting observations; later shared-quota failure; profile digest; Unum-only sub4; shared capacity identities |
| Reviewer independence | Current kernel assignment required; exact PR/head/account; no direct review resolution; external release required; high-risk frontier floor; family/account/actor overlap; both mixed author families excluded |
| Compiler integrity | Every CLI effort mechanism and prompt arity; literal shell-like prose; forbidden executables/env; workspace mismatch; ordinary and rehashed payload tampering; source changes; future/expired plans; probe-bounded plan lifetime |
| Source interface | Real list/validate/explain executions, machine schema, synthetic examples, import API and #631 integration instructions |

All successful probes in unit tests and examples are **explicit synthetic
fixtures, not live capability, entitlement or modality proof**. No model
inference or probe was performed during this author continuation. No auth,
credits, installed runtime, kernel scripts or `integrations/hermes` were changed.

## Catalog and CLI evidence

All 13 personas / 20 approved variants were compared programmatically against
`~/.hermes/reports/model-personas-revised/catalogs.json` and the Claude entries
in `complete-role-map.json`; every exact ID matched, and every assigned Codex
effort appeared in `supported_reasoning_levels`. The broad catalogs contain
additional models/efforts that this package deliberately does not assign.

Read-only `claude --help`, `codex exec --help`, `cursor-agent --help` and
`agy --help` were inspected. Version reads confirmed Claude 2.1.266,
Codex CLI 0.153.4 and Cursor 2026.09.08-6caf4ff. The partial implementation's agy
1.1.28 version is recorded metadata; its flag surface was reread here, not a
new version or availability probe. Claude documents high and xhigh literally;
no translation is made. Codex uses the recorded config effort mechanism.
Cursor/Antigravity select catalog effort IDs with no contradictory extra flag.

Historical capability archives remain non-authorizing. The earlier Opus,
Sonnet and Haiku successes are dated observations, not a claim that a Claude
subscription is currently usable. The later operator handoff reports all four
Claude profiles session-limited and an Astra High success; that report was not
converted into fabricated fresh package evidence. Date-only historical Fable
observations explicitly identify midnight timestamps as archive date sentinels.

## Remaining authority and integration work

The package is tested source and an offline CLI. It is not installed or wired
into the Driver, does not enforce raw vendor CLI use, and does not establish
actual multimodal access. The default agy binding remains text-only; Pro needs
separate real harness and exact model image evidence. Inputs remain a trusted
Driver boundary; Python objects, hashes and JSON do not authenticate GitHub,
auth-profile contents, binary identity or lock ownership.

Hermes owns current source verification, independent review selection, governed
PR creation, exact-head server CI and merge. No PR was opened, reviewer assigned,
issue checkbox changed or merge attempted here. #631 owns runtime wiring and
real reservation/supervision integration validation. See `INTEGRATION.md`.
