"""MYX MCP server — stdio JSON-RPC 2.0, no external deps.

Exposes MYX player controls as MCP tools so any agent can drive music.
Reuses the existing daemon IPC (CMD_SOCK + STATE_PATH); does not own playback.
"""

from __future__ import annotations

import json
import math
import os
import re
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
    from .player import (
        CMD_SOCK as PLAYER_CMD_SOCK,
    )
    from .player import (
        MPV_SOCK as PLAYER_MPV_SOCK,
    )
    from .player import (
        STATE_PATH as PLAYER_STATE_PATH,
    )
except Exception:
    PLAYER_STATE_PATH = Path(gettempdir()) / "myx-state.json"
    PLAYER_CMD_SOCK = Path(gettempdir()) / "myx-cmd.sock"
    PLAYER_MPV_SOCK = Path(gettempdir()) / "myx-mpv.sock"

STATE_PATH = Path(os.environ.get("MYX_STATE", PLAYER_STATE_PATH))
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", PLAYER_CMD_SOCK))
MAX_RPC_LINE_BYTES = 1024 * 1024  # 1 MiB cap per JSON-RPC line


def _is_trusted_local_path(path: Path) -> bool:
    """Reject symlinks and files owned by a different local UID."""
    try:
        if path.is_symlink():
            return False
        if path.exists() and hasattr(os, "getuid"):
            if path.stat().st_uid != os.getuid():
                return False
        return True
    except OSError:
        return False


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
    for p in [STATE_PATH, PLAYER_STATE_PATH] + [
        d / "myx-state.json" for d in _get_fallback_tmpdirs()
    ]:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _candidate_cmd_socks() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in [CMD_SOCK, PLAYER_CMD_SOCK] + [
        d / "myx-cmd.sock" for d in _get_fallback_tmpdirs()
    ]:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _candidate_mpv_socks() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    mpv_env = os.environ.get("MYX_MPV_SOCK")
    candidates = ([Path(mpv_env)] if mpv_env else []) + [PLAYER_MPV_SOCK] + [
        d / "myx-mpv.sock" for d in _get_fallback_tmpdirs()
    ]
    for p in candidates:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


SERVER_NAME = "myx"
SERVER_VERSION = "0.2.0"


def _send_cmd(cmd: str, **payload: Any) -> dict[str, Any]:
    msg = {"cmd": cmd, **payload}
    sock_timeout = 22.0 if cmd in ("search", "search_query") else 2.5
    last_err: Exception | None = None
    for sock_path in _candidate_cmd_socks():
        if not _is_trusted_local_path(sock_path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(sock_path))
                s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
                data = b""
                s.settimeout(sock_timeout)
                while len(data) < 1024 * 1024:
                    chunk = s.recv(8192)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
            line = (
                data.decode(errors="ignore").strip().splitlines()[0] if data else "{}"
            )
            return json.loads(line or "{}")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        f"myx daemon not reachable (tried {', '.join(str(p) for p in _candidate_cmd_socks())}): {last_err}"
    )


def _mpv_direct(command: list[Any]) -> Any:
    msg = {"command": command}
    last_err: Exception | None = None
    for sock_path in _candidate_mpv_socks():
        if not _is_trusted_local_path(sock_path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(sock_path))
                s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
                data = b""
                s.settimeout(2.0)
                while len(data) < 1024 * 1024:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
            line = (
                data.decode(errors="ignore").strip().splitlines()[0] if data else "{}"
            )
            resp = json.loads(line or "{}")
            err = resp.get("error")
            if err not in (None, "success"):
                raise RuntimeError(err)
            return resp.get("data")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(
        f"mpv not reachable (tried {', '.join(str(p) for p in _candidate_mpv_socks())}): {last_err}"
    )


