"""Synthetic transport fixtures; no provider, credential or network access."""
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_quota import qh as qh
from aru_project_driver import quota, quota_collect
from aru_project_driver.config import DriverError


@pytest.mark.parametrize("family", ["claude-code", "xai-cursor", "google-antigravity"])
def test_unsupported_unattended_surfaces_are_bounded_unknown(qh, monkeypatch, family):
    monkeypatch.undo()
    qh.config.lanes["codex-one"]["family"] = family
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unknown adapter must not run a probe"))
    data = quota_collect.collect(qh.config, REPO := "example/project", "codex-one")
    assert data["state"] == "unknown" and data["windows"] == {} and not data["identity_verified"]
    assert quota.validate(data, qh.config.lane(REPO, "codex-one"), time.time(), 120)


def test_missing_jsonschema_is_explicit_unavailable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with pytest.raises(DriverError, match="schema-validator-unavailable"):
        quota_collect.codex_read(["/not/executed"], tmp_path)


def fake_cli(tmp_path, mode):
    script = tmp_path / "codex.py"
    script.write_text('''import json,sys,time
from pathlib import Path
mode = ''' + repr(mode) + '''
if "generate-json-schema" in sys.argv:
    root=Path(sys.argv[-1])/"v2"
    root.mkdir()
    for name in ("GetAccountResponse", "GetAccountRateLimitsResponse"):
        (root/(name+".json")).write_text(json.dumps({"title":name,"type":"object"}))
else:
    for line in sys.stdin:
        request=json.loads(line)
        if "id" not in request: continue
        if mode == "timeout": time.sleep(5)
        if mode == "malformed":
            print("not json", flush=True)
            continue
        if mode == "error":
            print(json.dumps({"id":request["id"],"error":{"message":"sensitive provider error"}}), flush=True)
            continue
        result={"account":{"type":"chatgpt"},"requiresOpenaiAuth":True}
        if request["method"] == "account/rateLimits/read": result={"rateLimits":{}}
        if mode == "account-change" and request["id"] == 4: result["account"]={"type":"apiKey"}
        if mode == "schema-invalid": result=[]
        print(json.dumps({"method":"ignored-notification","params":{}}),flush=True)
        print(json.dumps({"id":request["id"],"result":result}),flush=True)
''')
    return [sys.executable, str(script)]


@pytest.mark.parametrize("mode", ["valid", "timeout", "malformed", "error", "account-change", "schema-invalid"])
def test_supported_transport_is_bounded_and_validates_every_response(tmp_path, monkeypatch, mode):
    class Invalid(Exception):
        pass
    calls = []
    def validate(value, schema):
        calls.append(schema["title"])
        if not isinstance(value, dict):
            raise Invalid()
    # Protocol tests inject a schema-validator double; genuine Draft7 validation
    # is independently captured by the live artifact, never claimed by this fixture.
    monkeypatch.setitem(sys.modules, "jsonschema", SimpleNamespace(
        Draft7Validator=SimpleNamespace(check_schema=lambda s: None), SchemaError=Invalid,
        ValidationError=Invalid, validate=validate))
    start = time.monotonic()
    if mode == "valid":
        account, limits = quota_collect.codex_read(fake_cli(tmp_path, mode), tmp_path, timeout=1)
        assert account["account"]["type"] == "chatgpt" and limits == {"rateLimits": {}}
        assert calls == ["GetAccountResponse", "GetAccountResponse", "GetAccountRateLimitsResponse"]
    else:
        with pytest.raises((DriverError, ValueError)):
            quota_collect.codex_read(fake_cli(tmp_path, mode), tmp_path, timeout=.2)
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("change", ["wrong-pool", "model", "disagree", "negative", "nan", "extra-duration", "blocked"])
def test_provider_pool_refusals_and_explicit_exhaustion(qh, change):
    lane = qh.config.lanes["codex-one"]
    lane["quota"]["account_sha256"] = quota.digest("fixture")
    pool = {"limitId": "codex", "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": int(time.time()) + 1000}}
    if change == "wrong-pool":
        pool["limitId"] = "other"
    if change == "model":
        pool["normalModelSlug"] = "other"
    if change == "negative":
        pool["primary"]["usedPercent"] = -1
    if change == "nan":
        pool["primary"]["usedPercent"] = float("nan")
    if change == "extra-duration":
        pool["primary"]["windowDurationMins"] = 600
    limits = {"accountId": "fixture", "ordinaryUsageAllowed": change != "blocked", "rateLimits": pool,
              "rateLimitsByLimitId": {"codex": pool.copy()}}
    if change == "disagree":
        limits["rateLimits"] = {"limitId": "codex"}
    if change == "blocked":
        data = quota_collect.normalize(lane, {"account": {"type": "chatgpt"}}, limits, time.time())
        assert data["state"] == "exhausted" and data["reason"] == "provider-blocked"
    else:
        with pytest.raises(DriverError):
            quota_collect.normalize(lane, {"account": {"type": "chatgpt"}}, limits, time.time())
