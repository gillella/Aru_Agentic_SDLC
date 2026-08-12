# Aru Software Factory — Build Plan

**Goal:** idea → deployed software through an autonomous governed loop, with
operator visibility and exceptional human intervention only for an unresolved
severe merge or close-out failure.

**Status:** supersedes [`Aru-Software-Factory-Future-Improvements.md`](Aru-Software-Factory-Future-Improvements.md) as the working plan. That document's factory model, coordination insight, and non-goals hold and are carried forward. This one corrects its stale claims, adds what a full working session surfaced, and sequences the work.

**Companion:** [`PROCESS-AUDIT-2026-08.md`](PROCESS-AUDIT-2026-08.md) — the baseline gap analysis.

**Strategy briefing:** [`AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md`](AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md) — industry research (OpenAI harness, BCG factory, Spec Kit, Mastra, CodeRabbit) mapped onto this build plan. Board choice: [`PROJECT-BOARD-FOR-AGENTIC-FACTORY.md`](PROJECT-BOARD-FOR-AGENTIC-FACTORY.md).

---

## 1. What is settled and must not be re-litigated

Carried forward from the source document, verified and agreed:

- **The board is the orchestrator.** Agents ask `fetch_next_work.py` what to do next. No supervisor process, no MCP lock bus, no `tasks.md`. This survives crashed sessions and reboots in a way in-memory orchestration does not.
- **Mechanisms over instructions.** A rule that lives only in prose is followed probabilistically.
- **The middle of the factory is strong.** Claim → isolate → implement → review → merge is genuinely better engineered than most public multi-agent setups.
- **The work is at the two ends.** Front: idea → trustworthy Ready work. Back: merge → deploy → observe → learn.
- **Non-goals stand** (§7 below extends them).

---

## 2. Corrections to the baseline

Three claims in the source document are stale or wrong. They matter because two of them would misdirect P0 work. The first has since been fixed in flight — it is kept here with its status marked, because the shape of the near-miss is worth more than the ticket.

### 2.1 F0.1's first revision would have landed a broken fix — since corrected

The source lists "land in-flight review/stamp fixes (#19, #24, #27, #25)" as P0. PR #25's **first revision** rested on a false premise and introduced a security hole in the only merge gate. It has since been reworked; this section is kept because the failure shape is instructive, not because the work is outstanding.

#24 originally asserted that nothing writes `reviewed-by:`. Something does: `prompts/fleet-worker.md:104`, step 6 of the documented review workflow. The two labels are not a mismatch — they are two distinct states that the first revision collapsed into one:

| label | meaning | lifetime |
|---|---|---|
| `reviewer:<id>` | this agent has **claimed** the PR | transient, released at step 6 |
| `reviewed-by:<id>` | this agent **has reviewed** | terminal, the attribution |

Accepting `reviewer:` as proof of review means: author leaves a same-account `COMMENTED` review, any peer merely *claims* the PR, and the gate passes before that peer has read a line.

**The real defect is narrower and more interesting:** the reviewing agent claimed but never performed step 6, because step 6 lived in a prompt and supplied a command only for the release that followed it. This is Principle 1 failing inside the review workflow itself. The correct fix is to make the completion stamp mechanical, not to merge the two states.

**Status:** done in `6e07316`. `complete_review` now attributes and releases in one command, refuses a non-holder and refuses the author, and `check_reviews` still requires `reviewed-by:` and explicitly does not accept `reviewer:`. The exploit above is now a regression test.

**Standing lesson:** the review that caught this read the diff against the *claim in the PR body* rather than against the gate's actual behaviour. Both halves of a two-state protocol have to be checked against the state machine, not against the narrative.

### 2.2 F0.3 understates the dogfooding gap

The source says "align this playbook's CI with the hardened template." Verified, the gap is larger than alignment:

| | this repo's `ci.yml` | what it ships to new projects |
|---|---|---|
| lint | none | `ruff check .`, blocking |
| secrets | none | gitleaks over full history |
| deps | none | `pip-audit` |
| boundaries | none | import-linter when configured |
| tests | `if [ -d tests ]` | fails when source exists without tests |

The factory currently ships five gates it does not run on itself. Every defect found today was found by a human or by Codex, not by CI.

### 2.3 "Recently hardened" overstates the review layer

