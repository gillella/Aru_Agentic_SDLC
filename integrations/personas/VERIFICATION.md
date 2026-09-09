# Issue #630 / PR #634 source verification

Verified 2026-09-09 by GPT-6 Astra High in the existing isolated branch
`feat/issue-630-feat-integration-define-and-validate-the-1`, continuing from
`276333d952b4e3501396487063ddbbd8e38a5e10`. This report is local author
verification, **not independent review or exact-head server authority**.
The resulting commit is identified in Git history and the final handoff.

## Scope and author lineage

The live issue was reread through the Factory App runner. It is open, In Review,
with the sole claim `agent:codex-astra-personas`. Its corrected declaration is:

```text
touches: integrations/personas/**, tests/test_surface.py, tests/test_persona_policy.py
```

The actual `scripts.touches.parse_touches` and `path_allowed` functions accepted
all 34 paths in the complete PR diff against merge base
`a1221e573607691d84bc644923b371afd1e69fa3`, with zero scope violations.
The local `/tmp/persona-issue-630-current.md` was also read.

Claude Opus subscriptions 1 and 3 began this package; GPT-6 Astra High continued
after their session exhaustion. Author families remain **`anthropic-claude` AND
`openai-codex`**. Neither can authoritatively review this PR. Claim, persona and
account changes do not reset authorship. No authoritative code review has
completed. Reviewer selection and infrastructure remain with Hermes.

## Reproduced failure and focused repair

Exact-head server run `34377920113` failed the tracked-state surface test at the
starting head: it rejected eleven persona JSON files. The same failure was
reproduced locally. The original 49 unittest tests passed independently, but
`pyproject.toml` testpaths omitted the package; the workflow's existing
`python -m pytest -q` therefore did not collect them. This repository's workflow
runs pytest and Ruff directly; this checkout has no `.aru/verify.sh`.

Deleted the generated `VERIFICATION.json` and both real historical observation
files, `evidence/observed-2026-09-09.json` and
`evidence/recorded-2026-09-09.json`. The operator retains copies outside Git.
They were not renamed or converted into another machine-readable ledger.

The existing surface test now calls the explicit eight-file inventory and
semantic validator in `integrations/personas/tests/source_inventory.py`.
It admits only the immutable request schema, route catalog, five synthetic
request examples and their shared synthetic binding. SHA-256 pins bind every
byte, including nested fields; recognized names and synthetic markers alone
cannot admit new execution history. Deliberate static changes require reviewing
both asset semantics and inventory pins. No directory exemption was added.

Six adversarial tests cover the actual surface guard, unknown JSON/JSONL/DB/
SQLite paths (including uppercase extensions), runtime fields inserted into
each allowed file, nested execution history and replacement by live probes.
The two-import `tests/test_persona_policy.py` bridge collects the original test
classes without copying tests or changing workflows/testpaths. Repository test
source remains at its existing 9,000-line limit; no budget was raised.

## Actual local verification

Runtime: `/Users/gillella/.hermes/hermes-agent/venv/bin/python` (Python 3.11).
Commands below use that Python from the repository root.

| Command / check | Current outcome |
| --- | --- |
| `python -m pytest tests/test_surface.py tests/test_persona_policy.py -q -o addopts=` | 66 passed; 255 subtests passed |
| `python -m pytest --collect-only -q -o addopts=` | 1,536 tests collected |
| `python -m pytest integrations/personas/tests --collect-only -q -o addopts=` | All 55 package tests collected; comparing class/method counters proves CI collects each exactly once, including all original 49 and six new adversarial tests |
| `python -m pytest -q` | Passed (exit 0); all 1,536 collected tests; no failures or skips |
| `python -m integrations.personas validate` | Valid; exactly 13 personas; zero archived observations; execution authority false |
| `python -m integrations.personas.tests.check_examples` | All five examples passed; JSON Schema validated with installed jsonschema |
| `python -m compileall -q integrations/personas` | Passed |
| `python -m ruff check tests/test_surface.py tests/test_persona_policy.py integrations/personas/evidence.py integrations/personas/tests/source_inventory.py integrations/personas/tests/test_source_inventory.py` | Passed on every changed Python path |
| `python -m ruff check scripts hooks tests integrations/hermes` | Existing server lint command passed |
| `git diff --cached --check` | Passed |

Actual collection logs and full pytest output are outside the repository at
`/tmp/persona-630-collection-after.txt`, `/tmp/persona-630-package-collection.txt`
and `/tmp/persona-630-full-pytest.log`.

Current policy source digest:
`9b99665620a022465d40e685d5781d7d5e21a0626a66901adfcc65cac10fb09a`.
The digest changed because `evidence.py` now truthfully documents operator-owned
historical observations. No routing behavior or static fixture format changed.

The original suite covers all 13 identities / 20 approved model-effort variants,
risk and effort precedence, Fable → Astra → Opus fallback, account/capacity and
modality gates, mixed authorship and independent review refusal, and plan/argv/
prompt tampering. It exercises real CLI list/validate and explain for all 13
personas plus unsupported-model refusal. The example runner verifies four
successful selections (including Astra performing the architect role) and the
expected exit-2 unsupported-model refusal, always with execution authority false.

## Historical evidence and remaining work

The earlier source-author verification compared all 13 personas / 20 variants
against the operator's recorded catalogs and inspected installed CLI help for
Claude, Codex, Cursor and Antigravity. Those were dated catalog/flag observations,
not proof of current usable capacity. They were not repeated as live capability
probes during this repair. The historical Claude successes and later session
limits remain operator evidence outside Git.

All successful package test/example probes are explicitly synthetic. This
continuation made no provider inference call or installation change. The
package remains source and an offline CLI. #631 owns actual Driver/operator
wiring, installation/cutover, capacity supervision and live enforcement checks;
this repair does not complete that issue. Real multimodal access remains gated
on exact route/account/harness evidence. See `INTEGRATION.md`.

The user reports that the exact Grok High CLI smoke works while the generic
kernel reviewer availability probe refuses; Hermes owns that separate repair.
This author continuation does not select reviewers, modify review labels,
change issue criteria/status, merge, or claim independent approval. A push
requires fresh exact-head server verification and independent review before
any later governed merge.
