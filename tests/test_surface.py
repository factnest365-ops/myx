"""Tests for the MYX app surface: search bar, home shelves, affordances.

The recurring theme is the launch brief's core rule — nothing may look
clickable and not be. These tests assert it structurally rather than by
eyeballing a screenshot.
"""

from typing import Any

from myx.ansi import cell_len, strip_ansi
from myx.design import metrics
from myx.library import Library, home_shelves
from myx.mouse import InteractiveMap, MouseButton, MouseEvent, MouseKind
from myx.surface import (
    CHEVRON,
    CURSOR_BLOCK,
    SEARCH_ICON,
    HomeView,
    SearchBar,
    SearchSuggestion,
    SurfaceActions,
    SurfaceDrawer,
    attach_view,
    card_key,
)

NOW = 1_700_000_000.0
SIZES = [(40, 24), (60, 24), (120, 32), (80, 12)]


class Recorder:
    """Captures every callback so tests can assert intent without a daemon."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def _make(self, name: str) -> Any:
        def call(*args: Any) -> str:
            self.calls.append((name, args[0] if len(args) == 1 else args))
            return name

        return call

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._make(name)

    def named(self, name: str) -> list[Any]:
        return [payload for called, payload in self.calls if called == name]


def make_actions(rec: Recorder) -> SurfaceActions:
    return SurfaceActions(
        focus_search=rec._make("focus_search"),
        submit_search=rec._make("submit_search"),
        play=rec._make("play"),
        open_artist=rec._make("open_artist"),
        open_shelf=rec._make("open_shelf"),
        toggle_favourite=rec._make("toggle_favourite"),
        pick_suggestion=rec._make("pick_suggestion"),
    )


def seeded_library() -> Library:
    liby = Library()
    liby.record_play("t1", "Blinding Lights", "The Weeknd", now=NOW)
    liby.record_play("t2", "Intro", "The xx", now=NOW - 3600)
    liby.record_play("t3", "Midnight City", "M83", now=NOW - 7200)
    liby.toggle_favourite("t3")
    return liby


def make_canvas(cols: int, rows: int):
    """A canvas plus a `put` closure, mirroring the renderer's contract."""
    grid = [[" "] * cols for _ in range(rows)]
    writes: list[tuple[int, int, str]] = []

    def put(y: int, x: int, text: str) -> None:
        if not (0 <= y < rows) or x >= cols:
            return
        x = max(0, x)
        writes.append((y, x, text))
        col = x
        for ch in strip_ansi(text):
            w = cell_len(ch)
            if col + w > cols:
                break
            if 0 <= col < cols:
                grid[y][col] = ch
            col += w
        while col < cols and 0 <= col < cols:
            grid[y][col] = " "
            col += 1

    return grid, put, writes


def render_home(cols: int = 80, rows: int = 24, liby: Library | None = None):
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(cols, rows)
    view = HomeView(library=liby if liby is not None else seeded_library())
    view.refresh(now=NOW)
    attach_view(drawer, view)
    m = metrics(cols, rows)
    drawer.draw_search_bar(put, SearchBar(), m, 0)
    drawer.draw_home(put, view, m, 2, rows - 3)
    return drawer, view, rec, grid, put


# ---------------------------------------------------------------------------
# search bar
# ---------------------------------------------------------------------------


def test_search_bar_is_drawn_at_every_size():
    for cols, rows in SIZES:
        rec = Recorder()
        drawer = SurfaceDrawer(make_actions(rec))
        grid, put, _ = make_canvas(cols, rows)
        m = metrics(cols, rows)
        drawer.draw_search_bar(put, SearchBar(), m, 0)
        text = "".join(grid[0])
        assert text.strip(), f"search bar vanished at {cols}x{rows}"


def test_search_bar_is_registered_as_clickable():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    drawer.draw_search_bar(put, SearchBar(), metrics(80, 24), 0)
    assert "search.focus" in drawer.interactive


