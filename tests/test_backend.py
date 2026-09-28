from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path

from myx import sources
from myx.player import PlayerDaemon


def test_search_shapes_yt_dlp_json(monkeypatch):
    payload = {
        "entries": [
            {
                "id": "abc123",
                "title": "Song",
                "uploader": "Artist - Topic",
                "duration": 180,
                "thumbnails": [{"url": "small"}, {"url": "big"}],
            }
        ]
    }
    monkeypatch.setattr(sources, "_run_ytdlp", lambda args: json.dumps(payload))
    assert sources.search("song") == [
        {"id": "abc123", "title": "Song", "artist": "Artist", "duration": 180, "thumb": "big"}
    ]


def test_mix_chain_parses_json_lines(monkeypatch):
    monkeypatch.setattr(sources, "_run_ytdlp", lambda args: '{"id":"a"}\n{"id":"b"}\n')
    assert sources.mix_chain("seed") == ["a", "b"]


def test_lyrics_404_returns_nones(monkeypatch):
    import urllib.error

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError("url", 404, "missing", {}, None)

    monkeypatch.setattr(sources.urllib.request, "urlopen", boom)
    assert sources.lyrics("nope", "nobody") == {"lrc": None, "plain": None}


def test_player_command_protocol_roundtrip(monkeypatch):
    import myx.player as player

    sock_dir = Path(tempfile.gettempdir()) / f"myx-test-{os.getpid()}"
    sock_dir.mkdir(exist_ok=True)
    sock_path = sock_dir / "cmd.sock"
    monkeypatch.setattr(player, "CMD_SOCK", sock_path)
    daemon = PlayerDaemon("https://youtube.com/watch?v=seed")
    calls = []

    def fake_mpv(cmd):
        calls.append(cmd)
        return None

    daemon._mpv = fake_mpv  # type: ignore[method-assign]
    thread = threading.Thread(target=daemon._command_server, daemon=True)
    thread.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(100):
        try:
            client.connect(str(sock_path))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            import time
            time.sleep(0.01)
    else:
        raise AssertionError("command socket was not ready")
    with client:
        client.sendall(b'{"cmd":"vol","arg":0}\n')
        resp = client.recv(1024)
    daemon.stop.set()
    assert json.loads(resp.decode()) == {"ok": True}
    assert calls == [["set_property", "volume", 0]]


def test_refill_falls_back_to_repeats_when_seen(monkeypatch):
    daemon = PlayerDaemon("https://youtube.com/watch?v=seed")
    daemon.seen = {"a", "b"}
    monkeypatch.setattr(daemon, "_current_id", lambda: "seed")
    monkeypatch.setattr(sources, "mix_chain", lambda cur: ["a", "b"])
    appended = []
    monkeypatch.setattr(daemon, "_append_track", lambda vid, mark_seen=True: appended.append((vid, mark_seen)) or True)
    assert daemon._append_related() is True
    assert appended == [("a", False), ("b", False)]


# --- BUG-001: seed by URL must resolve metadata, not show raw CDN title ---

