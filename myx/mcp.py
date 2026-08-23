"""MYX MCP server — stdio JSON-RPC 2.0, no external deps.

Exposes MYX player controls as MCP tools so any agent can drive music.
Reuses the existing daemon IPC (CMD_SOCK + STATE_PATH); does not own playback.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from . import sources

try:
    from .player import STATE_PATH as PLAYER_STATE_PATH, CMD_SOCK as PLAYER_CMD_SOCK
except Exception:
    PLAYER_STATE_PATH = Path(gettempdir()) / "myx-state.json"
    PLAYER_CMD_SOCK = Path(gettempdir()) / "myx-cmd.sock"

STATE_PATH = Path(os.environ.get("MYX_STATE", PLAYER_STATE_PATH))
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", PLAYER_CMD_SOCK))

SERVER_NAME = "myx"
SERVER_VERSION = "0.2.0"


def _send_cmd(cmd: str, **payload: Any) -> dict[str, Any]:
    msg = {"cmd": cmd, **payload}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2.0)
        s.connect(str(CMD_SOCK))
        s.sendall((json.dumps(msg) + "\n").encode())
        data = b""
        s.settimeout(2.0)
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
    line = data.decode(errors="ignore").strip().splitlines()[0] if data else "{}"
    return json.loads(line or "{}")


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"playing": False, "paused": True, "title": None, "artist": None, "queue": []}
    except Exception as e:
        return {"error": str(e), "playing": False}


def _spotify_search(q: str) -> list[dict[str, Any]]:
    """Reuse sources.search which is ytsearch20."""
    return sources.search(q)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "myx_play",
        "description": "Play a YouTube/YT Music URL or video id. Starts the radio; if daemon is running, appends and plays. Example: https://www.youtube.com/watch?v=dQw4w9WgXcQ or dQw4w9WgXcQ",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "YouTube URL or video id"}},
            "required": ["url"],
        },
    },
    {
        "name": "myx_search",
        "description": "Search YouTube (ytsearch20) and enqueue all results into the MYX queue. Returns queued count. Agents: use this to add music without needing a URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query, e.g. 'weeknd blinding lights'"}},
            "required": ["query"],
        },
    },
    {
        "name": "myx_state",
        "description": "Get current player state: playing/paused, title/artist, position/duration, volume, queue, lyrics (lrc/plain). Pollable at 4Hz.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_queue",
        "description": "Get the current queue (upcoming tracks after the current one). Same as state.queue but trimmed.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_skip",
        "description": "Skip to next track (playlist-next).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_prev",
        "description": "Go to previous track (playlist-prev).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_pause",
        "description": "Pause playback.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_resume",
        "description": "Resume playback.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_toggle",
        "description": "Toggle pause/resume.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_volume",
        "description": "Set or adjust volume. If 'volume' is 0..100, sets absolute. If 'delta' is provided (±5 typical), adjusts relative.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "volume": {"type": "integer", "description": "Absolute volume 0..100"},
                "delta": {"type": "integer", "description": "Relative delta, e.g. 5 or -5"},
            },
        },
    },
    {
        "name": "myx_shuffle",
        "description": "Shuffle the queue.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_lyrics",
        "description": "Get lyrics for the current track (lrc synced + plain). Returns {lrc, plain}.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_remove",
        "description": "Remove a track from the queue by index (0 = first after current).",
        "inputSchema": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "Queue index to remove"}},
            "required": ["index"],
        },
    },
    {
        "name": "myx_play_index",
        "description": "Jump to a queue track by index (0 = first after current).",
        "inputSchema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
        },
    },
]


def _tool_result(content: Any) -> dict[str, Any]:
    text = json.dumps(content, indent=2, ensure_ascii=False) if not isinstance(content, str) else content
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "myx_play":
            url = str(args.get("url", "")).strip()
            if not url:
                return _tool_error("url required")
            # try daemon; if not running, boot it
            try:
                resp = _send_cmd("load", arg=url)
                if resp.get("ok"):
                    # also jump to it if desired? For now append + jump
                    st = _read_state()
                    # find index of newly added if possible — just return ok
                    return _tool_result({"ok": True, "queued": url, "state": st})
                # if daemon not reachable, try booting via CLI helper
                raise RuntimeError(resp.get("error", "daemon not reachable"))
            except Exception:
                # boot daemon as subprocess (fire-and-forget)
                subprocess.Popen([sys.executable, "-m", "myx.player", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.5)
                return _tool_result({"ok": True, "booted": True, "url": url})

        elif name == "myx_search":
            q = str(args.get("query", "")).strip()
            if not q:
                return _tool_error("query required")
            resp = _send_cmd("search", arg=q)
            if resp.get("ok") is False and resp.get("error"):
                # daemon not running — fallback to direct search (still useful)
                try:
                    items = _spotify_search(q)
                    return _tool_result({"ok": True, "queued": len(items), "results": items[:5], "note": "daemon not running, results not enqueued"})
                except Exception as e:
                    return _tool_error(str(e))
            return _tool_result(resp)

        elif name == "myx_state":
            return _tool_result(_read_state())

        elif name == "myx_queue":
            st = _read_state()
            return _tool_result({"queue": st.get("queue", []), "current": {"title": st.get("title"), "artist": st.get("artist"), "id": st.get("id")}})

        elif name == "myx_skip":
            return _tool_result(_send_cmd("skip"))

        elif name == "myx_prev":
            return _tool_result(_send_cmd("prev"))

        elif name == "myx_pause":
            return _tool_result(_send_cmd("pause"))

        elif name == "myx_resume":
            return _tool_result(_send_cmd("resume"))

        elif name == "myx_toggle":
            return _tool_result(_send_cmd("toggle"))

        elif name == "myx_volume":
            if "delta" in args and args["delta"] is not None:
                return _tool_result(_send_cmd("vol", value=int(args["delta"])))
            if "volume" in args and args["volume"] is not None:
                return _tool_result(_send_cmd("vol", arg=int(args["volume"])))
            return _tool_error("provide volume (0..100) or delta (±N)")

        elif name == "myx_shuffle":
            return _tool_result(_send_cmd("shuffle"))

        elif name == "myx_lyrics":
            st = _read_state()
            return _tool_result({"lrc": st.get("lrc"), "plain": st.get("plain") or st.get("lyrics"), "title": st.get("title"), "artist": st.get("artist")})

        elif name == "myx_remove":
            idx = int(args.get("index", 0))
            return _tool_result(_send_cmd("del", arg=idx))

        elif name == "myx_play_index":
            idx = int(args.get("index", 0))
            return _tool_result(_send_cmd("play_index", arg=idx))

        else:
            return _tool_error(f"unknown tool: {name}")
    except Exception as e:
        return _tool_error(str(e))


def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve_stdio() -> int:
    """Run MCP JSON-RPC 2.0 over stdio. Handles initialize, tools/list, tools/call, ping, shutdown."""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method")

        def reply(result: Any) -> None:
            _write({"jsonrpc": "2.0", "id": mid, "result": result})

        def error(code: int, message: str) -> None:
            _write({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

        try:
            if method == "initialize":
                reply({
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                })
                # spec says server should send initialized notification; we just continue
                _write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                reply({"tools": TOOLS})
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                result = handle_tool(name, args)
                reply(result)
            elif method == "ping":
                reply({})
            elif method == "shutdown":
                reply({})
                break
            elif method is None:
                error(-32600, "Invalid Request")
            else:
                error(-32601, f"Method not found: {method}")
        except Exception as e:
            if mid is not None:
                error(-32603, str(e))
    return 0


def main(argv: list[str] | None = None) -> int:
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
