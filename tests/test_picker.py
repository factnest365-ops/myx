import time

import myx.picker as picker
from myx.picker import (
    COMMAND_PALETTE,
    KEYBINDS,
    TOUR_STEPS,
    Debouncer,
    PickerState,
    command_palette_search,
    format_duration,
    format_result_line,
    fuzzy_filter,
    fuzzy_score,
    should_search,
)


def test_fuzzy_filter_scoring():
    items = ["hello world", "hello", "hallo", "goodbye", "help"]
    # exact substring should rank highest
    filtered = fuzzy_filter("hello", items)
    assert filtered[0] == "hello world" or filtered[0] == "hello"
    assert "hello" in filtered
    assert "goodbye" not in filtered  # low score filtered
    # typo tolerance: "helo" should still match "hello" and "hello world"
    typo = fuzzy_filter("helo", items, threshold=0.3)
    assert "hello" in typo
    # empty query preserves order
    assert fuzzy_filter("", items) == items
    assert fuzzy_filter("   ", items) == items
    # with key function
    dicts = [{"title": "Bohemian Rhapsody"}, {"title": "Bohemian"}, {"title": "Rap God"}]
    res = fuzzy_filter("bohemian", dicts, key=lambda d: d["title"])
    assert res[0]["title"] in ("Bohemian Rhapsody", "Bohemian")
    assert len(res) == 2


def test_fuzzy_score_ordering():
    # substring and typo behavior
    assert fuzzy_score("week", "The Weeknd") >= 0.7
    assert fuzzy_score("week", "Weekday Jog") >= 0.7
    # prefix boost — 'the' as prefix should still score well
    assert fuzzy_score("the", "The Weeknd") >= 0.7
    # empty query is neutral
    assert fuzzy_score("", "anything") == 1.0
    # high score for exact
    assert fuzzy_score("hello", "hello") == 1.0
    # typo still decent
    assert fuzzy_score("helo", "hello") > 0.6
    assert fuzzy_score("xyz", "hello") < 0.4


def test_debouncer_or_picker_state_transitions():
    # should_search logic
    assert should_search(0.0, 0.3, debounce_ms=250) is True
    assert should_search(0.0, 0.1, debounce_ms=250) is False
    assert should_search(1.0, 1.25, debounce_ms=250) is True

    d = Debouncer(delay_ms=250)
    d.mark(now=0.0)
    assert d.ready(now=0.1) is False
    assert d.ready(now=0.3) is True

    # PickerState transitions
    ps = PickerState()
    ps.set_query("hel", now=0.0)
    assert ps.query == "hel"
    assert ps.selected == 0
    assert ps.should_search_now(now=0.1) is False
    assert ps.should_search_now(now=0.3) is True

    ps.set_results([{"id": "1", "title": "Hello"}, {"id": "2", "title": "Help"}])
    assert ps.loading is False
    assert len(ps.results) == 2
    assert ps.selected_result()["id"] == "1"
    ps.move(1)
    assert ps.selected == 1
    assert ps.selected_result()["id"] == "2"
    ps.move(10)
    assert ps.selected == 1  # clamped
    ps.move(-10)
    assert ps.selected == 0
    ps.backspace(now=1.0)
    assert ps.query == "he"
    ps.append_char("y", now=1.1)
    assert ps.query == "hey"
    ps.clear()
    assert ps.query == ""
    assert ps.results == []
    ps.set_error("oops")
    assert ps.error == "oops"
    assert ps.loading is False
    ps.set_loading(True)
    assert ps.loading is True
    assert ps.error is None


def test_command_palette_search():
    # empty returns all
    all_cmds = command_palette_search("")
    assert len(all_cmds) == len(COMMAND_PALETTE)
    # fuzzy search for play
    res = command_palette_search("play")
    ids = [r["id"] for r in res]
    assert "play" in ids
    # search for volume
    res2 = command_palette_search("vol")
    ids2 = [r["id"] for r in res2]
    assert "vol_up" in ids2 or "vol_down" in ids2
    # nonsense yields empty
    assert command_palette_search("zzzzzzzzzzz") == []
    # case insensitive
    assert command_palette_search("SHUFFLE")[0]["id"] == "shuffle"


def test_format_helpers():
    assert format_duration(0) == "0:00"
    assert format_duration(65) == "1:05"
    assert format_duration("125") == "2:05"
    assert format_duration(None) == "0:00"
    assert format_duration("bad") == "0:00"
    line = format_result_line({"title": "Hello", "artist": "Adele", "duration": 125})
    assert "Hello" in line
    assert "Adele" in line
    assert "2:05" in line
    # missing artist
    line2 = format_result_line({"title": "Solo", "duration": 60})
    assert "Solo" in line2


def test_help_and_tour_content():
    assert len(TOUR_STEPS) == 3
    assert all("title" in s and "description" in s for s in TOUR_STEPS)
    assert TOUR_STEPS[0]["title"].startswith("Step 1")
    assert len(KEYBINDS) >= 8
    assert any(k["keys"] == "?" for k in KEYBINDS)


# --- TUI picker overlay hermetic tests (no mpv/network) ---

