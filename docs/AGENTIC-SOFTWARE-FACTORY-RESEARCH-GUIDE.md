# Agentic Software Factory — Research and Evidence Guide

**Evidence for transforming a 25-year SDLC into an idea→product agent factory**

**Audience:** Factory owner / architect (25 years of software craft)  
**Date:** 2026-08-11  
**Research origin:** #78 (closed; consolidated by #113)
**Companion artifacts:**
- Working build plan: [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md)
- Interactive briefing: [`agentic-software-factory-briefing.html`](agentic-software-factory-briefing.html)
- Board choice deep-dive: [`PROJECT-BOARD-FOR-AGENTIC-FACTORY.md`](PROJECT-BOARD-FOR-AGENTIC-FACTORY.md)
- Process baseline: [`PROCESS-AUDIT-2026-08.md`](PROCESS-AUDIT-2026-08.md)

> **Authority boundary.** This document is evidence, not plan. It explains the
> research, industry patterns, and operating-model implications behind Aru.
> [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md) is the canonical
> narrative plan; [roadmap epic #335](https://github.com/gillella/Aru_Agentic_SDLC/issues/335)
> and the Project Board are the live authority for phases, sequencing, and
> implementation status. If an example or Historical recommendation here
> conflicts with that authority, the board-backed roadmap wins.
> **Interactive briefing.**
> [`agentic-software-factory-briefing.html`](agentic-software-factory-briefing.html)
> provides a self-contained, tool-neutral tour of this evidence. It requires no
> editor, build, server, or network connection. The briefing summarizes this
> guide; it does not create a second roadmap, and #335 remains authoritative.

---

## Lifecycle status vocabulary

These terms are normative across the README and both canonical factory guides.
Research examples do not confer implementation status.

| Status | Meaning |
|---|---|
| **Shipped** | Present in the repository with linked implementation evidence. |
| **Current** | The live roadmap slice represented by open board work; consult the board for item state. |
| **Deferred** | Intentionally sequenced after an unmet phase entry gate; not available now. |
| **Blocked** | Cannot start or finish until an explicit dependency or operator decision is satisfied. |
| **Historical** | Dated evidence about an earlier state; never a current capability claim. |
| **Audit-only** | Records governance evidence but does not prove a runnable artifact or environment. |

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

**Implication for you:** The base front-of-factory path is **Shipped** through
claimable research, `idea-to-prd`, and `prd-to-issues` (#101–#103). Spec Kit
remains a useful intake reference, but Aru's board is the durable execution
harness. Clarification and convergence stations are **Deferred** to roadmap
Phase 3 (#339); they extend the shipped capabilities rather than rebuilding
them or replacing the board with Markdown task files.

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
         │  Pages preview [Shipped] · promotion record [Audit-only]   │
         │  provider delivery + rollback [Deferred: #345 / #84]       │
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
| Deploy | Merged SHA | GitHub Pages preview URL | `deploy-preview` — **Shipped** |
| Record promotion | Checkpoint evidence | GitHub state trail, not hosting | `promote.py` — **Audit-only** |
| Prove provider delivery | Immutable deployment | Authoritative URLs, smoke, promotion, rollback | #345 then #84 — **Deferred** |
| Observe | Telemetry | New board issues | SRE/monitoring agent |

---

## 5. Where Aru stands today (honest placement)

This is a capability summary, not a replacement for live board state:

| Factory segment | Status | Evidence boundary |
|---|---|---|
| Governed middle: claim → worktree → PR → review → merge | **Shipped** | Board orchestration, exact-head review evidence, acceptance runner, and mechanical merge gate |
| Base intake: research → PRD → issue DAG | **Shipped** | #101–#103; Phase 3 clarification/convergence remains **Deferred** (#339) |
| Runnable preview | **Shipped** | `scripts/deploy_preview.py` records exact commit, GitHub Pages URL, and smoke evidence (#109/#111) |
| Promotion record | **Audit-only** | `scripts/promote.py` records GitHub state; it does not prove artifact movement or hosted staging/production (#110) |
| Real provider delivery | **Deferred** | #345 must prove immutable identity, authoritative URLs, live smoke, promotion without rebuild, and rollback before Phase 4 (#84) generalizes it |
| Operator telemetry | **Shipped** | #106–#108 provide dwell, rework, cycle time, available cost evidence, and `fleet_status.py` |
| Dogfooding CI | **Historical** evidence; capability **Shipped** | Verified 2026-08-15; the current workflow remains the code authority |
| History / versioning | **Shipped** | `merge_pr.py` defaults to merge commits; squash is opt-in (#89); checkpoint/SemVer tags and consumer pinning exist |
| Governed kernel and capability admission | **Current** / **Deferred** | Phase 0 is Current (#336); kernel boundary and capability admission wait for their entry gates (#337/#338) |

**Strategic read:** Aru already ships the governed middle, base intake, preview,
telemetry, and history spine. The Current program is making that baseline
truthful and proving one real provider delivery before extracting a governed
kernel. Aru's differentiator remains: **any agent, one board, mechanisms over
vibes.**

---

## 6. Relationship to the canonical plan

The evidence above supports the trust-first sequence now recorded in #335:
restore the baseline and prove external delivery, define the kernel, admit
optional capabilities, add clarification/convergence, then generalize delivery
and multi-project release. It does not define or duplicate that sequence.

For the current phases, issue mapping, order, status, and non-goals, use
[`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md), roadmap epic #335, and
the Project Board. Historical roadmap restatements from the predecessor guides
remain evidence only, so agents cannot choose among competing schedules.

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
2. **Focused local verification** — each story runs its issue-declared tests,
   affected checks, and static/build predicates before push; exact evidence is
   attached to the PR.
3. **CI and checkpoints** — PRs run the fast repository gates; the complete
   supported Python 3.11 suite runs on an exact phase-exit commit and before
   release. #341 and #357 are the Current enforcement work for this approved
   policy, so their open state must not be described as Shipped.
4. **Independent agent review** — claimable work; mechanical `reviewed-by:`.
5. **Scenario / E2E** — behavioral scenarios derived from requirements; prefer paths agents cannot silently rewrite.
6. **Architecture contracts** — import-linter / ArchUnit-style gates (agents erode boundaries).
7. **Merge gate** — single sanctioned merge path.
8. **Deploy safety** — runnable GitHub Pages preview is Shipped; promotion
   records are Audit-only; real provider promotion/rollback is Deferred to #345
   and #84. `revert_merge.py` remains the governed source rollback path.
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

The baseline is **Shipped** in `fleet_status.py` and `factory_metrics.py`
(#106–#108). Phase 5 (#186) is Deferred proof that the multi-project factory can
use those measures consistently; absent producer evidence must stay visibly
unavailable rather than being estimated as fact.

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

## 11. Evidence-to-plan boundary

Use this guide to evaluate claims, choose mechanisms, and understand tradeoffs.
Use the canonical build plan, #335, and the Project Board to decide what happens
next. A research finding may justify changing the plan, but that change must be
reconciled in governed issues rather than introduced here as a parallel
schedule.

Evidence that the factory is functioning includes: ideas becoming durable
board work, agents sharing one lifecycle contract, merges passing the governed
gate, operator-visible state, deploy-to-observe traceability, and measurable
cost and cycle time. These are evaluation dimensions, not a dated delivery
sequence.

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
| [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md) + #335 | Narrative plan plus live board-governed sequencing and non-goals |

### Academic evidence index

| Paper | arXiv ID | Evidence retained from the predecessor landscape guide |
|---|---|---|
| SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | 2405.15793 | Interface design matters as much as model capability |
| An Empirical Study of Coordination Mode as the First-Class Citizen in From-Scratch Multi-Agent Coding | 2607.27877 | Topology rivals model capability in speed-cost-quality |
| ACEM: A Cost Estimation Model for Agentic Software Engineering | 2608.02582 | Agentic cost includes tokens, human oversight, and infrastructure |
| The Productivity-Reliability Paradox: Specification-Driven Governance | 2605.01160 | Specification discipline is a binding reliability constraint |
| Trust but Verify? Uncovering the Security Debt of Autonomous Coding Agents | 2607.12428 | Agent-generated changes accumulate security debt without gates |
| Early Adoption of Agentic Coding Tools by GitHub Projects | 2607.14037 | Adoption is real but concentrated among intensive users |
| AgentForge: An Immersive Role-Playing Platform for Learning Agentic Software Engineering | 2608.04148 | Multi-role repair workflows support specialized agent stations |
| Code Is the Body: Agent-Owned Software Bodies for Recursive Evolution and Descent | 2607.28691 | Self-evolution still requires governed custody and merge control |
| Where Is the Cost of Third-Party API Routers in Agentic Software Development? | 2607.23624 | Routers occupy a consequential trust and cost boundary |
| Why Are Agentic Pull Requests Merged or Rejected? | 2605.22534 | Workflow constraints explain many agentic PR rejections |

### Open-source landscape index

| Project | Reference | Evidence retained from the predecessor landscape guide |
|---|---|---|
| delivery-loop | `github.com/blakemartz/delivery-loop` | Specs-to-merge loop using claims, worktrees, and adversarial review |
| software-factory | `github.com/deepkawal/software-factory` | Enterprise multi-role pipeline with explicit governance |
| SWE-agent | `github.com/SWE-agent/SWE-agent` | Agent-computer interface research and SWE-bench implementation |
| Mini-SWE-Agent | Princeton / Stanford successor to SWE-agent | A deliberately small agent implementation showing that interface simplicity can outperform orchestration complexity |
| OpenHands Agent Canvas | `github.com/OpenHands/OpenHands` | Self-hosted multi-agent control surface and protocol work |
| OurArk | Academic agent-owned software-body project | Governed self-evolution in which software changes remain under human custody and merge control |
| MSEval / LegoGent | Academic multi-agent evaluation project | Comparison of collaboration topologies showing that coordination design rivals model selection |
| jddelia/agentic-factory | `github.com/jddelia/agentic-factory` | Codex-oriented factory with durable SQLite ledgers |
| repoach | `github.com/repoachhq/repoach` | Autonomous review-and-merge pipeline focused on the review constraint |

### Adjacent orchestration and platform comparisons

| Project or platform | Relevance and retained conclusion |
|---|---|
| CrewAI | Crews and event-driven flows are useful role/task references, but Aru does not need a second orchestrator beside the board |
| LangGraph | Durable execution and human-interrupt patterns are useful for exception design; adopting its state machine would duplicate board state |
| Claude Code Agent Teams | Its shared `tasks.md` model is less durable than GitHub Issues and Projects, so Aru keeps the board authoritative |
| GitHub Spec Kit | Its specify → plan → tasks artifact pattern informs intake without replacing board execution |
| GitHub Copilot Workspace | Demonstrates GitHub-native issue-to-plan-to-PR flows and supports the platform-as-factory direction |
| GitHub Models and Copilot Extensions | Demonstrate GitHub's role as an integration surface for models and agent capabilities |

### Industry guidance retained

| Source | Evidence retained from the predecessor landscape guide |
|---|---|
| Anthropic, *Steering Claude Code* | Deterministic hooks and permissions make stronger guardrails than prose |
| GitHub Spec Kit | Specify → plan → tasks → implement is a reusable intake artifact pattern |
| OpenHands best-practice and protocol work | Vendor-neutral agent interfaces reduce runtime lock-in |
| The New Stack, *85% say code review is the new bottleneck* | Review capacity becomes the constraint as implementation accelerates |

### Consolidation record

Issue #113 merged the evidence produced under #78 with the earlier *State of
the Art & Guide*. Nothing was silently discarded:

| Predecessor material | Canonical location after consolidation |
|---|---|
| Vision and classical-to-agentic mapping | Sections 0-1 |
| Industry and open-source landscape | Section 2 and the source indexes above |
| Academic findings and all ten arXiv identifiers | Sections 3 and 13 |
| Board, claim, worktree, path-budget, merge, and plan patterns | Sections 3-5 and 8 |
| Constraints, metrics, and anti-patterns | Sections 5 and 7-10 |
| Roadmap and non-goal restatements | [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md), with #335 and the Project Board as live authority |

The deleted predecessor filename was
`AGENTIC-SOFTWARE-FACTORY-GUIDE.md`. Git history preserves its exact prose;
this appendix preserves its evidence, findings, citations, and mapping without
leaving a second document that an agent could mistake for the plan.

---

## 14. Bottom line

You are not trying to invent “AI that codes.” That commodity already exists in Claude Code, Codex, Cursor, Antigravity, and Copilot.

You are building what the industry now names and proves at the highest levels: an **agentic software factory** — harness + intent + board + mechanical trust — that turns a 25-year craft process into a governed production system.

**Aru's governed middle and base intake are Shipped.** The Current program
restores a trustworthy baseline and proves real external delivery; governed
kernel extraction, capability admission, clarification/convergence, generalized
delivery, and multi-project release remain Deferred behind explicit phase
gates. Resist a second orchestrator. Measure unit economics. Keep Issue-First
absolute.

When those pieces lock, the factory does what you want: **idea in, quality software out** — sprint by sprint, agent by agent, every action logged on the board first.
