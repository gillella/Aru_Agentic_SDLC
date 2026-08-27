# Project Boards for Agentic Software Factories

**Which tracker should orchestrate humans + coding agents in 2026?**

**Date:** 2026-08-11
**Companion:** [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md)
**Issue:** #78

---

## 0. Short answer

| If your primary failure mode is… | Prefer |
|---|---|
| Context switching between planning and code; you want a **governed, programmable factory** next to git | **GitHub Issues + Projects** |
| Human–agent collaboration UX; startups/scale-ups; first-class agent delegation | **Linear** |
| Enterprise process, compliance, cross-org program management | **Jira (Cloud + Rovo)** |
| You already live in Azure DevOps and cannot move planning | **Azure Boards** (keep; pair repos with GitHub for strongest agents) |
| You want agile ceremony + sprint AI on top of GitHub | **ZenHub** (or keep GitHub Projects and DIY) |

**For Aru_Agentic_SDLC specifically: stay on GitHub Issues + Projects as the factory coordinator.** That is not nostalgia — it is the correct fit for a vendor-neutral, script-driven, Issue-First harness. Treat Linear as the strongest *adjacent* product if you ever want a polished product-ops surface that *delegates into* GitHub PRs — not as a replacement for the merge/CI spine.

There is no universal winner. There is a clear winner **per constraint**.

---

## 1. What “good for agentic development” actually means

Classic PM comparisons scored boards, Gantt, and reports. Agentic factories need different criteria:

| Criterion | Why it matters for agents |
|---|---|
| **Code adjacency** | Fewer sync seams = fewer broken `Closes #N` / status lies |
| **Agent as first-class actor** | Assign/delegate, mention, session state, activity timeline |
| **API / CLI / MCP surface** | Factory scripts and coding agents must claim, triage, stamp without humans |
| **Context quality** | Crisp issues with AC beat bureaucratic fields agents ignore |
| **Governance & audit** | Who did what; labels/stamps; compliance when throughput explodes |
| **PR / CI coupling** | Branch → PR → checks → Done must be mechanical |
| **Multi-agent identity** | Distinct author vs reviewer when many agents share one GitHub user |
| **Opinionation vs configurability** | AI amplifies process entropy; over-configurable trackers fossilize chaos |
| **Vendor lock of the coding loop** | Board that only works with one coding agent fights a multi-tool factory |

**Relocation of the bottleneck:** code is cheap; **coordination + verification** are expensive. The board is now the **control plane**, not a ticket dump.

---

## 2. Contenders scored for factory use

Scores are relative (1–5) for *agentic software factory* fitness, not general PM popularity.

| Contender | Code adjacency | Agent-native UX | Automation API | Governance | Multi-tool factory | Overall for Aru-like harness |
|---|---|---|---|---|---|---|
| **GitHub Issues + Projects** | 5 | 3 | 5 (`gh`, GraphQL, Actions) | 3–4* | 5 | **Best fit** |
| **Linear** | 4 (deep GitHub sync) | **5** | 5 (GraphQL, MCP, Agent API) | 3 | 4 | Best *agent UX* |
| **Jira Cloud + Rovo** | 3 (key sync friction) | 4 (Agents GA 2026) | 4 (rich, heavy) | **5** | 3 | Best *enterprise* |
| **Azure Boards** | 3–4 w/ GitHub repos | 3 | 4 | 4 | 3 | Keep if ADO-locked |
| **ZenHub** | 5 (lives in GitHub) | 3 | 3–4 | 3 | 4 | Sprint layer on GH |
| **ClickUp / monday.dev** | 2–3 | 3 | 3 | 3 | 2 | Weak for code factories |
| **Height / Plane / Shortcut** | 2–3 | 2–3 | 3 | 2 | 2 | Niche / secondary |

\*GitHub governance improves with Pro/Enterprise rulesets, required checks, Apps for separate reviewer identity — which Aru already plans (S0.7 / S0.7a).

---

## 3. Deep dive — the big three

### 3.1 GitHub Issues + Projects

**What it optimizes:** minimum distance from **intent → branch → PR → CI → merge**.

