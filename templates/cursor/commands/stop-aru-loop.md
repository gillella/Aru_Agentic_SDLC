Stop the Aru desktop factory loop for this project.

1. Resolve the project path (`git rev-parse --show-toplevel` or the workspace root).
2. Run:
   `bash "$ARU_SDLC_HOME/scripts/install_local_agent_integrations.sh" --stop-loop --project "<abs-path>"`
3. Do not ask the picker again. Do not arm Cursor loop heartbeats, Codex thread
   automations, or Antigravity `/schedule` wakes for this project.
4. Confirm the stop file `$HOME/.aru/factory-loop.stop` applies to this project.
