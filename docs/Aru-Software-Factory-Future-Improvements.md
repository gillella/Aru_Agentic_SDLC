# Aru Software Factory — Future Improvements

**Audience:** you (factory owner / architect)  
**Date:** 2026-08-10  
**Companion:** [`PROCESS-AUDIT-2026-08.md`](PROCESS-AUDIT-2026-08.md) (baseline gaps); this doc is the *forward* roadmap after hardening landed.

> Governance note: independent review is performed by a distinct agent and
> merge execution is mechanical through `merge_pr.py`. Risk, diff size, and
> review count strengthen evidence but do not create human gates. Human
> intervention is reserved for a severe merge conflict or merge/close-out
> failure agents cannot safely resolve.

---

## 1. What you have built

Aru_Agentic_SDLC is not “an AI coding assistant prompt.” It is a **coordination and governance factory** for software work:

| Layer | Role today |
|---|---|
| **Board** | Durable shared state (Backlog → Ready → In Progress → In Review → Done) |
| **Skills** | Declarative procedures agents must follow (vendor-portable Markdown) |
| **Scripts** | Deterministic GitHub/git actions (claim, pick, PR, merge, triage) |
| **Hooks** | Mechanisms that *refuse* bad actions (`touches:` budget, no push to main) |
| **Fleet** | N isolated clones + one loop: feedback → review → implement → idle |

**Core insight (keep it):** the GitHub Project board *is* the orchestrator. Agents do not need a supervisor process; they ask `fetch_next_work.py` what to do next. That survives crashed sessions, wiped worktrees, and machine reboots in a way in-memory orchestrators do not.

**What recently hardened (audit → reality):**

- Deterministic `touches:` enforcement + `pre-push` main protection  
- `merge_pr.py` Definition-of-Done gate (CI, reviews, threads, acceptance, size)  
- `triage-backlog` skill/script (Ready contract + capacity)  
- Unified picker `fetch_next_work.py` (feedback / review / issue)  
- Cross-family review routing + author/reviewer stamps  
- Blocking CI template pieces (gitleaks / pip-audit in generated projects)  
- Cursor cross-repo install via `$ARU_SDLC_HOME`

You already own a strong **middle factory** (claim → isolate → implement → review → merge). The remaining work is the **front** (idea → trustworthy ready work) and the **back** (deploy → observe → learn).

---

## 2. Factory lifecycle map — coverage vs gaps

```
 Idea / PRD ──► Spec / Issues ──► Triage ──► Design gate ──► Implement
      │              │              │            │              │
   WEAK           ADEQUATE       BUILT        PARTIAL         STRONG
                                                                  │
 Deploy / Ops ◄── Release ◄── Merge gate ◄── Review ◄── Verify ◄─┘
    MISSING      WEAK         BUILT         BUILT      ADEQUATE
                                                                  │
                            Metrics / learning loop ──────────────┘
                                      MISSING
```

