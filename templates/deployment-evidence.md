# Deployment evidence

Consumer-owned record for the existing deployment platform; no Kernel authority.

| Field | Required value |
| --- | --- |
| Consumer and source change | Repository, merged commit and PR/change link |
| Artifact | Immutable identifier and digest |
| Target | Environment, account/project and service |
| Authorization | Approver or explicit automatic-promotion policy, artifact/target binding, time and expiry |
| Preflight | Required checks, actual results and run links |
| Data changes | Compatibility, backup/recovery and migration approval where applicable |
| Deployment | Existing platform action, run/deployment ID, start/end and final result |
| Runtime readback | Installed artifact/version and observation time |
| Health evidence | Readiness/smoke checks, environment and actual outcomes |
| Rollback | Known-good artifact, approved procedure, trigger and responsible operator |
| Rollback result | Actual restored version and health evidence, if exercised |
| Final outcome | Deployed and verified, refused, failed, rolled back, or blocked |
| Outstanding action | Specific owner, blocker and next authorized step |

Missing evidence stays missing. A merged PR, green CI, started deployment request
or configured URL cannot fill the runtime-readback or health-evidence fields.
