# Restricted mechanical merge integration

This optional operator package admits one mechanical merge request under a
separate execution user. It adds no scheduler, service, queue, repository runtime
or lifecycle store. GitHub Issues/Project remain the authority. An existing
external caller decides when to request one eligible PR/head. This source tree
does **not** establish that the capability is installed or operational.

The caller receives only this interface, after reviewed installation and host
proof:

```sh
sudo -n -u aru-merge /usr/bin/python3 -I \
  /private/var/db/aru-trusted-merge/request.py \
  --pr <number> --expected-head <full-current-SHA>
```

Add a final `--check` for canonical read-only gate evaluation. Arguments cannot
select a repository, issue, interpreter, path, environment, endpoint, decision or
helper. Sudo's argument wildcard is only routing; the program validates exact
argument count, order and grammar before reading configuration or accessing
authority. Overlapping calls refuse immediately through an ephemeral lock;
there is no pending-work queue. Refusals do not authorize another route.

## Protected execution chain

The installation contains root-owned request/adapter code, exactly seven
canonical helper modules, a sealed `gh` binary, and an empty Git metadata stub
with one fixed origin and disabled hooks. It never checks out or executes PR
code. Every request verifies the code hashes, interpreter identity, protected
ancestor ownership/modes, private credential/runtime directories and the
non-root merge principal before importing the canonical helper. A root/OS update
or changed sealed component can require a separately reviewed reinstallation;
there is no automatic helper upgrade or overwrite option.

The operator configures the approved repository and issue allowlist at package
build time, not in request arguments. Resolve the current PR's one `Closes`
directive against that scope, require GitHub-counted `APPROVED`, then call the
canonical helper. Its actual checks retain accepted criteria, exclusive claim,
live touches, linked Project/dependencies, current base/head, same-repository
exact-head CI, independent exact-head review, every finding/thread, and queue/
auto-merge refusal. GitHub still enforces its ruleset on the submission. No
admin bypass or GitHub auto-merge is used.

Construct the environment from protected constants. Only the installed Git stub
is marked safe; caller tokens, imports, Git hooks/config, executable search paths
and App runner variables are discarded. The protected CLI login is separately
authorized by the operator under `aru-merge`, including required Project access;
it is never copied from a coding user's login or supplied by the caller. That
execution identity and App 4921120's check authority remain separate from the
coding process. This package does not manufacture a reviewer identity.

The new private App adapter exposes no generic App API: only the fixed repository
metadata read and `aru-merge-authorized` check at the request's exact head, with
canonical success/failure conclusions. It uses App **4921120**, a single approved
installation/repository token with **Checks write and Metadata read only**, and
revokes the token on completion when GitHub is reachable. GitHub tokens otherwise
retain GitHub's bounded expiration. Signing material, tokens, HTTP errors and
command diagnostics never reach the caller. The adapter is **not** in sudoers;
only the request program can be invoked without an operator password. The
existing generic operator runner is not reused, modified or exposed.

The canonical helper posts authorization only after its final live checks. It
submits the exact head, verifies GitHub's merged head/commit and closes the issue
and Project. A retry of a GitHub-confirmed merge performs only canonical
close-out recovery, never another merge. The caller reads GitHub back and uses
the ordinary cleanup helper, preserving other workers and worktrees.

## Prepare one reviewed package

An unprivileged builder reads immutable Git objects for both the independently
reviewed integration commit and an independently reviewed canonical helper
commit. It includes no PEM, token or CLI login. For the focused Aru release the
scope is `gillella/Aru_Agentic_SDLC`, issues **821, 822, 792**; other repositories
require their own explicit approval. Review the source/helper revisions and the
official CLI provenance before approving the resulting package digest.

```sh
python3 integrations/merge-authority/bundle.py \
  --source <clean-source-checkout> --revision <reviewed-integration-full-SHA> \
  --helper <reviewed-canonical-helper-full-SHA> --gh <reviewed-gh-binary> \
  --repository gillella/Aru_Agentic_SDLC \
  --issue 821 --issue 822 --issue 792 --output <new-package.zip>
```

