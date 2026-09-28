import io
import json
import os
import pty
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from myx import tui


def mock_state(tmp_path: Path) -> Path:
    state = {
        "version": 1,
        "playing": True,
        "paused": False,
        "title": "Call Out My Name",
        "artist": "The Weeknd",
        "id": "abc123",
        "url": "https://example.invalid/audio",
        "thumb": None,
        "duration": 228,
        "position": 11,
        "volume": 100,
        "queue": [
            {"id": "2", "title": "Holiday", "artist": "Green Day", "duration": 232, "thumb": None},
            {"id": "3", "title": "Never Fade Away", "artist": "SAMURAI", "duration": 205, "thumb": None},
        ],
        "lrc": "[00:10.00]Call out my name\n[00:20.00]When I kiss you",
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    return path


def test_render_one_frame_to_buffer_from_mock_state(tmp_path):
    state_path = mock_state(tmp_path)
    out = io.StringIO()
    renderer = tui.Renderer(state_path=state_path, out=out)

    frame = renderer.render_once(size=(120, 32))

    assert "M Y X" in frame
    assert "Call Out My Name" in frame
    assert tui.FOOTER in frame


def test_lrc_highlights_current_line():
    lyrics = tui.LyricsState()
    lyrics.lrc = tui.parse_lrc("[00:01.00]one\n[00:02.50]two")

    visible = lyrics.visible(3, 5)

    assert ("two", True) in visible


def test_pty_render_three_frames_contains_footer(tmp_path):
    state_path = mock_state(tmp_path)
    code = f"""
import json, sys
from pathlib import Path
from myx import tui
r = tui.Renderer(state_path=Path({str(state_path)!r}), out=sys.stdout)
for _ in range(3):
    r.render_once(size=(120, 32))
"""
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=slave,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parents[1],
            text=False,
        )
        os.close(slave)
        chunks = []
        while True:
            try:
                data = os.read(master, 8192)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        _, err = proc.communicate(timeout=5)
        output = b"".join(chunks).decode(errors="ignore")
    finally:
        os.close(master)
    assert proc.returncode == 0, err.decode(errors="ignore")
    assert tui.FOOTER in output


_ANSI_INDEP = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b_[^\x1b]*\x1b\\"
)


def _independent_disp_width(line: str) -> int:
    """Terminal width of a rendered line; independent of myx.tui helpers."""
    plain = _ANSI_INDEP.sub("", line)
    total = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def test_cell_len_counts_wide_chars():
    assert tui.cell_len("abc") == 3
    assert tui.cell_len("日本語") == 6
    assert tui.cell_len("夜に駆ける") == 10
    assert tui.cell_len(tui.fg("accent") + "ab") == 2
    assert tui.cell_len("🔀") == 2


def test_crop_and_pad_are_cell_aware():
    assert tui.crop("日本語", 4) == "日本"
    assert tui.crop("日本語", 5) == "日本"
    assert tui.pad("日", 4) == "日  "
    assert tui.pad("abc", 4) == "abc "


def test_clip_keeps_ansi_and_drops_overflow():
    s = tui.fg("dim") + "abcdef" + tui.RESET
    out = tui.clip(s, 3)
    assert out.startswith(tui.fg("dim"))
    assert out.endswith(tui.RESET)
    assert tui.cell_len(out) == 3
    assert "abc" in out and "def" not in out
    # image-protocol escapes must survive clipping untouched
    osc = "\x1b]1337;File=inline=1:QUJD\a"
    assert tui.clip(osc, 0) == ""
    assert tui.strip_ansi(osc) == ""


def test_narrow_terminal_lines_fit_width():
    """Every rendered line must fit the terminal (footer overflow + CJK widths)."""
    state = {
        "version": 1,
        "playing": True,
        "paused": False,
        "title": "夜に駆ける (YOASOBI) 完全版",
        "artist": "YOASOBI",
        "id": "x",
        "thumb": None,
        "duration": 200,
        "position": 10,
        "queue": [
            {"id": "2", "title": "打上花火 (DAOKO × 米津玄師)", "artist": "DAOKO", "duration": 180, "thumb": None},
            {"id": "3", "title": "Album 🔀 Unshuffle", "artist": "Artist", "duration": 90, "thumb": None},
        ],
    }
    out = io.StringIO()
    renderer = tui.Renderer(out=out)
    frame = renderer.render_once(state=state, size=(80, 24))
    lines = frame.split("\n")
    assert len(lines) == 24
    for i, line in enumerate(lines):
        w = _independent_disp_width(line)
        assert w <= 80, f"line {i} is {w} cells wide (max 80): {line!r}"


def test_wide_terminal_keeps_full_footer():
    state = {"version": 1, "playing": True, "title": "Song", "artist": "A", "queue": []}
    renderer = tui.Renderer(out=io.StringIO())
    frame = renderer.render_once(state=state, size=(120, 32))
    footer_lines = [l for l in frame.split("\n") if "type to search" in l]
    assert footer_lines
    assert tui.FOOTER in footer_lines[0]
