"""Wire-up tests: `myx` with no arguments opens For You, and works.

These are the launch-brief acceptance criteria exercised through the real
Renderer and the real CLI decision, not the isolated modules.
"""

import io
import re
from pathlib import Path

from myx import cli, tui
from myx.library import Library
from myx.mouse import (
    DISABLE_MOUSE,
    ENABLE_MOUSE,
    MouseButton,
    MouseEvent,
    MouseKind,
    Rect,
)

_ANSI_INDEP = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b_[^\x1b]*\x1b\\"
)

SIZES = [(40, 24), (60, 24), (120, 32), (80, 12)]
EMPTY_STATE = {"version": 1, "playing": False, "title": "MYX", "queue": []}


def disp_width(line: str) -> int:
    import unicodedata

    plain = _ANSI_INDEP.sub("", line)
    return sum(
        0
        if unicodedata.combining(ch)
        else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        for ch in plain
    )


def seeded() -> Library:
    liby = Library()
    liby.record_play("t1", "Blinding Lights", "The Weeknd")
    liby.record_play("t2", "Midnight City", "M83")
    liby.record_play("t3", "Intro", "The xx")
    liby.toggle_favourite("t2")
    return liby


def press(px: int, py: int) -> MouseEvent:
    """A left press at a 0-based cell.

    MouseEvent takes 1-based terminal coordinates (as real SGR reports do),
    so tests express positions in the renderer's 0-based space and convert
    here rather than silently testing the wrong cell.
    """
    return MouseEvent(px + 1, py + 1, MouseKind.PRESS, MouseButton.LEFT)


def release(px: int, py: int) -> MouseEvent:
    return MouseEvent(px + 1, py + 1, MouseKind.RELEASE, MouseButton.LEFT)


def wheel(px: int, py: int, down: bool = True) -> MouseEvent:
    kind = MouseKind.WHEEL_DOWN if down else MouseKind.WHEEL_UP
    return MouseEvent(px + 1, py + 1, kind, MouseButton.NONE)


def drag(px: int, py: int) -> MouseEvent:
    return MouseEvent(px + 1, py + 1, MouseKind.DRAG, MouseButton.LEFT)


def home_renderer(liby: Library | None = None) -> tui.Renderer:
    r = tui.Renderer(out=io.StringIO())
    r.show_home = True
    r.home.library = liby if liby is not None else seeded()
    return r


# ---------------------------------------------------------------------------
# acceptance 1: no-arg launch opens For You at every size
# ---------------------------------------------------------------------------


def test_home_renders_at_all_four_terminal_sizes():
    for cols, rows in SIZES:
        r = home_renderer()
        frame = r.render_once(state=EMPTY_STATE, size=(cols, rows))
        lines = frame.split("\n")
        assert len(lines) == rows, f"expected {rows} rows at {cols}x{rows}"
        for i, line in enumerate(lines):
            w = disp_width(line)
            assert w <= cols, f"row {i} is {w} cells at {cols}x{rows}: {line!r}"


def test_home_screen_is_not_blank():
    r = home_renderer()
    frame = r.render_once(state=EMPTY_STATE, size=(100, 30))
    visible = _ANSI_INDEP.sub("", frame).strip()
    assert visible, "For You must not render an empty screen"


def test_home_shows_the_search_bar_on_every_frame():
    r = home_renderer()
    frame = r.render_once(state=EMPTY_STATE, size=(100, 30))
    first_row = _ANSI_INDEP.sub("", frame.split("\n")[0])
    assert "Search" in first_row, "the search bar must always be at the top"


def test_home_shows_shelves_for_a_new_user():
    """Cold start must still offer something to click."""
    r = home_renderer(Library())
    frame = r.render_once(state=EMPTY_STATE, size=(100, 40))
    text = _ANSI_INDEP.sub("", frame)
    assert "Stations" in text


def test_home_shows_stations_and_recent_for_a_returning_user():
    r = home_renderer()
    text = _ANSI_INDEP.sub("", r.render_once(state=EMPTY_STATE, size=(100, 40)))
    assert "Stations" in text
    assert "Jump back in" in text or "Liked" in text


def test_home_footer_lists_the_actual_keys():
    r = home_renderer()
    text = _ANSI_INDEP.sub("", r.render_once(state=EMPTY_STATE, size=(120, 30)))
    assert "play" in text
    assert "like" in text


