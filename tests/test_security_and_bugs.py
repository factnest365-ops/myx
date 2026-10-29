from __future__ import annotations

import io
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from myx import cli, mcp, player, sources, tui
from myx.player import PlayerDaemon

# ---------------------------------------------------------------------------
# Security regression tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/thumb.jpg",
        "data:image/png;base64,AAAA",
        "http://127.0.0.1:8080/secret.jpg",
        "http://localhost/secret.jpg",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/admin.jpg",
        "http://[::1]/secret.jpg",
    ],
)
def test_cache_thumb_blocks_local_file_and_ssrf_urls(bad_url, tmp_path, monkeypatch):
    monkeypatch.setenv("MYX_CACHE_DIR", str(tmp_path))
    with pytest.raises(sources.SourcesError):
        sources.cache_thumb("vid123", bad_url)


def test_safe_redirect_handler_blocks_redirect_to_loopback():
    handler = sources._SafeRedirectHandler()
    req = sources.urllib.request.Request("https://cdn.example.com/thumb.jpg")
    with pytest.raises(sources.SourcesError):
        handler.redirect_request(
            req, None, 302, "Found", {}, "http://127.0.0.1:9000/internal"
        )


def test_cache_thumb_rejects_symlink_and_oversized_response(tmp_path, monkeypatch):
    monkeypatch.setenv("MYX_CACHE_DIR", str(tmp_path))
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (thumbs / "sym123.jpg").symlink_to(victim)

    with pytest.raises(sources.SourcesError, match="symlink"):
        sources.cache_thumb("sym123", "https://i.ytimg.com/vi/sym123/hqdefault.jpg")

    monkeypatch.setattr(sources, "MAX_THUMB_BYTES", 16)

    class OversizedResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, n=-1):
            return b"x" * 32

    class FakeOpener:
        def open(self, req, timeout=None):
            return OversizedResp()

    monkeypatch.setattr(sources.urllib.request, "build_opener", lambda *h: FakeOpener())
    with pytest.raises(sources.SourcesError, match="exceeds maximum"):
        sources.cache_thumb("big123", "https://i.ytimg.com/vi/big123/hqdefault.jpg")


def test_sources_strips_terminal_escape_injection(monkeypatch):
    malicious_payload = {
        "entries": [
            {
                "id": "abc123xyz00",
                "title": "Evil\x1b]52;c;cm0gLXJmIC8=\x07Title\r\nLine2",
                "uploader": "Hacker\x1b[2J - Topic",
                "duration": 100,
                "thumbnails": [{"url": "https://i.ytimg.com/vi/abc/hq.jpg"}],
            }
        ]
    }
    captured_args: list[list[str]] = []

    def fake_ytdlp(args):
        captured_args.append(args)
        return json.dumps(malicious_payload)

    monkeypatch.setattr(sources, "_run_ytdlp", fake_ytdlp)
    items = sources.search("-evil-flag-query")
    # Ensure '--' option terminator precedes user search query
    assert "--" in captured_args[0]
    assert "\x1b" not in items[0]["title"]
    assert "\n" not in items[0]["title"]
    assert items[0]["artist"] == "Hacker"


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "../../etc/passwd",
        "-o /tmp/pwn",
        "abc&list=evil",
        "abc\ndef",
    ],
)
def test_sources_validate_video_id_rejects_injection(bad_id):
    with pytest.raises(sources.SourcesError):
        sources.meta(bad_id)


def test_player_load_and_numeric_commands_reject_unsafe_inputs():
    daemon = PlayerDaemon(None)
    daemon._mpv = MagicMock(return_value=[])

    assert daemon.handle_command({"cmd": "load", "arg": "file:///etc/passwd"})["ok"] is False
    assert daemon.handle_command({"cmd": "load", "arg": "../../etc/passwd"})["ok"] is False
    assert daemon.handle_command({"cmd": "jump", "arg": float("nan")})["ok"] is False
    assert daemon.handle_command({"cmd": "seek", "arg": float("inf")})["ok"] is False
    assert daemon.handle_command({"cmd": "vol", "arg": float("nan")})["ok"] is False


