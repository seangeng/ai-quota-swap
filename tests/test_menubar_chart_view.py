"""The usage chart's AppKit view (``menubar_chart_view``), drawn off screen
into a bitmap. macOS only, and only where AppKit is installed (the menubar
extra brings it: ``uv run --locked --extra menubar pytest``); skipped
everywhere else, except where ``QSWAP_TEST_REQUIRE_APPKIT=1`` says AppKit
must be there (CI's macOS step), where a broken import fails. Nothing is
shown: no window, no application object, no run loop, so a headless runner
draws it too."""

from __future__ import annotations

import os
import sys

import pytest

if os.environ.get("QSWAP_TEST_REQUIRE_APPKIT") == "1":
    import AppKit  # a broken install fails here rather than skipping
else:
    if sys.platform != "darwin":
        pytest.skip("AppKit is macOS only", allow_module_level=True)
    AppKit = pytest.importorskip("AppKit")

from ai_quota_swap import menubar_chart  # noqa: E402
from ai_quota_swap import menubar_chart_view as view_mod  # noqa: E402
from tests.test_menubar_chart import (  # noqa: E402
    A1,
    A2,
    A3,
    A5,
    LONG,
    SEVEN,
    SHOWCASE,
    X1,
    X2,
    _cost,
    assert_legend_uses_its_room,
)

from ai_quota_swap.menubar_chart import build_chart  # noqa: E402

SCALE = 2


def _bitmap(view, *, dark: bool):
    name = AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua
    view.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name))
    w, h = view.frame().size.width, view.frame().size.height
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, int(w * SCALE), int(h * SCALE), 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0,
    )
    rep.setSize_((w, h))
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    return rep


def _alpha(rep, x: float, y: float) -> float:
    """Coverage at point (x, y) of the flipped view."""
    colour = rep.colorAtX_y_(int(x * SCALE), int(y * SCALE))
    return float(colour.alphaComponent()) if colour is not None else 0.0


def _columns(view):
    """Where the shared layout puts each day in this view."""
    left = view_mod.TITLE_INSET + view_mod.STATE_COLUMN
    inner = view.frame().size.width - left - view_mod.RIGHT_INSET
    return menubar_chart.layout_columns(view.chart, left=left, width=inner, base=BASE)


BASE = view_mod._frame().base


def test_the_view_draws_off_screen_in_light_and_dark():
    chart = build_chart(_cost(SEVEN), both=True)
    view = view_mod.make_view(chart)
    assert 90 <= view.frame().size.height <= menubar_chart.MAX_HEIGHT
    assert view.frame().size.width == pytest.approx(view_mod.min_width(chart))
    assert view.autoresizingMask() & AppKit.NSViewWidthSizable
    view.setFrame_(((0, 0), (430.0, view.frame().size.height)))  # a menu's width
    images = []
    for dark in (False, True):
        rep = _bitmap(view, dark=dark)
        images.append(bytes(rep.bitmapData()))
        cols = _columns(view)
        # The tallest day (Saturday) fills the bar area; every day has a bar,
        # drawn where the shared layout puts it, and nothing between bars.
        assert _alpha(rep, cols[3].centre, BASE - menubar_chart.BAR_AREA + 1) > 0.5
        assert _alpha(rep, cols[3].centre, BASE - menubar_chart.BAR_AREA - 1) == 0
        for col in cols:
            assert _alpha(rep, col.centre, BASE - 0.5) > 0.5
            assert _alpha(rep, col.x + 0.25, BASE - 0.5) > 0.5
            assert _alpha(rep, col.x + col.width - 0.25, BASE - 0.5) > 0.5
            assert _alpha(rep, col.x - 0.25, BASE - 0.5) == 0
            assert _alpha(rep, col.x + col.width + 0.25, BASE - 0.5) == 0
        # The bars run edge to edge: the first starts where the caption
        # does, the last ends on the right inset.
        assert cols[0].width > 30
        assert cols[0].x == view_mod.TITLE_INSET + view_mod.STATE_COLUMN
        assert cols[-1].x + cols[-1].width == 430.0 - view_mod.RIGHT_INSET
    assert images[0] != images[1]  # light and dark draw differently
    assert view_mod._failed is False


def test_an_empty_day_draws_a_one_point_stub():
    view = view_mod.make_view(build_chart(_cost({A1: (5, 0, 3, 4, 2, 6, 1)})))
    rep = _bitmap(view, dark=False)
    stub = _columns(view)[1]
    assert _alpha(rep, stub.centre, BASE - 0.5) > 0.2  # the stub on the baseline
    assert _alpha(rep, stub.centre, BASE - 2.5) == 0  # and nothing above it


def test_a_gap_shows_only_between_two_tall_segments():
    view = view_mod.make_view(build_chart(_cost({A1: (10,) * 7, A2: (10,) * 7})))
    rep = _bitmap(view, dark=False)
    col = _columns(view)[0]
    low, high = col.segments
    gap = high.box.y + high.box.h + 0.5  # the unpainted point between them
    assert _alpha(rep, col.centre, gap) == 0
    assert _alpha(rep, col.centre, gap - 1) > 0.5 and _alpha(rep, col.centre, gap + 1) > 0.5


