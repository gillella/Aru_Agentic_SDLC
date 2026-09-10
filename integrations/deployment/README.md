# Consumer-owned deployment

Aru Code Factory's Kernel stops at confirmed merge. A consumer can use this
guide to make the next step explicit in its existing delivery platform, without
adding a Factory scheduler, credentials service or second lifecycle database.

1. Build an immutable artifact from a known merged commit. Record both identities;
   rebuilding a mutable tag is not the same artifact. Keep secrets in the consumer's
   deployment platform and out of issue bodies, commits and agent output.
2. Select the exact environment and existing deployment action. Define who may
   approve it and whether the consumer permits automatic low-risk promotion.
   Production approval binds artifact, environment and an expiry or deployment
   window. A new artifact or target requires new authorization.
3. Run the consumer's required preflight, migration compatibility and rollback
   checks. Missing approval, failed preflight or unknown target stops deployment.
4. Use the existing platform to deploy. Record its deployment/run identifier and
   result. An API accepting a request is not successful deployment.
5. Read back the installed artifact/version and perform actual readiness and smoke
   checks against the target environment. Set the consumer's delivery outcome only
   from that evidence. Kernel issue Done continues to mean the governed source
   change is merged, not that an application is running.
6. On failure, use the previously approved rollback path to a recorded known-good
   artifact. Verify restoration. Stop and escalate if rollback fails or ownership
   is unknown. Preserve logs and work; do not replay destructive migrations blindly.

Copy [the evidence template](../../templates/deployment-evidence.md) into the
consumer's existing change record or runbook. Start with one real consumer and
its deployment platform. The template records evidence; it does not itself enforce
approval or execute deployment. Implement enforcement in that platform's existing
environment rules and action permissions. A terminal command and a checked box
must never be presented as runtime proof by themselves.

For a first pilot, demonstrate successful staging deployment, deliberate preflight
refusal, changed-artifact approval rejection, a failed health check, and rollback
verification. Then consider production under the consumer's actual risk policy.
Platform-specific commands require a chosen target, artifact store and runtime;
this generic integration intentionally supplies no guessed deployment endpoint.

Jira integration is future work. If needed, preserve one authoritative work tracker
per project and keep its stable identity/revision/status/dependencies interface
separate from GitHub PR/commit/CI evidence. Do not mirror two writable lifecycle
authorities and try to resolve disagreements after a mutation.
