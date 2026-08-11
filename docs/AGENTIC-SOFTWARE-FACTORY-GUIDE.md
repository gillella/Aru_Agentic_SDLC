# The Agentic Software Factory — State of the Art & Guide

> A deep research synthesis for Aru: what the world is building, what works, what breaks, and how to build your software factory.

**Based on:** 8 arXiv papers (2024–2026), 5 open-source projects, Anthropic/OpenHands/GitHub engineering guidance, and Aru's own v0.1 factory audit.

---

## Table of Contents

1. [The Vision: Why a Software Factory Now](#1-the-vision-why-a-software-factory-now)
2. [The Landscape: Who's Building What](#2-the-landscape-whos-building-what)
3. [The Research: What Academia Has Proven](#3-the-research-what-academia-has-proven)
4. [The Architecture: Patterns That Work](#4-the-architecture-patterns-that-work)
5. [The Mechanics: What Must Be Deterministic](#5-the-mechanics-what-must-be-deterministic)
6. [The Constraints: What Will Break First](#6-the-constraints-what-will-break-first)
7. [The Roadmap: Your Build Sequence](#7-the-roadmap-your-build-sequence)
8. [The Non-Goals: What NOT to Build](#8-the-non-goals-what-not-to-build)
9. [Appendix: Source Index](#9-appendix-source-index)

---

## 1. The Vision: Why a Software Factory Now

### The old pipeline you're replacing

```
Idea → Research → Requirements Doc → Sprint-by-Sprint Execution
        (human)     (human)            (humans, ~2-week cycles)
```

Every handoff is a bottleneck. Every sprint planning session is a scheduling meeting for human attention. The last 20 years of agile, TDD, CI/CD, and DevOps built incredible infrastructure — but it was all designed for **humans turning the crank**.

### The new pipeline

```
Idea → PRD → Dependency-Ordered Issues → Claim → Isolate → Implement → Verify → Review → Merge → Deploy → Observe
  |      |              |                   |        |          |          |         |       |        |        |
human  agent+human    agent               agent    agent     agent     agent    agent   agent   agent    agent
```

The factory is **not** "AI writes all the code." It is:

> **Humans define direction. Agents execute mechanics. Gates enforce correctness. The board coordinates everything.**

### Why now?

Three things converged in 2025–2026:

1. **Coding agents crossed the capability threshold.** SWE-agent hit 65%+ on SWE-bench Verified. Claude Code, Codex, and Cursor can autonomously navigate repos, write tests, and fix bugs.
2. **Multi-agent coordination became practical.** The MSEval study (Aug 2026) proved that organizational topology rivals model capability in determining speed–cost–quality outcomes.
3. **GitHub became programmable infrastructure.** Issues, Projects, Actions, and the `gh` CLI form a durable state machine that survives crashed sessions and machine reboots.

---

## 2. The Landscape: Who's Building What

### 2.1 Academic projects

| Project | Institution | Approach | Key Insight |
|---|---|---|---|
| **SWE-agent** | Princeton / Stanford | LM + custom Agent-Computer Interface (ACI) to fix GitHub issues | Interface design matters as much as model capability; SoTA on SWE-bench |
| **Mini-SWE-Agent** | Princeton / Stanford | 100 lines of Python, 65% on SWE-bench Verified | Simplicity wins — the successor to SWE-agent is dramatically simpler |
| **OpenHands** (formerly OpenDevin) | All Hands AI | Self-hosted "Agent Canvas" — control center for multiple coding agents | Agent-Client Protocol (ACP) for vendor-agnostic agent orchestration |
| **MSEval / LegoGent** | Academic | Multi-agent from-scratch benchmark across 10 collaboration topologies | **Topology rivals model capability** in speed–cost–quality |
| **OurArk** | Academic | Agent-owned "software bodies" — self-evolving code artifacts under human custody | Governed self-evolution; merge under human control |

### 2.2 Open-source software factories

These are the ones you should study — they solve the same problem you're solving:

| Project | Approach | Strengths | Weaknesses |
|---|---|---|---|
| **delivery-loop** (blakemartz) | Claude Code plugin. Specs → backlog → claim → implement → adversarial review → patch → merge. Git worktrees. One `check.sh` gate. | Cleanest design. Board-as-state. Adversarial review. No server, no DB. Human-in-loop or auto-merge modes. `--parallel N`. | Claude Code only. Shell scripts limit complexity. Early stage. |
| **software-factory** (deepkawal) | 10-agent pipeline (Product, Architect, Engineer, Reviewer, QA, Docs, Ops). Enterprise-oriented. | Full lifecycle modeled. ADR/PD discipline. Human-governed delivery for regulated environments. | Complex. Opinionated about org structure. |
| **jddelia/agentic-factory** | Codex plugin with SQLite-backed durable ledgers. | Durable state tracking. Codex integration. | Niche (Codex only). |
| **repoach** | Autonomous PR review + merge pipeline. Self-hosted multi-agent. | Focused on the review constraint. | Narrower scope — just review+merge, not full factory. |

### 2.3 Orchestration frameworks (not factories, but tooling)

| Framework | What it does | Relevance to your factory |
|---|---|---|
| **CrewAI** | High-level multi-agent orchestration with Crews (autonomous) and Flows (event-driven) | Good pattern reference for agent roles/tasks. But you don't need a separate orchestrator — your board IS the orchestrator. |
| **LangGraph** | Low-level stateful agent orchestration with durable execution, HITL, memory | Overkill for your model. The board is your durable execution. But their HITL interrupt pattern is worth studying for escalations. |
| **Claude Code Agent Teams** | Shared `tasks.md` file for multi-Claude coordination | **Don't adopt this.** Your board is strictly more durable than a `tasks.md` file. |
| **GitHub Spec Kit** | Specify → Plan → Tasks → Implement pipeline with AI | The front-of-factory pattern you're missing. Their decompose workflow is the blueprint for your intake stage. |

### 2.4 What GitHub itself is doing

- **Spec-driven development** — GitHub's official toolkit for AI-assisted development: write a spec, decompose into tasks, implement. 90k+ stars.
- **GitHub Copilot Workspace** — Native issue-to-PR flow with agentic planning.
- **GitHub Models / Copilot Extensions** — Marketplace for agent integrations.

The trend is clear: **GitHub is becoming the OS for agentic development.** Your bet on "GitHub Project Board as orchestrator" is aligned with the platform's direction.

---

## 3. The Research: What Academia Has Proven

### 3.1 Specification discipline is the binding constraint

**Key paper:** *"The Productivity-Reliability Paradox: Specification-Driven Governance"* (arXiv:2605.01160)

> "Specification discipline, not model capability, is the binding constraint on AI-assisted software dependability."

Translation: better prompts won't fix this. Better specifications will. This is why your factory needs a **plan gate** between "issue" and "code" — not a human gate, but a mechanism that ensures an implementation plan exists as a durable artifact before the first edit.

### 3.2 Coordination topology rivals model capability

**Key paper:** *"An Empirical Study of Coordination Mode as the First-Class Citizen in From-Scratch Multi-Agent Coding"* (arXiv:2607.27877, Aug 2026)

Tested 10 collaboration topologies across 10 real-world full-stack projects. Finding: **for identical tasks and models, organizational design drives the speed–cost–quality trade-off as much as model choice.**

This validates your architecture: the claim protocol, worktree isolation, `touches:` path budget, and board-as-coordinator are not implementation details — they are the factory's competitive advantage.

### 3.3 Code review IS the bottleneck

**Key finding from multiple 2026 studies:**

- 98% more PRs merged with 91% longer review times (telemetry)
- Agentic PRs wait ~5.3x longer for reviewer pickup
- Only 35.7% of agentic PR rejections were real failures; 31.2% were workflow constraints

**The implication for your factory:** unmodeled review destroys good work. Your `code-review` skill is necessary but insufficient — it must be **mechanically invoked** (not just documented) and the reviewer must be a **distinct agent** (not the same model instance that wrote the code).

### 3.4 Security debt is real and growing

**Key paper:** *"Trust but Verify? Uncovering the Security Debt of Autonomous Coding Agents"* (arXiv:2607.12428)

> 38.9% of agent-generated PRs contain at least one security smell. Supply chain issues are the most common class.

Your factory needs `gitleaks` + `pip-audit` in CI from day one. Not optional.

### 3.5 Adoption is concentrated but real

**Key paper:** *"Early Adoption of Agentic Coding Tools by GitHub Projects"* (arXiv:2607.14037)

Analysis of 25,264 agentic PRs across 2,361 repos: the median repo generates only 1-2 agentic PRs in 3 months. Intensive adoption is concentrated. This means **the factories that work will have first-mover advantage** — the gap between early adopters and the median will widen fast.

### 3.6 Cost estimation needs a new model

**Key paper:** *"ACEM: A Cost Estimation Model for Agentic Software Engineering"* (arXiv:2608.02582)

Traditional models (COCOMO II, Function Points, Story Points) assume human labor. Agentic SE adds three new cost dimensions:
1. **LLM token consumption** across agent actions (nondeterministic)
2. **Human-in-the-Loop (HITL) oversight effort**
3. **Infrastructure costs** for agent orchestration

Your factory needs cost capture per issue from the start — otherwise you can't answer "which stages are worth automating further?"

---

## 4. The Architecture: Patterns That Work

### 4.1 The Board IS the Orchestrator

This is the single most important architectural decision, and you already got it right.

```
❌ WRONG: Supervisor agent → task queue → workers
         (fails when supervisor crashes; needs always-on process)

✅ RIGHT: GitHub Project Board → agents poll `fetch_next_work.py`
         (survives crashes, reboots, and machine failures)
```

The board is a **durable shared state machine**:
- `Backlog` → `Ready` → `In Progress` → `In Review` → `Done`
- Issues carry `touches:` (write footprint), `depends-on:` (DAG edges), acceptance criteria
- Labels track claim ownership, review completion, merge state
- Nothing is held in memory that can't be reconstructed from the board

### 4.2 The Claim Protocol

Your optimistic claim protocol is the correct design:

```
1. Agent reads board, finds next unblocked Ready issue
2. Optimistically writes `claimed-by:<agent_id>` label
3. Reads back — if another agent also claimed, deterministic tie-break (lowest agent ID wins)
4. Loser removes its own label, picks again
5. Stale claims reaped via three-way liveness check (idle time + no open PR + no remote branch)
```

This is better than optimistic locking alone (no compare-and-swap on GitHub labels) and better than a central lock server (single point of failure).

### 4.3 Worktree Isolation

Each agent works in a dedicated `git worktree` — a throwaway checkout that:

- Prevents file-level conflicts between parallel agents
- Survives agent crashes (just prune the worktree, re-clone)
- Enables true parallel execution (N agents = N checkouts)
- Clean verification: review happens in a fresh checkout, not the implementer's workspace

This is what `delivery-loop` does and what your factory already specifies. It's the right isolation primitive.

### 4.4 The `touches:` Budget

Your `touches:` path-conflict detection is **the single most important idea in your framework** and most public setups haven't caught up to it:

```
Issue #42:
  touches: src/payment/processor.py, src/payment/types.py, tests/test_processor.py

Agent's PreToolUse hook: refuses Edit/Write/Bash targeting any path
outside the agent's claimed issue's touches: declaration.
```

This prevents two agents from silently editing the same file in parallel, which is the #1 source of merge conflicts in multi-agent setups.

### 4.5 The Merge Gate

The merge gate must be mechanical, not human. `merge_pr.py` should refuse unless:

- ✅ CI green (with real, blocking checks — not advisory)
- ✅ ≥1 distinct-agent review with `reviewed-by:` stamp
- ✅ No unresolved review threads
- ✅ Every acceptance checkbox ticked (or explicitly waived)
- ✅ Branch rebased on current `main`
- ✅ PR size under cap (or explicit waiver)
- ✅ `touches:` budget not exceeded

Then merge, delete branch, move issue to Done, prune worktree. **No human in the merge path** — the human set the gate conditions; the gate enforces them.

### 4.6 The Plan Gate

Between "issue claimed" and "first edit," insert a plan gate:

```
type:feat or needs-design label
  → Agent posts implementation plan as issue comment:
    - Approach (what changes, not how to code)
    - Files to touch
    - Schema/API deltas (if any)
    - Test strategy
    - Rejected alternatives
  → Post-and-proceed (not post-and-block)
```

The plan becomes a durable artifact for review, not a permission step. Risk (money, PII, schema) increases plan depth but doesn't create a human gate.

---

## 5. The Mechanics: What Must Be Deterministic

The audit's most important finding: **"Every guardrail is an instruction; none is a mechanism."**

Rules in prose are followed probabilistically. Rules in hooks are followed deterministically. Here's what must be mechanisms:

### 5.1 Enforcement Layer

| Rule (prose) | Mechanism (hook/script) | Failure mode without it |
|---|---|---|
| "No direct pushes to main" | `pre-push` git hook → exit 1 | Agent bypasses PR process |
| "Stay within touches: budget" | `PreToolUse` hook → exit 2 on path outside touches: | Parallel agents corrupt each other's work |
| "CI must pass before merge" | GitHub ruleset with required status checks | Broken code lands on main |
| "Distinct agent must review" | `merge_pr.py` refuses without `reviewed-by:` label from different agent | Same agent reviews its own code |
| "Branch naming convention" | `create_branch.py` enforces `issue-N-slug` format | `fetch_next_work.py` can't resume sessions |
| "Issue must exist before work" | `create_branch.py` / `implement-next-issue` requires issue number | Ungoverned work bypasses the board |

### 5.2 GitHub Pro Is Infrastructure, Not an Expense

At ~$4/month, GitHub Pro unlocks:
- **Rulesets** on private repos (required status checks, no direct push, required linear history)
- **Required reviews** (once you have a distinct reviewer identity — see §6.2)

Without it, server-side protection doesn't exist. Every hook can be bypassed. This is not a nice-to-have; it's the difference between a governed factory and a suggestion system.

### 5.3 CI Must Be Real

Current CI (`ci.yml`) has three gates that all no-op:
```yaml
ruff check . || echo "::warning::"     # never fails
# tests: "if no tests present, skip"    # never runs
# import-linter: "if no config, skip"   # never runs
```

Real CI:
```yaml
ruff check .                            # blocks on findings
pytest (fail if src exists, tests don't) # requires test coverage
gitleaks                                # blocks on leaked secrets
pip-audit                               # blocks on known vulns
import-linter (when configured)         # blocks on boundary violations
```

Green CI must mean something, or the merge gate is theater.

---

## 6. The Constraints: What Will Break First

### 6.1 Review throughput (constraint #1)

**Evidence from your audit:** PR #31 went through 6 review rounds, 23 threads, before merge. The reviewer was an unowned Codex bot wired outside the framework.

**Evidence from the field:** 98% more PRs, 91% longer review times. Agentic PRs wait 5.3x longer for reviewer pickup.

**Your fix, in order of impact:**

1. **Mechanical review invocation.** When a PR opens, `code-review` skill runs automatically (CI action or agent tick). Review starts within a minute, not whenever someone looks.
2. **Distinct-agent review.** The reviewing agent must differ from the implementing agent. This is enforced by your `reviewed-by:` stamp mechanism.
3. **Adversarial review posture.** The reviewer starts from "find what's wrong" not "confirm it looks good." This is what `delivery-loop` does and it catches more defects.
4. **Bounded fix cycles.** 3 rounds max, then escalate to human with evidence. Prevents infinite review loops.
5. **PR size cap.** 400-line soft limit. Agentic PRs run ~2.6x larger than human ones. Size correlates with review rounds.

### 6.2 Single GitHub account (constraint #2)

Every agent authenticates as the same GitHub account. This means:

- GitHub's "required approving review" **deadlocks** — an account can't approve its own PR
- You must rely on label-based identity (`reviewed-by:<agent_id>`) instead of server-enforced review
- Until you have a separate reviewer identity (GitHub App or second account), server-side required approval is unavailable

**Workaround:** Label-based gate in `merge_pr.py` is correct for now. Phase in a GitHub App for reviewer identity when you need server-enforced approval.

### 6.3 Ready-column depth (constraint #3)

With N agents, the Ready column drains in hours. If triage isn't automated, agents idle. Your `triage-backlog` skill + "Ready depth SLO" is the right answer: nag when Ready < fleet size.

### 6.4 Framework collision (constraint #4)

GSD, superpowers, ralph-loop, and claude-mem all want to own the workflow. An agent starting a session receives contradictory process instructions. One process owner per repo. Your `AGENTS.md` fix from the audit is correct: Aru owns the lifecycle; disable or remap competitors.

### 6.5 Squash merges (constraint #5)

Squash merges collapse branch history. Your audit found 46 branch commits → 6 on main. This hides the signal you need to detect agent drift. Switch default to `merge` commits.

---

## 7. The Roadmap: Your Build Sequence

### Phase 0 — Make the spine trustworthy (this week)

**The rule:** nothing else matters while the merge gate can fall open.

| # | Action | Effort | Status |
|---|---|---|---|
| 0.1 | Fix `_git_write_to_protected` quoting | 1h | Open |
| 0.2 | Fix `merge_pr.py` close-out ordering (merges silently leave board stale) | 1h | Open |
| 0.3 | Make CI gates blocking: ruff, tests-required, gitleaks, pip-audit | 1h | Open |
| 0.4 | GitHub Pro + rulesets: no direct push to main, required status checks | 15min | Needs purchase |
| 0.5 | `pre-push` hook + `PreToolUse` hook: touches enforcement, main protection | 2h | Partially done |
| 0.6 | Process ownership doc in AGENTS.md: Aru owns lifecycle | 30min | Done (issue #48) |
| 0.7 | Fix stale `create_pr.py` call sites | 1h | Done |
| 0.8 | Make `reviewed-by:` stamp mechanical (complete_review script) | 1h | Done (6e07316) |

### Phase 1 — History, versioning, rollback (next 3–5 days)

| # | Action | Why |
|---|---|---|
| 1.1 | Switch `merge_pr.py` default from `squash` to `merge` | Stops deleting 87% of history |
| 1.2 | Annotated `ckpt/*` tag on every merge (PR, issues, author, reviewer, gate verdicts) | Audit trail + rollback grid |
| 1.3 | SemVer `v0.x` release tags; MAJOR on CLI break | Consumers need version pinning |
| 1.4 | Consumer pinning: `ARU_SDLC_REF` env var; `install_cursor_integration.sh` honours it | Stop silent breakage on `main` updates |
| 1.5 | `Agent: <id>` commit trailer | Per-commit attribution |
| 1.6 | Documented revert path in AGENTS.md + `revert_merge.py` | Factories need reverse gear |

### Phase 2 — Review constraint (weeks 1–2)

| # | Action | Why |
|---|---|---|
| 2.1 | Evidence trail: verify commands + exit codes → PR body | "Tests passed" without evidence is theater |
| 2.2 | Acceptance-criteria runner: parse checkboxes → verification commands | Decorative ACs become real gates |
| 2.3 | Codify "narrower-than-reality" heuristic in `code-review/SKILL.md` | Your #1 recurring defect class |
| 2.4 | Surface narrowly-scoped unresolved merge/close-out intervention with evidence | Routine review stays in agent loop |
| 2.5 | Split guidance in triage for oversized scopes | Prevent review death spirals upstream |

### Phase 3 — Front of factory: idea → Ready work (weeks 2–3)

| # | Action | Why |
|---|---|---|
| 3.1 | `idea-to-prd` skill — thin wrapper over existing interrogation flow | Factory starts at intent, not shaped issues |
| 3.2 | `prd-to-issues` — epic → phased issues with `depends-on` DAG, `touches:`, ACs | Epics become claimable work |
| 3.3 | Plan gate as mechanism: `type:feat` / `needs-design` requires plan artifact before branch | Today it lives only in skill prose |
| 3.4 | Ready-depth SLO + nag when Ready < fleet size | Idle fleet = triage failure |
| 3.5 | Board attachment on issue create | Orphan issues are invisible to coordinator |

### Phase 4 — Back of factory: deploy + observe (weeks 3–6)

| # | Action | Why |
|---|---|---|
| 4.1 | `fleet_status.py` — one screen: holders, PR ages, review rounds, CI rate, Ready depth | You cannot steer what you cannot see |
| 4.2 | Cost and cycle-time capture per closed issue (tokens, wall time, review rounds) | Input to all future prioritization |
| 4.3 | `deploy-preview` skill, gated post-merge | Idea→merge is half a factory |
| 4.4 | Promotion path: preview → staging → prod with issue/PR trail | Issue-First survives past merge |
| 4.5 | Smoke / E2E in CI for products with runnable surface | Unit green ≠ product works |
| 4.6 | Post-merge cleanup automation | Done must mean clean |

### Phase 5 — Productization (when consulting takes off)

| # | Action | Why |
|---|---|---|
| 5.1 | Stack packs: Node/TS, Python, Go CI + test + deploy templates | Stop being Python-shaped |
| 5.2 | Trust boundary for untrusted issue/PR text | Required before any repo accepts external issues |
| 5.3 | Degraded-mode decision (GH outage = factory stops) | Document the dependency or mitigate it |
| 5.4 | Golden-path demo repo exercising full loop including deploy | Onboarding proof, not slides |
| 5.5 | Merge-queue view wrapping `fleet_status` + `merge_pr --dry-run` | Operator visibility |

---

## 8. The Non-Goals: What NOT to Build

These are non-negotiable based on both your audit findings and the broader research:

1. **Supervisor / orchestrator agent.** The board IS the coordinator. An orchestrator process is a single point of failure and adds nothing the board doesn't already provide.
2. **MCP coordination server with file locking.** Duplicates `touches:`. Adds a process that must be running for work to happen.
3. **Replacing GitHub Issues with a local task file.** `tasks.md` is what Claude Code Agent Teams uses — it's strictly worse than your board. No durability across sessions.
4. **More lifecycle skills for coverage's sake.** Seven skills (+ triage) is enough. Add stages, not skill files.
5. **Risk-category human review or merge gates.** Money/PII/schema work strengthens evidence requirements (plan depth, test coverage, review rigor) — it does not change merge authority from mechanical to human.
6. **CI reviewer agent as a second review path.** Distinct-agent review is the path. A CI bot that just stamps PRs is theater.
7. **Automating the intent gate.** Direction is the one thing the factory must not decide. Humans define what to build; agents determine how.

---

## 9. Appendix: Source Index

### Academic Papers

| Paper | arXiv ID | Key Finding |
|---|---|---|
| SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | 2405.15793 | Interface design matters as much as model capability |
| An Empirical Study of Coordination Mode as the First-Class Citizen in From-Scratch Multi-Agent Coding | 2607.27877 | Topology rivals model capability in speed–cost–quality |
| ACEM: A Cost Estimation Model for Agentic Software Engineering | 2608.02582 | Three new cost dimensions: tokens, HITL, infra |
| The Productivity-Reliability Paradox: Specification-Driven Governance | 2605.01160 | Specification discipline, not model capability, is the binding constraint |
| Trust but Verify? Uncovering the Security Debt of Autonomous Coding Agents | 2607.12428 | 38.9% of agent PRs contain security smells |
| Early Adoption of Agentic Coding Tools by GitHub Projects | 2607.14037 | 25,264 agentic PRs analyzed; adoption is concentrated |
| AgentForge: An Immersive Role-Playing Platform for Learning Agentic Software Engineering | 2608.04148 | Four-role multi-agent code repair workflow |
| Code Is the Body: Agent-Owned Software Bodies for Recursive Evolution and Descent | 2607.28691 | Governed self-evolution with merge-under-human-control |
| Where Is the Cost of Third-Party API Routers in Agentic Software Development? | 2607.23624 | API routers occupy the trusted path; control gap is real |
| Why Are Agentic Pull Requests Merged or Rejected? | 2605.22534 | Only 35.7% of rejections were real agentic failures |

### Open-Source Projects

| Project | URL | Approach |
|---|---|---|
| delivery-loop | github.com/blakemartz/delivery-loop | Claude Code plugin; specs→backlog→claim→implement→review→patch→merge |
| software-factory | github.com/deepkawal/software-factory | 10-agent pipeline; enterprise-oriented; human-governed |
| SWE-agent | github.com/SWE-agent/SWE-agent | Princeton/Stanford; SoTA on SWE-bench |
| OpenHands Agent Canvas | github.com/OpenHands/OpenHands | Self-hosted multi-agent control center |
| jddelia/agentic-factory | github.com/jddelia/agentic-factory | Codex plugin with SQLite ledgers |
| repoach | github.com/repoachhq/repoach | Autonomous PR review + merge pipeline |

### Industry Guidance

| Source | Key Takeaway |
|---|---|
| Anthropic: Steering Claude Code (hooks, skills, subagents) | "A real guardrail needs to be deterministic — hooks and permissions, not prose" |
| GitHub Spec Kit | Specify → Plan → Tasks → Implement is the consensus pattern |
| OpenHands: Claude Code Best Practices | Agent-Client Protocol for vendor-agnostic orchestration |
| The New Stack: "85% say code review is the new bottleneck" | Review is now the acknowledged constraint |

---

## Bottom Line

You already built the hard part — durable multi-agent coordination with path budgets and a real merge gate. Most public setups skip straight to "agent writes code" and discover these problems in production.

The factory becomes complete when:

1. **Every guardrail is a mechanism** (hooks, not prose)
2. **Intake turns ideas into dependency-aware Ready work** without you hand-writing every issue
3. **Review is mechanically invoked** with a distinct agent, not waiting for human attention
4. **Ship continues past merge** into preview/prod with smoke checks
5. **Telemetry tells you** which stage is the real bottleneck next week

The research confirms: your architecture is sound, your problem analysis is correct, and your build sequence is right. The work ahead is not redesign — it's hardening what you already designed.

---

*Document compiled by Hermes Agent for Aru (@arugil) — August 11, 2026*
*Sources: 10 arXiv papers, 6 open-source projects, Anthropic/GitHub/OpenHands engineering guidance, Aru_Agentic_SDLC v0.1 audit*
