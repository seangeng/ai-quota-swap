"""Draw the macOS menu bar's menu as a self-contained HTML page.

The rows are the app's own: ``ai_quota_swap.menubar.build_menu`` describes
the whole menu (title, sections, rows, submenus), and ``menubar_app`` turns
each row into a native item. This module draws that description the way the
app's items look on a Mac, at 2x: the right-hand end of the menu bar with
qswap's title (the status item is text only, ``⇄ personal · 46% · 38%``), the
menu open under it, and optionally one chain of submenus beside it.

What it mirrors from ``menubar_app._item`` and ``_draw``:

* a row with no action and no children is a disabled item (greyed): the
  information rows and the section headings alike;
* ``checked`` is the item's state, a check mark in the state column, which
  every row keeps room for;
* ``attention`` draws the title in the system orange (no extra glyph: the
  ``⚠`` in ``Menu.outline`` is the outline's own marker);
* ``children`` make a submenu (a chevron at the right), separators are
  separator items;
* the open submenu's parent row is selected, as AppKit draws it;
* a row with a ``chart`` (the cost row) is drawn as the chart, as
  ``menubar_chart_view`` draws it: the caption, the columns stacked by
  account, the weekday letters and the legend line, laid out by the same
  ``menubar_chart.layout_columns`` in inline SVG, in the system colours'
  light and dark values (:data:`SYSTEM_COLOURS`).

It can also draw one notification banner, top right under the bar, as macOS
shows them: its title and body are a ``menubar_notify.Notification`` the
caller got from the app's own ``Notifier``. The sender's icon is a neutral
placeholder (the wrapper app has none of its own).

A status item near the right-hand end of the bar gets its menu pushed left
to stay on screen, so submenus open to the left. That is what this draws.

No external fonts, scripts or images: the page renders the same offline and
the demo sandbox refuses the network. Not imported by anything in
``ai_quota_swap``; it reads only the attributes of the menu it is handed,
and lays the chart out with ``menubar_chart``'s pure functions.

A page has no script, so the browser cannot say how wide the menu's rows
are; the chart needs that width to place its columns and to cut legend
names. :func:`text_width` estimates it from the system font's advances,
measured in the headless Chrome the harness draws with (within 2% of what
that Chrome lays out), and the chart row is made at least as wide as the
widest row, so the menu is as wide as the chart.
"""

from __future__ import annotations

import base64
import html
import struct
from dataclasses import dataclass
from typing import Sequence

# Metrics in CSS px (the page is captured at 2x).
BAR_H = 24
MENU_GAP = 2  # between the bar and the menu's top edge
PAD_V = 5  # the panel's padding above the first row and below the last
ROW_H = 22
SEP_H = 11
SUB_OVERLAP = 4  # a submenu overlaps its parent's edge
EDGE = 8  # the menu stays this far from the screen's right edge
LEFT_ROOM = 96  # wallpaper left of the menu
SUB_LEFT_ROOM = 28  # ... and left of an open submenu
BOTTOM_ROOM = 34
NOTE_W = 344  # a notification banner's width
NOTE_TOP = 6  # ... its gap under the bar
NOTE_RIGHT = 10  # ... and from the screen's right edge
ITEM_LEFT = 17  # a row's title starts this far into the row (the check column)
ITEM_RIGHT = 18  # ... and ends this far from its right edge
SUBMENU_RIGHT = 34  # ... a submenu row's, to leave room for its chevron
DAY_LINE = 12  # the chart's weekday letters: 10 px type on a 12 px line
LEGEND_LINE = 13  # its legend: 11 px type on a 13 px line

