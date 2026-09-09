# Optional Chopin Catalog meeting pilot — phase one

This package prepares PM/BA meeting drafts for `gillella/unum-catalog`. It is
optional external integration source, not imported by Aru helpers or consumer
bootstrap. **No service, App, plugin or deployment is activated by this PR.**
The operator's scoped feature-freeze exception is issue #618.

`/plan <topic>` creates a conservative draft through stock MCP, with actual
remote `refs/heads/main` provenance, explicit questions and no settled decisions.
`/plan_status [document-id]` reports repository document count or source revision.
`/plan_compile` always refuses: stock MCP does not export authoritative comments
and decisions consistently. The original collaboration-to-Ready-issue workflow
is **not fulfilled by this phase**.

## Boundaries and observed contracts

Pinned upstream: `githubnext/chopin@9882eb3a13814ca0f198b6b580858bea86171226`,
Bun **1.3.2**, PostgreSQL **17**. Sources inspected: `docs/self-hosting.md`,
`docs/local-agent-mcp.md`, `apps/server/src/auth/config.ts`, `mcp.ts`,
`mcp/create.ts`, `mcp/hosted.ts`, and `packages/protocol/document-url.ts`.

The [Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/)
and installed source agree that `register_command` handlers receive raw arguments
only. Installed Hermes also reserves `/plan` as a built-in. This plugin uses
`register_telegram_handler` on the existing Telegram Application, registers PTB
commands `plan`, `plan_status`, `plan_compile` in group -10, and stops further
dispatch. It checks the real Update's chat/user, rejects channel/anonymous/edited
messages, and requires the default profile. `/plan` takes this pilot meaning on
Telegram while this plugin is enabled; other platforms retain Hermes behavior.
Generic plugin status/compile callbacks have no identity context and refuse.
There is no new bot, polling loop, gateway injection, scheduler or Hermes core edit.

The pilot binds **all three**: `telegram`, chat `-5325492504`, user `6431233670`.
Chat membership grants nobody else the operator bearer. Repository selection is
configuration, never an argument or remote instruction. Initial web admission is
only `gillella`; adding a colleague means explicitly editing `allowed_users`
and separately granting the App installation/repository access. Telegram operator
admission and browser collaboration admission are different boundaries.

The client initializes JSON-RPC protocol `2025-03-26`, carries the returned
`mcp-session-id`, sends `notifications/initialized`, and accepts the pinned
server's JSON POST responses (202 for notifications). Only `list_documents`,
`read_document`, `create_document` are callable. No server instructions are
executed or routed to an LLM. Document IDs must occur in a fresh listing for the
configured repository before source is read; source remains untrusted inert text
and is not relayed to the chat. URLs come from the server's relative
`/documents/owner/repository/slug` result and are joined to the configured origin
after validation and readback. Redirects and proxy environment overrides are
refused; the bearer goes only to the configured same-origin `/mcp` endpoint.

**Stock Chopin still exposes its other authenticated MCP tools server-side.**
`AGENT=off` disables hosted agent/background turns, not MCP. There is no supported
per-tool server flag in this pin. The restriction here is the plugin's explicit
client surface; do not advertise a generic MCP connection to the Factory agent
or expose its bearer to other users. Server-wide removal of lifecycle tools would
require separately reviewed upstream work. No paid Copilot turns are enabled.

Receipts are owner-only `chopin-create/v1` JSON containing the original payload,
key and binding, fsynced before submission, with a per-key process lock. The same
exact trimmed topic under the same origin/repository/operator binding reuses its
original commit/payload/key after duplicate delivery or network ambiguity. A new
meeting needs a distinct topic (e.g. append the meeting date); do not delete a
receipt to retry. A failed response confirms no document name/URL. Receipts are
operational deduplication, never another lifecycle or consensus store.

## Prepare the private configuration

Run commands from the reviewed Aru feature worktree. Nothing below is performed
automatically. The supplied absolute paths match this Mini; edit the generated
private JSON for another host. Check that names/paths are dedicated and the pinned upstream
checkout has no tracked changes or runtime dotenv files. The helper refuses
ambient dotenv without reading it. If present, prepare a separate clean checkout
at the pin; do not delete or source existing secrets.

