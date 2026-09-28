"""Hermetic tests for daemon-side stream resolution."""
from myx import sources
from myx.player import PlayerDaemon


def test_stream_for_caches_and_falls_back(monkeypatch):
    d = PlayerDaemon("https://www.youtube.com/watch?v=seed123")
    calls = []

    def fake_resolve(vid):
        calls.append(vid)
        if vid == "bad":
            raise sources.SourcesError("boom")
        return f"https://cdn.example/{vid}.m8a"

    monkeypatch.setattr(sources, "stream_url", fake_resolve)
    assert d._stream_for("abc") == "https://cdn.example/abc.m8a"
    assert d._stream_for("abc") == "https://cdn.example/abc.m8a"  # cache hit
    assert calls == ["abc"]
    assert d._stream_for("bad") == "https://www.youtube.com/watch?v=bad"  # graceful fallback

def test_ytdlp_whitespace_only_failure_raises_sources_error(monkeypatch):
    import pytest

    class FakeProc:
        returncode = 1
        stderr = " \n\t\n"
        stdout = ""

    monkeypatch.setattr(sources.subprocess, "run", lambda *a, **k: FakeProc())
    with pytest.raises(sources.SourcesError, match="yt-dlp failed"):
        sources._run_ytdlp(["--version"])
