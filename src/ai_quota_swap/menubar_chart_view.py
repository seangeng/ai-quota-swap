"""Draws the cost row's chart (:class:`menubar_chart.UsageChart`) as the
menu item's view. AppKit only: ``menubar_app`` imports it when it draws a
chart row, after ``rumps`` (and so AppKit) imported.

The view draws only from the description it was given: no file, no
database, no lock. Every colour is a system colour looked up while drawing,
so light and dark follow the menu's appearance with no theme code. Its width
follows the menu (the menu stretches a width-sizable view to its own width),
its height is fixed.

Where each part goes comes from ``menubar_chart`` (:func:`frame`,
:func:`layout_columns`, :func:`legend_width`), which the demo harness's HTML
drawing of the menu uses too. Only the insets are this view's own.
``NSMenu.size`` on macOS 26 pads a row's title by 32 in all, taken here as
16 on each side, and adds a 14 point check-mark column before the titles
once any row has a check mark. So the caption and the chart start 16 points
in, 30 once the menu shows check marks (as it does while an account is
active), to line up with the titles above. NSMenu adds that column to a
view's width as well (a view W wide in a menu with a checked row makes the
menu W + 14, measured off screen on macOS 27), so the width the view asks
for leaves the column out (:func:`min_width`). None of these numbers is
public API; they are measured, not promised, and only off screen.
"""

from __future__ import annotations

import logging

import AppKit

from ai_quota_swap.menubar_chart import (
    ACCOUNT_COLOURS,
    CAPTION_BAND,
    DOT,
    DOT_GAP,
    ENTRY_GAP,
    OTHER_COLOUR,
    UsageChart,
    frame,
    layout_columns,
    legend_width,
)

_logger = logging.getLogger("ai-quota-swap")

TITLE_INSET = 16.0
STATE_COLUMN = 14.0
RIGHT_INSET = 16.0
DAY_FONT_SIZE = 10.0
LEGEND_FONT_SIZE = 11.0

#: Set by a failure while drawing: from then on :func:`make_view` refuses,
#: so the menu shows the plain text row until the app restarts.
_failed = False


def _fonts():
    return (
        AppKit.NSFont.menuFontOfSize_(0),
        AppKit.NSFont.systemFontOfSize_(DAY_FONT_SIZE),
        AppKit.NSFont.systemFontOfSize_(LEGEND_FONT_SIZE),
    )


def _text(text: str, font, colour) -> AppKit.NSAttributedString:
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: colour},
    )


def _line_height(font) -> float:
    return float(_text("Mg", font, AppKit.NSColor.labelColor()).size().height)


def _frame():
    _caption, day, legend = _fonts()
    return frame(_line_height(day), _line_height(legend))


def height() -> float:
    """The view's height: the caption's row, the bars, the weekday letters
    and the legend line (:func:`menubar_chart.frame`)."""
    return _frame().height


def _colour(index: int):
    if index == OTHER_COLOUR or not 0 <= index < len(ACCOUNT_COLOURS):
        return AppKit.NSColor.systemGrayColor()
    name = ACCOUNT_COLOURS[index]
    return getattr(AppKit.NSColor, f"system{name[:1].upper()}{name[1:]}Color")()


def _entry_text(name: str, amount: str, font) -> AppKit.NSAttributedString:
    """``#2 work $41``: the name in the secondary label colour, the amount
    in the primary one."""
    text = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(
        _text(name + " ", font, AppKit.NSColor.secondaryLabelColor())
    )
    text.appendAttributedString_(_text(amount, font, AppKit.NSColor.labelColor()))
    return text


def _legend_width(chart: UsageChart, names, font) -> float:
    return legend_width([
        float(_entry_text(n, e.amount_text, font).size().width) for n, e in zip(names, chart.legend)
    ])


def _top_rounded(x: float, y: float, w: float, h: float, radius: float):
    """A rectangle with its top corners rounded (y grows downwards)."""
    path = AppKit.NSBezierPath.bezierPath()
    r = max(0.0, min(radius, w / 2, h))
    path.moveToPoint_((x, y + h))
    path.lineToPoint_((x, y + r))
    path.appendBezierPathWithArcFromPoint_toPoint_radius_((x, y), (x + r, y), r)
    path.lineToPoint_((x + w - r, y))
    path.appendBezierPathWithArcFromPoint_toPoint_radius_((x + w, y), (x + w, y + r), r)
    path.lineToPoint_((x + w, y + h))
    path.closePath()
    return path


