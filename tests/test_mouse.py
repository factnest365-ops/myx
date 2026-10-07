"""Tests for MYX mouse support: protocol decoding and hit testing."""

from myx import mouse
from myx.mouse import (
    DISABLE_MOUSE,
    ENABLE_MOUSE,
    DragTracker,
    InteractiveMap,
    MouseButton,
    MouseEvent,
    MouseKind,
    Rect,
    WheelTarget,
    decode_sgr,
    has_mouse,
    parse_mouse,
)


def noop() -> None:
    return None


# ---------------------------------------------------------------------------
# protocol: enable / disable
# ---------------------------------------------------------------------------


def test_enable_sequence_turns_on_all_three_modes():
    """1000 normal + 1002 drag + 1006 SGR coordinates."""
    assert "\x1b[?1000h" in ENABLE_MOUSE
    assert "\x1b[?1002h" in ENABLE_MOUSE, "drag-to-seek needs button-event tracking"
    assert "\x1b[?1006h" in ENABLE_MOUSE, "SGR mode is required for modern coords"


def test_disable_sequence_turns_all_modes_off():
    for mode in ("1000", "1002", "1006"):
        assert f"\x1b[?{mode}l" in DISABLE_MOUSE


def test_disable_is_the_exact_inverse_of_enable():
    """Terminal must not be left in a reporting state after exit."""
    for mode in ("1000", "1002", "1006"):
        assert f"\x1b[?{mode}h" in ENABLE_MOUSE
        assert f"\x1b[?{mode}l" in DISABLE_MOUSE


# ---------------------------------------------------------------------------
# protocol: decoding
# ---------------------------------------------------------------------------


def test_parse_left_click():
    events = parse_mouse("\x1b[<0;10;5M")
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == MouseKind.PRESS
    assert ev.button == MouseButton.LEFT
    assert ev.x == 9 and ev.y == 4, "terminal coords are 1-based"
    assert ev.is_click


def test_parse_release():
    (ev,) = parse_mouse("\x1b[<0;10;5m")
    assert ev.kind == MouseKind.RELEASE
    assert not ev.is_click


def test_parse_middle_and_right_clicks():
    (mid,) = parse_mouse("\x1b[<1;1;1M")
    assert mid.button == MouseButton.MIDDLE
    (right,) = parse_mouse("\x1b[<2;1;1M")
    assert right.button == MouseButton.RIGHT


def test_parse_drag_reports_motion_flag():
    (ev,) = parse_mouse("\x1b[<32;12;7M")
    assert ev.kind == MouseKind.DRAG
    assert ev.button == MouseButton.LEFT


def test_parse_wheel_up_and_down():
    (up,) = parse_mouse("\x1b[<64;3;4M")
    assert up.kind == MouseKind.WHEEL_UP
    (down,) = parse_mouse("\x1b[<65;3;4M")
    assert down.kind == MouseKind.WHEEL_DOWN


def test_parse_reports_beyond_80_columns():
    """SGR coordinates are not offset by 32; a 200-column window works."""
    (ev,) = parse_mouse("\x1b[<0;200;60M")
    assert ev.x == 199 and ev.y == 59


def test_parse_multiple_reports_in_one_read():
    events = parse_mouse("\x1b[<0;1;1M\x1b[<64;2;2M\x1b[<0;3;3m")
    assert [e.kind for e in events] == [
        MouseKind.PRESS,
        MouseKind.WHEEL_UP,
        MouseKind.RELEASE,
    ]


def test_parse_ignores_keyboard_input():
    assert parse_mouse("hello") == []
    assert parse_mouse("") == []


def test_parse_ignores_arrow_keys():
    """Arrows are CSI but not SGR mouse reports."""
    assert parse_mouse("\x1b[A") == []
    assert parse_mouse("\x1b[B") == []


def test_parse_ignores_incomplete_trailing_report():
    """A partial read must not be misread as a click."""
    assert parse_mouse("\x1b[<0;10;") == []
    assert parse_mouse("\x1b[<0;10;5") == []


def test_parse_keeps_complete_report_before_partial_one():
    events = parse_mouse("\x1b[<0;10;5M\x1b[<0;2")
    assert len(events) == 1
    assert events[0].x == 9


def test_has_mouse_detects_reports():
    assert has_mouse("\x1b[<0;1;1M")
    assert not has_mouse("abc")
    assert not has_mouse("\x1b[A")


def test_decode_ignores_buttonless_motion():
    """3 with the motion bit is 'no button held' — not an event we want."""
    assert decode_sgr(35, 5, 5, "M") is None


def test_decode_handles_high_column_values():
    ev = decode_sgr(0, 320, 200, "M")
    assert ev is not None
    assert ev.col == 320 and ev.row == 200


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def test_rect_contains_is_half_open():
    r = Rect(2, 3, 4, 5)
    assert r.contains(2, 3), "top-left corner is inside"
    assert r.contains(5, 7), "bottom-right cell is inside"
    assert not r.contains(6, 7), "x + w is outside"
    assert not r.contains(5, 8), "y + h is outside"
    assert not r.contains(1, 3)


