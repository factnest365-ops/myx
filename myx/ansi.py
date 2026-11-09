"""ANSI and display-width primitives for MYX.

Single source of truth for colours, cell widths, and text fitting. Both the
renderer (`tui.py`) and the app surface (`surface.py`) depend on this, which is
why it lives in its own module: the alternative was a circular import.

`tui.py` re-exports every name here, so existing callers and tests are
unaffected.
"""

from __future__ import annotations

import re
import unicodedata

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


def styled(bold: bool, emphasis: str, text: str) -> str:
    """Wrap `text` in a design-system weight + colour, with a reset."""
    return (BOLD if bold else "") + fg(emphasis) + text + RESET