Do not run an unverified mutable checkout installer as root. After independent
review and explicit operator installation approval, a human Terminal uses this
bootstrap with the **actual approved package digest**. It reads the package once,
verifies that digest, and executes the installer from the verified in-memory
bytes. The installer separately rejects unexpected, duplicate, linked or
tampered archive entries.

```sh
sudo /usr/bin/python3 -I -c '
import hashlib, io, sys, zipfile
from pathlib import Path
raw = Path(sys.argv[1]).read_bytes()
if hashlib.sha256(raw).hexdigest() != sys.argv[2]:
    raise SystemExit("Package digest mismatch")
with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    source = archive.read("install.py")
namespace = {"__name__": "__main__", "_BUNDLE_BYTES": raw}
exec(compile(source, "operator-reviewed/install.py", "exec"), namespace)
' <package.zip> <approved-SHA256>
```

The installer requires a human root TTY, isolated Apple Python, existing distinct
caller and `aru-merge` users, the existing App's installation ID, and a private
local PEM path entered **only on that host**. It copies the key into the separate
user's 0700 directory without printing it. No new App, key generation, broader
permissions or ruleset change is required. Never send a PEM/token to an agent.
Existing installation, sudo policy and staging paths are preserved rather than
overwritten. Validate the complete root-owned payload and sudo policy before
publishing the narrowly scoped `NOPASSWD: NOSETENV` entry.

## Operator proof before automatic use

The human configures a fresh, appropriately authorized CLI login under the
protected directory. Do not reuse an agent's environment or copy its credentials.
For example, interactively on the installed host:

```sh
sudo -u aru-merge env -i HOME=/Users/aru-merge \
  PATH=/usr/bin:/bin GH_CONFIG_DIR=/private/var/db/aru-trusted-merge/credentials \
  /private/var/db/aru-trusted-merge/bin/gh auth login \
  --hostname github.com --git-protocol https --web --insecure-storage
```

The final flag stores the separate CLI login inside the protected directory,
avoiding a shared desktop keychain. Check that its files remain private to
`aru-merge`; do not print their contents. Prove the new private App adapter's
authentication/single-repository token in the operator session with a metadata
read, **not** a fabricated authorization check:

```sh
sudo -u aru-merge env -i HOME=/Users/aru-merge PATH=/usr/bin:/bin \
  ARU_TRUSTED_HEAD=<reviewed-candidate-full-SHA> \
  /usr/bin/python3 -I /private/var/db/aru-trusted-merge/app.py \
  --repo gillella/Aru_Agentic_SDLC -- gh api repos/gillella/Aru_Agentic_SDLC
```

After recording real installation/authentication and obtaining human acceptance
of the live issue criteria, prove the full eligible current-head `--check` from
the caller. With cached sudo authority invalidated, prove `sudo -n` works only
for the fixed request: malformed/extra arguments, stale head and out-of-scope PR
refuse; a generic shell, interpreter command or App runner is not authorized.
Use permission-list inspection for the generic runner; do not execute it merely
to test denial. Never tamper with installed production code to test its hashes;
the offline tests cover changed code/ownership, archive integrity and real
canonical-gate refusals.

Record nonsecret outcomes, exact installed hashes, scope, current head/checks/
review and denials in the GitHub issue. Do not mark criteria from an installer
exit code alone. If effective sudo policy, CLI/Project authorization, App token
scope or immutable dependencies are unproven, automatic merge stays unavailable.
Once proven and accepted, the existing external continuation invokes the bounded
request without a per-PR human click. Verify one actual unattended governed merge
and GitHub/issue/Project close-out before calling the release delivered.

The computer must remain on and the external caller must actually run. A sleeping
human is compatible with this route; a sleeping/offline host cannot progress.
This package changes no power settings or scheduled jobs. Operator rollback
first removes only the installed narrow sudo rule, disabling future requests;
credentials and installation files are retained for deliberate operator cleanup.
