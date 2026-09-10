# PR #639 bounded author remediation

Review: [n1's formal changes request](https://github.com/gillella/Aru_Agentic_SDLC/pull/639#pullrequestreview-5161892013)
at `a712add06f7618ff17d87c7b51e9de010285dce9`. Scope is only
`integrations/hermes/**`; the reviewer remains independent.

## 1. Scratch body creation: evidence-backed disagreement and explicit guidance

The installed Claude 2.1.266 binary at
`/opt/homebrew/Caskroom/claude-code@latest/2.1.266/claude` has SHA-256
`553d1b9e9e7068b275c0a783c7e139ff6503096f286e674c8c919379fb0eca62`.
Its embedded JavaScript contradicts the premise that Edit cannot create a file:

- Edit's `validateInput` reads the target, treats ENOENT as `U=null`, and accepts
  the missing target with `if(U===null){if(y==="")return{result:!0}`. Here `y`
  is `old_string`. This occurs before the existing-file read-state checks.
- Edit's `checkPermissions` calls `uA(iS,...)`; this evaluates `edit` rules.
  The compiler already emits the exact body-path Edit rule for each role.
- Edit's `Sio` execution calls `fx` with `createParents:D===""`, where `D` is
  `old_string`. Its `kio` reader maps a missing file to empty content and
  `fileExists:false`, so the existing-file stale-read check is skipped.
- The edit transformer `$Qe` uses the supplied `new_string` as the complete
  content when `old_string` is empty. `Sio` passes that result to `QZ`, which
  writes it and verifies its on-disk byte count.

A local source probe extracted the installed `validateInput`, `Esn`/`$Qe`, and
`QZ` functions without changing their bodies. Node executed them with temporary
author/reviewer body paths and `old_string:""`; filesystem calls used Node's
local filesystem API. Unrelated session, permission-denial and telemetry
dependencies were stubs. Both missing-file validations returned `{result:true}`,
and both resulting files matched their supplied body content exactly:

```text
author: missing-file validation and Edit transform/write PASS
reviewer: missing-file validation and Edit transform/write PASS
```

This proves the installed tool's creation semantics, not live account or CLI
permission routing. The probe did not launch Claude, Driver, or a productive
worker. Permission compilation tests cover the exact scratch-path grant in both
roles and the explicit creation instruction. The prompt now tells workers to
create with Edit and empty `old_string`; no broader grant was added. The
[official permission documentation](https://code.claude.com/docs/en/permissions)
describes path-scoped rules and `dontAsk`; installed harness enforcement still
needs the separately authorized operator probe.

## 2. Stop before child execution: code fix

Three new tests failed on the reviewed implementation: Stop before worker
execution, Stop followed by Start before an old supervisor runs, and Stop during
revalidation. Each replaced the true Stop reason with `result_unavailable` and
created a permanent retry blocker despite no child running.

Finalization now observes structured output only if a child was created. It
still closes output, persists the exit receipt, and releases the reservation.
All three tests now prove the old child stays fenced, the same claim resumes
once after a synthetic Start, and repeated reconciles/heartbeats do not launch
duplicates. The denial regression also covers Stop after child creation, when
the genuine denial must remain blocking.

## 3. Receipt/result text amplification: code fix

Four new tests failed with large `result`, `errors`, `subtype`, and denial input
fields: a roughly 544 KiB field became a receipt of roughly 544 KiB, or more
than 1 MiB when duplicated in the denial reason. Every reconcile rereads those
receipts. This is a correctness concern despite its low review severity.

Result observations now omit bulky details above 8 KiB and use a short reason
pointing to the receipt's private `result_path`. Result-derived reasons are
limited to 2 KiB. Full accepted denial evidence remains in the result log;
completed logs exceeding 8 MiB retain only the first 8 MiB and remain failed.
This does not impose a streaming disk quota or rewrite historical receipts.
All four regression cases pass, as does the completed-log bound test.

## 4. Receipt argv trust: documented existing boundary

The snapshot fingerprint detects config drift. It does not authenticate an argv
stored beside that fingerprint. `WORKER-PERMISSIONS.md` now explicitly identifies
the private state directory and local operator account as the trust boundary.
No authentication system or additional scheduler was introduced.

## Verification and handoff

The seven initial defect probes failed before the code fixes and passed after
them. The focused permissions, execution, controller, config, and state suites
then passed all 205 cases. Ruff passed for the changed Python paths. No full
repository suite was rerun. Exact-new-head GitHub CI and the distinct reviewer's
delta verdict remain required; source preflight is not merge authority.

No runtime install/configuration, live preflight, productive worker artifact,
Driver activation, #637 change, merge, release, or deployment was performed.
All live acceptance gates remain unchecked. Logs containing model/tool inputs
stay local; published evidence contains synthetic data and source facts only.