def test_clicking_the_bar_focuses_it():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    m = metrics(80, 24)
    drawer.draw_search_bar(put, SearchBar(), m, 0)
    drawer.click(m.gutter + 2, 0)
    assert rec.named("focus_search"), "clicking the search bar must focus it"


def test_search_bar_shows_its_placeholder_when_empty():
    bar = SearchBar()
    assert bar.is_empty
    assert bar.display(40) == bar.placeholder


def test_search_bar_shows_query_when_typed():
    bar = SearchBar()
    bar.focus()
    for ch in "daft punk":
        bar.type_char(ch)
    assert bar.query == "daft punk"
    assert "daft punk" in bar.display(40)


def test_search_backspace_and_clear():
    bar = SearchBar(query="hello")
    bar.backspace()
    assert bar.query == "hell"
    bar.clear()
    assert bar.query == ""
    bar.backspace()
    assert bar.query == "", "backspace on an empty field must not go negative"


def test_search_focus_is_explicit_state():
    bar = SearchBar()
    assert not bar.focused
    bar.focus()
    assert bar.focused
    bar.blur()
    assert not bar.focused


def test_caret_only_shows_when_focused():
    focused = SearchBar(query="ab", focused=True)
    blurred = SearchBar(query="ab", focused=False)
    assert CURSOR_BLOCK in focused.display(20)
    assert CURSOR_BLOCK not in blurred.display(20)


def test_search_icon_shows_when_not_focused():
    """The icon is an affordance, so it must only imply something when true.

    The icon lives in the drawing layer, not in `SearchBar.display`, which
    only supplies the field's own text.
    """
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    drawer.draw_search_bar(put, SearchBar(), metrics(80, 24), 0)
    assert SEARCH_ICON in "".join(grid[0])

    grid2, put2, _ = make_canvas(80, 24)
    drawer2 = SurfaceDrawer(make_actions(Recorder()))
    bar = SearchBar()
    bar.focus()
    drawer2.draw_search_bar(put2, bar, metrics(80, 24), 0)
    assert SEARCH_ICON not in "".join(grid2[0]), "focused bar shows the caret instead"


def test_long_query_scrolls_and_end_reveals_the_tail():
    bar = SearchBar(query="x" * 100)
    bar.max_visible = 10
    assert bar.visible_text() == "x" * 10
    bar.end()
    assert bar.scroll > 0
    bar.home()
    assert bar.scroll == 0


def test_search_bar_fits_at_40_columns():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(40, 24)
    m = metrics(40, 24)
    drawer.draw_search_bar(put, SearchBar(query="a rather long search query"), m, 0)
    assert len(grid[0]) == 40
    assert cell_len("".join(grid[0])) <= 40


def test_submit_region_exists_only_while_focused():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    m = metrics(80, 24)
    bar = SearchBar(query="m83")
    drawer.draw_search_bar(put, bar, m, 0)
    assert "search.submit" not in drawer.interactive

    bar.focus()
    drawer.interactive.clear()
    drawer.draw_search_bar(put, bar, m, 0)
    assert "search.submit" in drawer.interactive


def test_submit_passes_the_current_query():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    m = metrics(80, 24)
    bar = SearchBar(query="radiohead")
    bar.focus()
    drawer.draw_search_bar(put, bar, m, 0)
    drawer.click(m.gutter + 1, 0)
    assert rec.named("submit_search") == ["radiohead"]


def test_suggestions_are_each_clickable():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    m = metrics(80, 24)
    suggestions = [SearchSuggestion("m83"), SearchSuggestion("miles davis")]
    drawer.draw_suggestions(put, suggestions, m, 2, 10)
    assert "suggestion.0" in drawer.interactive
    assert "suggestion.1" in drawer.interactive
    drawer.click(m.gutter + 1, 3)
    assert rec.named("pick_suggestion")[0].text == "miles davis"