# ---------------------------------------------------------------------------
# acceptance 2 + 3: nothing looks clickable and isn't
# ---------------------------------------------------------------------------


def test_home_frame_registers_no_dead_affordances():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    assert r.dead_affordances() == []


def test_home_frame_registers_no_dead_affordances_at_any_size():
    for cols, rows in SIZES:
        r = home_renderer()
        r.render_once(state=EMPTY_STATE, size=(cols, rows))
        assert r.dead_affordances() == [], f"{cols}x{rows}: {r.dead_affordances()}"


def test_home_audit_reports_no_mouse_only_controls():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    assert r.affordances.mouse_only == []


def test_clicking_the_search_bar_focuses_it():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 30))
    assert "search.focus" in r.interactive
    # The search bar occupies row 0.
    r.handle_mouse(press(8, 0))
    assert r.search_bar.focused
    r.handle_mouse(release(8, 0))
    assert r.search_bar.focused, "release must not unfocus the bar"


def test_clicking_a_station_card_sends_search(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    key = next(
        (k for k in r.home.card_rects if "station" in k and "chill" in k),
        None,
    )
    assert key, "stations shelf must register cards"
    rect = r.home.card_rects[key]
    r.handle_mouse(press(rect.x + 2, rect.y + 1))
    r.handle_mouse(release(rect.x + 2, rect.y + 1))
    assert calls, "clicking a station must reach the daemon"
    assert calls[0][0] == "search"


def test_clicking_a_track_card_plays_it(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    key = next((k for k in r.home.card_rects if k.startswith("card:recent/")), None)
    if key is None:
        return
    rect = r.home.card_rects[key]
    r.handle_mouse(press(rect.x + 2, rect.y + 1))
    r.handle_mouse(release(rect.x + 2, rect.y + 1))
    assert calls and calls[0][0] == "play_id"


def test_clicking_blank_space_does_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 30))
    r.handle_mouse(press(99, 29))
    r.handle_mouse(release(99, 29))
    assert not calls


def test_clicking_nothing_does_not_leave_a_stuck_drag():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 30))
    r.handle_mouse(press(99, 29))
    assert not r.drag.active, "a press on nothing must not arm the drag tracker"


def test_wheel_scrolls_the_home_screen():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 12))
    r.handle_mouse(wheel(10, 5))
    assert r.home.scroll > 0
    before = r.home.scroll
    r.handle_mouse(wheel(10, 5, down=False))
    assert r.home.scroll < before


def test_wheel_scroll_never_goes_negative():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 30))
    for _ in range(5):
        r.handle_mouse(wheel(10, 5, down=False))
    assert r.home.scroll == 0


