from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("driver_install", Path(__file__).resolve().parents[1] / "install.py")
installer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(installer)


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "source"
    package = source / "aru_project_driver"
    skill = source / "skill"
    package.mkdir(parents=True)
    skill.mkdir()
    (package / "driver.py").write_text("print('driver')\n")
    (package / "scheduler.py").write_text("# scheduler\n")
    (skill / "SKILL.md").write_text("---\nname: hermes-project-driver\n---\nNew skill\n")
    (source / "example-config.json").write_text('{"do_not_copy":true}')
    kernel = tmp_path / "kernel"
    kernel.mkdir()
    home = tmp_path / "hermes"
    config = tmp_path / "project.json"
    config.write_text(json.dumps({"kernel_root": str(kernel), "hermes_home": str(home)}))
    return source, home, config


def test_default_preview_has_no_filesystem_or_scheduler_side_effect(setup):
    source, home, config = setup
    result = installer.install(source, config)
    assert not result["applied"]
    assert not result["activated"]
    assert len(result["files"]) == 3
    assert not home.exists()


def test_apply_preserves_unrelated_config_jobs_and_changed_source_backup(setup):
    source, home, config = setup
    destination = home / "scripts" / "aru_project_driver" / "driver.py"
    destination.parent.mkdir(parents=True)
    destination.write_text("old driver\n")
    (home / "config.yaml").write_text("model: keep-me\n")
    (home / "cron").mkdir()
    (home / "cron" / "jobs.json").write_text('{"jobs":[{"id":"existing"}]}')
    result = installer.install(source, config, apply=True)
    assert result["applied"] and not result["activated"]
    assert destination.read_text() == "print('driver')\n"
    assert len(result["backups"]) == 1
    assert Path(result["backups"][0]).read_text() == "old driver\n"
    assert (home / "config.yaml").read_text() == "model: keep-me\n"
    assert (home / "cron" / "jobs.json").read_text() == '{"jobs":[{"id":"existing"}]}'
    assert not (home / "example-config.json").exists()
    rerun = installer.install(source, config, apply=True)
    assert not rerun["backups"]
    assert not any(f["changed"] for f in rerun["files"])


def test_relative_kernel_and_incomplete_payload_rejected_before_writes(setup):
    source, home, config = setup
    config.write_text(json.dumps({"kernel_root": "relative", "hermes_home": str(home)}))
    with pytest.raises(installer.InstallError, match="absolute path"):
        installer.install(source, config, apply=True)
    assert not home.exists()


def test_destination_symlink_escape_rejected_before_writes(setup, tmp_path):
    source, home, config = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    home.mkdir()
    (home / "scripts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(installer.InstallError, match="outside Hermes home"):
        installer.install(source, config, apply=True)
    assert not list(outside.iterdir())


def _subscriptions(setup):
    source, home, config = setup
    home.mkdir()
    values = json.loads(config.read_text())
    values["projects"] = {"owner/repo": {"webhook_subscriptions": ["project-route"]}}
    config.write_text(json.dumps(values))
    subscriptions = {
        "project-route": {"secret": "PRIVATE-HMAC-SENTINEL", "prompt": "legacy prompt",
                          "skills": ["old-skill"], "deliver": "telegram",
                          "deliver_extra": {"chat_id": "chat"}, "events": ["pull_request"],
                          "custom_setting": {"preserve": True}},
        "unrelated": {"secret": "OTHER-PRIVATE-SENTINEL", "prompt": "leave alone"},
    }
    path = home / "webhook_subscriptions.json"
    path.write_text(json.dumps(subscriptions))
    return path, subscriptions


def test_webhook_opt_in_previews_without_secrets_and_preserves_route_settings(setup):
    import stat
    source, home, config = setup
    path, original = _subscriptions(setup)
    before = path.read_bytes()
    preview = installer.install(source, config, update_webhooks=True)
    assert path.read_bytes() == before
    assert "PRIVATE" not in json.dumps(preview)
    assert preview["webhook_changes"] == [{"subscription": "project-route", "project": "owner/repo", "changed": True}]
    assert not (home / "scripts").exists()
    result = installer.install(source, config, update_webhooks=True, apply=True)
    updated = json.loads(path.read_text())
    assert updated["unrelated"] == original["unrelated"]
    for key in original["project-route"].keys() - {"prompt", "skills"}:
        assert updated["project-route"][key] == original["project-route"][key]
    assert updated["project-route"]["skills"] == ["hermes-project-driver"]
    assert "ARU_PROJECT_DRIVER_AUTHENTICATED_EVENT_V1" in updated["project-route"]["prompt"]
    backup = next(Path(p) for p in result["backups"] if p.endswith("webhook_subscriptions.json"))
    assert backup.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "unauthenticated", "delivery-only"])
def test_bad_webhook_mapping_fails_before_source_install(setup, kind):
    source, home, config = setup
    path, original = _subscriptions(setup)
    values = json.loads(config.read_text())
    if kind == "unknown":
        values["projects"]["owner/repo"]["webhook_subscriptions"] = ["absent-route"]
    elif kind == "duplicate":
        values["projects"]["owner/other"] = {"webhook_subscriptions": ["project-route"]}
    elif kind == "unauthenticated":
        original["project-route"]["secret"] = "INSECURE_NO_AUTH"
    else:
        original["project-route"]["deliver_only"] = True
    config.write_text(json.dumps(values))
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    with pytest.raises(installer.InstallError):
        installer.install(source, config, update_webhooks=True, apply=True)
    assert path.read_bytes() == before
    assert not (home / "scripts").exists()


def test_webhook_apply_detects_concurrent_change(setup):
    source, home, config = setup
    path, original = _subscriptions(setup)
    plan = installer._webhook_plan(config, home)
    original["peer-route"] = {"secret": "peer-secret", "prompt": "peer"}
    path.write_text(json.dumps(original))
    with pytest.raises(installer.InstallError, match="changed during installation"):
        installer._write_subscription_plan(plan, home, home / "state" / "backups")
    assert json.loads(path.read_text()) == original
