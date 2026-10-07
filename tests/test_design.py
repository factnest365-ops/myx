"""Tests for the MYX design system.

The launch brief names four terminal sizes that must all render correctly:
40-column, 60-column, wide, and short. These tests pin the spacing scale, the
type roles, and the layout invariants at each of them.
"""

from myx import design
from myx.design import (
    Breakpoint,
    Role,
    Step,
    clamp_width,
    for_size,
    metrics,
    space,
    type_of,
)

# The four sizes the brief names, plus the extras worth pinning.
SIZES = {
    "tiny_40": (40, 24),
    "narrow_60": (60, 24),
    "wide_120": (120, 32),
    "short_80x12": (80, 12),
}


def test_space_steps_are_multiples_of_base_unit():
    """Every spacing step is on-rhythm, so gutters line up across screens."""
    for step in Step:
        cells = space(step)
        assert cells >= 0
        assert cells % design.BASE_UNIT == 0, f"{step} -> {cells} is off-rhythm"


def test_space_steps_are_strictly_increasing():
    ordered = [space(s) for s in (Step.NONE, Step.HAIRO, Step.THIN, Step.SNUG,
                                  Step.BASE, Step.LOOSE, Step.CHUNKY)]
    assert ordered == sorted(ordered)
    assert len(set(ordered)) == len(ordered)


def test_space_never_returns_negative():
    assert space(Step.NONE) == 0
    assert space(-5) == 0


def test_cells_counts_wide_chars():
    assert design.cells("abc") == 3
    assert design.cells("日本語") == 6
    assert design.cells("🔀") == 2


def test_for_size_classifies_the_four_named_sizes():
    assert for_size(40) == Breakpoint.TINY
    assert for_size(60) == Breakpoint.NARROW
    assert for_size(120) == Breakpoint.WIDE
    # short is a row property, not a width property: 80x12 is still 80 wide
    assert for_size(80, 12) == Breakpoint.MEDIUM


def test_for_size_boundary_values():
    assert for_size(0) == Breakpoint.TINY
    assert for_size(49) == Breakpoint.TINY
    assert for_size(50) == Breakpoint.NARROW
    assert for_size(79) == Breakpoint.NARROW
    assert for_size(80) == Breakpoint.MEDIUM
    assert for_size(109) == Breakpoint.MEDIUM
    assert for_size(110) == Breakpoint.WIDE


def test_rows_do_not_change_the_width_breakpoint():
    """A very short but very wide window is still wide, not 'tiny'."""
    assert for_size(200, 8) == Breakpoint.WIDE
    assert for_size(200, 8).value == "wide"


def test_type_scale_defines_every_role():
    for bp in Breakpoint:
        scale = design.type_scale(bp)
        for role in Role:
            style = scale.style(role)
            assert style.emphasis in ("accent", "fg", "dim", "err")
            assert 0.0 < style.width_weight <= 1.0


def test_type_scale_accepts_string_roles():
    bp = Breakpoint.WIDE
    assert type_of(bp, "display") == type_of(bp, Role.DISPLAY)


def test_display_is_always_the_strongest_accent():
    """The one big thing on screen must win at every size."""
    for bp in Breakpoint:
        display = type_of(bp, Role.DISPLAY)
        assert display.bold
        assert display.emphasis == "accent"


def test_meta_and_muted_are_never_bold():
    """Quiet roles stay quiet — hierarchy that shouts everywhere is noise."""
    for bp in Breakpoint:
        assert not type_of(bp, Role.META).bold
        assert not type_of(bp, Role.MUTED).bold
        assert not type_of(bp, Role.LABEL).bold


def test_narrow_breaks_de_emphasise_competing_accents():
    """Two bold accent things on one narrow line is noise, not hierarchy."""
    wide = type_of(Breakpoint.WIDE, Role.ACCENT)
    tight = type_of(Breakpoint.TINY, Role.ACCENT)
    assert wide.bold
    assert not tight.bold


def test_metrics_gutter_shrinks_on_tiny():
    assert metrics(40, 24).gutter < metrics(60, 24).gutter
    assert metrics(60, 24).gutter == space(Step.SNUG)


def test_metrics_content_width_accounts_for_gutters():
    for name, (cols, rows) in SIZES.items():
        m = metrics(cols, rows)
        assert m.content_width >= 1, name
        assert m.content_width == cols - 2 * m.gutter, name


def test_metrics_never_produce_negative_dimensions():
    """Degenerate terminal sizes must not yield negative widths."""
    for cols, rows in ((0, 0), (1, 1), (2, 3), (5, 2)):
        m = metrics(cols, rows)
        assert m.content_width >= 1
        assert m.card_width >= 1
        assert m.main_width >= 1
        assert m.gutter >= 0
        assert m.section_gap >= 1


def test_columns_increase_with_width():
    counts = [metrics(c, 24).columns for c in (40, 60, 80, 120, 200)]
    assert counts == sorted(counts)
    assert counts[0] == 1, "a 40-column terminal must be single-column"


def test_cards_tile_within_content_width():
    """cards * columns + gaps must not overflow the content area."""
    for name, (cols, rows) in SIZES.items():
        m = metrics(cols, rows)
        used = m.card_width * m.columns + m.gap * (m.columns - 1)
        assert used <= m.content_width, name


def test_sidebar_only_appears_when_there_is_room():
    assert not metrics(40, 24).show_sidebar
    assert not metrics(60, 24).show_sidebar
    assert metrics(120, 32).show_sidebar
    # a short terminal drops the sidebar even when it is wide enough
    assert not metrics(120, 12).show_sidebar


def test_main_plus_sidebar_fits_content_width():
    for name, (cols, rows) in SIZES.items():
        m = metrics(cols, rows)
        if not m.show_sidebar:
            assert m.main_width == m.content_width, name
            continue
        assert m.main_width + m.sidebar_width + 1 <= m.content_width, name


def test_is_short_is_a_row_property():
    assert metrics(120, 12).is_short
    assert metrics(120, 24).is_short is False
    assert metrics(120, 15).is_short is True
    assert metrics(120, 16).is_short is False


def test_short_terminals_tighten_section_spacing():
    assert metrics(120, 12).section_gap < metrics(120, 32).section_gap
    assert metrics(40, 24).section_gap == 1


def test_clamp_width_stays_in_range():
    assert clamp_width(5, 10, 20) == 10
    assert clamp_width(50, 10, 20) == 20
    assert clamp_width(15, 10, 20) == 15
    assert clamp_width(-4, 10, 20) == 10
    assert clamp_width(7) == 7
    assert clamp_width(-1) == 0
