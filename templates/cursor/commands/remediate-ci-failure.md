Remediate a failing CI run on an open PR under Aru_Agentic_SDLC.

1. Read and follow `$ARU_SDLC_HOME/skills/remediate-ci-failure/SKILL.md`.
2. Fetch full failed logs before hypothesizing.
3. Fix the root cause in the branch worktree; never delete failing assertions
   or weaken CI to go green.
4. Push and re-poll until checks pass.
