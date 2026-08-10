Promote Backlog issues to Ready under Aru_Agentic_SDLC.

1. Read and follow `$ARU_SDLC_HOME/skills/triage-backlog/SKILL.md`.
2. Run `python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" [--capacity]`.
3. An issue is only Ready when it has acceptance criteria, a `touches:`
   declaration, and no unresolved `depends-on:`.
4. Promote enough work to keep the fleet fed; leave the rest in Backlog.
