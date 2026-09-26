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

## Subagent Personas

Five shared prompts live in `plugin/agents/`, one per role the fleet routes. The
role name in `agent-fleet`'s `config/fleet.json` is the persona's suffix, so a
seat selected for a role loads the matching prompt without a client-specific
file.

| Persona | Fleet role | What it does |
| --- | --- | --- |
| `aru-implementer` | `implementer` | Implements one claimed Ready issue inside its declared paths and opens the governed PR. |
| `aru-reviewer` | `reviewer` | Reviews a PR against acceptance criteria, the exact diff, and focused verification. Refuses a PR it authored. |
| `aru-triager` | `triager` | Validates Backlog issues against the Ready contract. Never promotes by hand. |
| `aru-tester` | `tester` | Encodes acceptance criteria as a test that fails before the change and passes after it. Never loosens an assertion. |
| `aru-docs` | `docs` | Changes only declared documentation paths and keeps every restatement in agreement with `scripts/policy.toml`. |

Every persona must announce, before its first action on a claimed issue, its
role, the model it is running as, and whether that identity is fleet-launched
(a launcher recorded it) or self-reported. A fleet-launched announcement is
checkable against the launcher record; a self-reported one is a claim that
nothing verifies, and the two are not equally authoritative. An agent that
cannot determine the model says so rather than guessing from context. The rule
lives in the five prompts so every client that loads the plugin receives it
once.

The Cursor adapter (`.cursor-plugin/plugin.json`) points at the directory, so a
new persona ships by adding a file. The Claude adapter
(`.claude-plugin/plugin.json`) must name each file instead — `claude plugin
validate .` rejects a directory string with `agents: Invalid input` — so a new
persona is added in both places. `test_claude_adapter_agrees_with_root_manifest`
fails if the two fall out of step. The root `plugin.json` carries no `agents`
key.

## Per-Client Capabilities

| Client | Standard Skills | Subagent Personas | Advisory Hooks | Setup / Install Path |
| --- | --- | --- | --- | --- |
| **Claude Code** | Yes (`skills/`) | Yes (`plugin/agents/`) | Yes (`plugin/hooks/`) | `/plugin marketplace add gillella/Aru_Agentic_SDLC`<br>`/plugin install aru-codefactory` |
| **Copilot CLI** | Yes (`skills/`) | Claude adapter only | Claude adapter only | `copilot plugin marketplace add gillella/Aru_Agentic_SDLC` |
| **Codex** | Yes (`skills/`) | No | No (hooks not reached) | `/plugin marketplace add gillella/Aru_Agentic_SDLC` or `scripts/install_agent_integration.sh` |
| **Cursor** | Yes (`skills/`) | Yes (`plugin/agents/`) | Opt-in `Write` only | Native Cursor manifest (`.cursor-plugin/plugin.json`) via Customize > Plugins (local plugin folder), or `scripts/install_agent_integration.sh` |
| **Hermes Agent** | Yes (`skills/`) | No | No | `scripts/install_agent_integration.sh` (no plugin mechanism; skills land in `~/.hermes/skills/software-development/`, guidance in `~/.hermes/SOUL.md`) |

The six plugin skills are the current workflow instructions. If Claude has this
plugin installed, the local installer uses those skills and removes only a
recognized duplicate Aru managed block from `~/.claude/CLAUDE.md`; personal and
stricter consumer instructions remain. Unrecognized stale text or malformed
boundaries require manual reconciliation. To reconcile the historical Cursor
project rule in a named consumer checkout, run
`scripts/install_agent_integration.sh --project <project-path>`; the installer
preserves the rule's consumer sections and does not install a second plugin copy.
Managed guidance names the six skills, uses `fetch_next_work.py`, and requires
approval of every PR's current head by a different GitHub account.

## Coverage Limits

- **Bash tool writes**: File writes via Bash commands (`cat > file`, `echo > file`) bypass the `PreToolUse` matcher (`Write|Edit|NotebookEdit`).
- **Cursor**: Supports `Write` tool only (no `Edit`), and hooks are loaded opt-in from `.claude/settings*.json`. The five personas load via `.cursor-plugin/plugin.json` (or `.claude-plugin/plugin.json`).
- **Codex**: Hooks are not reached by this plugin.

## Verification Commands

Validate the plugin with:

```bash
claude plugin validate .
```
