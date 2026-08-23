from __future__ import annotations

import json
import socket
import threading

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


def test_player_command_protocol_roundtrip(monkeypatch, tmp_path):
    import myx.player as player

    sock_path = tmp_path / "cmd.sock"
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