```sh
umask 077
mkdir -p /Users/gillella/.hermes/workspace/chopin-pilot/operator
python3 -m integrations.chopin.example_config --output /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json
```

The generator in `example_config.py` retains the explicit pilot defaults and
`chopin-pilot/v1` runtime JSON format; it creates a new private file without
overwriting one. No runtime JSON is tracked in Git.

Edit `operator/config.json` in an editor. It must stay 0600, in its 0700 directory;
symlinks and overwrites are refused. It contains paths and explicit admission,
not tokens. All examples below use this configuration. Set `CHOPIN_PILOT_CONFIG`
in the **existing Hermes gateway's environment** to that absolute path when the
plugin is eventually enabled. No secondary Hermes profile is used.

## One browser approval, private setup origin

**The parent/operator must independently inspect and test the setup helper before
launching it.** It has not been run live by the implementation lane.

```sh
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_chopin_integration.py -k 'setup or manifest or conversion or existing_destination' -o addopts= -q
python3 -m integrations.chopin.manifest_setup --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json
```

Default setup origin is `http://100.95.239.18:8766`. Before binding, the helper
requires `tailscale ip -4` to report exactly that address. Open the printed
`http://100.95.239.18:8766/setup/<one-time-capability>` URL on the **tailnet MacBook**
where GitHub is logged in. Keep this terminal URL out of shared logs. The page
posts the manifest to GitHub; approve creation as `gillella`. GitHub redirects
automatically to the separate private `/manifest/callback` with state and code.
The initiating browser cookie, exact Host/path, capability, state, one-shot lock,
and 600-second expiry protect this operation. The credential destination is
reserved exclusively; conversion failure consumes the attempt and leaves the
destination for operator inspection rather than risking a second conversion.

