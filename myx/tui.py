"""Terminal renderer for MYX.

The TUI reads player state from a JSON file and sends newline-delimited JSON
commands to the player command socket. Playback logic lives in myx.player.
"""

from __future__ import annotations

import base64
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import IO, Any

try:  # numpy is allowed for the spectrum tap.
    import numpy as np
except Exception:  # pragma: no cover - exercised only without project deps
    np = None

try:  # pillow is a core project dependency; use for fast Lanczos half-block art.
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]

try:
    from .picker import (  # type: ignore
        KEYBINDS,
        TOUR_STEPS,
        PickerState,
        command_palette_search,
        format_duration,
    )
except Exception:  # fallback for hermetic env without package install
    from myx.picker import (  # type: ignore
        KEYBINDS,
        TOUR_STEPS,
        PickerState,
        command_palette_search,
        format_duration,
    )

STATE_PATH = Path(os.environ.get("MYX_STATE", Path(gettempdir()) / "myx-state.json"))
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", Path(gettempdir()) / "myx-cmd.sock"))
FOOTER = "type to search · ? help · q quit · space pause · n next · p prev · s shuffle · +,- vol · l lyrics · enter play"
CONTROLS_HINT = "[ ▶ Play ] [ ⏸ Pause ] [ ⏭ Next ] [ 🔀 Shuffle ]"
AUTO_HINT = (
    "♪  Search to add songs  ·  Updating player... run myx doctor if this persists"
)

THEME = {
    "bg": "#0d0d0d",
    "fg": "#e8e3d8",
    "accent": "#ff6b35",
    "dim": "#7a756c",
    "err": "#ff4444",
}


def _rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)


def fg(name: str) -> str:
    r, g, b = _rgb(THEME[name])
    return f"\x1b[38;2;{r};{g};{b}m"


def bg(name: str) -> str:
    r, g, b = _rgb(THEME[name])
    return f"\x1b[48;2;{r};{g};{b}m"


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CLEAR = "\x1b[2J\x1b[H"
HOME = "\x1b[H"
SYNC_ON = "\x1b[?2026h"
SYNC_OFF = "\x1b[?2026l"
HIDE = "\x1b[?25l"
SHOW = "\x1b[?25h"
ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"

_SGR_MOUSE_RE = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])$")
_PALETTE_CACHE: dict[str, tuple[str, str]] = {}


def _ensure_readable_accent(r: int, g: int, b: int) -> tuple[int, int, int]:
    """Ensure vibrant saturation & >= 4.5:1 luminance contrast against dark #0d0d0d."""
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if lum < 95.0:
        scale = min(3.2, 115.0 / max(12.0, lum))
        r = min(255, int(r * scale + 18))
        g = min(255, int(g * scale + 18))
        b = min(255, int(b * scale + 18))
    elif lum > 225.0:
        r = max(80, int(r * 0.85))
        g = max(80, int(g * 0.85))
        b = max(80, int(b * 0.85))
    return max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))


def extract_art_palette(path: str | None) -> tuple[str, str] | None:
    """Extract WCAG-safe (dominant_hex, secondary_hex) palette from cached album art."""
    if not path or Image is None:
        return None
    if path in _PALETTE_CACHE:
        return _PALETTE_CACHE[path]
    p = Path(path)
    try:
        if not p.is_file() or p.is_symlink() or p.stat().st_size > 15 * 1024 * 1024:
            return None
        with Image.open(p) as im:
            small = im.convert("RGB").resize((24, 24), Image.Resampling.BILINEAR)
            pixels = (
                list(small.get_flattened_data())
                if hasattr(small, "get_flattened_data")
                else list(small.getdata())
            )
    except Exception:
        return None
    if not pixels:
        return None

    buckets: dict[tuple[int, int, int], list[int]] = {}
    for r, g, b in pixels:
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        if lum < 22 or lum > 242:
            continue
        key = (r // 32, g // 32, b // 32)
        acc = buckets.get(key)
        if acc is None:
            buckets[key] = [1, r, g, b]
        else:
            acc[0] += 1
            acc[1] += r
            acc[2] += g
            acc[3] += b

    if not buckets:
        avg_r = sum(px[0] for px in pixels) // len(pixels)
        avg_g = sum(px[1] for px in pixels) // len(pixels)
        avg_b = sum(px[2] for px in pixels) // len(pixels)
        r1, g1, b1 = _ensure_readable_accent(avg_r, avg_g, avg_b)
        r2, g2, b2 = _ensure_readable_accent(min(255, r1 + 35), max(60, g1 - 20), min(255, b1 + 45))
    else:
        scored: list[tuple[float, tuple[int, int, int]]] = []
        for cnt, sr, sg, sb in buckets.values():
            mr, mg, mb = sr // cnt, sg // cnt, sb // cnt
            chroma = max(mr, mg, mb) - min(mr, mg, mb)
            score = cnt * (1.0 + (chroma / 85.0))
            scored.append((score, (mr, mg, mb)))
        scored.sort(key=lambda x: x[0], reverse=True)
        dom = scored[0][1]
        sec = dom
        for _, cand in scored[1:]:
            dist = abs(cand[0] - dom[0]) + abs(cand[1] - dom[1]) + abs(cand[2] - dom[2])
            if dist >= 75:
                sec = cand
                break
        if sec == dom:
            sec = (min(255, dom[0] + 45), min(255, dom[1] + 25), max(70, dom[2] - 35))
        r1, g1, b1 = _ensure_readable_accent(*dom)
        r2, g2, b2 = _ensure_readable_accent(*sec)

    res = (f"#{r1:02x}{g1:02x}{b1:02x}", f"#{r2:02x}{g2:02x}{b2:02x}")
    if len(_PALETTE_CACHE) >= 256:
        _PALETTE_CACHE.pop(next(iter(_PALETTE_CACHE)), None)
    _PALETTE_CACHE[path] = res
    return res


# CSI (cursor/color), OSC (iterm2 image; BEL or ST terminated), APC/DCS (kitty image).
ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b_[^\x1b]*\x1b\\"
    r"|\x1b[PX^][^\x1b]*\x1b\\"
)

_UNTRUSTED_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)"
    r"|\x1b_[^\x1b]*(?:\x1b\\|$)"
    r"|\x1b[PX^][^\x1b]*(?:\x1b\\|$)"
    r"|\x1b."
)


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def sanitize_untrusted(value: Any) -> str:
    """Strip terminal escapes and control characters from untrusted external text."""
    if value is None:
        return ""
    s = _UNTRUSTED_ESCAPE_RE.sub("", str(value))
    return "".join(
        " " if ch in ("\r", "\n", "\t") else ch
        for ch in s
        if ch in ("\r", "\n", "\t")
        or (ord(ch) >= 0x20 and not (0x7F <= ord(ch) <= 0x9F))
    )


def _cell_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_len(s: str) -> int:
    """Visible width of `s` in terminal cells (CJK/emoji count 2)."""
    return sum(_cell_width(ch) for ch in strip_ansi(s))


def crop(s: str, width: int) -> str:
    """Plain-text prefix fitting in `width` cells (never splits a wide char)."""
    plain = strip_ansi(s)
    out: list[str] = []
    w = 0
    for ch in plain:
        cw = _cell_width(ch)
        if w + cw > max(0, width):
            break
        out.append(ch)
        w += cw
    return "".join(out)


def clip(s: str, width: int) -> str:
    """Truncate to `width` cells, keeping every ANSI escape (incl. trailing RESET)."""
    if width <= 0:
        return ""
    out: list[str] = []
    w = 0
    over = False

    def take(chunk: str) -> None:
        nonlocal w, over
        for ch in chunk:
            if over:
                return
            cw = _cell_width(ch)
            if w + cw > width:
                over = True
                return
            out.append(ch)
            w += cw

    pos = 0
    for m in ANSI_RE.finditer(s):
        take(s[pos : m.start()])
        out.append(m.group(0))
        pos = m.end()
    take(s[pos:])
    return "".join(out)


def _plain_between(s: str, start_col: int, end_col: int | None = None) -> str:
    """Plain text covering display columns [start_col, end_col) of `s`."""
    out: list[str] = []
    w = 0
    for ch in s:
        cw = _cell_width(ch)
        if w >= start_col and (end_col is None or w < end_col):
            out.append(ch)
        w += cw
        if end_col is not None and w >= end_col:
            break
    return "".join(out)


def _ansi_between(s: str, start_col: int, end_col: int | None = None) -> str:
    """ANSI-preserving slice covering display columns [start_col, end_col) of `s`."""
    if end_col is not None and end_col <= start_col:
        return ""
    out: list[str] = []
    active_sgr: list[str] = []
    dirty_sgr: list[str] = []
    col = 0
    started = False

    def handle_text(chunk: str) -> bool:
        nonlocal col, started
        for ch in chunk:
            cw = _cell_width(ch)
            if cw == 0:
                if started and (end_col is None or col <= end_col):
                    out.append(ch)
                continue
            if col + cw <= start_col:
                col += cw
                continue
            if end_col is not None and col >= end_col:
                return False
            if not started:
                started = True
                if active_sgr:
                    out.extend(active_sgr)
                dirty_sgr.clear()
            elif dirty_sgr:
                out.extend(dirty_sgr)
                dirty_sgr.clear()
            if col >= start_col and (end_col is None or col + cw <= end_col):
                out.append(ch)
            else:
                ov_start = max(col, start_col)
                ov_end = min(col + cw, end_col) if end_col is not None else (col + cw)
                out.append(" " * max(0, ov_end - ov_start))
            col += cw
        return True

    pos = 0
    for m in ANSI_RE.finditer(s):
        if not handle_text(s[pos : m.start()]):
            break
        esc = m.group(0)
        if esc == RESET:
            active_sgr = [bg("bg"), fg("fg")]
            if started:
                dirty_sgr = [RESET, bg("bg"), fg("fg")]
        elif esc.startswith("\x1b[") and esc.endswith("m"):
            active_sgr.append(esc)
            if len(active_sgr) > 8:
                active_sgr = active_sgr[-8:]
            if started:
                dirty_sgr.append(esc)
        else:
            # Zero-width image escape (kitty/iterm2): preserve if anchored in range
            if col >= start_col and (end_col is None or col < end_col):
                out.append(esc)
        pos = m.end()
    else:
        handle_text(s[pos:])

    if out:
        out.append(RESET)
    return "".join(out)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - cell_len(s))


def mmss(value: Any) -> str:
    try:
        seconds = max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}:{seconds % 60:02d}"


