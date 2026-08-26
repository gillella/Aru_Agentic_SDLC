Resume the Aru desktop factory loop after an explicit operator stop.

1. Resolve the project path (`git rev-parse --show-toplevel` or the workspace root).
2. Check loop state before resuming:
   `python3 "$ARU_SDLC_HOME/scripts/loop_control.py" status --project "<abs-path>"`
3. Run:
   `bash "$ARU_SDLC_HOME/scripts/install_local_agent_integrations.sh" --resume-loop --project "<abs-path>" [--reason <token>]`
   If no stop was requested, resuming is a structured silent success (exits 0 with `No stop requested; continuing`).
4. Then follow `$ARU_SDLC_HOME/skills/run-aru-factory/SKILL.md` in **loop** mode
   in this same desktop task. Do not start a CLI agent instead.
