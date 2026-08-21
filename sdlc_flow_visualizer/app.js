// line-ceiling: 468
/**
 * Aru Agentic SDLC — Interactive Graphical Visualizer Logic
 * Provides component metadata, drawer inspection, search filtering,
 * view mode switching, and animated idea flow simulation.
 */

document.addEventListener('DOMContentLoaded', () => {
  // Component Knowledge Base
  const NODE_DETAILS = {
    idea: {
      tag: 'Trigger',
      title: 'Raw Idea & Feature Intent',
      subtitle: 'Intake Trigger',
      description: 'The SDLC originates from human operator intent, customer requirements, or bug reports. Direct code edits without an issue origin are strictly forbidden by the Issue-First Law.',
      code: 'Follow skills/create-github-issue/SKILL.md',
      rules: [
        'No code edit or refactor may occur without a tracked issue.',
        'Issue must define summary, background context, and clear acceptance criteria checkboxes.',
        'Must specify declared touches: paths and depends-on: DAG tags.'
      ],
      remediation: 'If intent is underspecified, idea-to-prd marks the draft BLOCKED, keeps its questions visible, and stops before GitHub publication until the operator explicitly approves the exact draft.'
    },
    prd: {
      tag: 'Skill: idea-to-prd',
      title: 'PRD & Architecture Spec',
      subtitle: 'Shipped intake capability — issue #102 closed',
      description: 'The shipped idea-to-prd skill wraps Grill-Aru discovery and preserves product decisions before implementation. After explicit operator approval, it publishes the exact approved PRD as a governed Backlog artifact.',
      code: 'skills/idea-to-prd/SKILL.md',
      rules: [
        'Defines clear boundaries for what is in-scope and out-of-scope.',
        'Identifies money, PII, schema, or migration risks early.',
        'Requires clear, testable verification commands.'
      ],
      remediation: 'An approved BLOCKED PRD may be published to Backlog for visibility, but its blocking questions remain explicit and decomposition waits for READY_FOR_PLANNING.'
    },
    dag: {
      tag: 'Skill: prd-to-issues',
      title: 'Issue DAG Generator',
      subtitle: 'Shipped decomposition capability — issue #103 closed',
      description: 'The shipped prd-to-issues skill decomposes an approved PRD into small governed issues with explicit depends-on links, repository-backed touches, and computed parallel eligibility.',
      code: 'skills/prd-to-issues/SKILL.md',
      rules: [
        'Declares touches: src/a/*, tests/b.py footprint so the picker detects path collisions.',
        'Establishes dependency ordering so dependent tasks stay blocked until parent PRs merge.',
        'Keeps each issue small enough for independent review.'
      ],
      remediation: 'If touches: is empty, the issue picker refuses to offer it to any agent until triaged.'
    },
    triage: {
      tag: 'Script: triage_backlog.py',
      title: 'Backlog Quality & Triage',
      subtitle: 'Automation Script: triage_backlog.py',
      description: 'Automated triager that evaluates open Backlog issues against the Ready contract: checks that acceptance criteria exist, touches: is declared and non-empty, and dependencies are resolved.',
      code: 'python3 scripts/triage_backlog.py --capacity',
      rules: [
        'Promotes Backlog → Ready only when all metadata is complete.',
        'Reports claimable capacity after dependency and path-conflict filtering.',
        'Prevents un-triaged issues from reaching the picker.'
      ],
      remediation: 'If an issue lacks acceptance criteria, it remains in Backlog with a diagnostic warning.'
    },
    picker: {
      tag: 'Script: fetch_next_work.py',
      title: 'Multi-Task Priority Picker',
      subtitle: 'Automation Script: fetch_next_work.py',
      description: 'The single multi-task queue picker for fleet agents. Resolves work in strict priority order: 1. Feedback > 2. Merge > 3. Review > 4. Issue. Finishing beats starting!',
      code: 'python3 scripts/fetch_next_work.py --agent agent-1 --family anthropic --claim',
      rules: [
        'Enforces finishing in-flight work before starting new issues.',
        'Routes cross-family reviews to avoid model blind spots.',
        'Evaluates dependency DAG and touches: path collisions across active claims.'
      ],
      remediation: 'If a claim race occurs (exit code 2), the picker automatically tries the next eligible candidate.'
    },
    claim: {
      tag: 'Script: claim_issue.py',
      title: 'Optimistic Claim & Tie-Break',
      subtitle: 'Automation Script: claim_issue.py',
      description: 'GitHub lacks compare-and-swap on issue assignment, so claiming writes the agent:<id> label, reads back the issue state, and resolves races deterministically using agent ID sorting.',
      code: 'python3 scripts/claim_issue.py --issue 104 --agent agent-1',
      rules: [
        'Lowest-sorting agent ID wins claim races.',
        'The loser automatically removes its label and backs off.',
        'Updates board status to status:in-progress.'
      ],
      remediation: 'The designated janitor adds --reap-after 4 to picker cycles to release abandoned claims.'
    },
    worktree: {
      tag: 'Script: create_branch.py',
      title: 'Worktree Isolation',
      subtitle: 'Automation Script: create_branch.py',
      description: 'Creates a clean, isolated git worktree inside .worktrees/feat-issue-X to keep the main clone pristine. Retries on .git/index.lock contention.',
      code: 'python3 scripts/create_branch.py --issue 104 --type feat --worktree',
      rules: [
        'All implementation and review edits happen under .worktrees/.',
        'Direct edits on main are strictly forbidden.',
        'Worktree names follow standard conventions: feat/issue-X-slug.'
      ],
      remediation: 'If git lock contention occurs during creation, the script uses exponential backoff to retry.'
    },
    plangate: {
      tag: 'Gate: Plan Gate',
      title: 'Post-and-Proceed Plan Gate',
      subtitle: 'Mandatory Plan Gate',
      description: 'Before making the first edit for a feature (or money, PII, schema, migration task), the agent posts a durable ## Implementation Plan comment on the issue.',
      code: 'gh issue comment 104 --body-file .worktrees/plan.md',
      rules: [
        'Post-and-proceed: agent posts the comment and immediately proceeds without waiting for human approval.',
        'Must outline files to edit, schema deltas, test strategy, and rejected alternatives.',
        'If scope changes materially during execution, an amended plan comment is posted.'
      ],
      remediation: 'If product intent is missing, the issue is left blocked for operator clarification.'
    },
    implementation: {
      tag: 'Hook: enforce_touches.py',
      title: 'Source Edit & Path Guard',
      subtitle: 'PreToolUse Guardrail Hook',
      description: 'Deterministic PreToolUse hook that intercepts every file edit or shell write command, verifying the target file is inside the issue\'s declared touches: footprint.',
      code: 'hooks/enforce_touches.py (PreToolUse Hook)',
      rules: [
        'Blocks writes outside declared touches: paths with exit code 2.',
        'Blocks direct commits or pushes targeting protected default branches (main/master).',
        'Forces widening touches: on the issue before writing to new files.'
      ],
      remediation: 'If an edit is blocked, the agent must widen touches: on the GitHub issue first.'
    },
    localtest: {
      tag: 'Verification',
      title: 'Local Verification',
      subtitle: 'Test Discipline',
      description: 'Executes the issue acceptance predicates, directly affected tests, and every lint, syntax, documentation, or build check required by current governance. Never claims success without empirical proof.',
      code: 'Use the exact verify: commands declared on the issue',
      rules: [
        'Never commit or push code with failing local unit tests or lint errors.',
        'Preserves existing docstrings, public API contracts, and formatting.',
        'Runs exact verification commands specified in the issue.'
      ],
      remediation: 'If local tests fail, the agent debugs and resolves failures inside the worktree.'
    },
    pr: {
      tag: 'Script: create_pr.py',
      title: 'Stamped Pull Request',
      subtitle: 'Automation Script: create_pr.py',
      description: 'Opens a Pull Request targeting main, stamping mandatory author:<id> and family:<family> labels, and populating Closes #ID in the body.',
      code: 'python3 scripts/create_pr.py --issue 104 --agent agent-1 --model-family anthropic --title "..." --body "..."',
      rules: [
        'Requires --agent flag; omitting it exits non-zero.',
        'Stamps author:<id> so the merge gate can distinguish peer reviews from self-reviews.',
        'Includes mandatory Closes #X link for automatic issue closure.'
      ],
      remediation: 'If branch is behind main, rebase on main before opening PR.'
    },
    ci: {
      tag: 'Script: check_ci.py',
      title: 'CI Pipeline Gate',
      subtitle: 'Automation Script: check_ci.py',
      description: 'Polls automated GitHub Actions CI pipeline runs. If CI fails, triggers the remediate-ci-failure skill to parse un-truncated build logs and apply targeted fixes.',
      code: 'python3 scripts/check_ci.py --pr 42 --wait',
      rules: [
        'CI checks must be completely green before review or merge.',
        'Never gloss over build timeouts or permission errors.',
        'Parses un-truncated logs for empirical failure tracebacks.'
      ],
      remediation: 'Invokes skills/remediate-ci-failure to analyze logs, apply targeted fixes, and push updates.'
    },
    review: {
      tag: 'Skill: code-review',
      title: 'Independent Peer Review',
      subtitle: 'SkillsMP Procedure: code-review',
      description: 'A distinct peer agent (preferring cross-family) claims the PR for review, checks out an isolated review worktree (.worktrees/review-pr-Y), and inspects the diff against the checklist.',
      code: 'python3 scripts/claim_issue.py --pr 42 --agent agent-2',
      rules: [
        'Authors can NEVER review their own PR (merge gate rejects self-reviews).',
        'Peer agent claims first, leaves inline comments for findings, and uses --complete-review only when clean.',
        'Stamps reviewed-by:agent-2 label upon completion.'
      ],
      remediation: 'If blocking findings exist, threads are left open and the claim is released.'
    },
    threads: {
      tag: 'Script: fetch_pr_feedback.py',
      title: 'Thread Verification',
      subtitle: 'Automation Script: fetch_pr_feedback.py',
      description: 'Parses active review comments and ensures every finding has a commit fix landing after the finding or an explicit Withdrawn: reply.',
      code: 'python3 scripts/fetch_pr_feedback.py --pr 42',
      rules: [
        'Resolving a thread UI toggle alone is insufficient; a commit or explicit withdrawal is required.',
        'Withdrawn replies must state why the finding was retracted.',
        'Prevents unaddressed findings from reaching the merge gate.'
      ],
      remediation: 'Author agent invokes address-pr-feedback skill to push fixes or post withdrawal replies.'
    },
    freshhead: {
      tag: 'Head Check',
      title: 'Head Freshness Guard',
      subtitle: 'Validation Guard',
      description: 'Ensures the latest review attests to the exact current head SHA. If new commits were pushed after review, re-review is required before merge.',
      code: 'merge_pr.py check_reviews() evidence["reviewed_head"]',
      rules: [
        'Reviews of previous commit SHAs cover code no longer proposed.',
        'Prevents force-push bypasses after approval.',
        'Requires peer re-review of updated head SHA.'
      ],
      remediation: 'Peer agent re-evaluates the updated head SHA and updates attestation.'
    },
    dodgate: {
      tag: 'Script: merge_pr.py',
      title: 'Definition-of-Done Gate',
      subtitle: 'Automation Script: merge_pr.py',
      description: 'The single sanctioned merge gate validates PR state and issue linkage, current-head verification, CI, independent review, rebase state, size and test coverage, spec sync, review rounds, and live acceptance predicates.',
      code: 'python3 scripts/merge_pr.py --pr 42',
      rules: [
        'Fail-closed: any failed check aborts merge with non-zero exit code.',
        'Direct pushes to main and gh pr merge bypasses are strictly forbidden.',
        'Only script that has authority to merge code.'
      ],
      remediation: 'Prints exact unmet condition; agent resolves condition and re-runs script.'
    },
    merge: {
      tag: 'Server Merge',
      title: 'Mechanical Merge & Tag',
      subtitle: 'Git Server Merge & Audit',
      description: 'The gated merge helper executes the server merge and writes an annotated checkpoint tag named ckpt/<PR>-<short-SHA> with the PR, issue, author, reviewer, and gate evidence.',
      code: 'python3 scripts/merge_pr.py --pr 42 --expected-head <SHA>',
      rules: [
        'Current default is a merge commit; #89 is closed and squash is opt-in.',
        'Creates immutable audit trail checkpoint tag.',
        'Use --merge-method squash only as an explicit exception when its history loss is acceptable.'
      ],
      remediation: 'If remote server merge fails, merge_pr.py reports error without corrupting local state.'
    },
    closeout: {
      tag: 'Auto Close',
      title: 'Board Sync & Worktree Pruning',
      subtitle: 'Close-Out Engine',
      description: 'Performs post-merge close-out: auto-closes linked GitHub issues, transitions board items to status:done, clears agent claims, and prunes local git worktree.',
      code: 'python3 scripts/merge_pr.py --pr 42 --expected-head <SHA>',
      rules: [
        'Guarantees Done means clean workspace and sync\'d board.',
        'Deregisters and prunes .worktrees/feat-issue-X.',
        'Clears merger: and agent:<id> claims.'
      ],
      remediation: 'If close-out fails mid-way, running merge_pr.py again resumes close-out safely.'
    },
    preview: {
      tag: 'Skill: deploy-preview',
      title: 'Runnable Preview & Smoke',
      subtitle: 'Shipped GitHub Pages first-stack preview',
      description: 'The governed preview helper deploys an exact merged commit to the configured GitHub Pages target, verifies exact-run metadata and its authoritative Pages URL, and records smoke/E2E evidence for runnable products.',
      code: 'python3 scripts/deploy_preview.py --commit <SHA> --issue <N>',
      rules: [
        'Begins only from a governed merged commit.',
        'Binds the successful run, source commit, repository, and Pages URL.',
        'Visibly skips libraries with no runnable preview surface.'
      ],
      remediation: 'A failed preview files a governed remediation issue; it never records a false success.'
    },
    deploy: {
      tag: 'Script: promote.py · audit-only',
      title: 'Promotion Evidence Record',
      subtitle: 'Shipped GitHub state trail — not runnable hosting',
      description: 'The promotion helper records adjacent preview, staging, and production GitHub Environment/Deployment state with checkpoint and issue evidence. It does not deploy, copy, rebuild, or prove movement of a runnable artifact.',
      code: 'python3 scripts/promote.py --commit <SHA> --checkpoint <TAG> ...',
      rules: [
        'A workflow run URL is an audit log link, not an application URL.',
        'No immutable provider artifact, staging URL, or production URL is claimed.',
        'Reversal records GitHub state; source rollback uses revert_merge.py.'
      ],
      remediation: 'Do not report staging or production availability from this record alone.'
    },
    provider: {
      tag: 'Open: #345 · Vercel proof',
      title: 'Real Provider Delivery',
      subtitle: 'Deferred Phase 0 external proof',
      description: 'Issue #345 must deploy the calculator tracer to Vercel and record immutable deployment identity, commit-specific preview URL, live smoke results, production promotion without a rebuild, and governed rollback evidence.',
      code: 'Tracked by GitHub issue #345',
      rules: [
        'Provider deployment identity and authoritative hosted URLs are required.',
        'Preview smoke covers health, operations, and invalid input.',
        'Production promotion and rollback must preserve and prove deployment identity.'
      ],
      remediation: 'Until #345 closes with live evidence, Aru must not claim real provider delivery.'
    },
    telemetry: {
      tag: 'Scripts: fleet_status.py + factory_metrics.py',
      title: 'Telemetry & Operator View',
      subtitle: 'Monitoring Dashboard',
      description: 'fleet_status.py evaluates live issues, PRs, claims, board drift, worktrees, and CI failure rate. factory_metrics.py supplies dwell, rework, cycle-time, CI-run counts, and available cost evidence, reporting unavailable measurements instead of inventing them.',
      code: 'python3 scripts/fleet_status.py; python3 scripts/factory_metrics.py',
      rules: [
        'Fails closed when GitHub or board queries are ambiguous.',
        'Reports open governed work, claims, drift, and orphan worktrees.',
        'Marks missing cost or timing evidence unavailable rather than zero.'
      ],
      remediation: 'Use triage_backlog.py --capacity beside fleet_status.py when the Ready queue is constrained.'
    }
  };

  // DOM Elements
  const drawer = document.getElementById('detail-drawer');
  const overlay = document.getElementById('drawer-overlay');
  const closeBtn = document.getElementById('close-drawer-btn');
  const cards = document.querySelectorAll('.flow-card');
  const searchInput = document.getElementById('search-input');
  const modeButtons = document.querySelectorAll('.mode-btn');
  const simBtn = document.getElementById('sim-toggle-btn');
  const simBtnText = document.getElementById('sim-btn-text');
  const backgroundRegions = document.querySelectorAll('.app-header, .toolbar, .pipeline-container, .app-footer');

  let simRunning = false;
  let simInterval = null;
  let activeView = 'macro';
  let searchQuery = '';
  let lastActiveCard = null;

  // 1. Drawer Inspection Logic
  function openDrawer(nodeId, trigger) {
    const data = NODE_DETAILS[nodeId];
    if (!data) return;

    lastActiveCard = trigger || null;

    document.getElementById('drawer-tag').textContent = data.tag;
    document.getElementById('drawer-title').textContent = data.title;
    document.getElementById('drawer-subtitle').textContent = data.subtitle;
    document.getElementById('drawer-description').textContent = data.description;
    document.getElementById('drawer-code').textContent = data.code;

    const rulesUl = document.getElementById('drawer-rules');
    rulesUl.innerHTML = '';
    data.rules.forEach(rule => {
      const li = document.createElement('li');
      li.textContent = rule;
      rulesUl.appendChild(li);
    });

    document.getElementById('drawer-remediation').textContent = data.remediation;

    cards.forEach(c => c.classList.remove('active-inspect'));
    const activeCard = document.getElementById(`node-${nodeId}`);
    if (activeCard) activeCard.classList.add('active-inspect');

    drawer.classList.add('open');
    overlay.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    backgroundRegions.forEach(region => { region.inert = true; });
    closeBtn.focus();
  }

  function closeDrawer() {
    drawer.classList.remove('open');
    overlay.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    backgroundRegions.forEach(region => { region.inert = false; });
    cards.forEach(c => c.classList.remove('active-inspect'));
    if (lastActiveCard) lastActiveCard.focus();
  }

  cards.forEach(card => {
    const title = card.querySelector('h3').textContent;
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-label', `Inspect ${title}`);
    card.addEventListener('click', () => {
      const nodeId = card.getAttribute('data-id');
      openDrawer(nodeId, card);
    });
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDrawer(card.getAttribute('data-id'), card);
      }
    });
  });

  closeBtn.addEventListener('click', closeDrawer);
  overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && drawer.classList.contains('open')) closeDrawer();
    if (event.key === 'Tab' && drawer.classList.contains('open')) {
      const focusable = [...drawer.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
        .filter(element => !element.disabled);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  // 2. Search & Filter Logic
  const macroIds = new Set(['idea', 'triage', 'picker', 'pr', 'review', 'dodgate', 'preview', 'deploy', 'provider']);
  const remediationIds = new Set(['triage', 'claim', 'plangate', 'implementation', 'ci', 'threads', 'freshhead', 'dodgate']);

  function applyFilters() {
    cards.forEach(card => {
      const nodeId = card.getAttribute('data-id');
      const data = NODE_DETAILS[nodeId];
      if (!data) return;

      const searchableText = `${data.title} ${data.subtitle} ${data.description} ${data.code} ${data.tag} ${data.rules.join(' ')}`.toLowerCase();
      const matchesSearch = !searchQuery || searchableText.includes(searchQuery);
      const matchesView = activeView === 'micro'
        || (activeView === 'macro' && macroIds.has(nodeId))
        || (activeView === 'remediation' && remediationIds.has(nodeId));
      card.style.opacity = matchesSearch ? (matchesView ? '1' : '0.4') : '0.2';
    });
  }

  searchInput.addEventListener('input', (event) => {
    searchQuery = event.target.value.toLowerCase().trim();
    applyFilters();
  });

  // 3. View Mode Switcher
  modeButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      modeButtons.forEach(b => b.classList.remove('active'));
      modeButtons.forEach(b => b.setAttribute('aria-pressed', 'false'));
      btn.classList.add('active');
      btn.setAttribute('aria-pressed', 'true');
      activeView = btn.getAttribute('data-view');
      applyFilters();
    });
  });

  modeButtons.forEach(btn => btn.setAttribute('aria-pressed', btn.classList.contains('active') ? 'true' : 'false'));
  applyFilters();

  // 4. Animated Flow Simulation
  const SIM_SEQUENCE = ['idea', 'prd', 'dag', 'triage', 'picker', 'claim', 'worktree', 'plangate', 'implementation', 'localtest', 'pr', 'ci', 'review', 'threads', 'freshhead', 'dodgate', 'merge', 'closeout', 'preview', 'deploy', 'provider', 'telemetry'];
  let simIndex = 0;

  function runSimulationStep() {
    cards.forEach(c => c.classList.remove('simulating-active'));
    const currentId = SIM_SEQUENCE[simIndex];
    const currentCard = document.getElementById(`node-${currentId}`);
    if (currentCard) {
      currentCard.classList.add('simulating-active');
      currentCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    simIndex = (simIndex + 1) % SIM_SEQUENCE.length;
  }

  simBtn.addEventListener('click', () => {
    if (simRunning) {
      clearInterval(simInterval);
      simRunning = false;
      simBtn.classList.remove('running');
      simBtnText.textContent = 'Simulate Idea Flow';
      cards.forEach(c => c.classList.remove('simulating-active'));
    } else {
      simRunning = true;
      simIndex = 0;
      simBtn.classList.add('running');
      simBtnText.textContent = 'Pause Simulation';
      runSimulationStep();
      simInterval = setInterval(runSimulationStep, 1600);
    }
  });

  console.log('Aru Agentic SDLC Visualizer Loaded Cleanly.');
});
