"""Picker logic for MYX TUI — pure functions, no TUI rendering, no network.

Covers fuzzy search, debounce, PickerState, command palette, help/tour, and
formatting helpers. Stdlib only.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Any

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean_inline(value: Any) -> str:
    return _CTRL_RE.sub("", str(value or ""))


# ---------------------------------------------------------------------------
# fuzzy helpers
# ---------------------------------------------------------------------------


def fuzzy_score(query: str, text: str) -> float:
    """Score how well *query* matches *text* in [0, 1].

    - empty query scores 1.0 (neutral)
    - exact substring gets strong boost (+0.4)
    - prefix bonus (+0.1)
    - underlying SequenceMatcher ratio for typo tolerance
    """
    if not query:
        return 1.0
    q = query.lower().strip()
    t = text.lower().strip()
    if not t:
        return 0.0
    if not q:
        return 1.0
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    if q in t:
        ratio = min(1.0, ratio + 0.4)
        if t.startswith(q):
            ratio = min(1.0, ratio + 0.1)
        elif any(w.startswith(q) for w in t.split()):
            ratio = min(1.0, ratio + 0.06)
    return max(0.0, min(1.0, ratio))


def fuzzy_filter(
    query: str,
    items: list[Any],
    key: Any | None = None,
    threshold: float = 0.3,
) -> list[Any]:
    """Return *items* filtered/sorted by fuzzy_score.

    If query is empty/whitespace, returns a shallow copy preserving order.
    """
    if not query or not query.strip():
        return list(items)
    q = query.strip()
    scored: list[tuple[float, Any]] = []
    for item in items:
        text = key(item) if key else str(item)
        score = fuzzy_score(q, text)
        if score >= threshold:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored]


# ---------------------------------------------------------------------------
# debounce
# ---------------------------------------------------------------------------


def should_search(
    last_query_time: float, current_time: float, debounce_ms: int = 250
) -> bool:
    """Return True when *debounce_ms* has elapsed since *last_query_time*."""
    return (current_time - last_query_time) * 1000 >= debounce_ms


@dataclass
class Debouncer:
    """Simple time-based debouncer (no threads). Hermetic / test-friendly."""

    delay_ms: int = 250
    last_time: float | None = None

    def mark(self, now: float | None = None) -> None:
        self.last_time = now if now is not None else time.monotonic()

    def ready(self, now: float | None = None) -> bool:
        if self.last_time is None:
            return True
        cur = now if now is not None else time.monotonic()
        return should_search(self.last_time, cur, self.delay_ms)

    def should_fire(self, now: float | None = None) -> bool:
        """Return True if ready and mark consumed. Convenience for loops."""
        if self.ready(now):
            return True
        return False


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def format_duration(value: Any) -> str:
    try:
        s = int(float(value or 0))
    except Exception:
        s = 0
    s = max(0, s)
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def format_result_line(item: dict[str, Any], width: int = 48) -> str:
    """Single-line summary for a picker result: 'Title — Artist  m:ss'."""
    title = (_clean_inline(item.get("title")) or "Unknown")[: max(8, width - 16)]
    artist = _clean_inline(item.get("artist"))
    dur = format_duration(item.get("duration"))
    if artist:
        avail = max(8, width - len(title) - len(dur) - 5)
        artist = artist[:avail]
        return f"{title} — {artist}  {dur}"
    return f"{title}  {dur}"


# ---------------------------------------------------------------------------
# PickerState
# ---------------------------------------------------------------------------


@dataclass
class PickerState:
    """State for the live fuzzy search picker."""

    query: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)
    selected: int = 0
    loading: bool = False
    error: str | None = None
    debounce_ms: int = 250
    last_query_time: float = field(default_factory=lambda: 0.0)

    def set_query(self, query: str, now: float | None = None) -> None:
        self.query = _clean_inline(query)
        self.selected = 0
        self.error = None
        self.last_query_time = now if now is not None else time.monotonic()
        if not self.query.strip():
            self.results = []
            self.loading = False

    def append_char(self, ch: str, now: float | None = None) -> None:
        self.set_query(self.query + ch, now=now)

    def backspace(self, now: float | None = None) -> None:
        if self.query:
            self.set_query(self.query[:-1], now=now)

    def clear(self) -> None:
        self.query = ""
        self.results = []
        self.selected = 0
        self.loading = False
        self.error = None

    def move(self, delta: int) -> None:
        if not self.results:
            self.selected = 0
            return
        self.selected = max(0, min(len(self.results) - 1, self.selected + delta))

    def set_results(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.loading = False
        self.error = None
        self.selected = max(0, min(self.selected, max(0, len(self.results) - 1)))

    def set_loading(self, loading: bool = True) -> None:
        self.loading = loading
        if loading:
            self.error = None

    def set_error(self, error: str | None) -> None:
        self.error = _clean_inline(error) if error else None
        self.loading = False

    def selected_result(self) -> dict[str, Any] | None:
        if 0 <= self.selected < len(self.results):
            return self.results[self.selected]
        return None

    def should_search_now(self, now: float | None = None) -> bool:
        cur = now if now is not None else time.monotonic()
        return should_search(self.last_query_time, cur, self.debounce_ms)


# ---------------------------------------------------------------------------
# Command palette entries
# ---------------------------------------------------------------------------

COMMAND_PALETTE: list[dict[str, str]] = [
    {"id": "play", "label": "Play / Resume", "keys": "space", "action": "toggle"},
    {"id": "pause", "label": "Pause", "keys": "space", "action": "pause"},
    {"id": "next", "label": "Next track", "keys": "n", "action": "skip"},
    {"id": "prev", "label": "Previous track", "keys": "p", "action": "prev"},
    {"id": "seek_fwd", "label": "Seek forward +5s", "keys": "→", "action": "seek_fwd"},
    {"id": "seek_back", "label": "Seek backward -5s", "keys": "←", "action": "seek_back"},
    {"id": "like", "label": "Like / Unlike current song ♥", "keys": "ctrl-l", "action": "like"},
    {"id": "play_liked", "label": "Play Liked Songs ♥", "keys": "click / lib", "action": "play_liked"},
    {"id": "cache_track", "label": "Cache current song offline", "keys": "offline", "action": "cache_track"},
    {"id": "playlist_save", "label": "Save queue to 'Favorites' playlist", "keys": "playlist", "action": "playlist_save"},
    {"id": "eq_cycle", "label": "Cycle Audiophile EQ / DSP preset", "keys": "ctrl-e", "action": "eq_cycle"},
    {"id": "eq_bass", "label": "EQ Preset: Bass Boost", "keys": "eq", "action": "eq_bass"},
    {"id": "eq_vocal", "label": "EQ Preset: Vocal Clarity", "keys": "eq", "action": "eq_vocal"},
    {"id": "eq_spatial", "label": "EQ Preset: Spatial Crossfeed", "keys": "eq", "action": "eq_spatial"},
    {"id": "eq_slowed", "label": "DSP Preset: Slowed + Reverb", "keys": "dsp", "action": "eq_slowed"},
    {"id": "eq_nightcore", "label": "DSP Preset: Nightcore", "keys": "dsp", "action": "eq_nightcore"},
    {"id": "eq_flat", "label": "EQ Preset: Flat (Bypass)", "keys": "eq", "action": "eq_flat"},
    {"id": "normalize", "label": "Toggle Loudness Normalization", "keys": "norm", "action": "normalize"},
    {"id": "crossfade", "label": "Toggle 4s Smart Crossfade", "keys": "xfade", "action": "crossfade"},
    {"id": "sponsorblock", "label": "Toggle SponsorBlock Auto-Skip", "keys": "ctrl-b", "action": "sponsorblock"},
    {"id": "next_chapter", "label": "Next YouTube Chapter", "keys": "}", "action": "next_chapter"},
    {"id": "prev_chapter", "label": "Previous YouTube Chapter", "keys": "{", "action": "prev_chapter"},
    {"id": "chameleon", "label": "Toggle Dynamic Album-Art Theme", "keys": "ctrl-t", "action": "chameleon"},
    {"id": "shuffle", "label": "Shuffle queue", "keys": "s", "action": "shuffle"},
    {"id": "loop", "label": "Cycle repeat (off / one / all)", "keys": "ctrl-r", "action": "loop"},
    {"id": "mute", "label": "Mute / Unmute audio", "keys": "ctrl-g", "action": "mute"},
    {"id": "vol_up", "label": "Volume up", "keys": "+ / =", "action": "vol_up"},
    {"id": "vol_down", "label": "Volume down", "keys": "- ", "action": "vol_down"},
    {"id": "clear", "label": "Clear upcoming queue", "keys": "clear", "action": "clear"},
    {
        "id": "lyrics",
        "label": "Toggle lyrics view",
        "keys": "l / tab",
        "action": "lyrics",
    },
    {"id": "queue", "label": "Toggle queue view", "keys": "tab", "action": "queue"},
    {
        "id": "search",
        "label": "Search YouTube / YT Music",
        "keys": "/ · type to search",
        "action": "search",
    },
    {"id": "help", "label": "Show help & tour", "keys": "? ", "action": "help"},
    {
        "id": "doctor",
        "label": "Run doctor checks",
        "keys": "doctor",
        "action": "doctor",
    },
    {"id": "quit", "label": "Quit MYX", "keys": "q", "action": "quit"},
]


def command_palette_search(query: str) -> list[dict[str, str]]:
    """Fuzzy-filter COMMAND_PALETTE by label/id/keys."""
    if not query.strip():
        return list(COMMAND_PALETTE)
    return fuzzy_filter(
        query, COMMAND_PALETTE, key=lambda e: f"{e['label']} {e['id']} {e['keys']}"
    )


# ---------------------------------------------------------------------------
# Help content — keybinds + first-run tour
# ---------------------------------------------------------------------------

KEYBINDS: list[dict[str, str]] = [
    {"keys": "type", "desc": "Type to search — results appear instantly"},
    {"keys": "/", "desc": "Open search picker (tab enqueues, enter plays)"},
    {"keys": "↑ / ↓  · j / k", "desc": "Navigate queue or search results"},
    {"keys": "← / →", "desc": "Seek backward / forward 5 seconds"},
    {"keys": "enter", "desc": "Play selected / confirm"},
    {"keys": "esc", "desc": "Close picker / overlay"},
    {"keys": "space", "desc": "Play / pause"},
    {"keys": "n / p", "desc": "Next / previous track (p restarts if >5s)"},
    {"keys": "+ / -", "desc": "Volume up / down"},
    {"keys": "s", "desc": "Shuffle queue"},
    {"keys": "ctrl-l", "desc": "Like / unlike current song ♥"},
    {"keys": "ctrl-e", "desc": "Cycle EQ / DSP preset (Bass, Vocal, Nightcore...)"},
    {"keys": "ctrl-b", "desc": "Toggle SponsorBlock non-music auto-skip"},
    {"keys": "ctrl-t", "desc": "Toggle Album-Art Chameleon dynamic theme"},
    {"keys": "{ / }", "desc": "Previous / next YouTube chapter"},
    {"keys": "mouse", "desc": "Click progress bar to scrub, click tabs/queue/controls"},
    {"keys": "ctrl-r", "desc": "Cycle repeat mode (off → one → all)"},
    {"keys": "l / tab", "desc": "Cycle view: Now → Lyrics → Queue"},
    {"keys": "[ / ]", "desc": "Nudge synced lyrics offset ∓0.5s"},
    {"keys": "d", "desc": "Delete selected (press twice to confirm)"},
    {"keys": "ctrl-k", "desc": "Command palette (mute, clear, repeat, etc.)"},
    {"keys": "?", "desc": "Help & tour"},
    {"keys": "q", "desc": "Quit"},
]

TOUR_STEPS: list[dict[str, str]] = [
    {
        "title": "Step 1 — Search",
        "description": "Type to search YouTube / YT Music, results appear instantly. No need to press / — just type.",
    },
    {
        "title": "Step 2 — Queue",
        "description": "Arrow keys navigate, enter plays, d deletes (press twice to confirm). Now-playing is highlighted.",
    },
    {
        "title": "Step 3 — Play",
        "description": "Space pauses, n / p skips, ← / → seeks ±5s, q quits. [▶] [⏸] [⏭] [🔀] controls are always in the footer.",
    },
]
