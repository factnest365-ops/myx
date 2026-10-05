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
import socket
import subprocess
import sys
import termios
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
HIDE = "\x1b[?25l"
SHOW = "\x1b[?25h"
ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"


# CSI (cursor/color), OSC (iterm2 image; BEL or ST terminated), APC/DCS (kitty image).
ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b_[^\x1b]*\x1b\\"
    r"|\x1b[PX^][^\x1b]*\x1b\\"
)


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


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
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = [p.strip() for p in line.split("=", 1)]
        value = value.strip("\"'")
        if key in THEME and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            THEME[key] = value


def read_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
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
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.5)
        sock.connect(str(CMD_SOCK))
        sock.sendall((json.dumps(message) + "\n").encode())
        data = sock.recv(4096)
    return json.loads(data.decode() or '{"ok": true}')


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

    def values_with_peaks(self) -> tuple[list[float], list[float]]:
        vals = self.values()
        # exponential decay of peaks
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
        if shutil.which("cava"):
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
        if self.proc:
            self.proc.terminate()


@dataclass
class FfmpegTapSource(SpectrumSource):
    stream_url: str | None = None
    proc: subprocess.Popen[bytes] | None = None
    _buf: bytes = b""
    _last_vals: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.stream_url and shutil.which("ffmpeg") and np is not None:
            self.proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-loglevel",
                    "quiet",
                    "-re",
                    "-i",
                    self.stream_url,
                    "-f",
                    "s16le",
                    "-ac",
                    "1",
                    "-ar",
                    "8000",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        if self.proc and self.proc.stdout:
            try:
                os.set_blocking(self.proc.stdout.fileno(), False)
            except (OSError, ValueError, AttributeError):
                pass

    def values(self) -> list[float]:
        if not self.proc or not self.proc.stdout or np is None:
            return super().values()
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
        n = min(len(self._buf), 8192)
        n -= n % 2
        raw = self._buf[-n:]
        self._buf = b""
        if len(raw) < 512:
            return list(self._last_vals) if self._last_vals else super().values()
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        fft = np.abs(np.fft.rfft(samples))[:64]
        if float(fft.max(initial=0)) <= 0:
            return super().values()
        bins = np.array_split(np.log1p(fft), self.bars)
        vals = [float(b.mean()) for b in bins]
        m = max(vals) or 1.0
        vals = [min(1.0, v / m) for v in vals]
        self._last_vals = vals
        return vals

    def close(self) -> None:
        if self.proc:
            self.proc.terminate()


def _bar_color(frac: float) -> str:
    """Accent gradient from dim -> accent -> bright by bar height/position."""
    ar, ag, ab = _rgb(THEME["accent"])
    dr, dg, db = _rgb(THEME["dim"])
    # interpolate dim->accent then boost
    t = max(0.0, min(1.0, frac))
    r = int(dr + (ar - dr) * t + (255 - ar) * t * 0.15)
    g = int(dg + (ag - dg) * t + (255 - ag) * t * 0.15)
    b = int(db + (ab - db) * t + (255 - ab) * t * 0.15)
    r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
    return f"\x1b[38;2;{r};{g};{b}m"