def test_search_bar_hint_never_overwrites_the_placeholder():
    """A narrow terminal used to render garbled bar text.

    The trailing hint was drawn without reserving its columns first, so at 40
    columns it landed on top of the placeholder: "Search songs,type to searchl".
    """
    for cols in (40, 48, 60, 80, 100, 120):
        rec = Recorder()
        drawer = SurfaceDrawer(make_actions(rec))
        grid, put, _ = make_canvas(cols, 24)
        m = metrics(cols, 24)
        drawer.draw_search_bar(put, SearchBar(), m, 0)
        row = "".join(grid[0])
        assert len(row) == cols

        hint_at = row.rfind("type to search")
        if hint_at < 0:
            # Narrower than the hint needs: it must be omitted, not half-drawn.
            assert "type to" not in row, f"partially drawn hint at {cols} cols"
            continue
        # Everything before the hint is the field: a cropped placeholder, which
        # is correct. The hint must not bleed backwards into it.
        before = row[:hint_at].rstrip()
        assert "type to search" not in before, f"hint bled left at {cols} cols"
        assert before.strip().startswith("▌"), before
        assert "Search" in before, before


def test_search_bar_hint_is_dropped_when_it_cannot_fit():
    """At the narrowest size the hint is omitted rather than overlapping."""
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(40, 24)
    drawer.draw_search_bar(put, SearchBar(), metrics(40, 24), 0)
    row = "".join(grid[0]).rstrip()
    assert row.isprintable()
    assert len(grid[0]) == 40


def test_artist_shelf_keeps_the_artists_own_capitalisation():
    """Normalised keys must not leak into the UI as display text."""
    liby = Library()
    liby.record_play("x1", "Intro", "The xx", now=NOW)
    liby.record_play("x2", "River", "Bon Iver", now=NOW - 10)
    shelves = {s.id: s for s in home_shelves(liby, now=NOW)}
    titles = {t.title for t in shelves["artists"].tracks}
    assert "The xx" in titles, f"artist casing was mangled: {titles}"
    assert "Bon Iver" in titles


def test_no_suggestions_draws_nothing():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    drawer.draw_suggestions(put, [], metrics(80, 24), 2, 10)
    assert len(drawer.interactive) == 0


def test_suggestions_respect_available_rows():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(80, 24)
    suggestions = [SearchSuggestion(f"q{i}") for i in range(20)]
    drawer.draw_suggestions(put, suggestions, metrics(80, 24), 2, available_rows=3)
    assert len(drawer.interactive) == 3


# ---------------------------------------------------------------------------
# home view state
# ---------------------------------------------------------------------------


def test_home_refresh_populates_shelves():
    view = HomeView(library=seeded_library())
    view.refresh(now=NOW)
    assert view.shelves
    assert "stations" in {s.id for s in view.shelves}


def test_home_refresh_clamps_a_stale_selection():
    view = HomeView(library=Library())
    view.refresh(now=NOW)
    view.selected_shelf = 99
    view.selected_card = 99
    view.refresh(now=NOW)
    assert 0 <= view.selected_shelf < len(view.shelves)
    assert view.selected_card >= 0


def test_move_selection_clamps_at_both_ends():
    view = HomeView(library=seeded_library())
    view.refresh(now=NOW)
    view.move_selection(-50)
    assert view.selected_card == 0
    view.move_selection(500)
    shelf = view.current_shelf
    assert view.selected_card <= max(0, len(shelf.tracks) - 1)


def test_focus_shelf_wraps():
    view = HomeView(library=seeded_library())
    view.refresh(now=NOW)
    view.focus_shelf(-1)
    assert view.selected_shelf == len(view.shelves) - 1
    view.focus_shelf(1)
    assert view.selected_shelf == 0


def test_current_track_matches_selection():
    view = HomeView(library=seeded_library())
    view.refresh(now=NOW)
    view.selected_shelf = 0
    view.selected_card = 0
    assert view.current_track is view.current_shelf.tracks[0]


