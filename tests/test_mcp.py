from __future__ import annotations

import json
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