def test_write_state_refuses_symlink(tmp_path, monkeypatch):
    victim = tmp_path / "victim.json"
    victim.write_text("original")
    sym_state = tmp_path / "state.json"
    sym_state.symlink_to(victim)
    monkeypatch.setattr(player, "STATE_PATH", sym_state)

    daemon = PlayerDaemon(None, no_lyrics=True)
    daemon._mpv = MagicMock(return_value=None)
    with pytest.raises(RuntimeError, match="symlink"):
        daemon.write_state()
    assert victim.read_text() == "original"


def test_command_server_permissions_and_nonblocking_concurrent_clients(monkeypatch):
    # Use short /tmp path because AF_UNIX sun_path has a 104-108 byte kernel limit
    import tempfile as _tf

    short_dir = Path(_tf.mkdtemp(prefix="myx-s-"))
    sock_path = short_dir / "cmd.sock"
    monkeypatch.setattr(player, "CMD_SOCK", sock_path)
    monkeypatch.setattr(
        PlayerDaemon, "_candidate_cmd_socks", lambda self: [sock_path]
    )
    daemon = PlayerDaemon(None)
    daemon._mpv = MagicMock(return_value=None)
    t = threading.Thread(target=daemon._command_server, daemon=True)
    t.start()

    stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(100):
        try:
            stalled.connect(str(sock_path))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.01)
    else:
        raise AssertionError("command socket did not start listening")

    try:
        mode = stat.S_IMODE(sock_path.stat().st_mode)
        assert mode & 0o077 == 0, f"socket permissions too broad: {oct(mode)}"

        # Client 1 stalls without sending newline
        stalled.sendall(b'{"cmd":')

        # Client 2 must still be served promptly without blocking on Client 1
        fast = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        fast.settimeout(1.5)
        fast.connect(str(sock_path))
        fast.sendall(b'{"cmd":"toggle"}\n')
        reply = json.loads(fast.recv(1024).decode())
        fast.close()
        stalled.close()
        assert reply == {"ok": True}
    finally:
        daemon.stop.set()
        t.join(timeout=1.0)
        sock_path.unlink(missing_ok=True)
        short_dir.rmdir()


def test_ffmpeg_tap_blocks_non_http_urls(monkeypatch):
    spawned = []
    monkeypatch.setattr(tui.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        tui.subprocess, "Popen", lambda args, **kw: spawned.append(args)
    )
    tap = tui.FfmpegTapSource(stream_url="file:///etc/passwd")
    assert tap.proc is None
    assert spawned == []


def test_mcp_mpv_socks_excludes_cmd_sock():
    mpv_socks = {p.name for p in mcp._candidate_mpv_socks()}
    assert "myx-cmd.sock" not in mpv_socks


def test_cli_write_lock_permissions_and_symlink_guard(tmp_path, monkeypatch):
    lock_file = tmp_path / "myx.lock"
    monkeypatch.setattr(cli, "LOCK_PATH", lock_file)
    cli.write_lock(12345)
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    lock_file.unlink()
    target = tmp_path / "other"
    target.write_text("safe")
    lock_file.symlink_to(target)
    with pytest.raises((PermissionError, OSError)):
        cli.write_lock(12345)


# ---------------------------------------------------------------------------
# Playback, TUI, CLI & MCP bugfix tests
# ---------------------------------------------------------------------------


def test_tui_spacebar_toggles_pause_instead_of_opening_search(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tui, "send_command", lambda cmd, **kw: (calls.append(cmd), {"ok": True})[1]
    )
    r = tui.Renderer(out=io.StringIO())
    r.handle_key(" ")
    assert r.overlay is None, "spacebar must toggle playback, not open search"
    assert calls == ["toggle"]


def test_tui_search_and_command_inputs_allow_typing_j_and_k(monkeypatch):
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: {"ok": True})
    r = tui.Renderer(out=io.StringIO())
    r.open_search()
    for ch in "the weeknd & joji":
        r.handle_key(ch)
    assert r.picker.query == "the weeknd & joji"

    r.open_command_palette()
    for ch in "skip":
        r.handle_key(ch)
    assert r.command_query == "skip"