#: The system colours the chart's AppKit view draws with, as NSColor gives
#: them on macOS 26 in the light (Aqua) and dark (DarkAqua) appearance.
SYSTEM_COLOURS = {
    #                light        dark
    "blue": ("#0088ff", "#0091ff"),
    "orange": ("#ff8d28", "#ff9230"),
    "purple": ("#cb30e0", "#db34f2"),
    "teal": ("#00c3d0", "#00d2e0"),
    "green": ("#34c759", "#30d158"),
    "brown": ("#ac7f5e", "#b78a66"),
    "indigo": ("#6155f5", "#6d7cff"),
    "yellow": ("#ffcc00", "#ffd600"),
    "gray": ("#8e8e93", "#98989d"),  # "other"
    "label": ("#000000d8", "#ffffffd8"),  # amounts, today's letter
    "secondaryLabel": ("#0000007f", "#ffffff8c"),  # caption, names, letters
    "tertiaryLabel": ("#00000042", "#ffffff3f"),  # a day with nothing
}

# The system font's advances at 13 px, in thousandths of an em, for the
# characters below, as headless Chrome lays out -apple-system; other
# characters count as a whole em. At 11 px each character is
# _TRACKING[11] em wider (the font's own tracking).
_CHARS = "".join(chr(c) for c in range(32, 127)) + "·…→—–’"
_ADVANCES = dict(zip(_CHARS, (
    275, 305, 472, 624, 624, 919, 706, 291, 376, 376, 466, 624, 291, 466, 291, 299,
    624, 458, 598, 621, 638, 612, 631, 582, 633, 631, 291, 291, 624, 624, 624, 507,
    912, 668, 651, 710, 721, 590, 566, 741, 736, 262, 532, 653, 562, 868, 736, 766,
    629, 766, 647, 631, 628, 731, 668, 962, 673, 649, 656, 376, 299, 376, 624, 541,
    494, 546, 608, 554, 608, 565, 356, 604, 583, 241, 241, 537, 247, 864, 578, 585,
    605, 604, 384, 508, 357, 578, 536, 769, 519, 537, 533, 376, 253, 376, 624, 291,
    799, 900, 872, 578, 291,
)))
_TRACKING = {13: 0.0, 11: 0.012}


class UnknownSubmenu(ValueError):
    """``open`` named no submenu of the menu (or of the submenu before it)."""


class UnknownRow(ValueError):
    """``highlight`` matched no clickable row of the menu it looked in."""


@dataclass(frozen=True)
class Backdrop:
    """A window on the desktop behind the menu (the hero: the dashboard).

    ``png`` is a 2x screenshot; it is drawn ``width`` CSS px wide, ``top``
    below the bar, its right edge ``overlap`` px under the menu, ``left`` px
    from the picture's left edge."""

    png: bytes
    width: int = 760
    top: int = 40
    overlap: int = 196  # the reset times stay clear of the menu
    left: int = 32  # wallpaper left of the window
    alt: str = ""

    @property
    def height(self) -> int:
        w, h = struct.unpack(">II", self.png[16:24])  # the IHDR chunk
        return round(self.width * h / w)


