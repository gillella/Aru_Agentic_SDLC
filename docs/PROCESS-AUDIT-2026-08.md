# Aru_Agentic_SDLC — Process Audit, August 2026

Audit of the framework as implemented (`$ARU_SDLC_HOME`) and as actually practiced
(`unum-catalog`), against current public practice in agentic SDLC.

Method: read all 7 skills, 9 helper scripts, docs, templates, and the framework's own
unit tests; inspected unum-catalog's board (34 issues), its one merged PR, its CI
workflow, git history, hooks, and agent settings; then compared against Anthropic's
steering guidance, GitHub Spec Kit, Claude Code Agent Teams, and 2026 empirical data on
agentic PR outcomes. Sources at the end.

---

## Verdict

The **coordination layer is genuinely strong** — better engineered than most public
multi-agent setups. The **enforcement layer does not exist**: every rule in this
framework is an instruction an agent chooses to follow, and there is no mechanism that
makes any of them true. The **review and triage stages are unmodeled**, and those are
precisely the two places 2026 field data says agentic workflows break.

| SDLC stage | State | Grade |
|---|---|---|
| Requirement → issue | Issue forms enforce acceptance criteria, verification, `touches`, `depends-on` | **Strong** |
| Triage (Backlog → Ready) | Unowned, manual, undocumented; already the throughput cap | **Missing** |
| Design / plan before code | No artifact, no gate | **Missing** |
| Claim / parallelism | Optimistic claim, read-back tie-break, path-conflict filter, stale reaping | **Strong** |
| Isolation | Worktrees, retry on lock contention, per-agent clones documented | **Strong** |
| Implementation | Procedure documented; nothing verifies compliance | **Adequate** |
| Local verification | Mandated in prose only; no hook, no evidence trail | **Weak** |
| CI gate | Lint non-blocking, tests skipped when absent — green means ~nothing | **Weak** |
| Code review | Skill exists but is wired to nothing; no reviewer, no merge gate | **Missing** |
| Merge | Authority undefined; no branch protection possible on current plan | **Missing** |
| Done / traceability | Stops at "In Review"; no acceptance verification, no cleanup | **Weak** |
| Feedback / metrics | None | **Missing** |

---

## What is genuinely good (do not rebuild these)

1. **The claim protocol.** Optimistic write → read-back → deterministic lowest-agent-id
   tie-break, with exit code 2 as a distinct outcome, is the correct design given GitHub
   offers no compare-and-swap. It is unit-tested (`test_claim_issue.py` asserts the loser
   removes only its own label and never touches shared status labels). Most public
   multi-agent setups coordinate through a `tasks.md` file; Claude Code's own Agent Teams
   feature does exactly that. **A GitHub issue board is strictly more durable** — it
   survives a crashed session, a wiped worktree, and a machine reboot. This is a real
   advantage over the mainstream pattern, not a lag behind it.

2. **`touches:` path-conflict detection.** The insight that `parallel-eligible` says
   nothing about two agents editing the same file — and that the fix is a declared write
   footprint — is the single most important idea in the framework. Public practice
   largely has not caught up to this; the common advice is still "use worktrees," which
   isolates the workspace but does nothing about the merge.

3. **Stale-claim reaping with three-way liveness proof** (idle time AND no open PR AND no
   remote branch) rather than a naive timeout.

4. **Issue quality.** Sampled #22 and #23: summary, background with parent link,
   checkbox acceptance criteria, an exact verification command, `touches`, `depends-on`,
   phase. This is better than most human-run backlogs.

5. **The framework tests itself.** Five test modules covering the claim protocol, path
   overlap, board selection, status updates, and bootstrap. Rare and worth preserving.

6. **Vendor portability.** Skills as plain Markdown procedures readable by Claude Code,
   Cursor, and Codex is a deliberate and correct call given your consulting constraint.

---

## Gaps, ranked by what will hurt first

### G1 — Every guardrail is an instruction; none is a mechanism

**Evidence in your setup:**
- `gh api .../branches/main/protection` → **403, "Upgrade to GitHub Pro"**. The repo is
  private on a free plan, so *no* branch protection or ruleset exists. "No direct pushes
  to main" and "CI green before merge" are currently unenforceable server-side.
- `.git/hooks/` contains only samples — no pre-commit, no pre-push.
- No `CODEOWNERS`, no `.pre-commit-config.yaml`.
- `~/.claude/settings.json` sets `skipDangerousModePermissionPrompt: true`, and
  unum-catalog's `.claude/settings.local.json` allowlists `Bash(git push *)`,
  `Bash(git commit *)`, `Bash(git checkout *)`.
