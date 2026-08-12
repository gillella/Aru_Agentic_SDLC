# Building an Agentic Software Factory

**A research and strategy guide for transforming a 25-year SDLC into an idea→product agent factory**

**Audience:** Factory owner / architect (25 years of software craft)  
**Date:** 2026-08-11  
**Issue:** Closes context for #78  
**Companion artifacts:**
- Working build plan: [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md)
- Board choice deep-dive: [`PROJECT-BOARD-FOR-AGENTIC-FACTORY.md`](PROJECT-BOARD-FOR-AGENTIC-FACTORY.md)
- Landscape and citations: [`AGENTIC-SOFTWARE-FACTORY-GUIDE.md`](AGENTIC-SOFTWARE-FACTORY-GUIDE.md)
- Process baseline: [`PROCESS-AUDIT-2026-08.md`](PROCESS-AUDIT-2026-08.md)

> **Scope note.** An interactive briefing over this material is **not** part of
> this change and is tracked in [#122](../../issues/122). It is referenced by
> issue rather than by filename, because a document that advertises an artifact
> which does not exist is a defect in its own right.
>
> The original plan named a Cursor-specific canvas. #122 reframes it
> tool-neutrally: this repository commits to being vendor-neutral and
> tool-agnostic (`AGENTS.md`, `README.md`), so nothing in `docs/` should require
> a particular editor to read.

---

## 0. One-sentence thesis

> **The competitive advantage is no longer the coding model. It is the harness: durable board state, executable specs, mechanical gates, independent review, and a learning loop — so any capable agent (Claude Code, Codex, Cursor, Antigravity, Copilot) can turn intent into shipped software without inventing a second process.**

That is exactly what you have been reaching for with **Issue-First Law**, GitHub Project as coordinator, and Aru skills/scripts. This guide situates that design in the wider industry, shows what leaders are proving in production, and gives you a concrete path from “agents that code” to “a factory that builds products.”

---

## 1. What you already know — remapped for agents

Your classical loop is still correct. The *actors* and *bottlenecks* change.

| Classical stage (your 25 years) | Agentic factory equivalent | Who owns it | What breaks if weak |
|---|---|---|---|
| 1. Idea | Intent capture / problem statement | Human (product) | Agents invent the wrong product |
| 2. Research & information gathering | Context engineering + domain research agents | Human + research agents | Hallucinated domain assumptions |
| 3. Requirements documents | Spec-driven artifacts (PRD → issues → acceptance criteria) | Human validates; agents draft | “Looks right, wrong intent” PRs |
| 4. Sprint-by-sprint delivery | Claim → isolate → implement → verify → review → merge → deploy | Agents execute; board orchestrates | Chaos at velocity |
| Agile ceremonies | Board status + fleet telemetry (not standups for agents) | Mechanisms | Invisible stuck work |
| Test-first / TDD | Acceptance criteria + local verify + CI as merge inputs | Agents + gates | Theater tests |
| Code as deployment | Preview → staged promote → observe → remediate | Deterministic CD + ops agents | Half a factory (merge ≠ ship) |

**The decisive relocation of human effort** (BCG Platinion, 2026):

- Humans stop being the *writers of every line*.
- Humans become the *authors of intent* and the *designers of the harness*.
- Agents own execution inside hard boundaries.
- Verification becomes the scarce resource — not typing speed.

CodeRabbit’s framing matches your experience: traditional SDLC bottlenecks on coding and reviewer availability; agentic SDLC bottlenecks on **verification, review, and governance**. DORA’s “verification tax” is real: time saved writing is re-spent auditing unless the factory engineers trust.

---

## 2. Industry landscape — how people are actually building this

Five mature patterns keep recurring. They are not mutually exclusive; the best factories combine them.

### 2.1 Pattern A — Harness engineering (OpenAI / Codex)

**Proof point:** ~1M LOC internal product in ~5 months; ~10× speed; **zero manually written application lines**; humans steered, agents executed ([OpenAI: Harness engineering](https://openai.com/index/harness-engineering/)).

**What they treat as the product:** not the model — the environment:

1. **Context engineering** — repo-local truth (`AGENTS.md`, architecture docs, observability). If the agent cannot read it, it does not exist.
2. **Architectural constraints** — mechanically enforced layering, linters, structural tests. Constraints *improve* agent convergence.
3. **Entropy management** — scheduled agents that scan drift and open cleanup PRs (“garbage collection” for AI codebases).
4. **Agent-to-agent review loops** — drive PRs to completion with local + cloud agent reviewers; humans optional over time.

**Implication for you:** Aru’s scripts, hooks, merge gate, and skills *are* harness engineering. Keep investing here before buying another orchestrator.

### 2.2 Pattern B — Spec-driven development (GitHub Spec Kit)

**Proof point:** GitHub Spec Kit (2025–2026) — specify → plan → tasks → implement, with checkpoints between phases ([GitHub Blog](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/)).

**Core idea:** Specs are **living, executable artifacts**, not wiki pages. Vague prompts force models to guess thousands of requirements; structured specs make them literal pair programmers.

Microsoft Azure demos wire Spec Kit → GitHub issues → Copilot coding agent → quality review → Actions deploy → SRE agent — a full idea→ops loop ([Microsoft Tech Community](https://techcommunity.microsoft.com/blog/appsonazureblog/an-ai-led-sdlc-building-an-end-to-end-agentic-software-development-lifecycle-wit/4491896)).

**Implication for you:** This is your **front of factory** gap (`idea-to-prd` / `prd-to-issues` in the Aru roadmap). Spec Kit is a ready intake harness; Aru’s board is the durable execution harness. Bridge them — do not replace the board with markdown task files.

### 2.3 Pattern C — Board / ticket as orchestrator (your Aru design; industry converging)

**Your insight, validated externally:** durable shared state (GitHub Project / Issues / Linear) beats in-memory supervisor agents. Work survives crashed sessions and reboots.

Mastra’s software factory formalizes six job functions as specialized agents — triage, codegen, validation, release, docs, monitoring — triggered from GitHub/Sentry/schedules ([Mastra](https://mastra.ai/blog/software-factory)). Stripe “Minions” and Spotify background agents show the same shape at scale: task in shared surface → agent PR → human/agent verify → merge.

**Implication for you:** Do **not** add a supervisor agent that duplicates `fetch_next_work.py`. You are already on the durable-orchestrator side of the industry split. Keep the board as source of truth; use specialized skills as “stations,” not a second lifecycle owner.

### 2.4 Pattern D — Agentic Software Factory as operating model (BCG)

BCG Platinion’s five pillars map cleanly onto your program ([BCG Platinion](https://www.bcgplatinion.com/insights/the-agentic-software-factory)):

| BCG pillar | Aru / your translation |
|---|---|
| Intent-driven operating model | Idea → PRD → issues with AC; human owns direction |
| Codified knowledge & tech readiness | `AGENTS.md`, skills, coding standards, CI templates |
| Workforce / role evolution | Operator + harness engineer; agents as workers |
| Assembly lines & harness engineering | Fleet clones, worktrees, per-archetype skills |
| Governance, quality, trust | `merge_pr.py`, independent review, scenario tests |

They also rename sprints → **bolts**: compressed delivery units where humans define intent and validate at stage gates. Your Issue-First board *is* bolt inventory.

Reported ranges (treat as directional, not targets): early factories claim **3–5×** productivity; OpenAI ~**10×** on a constrained experiment; Spotify **60–90%** time cut on large migrations with heavy AI PR volume.

### 2.5 Pattern E — Methodology frameworks (PROSE / Agentic SDLC Handbook)

Daniel Meppiel’s *Agentic SDLC Handbook* argues most orgs adopt agents without a methodology — measuring LOC generated instead of reliable delivery. PROSE encodes architectural constraints that make agent output verifiable and maintainable; companion tooling (APM / Genesis) packages skills across Copilot, Claude Code, Cursor, Codex ([Handbook](https://danielmeppiel.github.io/agentic-sdlc-handbook/)).

**Implication for you:** Aru is already a methodology playbook (SkillsMP skills + scripts). Align vocabulary with PROSE/Spec Kit where useful; do not fork a second doctrine inside governed repos.

---

## 3. Convergent principles — what serious builders agree on

These show up across OpenAI, BCG, GitHub, CodeRabbit, Mastra, and your own factory audits:

1. **Intent is the scarce input.** Unclear requirements produce plausible wrong software at machine speed.
2. **Mechanisms over prompts.** Hooks, CI, merge gates, and typed handoffs beat longer skill prose.
3. **Repo is the knowledge plane.** Slack/Confluence invisible to agents might as well not exist.
4. **Separation of generation and verification.** Author must not be sole reviewer (your `author:` / `reviewed-by:` stamps).
5. **Deterministic seams stay deterministic.** CI/CD, payments, policy engines — agents *call* them; they do not improvise them.
6. **Review is the binding constraint.** Your session evidence (“every PR had ≥1 P1”) matches industry “confidence gap” data (AI PRs denser with issues than human PRs in CodeRabbit studies).
7. **History and telemetry matter.** Squash-everything and no unit economics make factories ungovernable.
8. **One process owner.** Competing lifecycle tools (GSD resume, ad-hoc ralph loops, second boards) create contradictory instructions — you already documented this.
9. **Trust is engineered.** Scenario tests outside agent-writable paths, layered static analysis, red-team agents, rollback.
10. **Humans escalate only on true exceptions.** Your rule — severe merge/close-out failure — matches mature factories; risk categories raise *evidence*, not *authority*.

---

## 4. Reference architecture — the factory you should build

```
                         ┌─────────────────────────────────────┐
                         │         HUMAN OPERATOR              │
                         │  Intent · Exception · Harness design │
                         └──────────────┬──────────────────────┘
                                        │
         ┌──────────────────────────────▼──────────────────────────────┐
         │                    INTAKE (Front)                           │
         │  Idea → Research → PRD/Spec → Epic → Issues (AC, depends-on)│
         │  Tools: Spec Kit / grill-aru / idea-to-prd / prd-to-issues  │
         └──────────────────────────────┬──────────────────────────────┘
                                        │
         ┌──────────────────────────────▼──────────────────────────────┐
         │              COORDINATOR = PROJECT BOARD                    │
         │   Backlog → Ready → In Progress → In Review → Done          │
         │   Picker: fetch_next_work (feedback | review | implement)   │
         └──────┬───────────────────┬───────────────────┬──────────────┘
                │                   │                   │
         ┌──────▼──────┐     ┌──────▼──────┐     ┌──────▼──────┐
         │  IMPLEMENT  │     │   REVIEW    │     │  REMEDIATE  │
         │  worktree   │     │  peer agent │     │  CI / PR FB │
         │  any tool*  │     │  claimable  │     │  same board │
         └──────┬──────┘     └──────┬──────┘     └──────┬──────┘
                │                   │                   │
                └────────────┬──────┴───────────────────┘
                             │
         ┌───────────────────▼─────────────────────────────────────────┐
         │                 DEFINITION OF DONE                          │
         │  merge_pr.py: CI · reviews · threads · AC · size · stamps   │
         └───────────────────┬─────────────────────────────────────────┘
                             │
         ┌───────────────────▼─────────────────────────────────────────┐
         │                   SHIP (Back)                               │
         │  Preview → Staging → Prod · smoke · observe · incident→issue│
         └───────────────────┬─────────────────────────────────────────┘
                             │
         ┌───────────────────▼─────────────────────────────────────────┐
         │                 LEARNING LOOP                               │
         │  Cost/cycle metrics · harness edits · entropy agents        │
         └─────────────────────────────────────────────────────────────┘

* Claude Code · Codex · Cursor · Antigravity · Copilot — same board contract
```

### Station model (assembly line)

| Station | Input | Output | Mechanism |
|---|---|---|---|
| Research | Idea / domain question | Cited notes attached to issue | Research skill; board item first |
| Spec | Idea + research | PRD / Spec Kit artifacts | `idea-to-prd` |
| Decompose | PRD | Issues with `depends-on`, `touches`, AC | `prd-to-issues` |
| Triage | Backlog | Ready depth ≥ fleet size | `triage_backlog.py` |
| Plan gate | `type:feat` / high-risk | Durable plan comment | Scripted gate before branch |
| Implement | Ready issue | PR `Closes #N` | `implement-next-issue` + worktree |
| Verify | PR | Evidence in PR body | Local suite + CI |
| Review | Open PR | `reviewed-by:` + threads | Distinct agent; claimable work |
| Merge | Green PR | Main + Done | `merge_pr.py` only |
| Deploy | Merged SHA | Preview/prod URL | Deterministic CD skill |
| Observe | Telemetry | New board issues | SRE/monitoring agent |

---

## 5. Where Aru stands today (honest placement)

From your own factory docs and audits:

| Factory segment | Maturity | Notes |
|---|---|---|
| Middle (claim → worktree → PR → review → merge) | **Strong / best-in-class direction** | Board-as-orchestrator; mechanical merge gate |
| Front (idea → Ready) | **Weak / partial** | Roadmap S3; Spec Kit / grill-aru bridge |
| Back (deploy → observe → learn) | **Missing / weak** | Roadmap S4 |
| Review throughput | **Binding constraint** | Needs evidence trail + AC runner + identity split |
| Dogfooding CI | **Gap** | Playbook ships gates it does not run on itself |
| Unit economics | **Missing** | Tokens, wall time, review rounds per issue |
| History / versioning | **Fragile** | Squash collapsing narrative; consumer live-tracks `main` |

**Strategic read:** You are not starting from zero. You are mid-build on the rarest part — a **vendor-neutral governance factory**. Most public “software factories” either (a) lock to one agent runtime (Mastra/TS), (b) stay IDE-prompt-only, or (c) demo Spec→Copilot without durable multi-agent claim protocols. Aru’s differentiator should stay: **any agent, one board, mechanisms over vibes.**

---

## 6. Transformation roadmap — idea to product factory

Aligned with [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md). Do not reorder casually: feeding more intake into a broken review gate worsens the queue.

### Phase 0 — Spine trustworthy (days)
Make the merge gate and stamps mechanically correct; dogfood CI; protect `main`; one process owner.

### Phase 1 — Memory & rollback (days)
Merge (not squash) by default; checkpoint tags; SemVer for consumed CLI; pin consumers; revert path.

### Phase 2 — Close the review constraint (1–2 weeks)
Evidence trail; acceptance-criteria runner; “narrower-than-reality” review heuristic; split oversized scopes at triage.

### Phase 3 — Front of factory (1–2 weeks)
`idea-to-prd` → `prd-to-issues` → plan gate as mechanism → Ready-depth SLO → always attach issues to board.

**Recommended intake recipe (industry + Aru):**

```
Human idea
  → research board item (claimed)
  → Spec Kit / grill-aru → PRD
  → prd-to-issues (depends-on DAG)
  → triage_backlog → Ready
  → fleet picker
```

### Phase 4 — Back of factory (2–4 weeks)
`fleet_status.py`; cost/cycle metrics; deploy-preview; promote path; smoke/E2E; cleanup automation.

### Phase 5 — Productization
Stack packs (post-CI release templates); trust boundary for untrusted issue text; degraded-mode decision; golden-path demo repo.

---

## 7. Operating model for a 25-year engineer

### New primary jobs

| Old job | New job |
|---|---|
| Write the feature | Write the **intent** and **acceptance scenarios** |
| Review every line | Design **verification** that scales with agent volume |
| Remember tribal knowledge | **Codify** it in-repo for agents |
| Run the sprint | Run the **factory** (Ready depth, stuck claims, cost) |
| Choose tools | Keep tools **pluggable**; own the **contract** |

### Roles in a small factory (even a one-person company)

1. **Operator** — feeds intent, watches the one screen (§4.2 in factory plan), handles true exceptions.
2. **Harness engineer** — skills, scripts, gates, CI, entropy agents (this is you).
3. **Fleet workers** — N agent sessions with stable `--agent` ids (Cursor, Codex, Claude Code, …).
4. **Independent reviewers** — distinct agent ids; never author of the PR.

### Tool policy (vendor-neutral)

- Any coding agent may implement **if** it follows Aru skills and uses scripts for GitHub/git.
- No agent invents a parallel lifecycle.
- Board item first — research included. No “quick research in chat” that never becomes an issue.

---

## 8. Quality system — engineering trust without reading every line

Layered verification (industry consensus + your lessons):

1. **Spec / AC** — what “done” means, in the issue.
2. **Local suite** — agent must run before push; evidence attached.
3. **CI** — lint, secrets, deps, tests, boundaries (dogfood the template).
4. **Independent agent review** — claimable work; mechanical `reviewed-by:`.
5. **Scenario / E2E** — behavioral scenarios derived from requirements; prefer paths agents cannot silently rewrite.
6. **Architecture contracts** — import-linter / ArchUnit-style gates (agents erode boundaries).
7. **Merge gate** — single sanctioned merge path.
8. **Deploy safety** — preview, canary, rollback (`revert_merge.py`).
9. **Production loop** — alerts → new issues → factory again.
10. **Harness learning** — defects become gate/heuristic upgrades (your “narrower-than-reality” class).

**Do not** equate “AI PR review bot comments” with independent review ownership. Bots assist; a distinct *agent claim* with merge-gate stamps is the accountability model you chose — keep it.

---

## 9. Metrics that make it a factory (not a hobby)

Without these, you cannot prioritize harness work:

| Metric | Question it answers |
|---|---|
| Ready depth vs fleet size | Are we starving the line? |
| Claim age | Is work stuck? |
| Review queue age / rounds | Is verification the bottleneck? |
| CI fail rate | Gates useful or noise? |
| Defect escape / P1s per PR | Is quality holding? |
| Tokens + wall time per closed issue | Unit economics |
| Time idea→Ready and Ready→Done | Where friction lives |
| % work with board origin | Issue-First compliance |

Ship `fleet_status.py` early in Phase 4 — operator visibility is the difference between steering and archaeology.

---

## 10. Anti-patterns (seen in the wild and in your audits)

| Anti-pattern | Why it fails | Better move |
|---|---|---|
| Vibe coding at scale | Plausible wrong software, fast | Spec → tasks → implement |
| Supervisor agent + board | Two sources of truth | Board only |
| Prompt-only governance | Probabilistic compliance | Hooks + merge scripts |
| Same identity author+reviewer | Self-review theater | Stamps + later separate GitHub App |
| Squash everything | Audit/conflict/rollback loss | Merge commits + tags |
| Live-track `main` for consumers | Silent breaking changes | SemVer pin |
| Automate intent | Factory invents the product | Human direction gate |
| Human gates for every risk label | Throughput collapse | Raise evidence, keep agent loop |
| Skipping deploy/observe | Half factory | Close the back |
| Competing SDLC plugins as owners | Contradictory agent instructions | Aru owns lifecycle |

---

## 11. 90-day achievement plan (practical)

### Days 1–14 — Trust the spine
- Finish Phase 0/1 items still open in `ARU-SOFTWARE-FACTORY.md`.
- Run one golden-path demo: issue → worktree → PR → peer review → merge — measured.

### Days 15–45 — Intake + review
- Wire Spec Kit *or* grill-aru into `idea-to-prd` / `prd-to-issues`.
- Acceptance-criteria runner + PR evidence trail.
- Ready-depth SLO; stop starting new products until Ready feeds the fleet.

### Days 46–90 — Ship and learn
- Preview deploy skill; smoke tests; `fleet_status` + cost capture.
- One product built end-to-end through the factory (idea → production).
- Write a postmortem of harness defects into skills (entropy → mechanism).

**Success criteria for “we have a factory”:**

1. A new idea becomes board issues without chat archaeology.
2. Any listed coding agent can claim and ship under the same contract.
3. Merge only through the gated helper; main protected.
4. Operator sees status on one screen.
5. At least one product has preview→prod with issue trail after merge.
6. You can quote cost and cycle time per closed issue.

---

## 12. How this maps to your stated requirements

| Your requirement | Factory answer |
|---|---|
| Start with idea | Intake station; board item from day zero |
| Gather information / research | Research as claimable board work, artifacts attached |
| Proper requirement documents | Spec Kit / PRD skills; living specs in repo |
| Sprint by sprint | Bolts/issues on the board; fleet pulls Ready work |
| Agile + test-first + deployable code | AC + TDD by agents; CI; CD as back-of-factory |
| Works with Claude/Codex/Antigravity/Cursor | Vendor-neutral skills + `$ARU_SDLC_HOME` |
| Every work item on the board first | Issue-First Law — non-negotiable |
| GitHub Project initially | Board = orchestrator; abstract later if needed |

**Board choice (GitHub vs Linear vs Jira):** see the dedicated analysis [`PROJECT-BOARD-FOR-AGENTIC-FACTORY.md`](PROJECT-BOARD-FOR-AGENTIC-FACTORY.md). Short form: keep GitHub Issues + Projects as the Aru spine; Linear wins agent-delegation UX; Jira wins enterprise governance; never run two spines.

---

## 13. Curated sources (primary)

| Source | Why read it |
|---|---|
| [OpenAI — Harness engineering](https://openai.com/index/harness-engineering/) | Canonical “agents write everything; humans build harness” proof |
| [BCG Platinion — Agentic Software Factory](https://www.bcgplatinion.com/insights/the-agentic-software-factory) | Enterprise operating model & five pillars |
| [GitHub Spec Kit](https://github.com/github/spec-kit) / [blog](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/) | Intent → executable specs |
| [Microsoft — AI-led SDLC](https://techcommunity.microsoft.com/blog/appsonazureblog/an-ai-led-sdlc-building-an-end-to-end-agentic-software-development-lifecycle-wit/4491896) | Spec→issue→agent→deploy→SRE end-to-end |
| [CodeRabbit — Agentic SDLC guide](https://www.coderabbit.ai/guides/agentic-sdlc) | Verification tax & review layer |
| [Mastra — AI Software Factory](https://mastra.ai/blog/software-factory) | Six-agent station model (typed handoffs) |
| [Agentic SDLC Handbook (Meppiel)](https://danielmeppiel.github.io/agentic-sdlc-handbook/) | Methodology / PROSE for orgs |
| Forrester — State of Agentic Software Development 2026 | Market frame: assistants → orchestrated SDLC agents |
| Your [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md) | Local truth — sequencing & non-goals |

---

## 14. Bottom line

You are not trying to invent “AI that codes.” That commodity already exists in Claude Code, Codex, Cursor, Antigravity, and Copilot.

You are building what the industry now names and proves at the highest levels: an **agentic software factory** — harness + intent + board + mechanical trust — that turns a 25-year craft process into a governed production system.

**Aru’s middle is already the hard part most teams skip.** Finish making it trustworthy, close the review constraint, then extend **forward** into idea/spec and **backward** into deploy/observe. Resist a second orchestrator. Measure unit economics. Keep Issue-First absolute.

When those pieces lock, the factory does what you want: **idea in, quality software out** — sprint by sprint, agent by agent, every action logged on the board first.