def test_idle_daemon_starts_playback_from_queue_without_duplicate_append(monkeypatch):
    daemon = PlayerDaemon(None)
    mpv_playlist: list[dict] = []
    mpv_cmds: list[list] = []

    def fake_mpv(cmd):
        mpv_cmds.append(cmd)
        if cmd == ["get_property", "playlist"]:
            return list(mpv_playlist)
        if cmd == ["get_property", "playlist-pos"]:
            return 0 if mpv_playlist else None
        if cmd[0] == "loadfile" and cmd[2] == "replace":
            mpv_playlist[:] = [{"filename": cmd[1]}]
            return None
        if cmd[0] == "loadfile" and cmd[2] == "append":
            mpv_playlist.append({"filename": cmd[1]})
            return None
        return None

    daemon._mpv = fake_mpv  # type: ignore[method-assign]
    monkeypatch.setattr(sources, "stream_url", lambda vid: f"https://cdn.example/{vid}.m4a")

    # Enqueue two tracks while daemon is completely idle
    daemon.handle_command({"cmd": "load", "arg": "trackA11"})
    daemon.handle_command({"cmd": "load", "arg": "trackB22"})
    # Buffer worker must NOT append to empty playlist
    assert mpv_playlist == []

    # Tick maintenance loop: trackA11 starts playing via replace, trackB22 stays in queue
    daemon._maintain_playback()
    assert daemon.current_track is not None
    assert daemon.current_track["id"] == "trackA11"
    assert [x["id"] for x in daemon.queue] == ["trackB22"]
    assert ["loadfile", "https://cdn.example/trackA11.m4a", "replace"] in mpv_cmds


def test_prev_restarts_after_5s_and_walks_history_within_5s(monkeypatch):
    daemon = PlayerDaemon(None)
    monkeypatch.setattr(sources, "stream_url", lambda vid: f"https://cdn.example/{vid}.m4a")
    pos_box = [12.0]
    mpv_cmds: list[list] = []

    def fake_mpv(cmd):
        mpv_cmds.append(cmd)
        if cmd == ["get_property", "time-pos"]:
            return pos_box[0]
        if cmd == ["get_property", "playlist"]:
            return [{"filename": "https://cdn.example/cur.m4a"}]
        if cmd == ["get_property", "playlist-pos"]:
            return 0
        return None

    daemon._mpv = fake_mpv  # type: ignore[method-assign]
    daemon._play_now("song_one")
    daemon.queue.append({"id": "song_two", "title": "Two", "stream_url": None})

    # Skip advances to song_two and pushes song_one into history
    daemon.handle_command({"cmd": "skip"})
    assert daemon.current_track["id"] == "song_two"
    assert [h["id"] for h in daemon.history] == ["song_one"]

    # prev when position > 5s seeks to 0 instead of changing tracks
    pos_box[0] = 12.0
    daemon.handle_command({"cmd": "prev"})
    assert ["seek", 0, "absolute"] in mpv_cmds
    assert daemon.current_track["id"] == "song_two"

    # prev when position <= 5s restores song_one and puts song_two back at queue[0]
    pos_box[0] = 2.0
    daemon.handle_command({"cmd": "prev"})
    assert daemon.current_track["id"] == "song_one"
    assert daemon.queue[0]["id"] == "song_two"


def test_stream_cache_evicts_expired_urls(monkeypatch):
    daemon = PlayerDaemon(None)
    calls = []
    expired_ts = int(time.time()) - 100
    fresh_ts = int(time.time()) + 7200

    def fake_stream(vid):
        calls.append(vid)
        ts = expired_ts if len(calls) == 1 else fresh_ts
        return f"https://rr1.googlevideo.com/videoplayback?id={vid}&expire={ts}"

    monkeypatch.setattr(sources, "stream_url", fake_stream)
    u1 = daemon._stream_for("vid_exp")
    u2 = daemon._stream_for("vid_exp")  # first was expired -> must re-fetch
    u3 = daemon._stream_for("vid_exp")  # second is fresh -> cache hit
    assert u1 != u2
    assert u2 == u3
    assert len(calls) == 2