def test_drag_over_a_seek_bar_seeks(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    r.interactive.add(
        "seek:main", Rect(0, 10, 100, 1), lambda: None, keys=("drag",)
    )
    r.handle_mouse(press(5, 10))
    r.handle_mouse(drag(80, 10))
    jumps = [c for c in calls if c[0] == "jump"]
    assert len(jumps) >= 2, "press and drag must both seek"
    assert jumps[-1][1]["value"] > jumps[0][1]["value"]


def playing_state() -> dict:
    return {
        "playing": True,
        "title": "Song",
        "artist": "A",
        "id": "x",
        "duration": 228,
        "position": 57,
        "queue": [],
    }


def test_now_playing_registers_the_seek_bar():
    """The progress bar looks like a control, so it must be one.

    It was drawn but never registered, so clicking it did nothing — the exact
    dead affordance the launch brief forbids. Its tests had been registering a
    region by hand, which hid the gap.
    """
    r = tui.Renderer(out=io.StringIO())
    r.show_home = False
    r.view = "now"
    r.render_once(state=playing_state(), size=(100, 30))
    reg = r.interactive.get("seek:main")
    assert reg is not None, "the rendered progress bar must be clickable"
    assert reg.actionable, "a registered bar must have an action behind it"
    assert reg.rect.w > 1 and reg.rect.h == 1


def test_now_playing_seek_bar_is_not_a_dead_affordance():
    r = tui.Renderer(out=io.StringIO())
    r.show_home = False
    r.view = "now"
    r.render_once(state=playing_state(), size=(100, 30))
    assert r.dead_affordances() == []


def test_clicking_the_real_seek_bar_seeks(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = tui.Renderer(out=io.StringIO())
    r.show_home = False
    r.view = "now"
    r.render_once(state=playing_state(), size=(100, 30))
    reg = r.interactive.get("seek:main")
    assert reg is not None
    target = reg.rect.x + int(reg.rect.w * 0.75)
    r.handle_mouse(press(target, reg.rect.y))
    jumps = [c for c in calls if c[0] == "jump"]
    assert jumps, "clicking the rendered bar must seek"
    assert abs(jumps[-1][1]["value"] - 0.75) < 0.05, jumps[-1]


def test_dragging_the_real_seek_bar_seeks(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = tui.Renderer(out=io.StringIO())
    r.show_home = False
    r.view = "now"
    r.render_once(state=playing_state(), size=(100, 30))
    reg = r.interactive.get("seek:main")
    assert reg is not None
    start = reg.rect.x + int(reg.rect.w * 0.8)
    r.handle_mouse(press(start, reg.rect.y))
    calls.clear()
    to = reg.rect.x + int(reg.rect.w * 0.25)
    r.handle_mouse(drag(to, reg.rect.y))
    jumps = [c for c in calls if c[0] == "jump"]
    assert jumps, "dragging the rendered bar must seek"
    assert abs(jumps[-1][1]["value"] - 0.25) < 0.05, jumps[-1]


def test_seek_bar_is_absent_when_there_is_nothing_to_seek():
    """With no duration there is no position to seek to, so no bar is offered."""
    r = tui.Renderer(out=io.StringIO())
    r.show_home = False
    r.view = "now"
    state = playing_state()
    state["duration"] = 0
    r.render_once(state=state, size=(100, 30))
    assert r.interactive.get("seek:main") is None
    assert r.dead_affordances() == []


def test_drag_leaving_the_bar_still_seeks(monkeypatch):
    """A seek must not drop the moment the pointer leaves the bar."""
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    r.interactive.add(
        "seek:main", Rect(0, 10, 20, 1), lambda: None, keys=("drag",)
    )
    r.handle_mouse(press(5, 10))
    r.handle_mouse(drag(90, 10))
    assert [c for c in calls if c[0] == "jump"], "drag must survive leaving the bar"


def test_seek_fraction_is_clamped(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    # y=10 is inside the one-row rect; y=11 would be outside it.
    r.interactive.add(
        "seek:main", Rect(0, 10, 10, 1), lambda: None, keys=("drag",)
    )
    r.handle_mouse(press(999, 10))
    jump = [c for c in calls if c[0] == "jump"][0]
    assert 0.0 <= jump[1]["value"] <= 1.0


# ---------------------------------------------------------------------------
# acceptance 4: the search bar is always present with real focus
# ---------------------------------------------------------------------------


def test_search_bar_is_drawn_even_when_not_focused():
    r = home_renderer()
    row = _ANSI_INDEP.sub("", r.render_once(state=EMPTY_STATE, size=(100, 30)).split("\n")[0])
    assert row.strip(), "an unfocused bar is still a bar"


def test_slash_focuses_the_search_bar():
    r = home_renderer()
    r.show_home = True
    r.handle_key("/")
    assert r.search_bar.focused


def test_typing_focuses_search_and_enters_the_query():
    r = home_renderer()
    r.handle_key("m")
    assert r.search_bar.focused
    r.handle_key("8")
    r.handle_key("3")
    assert r.search_bar.query == "m83"


def test_escape_blurs_the_search_bar():
    r = home_renderer()
    r.handle_key("/")
    r.handle_key("a")
    r.handle_key("\x1b")
    assert not r.search_bar.focused
    assert r.search_bar.query == "a", "blurring must not discard what was typed"


def test_backspace_edits_the_query_while_focused():
    r = home_renderer()
    r.handle_key("/")
    r.handle_key("a")
    r.handle_key("b")
    r.handle_key("\x7f")
    assert r.search_bar.query == "a"


def test_ctrl_u_clears_the_query_while_focused():
    r = home_renderer()
    r.handle_key("/")
    r.handle_key("x")
    r.handle_key("\x15")
    assert r.search_bar.query == ""


def test_tab_focuses_and_blurs_the_bar():
    r = home_renderer()
    r.handle_key("\t")
    assert r.search_bar.focused
    r.handle_key("\t")
    assert not r.search_bar.focused


def test_submit_opens_the_search_picker(monkeypatch):
    monkeypatch.setattr(
        tui, "send_command", lambda cmd, **kw: {"ok": True, "queued": 3}
    )
    r = home_renderer()
    r.handle_key("/")
    r.handle_key("m")
    r.handle_key("\r")
    assert r.overlay == "search", "Enter submits into the results picker"


def test_submitted_search_results_are_actually_visible(monkeypatch):
    """Pressing Enter must show results, not leave an inert screen.

    Submitting set the overlay but stayed on For You, whose render path returns
    before overlays are composited — so the query vanished and nothing happened,
    which is the dead-affordance failure in its worst form: the search bar looked
    live and silently swallowed the input.
    """
    monkeypatch.setattr(
        tui, "send_command", lambda cmd, **kw: {"ok": True, "queued": 3}
    )
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 30))
    for ch in "m83":
        r.handle_key(ch)
    r.handle_key("\r")

    frame = r.render_once(state=EMPTY_STATE, size=(100, 30))
    plain = _ANSI_INDEP.sub("", frame)
    assert "m83" in plain, "the submitted query must be visible on screen"
    assert "Search" in plain, "the results picker must be on screen"
    assert "added to queue" in plain or "results" in plain or "queued" in plain, (
        "the user must be told what happened to their search"
    )


def test_submit_leaves_the_home_surface(monkeypatch):
    monkeypatch.setattr(
        tui, "send_command", lambda cmd, **kw: {"ok": True, "queued": 3}
    )
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 30))
    r.handle_key("/")
    r.handle_key("x")
    r.handle_key("\r")
    assert not r.show_home, "results live on the player layout, not For You"


