#!/usr/bin/env python3
"""Interactive Live TUI & Architecture Preview Server for MYX.

Runs the real MYX stack end-to-end inside an isolated runtime directory:
  Browser Keyboard / SGR 1006 Mouse
    -> tui.Renderer.handle_key()
    -> tui.send_command() over real AF_UNIX myx-cmd.sock (0600)
    -> player.PlayerDaemon.handle_command() + SQLite Library + DSPConfig
    -> player.PlayerDaemon.write_state() -> atomic myx-state.json
    -> tui.Renderer.render_once() + real Hann/Mel FFT FfmpegTapSource
    -> Pixel-accurate HTML5 Canvas terminal + Live Engine X-Ray Inspector
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Isolated runtime directory with short path for AF_UNIX sun_path safety
RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="myx-live-"))
os.environ["MYX_CMD_SOCK"] = str(RUNTIME_DIR / "myx-cmd.sock")
os.environ["MYX_MPV_SOCK"] = str(RUNTIME_DIR / "myx-mpv.sock")
os.environ["MYX_STATE"] = str(RUNTIME_DIR / "myx-state.json")
os.environ["MYX_DATA_DIR"] = str(RUNTIME_DIR / "data")
os.environ.pop("PYTEST_CURRENT_TEST", None)

from myx import dsp, player, sources, tui  # noqa: E402
from myx.library import Library  # noqa: E402


# ---------------------------------------------------------------------------
# Procedural Album Covers + Demo Catalog (for offline sandbox preview)
# ---------------------------------------------------------------------------


def _make_cover(path: Path, c1: tuple[int, int, int], c2: tuple[int, int, int], accent: tuple[int, int, int], style: str) -> str:
    w, h = 160, 160
    img = Image.new("RGB", (w, h), c1)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))

    if style == "sun":
        draw.ellipse([36, 26, 124, 114], fill=accent)
        for y in range(74, 116, 7):
            draw.rectangle([30, y, 130, y + 2], fill=c2)
        for x in range(0, w, 16):
            draw.line([(w // 2, 112), (x, h)], fill=accent, width=2)
    elif style == "city":
        rng = [28, 52, 38, 74, 92, 60, 84, 44, 68, 34]
        bw = w // len(rng)
        for idx, bh in enumerate(rng):
            x0 = idx * bw + 2
            draw.rectangle([x0, h - bh, x0 + bw - 3, h], fill=(16, 18, 30))
            for wy in range(h - bh + 6, h - 8, 10):
                draw.rectangle([x0 + 4, wy, x0 + bw - 7, wy + 4], fill=accent)
    elif style == "prism":
        draw.polygon([(80, 24), (24, 132), (136, 132)], outline=accent, fill=(24, 28, 34))
        draw.line([(0, 88), (58, 78)], fill=(255, 255, 255), width=4)
        rainbow = [(255, 75, 75), (255, 190, 50), (60, 235, 150), (65, 175, 255)]
        for i, col in enumerate(rainbow):
            draw.line([(102, 74 + i * 5), (160, 88 + i * 10)], fill=col, width=4)
    else:
        draw.ellipse([22, 22, 138, 138], outline=accent, width=6)
        draw.ellipse([54, 54, 106, 106], fill=accent)
        draw.ellipse([73, 73, 87, 87], fill=(15, 15, 20))

    img.save(path, format="PNG")
    return str(path)


ART_DIR = RUNTIME_DIR / "art"
ART_DIR.mkdir(parents=True, exist_ok=True)

DEMO_TRACKS: list[dict[str, Any]] = [
    {
        "id": "fHI8X4OXluQ",
        "title": "Blinding Lights",
        "artist": "The Weeknd",
        "duration": 200.0,
        "bpm": 171.0,
        "thumb": _make_cover(ART_DIR / "weeknd.png", (42, 8, 28), (195, 24, 74), (255, 168, 52), "sun"),
        "chapters": [
            {"title": "01. Non-Music Radio Intro", "start_time": 0.0, "end_time": 12.0},
            {"title": "02. Verse I — Empty City", "start_time": 12.0, "end_time": 58.0},
            {"title": "03. Synth Lead Hook", "start_time": 58.0, "end_time": 108.0},
            {"title": "04. Chorus — Sin City", "start_time": 108.0, "end_time": 158.0},
            {"title": "05. Final Overdrive", "start_time": 158.0, "end_time": 200.0},
        ],
        "sb_segments": [
            {"start": 0.0, "end": 12.0, "category": "music_offtopic"},
        ],
        "lrc": (
            "[00:00.00]♪ (Radio static intro skit)\n"
            "[00:12.00]Yeah, I've been tryna call\n"
            "[00:16.50]I've been on my own for long enough\n"
            "[00:21.00]Maybe you can show me how to love, maybe\n"
            "[00:27.00]I'm going through withdrawals\n"
            "[00:32.00]You don't even have to do too much\n"
            "[00:37.50]You can turn me on with just a touch, baby\n"
            "[00:44.00]I look around and Sin City's cold and empty\n"
            "[00:50.00]No one's around to judge me\n"
            "[00:58.00]♪ [Analog Synth Lead Drop] ♪\n"
            "[01:08.00]I said, ooh, I'm blinded by the lights\n"
            "[01:14.50]No, I can't sleep until I feel your touch\n"
            "[01:21.00]I said, ooh, I'm drowning in the night\n"
            "[01:28.00]Oh, when I'm like this, you're the one I trust\n"
            "[01:58.00]I'm just walking by to let you know\n"
            "[02:38.00]I said, ooh, I'm blinded by the lights"
        ),
    },
    {
        "id": "dX3k_QDnzHE",
        "title": "Midnight City",
        "artist": "M83",
        "duration": 243.0,
        "bpm": 105.0,
        "thumb": _make_cover(ART_DIR / "m83.png", (10, 18, 54), (24, 96, 168), (56, 228, 255), "city"),
        "chapters": [
            {"title": "01. Vocal Synth Riff", "start_time": 0.0, "end_time": 48.0},
            {"title": "02. Waiting in a Car", "start_time": 48.0, "end_time": 118.0},
            {"title": "03. Neon Bridge", "start_time": 118.0, "end_time": 182.0},
            {"title": "04. Saxophone Finale", "start_time": 182.0, "end_time": 243.0},
        ],
        "sb_segments": [],
        "lrc": (
            "[00:00.00]♪ [Iconic Pitch-Shifted Synth Hook] ♪\n"
            "[00:24.00]Waiting in a car\n"
            "[00:29.00]Waiting for a ride in the dark\n"
            "[00:36.00]The night city grows\n"
            "[00:41.00]Look and see her eyes, they glow\n"
            "[00:48.00]Waiting in a car\n"
            "[00:55.00]Waiting for a ride in the dark\n"
            "[01:18.00]The city is my church\n"
            "[01:25.00]It wraps me in the sparkling twilight\n"
            "[03:02.00]♪ [Saxophone Solo] ♪"
        ),
    },
    {
        "id": "TCd6PfxOy0Y",
        "title": "Veridis Quo",
        "artist": "Daft Punk",
        "duration": 345.0,
        "bpm": 114.0,
        "thumb": _make_cover(ART_DIR / "daftpunk.png", (12, 32, 26), (26, 78, 56), (92, 242, 168), "prism"),
        "chapters": [
            {"title": "01. Baroque Organ", "start_time": 0.0, "end_time": 85.0},
            {"title": "02. Analog Bass Arpeggio", "start_time": 85.0, "end_time": 210.0},
            {"title": "03. Horizon Fade", "start_time": 210.0, "end_time": 345.0},
        ],
        "sb_segments": [],
        "lrc": (
            "[00:00.00]♪ [Instrumental — Baroque Synth Organ] ♪\n"
            "[01:25.00]♪ [Warm French-House Bassline Enters] ♪\n"
            "[03:30.00]♪ [Counterpoint Lead Melody] ♪"
        ),
    },
    {
        "id": "x8VYWazR5mE",
        "title": "夜に駆ける (Racing Into the Night)",
        "artist": "YOASOBI",
        "duration": 261.0,
        "bpm": 130.0,
        "thumb": _make_cover(ART_DIR / "yoasobi.png", (38, 18, 66), (218, 72, 128), (255, 132, 188), "vinyl"),
        "chapters": [
            {"title": "01. Piano Intro", "start_time": 0.0, "end_time": 35.0},
            {"title": "02. Verse — 沈むように", "start_time": 35.0, "end_time": 98.0},
            {"title": "03. Chorus — 夜に駆ける", "start_time": 98.0, "end_time": 261.0},
        ],
        "sb_segments": [],
        "lrc": (
            "[00:00.00]沈むように溶けてゆくように\n"
            "[00:15.00]二人だけの空が広がる夜に\n"
            "[00:35.00]「さよなら」だけだった\n"
            "[00:40.00]その一言で全てが分かった\n"
            "[01:38.00]騒がしい日々に笑えない君に\n"
            "[01:45.00]思い付く限り眩しい明日を"
        ),
    },
    {
        "id": "5qap5aO4i9A",
        "title": "Snowfall (Slowed Tape)",
        "artist": "Øneheart × reidenshi",
        "duration": 164.0,
        "bpm": 84.0,
        "thumb": _make_cover(ART_DIR / "snowfall.png", (16, 24, 42), (48, 76, 118), (154, 204, 255), "city"),
        "chapters": [
            {"title": "01. Ambient Pad", "start_time": 0.0, "end_time": 68.0},
            {"title": "02. Sub-Bass Swell", "start_time": 68.0, "end_time": 164.0},
        ],
        "sb_segments": [],
        "lrc": "[00:00.00]♪ [Ambient Tape Loops & Sub-Bass] ♪",
    },
]

CATALOG_BY_ID = {t["id"]: t for t in DEMO_TRACKS}


# ---------------------------------------------------------------------------
# Simulated mpv Engine (drives real PlayerDaemon without needing hardware audio)
# ---------------------------------------------------------------------------


class SimulatedMpv:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.playlist: list[dict[str, Any]] = []
        self.playlist_pos: int = 0
        self.paused: bool = False
        self.muted: bool = False
        self.volume: int = 85
        self.speed: float = 1.0
        self.pitch_correction: str = "yes"
        self.af: str = ""
        self.loop_file: str = "no"
        self.force_media_title: str = ""
        self._pos: float = 14.0
        self._last_tick: float = time.monotonic()

    def _advance_clock(self) -> None:
        now = time.monotonic()
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        if self.paused or not self.playlist:
            return
        cur = self.playlist[min(self.playlist_pos, len(self.playlist) - 1)]
        dur = float(cur.get("duration") or 200.0)
        self._pos += dt * self.speed
        if self._pos >= dur:
            if self.loop_file == "inf":
                self._pos = 0.0
            elif self.playlist_pos + 1 < len(self.playlist):
                self.playlist_pos += 1
                self._pos = 0.0
            else:
                self.playlist.clear()
                self.playlist_pos = 0
                self._pos = 0.0

    def command(self, cmd: list[Any]) -> Any:
        with self.lock:
            self._advance_clock()
            if not cmd:
                return None
            op = cmd[0]
            if op == "get_property":
                prop = cmd[1]
                if prop == "playlist":
                    return [dict(x) for x in self.playlist]
                if prop == "playlist-pos":
                    return self.playlist_pos if self.playlist else 0
                if prop == "time-pos":
                    return round(self._pos, 2)
                if prop == "duration":
                    if not self.playlist:
                        return 0.0
                    return self.playlist[min(self.playlist_pos, len(self.playlist) - 1)].get("duration", 200.0)
                if prop == "volume":
                    return self.volume
                if prop == "pause":
                    return self.paused
                if prop == "mute":
                    return self.muted
                if prop == "media-title":
                    return self.force_media_title
                return None
            if op == "set_property":
                prop, val = cmd[1], cmd[2]
                if prop == "pause":
                    self.paused = bool(val)
                elif prop == "mute":
                    self.muted = bool(val)
                elif prop == "volume":
                    self.volume = int(val)
                elif prop == "af":
                    self.af = str(val)
                elif prop == "speed":
                    self.speed = float(val)
                elif prop == "audio-pitch-correction":
                    self.pitch_correction = str(val)
                elif prop == "loop-file":
                    self.loop_file = str(val)
                elif prop == "force-media-title":
                    self.force_media_title = str(val)
                return None
            if op == "cycle":
                if cmd[1] == "pause":
                    self.paused = not self.paused
                elif cmd[1] == "mute":
                    self.muted = not self.muted
                return None
            if op == "loadfile":
                url, mode = str(cmd[1]), str(cmd[2] if len(cmd) > 2 else "replace")
                vid = url.rsplit("/", 1)[-1].split(".")[0]
                info = CATALOG_BY_ID.get(vid, DEMO_TRACKS[0])
                entry = {
                    "filename": url,
                    "title": info["title"],
                    "duration": info["duration"],
                }
                if mode == "replace":
                    self.playlist = [entry]
                    self.playlist_pos = 0
                    self._pos = 14.0 if vid == "fHI8X4OXluQ" and not hasattr(self, "_booted") else 0.0
                    self._booted = True
                else:
                    self.playlist.append(entry)
                return None
            if op == "playlist-next":
                if self.playlist_pos + 1 < len(self.playlist):
                    self.playlist_pos += 1
                    self._pos = 0.0
                else:
                    self.playlist.clear()
                    self.playlist_pos = 0
                    self._pos = 0.0
                return None
            if op == "playlist-prev":
                self._pos = 0.0
                return None
            if op == "playlist-remove":
                idx = int(cmd[1])
                if 0 <= idx < len(self.playlist):
                    self.playlist.pop(idx)
                    if idx < self.playlist_pos:
                        self.playlist_pos = max(0, self.playlist_pos - 1)
                return None
            if op == "seek":
                val = float(cmd[1])
                mode = str(cmd[2] if len(cmd) > 2 else "relative")
                dur = 200.0
                if self.playlist:
                    dur = float(self.playlist[min(self.playlist_pos, len(self.playlist) - 1)].get("duration", 200.0))
                if mode == "absolute":
                    self._pos = max(0.0, min(dur, val))
                else:
                    self._pos = max(0.0, min(dur, self._pos + val))
                return None
            return None


# ---------------------------------------------------------------------------
# Wire offline demo hooks into `sources` so Search, Lyrics, Art & SB work live
# ---------------------------------------------------------------------------


def _demo_search(query: str) -> list[dict[str, Any]]:
    q = query.lower().strip()
    scored = []
    for t in DEMO_TRACKS:
        hay = f"{t['title']} {t['artist']}".lower()
        score = 2 if q in hay else (1 if any(tok in hay for tok in q.split()) else 0)
        scored.append((score, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    matches = [t for sc, t in scored if sc > 0] or list(DEMO_TRACKS)
    return [
        {
            "id": t["id"],
            "title": t["title"],
            "artist": t["artist"],
            "duration": t["duration"],
            "thumb": t["thumb"],
            "chapters": t["chapters"],
        }
        for t in matches
    ]


sources.search = _demo_search  # type: ignore[assignment]
sources.meta = lambda vid: dict(CATALOG_BY_ID.get(vid, DEMO_TRACKS[0]))  # type: ignore[assignment]
sources.stream_url = lambda vid: f"https://demo-stream.invalid/{vid}.opus"  # type: ignore[assignment]
sources.mix_chain = lambda vid: [t["id"] for t in DEMO_TRACKS if t["id"] != vid]  # type: ignore[assignment]
sources.mix_chain_items = lambda vid: _demo_search("")  # type: ignore[assignment]
sources.cache_thumb = lambda vid, url: str(CATALOG_BY_ID.get(vid, DEMO_TRACKS[0])["thumb"])  # type: ignore[assignment]
sources.lyrics = lambda title, artist, duration_s=None: next(  # type: ignore[assignment]
    ({"lrc": t["lrc"], "plain": None} for t in DEMO_TRACKS if t["title"] == title),
    {"lrc": DEMO_TRACKS[0]["lrc"], "plain": None},
)
sources.sponsorblock_segments = lambda vid: list(CATALOG_BY_ID.get(vid, {}).get("sb_segments", []))  # type: ignore[assignment]


def _demo_cache_audio(vid: str) -> str:
    audio_dir = RUNTIME_DIR / "audio-cache"
    audio_dir.mkdir(parents=True, exist_ok=True)
    p = audio_dir / f"{vid}.opus"
    if not p.exists():
        p.write_bytes(b"OggS-simulated-opus-payload-" * 512)
    return str(p)


sources.cache_audio = _demo_cache_audio  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Boot Real PlayerDaemon + Real FfmpegTapSource FFT + Real Renderer
# ---------------------------------------------------------------------------

sim_mpv = SimulatedMpv()
lib = Library(RUNTIME_DIR / "data" / "library.db")
# Pre-seed 2 liked songs & 1 saved playlist so user sees SQLite working right away
lib.set_liked(DEMO_TRACKS[1], True)
lib.set_liked(DEMO_TRACKS[2], True)
lib.replace_playlist_tracks("Synthwave Night Drive", DEMO_TRACKS[:3])

daemon = player.PlayerDaemon(url=DEMO_TRACKS[0]["id"], library=lib, sponsorblock=True)
daemon._mpv = sim_mpv.command  # type: ignore[method-assign]

IPC_LOG: list[dict[str, Any]] = []
_orig_handle = daemon.handle_command


def _logged_handle_command(req: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    resp = _orig_handle(req)
    try:
        daemon._maintain_playback()
        daemon.write_state()
    except Exception:
        pass
    dt_ms = round((time.perf_counter() - t0) * 1000, 2)
    IPC_LOG.append(
        {
            "ts": time.strftime("%H:%M:%S"),
            "req": req,
            "resp": resp,
            "ms": dt_ms,
        }
    )
    if len(IPC_LOG) > 30:
        del IPC_LOG[:-30]
    return resp


daemon.handle_command = _logged_handle_command  # type: ignore[method-assign]

# Start initial track + queue + real command socket server + real state loop
daemon._play_now(DEMO_TRACKS[0]["id"])
for extra in DEMO_TRACKS[1:]:
    daemon._append_track(extra, mark_seen=True)

threading.Thread(target=daemon._command_server, daemon=True).start()


def _bg_daemon_loop() -> None:
    while not daemon.stop.is_set():
        try:
            daemon._maintain_playback()
            daemon.write_state()
        except Exception:
            pass
        time.sleep(0.10)


threading.Thread(target=_bg_daemon_loop, daemon=True).start()

# Feed real multi-harmonic synthesized PCM audio into the REAL FfmpegTapSource
_pcm_r, _pcm_w = os.pipe()
_fake_proc = types.SimpleNamespace(
    stdout=os.fdopen(_pcm_r, "rb"), terminate=lambda: None, pid=None
)
live_tap = tui.FfmpegTapSource(proc=_fake_proc, sample_rate=16000)


def _synth_pcm_feeder() -> None:
    phase = 0.0
    sr = 16000
    chunk_n = 1600
    while not daemon.stop.is_set():
        if not sim_mpv.paused and sim_mpv.playlist:
            cur_title = sim_mpv.playlist[min(sim_mpv.playlist_pos, len(sim_mpv.playlist) - 1)].get("title")
            trk = next((t for t in DEMO_TRACKS if t["title"] == cur_title), DEMO_TRACKS[0])
            bpm = float(trk.get("bpm", 120.0)) * sim_mpv.speed
            beat_hz = bpm / 60.0
            t = (np.arange(chunk_n, dtype=np.float32) + phase) / sr
            phase += chunk_n

            beat_env = np.exp(-((t * beat_hz) % 1.0) * 5.5)
            sub = np.sin(2 * math.pi * 62.0 * t) * beat_env * 0.65
            bass = np.sin(2 * math.pi * 135.0 * t) * (0.3 + 0.3 * np.sin(2 * math.pi * 0.5 * beat_hz * t))
            mid = np.sin(2 * math.pi * (440.0 + 110.0 * np.sin(t * 1.7)) * t) * 0.24
            arp = np.sin(2 * math.pi * (1320.0 + 440.0 * ((t * beat_hz * 2).astype(int) % 4)) * t) * 0.16
            hat_env = np.exp(-((t * beat_hz * 2.0) % 1.0) * 14.0)
            air = np.sin(2 * math.pi * 5400.0 * t + np.sin(2 * math.pi * 830.0 * t) * 3.0) * hat_env * 0.14

            # Reflect EQ preset energy in synthesized audio too!
            preset = daemon.dsp.preset
            if preset in ("bass_boost", "electronic", "rock"):
                sub *= 1.65
                bass *= 1.4
            elif preset == "vocal":
                mid *= 1.8
            elif preset == "treble_sparkle":
                air *= 2.1

            mix = np.clip((sub + bass + mid + arp + air) * 18000.0, -32000, 32000).astype(np.int16)
            try:
                os.write(_pcm_w, mix.tobytes())
            except OSError:
                break
        time.sleep(0.085)


threading.Thread(target=_synth_pcm_feeder, daemon=True).start()

RENDER_COLS = 112
RENDER_ROWS = 32
RENDER_LOCK = threading.Lock()
renderer = tui.Renderer(
    state_path=Path(os.environ["MYX_STATE"]),
    out=io.StringIO(),
    spectrum=live_tap,
)
renderer.spectrum_auto = False  # keep live_tap bound
renderer.async_search = False


# ---------------------------------------------------------------------------
# Fast ANSI Frame -> Structured Cell Runs Parser for Pixel-Crisp HTML5 Canvas
# ---------------------------------------------------------------------------

_SGR_TOKEN_RE = re.compile(r"\x1b\[([0-9;]*)m|\x1b\[[0-9;?]*[A-Za-z]")


def _parse_ansi_frame_to_rows(frame: str, cols: int, rows: int) -> list[list[list[Any]]]:
    """Convert Renderer ANSI output into per-row run lists: [text, fg_hex, bg_hex, bold_int]."""
    raw_lines = frame.split("\n")
    out_rows: list[list[list[Any]]] = []
    default_fg = tui.THEME["fg"]
    default_bg = tui.THEME["bg"]

    for r_idx in range(min(rows, len(raw_lines))):
        line = raw_lines[r_idx]
        if r_idx == 0:
            # Strip leading HOME/HIDE control codes on row 0
            line = re.sub(r"^(?:\x1b\[[0-9;?]*[A-Za-z])+", "", line)
        cur_fg = default_fg
        cur_bg = default_bg
        cur_bold = 0
        runs: list[list[Any]] = []
        pos = 0

        def push_text(txt: str) -> None:
            if not txt:
                return
            if runs and runs[-1][1] == cur_fg and runs[-1][2] == cur_bg and runs[-1][3] == cur_bold:
                runs[-1][0] += txt
            else:
                runs.append([txt, cur_fg, cur_bg, cur_bold])

        for m in _SGR_TOKEN_RE.finditer(line):
            if m.start() > pos:
                push_text(line[pos : m.start()])
            pos = m.end()
            params_str = m.group(1)
            if params_str is None:
                continue
            parts = [int(p) if p.isdigit() else 0 for p in params_str.split(";")] if params_str else [0]
            i = 0
            while i < len(parts):
                code = parts[i]
                if code == 0:
                    cur_fg = default_fg
                    cur_bg = default_bg
                    cur_bold = 0
                elif code == 1:
                    cur_bold = 1
                elif code == 2:
                    cur_fg = tui.THEME["dim"]
                elif code == 38 and i + 4 < len(parts) and parts[i + 1] == 2:
                    cur_fg = f"#{parts[i+2]:02x}{parts[i+3]:02x}{parts[i+4]:02x}"
                    i += 4
                elif code == 48 and i + 4 < len(parts) and parts[i + 1] == 2:
                    cur_bg = f"#{parts[i+2]:02x}{parts[i+3]:02x}{parts[i+4]:02x}"
                    i += 4
                i += 1
        if pos < len(line):
            push_text(line[pos:])
        out_rows.append(runs)
    return out_rows


def _build_snapshot() -> dict[str, Any]:
    with RENDER_LOCK:
        renderer.out = io.StringIO()
        state = tui.read_state(renderer.state_path)
        if renderer.overlay == "search":
            renderer._maybe_fire_search()
        frame = renderer.render_once(state=state, size=(RENDER_COLS, RENDER_ROWS))
        grid_rows = _parse_ansi_frame_to_rows(frame, RENDER_COLS, RENDER_ROWS)
        accent_hex = renderer._accent_hex()
        accent2_hex = renderer._accent2_hex()
        overlay = renderer.overlay
        view = renderer.view

    dsp_cmds = dsp.mpv_dsp_commands(daemon.dsp)
    return {
        "cols": RENDER_COLS,
        "rows": RENDER_ROWS,
        "grid": grid_rows,
        "accent": accent_hex,
        "accent2": accent2_hex or accent_hex,
        "chameleon": renderer.chameleon,
        "view": view,
        "overlay": overlay,
        "state": {
            "playing": state.get("playing"),
            "paused": state.get("paused"),
            "title": state.get("title"),
            "artist": state.get("artist"),
            "position": state.get("position"),
            "duration": state.get("duration"),
            "volume": state.get("volume"),
            "loop": state.get("loop"),
            "liked": state.get("liked"),
            "liked_count": state.get("liked_count"),
            "chapter": state.get("chapter"),
            "cached_audio": bool(state.get("cached_audio")),
            "lyrics_offset": state.get("lyrics_offset"),
            "sponsorblock": state.get("sponsorblock"),
            "dsp": state.get("dsp"),
        },
        "mpv_dsp_cmds": dsp_cmds,
        "library": {
            "liked": [
                {"id": t["id"], "title": t["title"], "artist": t["artist"]}
                for t in lib.liked_tracks(8)
            ],
            "playlists": lib.list_playlists(),
            "most_played": [
                {"title": t["title"], "plays": t.get("play_count", 0)}
                for t in lib.most_played(5)
            ],
        },
        "ipc_log": list(reversed(IPC_LOG[-10:])),
    }


# ---------------------------------------------------------------------------
# Self-Contained Interactive Web UI (Zero External CDN Deps, Relative URLs)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>MYX — Live Terminal Music Player & Engine X-Ray</title>
<style>
  :root {
    --bg: #08080a;
    --panel: #111116;
    --border: #252530;
    --fg: #ebe6dc;
    --dim: #8b867e;
    --accent: #ff6b35;
    --accent2: #ff9f5a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: radial-gradient(circle at 20% 0%, rgba(255,107,53,0.10), transparent 45%), var(--bg);
    color: var(--fg);
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    padding: 12px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    background: rgba(14, 14, 18, 0.88);
    backdrop-filter: blur(10px);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-badge {
    font-family: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    font-weight: 800;
    letter-spacing: 0.22em;
    padding: 5px 10px;
    border-radius: 6px;
    background: var(--accent);
    color: #0b0b0d;
    font-size: 13px;
    transition: background 0.25s ease;
  }
  .brand-sub {
    font-size: 13px;
    color: var(--dim);
  }
  .pill-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  button.act {
    background: #191922;
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    font-family: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    cursor: pointer;
    transition: all 0.14s ease;
  }
  button.act:hover {
    border-color: var(--accent);
    background: #22222e;
    transform: translateY(-1px);
  }
  button.act.primary {
    border-color: var(--accent);
    color: var(--accent);
  }
  main {
    display: grid;
    grid-template-columns: minmax(640px, 1.45fr) minmax(330px, 1fr);
    gap: 16px;
    padding: 16px 20px 24px;
    flex: 1;
  }
  @media (max-width: 1120px) {
    main { grid-template-columns: 1fr; }
  }
  .term-card {
    background: #0d0d0d;
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.65);
    display: flex;
    flex-direction: column;
  }
  .term-titlebar {
    background: #16161c;
    padding: 8px 14px;
    border-bottom: 1px solid #23232c;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-family: ui-monospace, "JetBrains Mono", Menlo, monospace;
    font-size: 12px;
    color: var(--dim);
  }
  .dots { display: flex; gap: 6px; align-items: center; }
  .dot { width: 10px; height: 10px; border-radius: 50%; }
  .dot.r { background: #ff5f56; }
  .dot.y { background: #ffbd2e; }
  .dot.g { background: #27c93f; }
  .focus-pill {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--accent);
    color: var(--accent);
  }
  .canvas-wrap {
    padding: 10px;
    display: flex;
    justify-content: center;
    align-items: center;
    background: #0d0d0d;
    outline: none;
    cursor: pointer;
    overflow-x: auto;
  }
  canvas#tui {
    display: block;
    max-width: 100%;
    height: auto;
    border-radius: 4px;
  }
  .term-hint {
    padding: 8px 14px;
    background: #121217;
    border-top: 1px solid #22222b;
    font-size: 12px;
    color: var(--dim);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }
  .xray-col {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
  }
  .card h3 {
    margin: 0 0 8px 0;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--accent);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .arch-flow {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 6px;
    font-family: ui-monospace, "JetBrains Mono", Menlo, monospace;
    font-size: 11px;
  }
  .arch-node {
    background: #181820;
    border: 1px solid #2b2b38;
    border-radius: 7px;
    padding: 8px;
  }
  .arch-node strong {
    display: block;
    color: var(--fg);
    margin-bottom: 3px;
  }
  .arch-node span { color: var(--dim); font-size: 10px; line-height: 1.35; display: block; }
  pre.codebox {
    margin: 0;
    padding: 8px 10px;
    background: #0a0a0e;
    border: 1px solid #20202a;
    border-radius: 6px;
    font-family: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    font-size: 11px;
    color: #d7d2c8;
    overflow-x: auto;
    max-height: 145px;
    line-height: 1.45;
  }
  .kv-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 6px;
    font-size: 12px;
    font-family: ui-monospace, "JetBrains Mono", Menlo, monospace;
  }
  .kv {
    background: #17171f;
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid #242430;
  }
  .kv .k { color: var(--dim); font-size: 10px; display: block; }
  .kv .v { color: var(--fg); font-weight: 600; }
  .swatch {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    vertical-align: middle;
    margin-right: 5px;
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="brand-badge" id="brandBadge">M Y X</span>
    <div>
      <div style="font-weight:600;font-size:14px;">Live Python TUI + Daemon + SQLite + DSP Engine</div>
      <div class="brand-sub">Click directly inside the terminal (progress bar, tabs, heart ♥, Liked Songs) or press real keys!</div>
    </div>
  </div>
  <div class="pill-row">
    <button class="act primary" onclick="sendKey(' ')">⏯ Space (Play/Pause)</button>
    <button class="act" onclick="sendKey('n')">⏭ Next (Chameleon Shift!)</button>
    <button class="act" onclick="sendKey('\x0c')">♥ Like (Ctrl-L)</button>
    <button class="act" onclick="sendKey('\x05')">🎛 Cycle EQ (Ctrl-E)</button>
    <button class="act" onclick="sendKey('l')">📜 Cycle View (l)</button>
    <button class="act" onclick="sendKey('/')">🔍 Search (/)</button>
    <button class="act" onclick="sendKey('\x0b')">⌘ Palette (Ctrl-K)</button>
    <button class="act" onclick="sendKey('}')">§ Next Chapter (})</button>
    <button class="act" onclick="triggerSB()">⏭ Demo SponsorBlock Skip</button>
    <button class="act" onclick="sendKey('?')">? Help</button>
  </div>
</header>

<main>
  <section class="term-card">
    <div class="term-titlebar">
      <div class="dots">
        <span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
        <span style="margin-left:8px;">myx — myx.tui.Renderer (112×32 · SGR 1006 Mouse ON · Synced 12 FPS)</span>
      </div>
      <span class="focus-pill" id="focusStatus">Keyboard Active — Type or Click</span>
    </div>
    <div class="canvas-wrap" id="termWrap" tabindex="0">
      <canvas id="tui"></canvas>
    </div>
    <div class="term-hint">
      <span>🖱️ <b>Try SGR 1006 Mouse:</b> Click anywhere on the progress bar to scrub · Click <code>Lyrics</code> / <code>Queue</code> tabs · Click <code>▸ Play Liked Songs ♥</code> · Wheel to scroll</span>
      <span id="chameleonInfo">Chameleon Palette: --</span>
    </div>
  </section>

  <aside class="xray-col">
    <div class="card">
      <h3><span>How MYX Works — Live Architecture Pipeline</span><span id="fpsLabel" style="color:var(--dim)">12 FPS</span></h3>
      <div class="arch-flow">
        <div class="arch-node">
          <strong>1. TUI / CLI / MCP</strong>
          <span>Zero playback logic. Sends JSON lines over <code>0600</code> Unix socket; reads state JSON.</span>
        </div>
        <div class="arch-node">
          <strong>2. PlayerDaemon</strong>
          <span>JIT 2-track buffer, SponsorBlock watchdog, Crossfade ramp &amp; thread-safe IPC.</span>
        </div>
        <div class="arch-node">
          <strong>3. DSP &amp; mpv</strong>
          <span>Compiles 10 EQ presets, 5-band EQ &amp; <code>dynaudnorm</code> into <code>lavfi=[...]</code>.</span>
        </div>
        <div class="arch-node">
          <strong>4. SQLite WAL</strong>
          <span>Persists ♥ Likes, Playlists, M3U/YT imports, LRC offsets &amp; Opus offline cache.</span>
        </div>
      </div>
    </div>

    <div class="card">
      <h3><span>Live Engine Telemetry (<code>myx-state.json</code>)</span><span id="offlineBadge" style="color:var(--dim)">stream</span></h3>
      <div class="kv-grid" id="telemetryGrid"></div>
    </div>

    <div class="card">
      <h3>
        <span>Audiophile DSP → <code>mpv</code> IPC Commands</span>
        <span>
          <button class="act" style="padding:2px 7px;font-size:11px;" onclick="sendCmd('normalize')">Toggle Norm</button>
          <button class="act" style="padding:2px 7px;font-size:11px;" onclick="toggleXfade()">Toggle 4s Xfade</button>
          <button class="act" style="padding:2px 7px;font-size:11px;" onclick="sendCmd('cache_track', {blocking:true})">↓ Cache Offline</button>
        </span>
      </h3>
      <pre class="codebox" id="dspBox"></pre>
    </div>

    <div class="card">
      <h3><span>Persistent SQLite <code>library.db</code> (WAL)</span><span id="likedCountBadge" style="color:var(--dim)"></span></h3>
      <pre class="codebox" id="sqliteBox"></pre>
    </div>

    <div class="card">
      <h3><span>Live Socket Wire Log (<code>myx-cmd.sock</code>)</span><span style="color:var(--dim)">JSON-per-line</span></h3>
      <pre class="codebox" id="ipcBox"></pre>
    </div>
  </aside>
</main>

<script>
const canvas = document.getElementById("tui");
const ctx = canvas.getContext("2d", { alpha: false });
const wrap = document.getElementById("termWrap");

const CELL_W = 8.4;
const CELL_H = 17;
const DPR = Math.min(2, window.devicePixelRatio || 1);
let COLS = 112;
let ROWS = 32;
let lastXfade = 0;

function resizeCanvas(cols, rows) {
  if (canvas.width === Math.round(cols * CELL_W * DPR) && canvas.height === Math.round(rows * CELL_H * DPR)) return;
  COLS = cols;
  ROWS = rows;
  canvas.width = Math.round(cols * CELL_W * DPR);
  canvas.height = Math.round(rows * CELL_H * DPR);
  canvas.style.width = Math.round(cols * CELL_W) + "px";
  canvas.style.height = Math.round(rows * CELL_H) + "px";
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.textBaseline = "top";
}

function charCells(cp) {
  if (
    (cp >= 0x1100 && cp <= 0x115f) ||
    (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe10 && cp <= 0xfe6f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f300 && cp <= 0x1faff)
  ) return 2;
  return 1;
}

function drawGrid(grid, cols, rows) {
  resizeCanvas(cols, rows);
  ctx.fillStyle = "#0d0d0d";
  ctx.fillRect(0, 0, cols * CELL_W, rows * CELL_H);

  for (let r = 0; r < grid.length; r++) {
    const runs = grid[r];
    const py = r * CELL_H;
    let c = 0;
    for (let i = 0; i < runs.length; i++) {
      const [text, fg, bg, bold] = runs[i];
      for (const ch of text) {
        if (c >= cols) break;
        const cp = ch.codePointAt(0);
        const wCells = charCells(cp);
        const px = c * CELL_W;
        const pw = wCells * CELL_W;

        if (ch === "▀") {
          // Exact pixel half-block rendering for crisp album art & spectrum caps!
          ctx.fillStyle = bg;
          ctx.fillRect(px, py + CELL_H / 2, Math.ceil(pw), Math.ceil(CELL_H / 2));
          ctx.fillStyle = fg;
          ctx.fillRect(px, py, Math.ceil(pw), Math.ceil(CELL_H / 2));
        } else if (ch === "█") {
          ctx.fillStyle = fg;
          ctx.fillRect(px, py, Math.ceil(pw), CELL_H);
        } else {
          if (bg && bg !== "#0d0d0d") {
            ctx.fillStyle = bg;
            ctx.fillRect(px, py, Math.ceil(pw), CELL_H);
          }
          if (ch !== " ") {
            ctx.fillStyle = fg;
            ctx.font = (bold ? "700 " : "400 ") + "13.2px 'JetBrains Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace";
            ctx.fillText(ch, px, py + 2);
          }
        }
        c += wCells;
      }
    }
  }
}

async function sendKey(key) {
  const res = await fetch("api/input", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key })
  });
  const snap = await res.json();
  applySnapshot(snap);
}

async function sendCmd(cmd, extra = {}) {
  const res = await fetch("api/cmd", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd, ...extra })
  });
  const snap = await res.json();
  applySnapshot(snap);
}

function toggleXfade() {
  const next = lastXfade > 0 ? 0 : 4;
  sendCmd("crossfade", { arg: next });
}

async function triggerSB() {
  // Play Blinding Lights at 2.0s where the [0..12s] SponsorBlock non-music intro segment sits!
  await sendCmd("play_id", { arg: "fHI8X4OXluQ" });
  await sendCmd("sponsorblock", { arg: true });
  await sendCmd("jump", { arg: 2.0 });
}

// SGR 1006 Mouse Support right on the HTML5 Canvas!
canvas.addEventListener("mousedown", (ev) => {
  wrap.focus();
  const rect = canvas.getBoundingClientRect();
  const scaleX = COLS / rect.width;
  const scaleY = ROWS / rect.height;
  const col1 = Math.max(1, Math.min(COLS, Math.floor((ev.clientX - rect.left) * scaleX) + 1));
  const row1 = Math.max(1, Math.min(ROWS, Math.floor((ev.clientY - rect.top) * scaleY) + 1));
  const btn = ev.button === 2 ? 2 : 0;
  sendKey(`\x1b[<${btn};${col1};${row1}M`);
});

canvas.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const col1 = Math.max(1, Math.min(COLS, Math.floor((ev.clientX - rect.left) * (COLS / rect.width)) + 1));
  const row1 = Math.max(1, Math.min(ROWS, Math.floor((ev.clientY - rect.top) * (ROWS / rect.height)) + 1));
  const code = ev.deltaY < 0 ? 64 : 65;
  sendKey(`\x1b[<${code};${col1};${row1}M`);
}, { passive: false });

window.addEventListener("keydown", (ev) => {
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA") return;
  let seq = null;
  if (ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    const k = ev.key.toLowerCase();
    if (k === "k") seq = "\x0b";
    else if (k === "l") seq = "\x0c";
    else if (k === "e") seq = "\x05";
    else if (k === "b") seq = "\x02";
    else if (k === "t") seq = "\x14";
    else if (k === "r") seq = "\x12";
    else if (k === "g") seq = "\x07";
    else if (k === "u") seq = "\x15";
    else if (k === "n") seq = "\x0e";
    else if (k === "p") seq = "\x10";
  } else if (!ev.metaKey && !ev.altKey) {
    if (ev.key === "ArrowUp") seq = "\x1b[A";
    else if (ev.key === "ArrowDown") seq = "\x1b[B";
    else if (ev.key === "ArrowRight") seq = "\x1b[C";
    else if (ev.key === "ArrowLeft") seq = "\x1b[D";
    else if (ev.key === "Escape") seq = "\x1b";
    else if (ev.key === "Enter") seq = "\r";
    else if (ev.key === "Tab") seq = "\t";
    else if (ev.key === "Backspace") seq = "\x7f";
    else if (ev.key === "Delete") seq = "\x1b[3~";
    else if (ev.key.length === 1 && ev.key !== "q") seq = ev.key;
  }
  if (seq !== null) {
    ev.preventDefault();
    sendKey(seq);
  }
});

function applySnapshot(snap) {
  if (!snap || !snap.grid) return;
  drawGrid(snap.grid, snap.cols, snap.rows);
  document.documentElement.style.setProperty("--accent", snap.accent);
  document.getElementById("brandBadge").style.background = snap.accent;

  document.getElementById("chameleonInfo").innerHTML =
    `<span class="swatch" style="background:${snap.accent}"></span>` +
    `<span class="swatch" style="background:${snap.accent2}"></span>` +
    `Chameleon: <b>${snap.chameleon ? "ON" : "OFF"}</b> (${snap.accent} → ${snap.accent2})`;

  const st = snap.state || {};
  const dsp = st.dsp || {};
  lastXfade = dsp.crossfade_s || 0;
  document.getElementById("offlineBadge").textContent = st.cached_audio ? "↓ OFFLINE OPUS CACHED" : "🌐 JIT STREAM";
  document.getElementById("offlineBadge").style.color = st.cached_audio ? snap.accent : "var(--dim)";

  const cells = [
    ["Track", `${st.title || "--"} ${st.liked ? "♥" : "♡"}`],
    ["Artist / Chapter", `${st.artist || "--"}${st.chapter ? " · § " + st.chapter : ""}`],
    ["Position", `${(st.position || 0).toFixed(1)}s / ${(st.duration || 0).toFixed(0)}s (${st.paused ? "PAUSED" : "PLAYING"})`],
    ["DSP / EQ Preset", `${dsp.preset_label || "Flat"} (${dsp.speed || 1}x)`],
    ["SponsorBlock / Loop", `SB:${st.sponsorblock ? "ON" : "OFF"} · Repeat:${st.loop || "off"}`],
    ["View / Overlay", `view=${snap.view} · overlay=${snap.overlay || "none"}`],
  ];
  document.getElementById("telemetryGrid").innerHTML = cells
    .map(([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`)
    .join("");

  document.getElementById("dspBox").textContent = JSON.stringify(snap.mpv_dsp_cmds, null, 2);
  document.getElementById("likedCountBadge").textContent = `${(snap.library.liked || []).length} liked`;
  document.getElementById("sqliteBox").textContent = JSON.stringify(snap.library, null, 2);
  document.getElementById("ipcBox").textContent = (snap.ipc_log || [])
    .map(e => `[${e.ts}] ${JSON.stringify(e.req)} -> ${JSON.stringify(e.resp)} (${e.ms}ms)`)
    .join("\n") || "Click or press a key to watch live Unix socket IPC traffic...";
}

let inFlight = false;
setInterval(async () => {
  if (inFlight) return;
  inFlight = true;
  try {
    const r = await fetch("api/frame");
    const snap = await r.json();
    applySnapshot(snap);
  } catch (e) {
  } finally {
    inFlight = false;
  }
}, 95);

fetch("api/frame").then(r => r.json()).then(applySnapshot);
wrap.focus();
</script>
</body>
</html>
"""


class PreviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return  # keep logs clean

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.endswith("/api/frame") or path == "/api/frame":
            self._send_json(_build_snapshot())
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 65536)) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            payload = {}

        if path.endswith("/api/input") or path == "/api/input":
            key = str(payload.get("key") or "")
            if key and key != "q":
                with RENDER_LOCK:
                    renderer.handle_key(key)
            time.sleep(0.02)
            self._send_json(_build_snapshot())
            return

        if path.endswith("/api/cmd") or path == "/api/cmd":
            cmd = str(payload.get("cmd") or "")
            extra = {k: v for k, v in payload.items() if k != "cmd"}
            if cmd and cmd != "quit":
                try:
                    tui.send_command(cmd, **extra)
                except Exception:
                    pass
            time.sleep(0.03)
            self._send_json(_build_snapshot())
            return

        self._send_json({"error": "not found"}, status=404)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), PreviewHandler)
    print(f"[myx-preview] Live interactive TUI & Engine X-Ray listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