def load_theme() -> None:
    path = Path.home() / ".config" / "myx" / "config.toml"
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
            return
        raw_text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    for raw in raw_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = [p.strip() for p in line.split("=", 1)]
        value = value.strip("\"'")
        if key in THEME and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            THEME[key] = value


def read_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise PermissionError("refusing symlinked state file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {
            "version": 1,
            "playing": False,
            "paused": True,
            "title": "MYX",
            "artist": "Waiting for player",
            "queue": [],
        }
    except Exception:
        pass
    return {
        "version": 1,
        "playing": False,
        "paused": True,
        "title": "State unavailable",
        "artist": "",
        "queue": [],
    }


def send_command(cmd: str, **payload: Any) -> dict[str, Any]:
    message = {"cmd": cmd, **payload}
    sock_timeout = 22.0 if cmd in ("search", "search_query") else 2.5
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(sock_timeout)
        sock.connect(str(CMD_SOCK))
        sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        total = 0
        while total < 1024 * 1024:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\n" in chunk:
                break
    raw = b"".join(chunks).decode("utf-8", errors="replace").splitlines()
    first = raw[0] if raw else ""
    return json.loads(first or '{"ok": true}')


def _terminate_proc(proc: Any) -> None:
    if not proc:
        return
    stdout = getattr(proc, "stdout", None)
    if stdout is not None:
        try:
            stdout.close()
        except Exception:
            pass
    try:
        proc.terminate()
    except Exception:
        return
    wait_fn = getattr(proc, "wait", None)
    if callable(wait_fn):
        try:
            wait_fn(timeout=0.2)
        except Exception:
            kill_fn = getattr(proc, "kill", None)
            if callable(kill_fn):
                try:
                    kill_fn()
                    wait_fn(timeout=0.2)
                except Exception:
                    pass


@dataclass
class SpectrumSource:
    bars: int = 24
    peaks: list[float] = field(default_factory=list)
    peak_decay: float = 0.08

    def __post_init__(self) -> None:
        if not self.peaks:
            self.peaks = [0.0] * self.bars

    def values(self) -> list[float]:
        return [0.04] * self.bars

    def values_with_peaks(self, *, paused: bool = False) -> tuple[list[float], list[float]]:
        vals = [0.04] * self.bars if paused else self.values()
        for i, v in enumerate(vals):
            if i < len(self.peaks):
                if v > self.peaks[i]:
                    self.peaks[i] = v
                else:
                    self.peaks[i] = max(0.0, self.peaks[i] - self.peak_decay)
        return vals, list(self.peaks)

    def close(self) -> None:
        pass