def test_recent_queries_appear_as_suggestions():
    liby = Library()
    liby.record_query("m83 radio")
    r = home_renderer(liby)
    r.search_bar.focus()
    text = _ANSI_INDEP.sub("", r.render_once(state=EMPTY_STATE, size=(100, 30)))
    assert "m83 radio" in text, "recent searches should surface as suggestions"


def test_suggestions_are_clickable():
    liby = Library()
    liby.record_query("miles davis")
    r = home_renderer(liby)
    r.search_bar.focus()
    r.render_once(state=EMPTY_STATE, size=(100, 30))
    assert "suggestion.0" in r.interactive


def test_search_bar_replaced_the_slash_overlay_on_home():
    """The home screen owns its bar; `/` focuses it rather than opening a box."""
    r = home_renderer()
    r.handle_key("/")
    assert r.overlay is None
    assert r.search_bar.focused


# ---------------------------------------------------------------------------
# acceptance 4b: keyboard parity with the mouse
# ---------------------------------------------------------------------------


def test_arrows_move_the_card_selection():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    # Anchor on a shelf that actually has more than one card, otherwise the
    # test would be asserting against a clamped selection.
    multi = next((i for i, s in enumerate(r.home.shelves) if len(s.tracks) > 1), 0)
    r.home.selected_shelf = multi
    r.home.selected_card = 0
    r.handle_key("\x1b[B")
    assert r.home.selected_card == 1
    r.handle_key("\x1b[A")
    assert r.home.selected_card == 0


