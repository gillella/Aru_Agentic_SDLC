Run the Aru_Agentic_SDLC factory (`aru code` / `aru` is the canonical trigger).

1. Read and follow `$ARU_SDLC_HOME/skills/run-aru-factory/SKILL.md`.
2. Default mode is **loop** when the user said aru code / continue / keep going /
   run the factory. Use `next` only when they asked for one unit, then stop.
   `aru video` / `aru poem` are reserved for factories not yet built — stop
   rather than improvising from Code Factory skills.
3. Pass `--family` on the picker. Omit `--agent` to derive the stable
   `<product>-<fingerprint>` from machine, checkout, and family; set
   `ARU_AGENT_ID` or pass `--agent` only to pin an explicit id.
4. In the Cursor desktop app, keep this current project task in charge. Do not
   replace it with a CLI agent. A recoverable wait does not end the loop.