def test_loop_mute_seek_and_clear_commands():
    daemon = PlayerDaemon(None)
    mpv_cmds: list[list] = []
    daemon._mpv = lambda cmd: mpv_cmds.append(cmd) and None  # type: ignore[method-assign]
    daemon.queue = [{"id": "q1", "stream_url": None}]

    r1 = daemon.handle_command({"cmd": "loop"})
    assert r1 == {"ok": True, "loop": "one"}
    assert ["set_property", "loop-file", "inf"] in mpv_cmds

    r2 = daemon.handle_command({"cmd": "loop"})
    assert r2 == {"ok": True, "loop": "all"}
    assert ["set_property", "loop-file", "no"] in mpv_cmds

    assert daemon.handle_command({"cmd": "seek", "arg": -5}) == {"ok": True}
    assert ["seek", -5.0, "relative"] in mpv_cmds

    assert daemon.handle_command({"cmd": "mute"}) == {"ok": True}
    assert ["cycle", "mute"] in mpv_cmds

    assert daemon.handle_command({"cmd": "clear"}) == {"ok": True}
    assert daemon.queue == []


def test_tui_put_preserves_sidebar_and_overlay_colors():
    r = tui.Renderer(out=io.StringIO())
    r.open_help()
    state = {
        "playing": True,
        "paused": False,
        "title": "Vibrant Track",
        "artist": "Artist",
        "duration": 200,
        "position": 50,
        "volume": 75,
        "queue": [
            {"id": f"id{i}", "title": f"Song {i}", "artist": "Art", "duration": 120}
            for i in range(1, 30)
        ],
    }
    r.selected = 25
    frame = r.render_once(state=state, size=(100, 28))
    # Overlay borders keep accent color on both left and right edges
    accent_esc = tui.fg("accent")
    border_rows = [line for line in frame.split("\n") if line.count("│") >= 2]
    assert any(line.count(accent_esc) >= 2 for line in border_rows)

    # Closing overlay and rendering scrolled queue shows selected track #25
    r.close_overlay()
    frame_scrolled = r.render_once(state=state, size=(100, 28))
    assert "Song 25" in frame_scrolled


def test_read_escape_sequence_consumes_full_csi():
    r_fd, w_fd = os.pipe()
    os.write(w_fd, b"[1;5C")
    os.close(w_fd)
    with os.fdopen(r_fd, "r") as stream:
        seq = tui._read_escape_sequence(stream, wait_s=0.05)
    assert seq == "\x1b[1;5C"


def test_art_renderer_supports_ghostty_wezterm_and_pillow_halfblock(tmp_path):
    assert tui.ArtRenderer.detect_protocol({"TERM": "xterm-ghostty"}) == "kitty"
    assert tui.ArtRenderer.detect_protocol({"TERM_PROGRAM": "WezTerm"}) == "iterm2"

    if tui.Image is not None:
        img_path = tmp_path / "cover.png"
        im = tui.Image.new("RGBA", (16, 16), (255, 107, 53, 255))
        im.save(img_path)
        art = tui.ArtRenderer()
        lines = art._half_block(str(img_path), 8, 4)
        assert len(lines) == 4
        assert "▀" in lines[0]
        assert "255;107;53" in lines[0]


def test_new_mcp_and_cli_controls(monkeypatch):
    sent = []
    monkeypatch.setattr(
        mcp, "_send_cmd", lambda cmd, **kw: sent.append((cmd, kw)) or {"ok": True}
    )
    assert mcp.handle_tool("myx_seek", {"delta": 10})["isError"] is False
    assert mcp.handle_tool("myx_loop", {"mode": "one"})["isError"] is False
    assert mcp.handle_tool("myx_mute", {"muted": True})["isError"] is False
    assert mcp.handle_tool("myx_clear", {})["isError"] is False
    assert [c[0] for c in sent] == ["seek", "loop", "mute", "clear"]

    cli_sent = []
    monkeypatch.setattr(cli, "daemon_running", lambda: True)
    monkeypatch.setattr(
        cli.tui, "send_command", lambda cmd, arg=None: cli_sent.append((cmd, arg))
    )
    assert cli.main(["--seek", "15"]) == 0
    assert cli.main(["--loop", "all"]) == 0
    assert cli.main(["--mute"]) == 0
    assert cli.main(["--clear"]) == 0
    assert cli_sent == [("seek", 15.0), ("loop", "all"), ("mute", None), ("clear", None)]
