"""The MYX app surface: search bar, home shelves, and the affordance contract.

This module draws the *app-like* half of the TUI — a persistent top search bar
and a For You home screen — onto a canvas, while registering every interactive
element it draws in an `InteractiveMap`.

The central guarantee is structural, not a convention:

    `_element()` is the only way to draw something interactive, and it refuses
    to register a region with no real action behind it.

So a screen cannot paint a control that looks clickable and do nothing about
it. If it is drawn, it is wired; if it cannot be wired, it is not drawn. The
keybinding travels in the same call, which makes keyboard parity provable
rather than aspirational.

Drawing is pure: it takes the `put` closure the renderer already uses, writes
to the canvas, and registers regions. It performs no network or filesystem IO —
commands are handed back to the caller through injected callbacks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .ansi import cell_len, crop, pad, styled
from .design import Metrics, Role
from .library import Library, Shelf, Track, home_shelves
from .mouse import InteractiveMap, Rect

# Visual grammar. Every one of these only ever appears on an element that was
# registered, so an affordance glyph is proof of a wired control.
CHEVRON = "›"  # "this section opens"
CURSOR_BLOCK = "▌"  # focus caret
SEARCH_ICON = "⌕"  # "this is search"
SELECTED_MARK = "▸"  # current card
HEART = "♥"  # favourite toggle


@dataclass
class SearchBar:
    """State for the always-present top search field.

    The bar is drawn on every frame of every screen. Focus is an explicit state
    rather than a mode entered by remembering a key: `focused` decides whether
    typed characters reach the field or the app.
    """

    query: str = ""
    focused: bool = False
    placeholder: str = "Search songs, artists, playlists"
    max_visible: int = 0
    scroll: int = 0

    def focus(self) -> None:
        self.focused = True

    def blur(self) -> None:
        self.focused = False

    def type_char(self, ch: str) -> None:
        self.query += ch
        self.scroll = 0

    def backspace(self) -> None:
        self.query = self.query[:-1]
        self.scroll = 0

    def clear(self) -> None:
        self.query = ""
        self.scroll = 0

    def home(self) -> None:
        self.scroll = 0

    def end(self) -> None:
        """Show the tail of a long query, so the caret stays visible."""
        if self.max_visible:
            self.scroll = max(0, len(self.query) - self.max_visible)

    @property
    def is_empty(self) -> bool:
        return not self.query.strip()

    def visible_text(self) -> str:
        """The slice of the query that fits, scrolled to the caret."""
        if not self.max_visible or len(self.query) <= self.max_visible:
            self.scroll = 0
            return self.query
        self.scroll = max(0, min(self.scroll, len(self.query) - self.max_visible))
        return self.query[self.scroll : self.scroll + self.max_visible]

    def display(self, width: int) -> str:
        """What to paint, with the caret only while focused."""
        if width <= 0:
            return ""
        if self.is_empty:
            return self.placeholder
        text = self.visible_text()
        return text + (CURSOR_BLOCK if self.focused else "")


@dataclass
class SearchSuggestion:
    """One suggestion under the search bar."""

    text: str
    kind: str = "recent"
    id: str = ""


@dataclass
class HomeView:
    """The For You screen's own state: shelves, selection, and card geometry.

    Keyboard and mouse both drive this one object, so a click and a keypress
    cannot drift into different behaviours.
    """

    library: Library = field(default_factory=Library)
    shelves: list[Shelf] = field(default_factory=list)
    selected_shelf: int = 0
    selected_card: int = 0
    # Rebuilt every frame: card id -> where it was drawn, for click resolution.
    card_rects: dict[str, Rect] = field(default_factory=dict)
    # Shelves that did not fit this frame. Empty means everything was shown.
    dropped_shelf_ids: list[str] = field(default_factory=list)
    # Row offset applied when drawing, so a shelf past the fold stays reachable.
    scroll: int = 0

    def scroll_by(self, delta: int) -> None:
        """Move the home screen's vertical offset, never below zero."""
        self.scroll = max(0, self.scroll + delta)

    @property
    def has_more_below(self) -> bool:
        """Whether any shelf is currently below the fold."""
        return bool(self.dropped_shelf_ids)

    def refresh(self, now: float | None = None) -> None:
        """Recompute shelves from the library, keeping the selection in range."""
        self.shelves = home_shelves(self.library, now=now)
        self._clamp_selection()

    def _clamp_selection(self) -> None:
        if not self.shelves:
            self.selected_shelf = self.selected_card = 0
            return
        self.selected_shelf = max(0, min(self.selected_shelf, len(self.shelves) - 1))
        tracks = self.shelves[self.selected_shelf].tracks
        self.selected_card = max(0, min(self.selected_card, max(0, len(tracks) - 1)))

    @property
    def current_shelf(self) -> Shelf | None:
        if 0 <= self.selected_shelf < len(self.shelves):
            return self.shelves[self.selected_shelf]
        return None

    @property
    def current_track(self) -> Track | None:
        shelf = self.current_shelf
        if shelf and 0 <= self.selected_card < len(shelf.tracks):
            return shelf.tracks[self.selected_card]
        return None

    def move_selection(self, delta: int) -> None:
        """Move along the focused shelf's cards, clamped at both ends."""
        if not self.shelves:
            return
        self._clamp_selection()
        tracks = self.shelves[self.selected_shelf].tracks
        if not tracks:
            self.selected_card = 0
            return
        self.selected_card = max(0, min(len(tracks) - 1, self.selected_card + delta))

    def focus_shelf(self, delta: int) -> None:
        """Move between shelves, wrapping at the ends."""
        if not self.shelves:
            return
        self.selected_shelf = (self.selected_shelf + delta) % len(self.shelves)
        self._clamp_selection()


