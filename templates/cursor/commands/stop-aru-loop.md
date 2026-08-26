Stop the Aru desktop factory loop for this project.

1. Resolve the project path (`git rev-parse --show-toplevel` or the workspace root).
2. Run:
   `bash "$ARU_SDLC_HOME/scripts/install_local_agent_integrations.sh" --stop-loop --project "<abs-path>" [--reason <token>]`
   Valid pause reasons: `operator-requested`, `factory-complete`, `human-intervention`, `quota-exhausted`, `maintenance`, `error-threshold`.
3. Do not ask the picker again. Do not arm Cursor loop heartbeats, Codex thread
   automations, or Antigravity `/schedule` wakes for this project.
4. Check loop state with the read-only status command:
   `python3 "$ARU_SDLC_HOME/scripts/loop_control.py" status --project "<abs-path>"`
5. Note that desktop stop records local intent and pauses local agent hooks; it never directly mutates or pauses an external orchestrator.