- Branch for issue #30 was named `docs/30-current-state-gap-analysis`, not the mandated
  `docs/issue-30-<slug>`. This is not cosmetic: `fetch_next_issue.py` resumes sessions by
  matching `issue-(\d+)` against the branch name. That branch **cannot** be resumed by the
  picker. The rule was skipped and nothing noticed.

**What the field says:** Anthropic's own guidance is explicit — *"'Never do this' in
CLAUDE.md… is the wrong tool. Claude will follow the instruction most of the time, but
when under pressure, in a long session or an ambiguous situation, or due to a prompt
injection… the model can fail to follow a prompted rule. A real guardrail needs to be
deterministic, and the enforcement methods are hooks and permissions."* A `PreToolUse`
hook exiting with code 2 blocks the call outright.

**This is the top gap** because the entire fleet design assumes agents respect the
`touches:` budget. One agent that doesn't corrupts other agents' work, and you will find
out at merge time.

**Fix:**
- `PreToolUse` hook on `Edit|Write|Bash`: read the agent's claimed issue, block writes to
  paths outside its `touches:` declaration, block commits/pushes targeting `main`.
- A `pre-push` git hook in every clone — this catches Codex, Cursor, and a human at the
  keyboard, not just Claude Code.
- GitHub Pro is ~$4/month and unlocks rulesets on private repos. Required checks +
  required review + linear history for the cost of a coffee. Take it.

### G2 — Nothing sits between "issue" and "code"

There is no design artifact and no plan gate. `implement-next-issue` Step 5 is
"Formulate a minimal, targeted implementation plan" — held in the agent's head, reviewable
by nobody, lost on compaction.

**What the field says:** plan-then-execute is the consensus pattern across every 2026
practitioner guide. GitHub Spec Kit (90k+ stars) formalizes Specify → Plan → Tasks →
Implement. The strongest claim comes from a multivocal review of 67 sources: *"Specification
discipline, not model capability, is the binding constraint on AI-assisted software
dependability."* The same paper names the code review bottleneck and the context window
as the two amplifying mechanisms of the productivity–reliability paradox.

**Your situation is subtler than "you have no spec."** You have `PROJECT-PLAN.md` and
`CURRENT-STATE-GAPS.md` — project-level specs that are unusually good. What is missing is
the **per-issue design step for issues where the design is the hard part**: schema shape,
state machines, money semantics. Those are exactly unum-catalog's Phase 1+ issues.

**Fix:** add a plan gate to `implement-next-issue`, triggered by a `needs-design` label
(and default-on for `type:feat`): before any edit, the agent posts an implementation plan
as an issue comment — approach, files, schema/API deltas, test strategy, rejected
alternatives. For a solo operator, make it *post-and-proceed*, not *post-and-block*: you
get an artifact and a review surface without becoming the bottleneck. Add
`--require-plan-ack` for the money and schema paths where you do want to block.

### G3 — Review is unmodeled, and it is already your bottleneck

**Evidence:** PR #31 (865 additions) went through **six review rounds and 23 review
threads** before merge. The reviewer was `chatgpt-codex-connector` — a Codex bot wired up
outside the framework, which no skill or doc mentions. At merge, `reviewDecision` was
empty: no approving review, merged by you. Six `fix(review):` commits are in the history.
The `code-review` skill exists but nothing invokes it, assigns it, or requires it.

**What the field says:** review is now the acknowledged constraint. Reported 2026
telemetry: 98% more PRs merged with 91% longer review times and flat delivery metrics;
agentic PRs waiting ~5.3x longer for reviewer pickup. The empirical study of 11,048 closed
agentic PRs found rejection outcomes *overstate* agent error — only 35.7% of rejections
were real agentic failures, 31.2% were workflow constraints — which means an unmodeled
review process actively destroys good work.

**Now project that onto a 4-agent fleet.** Four agents × ~1 PR each per cycle, each
needing your attention, each capable of six review rounds. You are the merge gate and you
have a day job.

**Fix, in order:**
1. **Automate the first review pass in CI.** `anthropics/claude-code-action@v1` on
   `pull_request`, running your `code-review` skill's checklist as the prompt. Review
   starts within a minute of PR open instead of whenever you look. Single-digit dollars a
   month at your volume. Your Codex bot already does a version of this — either formalize
   it in the framework or replace it, but stop having an unowned reviewer.