@dataclass
class AffordanceReport:
    """The result of checking a frame against the no-dead-affordances rule."""

    drawn: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    mouse_only: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.dead

    def summary(self) -> str:
        if self.dead:
            return f"dead affordances: {', '.join(self.dead)}"
        if self.mouse_only:
            return f"mouse-only: {', '.join(self.mouse_only)}"
        return f"{len(self.drawn)} controls, all live"


# ---------------------------------------------------------------------------
# callbacks the host supplies
# ---------------------------------------------------------------------------


@dataclass
class SurfaceActions:
    """Everything the surface can do, injected by the host.

    Keeping these explicit means the drawer cannot invent behaviour, and tests
    can observe intent without a daemon.
    """

    focus_search: Callable[[], Any]
    submit_search: Callable[[str], Any]
    play: Callable[[Track], Any]
    open_artist: Callable[[Track], Any]
    open_shelf: Callable[[Shelf], Any]
    toggle_favourite: Callable[[Track], Any]
    pick_suggestion: Callable[[SearchSuggestion], Any]


class SurfaceDrawer:
    """Draws app chrome and registers every control it draws."""

    def __init__(
        self,
        actions: SurfaceActions,
        interactive: InteractiveMap | None = None,
    ) -> None:
        self.actions = actions
        self.interactive = interactive if interactive is not None else InteractiveMap()

    # -- the one way to draw something interactive --
    def _element(
        self,
        element_id: str,
        row: int,
        x: int,
        width: int,
        action: Callable[..., Any] | None,
        args: tuple = (),
        keys: Sequence[str] = (),
        height: int = 1,
        enabled: bool = True,
    ) -> Rect | None:
        """Register an interactive region; None if it cannot be made real.

        This guard is the point of the module. No action, no area, or a
        disabled control means we refuse to register rather than advertise
        something that does nothing.
        """
        rect = Rect(x, row, max(0, int(width)), max(0, int(height)))
        if not rect or not action:
            return None
        self.interactive.add(element_id, rect, action, tuple(args), tuple(keys), enabled)
        return rect

    # -- search bar --
    def draw_search_bar(
        self, put: Any, bar: SearchBar, m: Metrics, row: int
    ) -> None:
        """The always-present top search field, with real focus semantics."""
        x = m.gutter
        width = m.content_width
        hint = "↵ search" if bar.focused else "type to search"
        # Reserve the hint's columns before sizing the field text. Drawing the
        # hint without reserving space let it overwrite the placeholder on a
        # narrow terminal, producing garbled text like "Search songs,type to…".
        hint_w = cell_len(hint) + 2 if cell_len(hint) + 2 <= width else 0
        # Reserve the focus edge and the icon, plus the hint if it is shown.
        inner_w = max(1, width - 4 - hint_w)
        bar.max_visible = max(1, inner_w - 2)

        icon = CURSOR_BLOCK if bar.focused else SEARCH_ICON
        text = bar.display(inner_w)
        emphasis = "fg" if (bar.focused and not bar.is_empty) else "dim"
        # While focused and empty the placeholder stays muted — the caret is the
        # signal that the field has focus, not a louder placeholder.
        body = styled(False, emphasis, crop(text, inner_w))

        line = (
            styled(False, "accent" if bar.focused else "dim", "▌")
            + " "
            + styled(False, "accent" if bar.focused else "dim", icon)
            + " "
            + body
        )
        put(row, x, pad(line, width))

        if hint_w:
            put(
                row,
                x + width - cell_len(hint) - 1,
                styled(False, "accent" if bar.focused else "dim", hint),
            )

        # Clicking anywhere on the bar focuses it — always, focused or not.
        self._element("search.focus", row, x, width, self.actions.focus_search, keys=("/",))
        # Submit is the same visible field while focused, not a hidden button.
        if bar.focused:
            self._element(
                "search.submit",
                row,
                x,
                width,
                lambda q=bar.query: self.actions.submit_search(q),
                keys=("enter",),
            )

    def draw_suggestions(
        self,
        put: Any,
        suggestions: Sequence[SearchSuggestion],
        m: Metrics,
        row: int,
        available_rows: int,
    ) -> None:
        """Suggestions under the bar; each one registered and clickable."""
        if not suggestions:
            return
        x = m.gutter
        width = m.content_width
        limit = max(0, min(len(suggestions), available_rows))
        for i, suggestion in enumerate(suggestions[:limit]):
            marker = CHEVRON if i == 0 else " "
            line = (
                styled(False, "accent", marker)
                + " "
                + styled(False, "fg", crop(suggestion.text, max(1, width - 4)))
            )
            put(row + i, x, pad(line, width))
            self._element(
                f"suggestion.{i}",
                row + i,
                x,
                width,
                self.actions.pick_suggestion,
                args=(suggestion,),
                keys=("enter",),
            )

    # -- home shelves --
    def draw_home(
        self, put: Any, view: HomeView, m: Metrics, top_row: int, height: int
    ) -> None:
        """Draw the For You shelves, registering every card as actionable.

        When the shelves do not fit, the ones past the fold are *reported* on
        `view.dropped_shelf_ids` rather than vanishing silently. A shelf that
        exists in the model but is never drawn is a dead affordance in spirit
        even though nothing on screen looks broken, so the caller can offer a
        way to reach them.
        """
        view.card_rects.clear()
        view.dropped_shelf_ids = []
        bottom = top_row + max(0, height)
        for shelf_index, shelf in enumerate(view.shelves):
            # `scroll` lifts the first shelves off the top so a lower shelf can
            # take the visible band. Skipped shelves count as shown, not lost.
            if shelf_index < view.scroll:
                continue
            if top_row >= bottom:
                view.dropped_shelf_ids.append(shelf.id)
                continue
            before = top_row
            top_row = self._draw_shelf_header(put, view, shelf, m, top_row)
            # A shelf needs its header plus at least one card row to be worth
            # showing; otherwise the whole section is folded away.
            if top_row + 1 >= bottom:
                view.dropped_shelf_ids.append(shelf.id)
                top_row = before
                continue
            top_row = self._draw_shelf_cards(
                put, view, shelf, shelf_index, m, top_row, bottom
            )
            top_row += m.section_gap

    def _draw_shelf_header(
        self, put: Any, view: HomeView, shelf: Shelf, m: Metrics, row: int
    ) -> int:
        """Section header with a chevron that genuinely opens the section."""
        if not shelf.title.strip():
            return row
        x = m.gutter
        width = m.content_width
        chevron = styled(False, "accent", CHEVRON)
        # Reserve the chevron's column so a long title cannot overwrite it.
        title_room = max(1, width - cell_len(chevron) - 1)
        title = crop(shelf.title, title_room)
        line = styled(True, "fg", title)
        if shelf.subtitle and not m.is_tiny:
            used = cell_len(line)
            spare = title_room - used
            if spare >= cell_len(shelf.subtitle) + 2:
                gap = width - used - cell_len(shelf.subtitle) - 2
                line += " " * max(1, gap) + styled(False, "dim", shelf.subtitle)
        put(row, x, line)
        put(row, x + width - 1, chevron)
        self._element(
            f"shelf.{shelf.id}",
            row,
            x,
            width,
            self.actions.open_shelf,
            args=(shelf,),
            keys=("enter",),
        )
        return row + 1

    def _draw_shelf_cards(
        self,
        put: Any,
        view: HomeView,
        shelf: Shelf,
        shelf_index: int,
        m: Metrics,
        row: int,
        bottom: int,
    ) -> int:
        """Cards for one shelf, in a responsive grid."""
        tracks = [t for t in shelf.tracks if t.id]
        if not tracks:
            return row
        card_w = m.card_width
        cols = max(1, m.columns)
        card_h = 2 if m.is_tiny else 3
        gap = m.gap
        drawn = 0
        for index, track in enumerate(tracks):
            cx = m.gutter + (index % cols) * (card_w + gap)
            cy = row + (index // cols) * card_h
            if cy + card_h > bottom:
                break
            self._draw_card(
                put,
                view,
                shelf,
                shelf_index,
                track,
                index,
                m,
                cy,
                cx,
                card_w,
                card_h,
            )
            drawn += 1
        rows_used = (drawn + cols - 1) // cols if drawn else 0
        return row + rows_used * card_h

    def _draw_card(
        self,
        put: Any,
        view: HomeView,
        shelf: Shelf,
        shelf_index: int,
        track: Track,
        card_index: int,
        m: Metrics,
        row: int,
        x: int,
        width: int,
        height: int,
    ) -> None:
        """One card: marker, title, detail — all registered as clickable.

        The registered action depends on what the card *is*: a station starts a
        station, an artist opens its page, a track plays.
        """
        if width <= 0 or height <= 0:
            return
        selected = (
            view.selected_shelf == shelf_index and view.selected_card == card_index
        )

        title_style = m.style(Role.TITLE if selected else Role.BODY)
        detail_style = m.style(Role.META)
        reason_style = m.style(Role.MUTED)

        marker = styled(False, "accent", SELECTED_MARK if selected else " ")
        title = crop(track.title, max(1, width - 2))
        put(row, x, marker)
        put(
            row,
            x + 1,
            styled(title_style.bold, "accent" if selected else title_style.emphasis, title),
        )

        detail = track.artist or track.reason
        if detail and height >= 2:
            put(
                row + 1,
                x + 1,
                styled(False, detail_style.emphasis, crop(detail, max(1, width - 2))),
            )
        if height >= 3 and track.artist and track.reason:
            put(
                row + 2,
                x + 1,
                styled(False, reason_style.emphasis, crop(track.reason, max(1, width - 2))),
            )

        if track.kind == "artist":
            action = self.actions.open_artist
        else:
            action = self.actions.play

        # Card ids must be unique across the whole screen: a favourited track
        # also appears in "recent", and station ids already contain ':'. Using
        # the bare track id would let a later shelf overwrite an earlier one's
        # rect, making that card silently unclickable.
        card_id = f"card:{shelf.id}/{track.id}"
        rect = self._element(
            card_id,
            row,
            x,
            width,
            action,
            args=(track,),
            keys=("enter",),
            height=height,
        )
        if rect is not None:
            view.card_rects[card_id] = rect

        # A heart only appears where favouriting is meaningful, so it is never
        # a decoration that does nothing.
        if track.action == "play_id" and width >= 12:
            self._element(
                f"favourite:{shelf.id}/{track.id}",
                row,
                x + width - 1,
                1,
                self.actions.toggle_favourite,
                args=(track,),
                keys=("f",),
            )

    # -- affordance audit --
    def audit(self) -> AffordanceReport:
        """Check the registered regions against the core rule."""
        return AffordanceReport(
            drawn=self.interactive.ids(),
            dead=self.interactive.dead_affordances(),
            mouse_only=self.interactive.missing_keyboard_parity(),
        )

    def click(self, px: int, py: int) -> Any:
        """Run the action under a click, if there is one."""
        region = self.interactive.at(px, py)
        if region is None or not region.action:
            return None
        return region.action(*region.args)

    def card_at(self, px: int, py: int) -> Track | None:
        """Which track card (if any) a click landed on."""
        for card_id, rect in self._last_view_cards().items():
            if rect.contains(px, py):
                return self._last_view_tracks().get(card_id)
        return None

    # The most recent frame's view, kept so card_at() can map a rect to a track
    # without the caller re-supplying the whole shelf list on every click.
    _view: HomeView | None = None

    def _last_view_cards(self) -> dict[str, Rect]:
        return self._view.card_rects if self._view else {}

    def _last_view_tracks(self) -> dict[str, Track]:
        """Qualified card id -> Track, mirroring card_rects' keying."""
        if not self._view:
            return {}
        return {
            card_key(shelf.id, track.id): track
            for shelf in self._view.shelves
            for track in shelf.tracks
        }


def card_key(shelf_id: str, track_id: str) -> str:
    """The screen-unique id for one card."""
    return f"card:{shelf_id}/{track_id}"


def attach_view(drawer: SurfaceDrawer, view: HomeView) -> None:
    """Remember which view the drawer last drew, for click resolution."""
    drawer._view = view
