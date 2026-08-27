# Slack control room

Status: **Historical / removed from this repository**

The in-repo Slack bridge that previously routed factory alerts and operator
commands is no longer part of the supported runtime surface here. The deleted
Slack helper scripts and their dedicated dependency file are gone and must not
be referenced as runnable commands.

## Current supported behavior

GitHub remains the authoritative work queue and durable coordination record.
Factory agents report routine progress through normal stdout/status output and
record blockers, waiting states, and human-decision requests on the linked
GitHub issue or pull request.

If the operator or an external gateway mirrors that information into Slack or
Hermes, treat that routing as external infrastructure rather than a local repo
helper. This repository does not provide a supported command to send, receive,
or manage Slack traffic.

For current operator-facing guidance, use:

- [AGENTS.md](../AGENTS.md)
- [docs/desktop-agent-continuity.md](desktop-agent-continuity.md)
- [docs/project_board_workflow.md](project_board_workflow.md)

Historical Slack decisions may still appear in older audit text or issue
history. Treat those as evidence about past operations, not as proof of a
current executable feature.
