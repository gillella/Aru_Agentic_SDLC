Resume the Aru factory from GitHub board state (`aru code` / `aru` loop).

1. Read and follow `$ARU_SDLC_HOME/skills/run-aru-factory/SKILL.md` in **loop**
   mode (not `next`, not `implement-next-issue`).
2. Omit `--agent` to auto-assign a free identity, or keep this session's id and
   family and pass `--agent` / `--family` on every picker command.
3. Recover first: `fetch_next_work.py --claim --json`.
   Finish in-flight work for this id before claiming anything new.
4. Pace dynamically: after each unit, ask the picker again if it would return
   work; if the blocker is CI or a peer review you must not perform, wait on
   that event with a long fallback heartbeat. Do not poll on a fixed interval.
5. Ask the operator only as last resort — a product decision the issue does
   not settle, or a severe merge/close-out agents cannot remediate.
6. If `$HOME/.aru/factory-loop.stop` applies to this project, stop. Do not arm
   a Cursor loop heartbeat or a Cursor Automation (Automations start a new agent).
7. Keep this Cursor desktop app task working after every unit and recoverable wait.
   Stop only when the operator explicitly stops it or human intervention is
   genuinely required; do not replace it with a CLI agent.