Loopback alternative: use a **clean/private browser profile** for the entire
registration flow. Browser cookies ignore ports, and the setup callback intentionally
requires exactly its own cookie; an unrelated localhost cookie causes refusal.
Then change `setup_origin` to `http://127.0.0.1:8766`, and on the
MacBook run `ssh -N -L 8766:127.0.0.1:8766 gillella@aravinds-mac-mini-1` (substitute
the operator's existing SSH host alias if needed). Open the printed loopback URL
in that same clean browser profile while the SSH forward stays open. GitHub acceptance
of the tailnet HTTP manifest redirect remains unverified; use this loopback alternative
if GitHub refuses it. Exact Host remains
`127.0.0.1:8766`. Never route this listener through Funnel or bind `0.0.0.0`.

The App manifest requests read-only contents, pull_requests, checks, statuses,
metadata; public installation; OAuth-on-install false; no events; inactive
webhooks. Browser OAuth URLs are exactly:

- `https://aravinds-mac-mini-1.tail3df1c1.ts.net:8443/auth/github/callback`
- `https://aravinds-mac-mini-1.tail3df1c1.ts.net:8443/auth/github/setup`

These are **not** the manifest conversion redirect. Conversion checks owner,
name, identity, homepage, permissions/events and any reported settings before
writing `credentials_dir/credentials.json` (0600, directory 0700). It retains
OAuth client credentials and App key, never Factory credentials. No response body,
code, token or request target is logged.

The [GitHub manifest schema](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
has no expiring-user-token field. In the created App settings, verify **expiring
user tokens ON**, **device flow OFF**, exact callback/setup URLs, public
installation, OAuth during installation OFF and inactive webhooks. Some of these
settings are not reported by the conversion API; they are not remotely proven by
this helper. Only after checking them, set `app_browser_settings_verified: true`
in private config. Install the App on **only** `gillella/unum-catalog` for the
pilot. Setup redirect may fail until the runtime is installed; App creation and
installation are not proof of working Chopin OAuth.

## Explicit operator deployment sequence

Run these only after independent review, real App approval and operator deployment
authorization. Commands fail with static errors rather than echoing secrets.
`prepare` validates real App private-key authentication against GitHub `/app`
and checks reported identity/policy; complete OAuth client-secret exchange remains
a browser smoke test. It captures the actual pinned Bun executable using npm
**as launcher only**, copies it to `state_dir/bin/bun`, creates a private strict
`runtime.env`, and never rotates existing credentials on repeat. It does not
npm-install the Chopin workspace.

```sh
python3 -m integrations.chopin.operator --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json prepare
python3 -m integrations.chopin.operator --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json database
python3 -m integrations.chopin.operator --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json build
python3 -m integrations.chopin.operator --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json migrate
python3 -m integrations.chopin.operator --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json install
python3 -m integrations.chopin.operator --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json health
```

Database preparation uses one PostgreSQL advisory-lock session, safe `%I`/`%L`
SQL quoting and generated passwords through stdin/environment, not command-line
arguments. Dedicated role/database comments bind them to this preparation's random
marker. Existing foreign objects, unexpected privileges/membership/owner, wrong
PostgreSQL version and credentials fail closed. The helper does not adopt/drop
objects, alter unrelated databases, use sudo or touch JMC. Partial creation before
a marker is recorded fails closed on retry and needs operator inspection. The
admin connection uses the configured local DB administrator, never ambient
PGPASSFILE; optional `database.admin_password_file` must be a dedicated 0600 file.
The role password is tested before success. Repeated valid database preparation
reuses the role/database. Keep runtime.env with backups: its marker and encryption
key are part of this deployment's private configuration.

Build uses `bun install --frozen-lockfile`, then `bun run build`. Migration is a
separate explicit invocation with a private configuration-bound receipt and lock;
launchd never reruns migrations. An interrupted migration without a receipt may
be rerun explicitly: upstream checks its migration history/checksums. No automatic
schema rollback exists.

Install refuses a used port 3050 or existing launch label/file. It writes a 0600
plist in private state and exclusively hard-links it to
`~/Library/LaunchAgents/local.chopin.catalog-pilot.plist`, then bootstraps the
current GUI domain. ProgramArguments directly invoke the pinned absolute Bun,
absolute `--env-file`, and `apps/server/src/main.ts` with absolute cwd. Runtime.env
is parsed as strict data, never shell-sourced. Bun 1.3.2 gives inherited environment
values precedence over its env file, verified with isolated dummy data. The private
plist deliberately retains explicit runtime values to prevent ambient credentials
or database settings from overriding that file. Keep the plist 0600 and never share
`launchctl print` output. KeepAlive and RunAtLoad are both
true, including restart after clean exit; the agent starts only **after user
login**, not at unattended pre-login boot. Failed bootstrap leaves the file for
inspection; do not blindly overwrite/retry a collision. All log paths remain
private and precreated 0600. Do not enable verbose request/OAuth logging or share
raw runtime logs; bounded helper diagnostics suppress remote details.

The existing reserved Funnel routes public :8443 to loopback :3050. Verify that
route independently during deployment. This package makes **no Tailscale route
changes**. JMC :443/:3001 must stay private and unchanged. `/api/session` health
checks only HTTP liveness, not OAuth, database transactions, MCP or canvas.

Full private backup, after operator deployment:

```sh
python3 -m integrations.chopin.operator --config /Users/gillella/.hermes/workspace/chopin-pilot/operator/config.json backup
```

Backups stream `pg_dump -Fc` to exclusive 0600 files under private `backups/`.
Rehearse restore into a separate dedicated database before relying on them. Stop
the one writer before any future restore/upgrade; no restore/destructive command
is supplied here. Review private logs and remove/rotate them manually during a
planned service stop; there is no added log daemon. Upstream shutdown/restart
invalidates browser sessions. No zero-downtime or production-readiness claim.

## Operator MCP bearer and plugin installation

MCP authenticates a **GitHub user** token, independently from browser App
installation. Do not use the Factory installation token as though it were a user
bearer. Capture the existing operator `gh` user credential without terminal
copy/paste using the bounded command below, after reviewing its destination:

```sh
python3 - <<'PY'
import subprocess
from pathlib import Path
from integrations.chopin.private import write_new
result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'],
                        capture_output=True, timeout=15, check=True)
write_new(Path('/Users/gillella/.hermes/credentials/chopin-mcp/bearer'), result.stdout.strip())
PY
```

Do not put the bearer in repository dotenv, Hermes shared MCP configuration or a
receipt. Alternatively set `mcp_bearer` to `{"env":"CHOPIN_MCP_BEARER"}` and supply
it privately to the existing gateway. Once the service runs, an authorized
`/plan_status` is the first narrow MCP authentication probe.

To install this reviewed local source without kernel/core edits:

```sh
python3 - <<'PY'
import shutil
from pathlib import Path
source = Path('integrations/chopin').resolve()
target = Path('/Users/gillella/.hermes/plugins/chopin-pilot')
if target.exists() or target.is_symlink():
    raise SystemExit('Existing plugin destination: inspect instead of overwriting')
shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
PY
hermes plugins doctor /Users/gillella/.hermes/plugins/chopin-pilot --ci
hermes plugins enable chopin-pilot
```

The copy/discovery path is exercised by the offline compatibility test. Set
`CHOPIN_PILOT_CONFIG` in the existing gateway's service environment, then restart
that gateway through its existing operator procedure; this package never edits
its launch configuration or starts a second gateway. `hermes plugins disable
chopin-pilot` and an existing-gateway restart restore normal `/plan` behavior.
Only the configured Telegram user can use this plugin; changing web allowed users
does not change that restriction.

## Verification and remaining work

```sh
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_chopin_integration.py -o addopts= -q
/Users/gillella/.hermes/hermes-agent/venv/bin/python -m ruff check integrations/chopin tests/test_chopin_integration.py
```

The repository's governed job discovers the one-line
`tests/test_chopin_integration.py` import bridge; every test and fixture lives visibly
under `integrations/chopin/tests/test_pilot.py`. This follows the optional integration
boundary and keeps tracked kernel test LOC at 9,000 without changing testpaths,
budgets or exemptions. Direct discovery and bridge discovery must collect the same
cases; run either path (not both together, which would execute each case twice).

The real-Hermes compatibility case always loads the actual installed PluginManager,
imports this plugin and verifies its command/factory registration in a marked temporary
HOME/HERMES_HOME. `HERMES_TEST_PYTHON` explicitly selects the installation; the default
is `~/.hermes/hermes-agent/venv/bin/python`. Missing Hermes or incompatible installed
APIs fail, never skip. A **separate** PTB test runs real Application.process_update
with fixture HTTP. Only `find_spec('telegram') is None` in that selected installation
returns the explicit unavailable result; broken imports or command failures fail.
An absent Telegram extra may skip that test on the governed runner, and such a run
proves plugin registration compatibility only, not Telegram readiness.

Pilot-local release readiness on the Mini requires the full path, with **no Telegram
skip permitted**. Run explicitly against its real Hermes installation:

```sh
CHOPIN_REQUIRE_TELEGRAM=1 HERMES_TEST_PYTHON=/Users/gillella/.hermes/hermes-agent/venv/bin/python /Users/gillella/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_chopin_integration.py tests/test_surface.py -o addopts= -q -rs
```

`CHOPIN_REQUIRE_TELEGRAM=1` turns actual Telegram-extra absence into a failure. Neither
mode installs packages, reads live Hermes configuration, starts polling, or claims
Telegram E2E success. The separate `contract_probe.ts` checks generated fixture
payloads against the pinned upstream creation parser and MDX canonicalizer; it is not
a live MCP/OAuth/canvas test.

Still unverified: browser App approval and settings, real OAuth exchange,
role/database/migration execution, launchd restart, public routing, MCP user bearer
admission/creation/readback, browser canvas, multiple PM/BA users, repository
permission denial and meeting usefulness. No paid Planner turn is authorized by
this phase. Perform those deliberately after source review and real setup.

A separate export/compiler change must provide authenticated repository/document
identity, a single consistent source/decision/comment revision (or snapshot hash),
author identity and authorization, resolved/open decisions, unresolved blockers,
comment provenance and explicit approval semantics. MDX text alone, rendered
canvas, LLM summaries or client-side files are not that authority. Export must
fail on stale/missing/contradictory data and untrusted instruction injection.
Only then can a separately authorized compiler generate complete unchecked Ready
acceptance criteria, safe touches and resolved dependencies; create idempotent
GitHub issues through governed helpers with snapshot-bound deduplication and
reviewable provenance. It must never mark unapproved consensus Ready or infer
approval from chat membership. This phase implements none of that compilation.