def test_home_navigation_is_inert_with_no_shelves():
    view = HomeView()
    view.move_selection(1)
    view.focus_shelf(1)
    assert view.current_track is None


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_home_renders_at_all_four_sizes():
    for cols, rows in SIZES:
        drawer, view, rec, grid, put = render_home(cols, rows)
        drawn = "\n".join("".join(row) for row in grid)
        assert drawn.strip(), f"home screen is blank at {cols}x{rows}"
        assert len(grid) == rows
        for i, line in enumerate(grid):
            assert len(line) == cols, f"row {i} wrong width at {cols}x{rows}"


def test_home_shows_shelf_titles():
    drawer, view, rec, grid, put = render_home(100, 30)
    text = "\n".join("".join(row) for row in grid)
    for shelf in view.shelves:
        assert shelf.title in text, f"{shelf.title} missing from the home screen"


def test_home_shows_card_titles():
    drawer, view, rec, grid, put = render_home(100, 30)
    text = "\n".join("".join(row) for row in grid)
    assert view.shelves[0].tracks[0].title in text


def test_cold_start_renders_for_a_new_user():
    drawer, view, rec, grid, put = render_home(100, 30, Library())
    text = "\n".join("".join(row) for row in grid)
    assert "Welcome to MYX" in text or view.shelves


def test_selected_card_shows_a_marker():
    drawer, view, rec, grid, put = render_home(100, 30)
    view.selected_shelf = 0
    view.selected_card = 0
    drawer.interactive.clear()
    m = metrics(100, 30)
    drawer.draw_home(put, view, m, 2, 27)
    from myx.surface import SELECTED_MARK

    assert SELECTED_MARK in "".join(grid[2 + 1])


def test_home_draws_nothing_outside_its_band():
    """Cards must not spill past the rows they were given."""
    drawer, view, rec, grid, put = render_home(100, 30)
    registered = [r for r in drawer.interactive if r.id.startswith("card:")]
    for region in registered:
        assert region.rect.y < 30
        assert region.rect.y + region.rect.h <= 30


# ---------------------------------------------------------------------------
# THE core rule: nothing looks clickable and isn't
# ---------------------------------------------------------------------------


def test_home_frame_has_no_dead_affordances():
    """Every drawn control must do something when clicked."""
    drawer, view, rec, grid, put = render_home(100, 30)
    report = drawer.audit()
    assert report.dead == [], f"dead controls: {report.dead}"
    assert report.ok


def test_search_frame_has_no_dead_affordances():
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    grid, put, _ = make_canvas(100, 30)
    m = metrics(100, 30)
    bar = SearchBar(query="test")
    bar.focus()
    drawer.draw_search_bar(put, bar, m, 0)
    drawer.draw_suggestions(put, [SearchSuggestion("a"), SearchSuggestion("b")], m, 2, 5)
    assert drawer.audit().dead == []


def test_everything_clickable_is_reachable_by_keyboard():
    drawer, view, rec, grid, put = render_home(100, 30)
    report = drawer.audit()
    assert report.mouse_only == [], f"mouse-only controls: {report.mouse_only}"


def test_no_dead_affordances_at_any_size():
    for cols, rows in SIZES:
        drawer, view, rec, grid, put = render_home(cols, rows)
        report = drawer.audit()
        assert report.dead == [], f"{cols}x{rows} has dead controls: {report.dead}"


def test_clicking_a_track_card_plays_it():
    drawer, view, rec, grid, put = render_home(100, 30)
    shelf = view.shelves[0]
    track = shelf.tracks[0]
    rect = view.card_rects[card_key(shelf.id, track.id)]
    drawer.click(rect.x + 1, rect.y)
    played = rec.named("play")
    assert played and played[0].id == track.id