def test_play_now_caches_meta_for_state(monkeypatch):
    daemon = PlayerDaemon("https://www.youtube.com/watch?v=abc123xyz_-")
    meta_calls = []
    monkeypatch.setattr(daemon, "_stream_for", lambda vid: "https://cdn.example/stream.webm?sig=deadbeef")
    monkeypatch.setattr(sources, "meta", lambda vid: meta_calls.append(vid) or {
        "id": vid, "title": "Real Title", "artist": "Real Artist", "duration": 180, "thumb": None,
    })
    monkeypatch.setattr(daemon, "_mpv", lambda cmd: None)
    daemon._play_now("abc123xyz_-")
    assert meta_calls == ["abc123xyz_-"]
    assert daemon._current_meta["title"] == "Real Title"

    # write_state must prefer the cached metadata over mpv media-title
    monkeypatch.setattr(daemon, "_playlist", lambda: [{
        "filename": "https://cdn.example/stream.webm?sig=deadbeef", "title": "webm&rqh=1&gir=yes&clen=1",
    }])
    def fake_mpv(cmd):
        if cmd == ["get_property", "playlist-pos"]:
            return 0
        if cmd == ["get_property", "media-title"]:
            return "webm&rqh=1&gir=yes&clen=1&sig=deadbeef"
        if cmd == ["get_property", "duration"]:
            return 180.0
        if cmd == ["get_property", "time-pos"]:
            return 1.0
        if cmd == ["get_property", "volume"]:
            return 50.0
        if cmd == ["get_property", "pause"]:
            return False
        return None
    monkeypatch.setattr(daemon, "_mpv", fake_mpv)
    monkeypatch.setattr(sources, "lyrics", lambda *a, **k: {"lrc": None, "plain": None})
    monkeypatch.setattr(sources, "cache_thumb", lambda *a, **k: None)
    state = daemon.write_state()
    assert state["title"] == "Real Title"
    assert state["artist"] == "Real Artist"


def test_state_loop_starts_after_seed_meta(monkeypatch):
    """BUG-001 regression: first state write must not race seed metadata fetch."""
    import myx.player as player_mod

    order = []
    daemon = PlayerDaemon("https://www.youtube.com/watch?v=abc123xyz_-")
    monkeypatch.setattr(daemon, "_command_server", lambda: None)
    monkeypatch.setattr(daemon, "_refill_loop", lambda: None)
    monkeypatch.setattr(daemon, "_play_now", lambda vid: order.append("meta"))
    monkeypatch.setattr(daemon, "_state_loop", lambda: order.append("state"))
    monkeypatch.setattr(daemon, "_queue_hydration_loop", lambda: None)
    monkeypatch.setattr(daemon, "_healthcheck_loop", lambda: None)
    monkeypatch.setattr(daemon, "_acquire_singleton", lambda url: True)
    monkeypatch.setattr(daemon, "_cleanup_sockets", lambda: None)
    monkeypatch.setattr(daemon, "_spawn_mpv", lambda: None)
    monkeypatch.setattr(daemon, "_wait_for_socket", lambda path: None)
    monkeypatch.setattr(player_mod.subprocess, "run", lambda *a, **k: None)
    daemon.mpv = None
    daemon.stop.set()  # main loop exits immediately
    # run() calls shutdown() in finally; neuter it
    monkeypatch.setattr(daemon, "shutdown", lambda: None)
    daemon.run()
    assert order == ["meta", "state"], f"state loop must start after seed meta, got {order}"


# --- BUG-002: dead daemon must not leave a lying state file ---

def test_shutdown_removes_state_file(tmp_path, monkeypatch):
    import myx.player as player_mod
    state_path = tmp_path / "myx-state.json"
    state_path.write_text('{"playing": true, "title": "stale"}')
    monkeypatch.setattr(player_mod, "STATE_PATH", state_path)
    daemon = PlayerDaemon(None)
    monkeypatch.setattr(daemon, "_mpv", lambda cmd: (_ for _ in ()).throw(RuntimeError("no mpv")))
    daemon.shutdown()
    assert not state_path.exists(), "shutdown must unlink the state file"


def test_state_missing_after_daemon_death(tmp_path, monkeypatch):
    """myx --state on a dead daemon must report not-playing, never stale data."""
    import myx.player as player_mod
    state_path = tmp_path / "myx-state.json"
    monkeypatch.setattr(player_mod, "STATE_PATH", state_path)
    daemon = PlayerDaemon(None)
    daemon.write_state = lambda: state_path.write_text('{"playing": true}') or {"playing": True}
    monkeypatch.setattr(daemon, "_mpv", lambda cmd: (_ for _ in ()).throw(RuntimeError("no mpv")))
    daemon.shutdown()
    assert not state_path.exists()


