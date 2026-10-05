from __future__ import annotations

import time
from typing import Any

import pytest

import myx.mcp as mcp
from myx import sources


def test_mcp_play_with_video_id(monkeypatch):
    """myx_play with direct video ID or URL delegates to play_id without search."""
    calls = []
    monkeypatch.setattr(mcp, "_send_cmd", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or {"ok": True})
    monkeypatch.setattr(mcp, "_read_state", lambda: {"playing": True, "id": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up"})

    t0 = time.monotonic()
    resp = mcp.handle_tool("myx_play", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    elapsed = time.monotonic() - t0

    assert resp["isError"] is False
    assert elapsed < 0.5
    assert len(calls) == 1
    assert calls[0] == ("play_id", {"arg": "dQw4w9WgXcQ"})


def test_mcp_play_with_search_query(monkeypatch):
    """myx_play with a plain query searches sources, picks top result, and plays."""
    search_queries = []

    def fake_search(query: str):
        search_queries.append(query)
        return [{"id": "blinding123", "title": "Blinding Lights", "artist": "The Weeknd"}]

    monkeypatch.setattr(sources, "search", fake_search)
    calls = []
    monkeypatch.setattr(mcp, "_send_cmd", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or {"ok": True})
    monkeypatch.setattr(mcp, "_read_state", lambda: {"playing": True, "id": "blinding123", "title": "Blinding Lights"})

    t0 = time.monotonic()
    resp = mcp.handle_tool("myx_play", {"query": "blinding lights the weeknd"})
    elapsed = time.monotonic() - t0

    assert resp["isError"] is False
    assert elapsed < 0.5
    assert search_queries == ["blinding lights the weeknd"]
    assert calls == [("play_id", {"arg": "blinding123"})]


def test_mcp_queue_add_with_url(monkeypatch):
    """myx_queue_add with URL directly calls load with video ID."""
    calls = []
    monkeypatch.setattr(mcp, "_send_cmd", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or {"ok": True})

    t0 = time.monotonic()
    resp = mcp.handle_tool("myx_queue_add", {"url": "https://youtu.be/dQw4w9WgXcQ"})
    elapsed = time.monotonic() - t0

    assert resp["isError"] is False
    assert elapsed < 0.5
    assert calls == [("load", {"arg": "dQw4w9WgXcQ"})]


def test_mcp_queue_add_with_query(monkeypatch):
    """myx_queue_add with query searches and enqueues the top result."""
    search_queries = []

    def fake_search(query: str):
        search_queries.append(query)
        return [{"id": "top_track_id", "title": "Top Track", "artist": "Top Artist"}]

    monkeypatch.setattr(sources, "search", fake_search)
    calls = []
    monkeypatch.setattr(mcp, "_send_cmd", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or {"ok": True})

    t0 = time.monotonic()
    resp = mcp.handle_tool("myx_queue_add", {"query": "some song name"})
    elapsed = time.monotonic() - t0

    assert resp["isError"] is False
    assert elapsed < 0.5
    assert search_queries == ["some song name"]
    assert calls == [("load", {"arg": "top_track_id"})]


def test_mcp_state_and_queue_latency(monkeypatch):
    """myx_state and myx_queue must read state directly without slow hydration."""
    fake_state = {
        "playing": True,
        "paused": False,
        "title": "Fast Song",
        "artist": "Fast Artist",
        "queue": [
            {"id": "q1", "title": "Queued 1"},
            {"id": "q2", "title": None},  # unhydrated should NOT trigger sources.meta
        ],
    }
    monkeypatch.setattr(mcp, "_read_state", lambda: fake_state)

    def boom(*args, **kwargs):
        raise AssertionError("sources.meta must not be called during state/queue inspection")

    monkeypatch.setattr(sources, "meta", boom)

    t0 = time.monotonic()
    st_resp = mcp.handle_tool("myx_state", {})
    t_st = time.monotonic() - t0

    t1 = time.monotonic()
    q_resp = mcp.handle_tool("myx_queue", {})
    t_q = time.monotonic() - t1

    assert st_resp["isError"] is False
    assert q_resp["isError"] is False
    assert t_st < 0.1
    assert t_q < 0.1
