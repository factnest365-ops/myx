import io
import json
import os
import pty
import subprocess
import sys
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

    frame = renderer.render_once(size=(100, 32))

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
    r.render_once(size=(100, 32))
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