`author:`/`reviewer:` stamping is listed as hardened. It is written but **opt-in and therefore absent in practice** — `create_pr.py --agent` defaulted to empty, so PR #18 was opened through the sanctioned path with no labels at all. A stamp nothing enforces is not hardening. (#19 / PR #27 fixes the cause.)

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

## 3. What a full working session added

Eight findings the source document predates. Each is evidenced, not inferred.

### 3.1 Squash merges are deleting 87% of the history — **not in the source doc at all**

Every PR merges with `--squash`. **46 branch commits have collapsed into 6 commits on `main`.**

PR #8's branch narrative — implement → make concurrent-safe → harden → test → address review → refactor — is one line on `main`. That sequence is precisely the signal you need to detect an agent drifting.

Three costs, all already paid:

1. **Audit loss.** Issue #14's delivery had to be reconstructed forensically from test names, because the squash hid the commit that did it.
2. **Conflict cascades.** A squash commit carries no ancestry, so parallel branches off shared files conflict on merge. This forced hand-resolution on #8 and #16 and consumed a meaningful share of the session.
3. **No rollback grid.** There is no addressable known-good state between "this PR" and "the one before."

This is foundational to drift control and belongs before tagging, not after.

### 3.2 Consumers track `main` live, with no way to pin

Consuming repos bind by symlink into a live checkout:

```
~/.cursor/skills/code-review -> /Users/.../Aru_Agentic_SDLC/skills/code-review
```

Zero tags, zero releases, no version string, no CHANGELOG. Every consumer silently follows whatever `main` is at that instant.

**A breaking change is in flight right now.** PR #27 makes `create_pr.py --agent` required. On merge, every consumer invoking it without the flag breaks, with no signal and no way to stay on the old behaviour. The source document has versioning at **P4** — the lowest priority. Given live coupling plus in-flight breakage, it belongs at **P1**.

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
| `_git_write_to_protected` | quoting — still broken today |

The enforcement layer's design is sound. What keeps breaking is the gap between what a gate believes and what the system actually does. **This deserves to be a named review heuristic**, because it is predictive: for every new gate, ask *what does the thing I am gating actually accept, and am I narrower?*

### 3.5 `merge_pr.py` abandons its close-out after a successful merge

Merging #23: the merge landed, then `gh pr merge --delete-branch` failed to delete a local branch held by a worktree, and `merge_pr.py` treated that as total failure — skipping worktree pruning and the move to Done. Both were completed by hand.

The ordering is the bug: `merge_pr.py` has its own `prune_worktree` that handles exactly this, but it runs *after* `gh pr merge` has already tried and failed. The result contradicts the script's own docstring: *"so the tail of the lifecycle stops depending on someone remembering it."*

### 3.6 Untrusted input is an unexamined execution vector — **absent from the source doc**

Agents read issue bodies, PR descriptions, and review comments, then act on them. `touches:` and `depends-on:` are parsed from issue text and drive what an agent is permitted to write.

Today the board is authored by you and by trusted agents, so the risk is theoretical. It stops being theoretical the moment any governed repo accepts issues from outside, or an agent reads a dependency's changelog, or a PR arrives from a fork. A factory that executes against text it did not author needs a stated trust boundary.

### 3.7 No degraded mode

GitHub is the coordinator, the work queue, the state store, and the gate. An outage stops the factory completely. That may be an acceptable trade — but it should be a decision, not an accident.

### 3.8 The factory has no unit economics

There is no measure of tokens, wall time, or review rounds per issue. Without it there is no way to answer the question that governs a factory: **which stages are worth automating further, and which should stay human?** The source has this at F3.7; it is the input to every future prioritisation decision and deserves to come earlier.

---

## 4. The operator visibility and intervention interface

The factory proceeds autonomously through planning, independent-agent review,
and mechanical merge. Money, PII, security, schema, migration, diff size, and
review-round count increase the evidence required but do not create human
review or merge gates. Product intent still belongs in issue acceptance
criteria; if a required decision is absent, agents record the options and keep
the requirement blocked rather than inventing it.

### 4.1 The one mandatory human intervention

Human intervention is required only when a severe merge conflict or merge/
close-out failure remains unsafe or impossible for agents to resolve through
governed remediation. The escalation must state the evidence, attempted fixes,
preserved artifacts, and exact action needed. Everything else stays in the
agent implementation/review/remediation loop.

