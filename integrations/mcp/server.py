#!/usr/bin/env python3
"""An MCP server over the supported kernel lifecycle commands.

An agent drives the kernel from tool schemas and structured refusals instead of
several thousand tokens of prose restating a command sequence and its flags.

MCP is an open protocol, not a vendor framework, so one server serves any MCP
client. This is not an agent-framework adapter of the kind deleted with the
Hermes, chopin and personas trees: it ships no loop, no scheduler, no persona
catalog, and no state.

An adapter, never a gate. Every tool shells out to the existing helper, so
nothing here can authorize a transition the command line would refuse, and a
refusal is returned with the helper's own message rather than reinterpreted.

Stdio transport is newline-delimited JSON-RPC 2.0, implemented on the standard
library so the kernel gains no runtime dependency.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools as tool_table  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "aru-kernel", "version": "1.0.0"}
DEFAULT_TIMEOUT = 900

# JSON-RPC reserved codes, plus the one this server adds.
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def scripts_dir() -> Path:
    """Locate the kernel helpers.

    The co-located checkout wins over ARU_SDLC_HOME on purpose. This server and
    those helpers ship and change together, so a stale environment variable must
    not silently point a governance tool at a different kernel revision than the
    one it was built against. ARU_SDLC_HOME is the fallback for an installation
    that does not carry its own scripts.
    """
    local = Path(__file__).resolve().parents[2] / "scripts"
    if local.is_dir():
        return local
    home = os.environ.get("ARU_SDLC_HOME")
    if home and (Path(home) / "scripts").is_dir():
        return Path(home) / "scripts"
    return local


def invoke(name: str, arguments: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run one helper and return its result, or its refusal, structured.

    A non-zero exit is the kernel refusing. That is reported as a tool error
    carrying the helper's exact message and a stable code, never swallowed into
    a success with partial content.
    """
    spec = tool_table.TOOLS[name]
    argv = tool_table.build_argv(name, arguments)
    script = scripts_dir() / spec["script"]
    if not script.is_file():
        return _error("kernel_helper_missing", f"{script} is not present", tool=name)
    try:
        done = subprocess.run(
            [sys.executable, str(script), *argv],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONPATH": str(scripts_dir())},
        )
    except subprocess.TimeoutExpired:
        return _error("kernel_timeout", f"{spec['script']} did not finish within {timeout}s", tool=name)
    except OSError as exc:
        return _error("kernel_unavailable", str(exc), tool=name)

    if done.returncode != 0:
        message = (done.stderr or done.stdout or "").strip().splitlines()
        return _error(
            "kernel_refusal",
            message[-1] if message else f"{spec['script']} exited {done.returncode}",
            tool=name, exit_code=done.returncode,
        )
    if not spec["json"]:
        return {"ok": True, "tool": name, "output": done.stdout.strip()}
    try:
        return {"ok": True, "tool": name, "result": json.loads(done.stdout or "null")}
    except json.JSONDecodeError:
        # A helper that claims JSON and does not produce it is a defect, not a
        # result. Reporting the raw text as success would hide it.
        return _error("kernel_output_unreadable", "helper did not return JSON", tool=name)


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message, **extra}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request. Returns None for notifications."""
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None  # a notification; MCP requires no reply

    if method == "initialize":
        return _ok(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "tools/list":
        return _ok(request_id, {"tools": tool_table.descriptors()})
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        if name not in tool_table.TOOLS:
            return _fail(request_id, INVALID_PARAMS, f"unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _fail(request_id, INVALID_PARAMS, "arguments must be an object")
        try:
            outcome = invoke(name, arguments)
        except ValueError as exc:
            return _fail(request_id, INVALID_PARAMS, str(exc))
        return _ok(request_id, {
            "content": [{"type": "text", "text": json.dumps(outcome, sort_keys=True)}],
            "isError": not outcome["ok"],
        })
    if method in {"ping"}:
        return _ok(request_id, {})
    return _fail(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _fail(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(stdin: Any = None, stdout: Any = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue  # an unparseable frame is dropped, never guessed at
        if not isinstance(request, dict):
            continue
        try:
            response = handle(request)
        except Exception as exc:  # a crash must not take the transport down
            response = _fail(request.get("id"), INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