| Stage | Today | Gap class |
|---|---|---|
| Idea → product intent | Manual chat / ad-hoc issues | **Intake** |
| PRD / epic decomposition | No first-class skill | **Intake** |
| Issue quality | Templates + `touches` / `depends-on` | Solid |
| Triage Backlog → Ready | `triage_backlog.py` | Solid; still human-judgment heavy |
| Design before code | Prompt-level plan gate only | **Mechanism** |
| Parallel claim & isolation | Claim protocol + worktrees + fleet | Best-in-class |
| Local verify | Mandated; uneven evidence | Soft gate |
| CI | Playbook CI still light; template stronger | Align playbook ↔ template |
| Review | Claimable work + merge gate | Label bugs still in flight (#19, #24) |
| Merge / Done | `merge_pr.py` | Server-side protection still off (free plan) |
| Deploy / preview / prod | None | **Back of factory** |
| Test beyond unit | None (E2E, contract, load) | **Quality** |
| Ops / incident / rollback | One-line revert missing | **Ops** |
| Factory telemetry | None | **Learning** |

---

## 3. Principles for what to improve next

1. **Mechanisms over instructions.** Prefer hooks, CI jobs, and `merge_pr.py` checks over longer skill prose.  
2. **One process owner.** Aru owns lifecycle in governed repos; disable competing workflow owners (GSD resume, ralph-loop, etc.) or map them explicitly into Aru stages.  
3. **Risk strengthens evidence, not merge authority:** money/PII/schema plans,
   oversized diffs, and repeated review rounds stay inside the autonomous
   planning, testing, independent-review, and remediation loop.
4. **Do not rebuild the coordinator.** No supervisor agent, no MCP file-lock bus, no `tasks.md` migration.  
5. **Finish the spine before adding stages.** Open PRs that close review-gate holes (#19, #24, #27, #25) beat new skills.

---

## 4. Prioritized future improvements

### P0 — Make the current factory trustworthy (days)

| ID | Improvement | Why |
|---|---|---|
| **F0.1** | ~~Land in-flight review/stamp fixes (#19, #24, #27, #25)~~ | **Done** — review gate no longer falls open for bot reviews; claim/completion stamps are mechanical (verified 2026-08-15) |
| **F0.2** | GitHub Pro + rulesets on private app repos | Server-side: no direct main, required checks, required review |
| **F0.3** | Align *this* playbook’s `.github/workflows/ci.yml` with the hardened template | Factory dogfoods its own gates |
| **F0.4** | Close process-ownership doc (#7) | Agents stop getting contradictory process instructions |
| **F0.5** | `install_hooks.sh` as required bootstrap step in every governed clone | Enforcement must travel with the fleet |

### P1 — Front of factory: idea → Ready (1–2 weeks)

| ID | Improvement | Why |
|---|---|---|
| **F1.1** | `idea-to-prd` skill (or thin wrapper over your existing write-to-prd / grill flows) | Factory starts at *intent*, not at already-shaped issues |
| **F1.2** | `prd-to-issues` skill wired to board + phase + `depends-on` DAG | Epics become claimable work, not chat archaeology |
| **F1.3** | Plan gate as mechanism: `needs-design` / `type:feat` → require a durable post-and-proceed plan comment before `create_branch.py` succeeds; money/PII/schema increase required evidence | Spec discipline is the binding constraint; risk alone is not a human gate |
| **F1.4** | Triage “Ready depth” SLO + auto-nag when Ready &lt; fleet size | Idle fleets are a triage failure, not an agent failure |
| **F1.5** | Issue #20: create-github-issue always attaches to the project board | Orphan issues break the factory’s sole coordinator |

### P2 — Quality & review throughput (2–4 weeks)

| ID | Improvement | Why |
|---|---|---|
| **F2.1** | Evidence trail: local verify summary attached to PR body (commands + exit codes) | “Tests passed” without evidence is theater |
| **F2.2** | Architecture contracts in app repos (import-linter / boundary checks) | Parallel agents erode module boundaries silently |
| **F2.3** | Acceptance-criteria runner: parse issue checkboxes → map to verification commands | Turns decorative AC into a merge input |
| **F2.4** | Soft PR size already in `merge_pr.py`; add *split guidance* in triage for oversized scopes | Prevents 6-round review death spirals upstream |
| **F2.5** | Port CI `touches` check + model-routed reviewer into template (#11) | Fleet guarantees must ship with every new product |

### P3 — Back of factory: ship & learn (ongoing)

| ID | Improvement | Why |
|---|---|---|
| **F3.1** | `deploy-preview` skill (Vercel / Cloud Run / your host) gated after merge or on PR | Idea→implementation without deploy is half a factory |
| **F3.2** | Environment promotion skill: preview → staging → prod with explicit issue/PR trail | Keeps Issue-First Law past merge |
| **F3.3** | Smoke / E2E hook in CI for products that have a runnable surface | Unit green ≠ product works |
| **F3.4** | `fleet_status.py` one-screen ops: holders, PR age, review rounds, CI fail rate, Ready depth | You cannot improve what you cannot see |
| **F3.5** | Post-merge cleanup automation: prune merged worktrees, delete stale branches, release leftover labels | Done must mean clean |
| **F3.6** | Documented revert / incident path in `AGENTS.md` (one paragraph + script) | Factories need a reverse gear |
| **F3.7** | Cost & cycle-time metrics per issue (model family, review rounds, wall time) | Guides which work to automate vs keep human |

### P4 — Productization (when consulting / multi-repo)

| ID | Improvement | Why |
|---|---|---|
| **F4.1** | Versioned factory releases (`aru-sdlc@0.x`) with changelog | App repos pin a known governance version |
| **F4.2** | Stack packs: Node/TS, Python, Go CI + test + deploy templates | Init stops being Python-shaped |
| **F4.3** | Native skill discovery for Claude Code / Codex (symlinks from `$ARU_SDLC_HOME`) | Progressive disclosure without forking procedures |
| **F4.4** | Golden-path demo repo (beyond `dummy_calculator_app`) that exercises full loop including deploy | Onboarding proof, not slides |
| **F4.5** | Merge-readiness UI / CLI dashboard wrapping `fleet_status` + `merge_pr --dry-run` | Gives the operator visibility without making routine merge execution human-only |

---

## 5. Explicit non-goals (do not build)

- **Supervisor / orchestrator agent** — the board already coordinates.  
- **MCP coordination server with file locking** — duplicates `touches:`.  
- **More lifecycle skills for the sake of coverage** — finish mechanisms first; seven (+ triage) is enough until deploy/ops.  
- **Replacing GitHub Issues with a local task file** — worse durability and worse multi-agent safety.  
- **Risk-category human review or merge gates** — money/PII/schema work needs
  stronger evidence, not a different merge authority.

---

## 6. Recommended build sequence (factory owner view)

```
Week 0     Close review-gate bugs; Pro + rulesets; dogfood CI; process ownership
Week 1     Plan-gate mechanism; board attach on issue create; Ready-depth SLO
Week 2     PRD → issues intake; evidence trail on PRs; template port (#11)
Week 3–4   Deploy-preview + smoke; fleet_status; cleanup automation
Later      Stack packs; versioned releases; metrics; golden-path demo
```

**Operator loop once the spine is green:**

1. Intake intent → PRD → phased issues  
2. Triage to Ready (capacity ≥ fleet size)  
3. `launch_fleet.sh -n N`  
4. A factory agent runs `merge_pr.py` after a distinct review and all gates pass
5. Deploy skill promotes; `fleet_status` shows what to fix in the factory itself  

---

## 7. How to use this document

- Treat each **F\*** row as a candidate GitHub issue (or epic) under this playbook — Issue-First applies to the factory too.  
- Prefer one issue per mechanism (hook, script check, CI job), not one mega “improve factory” issue.  
- Re-audit quarterly against this map; move rows to a “Shipped” appendix rather than rewriting history.

---

## 8. Bottom line

You have already built the hard part most people skip: **durable multi-agent coordination with path budgets and a real merge gate.**  

The factory becomes complete when:

1. **Intake** turns ideas into dependency-aware Ready work without you hand-writing every issue,  
2. **Design** is enforced for the work that needs it,  
3. **Ship** continues past merge into preview/prod with smoke checks, and  
4. **Telemetry** tells you which stage is the real bottleneck next week.

Until then, protect the spine: fix open gate bugs, keep one process owner, and resist adding orchestration theater.