# --- BUG-003: headless controls must parse and route ---

def test_headless_flags_parse():
    from myx import cli
    for flag, attr in [("--quit", "quit"), ("--skip", "skip"), ("--prev", "prev"), ("--toggle", "toggle")]:
        args = cli.parse_args([flag])
        assert getattr(args, attr) is True, f"{flag} should set {attr}"
    args = cli.parse_args(["--vol", "80"])
    assert args.vol == 80
    # flags coexist with a seed url
    args = cli.parse_args(["https://www.youtube.com/watch?v=abc123xyz_-", "--vol", "30"])
    assert args.url is not None and args.vol == 30


def test_headless_flag_sends_command(monkeypatch, capsys):
    from myx import cli
    sent = []
    monkeypatch.setattr(cli, "daemon_running", lambda: True)
    monkeypatch.setattr(cli.tui, "send_command", lambda cmd, arg=None: sent.append((cmd, arg)))
    rc = cli.main(["--skip"])
    assert rc == 0
    assert sent == [("skip", None)]

    sent.clear()
    rc = cli.main(["--vol", "77"])
    assert rc == 0
    assert sent == [("vol", 77)]


def test_headless_flag_without_daemon_errors(monkeypatch, capsys):
    from myx import cli
    monkeypatch.setattr(cli, "daemon_running", lambda: False)
    rc = cli.main(["--toggle"])
    assert rc == 1
    assert "daemon not running" in capsys.readouterr().err


def test_no_lyrics_flag_parses_and_skips_fetch(monkeypatch):
    from myx import cli
    import myx.player as player_mod

    assert cli.parse_args(["--no-lyrics"]).no_lyrics is True
    monkeypatch.delenv("MYX_NO_LYRICS", raising=False)
    daemon = player_mod.PlayerDaemon(None, no_lyrics=True)
    monkeypatch.setattr(player_mod.sources, "lyrics", lambda *args: (_ for _ in ()).throw(AssertionError("lyrics fetched")))
    assert daemon._resolve_lyrics("abc123", "Title", "Artist", 120) == {"lrc": None, "plain": None}
    monkeypatch.setenv("MYX_NO_LYRICS", "1")
    assert player_mod.PlayerDaemon(None).no_lyrics is True


def test_queue_thumbnail_uses_each_item_thumbnail(monkeypatch, tmp_path):
    import myx.player as player_mod

    monkeypatch.setattr(player_mod, "STATE_PATH", tmp_path / "state.json")
    daemon = player_mod.PlayerDaemon(None, no_lyrics=True)
    playlist = [
        {"filename": "seed", "thumbnail": "current-thumb"},
        {"filename": "next", "thumbnail": "next-thumb"},
    ]
    monkeypatch.setattr(daemon, "_playlist", lambda: playlist)
    monkeypatch.setattr(daemon, "_resolve_thumb", lambda vid, thumb: thumb)
    def fake_mpv(cmd):
        values = {
            ("get_property", "playlist-pos"): 0,
            ("get_property", "duration"): 120,
            ("get_property", "time-pos"): 1,
            ("get_property", "volume"): 50,
            ("get_property", "pause"): False,
        }
        return values.get(tuple(cmd))
    monkeypatch.setattr(daemon, "_mpv", fake_mpv)
    state = daemon.write_state()
    assert state["queue"][0]["thumb"] == "next-thumb"


def test_cli_state_rejects_stale_state(monkeypatch, tmp_path, capsys):
    from myx import cli

    state = tmp_path / "myx-state.json"
    state.write_text('{"playing": true}')
    monkeypatch.setattr(cli, "daemon_running", lambda: False)
    monkeypatch.setattr(cli, "gettempdir", lambda: str(tmp_path))
    assert cli.main(["--state"]) == 0
    assert json.loads(capsys.readouterr().out)["playing"] is False