def make_spectrum(stream_url: str | None = None) -> SpectrumSource:
    if shutil.which("cava"):
        try:
            return CavaSource()
        except Exception:
            pass
    if stream_url and shutil.which("ffmpeg") and np is not None:
        try:
            return FfmpegTapSource(stream_url=stream_url)
        except Exception:
            pass
    return SpectrumSource()


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
        if env.get("KITTY_WINDOW_ID") or "kitty" in term:
            return "kitty"
        if "iterm" in program:
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

    def _kitty(self, path: str, cells: int, rows: int) -> list[str]:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        return [f"\x1b_Gf=100,a=T,s={cells},v={rows};{data}\x1b\\"] + [""] * max(
            0, rows - 1
        )

    def _iterm2(self, path: str, cells: int, rows: int) -> list[str]:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        return [
            f"\x1b]1337;File=inline=1;width={cells};height={rows};preserveAspectRatio=1:{data}\a"
        ] + [""] * max(0, rows - 1)

    def _half_block(self, path: str, cells: int, rows: int) -> list[str]:
        px_w = max(2, cells)
        px_h = max(2, rows * 2)
        try:
            raw = subprocess.check_output(
                [
                    "ffmpeg",
                    "-loglevel",
                    "quiet",
                    "-i",
                    path,
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

    def load_from_state(self, state: dict[str, Any]) -> None:
        sid = str(state.get("id") or "")
        if sid == self.loaded_id:
            return
        self.loaded_id = sid
        self.lrc = parse_lrc(state.get("lrc") or "")
        plain = state.get("lyrics") or state.get("plain") or ""
        self.plain = [line.strip() for line in str(plain).splitlines() if line.strip()]

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
                    match.group(3).strip(),
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
    _spectrum_key: Any = None
    # --- app-like picker state ---
    overlay: str | None = None  # None | "search" | "command" | "help"
    picker: PickerState = field(default_factory=PickerState)
    command_query: str = ""
    command_selected: int = 0
    search_fn: Callable[[str], dict[str, Any]] | None = None
    _pending_delete_idx: int | None = None
    _delete_confirm_time: float = 0.0
    _last_search_fire: float = 0.0
    _search_note: str = ""

    def _is_empty_state(self, state: dict[str, Any]) -> bool:
        queue = state.get("queue") or []
        playing = state.get("playing")
        title = str(state.get("title") or "")
        idle_titles = {"MYX", "Waiting for player", "State unavailable", "Unknown", ""}
        return (not queue and not playing) or title in idle_titles and not queue

    def _maybe_fire_search(self, now: float | None = None) -> None:
        """Debounced search via daemon (send_command) or injected search_fn."""
        if self.overlay != "search":
            return
        if not self.picker.query.strip():
            return
        cur = now if now is not None else time.monotonic()
        # debounce 250ms
        if not self.picker.should_search_now(cur):
            return
        # avoid re-firing same query
        if cur - self._last_search_fire < 0.25:
            return
        self._last_search_fire = cur
        self._search_note = ""
        q = self.picker.query.strip()
        self.picker.set_loading(True)
        try:
            if self.search_fn is not None:
                resp = self.search_fn(q)
            else:
                resp = send_command("search", arg=q)
            if isinstance(resp, dict) and resp.get("ok") is False:
                self.picker.set_error(str(resp.get("error") or "search failed"))
                # also surface as toast for visibility
                self.toast = str(resp.get("error") or "search failed")
            elif isinstance(resp, dict) and "results" in resp:
                # injected search may return {"results": [...]}
                results = resp.get("results") or []
                self.picker.set_results(results if isinstance(results, list) else [])
                if not self.picker.results:
                    self.toast = f"No results for '{q}'"
            elif isinstance(resp, list):
                self.picker.set_results(resp)
            else:
                # daemon search returns {"ok": True, "queued": N} — don't populate picker results,
                # just toast and clear loading
                self.picker.set_loading(False)
                if isinstance(resp, dict) and resp.get("ok"):
                    queued = resp.get("queued")
                    if queued is not None:
                        self.toast = f"queued {queued}"
                        self._search_note = f"queued {queued} — added to queue"
                    self.picker.set_results([])
        except Exception as exc:
            self.picker.set_error(str(exc))
            self.toast = str(exc)

    # -- overlay helpers --
    def open_search(self, initial: str = "") -> None:
        self.overlay = "search"
        self.picker.set_query(initial, now=time.monotonic())
        self._last_search_fire = 0.0
        self._search_note = ""
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
        """Lazily bind the best available spectrum source (once per stream url)."""
        if not self.spectrum_auto:
            return
        url = str(state.get("url") or "") or None
        if isinstance(self.spectrum, CavaSource):
            key: Any = ("cava",)
        else:
            key = ("tap", url)
        if key == self._spectrum_key:
            return
        if self._spectrum_key is not None:
            try:
                self.spectrum.close()
            except Exception:  # noqa: BLE001 - plugin cleanup must not kill the TUI
                pass
        self.spectrum = make_spectrum(url)
        self._spectrum_key = (
            ("cava",) if isinstance(self.spectrum, CavaSource) else ("tap", url)
        )

    def render_once(
        self, state: dict[str, Any] | None = None, size: tuple[int, int] | None = None
    ) -> str:
        state = state or read_state(self.state_path)
        self.lyrics.load_from_state(state)
        self.ensure_spectrum(state)
        cols, rows = size or shutil.get_terminal_size((100, 32))
        left_w = max(24, int(cols * 0.38))
        right_w = cols - left_w - 1
        body_h = rows - 3
        canvas = [bg("bg") + fg("fg") + " " * cols + RESET for _ in range(rows)]

        def put(y: int, x: int, text: str) -> None:
            if not (0 <= y < rows) or x >= cols:
                return
            x = max(0, x)
            vis = cell_len(text)
            allow = cols - x
            if vis > allow:
                text = clip(text, allow)
                vis = cell_len(text)
            raw = strip_ansi(canvas[y])
            pre = _plain_between(raw, 0, x)
            post = _plain_between(raw, x + vis) if x + vis < cols else ""
            canvas[y] = bg("bg") + fg("fg") + pre + text + post + RESET

        tabs = [("now", "Now Playing"), ("lyrics", "Lyrics"), ("queue", "Queue")]
        tab_parts: list[str] = []
        for idx, (key, label) in enumerate(tabs):
            active = self.view == key or (self.view == "now" and key == "now")
            if key == "queue" and self.view == "queue":
                active = True
            sep = " · " if idx else ""
            tab_parts.append(
                sep
                + (fg("accent") + "•• " + label if active else fg("dim") + label)
                + RESET
            )
            if idx == 0 and active:
                pass
        tabs_str = "".join(tab_parts)
        # one put for the whole header: a second put would strip the first one's styles
        tabs_x = max(8, cols - cell_len(strip_ansi(tabs_str)) - 2)
        gap = max(1, tabs_x - 6)
        put(0, 0, " " + fg("accent") + BOLD + "M Y X" + RESET + " " * gap + tabs_str)
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

        # resilience toast: idle state shows updating hint; drop it once recovered
        if self.toast == AUTO_HINT and (
            not self._is_empty_state(state) or self.overlay == "search"
        ):
            self.toast = ""
        if not self.toast and self._is_empty_state(state) and self.overlay != "search":
            self.toast = AUTO_HINT

        if self.toast:
            # toast wins row rows-2: controls hint would collide on narrow terminals
            put(rows - 2, 2, fg("accent") + crop(self.toast, max(0, cols - 4)) + RESET)
        else:
            controls = fg("dim") + CONTROLS_HINT + RESET
            put(rows - 2, max(0, cols - cell_len(strip_ansi(controls)) - 2), controls)

        footer = fg("dim") + FOOTER + RESET
        # spec: always full footer visible — write at 0, not centered-clip
        put(rows - 1, 0, footer)

        # overlays on top
        if self.overlay == "search":
            self._draw_search_picker(put, cols, rows)
        elif self.overlay == "command":
            self._draw_command_palette(put, cols, rows)
        elif self.overlay == "help":
            self._draw_help_overlay(put, cols, rows)

        frame = CLEAR + HIDE + "\n".join(canvas)
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
        queue = state.get("queue") or []
        current = {
            "title": state.get("title"),
            "artist": state.get("artist"),
            "duration": state.get("duration"),
            "thumb": state.get("thumb"),
        }
        items = [current] + list(queue)
        total = len(items)
        pos = 1
        total_sec = int(sum(float(i.get("duration") or 0) for i in items))
        self.selected = max(0, min(self.selected, max(0, total - 1)))
        put(1, 2, fg("dim") + f"< Library  {pos}/{max(total, 1)} · {total_sec}" + RESET)
        header = "▸ Play Liked Songs"
        put(2, 2, bg("accent") + fg("bg") + " " + pad(header, width - 4) + " " + RESET)
        y = 3
        avail = max(0, height - 4)
        # empty queue illustration centered
        if not queue and self._is_empty_state(state):
            # show centered illustration hint inside left panel below header
            hint = "♪  Search above to add songs"
            put(
                y + avail // 2,
                2 + max(0, (width - len(hint)) // 2),
                fg("dim") + hint + RESET,
            )
            box_hint = fg("dim") + "type to search — results appear instantly" + RESET
            put(
                y + avail // 2 + 1,
                2 + max(0, (width - cell_len(strip_ansi(box_hint))) // 2),
                box_hint,
            )
        for i, item in enumerate(items[:avail]):
            # skip drawing placeholder "MYX/Unknown" when empty state — hint already shown
            if self._is_empty_state(state) and i == 0 and not queue:
                # draw dim placeholder row for current but subtle
                title = str(item.get("title") or "No track")
                if title in ("MYX", "Waiting for player", "State unavailable"):
                    continue
            title = str(item.get("title") or "Unknown")
            artist = str(item.get("artist") or "")
            tw = max(8, width - 14)
            t = crop(title, tw)
            a = crop(artist, 10) if artist else ""
            if i == 0:
                line = fg("accent") + pad(t, tw) + RESET
                if a:
                    line += " " + fg("dim") + a + RESET
            elif i == self.selected:
                line = bg("accent") + fg("bg") + pad(t, tw) + RESET
                if a:
                    line += " " + fg("bg") + a + RESET
            else:
                base = fg("dim") if i < self.selected else fg("fg")
                line = base + pad(t, tw) + RESET
                if a:
                    line += " " + fg("dim") + a + RESET
            put(y, 2, " " + line)
            y += 1

    def _draw_queue_full(
        self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int
    ) -> None:
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
        put(y, x, fg("accent") + BOLD + f"Queue — {len(items)} tracks" + RESET)
        if not queue and self._is_empty_state(state):
            hint = "♪  Search above to add songs"
            put(
                y + height // 2,
                x + max(0, (width - len(hint)) // 2),
                fg("dim") + hint + RESET,
            )
            return
        for idx, item in enumerate(items[: height - 2]):
            title = str(item.get("title") or "Unknown")
            artist = str(item.get("artist") or "")
            dur = mmss(item.get("duration"))
            line = f"{idx + 1:>2}. {crop(title, max(10, width - 22))}"
            if artist:
                line = pad(line, width - 8) + fg("dim") + crop(artist, 10) + RESET
            color = (
                bg("accent") + fg("bg")
                if idx == self.selected
                else (fg("accent") if idx == 0 else fg("fg"))
            )
            put(
                y + 1 + idx,
                x,
                color + pad(crop(line, width), width) + fg("dim") + f" {dur}" + RESET,
            )

    def _draw_now(
        self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int
    ) -> None:
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
        title = str(state.get("title") or "Unknown")
        artist = str(state.get("artist") or "")
        put(
            ay + art_rows + 1,
            x + max(0, (width - min(cell_len(title), width - 2)) // 2),
            fg("fg") + BOLD + crop(title, width - 2) + RESET,
        )
        if artist:
            put(
                ay + art_rows + 2,
                x + max(0, (width - min(cell_len(artist), width - 2)) // 2),
                fg("dim") + crop(artist, width - 2) + RESET,
            )
        pos = float(state.get("position") or 0)
        dur = float(state.get("duration") or 0)
        bar_w = max(10, width - 16)
        fill = int((pos / dur) * bar_w) if dur else 0
        bar = "─" * max(0, fill) + "o" + "─" * max(0, bar_w - fill - 1)
        put(
            ay + art_rows + 4,
            x + max(0, (width - (bar_w + 10)) // 2),
            fg("dim")
            + f"{mmss(pos)} "
            + fg("accent")
            + bar
            + fg("dim")
            + f" {mmss(dur)}"
            + RESET,
        )
        vals, peaks = self.spectrum.values_with_peaks()
        base = y + height - 1
        # spectrum lives strictly below the progress bar row
        avail = base - (ay + art_rows + 4) - 1
        if avail < 3:
            return
        max_h = max(3, min(8, avail, height // 4 if height > 20 else 4))
        start_x = x + max(0, (width - len(vals) * 2) // 2)
        n = len(vals)
        # one put per row: many puts on the same row would strip each other's styles
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
                    parts.append(_bar_color(frac) + "█")
                elif row_off == peak_h and peak_h > h:
                    parts.append(fg("accent") + "·")
                elif row_off == h and peak_h == h and h < max_h:
                    parts.append(fg("accent") + "▀")
                else:
                    parts.append(" ")
                parts.append(" ")
            put(r, start_x, "".join(parts) + RESET)

    def _draw_lyrics(
        self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int
    ) -> None:
        lines = self.lyrics.visible(float(state.get("position") or 0), height)
        start_y = y + max(0, (height - len(lines)) // 2)
        for n, (line, active) in enumerate(lines):
            color = fg("accent") + BOLD if active else fg("fg")
            put(
                start_y + n,
                x + max(0, (width - cell_len(line)) // 2),
                color + crop(line, width) + RESET,
            )

    # -- picker overlays --
    def _overlay_box(
        self, put: Any, cols: int, rows: int, w: int, h: int
    ) -> tuple[int, int]:
        """Compute centered box origin; caller draws border/content."""
        w = min(w, cols - 4)
        h = min(h, rows - 4)
        x = (cols - w) // 2
        y = (rows - h) // 2
        # border
        put(y, x, fg("accent") + "┌" + "─" * (w - 2) + "┐" + RESET)
        for yy in range(1, h - 1):
            put(y + yy, x, fg("accent") + "│" + RESET)
            put(y + yy, x + w - 1, fg("accent") + "│" + RESET)
            # fill interior with bg
            put(y + yy, x + 1, " " * (w - 2))
        put(y + h - 1, x, fg("accent") + "└" + "─" * (w - 2) + "┘" + RESET)
        return x, y

    def _draw_search_picker(self, put: Any, cols: int, rows: int) -> None:
        w = min(64, cols - 6)
        h = min(18, rows - 4)
        x, y = self._overlay_box(put, cols, rows, w, h)
        inner_w = w - 4
        # title
        put(
            y + 1,
            x + 2,
            fg("accent")
            + BOLD
            + "Search"
            + RESET
            + fg("dim")
            + "  ·  type to filter, enter to play, esc to close"
            + RESET,
        )
        # input field
        query_disp = self.picker.query if self.picker.query else ""
        placeholder = "Search for songs, artists..." if not query_disp else ""
        cursor = "▌" if self.picker.query is not None else ""
        field_text = f"Search › {query_disp}{cursor}"
        if placeholder and not query_disp:
            field_text = f"Search › {fg('dim')}{placeholder}{RESET}"
        put(
            y + 2,
            x + 2,
            bg("accent")
            + fg("bg")
            + " "
            + pad(
                crop(
                    strip_ansi(field_text) if placeholder else field_text, inner_w - 2
                ),
                inner_w - 2,
            )
            + " "
            + RESET,
        )
        # status line
        status = ""
        if self.picker.loading:
            status = fg("dim") + "Searching..." + RESET
        elif self.picker.error:
            status = fg("err") + crop(self.picker.error, inner_w) + RESET
        elif self._search_note:
            status = fg("accent") + crop(self._search_note, inner_w) + RESET
        elif self.picker.query and not self.picker.results:
            status = (
                fg("dim")
                + f"No results for '{crop(self.picker.query, 20)}' — try another search"
                + RESET
            )
        elif self.picker.results:
            status = (
                fg("dim")
                + f"{len(self.picker.results)} results  ·  ↑↓ navigate  ·  enter play"
                + RESET
            )
        else:
            status = (
                fg("dim")
                + "Type to search YouTube / YT Music — results appear instantly"
                + RESET
            )
        put(y + 3, x + 2, status)
        # results list
        avail = h - 6
        start = max(0, self.picker.selected - avail // 2)
        for idx in range(avail):
            ri = start + idx
            if ri >= len(self.picker.results):
                break
            item = self.picker.results[ri]
            title = str(item.get("title") or "Unknown")
            artist = str(item.get("artist") or "")
            dur = format_duration(item.get("duration"))
            thumb_mark = "●"
            title_w = max(10, inner_w - 20)
            title_disp = crop(title, title_w)
            artist_disp = crop(artist, 10) if artist else ""
            # fixed columns: thumb + title + artist + dur
            base = f"{thumb_mark} {pad(title_disp, title_w)}"
            if artist_disp:
                base = base + " " + fg("dim") + pad(artist_disp, 10) + RESET
            else:
                base = base + " " + " " * 10
            # ensure dur fits; truncate base if needed
            allow = inner_w - 2 - 6  # leave room for dur + spaces
            # base may contain ansi, so use cell_len for trimming; keep simple: crop ansi-stripped then rebuild? Instead just pad base's visible length
            vis = cell_len(base)
            if vis > allow:
                base = crop(strip_ansi(base), allow)
            line = pad(base, allow) + " " + fg("dim") + dur + RESET
            is_sel = ri == self.picker.selected
            if is_sel:
                put(
                    y + 4 + idx,
                    x + 2,
                    bg("accent")
                    + fg("bg")
                    + " "
                    + pad(crop(strip_ansi(line), inner_w - 2), inner_w - 2)
                    + " "
                    + RESET,
                )
            else:
                put(y + 4 + idx, x + 2, " " + line)

    def _draw_command_palette(self, put: Any, cols: int, rows: int) -> None:
        w = min(60, cols - 6)
        filtered = command_palette_search(self.command_query)
        h = min(16, 5 + len(filtered) + 2)
        h = max(8, min(h, rows - 4))
        x, y = self._overlay_box(put, cols, rows, w, h)
        inner_w = w - 4
        put(
            y + 1,
            x + 2,
            fg("accent")
            + BOLD
            + "Command Palette"
            + RESET
            + fg("dim")
            + "  ·  ctrl-k / ?  ·  esc to close"
            + RESET,
        )
        qdisp = self.command_query or ""
        placeholder = "Type a command..." if not qdisp else ""
        field = f"› {qdisp}▌" if qdisp else f"› {fg('dim')}{placeholder}{RESET}"
        put(
            y + 2,
            x + 2,
            bg("accent")
            + fg("bg")
            + " "
            + pad(
                crop(strip_ansi(field) if not qdisp else field, inner_w - 2),
                inner_w - 2,
            )
            + " "
            + RESET,
        )
        avail = h - 5
        # clamp selection
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
            line = f"{crop(label, inner_w - 14)}"
            line = pad(line, inner_w - 12) + fg("dim") + crop(keys, 10) + RESET
            is_sel = fi == self.command_selected
            if is_sel:
                put(
                    y + 3 + idx,
                    x + 2,
                    bg("accent")
                    + fg("bg")
                    + " "
                    + pad(crop(strip_ansi(line), inner_w - 2), inner_w - 2)
                    + " "
                    + RESET,
                )
            else:
                put(y + 3 + idx, x + 2, " " + line)

    def _draw_help_overlay(self, put: Any, cols: int, rows: int) -> None:
        w = min(68, cols - 6)
        h = min(22, rows - 4)
        x, y = self._overlay_box(put, cols, rows, w, h)
        inner_w = w - 4
        put(
            y + 1,
            x + 2,
            fg("accent")
            + BOLD
            + "Help  ·  MYX Tour"
            + RESET
            + fg("dim")
            + "  ·  ? / esc to close"
            + RESET,
        )
        # tour steps
        line_y = y + 2
        for step in TOUR_STEPS:
            if line_y >= y + h - 2:
                break
            title = step.get("title", "")
            desc = step.get("description", "")
            put(line_y, x + 2, fg("accent") + crop(title, inner_w) + RESET)
            line_y += 1
            # wrap desc roughly
            words = desc.split()
            cur = ""
            for wrd in words:
                if cell_len(cur + " " + wrd) > inner_w:
                    put(line_y, x + 2, fg("dim") + cur + RESET)
                    line_y += 1
                    cur = wrd
                else:
                    cur = (cur + " " + wrd).strip()
            if cur:
                put(line_y, x + 2, fg("dim") + cur + RESET)
                line_y += 1
            line_y += 1
        # keybinds footer inside overlay
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
                + crop(desc, inner_w - 20)
                + RESET,
            )
            line_y += 1

    # -- key handling --
    def _handle_search_key(self, key: str) -> bool:
        if key in ("\x1b", "\x1b\x1b"):
            self.close_overlay()
            return True
        # also handle bare esc sequence leftover
        if key == "\x1b":
            self.close_overlay()
            return True
        if key in ("\x7f", "\x08", "\b"):
            self.picker.backspace(now=time.monotonic())
            self._last_search_fire = 0.0
            self._search_note = ""
            return True
        if key == "\x15":  # ctrl-u clear
            self.picker.set_query("", now=time.monotonic())
            self._last_search_fire = 0.0
            self._search_note = ""
            return True
        if key in ("\x1b[A", "k"):
            self.picker.move(-1)
            return True
        if key in ("\x1b[B", "j"):
            self.picker.move(1)
            return True
        if key in ("\r", "\n"):
            res = self.picker.selected_result()
            if res and res.get("id"):
                try:
                    self._send_play_id(str(res["id"]))
                    self.toast = f"Playing {res.get('title') or res['id']}"
                    self.close_overlay()
                except Exception as exc:
                    self.toast = str(exc)
            else:
                # no selection but query present — trigger search via daemon queue
                q = self.picker.query.strip()
                if q and self._search_note:
                    # this query already went through; Enter must not double-queue
                    self.toast = self._search_note
                    self.close_overlay()
                elif q:
                    try:
                        fn = self.search_fn or (
                            lambda qq: send_command("search", arg=qq)
                        )
                        resp = fn(q)
                        if isinstance(resp, dict) and resp.get("ok") is False:
                            self.toast = str(resp.get("error") or "search failed")
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
                        self.toast = str(exc)
            return True
        if len(key) == 1 and key.isprintable():
            self.picker.append_char(key, now=time.monotonic())
            self._last_search_fire = 0.0
            self._search_note = ""
            # lazy attempt will fire on next render tick via _maybe_fire_search from run loop;
            # also show loading hint
            return True
        return True

    def _send_play_id(self, vid: str) -> None:
        if self.search_fn is not None:
            # allow test injection to capture play_id via search_fn sentinel — but prefer send_command
            # if search_fn is a mock expecting search queries, don't misuse it; try send_command first
            try:
                send_command("play_id", arg=vid)
                return
            except Exception:
                pass
        send_command("play_id", arg=vid)

    def _handle_command_key(self, key: str) -> bool:
        if key in ("\x1b", "\x1b\x1b"):
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
        if key in ("\x1b[A", "k"):
            self.command_selected = max(0, self.command_selected - 1)
            return True
        if key in ("\x1b[B", "j"):
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
            elif act == "shuffle":
                send_command("shuffle")
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
                # generic: send act as cmd
                send_command(act or "toggle")
        except Exception as exc:
            self.toast = str(exc)

    def handle_key(self, key: str) -> bool:
        # overlay handling first
        if self.overlay == "search":
            return self._handle_search_key(key)
        if self.overlay == "command":
            return self._handle_command_key(key)
        if self.overlay == "help":
            if key in ("\x1b", "q", "?", "\x03", "\x1b\x1b"):
                self.close_overlay()
                return True
            if key in ("j", "\x1b[B"):
                # allow scrolling if needed (no-op for now)
                return True
            if key in ("k", "\x1b[A"):
                return True
            return True

        # global overlay triggers when not in overlay
        if key in ("/",):
            self.open_search()
            return True
        if key == "\x0b":  # ctrl-k
            self.open_command_palette()
            return True
        if key == "?":
            self.open_help()
            return True

        # auto-enter search on printable char (app-like: no need to press /)
        if len(key) == 1 and key.isprintable() and key not in ("\t", "\r", "\n"):
            # Never hijack single-char command keys (q,n,p,s,l,d etc.) even in empty state;
            # they have explicit UX and tests expect them to work. Only non-command
            # printable chars auto-open search. In empty state this still gives
            # "type to search" front-door for letters/digits/symbols.
            known_cmds = {
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
                "j",
                "k",
            }
            if key in known_cmds:
                pass
            else:
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
            elif key == "+":
                send_command("vol", value=5)
            elif key == "-":
                send_command("vol", value=-5)
            elif key == "=":
                send_command("vol", value=5)
            elif key == "s":
                send_command("shuffle")
            elif key in ("l", "\t"):
                # cycle now -> lyrics -> queue -> now
                order = ["now", "lyrics", "queue"]
                idx = order.index(self.view) if self.view in order else 0
                self.view = order[(idx + 1) % len(order)]
            elif key == "[":
                self.lyrics.offset -= 0.5
            elif key == "]":
                self.lyrics.offset += 0.5
            elif key in ("j", "\x1b[B"):  # down arrow sends CSI B
                self.selected += 1
            elif key in ("k", "\x1b[A"):
                self.selected = max(0, self.selected - 1)
            elif key in ("\r", "\n"):
                # enter: play selected (0 is current, skip; else play_index)
                if self.selected > 0:
                    send_command("play_index", arg=self.selected - 1)
                else:
                    send_command("skip")
            elif key == "d":
                if self.selected > 0:
                    # double-press to confirm within 2s
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
                        # need queue title for toast — best effort without extra state read
                        self._pending_delete_idx = idx
                        self._delete_confirm_time = now
                        self.toast = "Press d again to confirm delete"
                    # clear confirm after timeout handled implicitly on next d
                else:
                    self.toast = "Nothing to delete"
        except Exception as exc:
            self.toast = str(exc)
        return True

    def search_overlay(self) -> None:
        self.toast = "search unavailable in non-interactive renderer"


def run(state_path: Path = STATE_PATH, out: IO[str] = sys.stdout) -> int:
    load_theme()
    renderer = Renderer(state_path=state_path, out=out)
    renderer.spectrum_auto = True
    old = None
    interactive = out.isatty() and sys.stdin.isatty()
    if interactive:
        old = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        out.write(ALT_ON + HIDE)
    sel = selectors.DefaultSelector()
    if interactive:
        sel.register(sys.stdin, selectors.EVENT_READ)
    try:
        running = True
        while running:
            state = read_state(state_path)
            # debounce fire before render so picker results show promptly
            if renderer.overlay == "search":
                renderer._maybe_fire_search()
            renderer.render_once(state)
            deadline = time.time() + 1 / 12
            while interactive and time.time() < deadline:
                events = sel.select(deadline - time.time())
                if events:
                    key = sys.stdin.read(1)
                    # handle escape sequences (arrows) as composite
                    if key == "\x1b":
                        sel2 = selectors.DefaultSelector()
                        sel2.register(sys.stdin, selectors.EVENT_READ)
                        if sel2.select(0.05):
                            seq = sys.stdin.read(2)
                            key = key + seq
                        sel2.close()
                    # delegate all keys to renderer (includes / , ?, ctrl-k, typing)
                    running = renderer.handle_key(key)
                    if not running:
                        break
            if not interactive:
                break
            time.sleep(max(0, deadline - time.time()))
    finally:
        renderer.spectrum.close()
        if interactive and old:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
            out.write(SHOW + ALT_OFF)
            out.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