2. **Define merge authority in writing.** Today it is undefined, which means it is you,
   implicitly, with no criteria.
3. **`merge_pr.py`** that refuses unless: CI green, ≥1 review with no unresolved threads,
   every acceptance checkbox ticked, branch rebased. Then merges, deletes the branch, and
   moves the board to Done. This is your enforceable gate while you have no branch
   protection — and it stays useful after you get it.
4. **Cap PR size.** Reported agentic PRs run ~2.6x larger than human ones; #31 was 865
   lines. Add a soft limit (say 400 lines) that requires splitting or an explicit waiver.

### G4 — CI is advisory, so "CI green" certifies almost nothing

From `.github/workflows/ci.yml`:
```yaml
ruff check . || echo "::warning::ruff reported findings"     # never fails
# tests: "if no tests present, skip"
# import-linter: "if no contracts configured, skip"
```
All three gates no-op on the current repo. PR #31's green check verified whitespace and
nothing else — the PR body says so honestly. The framework's stated law is "CI green
before merge; remediate rather than weaken gates," but the gate shipped pre-weakened.

**Fix:** drop `|| echo`; make ruff blocking. Replace "skip if no tests" with "fail if
`src/**` has content and `tests/**` does not." Land the import-linter contracts (#25)
*before* the first feature, as that issue's title already argues — architecture guards are
worth ten review comments each, and they are the only thing that will stop four parallel
agents from eroding your schema-per-module boundary.

### G5 — Definition of Done stops at "In Review"

No step verifies the acceptance criteria were actually met (the checkboxes are decorative);
`TRACEABILITY.md` is issue #19, still in Backlog; no post-merge cleanup — your working
copy is *still sitting on the merged branch* `docs/30-current-state-gap-analysis` at a
commit six behind what was merged; no documented revert path when a merged agent PR turns
out wrong.

**Fix:** DoD checklist enforced by `merge_pr.py` (above); auto-delete branch on merge;
`cleanup_worktrees.py` that prunes worktrees whose PR is closed; a one-paragraph revert
procedure in `AGENTS.md`.

### G6 — Triage is the real throughput cap, and nobody owns it

Board right now: 8 issues Ready and claimable, 12 in Backlog, 2 held. The picker cannot
hand out a Backlog issue, so **a 4-agent fleet drains the entire Ready column in about a
day** and then idles. There is no triage skill; `create-github-issue` handles one-off
creation, not promotion.

**Fix:** a `triage-backlog` skill + script that promotes issues by *verifying* the Ready
contract (acceptance criteria present and testable, `touches:` present and plausible,
`depends-on` resolved, phase assigned) and reports "N agent-days of Ready work available."
Run it before every fleet launch. Triage is also the right place for a human — it is
where judgment has the highest leverage per minute spent.

### G7 — Four process frameworks are installed and only one is acknowledged

Active in this project simultaneously: **Aru_Agentic_SDLC** (board governance),
**GSD** (writes `.planning/HANDOFF.json`; its SessionStart hook instructed me to run
`/gsd:resume-work` immediately, before your actual request), **superpowers** (mandates
brainstorming-first before creative work), **claude-mem** (memory), **ralph-loop**
(autonomous looping), plus the **Codex PR bot**. `HANDOFF.json` is an empty auto-checkpoint
— GSD is writing state nothing consumes.

Every one of these wants to own the workflow. An agent starting a session receives
contradictory process instructions before it reads your prompt. That is a correctness
problem for a governance framework whose whole value is that agents follow it.

**Fix:** decide the boundary and write it into `AGENTS.md` — my recommendation: Aru owns
the lifecycle (issue → claim → PR → merge), GSD is disabled in Aru-governed repos, and
superpowers' brainstorming maps to the G2 plan gate rather than running alongside it.
One process owner per repo.

### G8 — Skills are not discoverable as skills

They are Markdown procedures loaded because CLAUDE.md tells the agent to `Read` them.
That works and it is portable, but it forfeits progressive disclosure: Claude Code loads a
native skill's name + description at session start and the body only on invocation, and
re-injects invoked skills after compaction. Your router adds a hop and your full procedure
text competes for the same context budget as the work.

**Fix (low effort, keep both):** keep `$ARU_SDLC_HOME/skills/*/SKILL.md` as source of
truth, and symlink them into `.claude/skills/` in each governed repo — you already do the
equivalent for Cursor rules (`.cursor/rules/aru-agentic-sdlc.mdc`). Same source, native
discovery in Claude Code, no fork.

