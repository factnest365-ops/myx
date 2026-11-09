"""Mouse support for the MYX TUI.

Two halves:

- **Protocol** — turn SGR (1006) mouse reports from the terminal into
  `MouseEvent`s, and emit the enable/disable sequences. Reporting is turned on
  at start and off at exit so no terminal is left in a reporting state.
- **Hit testing** — a registry of named rectangular regions that the renderer
  populates as it draws. A click looks up the region under the cursor.

The registry is the backbone of the "nothing looks clickable and isn't" rule:
the renderer cannot draw an interactive-looking element without registering a
region for it, and `InteractiveMap` refuses to report a region as usable unless
it carries a real action.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum

# Terminal enable/disable sequences for the modes we need.
# 1000 = normal tracking, 1002 = button-event (drag) tracking,
# 1006 = SGR extended coordinates.
ENABLE_MOUSE = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
DISABLE_MOUSE = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"

# SGR mouse report: ESC [ < button ; col ; row (M press | m release)
_SGR_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


class MouseButton(int, Enum):
    LEFT = 0
    MIDDLE = 1
    RIGHT = 2
    NONE = 3


class MouseKind(str, Enum):
    PRESS = "press"
    RELEASE = "release"
    DRAG = "drag"
    WHEEL_UP = "wheel_up"
    WHEEL_DOWN = "wheel_down"


@dataclass(frozen=True)
class MouseEvent:
    """One decoded mouse report, in 1-based terminal cells."""

    col: int
    row: int
    kind: MouseKind
    button: MouseButton

    @property
    def x(self) -> int:
        """0-based column."""
        return self.col - 1

    @property
    def y(self) -> int:
        """0-based row."""
        return self.row - 1

    @property
    def is_click(self) -> bool:
        return self.kind == MouseKind.PRESS


def decode_sgr(button_code: int, col: int, row: int, final: str) -> MouseEvent | None:
    """Decode one SGR mouse parameter set.

    Buttons use the low two bits; bit 2 (value 32) marks motion, and the
    64/65 pair marks the wheel. Returns None for codes that are not mouse
    reports at all.
    """
    motion = bool(button_code & 32)
    base = button_code & 0b11
    is_press = final == "M"

    if button_code & 64:
        # 64 = wheel up, 65 = wheel down
        kind = MouseKind.WHEEL_UP if base == 0 else MouseKind.WHEEL_DOWN
        return MouseEvent(col, row, kind, MouseButton.NONE)

    if base == 3:
        # 3 with motion is a drag with no button held; ignore it.
        return None
    if not is_press and not motion:
        return MouseEvent(col, row, MouseKind.RELEASE, MouseButton(base))

    button = MouseButton(base)
    if motion:
        return MouseEvent(col, row, MouseKind.DRAG, button)
    return MouseEvent(col, row, MouseKind.PRESS, button)


def parse_mouse(data: str) -> list[MouseEvent]:
    """Extract every complete SGR mouse report from a chunk of stdin.

    Incomplete trailing sequences are ignored rather than misread — the run loop
    will see the rest on the next read.
    """
    events: list[MouseEvent] = []
    for match in _SGR_RE.finditer(data):
        event = decode_sgr(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4)
        )
        if event is not None:
            events.append(event)
    return events


def has_mouse(data: str) -> bool:
    """Whether a chunk contains an SGR mouse report."""
    return _SGR_RE.search(data) is not None


# ---------------------------------------------------------------------------
# hit testing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """An inclusive-exclusive rectangle in 0-based screen cells."""

    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def __bool__(self) -> bool:
        """Empty rects are falsy, so callers can test a region for real area."""
        return self.w > 0 and self.h > 0


@dataclass
class Region:
    """A clickable area bound to an action."""

    id: str
    rect: Rect
    action: Callable[..., object] | None = None
    args: tuple = ()
    # Populated by the renderer so keyboard parity can be proven.
    keys: tuple[str, ...] = ()
    # Whether the region currently accepts input (disabled buttons stay visible).
    enabled: bool = True

    @property
    def actionable(self) -> bool:
        """A region is only real if it has area and something to run."""
        return bool(self.rect) and self.action is not None


class InteractiveMap:
    """Registry of clickable regions, rebuilt every frame.

    Later registrations win ties, so a specific control drawn inside a broad
    container still gets the click.
    """

    def __init__(self) -> None:
        self._regions: dict[str, Region] = {}

    def __len__(self) -> int:
        return len(self._regions)

    def __iter__(self) -> Iterator[Region]:
        return iter(self._regions.values())

    def __contains__(self, region_id: object) -> bool:
        return region_id in self._regions

    def clear(self) -> None:
        self._regions.clear()

    def add(
        self,
        region_id: str,
        rect: Rect,
        action: Callable[..., object] | None = None,
        args: tuple = (),
        keys: tuple[str, ...] = (),
        enabled: bool = True,
    ) -> Region:
        """Register a region. Overwrites a previous region with the same id."""
        region = Region(region_id, rect, action, args, keys, enabled)
        self._regions[region_id] = region
        return region

    def get(self, region_id: str) -> Region | None:
        return self._regions.get(region_id)

    def ids(self) -> list[str]:
        return list(self._regions)

    def actionable_ids(self) -> list[str]:
        return [r.id for r in self._regions.values() if r.actionable]

    def at(self, px: int, py: int) -> Region | None:
        """Topmost enabled region containing the point, else None."""
        hit: Region | None = None
        for region in self._regions.values():
            if region.enabled and region.rect.contains(px, py):
                hit = region  # later registration wins
        return hit

    def dead_affordances(self) -> list[str]:
        """Ids that look registered but would do nothing if clicked.

        Anything returned here violates the launch brief's core rule.
        """
        return sorted(r.id for r in self._regions.values() if not r.actionable)

    def missing_keyboard_parity(self) -> list[str]:
        """Actionable ids with no keybinding, i.e. mouse-only reachability."""
        return sorted(r.id for r in self._regions.values() if r.actionable and not r.keys)


@dataclass
class DragTracker:
    """Tracks a press-drag-release gesture over one region (seek bars).

    The press picks the region, drags keep reporting the same region even when
    the cursor leaves it — otherwise a seek would drop the moment you moved.
    """

    region_id: str | None = None
    start: tuple[int, int] | None = None
    moved: bool = False
    on_release: Callable[[str], object] | None = None

    def press(self, region_id: str | None, px: int, py: int) -> None:
        self.region_id = region_id
        self.start = (px, py)
        self.moved = False

    def drag(self, px: int, py: int) -> None:
        if self.start is not None:
            self.moved = self.moved or (px, py) != self.start

    def release(self, px: int, py: int) -> str | None:
        self.drag(px, py)
        finished = self.region_id
        self.region_id = None
        self.start = None
        self.moved = False
        return finished

    def cancel(self) -> None:
        self.region_id = None
        self.start = None
        self.moved = False

    @property
    def active(self) -> bool:
        return self.region_id is not None


@dataclass
class WheelTarget:
    """What a wheel event scrolls, resolved from the pointer position."""

    scrollable_ids: tuple[str, ...] = ()
    registry: InteractiveMap = field(default_factory=InteractiveMap)

    def resolve(self, px: int, py: int) -> str | None:
        """Region under the cursor that can scroll, else the global one."""
        hit = self.registry.at(px, py)
        if hit is not None and hit.id in self.scrollable_ids:
            return hit.id
        return "global" if "global" in self.scrollable_ids else None
