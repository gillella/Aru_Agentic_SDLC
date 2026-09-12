# 0002 - Path-derived risk tiers are not Kernel gates

Date: 2026-09-12. Status: accepted.

`scripts/review_risk.py` still derives a tier from changed paths, but no Kernel
gate reads it: the one-approval review rule does not vary by path. The tier
table below is retained for consumers who want to scale their *own* evidence,
and for an external Driver's capacity admission. It is not Aru policy.

Issue #696 removes the unused module and its re-export. This record keeps the
table so that removal loses no information.

## The tier table, as the contract stated it

The Kernel's review rule does not vary by path. `scripts/review_risk.py` still
derives the highest applicable tier from changed paths (an unrecognized but
safe path fails upward to Tier 2; empty, malformed, or unsafe evidence to Tier
3), but no Kernel gate reads it. An external Driver may use it for capacity
admission, and consumers may use it to scale their own evidence:

| Tier | Typical scope | Consumer-owned evidence |
| --- | --- | --- |
| **0 — docs** | Markdown, text, and documentation only | Small documentation checks in `.aru/verify.sh` |
| **1 — ordinary code** | Ordinary source and tests | Focused affected build, lint, and tests |
| **2 — sensitive/contract** | Agent rules, skills, workflows, `.aru/`, hooks, Kernel gate scripts, auth, security, migrations, dependencies, configuration, trading, payments, infrastructure, or unrecognized safe paths | Targeted integration, migration, compatibility, or security evidence |
| **3 — production/destructive** | Deploy, production, destructive, rollback, or revert paths; empty, malformed, or unsafe paths | Human/domain approval, broader release evidence, rollback rehearsal, staged deployment, and observability |

All `scripts/` paths are conservatively classified as sensitive control surfaces.
Documentation-name exceptions apply only to documentation; executable files such
as `README.py` retain a code tier. Production/destructive matches still take precedence.

The Kernel neither requests nor checks the consumer-owned column. Record extra
evidence in the consumer issue, `.aru/verify.sh`, branch rules, or runbook.
Consumers may add stricter parallel checks, approvals, or deployment controls;
the Kernel requires exactly one approval and no serial review rounds.
Deployment is never implied by merge.
