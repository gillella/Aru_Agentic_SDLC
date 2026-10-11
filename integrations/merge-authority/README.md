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

Installation leaves submissions disabled. Add a final `--check` for canonical
read-only gate evaluation, available while disabled. Every non-check request
requires the human-created root-owned `enabled.json` marker bound to the exact
installed `config.json` bytes. Missing, malformed, mutable, linked or mismatched
evidence refuses with `submissions-disabled` before authority use, helper import
or lock creation. The marker is fixed host configuration, not lifecycle state,
and never supplies acceptance, claim, check, review or close-out evidence.
Arguments cannot
select a repository, issue, interpreter, path, environment, endpoint, decision or
helper. Sudo's argument wildcard is only routing; the program validates exact
argument count, order and grammar before reading configuration or accessing
authority. Overlapping calls refuse immediately through an ephemeral lock;
there is no pending-work queue. Refusals do not authorize another route.

## Protected execution chain

The installation contains root-owned request/adapter code, exactly seven
canonical helper modules, a sealed `gh` binary, and an empty Git metadata stub
with one fixed origin and disabled hooks. It never checks out or executes PR
code. Every request first rechecks the merge principal's live account name,
IDs and home (`wrong-execution-principal`) and exact `/usr/bin/false` shell
(`login-capable-principal`), then verifies code hashes, interpreter identity,
protected ancestor ownership/modes and private credential/runtime directories
before importing the canonical helper. A root/OS update
or changed sealed component can require a separately reviewed reinstallation;
there is no automatic helper upgrade or overwrite option.

Those hashes cover only the bundled request/adapter code, helper modules, `gh`
binary, Git stub and the interpreter binary. The OS standard library and system
site-packages, including `.pth` files, are trusted operating-system dependencies
and are not individually inventoried. `python3 -I` and directory ownership narrow
the import path but do not prove the whole import chain, so the operator host
audit below must also show that coding, CI and caller identities cannot write
those OS locations. Until that audit is recorded, runtime import-chain proof
remains unresolved and automatic merge stays unavailable.

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
installation/repository token with **Checks write and Metadata read only**. Token
revocation is best effort: a failed revocation never replaces a confirmed check
or the primary refusal, and GitHub's bounded token expiration otherwise applies.
Signing material, tokens, HTTP errors and
command diagnostics never reach the caller. The adapter is **not** in sudoers;
only the request program can be invoked without an operator password. The
existing generic operator runner is not reused, modified or exposed.

The adapter returns a check only after verifying its positive integer id, App id,
head, name and conclusion against the request. A malformed or uncertain POST
response may still have been recorded by GitHub, and the canonical helper cannot
revoke a post it saw refused. The adapter then posts at most two same-head
`failure` runs through the same scoped token and check endpoint, and refuses. It
never repeats a success, merges or waives a gate. This is not an atomic
revocation guarantee: a transport outage outlasting those attempts can leave an
unconfirmed success visible until a later helper run supersedes it. After an
`authority-unavailable` refusal, read back that head's `aru-merge-authorized`
runs before any further action; never merge that head by hand. A private
adapter failure receives `authority-unavailable`; named canonical gates keep
their gate label. A plain canonical `KernelError` receives `kernel-refused`,
without copying private diagnostic text. The pinned helper uses that one type
for both unmet gates and unreadable GitHub evidence, so the latter label does
not claim a narrower diagnosis: refresh GitHub to distinguish those outcomes.
Neither label grants permission to retry or use another merge route.

The request handles every catchable default-terminating/stopping signal on
the installed OS, including quit, user, alarm and job-control signals. The first
interruption uses `pthread_sigmask(SIG_BLOCK)` to block the entire catchable set
before raising; reentrant Python handler delivery returns without raising again.
The set stays blocked through unwind and same-head failure compensation. Ordinary
exceptions also enter compensation with the full set blocked and queued Python
handlers unable to raise out of cleanup. It tracks attempted success before the
adapter call, so interruption even in the canonical post/try gap attempts a
same-head failure. Canonical revocation is not duplicated after a confirmed
failure. Original handlers are restored while blocked, then the original mask
is restored; pending signals can invoke those original handlers afterward.
Offline regressions exercise second OS signals during unwind and compensation,
reentrant delivery and the real pinned helper, not a live host signal relay.
Uncatchable SIGKILL/SIGSTOP, native faults, host loss and transport outages
cannot guarantee cancellation;
the existing non-atomic authorization/submission boundary still applies. The
sudo policy pins all supported CPU, file-size, memory, stack, descriptor and
process limits for only the digest-sealed request; core dumps are disabled.
The policy also disables caller-selected command timeouts and pins a private
umask with override; the program resets its umask before any runtime file
creation, preventing an unusable lock from a strict caller mask. The
request verifies both soft and hard values before authority access and refuses
if sudo inherited or could not replace caller limits. Host proof must include a
caller with deliberately restricted CPU/file-size limits still reaching an
ordinary safe request denial, without those limits reaching the merge process.
The operator must audit the caller context and prevent coding/CI access to generic
merge credentials and endpoints. A narrowly scoped caller may share the coding
account only if that reviewed trust boundary and every live gate are established;
this does not grant a generic merge credential to that account.

