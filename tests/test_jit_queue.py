from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from myx import sources
from myx.player import PlayerDaemon


def test_queue_items_do_not_resolve_stream_upfront(monkeypatch):
    """Tracks 2..N in the queue must remain metadata-only with stream_url=None."""
    resolved = []

    def fake_stream_url(vid: str) -> str:
        resolved.append(vid)
        return f"https://cdn.example/{vid}.m8a"

    monkeypatch.setattr(sources, "stream_url", fake_stream_url)

    daemon = PlayerDaemon("https://www.youtube.com/watch?v=track0")
    daemon._mpv = MagicMock(return_value=None)

    # Play track 0
    daemon._play_now("track0")
    assert resolved == ["track0"]

    # Append 5 items to queue
    for i in range(1, 6):
        daemon._append_track(f"track{i}")

    # Queue items 1..5 are in queue
    assert len(daemon.queue) == 5
    # Only track 1 may be resolved by buffer worker, tracks 2..5 must have stream_url=None
    for item in daemon.queue[1:]:
        assert item["stream_url"] is None
        assert item["id"] not in resolved[1:]  # ensure stream_url was not called for tracks 2..5


def test_play_id_is_non_blocking_and_triggers_refill(monkeypatch):
    """play_id must resolve only the target track, set refill event, and return immediately."""
    resolved = []

    def fake_stream_url(vid: str) -> str:
        resolved.append(vid)
        return f"https://cdn.example/{vid}.m8a"

    monkeypatch.setattr(sources, "stream_url", fake_stream_url)

    daemon = PlayerDaemon("https://www.youtube.com/watch?v=old_seed")
    daemon._mpv = MagicMock(return_value=None)
    daemon._refill_event.clear()
    daemon.queue = [{"id": "stale", "stream_url": None}]

    # play_id should be fast and non-blocking
    t0 = time.monotonic()
    resp = daemon.handle_command({"cmd": "play_id", "arg": "new_seed"})
    duration = time.monotonic() - t0

    assert resp == {"ok": True}
    assert duration < 0.1
    assert daemon.current_track["id"] == "new_seed"
    assert daemon.seen == {"new_seed"}
    assert daemon._refill_event.is_set()
    assert len(daemon.queue) == 0  # old queue cleared


def test_load_enqueues_without_resolving_stream():
    """load must append metadata to queue without resolving stream_url."""
    daemon = PlayerDaemon("https://www.youtube.com/watch?v=seed")
    daemon._mpv = MagicMock(return_value=[{"filename": "stream0"}])

    resp = daemon.handle_command({"cmd": "load", "arg": "loaded_track_id"})
    assert resp == {"ok": True}
    assert len(daemon.queue) == 1
    assert daemon.queue[0]["id"] == "loaded_track_id"
    assert daemon.queue[0]["stream_url"] is None


def test_search_enqueues_results_without_bulk_stream_resolution(monkeypatch):
    """search must add items to queue metadata without running stream_url for each."""
    mock_results = [
        {"id": f"res{i}", "title": f"Song {i}", "artist": "Artist", "duration": 180, "thumb": None}
        for i in range(10)
    ]
    monkeypatch.setattr(sources, "search", lambda q: mock_results)
    resolved = []
    monkeypatch.setattr(sources, "stream_url", lambda vid: resolved.append(vid) or f"https://cdn/{vid}")

    daemon = PlayerDaemon("https://www.youtube.com/watch?v=seed")
    daemon._mpv = MagicMock(return_value=[{"filename": "stream0"}])

    resp = daemon.handle_command({"cmd": "search", "arg": "lofi hip hop"})
    assert resp == {"ok": True, "queued": 10}
    assert len(daemon.queue) == 10
    # Search items in queue have metadata
    assert daemon.queue[0]["title"] == "Song 0"
    # Bulk streams were NOT synchronously resolved
    assert len(resolved) <= 1


def test_del_and_play_index_operations():
    """Queue indexing commands operate accurately on self.queue."""
    daemon = PlayerDaemon("https://www.youtube.com/watch?v=seed")
    daemon._mpv = MagicMock(return_value=[{"filename": "s0"}])
    daemon.queue = [
        {"id": "q0", "title": "Track 0", "stream_url": "s0"},
        {"id": "q1", "title": "Track 1", "stream_url": None},
        {"id": "q2", "title": "Track 2", "stream_url": None},
    ]

    # Delete index 1
    del_resp = daemon.handle_command({"cmd": "del", "arg": 1})
    assert del_resp == {"ok": True}
    assert len(daemon.queue) == 2
    assert [x["id"] for x in daemon.queue] == ["q0", "q2"]

    # Delete out of range
    err_del = daemon.handle_command({"cmd": "del", "arg": 10})
    assert err_del == {"ok": False, "error": "index out of range"}


def test_write_state_is_non_blocking_with_uncached_lyrics_and_thumbs(monkeypatch, tmp_path):
    """write_state should never block when lyrics or thumbnails are uncached."""
    import myx.player as player
    monkeypatch.setattr(player, "STATE_PATH", tmp_path / "state.json")

    daemon = PlayerDaemon("https://www.youtube.com/watch?v=seed")
    daemon._mpv = MagicMock(return_value=None)
    daemon.current_track = {"id": "seed", "title": "Playing Title", "artist": "Artist", "duration": 200, "thumb": None}

    # If sources.lyrics or sources.cache_thumb take time, write_state must not wait
    def slow_lyrics(*args):
        time.sleep(1.0)
        return {"lrc": "slow", "plain": "slow"}

    monkeypatch.setattr(sources, "lyrics", slow_lyrics)

    t0 = time.monotonic()
    st = daemon.write_state()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.1  # Fast sub-100ms
    assert st["title"] == "Playing Title"
    # Lyrics are initially None while background resolution runs
    assert st["lrc"] is None