def _url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _video_id(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(ord(c) < 0x20 for c in raw):
        return None
    for pat in (
        r"[?&]v=([^&#/?\s]+)",
        r"youtu\.be/([^?&#/\s]+)",
        r"/shorts/([^?&#/\s]+)",
        r"/embed/([^?&#/\s]+)",
        r"/live/([^?&#/\s]+)",
        r"/watch/([^?&#/\s]+)",
    ):
        m = re.search(pat, raw)
        if m:
            candidate = m.group(1)
            if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", candidate):
                return candidate
            return None
    if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", raw):
        return raw
    return None


_meta_cache: dict[str, dict[str, Any]] = {}


def _meta_with_timeout(video_id: str, timeout: float = 2.0) -> dict[str, Any] | None:
    res: dict[str, Any] | None = None

    def _run() -> None:
        nonlocal res
        try:
            res = sources.meta(video_id)
        except sources.SourcesError:
            res = {}

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    return res


def _hydrate_queue(state: dict[str, Any]) -> dict[str, Any]:
    return state


def _read_state() -> dict[str, Any]:
    empty = {
        "playing": False,
        "paused": True,
        "title": None,
        "artist": None,
        "queue": [],
    }
    for p in _candidate_states():
        try:
            lock = p.parent / "myx.lock"
            cmd_sock = p.parent / "myx-cmd.sock"
            if not _is_trusted_local_path(p) or not _is_trusted_local_path(lock):
                continue
            pid = int(lock.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            if p.exists() and _daemon_reachable(cmd_sock):
                state = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(state, dict):
                    return _hydrate_queue(state)
                return empty
        except (OSError, ValueError, TypeError):
            continue
    return empty


def _daemon_reachable(cmd_sock: Path) -> bool:
    if not cmd_sock.exists() or not _is_trusted_local_path(cmd_sock):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(str(cmd_sock))
            return True
    except OSError:
        return False


def _spotify_search(q: str) -> list[dict[str, Any]]:
    """Reuse sources.search which is ytsearch20."""
    return sources.search(q)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "myx_play",
        "description": "Play a YouTube URL, video id, or search query (e.g. 'blinding lights the weeknd'). Starts playback and radio.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "YouTube URL, video id, or search query"},
                "query": {"type": "string", "description": "Search query or YouTube URL/video id (alias for url)"},
            },
        },
    },
    {
        "name": "myx_queue_add",
        "description": "Add a track to the queue by search query, YouTube URL, or video ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or YouTube URL/video id"},
                "url": {"type": "string", "description": "YouTube URL or video id (alias for query)"},
            },
        },
    },
    {
        "name": "myx_search",
        "description": "Search YouTube (ytsearch20) and enqueue all results into the MYX queue. Returns queued count. Agents: use this to add music without needing a URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'weeknd blinding lights'",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "myx_state",
        "description": "Get current player state: playing/paused, title/artist, position/duration, volume, muted, loop, queue, lyrics (lrc/plain). Pollable at 4Hz.",
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
        "description": "Go to previous track (or restart current if >5s in).",
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
        "name": "myx_seek",
        "description": "Seek within current track. Provide 'delta' (±seconds relative) or 'position' (seconds from 0).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "delta": {"type": "number", "description": "Relative seek in seconds, e.g. 10 or -5"},
                "position": {"type": "number", "description": "Absolute position in seconds, e.g. 60"},
            },
        },
    },
    {
        "name": "myx_volume",
        "description": "Set or adjust volume. If 'volume' is 0..100, sets absolute. If 'delta' is provided (±5 typical), adjusts relative.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "volume": {"type": "integer", "description": "Absolute volume 0..100"},
                "delta": {
                    "type": "integer",
                    "description": "Relative delta, e.g. 5 or -5",
                },
            },
        },
    },
    {
        "name": "myx_mute",
        "description": "Toggle mute or set explicit muted boolean.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "muted": {"type": "boolean", "description": "Optional explicit mute state"}
            },
        },
    },
    {
        "name": "myx_loop",
        "description": "Set or cycle repeat mode ('off', 'one', 'all').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["off", "one", "all"],
                    "description": "Repeat mode; omit to cycle",
                }
            },
        },
    },
    {
        "name": "myx_shuffle",
        "description": "Shuffle the queue.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "myx_clear",
        "description": "Clear upcoming tracks from the queue.",
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
            "properties": {
                "index": {"type": "integer", "description": "Queue index to remove"}
            },
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
    {
        "name": "myx_eq",
        "description": "Set or cycle audiophile EQ/DSP preset (flat, bass_boost, vocal, treble_sparkle, rock, electronic, late_night, spatial, slowed_reverb, nightcore) or custom 5-band dB gains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "description": "Preset name or 'cycle'"},
                "bands": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Optional 5-band dB gains [-12..+12] at [60, 230, 910, 3600, 14000] Hz",
                },
            },
        },
    },
    {
        "name": "myx_dsp",
        "description": "Configure loudness normalization, smart crossfade (0..12s), silence trimming, or SponsorBlock non-music auto-skip.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "normalize": {"type": "boolean", "description": "Enable/disable dynamic loudness normalization"},
                "crossfade_s": {"type": "number", "description": "Crossfade duration in seconds (0..12)"},
                "trim_silence": {"type": "boolean", "description": "Enable/disable silence trimming"},
                "sponsorblock": {"type": "boolean", "description": "Enable/disable SponsorBlock non-music auto-skip"},
            },
        },
    },
    {
        "name": "myx_like",
        "description": "Like/unlike songs or list/play Liked Songs from the persistent SQLite library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["toggle", "list", "play"],
                    "description": "Default 'toggle' likes/unlikes current track; 'list' returns liked songs; 'play' loads liked songs into queue",
                },
                "liked": {"type": "boolean", "description": "Explicit like state when action='toggle'"},
            },
        },
    },
    {
        "name": "myx_playlist",
        "description": "Manage persistent playlists or import YouTube/M3U playlists (actions: list, save, load, add, delete, import, export).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "save", "load", "add", "delete", "import", "export"],
                },
                "name": {"type": "string", "description": "Playlist name"},
                "url": {"type": "string", "description": "YouTube/YT Music playlist URL or ID (for action='import')"},
                "m3u": {"type": "string", "description": "M3U playlist text (for action='import')"},
                "replace": {"type": "boolean", "description": "Replace current playback when loading/importing"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "myx_chapter",
        "description": "Navigate YouTube chapters on the current track ('next', 'prev', or 'list').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["next", "prev", "list"],
                    "description": "Navigate to next/prev chapter or list chapters",
                }
            },
        },
    },
    {
        "name": "myx_history",
        "description": "Get recent listening history and most-played tracks from the persistent SQLite library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries (1..100, default 25)"}
            },
        },
    },
    {
        "name": "myx_cache_track",
        "description": "Download and cache the current track (or video ID) as local Opus/M4A for instant offline playback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Optional YouTube video ID or URL; defaults to current track"}
            },
        },
    },
]