**Why it fits agentic factories:**

- Planning object and code object share one world (Issues, Projects, PRs, Actions, Copilot, Codespaces).
- Sub-issues, dependencies, custom fields, Project workflows, GraphQL, and `gh` make it **programmable infrastructure** — exactly what harness engineering needs.
- Copilot coding agent (and many others) can take an issue and open a PR in-platform.
- Aru already encodes Issue-First, board status, claim scripts, and `merge_pr.py` on this substrate.

**Weaknesses:**

- Less polished product-ops UX than Linear.
- Agent *delegation* model is less first-class than Linear’s app-user / delegate design (you simulate identity with labels like `author:` / `reviewed-by:`).
- Cross-functional enterprise program management still trails Jira.
- You assemble some workflow yourself (that is a feature for a factory owner).

**Best when:** engineering is the center of gravity; git host is GitHub; you want mechanisms over ceremonies; multiple coding agents (Cursor, Codex, Claude Code, Antigravity, Copilot) must share one contract.

**Aru verdict:** **Keep as coordinator.** Do not migrate the spine for fashion.

### 3.2 Linear

**What it optimizes:** speed, issue hygiene, and **humans + agents as coworkers**.

**Why it is the strongest agent-native PM product in 2026:**

- Agents are installable **app users**: mention, delegate, project membership; human remains owner/assignee while agent is delegate ([Linear Agents docs](https://linear.app/docs/agents-in-linear)).
- First-party Agent Session API (Developer Preview): webhooks, `promptContext`, activity timeline, guidance markdown for conventions ([Linear Developers — Agents](https://linear.app/developers/agents)).
- Directory of coding agents: Cursor, Codex, Copilot, Devin, Factory, Warp, Tembo, etc. ([linear.app/agents](https://linear.app/agents)).
- MCP access (Business+), Triage Intelligence, Code Intelligence, Coding Sessions.
- Opinionated taxonomy → cleaner context for agents (agents hate Jira field mazes).

**Weaknesses:**

- Still an **integration** to GitHub for the real merge/CI truth — sync can drift.
- Weaker classic agile metrics (burndown/velocity) than Jira/ZenHub.
- Less suitable as sole system of record for regulated multi-department enterprises.
- Building Aru’s Definition-of-Done on Linear means re-implementing claim/merge gates against Linear’s API — a second spine.

**Best when:** product-led teams want agent delegation UX now; willing to treat GitHub as execution and Linear as intake/ops; not yet invested in a GitHub-scripted harness.

**Aru verdict:** **Best future *intake / operator* surface** if you need polished agent delegation — but wire it *into* GitHub Issues (create/sync issues), do not replace `merge_pr.py` / Projects as authority.

### 3.3 Jira Cloud + Rovo

**What it optimizes:** organizational complexity, auditability, cross-functional process.

**Why enterprises pick it for AI era:**

- Rovo / Agents in Jira (GA path through 2025–2026): assign work to Rovo or third-party agents; actions logged on the work item.
- Deep workflow engines, permissions, multi-site, compliance posture.
- Confluence adjacency for specs and decision logs (intent corpus).
- Automation is mature (sometimes Marketplace-dependent for “AI sprint generation”).

**Weaknesses:**

- GitHub linking via issue keys is brittle compared to native GitHub Issues.
- Configurability + AI ticket generation = **entropy amplifier** without ruthless process discipline.
- Heavier cognitive load → worse default context for coding agents.
- Seat/process cost often exceeds engineering-only shops.

**Best when:** security/compliance/IT/support must share the same work OS; formal change control; you already run Atlassian.

**Aru verdict:** Use as **enterprise upstream** (intake from Jira → mirrored GitHub issues) if a customer mandates it. Do not make Jira the merge authority for an agent coding factory.

---

## 4. Other boards (when they matter)

### Azure Boards
Microsoft’s path to agentic AI increasingly says: **repos on GitHub** for full Copilot agent power; Boards can stay in ADO. Copilot can be assigned from Azure Boards. Fine for ADO-locked orgs; weaker as a greenfield agent factory than GitHub Issues.

### ZenHub
GitHub-native sprint ceremony (velocity, planning poker, auto sprints). Useful if you want Scrum optics without leaving GitHub. Optional layer; not required for Issue-First factories that use “bolts” instead of two-week sprints.

### ClickUp / monday.dev
Strong general work management and sprint AI marketing; **weak code-native factory spines**. Extra translation layer to PRs/CI. Skip for Aru-style harnesses.

### Plane / Height / Shortcut / Asana
Capable trackers; thinner agent platforms and weaker “coding agent as coworker” ecosystems than Linear/GitHub in 2026. Not first choice for a software factory control plane.

---

## 5. Decision framework (use this, not vibes)

```
Is GitHub already the system of record for code + CI?
├─ YES → Is your factory scripted (claim, touches, merge gates)?
│        ├─ YES → GitHub Issues + Projects (Aru default)
│        └─ NO  → GitHub Issues still; or Linear if you want agent UX first
└─ NO  → Where must planning live politically?
         ├─ Atlassian estate → Jira; mirror issues to GitHub for agents
         ├─ Azure DevOps estate → Azure Boards; prefer GitHub repos
         └─ Greenfield product team → Linear (+ GitHub for PRs)
```

### Anti-patterns

| Anti-pattern | Why it hurts agents |
|---|---|
| Jira + GitHub + Linear all “source of truth” | Triple sync; status lies; agents update the wrong system |
| Board with no API-first claim protocol | Race conditions; double implementation |
| Choosing Linear *only* for aesthetics while keeping Aru scripts on GitHub without a bridge | Two coordinators |
| Migrating mid-factory for agent marketing features | Breaks harness; months of rewrite |
| Expecting the board to replace CI/merge gates | Boards coordinate; they do not verify code |

---

## 6. Recommendation for *your* software factory

Given:

1. You already built Aru around **GitHub Project board as orchestrator**.
2. You run **multiple coding agents** under one governance contract.
3. You care about **mechanical Definition of Done**, worktrees, and `Closes #N`.
4. You said boards are pluggable later, GitHub first.

**Do this:**

1. **Keep GitHub Issues + Projects as the factory control plane** (Ready → Done, picker, merge gate).
2. **Invest in GitHub-side agent identity** (GitHub App / second account for reviewers — your S0.7a) rather than switching trackers to get cleaner assignee semantics.
3. **Optionally evaluate Linear later** as an *operator console / intake* that creates GitHub issues (one-way or sync), if agent-delegation UX becomes the binding product need — after the spine is trustworthy.
4. **Only adopt Jira** when an enterprise customer or compliance regime forces it; mirror into GitHub for execution.
5. **Abstract the board behind Aru scripts** (`fetch_next_work`, `claim_issue`, `update_issue_status`) so a future Linear/Jira adapter is a backend swap, not a religion change.

### One-line strategy

> **GitHub Issues for the factory spine. Linear if you need the best agent coworker UX on top. Jira when the organization is the product. Never two spines.**

---

## 7. Sources

- [Linear vs Jira vs GitHub Issues for AI development (Octopus Builds, 2026)](https://octopusbuilds.com/blog/linear-vs-jira-vs-github-issues-ai-driven-development)
- [Augment — AI sprint planning tools comparison](https://www.augmentcode.com/tools/ai-sprint-planning-tools-jira-linear-github)
- [Linear — AI Agents in Linear](https://linear.app/docs/agents-in-linear)
- [Linear — Agents platform](https://linear.app/agents)
- [Linear — Developers: Agents API](https://linear.app/developers/agents)
- [Linear — GitHub Copilot integration](https://linear.app/integrations/github-copilot)
- [GitHub Copilot Agents](https://github.com/features/copilot/agents)
- [Azure DevOps + GitHub path to agentic AI](https://devblogs.microsoft.com/devops/azure-devops-and-github-repositories-next-steps-in-the-path-to-agentic-ai/)
- Aru local truth: [`ARU-SOFTWARE-FACTORY.md`](ARU-SOFTWARE-FACTORY.md) (“the board is the orchestrator”)
