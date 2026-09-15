from __future__ import annotations

import subprocess

import pytest

import init_project
VERIFIER = init_project.Path(__file__).resolve().parents[1] / "scripts" / "verify_consumer.sh"



def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def commit(repo, message):
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)


def verify(repo):
    return subprocess.run(
        ["bash", str(VERIFIER)], cwd=repo, capture_output=True, text=True,
    )


@pytest.mark.parametrize("state", ["starter", "missing", "not-executable"])
def test_unconfigured_consumer_cannot_report_success(tmp_path, state):
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    script = tmp_path / ".aru/verify-project.sh"
    if state == "missing":
        script.unlink()
    elif state == "not-executable":
        script.chmod(0o644)
    commit(tmp_path, "unconfigured consumer")
    result = verify(tmp_path)
    assert result.returncode != 0
    assert "proportional verification passed" not in result.stdout
    assert ("not configured" if state == "starter" else "must exist and be executable") in result.stderr


@pytest.mark.parametrize("mode", ["tree", "diff"])
def test_actual_consumer_check_rejects_broken_application_and_accepts_repair(tmp_path, mode):
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    script = tmp_path / ".aru/verify-project.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "python3 -c 'from pathlib import Path; compile(Path(\"app.py\").read_text(), \"app.py\", \"exec\")'\n"
    )
    app = tmp_path / "app.py"
    app.write_text("def answer():\n    return 42\n")
    commit(tmp_path, "configured consumer")
    if mode == "diff":
        git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    app.write_text("def answer(:\n    return 42\n")
    commit(tmp_path, "broken application")
    failed = verify(tmp_path)
    assert failed.returncode != 0
    assert "SyntaxError" in failed.stderr and "consumer verification failed" in failed.stderr
    assert "proportional verification passed" not in failed.stdout
    app.write_text("def answer():\n    return 43\n")
    commit(tmp_path, "repaired application")
    passed = verify(tmp_path)
    assert passed.returncode == 0, passed.stderr
    assert "proportional verification passed" in passed.stdout


def test_scaffold_refuses_to_replace_existing_consumer_checks(tmp_path):
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    script = tmp_path / ".aru/verify-project.sh"
    original = "#!/usr/bin/env bash\nset -euo pipefail\npython3 -m pytest\n"
    script.write_text(original)
    with pytest.raises(init_project.BootstrapError, match="refusing to overwrite"):
        init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    assert script.read_text() == original