def _tool_result(content: Any) -> dict[str, Any]:
    text = (
        json.dumps(content, indent=2, ensure_ascii=False)
        if not isinstance(content, str)
        else content
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "myx_play":
            target = str(args.get("url") or args.get("query") or "").strip()
            if not target:
                return _tool_error("url or query required")
            vid = _video_id(target)
            search_title = None
            if not vid:
                try:
                    items = sources.search(target)
                    if items and items[0].get("id"):
                        vid = str(items[0]["id"])
                        search_title = items[0].get("title")
                    else:
                        return _tool_error(f"no search results found for: {target}")
                except sources.SourcesError as e:
                    return _tool_error(f"search failed: {e}")
                except Exception as e:
                    return _tool_error(f"search failed: {e}")
            try:
                resp = _send_cmd("play_id", arg=vid)
                if resp.get("ok"):
                    time.sleep(0.05)
                    st = _read_state()
                    return _tool_result(
                        {
                            "ok": True,
                            "playing": st.get("title") or search_title or vid,
                            "id": vid,
                            "state": st,
                        }
                    )
                try:
                    _mpv_direct(["loadfile", _url(vid), "replace"])
                    time.sleep(0.05)
                    st = _read_state()
                    return _tool_result(
                        {
                            "ok": True,
                            "playing": st.get("title") or search_title or vid,
                            "id": vid,
                            "state": st,
                            "fallback": "mpv direct",
                        }
                    )
                except Exception as direct_exc:
                    return _tool_error(resp.get("error", f"play failed: {direct_exc}"))
            except Exception:
                subprocess.Popen(
                    [sys.executable, "-m", "myx.player", vid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.2)
                st = _read_state()
                return _tool_result(
                    {
                        "ok": True,
                        "booted": True,
                        "id": vid,
                        "playing": search_title or vid,
                        "state": st,
                    }
                )

        elif name == "myx_queue_add":
            target = str(args.get("query") or args.get("url") or "").strip()
            if not target:
                return _tool_error("query or url required")
            vid = _video_id(target)
            item_title = None
            if not vid:
                try:
                    items = sources.search(target)
                    if items and items[0].get("id"):
                        vid = str(items[0]["id"])
                        item_title = items[0].get("title")
                    else:
                        return _tool_error(f"no search results found for: {target}")
                except sources.SourcesError as e:
                    return _tool_error(f"search failed: {e}")
                except Exception as e:
                    return _tool_error(f"search failed: {e}")
            try:
                resp = _send_cmd("load", arg=vid)
                if resp.get("ok"):
                    return _tool_result(
                        {"ok": True, "queued": item_title or vid, "id": vid}
                    )
                return _tool_error(resp.get("error", "load failed"))
            except Exception:
                subprocess.Popen(
                    [sys.executable, "-m", "myx.player", vid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.2)
                st = _read_state()
                return _tool_result(
                    {
                        "ok": True,
                        "booted": True,
                        "queued": item_title or vid,
                        "id": vid,
                        "state": st,
                    }
                )

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
                    return _tool_result(
                        {
                            "ok": True,
                            "queued": len(items),
                            "results": items[:5],
                            "note": "daemon not running, results not enqueued",
                        }
                    )
                except sources.SourcesError as e:
                    return _tool_result(
                        {"ok": False, "queued": 0, "results": [], "error": str(e)}
                    )
                except Exception as e:
                    return _tool_error(str(e))
            return _tool_result(resp)

        elif name == "myx_state":
            return _tool_result(_read_state())

        elif name == "myx_queue":
            st = _read_state()
            return _tool_result(
                {
                    "queue": st.get("queue", []),
                    "current": {
                        "title": st.get("title"),
                        "artist": st.get("artist"),
                        "id": st.get("id"),
                    },
                }
            )

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

        elif name == "myx_seek":
            if "position" in args and args["position"] is not None:
                pos = float(args["position"])
                if not math.isfinite(pos):
                    return _tool_error("position must be finite")
                return _tool_result(_send_cmd("jump", arg=pos))
            if "delta" in args and args["delta"] is not None:
                delta = float(args["delta"])
                if not math.isfinite(delta):
                    return _tool_error("delta must be finite")
                return _tool_result(_send_cmd("seek", arg=delta))
            return _tool_error("provide delta (±seconds) or position (seconds)")

        elif name == "myx_volume":
            if "delta" in args and args["delta"] is not None:
                return _tool_result(_send_cmd("vol", value=int(args["delta"])))
            if "volume" in args and args["volume"] is not None:
                return _tool_result(_send_cmd("vol", arg=int(args["volume"])))
            return _tool_error("provide volume (0..100) or delta (±N)")

        elif name == "myx_mute":
            if "muted" in args and isinstance(args["muted"], bool):
                return _tool_result(_send_cmd("mute", arg=args["muted"]))
            return _tool_result(_send_cmd("mute"))

        elif name == "myx_loop":
            mode = args.get("mode")
            if mode is not None:
                if mode not in ("off", "one", "all"):
                    return _tool_error("mode must be 'off', 'one', or 'all'")
                return _tool_result(_send_cmd("loop", arg=mode))
            return _tool_result(_send_cmd("loop"))

        elif name == "myx_shuffle":
            return _tool_result(_send_cmd("shuffle"))

        elif name == "myx_clear":
            return _tool_result(_send_cmd("clear"))

        elif name == "myx_lyrics":
            st = _read_state()
            return _tool_result(
                {
                    "lrc": st.get("lrc"),
                    "plain": st.get("plain") or st.get("lyrics"),
                    "title": st.get("title"),
                    "artist": st.get("artist"),
                }
            )

        elif name == "myx_remove":
            idx = int(args.get("index", 0))
            return _tool_result(_send_cmd("del", arg=idx))

        elif name == "myx_play_index":
            idx = int(args.get("index", 0))
            return _tool_result(_send_cmd("play_index", arg=idx))

        elif name == "myx_eq":
            payload: dict[str, Any] = {}
            if "bands" in args and isinstance(args["bands"], list):
                payload["bands"] = args["bands"]
            preset = args.get("preset")
            return _tool_result(_send_cmd("eq", arg=preset, **payload))

        elif name == "myx_dsp":
            results: dict[str, Any] = {}
            if "normalize" in args:
                results["normalize"] = _send_cmd("normalize", arg=bool(args["normalize"]))
            if "crossfade_s" in args:
                results["crossfade"] = _send_cmd("crossfade", arg=float(args["crossfade_s"]))
            if "trim_silence" in args:
                results["trim_silence"] = _send_cmd(
                    "trim_silence", arg=bool(args["trim_silence"])
                )
            if "sponsorblock" in args:
                results["sponsorblock"] = _send_cmd(
                    "sponsorblock", arg=bool(args["sponsorblock"])
                )
            if not results:
                st = _read_state()
                return _tool_result(
                    {"dsp": st.get("dsp"), "sponsorblock": st.get("sponsorblock")}
                )
            return _tool_result({"ok": True, **results})

        elif name == "myx_like":
            action = str(args.get("action") or "toggle").lower()
            if action == "list":
                return _tool_result(_send_cmd("liked_list"))
            if action == "play":
                return _tool_result(_send_cmd("play_liked"))
            extra = {"liked": args["liked"]} if isinstance(args.get("liked"), bool) else {}
            return _tool_result(_send_cmd("like", **extra))

        elif name == "myx_playlist":
            action = str(args.get("action") or "list").lower()
            cmd_map = {
                "list": "playlist_list",
                "save": "playlist_save",
                "load": "playlist_load",
                "add": "playlist_add",
                "delete": "playlist_delete",
                "import": "playlist_import",
                "export": "playlist_export",
            }
            if action not in cmd_map:
                return _tool_error(f"invalid playlist action: {action}")
            kw: dict[str, Any] = {}
            for k in ("name", "url", "m3u", "replace"):
                if k in args and args[k] is not None:
                    kw[k] = args[k]
            return _tool_result(_send_cmd(cmd_map[action], **kw))

        elif name == "myx_chapter":
            action = str(args.get("action") or "list").lower()
            if action == "next":
                return _tool_result(_send_cmd("next_chapter"))
            if action == "prev":
                return _tool_result(_send_cmd("prev_chapter"))
            st = _read_state()
            return _tool_result(
                {
                    "chapter": st.get("chapter"),
                    "chapters": st.get("chapters") or [],
                }
            )

        elif name == "myx_history":
            limit = int(args.get("limit") or 25)
            return _tool_result(_send_cmd("history", limit=limit))

        elif name == "myx_cache_track":
            return _tool_result(_send_cmd("cache_track", arg=args.get("id")))

        else:
            return _tool_error(f"unknown tool: {name}")
    except Exception as e:  # noqa: BLE001 - convert all handler failures to JSON-RPC errors
        return _tool_error(str(e))


def _write(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _rpc_reply(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _dispatch(msg: Any) -> dict[str, Any] | None:
    """Handle one decoded JSON-RPC message.

    Returns the response object, or None when no response is due
    (notifications per JSON-RPC 2.0 MUST NOT be answered).
    """
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    mid = msg.get("id")
    has_id = "id" in msg
    method = msg.get("method")
    if not isinstance(method, str):
        return _rpc_error(mid, -32600, "Invalid Request")
    notification = not has_id
    try:
        if method == "initialize":
            resp = _rpc_reply(
                mid,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
            return None if notification else resp
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            resp = _rpc_reply(mid, {"tools": TOOLS})
            return None if notification else resp
        if method == "tools/call":
            params = msg.get("params")
            if not isinstance(params, dict):
                return (
                    None if notification else _rpc_error(mid, -32602, "Invalid params")
                )
            name = params.get("name")
            args = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                return (
                    None if notification else _rpc_error(mid, -32602, "Invalid params")
                )
            resp = _rpc_reply(mid, handle_tool(name, args))
            return None if notification else resp
        if method == "ping":
            resp = _rpc_reply(mid, {})
            return None if notification else resp
        if method == "shutdown":
            resp = _rpc_reply(mid, {})
            return None if notification else resp
        if notification:
            return None
        return _rpc_error(mid, -32601, f"Method not found: {method}")
    except Exception as e:
        if not has_id:
            return None
        return _rpc_error(mid, -32603, str(e))


def serve_stdio() -> int:
    """Run MCP JSON-RPC 2.0 over stdio.

    Handles initialize, tools/list, tools/call, ping, shutdown; batch
    requests (JSON-RPC 2.0 §6), parse errors (-32700), invalid requests
    (-32600), and notifications (never answered).
    """
    for raw in sys.stdin:
        if len(raw) > MAX_RPC_LINE_BYTES:
            _write(_rpc_error(None, -32600, "Request too large"))
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _write(_rpc_error(None, -32700, "Parse error"))
            continue
        items = msg if isinstance(msg, list) else [msg]
        responses: list[dict[str, Any]] = []
        shutdown = False
        for item in items:
            resp = _dispatch(item)
            if resp is not None:
                responses.append(resp)
            if isinstance(item, dict) and item.get("method") == "shutdown":
                shutdown = True
        if isinstance(msg, list):
            if not msg:
                _write(_rpc_error(None, -32600, "Invalid Request"))
            elif responses:
                _write(responses)
        elif responses:
            _write(responses[0])
        if shutdown:
            break
    return 0


def main(argv: list[str] | None = None) -> int:
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