def test_the_legend_fits_the_view_whatever_its_width():
    # Whatever width the menu gives the view, which is never less than the
    # view's own width and the check-mark column NSMenu adds to it.
    _caption, _day, legend_font = view_mod._fonts()
    for week in (SEVEN, LONG, {A1: (1,) * 7, A2: (2,) * 7}):
        chart = build_chart(_cost(week), both=True)
        view = view_mod.make_view(chart)
        narrowest = view.frame().size.width + view_mod.STATE_COLUMN
        assert narrowest == pytest.approx(view_mod.menu_width(chart))
        for width in (narrowest, max(narrowest, 380.0), narrowest + 23.0, 520.0):
            inner = width - view_mod.TITLE_INSET - view_mod.STATE_COLUMN - view_mod.RIGHT_INSET
            names = chart.fit_legend(lambda ns: view_mod._legend_width(chart, ns, legend_font) <= inner)
            assert view_mod._legend_width(chart, names, legend_font) <= inner


def test_the_view_says_the_caption_and_each_days_total():
    chart = build_chart(_cost(SEVEN))
    view = view_mod.make_view(chart)
    assert view.accessibilityLabel() == chart.accessibility_label()
    assert view.accessibilityRole() == AppKit.NSAccessibilityImageRole
    assert view.isAccessibilityElement()


def test_a_failure_while_drawing_never_costs_the_menu(monkeypatch):
    monkeypatch.setattr(view_mod, "_failed", False)
    chart = build_chart(_cost(SEVEN))
    view = view_mod.make_view(chart)

    def broken(*_a):
        raise ValueError("boom")

    monkeypatch.setattr(view_mod, "_draw", broken)
    rep = _bitmap(view, dark=False)  # does not raise
    assert view_mod._failed is True
    left = view_mod.TITLE_INSET + view_mod.STATE_COLUMN
    assert any(_alpha(rep, left + dx, view_mod.CAPTION_BAND / 2) > 0 for dx in range(0, 40))  # the sentence
    with pytest.raises(RuntimeError):
        view_mod.make_view(chart)  # from now on the menu shows the text row


def test_the_drawn_legend_shows_every_entry_inside_the_view():
    # Long names in a 460 pt menu: each entry's dot is drawn where the
    # fitted line puts it, the last one ("other") included, inside the insets.
    chart = build_chart(_cost(LONG))
    _caption, _day, legend_font = view_mod._fonts()
    view = view_mod.make_view(chart)
    view.setFrame_(((0, 0), (460.0, view.frame().size.height)))
    rep = _bitmap(view, dark=False)
    left = view_mod.TITLE_INSET + view_mod.STATE_COLUMN
    inner = 460.0 - left - view_mod.RIGHT_INSET
    names = chart.fit_legend(lambda ns: view_mod._legend_width(chart, ns, legend_font) <= inner)
    assert names != [e.name for e in chart.legend]  # these names had to be cut
    y = view_mod._frame().legend_top + view_mod._line_height(legend_font) / 2
    x = left
    for name, entry in zip(names, chart.legend):
        assert _alpha(rep, x + menubar_chart.DOT / 2, y) > 0.5, name
        x += menubar_chart.DOT + menubar_chart.DOT_GAP
        x += view_mod._entry_text(name, entry.amount_text, legend_font).size().width + menubar_chart.ENTRY_GAP
    assert x - menubar_chart.ENTRY_GAP <= 460.0 - view_mod.RIGHT_INSET


def test_a_failure_finding_the_check_marks_never_reaches_appkit(monkeypatch):
    # The first thing drawRect_ does is ask the menu whether it shows check
    # marks; an error there is handled like any drawing error.
    monkeypatch.setattr(view_mod, "_failed", False)
    view = view_mod.make_view(build_chart(_cost(SEVEN)))

    def broken(_view):
        raise RuntimeError("no menu today")

    monkeypatch.setattr(view_mod, "_menu_has_check_marks", broken)
    rep = _bitmap(view, dark=True)  # does not raise
    assert view_mod._failed is True
    left = view_mod.TITLE_INSET + view_mod.STATE_COLUMN
    assert any(_alpha(rep, left + dx, menubar_chart.CAPTION_BAND / 2) > 0 for dx in range(0, 40))