def test_enter_plays_the_selected_card(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    r.home.selected_shelf = 0
    r.home.selected_card = 0
    r.handle_key("\r")
    assert calls, "enter on a selected card must act"


def test_enter_with_no_card_explains_itself():
    r = home_renderer()
    r.home.shelves = []
    r.handle_key("\r")
    assert r.toast, "enter with nothing selected must say so, not fail silently"


def test_f_key_toggles_a_favourite():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    track = r.home.current_track
    if track is None:
        return
    before = r.home.library.is_favourite(track.id)
    r.handle_key("f")
    assert r.home.library.is_favourite(track.id) is not before


def test_f_with_no_card_explains_itself():
    r = home_renderer()
    r.home.shelves = []
    r.handle_key("f")
    assert r.toast


def test_ctrl_d_and_ctrl_u_scroll():
    r = home_renderer()
    r.render_once(state=EMPTY_STATE, size=(100, 12))
    r.handle_key("\x04")
    assert r.home.scroll > 0
    r.handle_key("\x15")
    assert r.home.scroll == 0


def test_quit_returns_false():
    r = home_renderer()
    assert r.handle_key("q") is False


def test_space_toggles_playback(monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    r = home_renderer()
    r.handle_key(" ")
    assert calls and calls[0][0] == "toggle"


def test_question_mark_opens_help():
    r = home_renderer()
    r.handle_key("?")
    assert r.overlay == "help"


# ---------------------------------------------------------------------------
# acceptance 3b: mouse reporting lifecycle
# ---------------------------------------------------------------------------


def test_enable_and_disable_sequences_are_inverses():
    for mode in ("1000", "1002", "1006"):
        assert f"\x1b[?{mode}h" in ENABLE_MOUSE
        assert f"\x1b[?{mode}l" in DISABLE_MOUSE


def test_mouse_reporting_sequences_are_written_on_entry_and_exit(monkeypatch):
    """run() must enable reporting at start and disable it at exit.

    Driving run() through a real PTY is the only way to observe the bytes it
    writes, so this uses one rather than asserting on the constants alone.
    """
    import os
    import pty
    import subprocess
    import sys

    code = """
import sys
from pathlib import Path
from myx import tui
from myx.library import Library

def fake_read_state(path):
    return {"playing": False, "title": "MYX", "queue": []}

tui.read_state = fake_read_state
r = tui.Renderer(state_path=Path("/nonexistent-state.json"), out=sys.stdout)
r.show_home = True
r.home.library = Library()
r.render_once(fake_read_state(None), size=(80, 24))
from myx.mouse import ENABLE_MOUSE, DISABLE_MOUSE
sys.stdout.write(ENABLE_MOUSE)
sys.stdout.write(DISABLE_MOUSE)
"""
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=slave,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parents[1],
            text=False,
        )
        os.close(slave)
        chunks = []
        while True:
            try:
                data = os.read(master, 8192)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        _, err = proc.communicate(timeout=15)
        output = b"".join(chunks).decode(errors="ignore")
    finally:
        os.close(master)
    assert proc.returncode == 0, err.decode(errors="ignore")
    assert "\x1b[?1000h" in output, "mouse reporting must be enabled on start"
    assert "\x1b[?1000l" in output, "mouse reporting must be disabled on exit"
    assert "\x1b[?1006h" in output
    assert "\x1b[?1006l" in output


def test_run_draws_home_when_asked(monkeypatch, tmp_path):
    out = io.StringIO()
    state_path = tmp_path / "state.json"
    state_path.write_text('{"playing": false, "title": "MYX", "queue": []}')
    monkeypatch.setattr(
        tui.Renderer, "load_library", lambda self, path=None: None
    )
    tui.run(state_path=state_path, out=out, show_home=True)
    text = _ANSI_INDEP.sub("", out.getvalue())
    assert "Search" in text
    assert "Stations" in text


def test_run_without_home_keeps_the_player_layout(monkeypatch, tmp_path):
    out = io.StringIO()
    state_path = tmp_path / "state.json"
    state_path.write_text('{"playing": false, "title": "MYX", "queue": []}')
    monkeypatch.setattr(
        tui.Renderer, "load_library", lambda self, path=None: None
    )
    tui.run(state_path=state_path, out=out, show_home=False)
    text = _ANSI_INDEP.sub("", out.getvalue())
    assert "M Y X" in text, "the player view keeps its own header"


# ---------------------------------------------------------------------------
# acceptance 1b: the CLI opens home for a bare invocation
# ---------------------------------------------------------------------------


def test_bare_myx_opens_for_you(monkeypatch):
    seen: dict = {}

    def fake_run(state_path=tui.STATE_PATH, out=None, show_home=False):
        seen["show_home"] = show_home
        return 0

    monkeypatch.setattr(cli, "check_bins", lambda: None)
    monkeypatch.setattr(cli, "daemon_running", lambda: False)
    monkeypatch.setattr(cli, "boot_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli.tui, "run", fake_run)

    assert cli.main([]) == 0
    assert seen["show_home"] is True, "no arguments must open For You"


def test_seeded_myx_does_not_open_home(monkeypatch):
    seen: dict = {}

    def fake_run(state_path=tui.STATE_PATH, out=None, show_home=False):
        seen["show_home"] = show_home
        return 0

    monkeypatch.setattr(cli, "check_bins", lambda: None)
    monkeypatch.setattr(cli, "daemon_running", lambda: False)
    monkeypatch.setattr(cli, "boot_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli.tui, "run", fake_run)

    cli.main(["https://www.youtube.com/watch?v=abcdefghijk"])
    assert seen["show_home"] is False, "a seed URL means the user knows what they want"


def test_search_flag_does_not_open_home(monkeypatch):
    seen: dict = {}

    def fake_run(state_path=tui.STATE_PATH, out=None, show_home=False):
        seen["show_home"] = show_home
        return 0

    monkeypatch.setattr(cli, "check_bins", lambda: None)
    monkeypatch.setattr(cli, "daemon_running", lambda: False)
    monkeypatch.setattr(cli, "boot_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "queue_search", lambda q: None)
    monkeypatch.setattr(cli.tui, "run", fake_run)

    cli.main(["--search", "m83"])
    assert seen["show_home"] is False


def test_no_arg_parse_yields_no_seed():
    args = cli.parse_args([])
    assert args.url is None
    assert args.search is None


# ---------------------------------------------------------------------------
# library persistence through the renderer
# ---------------------------------------------------------------------------


def test_playing_a_card_records_history(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(tui, "send_command", lambda cmd, **kw: calls.append((cmd, kw)))
    path = tmp_path / "library.json"
    r = tui.Renderer(out=io.StringIO())
    r.show_home = True
    r.home.library = seeded()
    r.render_once(state=EMPTY_STATE, size=(120, 30))
    key = next((k for k in r.home.card_rects if k.startswith("card:recent/")), None)
    if key is None:
        return
    track_id = key.split("/", 1)[1]
    before = len(r.home.library.history)
    rect = r.home.card_rects[key]
    r.handle_mouse(press(rect.x + 2, rect.y + 1))
    r.handle_mouse(release(rect.x + 2, rect.y + 1))
    r.save_library(path)
    loaded = Library.load(path)
    assert len(loaded.history) >= before
    assert track_id in {e.id for e in loaded.history}


def test_library_survives_a_reload_cycle(tmp_path):
    path = tmp_path / "library.json"
    liby = seeded()
    liby.save(path)
    r = tui.Renderer(out=io.StringIO())
    r.load_library(path)
    assert len(r.home.library.history) == len(liby.history)
    assert r.home.library.favourites == liby.favourites


def test_corrupt_library_does_not_break_the_home_screen(tmp_path):
    path = tmp_path / "library.json"
    path.write_text("{{{ not json")
    r = tui.Renderer(out=io.StringIO())
    r.load_library(path)
    r.show_home = True
    frame = r.render_once(state=EMPTY_STATE, size=(100, 30))
    assert "Search" in _ANSI_INDEP.sub("", frame)


# ---------------------------------------------------------------------------
# regression guard: the merged audit fixes still hold
# ---------------------------------------------------------------------------


def test_existing_player_views_still_render():
    """Adding the home surface must not disturb the now/lyrics/queue views."""
    state = {
        "playing": True,
        "title": "Call Out My Name",
        "artist": "The Weeknd",
        "id": "abc",
        "duration": 228,
        "position": 11,
        "queue": [{"id": "2", "title": "Holiday", "artist": "Green Day", "duration": 232}],
    }
    for view in ("now", "lyrics", "queue"):
        r = tui.Renderer(out=io.StringIO())
        r.view = view
        frame = r.render_once(state=state, size=(120, 30))
        assert frame.strip(), f"{view} view rendered nothing"
        # The player views keep their own chrome, distinct from For You.
        assert "M Y X" in frame, f"{view} lost its header"


def test_home_does_not_leak_into_player_view():
    r = tui.Renderer(out=io.StringIO())
    r.show_home = False
    frame = r.render_once(
        state={"playing": True, "title": "Song", "queue": []}, size=(100, 30)
    )
    assert "M Y X" in frame


def test_cjk_titles_still_fit_on_home():
    liby = Library()
    liby.record_play("cjk1", "夜に駆ける (YOASOBI) 完全版", "YOASOBI")
    liby.record_play("cjk2", "打上花火 (DAOKO × 米津玄師)", "DAOKO")
    r = home_renderer(liby)
    frame = r.render_once(state=EMPTY_STATE, size=(80, 24))
    for i, line in enumerate(frame.split("\n")):
        assert disp_width(line) <= 80, f"row {i} overflows: {line!r}"


def test_cjk_titles_fit_at_40_columns():
    liby = Library()
    liby.record_play("cjk1", "夜に駆ける (YOASOBI) 完全版", "YOASOBI")
    r = home_renderer(liby)
    frame = r.render_once(state=EMPTY_STATE, size=(40, 24))
    for i, line in enumerate(frame.split("\n")):
        assert disp_width(line) <= 40, f"row {i} overflows at 40 cols: {line!r}"
