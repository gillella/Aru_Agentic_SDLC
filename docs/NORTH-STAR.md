# North star: ideation to deploy, with a small kernel

This file is destination memory. It is **not** an operating contract, not a
kernel component, and not an eighth operating document.

The seven operating files remain `README.md`, `AGENTS.md`,
`docs/KERNEL-CONTRACT.md`, `docs/ENFORCEMENT-REGISTER.md`,
`docs/OPERATIONS.md`, `docs/DEGRADED-MODE.md`, and `CHANGELOG.md`.
`tests/test_surface.py` excludes this file (and `docs/AUDIT-*.md` evidence)
from that set. Do not copy policy from here into runtime, hooks, or merge
gates.

No kernel runtime behavior is changed by this file. It does not implement the
phases below.

## 1. Destination

In the agent-coding era, many agents will produce software. That only works if
there is a fail-closed governance layer that answers four questions for every
change:

1. What work is approved?
2. Who may change which paths?
3. What evidence is required to merge?
4. Who may ship, and on what evidence?

Aru's job is that layer, plus the scaffolding that lets a new project adopt it.
Aru is not the coding agent, not the product manager, not the CI vendor, not
the deploy platform, and not the process that wakes the next command.

The destination is: a project can take an idea through a governed Ready gate,
implement it under exclusive claim and a path budget, merge only with
exact-head focused local verification and one distinct reviewer, and later
require consumer-owned deploy evidence before promotion — without Aru owning
scheduling, runtime, or production.

Today the kernel covers one approved GitHub issue through one safely merged
pull request. That is the foundation, not the whole destination.

## 2. Four layers

Progress is completing a phase. It is not growing `scripts/create_pr.py`,
adding a fifteenth command, or putting an orchestrator inside this repository.

| Layer | Question it answers | Where it lives |
| --- | --- | --- |
| **Kernel** | What is authorized, blocked, and evidenced for one issue → one merge? | This repository. Small, fail-closed, no daemon. |
| **Orchestration** | Who wakes the next kernel command, and when? | Outside this repository: a human cadence, cron, Hermes, or another agent loop. |
| **Scaffolding** | How does a new or existing project adopt the kernel and hold the workflow? | Init, skills, templates, consumer verification/runbooks. May live here as copies; truth of "it works" is a consumer repo. |
| **Deploy** | May this revision ship, roll back, or promote? | Consumer-owned ship and runtime. The kernel may later *require evidence* for those stages. Aru is not a deploy platform. |

Mixing layers is how the pre-v0.2 factory got large. A new kernel *component*
still needs the KERNEL-CONTRACT admission rule: it must authorize or block a
lifecycle transition, no simpler GitHub/Git/`gh`/test/document must suffice,
and it needs repeated evidence from three governed consumer repositories. A
vision file is not a component. An orchestrator is not a component of this
repo. A deploy pipeline is not a component of this repo.

## 3. Why we shrank

v0.2.0 deleted the factory — Hermes loop, scheduler, visualizer, reaper, Slack
bridge, preview, presence, handoff, and the rest — and kept a kernel that
moves one approved issue to one merge. That reset is a lesson, not amnesia
about the destination. The factory made ideation-to-deploy *feel* closer while
making the contract untrue: too many surfaces, a second lifecycle, and
authority that could not be explained as GitHub plus a small helper. The long
goal remains. The way to reach it is to keep the kernel as the authority
layer and compose orchestration, scaffolding, and deploy *around* it, not
inside it. The pre-reset tree is preserved at tag `pre-v0.2.0-2026-08-27`
(`388a22b3183e523ee67f857979448d2124e1a854`) as history, not as a menu of
surfaces to restore into this repo.

## 4. How to progress

1. **Correctness first.** The v0.2 feature freeze period has concluded, so
   correctness work is no longer restricted to security and correctness fixes
   only; the repository again accepts normal feature work.
2. **Do not skip Phase 1.** A slogan that GitHub will not enforce is not a
   platform. Close the leaky-contract holes before adding intake, richer
   scaffolding, or an external loop.
3. **Do not reintroduce pre-v0.2 factory surfaces into the kernel.** No
   scheduler, daemon, private queue, Slack, dashboard, visualizer, reaper,
   preview stack, `aru code` loop, or extra review provider belongs here.
   If stalled work hurts, the answer is a human or external cadence that
   *calls existing commands*, not a new in-repo waiter.
4. **New kernel surfaces need consumer-repo evidence.** Three governed
   consumer repositories, plus an explanation of why GitHub, Git, `gh`, a
   test, or a document cannot do the job more simply. Scaffolding quality is
   proven on a consumer, not by expanding templates until they look complete.
5. **Do not grow the reviewer machine as a substitute for progress.**
   `create_pr.py` is already near the 800-line file cap. Completing a phase
   means observable evidence in GitHub and consumer repos, not a larger
   assignment helper.

Known leaks of the fail-closed slogan (UI merge still allowed, `touches:`
parser split, path budget hook-only, docs vs external-first reviewer, stale
version line) are recorded in PR #522 / `docs/AUDIT-2026-08-28.md` on branch
`cursor/kernel-governance-audit-fd04`. Cite that audit; do not reopen it here.

