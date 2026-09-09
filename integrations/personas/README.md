# Approved fleet personas — source interface v1

This stdlib Python 3.11+ package implements issue #630 for the external Driver.
It resolves trusted task metadata into an exact command plan; it never launches
a provider, spends credits, authenticates an account, acquires a lock, selects
PR authority, or changes GitHub lifecycle state. Wiring is deferred to #631.

Run from the repository root; no installation or provider calls are needed:

```sh
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m integrations.personas list
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m integrations.personas validate
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m integrations.personas explain \
  --input integrations/personas/examples/architecture-fallback.json \
  --at 2026-09-09T18:00:00+00:00
```

The example is **synthetic test data, not live access proof**. `explain` emits
`execution_authority: false`, even when it can compile a plan. Without `--at`,
evidence expires against the actual clock. Exit 0 means valid/selected; exit 2
means refused or malformed input. CLI output never substitutes for kernel
verification. `list` includes every role's scope, output contract, authority
boundary, escalation and stop criteria.

| Stable persona | Default task | Approved efforts (default first) |
| --- | --- | --- |
| `fable-architect` | architecture_decision | high, xhigh |
| `opus-implementer` | complex_implementation | high, xhigh |
| `sonnet-reviewer` | code_review | high |
| `haiku-triage` | triage_documentation | default (no flag) |
| `astra-implementer` | security_implementation | high, xhigh |
| `sol-implementer` | bounded_implementation | high |
| `terra-maintainer` | maintenance_fix | medium, high |
| `luna-scout` | repository_scout | low, medium |
| `spark-pair` | interactive_pair_edit | high; optional |
| `grok-frontend` | frontend_implementation | high, medium |
| `composer-fixer` | rapid_ui_fix | default (no flag) |
| `flash-qa` | qa_verification | medium, high |
| `pro-design` | design_evidence_analysis | high; image capability required |

The exact model IDs and route flag surfaces are in `registry.py`, `catalog.py`
and `data/route-catalogs.json`. Claude uses `--effort`, Codex uses
`-c model_reasoning_effort=VALUE`, and Cursor/Antigravity encode effort in the
model ID. Haiku and Composer emit no effort choice. No aliases, `auto`, legacy
models, fast billing variants, max/ultra or undocumented effort translation
are assigned. CLI support and catalog visibility do not prove usable access.

## Deterministic policy

A request names an approved task family explicitly or through a trusted
`aru-task:` label. These labels are **consumer input vocabulary**, not new
kernel labels or lifecycle authority. Missing, contradictory or unknown tasks
are refused. Titles and free prose never classify work. Nonempty `touches`
are required. The local `risk.py` snapshot matches the current kernel path
classifier; a caller-supplied risk tier can increase but cannot lower that
floor. The Driver still supplies the live kernel's actual-diff tier. Unknown
safe paths rise to Tier 2; unsafe paths are refused.

Architecture always tries **Fable → Astra → Opus**, in declared account order
within each candidate. Each performs the full Chief Architect contract:
decision record, alternatives, invariants, decomposition, implementation
contracts and escalation. High is normal even at Tier 3; xhigh requires a
major unresolved decision and exact route support. A model assignment to
Astra or Opus is compatible with this explicitly approved acting role.

Other fallback lists and their effective roles are explicit registry data.
Every candidate passes role, risk, effort, project, account, capacity and
capability gates. Every skip records persona, account, code, kind and reason.
Unavailable, busy, unproven, forbidden-project and insufficient-modality
candidates are skipped without retries or provider calls. All unusable
candidates produce a structured refusal. An explicit `persona_override` pins
that persona and does not permit substitution; `model_override` must match the
exact selected variant. Sonnet bounded implementation is an explicit secondary
assignment, not automatic review substitution.

For non-architecture tasks, `nontrivial` selects the persona's documented
escalation; Tier 3 increases frontier effort to xhigh. Sensitive Flash
verification requires high. No sensitive task accepts effort below high.
Scout, triage, maintenance, UI and pair personas cannot perform Tier 2–3 work.
Spark requires both binding enablement and `allow_optional` on the request.

## Public Python interface

```python
from integrations.personas import (
    TaskRequest, PromptContext, FleetBinding, resolve, verify_payload,
    ReviewAssignment, plan_review, AuthorIdentity,
)

# The Driver creates these from trusted live issue/Git/account facts.
request = TaskRequest(
    project="gillella/Aru_Agentic_SDLC", issue=630,
    task_class="architecture_decision", touches=("integrations/personas/",),
    actor="codex-astra-personas",
    author_history=(AuthorIdentity(
        "opus-implementer", "claude-subscription-1", "claude-code-1"),),
    handoff_reason="operator-approved canonical claim transfer",
)
# binding = FleetBinding.load(operator_configuration_path)
# context = PromptContext(worktree, branch=branch, head=full_sha)
# expected = resolve(request, binding, context)  # real clock, while holding capacity lock
# checked = verify_payload(payload_to_dispatch, expected=expected)
# Existing supervisor consumes checked.argv, checked.env and checked.workspace.
```

