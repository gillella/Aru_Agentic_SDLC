Resume the Aru desktop factory loop after an explicit operator stop.

1. Resolve the project path (`git rev-parse --show-toplevel` or the workspace root).
2. Run:
   `bash "$ARU_SDLC_HOME/scripts/install_local_agent_integrations.sh" --resume-loop --project "<abs-path>"`
3. Then follow `$ARU_SDLC_HOME/skills/run-aru-factory/SKILL.md` in **loop** mode
   in this same desktop task. Do not start a CLI agent instead.