## 5. Phases

Each phase has a goal, in-scope work, explicit out-of-scope, an admission
rule, and an exit test. Later phases do not begin because someone is bored
with the kernel. They begin when the previous phase's exit test is true.

### Phase 0 — Kernel (now)

**Goal.** Keep one issue → one merge as the only in-repo product. Record the
destination so later work does not treat the v0.2 reset as the end of the
story.

**In scope.**

- Security and correctness of the existing lifecycle commands, hooks, and
  gates.
- Evidence files (`docs/AUDIT-*.md`, this file) that are excluded from the
  seven-document operating budget.
- Operator hygiene that is not a new kernel surface (leftover branch
  deletion, label cleanup, truthful version/changelog as correctness).

**Out of scope.**

- Scheduler, daemon, queue, Slack, dashboard, deploy, preview, extra review
  providers.
- An eighth operating document.
- Phase 2–5 product work in this repository.
- Growing `create_pr.py` to "feel more complete."

**Admission.** Already admitted: this is the current product after the v0.2.0
reset. HEAD at the time this file was written is the merge of PR #521
(`311ed91`).

**Done when.**

- The v0.2 feature freeze ran its course (security/correctness only while it
  was in force) and has concluded.
- This north-star file is tracked and excluded from the operating-document
  budget.
- Factory surfaces listed in `tests/test_surface.py` remain absent.
- Remaining fail-closed leaks are either fixed as correctness or explicitly
  listed as Phase 1 remaining work (PR #522 is that list; do not duplicate it
  into an operating doc).

### Phase 1 — The contract is real

**Goal.** The documented fail-closed chain is true on GitHub, not only in
helpers and local hooks. A consumer repo has completed one golden-path walk
on the v0.2 contract.

**In scope.**

- GitHub ruleset (or App-only merge) such that `scripts/merge_pr.py` is
  actually exclusive: UI squash/rebase/merge without the Aru gate is refused.
- One `touches:` parser shared by hook and `common.py`; path budget re-checked
  at PR create and merge, not only by a local pre-push hook.