`resolve(request, binding, context, now=None)` returns `CommandPlan` or a typed
`PersonaPolicyError`. `dry_run` returns the same refusal reasons as data.
`plan_review(assignment, current_head, binding, context, now=None,
current_assignment=fresh_kernel_snapshot)` validates one pre-existing assignment,
pins its exact account, and compiles the reviewer plan. Ordinary `resolve`
refuses review tasks. `validate_assignment` performs the same authority checks
without compiling a command.

Each plan binds project/issue/scope/classification, preferred and selected
persona, effective role, exact model/effort/account/capacity, cumulative author
history, branch/head/context, source digest, probe provenance, argv and prompt.
`verify_payload(payload, expected=fresh_plan)` compares the entire payload,
including recomputed hashes, against a separately resolved trusted plan. Plans
expire after at most 60 seconds or when the selected probe expires, whichever
comes first; future-dated and expired plans refuse verification.
SHA-256 is integrity **not authentication**. Do not deserialize a payload into
its own expected plan; do not use a stored expected plan after time or live
state changes. Re-resolve and replace the dispatch payload in that activation.
`lane_template()` is an integration hint, not a complete Driver configuration;
retain cwd/env and the existing Driver's capacity/probe/supervision fields.

## Account and evidence contract

Accounts have explicit literal project allowlists. Subscription 4 accepts only
`Unum-Inc/…`, including at configuration load. Claude accounts require distinct
absolute `CLAUDE_CONFIG_DIR` profiles; Codex requires absolute `CODEX_HOME`.
The compiler uses the canonical `claude` executable plus that environment,
not subscription wrappers that could select another account. All five Codex
models share `openai-codex` quota and lineage; each Claude subscription has one
capacity key, while all Claude subscriptions share author family. Grok and
Composer share Cursor capacity; Flash and Pro share Antigravity capacity.

`ProbeRecord` requires exact account/route/model/effort, authenticated provenance,
source reference, profile `identity_digest`, observed instant and modalities.
The digest binds the account ID plus explicit auth-profile environment. It does
not authenticate the contents of that profile. Only `ok` succeeds. The newest
record wins; conflicting records at the same instant refuse. Future, stale,
unauthenticated, mismatched and failed evidence refuse. A later account-wide
quota/auth failure blocks older successes for sibling models. Fable-specific
credits/entitlement failure does not claim that included Opus access failed.
The freshness window defaults to six hours and can only be shortened.

`AccountBinding.unavailable_reason` and `sessions_in_use` come from the Driver's
bounded authenticated account observations and shared reservation lock. They
are not inferred from a model's display name. No credits/top-ups/paid route are
requested. The package cannot turn missing evidence into a successful probe.

Image work needs input references, proven harness image support and an exact
model/effort probe carrying image capability. Codex passes input files with
`--image`. The recorded agy CLI has no documented local attachment flag: Pro
remains blocked with the default binding. An operator may declare a separately
proven ability to read the named local references, with installation evidence
and a matching image probe. Text-only probes are insufficient. The package
does not invent attachment flags or prove model perception from text output.

## Review and trust boundary

Kernel external-first authority remains unchanged: CodeRabbit is preferred;
retired providers in the historical report do not become eligible. The Driver
must reread the canonical helper's sole assignment and pass it as
`current_assignment`. The library checks exact PR/head, issue/project/scope,
external-first release evidence, persona/risk qualification, assigned account,
and independence from **every** recorded author actor/account/family. Sonnet
handles routine review; only approved independent frontier reviewers can carry
Tier 2–3. Reviewers get read-only/plan mode plus a no-edit output contract.
No persona, QA result, override or architecture record grants merge authority.

Explicitly authorized cross-family continuation appends contributors; it never
replaces the prior family. Architecture fallback has standing authorization;
other cross-family continuation requires a trusted handoff reason. For #630,
**Claude + Codex are both author lineage**, so neither family can authoritatively
review this implementation. Hermes must handle authority after source verification.

Python/JSON inputs are not self-authenticating. A malicious caller could forge
all inputs or bypass this package and invoke a vendor CLI. The Driver owns
GitHub authentication, complete authorship, binary/auth-profile identity,
clean environment construction (no ambient paid API keys), live worktree/head
validation, shared capacity locks, attachment checks and process supervision.
Prompt text and basenames do not provide OS confinement. No runtime enforcement,
independent approval or deployment is claimed by this source package.

See [INTEGRATION.md](INTEGRATION.md) for #631 and [VERIFICATION.md](VERIFICATION.md)
for actual local results. The machine envelope is
[data/request.schema.json](data/request.schema.json); semantic gates are enforced
by the Python API, not by JSON Schema alone.