def test_the_view_asks_for_its_width_without_the_check_mark_column():
    # NSMenu adds the check-mark column to a view's width by itself once a
    # row has a check mark, so the view's own width leaves it out: the
    # insets, and the caption or the legend at its shortest forms.
    caption_font, _day, legend_font = view_mod._fonts()
    for week in (SEVEN, SHOWCASE, LONG):
        chart = build_chart(_cost(week), both=True)
        shortest = chart.fit_legend(lambda _names: False)
        caption = view_mod._text(chart.caption, caption_font, AppKit.NSColor.labelColor()).size().width
        content = max(caption, view_mod._legend_width(chart, shortest, legend_font))
        view = view_mod.make_view(chart)
        assert view.frame().size.width == pytest.approx(view_mod.TITLE_INSET + content + view_mod.RIGHT_INSET)
        assert view_mod.menu_width(chart) == pytest.approx(view.frame().size.width + view_mod.STATE_COLUMN)
        assert view_mod.menu_width(chart, check_marks=False) == pytest.approx(view.frame().size.width)


def test_a_menu_of_a_short_row_and_the_chart_is_as_wide_as_the_chart_needs():
    # Measured, never shown: NSMenu.size lays the menu out without opening
    # it. With a checked row the menu is the view and the check-mark
    # column; without, the view alone. Never 14 points more.
    chart = build_chart(_cost(SEVEN), both=True)
    for checked in (False, True):
        menu = AppKit.NSMenu.alloc().init()
        row = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("#1 personal", None, "")
        row.setState_(AppKit.NSControlStateValueOn if checked else AppKit.NSControlStateValueOff)
        menu.addItem_(row)
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(chart.caption, None, "")
        item.setView_(view_mod.make_view(chart))
        menu.addItem_(item)
        assert menu.size().width == pytest.approx(view_mod.menu_width(chart, check_marks=checked), abs=0.5)


def test_every_bar_has_the_same_corners():
    # Each column is clipped to one outline rounded at 3 points, so the
    # corners are the same pixels on every bar, whatever its top segment.
    chart = build_chart(_cost(SEVEN), both=True)
    view = view_mod.make_view(chart)
    view.setFrame_(((0, 0), (460.0, view.frame().size.height)))
    rep = _bitmap(view, dark=False)
    cols = _columns(view)
    assert len({round(c.segments[-1].box.h, 1) for c in cols}) > 1  # top segments of all heights
    span = int(menubar_chart.CORNER * SCALE)  # the corner's own pixels, above any gap

    def corner(col, right: bool):
        o = col.outline
        x0 = int((o.x + o.w) * SCALE) - span if right else int(o.x * SCALE)
        y0 = int(o.y * SCALE)
        return tuple(
            round(float(rep.colorAtX_y_(x, y).alphaComponent()), 2)
            for y in range(y0, y0 + span) for x in range(x0, x0 + span)
        )

    for right in (False, True):
        shapes = {corner(c, right) for c in cols if c.outline.h >= menubar_chart.CORNER}
        assert len(shapes) == 1, right
    o = cols[0].outline  # the corner pixel is outside the rounded outline
    assert _alpha(rep, o.x + 0.25, o.y + 0.25) < 0.3 and _alpha(rep, o.x + 0.25, o.y + 3.25) > 0.5


def _view_measure(chart):
    _caption, _day, legend_font = view_mod._fonts()
    return lambda names: view_mod._legend_width(chart, names, legend_font)


#: The insets inside the view in a menu with check marks.
INSETS = view_mod.TITLE_INSET + view_mod.STATE_COLUMN + view_mod.RIGHT_INSET


def test_the_showcase_legend_with_the_views_own_fonts_from_360_to_600_points():
    # Each width the view could be drawn at: names start at their shortest
    # and grow in legend order, so no room is left a name could use, the
    # first entry never gets shorter as the menu gets wider, and no entry
    # is bare while its three-letter cut would fit.
    chart = build_chart(_cost(SHOWCASE), both=True)
    widths = range(360, 601, 4)
    shown = assert_legend_uses_its_room(chart, [w - INSETS for w in widths], _view_measure(chart))
    assert all("Codex #2" in names[3] for names in shown)
    # The view is never narrower than the line at its shortest forms.
    measure = _view_measure(chart)
    narrowest = view_mod.menu_width(chart) - INSETS
    shortest = chart.fit_legend(lambda _names: False)
    assert shortest == ["#1", "#2", "#5", "Codex #2", "other"]
    assert measure(shortest) == pytest.approx(narrowest)  # the legend sets the width, not the caption
    assert chart.fit_legend(lambda ns: measure(ns) <= narrowest) == shortest


def test_twin_numbers_read_apart_at_every_width():
    # Both providers with a #1 and a #2 in the legend.
    week = {A1: SEVEN[A1], X1: SEVEN[A2], A2: SEVEN[X1], X2: SEVEN[A3], A3: SEVEN[A5]}
    chart = build_chart(_cost(week), both=True)
    assert [e.name[: e.head] for e in chart.legend[:4]] == ["#1", "Codex #1", "#2", "Codex #2"]
    shown = assert_legend_uses_its_room(chart, [w - INSETS for w in range(320, 601, 4)], _view_measure(chart))
    for names in shown:
        assert sum(n.startswith("Codex #") for n in names) == 2 and len(set(names)) == len(names)