def test_tui_search_picker_overlay_and_typing(monkeypatch):
    from myx import tui
    import io

    # mock send_command to avoid socket
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: {"ok": True, "queued": 1})

    r = tui.Renderer(out=io.StringIO())
    # empty state check: should be considered empty
    empty = {"playing": False, "title": "MYX", "queue": []}
    assert r._is_empty_state(empty) is True
    nonempty = {"playing": True, "title": "Song", "queue": [{"id": "1"}]}
    assert r._is_empty_state(nonempty) is False

    # open search via method
    r.open_search()
    assert r.overlay == "search"
    assert r.picker.query == ""

    # typing appends
    r.handle_key("h")
    r.handle_key("i")
    assert r.picker.query == "hi"

    # backspace
    r.handle_key("\x7f")
    assert r.picker.query == "h"

    # arrow nav with results
    r.picker.set_results([{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}])
    r.handle_key("\x1b[B")  # down
    assert r.picker.selected == 1
    r.handle_key("\x1b[A")  # up
    assert r.picker.selected == 0

    # esc closes
    r.handle_key("\x1b")
    assert r.overlay is None

    # auto-enter search on printable when empty state: simulate typing in empty state
    r2 = tui.Renderer(out=io.StringIO())
    r2.state_path = r.state_path  # keep same tmp
    # monkeypatch read_state to return empty so _is_empty_state triggers
    monkeypatch.setattr(tui, "read_state", lambda p=None: empty)
    # handle printable 'a' should open search
    r2.handle_key("a")
    assert r2.overlay == "search"
    assert r2.picker.query == "a"


def test_tui_command_palette_and_help_toggle(monkeypatch):
    from myx import tui
    import io

    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: {"ok": True})

    r = tui.Renderer(out=io.StringIO())
    # ctrl-k opens command palette
    r.handle_key("\x0b")
    assert r.overlay == "command"
    r.handle_key("s")
    assert r.command_query == "s"
    filtered = command_palette_search("s")
    assert len(filtered) > 0
    # escape closes
    r.handle_key("\x1b")
    assert r.overlay is None

    # ? opens help
    r.handle_key("?")
    assert r.overlay == "help"
    r.handle_key("?")
    assert r.overlay is None
    r.handle_key("?")
    assert r.overlay == "help"
    r.handle_key("\x1b")
    assert r.overlay is None


def test_tui_queue_delete_confirm(monkeypatch):
    from myx import tui
    import io

    calls = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: (calls.append((cmd, kw)), {"ok": True})[1])
    # ensure non-empty state so 'd' is not hijacked as search
    monkeypatch.setattr(tui, "read_state", lambda p=None: {"playing": True, "title": "Song", "queue": [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}]})

    r = tui.Renderer(out=io.StringIO())
    r.selected = 1
    # first d should not delete, just toast
    r.handle_key("d")
    assert r.toast == "Press d again to confirm delete"
    assert not any(c[0] == "del" for c in calls)
    # second d within window should delete
    r.handle_key("d")
    assert any(c[0] == "del" for c in calls)


def test_tui_footer_and_controls_visible(monkeypatch):
    from myx import tui
    import io

    r = tui.Renderer(out=io.StringIO())
    state = {
        "version": 1,
        "playing": True,
        "paused": False,
        "title": "Test Song",
        "artist": "Artist",
        "id": "abc",
        "thumb": None,
        "duration": 200,
        "position": 10,
        "volume": 80,
        "queue": [{"id": "2", "title": "Next", "artist": "Other", "duration": 180, "thumb": None}],
    }
    frame = r.render_once(state=state, size=(120, 32))
    assert tui.FOOTER in frame
    assert "type to search" in frame
    assert "M Y X" in frame


def test_tui_search_picker_render_contains_placeholder(monkeypatch):
    from myx import tui
    import io

    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: {"ok": True})
    r = tui.Renderer(out=io.StringIO())
    r.open_search()
    frame = r.render_once(state={"playing": False, "title": "MYX", "queue": []}, size=(100, 32))
    assert "Search" in frame
    assert "Search for songs" in frame or "Search \u203a" in frame

    # with results, titles appear
    r.picker.set_results([
        {"id": "1", "title": "Bohemian Rhapsody", "artist": "Queen", "duration": 355, "thumb": None},
        {"id": "2", "title": "Stairway to Heaven", "artist": "Led Zeppelin", "duration": 482, "thumb": None},
    ])
    frame2 = r.render_once(state={"playing": False, "title": "MYX", "queue": []}, size=(100, 32))
    assert "Bohemian Rhapsody" in frame2
    assert "Queen" in frame2


def test_tui_help_overlay_renders_tour(monkeypatch):
    from myx import tui
    import io

    r = tui.Renderer(out=io.StringIO())
    r.open_help()
    frame = r.render_once(state={"playing": True, "title": "Song", "queue": []}, size=(100, 32))
    assert "Help" in frame
    assert "Step 1" in frame
    assert "Step 2" in frame
    assert "Step 3" in frame


def test_tui_command_palette_render_filters(monkeypatch):
    from myx import tui
    import io

    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: {"ok": True})
    r = tui.Renderer(out=io.StringIO())
    r.open_command_palette()
    r.command_query = "shuf"
    frame = r.render_once(state={"playing": True, "title": "Song", "queue": []}, size=(100, 32))
    assert "Shuffle" in frame


def test_is_empty_shows_hint(monkeypatch):
    from myx import tui
    import io

    r = tui.Renderer(out=io.StringIO())
    # empty state should produce updating hint in frame (as toast)
    frame = r.render_once(state={"playing": False, "title": "MYX", "queue": []}, size=(100, 32))
    assert "Search above to add songs" in frame or "Search to add songs" in frame