THEMES = {
    "dark": {
        "wallpaper": (
            "radial-gradient(120% 90% at 8% 0%, #3d4a8a 0%, rgba(61,74,138,0) 55%),"
            "radial-gradient(90% 80% at 100% 18%, #6b3f6e 0%, rgba(107,63,110,0) 60%),"
            "radial-gradient(110% 90% at 60% 110%, #1f5a63 0%, rgba(31,90,99,0) 60%),"
            "linear-gradient(160deg, #1b1f33 0%, #141622 100%)"
        ),
        "bar_bg": "rgba(16, 17, 26, 0.34)",
        "bar_fg": "rgba(255, 255, 255, 0.94)",
        "item_open": "rgba(255, 255, 255, 0.22)",
        "panel_bg": "rgba(36, 36, 40, 0.74)",
        "panel_ring": "0 0 0 0.5px rgba(0, 0, 0, 0.7), inset 0 0 0 0.5px rgba(255, 255, 255, 0.16)",
        "panel_shadow": "0 14px 36px rgba(0, 0, 0, 0.42), 0 2px 8px rgba(0, 0, 0, 0.25)",
        "text": "rgba(255, 255, 255, 0.88)",
        "dim": "rgba(255, 255, 255, 0.36)",
        "sep": "rgba(255, 255, 255, 0.13)",
        "attention": "#ff9f0a",
        "accent": "#0a6ad8",
        "accent_text": "#ffffff",
        "window_shadow": "0 24px 60px rgba(0, 0, 0, 0.55), 0 0 0 0.5px rgba(0, 0, 0, 0.6)",
        "note_bg": "rgba(44, 44, 48, 0.80)",
        "note_icon": "linear-gradient(180deg, #8a8a90 0%, #5e5e64 100%)",
    },
    "light": {
        "wallpaper": (
            "radial-gradient(120% 90% at 6% 0%, #c9d8ff 0%, rgba(201,216,255,0) 55%),"
            "radial-gradient(90% 80% at 100% 15%, #f6d3e2 0%, rgba(246,211,226,0) 60%),"
            "radial-gradient(110% 90% at 55% 110%, #cdeee8 0%, rgba(205,238,232,0) 60%),"
            "linear-gradient(160deg, #eef0f7 0%, #e7e9f2 100%)"
        ),
        "bar_bg": "rgba(255, 255, 255, 0.42)",
        "bar_fg": "rgba(0, 0, 0, 0.86)",
        "item_open": "rgba(0, 0, 0, 0.11)",
        "panel_bg": "rgba(242, 242, 244, 0.80)",
        "panel_ring": "0 0 0 0.5px rgba(0, 0, 0, 0.2), inset 0 0 0 0.5px rgba(255, 255, 255, 0.7)",
        "panel_shadow": "0 14px 36px rgba(30, 30, 60, 0.22), 0 2px 8px rgba(30, 30, 60, 0.12)",
        "text": "rgba(0, 0, 0, 0.86)",
        # Darker than AppKit's disabled grey, so the greyed rows of a light
        # drawing read about as well as the dark one's (3.2:1 on the panel).
        "dim": "rgba(0, 0, 0, 0.45)",
        "sep": "rgba(0, 0, 0, 0.11)",
        "attention": "#e07800",
        "accent": "#0a6ad8",
        "accent_text": "#ffffff",
        "window_shadow": "0 24px 60px rgba(40, 40, 80, 0.28), 0 0 0 0.5px rgba(0, 0, 0, 0.18)",
        "note_bg": "rgba(246, 246, 248, 0.86)",
        "note_icon": "linear-gradient(180deg, #c4c4ca 0%, #9c9ca3 100%)",
    },
}

