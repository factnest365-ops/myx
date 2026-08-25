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
import tempfile
import threading
import time
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from . import sources

try:
    from .player import STATE_PATH as PLAYER_STATE_PATH, CMD_SOCK as PLAYER_CMD_SOCK, MPV_SOCK as PLAYER_MPV_SOCK
except Exception:
    PLAYER_STATE_PATH = Path(gettempdir()) / "myx-state.json"
    PLAYER_CMD_SOCK = Path(gettempdir()) / "myx-cmd.sock"
    PLAYER_MPV_SOCK = Path(gettempdir()) / "myx-mpv.sock"

STATE_PATH = Path(os.environ.get("MYX_STATE", PLAYER_STATE_PATH))
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", PLAYER_CMD_SOCK))


def _get_fallback_tmpdirs() -> list[Path]:
    dirs = [Path("/tmp"), Path(tempfile.gettempdir())]
    if tmp := os.environ.get("MYX_FALLBACK_TMP"):
        dirs.append(Path(tmp))
    env_tmp = os.environ.get("TMPDIR")
    if env_tmp:
        dirs.append(Path(env_tmp))
    return dirs


def _candidate_states() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in [STATE_PATH, PLAYER_STATE_PATH] + [d / "myx-state.json" for d in _get_fallback_tmpdirs()]:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _candidate_cmd_socks() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in [CMD_SOCK, PLAYER_CMD_SOCK] + [d / "myx-cmd.sock" for d in _get_fallback_tmpdirs()]:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _candidate_mpv_socks() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in [PLAYER_MPV_SOCK, CMD_SOCK, PLAYER_CMD_SOCK] + [d / "myx-mpv.sock" for d in _get_fallback_tmpdirs()]:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


SERVER_NAME = "myx"
SERVER_VERSION = "0.2.0"


def _send_cmd(cmd: str, **payload: Any) -> dict[str, Any]:
    msg = {"cmd": cmd, **payload}
    last_err: Exception | None = None
    for sock_path in _candidate_cmd_socks():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(sock_path))
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
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"myx daemon not reachable (tried {', '.join(str(p) for p in _candidate_cmd_socks())}): {last_err}")


def _mpv_direct(command: list[Any]) -> Any:
    msg = {"command": command}
    last_err: Exception | None = None
    for sock_path in _candidate_mpv_socks():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(sock_path))
                s.sendall((json.dumps(msg) + "\n").encode())
                data = b""
                s.settimeout(2.0)
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
            line = data.decode(errors="ignore").strip().splitlines()[0] if data else "{}"
            resp = json.loads(line or "{}")
            err = resp.get("error")
            if err not in (None, "success"):
                raise RuntimeError(err)
            return resp.get("data")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"mpv not reachable (tried {', '.join(str(p) for p in _candidate_mpv_socks())}): {last_err}")


def _url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


_meta_cache: dict[str, dict[str, Any]] = {}


def _meta_with_timeout(video_id: str, timeout: float = 2.0) -> dict[str, Any] | None:
    res: dict[str, Any] | None = None

    def _run() -> None:
        nonlocal res
        try:
            res = sources.meta(video_id)
        except sources.SourcesError:
            res = {}

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout)
    return res


def _hydrate_queue(state: dict[str, Any]) -> dict[str, Any]:
    queue = state.get("queue") or []
    out: list[dict[str, Any]] = []
    for item in queue:
        item = dict(item)
        if item.get("title") is None and item.get("id"):
            vid = str(item["id"])
            if vid not in _meta_cache:
                _meta_cache[vid] = _meta_with_timeout(vid) or {}
            meta = _meta_cache[vid]
            if meta:
                item.setdefault("title", meta.get("title"))
                item.setdefault("artist", meta.get("artist"))
                item.setdefault("duration", meta.get("duration"))
                item.setdefault("thumb", meta.get("thumb"))
        out.append(item)
    state = dict(state)
    state["queue"] = out
    return state


def _read_state() -> dict[str, Any]:
    for p in _candidate_states():
        try:
            if p.exists():
                state = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(state, dict):
                    return _hydrate_queue(state)
                return state
        except Exception:
            continue
    return {"playing": False, "paused": True, "title": None, "artist": None, "queue": []}


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
            vid = None
            import re as _re
            for pat in (r"[?&]v=([^&]+)", r"youtu\.be/([^?&/]+)"):
                m = _re.search(pat, url)
                if m:
                    vid = m.group(1)
                    break
            if _re.fullmatch(r"[A-Za-z0-9_-]{6,}", url):
                vid = url
            try:
                if vid:
                    resp = _send_cmd("play_id", arg=vid)
                    if resp.get("ok"):
                        time.sleep(0.35)
                        st = _read_state()
                        if st.get("id") == vid and st.get("title"):
                            return _tool_result({"ok": True, "playing": st.get("title"), "id": st.get("id"), "state": st})
                    try:
                        _mpv_direct(["loadfile", _url(vid), "replace"])
                        time.sleep(0.35)
                        st = _read_state()
                        return _tool_result({"ok": True, "playing": st.get("title"), "id": st.get("id"), "state": st, "fallback": "mpv direct"})
                    except Exception as direct_exc:
                        raise RuntimeError(f"daemon play failed, direct mpv failed: {direct_exc}") from direct_exc
                else:
                    resp = _send_cmd("load", arg=url)
                    if resp.get("ok"):
                        st0 = _read_state()
                        qlen = len(st0.get("queue") or [])
                        if qlen:
                            _send_cmd("play_index", arg=qlen - 1)
                        time.sleep(0.35)
                        st = _read_state()
                        return _tool_result({"ok": True, "playing": st.get("title"), "id": st.get("id"), "state": st})
                    return _tool_error(resp.get("error", "load failed"))
            except Exception:
                subprocess.Popen([sys.executable, "-m", "myx.player", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.8)
                st = _read_state()
                return _tool_result({"ok": True, "booted": True, "url": url, "state": st})

        elif name == "myx_search":
            q = str(args.get("query", "")).strip()
            if not q:
                return _tool_error("query required")
            try:
                resp = _send_cmd("search", arg=q)
            except RuntimeError:
                resp = {"ok": False, "error": "daemon not reachable"}
            if resp.get("ok") is False:
                try:
                    items = _spotify_search(q)
                    return _tool_result({"ok": True, "queued": len(items), "results": items[:5], "note": "daemon not running, results not enqueued"})
                except sources.SourcesError as e:
                    return _tool_result({"ok": False, "queued": 0, "results": [], "error": str(e)})
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