### 4.2 What the operator must be able to see in one screen

Not built. This is `fleet_status.py` (source F3.4) and it is the difference between steering and archaeology:

- Ready depth vs fleet size — *is the factory starved?*
- Who holds what, and for how long — *is anything stuck?*
- PRs awaiting review, by age — *is the constraint binding?*
- Review rounds per PR — *is quality degrading?*
- CI failure rate — *are gates working or noise?*
- Cost and wall time per closed issue — *what is this costing?*

### 4.3 The operating principle

> **The issue defines direction. A distinct agent reviews. The gated helper
> merges. Human intervention is the last resort for an unresolved severe merge
> or close-out failure.**

Every proposed automation should be tested against it. A risk label, large
diff, repeated review, or tool preference is not a human gate.

---

## 5. Roadmap

Each row is an issue candidate. Issue-First applies to the factory itself. Ordered by what unblocks what — not by appeal.

### Phase 0 — Make the spine trustworthy (days)

Nothing else matters while the merge gate can fall open.

| ID | Work | Rationale | Depends on |
|---|---|---|---|
| **S0.1** | ~~Rewrite #24; rework PR #25 to keep claim and completion distinct~~ | **Done** in `6e07316` — see §2.1 | — |
| **S0.2** | ~~Make the `reviewed-by:` stamp mechanical, not a prompt step~~ | **Done** in `6e07316`: `complete_review` replaces the prose step | S0.1 |
| **S0.3** | ~~Fix #27's stale `create_pr.py` call sites~~ | **Done** in `45eae46` + `2b55cfc` — see §2.4 | — |
| **S0.4** | Fix `_git_write_to_protected` quoting (#28) | Blocks read-only work on `main`; same class as #21 | — |
| **S0.5** | Fix `merge_pr.py` close-out ordering | Merges silently leave the board stale | — |
| **S0.6** | Align this repo's CI with the template it ships (#F0.3) | The factory does not run its own gates | — |
| **S0.7** | GitHub Pro + rulesets: no direct push to `main`, required status checks. **Required-approval is deliberately excluded** — see below | Free plan confirmed: no server-side protection exists | purchase |
| **S0.7a** | Separate reviewer identity (GitHub App or second account) before any required-approval rule | Without it, required approval deadlocks every fleet PR | S0.7 |
| **S0.8** | Process ownership + merge authority doc (#7) | Agents receive contradictory process instructions | — |

**Why S0.7 stops short of required approval.** §3.3 establishes that every agent authenticates as the same GitHub account. GitHub refuses to let an account approve or request changes on its own pull request — reproduced on PR #27: `Review Can not request changes on your own pull request`. A ruleset requiring an approving review would therefore deadlock **every** fleet-authored PR, turning a hardening step into a full stop.

The ordering constraint is real and easy to get backwards: server-side approval enforcement is only available once reviewer identity is separable from author identity. Until S0.7a lands, the label-based gate in `merge_pr.py` stays the review authority, and the ruleset covers only direct-push protection and required status checks — both of which work fine under a single identity.

### Phase 1 — History, versioning, and the ability to roll back (days)

Prerequisite for drift control. Currently absent entirely.

| ID | Work | Rationale | Depends on |
|---|---|---|---|
| **S1.1** | Switch `merge_pr.py` default from `squash` to `merge` (`gh pr merge --merge`; there is no `--no-ff` flag on `gh`) | Stops deleting 87% of history; restores ancestry; ends conflict cascades | S0.5 |
| **S1.2** | Annotated `ckpt/*` tag on every merge, written by `merge_pr.py`, carrying PR, issues, author, reviewer, gate verdicts, test count, gated SHA | The rollback grid and the audit trail, with nothing to remember | S1.1 |
| **S1.3** | SemVer `v0.x` release tags; MAJOR on consumed-CLI break | #27 is a breaking change shipping with no version signal | S1.2 |
| **S1.4** | Consumer pinning: `ARU_SDLC_REF` honoured by `install_cursor_integration.sh`; scripts warn on major mismatch | Consumers currently track `main` live with no escape | S1.3 |
| **S1.5** | `Agent: <id>` commit trailer | Per-commit attribution; today only the PR is stamped | S1.1 |
| **S1.6** | Documented revert path in `AGENTS.md` + `revert_merge.py` | A factory needs a reverse gear | S1.2 |

### Phase 2 — Close the review constraint (1–2 weeks)

The binding constraint, per §3.3.

| ID | Work | Rationale |
|---|---|---|
| **S2.1** | Evidence trail: verification commands and exit codes captured into the PR body automatically | "Tests pass" without evidence is an assertion |
| **S2.2** | Acceptance-criteria runner: parse issue checkboxes → verification commands → merge input | Turns self-attested checkboxes into a real gate |
| **S2.3** | Codify the narrower-than-reality heuristic in `code-review/SKILL.md` with today's five cases | Predictive of the defect class that actually occurs |
| **S2.4** | Surface narrowly scoped unresolved merge/close-out intervention with its evidence and requested action | Routine review rounds must stay in the agent loop |
| **S2.5** | Split guidance in triage for oversized scopes | Prevents the review death spiral upstream |

### Phase 3 — Front of factory: intent → Ready work (1–2 weeks)

| ID | Work | Rationale |
|---|---|---|
| **S3.1** | `idea-to-prd` skill — thin wrapper over existing `grill-aru` interrogation flow, do not rebuild | Factory should start at intent, not at shaped issues |
| **S3.2** | `prd-to-issues` — epic → phased issues with `depends-on` DAG, `touches:`, acceptance criteria | Epics become claimable work |
| **S3.3** | Plan gate as mechanism: `type:feat` / `needs-design` requires a plan artifact before `create_branch.py` succeeds | Today it lives only in `fleet-worker.md` |
| **S3.4** | Ready-depth SLO + nag when Ready < fleet size | An idle fleet is a triage failure |
| **S3.5** | Board attachment on issue create (#20) | Orphan issues are invisible to the coordinator |

### Phase 4 — Back of factory: ship and observe (2–4 weeks)

| ID | Work | Rationale |
|---|---|---|
| **S4.1** | `fleet_status.py` — the §4.2 screen | You cannot steer what you cannot see |
| **S4.2** | Cost and cycle-time capture per closed issue | The input to every future prioritisation |
| **S4.3** | `deploy-preview` skill, gated post-merge | Idea→merge is half a factory |
| **S4.4** | Promotion path preview → staging → prod, with issue/PR trail | Issue-First survives past merge |
| **S4.5** | Smoke / E2E in CI for products with a runnable surface | Unit green ≠ product works |
| **S4.6** | Post-merge cleanup automation | Done must mean clean |

### Phase 5 — Productization

| ID | Work |
|---|---|
| **S5.1** | Stack packs: **deployment and release templates** per stack. `init_project.py` already accepts `node`/`nodejs`/`typescript`/`react`/`go`, picks the test runner, and renders per-stack CI (`init_project.py:312-337`, covered in `tests/test_init_project.py:148-195`). The gap is after CI, not at init. |
| **S5.2** | Trust boundary for untrusted issue/PR text (§3.6) — required before any repo accepts external issues |
| **S5.3** | Degraded-mode decision (§3.7) — document the dependency or mitigate it |
| **S5.4** | Golden-path demo repo exercising the full loop including deploy |
| **S5.5** | Merge-queue view wrapping `fleet_status` + `merge_pr --dry-run` |

---

## 6. Sequencing

```
Now        S0.1-S0.5  gate correctness      ← nothing ships past a gate that can fall open
Then       S0.6-S0.8  dogfood + protection + one process owner
Days       S1.*       history, tags, versioning, revert
Weeks 1-2  S2.*       review constraint
Weeks 2-3  S3.*       intake
Weeks 3-6  S4.*       deploy + telemetry
Later      S5.*       productization
```

**The ordering argument:** Phase 1 before Phase 2 because you cannot diagnose a review problem in a history you have deleted. Phase 2 before Phase 3 because feeding more work into the constraint makes the queue worse, not better. Phase 3 before Phase 4 because deploying work of unverified intent ships the wrong thing faster.

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

- Each **S\*** row is an issue candidate. One issue per mechanism — one hook, one script check, one CI job — never a mega "improve factory" issue.
- Move shipped rows to an appendix rather than deleting them; the record of what was tried is worth more than a tidy list.
- Re-audit against §3 quarterly. The findings there are evidence from one session; some will age out and new ones will appear.
- **When adding any gate, apply §3.4:** what does the thing I am gating actually accept, and am I narrower than it?
