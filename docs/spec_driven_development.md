# Specification-Driven Development & Bi-Directional Synchronization

<!-- claim:sdd-core verification="2026-08-17" -->

## Overview

In **Aru_Agentic_SDLC**, specifications (PRDs, architecture diagrams, CLI/API contracts, and issue predicates) serve as the primary control plane for autonomous coding agents.

When specifications and codebase implementations drift apart, autonomous agents make decisions based on stale assumptions, leading to hallucination cascades and cross-agent merge conflicts.

The **Bi-Directional Spec-Code Synchronization Engine** (`scripts/sync_spec.py`) statically audits documentation and code without executing untrusted scripts, verifying CLI argument contracts and claim annotations on every pull request.

---

## The Spec-Sync Protocol

### 1. AST Contract Extraction

`sync_spec.py` scans `scripts/*.py` to parse Python AST representations of:
- `argparse.ArgumentParser` flag definitions, options, defaults, and requirements.
- Public top-level function names and parameter signatures.

No untrusted or PR-supplied code is ever executed during audit; AST traversal is purely static and deterministic.

### 2. Markdown Claim Annotations

Architectural and specification claims in `docs/` and `skills/` can be annotated with claim tags:

```markdown
<!-- claim:sdd-core verification="2026-08-17" -->
```

- When `--check` runs, claim presence is indexed for traceability.
- When `--update` runs, verified claim timestamps are updated to the current date.

### 3. CLI Command Drift Detection

Markdown documentation frequently quotes concrete CLI commands. For example:

```bash
python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr 123 --dry-run
```

`sync_spec.py` cross-references all documented flags (e.g. `--pr`, `--dry-run`) against the script's AST argument declarations. If a flag is renamed, removed, or invalid, `sync_spec.py` reports a drift violation with file and line numbers.

---

## CLI Usage

```bash
# Run read-only synchronization audit (exit 0 on sync, exit 1 on drift)
python3 "$ARU_SDLC_HOME/scripts/sync_spec.py" --check

# Emit structured JSON report for machine consumption / CI
python3 "$ARU_SDLC_HOME/scripts/sync_spec.py" --json

# Update verified claim timestamps in documentation
python3 "$ARU_SDLC_HOME/scripts/sync_spec.py" --update
```

---

## Definition-of-Done (DoD) Gate Integration

The Definition-of-Done gate in `scripts/merge_pr.py` includes a mandatory `spec-sync` check:
- Every PR is evaluated for specification and CLI drift before merge.
- A PR that introduces undocumented or mismatched CLI flags fails the DoD gate with actionable remediation output.
