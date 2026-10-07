"""Design system for the MYX TUI.

One place that owns *how much* space and *what size* — instead of every screen
hand-rolling padding. Screens ask this module for numbers; they never invent
them.

Two concepts:

- **Spacing scale** (`space()`) — a rhythm in terminal cells, derived from the
  base unit, never an ad-hoc literal at the call site.
- **Type scale** (`TypeScale`, `type_of()`) — named semantic roles (display,
  title, body, meta, ...) mapped to weight/emphasis at a given breakpoint.
  Screens name the *role*, never a raw bold/dim combination.

Plus **breakpoints** (`Breakpoint`, `for_size()`) so 40-column, 60-column,
wide, and short terminals each get a deliberate layout instead of whatever
happens to fit.

Width math is display-cell math (CJK/emoji count 2), matching `tui.cell_len`,
but this module stays stdlib-only and import-time cheap so tests can use it
without pulling in the renderer.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum

# The base rhythm unit: everything spaces up is a multiple of this.
BASE_UNIT = 2


def _char_cells(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cells(text: str) -> int:
    """Display width of plain `text` in terminal cells (CJK/emoji count 2)."""
    return sum(_char_cells(ch) for ch in text)


class Step(int, Enum):
    """Named steps on the spacing scale."""

    NONE = 0
    HAIRO = BASE_UNIT  # hairline: 2
    THIN = 4  # 2 units
    SNUG = 6  # 3 units
    BASE = 8  # 4 units — the default gutter
    LOOSE = 12  # 6 units
    CHUNKY = 16  # 8 units — section breaks


# Ergonomic aliases. Screens read better with intent words than enum members.
HAIRO = Step.HAIRO
THIN = Step.THIN
SNUG = Step.SNUG
BASE = Step.BASE
LOOSE = Step.LOOSE
CHUNKY = Step.CHUNKY


def space(step: Step | int, unit: int = BASE_UNIT) -> int:
    """Cells for one spacing step. Always an integer; never negative.

    `Step` members carry multiples of `BASE_UNIT` directly, so `unit` only
    matters when a caller scales the whole system.
    """
    return max(0, int(step)) * max(1, unit) // BASE_UNIT


class Breakpoint(str, Enum):
    """Terminal-width classes. Ordered narrow -> wide."""

    TINY = "tiny"  # < 50 cols
    NARROW = "narrow"  # 50-79
    MEDIUM = "medium"  # 80-109
    WIDE = "wide"  # >= 110


# Each pair is (first column count of the class, class). Lookup keeps the last
# match, so a class covers every width up to the next threshold. The enum
# docstrings state the same ranges; keep the two in step.
BREAKPOINTS: tuple[tuple[int, Breakpoint], ...] = (
    (0, Breakpoint.TINY),
    (50, Breakpoint.NARROW),
    (80, Breakpoint.MEDIUM),
    (110, Breakpoint.WIDE),
)


def for_size(cols: int, rows: int = 24) -> Breakpoint:
    """Classify a terminal by columns (and rows, for the short-terminal case).

    Rows never change the width class — they are an independent axis handled by
    `Metrics.is_short`. Keeping them separate stops a 200x8 window from being
    treated as "wide".
    """
    width = max(0, int(cols))
    chosen = Breakpoint.TINY
    for threshold, bp in BREAKPOINTS:
        if width >= threshold:
            chosen = bp
    return chosen


class Role(str, Enum):
    """Semantic type roles. Screens name these, never raw SGR codes."""

    DISPLAY = "display"  # the one big thing: page title, track title
    TITLE = "title"  # section headings, card titles
    SUBTITLE = "subtitle"  # shelf captions, secondary titles
    BODY = "body"  # track titles, list rows
    META = "meta"  # artist, durations, counts
    LABEL = "label"  # uppercase eyebrows, column headers
    ACCENT = "accent"  # branded, active, actionable text
    MUTED = "muted"  # disabled, placeholder, inactive
    ERROR = "error"


@dataclass(frozen=True)
class TypeStyle:
    """What a role looks like at a given breakpoint."""

    bold: bool
    emphasis: str  # "accent" | "fg" | "dim" | "err"
    # Truncation budget as a fraction of available width, per role. Roles that
    # carry the page's meaning (display) may take more room than metadata.
    width_weight: float = 1.0


# Base (wide) rendering of each role. Narrow breakpoints tighten rather than
# restyle: the hierarchy stays legible, it just gets quieter.
_BASE_ROLES: dict[Role, TypeStyle] = {
    Role.DISPLAY: TypeStyle(bold=True, emphasis="accent", width_weight=1.0),
    Role.TITLE: TypeStyle(bold=True, emphasis="fg", width_weight=1.0),
    Role.SUBTITLE: TypeStyle(bold=False, emphasis="fg", width_weight=0.8),
    Role.BODY: TypeStyle(bold=False, emphasis="fg", width_weight=1.0),
    Role.META: TypeStyle(bold=False, emphasis="dim", width_weight=0.6),
    Role.LABEL: TypeStyle(bold=False, emphasis="dim", width_weight=0.5),
    Role.ACCENT: TypeStyle(bold=True, emphasis="accent", width_weight=0.8),
    Role.MUTED: TypeStyle(bold=False, emphasis="dim", width_weight=0.7),
    Role.ERROR: TypeStyle(bold=True, emphasis="err", width_weight=1.0),
}

# At small sizes, de-emphasise the things that fight for attention: two bold
# accent things on one line is noise, not hierarchy.
_TIGHT_ROLES: dict[Role, TypeStyle] = {
    Role.DISPLAY: TypeStyle(bold=True, emphasis="accent", width_weight=1.0),
    Role.TITLE: TypeStyle(bold=True, emphasis="fg", width_weight=1.0),
    Role.SUBTITLE: TypeStyle(bold=False, emphasis="dim", width_weight=0.8),
    Role.BODY: TypeStyle(bold=False, emphasis="fg", width_weight=1.0),
    Role.META: TypeStyle(bold=False, emphasis="dim", width_weight=0.5),
    Role.LABEL: TypeStyle(bold=False, emphasis="dim", width_weight=0.5),
    Role.ACCENT: TypeStyle(bold=False, emphasis="accent", width_weight=0.8),
    Role.MUTED: TypeStyle(bold=False, emphasis="dim", width_weight=0.6),
    Role.ERROR: TypeStyle(bold=True, emphasis="err", width_weight=1.0),
}


@dataclass(frozen=True)
class TypeScale:
    """Role -> TypeStyle for one breakpoint."""

    breakpoint: Breakpoint
    roles: dict[Role, TypeStyle]

    def __getitem__(self, role: Role) -> TypeStyle:
        return self.roles[role]

    def style(self, role: Role | str) -> TypeStyle:
        return self.roles[Role(role)]


def type_scale(bp: Breakpoint) -> TypeScale:
    """The type scale for a breakpoint."""
    base = _BASE_ROLES if bp in (Breakpoint.MEDIUM, Breakpoint.WIDE) else _TIGHT_ROLES
    return TypeScale(bp, dict(base))


def type_of(bp: Breakpoint, role: Role | str) -> TypeStyle:
    """Shorthand: the style for `role` at breakpoint `bp`."""
    return type_scale(bp).style(role)


# ---------------------------------------------------------------------------
# layout metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    """Everything a screen needs to lay itself out, resolved once.

    Screens take a `Metrics` instead of juggling `cols`/`rows`/magic padding, so
    every screen shares the same idea of where the gutters are.
    """

    cols: int
    rows: int
    bp: Breakpoint

    @property
    def is_short(self) -> bool:
        """Too short to afford a two-row section header plus content."""
        return self.rows < 16

    @property
    def is_tiny(self) -> bool:
        return self.bp == Breakpoint.TINY

    @property
    def is_wide(self) -> bool:
        return self.bp in (Breakpoint.MEDIUM, Breakpoint.WIDE)

    @property
    def gutter(self) -> int:
        """Outer left/right margin."""
        return space(THIN) if self.is_tiny else space(SNUG)

    @property
    def gap(self) -> int:
        """Gap between sibling items inside a section."""
        if self.bp == Breakpoint.WIDE:
            return space(HAIRO)
        return 1

    @property
    def section_gap(self) -> int:
        """Blank rows between top-level sections."""
        if self.is_short:
            return 1
        if self.is_tiny:
            return 1
        return 2

    @property
    def content_width(self) -> int:
        """Usable width inside the gutters, never negative."""
        return max(1, self.cols - 2 * self.gutter)

    @property
    def columns(self) -> int:
        """How many content columns fit side by side (cards per row)."""
        # A card needs a readable minimum before it is worth showing.
        min_card = 34 if self.is_wide else 26
        if self.content_width < min_card:
            return 1
        return max(1, self.content_width // min_card)

    @property
    def card_width(self) -> int:
        """Width of one card given the current column count."""
        if self.columns <= 1:
            return self.content_width
        return max(12, (self.content_width - self.gap * (self.columns - 1)) // self.columns)

    @property
    def show_sidebar(self) -> bool:
        """Whether a persistent left sidebar earns its columns."""
        return self.bp in (Breakpoint.MEDIUM, Breakpoint.WIDE) and not self.is_short

    @property
    def sidebar_width(self) -> int:
        if not self.show_sidebar:
            return 0
        return max(22, min(38, int(self.content_width * 0.34)))

    @property
    def main_width(self) -> int:
        return max(1, self.content_width - (self.sidebar_width + 1 if self.show_sidebar else 0))

    @property
    def type(self) -> TypeScale:
        return type_scale(self.bp)

    def style(self, role: Role | str) -> TypeStyle:
        return self.type.style(role)


def metrics(cols: int, rows: int) -> Metrics:
    """Resolve layout metrics for a terminal size."""
    c, r = max(1, int(cols)), max(1, int(rows))
    return Metrics(cols=c, rows=r, bp=for_size(c, r))


def clamp_width(value: int, *limits: int) -> int:
    """Clamp `value` into the range spanned by `limits`, never below 0."""
    if not limits:
        return max(0, int(value))
    lo, hi = min(limits), max(limits)
    return max(0, min(hi, max(lo, int(value))))