The canonical helper posts authorization only after its final live checks. It
submits the exact head, verifies GitHub's merged head/commit and closes the issue
and Project. A retry of a GitHub-confirmed merge performs only canonical
close-out recovery, never another merge. Once an exact-head GitHub merge is
confirmed, the request clears pending
authorization before further scope/close-out checks. A later failure preserves
its original close-out error without posting a misleading failure check on the
already-merged head. The caller reads GitHub back and uses
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
  --gh-sha256 <independently-reviewed-CLI-SHA256> \
  --repository gillella/Aru_Agentic_SDLC \
  --issue 821 --issue 822 --issue 792 --output <new-package.zip>
```

The archive is reproducible: fixed entry timestamps, modes and order, with no
compression-version dependency. The builder refuses CLI bytes that differ from
the independently reviewed `--gh-sha256`. Commit labels alone are not proof:
independently rebuild from the reviewed Git objects and reviewed CLI bytes,
using the same arguments and `--verify <existing-package.zip>` instead of
`--output`. Verification compares the complete reproduced archive byte for byte;
changed source/helper objects, CLI bytes or archive contents refuse. Only approve
the digest after this independent verification, not from a builder's printed
commit labels. The per-file hashes detect archive tampering but do not by
themselves prove those files correspond to the claimed Git commits.

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
publishing the narrowly scoped `NOPASSWD: NOSETENV` entry. File ownership
and permissions are set through the open descriptor, and credential parents
stay root-owned until the key is staged, so path replacement cannot redirect
root ownership changes.
The installer never creates `enabled.json`: publishing sudo routing permits
`--check` but leaves every real submission mechanically disabled. Human host
proof and enablement below are separate from installation approval.

Before reading the PEM or writing any file or policy, the installer reads the
live `aru-merge` account and refuses unless its shell is exactly `/usr/bin/false`.
It never creates or edits an account: a human administrator sets that shell
beforehand with the host's normal account tools and may confirm it read-only
with `dscl . -read /Users/aru-merge UserShell`. Sudo still executes the fixed
request and the operator setup commands below directly. The installer then
requires the operator to type `AUDITED`, confirming a completed host access
audit: coding, CI and external callers cannot authenticate as `aru-merge` by
password, SSH or other remote access; no other unrestricted passwordless sudo or
runner path reaches it; and no operator credential, sudo timestamp or TTY is
shared with those callers. Human administrators remain trusted. A false shell
closes only interactive login shells and does not prove any other access path;
the actual host access and denial proof below remains mandatory.

The admitted issue and exact head remain fixed through every canonical PR
reread within one request, including its post-merge finalization. A new-process
retry rechecks GitHub's `lastEditedAt`, merged time, body and exact head on every
merged reread; a body edited at or after merge, missing evidence or changed
metadata refuses before close-out. This conservative rule also refuses unrelated
post-merge body edits and timestamp equality. It uses GitHub evidence, not a
local receipt or second lifecycle store. A body change to another issue,
even another installed allowed issue, refuses the request. These refusals also
have the pinned helper's own error type, so a scope or approval change read
after authorization was posted still reaches its same-head failure run. GitHub
reads and the merge operation remain separate; this adds scope enforcement to the canonical
semantic rereads rather than claiming an atomic GitHub transaction. Canonical
close-out is likewise accepted only for exactly the admitted issue; any other
issue set refuses before status or close changes. The pinned helper already
passes that issue. Helpers are immutable here, and any future helper refactor
needs a newly reviewed installation; this wrapper does not promise scope
enforcement for arbitrary future code.

The integration does not treat the Kernel's standalone closing-line parser as
proof of every GitHub closing effect. Each admission requires a matching GitHub
GraphQL snapshot of the PR's body, title, state, head, base, branch and both
repositories; a complete `closingIssuesReferences` inventory including manual
links containing exactly the admitted issue in the installed repository; and
every PR commit's full message. Null, malformed, conflicting or unreadable
evidence refuses. Each inventory is bounded to 100 entries: a remaining page,
count mismatch, duplicate commit or missing head refuses rather than treating
partial data as complete. Merged reads also inspect the actual merge commit's
message and require its oid to match the canonical PR record.

Closing text is deliberately restricted instead of attempting to duplicate
GitHub's keyword parser. Only whole-line local directives for the admitted issue
are accepted. After removing those directives and Markdown heading markers,
text containing closing vocabulary and any remaining `#` or `github.com`
reference refuses. This includes colon/compound/qualified forms, extra targets
in commit messages, and ambiguous examples or unrelated references in the same
text, even when the extra issue is in the installation allowlist. Conventional
`fix:` text without references remains usable. The fixed GitHub merge header's
reference to this PR is removed before checking the generated message; the
branch, title and body are checked separately.

