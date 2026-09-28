"""Hermetic spectrum tests: non-blocking taps, wiring, gradient rendering.

No cava/ffmpeg binaries are spawned; pipes + monkeypatched make_spectrum only.
"""

import io
import os
import threading
import types

from myx import tui


def _pipe_source(cls, **kwargs):
    r_fd, w_fd = os.pipe()
    fake = types.SimpleNamespace(stdout=os.fdopen(r_fd, "rb"), terminate=lambda: None)
    src = cls(proc=fake, **kwargs)
    return src, w_fd


def test_cava_values_do_not_block_without_data(monkeypatch):
    monkeypatch.setattr(tui.shutil, "which", lambda name: None)
    src, w_fd = _pipe_source(tui.CavaSource)
    out: list = []

    t = threading.Thread(target=lambda: out.append(src.values()), daemon=True)
    t.start()
    t.join(0.5)
    assert not t.is_alive(), "CavaSource.values() blocked waiting for cava output"
    assert out and len(out[0]) == 24
    os.close(w_fd)


def test_cava_values_parses_latest_ascii_line(monkeypatch):
    monkeypatch.setattr(tui.shutil, "which", lambda name: None)
    src, w_fd = _pipe_source(tui.CavaSource)
    os.write(w_fd, b"10 20 30\n")
    vals = src.values()
    assert vals[:3] == [0.1, 0.2, 0.3]
    assert len(vals) == 24
    os.close(w_fd)


def test_cava_initializes_peak_hold(monkeypatch):
    monkeypatch.setattr(tui.shutil, "which", lambda name: None)
    src, w_fd = _pipe_source(tui.CavaSource)
    assert src.peaks == [0.0] * 24
    os.close(w_fd)


def test_ffmpeg_tap_values_do_not_block_without_data(monkeypatch):
    monkeypatch.setattr(tui.shutil, "which", lambda name: None)
    src, w_fd = _pipe_source(tui.FfmpegTapSource, stream_url=None)
    out: list = []

    t = threading.Thread(target=lambda: out.append(src.values()), daemon=True)
    t.start()
    t.join(0.5)
    assert not t.is_alive(), "FfmpegTapSource.values() blocked waiting for ffmpeg output"
    assert out and len(out[0]) == 24
    os.close(w_fd)


def _playing_state(url: str = "https://example.invalid/stream") -> dict:
    return {
        "version": 1,
        "playing": True,
        "paused": False,
        "title": "Song",
        "artist": "A",
        "id": "x",
        "url": url,
        "thumb": None,
        "duration": 100,
        "position": 10,
        "queue": [],
    }


class _FakeSpectrum(tui.SpectrumSource):
    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag
        self.closed = False

    def values_with_peaks(self):
        return [0.9] * 24, [0.9] * 24

    def close(self) -> None:
        self.closed = True


def test_spectrum_bound_once_per_stream_url(monkeypatch):
    calls: list = []
    made: list = []

    def fake_make(url):
        calls.append(url)
        s = _FakeSpectrum(str(url))
        made.append(s)
        return s

    monkeypatch.setattr(tui, "make_spectrum", fake_make)
    r = tui.Renderer(out=io.StringIO())
    r.spectrum_auto = True
    state = _playing_state("https://a/1")
    r.render_once(state=state, size=(100, 32))
    assert calls == ["https://a/1"]
    r.render_once(state=state, size=(100, 32))
    assert calls == ["https://a/1"], "rebind on every frame would respawn ffmpeg"
    r.render_once(state=_playing_state("https://a/2"), size=(100, 32))
    assert calls == ["https://a/1", "https://a/2"]
    assert made[0].closed, "old tap must be closed on rebind"
    assert r.spectrum is made[1]


def test_default_renderer_never_spawns_spectrum(monkeypatch):
    def boom(url):
        raise AssertionError("spectrum spawned without spectrum_auto")

    monkeypatch.setattr(tui, "make_spectrum", boom)
    r = tui.Renderer(out=io.StringIO())
    r.render_once(state=_playing_state(), size=(100, 32))
    assert isinstance(r.spectrum, tui.SpectrumSource)


def test_run_enables_spectrum_auto(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "load_theme", lambda: None)

    def fake_make(url):
        calls.append(url)
        return _FakeSpectrum("run")

    monkeypatch.setattr(tui, "make_spectrum", fake_make)
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / "state.json"
        state_path.write_text(json.dumps(_playing_state()))
        # non-tty out => run() renders one frame and exits
        tui.run(state_path=state_path, out=io.StringIO())
    assert calls, "run() must enable the spectrum for interactive sessions"


def test_spectrum_row_bars_keep_gradient_colors(monkeypatch):
    """All bars share a row; per-bar puts used to strip each other's colors."""
    r = tui.Renderer(out=io.StringIO())
    r.spectrum = _FakeSpectrum("grad")
    frame = r.render_once(state=_playing_state(), size=(100, 32))
    bar_lines = [l for l in frame.split("\n") if "█" in l]
    assert bar_lines, "spectrum bars expected at 100x32"
    import re

    per_row = [
        len(set(re.findall(r"\x1b\[38;2;\d+;\d+;\d+m", line)))
        for line in bar_lines
    ]
    assert max(per_row) >= 3, f"gradient colors stripped within row, got {per_row}"