- Operating docs and version/changelog that match the machine (including
  external-first reviewer assignment after PR #519).
- A completed golden-path demo on a consumer repository (the intended public
  example is `gillella/aru-golden-path-demo`; audit #522 found it stalled on a
  pre-reset contract — finish or rewind it there, not by adding kernel
  features).

**Out of scope.**

- Scheduler or any in-repo waiter for stalled review.
- New review providers.
- Ideation/intake product, deploy product, or inventing a kernel-owned runner
  in place of consumer-owned exact-head focused local verification bound by
  the sanctioned Factory helper.
- Treating ruleset JSON in this repo as a substitute for applying the ruleset
  on GitHub.

**Admission.** Phase 1 begins only when Phase 0's exit test is true. Security
and correctness of the *existing* claim (exclusive merge, path budget, parser
parity, truthful docs/version) belong to Phase 0. Phase 1 *feature* work is
not admitted until those are done. Do not start Phase 2 because Phase 1 feels
slow.

**Done when.**

- An operator with write access cannot merge to the default branch except
  through the documented Aru gate (ruleset or App evidence, not a README
  sentence).
- `create_pr.py` and `merge_pr.py` refuse a diff outside the linked issue's
  `touches:` (including deletes).
- Hook and `common.py` accept the same `touches:` forms.
- README/CHANGELOG version identity matches tags and HEAD.
- One consumer repository has a merged PR that used claim → worktree →
  exact-head focused local verification → one distinct reviewer →
  `merge_pr.py`, with no preview-era leftover in that walk.

### Phase 2 — Ideation to Ready

**Goal.** "Should we build this?" is a governed gate. Intake, spec, and
issue-contract scaffolding get an idea to a valid Ready issue. A human or an
external agent still invokes every command.

**In scope.**

- Templates, skills, and checklists that turn an idea into an issue with
  `## Acceptance Criteria`, `touches:`, and no open `depends-on: #N`.
- Optional spec/intake documents that live in the *consumer* repo and whose
  Ready promotion still goes through `triage_backlog.py`.
- Teaching agents not to execute untrusted issue text.

**Out of scope.**

- A kernel daemon that files issues from Slack, mail, or a product-manager
  loop.
- Aru deciding product priority.
- A second lifecycle store "for ideas."
- Skipping Phase 1 because intake would be more visible.

**Admission.** Phase 1 exit tests are true. Any new kernel command or skill
still needs the KERNEL-CONTRACT admission rule and three consumer repos. Prefer
documents and GitHub issue forms over a new script.

**Done when.**

- A new idea can be taken to Ready on a consumer repo using published
  scaffolding, and `triage_backlog.py` is the only promotion mechanism.
- Invalid contracts still fail closed.
- No new in-repo scheduler exists.

### Phase 3 — Implementation scaffolding

**Goal.** Adopted projects carry honest project-specific focused verification
commands, and the sanctioned Factory helper binds that local evidence to the
exact head while worktrees plus skills hold up under ordinary agent use. The
kernel still has no daemon or state store.

**In scope.**

- Consumer-owned exact-head focused local verification whose commands are the
  project's honest lint/test/build slice, not the bootstrap
  `python3 -m compileall` baseline, and whose evidence is executed and bound
  by the sanctioned Factory helper rather than a repository workflow gate.
- Skills and worktree helpers that remain correct when hooks are installed,
  `ARU_SDLC_HOME` is set, and agents follow `implement-next-issue`.
- Bootstrap honesty: init does not claim a repo is governed until reviewers,
  Project Board, and honest project-specific focused verification commands
  exist (checklist already says this; make the generated output match).

**Out of scope.**

- Repository workflow gates, Actions-required merge gates, Aru-owned runners,
  preview stacks, or a 15th kernel command that "runs the consumer tests."
- Reintroducing a factory visualizer or worker fleet so scaffolding "feels
  attended."
- Any kernel daemon or state store that tracks verification or review progress,
  and kernel changes whose only evidence is a template edit with no consumer
  walk.

**Admission.** Phase 1 is done. Phase 2 may overlap if intake scaffolding is
document-only. Kernel code changes require three consumer repos that actually
used the change.

**Done when.**

- At least one governed consumer records exact-head focused local verification,
  executed by the sanctioned Factory helper, using honest project-specific
  commands rather than the bootstrap `python3 -m compileall` baseline or a
  repository workflow gate.
- One full implement-next-issue walk on that consumer succeeds without
  undocumented operator rescue.
- Kernel command count, skill count, and operating-document count still meet
  the v0.2 budgets unless a later admitted change revises the contract.

### Phase 4 — Closed-loop operation

**Goal.** Optional closed-loop operation exists *outside* this repository. An
orchestrator (Hermes, cron, a human cadence, or another agent product) only
calls kernel commands. The kernel still has no scheduler.

**In scope.**

- An external loop that invokes `fetch_next_work.py`, `claim_issue.py`,
  `create_pr.py`, `check_ci.py`, `merge_pr.py`, and the other supported
  commands.
- Documentation here that names the boundary: orchestration may retry, wait,
  and wake; it may not invent lifecycle state when GitHub is unavailable.

**Out of scope.**

- Any scheduler, daemon, presence registry, private queue, or capacity ledger
  in *this* repository.
- Restoring `run-aru-factory`, the Hermes-in-kernel loop, or the reaper as
  kernel surfaces.
- New kernel APIs created only to make an orchestrator prettier. If the
  orchestrator needs a new command, that command must pass Phase 1-style
  admission as a kernel component.

**Admission.** Phases 1 and 3 are done (the contract is real and a consumer
can implement under it). Phase 2 should be done unless the loop only consumes
already-Ready issues. Evidence: at least one external orchestrator repository
or operator runbook, plus three consumer repos that were driven that way
without kernel patches that reintroduce a waiter.

**Done when.**

- A closed-loop run can take Ready work to merge by calling published kernel
  commands plus the consumer's focused local verification commands.
- Deleting the orchestrator leaves the kernel fully usable by a human.
- `tests/test_surface.py` still forbids the factory filenames and retains
  the requirement that `.github/workflows` be absent in this repo.

### Phase 5 — Ship governance

**Goal.** Deploy, rollback, and promotion are consumer-owned stages. The
kernel may require evidence that those stages happened. Aru does not ship
software, host previews, or own production.

**In scope.**

- Issue/PR contract fields or merge-gate evidence that a consumer-defined
  deploy/rollback/promotion check passed, when a consumer opts into that
  stricter gate.
- Clear authority: consumer repository decides *how* to ship; Aru only
  blocks merge-or-promote when the consumer asked it to require that
  evidence.

**Out of scope.**

- Aru as a deploy platform, release manager, preview stack, smoke runner, or
  incident system.
- Kernel-owned environments, secrets, or production credentials.
- Collapsing Phase 5 into "add `release.py` to this repo" (that filename is
  forbidden by surface tests for a reason).

**Admission.** Phase 1 is done. At least three governed consumer repositories
already deploy by their own pipelines and can show what evidence a kernel gate
would read. Explain why GitHub Environments, required checks, or a consumer
workflow cannot carry the gate without a new kernel surface.

**Done when.**

- A consumer can require deploy/rollback/promotion evidence through Aru
  without Aru executing the deploy.
- Removing Aru does not take down the consumer's ability to ship (it only
  removes the governance gate).
- This repository still has no release/deploy/preview runtime.

## 6. What this file must never become

- A schedule. There are no target weeks and no calendar gate for any phase;
  phases advance on exit tests, not dates.
- A backlog. File GitHub issues for work; do not grow this file into a queue.
- A substitute for KERNEL-CONTRACT. If a rule must bind agents or merge,
  it belongs in an operating document and a test, not here.
- Permission to re-grow the factory. The destination is ideation → deploy
  *governance*. The kernel stays small. Orchestration stays outside.
