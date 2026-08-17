# Cursor Integration for Aru_Agentic_SDLC

This playbook is vendor-neutral. Cursor-specific wiring lives here so every
software project can share one Issue-First SDLC without copying skills into
each repo.

## What gets installed

Running `scripts/install_cursor_integration.sh` configures the local machine:

| Artifact | Location | Purpose |
|---|---|---|
| `ARU_SDLC_HOME` | shell profile (`~/.zshrc` / `~/.bashrc`) | Canonical path to this playbook |
| Agent skills | `~/.cursor/skills/<skill>/` and `~/.agents/skills/<skill>/` | Symlinks so Cursor discovers SDLC skills in every workspace |
| Slash commands | `~/.cursor/commands/*.md` | `/continue`, `/run-aru-factory`, `/implement-next-issue`, … |
| User-rules paste file | `~/.cursor/user-rules-aru-agentic-sdlc.md` | Text to paste into **Customize → Rules → User Rules** |
| Optional global rule file | `~/.cursor/rules/aru-agentic-sdlc.mdc` | Best-effort file-backed rule (User Rules UI is authoritative) |

Project bootstraps (`init_project.py`) also drop
`.cursor/rules/aru-agentic-sdlc.mdc` into each new repo so project rules
mirror the User Rule for teammates who do not have the global install.

### Pinning a Version with `ARU_SDLC_REF`

To prevent unannounced breaks on `main` from impacting consumer projects, pin a specific immutable commit SHA, release tag, or checkpoint tag before installing:

```bash
export ARU_SDLC_HOME=/Users/aravindgillella/projects/Aru_Agentic_SDLC
# Pin to an immutable commit SHA, release tag, or checkpoint:
export ARU_SDLC_REF=d3a0588
"$ARU_SDLC_HOME/scripts/install_cursor_integration.sh"
```

- When `ARU_SDLC_REF` is set to an immutable ref, the installer checks out that ref in `$ARU_SDLC_HOME` and exports `ARU_SDLC_REF` in your shell profile. Do not use moving branch names like `main` if you wish to prevent upstream changes from updating your environment.
- When `ARU_SDLC_REF` is unset, default behavior is unchanged (follows current branch / `main`).
- Helper scripts check `ARU_SDLC_REF` against the checked-out repository version and print a warning if MAJOR SemVer versions mismatch, but never hard-fail mid-session.

#### Discovering Current Ref and Available Versions

To inspect what ref is currently installed or list available release tags:

```bash
# Discover current installed ref:
git -C "$ARU_SDLC_HOME" rev-parse --short HEAD

# Discover current tags and checkpoints:
git -C "$ARU_SDLC_HOME" tag -l
```

#### Upgrading to a New Ref Deliberately

To move forward to a newer ref or update to the latest `main`:

```bash
cd "$ARU_SDLC_HOME"
git fetch origin
git checkout <target-ref-or-main>
export ARU_SDLC_REF=<target-ref-or-main>
"$ARU_SDLC_HOME/scripts/install_cursor_integration.sh"
```

### GitHub access: `gh`, not MCP

Authenticate GitHub with the `gh` CLI (`gh auth status`). Factory helpers
under `$ARU_SDLC_HOME/scripts/` call that `gh`. **Do not use GitHub MCP** for
claims, PRs, board status, reviews, or merges — it is a second credential
store and bypasses author/reviewer stamps and the merge gate. MCP GitHub is
optional and non-authoritative. Do not copy a PAT into MCP. Direct `gh` is
allowed only when no helper exists (`gh issue comment` for implementation
plans).

### Finding User Rules in the UI (Cursor 3.x)

The label moves between builds. Try these in order:

1. **Command Palette** (`Cmd+Shift+P`) → type `Cursor Settings` → open it →
   look for **Rules** / **Rules for AI** / **User Rules**.
2. Gear icon (top-right) → **Cursor Settings** (not “VS Code Settings”) →
   **Rules**.
3. Agents / Glass sidebar → **Customize** → **Rules**.
4. If you still cannot find a User Rules text box: you can skip it.
   Skills under `~/.cursor/skills/`, project `.cursor/rules/`, and
   `~/.cursor/global.rules.mdc` already carry the SDLC guidance.

## Per-project governance

Every software repo still needs:

- Root `AGENTS.md` (Issue-First Law + board contract)
- GitHub Project board with statuses
  `Backlog → Ready → In Progress → In Review → Done`
- Issue body metadata: `depends-on:`, `touches:`, `parallel-eligible:`
- PRs that include `Closes #<n>`

Do **not** vendor a second copy of `skills/` or `scripts/` into each app
repo. Point agents at `$ARU_SDLC_HOME` instead.

## Skill routing inside Cursor

Cursor auto-discovers personal skills from `~/.cursor/skills/` (and, on this
machine, `~/.agents/skills/`). The installer symlinks:

- `aru-agentic-sdlc` (router)
- `run-aru-factory` (please continue / work the board)
- `implement-next-issue`
- `init-agent-project`
- `create-github-issue`
- `code-review`
- `remediate-ci-failure`
- `address-pr-feedback`

In a new chat, **please continue** (or `/continue`) is loop mode: recover from
the board, then pick feedback → merge → review → issue. Do not route bare
"continue" to `implement-next-issue`; that skips review and merge.

Agents must **read** the matching `SKILL.md` before acting. Scripts are
invoked as:

```bash
python3 "$ARU_SDLC_HOME/scripts/<name>.py" ...
```

## Doctor

Verify this machine and a target repo without mutating anything:

```bash
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py"
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py" --json \
  --project /absolute/path/to/repo
python3 "$ARU_SDLC_HOME/scripts/doctor_local_agent_integrations.py" \
  --target-home /tmp/isolated-home --json
```

The command checks `$ARU_SDLC_HOME`, Cursor skill links under `~/.cursor/skills/`
and `~/.agents/skills/`, the `/run-aru-factory` command file, the managed
governance block, `git`/`gh`, and (with `--project`) `AGENTS.md`, hooks, the
Project Board, and `.worktrees/`. Exit `0` / `2` / `1` means healthy /
degraded / invalid. Failed checks print a repair command; the doctor never
installs or prints credentials. Default (no `--target-home`) diagnoses the
live user home, including system `/Applications`. Explicit `--target-home`
uses that directory as `HOME` for probes, does not inherit operator
`GH_TOKEN` / `GH_CONFIG_DIR` / related credential env vars, and does not
scan `/Applications` just because process `HOME` equals the fixture.

## Updating the playbook

Because skills are symlinked, edits in this repository are picked up on the
next Agent turn. After pulling playbook changes, no reinstall is required
unless skill directory names change — then re-run the installer.

## Opting a project out

If a repo should not use Issue-First governance, remove or rewrite its
`AGENTS.md` and delete `.cursor/rules/aru-agentic-sdlc.mdc`. Global User
Rules still apply; soften or remove the User Rule text if you need a
true exception.
