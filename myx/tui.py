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
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import IO, Any

try:  # numpy is allowed for the spectrum tap.
    import numpy as np
except Exception:  # pragma: no cover - exercised only without project deps
    np = None

STATE_PATH = Path(os.environ.get("MYX_STATE", Path(gettempdir()) / "myx-state.json"))
CMD_SOCK = Path(os.environ.get("MYX_CMD_SOCK", Path(gettempdir()) / "myx-cmd.sock"))
FOOTER = "space pause · n next · p prev · / search · s shuffle · +,- vol · l lyrics · tab view · j,k nav · enter play · d del · q quit"

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


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)


def cell_len(s: str) -> int:
    return len(strip_ansi(s))


def crop(s: str, width: int) -> str:
    plain = strip_ansi(s)
    return plain[: max(0, width)]


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
        value = value.strip('"\'')
        if key in THEME and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            THEME[key] = value


def read_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "playing": False, "paused": True, "title": "MYX", "artist": "Waiting for player", "queue": []}
    except Exception:
        return {"version": 1, "playing": False, "paused": True, "title": "State unavailable", "artist": "", "queue": []}


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

    def __post_init__(self) -> None:
        if shutil.which("cava"):
            self.proc = subprocess.Popen(["cava", "-p", "/dev/stdin"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if self.proc.stdin:
                self.proc.stdin.write(b"[output]\nmethod = raw\nraw_target = /dev/stdout\ndata_format = ascii\nascii_max_range = 100\n")
                self.proc.stdin.close()

    def values(self) -> list[float]:
        if not self.proc or not self.proc.stdout:
            return super().values()
        line = self.proc.stdout.readline().decode(errors="ignore").strip()
        nums = [int(x) / 100 for x in re.findall(r"\d+", line)]
        return (nums[: self.bars] + [0.04] * self.bars)[: self.bars]

    def close(self) -> None:
        if self.proc:
            self.proc.terminate()


@dataclass
class FfmpegTapSource(SpectrumSource):
    stream_url: str | None = None
    proc: subprocess.Popen[bytes] | None = None

    def __post_init__(self) -> None:
        if self.stream_url and shutil.which("ffmpeg") and np is not None:
            self.proc = subprocess.Popen(
                ["ffmpeg", "-loglevel", "quiet", "-re", "-i", self.stream_url, "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

    def values(self) -> list[float]:
        if not self.proc or not self.proc.stdout or np is None:
            return super().values()
        raw = self.proc.stdout.read(2048)
        if len(raw) < 512:
            return super().values()
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        fft = np.abs(np.fft.rfft(samples))[:64]
        if float(fft.max(initial=0)) <= 0:
            return super().values()
        bins = np.array_split(np.log1p(fft), self.bars)
        vals = [float(b.mean()) for b in bins]
        m = max(vals) or 1.0
        return [min(1.0, v / m) for v in vals]

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
    last_path: str | None = None
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
        if path == self.last_path and self.last_lines:
            return self.last_lines
        self.protocol = self.detect_protocol()
        if self.protocol == "kitty":
            self.last_lines = self._kitty(path, cells, rows)
        elif self.protocol == "iterm2":
            self.last_lines = self._iterm2(path, cells, rows)
        else:
            self.last_lines = self._half_block(path, cells, rows)
        self.last_path = path
        return self.last_lines

    def _kitty(self, path: str, cells: int, rows: int) -> list[str]:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        return [f"\x1b_Gf=100,a=T,s={cells},v={rows};{data}\x1b\\"] + [""] * max(0, rows - 1)

    def _iterm2(self, path: str, cells: int, rows: int) -> list[str]:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        return [f"\x1b]1337;File=inline=1;width={cells};height={rows};preserveAspectRatio=1:{data}\a"] + [""] * max(0, rows - 1)

    def _half_block(self, path: str, cells: int, rows: int) -> list[str]:
        px_w = max(2, cells)
        px_h = max(2, rows * 2)
        try:
            raw = subprocess.check_output(
                ["ffmpeg", "-loglevel", "quiet", "-i", path, "-vf", f"scale={px_w}:{px_h}", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                timeout=4,
            )
        except Exception:
            return self._placeholder(cells, rows)
        lines: list[str] = []
        row_bytes = px_w * 4
        if len(raw) < px_h * row_bytes:
            return self._placeholder(cells, rows)
        for y in range(0, px_h, 2):
            parts = []
            top_row = y * row_bytes
            bot_row = (y + 1) * row_bytes
            for x in range(px_w):
                top = top_row + x * 4
                bot = bot_row + x * 4
                if bot + 2 >= len(raw):
                    break
                tr, tg, tb = raw[top], raw[top + 1], raw[top + 2]
                br, bgc, bb = raw[bot], raw[bot + 1], raw[bot + 2]
                parts.append(f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bgc};{bb}m▀")
            lines.append("".join(parts) + RESET)
        return lines

    def _placeholder(self, cells: int, rows: int) -> list[str]:
        return [fg("dim") + "┌" + "─" * max(0, cells - 2) + "┐" + RESET] + [fg("dim") + "│" + " " * max(0, cells - 2) + "│" + RESET for _ in range(max(0, rows - 2))] + [fg("dim") + "└" + "─" * max(0, cells - 2) + "┘" + RESET]


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
            return [(line, start + n == idx) for n, (_, line) in enumerate(self.lrc[start : start + height])]
        lines = self.plain or ["No lyrics found"]
        start = max(0, (len(lines) - height) // 2)
        return [(line, False) for line in lines[start : start + height]]


def parse_lrc(text: str) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for line in (text or "").splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        if not stamps:
            continue
        lyric = re.sub(r"(?:\[\d+:\d+(?:\.\d+)?\])+", "", line).strip()
        for minute, second in stamps:
            try:
                out.append((int(minute) * 60 + float(second), lyric))
            except ValueError:
                continue
    out.sort(key=lambda item: item[0])
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

    def render_once(self, state: dict[str, Any] | None = None, size: tuple[int, int] | None = None) -> str:
        state = state or read_state(self.state_path)
        self.lyrics.load_from_state(state)
        cols, rows = size or shutil.get_terminal_size((100, 32))
        cols = max(60, cols)
        rows = max(20, rows)
        left_w = max(24, int(cols * 0.38))
        right_w = cols - left_w - 1
        body_h = rows - 3
        canvas = [bg("bg") + fg("fg") + " " * cols + RESET for _ in range(rows)]

        def put(y: int, x: int, text: str) -> None:
            if not (0 <= y < rows) or x >= cols:
                return
            vis = cell_len(text)
            allow = cols - x
            if vis > allow:
                # footer/toast rows need full overflow so the keybind strip stays intact
                if y >= rows - 2:
                    raw = strip_ansi(canvas[y])
                    pre = raw[:x]
                    canvas[y] = bg("bg") + fg("fg") + pre + text + RESET
                    return
                text = strip_ansi(text)[: max(0, allow)]
                vis = cell_len(text)
            raw = strip_ansi(canvas[y])
            pre = raw[:x]
            post = raw[x + vis :] if x + vis < cols else ""
            canvas[y] = bg("bg") + fg("fg") + pre + text + post + RESET

        put(0, 1, fg("accent") + BOLD + "M Y X" + RESET)
        tabs = [("now", "Now Playing"), ("lyrics", "Lyrics"), ("queue", "Queue")]
        tab_parts: list[str] = []
        for idx, (key, label) in enumerate(tabs):
            active = self.view == key or (self.view == "now" and key == "now")
            if key == "queue" and self.view == "queue":
                active = True
            sep = " · " if idx else ""
            tab_parts.append(sep + (fg("accent") + "•• " + label if active else fg("dim") + label) + RESET)
            if idx == 0 and active:
                pass
        tabs_str = "".join(tab_parts)
        put(0, cols - cell_len(strip_ansi(tabs_str)) - 2, tabs_str)
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
        footer = fg("dim") + FOOTER + RESET
        # spec: always full footer visible — write at 0, not centered-clip
        put(rows - 1, 0, footer)
        if self.toast:
            put(rows - 2, 2, fg("accent") + self.toast + RESET)
        frame = CLEAR + HIDE + "\n".join(canvas)
        self.out.write(frame)
        self.out.flush()
        return frame

    def _draw_queue(self, canvas: list[str], put: Any, state: dict[str, Any], width: int, height: int) -> None:
        queue = state.get("queue") or []
        current = {"title": state.get("title"), "artist": state.get("artist"), "duration": state.get("duration"), "thumb": state.get("thumb")}
        items = [current] + list(queue)
        total = len(items)
        pos = 1
        total_sec = int(sum(float(i.get("duration") or 0) for i in items))
        self.selected = max(0, min(self.selected, max(0, total - 1)))
        put(1, 2, fg("dim") + f"< Library  {pos}/{max(total,1)} · {total_sec}" + RESET)
        header = "▸ Play Liked Songs"
        put(2, 2, bg("accent") + fg("bg") + " " + pad(header, width - 4) + " " + RESET)
        y = 3
        avail = max(0, height - 4)
        for i, item in enumerate(items[:avail]):
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

    def _draw_queue_full(self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int) -> None:
        queue = state.get("queue") or []
        current = {"title": state.get("title"), "artist": state.get("artist"), "duration": state.get("duration"), "thumb": state.get("thumb"), "id": state.get("id")}
        items = [current] + list(queue)
        self.selected = max(0, min(self.selected, max(0, len(items) - 1)))
        put(y, x, fg("accent") + BOLD + f"Queue — {len(items)} tracks" + RESET)
        for idx, item in enumerate(items[:height - 2]):
            title = str(item.get("title") or "Unknown")
            artist = str(item.get("artist") or "")
            dur = mmss(item.get("duration"))
            line = f"{idx+1:>2}. {crop(title, max(10, width - 22))}"
            if artist:
                line = pad(line, width - 8) + fg("dim") + crop(artist, 10) + RESET
            color = bg("accent") + fg("bg") if idx == self.selected else (fg("accent") if idx == 0 else fg("fg"))
            put(y + 1 + idx, x, color + pad(crop(line, width), width) + fg("dim") + f" {dur}" + RESET)

    def _draw_now(self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int) -> None:
        reserved = 12
        art_h = max(6, height - reserved)
        art_cells = min(40, max(12, min(width - 4, art_h * 2)))
        art_rows = max(6, min(art_cells // 2, art_h))
        ax = x + max(0, (width - art_cells) // 2)
        ay = y
        for n, line in enumerate(self.art.render(state.get("thumb"), art_cells, art_rows)):
            put(ay + n, ax, line)
        title = str(state.get("title") or "Unknown")
        artist = str(state.get("artist") or "")
        put(ay + art_rows + 1, x + max(0, (width - min(len(title), width - 2)) // 2), fg("fg") + BOLD + crop(title, width - 2) + RESET)
        if artist:
            put(ay + art_rows + 2, x + max(0, (width - min(len(artist), width - 2)) // 2), fg("dim") + crop(artist, width - 2) + RESET)
        pos = float(state.get("position") or 0)
        dur = float(state.get("duration") or 0)
        bar_w = max(10, width - 16)
        fill = int((pos / dur) * bar_w) if dur else 0
        bar = "─" * max(0, fill) + "o" + "─" * max(0, bar_w - fill - 1)
        put(ay + art_rows + 4, x + max(0, (width - (bar_w + 10)) // 2), fg("dim") + f"{mmss(pos)} " + fg("accent") + bar + fg("dim") + f" {mmss(dur)}" + RESET)
        vals, peaks = self.spectrum.values_with_peaks()
        base = y + height - 1
        max_h = max(3, min(8, height // 4 if height > 20 else 4))
        start_x = x + max(0, (width - len(vals) * 2) // 2)
        for i, v in enumerate(vals):
            h = max(1, int(v * max_h))
            peak_h = max(1, int(peaks[i] * max_h)) if i < len(peaks) else h
            for j in range(h):
                frac = (j + 1) / max_h * 0.55 + (i / max(1, len(vals))) * 0.45
                put(base - j, start_x + i * 2, _bar_color(frac) + "█" + RESET)
            if peak_h > h:
                put(base - peak_h, start_x + i * 2, fg("accent") + "·" + RESET)
            elif peak_h == h and h < max_h:
                put(base - peak_h, start_x + i * 2, fg("accent") + "▀" + RESET)

    def _draw_lyrics(self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int) -> None:
        lines = self.lyrics.visible(float(state.get("position") or 0), height)
        start_y = y + max(0, (height - len(lines)) // 2)
        for n, (line, active) in enumerate(lines):
            color = fg("accent") + BOLD if active else fg("fg")
            put(start_y + n, x + max(0, (width - len(line)) // 2), color + crop(line, width) + RESET)

    def handle_key(self, key: str) -> bool:
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
                    send_command("del", arg=self.selected - 1)
                    self.selected = max(0, self.selected - 1)
        except Exception as exc:
            self.toast = str(exc)
        return True

    def search_overlay(self) -> None:
        self.toast = "search unavailable in non-interactive renderer"


def run(state_path: Path = STATE_PATH, out: IO[str] = sys.stdout) -> int:
    load_theme()
    renderer = Renderer(state_path=state_path, out=out)
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
            renderer.render_once(state)
            deadline = time.time() + 1 / 12
            while interactive and time.time() < deadline:
                events = sel.select(deadline - time.time())
                if events:
                    key = sys.stdin.read(1)
                    # handle escape sequences (arrows) as composite
                    if key == "\x1b":
                        # peek briefly for CSI
                        sel2 = selectors.DefaultSelector()
                        sel2.register(sys.stdin, selectors.EVENT_READ)
                        if sel2.select(0.05):
                            seq = sys.stdin.read(2)
                            key = key + seq
                        sel2.close()
                    if key == "/":
                        out.write("\x1b[2K\rsearch: ")
                        out.flush()
                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
                        query = sys.stdin.readline().strip()
                        tty.setcbreak(sys.stdin.fileno())
                        if query:
                            try:
                                resp = send_command("search", arg=query)
                                if resp.get("ok"):
                                    queued = resp.get("queued")
                                    renderer.toast = f"queued {queued}" if queued is not None else f"queued: {query}"
                                else:
                                    renderer.toast = resp.get("error") or "search failed"
                            except Exception as exc:
                                renderer.toast = str(exc)
                        else:
                            renderer.toast = ""
                    else:
                        running = renderer.handle_key(key)
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