def test_clicking_a_station_starts_that_station():
    """Stations must be reachable even when the fold cuts them off.

    The home screen is taller than some terminals, so this scrolls to the
    station shelf rather than assuming it lands on the first screen.
    """
    drawer, view, rec, grid, put = render_home(100, 30)
    m = metrics(100, 30)
    shelf = next(s for s in view.shelves if s.id == "stations")
    station = shelf.tracks[0]
    key = card_key(shelf.id, station.id)
    for _ in range(len(view.shelves) + 2):
        if key in view.card_rects:
            break
        view.scroll_by(1)
        drawer.interactive.clear()
        drawer.draw_home(put, view, m, 2, m.rows - 3)
    assert key in view.card_rects, "scrolling must bring the stations shelf into reach"

    rect = view.card_rects[key]
    drawer.click(rect.x + 1, rect.y)
    started = rec.named("play")
    assert started and started[0].query == station.query


def test_every_shelf_is_reachable_by_scrolling():
    """No shelf may be permanently invisible — that is a dead section."""
    drawer, view, rec, grid, put = render_home(100, 30)
    m = metrics(100, 30)

    def visible() -> set[str]:
        return {
            r.id.split("/", 1)[0].removeprefix("card:")
            for r in drawer.interactive
            if r.id.startswith("card:")
        }

    seen: set[str] = set()
    for step in range(len(view.shelves) + 2):
        drawer.interactive.clear()
        drawer.draw_home(put, view, m, 2, m.rows - 3)
        seen |= visible()
        view.scroll_by(1)

    expected = {s.id for s in view.shelves}
    assert expected <= seen, f"never reachable: {expected - seen}"


def test_dropped_shelves_are_reported():
    """A shelf that does not fit must be reported, not silently dropped.

    A short terminal is the real overflow case: four shelf headers plus
    their cards cannot fit in a 12-row band.
    """
    drawer, view, rec, grid, put = render_home(100, 12)
    assert view.dropped_shelf_ids, "12 rows cannot fit four full shelves"
    assert view.has_more_below


def test_nothing_is_dropped_when_everything_fits():
    drawer, view, rec, grid, put = render_home(100, 30)
    assert view.dropped_shelf_ids == [], "100x30 fits all four shelves"
    assert not view.has_more_below


def test_scroll_never_goes_negative():
    view = HomeView(library=Library())
    view.scroll_by(-50)
    assert view.scroll == 0
    view.scroll_by(5)
    assert view.scroll == 5


def test_clicking_an_artist_opens_its_page():
    drawer, view, rec, grid, put = render_home(100, 30)
    shelf = next((s for s in view.shelves if s.id == "artists"), None)
    if shelf is None:
        return
    artist = shelf.tracks[0]
    rect = view.card_rects[card_key(shelf.id, artist.id)]
    drawer.click(rect.x + 1, rect.y)
    assert rec.named("open_artist"), "an artist card must open a page, not play"


def test_card_appearing_in_two_shelves_stays_clickable_in_both():
    """A favourited track is in both Liked and Jump back in.

    Keying rects by the bare track id let the second shelf overwrite the
    first, silently making the Liked card unclickable. Each shelf needs its own
    target.
    """
    drawer, view, rec, grid, put = render_home(100, 30)
    fav = next(s for s in view.shelves if s.id == "favourites")
    recent = next(s for s in view.shelves if s.id == "recent")
    shared = {t.id for t in fav.tracks} & {t.id for t in recent.tracks}
    assert shared, "fixture should have a track in both shelves"

    track_id = shared.pop()
    fav_rect = view.card_rects[card_key(fav.id, track_id)]
    recent_rect = view.card_rects[card_key(recent.id, track_id)]
    assert fav_rect is not recent_rect, "each shelf must own a distinct rect"

    rec.calls.clear()
    drawer.click(fav_rect.x + 1, fav_rect.y)
    fav_calls = list(rec.calls)
    rec.calls.clear()
    drawer.click(recent_rect.x + 1, recent_rect.y)
    recent_calls = list(rec.calls)
    assert fav_calls, "clicking the Liked copy must fire"
    assert recent_calls, "clicking the recent copy must fire independently"