def _draw(view, chart: UsageChart, left: float) -> None:
    caption_font, day_font, legend_font = _fonts()
    bounds = view.bounds()
    width = float(bounds.size.width)
    right = width - RIGHT_INSET
    inner = max(0.0, right - left)
    secondary = AppKit.NSColor.secondaryLabelColor()

    # The caption: the cost row's sentence, cut rather than wrapped.
    caption = _text(chart.caption, caption_font, secondary)
    style = AppKit.NSMutableParagraphStyle.alloc().init()
    style.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    caption = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(caption)
    caption.addAttribute_value_range_(AppKit.NSParagraphStyleAttributeName, style, (0, caption.length()))
    line = _line_height(caption_font)
    caption.drawInRect_(((left, (CAPTION_BAND - line) / 2), (inner, line)))

    # The columns: oldest left, today right, the tallest day full height,
    # laid out by menubar_chart.layout_columns as the HTML drawing does.
    # Each column is clipped to one outline with its top corners rounded,
    # and its segments are plain rectangles inside it, so every bar has the
    # same corners whatever its top segment's height.
    bands = _frame()
    for col in layout_columns(chart, left=left, width=inner, base=bands.base):
        if col.stub is not None:
            AppKit.NSColor.tertiaryLabelColor().setFill()
            AppKit.NSBezierPath.fillRect_(((col.stub.x, col.stub.y), (col.stub.w, col.stub.h)))
        if col.outline is not None:
            o = col.outline
            AppKit.NSGraphicsContext.saveGraphicsState()
            try:
                _top_rounded(o.x, o.y, o.w, o.h, col.radius).addClip()
                for part in col.segments:
                    b = part.box
                    _colour(part.segment.colour).setFill()
                    AppKit.NSBezierPath.fillRect_(((b.x, b.y), (b.w, b.h)))
            finally:
                AppKit.NSGraphicsContext.restoreGraphicsState()
        # The weekday letter, centred under its bar, today's in the primary
        # label colour.
        colour = AppKit.NSColor.labelColor() if col.column.today else secondary
        letter = _text(col.column.label, day_font, colour)
        lw = float(letter.size().width)
        letter.drawAtPoint_((col.centre - lw / 2, bands.letters_top))

    # The legend: one line, names cut until it fits this width.
    y = bands.legend_top
    legend_line = _line_height(legend_font)
    names = chart.fit_legend(lambda ns: _legend_width(chart, ns, legend_font) <= inner)
    x = left
    for name, entry in zip(names, chart.legend):
        text = _entry_text(name, entry.amount_text, legend_font)
        tw = float(text.size().width)
        if x + DOT + DOT_GAP + tw > right + 0.5:
            break  # never a cut-off entry; the view is made wide enough for all
        _colour(entry.colour).setFill()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            ((x, y + (legend_line - DOT) / 2), (DOT, DOT))
        ).fill()
        x += DOT + DOT_GAP
        text.drawAtPoint_((x, y))
        x += tw + ENTRY_GAP


def _draw_caption_only(view, chart: UsageChart, left: float) -> None:
    """What the text row says, for a chart that could not be drawn."""
    caption_font = AppKit.NSFont.menuFontOfSize_(0)
    line = _line_height(caption_font)
    _text(chart.caption, caption_font, AppKit.NSColor.secondaryLabelColor()).drawAtPoint_(
        (left, (CAPTION_BAND - line) / 2)
    )


def _menu_has_check_marks(view) -> bool | None:
    item = view.enclosingMenuItem()
    menu = item.menu() if item is not None else None
    if menu is None:
        return None
    return any(i.state() != AppKit.NSControlStateValueOff for i in menu.itemArray())


class QswapUsageChartView(AppKit.NSView):
    """The chart. Made by :func:`make_view`; draws ``self.chart``."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        global _failed
        left = TITLE_INSET + STATE_COLUMN
        try:
            marks = _menu_has_check_marks(self)
            if marks is None:
                marks = self.check_marks
            left = TITLE_INSET + (STATE_COLUMN if marks else 0.0)
            _draw(self, self.chart, left)
        except Exception:
            # Never cost the menu: say what the text row says, and make the
            # text row from the next rebuild on.
            _failed = True
            _logger.debug("menu bar: the usage chart could not be drawn", exc_info=True)
            try:
                _draw_caption_only(self, self.chart, left)
            except Exception:
                pass


def min_width(chart: UsageChart) -> float:
    """The width the view asks its menu for: the insets, and room for the
    caption and for the legend line at its shortest forms. It leaves out
    the check-mark column: once a row has a check mark NSMenu adds those
    :data:`STATE_COLUMN` points to the view's width by itself, and the view
    then draws that much further in. The menu is at least this wide,
    usually wider (its account rows)."""
    caption_font, _day, legend_font = _fonts()
    caption = float(_text(chart.caption, caption_font, AppKit.NSColor.labelColor()).size().width)
    shortest = chart.fit_legend(lambda _names: False)
    legend = _legend_width(chart, shortest, legend_font)
    return TITLE_INSET + max(caption, legend) + RIGHT_INSET


def menu_width(chart: UsageChart, *, check_marks: bool = True) -> float:
    """The narrowest the view is drawn: :func:`min_width`, and the
    check-mark column NSMenu adds to it when ``check_marks``. For drawing
    it off screen at a menu's width."""
    return min_width(chart) + (STATE_COLUMN if check_marks else 0.0)


def make_view(chart: UsageChart, *, check_marks: bool = True) -> QswapUsageChartView:
    """A view drawing ``chart``, :func:`min_width` wide and stretched to the
    menu's width by the menu. ``check_marks``: whether the menu shows the
    check-mark column, for a view that cannot ask its menu (off screen); in
    a menu it asks. Raises after a drawing failure, so the caller shows the
    text row."""
    if _failed:
        raise RuntimeError("the usage chart failed to draw earlier")
    width = min_width(chart)
    view = QswapUsageChartView.alloc().initWithFrame_(((0, 0), (width, height())))
    view.chart = chart
    view.check_marks = check_marks
    view.setAutoresizingMask_(AppKit.NSViewWidthSizable)
    view.setAccessibilityElement_(True)
    view.setAccessibilityRole_(AppKit.NSAccessibilityImageRole)
    view.setAccessibilityLabel_(chart.accessibility_label())
    return view
