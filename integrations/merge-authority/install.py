#!/usr/bin/env python3
"""Human-only installation from an already verified in-memory package.

The documented bootstrap supplies _BUNDLE_BYTES after checking the operator's
approved SHA256. Do not execute a mutable checkout copy of this file as root.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path("/private/var/db/aru-trusted-merge")
POLICY = Path("/private/etc/sudoers.d/aru-trusted-merge")
KERNEL_FILES = (
    "merge_pr.py", "merge_authority.py", "merge_state.py", "common.py",
    "check_ci.py", "fetch_pr_feedback.py", "touches.py",
)
LOCKED_SHELL = "/usr/bin/false"


def unpack(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    required = {"request.py", "app.py", "install.py", "bin/gh", "bin/authority",
                "checkout/.git/config", "checkout/.git/HEAD", "manifest.json"}
    required.update("lib/" + name for name in KERNEL_FILES)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) != len(required) or {item.filename for item in entries} != required:
            raise ValueError("Package inventory is invalid")
        if any(item.file_size > 100_000_000 or stat.S_ISLNK(item.external_attr >> 16) for item in entries):
            raise ValueError("Package contains unsafe entries")
        payload = {item.filename: archive.read(item) for item in entries}
    manifest = json.loads(payload.pop("manifest.json"))
    expected = {"version", "repository", "issues", "source_commit", "helper_commit", "files"}
    if not isinstance(manifest, dict) or set(manifest) != expected or type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("Package manifest is invalid")
    if manifest["files"] != {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}:
        raise ValueError("Package content is invalid")
    if not isinstance(manifest["repository"], str) or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", manifest["repository"]) or ".." in manifest["repository"]:
        raise ValueError("Package repository is invalid")
    issues = manifest["issues"]
    if not isinstance(issues, list) or not issues or len(issues) > 100 or any(type(n) is not int or n <= 0 for n in issues) or len(set(issues)) != len(issues):
        raise ValueError("Package issue scope is invalid")
    for key in ("source_commit", "helper_commit"):
        if not isinstance(manifest[key], str) or not re.fullmatch(r"[0-9a-f]{40}", manifest[key]) or manifest[key] == "0" * 40:
            raise ValueError("Package revisions are invalid")
    return manifest, payload


def root_chain(path: Path) -> None:
    for item in [*reversed(path.parents), path]:
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError("Protected installation chain is mutable")


def system_interpreter() -> Path:
    # Apple CLT's sys.executable is a root-owned symlink on the target Mini.
    # Check its logical chain, then the resolved binary's full physical chain.
    alias = Path(sys.executable)
    for item in [*reversed(alias.parents), alias]:
        info = item.lstat()
        if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise ValueError("System interpreter alias is mutable")
    resolved = alias.resolve(strict=True)
    root_chain(resolved)
    return resolved


def require_locked_principal(principal) -> None:
    # Read-only check of live account metadata. A human administrator sets
    # the shell beforehand; this installer never creates or edits an account.
    if principal.pw_name != "aru-merge" or principal.pw_shell != LOCKED_SHELL:
        raise ValueError("Merge principal aru-merge must already have the non-login shell "
                         f"{LOCKED_SHELL}; accounts are never changed here")


LIMITS = {"as": None, "core": 0, "cpu": None, "data": None, "fsize": None,
          "memlock": None, "nofile": 1024, "nproc": 1024, "rss": None, "stack": 8_388_608}


def policy_text(caller: str, principal: str, interpreter_digest: str) -> str:
    if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) for name in (caller, principal)) or caller == principal:
        raise ValueError("Invalid distinct caller and principal")
    if not re.fullmatch(r"[0-9a-f]{64}", interpreter_digest):
        raise ValueError("Invalid interpreter digest")
    # Wildcards are sudo syntax, not an input validator. request.py accepts only
    # exactly four arguments plus optional --check before any authority access.
    options = ", ".join(f"rlimit_{name}={value if value is not None else 'infinity'}"
                        for name, value in LIMITS.items())
    return (f"Cmnd_Alias ARU_TRUSTED_MERGE = sha256:{interpreter_digest} "
            f"/usr/bin/python3 -I {ROOT}/request.py --pr * --expected-head *\n"
            f"Defaults!ARU_TRUSTED_MERGE {options}, umask=0077, umask_override, "
            "command_timeout=0, !user_command_timeouts\n"
            f"{caller} ALL=({principal}) NOPASSWD: NOSETENV: ARU_TRUSTED_MERGE\n")


def private_key(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 32_768:
            raise ValueError("Operator PEM must be a private regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(32_769)
        if not data.startswith(b"-----BEGIN ") or b"PRIVATE KEY-----" not in data[:100]:
            raise ValueError("Operator PEM is not a private key")
        return data
    finally:
        os.close(fd)


def write_file(path: Path, data: bytes, mode: int, uid: int = 0, gid: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        os.fchown(stream.fileno(), uid, gid)


def install_credentials(principal, key: bytes) -> None:
    # Keep parents root-owned until the private key has been staged safely.
    for directory in (ROOT / "credentials", ROOT / "runtime"):
        directory.mkdir(mode=0o700)
    write_file(ROOT / "credentials/merge-app.pem", key, 0o600, principal.pw_uid, principal.pw_gid)
    for directory in (ROOT / "credentials", ROOT / "runtime"):
        os.chown(directory, principal.pw_uid, principal.pw_gid)


def install_payload(manifest: dict, payload: dict[str, bytes], caller: str, principal, installation: int, key: bytes) -> None:
    if ROOT.exists() or ROOT.is_symlink() or POLICY.exists() or POLICY.is_symlink():
        raise ValueError("Existing installation/policy is preserved; no overwrite")
    staging = POLICY.with_name(".aru-trusted-merge-candidate")
    if staging.exists() or staging.is_symlink():
        raise ValueError("Existing policy staging file is preserved")
    require_locked_principal(principal)
    root_chain(ROOT.parent)
    root_chain(POLICY.parent)
    python = system_interpreter()
    root_chain(Path("/usr/bin/python3"))
    root_chain(Path("/usr/bin/openssl"))
    config = {"version": 1, "repository": manifest["repository"], "issues": manifest["issues"],
              "principal_uid": principal.pw_uid, "principal_gid": principal.pw_gid,
              "principal_home": principal.pw_dir, "helper_commit": manifest["helper_commit"],
              "authority_runner": str(ROOT / "bin/authority"), "authority_app_id": 4921120,
              "authority_installation_id": installation, "python": str(python),
              "python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
              "files": {name: digest for name, digest in manifest["files"].items() if name != "install.py"}}
    config["authority_files"] = {str(ROOT / name): config["files"][name] for name in ("app.py", "bin/authority")}
    policy = policy_text(caller, principal.pw_name, hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest()).encode()
    ROOT.mkdir(mode=0o755)
    ROOT.chmod(0o755)
    try:
        for name in ("lib", "bin", "checkout", "checkout/.git"):
            directory = ROOT / name
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
        for name, data in payload.items():
            if name != "install.py":
                write_file(ROOT / name, data, 0o755 if name.startswith("bin/") else 0o644)
        for directory in (ROOT / "checkout/.git/objects", ROOT / "checkout/.git/refs"):
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
        install_credentials(principal, key)
        write_file(ROOT / "config.json", json.dumps(config, sort_keys=True).encode(), 0o644)
        write_file(staging, policy, 0o440)
        subprocess.run(["/usr/sbin/visudo", "-cf", str(staging)], check=True, capture_output=True)
        # Install the capability only after validating every public payload hash.
        for name, digest in config["files"].items():
            root_chain(ROOT / name)
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
                raise ValueError("Installed content changed")
        # Same-directory exclusive publication also preserves a policy created
        # by another operator after the initial existence check.
        os.link(staging, POLICY, follow_symlinks=False)
        staging.unlink()
    except Exception:
        if staging.exists():
            staging.unlink()
        shutil.rmtree(ROOT)
        raise


def main() -> None:
    if os.geteuid() != 0 or not sys.flags.isolated or sys.platform != "darwin" or not sys.stdin.isatty():
        raise ValueError("Installation requires human root TTY, Apple Python -I")
    system_interpreter()
    root_chain(Path("/usr/bin/python3"))
    raw = globals().get("_BUNDLE_BYTES")
    if not isinstance(raw, bytes):
        raise ValueError("Use the digest-verified package bootstrap")
    manifest, payload = unpack(raw)
    print("Repository:", manifest["repository"], "Issues:", manifest["issues"])
    print("Reviewed code:", manifest["source_commit"], "Canonical helper:", manifest["helper_commit"])
    if input("Install this reviewed scope? Type INSTALL: ") != "INSTALL":
        raise ValueError("Operator declined installation")
    caller = input("Authorized existing caller (e.g. gillella): ").strip()
    principal = pwd.getpwnam("aru-merge")
    if pwd.getpwnam(caller).pw_uid in (0, principal.pw_uid) or principal.pw_uid == 0 or principal.pw_gid == 0:
        raise ValueError("Caller and merge user must be separate non-root identities")
    require_locked_principal(principal)
    # The false shell narrows interactive login only. The human operator attests
    # the host audit; this installer neither performs nor replaces that proof.
    print("Access audit: no coding, CI or external caller can authenticate as aru-merge by password,")
    print("SSH or remote access; no other unrestricted passwordless sudo or runner path reaches it;")
    print("no operator credential, sudo timestamp or TTY is shared with those callers.")
    if input("Is this host access audit complete? Type AUDITED: ") != "AUDITED":
        raise ValueError("Operator did not confirm the completed access audit")
    number = input("Existing merge App 4921120 installation ID: ").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,11}", number):
        raise ValueError("Invalid installation ID")
    # Enter only on the operator's host. No path, PEM, token or login is sent to
    # an agent; no credential material is printed or included in the package.
    key = private_key(Path(input("Private local merge-App PEM path: ").strip()))
    install_payload(manifest, payload, caller, principal, int(number), key)
    print("Installed bounded request. Configure the protected gh login and prove preflight/denials before use.")


if __name__ == "__main__":
    main()
