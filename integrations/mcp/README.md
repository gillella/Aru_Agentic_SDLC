# Aru kernel MCP server

Serves the supported kernel lifecycle commands as MCP tools over stdio, so an
agent drives the kernel from tool schemas and structured refusals instead of
prose restating a command sequence and its flags.

MCP is an open protocol, not a vendor framework: one server serves any MCP
client. It ships no loop, no scheduler, no persona catalog and no state, so it
is not an agent-framework adapter of the kind removed in v2.

## Install

No dependencies. The transport is newline-delimited JSON-RPC 2.0 on the standard
library.

Claude Code:

```bash
claude mcp add aru -- python3 "$ARU_SDLC_HOME/integrations/mcp/server.py"
```

Any other MCP client: run `python3 integrations/mcp/server.py` as a stdio server.

The server uses the `scripts/` directory of its own checkout. That is
deliberate: the server and the helpers change together, so a stale
`ARU_SDLC_HOME` must not point a governance tool at a different kernel revision.
`ARU_SDLC_HOME` is the fallback when an installation carries no scripts of its own.

## Tools

| Tool | Helper | Notes |
| --- | --- | --- |
| `aru_next_work` | `fetch_next_work.py` | Read-only; never claims or promotes |
| `aru_triage` | `triage_backlog.py` | Promotes one issue per run |
| `aru_claim` | `claim_issue.py` | Exclusive ownership; refuses on a race |
| `aru_create_branch` | `create_branch.py` | Requires In Progress and the exact claimant |
| `aru_open_pr` | `create_pr.py` | Appends the closing directive itself |
| `aru_check_ci` | `check_ci.py` | Fails closed on missing or stale evidence |
| `aru_pr_feedback` | `fetch_pr_feedback.py` | Unresolved findings |
| `aru_merge` | `merge_pr.py` | `dry_run` evaluates every gate without merging |
| `aru_cleanup` | `cleanup_worktrees.py` | Retains locked, dirty and ambiguous worktrees |
| `aru_revert` | `revert_merge.py` | Needs a separate approved revert issue |
| `aru_report` | `report.py` | Read-only delivery metrics |

`init_project.py` is deliberately not exposed: creating repositories, Projects
and rulesets is an operator action.

## It is an adapter, not a gate

Every tool shells out to the existing helper, so nothing here can authorize a
transition the command line would refuse. A refused transition comes back as a
tool error carrying the helper's exact message:

```json
{"ok": false, "code": "kernel_refusal", "message": "--since must look like 30d, 6w or 48h",
 "tool": "aru_report", "exit_code": 2}
```

Codes are stable: `kernel_refusal`, `kernel_helper_missing`, `kernel_timeout`,
`kernel_unavailable`, `kernel_output_unreadable`. An unknown argument is refused
rather than dropped, because silently ignoring an argument the caller believed it
passed is how you merge the wrong pull request.