def test_empty_rect_is_falsy():
    assert not Rect(0, 0, 0, 5)
    assert not Rect(0, 0, 5, 0)
    assert Rect(0, 0, 1, 1)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_map_returns_region_under_point():
    m = InteractiveMap()
    m.add("play", Rect(0, 0, 10, 2), noop)
    hit = m.at(5, 1)
    assert hit is not None and hit.id == "play"


def test_map_returns_none_outside_all_regions():
    m = InteractiveMap()
    m.add("play", Rect(0, 0, 10, 2), noop)
    assert m.at(50, 50) is None


def test_later_registration_wins_a_tie():
    """A control drawn on top of a container must receive the click."""
    m = InteractiveMap()
    m.add("card", Rect(0, 0, 20, 5), noop)
    m.add("card.play", Rect(2, 1, 8, 1), noop)
    hit = m.at(3, 1)
    assert hit is not None and hit.id == "card.play"


def test_disabled_region_does_not_receive_clicks():
    m = InteractiveMap()
    m.add("like", Rect(0, 0, 4, 1), noop, enabled=False)
    assert m.at(1, 0) is None


def test_add_overwrites_same_id():
    m = InteractiveMap()
    m.add("x", Rect(0, 0, 5, 5), noop)
    m.add("x", Rect(0, 0, 2, 2), noop)
    assert len(m) == 1
    assert m.get("x").rect.w == 2


def test_region_without_action_is_a_dead_affordance():
    """The core rule: a region that does nothing is a bug, not a feature."""
    m = InteractiveMap()
    m.add("looks_clickable", Rect(0, 0, 5, 1))
    assert m.dead_affordances() == ["looks_clickable"]


def test_region_with_zero_area_is_a_dead_affordance():
    m = InteractiveMap()
    m.add("invisible", Rect(0, 0, 0, 0), noop)
    assert m.dead_affordances() == ["invisible"]


def test_actioned_region_is_not_a_dead_affordance():
    m = InteractiveMap()
    m.add("play", Rect(0, 0, 5, 1), noop)
    assert m.dead_affordances() == []
    assert m.actionable_ids() == ["play"]


def test_missing_keyboard_parity_is_reported():
    m = InteractiveMap()
    m.add("with_key", Rect(0, 0, 5, 1), noop, keys=("space",))
    m.add("mouse_only", Rect(6, 0, 5, 1), noop)
    assert m.missing_keyboard_parity() == ["mouse_only"]


def test_clear_resets_every_frame():
    m = InteractiveMap()
    m.add("a", Rect(0, 0, 2, 2), noop)
    m.clear()
    assert len(m) == 0
    assert m.at(0, 0) is None


def test_map_membership_and_ids():
    m = InteractiveMap()
    m.add("a", Rect(0, 0, 2, 2), noop)
    assert "a" in m
    assert "b" not in m
    assert m.ids() == ["a"]


# ---------------------------------------------------------------------------
# drag / wheel
# ---------------------------------------------------------------------------


def test_drag_keeps_its_region_after_the_cursor_leaves():
    """A seek must not drop the instant the pointer moves off the bar."""
    t = DragTracker()
    t.press("seek", 10, 5)
    t.drag(90, 5)
    assert t.region_id == "seek"
    assert t.moved


def test_drag_release_reports_the_original_region():
    t = DragTracker()
    t.press("seek", 10, 5)
    assert t.release(60, 5) == "seek"
    assert not t.active


def test_drag_cancel_forgets_the_region():
    t = DragTracker()
    t.press("seek", 1, 1)
    t.cancel()
    assert not t.active
    assert t.release(5, 5) is None


def test_drag_without_press_is_inert():
    t = DragTracker()
    t.drag(3, 3)
    assert not t.moved
    assert t.release(3, 3) is None


def test_wheel_prefers_the_scrollable_under_the_cursor():
    m = InteractiveMap()
    m.add("shelf", Rect(0, 0, 40, 10), noop)
    m.add("sidebar", Rect(0, 12, 20, 10), noop)
    target = WheelTarget(("shelf", "sidebar", "global"), m)
    assert target.resolve(5, 5) == "shelf"
    assert target.resolve(5, 15) == "sidebar"


def test_wheel_falls_back_to_global_over_blank_space():
    m = InteractiveMap()
    target = WheelTarget(("global",), m)
    assert target.resolve(99, 99) == "global"


def test_wheel_resolves_to_nothing_when_nothing_scrolls():
    m = InteractiveMap()
    m.add("play", Rect(0, 0, 4, 1), noop)
    target = WheelTarget((), m)
    assert target.resolve(1, 0) is None


def test_wheel_ignores_nonscrollable_regions_under_cursor():
    m = InteractiveMap()
    m.add("play", Rect(0, 0, 4, 1), noop)
    target = WheelTarget(("global",), m)
    assert target.resolve(1, 0) == "global", "falls back rather than swallowing the wheel"


def test_event_is_hashable_for_use_as_dict_key():
    ev = MouseEvent(1, 1, MouseKind.PRESS, MouseButton.LEFT)
    assert {ev: "ok"}[ev] == "ok"
    assert mouse.Rect(0, 0, 1, 1).contains(0, 0)