CHECK = (
    '<svg class="check" viewBox="0 0 12 10" aria-hidden="true">'
    '<path d="M1.2 5.4 4.4 8.5 10.8 1.4" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
CHEVRON = (
    '<svg class="chevron" viewBox="0 0 7 12" aria-hidden="true">'
    '<path d="M1.3 1.3 5.7 6 1.3 10.7" fill="none" stroke="currentColor" '
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

# Other status items: neutral glyphs, no app's own icon.
GLYPHS = {
    "other": (
        '<svg viewBox="0 0 16 16"><rect x="2.5" y="3" width="11" height="10" rx="2.5" '
        'fill="none" stroke="currentColor" stroke-width="1.3"/>'
        '<path d="M5 8h6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>'
    ),
    "battery": (
        '<svg viewBox="0 0 26 14"><rect x="1" y="2" width="21" height="10" rx="3" fill="none" '
        'stroke="currentColor" stroke-width="1.1" opacity=".55"/>'
        '<rect x="2.8" y="3.8" width="14" height="6.4" rx="1.6" fill="currentColor"/>'
        '<path d="M23.6 5.5v3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" opacity=".55"/></svg>'
    ),
    "wifi": (
        '<svg viewBox="0 0 18 14"><path d="M1.6 5.2a10.8 10.8 0 0 1 14.8 0M4.2 8a7 7 0 0 1 9.6 0'
        'M6.8 10.7a3.2 3.2 0 0 1 4.4 0" fill="none" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round"/><circle cx="9" cy="12.6" r="0.9" fill="currentColor"/></svg>'
    ),
    "search": (
        '<svg viewBox="0 0 16 16"><circle cx="6.8" cy="6.8" r="4.6" fill="none" stroke="currentColor" '
        'stroke-width="1.5"/><path d="m10.3 10.3 3.8 3.8" stroke="currentColor" stroke-width="1.7" '
        'stroke-linecap="round"/></svg>'
    ),
    "controls": (
        '<svg viewBox="0 0 18 16"><rect x="1.5" y="2" width="15" height="5" rx="2.5" fill="none" '
        'stroke="currentColor" stroke-width="1.2"/><circle cx="12.5" cy="4.5" r="1.5" fill="currentColor"/>'
        '<rect x="1.5" y="9" width="15" height="5" rx="2.5" fill="none" stroke="currentColor" '
        'stroke-width="1.2"/><circle cx="5.5" cy="11.5" r="1.5" fill="currentColor"/></svg>'
    ),
}


def text_width(text: str, size: int, *, letter_spacing: float = 0.0) -> float:
    """About how wide ``text`` is in the system font at ``size`` px (13 or
    11), with ``letter_spacing`` px after each character; a little more
    rather than less."""
    em = sum(_ADVANCES.get(c, 1000) for c in text) / 1000 + _TRACKING[size] * len(text)
    return em * size + letter_spacing * len(text)


def _row_width(row) -> float:
    """A title row's width inside the menu's side margins."""
    right = SUBMENU_RIGHT if row.children else ITEM_RIGHT
    return ITEM_LEFT + text_width(row.text, 13, letter_spacing=-0.08) + right


def _legend_text_width(name: str, entry) -> float:
    return text_width(f"{name} {entry.amount_text}", 11)


def _chart_frame():
    from ai_quota_swap.menubar_chart import frame

    return frame(DAY_LINE, LEGEND_LINE)


def chart_row_width(chart, rows: Sequence) -> int:
    """The chart row's width: as wide as the widest other row of its menu,
    and wide enough for its caption and its legend with names as short as
    they go (as ``menubar_chart_view.min_width``), so it spans the menu."""
    from ai_quota_swap.menubar_chart import legend_width

    widest = max((_row_width(r) for r in rows if not r.separator and r.chart is None), default=0.0)
    shortest = chart.fit_legend(lambda _names: False)
    legend = legend_width([_legend_text_width(n, e) for n, e in zip(shortest, chart.legend)])
    caption = text_width(chart.caption, 13, letter_spacing=-0.08)
    own = ITEM_LEFT + max(caption, legend) + ITEM_RIGHT
    return int(-(-max(widest, own) // 1)) + 2  # a little more rather than less


def _colour_class(index: int) -> str:
    return "cx" if index < 0 else f"c{index}"


def _top_rounded_path(x: float, y: float, w: float, h: float, radius: float) -> str:
    """A rectangle with its top corners rounded, as the AppKit view draws
    a column's top segment."""
    r = max(0.0, min(radius, w / 2, h))
    return (f"M{x:g} {y + h:g}V{y + r:g}A{r:g} {r:g} 0 0 1 {x + r:g} {y:g}H{x + w - r:g}"
            f"A{r:g} {r:g} 0 0 1 {x + w:g} {y + r:g}V{y + h:g}Z")


def _chart_html(row, width: int) -> str:
    """The cost row's chart, ``width`` px wide: the caption and the legend
    as text, the columns and weekday letters as SVG."""
    from ai_quota_swap.menubar_chart import (
        CORNER, WEEKDAY_NAMES, layout_columns, legend_width,
    )

    chart = row.chart
    bands = _chart_frame()
    inner = width - ITEM_LEFT - ITEM_RIGHT
    days = []
    for col in layout_columns(chart, left=ITEM_LEFT, width=inner, base=bands.base):
        c = col.column
        parts = []
        if col.stub is not None:
            b = col.stub
            parts.append(f'<rect class="stub" x="{b.x:g}" y="{b.y:g}" width="{b.w:g}" height="{b.h:g}"/>')
        for part in col.segments:
            b, cls = part.box, f"seg {_colour_class(part.segment.colour)}"
            if part.top:
                parts.append(f'<path class="{cls}" d="{_top_rounded_path(b.x, b.y, b.w, b.h, CORNER)}"/>')
            else:
                parts.append(f'<rect class="{cls}" x="{b.x:g}" y="{b.y:g}" width="{b.w:g}" height="{b.h:g}"/>')
        letter = "letter today" if c.today else "letter"
        parts.append(f'<text class="{letter}" x="{col.centre:g}" y="{bands.letters_top + DAY_LINE / 2:g}">'
                     f"{html.escape(c.label)}</text>")
        total = next(text for column, text in chart.day_totals() if column is c)
        days.append(f'<g class="day" data-day="{WEEKDAY_NAMES[c.day.weekday()][:3]}" '
                    f'data-total="{html.escape(total, quote=True)}">{"".join(parts)}</g>')
    names = chart.fit_legend(lambda ns: legend_width(
        [_legend_text_width(n, e) for n, e in zip(ns, chart.legend)]) <= inner)
    entries = "".join(
        f'<span class="entry" data-name="{html.escape(e.name, quote=True)}" '
        f'data-amount="{html.escape(e.amount_text, quote=True)}">'
        f'<i class="dot {_colour_class(e.colour)}"></i><span class="lt">'
        f'<span class="name">{html.escape(n)}</span> <span class="amt">{html.escape(e.amount_text)}</span>'
        "</span></span>"
        for n, e in zip(names, chart.legend)
    )
    return (
        f'<div class="chart" role="img" aria-label="{html.escape(chart.accessibility_label(), quote=True)}" '
        f'style="width:{width}px;height:{bands.height:g}px">'
        f'<div class="cap" style="width:{inner}px">{html.escape(chart.caption)}</div>'
        f'<svg class="bars" width="{width}" height="{bands.height:g}" viewBox="0 0 {width} {bands.height:g}" '
        f'aria-hidden="true">{"".join(days)}</svg>'
        f'<div class="legend" style="top:{bands.legend_top:g}px">{entries}</div></div>'
    )


def _chart_css(theme: str) -> str:
    from ai_quota_swap.menubar_chart import ACCOUNT_COLOURS, CAPTION_BAND, DOT, DOT_GAP, ENTRY_GAP

    side = 0 if theme == "light" else 1
    colour = {name: pair[side] for name, pair in SYSTEM_COLOURS.items()}
    fills = "".join(
        f"  .c{i} {{ fill: {colour[name]}; background: {colour[name]}; }}\n"
        for i, name in enumerate(ACCOUNT_COLOURS)
    )
    return f"""  .chart {{ position: relative; margin: 0 5px; box-sizing: border-box; }}
  .chart .bars {{ position: absolute; left: 0; top: 0; overflow: visible; }}
  .chart .cap {{
    position: absolute; left: {ITEM_LEFT}px; top: 0; height: {CAPTION_BAND:g}px;
    line-height: {CAPTION_BAND:g}px; letter-spacing: -0.08px; color: {colour["secondaryLabel"]};
    overflow: hidden; text-overflow: ellipsis;
  }}
  .chart .stub {{ fill: {colour["tertiaryLabel"]}; }}
  .chart .letter {{ font-size: 10px; fill: {colour["secondaryLabel"]}; text-anchor: middle;
                    dominant-baseline: central; }}
  .chart .letter.today {{ fill: {colour["label"]}; }}
  .chart .legend {{
    position: absolute; left: {ITEM_LEFT}px; height: {LEGEND_LINE}px; display: flex;
    gap: {ENTRY_GAP:g}px; font-size: 11px; line-height: {LEGEND_LINE}px;
  }}
  .chart .entry {{ display: flex; align-items: center; gap: {DOT_GAP:g}px; }}
  .chart .dot {{ display: block; width: {DOT:g}px; height: {DOT:g}px; border-radius: 50%; }}
  .chart .name {{ color: {colour["secondaryLabel"]}; }}
  .chart .amt {{ color: {colour["label"]}; }}
{fills}  .cx {{ fill: {colour["gray"]}; background: {colour["gray"]}; }}
"""


def resolve(menu, open_path: Sequence[str] = ()) -> list[tuple[str | None, list]]:
    """The menus drawn, outermost first: ``[(None, main rows), (title, its
    rows), ...]``. Each name in ``open_path`` must be the exact text of a
    submenu row in the menu before it; anything else raises
    :class:`UnknownSubmenu` naming the submenus there are."""
    levels: list[tuple[str | None, list]] = [(None, list(menu.rows()))]
    for name in open_path:
        rows = levels[-1][1]
        hit = next((r for r in rows if not r.separator and r.children and r.text == name), None)
        if hit is None:
            where = "the menu" if levels[-1][0] is None else f"submenu {levels[-1][0]!r}"
            there = [r.text for r in rows if not r.separator and r.children]
            raise UnknownSubmenu(
                f"no submenu {name!r} in {where}; its submenus are: "
                + (", ".join(repr(t) for t in there) or "none")
            )
        levels.append((name, list(hit.children)))
    return levels


def _row_h(row) -> float:
    if row.separator:
        return SEP_H
    if getattr(row, "chart", None) is not None:
        return _chart_frame().height
    return ROW_H


def _row_top(rows: Sequence, index: int) -> float:
    """Where row ``index`` starts, below the panel's top padding."""
    return sum(_row_h(r) for r in rows[:index])


def _height(rows: Sequence) -> int:
    return 2 * PAD_V + _row_top(rows, len(rows))


def _row_html(row, *, selected: bool, chart_width: int = 0) -> str:
    if row.separator:
        return '<div class="sep" role="separator"></div>'
    if getattr(row, "chart", None) is not None:
        return _chart_html(row, chart_width)
    classes = ["item"]
    if row.checked:
        classes.append("checked")
    if row.heading:
        classes.append("heading")
    elif row.disabled:
        classes.append("disabled")
    if row.attention:
        classes.append("attention")
    if row.children:
        classes.append("submenu")
    if selected:
        classes.append("selected")
    check = CHECK if row.checked else ""
    chevron = CHEVRON if row.children else ""
    return (
        f'<div class="{" ".join(classes)}">{check}'
        f'<span class="t">{html.escape(row.text)}</span>{chevron}</div>'
    )


def _menu_html(rows: Sequence, *, level: int, title: str | None, selected: int | None,
               margin_top: int) -> str:
    chart_width = next(
        (chart_row_width(r.chart, rows) for r in rows if getattr(r, "chart", None) is not None), 0,
    )
    body = "".join(
        _row_html(r, selected=(i == selected), chart_width=chart_width) for i, r in enumerate(rows)
    )
    name = "" if title is None else f' data-title="{html.escape(title, quote=True)}"'
    return (
        f'<div class="menu" data-level="{level}"{name} '
        f'style="margin-top:{margin_top:g}px;z-index:{10 + level}">{body}</div>'
    )


def _index_of(rows: Sequence, text: str, *, prefix: bool) -> int | None:
    for i, r in enumerate(rows):
        if r.separator:
            continue
        if r.text == text or (prefix and r.text.startswith(text)):
            return i
    return None


def page(
    menu,
    *,
    theme: str = "dark",
    open_path: Sequence[str] = (),
    highlight: str | None = None,
    clock: str = "",
    closed: bool = False,
    backdrop: Backdrop | None = None,
    size: tuple[int, int] | None = None,
    caption: str = "",
    notification=None,
) -> str:
    """The page. ``open_path``: the submenus open, outermost first (each
    parent row drawn selected). ``highlight``: a row of the innermost open
    menu drawn under the pointer (its text, or the start of it; information
    rows cannot be). ``closed``: the menu laid out but not shown, so a
    closed frame lines up with an open one. ``size``: the stage in CSS px,
    for frames that must all match. ``notification``: a banner with its
    ``title`` and ``body`` (a ``menubar_notify.Notification``)."""
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}")
    c = THEMES[theme]
    levels = resolve(menu, open_path)
    selected: dict[int, int] = {}
    for depth in range(len(levels) - 1):
        rows = levels[depth][1]
        selected[depth] = _index_of(rows, levels[depth + 1][0], prefix=False)
    if highlight is not None:
        rows = levels[-1][1]
        i = _index_of(rows, highlight, prefix=True)
        if i is None or rows[i].disabled:
            raise UnknownRow(f"no clickable row starting {highlight!r} in "
                             + ("the menu" if levels[-1][0] is None else f"submenu {levels[-1][0]!r}"))
        selected[len(levels) - 1] = i

    # Each submenu's first row lines up with its parent row.
    tops = [MENU_GAP]
    for depth in range(1, len(levels)):
        parent_rows = levels[depth - 1][1]
        tops.append(tops[-1] + _row_top(parent_rows, selected[depth - 1]))
    menus = [
        _menu_html(rows, level=depth, title=title, selected=selected.get(depth), margin_top=tops[depth])
        for depth, (title, rows) in enumerate(levels)
    ]
    menus.reverse()  # drawn left to right: deepest submenu first, the menu last

    window = ""
    if backdrop is not None:
        data = base64.b64encode(backdrop.png).decode()
        window = (
            f'<img class="window" alt="{html.escape(backdrop.alt, quote=True)}" '
            f'src="data:image/png;base64,{data}" style="width:{backdrop.width}px;'
            f'height:{backdrop.height}px;margin-top:{backdrop.top}px;'
            f'margin-right:-{backdrop.overlap}px">'
        )
    stage_size = f"width:{size[0]}px;height:{size[1]}px;" if size else ""
    # A submenu sets the left extent itself; a lone menu gets some desktop.
    if backdrop is not None:
        left_room = backdrop.left
    else:
        left_room = SUB_LEFT_ROOM if len(levels) > 1 else LEFT_ROOM
    note = ""
    if notification is not None:
        note = (
            '<div class="note" id="note" role="status"><div class="note-icon"></div>'
            f'<div class="note-text"><div class="note-title">{html.escape(notification.title)}</div>'
            f'<div class="note-body">{html.escape(notification.body)}</div></div></div>'
        )
    title = html.escape(menu.title)
    hidden = " closed" if closed else ""
    glyph = GLYPHS
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<meta name="color-scheme" content="{theme}">
<title>{html.escape(caption or menu.title)}</title>
<style>
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{ display: flex; justify-content: center; align-items: flex-start; }}
  .stage {{
    position: relative; display: inline-flex; flex-direction: column; box-sizing: border-box;
    overflow: hidden; border-radius: 12px; background: {c["wallpaper"]};
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue",
      system-ui, sans-serif;
    font-size: 13px; -webkit-font-smoothing: antialiased; {stage_size}
  }}
  .bar {{
    height: {BAR_H}px; flex: none; display: flex; align-items: center; justify-content: flex-end;
    padding: 0 10px 0 0; background: {c["bar_bg"]}; color: {c["bar_fg"]};
    backdrop-filter: blur(24px) saturate(170%); -webkit-backdrop-filter: blur(24px) saturate(170%);
    font-weight: 500; white-space: nowrap;
  }}
  .bar .glyph {{ width: 32px; height: {BAR_H}px; display: flex; align-items: center;
                 justify-content: center; }}
  .bar .glyph svg {{ height: 14px; width: auto; }}
  .bar .glyph.battery svg {{ height: 13px; }}
  .bar .glyph.other {{ opacity: .9; }}
  .bar .status {{ height: 22px; line-height: 22px; padding: 0 8px; margin: 0 2px;
                  border-radius: 5px; }}
  .bar .status.open {{ background: {c["item_open"]}; }}
  .bar .clock {{ padding: 0 2px 0 10px; font-variant-numeric: tabular-nums; }}
  .desk {{
    flex: 1; display: flex; flex-direction: row; justify-content: flex-end;
    align-items: flex-start; padding: 0 {EDGE}px {BOTTOM_ROOM}px {left_room}px;
  }}
  .window {{ display: block; flex: none; border-radius: 12px; box-shadow: {c["window_shadow"]};
             position: relative; z-index: 1; }}
  .menu {{
    position: relative; flex: none; box-sizing: border-box; min-width: 160px;
    padding: {PAD_V}px 0; border-radius: 10px; background: {c["panel_bg"]};
    backdrop-filter: blur(30px) saturate(180%); -webkit-backdrop-filter: blur(30px) saturate(180%);
    box-shadow: {c["panel_ring"]}, {c["panel_shadow"]}; color: {c["text"]};
    white-space: nowrap; cursor: default;
  }}
  .menu + .menu {{ margin-left: -{SUB_OVERLAP}px; }}
  .closed .menu {{ visibility: hidden; }}
  .item {{
    position: relative; height: {ROW_H}px; line-height: {ROW_H}px; margin: 0 5px;
    padding: 0 18px 0 17px; border-radius: 5px; letter-spacing: -0.08px;
  }}
  .item.submenu {{ padding-right: 34px; }}
  .item.disabled, .item.heading {{ color: {c["dim"]}; }}
  .item.attention {{ color: {c["attention"]}; }}
  .item.selected {{ background: {c["accent"]}; color: {c["accent_text"]}; }}
  .check {{ position: absolute; left: 4px; top: 6px; width: 10px; height: 10px; }}
  .chevron {{ position: absolute; right: 9px; top: 5px; width: 6px; height: 12px; }}
  .sep {{ height: {SEP_H}px; margin: 0 10px; position: relative; }}
  .sep::after {{ content: ""; position: absolute; left: 0; right: 0; top: 5px; height: 1px;
                 background: {c["sep"]}; }}
  .note {{
    position: absolute; z-index: 40; top: {BAR_H + NOTE_TOP}px; right: {NOTE_RIGHT}px;
    width: {NOTE_W}px; box-sizing: border-box; display: flex; gap: 10px; align-items: flex-start;
    padding: 11px 14px 12px 11px; border-radius: 18px; background: {c["note_bg"]};
    backdrop-filter: blur(30px) saturate(180%); -webkit-backdrop-filter: blur(30px) saturate(180%);
    box-shadow: {c["panel_ring"]}, {c["panel_shadow"]}; color: {c["text"]};
    line-height: 16px; letter-spacing: -0.08px;
  }}
  .note-icon {{ flex: none; width: 32px; height: 32px; margin-top: 1px; border-radius: 8px;
                background: {c["note_icon"]}; }}
  .note-title {{ font-weight: 600; }}
{_chart_css(theme)}</style>
</head>
<body>
<div class="stage {theme}{hidden}" id="stage">
  <div class="bar">
    <span class="glyph other">{glyph["other"]}</span>
    <span class="status{"" if closed else " open"}" id="status">{title}</span>
    <span class="glyph battery">{glyph["battery"]}</span>
    <span class="glyph">{glyph["wifi"]}</span>
    <span class="glyph">{glyph["search"]}</span>
    <span class="glyph">{glyph["controls"]}</span>
    <span class="clock">{html.escape(clock)}</span>
  </div>
  <div class="desk">{window}{"".join(menus)}</div>{note}
</div>
</body>
</html>
"""
