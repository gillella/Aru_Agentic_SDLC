Run the Aru_Agentic_SDLC factory.

1. Read and follow `$ARU_SDLC_HOME/skills/run-aru-factory/SKILL.md`.
2. Default mode is **loop** when the user said continue / keep going / run the
   factory. Use `next` only when they asked for one unit, then stop.
3. Use agent id `cursor-1` unless another id is already in use for this session.
   Pass `--agent` and `--family` on every picker command.
4. In the Cursor desktop app, keep this current project task in charge. Do not
   replace it with a CLI agent. A recoverable wait does not end the loop.
