"""The scaffolded verifier's read-only workflow-permissions gate.

Split out of `tests/test_init_project.py` when that file reached the 800-line cap:
these two exercise `templates/verify.sh`, not the bootstrap, and the per-file budget
is met by moving tests rather than by compressing them.
"""

from __future__ import annotations

import re
import subprocess

import init_project


def _init_git_repo(path: init_project.Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=path, check=True, capture_output=True)




def test_verify_template_executable_rejects_indented_write_permission_on_macos(
    tmp_path, rehash_manifest,
):
    _init_git_repo(tmp_path)
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    (tmp_path / ".aru/verify-project.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    wf = tmp_path / ".github/workflows/governed-pr.yml"
    content = wf.read_text(encoding="utf-8")
    wf.write_text(
        content.replace("permissions:\n  contents: read", "permissions:\n  contents: write"),
        encoding="utf-8",
    )
    # The managed-file section runs first, so this mutation would stop there instead
    # of reaching the permissions gate under test. See the rehash_manifest fixture.
    rehash_manifest(tmp_path)
    subprocess.run(
        ["git", "commit", "-a", "-m", "add write perm"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    res = subprocess.run(
        ["bash", ".aru/verify.sh"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert res.returncode == 1
    assert "governed workflow permissions must stay read-only" in res.stderr

    wf.write_text(content, encoding="utf-8")
    rehash_manifest(tmp_path)
    subprocess.run(
        ["git", "commit", "-a", "-m", "restore read perm"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    res_ok = subprocess.run(
        ["bash", ".aru/verify.sh"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert res_ok.returncode == 0
    assert "proportional verification passed" in res_ok.stdout


def test_verify_template_workflow_permissions_portable_gate():
    path = init_project.Path(__file__).resolve().parents[1] / "templates/verify.sh"
    content = path.read_text(encoding="utf-8")

    match = re.search(r"if grep -Eq '([^']+)' \"\$\{workflow\}\"; then", content)
    assert match is not None
    perm_re = match.group(1)

    # Indented write / admin permissions must be rejected
    rejected = [
        "permissions:\n  contents: write\n",
        "permissions:\n\tcontents: write\n",
        "permissions:\n    contents: write\n",
        "contents: write\n",
        "permissions:\n  issues: write\n",
        "permissions:\n  pull-requests: write\n",
        "permissions:\n  actions: write\n",
        "permissions:\n  checks: write\n",
        "permissions:\n  deployments: write\n",
        "permissions:\n  packages: write\n",
        "permissions:\n  id-token: write\n",
        "permissions:\n  contents: admin\n",
        "permissions:\n  actions: admin\n",
    ]
    for item in rejected:
        res = subprocess.run(
            ["grep", "-Eq", perm_re],
            input=item,
            text=True,
            capture_output=True,
            check=False,
        )
        assert res.returncode == 0, f"Expected rejection for:\n{item}"

    # Read-only permissions must pass
    accepted = [
        "permissions:\n  contents: read\n  issues: read\n  pull-requests: read\n",
        "permissions:\n  actions: read\n  checks: read\n",
        "permissions:\n  deployments: read\n  packages: read\n  id-token: read\n",
        "permissions: read-all\n",
        "permissions: {}\n",
    ]
    for item in accepted:
        res = subprocess.run(
            ["grep", "-Eq", perm_re],
            input=item,
            text=True,
            capture_output=True,
            check=False,
        )
        assert res.returncode != 0, f"Expected acceptance for:\n{item}"