@dataclass
class CavaSource(SpectrumSource):
    proc: subprocess.Popen[bytes] | None = None
    _buf: bytes = b""
    _last_vals: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.proc is None and shutil.which("cava"):
            self.proc = subprocess.Popen(
                ["cava", "-p", "/dev/stdin"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if self.proc.stdin:
                self.proc.stdin.write(
                    b"[output]\nmethod = raw\nraw_target = /dev/stdout\ndata_format = ascii\nascii_max_range = 100\n"
                )
                self.proc.stdin.close()
        if self.proc and self.proc.stdout:
            try:
                os.set_blocking(self.proc.stdout.fileno(), False)
            except (OSError, ValueError, AttributeError):
                pass

    def values(self) -> list[float]:
        if not self.proc or not self.proc.stdout:
            return super().values()
        try:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
        except BlockingIOError:
            return list(self._last_vals) if self._last_vals else super().values()
        except OSError:
            return super().values()
        if not chunk:
            return super().values()  # EOF: cava exited
        self._buf += chunk
        if len(self._buf) > 65536:
            self._buf = self._buf[-65536:]
        if b"\n" not in self._buf:
            return list(self._last_vals) if self._last_vals else super().values()
        *lines, self._buf = self._buf.split(b"\n")
        line = lines[-1].decode(errors="ignore")
        nums = [int(x) / 100 for x in re.findall(r"\d+", line)]
        if not nums:
            return list(self._last_vals) if self._last_vals else super().values()
        vals = (nums[: self.bars] + [0.04] * self.bars)[: self.bars]
        self._last_vals = vals
        return vals

    def close(self) -> None:
        _terminate_proc(self.proc)
        self.proc = None


@dataclass
class FfmpegTapSource(SpectrumSource):
    stream_url: str | None = None
    start_pos: float = 0.0
    sample_rate: int = 16000
    proc: subprocess.Popen[bytes] | None = None
    _buf: bytes = b""
    _last_vals: list[float] = field(default_factory=list)
    _tap_paused: bool = False
    _anchor_pos: float = 0.0
    _anchor_mono: float = 0.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self._anchor_pos = max(0.0, float(self.start_pos or 0.0))
        self._anchor_mono = time.monotonic()
        if self.proc is None:
            self._spawn_ffmpeg(self._anchor_pos)
        elif self.proc and self.proc.stdout:
            try:
                os.set_blocking(self.proc.stdout.fileno(), False)
            except (OSError, ValueError, AttributeError):
                pass

    @staticmethod
    def _is_local_file(src: str | None) -> bool:
        if not src or src.startswith(("http://", "https://", "file:")):
            return False
        p = Path(src)
        if p.suffix.lower() not in (".opus", ".m4a", ".webm", ".ogg", ".mp3", ".flac", ".wav"):
            return False
        try:
            if not p.is_file() or p.is_symlink():
                return False
            if hasattr(os, "getuid") and p.stat().st_uid != os.getuid():
                return False
            return True
        except OSError:
            return False

    def _spawn_ffmpeg(self, seek_s: float = 0.0) -> None:
        if not isinstance(self.stream_url, str) or not shutil.which("ffmpeg") or np is None:
            return
        is_local = self._is_local_file(self.stream_url)
        is_http = self.stream_url.startswith(("https://", "http://"))
        if not (is_local or is_http):
            return
        args = ["ffmpeg", "-loglevel", "quiet", "-nostdin"]
        if is_http:
            args += ["-protocol_whitelist", "http,https,tcp,tls,crypto"]
        else:
            args += ["-protocol_whitelist", "file,pipe,crypto"]
        if seek_s >= 0.5:
            args += ["-ss", f"{seek_s:.2f}"]
        args += [
            "-re",
            "-i",
            self.stream_url,
            "-vn",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(self.sample_rate),
            "-",
        ]
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._anchor_pos = max(0.0, seek_s)
        self._anchor_mono = time.monotonic()
        self._tap_paused = False
        self._buf = b""
        if self.proc.stdout:
            try:
                os.set_blocking(self.proc.stdout.fileno(), False)
            except (OSError, ValueError, AttributeError):
                pass

    def sync_playback(self, *, paused: bool, position: float = 0.0) -> None:
        """Pause/resume ffmpeg via SIGSTOP/SIGCONT and resync on large seeks."""
        pid = getattr(self.proc, "pid", None)
        if paused:
            if not self._tap_paused:
                elapsed = max(0.0, time.monotonic() - self._anchor_mono)
                self._anchor_pos += elapsed
                self._tap_paused = True
                if isinstance(pid, int) and hasattr(signal, "SIGSTOP"):
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except OSError:
                        pass
            return

        if self._tap_paused:
            self._tap_paused = False
            self._anchor_mono = time.monotonic()
            if isinstance(pid, int) and hasattr(signal, "SIGCONT"):
                try:
                    os.kill(pid, signal.SIGCONT)
                except OSError:
                    pass

        if isinstance(pid, int) and position >= 0.0:
            est_pos = self._anchor_pos + max(0.0, time.monotonic() - self._anchor_mono)
            if abs(position - est_pos) > 3.0:
                self.close()
                self._spawn_ffmpeg(position)

    def values(self) -> list[float]:
        if not self.proc or not self.proc.stdout or np is None:
            return super().values()
        if self._tap_paused:
            return list(self._last_vals) if self._last_vals else super().values()
        try:
            chunk = os.read(self.proc.stdout.fileno(), 65536)
        except BlockingIOError:
            chunk = b""
        except OSError:
            return super().values()
        if chunk:
            self._buf += chunk
            if len(self._buf) > 32768:
                self._buf = self._buf[-32768:]
        if len(self._buf) < 1024:
            return list(self._last_vals) if self._last_vals else super().values()
        n = min(len(self._buf), 4096)
        n -= n % 2
        raw = self._buf[-n:]
        self._buf = b""
        if len(raw) < 512:
            return list(self._last_vals) if self._last_vals else super().values()
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        windowed = samples * np.hanning(len(samples)).astype(np.float32)
        mag = np.abs(np.fft.rfft(windowed))
        if len(mag) < 4 or float(mag.max(initial=0.0)) <= 1e-6:
            return super().values()

        # Log/Mel-like perceptual bin edges with +3 dB/octave HF compensation
        usable = mag[1:]
        edges = np.geomspace(1.0, float(len(usable)), num=self.bars + 1)
        vals: list[float] = []
        for i in range(self.bars):
            lo = max(0, int(edges[i]) - 1)
            hi = max(lo + 1, int(round(edges[i + 1])))
            band = usable[lo:hi]
            tilt = 1.0 + (i / max(1, self.bars - 1)) * 1.6
            energy = float(np.sqrt(np.mean(band * band))) * tilt if len(band) else 0.0
            vals.append(float(np.log1p(energy * 36.0)))

        peak_ref = max(max(vals), 0.35)
        normed = [min(1.0, v / peak_ref) for v in vals]
        if self._last_vals and len(self._last_vals) == len(normed):
            smoothed = [
                prev + (cur - prev) * (0.72 if cur >= prev else 0.38)
                for prev, cur in zip(self._last_vals, normed)
            ]
        else:
            smoothed = normed
        self._last_vals = smoothed
        return smoothed

    def close(self) -> None:
        pid = getattr(self.proc, "pid", None)
        if self._tap_paused and isinstance(pid, int) and hasattr(signal, "SIGCONT"):
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
        self._tap_paused = False
        _terminate_proc(self.proc)
        self.proc = None


def _bar_color(
    frac: float,
    accent_hex: str | None = None,
    accent2_hex: str | None = None,
) -> str:
    """Gradient from dim -> accent -> secondary accent / bright peak."""
    ar, ag, ab = _rgb(accent_hex or THEME["accent"])
    dr, dg, db = _rgb(THEME["dim"])
    t = max(0.0, min(1.0, frac))
    if accent2_hex:
        br, bg_c, bb = _rgb(accent2_hex)
        if t < 0.55:
            u = t / 0.55
            r = int(dr + (ar - dr) * u)
            g = int(dg + (ag - dg) * u)
            b = int(db + (ab - db) * u)
        else:
            u = (t - 0.55) / 0.45
            r = int(ar + (br - ar) * u)
            g = int(ag + (bg_c - ag) * u)
            b = int(ab + (bb - ab) * u)
    else:
        r = int(dr + (ar - dr) * t + (255 - ar) * t * 0.15)
        g = int(dg + (ag - dg) * t + (255 - ag) * t * 0.15)
        b = int(db + (ab - db) * t + (255 - ab) * t * 0.15)
    r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
    return f"\x1b[38;2;{r};{g};{b}m"


def make_spectrum(
    stream_url: str | None = None, *, start_pos: float = 0.0
) -> SpectrumSource:
    if shutil.which("cava"):
        try:
            return CavaSource()
        except Exception:
            pass
    if (
        isinstance(stream_url, str)
        and (
            stream_url.startswith(("https://", "http://"))
            or FfmpegTapSource._is_local_file(stream_url)
        )
        and shutil.which("ffmpeg")
        and np is not None
    ):
        try:
            return FfmpegTapSource(stream_url=stream_url, start_pos=start_pos)
        except Exception:
            pass
    return SpectrumSource()


_ORIGINAL_MAKE_SPECTRUM = make_spectrum


@dataclass
class ArtRenderer:
    last_key: tuple[str, int, int] | None = None
    last_lines: list[str] = field(default_factory=list)
    protocol: str = "none"

    @staticmethod
    def detect_protocol(env: dict[str, str] | None = None) -> str:
        env = env or os.environ
        term = env.get("TERM", "").lower()
        program = env.get("TERM_PROGRAM", "").lower()
        if (
            env.get("KITTY_WINDOW_ID")
            or "kitty" in term
            or "ghostty" in term
            or "ghostty" in program
        ):
            return "kitty"
        if "iterm" in program or "wezterm" in program:
            return "iterm2"
        return "half-block"

    def render(self, path: str | None, cells: int, rows: int) -> list[str]:
        if not path:
            return self._placeholder(cells, min(rows, cells // 2))
        key = (path, cells, rows)
        if key == self.last_key and self.last_lines:
            return self.last_lines
        self.protocol = self.detect_protocol()
        if self.protocol == "kitty":
            self.last_lines = self._kitty(path, cells, rows)
        elif self.protocol == "iterm2":
            self.last_lines = self._iterm2(path, cells, rows)
        else:
            self.last_lines = self._half_block(path, cells, rows)
        self.last_key = key
        return self.last_lines

    def _read_safe_art_bytes(self, path: str) -> bytes | None:
        p = Path(path)
        try:
            if not p.is_file() or p.is_symlink() or p.stat().st_size > 15 * 1024 * 1024:
                return None
            return p.read_bytes()
        except OSError:
            return None

    def _kitty(self, path: str, cells: int, rows: int) -> list[str]:
        raw_bytes = self._read_safe_art_bytes(path)
        if not raw_bytes:
            return self._placeholder(cells, rows)
        data = base64.b64encode(raw_bytes).decode()
        return [f"\x1b_Gf=100,a=T,s={cells},v={rows};{data}\x1b\\"] + [""] * max(
            0, rows - 1
        )

    def _iterm2(self, path: str, cells: int, rows: int) -> list[str]:
        raw_bytes = self._read_safe_art_bytes(path)
        if not raw_bytes:
            return self._placeholder(cells, rows)
        data = base64.b64encode(raw_bytes).decode()
        return [
            f"\x1b]1337;File=inline=1;width={cells};height={rows};preserveAspectRatio=1:{data}\a"
        ] + [""] * max(0, rows - 1)

    def _half_block(self, path: str, cells: int, rows: int) -> list[str]:
        px_w = max(2, cells)
        px_h = max(2, rows * 2)
        p = Path(path)
        if not p.is_file() or p.is_symlink():
            return self._placeholder(cells, rows)
        raw: bytes | None = None
        if Image is not None:
            try:
                with Image.open(p) as im:
                    rgba = im.convert("RGBA").resize(
                        (px_w, px_h), Image.Resampling.LANCZOS
                    )
                    raw = rgba.tobytes()
            except Exception:
                raw = None
        if raw is None:
            try:
                raw = subprocess.check_output(
                    [
                        "ffmpeg",
                        "-loglevel",
                        "quiet",
                        "-nostdin",
                        "-i",
                        str(p),
                        "-vf",
                        f"scale={px_w}:{px_h}",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgba",
                        "-",
                    ],
                    timeout=4,
                )
            except Exception:
                return self._placeholder(cells, rows)
        if len(raw) < px_w * px_h * 4:
            return self._placeholder(cells, rows)
        lines: list[str] = []
        for y in range(0, px_h, 2):
            parts = []
            for x in range(px_w):
                top = (y * px_w + x) * 4
                bot = ((y + 1) * px_w + x) * 4
                tr, tg, tb = raw[top], raw[top + 1], raw[top + 2]
                br, bgc, bb = raw[bot], raw[bot + 1], raw[bot + 2]
                parts.append(f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bgc};{bb}m▀")
            lines.append("".join(parts) + RESET)
        return lines

    def _placeholder(self, cells: int, rows: int) -> list[str]:
        return (
            [fg("dim") + "┌" + "─" * max(0, cells - 2) + "┐" + RESET]
            + [
                fg("dim") + "│" + " " * max(0, cells - 2) + "│" + RESET
                for _ in range(max(0, rows - 2))
            ]
            + [fg("dim") + "└" + "─" * max(0, cells - 2) + "┘" + RESET]
        )


@dataclass
class LyricsState:
    lrc: list[tuple[float, str]] = field(default_factory=list)
    plain: list[str] = field(default_factory=list)
    offset: float = 0.0
    loaded_id: str | None = None
    _loaded_sig: tuple[str, bool, bool] | None = None

    def load_from_state(self, state: dict[str, Any]) -> None:
        sid = str(state.get("id") or "")
        raw_lrc = str(state.get("lrc") or "")
        raw_plain = str(state.get("lyrics") or state.get("plain") or "")
        sig = (sid, bool(raw_lrc), bool(raw_plain))
        if sig == self._loaded_sig:
            return
        if sid != self.loaded_id:
            try:
                self.offset = float(state.get("lyrics_offset") or 0.0)
            except (TypeError, ValueError):
                self.offset = 0.0
        self.loaded_id = sid
        self._loaded_sig = sig
        self.lrc = parse_lrc(raw_lrc)
        self.plain = [
            sanitize_untrusted(line).strip()
            for line in raw_plain.splitlines()
            if sanitize_untrusted(line).strip()
        ]

    def visible(self, position: float, height: int) -> list[tuple[str, bool]]:
        if self.lrc:
            t = position + self.offset
            idx = 0
            for i, (ts, _) in enumerate(self.lrc):
                if ts <= t:
                    idx = i
            start = max(0, idx - height // 2)
            return [
                (line, start + n == idx)
                for n, (_, line) in enumerate(self.lrc[start : start + height])
            ]
        lines = self.plain or ["No lyrics found"]
        start = max(0, (len(lines) - height) // 2)
        return [(line, False) for line in lines[start : start + height]]


def parse_lrc(text: str) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for line in text.splitlines():
        match = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if match:
            out.append(
                (
                    int(match.group(1)) * 60 + float(match.group(2)),
                    sanitize_untrusted(match.group(3)).strip(),
                )
            )
    return out


@dataclass
class Renderer:
    state_path: Path = STATE_PATH
    out: IO[str] = sys.stdout
    art: ArtRenderer = field(default_factory=ArtRenderer)
    lyrics: LyricsState = field(default_factory=LyricsState)
    view: str = "now"
    selected: int = 0
    toast: str = ""
    spectrum: SpectrumSource = field(default_factory=SpectrumSource)
    spectrum_auto: bool = False
    async_search: bool = False
    _spectrum_key: Any = None
    _last_size: tuple[int, int] | None = None
    # --- app-like picker state ---
    overlay: str | None = None  # None | "search" | "command" | "help"
    picker: PickerState = field(default_factory=PickerState)
    command_query: str = ""
    command_selected: int = 0
    search_fn: Callable[[str], dict[str, Any]] | None = None
    _pending_delete_idx: int | None = None
    _delete_confirm_time: float = 0.0
    _last_search_fire: float = 0.0
    _last_fired_query: str = ""
    _search_note: str = ""
    _search_seq: int = 0
    _search_lock: threading.Lock = field(default_factory=threading.Lock)
    _pending_search_outcome: tuple[int, str, Any, Exception | None] | None = None
    # --- Chameleon dynamic theme & mouse hitboxes ---
    chameleon: bool = True
    _chameleon_accent: str | None = None
    _chameleon_accent2: str | None = None
    _last_thumb_palette: str | None = None
    _left_w: int = 32
    _tab_hitboxes: list[tuple[int, int, str]] = field(default_factory=list)
    _liked_hitbox: tuple[int, int, int] | None = None
    _queue_hitboxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    _progress_hitbox: tuple[int, int, int, float] | None = None
    _heart_hitbox: tuple[int, int, int] | None = None
    _control_hitboxes: list[tuple[int, int, int, str]] = field(default_factory=list)
    _overlay_rect: tuple[int, int, int, int] | None = None
    _overlay_row_hitboxes: list[tuple[int, int, int, int]] = field(default_factory=list)

    def _accent_hex(self) -> str:
        if self.chameleon and self._chameleon_accent:
            return self._chameleon_accent
        return THEME["accent"]

    def _accent2_hex(self) -> str | None:
        if self.chameleon and self._chameleon_accent2:
            return self._chameleon_accent2
        return None

    def _accent_fg(self) -> str:
        r, g, b = _rgb(self._accent_hex())
        return f"\x1b[38;2;{r};{g};{b}m"

    def _accent_bg(self) -> str:
        r, g, b = _rgb(self._accent_hex())
        return f"\x1b[48;2;{r};{g};{b}m"

    def _update_chameleon(self, thumb_path: Any) -> None:
        if not self.chameleon or not isinstance(thumb_path, str) or not thumb_path:
            if not thumb_path:
                self._chameleon_accent = None
                self._chameleon_accent2 = None
                self._last_thumb_palette = None
            return
        if thumb_path == self._last_thumb_palette:
            return
        self._last_thumb_palette = thumb_path
        pal = extract_art_palette(thumb_path)
        if pal:
            self._chameleon_accent, self._chameleon_accent2 = pal
        else:
            self._chameleon_accent = None
            self._chameleon_accent2 = None

    def _is_empty_state(self, state: dict[str, Any]) -> bool:
        queue = state.get("queue") or []
        playing = state.get("playing")
        title = str(state.get("title") or "")
        idle_titles = {"MYX", "Waiting for player", "State unavailable", "Unknown", ""}
        return (not queue and not playing) or title in idle_titles and not queue

    def _apply_search_response(self, q: str, resp: Any, exc: Exception | None = None) -> None:
        if exc is not None:
            err_msg = sanitize_untrusted(exc) or "search failed"
            self.picker.set_error(err_msg)
            self.toast = err_msg
            return
        if isinstance(resp, dict) and resp.get("ok") is False:
            err_msg = sanitize_untrusted(resp.get("error") or "search failed")
            self.picker.set_error(err_msg)
            self.toast = err_msg
        elif isinstance(resp, dict) and "results" in resp:
            results = resp.get("results") or []
            self.picker.set_results(results if isinstance(results, list) else [])
            if not self.picker.results:
                self.toast = f"No results for '{q}'"
        elif isinstance(resp, list):
            self.picker.set_results(resp)
        else:
            self.picker.set_loading(False)
            if isinstance(resp, dict) and resp.get("ok"):
                queued = resp.get("queued")
                if queued is not None:
                    self.toast = f"queued {queued}"
                    self._search_note = f"queued {queued} — added to queue"
                self.picker.set_results([])

    def _drain_async_search(self) -> None:
        with self._search_lock:
            outcome = self._pending_search_outcome
            self._pending_search_outcome = None
        if outcome is None:
            return
        seq, q, resp, exc = outcome
        if seq != self._search_seq or self.overlay != "search":
            return
        self._apply_search_response(q, resp, exc)

    def _maybe_fire_search(self, now: float | None = None) -> None:
        """Debounced search via daemon (send_command) or injected search_fn."""
        self._drain_async_search()
        if self.overlay != "search":
            return
        q = self.picker.query.strip()
        if not q:
            return
        cur = now if now is not None else time.monotonic()
        if not self.picker.should_search_now(cur):
            return
        if q == self._last_fired_query and self._last_search_fire > 0:
            return
        if cur - self._last_search_fire < 0.25:
            return
        self._last_search_fire = cur
        self._last_fired_query = q
        self._search_note = ""
        self.picker.set_loading(True)
        self._search_seq += 1
        seq = self._search_seq

        if self.async_search and self.search_fn is None:
            def _worker() -> None:
                try:
                    resp = send_command("search_query", arg=q)
                    err: Exception | None = None
                except Exception as exc:
                    resp = None
                    err = exc
                with self._search_lock:
                    if seq == self._search_seq:
                        self._pending_search_outcome = (seq, q, resp, err)

            threading.Thread(target=_worker, daemon=True).start()
            return

        try:
            if self.search_fn is not None:
                resp = self.search_fn(q)
            else:
                resp = send_command("search_query", arg=q)
            self._apply_search_response(q, resp, None)
        except Exception as exc:
            self._apply_search_response(q, None, exc)

    # -- overlay helpers --
    def open_search(self, initial: str = "") -> None:
        self.overlay = "search"
        self.picker.set_query(initial, now=time.monotonic())
        self._last_search_fire = 0.0
        self._last_fired_query = ""
        self._search_note = ""
        self._search_seq += 1
        self.toast = ""

    def open_command_palette(self) -> None:
        self.overlay = "command"
        self.command_query = ""
        self.command_selected = 0
        self.toast = ""

    def open_help(self) -> None:
        self.overlay = "help"
        self.toast = ""

    def close_overlay(self) -> None:
        self.overlay = None
        self.command_query = ""
        self.command_selected = 0

    def ensure_spectrum(self, state: dict[str, Any]) -> None:
        """Lazily bind the best available spectrum source (cached local file first)."""
        if not self.spectrum_auto:
            return
        cached_local = state.get("cached_audio")
        if isinstance(cached_local, str) and FfmpegTapSource._is_local_file(cached_local):
            source_url: str | None = cached_local
        else:
            source_url = str(state.get("url") or "") or None
        is_paused = bool(state.get("paused")) or not bool(state.get("playing"))
        try:
            pos = float(state.get("position") or 0.0)
        except (TypeError, ValueError):
            pos = 0.0

        if isinstance(self.spectrum, CavaSource):
            key: Any = ("cava",)
        else:
            key = ("tap", source_url)
        if key == self._spectrum_key:
            if isinstance(self.spectrum, FfmpegTapSource):
                self.spectrum.sync_playback(paused=is_paused, position=pos)
            return
        if self._spectrum_key is not None:
            try:
                self.spectrum.close()
            except Exception:  # noqa: BLE001 - plugin cleanup must not kill the TUI
                pass
        if make_spectrum is _ORIGINAL_MAKE_SPECTRUM:
            self.spectrum = make_spectrum(source_url, start_pos=pos)
        else:
            self.spectrum = make_spectrum(source_url)
        if isinstance(self.spectrum, FfmpegTapSource) and is_paused:
            self.spectrum.sync_playback(paused=True, position=pos)
        self._spectrum_key = (
            ("cava",) if isinstance(self.spectrum, CavaSource) else ("tap", source_url)
        )

    def render_once(
        self, state: dict[str, Any] | None = None, size: tuple[int, int] | None = None
    ) -> str:
        state = state or read_state(self.state_path)
        self.lyrics.load_from_state(state)
        self._update_chameleon(state.get("thumb"))
        self.ensure_spectrum(state)
        cols, rows = size or shutil.get_terminal_size((100, 32))
        cols = max(10, cols)
        rows = max(6, rows)
        left_w = max(20, min(cols - 12, int(cols * 0.38))) if cols >= 36 else max(10, cols // 2)
        self._left_w = left_w
        right_w = max(1, cols - left_w - 1)
        body_h = max(2, rows - 3)
        canvas = [bg("bg") + fg("fg") + " " * cols + RESET for _ in range(rows)]

        self._tab_hitboxes = []
        self._liked_hitbox = None
        self._queue_hitboxes = []
        self._progress_hitbox = None
        self._heart_hitbox = None
        self._control_hitboxes = []
        self._overlay_rect = None
        self._overlay_row_hitboxes = []
        acc_fg = self._accent_fg()

        def put(y: int, x: int, text: str) -> None:
            if not (0 <= y < rows) or x >= cols:
                return
            x = max(0, x)
            vis = cell_len(text)
            allow = cols - x
            if vis > allow:
                text = clip(text, allow)
                vis = cell_len(text)
            pre = _ansi_between(canvas[y], 0, x) if x > 0 else ""
            post = _ansi_between(canvas[y], x + vis, cols) if x + vis < cols else ""
            canvas[y] = (
                bg("bg")
                + fg("fg")
                + pre
                + bg("bg")
                + fg("fg")
                + text
                + bg("bg")
                + fg("fg")
                + post
                + RESET
            )

        tabs = [("now", "Now Playing"), ("lyrics", "Lyrics"), ("queue", "Queue")]
        tab_parts: list[str] = []
        tab_plain_spans: list[tuple[int, int, str]] = []
        running_tab_w = 0
        for idx, (key, label) in enumerate(tabs):
            active = self.view == key
            sep = " · " if idx else ""
            running_tab_w += cell_len(sep)
            shown = ("•• " + label) if active else label
            span_w = cell_len(shown)
            tab_plain_spans.append((running_tab_w, running_tab_w + span_w, key))
            running_tab_w += span_w
            tab_parts.append(
                sep
                + (acc_fg + shown if active else fg("dim") + shown)
                + RESET
            )
        tabs_str = "".join(tab_parts)
        tabs_x = max(8, cols - cell_len(strip_ansi(tabs_str)) - 2)
        for rel_x0, rel_x1, key in tab_plain_spans:
            self._tab_hitboxes.append((tabs_x + rel_x0, tabs_x + rel_x1, key))
        gap = max(1, tabs_x - 6)
        put(0, 0, " " + acc_fg + BOLD + "M Y X" + RESET + " " * gap + tabs_str)

        # vertical divider
        for yy in range(1, rows - 1):
            put(yy, left_w, fg("dim") + "│" + RESET)
        self._draw_queue(canvas, put, state, left_w, body_h)
        if self.view == "lyrics":
            self._draw_lyrics(put, state, left_w + 1, 1, right_w, body_h - 1)
        elif self.view == "queue":
            self._draw_queue_full(put, state, left_w + 1, 1, right_w, body_h - 1)
        else:
            self._draw_now(put, state, left_w + 1, 1, right_w, body_h - 1)

        sb_skip = state.get("sb_skipped")
        if isinstance(sb_skip, dict) and not self.toast:
            cat = sanitize_untrusted(sb_skip.get("category") or "segment")
            put(
                rows - 2,
                2,
                acc_fg + crop(f"⏭ SponsorBlock skipped {cat}", max(0, cols - 4)) + RESET,
            )
        else:
            if self.toast == AUTO_HINT and (
                not self._is_empty_state(state) or self.overlay == "search"
            ):
                self.toast = ""
            if not self.toast and self._is_empty_state(state) and self.overlay != "search":
                self.toast = AUTO_HINT

            if self.toast:
                safe_toast = sanitize_untrusted(self.toast)
                put(rows - 2, 2, acc_fg + crop(safe_toast, max(0, cols - 4)) + RESET)
            else:
                controls = fg("dim") + CONTROLS_HINT + RESET
                ctrl_x = max(0, cols - cell_len(strip_ansi(controls)) - 2)
                put(rows - 2, ctrl_x, controls)
                # Register clickable transport button hitboxes on row (rows - 2)
                btn_defs = [
                    ("[ ▶ Play ]", "resume"),
                    ("[ ⏸ Pause ]", "pause"),
                    ("[ ⏭ Next ]", "skip"),
                    ("[ 🔀 Shuffle ]", "shuffle"),
                ]
                cx = ctrl_x
                for b_txt, b_act in btn_defs:
                    bw = cell_len(b_txt)
                    self._control_hitboxes.append((rows - 2, cx, cx + bw, b_act))
                    cx += bw + 1

        footer = fg("dim") + FOOTER + RESET
        put(rows - 1, 0, footer)

        # overlays on top
        if self.overlay == "search":
            self._draw_search_picker(put, cols, rows)
        elif self.overlay == "command":
            self._draw_command_palette(put, cols, rows)
        elif self.overlay == "help":
            self._draw_help_overlay(put, cols, rows)

        resized = self._last_size != (cols, rows)
        self._last_size = (cols, rows)
        is_tty = bool(getattr(self.out, "isatty", lambda: False)())
        prefix = (CLEAR if resized else HOME) + HIDE
        if is_tty:
            frame = SYNC_ON + prefix + "\n".join(canvas) + SYNC_OFF
        else:
            frame = prefix + "\n".join(canvas)
        self.out.write(frame)
        self.out.flush()
        return frame

    def _draw_queue(
        self,
        canvas: list[str],
        put: Any,
        state: dict[str, Any],
        width: int,
        height: int,
    ) -> None:
        acc_fg = self._accent_fg()
        acc_bg = self._accent_bg()
        queue = state.get("queue") or []
        current = {
            "title": state.get("title"),
            "artist": state.get("artist"),
            "duration": state.get("duration"),
            "thumb": state.get("thumb"),
        }
        items = [current] + list(queue)
        total = len(items)
        total_sec = int(sum(float(i.get("duration") or 0) for i in items))
        self.selected = max(0, min(self.selected, max(0, total - 1)))
        pos = self.selected + 1 if total else 1
        put(1, 2, fg("dim") + f"< Library  {pos}/{max(total, 1)} · {mmss(total_sec)}" + RESET)
        liked_cnt = int(state.get("liked_count") or 0)
        header = (
            f"▸ Play Liked Songs ♥ ({liked_cnt})"
            if liked_cnt > 0
            else "▸ Play Liked Songs"
        )
        hdr_w = max(1, width - 4)
        put(2, 2, acc_bg + fg("bg") + " " + pad(crop(header, hdr_w), hdr_w) + " " + RESET)
        self._liked_hitbox = (2, 2, 2 + hdr_w + 2)
        y = 3
        avail = max(0, height - 4)
        if not queue and self._is_empty_state(state):
            hint = "♪  Search above to add songs"
            put(
                y + avail // 2,
                2 + max(0, (width - cell_len(hint)) // 2),
                fg("dim") + crop(hint, max(1, width - 3)) + RESET,
            )
            box_hint = fg("dim") + crop("type to search — results appear instantly", max(1, width - 3)) + RESET
            put(
                y + avail // 2 + 1,
                2 + max(0, (width - cell_len(strip_ansi(box_hint))) // 2),
                box_hint,
            )
        if avail <= 0:
            return
        start = 0
        if len(items) > avail:
            start = max(0, min(self.selected - avail // 2, len(items) - avail))
        for i in range(start, min(len(items), start + avail)):
            item = items[i]
            if self._is_empty_state(state) and i == 0 and not queue:
                title_raw = sanitize_untrusted(item.get("title") or "No track")
                if title_raw in ("MYX", "Waiting for player", "State unavailable"):
                    continue
            title = sanitize_untrusted(item.get("title")) or "Unknown"
            artist = sanitize_untrusted(item.get("artist"))
            tw = max(6, width - 14)
            t = crop(title, tw)
            a = crop(artist, max(4, width - tw - 5)) if artist else ""
            if i == self.selected and i > 0:
                line = acc_bg + fg("bg") + pad(t, tw)
                if a:
                    line += " " + pad(a, max(0, width - tw - 5))
                line += RESET
            elif i == 0:
                prefix = "▸ " if i == self.selected else ""
                t0 = crop(prefix + title, tw)
                line = acc_fg + BOLD + pad(t0, tw) + RESET
                if a:
                    line += " " + fg("dim") + a + RESET
            else:
                base = fg("dim") if i < self.selected else fg("fg")
                line = base + pad(t, tw) + RESET
                if a:
                    line += " " + fg("dim") + a + RESET
            put(y, 2, " " + line)
            self._queue_hitboxes.append((y, 2, width - 1, i))
            y += 1

    def _draw_queue_full(
        self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int
    ) -> None:
        acc_fg = self._accent_fg()
        acc_bg = self._accent_bg()
        queue = state.get("queue") or []
        current = {
            "title": state.get("title"),
            "artist": state.get("artist"),
            "duration": state.get("duration"),
            "thumb": state.get("thumb"),
            "id": state.get("id"),
        }
        items = [current] + list(queue)
        self.selected = max(0, min(self.selected, max(0, len(items) - 1)))
        put(y, x, acc_fg + BOLD + f"Queue — {len(items)} tracks" + RESET)
        if not queue and self._is_empty_state(state):
            hint = "♪  Search above to add songs"
            put(
                y + height // 2,
                x + max(0, (width - cell_len(hint)) // 2),
                fg("dim") + hint + RESET,
            )
            return
        avail = max(0, height - 2)
        start = 0
        if len(items) > avail > 0:
            start = max(0, min(self.selected - avail // 2, len(items) - avail))
        for row_idx, idx in enumerate(range(start, min(len(items), start + avail))):
            item = items[idx]
            title = sanitize_untrusted(item.get("title")) or "Unknown"
            artist = sanitize_untrusted(item.get("artist"))
            dur = mmss(item.get("duration"))
            dur_w = len(dur) + 1
            body_w = max(8, width - dur_w)
            artist_w = min(14, max(6, body_w // 4)) if artist else 0
            title_w = max(6, body_w - artist_w - 6)
            num_prefix = f"{idx + 1:>2}. "
            t_part = num_prefix + crop(title, title_w)
            if idx == self.selected:
                row_text = (
                    acc_bg
                    + fg("bg")
                    + pad(t_part, body_w - artist_w - (1 if artist_w else 0))
                    + ((" " + pad(crop(artist, artist_w), artist_w)) if artist_w else "")
                    + f" {dur}"
                    + RESET
                )
            else:
                t_col = acc_fg + BOLD if idx == 0 else fg("fg")
                row_text = (
                    t_col
                    + pad(t_part, body_w - artist_w - (1 if artist_w else 0))
                    + RESET
                    + ((" " + fg("dim") + pad(crop(artist, artist_w), artist_w) + RESET) if artist_w else "")
                    + fg("dim")
                    + f" {dur}"
                    + RESET
                )
            row_y = y + 1 + row_idx
            put(row_y, x, row_text)
            self._queue_hitboxes.append((row_y, x, x + width, idx))

    def _draw_now(
        self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int
    ) -> None:
        acc_fg = self._accent_fg()
        reserved = 12
        art_h = max(6, height - reserved)
        art_cells = min(40, max(12, min(width - 4, art_h * 2)))
        art_rows = max(6, min(art_cells // 2, art_h))
        ax = x + max(0, (width - art_cells) // 2)
        ay = y
        for n, line in enumerate(
            self.art.render(state.get("thumb"), art_cells, art_rows)
        ):
            put(ay + n, ax, line)
        title = sanitize_untrusted(state.get("title")) or "Unknown"
        artist = sanitize_untrusted(state.get("artist"))
        is_liked = bool(state.get("liked"))
        heart_glyph = " ♥" if is_liked else " ♡"
        show_heart = bool(state.get("playing")) and width >= 22
        max_title_w = max(1, width - (4 if show_heart else 2))
        cropped_title = crop(title, max_title_w)
        full_title_vis = cell_len(cropped_title) + (2 if show_heart else 0)
        tx = x + max(0, (width - full_title_vis) // 2)
        title_row = ay + art_rows + 1
        if show_heart:
            heart_col = acc_fg + BOLD if is_liked else fg("dim")
            put(
                title_row,
                tx,
                fg("fg") + BOLD + cropped_title + RESET + heart_col + heart_glyph + RESET,
            )
            self._heart_hitbox = (
                title_row,
                tx + cell_len(cropped_title),
                tx + full_title_vis + 1,
            )
        else:
            put(
                title_row,
                tx,
                fg("fg") + BOLD + cropped_title + RESET,
            )
        if artist:
            put(
                ay + art_rows + 2,
                x + max(0, (width - min(cell_len(artist), max(1, width - 2))) // 2),
                fg("dim") + crop(artist, max(1, width - 2)) + RESET,
            )
        pos = float(state.get("position") or 0)
        dur = float(state.get("duration") or 0)
        bar_w = max(10, width - 16)
        fill = max(0, min(bar_w - 1, int((pos / dur) * bar_w))) if dur > 0 else 0
        chapters = state.get("chapters") or []
        cells_list = ["─"] * bar_w
        if dur > 0 and isinstance(chapters, list) and len(chapters) > 1:
            for ch in chapters:
                if not isinstance(ch, dict):
                    continue
                try:
                    st_t = float(ch.get("start_time") or 0.0)
                except (TypeError, ValueError):
                    continue
                if 0.5 < st_t < dur:
                    tick_idx = max(0, min(bar_w - 1, int((st_t / dur) * bar_w)))
                    cells_list[tick_idx] = "┼"
        cells_list[fill] = "o"
        played_seg = "".join(cells_list[: fill + 1])
        remain_seg = "".join(cells_list[fill + 1 :])
        bar_row = ay + art_rows + 4
        time_prefix = f"{mmss(pos)} "
        row_x = x + max(0, (width - (bar_w + 10)) // 2)
        bar_x0 = row_x + len(time_prefix)
        put(
            bar_row,
            row_x,
            fg("dim")
            + time_prefix
            + acc_fg
            + played_seg
            + fg("dim")
            + remain_seg
            + f" {mmss(dur)}"
            + RESET,
        )
        if dur > 0:
            self._progress_hitbox = (bar_row, bar_x0, bar_w, dur)

        # Status & DSP / Chapter badges row
        status_parts: list[str] = []
        active_chapter = sanitize_untrusted(state.get("chapter"))
        if active_chapter:
            status_parts.append(f"§ {crop(active_chapter, 22)}")
        if state.get("playing"):
            status_parts.append("⏸ paused" if state.get("paused") else "▶ playing")
        vol = state.get("volume")
        if state.get("muted"):
            status_parts.append("muted")
        elif vol is not None:
            try:
                status_parts.append(f"vol {int(float(vol))}%")
            except (TypeError, ValueError):
                pass
        loop_mode = state.get("loop")
        if loop_mode in ("one", "all"):
            status_parts.append(f"repeat:{loop_mode}")
        dsp_info = state.get("dsp")
        if isinstance(dsp_info, dict):
            preset_key = dsp_info.get("preset")
            if preset_key and preset_key != "flat":
                status_parts.append(f"eq:{dsp_info.get('preset_label') or preset_key}")
            if dsp_info.get("normalize"):
                status_parts.append("norm")
            xf = float(dsp_info.get("crossfade_s") or 0.0)
            if xf > 0:
                status_parts.append(f"xfade:{xf:g}s")
        if state.get("cached_audio"):
            status_parts.append("↓ offline")
        if status_parts and ay + art_rows + 3 < y + height:
            badge = " · ".join(status_parts)
            put(
                ay + art_rows + 3,
                x + max(0, (width - min(cell_len(badge), max(1, width - 2))) // 2),
                fg("dim") + crop(badge, max(1, width - 2)) + RESET,
            )

        is_paused = bool(state.get("paused")) or not bool(state.get("playing"))
        try:
            vals, peaks = self.spectrum.values_with_peaks(paused=is_paused)
        except TypeError:
            vals, peaks = self.spectrum.values_with_peaks()
        base = y + height - 1
        avail = base - (ay + art_rows + 4) - 1
        if avail < 3:
            return
        max_h = max(3, min(8, avail, height // 4 if height > 20 else 4))
        start_x = x + max(0, (width - len(vals) * 2) // 2)
        n = len(vals)
        acc_hex = self._accent_hex()
        acc2_hex = self._accent2_hex()
        for row_off in range(max_h + 1):
            r = base - row_off
            if r < y:
                break
            parts: list[str] = []
            for i in range(n):
                h = max(1, int(vals[i] * max_h))
                peak_h = max(1, int(peaks[i] * max_h)) if i < len(peaks) else h
                if row_off < h:
                    frac = (row_off + 1) / max_h * 0.55 + (i / max(1, n)) * 0.45
                    parts.append(_bar_color(frac, acc_hex, acc2_hex) + "█")
                elif row_off == peak_h and peak_h > h:
                    parts.append(acc_fg + "·")
                elif row_off == h and peak_h == h and h < max_h:
                    parts.append(acc_fg + "▀")
                else:
                    parts.append(" ")
                parts.append(" ")
            put(r, start_x, "".join(parts) + RESET)

    def _draw_lyrics(
        self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int
    ) -> None:
        acc_fg = self._accent_fg()
        if self.lyrics.offset != 0.0 and height > 3:
            off_label = f"sync offset: {self.lyrics.offset:+.1f}s ([ / ] to adjust)"
            put(
                y,
                x + max(0, (width - cell_len(off_label)) // 2),
                fg("dim") + crop(off_label, width) + RESET,
            )
            y += 1
            height -= 1
        lines = self.lyrics.visible(float(state.get("position") or 0), height)
        start_y = y + max(0, (height - len(lines)) // 2)
        for n, (line, active) in enumerate(lines):
            safe_line = sanitize_untrusted(line)
            color = acc_fg + BOLD if active else fg("fg")
            put(
                start_y + n,
                x + max(0, (width - cell_len(safe_line)) // 2),
                color + crop(safe_line, width) + RESET,
            )

    # -- picker overlays --
    def _overlay_box(
        self, put: Any, cols: int, rows: int, w: int, h: int
    ) -> tuple[int, int]:
        """Compute centered box origin and draw accent-bordered modal frame."""
        acc_fg = self._accent_fg()
        w = max(8, min(w, cols - 4))
        h = max(5, min(h, rows - 4))
        x = max(0, (cols - w) // 2)
        y = max(0, (rows - h) // 2)
        self._overlay_rect = (x, y, x + w, y + h)
        put(y, x, acc_fg + "┌" + "─" * (w - 2) + "┐" + RESET)
        interior = bg("bg") + fg("fg") + " " * (w - 2)
        for yy in range(1, h - 1):
            put(
                y + yy,
                x,
                acc_fg + "│" + RESET + interior + acc_fg + "│" + RESET,
            )
        put(y + h - 1, x, acc_fg + "└" + "─" * (w - 2) + "┘" + RESET)
        return x, y

    def _draw_search_picker(self, put: Any, cols: int, rows: int) -> None:
        acc_fg = self._accent_fg()
        acc_bg = self._accent_bg()
        w = min(68, max(20, cols - 6))
        h = min(18, max(8, rows - 4))
        x, y = self._overlay_box(put, cols, rows, w, h)
        inner_w = max(6, w - 4)
        put(
            y + 1,
            x + 2,
            acc_fg
            + BOLD
            + "Search"
            + RESET
            + fg("dim")
            + "  ·  ↑↓ select · enter play · tab queue · esc close"
            + RESET,
        )
        query_disp = sanitize_untrusted(self.picker.query) if self.picker.query else ""
        placeholder = "Search for songs, artists..." if not query_disp else ""
        cursor = "▌" if self.picker.query is not None else ""
        field_text = f"Search › {query_disp}{cursor}"
        if placeholder and not query_disp:
            field_text = f"Search › {fg('dim')}{placeholder}{RESET}"
        put(
            y + 2,
            x + 2,
            acc_bg
            + fg("bg")
            + " "
            + pad(
                crop(
                    strip_ansi(field_text) if placeholder else field_text,
                    max(1, inner_w - 2),
                ),
                max(1, inner_w - 2),
            )
            + " "
            + RESET,
        )
        status = ""
        if self.picker.loading:
            status = fg("dim") + "Searching..." + RESET
        elif self.picker.error:
            status = fg("err") + crop(sanitize_untrusted(self.picker.error), inner_w) + RESET
        elif self._search_note:
            status = acc_fg + crop(sanitize_untrusted(self._search_note), inner_w) + RESET
        elif self.picker.query and not self.picker.results:
            status = (
                fg("dim")
                + f"No results for '{crop(query_disp, 20)}' — try another search"
                + RESET
            )
        elif self.picker.results:
            status = (
                fg("dim")
                + f"{len(self.picker.results)} results  ·  ↑↓ navigate  ·  enter play  ·  tab queue"
                + RESET
            )
        else:
            status = (
                fg("dim")
                + "Type to search YouTube / YT Music — results appear instantly"
                + RESET
            )
        put(y + 3, x + 2, status)
        avail = max(0, h - 6)
        start = max(0, self.picker.selected - avail // 2)
        for idx in range(avail):
            ri = start + idx
            if ri >= len(self.picker.results):
                break
            item = self.picker.results[ri]
            title = sanitize_untrusted(item.get("title")) or "Unknown"
            artist = sanitize_untrusted(item.get("artist"))
            dur = format_duration(item.get("duration"))
            thumb_mark = "●"
            title_w = max(10, inner_w - 22)
            title_disp = crop(title, title_w)
            artist_disp = crop(artist, 12) if artist else ""
            base = f"{thumb_mark} {pad(title_disp, title_w)}"
            if artist_disp:
                base = base + " " + fg("dim") + pad(artist_disp, 12) + RESET
            else:
                base = base + " " + " " * 12
            allow = max(4, inner_w - 2 - 6)
            vis = cell_len(base)
            if vis > allow:
                base = crop(strip_ansi(base), allow)
            line = pad(base, allow) + " " + fg("dim") + dur + RESET
            is_sel = ri == self.picker.selected
            row_y = y + 4 + idx
            if is_sel:
                put(
                    row_y,
                    x + 2,
                    acc_bg
                    + fg("bg")
                    + " "
                    + pad(crop(strip_ansi(line), max(1, inner_w - 2)), max(1, inner_w - 2))
                    + " "
                    + RESET,
                )
            else:
                put(row_y, x + 2, " " + line)
            self._overlay_row_hitboxes.append((row_y, x + 2, x + 2 + inner_w, ri))

    def _draw_command_palette(self, put: Any, cols: int, rows: int) -> None:
        acc_fg = self._accent_fg()
        acc_bg = self._accent_bg()
        w = min(62, max(20, cols - 6))
        filtered = command_palette_search(self.command_query)
        h = min(16, 5 + len(filtered) + 2)
        h = max(8, min(h, rows - 4))
        x, y = self._overlay_box(put, cols, rows, w, h)
        inner_w = max(6, w - 4)
        put(
            y + 1,
            x + 2,
            acc_fg
            + BOLD
            + "Command Palette"
            + RESET
            + fg("dim")
            + "  ·  ctrl-k / ?  ·  esc to close"
            + RESET,
        )
        qdisp = sanitize_untrusted(self.command_query)
        placeholder = "Type a command..." if not qdisp else ""
        field_s = f"› {qdisp}▌" if qdisp else f"› {fg('dim')}{placeholder}{RESET}"
        put(
            y + 2,
            x + 2,
            acc_bg
            + fg("bg")
            + " "
            + pad(
                crop(strip_ansi(field_s) if not qdisp else field_s, max(1, inner_w - 2)),
                max(1, inner_w - 2),
            )
            + " "
            + RESET,
        )
        avail = max(0, h - 5)
        self.command_selected = max(
            0, min(self.command_selected, max(0, len(filtered) - 1))
        )
        start = max(0, self.command_selected - avail // 2)
        for idx in range(avail):
            fi = start + idx
            if fi >= len(filtered):
                break
            e = filtered[fi]
            label = e.get("label", "")
            keys = e.get("keys", "")
            line = f"{crop(label, max(6, inner_w - 14))}"
            line = pad(line, max(6, inner_w - 12)) + fg("dim") + crop(keys, 10) + RESET
            is_sel = fi == self.command_selected
            row_y = y + 3 + idx
            if is_sel:
                put(
                    row_y,
                    x + 2,
                    acc_bg
                    + fg("bg")
                    + " "
                    + pad(crop(strip_ansi(line), max(1, inner_w - 2)), max(1, inner_w - 2))
                    + " "
                    + RESET,
                )
            else:
                put(row_y, x + 2, " " + line)
            self._overlay_row_hitboxes.append((row_y, x + 2, x + 2 + inner_w, fi))

    def _draw_help_overlay(self, put: Any, cols: int, rows: int) -> None:
        acc_fg = self._accent_fg()
        w = min(70, max(24, cols - 6))
        h = min(24, max(10, rows - 4))
        x, y = self._overlay_box(put, cols, rows, w, h)
        inner_w = max(8, w - 4)
        put(
            y + 1,
            x + 2,
            acc_fg
            + BOLD
            + "Help  ·  MYX Tour"
            + RESET
            + fg("dim")
            + "  ·  ? / esc to close"
            + RESET,
        )
        line_y = y + 2
        for step in TOUR_STEPS:
            if line_y >= y + h - 2:
                break
            title = step.get("title", "")
            desc = step.get("description", "")
            put(line_y, x + 2, acc_fg + crop(title, inner_w) + RESET)
            line_y += 1
            words = desc.split()
            cur = ""
            for wrd in words:
                if cell_len(cur + " " + wrd) > inner_w:
                    put(line_y, x + 2, fg("dim") + cur + RESET)
                    line_y += 1
                    cur = wrd
                else:
                    cur = (cur + " " + wrd).strip()
            if cur and line_y < y + h - 1:
                put(line_y, x + 2, fg("dim") + cur + RESET)
                line_y += 1
            line_y += 1
        if line_y < y + h - 2:
            put(line_y, x + 2, fg("dim") + "─" * inner_w + RESET)
            line_y += 1
        for kb in KEYBINDS[: max(0, y + h - 1 - line_y)]:
            keys = kb.get("keys", "")
            desc = kb.get("desc", "")
            put(
                line_y,
                x + 2,
                fg("fg")
                + pad(crop(keys, 18), 18)
                + fg("dim")
                + crop(desc, max(1, inner_w - 20))
                + RESET,
            )
            line_y += 1

    # -- key handling --
    def _handle_search_key(self, key: str) -> bool:
        if key in ("\x1b", "\x1b\x1b", "\x03"):
            self.close_overlay()
            return True
        if key in ("\x7f", "\x08", "\b"):
            self.picker.backspace(now=time.monotonic())
            self._last_search_fire = 0.0
            self._last_fired_query = ""
            self._search_note = ""
            self._search_seq += 1
            return True
        if key == "\x15":  # ctrl-u clear
            self.picker.set_query("", now=time.monotonic())
            self._last_search_fire = 0.0
            self._last_fired_query = ""
            self._search_note = ""
            self._search_seq += 1
            return True
        # Only use non-printable arrow / ctrl keys for result navigation so
        # letters like 'j' and 'k' (e.g. 'weeknd', 'drake', 'joji') type normally!
        if key in ("\x1b[A", "\x10"):  # Up arrow or Ctrl-P
            self.picker.move(-1)
            return True
        if key in ("\x1b[B", "\x0e"):  # Down arrow or Ctrl-N
            self.picker.move(1)
            return True
        if key == "\t":  # Tab enqueues selected track without interrupting current song
            res = self.picker.selected_result()
            if res and res.get("id"):
                try:
                    send_command("load", arg=str(res["id"]))
                    self.toast = f"Queued {sanitize_untrusted(res.get('title')) or res['id']}"
                except Exception as exc:
                    self.toast = sanitize_untrusted(exc)
            return True
        if key in ("\r", "\n"):
            res = self.picker.selected_result()
            if res and res.get("id"):
                try:
                    self._send_play_id(str(res["id"]))
                    self.toast = f"Playing {sanitize_untrusted(res.get('title')) or res['id']}"
                    self.close_overlay()
                except Exception as exc:
                    self.toast = sanitize_untrusted(exc)
            else:
                q = self.picker.query.strip()
                if q and self._search_note:
                    self.toast = self._search_note
                    self.close_overlay()
                elif q:
                    try:
                        fn = self.search_fn or (
                            lambda qq: send_command("search", arg=qq)
                        )
                        resp = fn(q)
                        if isinstance(resp, dict) and resp.get("ok") is False:
                            self.toast = sanitize_untrusted(resp.get("error") or "search failed")
                        else:
                            queued = (
                                resp.get("queued") if isinstance(resp, dict) else None
                            )
                            self.toast = (
                                f"queued {queued}"
                                if queued is not None
                                else f"queued: {q}"
                            )
                            self.close_overlay()
                    except Exception as exc:
                        self.toast = sanitize_untrusted(exc)
            return True
        if len(key) == 1 and key.isprintable():
            self.picker.append_char(key, now=time.monotonic())
            self._last_search_fire = 0.0
            self._last_fired_query = ""
            self._search_note = ""
            self._search_seq += 1
            return True
        return True

    def _send_play_id(self, vid: str) -> None:
        send_command("play_id", arg=vid)

    def _handle_command_key(self, key: str) -> bool:
        if key in ("\x1b", "\x1b\x1b", "\x03"):
            self.close_overlay()
            return True
        if key in ("\x7f", "\x08", "\b"):
            if self.command_query:
                self.command_query = self.command_query[:-1]
                self.command_selected = 0
            return True
        if key == "\x15":
            self.command_query = ""
            self.command_selected = 0
            return True
        if key in ("\x1b[A", "\x10"):  # Up arrow or Ctrl-P
            self.command_selected = max(0, self.command_selected - 1)
            return True
        if key in ("\x1b[B", "\x0e"):  # Down arrow or Ctrl-N
            filtered = command_palette_search(self.command_query)
            self.command_selected = min(
                max(0, len(filtered) - 1), self.command_selected + 1
            )
            return True
        if key in ("\r", "\n"):
            filtered = command_palette_search(self.command_query)
            if 0 <= self.command_selected < len(filtered):
                entry = filtered[self.command_selected]
                self.close_overlay()
                self._execute_command(entry)
            else:
                self.close_overlay()
            return True
        if len(key) == 1 and key.isprintable():
            self.command_query += key
            self.command_selected = 0
            return True
        return True

    def _execute_command(self, entry: dict[str, str]) -> None:
        act = entry.get("action") or entry.get("id")
        try:
            if act == "toggle":
                send_command("toggle")
            elif act == "pause":
                send_command("pause")
            elif act == "skip":
                send_command("skip")
            elif act == "prev":
                send_command("prev")
            elif act == "seek_fwd":
                send_command("seek", arg=5)
            elif act == "seek_back":
                send_command("seek", arg=-5)
            elif act == "like":
                resp = send_command("like")
                if isinstance(resp, dict) and resp.get("ok"):
                    self.toast = "Liked ♥" if resp.get("liked") else "Removed from Liked Songs"
                elif isinstance(resp, dict) and resp.get("error"):
                    self.toast = sanitize_untrusted(resp["error"])
            elif act == "play_liked":
                resp = send_command("play_liked")
                if isinstance(resp, dict) and resp.get("ok"):
                    self.toast = f"Playing {resp.get('loaded', 0)} Liked Songs ♥"
                elif isinstance(resp, dict) and resp.get("error"):
                    self.toast = sanitize_untrusted(resp["error"])
            elif act == "cache_track":
                resp = send_command("cache_track")
                if isinstance(resp, dict) and resp.get("ok"):
                    self.toast = "Caching track for offline playback..."
                elif isinstance(resp, dict) and resp.get("error"):
                    self.toast = sanitize_untrusted(resp["error"])
            elif act == "playlist_save":
                resp = send_command("playlist_save", name="Favorites")
                if isinstance(resp, dict) and resp.get("ok"):
                    self.toast = f"Saved {resp.get('count', 0)} tracks to 'Favorites'"
            elif act == "eq_cycle":
                resp = send_command("eq", arg="cycle")
                if isinstance(resp, dict) and isinstance(resp.get("dsp"), dict):
                    self.toast = f"EQ: {resp['dsp'].get('preset_label')}"
            elif act in ("eq_bass", "eq_vocal", "eq_spatial", "eq_slowed", "eq_nightcore", "eq_flat"):
                preset_map = {
                    "eq_bass": "bass_boost",
                    "eq_vocal": "vocal",
                    "eq_spatial": "spatial",
                    "eq_slowed": "slowed_reverb",
                    "eq_nightcore": "nightcore",
                    "eq_flat": "flat",
                }
                resp = send_command("eq", arg=preset_map[act])
                if isinstance(resp, dict) and isinstance(resp.get("dsp"), dict):
                    self.toast = f"EQ: {resp['dsp'].get('preset_label')}"
            elif act == "normalize":
                resp = send_command("normalize")
                if isinstance(resp, dict):
                    on = resp.get("normalize")
                    self.toast = f"Loudness Normalization: {'ON' if on else 'OFF'}"
            elif act == "crossfade":
                st = read_state(self.state_path)
                cur_xf = float((st.get("dsp") or {}).get("crossfade_s") or 0.0)
                nxt_xf = 0.0 if cur_xf > 0 else 4.0
                resp = send_command("crossfade", arg=nxt_xf)
                if isinstance(resp, dict):
                    self.toast = f"Smart Crossfade: {nxt_xf:g}s"
            elif act == "sponsorblock":
                resp = send_command("sponsorblock")
                if isinstance(resp, dict):
                    self.toast = f"SponsorBlock: {'ON' if resp.get('sponsorblock') else 'OFF'}"
            elif act in ("next_chapter", "prev_chapter"):
                resp = send_command(act)
                if isinstance(resp, dict) and resp.get("ok"):
                    self.toast = f"Chapter: {sanitize_untrusted(resp.get('chapter') or '')}"
                elif isinstance(resp, dict) and resp.get("error"):
                    self.toast = sanitize_untrusted(resp["error"])
            elif act == "chameleon":
                self.chameleon = not self.chameleon
                self.toast = f"Chameleon Dynamic Theme: {'ON' if self.chameleon else 'OFF'}"
            elif act == "shuffle":
                send_command("shuffle")
            elif act == "loop":
                resp = send_command("loop")
                if isinstance(resp, dict) and resp.get("loop"):
                    self.toast = f"Repeat: {resp['loop']}"
            elif act == "mute":
                send_command("mute")
                self.toast = "Toggled mute"
            elif act == "clear":
                send_command("clear")
                self.toast = "Cleared queue"
            elif act == "vol_up":
                send_command("vol", value=5)
            elif act == "vol_down":
                send_command("vol", value=-5)
            elif act == "lyrics":
                self.view = "lyrics"
            elif act == "queue":
                self.view = "queue"
            elif act == "search":
                self.open_search()
            elif act == "help":
                self.open_help()
            elif act == "doctor":
                self.toast = "run: myx doctor"
            elif act == "quit":
                try:
                    send_command("quit")
                except Exception:
                    pass
            else:
                send_command(act or "toggle")
        except Exception as exc:
            self.toast = sanitize_untrusted(exc)

    def handle_mouse(self, btn: int, col1: int, row1: int, pressed: bool) -> bool:
        """Process SGR 1006 mouse clicks & scroll wheel events (1-based coords)."""
        x = max(0, col1 - 1)
        y = max(0, row1 - 1)

        # Mouse wheel: 64 = wheel up, 65 = wheel down
        if btn in (64, 65):
            delta = -1 if btn == 64 else 1
            if self.overlay == "search":
                self.picker.move(delta)
            elif self.overlay == "command":
                filtered = command_palette_search(self.command_query)
                self.command_selected = max(
                    0, min(max(0, len(filtered) - 1), self.command_selected + delta)
                )
            elif self.view == "lyrics" and x > self._left_w:
                self.lyrics.offset = round(self.lyrics.offset - delta * 0.5, 2)
                try:
                    send_command("lyrics_offset", arg=self.lyrics.offset)
                except Exception:
                    pass
            else:
                self.selected = max(0, self.selected + delta)
            return True

        # Only trigger actions on left-button press (btn == 0 and pressed == True)
        if btn != 0 or not pressed:
            return True

        if self.overlay is not None:
            if self._overlay_rect is not None:
                ox0, oy0, ox1, oy1 = self._overlay_rect
                if not (ox0 <= x < ox1 and oy0 <= y < oy1):
                    self.close_overlay()
                    return True
            for ry, rx0, rx1, r_idx in self._overlay_row_hitboxes:
                if y == ry and rx0 <= x <= rx1:
                    if self.overlay == "search" and 0 <= r_idx < len(self.picker.results):
                        self.picker.selected = r_idx
                        res = self.picker.selected_result()
                        if res and res.get("id"):
                            try:
                                self._send_play_id(str(res["id"]))
                                self.toast = f"Playing {sanitize_untrusted(res.get('title')) or res['id']}"
                                self.close_overlay()
                            except Exception as exc:
                                self.toast = sanitize_untrusted(exc)
                    elif self.overlay == "command":
                        filtered = command_palette_search(self.command_query)
                        if 0 <= r_idx < len(filtered):
                            entry = filtered[r_idx]
                            self.close_overlay()
                            self._execute_command(entry)
                    return True
            return True

        try:
            # 1. Top view tabs (row 0)
            if y == 0:
                for tx0, tx1, view_key in self._tab_hitboxes:
                    if tx0 <= x <= tx1:
                        self.view = view_key
                        return True

            # 2. Progress bar scrub click
            if self._progress_hitbox is not None:
                py, px0, pw, dur = self._progress_hitbox
                if y == py and px0 <= x < px0 + pw and dur > 0:
                    ratio = max(0.0, min(1.0, (x - px0) / max(1, pw - 1)))
                    target_s = round(ratio * dur, 2)
                    send_command("jump", arg=target_s)
                    self.toast = f"Seeked to {mmss(target_s)}"
                    return True

            # 3. Now Playing ♥ Heart toggle
            if self._heart_hitbox is not None:
                hy, hx0, hx1 = self._heart_hitbox
                if y == hy and hx0 <= x <= hx1:
                    resp = send_command("like")
                    if isinstance(resp, dict) and resp.get("ok"):
                        self.toast = "Liked ♥" if resp.get("liked") else "Removed from Liked Songs"
                    return True

            # 4. Sidebar "▸ Play Liked Songs" button
            if self._liked_hitbox is not None:
                ly, lx0, lx1 = self._liked_hitbox
                if y == ly and lx0 <= x <= lx1:
                    resp = send_command("play_liked")
                    if isinstance(resp, dict) and resp.get("ok"):
                        self.toast = f"Playing {resp.get('loaded', 0)} Liked Songs ♥"
                    elif isinstance(resp, dict) and resp.get("error"):
                        self.toast = sanitize_untrusted(resp["error"])
                    return True

            # 5. Transport footer buttons
            for cy, cx0, cx1, c_act in self._control_hitboxes:
                if y == cy and cx0 <= x <= cx1:
                    send_command(c_act)
                    return True

            # 6. Queue item rows (click to select; click selected item to play)
            for qy, qx0, qx1, q_idx in self._queue_hitboxes:
                if y == qy and qx0 <= x <= qx1:
                    if self.selected == q_idx and q_idx > 0:
                        send_command("play_index", arg=q_idx - 1)
                    else:
                        self.selected = q_idx
                    return True
        except Exception as exc:
            self.toast = sanitize_untrusted(exc)
        return True

    def handle_key(self, key: str) -> bool:
        m_mouse = _SGR_MOUSE_RE.match(key)
        if m_mouse:
            return self.handle_mouse(
                int(m_mouse.group(1)),
                int(m_mouse.group(2)),
                int(m_mouse.group(3)),
                m_mouse.group(4) == "M",
            )

        if self.overlay == "search":
            return self._handle_search_key(key)
        if self.overlay == "command":
            return self._handle_command_key(key)
        if self.overlay == "help":
            if key in ("\x1b", "q", "?", "\x03", "\x1b\x1b"):
                self.close_overlay()
                return True
            return True

        if key == "/":
            self.open_search()
            return True
        if key == "\x0b":  # ctrl-k
            self.open_command_palette()
            return True
        if key == "?":
            self.open_help()
            return True
        if key == "\x0c":  # ctrl-l likes/unlikes current track
            self._execute_command({"action": "like"})
            return True
        if key == "\x05":  # ctrl-e cycles audiophile EQ/DSP presets
            self._execute_command({"action": "eq_cycle"})
            return True
        if key == "\x02":  # ctrl-b toggles SponsorBlock auto-skip
            self._execute_command({"action": "sponsorblock"})
            return True
        if key == "\x14":  # ctrl-t toggles Chameleon dynamic album-art theme
            self._execute_command({"action": "chameleon"})
            return True
        if key == "\x12":  # ctrl-r cycles repeat mode without hijacking letter 'r'
            try:
                resp = send_command("loop")
                if isinstance(resp, dict) and resp.get("loop"):
                    self.toast = f"Repeat: {resp['loop']}"
            except Exception as exc:
                self.toast = sanitize_untrusted(exc)
            return True
        if key == "\x07":  # ctrl-g toggles mute without hijacking letter 'm'
            try:
                send_command("mute")
                self.toast = "Toggled mute"
            except Exception as exc:
                self.toast = sanitize_untrusted(exc)
            return True

        if len(key) == 1 and key.isprintable() and key not in ("\t", "\r", "\n"):
            known_cmds = {
                " ",
                "q",
                "n",
                "p",
                "s",
                "l",
                "d",
                "+",
                "-",
                "=",
                "[",
                "]",
                "{",
                "}",
                "j",
                "k",
            }
            if key not in known_cmds:
                self.open_search(initial=key)
                return True

        try:
            if key == "q":
                send_command("quit")
                return False
            if key == " ":
                send_command("toggle")
            elif key == "n":
                send_command("skip")
            elif key == "p":
                send_command("prev")
            elif key == "\x1b[C":  # Right arrow: seek +5s
                send_command("seek", arg=5)
            elif key == "\x1b[D":  # Left arrow: seek -5s
                send_command("seek", arg=-5)
            elif key == "+":
                send_command("vol", value=5)
            elif key == "-":
                send_command("vol", value=-5)
            elif key == "=":
                send_command("vol", value=5)
            elif key == "s":
                send_command("shuffle")
            elif key in ("l", "\t"):
                order = ["now", "lyrics", "queue"]
                idx = order.index(self.view) if self.view in order else 0
                self.view = order[(idx + 1) % len(order)]
            elif key == "[":
                self.lyrics.offset = round(self.lyrics.offset - 0.5, 2)
                try:
                    send_command("lyrics_offset", arg=self.lyrics.offset)
                except Exception:
                    pass
            elif key == "]":
                self.lyrics.offset = round(self.lyrics.offset + 0.5, 2)
                try:
                    send_command("lyrics_offset", arg=self.lyrics.offset)
                except Exception:
                    pass
            elif key == "{":
                self._execute_command({"action": "prev_chapter"})
            elif key == "}":
                self._execute_command({"action": "next_chapter"})
            elif key in ("j", "\x1b[B"):
                self.selected += 1
            elif key in ("k", "\x1b[A"):
                self.selected = max(0, self.selected - 1)
            elif key == "\x1b[6~":  # PageDown
                self.selected += 10
            elif key == "\x1b[5~":  # PageUp
                self.selected = max(0, self.selected - 10)
            elif key in ("\x1b[H", "\x1bOH"):  # Home
                self.selected = 0
            elif key in ("\x1b[F", "\x1bOF"):  # End
                self.selected += 10_000
            elif key in ("\r", "\n"):
                if self.selected > 0:
                    send_command("play_index", arg=self.selected - 1)
                else:
                    send_command("skip")
            elif key in ("d", "\x1b[3~"):  # 'd' or Delete key
                if self.selected > 0:
                    now = time.monotonic()
                    idx = self.selected - 1
                    if (
                        self._pending_delete_idx == idx
                        and (now - self._delete_confirm_time) < 2.0
                    ):
                        send_command("del", arg=idx)
                        self.selected = max(0, self.selected - 1)
                        self._pending_delete_idx = None
                        self.toast = "Deleted"
                    else:
                        self._pending_delete_idx = idx
                        self._delete_confirm_time = now
                        self.toast = "Press d again to confirm delete"
                else:
                    self.toast = "Nothing to delete"
        except Exception as exc:
            self.toast = sanitize_untrusted(exc)
        return True

    def search_overlay(self) -> None:
        self.toast = "search unavailable in non-interactive renderer"


def _read_escape_sequence(stream: IO[str], wait_s: float = 0.03) -> str:
    """Read a complete ANSI/SGR escape sequence so trailing CSI bytes never leak."""
    sel = selectors.DefaultSelector()
    try:
        sel.register(stream, selectors.EVENT_READ)
        if not sel.select(wait_s):
            return "\x1b"
        c1 = stream.read(1)
        if not c1:
            return "\x1b"
        if c1 == "[":
            buf = ["\x1b", "["]
            for _ in range(32):
                if not sel.select(0.01):
                    break
                ch = stream.read(1)
                if not ch:
                    break
                buf.append(ch)
                if 0x40 <= ord(ch) <= 0x7E:
                    break
            return "".join(buf)
        if c1 == "O":
            if sel.select(0.01):
                c2 = stream.read(1)
                return "\x1bO" + c2
            return "\x1bO"
        return "\x1b" + c1
    finally:
        sel.close()


def run(state_path: Path = STATE_PATH, out: IO[str] = sys.stdout) -> int:
    load_theme()
    renderer = Renderer(state_path=state_path, out=out)
    renderer.spectrum_auto = True
    renderer.async_search = True
    old = None
    interactive = out.isatty() and sys.stdin.isatty()
    if interactive:
        old = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        out.write(ALT_ON + HIDE + MOUSE_ON)
        out.flush()
    sel = selectors.DefaultSelector()
    if interactive:
        sel.register(sys.stdin, selectors.EVENT_READ)
    try:
        running = True
        while running:
            state = read_state(state_path)
            if renderer.overlay == "search":
                renderer._maybe_fire_search()
            renderer.render_once(state)
            deadline = time.time() + 1 / 12
            while interactive and time.time() < deadline:
                events = sel.select(max(0.0, deadline - time.time()))
                if events:
                    key = sys.stdin.read(1)
                    if key == "\x1b":
                        key = _read_escape_sequence(sys.stdin)
                    running = renderer.handle_key(key)
                    if not running:
                        break
            if not interactive:
                break
            time.sleep(max(0, deadline - time.time()))
    finally:
        sel.close()
        renderer.spectrum.close()
        if interactive and old:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
            out.write(MOUSE_OFF + SHOW + ALT_OFF)
            out.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
