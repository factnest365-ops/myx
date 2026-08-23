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

    def values(self) -> list[float]:
        return [0.04] * self.bars

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
    for line in text.splitlines():
        match = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if match:
            out.append((int(match.group(1)) * 60 + float(match.group(2)), match.group(3).strip()))
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
            plain_width = cols - x
            text = text[: plain_width + 80]
            raw = strip_ansi(canvas[y])
            canvas[y] = bg("bg") + raw[:x] + text + RESET + raw[x + min(cell_len(text), plain_width) :]

        put(0, 1, fg("accent") + BOLD + "M Y X" + RESET)
        tabs = "•• Now Playing · Lyrics · Queue"
        put(0, cols - len(tabs) - 2, fg("accent") + tabs[:2] + fg("fg") + tabs[2:] + RESET)
        self._draw_queue(canvas, put, state, left_w, body_h)
        if self.view == "lyrics":
            self._draw_lyrics(put, state, left_w + 1, 2, right_w, body_h - 2)
        else:
            self._draw_now(put, state, left_w + 1, 2, right_w, body_h - 2)
        footer = fg("dim") + FOOTER + RESET
        put(rows - 1, max(0, (cols - len(FOOTER)) // 2), footer)
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
        pos = min(total, 1)
        put(2, 2, fg("dim") + f"< Library  {pos}/{max(total,1)} · {int(sum(float(i.get('duration') or 0) for i in items))}" + RESET)
        header = "▸ Play Liked Songs"
        put(3, 3, bg("accent") + fg("bg") + pad(header, width - 5) + RESET)
        y = 4
        for i, item in enumerate(items[: max(0, height - 4)]):
            title = str(item.get("title") or "Unknown")
            artist = str(item.get("artist") or "")
            line = crop(title, max(8, width - 10))
            if artist:
                line = pad(line, width - 10) + fg("dim") + crop(artist, 8) + RESET
            color = fg("accent") if i == 0 else (fg("dim") if i < self.selected else fg("fg"))
            put(y, 3, color + crop(line, width - 4) + RESET)
            y += 1

    def _draw_now(self, put: Any, state: dict[str, Any], x: int, y: int, width: int, height: int) -> None:
        art_cells = min(40, max(12, min(width - 8, height - 10)))
        art_rows = max(6, min(art_cells // 2, height - 10))
        ax = x + max(0, (width - art_cells) // 2)
        for n, line in enumerate(self.art.render(state.get("thumb"), art_cells, art_rows)):
            put(y + n, ax, line)
        title = str(state.get("title") or "Unknown")
        artist = str(state.get("artist") or "")
        put(y + art_rows + 1, x + max(0, (width - len(title)) // 2), fg("fg") + BOLD + crop(title, width) + RESET)
        put(y + art_rows + 2, x + max(0, (width - len(artist)) // 2), fg("accent") + crop(artist, width) + RESET)
        pos = float(state.get("position") or 0)
        dur = float(state.get("duration") or 0)
        bar_w = max(10, width - 18)
        fill = int((pos / dur) * bar_w) if dur else 0
        bar = "─" * max(0, fill) + "o" + "─" * max(0, bar_w - fill - 1)
        put(y + art_rows + 4, x + 2, fg("dim") + f"{mmss(pos)} " + fg("accent") + bar + fg("dim") + f" {mmss(dur)}" + RESET)
        vals = self.spectrum.values()
        base = y + height - 2
        max_h = max(3, min(8, height // 3))
        start_x = x + max(0, (width - len(vals) * 2) // 2)
        for i, v in enumerate(vals):
            h = max(1, int(v * max_h))
            for j in range(h):
                put(base - j, start_x + i * 2, fg("accent") + "█" + RESET)

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
            elif key == "s":
                send_command("shuffle")
            elif key in ("l", "\t"):
                self.view = "lyrics" if self.view == "now" else "now"
            elif key == "[":
                self.lyrics.offset -= 0.5
            elif key == "]":
                self.lyrics.offset += 0.5
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
                    if key == "/":
                        out.write("\x1b[2K\rsearch: ")
                        out.flush()
                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
                        query = sys.stdin.readline().strip()
                        tty.setcbreak(sys.stdin.fileno())
                        try:
                            resp = send_command("search", arg=query)
                            renderer.toast = resp.get("error") or f"queued: {query}"
                        except Exception as exc:
                            renderer.toast = str(exc)
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