The complete effect proof is reread on every canonical PR read and immediately
before posting success authorization, submitting the merge command, and calling
canonical close-out. Observed drift before submission refuses; drift after a
success authorization triggers the existing failure compensation. Once GitHub
confirms merge, drift blocks close-out but cannot undo the merge or automatic
closures. These separate reads remain non-atomic and do not lock GitHub metadata.

## Operator proof before automatic use

The human configures a fresh, appropriately authorized CLI login under the
protected directory. Do not reuse an agent's environment or copy its credentials.
For example, interactively on the installed host:

```sh
sudo -u aru-merge env -i HOME=/Users/aru-merge \
  PATH=/usr/bin:/bin GH_CONFIG_DIR=/private/var/db/aru-trusted-merge/credentials \
  /private/var/db/aru-trusted-merge/bin/gh auth login \
  --hostname github.com --git-protocol https --web --scopes project --insecure-storage
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
Separately prove the host access audit the installer only confirms: coding, CI
and caller contexts cannot authenticate as `aru-merge` by password, SSH or other
remote access; `sudo -l -U <identity>` for those identities shows no unrestricted
passwordless route to it; and no operator credential, cached sudo authority or
TTY is shared. Also prove those identities cannot write the OS standard library,
system site-packages or `.pth` locations the interpreter loads. Neither the
typed `AUDITED` confirmation nor the false shell is that proof.

Record nonsecret outcomes, exact installed hashes, scope, current head/checks/
review and denials in the GitHub issue. Do not mark criteria from an installer
exit code alone. If effective sudo policy, CLI/Project authorization, App token
scope or immutable dependencies are unproven, automatic merge stays unavailable.
Once proven and accepted, the existing external continuation invokes the bounded
request without a per-PR human click **only after separate human enablement**.
Verify one actual unattended governed merge
and GitHub/issue/Project close-out before calling the release delivered.

## Human enablement and rollback

After the access/import audits, real denials, authentication and eligible
current-head `--check` are recorded, and the human explicitly accepts the
expanded live issue criteria and this exact installed configuration, the human
operator creates the marker from a root Terminal. This is a one-time host switch,
not a per-PR approval or lifecycle receipt. Do not enable from a coding/CI agent,
shared operator TTY or cached sudo authority. The narrow request sudo rule does
not authorize this command. Inspect the displayed repository, issue allowlist
and configuration digest against the accepted installation before typing ENABLE:

```sh
sudo /usr/bin/python3 -I -c '
import hashlib, json, os, stat, sys
from pathlib import Path
if os.geteuid() != 0 or not sys.stdin.isatty():
    raise SystemExit("Human root Terminal required")
root = Path("/private/var/db/aru-trusted-merge")
config = root / "config.json"
for path in [*reversed(config.parents), config]:
    info = path.lstat()
    expected = stat.S_ISREG if path == config else stat.S_ISDIR
    if info.st_uid != 0 or info.st_mode & 0o022 or not expected(info.st_mode):
        raise SystemExit("Unprotected configuration")
raw = config.read_bytes()
value = json.loads(raw)
digest = hashlib.sha256(raw).hexdigest()
print("Repository:", value["repository"], "Issues:", value["issues"], "Config SHA256:", digest)
if input("Audits, denials, preflight and explicit acceptance complete? Type ENABLE: ") != "ENABLE":
    raise SystemExit("Not enabled")
fd = os.open(root / "enabled.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
with os.fdopen(fd, "w") as stream:
    os.fchmod(stream.fileno(), 0o644)
    os.fchown(stream.fileno(), 0, 0)
    json.dump({"version": 1, "config_sha256": digest}, stream, sort_keys=True)
'
```

No credential is read by this enablement step. Existing markers are never
overwritten. Any change to installed configuration invalidates the binding and
requires renewed proof and human acceptance; enabled requests still verify the
protected payload and every live canonical gate. Record the nonsecret enablement
outcome in the issue; GitHub remains the sole lifecycle authority.

Rollback first removes `/private/var/db/aru-trusted-merge/enabled.json` from the
operator context, disabling new submissions while keeping `--check` available.
Then remove only the installed narrow sudo rule to disable future requests
entirely; retain credentials and installation files for deliberate operator
cleanup. An already admitted/in-flight request is not undone by marker removal;
coordinate with the caller and check GitHub for submission/close-out evidence.

The computer must remain on and the external caller must actually run. A sleeping
human is compatible with this route; a sleeping/offline host cannot progress.
This package changes no power settings or scheduled jobs.
