from __future__ import annotations

import json
import os
import subprocess
import sys

from myx.mcp import TOOLS, handle_tool


def test_mcp_tools_list_shape():
    assert len(TOOLS) >= 12
    names = {t["name"] for t in TOOLS}
    assert "myx_play" in names
    assert "myx_search" in names
    assert "myx_state" in names
    assert "myx_skip" in names
    assert "myx_volume" in names
    for t in TOOLS:
        assert "name" in t and "description" in t and "inputSchema" in t


def test_mcp_state_via_handle(tmp_path, monkeypatch):
    import myx.mcp as mcp

    fake_state = {"playing": True, "title": "Hello", "queue": []}
    monkeypatch.setattr(mcp, "_read_state", lambda: fake_state)
    resp = handle_tool("myx_state", {})
    assert resp["isError"] is False
    assert "Hello" in resp["content"][0]["text"]


def test_mcp_stdio_roundtrip():
    code = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
    )
    proc = subprocess.run(
        [sys.executable, "-m", "myx.mcp"],
        input=code,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 0
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    by_id = {l.get("id"): l for l in lines if "id" in l}
    assert "result" in by_id[1]
    assert "tools" in by_id[2]["result"]
    assert len(by_id[2]["result"]["tools"]) >= 12


def test_cli_state_outputs_json(tmp_path):
    import myx.cli as cli

    # --state should not require mpv, just cat state file
    assert cli.parse_args(["--state"]).state is True
    assert cli.parse_args(["mcp"]).subcommand == "mcp"


def _run_stdio(payload: str) -> list:
    proc = subprocess.run(
        [sys.executable, "-m", "myx.mcp"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]


def test_mcp_batch_request_does_not_crash():
    """BUG-004: a JSON-RPC batch (array) must not kill the server."""
    code = '[{"jsonrpc":"2.0","id":1,"method":"ping"},{"jsonrpc":"2.0","id":2,"method":"tools/list"}]\n'
    lines = _run_stdio(code)
    assert len(lines) == 1, "batch must answer with a single array response"
    batch = lines[0]
    assert isinstance(batch, list) and len(batch) == 2
    by_id = {r["id"]: r for r in batch}
    assert "result" in by_id[1] and "tools" in by_id[2]["result"]


def test_mcp_bad_json_returns_parse_error():
    """BUG-004: malformed JSON must get -32700, not a silent drop (client would hang)."""
    lines = _run_stdio("this is not json\n")
    assert len(lines) == 1
    assert lines[0]["error"]["code"] == -32700
    assert lines[0]["id"] is None


def test_mcp_non_object_request_is_invalid():
    code = '"just a string"\n[42]\n'
    lines = _run_stdio(code)
    assert len(lines) == 2
    # bare non-object -> single error response
    assert lines[0]["error"]["code"] == -32600
    # batch of one invalid item -> array of one error response (JSON-RPC 2.0 §6)
    assert isinstance(lines[1], list) and lines[1][0]["error"]["code"] == -32600


def test_mcp_notification_not_answered():
    """JSON-RPC 2.0: notifications (no id) MUST NOT receive a response."""
    code = '{"jsonrpc":"2.0","method":"notifications/whatever"}\n{"jsonrpc":"2.0","id":5,"method":"ping"}\n'
    lines = _run_stdio(code)
    assert len(lines) == 1
    assert lines[0]["id"] == 5


def test_mcp_unknown_method_and_server_survives():
    code = (
        '{"jsonrpc":"2.0","id":1,"method":"no/such/method"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    lines = _run_stdio(code)
    by_id = {l.get("id"): l for l in lines}
    assert by_id[1]["error"]["code"] == -32601
    assert "result" in by_id[2], "server must keep serving after an unknown method"


def test_mcp_empty_batch_is_invalid_request():
    lines = _run_stdio("[]\n")
    assert lines[0]["error"]["code"] == -32600


def test_mcp_tools_call_rejects_non_object_params():
    lines = _run_stdio('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[]}\n')
    assert lines[0]["error"]["code"] == -32602


def test_mcp_state_rejects_dead_daemon_state(tmp_path, monkeypatch):
    import myx.mcp as mcp

    state = tmp_path / "myx-state.json"
    state.write_text('{"playing": true}')
    (tmp_path / "myx.lock").write_text("999999999")
    (tmp_path / "myx-cmd.sock").touch()
    monkeypatch.setattr(mcp, "STATE_PATH", state)
    monkeypatch.setattr(mcp, "PLAYER_STATE_PATH", state)
    monkeypatch.setattr(mcp, "_get_fallback_tmpdirs", lambda: [])
    monkeypatch.setattr(mcp, "_daemon_reachable", lambda path: False)
    assert mcp._read_state()["playing"] is False


def test_mcp_state_accepts_old_state_from_live_daemon(tmp_path, monkeypatch):
    import myx.mcp as mcp

    state = tmp_path / "myx-state.json"
    state.write_text('{"playing": true}')
    os.utime(state, (0, 0))
    (tmp_path / "myx.lock").write_text("123")
    (tmp_path / "myx-cmd.sock").touch()
    monkeypatch.setattr(mcp, "STATE_PATH", state)
    monkeypatch.setattr(mcp, "PLAYER_STATE_PATH", state)
    monkeypatch.setattr(mcp, "_get_fallback_tmpdirs", lambda: [])
    monkeypatch.setattr(mcp.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(mcp, "_daemon_reachable", lambda path: True)
    assert mcp._read_state()["playing"] is True


def test_initialize_response_precedes_no_server_notification():
    lines = _run_stdio('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
    assert len(lines) == 1
    assert lines[0]["id"] == 1
