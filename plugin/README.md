# Aru Code Factory Plugin

A portable Agent Plugins 1.0 package for the Aru Code Factory minimal kernel.

## Real Enforcement Boundary

This plugin contains advisory tools, subagent personas, and environment hooks.
**It never blocks or authorizes anything.**
Real enforcement lives in:
- `hooks/pre-push`
- `hooks/enforce_touches.py`
- `aru-governed-pr` and `aru-merge-policy` server checks
- `scripts/merge_pr.py`

GitHub Actions never loads this plugin.

## Per-Client Capabilities

| Client | Standard Skills | Subagent Personas | Advisory Hooks | Setup / Install Path |
| --- | --- | --- | --- | --- |
| **Claude Code** | Yes (`skills/`) | Yes (`plugin/agents/`) | Yes (`plugin/hooks/`) | `/plugin marketplace add gillella/Aru_Agentic_SDLC`<br>`/plugin install aru-codefactory` |
| **Copilot CLI** | Yes (`skills/`) | Claude adapter only | Claude adapter only | `copilot plugin marketplace add gillella/Aru_Agentic_SDLC` |
| **Codex** | Yes (`skills/`) | No | No (hooks not reached) | `/plugin marketplace add gillella/Aru_Agentic_SDLC` or `scripts/install_agent_integration.sh` |
| **Cursor** | Yes (`skills/`) | No | Opt-in `Write` only | `scripts/install_agent_integration.sh` until marketplace accepts private repos |

## Coverage Limits

- **Bash tool writes**: File writes via Bash commands (`cat > file`, `echo > file`) bypass the `PreToolUse` matcher (`Write|Edit|NotebookEdit`).
- **Cursor**: Supports `Write` tool only (no `Edit`), and hooks are loaded opt-in from `.claude/settings*.json`.
- **Codex**: Hooks are not reached by this plugin.

## Verification Commands

Validate the plugin with:

```bash
claude plugin validate .
```
