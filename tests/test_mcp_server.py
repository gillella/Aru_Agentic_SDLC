from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integrations" / "mcp"))

import server  # noqa: E402
import tools as tool_table  # noqa: E402


def call(method, params=None, request_id=1):
    return server.handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})


def payload(response):
    return json.loads(response["result"]["content"][0]["text"])


# --- registration -----------------------------------------------------------

def test_initialize_reports_protocol_and_server():
    result = call("initialize")["result"]
    assert result["protocolVersion"] == server.PROTOCOL_VERSION
    assert result["capabilities"]["tools"] == {}
    assert result["serverInfo"]["name"] == "aru-kernel"


def test_tools_list_describes_every_tool_with_a_schema():
    listed = call("tools/list")["result"]["tools"]
    assert {t["name"] for t in listed} == set(tool_table.TOOLS)
    for descriptor in listed:
        schema = descriptor["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert descriptor["description"].strip()


def test_bootstrap_is_not_exposed():
    # Creating repositories, Projects and rulesets stays an operator action.
    scripts = {spec["script"] for spec in tool_table.TOOLS.values()}
    assert "init_project.py" not in scripts


def test_every_tool_maps_to_a_real_kernel_helper():
    for name, spec in tool_table.TOOLS.items():
        assert (ROOT / "scripts" / spec["script"]).is_file(), name


def test_notifications_get_no_reply():
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_and_unknown_tool_are_rejected():
    assert call("nope")["error"]["code"] == server.METHOD_NOT_FOUND
    assert call("tools/call", {"name": "aru_nope"})["error"]["code"] == server.INVALID_PARAMS


# --- argument mapping -------------------------------------------------------

def test_arguments_map_onto_the_helper_flags():
    assert tool_table.build_argv("aru_check_ci", {"pr": 7}) == ["--pr", "7", "--json"]
    assert tool_table.build_argv("aru_claim", {"issue": 5, "agent": "a"}) == ["--issue", "5", "--agent", "a"]


def test_false_booleans_do_not_emit_a_flag():
    argv = tool_table.build_argv("aru_merge", {"pr": 1, "expected_head": "abc", "dry_run": False})
    assert "--dry-run" not in argv
    assert tool_table.build_argv("aru_merge", {"pr": 1, "expected_head": "abc", "dry_run": True}).count("--dry-run") == 1


def test_unknown_arguments_are_refused_not_dropped():
    # Silently dropping an argument the caller believed it passed is how you
    # merge the wrong pull request.
    with pytest.raises(ValueError, match="unknown argument"):
        tool_table.build_argv("aru_merge", {"pr": 1, "expected_head": "abc", "force": True})


def test_missing_required_arguments_are_refused():
    with pytest.raises(ValueError, match="missing required argument"):
        tool_table.build_argv("aru_merge", {"pr": 1})
    assert call("tools/call", {"name": "aru_merge", "arguments": {"pr": 1}})["error"]["code"] == server.INVALID_PARAMS


def test_non_object_arguments_are_refused():
    response = call("tools/call", {"name": "aru_check_ci", "arguments": [1, 2]})
    assert response["error"]["code"] == server.INVALID_PARAMS


# --- a real call, and a real refusal ----------------------------------------

def test_a_successful_call_returns_the_helper_result_unchanged():
    # report.py is the one tool that is read-only and needs no board state.
    response = call("tools/call", {"name": "aru_report",
                                   "arguments": {"repo": "gillella/aru-golden-path-demo",
                                                 "since": "90d", "limit": 3}})
    body = payload(response)
    assert response["result"]["isError"] is False
    assert body["ok"] is True and body["tool"] == "aru_report"
    assert body["result"]["repository"] == "gillella/aru-golden-path-demo"
    assert "merged_pull_requests" in body["result"]


def test_a_refused_transition_carries_the_helpers_own_message():
    response = call("tools/call", {"name": "aru_report", "arguments": {"since": "not-a-window"}})
    body = payload(response)
    assert response["result"]["isError"] is True
    assert body["ok"] is False
    assert body["code"] == "kernel_refusal"
    assert "--since must look like" in body["message"]  # the helper's exact words
    assert body["exit_code"] != 0


def test_a_missing_helper_is_reported_not_guessed(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "scripts_dir", lambda: tmp_path)
    body = server.invoke("aru_check_ci", {"pr": 1})
    assert body["ok"] is False and body["code"] == "kernel_helper_missing"


def test_a_helper_that_hangs_is_bounded(monkeypatch):
    import subprocess as sp

    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(server.subprocess, "run", boom)
    body = server.invoke("aru_check_ci", {"pr": 1}, timeout=1)
    assert body["ok"] is False and body["code"] == "kernel_timeout"


# --- transport ---------------------------------------------------------------

def test_serve_answers_over_stdio_and_survives_a_bad_frame():
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        "not json at all",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    out = StringIO()
    server.serve(stdin=StringIO("\n".join(lines) + "\n"), stdout=out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2]  # bad frame dropped, notification silent


def test_a_handler_crash_becomes_an_error_not_a_dead_transport(monkeypatch):
    monkeypatch.setattr(server, "handle", lambda req: (_ for _ in ()).throw(RuntimeError("boom")))
    out = StringIO()
    server.serve(stdin=StringIO(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"), stdout=out)
    response = json.loads(out.getvalue())
    assert response["error"]["code"] == server.INTERNAL_ERROR


# --- no state, no new dependency --------------------------------------------

def test_the_server_keeps_no_state_and_adds_no_dependency():
    source = (ROOT / "integrations" / "mcp" / "server.py").read_text(encoding="utf-8")
    for forbidden in ("import mcp", "sqlite3", "shelve", "pickle", "global ", "open("):
        assert forbidden not in source, forbidden


def test_nothing_in_scripts_imports_the_adapter():
    for path in (ROOT / "scripts").glob("*.py"):
        assert "integrations" not in path.read_text(encoding="utf-8"), path


def test_no_added_file_exceeds_the_per_file_cap():
    # integrations/ is exempt from test_surface.py's cap, so hold it here.
    for path in (ROOT / "integrations" / "mcp").glob("*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 800, path
