"""The usage chart's AppKit view (``menubar_chart_view``), drawn off screen
into a bitmap. macOS only, and only where AppKit is installed (the menubar
extra brings it; ``uv run --with pyobjc-framework-Cocoa pytest`` adds it);
skipped everywhere else. Nothing is shown: no window, no application
object, no run loop, so a headless runner draws it too."""

from __future__ import annotations

import sys

import pytest

if sys.platform != "darwin":
    pytest.skip("AppKit is macOS only", allow_module_level=True)
AppKit = pytest.importorskip("AppKit")

from ai_quota_swap import menubar_chart_view as view_mod  # noqa: E402
from tests.test_menubar_chart import A1, A2, LONG, SEVEN, _cost  # noqa: E402

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
    """Each column's left edge and the bar width, as the view lays them out."""
    left = view_mod.TITLE_INSET + view_mod.STATE_COLUMN
    inner = view.frame().size.width - left - view_mod.RIGHT_INSET
    bar = min(view_mod.MAX_BAR_WIDTH, inner / 13)
    gap = (inner - 7 * bar) / 6
    return [left + i * (bar + gap) for i in range(7)], bar


BASE = view_mod.CAPTION_BAND + view_mod.BAR_TOP_GAP + view_mod.BAR_HEIGHT


def test_the_view_draws_off_screen_in_light_and_dark():
    chart = build_chart(_cost(SEVEN))
    view = view_mod.make_view(chart)
    assert 90 <= view.frame().size.height <= 110
    assert view.frame().size.width == pytest.approx(view_mod.min_width(chart))
    assert view.autoresizingMask() & AppKit.NSViewWidthSizable
    images = []
    for dark in (False, True):
        rep = _bitmap(view, dark=dark)
        images.append(bytes(rep.bitmapData()))
        xs, bar = _columns(view)
        # The tallest day (Saturday) fills the height; every day has a bar.
        assert _alpha(rep, xs[3] + bar / 2, BASE - view_mod.BAR_HEIGHT + 2) > 0.5
        assert all(_alpha(rep, x + bar / 2, BASE - 1.5) > 0.5 for x in xs)
        assert _alpha(rep, xs[0] - 4, BASE - 2) == 0  # nothing left of the first bar
    assert images[0] != images[1]  # light and dark draw differently
    assert view_mod._failed is False


def test_an_empty_day_draws_a_one_point_stub():
    view = view_mod.make_view(build_chart(_cost({A1: (5, 0, 3, 4, 2, 6, 1)})))
    rep = _bitmap(view, dark=False)
    xs, bar = _columns(view)
    assert _alpha(rep, xs[1] + bar / 2, BASE - 0.5) > 0.2  # the stub on the baseline
    assert _alpha(rep, xs[1] + bar / 2, BASE - 2.5) == 0  # and nothing above it


def test_the_legend_fits_the_view_whatever_its_width():
    _caption, _day, legend_font = view_mod._fonts()
    for week in (SEVEN, LONG, {A1: (1,) * 7, A2: (2,) * 7}):
        chart = build_chart(_cost(week))
        view = view_mod.make_view(chart)
        for width in (view.frame().size.width, 380.0, 520.0):
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
    y = BASE + view_mod.LABEL_GAP + view_mod._line_height(_day) + view_mod.LEGEND_GAP
    y += view_mod._line_height(legend_font) / 2
    x = left
    for name, entry in zip(names, chart.legend):
        assert _alpha(rep, x + view_mod.DOT / 2, y) > 0.5, name
        x += view_mod.DOT + view_mod.DOT_GAP
        x += view_mod._entry_text(name, entry.amount_text, legend_font).size().width + view_mod.ENTRY_GAP
    assert x - view_mod.ENTRY_GAP <= 460.0 - view_mod.RIGHT_INSET
