Resume the Aru factory from GitHub board state.

1. Read and follow `$ARU_SDLC_HOME/skills/run-aru-factory/SKILL.md` in **loop**
   mode (not `next`, not `implement-next-issue`).
2. Use agent id `cursor-1` and family `xai` unless this session already has
   another id. Pass `--agent` and `--family` on every picker command.
3. Recover first: `fetch_next_work.py --agent <id> --family <family> --claim --json`.
   Finish in-flight work for this id before claiming anything new.
4. Pace dynamically: after each unit, ask the picker again if it would return
   work; if the blocker is CI or a peer review you must not perform, wait on
   that event with a long fallback heartbeat. Do not poll on a fixed interval.
5. Ask the operator only as last resort — a product decision the issue does
   not settle, or a severe merge/close-out agents cannot remediate.
