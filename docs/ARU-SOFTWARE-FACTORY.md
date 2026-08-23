# Aru Software Factory — Build Plan

> **Current review policy (Issue #373):** CodeRabbit is the sole PR code-review
> authority. Coding-agent `reviewer:` / `reviewed-by:` workflows described in
> historical sections below are retired and cannot satisfy the merge gate.
> Claude, Codex, Cursor, and Antigravity implement, remediate, and mechanically
> merge only through the governed `merge_pr.py` helper after CodeRabbit
> completes a substantive exact-head review and every enforced gate passes;
> they never review.

**Goal:** idea → deployed software through an autonomous governed loop, with
operator visibility and exceptional human intervention only for an unresolved
severe merge or close-out failure.

**Status:** canonical narrative companion to [roadmap epic #335](https://github.com/gillella/Aru_Agentic_SDLC/issues/335). The epic and governed Project Board are the live authority for phases, item state, and delivery order. Dated S0–S5 material below is preserved as historical evidence, not current work.

**Companion:** [`PROCESS-AUDIT-2026-08.md`](PROCESS-AUDIT-2026-08.md) — the baseline gap analysis.

**Strategy briefing:** [`agentic-software-factory-briefing.html`](agentic-software-factory-briefing.html) is a self-contained, tool-neutral tour of the factory thesis, operating path, maturity, and trust model. [`AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md`](AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md) provides the supporting industry evidence (OpenAI harness, BCG factory, Spec Kit, Mastra, CodeRabbit). Both explain this plan without creating another roadmap. Board choice: [`PROJECT-BOARD-FOR-AGENTIC-FACTORY.md`](PROJECT-BOARD-FOR-AGENTIC-FACTORY.md).

**Interactive flow:** [`../sdlc_flow_visualizer/index.html`](../sdlc_flow_visualizer/index.html) is a no-build explorer of lifecycle and remediation mechanics. Issue #342 is the Current status-legend correction; until it merges, the visualizer may lag this document and must not be used as live status authority.

---

## Lifecycle status vocabulary

These terms are normative across the README and both canonical factory guides.
The Project Board, not a prose snapshot, determines an issue's live state.

| Status | Meaning |
|---|---|
| **Shipped** | Present in the repository with linked implementation evidence. |
| **Current** | The live roadmap slice represented by open board work; consult the board for item state. |
| **Deferred** | Intentionally sequenced after an unmet phase entry gate; not available now. |
| **Blocked** | Cannot start or finish until an explicit dependency or operator decision is satisfied. |
| **Historical** | Dated evidence about an earlier state; never a current capability claim. |
| **Audit-only** | Records governance evidence but does not prove a runnable artifact or environment. |

Freshness remains an extension of `scripts/check_docs.py` (issue #115) and the
spec/code synchronization gate (issue #242). Focused status contract tests add
regression examples; they do not create another documentation authority.

## 1. What is settled and must not be re-litigated

Carried forward from the source document, verified and agreed:

- **The board is the orchestrator.** Agents ask `fetch_next_work.py` what to do next. No supervisor process, no MCP lock bus, no `tasks.md`. This survives crashed sessions and reboots in a way in-memory orchestration does not.
- **Mechanisms over instructions.** A rule that lives only in prose is followed probabilistically.
- **The middle of the factory is strong.** Claim → isolate → implement → review → merge is genuinely better engineered than most public multi-agent setups.
- **The base ends now exist, but the proof is incomplete.** Intake capabilities
  and a GitHub Pages preview are Shipped. Clarification/convergence and real
  provider delivery/observation remain Deferred behind #335's phase gates.
- **Non-goals stand** (§7 below extends them).

---

## 2. Historical baseline corrections — snapshot 2026-08-15

Three claims in the source document are stale or wrong. They matter because two of them would misdirect P0 work. The first has since been fixed in flight — it is kept here with its status marked, because the shape of the near-miss is worth more than the ticket.

### 2.1 F0.1's first revision would have landed a broken fix — since corrected

The source lists "land in-flight review/stamp fixes (#19, #24, #27, #25)" as P0. PR #25's **first revision** rested on a false premise and introduced a security hole in the only merge gate. It has since been reworked; this section is kept because the failure shape is instructive, not because the work is outstanding.

Issue #24 originally asserted that nothing writes `reviewed-by:`. Something does: `prompts/fleet-worker.md:104`, step 6 of the documented review workflow. The two labels are not a mismatch — they are two distinct states that the first revision collapsed into one:

| label | meaning | lifetime |
|---|---|---|
| `reviewer:<id>` | this agent has **claimed** the PR | transient, released at step 6 |
| `reviewed-by:<id>` | this agent **has reviewed** | terminal, the attribution |

Accepting `reviewer:` as proof of review means: author leaves a same-account `COMMENTED` review, any peer merely *claims* the PR, and the gate passes before that peer has read a line.

**The real defect is narrower and more interesting:** the reviewing agent claimed but never performed step 6, because step 6 lived in a prompt and supplied a command only for the release that followed it. This is Principle 1 failing inside the review workflow itself. The correct fix is to make the completion stamp mechanical, not to merge the two states.

**Status:** done in `6e07316`. `complete_review` now attributes and releases in one command, refuses a non-holder and refuses the author, and `check_reviews` still requires `reviewed-by:` and explicitly does not accept `reviewer:`. The exploit above is now a regression test.

**Standing lesson:** the review that caught this read the diff against the *claim in the PR body* rather than against the gate's actual behaviour. Both halves of a two-state protocol have to be checked against the state machine, not against the narrative.

### 2.2 F0.3 understates the dogfooding gap — **corrected 2026-08-15**

The source said "align this playbook's CI with the hardened template." At the time of the original audit the gap was larger than alignment. **Verified 2026-08-15** against `.github/workflows/ci.yml`: this playbook now dogfoods the gates it ships.

| | this repo's `ci.yml` (2026-08-15) | what it ships to new projects |
|---|---|---|
| lint | `ruff check .`, blocking | `ruff check .`, blocking |
| secrets | gitleaks-action | gitleaks over full history |
| deps | `pip-audit` | `pip-audit` |
| boundaries | import-linter when configured | import-linter when configured |
| tests | unittest suite (fails when source lacks tests) | fails when source exists without tests |

Historical note: before that alignment, defects were found by humans/Codex rather than CI. Do not re-open S0.6 as if the dogfooding gap still exists.

### 2.3 "Recently hardened" overstated the review layer — **historical; fixed**

At the snapshot, `author:`/`reviewer:` stamping was written but opt-in and
therefore absent in practice: `create_pr.py --agent` defaulted to empty, so
PR #18 was opened through the sanctioned path with no labels. The shipped helper
now requires the author identity and stamps the PR mechanically (#19 / PR #27).

### 2.4 The `create_pr.py` call-site survey, corrected

S0.3 originally named three stale call sites. Re-checked against the branch, the tally is:

| site | state |
|---|---|
| `AGENTS.md:52` | fixed in PR #27's original commit |
| `skills/implement-next-issue/SKILL.md:121` | **was broken** — the master directive routes every feature and bug task through this skill, so the canonical workflow would have exited 2 at the open-PR step. Fixed in `45eae46`. |
| `hooks/pre-push:33` | **was broken** — the refusal message printed on a direct push to `main` handed the operator a `create_pr.py` command with no `--agent`. Fixed in `2b55cfc`. |
| `scripts/fetch_next_work.py:102` | stale docstring, not a breakage. Fixed in `45eae46`. |
| `prompts/fleet-worker.md:145` | already correct; S0.3 was wrong to list it |

**Two method notes, both of which cost a defect here.**

The first survey of these sites grepped `*.md`, `*.py` and `*.sh` and therefore never opened `hooks/pre-push`, which is extensionless. An executable-file survey filtered by extension will silently skip hooks, which is precisely the class of file that hands instructions to a blocked operator.

More generally: a required flag is an API break, and the blast radius is every place the command is *written down*, not just every place it is *called*. Prose, help text, hook output and skill files are all call sites when the reader is an agent or an operator following instructions literally.

---

## 3. Historical working-session findings — snapshot 2026-08-15

Eight findings the source document predates. Each is evidenced, not inferred.

### 3.1 Squash merges were deleting history — **historical finding; fixed**

**Session evidence (pre-fix):** every PR merged with `--squash`. **46 branch commits had collapsed into 6 commits on `main`.** PR #8's branch narrative — implement → harden → test → address review → refactor — became one line on `main`.

Three costs were already paid under squash:

1. **Audit loss.** Issue #14's delivery had to be reconstructed forensically from test names.
2. **Conflict cascades.** Squash commits carry no ancestry across parallel branches (#8, #16).
3. **No rollback grid.** No addressable known-good state between successive PRs.

**Current behaviour (verified 2026-08-15):** `merge_pr.py` defaults to `--merge-method merge` (PR #157 / `82abd26`). Squash remains available as an explicit override, not the default. Checkpoint tags (`ckpt/*`) and SemVer release tags now provide the rollback grid (see appendix §9).

### 3.2 Consumers tracked `main` live, with no way to pin — **historical; pinning shipped**

Consuming repos bind by symlink into a live checkout:

```
~/.cursor/skills/code-review -> /Users/.../Aru_Agentic_SDLC/skills/code-review
```

**Session evidence (pre-fix):** zero tags, zero releases, no pin, consumers silently followed `main`. PR #27 (`create_pr.py --agent` required) was the breaking-change example that made pinning urgent.

**Current behaviour (verified 2026-08-15):** annotated `ckpt/*` tags on merge (PR #140), SemVer `v0.x` release tags (PR #155), and `ARU_SDLC_REF` honouring in the installer (PR #158 / `docs/cursor-integration.md`) give consumers an escape from live `main`. See appendix §9.

### 3.3 Review is the binding constraint, and the source marks it BUILT

Empirically, in one session: **every single PR reviewed had at least one P1 finding.** Not style notes — a data-loss bug that would `rm -rf` a user's customised skill directory, a gate narrower than the runner it gated, a fix that traded false positives for false negatives.

Two structural problems the source doesn't name:

- **One GitHub account.** Every agent authenticates identically, so the gate cannot tell reviewer from author except by labels agents must remember to apply. This is why #24 exists at all.
- **Review is the only stage with no mechanical assist.** Everything else has a script. Review has a checklist in a skill file.

### 3.4 The recurring defect shape: a check narrower than the reality it checks

Every substantive bug this session was the same mistake:

| check | narrower than |
|---|---|
| redirect scan | shell quoting rules |
| test-discovery glob | pytest's `python_files` defaults |
| Jest test glob | Jest's `testMatch` defaults |
| merge gate label read | the labels the framework writes |
| `_git_write_to_protected` | quoting — unresolved at this snapshot |

The enforcement layer's design is sound. What keeps breaking is the gap between what a gate believes and what the system actually does. **This deserves to be a named review heuristic**, because it is predictive: for every new gate, ask *what does the thing I am gating actually accept, and am I narrower?*

### 3.5 `merge_pr.py` abandoned close-out after a successful merge — **historical; fixed**

Merging #23: the merge landed, then `gh pr merge --delete-branch` failed to delete a local branch held by a worktree, and `merge_pr.py` treated that as total failure — skipping worktree pruning and the move to Done. Both were completed by hand.

The ordering was the bug: `merge_pr.py` had its own `prune_worktree` that
handled exactly this, but it ran *after* `gh pr merge` had already tried and
failed. PR #55 shipped idempotent, recoverable close-out; this paragraph is the
dated failure evidence, not current behavior.

### 3.6 Untrusted input was unexamined — **historical; baseline boundary shipped**

Agents read issue bodies, PR descriptions, and review comments, then act on them. `touches:` and `depends-on:` are parsed from issue text and drive what an agent is permitted to write.

At the snapshot the board was authored by trusted agents, so the risk appeared
theoretical. The current trust boundary is defined in
[`coding_standards.md`](coding_standards.md#trust-boundary-for-issue-pr-and-review-text);
Phase 2 (#338) is Deferred work for optional-capability admission, not evidence
that the baseline boundary is still absent.

### 3.7 No degraded mode — **historical; policy shipped**

GitHub is the coordinator, work queue, state store, and gate. The current
[`degraded-mode.md`](degraded-mode.md) policy makes outage behavior explicit:
coordination stops gracefully rather than inventing a fallback queue or
bypassing authority.

### 3.8 The factory had no unit economics — **historical; baseline telemetry shipped**

At the snapshot there was no measure of tokens, wall time, or review rounds per
issue. Issues #106–#108 shipped dwell, rework, cycle-time, available cost
evidence, and the `fleet_status.py` operator view. Missing producer evidence is
reported as unavailable rather than converted into a false cost claim.

---

## 4. The operator visibility and intervention interface

The factory proceeds autonomously through planning, CodeRabbit review, agent
remediation, and mechanical merge. Money, PII, security, schema, migration,
diff size, and review-round count increase the evidence required but do not
create human review or merge gates. Product intent still belongs in issue
acceptance criteria; if a required decision is absent, agents record the
options and keep the requirement blocked rather than inventing it.

### 4.1 The one mandatory human intervention

Human intervention is required only when a severe merge conflict or merge/
close-out failure remains unsafe or impossible for agents to resolve through
governed remediation. The escalation must state the evidence, attempted fixes,
preserved artifacts, and exact action needed. Everything else stays in the
agent implementation/remediation/mechanical-merge loop.

### 4.2 What the operator must be able to see in one screen

**Shipped:** `fleet_status.py` and `factory_metrics.py` implement the first
operator screen (#106–#108). They expose:

- Ready depth vs fleet size — *is the factory starved?*
- Who holds what, and for how long — *is anything stuck?*
- PRs awaiting review, by age — *is the constraint binding?*
- Review rounds per PR — *is quality degrading?*
- CI failure rate — *are gates working or noise?*
- Available cost and wall-time evidence per closed issue — *what evidence is
  present, and what is still unavailable?*

### 4.3 The operating principle

> **The issue defines direction. CodeRabbit reviews. The gated helper
> merges. Human intervention is the last resort for an unresolved severe merge
> or close-out failure.**

Every proposed automation should be tested against it. There is no separate
coding-agent review lane: implementation/remediation agents fix findings, and
CodeRabbit is the reviewer of record. A risk label, large diff, repeated
review, or tool preference is not a human gate.

---

## 5. Roadmap

### 5.1 Current governed roadmap

[Roadmap epic #335](https://github.com/gillella/Aru_Agentic_SDLC/issues/335)
is the approved program container. Phase epics are planning containers, not
claimable stories; their entry and exit gates express order. The board is the
only authority for a child's live Backlog, Ready, In Progress, In Review, or
Done state.

| Phase | Status | Outcome | Authority |
|---|---|---|---|
| 0 | **Current** | Restore a trustworthy baseline and prove one real external deployment | #336 |
| 1 | **Deferred** | Define and enforce the governed kernel boundary after Phase 0 evidence | #337 |
| 2 | **Deferred** | Specify and enforce capability admission after Phase 1 and an approved PRD | #338 |
| 3 | **Deferred** | Compose clarification and convergence stations after the kernel/admission gates | #339 |
| 4 | **Deferred** | Generalize real delivery, observation, rollback, and learning from provider proof | #84 |
| 5 | **Deferred** | Prove, measure, and release the isolated multi-project factory | #186 |

Phase 0's exact dependency graph and exit gate live in #336. The durable policy
is focused, issue-declared verification for each story, followed by the complete
Python 3.11 suite on the exact phase-exit commit and again before release.
Issue #341 is the Current implementation of that verification contract; issue #357 is
the Current fail-closed release-checkpoint enforcement. Until their PRs merge,
their presence on the roadmap is not a Shipped claim.

The Shipped merge contract is unchanged: `merge_pr.py` defaults to a merge
commit, while squash is an explicit opt-in (#89).

#### Deployment truth

- **Shipped:** `scripts/deploy_preview.py` can produce a runnable GitHub Pages
  preview tied to an exact merged commit and smoke evidence.
- **Audit-only:** `scripts/promote.py` records GitHub Environment/Deployment
  state. It does not deploy, copy, rebuild, or prove movement of a runnable
  artifact.
- **Deferred:** issue #345 must prove the first Vercel adapter with immutable
  deployment identity, authoritative preview and production URLs, live smoke,
  promotion without rebuild, and rollback. Phase 4 (#84) generalizes only from
  that real evidence.

### 5.2 Historical S-roadmap snapshot — verified 2026-08-15

Everything below through §6 is a dated predecessor schedule retained as
evidence. Its “Done,” “Now,” “Weeks,” and dependency cells describe the
2026-08-15 snapshot and must not be used to select current work. #335 and the
Project Board supersede it.

#### Historical Phase 0 — Make the spine trustworthy (days)

Gate correctness first. **Verified 2026-08-15:** the review gate no longer falls open for bot/`[bot]` accounts (`is_advisory_review_account` in `scripts/merge_pr.py`); active `reviewer:` claims block shortcuts; resolved threads need a post-finding commit or an explicit `Withdrawn:` reply; reviews must be at current head (#50, #72, #26).

| ID | Work | Rationale | Depends on |
|---|---|---|---|
| **S0.1** | ~~Rewrite #24; rework PR #25 to keep claim and completion distinct~~ | **Done** in `6e07316` — see §2.1 | — |
| **S0.2** | ~~Make the `reviewed-by:` stamp mechanical, not a prompt step~~ | **Done** in `6e07316`: `complete_review` replaces the prose step | S0.1 |
| **S0.3** | ~~Fix #27's stale `create_pr.py` call sites~~ | **Done** in `45eae46` + `2b55cfc` — see §2.4 | — |
| **S0.4** | Fix `_git_write_to_protected` quoting (#28) | Blocks read-only work on `main`; same class as #21 | — |
| **S0.5** | ~~Fix `merge_pr.py` close-out ordering~~ | **Done** in PR #55 (`c68abf2`) — close-out is idempotent and recoverable | — |
| **S0.6** | ~~Align this repo's CI with the template it ships (#F0.3)~~ | **Done** — playbook CI runs ruff, gitleaks, pip-audit, import-linter, tests (verified 2026-08-15; see §2.2) | — |
| **S0.7** | GitHub Pro + rulesets: no direct push to `main`, required status checks. **Required-approval is deliberately excluded** — see below | Free plan confirmed: no server-side protection exists | purchase |
| **S0.7a** | Separate reviewer identity (GitHub App or second account) before any required-approval rule | Without it, required approval deadlocks every fleet PR | S0.7 |
| **S0.8** | Process ownership + merge authority doc (#7) | Agents receive contradictory process instructions | — |

**Why S0.7 stops short of required approval.** §3.3 establishes that every agent authenticates as the same GitHub account. GitHub refuses to let an account approve or request changes on its own pull request — reproduced on PR #27: `Review Can not request changes on your own pull request`. A ruleset requiring an approving review would therefore deadlock **every** fleet-authored PR, turning a hardening step into a full stop.

The ordering constraint is real and easy to get backwards: server-side approval enforcement is only available once reviewer identity is separable from author identity. Until S0.7a lands, the label-based gate in `merge_pr.py` stays the review authority, and the ruleset covers only direct-push protection and required status checks — both of which work fine under a single identity.

#### Historical Phase 1 — History, versioning, and the ability to roll back (days)

Prerequisite for drift control. **Core history/pinning rows have shipped** (verified 2026-08-15); remaining work is attribution polish and operator docs.

| ID | Work | Rationale | Depends on |
|---|---|---|---|
| **S1.1** | ~~Switch `merge_pr.py` default from `squash` to `merge`~~ | **Done** in PR #157 (`82abd26`) — default is `merge`; squash is opt-in | S0.5 |
| **S1.2** | ~~Annotated `ckpt/*` tag on every merge~~ | **Done** in PR #140 (`54086f3`) | S1.1 |
| **S1.3** | ~~SemVer `v0.x` release tags; MAJOR on consumed-CLI break~~ | **Done** in PR #155 (`c8defb9`) | S1.2 |
| **S1.4** | ~~Consumer pinning: `ARU_SDLC_REF`~~ | **Done** in PR #158 (`6af5efc`); see `docs/cursor-integration.md` | S1.3 |
| **S1.5** | `Agent: <id>` commit trailer | Per-commit attribution; today only the PR is stamped | S1.1 |
| **S1.6** | ~~Documented revert path + `revert_merge.py`~~ | **Done** in PR #166 (`7ebf667`); helper listed in `AGENTS.md` | S1.2 |

#### Historical Phase 2 — Close the review constraint (1–2 weeks)

The binding constraint, per §3.3.

| ID | Work | Rationale |
|---|---|---|
| **S2.1** | Evidence trail: verification commands and exit codes captured into the PR body automatically | "Tests pass" without evidence is an assertion |
| **S2.2** | Acceptance-criteria runner: parse issue checkboxes → verification commands → merge input | Turns self-attested checkboxes into a real gate |
| **S2.3** | Codify the narrower-than-reality heuristic in `code-review/SKILL.md` with today's five cases | Predictive of the defect class that actually occurs |
| **S2.4** | Surface narrowly scoped unresolved merge/close-out intervention with its evidence and requested action | Routine review rounds must stay in the agent loop |
| **S2.5** | Split guidance in triage for oversized scopes | Prevents the review death spiral upstream |

#### Historical Phase 3 — Front of factory: intent → Ready work (1–2 weeks)

| ID | Work | Rationale |
|---|---|---|
| **S3.1** | `idea-to-prd` skill — thin wrapper over existing `grill-aru` interrogation flow, do not rebuild | Factory should start at intent, not at shaped issues |
| **S3.2** | `prd-to-issues` — epic → phased issues with `depends-on` DAG, `touches:`, acceptance criteria | Epics become claimable work |
| **S3.3** | Plan gate as mechanism: `type:feat` / `needs-design` requires a plan artifact before `create_branch.py` succeeds | Today it lives only in `fleet-worker.md` |
| **S3.4** | Ready-depth SLO + nag when Ready < fleet size | An idle fleet is a triage failure |
| **S3.5** | Board attachment on issue create (#20) | Orphan issues are invisible to the coordinator |

#### Historical Phase 4 — Back of factory: ship and observe (2–4 weeks)

| ID | Work | Rationale |
|---|---|---|
| **S4.1** | `fleet_status.py` — the §4.2 screen | You cannot steer what you cannot see |
| **S4.2** | Cost and cycle-time capture per closed issue | The input to every future prioritisation |
| **S4.3** | `deploy-preview` skill, gated post-merge | Idea→merge is half a factory |
| **S4.4** | Promotion path preview → staging → prod, with issue/PR trail | Issue-First survives past merge |
| **S4.5** | Smoke / E2E in CI for products with a runnable surface | Unit green ≠ product works |
| **S4.6** | Post-merge cleanup automation | Done must mean clean |

#### Historical Phase 5 — Productization

| ID | Work |
|---|---|
| **S5.1** | Stack packs: **deployment and release templates** per stack. `init_project.py` already accepts `node`/`nodejs`/`typescript`/`react`/`go`, picks the test runner, and renders per-stack CI (`init_project.py:312-337`, covered in `tests/test_init_project.py:148-195`). The gap is after CI, not at init. |
| **S5.2** | Trust boundary for untrusted issue/PR text (§3.6) — required before any repo accepts external issues |
| **S5.3** | Degraded-mode decision (§3.7) — document the dependency or mitigate it |
| **S5.4** | Golden-path demo repo exercising the full loop including deploy. Companion and walk: `docs/golden-path-demo.md`. |
| **S5.5** | Merge-queue view wrapping `fleet_status` + `merge_pr --dry-run` |

---

## 6. Historical sequencing snapshot — verified 2026-08-15

```
Done       S0.1-S0.3, S0.5-S0.6, S1.1-S1.4, S1.6  gate + history spine (verified 2026-08-15)
Now        S0.4, S0.7-S0.8, S1.5             remaining spine polish
Weeks 1-2  S2.*       review constraint
Weeks 2-3  S3.*       intake
Weeks 3-6  S4.*       deploy + telemetry
Later      S5.*       productization
```

**Historical ordering argument:** Phase 1 before Phase 2 because you cannot
diagnose a review problem in a history you have deleted. Phase 2 before Phase 3
because feeding more work into the constraint makes the queue worse, not
better. Phase 3 before Phase 4 because deploying work of unverified intent
ships the wrong thing faster. This explains the predecessor plan; it does not
override #335.

---

## 7. Non-goals

Carried forward, all still correct:

- Supervisor / orchestrator agent — the board coordinates
- MCP coordination server with file locking — duplicates `touches:`
- Replacing GitHub Issues with a local task file
- More lifecycle skills for coverage's sake
- Risk-category human review or merge gates; high-risk work strengthens
  evidence without changing authority

Added:

- **Do not automate the intent gate.** §4.3: direction is the one thing the factory must not decide.
- **Do not add a manually-bumped version file.** Derive versions from tags; a hand-maintained one drifts within a week.
- **Do not tag every commit.** The merge boundary is the meaningful checkpoint; per-commit tags are noise.
- **Do not build a second review path.** A CI reviewer was explicitly rejected (#12/#15) in favour of review-as-claimable-work. Issue #6's original AC4/AC5 contradicted it and were struck.

---

## 8. How to use this document

- Select current work only from #335's governed phase issues and the Project
  Board. The **S\*** rows are Historical evidence, not an issue queue.
- Keep one issue per bounded mechanism — one hook, script check, CI job, or thin
  vertical slice — never a mega “improve factory” issue.
- Preserve dated shipped rows in an appendix; historical evidence is valuable
  only when it cannot be mistaken for live status.
- Re-audit against §3 quarterly. The findings there are evidence from one session; some will age out and new ones will appear.
- **When adding any gate, apply §3.4:** what does the thing I am gating actually accept, and am I narrower than it?
- **Statements about current code behaviour should carry a verification date** so aged claims are visible.

---

## 9. Historical appendix — shipped roadmap rows verified 2026-08-15

Moved here per §8 instead of deleting. Commit SHAs are commits on `main` that landed the work. Only entries with two parents are Git merge commits (for example `6af5efc`, `7ebf667`); single-parent SHAs such as `c68abf2`, `82abd26`, `54086f3`, and `c8defb9` are ordinary commits from the historical squash era, not merge commits.

| ID | Shipped as | Evidence |
|---|---|---|
| S0.1 / S0.2 | Review claim vs `reviewed-by:` completion split | `6e07316` |
| S0.3 | Required `--agent` call-site sweep | `45eae46`, `2b55cfc` |
| S0.5 | Idempotent / resumable merge close-out | PR #55 `c68abf2` |
| S0.6 | Dogfood CI (ruff, gitleaks, pip-audit, import-linter, tests) | `.github/workflows/ci.yml` (aligned with shipped template) |
| S1.1 | Default merge method `merge` (not squash) | PR #157 `82abd26` |
| S1.2 | Annotated `ckpt/*` tags on merge | PR #140 `54086f3` |
| S1.3 | SemVer release tags | PR #155 `c8defb9` |
| S1.4 | `ARU_SDLC_REF` consumer pinning | PR #158 `6af5efc` |
| S1.6 | Governed revert helper | PR #166 `7ebf667` |

**Review-gate hardening (issue table in #114):** bot/`[bot]` exclusion, live `reviewer:` claim blocking, thread resolution evidence, and head-SHA review requirements — closed via #50, #72, #26 (and related merge-gate work). Do not document the pre-fix “falls open for bot reviews” behaviour as current.