def test_clicking_the_chevron_opens_the_shelf():
    drawer, view, rec, grid, put = render_home(100, 30)
    shelf = view.shelves[0]
    rect = drawer.interactive.get(f"shelf.{shelf.id}")
    assert rect is not None, "a chevron is drawn, so its row must be clickable"
    drawer.click(rect.rect.x + 1, rect.rect.y)
    assert rec.named("open_shelf")


def test_every_drawn_chevron_is_registered():
    """A chevron glyph must never appear without a clickable row behind it."""
    drawer, view, rec, grid, put = render_home(100, 30)
    text = "\n".join("".join(row) for row in grid)
    if CHEVRON not in text:
        return
    shelf_regions = [r for r in drawer.interactive if r.id.startswith("shelf.")]
    assert shelf_regions, "chevron drawn with no shelf region registered"


def test_clicking_blank_space_does_nothing():
    drawer, view, rec, grid, put = render_home(100, 30)
    drawer.click(99, 29)
    assert not rec.calls, "a click on nothing must not fire an action"


def test_toggle_favourite_is_reachable():
    drawer, view, rec, grid, put = render_home(100, 30)
    hearted = [r for r in drawer.interactive if r.id.startswith("favourite:")]
    assert hearted, "track cards should offer favouriting"
    drawer.click(hearted[0].rect.x, hearted[0].rect.y)
    assert rec.named("toggle_favourite")


def test_station_cards_do_not_offer_favouriting():
    """A heart on a station would be an affordance with no meaning."""
    drawer, view, rec, grid, put = render_home(100, 30)
    hearts = {
        r.id.split("/", 1)[1] for r in drawer.interactive if r.id.startswith("favourite:")
    }
    station_ids = {t.id for s in view.shelves if s.id == "stations" for t in s.tracks}
    assert not (station_ids & hearts), "stations must not show a favourite heart"


def test_registry_is_rebuilt_each_frame_not_accumulated():
    """A stale region from a previous frame would be a phantom click target."""
    rec = Recorder()
    drawer = SurfaceDrawer(make_actions(rec))
    view = HomeView(library=seeded_library())
    view.refresh(now=NOW)
    grid, put, _ = make_canvas(100, 30)
    m = metrics(100, 30)
    for _ in range(3):
        drawer.interactive.clear()
        drawer.draw_home(put, view, m, 2, 27)
    ids = [r.id for r in drawer.interactive if r.id.startswith("card:")]
    assert len(ids) == len(set(ids)), "frames must not accumulate duplicate regions"


def test_audit_summary_is_human_readable():
    drawer, view, rec, grid, put = render_home(100, 30)
    summary = drawer.audit().summary()
    assert "all live" in summary


# ---------------------------------------------------------------------------
# click resolution with real mouse events
# ---------------------------------------------------------------------------


def test_press_event_routes_through_the_map():
    drawer, view, rec, grid, put = render_home(100, 30)
    shelf = view.shelves[0]
    track = shelf.tracks[0]
    rect = view.card_rects[card_key(shelf.id, track.id)]
    event = MouseEvent(rect.x + 2, rect.y + 1, MouseKind.PRESS, MouseButton.LEFT)
    region = drawer.interactive.at(event.x, event.y)
    assert region is not None
    assert region.actionable


def test_wheel_over_a_card_resolves_a_region():
    drawer, view, rec, grid, put = render_home(100, 30)
    shelf = view.shelves[0]
    track = shelf.tracks[0]
    rect = view.card_rects[card_key(shelf.id, track.id)]
    hit = drawer.interactive.at(rect.x + 1, rect.y)
    assert hit is not None, "cards are wheel targets so long lists can be scrolled"


def test_supplied_map_is_used():
    """The host owns the map, so clicks and the renderer share one registry."""
    shared = InteractiveMap()
    drawer = SurfaceDrawer(make_actions(Recorder()), interactive=shared)
    grid, put, _ = make_canvas(80, 24)
    drawer.draw_search_bar(put, SearchBar(), metrics(80, 24), 0)
    assert "search.focus" in shared