### G9 — No feedback loop

Nothing records cycle time, review rounds, CI failure rate, rework, or cost per issue.
Six review rounds on a docs PR is a signal, and you only know it because I went looking.
With a fleet running, you will have no idea which agent, issue type, or phase is burning
your time.

**Fix:** `fleet_status.py` — one screen: who holds what, PR ages, review rounds per PR,
CI pass rate, Ready-column depth. Derive it from `gh` on demand; do not build a database.

### G10 — No security gates, on a project that handles money and PII

CI has no secret scanning, no dependency audit. unum-catalog's domain — settlement,
payouts, customer PII, supplier anonymity — makes this a Phase-1 blocker, not a nicety.

**Fix:** `gitleaks` and `pip-audit` in CI now, while it is a three-line change. Add
"touches money or PII paths" as a label that forces human review in `merge_pr.py`.

---

## Recommended sequence

**Before you launch the fleet** (these are the ones that change outcomes):

| # | Action | Effort |
|---|---|---|
| 1 | Make CI gates blocking (ruff, tests-required-when-source-exists) | 15 min |
| 2 | `pre-push` git hook: refuse pushes to `main`; `PreToolUse` hook: refuse writes outside claimed `touches:` | 2 h |
| 3 | Buy GitHub Pro; enable ruleset: required checks + required review + no direct push to main | 15 min |
| 4 | `claude-code-action@v1` PR review workflow running the `code-review` checklist | 45 min |
| 5 | `merge_pr.py` with the DoD gate; document merge authority in `AGENTS.md` | 2 h |
| 6 | `triage-backlog` skill; promote enough issues to feed the fleet | 1–2 h |
| 7 | Resolve the framework collision (G7) in `AGENTS.md` | 30 min |

**Next:** plan gate for `type:feat` / `needs-design` (G2); import-linter contracts (#25)
ahead of any feature work; branch/worktree cleanup; symlink skills into `.claude/skills/`.

**Later:** `fleet_status.py`; gitleaks + pip-audit; PR size cap.

## What I would *not* add

- **A supervisor/orchestrator agent.** The board already is the coordinator, and it is
  durable across crashes in a way an orchestrator's context is not.
- **An MCP coordination server with file locking.** Publicly promoted, but it duplicates
  `touches:` and adds a process that must be running for work to happen.
- **Migrating to Claude Code Agent Teams' shared task list.** It is a `tasks.md`. You
  already have something better, and it is not vendor-portable — which your consulting
  work requires.
- **More skills.** Seven is enough; the gaps are gates and mechanisms, not procedures.

---

## Sources

Anthropic guidance
- [Steering Claude Code: when to use CLAUDE.md, skills, hooks, and subagents](https://claude.com/blog/steering-claude-code-skills-hooks-rules-subagents-and-more)
- [Automate actions with hooks — Claude Code Docs](https://code.claude.com/docs/en/hooks-guide)
- [Orchestrate teams of Claude Code sessions — Agent Teams](https://code.claude.com/docs/en/agent-teams)
- [anthropics/claude-code-action](https://github.com/anthropics/claude-code-action)

Empirical / research
- [Why Are Agentic Pull Requests Merged or Rejected? An Empirical Study (arXiv:2605.22534)](https://arxiv.org/abs/2605.22534)
- [The Productivity-Reliability Paradox: Specification-Driven Governance (arXiv:2605.01160)](https://arxiv.org/abs/2605.01160)
- [AgenticFlict: merge conflicts in AI coding agent PRs (arXiv:2604.03551)](https://arxiv.org/pdf/2604.03551)

Practice
- [Spec-driven development with AI — GitHub Blog](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/)
- [github/spec-kit](https://github.com/github/spec-kit)
- [85% say code review is the new bottleneck — The New Stack](https://thenewstack.io/merge-gate-coding-agents/)
- [AI Is Breaking Code Review — Codacy](https://blog.codacy.com/ai-breaking-code-review-how-engineering-teams-survive-pr-bottleneck)
- [10 Claude Code Best Practices for Agentic Coding — OpenHands](https://www.openhands.dev/blog/claude-code-best-practices-agentic-coding)
- [Don't rely on instructions, use Agent Hooks to enforce guardrails](https://zarar.dev/agent-hooks-deterministic-guardrails-for-ai-generated-code/)
