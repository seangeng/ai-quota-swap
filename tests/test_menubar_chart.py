"""Tests for the cost row's chart: the pure description
(``menubar_chart``), the cost row and its setting in ``build_menu``, the
per-day read in ``menubar_data.read_cost``, and the app's fallback to the
text row. No AppKit here; the drawing is tested in
``test_menubar_chart_view.py`` where AppKit is installed."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from ai_quota_swap import menubar, menubar_chart, menubar_data
from ai_quota_swap.menubar import MenuBarSettings, Row
from ai_quota_swap.menubar_chart import (
    LEGEND_LIMIT,
    OTHER_COLOUR,
    CostAccount,
    CostDay,
    CostSummary,
    build_chart,
    fit_names,
)
from tests.test_menubar import _C1, _C2, _X1, _X2, _claude, _codex, _inp

TODAY = _dt.date(2026, 11, 10)  # a Tuesday


def _claude_acc(n: int, name: str) -> CostAccount:
    return CostAccount("claude", f"user{n}@example.com|", n, f"#{n} {name}")


def _codex_acc(n: int, name: str) -> CostAccount:
    return CostAccount("codex", f"user{n}@example.com|", n, f"#{n} {name}")


UNATTRIBUTED = CostAccount(None, "", None, "unattributed")


def _cost(rows: dict[CostAccount, tuple[float, ...]]) -> CostSummary:
    """A week of made-up costs: each account's seven days, oldest first."""
    days = []
    for i in range(7):
        costs = tuple((acc, v[i]) for acc, v in rows.items() if v[i] > 0)
        days.append(CostDay(TODAY - _dt.timedelta(days=6 - i), costs))
    today = sum(c for _a, c in days[-1].costs)
    week = sum(c for d in days for _a, c in d.costs)
    return CostSummary(today=round(today, 6), week=round(week, 6), days=tuple(days))


A1, A2, A3, A4, A5 = (_claude_acc(n, nm) for n, nm in (
    (1, "personal"), (2, "work"), (3, "night-shift"), (4, "side"), (5, "ci"),
))
X1, X2 = _codex_acc(1, "maya@example.com"), _codex_acc(2, "work")

SEVEN = {
    A1: (14.2, 18.9, 6.1, 22.4, 19.8, 11.0, 9.3),
    A2: (9.8, 12.1, 0.0, 15.6, 13.2, 8.4, 7.7),
    A3: (4.1, 0.0, 3.2, 6.8, 5.5, 2.9, 1.8),
    A4: (0.0, 2.4, 1.1, 0.0, 3.6, 0.0, 1.2),
    A5: (1.9, 2.2, 0.0, 2.5, 2.1, 1.7, 0.6),
    X1: (3.4, 4.8, 1.2, 5.1, 6.0, 2.2, 3.9),
    X2: (0.0, 1.1, 0.0, 2.0, 0.0, 0.8, 0.0),
}


# --- the description ----------------------------------------------------------------------


def test_seven_columns_oldest_first_today_last_with_weekday_letters():
    chart = build_chart(_cost({A1: (1, 2, 3, 4, 5, 6, 7)}))
    assert [c.day for c in chart.columns] == [TODAY - _dt.timedelta(days=6 - i) for i in range(7)]
    assert [c.label for c in chart.columns] == ["W", "T", "F", "S", "S", "M", "T"]
    assert [c.today for c in chart.columns] == [False] * 6 + [True]
    assert chart.peak == 7


def test_stacking_sums_to_each_days_total():
    cost = _cost(SEVEN)
    chart = build_chart(cost)
    for column, day in zip(chart.columns, cost.days):
        assert column.total == pytest.approx(sum(c for _a, c in day.costs))
    assert sum(c.total for c in chart.columns) == pytest.approx(cost.week)


def test_the_caption_is_the_cost_rows_sentence():
    cost = _cost(SEVEN)
    assert build_chart(cost).caption == cost.text() == "Today $24.50 · 7 days $242.60 (estimated)"


def test_the_legend_names_the_top_four_and_merges_the_rest_into_other():
    chart = build_chart(_cost(SEVEN), both=True)
    names = [e.name for e in chart.legend]
    # By seven-day amount: #1 $101.70, #2 $66.80, Codex #1 $26.60, #3 $24.30.
    assert names == ["#1 personal", "#2 work", "Codex #1 maya@example.com", "#3 night-shift", "other"]
    other = chart.legend[-1]
    assert other.colour == OTHER_COLOUR
    assert other.amount == pytest.approx(sum(sum(SEVEN[a]) for a in (A4, A5, X2)))
    assert [e.amount_text for e in chart.legend] == ["$102", "$67", "$27", "$24", "$23"]
    # "other" sits on top of each column it has a part in, in grey.
    for column in chart.columns:
        keys = [s.key for s in column.segments]
        assert "other" not in keys[:-1]
        if "other" in keys:
            assert column.segments[-1].colour == OTHER_COLOUR
    # Segments stack in the legend's order, the biggest account at the bottom.
    assert [s.key for s in chart.columns[0].segments] == ["claude#1", "claude#2", "codex#1", "claude#3", "other"]


def test_no_other_with_four_accounts_or_fewer():
    chart = build_chart(_cost({a: SEVEN[a] for a in (A1, A2, A3, X1)}))
    assert [e.key for e in chart.legend] == ["claude#1", "claude#2", "codex#1", "claude#3"]
    assert all(s.colour != OTHER_COLOUR for c in chart.columns for s in c.segments)


def test_an_account_too_small_to_see_joins_other_when_other_has_more():
    small = _claude_acc(6, "tiny")
    week = {A1: (50,) * 7, A2: (40,) * 7, A3: (30,) * 7, small: (1,) * 7, A4: (0.5,) * 7, A5: (0.5,) * 7}
    chart = build_chart(_cost(week))
    assert [e.name for e in chart.legend] == ["#1 personal", "#2 work", "#3 night-shift", "other"]
    assert chart.legend[-1].amount == pytest.approx(14.0)
    # Alone it keeps its name: "other" would hold only it.
    chart = build_chart(_cost({A1: (50,) * 7, small: (1,) * 7}))
    assert [e.name for e in chart.legend] == ["#1 personal", "#6 tiny"]


def test_colours_follow_provider_and_number_never_rank():
    chart = build_chart(_cost({A1: SEVEN[A1], A2: SEVEN[A2], A3: SEVEN[A3], X1: SEVEN[X1]}))
    colours = {s.key: s.colour for c in chart.columns for s in c.segments}
    # Claude Code from blue in order, Codex #1 at green.
    names = menubar_chart.ACCOUNT_COLOURS
    assert names == ("blue", "orange", "purple", "teal", "green", "brown", "indigo", "yellow")
    assert colours["claude#1"] == names.index("blue")
    assert colours["claude#2"] == names.index("orange")
    assert colours["claude#3"] == names.index("purple")
    assert colours["codex#1"] == names.index("green")
    assert not {"red", "pink"} & set(names)
    # The same in every column.
    for column in chart.columns:
        for seg in column.segments:
            assert seg.colour == colours[seg.key]
    # A later week where #2 overtakes #1: neither changes colour.
    later = build_chart(_cost({A1: (1,) * 7, A2: (9,) * 7}))
    assert [e.key for e in later.legend] == ["claude#2", "claude#1"]
    assert {e.key: e.colour for e in later.legend} == {"claude#1": colours["claude#1"], "claude#2": colours["claude#2"]}


def test_an_accounts_colour_does_not_depend_on_who_is_in_the_top_four():
    # Claude #5 and Codex #1 both start at green; Claude #5 comes first, so
    # Codex #1 moves to the next free colour. That must hold whether or not
    # Claude #5 is one of the four the legend names: colours are given over
    # every account of the week. (Given over the legend's accounts only,
    # Codex #1 was brown the week #5 made the top four and green the next.)
    week = {A1: (50,) * 7, A2: (40,) * 7, X1: (30,) * 7, A5: (20,) * 7, A3: (1,) * 7, A4: (1,) * 7}
    with_five = build_chart(_cost(week))
    assert "claude#5" in [e.key for e in with_five.legend]
    week.update({A5: (1,) * 7, A3: (20,) * 7})
    without_five = build_chart(_cost(week))
    assert "claude#5" not in [e.key for e in without_five.legend]

    def colour(chart, key):
        return next(e.colour for e in chart.legend if e.key == key)

    names = menubar_chart.ACCOUNT_COLOURS
    assert colour(with_five, "codex#1") == colour(without_five, "codex#1") == names.index("brown")
    assert colour(with_five, "claude#5") == names.index("green")


def test_two_accounts_that_would_share_a_colour_are_told_apart():
    a9 = _claude_acc(9, "ninth")  # wraps round to blue, #1's
    chart = build_chart(_cost({A1: (5,) * 7, a9: (4,) * 7}))
    assert len({e.colour for e in chart.legend}) == 2
    assert {e.key: e.colour for e in chart.legend}["claude#1"] == 0  # the earlier keeps it


def test_an_empty_day_is_a_column_with_nothing_in_it():
    chart = build_chart(_cost({A1: (5, 0, 0, 3, 0, 4, 1)}))
    assert [len(c.segments) for c in chart.columns] == [1, 0, 0, 1, 0, 1, 1]
    assert chart.columns[1].total == 0


def test_a_single_day_of_data_and_one_day_ten_times_the_others():
    chart = build_chart(_cost({A1: (0, 0, 0, 0, 0, 0, 6.85)}))
    assert [c.total for c in chart.columns] == [0] * 6 + [6.85]
    assert chart.peak == 6.85
    spike = build_chart(_cost({A1: (4, 4, 40, 4, 4, 4, 4)}))
    assert spike.peak == 40 and spike.columns[2].total == 40


def test_unattributed_usage_is_its_own_segment_named_as_qswap_usage_names_it():
    chart = build_chart(_cost({A1: (5,) * 7, UNATTRIBUTED: (1,) * 7}))
    assert [e.name for e in chart.legend] == ["#1 personal", "unattributed"]
    assert chart.columns[0].segments[1].key == "unattributed"
    # Outside the top four it counts toward "other".
    week = {A1: (9,) * 7, A2: (8,) * 7, A3: (7,) * 7, A4: (6,) * 7, UNATTRIBUTED: (1,) * 7}
    chart = build_chart(_cost(week))
    assert "unattributed" not in [e.name for e in chart.legend]
    assert chart.legend[-1].name == "other" and chart.legend[-1].amount == pytest.approx(7.0)


def test_with_both_providers_every_codex_entry_says_codex_and_no_claude_entry_says_claude():
    # The menu shows both providers: a Codex entry starts with "Codex",
    # with its number in the part a cut keeps, whatever the other names.
    chart = build_chart(_cost({A1: (5,) * 7, X1: (4,) * 7}), both=True)
    assert [e.name for e in chart.legend] == ["#1 personal", "Codex #1 maya@example.com"]
    assert [e.head for e in chart.legend] == [len("#1"), len("Codex #1")]
    chart = build_chart(_cost({A2: (5,) * 7, X2: (4,) * 7}), both=True)
    assert [e.name for e in chart.legend] == ["#2 work", "Codex #2 work"]
    assert [e.head for e in chart.legend] == [len("#2"), len("Codex #2")]
    # The menu shows Claude Code only: no entry has a provider word.
    chart = build_chart(_cost({A1: (5,) * 7, A2: (4,) * 7}))
    assert [e.name for e in chart.legend] == ["#1 personal", "#2 work"]
    chart = build_chart(_cost({A1: (5,) * 7, X1: (4,) * 7}), both=False)
    assert [e.name for e in chart.legend] == ["#1 personal", "#1 maya@example.com"]
    # The rows' names win over the ones the read carried (an alias set since).
    chart = build_chart(_cost({A2: (5,) * 7}), names={("claude", "2"): "#2 renamed"})
    assert chart.legend[0].name == "#2 renamed"


def test_the_provider_word_goes_by_provider_not_by_a_clash_of_names():
    # Codex #2 "work" leads; Claude Code's #2 "work" is merged into "other",
    # or has no usage this week. Only the Codex entry says its provider,
    # and a Claude Code entry never does, clash or not.
    week = {X2: (50,) * 7, A1: (40,) * 7, A3: (30,) * 7, X1: (20,) * 7, A2: (1,) * 7, A4: (1,) * 7}
    chart = build_chart(_cost(week), both=True)
    assert "claude#2" not in [e.key for e in chart.legend]
    assert [e.name for e in chart.legend] == [
        "Codex #2 work", "#1 personal", "#3 night-shift", "Codex #1 maya@example.com", "other",
    ]
    del week[A2]
    rows = {("claude", "2"): "#2 work", ("codex", "2"): "#2 work", ("claude", "1"): "#1 personal"}
    chart = build_chart(_cost(week), names=rows, both=True)
    assert chart.legend[0].name == "Codex #2 work" and chart.legend[1].name == "#1 personal"


def test_entries_that_are_not_a_managed_account_are_named_as_before():
    # Usage with no account, and a login qswap does not manage, carry no
    # number; the provider is added only to two such logins that read the
    # same, one per provider, with or without both providers in the menu.
    stranger = CostAccount("claude", "jordan@example.org|", None, "jordan@example.org (not managed)")
    twin = CostAccount("codex", "jordan@example.org|", None, "jordan@example.org (not managed)")
    for both in (False, True):
        chart = build_chart(_cost({A1: (5,) * 7, stranger: (2,) * 7, UNATTRIBUTED: (1,) * 7}), both=both)
        assert [e.name for e in chart.legend] == ["#1 personal", "jordan@example.org (not managed)", "unattributed"]
        assert [e.head for e in chart.legend] == [2, 0, 0]
        chart = build_chart(_cost({stranger: (2,) * 7, twin: (1,) * 7}), both=both)
        assert [e.name for e in chart.legend] == [
            "Claude jordan@example.org (not managed)", "Codex jordan@example.org (not managed)",
        ]


def test_nothing_to_draw_is_no_chart():
    assert build_chart(CostSummary(today=1.0, week=2.0)) is None  # days not read
    empty = tuple(CostDay(TODAY - _dt.timedelta(days=6 - i)) for i in range(7))
    assert build_chart(CostSummary(today=0.0, week=0.0, today_unpriced=5, days=empty)) is None


def test_accessibility_label_is_the_caption_and_each_days_total():
    chart = build_chart(_cost({A1: (1, 2, 0, 4, 5, 6, 7.5)}))
    assert chart.accessibility_label() == (
        "Today $7.50 · 7 days $25.50 (estimated). Wednesday $1.00, Thursday $2.00, "
        "Friday $0.00, Saturday $4.00, Sunday $5.00, Monday $6.00, today $7.50."
    )


def test_outline_line_has_each_days_total_and_the_legend():
    chart = build_chart(_cost({A1: (1, 2, 0, 4, 5, 6, 7.5), A2: (0.2,) * 7}))
    assert chart.outline() == (
        "chart: Wed $1.20 · Thu $2.20 · Fri $0.20 · Sat $4.20 · Sun $5.20 · Mon $6.20 · "
        "Tue $7.70 | #1 personal $26 · #2 work $1"
    )


def test_whole_dollars():
    assert menubar_chart.whole_dollars(0.0) == "$0"
    assert menubar_chart.whole_dollars(0.3) == "<$1"
    assert menubar_chart.whole_dollars(2.5) == "$3"
    assert menubar_chart.whole_dollars(1204.4) == "$1,204"


# --- where it goes ---------------------------------------------------------------------------


def _layout(week, width=384.0, left=30.0):
    chart = build_chart(_cost(week))
    bands = menubar_chart.frame(12.0, 13.0)
    return chart, bands, menubar_chart.layout_columns(chart, left=left, width=width, base=bands.base)


def test_the_bars_run_edge_to_edge():
    # The owner's numbers: a bar is 64% of a seventh of the width, at most
    # 40 points. The first bar's left edge is the content's left edge (the
    # caption's), the last bar's right edge its right edge, the others
    # evenly spaced, every edge on a device pixel.
    snap = menubar_chart.SNAP
    for left, width in ((30.0, 300.0), (30.0, 384.0), (16.0, 381.0), (30.0, 520.0), (30.0, 700.0), (17.0, 367.9)):
        _chart, _bands, cols = _layout(SEVEN, width=width, left=left)
        bar = min(40.0, 0.64 * width / 7)
        assert len({c.width for c in cols}) == 1
        assert cols[0].width == pytest.approx(bar, abs=snap)
        assert cols[0].x == left
        assert cols[-1].x + cols[-1].width == pytest.approx(left + width, abs=snap / 2)
        steps = [b.x - a.x for a, b in zip(cols, cols[1:])]
        assert max(steps) - min(steps) <= snap
        for i, col in enumerate(cols):
            assert col.x == pytest.approx(left + i * (width - col.width) / 6, abs=snap / 2)
            assert col.x / snap == int(col.x / snap)  # on a device pixel
            assert col.centre == col.x + col.width / 2  # the weekday letter's
            assert all(p.box.x == col.x and p.box.w == col.width for p in col.segments)
    # A wide menu: the bars stop at 40 points and the gaps between them grow.
    _chart, _bands, cols = _layout(SEVEN, width=700.0)
    assert {c.width for c in cols} == {40.0}


def test_the_tallest_day_fills_the_bar_area_and_every_segment_shows():
    chart, bands, cols = _layout(SEVEN)
    tallest = max(cols, key=lambda c: c.column.total)
    assert tallest.segments[-1].box.y == bands.base - menubar_chart.BAR_AREA
    assert tallest.outline.y == bands.base - menubar_chart.BAR_AREA
    for col in cols:
        assert col.stub is None
        assert len(col.segments) == len(col.column.segments)
        assert all(p.box.h >= menubar_chart.MIN_SEGMENT - menubar_chart.SEGMENT_GAP for p in col.segments)
        assert col.segments[0].box.y + col.segments[0].box.h == bands.base  # on the baseline
        assert all(p.box.y >= bands.bars_top for p in col.segments)


def test_each_column_is_one_outline_with_the_same_corners():
    # The segments are plain rectangles inside one outline per column: from
    # the top of the stack to the baseline, its top corners rounded at 3
    # points whatever the top segment's height (here a 1 point "other" on
    # every day, which rounded on its own gave 1 point corners).
    week = {A1: (12, 9, 14, 8, 11, 10, 13), A2: (6, 7, 2, 5, 4, 3, 6), A3: (3, 2, 4, 1, 2, 3, 2),
            A4: (1, 2, 1, 2, 1, 2, 1), A5: (0.1,) * 7, X2: (0.1,) * 7}
    chart, bands, cols = _layout(week)
    assert all(c.column.segments[-1].key == "other" for c in cols)
    assert {c.segments[-1].box.h for c in cols} == {menubar_chart.MIN_SEGMENT}
    for col in cols:
        top = col.segments[-1].box
        assert col.outline == menubar_chart.Box(col.x, top.y, col.width, bands.base - top.y)
        assert col.radius == menubar_chart.CORNER == 3.0
        for part in col.segments:  # inside the outline
            b = part.box
            assert col.outline.x <= b.x and b.x + b.w <= col.outline.x + col.outline.w
            assert col.outline.y <= b.y and b.y + b.h <= col.outline.y + col.outline.h
    # Only a stack shorter than the corner has smaller corners; an empty
    # day has no outline, only its stub.
    _chart, _bands, cols = _layout({A1: (50, 0, 0.01, 50, 50, 50, 50)})
    assert cols[2].outline.h == menubar_chart.MIN_SEGMENT and cols[2].radius == menubar_chart.MIN_SEGMENT
    assert cols[1].outline is None and cols[1].radius == 0.0 and cols[1].stub is not None


def test_a_thin_segment_is_one_point_and_gaps_only_between_two_tall_ones():
    # Monday: #1 at 40, #2 a sliver (it ranks second on its Wednesday), #3
    # at 20; Tuesday all slivers.
    week = {A1: (0, 0, 0, 0, 0, 40, 0.01), A2: (0, 0, 30, 0, 0, 0.01, 0.01), A3: (0, 0, 0, 0, 0, 20, 0.01)}
    _chart, bands, cols = _layout(week)
    mon, tue = cols[5], cols[6]
    # Painted heights: the sliver is a point tall, and the two tall ones
    # beside it keep their gap-free edge against it (no stripes).
    full = [round(b.box.h, 2) for b in mon.segments]
    assert full[1] == menubar_chart.MIN_SEGMENT
    assert mon.segments[0].box.y + mon.segments[0].box.h == bands.base
    assert mon.segments[1].box.y + mon.segments[1].box.h == mon.segments[0].box.y  # no gap below the sliver
    assert mon.segments[2].box.y + mon.segments[2].box.h == mon.segments[1].box.y  # nor above it
    assert mon.segments[2].box.y == bands.base - menubar_chart.BAR_AREA
    # Three slivers stack as three points, no gaps, each one showing.
    assert [b.box.h for b in tue.segments] == [1.0, 1.0, 1.0]
    assert tue.segments[-1].box.y == bands.base - 3.0
    # A sliver on top of the tallest day: a point tall, and the day still
    # exactly the bar area's height.
    _chart, bands, cols = _layout({A1: (50,) * 7, A2: (0.01,) * 7})
    low, top = cols[0].segments
    assert top.box.h == 1.0 and top.box.y == bands.base - menubar_chart.BAR_AREA
    # Two tall neighbours: one point left unpainted between them.
    _chart, _bands, cols = _layout({A1: (10,) * 7, A2: (10,) * 7})
    low, high = cols[0].segments
    assert high.box.y + high.box.h + menubar_chart.SEGMENT_GAP == low.box.y
    assert low.box.h + high.box.h + menubar_chart.SEGMENT_GAP == menubar_chart.BAR_AREA


def test_an_empty_day_is_a_stub_on_the_baseline():
    _chart, bands, cols = _layout({A1: (5, 0, 3, 4, 2, 6, 1)})
    stub = cols[1].stub
    assert cols[1].segments == () and stub is not None
    assert (stub.y, stub.h, stub.x, stub.w) == (bands.base - 1.0, 1.0, cols[1].x, cols[1].width)


def test_the_view_is_never_taller_than_110_points():
    # The AppKit view's line heights (10 and 11 point system font) and the
    # HTML drawing's.
    for day_line, legend_line in ((11.93, 13.13), (12.0, 13.0), (13.0, 14.0)):
        assert menubar_chart.frame(day_line, legend_line).height <= menubar_chart.MAX_HEIGHT
    bands = menubar_chart.frame(12.0, 13.0)
    assert bands.base - bands.bars_top == menubar_chart.BAR_AREA == 44.0
    assert menubar_chart.MAX_HEIGHT == 110.0


def test_legend_width_is_dots_texts_and_gaps():
    assert menubar_chart.legend_width([]) == 0
    assert menubar_chart.legend_width([40.0, 25.0]) == 7 + 4 + 40 + 10 + 7 + 4 + 25


# --- the legend's width ---------------------------------------------------------------------


LONG = {
    _claude_acc(1, "maya.rodriguez-castellanos@example.com"): (12, 15, 9, 18, 11, 8, 6),
    _claude_acc(2, "northwind-labs-platform-team"): (8, 6, 7, 9, 5, 4, 3),
    _claude_acc(3, "weekend-side-project-account"): (0, 3, 0, 0, 6, 2, 0),
    CostAccount("claude", "jordan@example.org|", None, "jordan.lee.contractor@example.org (not managed)"):
        (1, 0, 2, 1, 0, 2, 1),
    UNATTRIBUTED: (1, 0.3, 0, 1, 0.2, 0, 0.1),
}


@pytest.mark.parametrize("week", [SEVEN, LONG, {A1: (5,) * 7}], ids=["seven", "long", "one"])
def test_the_legend_is_never_longer_than_its_width_budget(week):
    chart = build_chart(_cost(week))
    names = chart.fit_legend()
    line = chart.legend_text(names)
    assert len(line) <= LEGEND_LIMIT and "\n" not in line
    for name, entry in zip(names, chart.legend):
        if entry.head:  # the number always stays
            assert name.startswith(entry.name[: entry.head])
        assert name == entry.name or name.endswith("…") or name == entry.name[: entry.head]
    assert names[-1] == chart.legend[-1].name or chart.legend[-1].name != "other"


def test_fit_names_starts_short_and_grows_in_legend_order():
    names = ["#1 personal", "#2 a-very-long-account-name", "other"]
    heads = [2, 2, 0]

    def chars(limit):
        return lambda ns: sum(map(len, ns)) <= limit

    # Room for every whole name: nothing is cut.
    assert fit_names(names, heads, chars(60)) == names
    # The first entry takes its whole name, the next the longest cut left.
    assert fit_names(names, heads, chars(30)) == ["#1 personal", "#2 a-very-lon…", "other"]
    # No room for the second's three letters once the first is whole: it
    # stays at its number. Names are not cut to one common length.
    assert fit_names(names, heads, chars(20)) == ["#1 personal", "#2", "other"]
    assert fit_names(names, heads, chars(15)) == ["#1 pers…", "#2", "other"]
    # Nothing fits: every name at its shortest; "other" is never cut.
    assert fit_names(names, heads, lambda ns: False) == ["#1", "#2", "other"]
    # A cut never ends in a dot before the ellipsis, and a name with no
    # number keeps six characters.
    assert fit_names(["jordan.lee@example.org"], [0], chars(8)) == ["jordan…"]
    assert fit_names(["unattributed"], [0], lambda ns: False) == ["unattr…"]


def test_the_shortest_forms_keep_the_provider_word():
    # With both providers in the menu, cut to its number a Codex entry
    # still says "Codex": no two entries read the same at their shortest.
    chart = build_chart(_cost(SEVEN), both=True)
    shortest = chart.fit_legend(lambda _names: False)
    assert shortest == ["#1", "#2", "Codex #1", "#3", "other"]
    names = ["Codex #2 work", "#1 personal"]
    assert fit_names(names, [8, 2], lambda ns: False) == ["Codex #2", "#1"]
    assert menubar_chart.name_cuts("Codex #2 work", 8) == []  # too short to cut: whole or "Codex #2"
    assert menubar_chart.name_cuts("Codex #1 maya@example.com", 8)[0] == "Codex #1 may…"


#: A width model for the legend line: the view's dot, gap and spacing, and
#: six points a character, about the 11 point system font's average.
CHAR = 6.0


def legend_measure(chart):
    def measure(names):
        return menubar_chart.legend_width(
            [CHAR * len(f"{n} {e.amount_text}") for n, e in zip(names, chart.legend)])
    return measure


def _forms(name: str, head: int) -> list[str]:
    """Every form fit_names may show for a name, shortest first."""
    return list(dict.fromkeys(
        [menubar_chart.shortest_name(name, head), *menubar_chart.name_cuts(name, head), name]))


def assert_legend_uses_its_room(chart, inners, measure) -> list[list[str]]:
    """The legend's rule at each content width in ``inners`` (ascending),
    for a ``measure`` of a line of names. Returns the names at each."""
    full = [e.name for e in chart.legend]
    heads = [e.head for e in chart.legend]
    shortest = [menubar_chart.shortest_name(n, h) for n, h in zip(full, heads)]
    out, first = [], 0
    for inner in inners:
        def fits(ns, inner=inner):
            return measure(ns) <= inner

        names = chart.fit_legend(fits)
        out.append(names)
        # It fits, or nothing does and every name is at its shortest.
        assert fits(names) or names == shortest, (inner, names)
        # No two entries read the same.
        assert len(set(names)) == len(names), (inner, names)
        # A wider line never shows a shorter name for the first entry.
        assert len(names[0]) >= first, (inner, names)
        first = len(names[0])
        for i, (name, whole, head) in enumerate(zip(names, full, heads)):
            forms = _forms(whole, head)
            assert name in forms, (inner, name)
            if name == whole:
                continue
            longer = forms[forms.index(name) + 1:]
            # No room is left that this name could have used.
            assert not any(fits([*names[:i], f, *names[i + 1:]]) for f in longer), (inner, i, names)
            # Bare only when its next form did not fit in the room the
            # entries before it left.
            if name == shortest[i]:
                assert not any(fits([*names[:i], f, *shortest[i + 1:]]) for f in longer), (inner, i, names)
    return out


#: The showcase scenario's week (scripts/demo/scenarios.py, as read in the
#: demo sandbox): its legend reads "#1 personal $34 · #2 work $23 · #5 ci
#: $16 · Codex #2 work $3 · other $3" (checked against the scenario below).
SHOWCASE = {
    A1: (5.591012, 4.326862, 11.046293, 1.218191, 5.672755, 5.757831, 0.0),
    A2: (7.7149, 4.637641, 3.644558, 1.054096, 0.0, 3.846731, 1.919129),
    A5: (0.78694, 6.611378, 1.747924, 0.0, 0.0, 5.193986, 2.060336),
    X2: (0.484412, 0.0, 0.34524, 0.0, 0.0, 0.555237, 1.183374),
    A3: (2.0, 0.273495, 0.123706, 0.0, 0.0, 0.0, 0.0),
    X1: (0.457605, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
}
#: The view's insets in a menu with check marks: content = width - 46.
INSETS = 16.0 + 14.0 + 16.0


def test_the_showcase_legend_from_360_to_600_points():
    chart = build_chart(_cost(SHOWCASE), both=True)
    assert [(e.name, e.amount_text) for e in chart.legend] == [
        ("#1 personal", "$34"), ("#2 work", "$23"), ("#5 ci", "$16"), ("Codex #2 work", "$3"), ("other", "$3"),
    ]
    widths = range(360, 601, 4)
    shown = assert_legend_uses_its_room(chart, [w - INSETS for w in widths], legend_measure(chart))
    assert shown[0] == ["#1", "#2", "#5", "Codex #2", "other"]  # 360: the shortest forms
    assert shown[-1] == [e.name for e in chart.legend]  # 600: every name whole
    # Somewhere between, the first entry is cut but not bare.
    assert any(names[0].startswith("#1 ") and names[0].endswith("…") for names in shown)


def test_both_providers_sharing_numbers_one_and_two():
    chart = build_chart(_cost({A1: SEVEN[A1], X1: SEVEN[A2], A2: SEVEN[X1], X2: SEVEN[A3], A5: SEVEN[A5]}), both=True)
    assert [e.name for e in chart.legend] == [
        "#1 personal", "Codex #1 maya@example.com", "#2 work", "Codex #2 work", "other",
    ]
    shown = assert_legend_uses_its_room(chart, [w - INSETS for w in range(300, 601, 4)], legend_measure(chart))
    assert shown[0] == ["#1", "Codex #1", "#2", "Codex #2", "other"]
    for names in shown:
        assert [n.startswith("Codex #") for n in names] == [False, True, False, True, False]
        assert not any(n.startswith("Claude") for n in names)


def test_long_names_with_a_shared_prefix():
    a1 = CostAccount("claude", "platform@northwind.example.com|", 1, "#1 northwind-platform-production")
    a2 = CostAccount("claude", "staging@northwind.example.com|", 2, "#2 northwind-platform-staging")
    x1 = CostAccount("codex", "ci@northwind.example.com|", 1, "#1 northwind-platform-ci")
    chart = build_chart(_cost({a1: (16, 12, 19, 4, 3, 15, 11), a2: (6, 9, 5, 0, 2, 7, 4), x1: (1, 2, 1, 0, 1, 2, 1)}),
                        both=True)
    shown = assert_legend_uses_its_room(chart, [w - INSETS for w in range(300, 601, 4)], legend_measure(chart))
    for names in shown:
        assert names[2].startswith("Codex #1")
        for name, entry in zip(names, chart.legend):
            kept = name.removesuffix("…")[entry.head + 1:]
            assert name == entry.name[: entry.head] or len(kept) >= menubar_chart.NAME_KEEP


def test_amounts_of_a_thousand_dollars_and_more():
    week = {A1: (700, 650, 720, 690, 680, 700, 672), A2: (420, 430, 410, 425, 415, 440, 410),
            A3: (170, 175, 168, 172, 171, 174, 174), A4: (160, 165, 166, 162, 168, 164, 165),
            A5: (80, 81, 79, 80, 82, 78, 80), X1: (78, 79, 77, 80, 80, 79, 79)}
    chart = build_chart(_cost(week), both=True)
    assert [e.amount_text for e in chart.legend] == ["$4,812", "$2,950", "$1,204", "$1,150", "$1,112"]
    assert_legend_uses_its_room(chart, [w - INSETS for w in range(300, 701, 4)], legend_measure(chart))


def test_one_provider_only():
    chart = build_chart(_cost({a: SEVEN[a] for a in (A1, A2, A3, A4, A5)}))
    shown = assert_legend_uses_its_room(chart, [w - INSETS for w in range(300, 601, 4)], legend_measure(chart))
    assert shown[0] == ["#1", "#2", "#3", "#5", "other"]
    assert not any(n.startswith(("Claude", "Codex")) for names in shown for n in names)


# --- the cost row in the menu -----------------------------------------------------------------


def _cost_inp(cost, **kw):
    return _inp(_claude(_C1, _C2), cost=cost, **kw)


def test_cost_row_with_the_chart_on():
    cost = _cost({A1: (5,) * 7, A2: (3,) * 7})
    row = menubar.build_menu(_cost_inp(cost)).section("cost").rows[0]
    assert row.text == cost.text()  # the outline, VoiceOver and old tests read the sentence
    assert row.chart is not None and row.chart.caption == row.text
    assert row.action is None and row.disabled
    assert MenuBarSettings().usage_chart is True
    # The legend uses the rows' names.
    assert [e.name for e in row.chart.legend] == ["#1 personal", "#2 work"]


def test_cost_row_with_the_chart_off_is_the_text_row_as_before():
    cost = _cost({A1: (5,) * 7})
    menu = menubar.build_menu(_cost_inp(cost, settings=MenuBarSettings(usage_chart=False)))
    assert menu.section("cost").rows == (Row(cost.text()),)
    assert "chart:" not in menu.outline()


def test_no_cost_data_no_section_and_no_days_the_text_row():
    assert menubar.build_menu(_cost_inp(None)).section("cost") is None
    no_days = CostSummary(today=12.4, week=179.72)
    assert menubar.build_menu(_cost_inp(no_days)).section("cost").rows == (Row(no_days.text()),)


def test_a_failure_building_the_chart_shows_the_text_row(monkeypatch, caplog):
    def broken(*_a, **_kw):
        raise ValueError("boom")

    monkeypatch.setattr(menubar, "build_chart", broken)
    cost = _cost({A1: (5,) * 7})
    with caplog.at_level(logging.DEBUG, logger="ai-quota-swap"):
        menu = menubar.build_menu(_cost_inp(cost))
    assert menu.section("cost").rows == (Row(cost.text()),)
    assert any("usage chart could not be built" in r.getMessage() for r in caplog.records)


def test_outline_prints_one_line_for_the_chart():
    cost = _cost({A1: (1, 2, 0, 4, 5, 6, 7.5)})
    outline = menubar.build_menu(_cost_inp(cost)).outline().splitlines()
    i = outline.index(f"  {cost.text()}  (greyed)")
    assert outline[i + 1] == "    " + build_chart(cost).outline()
    assert outline[i + 2] == "────"


def test_the_legend_names_come_from_the_rows_for_both_providers():
    # The read carried older names (an alias set since): the rows' win.
    old_c1 = CostAccount("claude", "maya@example.com|", 1, "#1 maya@example.com")
    old_x2 = CostAccount("codex", "maya@northwind.example.com|", 2, "#2 maya@northwind.example.com")
    cost = _cost({old_c1: (5,) * 7, old_x2: (2,) * 7})
    menu = menubar.build_menu(_inp(_claude(_C1, _C2), _codex(_X1, _X2), cost=cost))
    # Codex's #2 is "work", and so is Claude Code's #2 in the menu above.
    assert [e.name for e in menu.section("cost").rows[0].chart.legend] == ["#1 personal", "Codex #2 work"]


def test_settings_offer_show_usage_chart_beside_the_display_toggles():
    menu = menubar.build_menu(_cost_inp(None))
    texts = [r.text for r in menu.find("Settings").children]
    assert texts.index("Show usage chart") == texts.index("Show model limits in title") + 1
    row = menu.find("Show usage chart")
    assert row.checked and row.action == menubar.Action("toggle-chart")
    menu = menubar.build_menu(_cost_inp(None, settings=MenuBarSettings(usage_chart=False)))
    assert not menu.find("Show usage chart").checked
    assert "toggle-chart" in menubar.ACTION_KINDS


# --- the setting --------------------------------------------------------------------------------


def test_usage_chart_setting_round_trip_and_an_older_file(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    MenuBarSettings(usage_chart=False, title_pct="7d").save(path)
    assert json.loads(path.read_text())["usage_chart"] is False
    assert MenuBarSettings.load(path) == MenuBarSettings(usage_chart=False, title_pct="7d")
    # Written by a build before the chart: the chart on, the rest as written.
    path.write_text(json.dumps({"title_pct": "5h", "auto_switch_enabled": True}))
    old = MenuBarSettings.load(path)
    assert old.usage_chart is True and old.title_pct == "5h" and old.auto_switch_enabled
    # A wrong type keeps the default; unknown keys survive a save.
    path.write_text(json.dumps({"usage_chart": "no", "some_future_key": [1]}))
    s = MenuBarSettings.load(path)
    assert s.usage_chart is True
    s.usage_chart = False
    s.save(path)
    raw = json.loads(path.read_text())
    assert raw["some_future_key"] == [1] and raw["usage_chart"] is False


# --- read_cost: each day by account ---------------------------------------------------------------


MODEL = "claude-opus-5"


def _hour(day: _dt.date, h: int = 12) -> int:
    local = _dt.datetime(day.year, day.month, day.day, h).astimezone()
    return int(local.timestamp() // 3600)


def _toks(inp: int, out: int = 0) -> tuple:
    return (inp, out, 0, 0, 0, 0, 1)


def _write_ledger(root: Path, buckets, sessions=()):
    from ai_quota_swap.ledger.store import LedgerStore

    store = LedgerStore(root / "ledger.db")
    try:
        store.begin()
        store.add_buckets(buckets)
        if sessions:
            store.note_sessions(list(sessions))
        store.commit()
    finally:
        store.close()


def _registry(root: Path):
    (root / "sequence.json").write_text(json.dumps({"accounts": {
        "1": {"email": "a@example.com", "uuid": "uuid-a", "organizationUuid": ""},
        "2": {"email": "b@example.com", "uuid": "uuid-b", "organizationUuid": "", "alias": "work"},
    }}))


def _price(inp: int, out: int = 0) -> float:
    from ai_quota_swap.ledger.pricing import PriceTable

    return PriceTable().lookup(MODEL)[1].cost(inp, out, 0, 0, 0)


def test_read_cost_reads_each_day_by_account(tmp_path):
    _registry(tmp_path)
    d0, d3 = TODAY, TODAY - _dt.timedelta(days=3)
    _write_ledger(tmp_path, [
        ("claude", _hour(d0), "a@example.com|", MODEL, "p", "s1", *_toks(1_000_000, 100_000)),
        ("claude", _hour(d0), "b@example.com|", MODEL, "p", "s2", *_toks(500_000)),
        ("claude", _hour(d3), "a@example.com|", MODEL, "p", "s3", *_toks(2_000_000)),
        ("claude", _hour(d3), "", MODEL, "p", "s4", *_toks(300_000)),
        ("codex", _hour(d3), "c@example.com|", MODEL, "p", "s5", *_toks(100_000)),
        ("claude", _hour(d3), "a@example.com|", "no-such-model", "p", "s6", *_toks(50)),
        ("claude", _hour(TODAY - _dt.timedelta(days=9)), "a@example.com|", MODEL, "p", "s7", *_toks(9_000_000)),
    ])
    cost = menubar_data.read_cost(tmp_path, today=TODAY)
    assert [d.day for d in cost.days] == [TODAY - _dt.timedelta(days=6 - i) for i in range(7)]
    by_day = {d.day: {(a.provider, a.key): (a, c) for a, c in d.costs} for d in cost.days}
    assert all(not by_day[TODAY - _dt.timedelta(days=i)] for i in (1, 2, 4, 5, 6))
    a, c = by_day[d0][("claude", "a@example.com|")]
    assert (a.number, a.name) == (1, "#1 a@example.com")
    assert c == pytest.approx(_price(1_000_000, 100_000))
    b, c = by_day[d0][("claude", "b@example.com|")]
    assert (b.number, b.name) == (2, "#2 work") and c == pytest.approx(_price(500_000))
    assert by_day[d3][("claude", "a@example.com|")][1] == pytest.approx(_price(2_000_000))
    u, c = by_day[d3][(None, "")]
    assert u == CostAccount(None, "", None, "unattributed") and c == pytest.approx(_price(300_000))
    x, _c = by_day[d3][("codex", "c@example.com|")]
    assert (x.number, x.name) == (None, "c@example.com (not managed)")
    # The totals and the sentence are what they were; the unpriced model
    # stays out of the columns and in the caption's "+".
    assert cost.today == pytest.approx(_price(1_000_000, 100_000) + _price(500_000))
    assert cost.week_unpriced == 50
    assert sum(c for d in cost.days for _a, c in d.costs) == pytest.approx(cost.week)
    assert cost.text().endswith("+ (estimated)")


def test_read_cost_attributes_days_as_qswap_usage_by_account_does(tmp_path):
    # A `qswap run 2` session in the shared tree: the activation log says #1
    # was live, Claude Code stamped #2 as its owner. qswap usage --by account
    # gives it to #2, and so does the chart.
    from ai_quota_swap.ledger.activations import record_activation
    from ai_quota_swap.ledger.pricing import PriceTable
    from ai_quota_swap.ledger.report import build_report
    from ai_quota_swap.ledger.store import LedgerStore
    from ai_quota_swap.ledger.usage import report_inputs

    _registry(tmp_path)
    record_activation(tmp_path, number=1, email="a@example.com", org="",
                      now=_hour(TODAY - _dt.timedelta(days=10)) * 3600)
    h = _hour(TODAY)
    _write_ledger(
        tmp_path,
        [
            ("claude", h, "a@example.com|", MODEL, "p", "sess-run", *_toks(1_000_000)),
            ("claude", h, "a@example.com|", MODEL, "p", "sess-a", *_toks(200_000)),
        ],
        sessions=[("claude", "sess-run", "uuid-b|", h * 3600 + 60.0, 1)],
    )
    cost = menubar_data.read_cost(tmp_path, today=TODAY)
    today = {a.number: c for a, c in cost.days[-1].costs}
    assert today == {2: pytest.approx(_price(1_000_000)), 1: pytest.approx(_price(200_000))}

    logs, registries = report_inputs(tmp_path, json.loads((tmp_path / "sequence.json").read_text()))
    store = LedgerStore(tmp_path / "ledger.db")
    try:
        report = build_report(store, since=TODAY - _dt.timedelta(days=6), until=TODAY, by="account",
                              registries=registries, logs=logs, prices=PriceTable.load(tmp_path))
    finally:
        store.close()
    usage = {row.extra["account"]["number"]: row.totals.cost for row in report.rows}
    assert usage == today


def test_a_swap_changes_the_stamp_and_the_legend_follows(temp_home):
    # The app reads the cost again only when ledger_stamp changes. `qswap
    # swap` renumbers accounts without touching the ledger; the legend's
    # names come from the rows, by number, so the read must follow the
    # swap or the menu names #2's row against #1's amount.
    from ai_quota_swap.menubar import AccountState
    from ai_quota_swap.models import Platform
    from ai_quota_swap.switcher import ClaudeAccountSwitcher
    from tests.test_account_policy_store import PolicyStoreBase

    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    seed = PolicyStoreBase()
    seed._seed(s, 1, "account1@example.com")
    seed._seed(s, 2, "account2@example.com")
    root = s.backup_dir
    _write_ledger(root, [
        ("claude", _hour(TODAY), "account1@example.com|", MODEL, "p", "s1", *_toks(10_000_000)),
        ("claude", _hour(TODAY), "account2@example.com|", MODEL, "p", "s2", *_toks(1_000_000)),
    ])

    def legend(cost):
        rows = [AccountState("claude", str(n), a["email"]) for n, a in s._get_sequence_data()["accounts"].items()]
        chart = menubar.build_menu(_inp(_claude(*rows), cost=cost)).section("cost").rows[0].chart
        return [(e.name, e.amount_text) for e in chart.legend]

    # The stamp as the app holds it: taken after a read, which leaves
    # SQLite's -wal and -shm beside the ledger (taken before, the read
    # alone would change it, whatever the swap did).
    cost = menubar_data.read_cost(root, today=TODAY)
    stamp = menubar_data.ledger_stamp(root)
    assert menubar_data.read_cost(root, today=TODAY) == cost and menubar_data.ledger_stamp(root) == stamp
    big, small = menubar_chart.whole_dollars(_price(10_000_000)), menubar_chart.whole_dollars(_price(1_000_000))
    assert legend(cost) == [("#1 account1@example.com", big), ("#2 account2@example.com", small)]
    s.swap_accounts("1", "2")
    assert menubar_data.ledger_stamp(root) != stamp  # so the app reads again
    cost = menubar_data.read_cost(root, today=TODAY)
    assert legend(cost) == [("#2 account1@example.com", big), ("#1 account2@example.com", small)]
    # Codex's registry counts too.
    stamp = menubar_data.ledger_stamp(root)
    (root / "codex").mkdir(exist_ok=True)
    (root / "codex" / "sequence.json").write_text(json.dumps({"accounts": {}}))
    assert menubar_data.ledger_stamp(root) != stamp


def test_a_helper_line_changes_the_stamp_and_the_split_follows(tmp_path):
    # Whose an hour is also comes from the activation logs: a key's helper
    # line (or a `qswap run` session) moves usage between accounts without
    # touching the ledger or a registry. Each provider's log is watched.
    from ai_quota_swap.ledger.activations import record_activation

    _registry(tmp_path)
    (tmp_path / "sequence.json").write_text(json.dumps({"accounts": {
        "1": {"email": "a@example.com", "uuid": "uuid-a", "organizationUuid": ""},
        "5": {"email": "key5@token.local", "uuid": "", "organizationUuid": ""},
    }}))
    h = _hour(TODAY)
    record_activation(tmp_path, number=1, email="a@example.com", org="", now=(h - 30) * 3600)
    record_activation(tmp_path, number=5, email="key5@token.local", org="", now=(h - 20) * 3600, how="saved-key")
    _write_ledger(
        tmp_path, [("claude", h, "key5@token.local|", MODEL, "p", "sess-old", *_toks(1_000_000))],
        sessions=[("claude", "sess-old", "", (h - 25) * 3600.0, 1)],
    )

    def split():
        return {a.number: round(c, 6) for a, c in menubar_data.read_cost(tmp_path, today=TODAY).days[-1].costs}

    # Under the saved-key line, a session started before it stays with #1.
    assert split() == {1: pytest.approx(_price(1_000_000))}
    stamp = menubar_data.ledger_stamp(tmp_path)
    # The same key entered again by its helper line: the hour is the key's.
    record_activation(tmp_path, number=5, email="key5@token.local", org="", now=(h - 10) * 3600, how="helper")
    assert menubar_data.ledger_stamp(tmp_path) != stamp
    assert split() == {5: pytest.approx(_price(1_000_000))}
    # Codex's log counts too.
    stamp = menubar_data.ledger_stamp(tmp_path)
    record_activation(tmp_path / "codex", number=1, email="c@example.com", org="", now=(h - 5) * 3600)
    assert menubar_data.ledger_stamp(tmp_path) != stamp


def test_the_stamp_follows_a_scan_whether_or_not_its_wal_is_checkpointed(tmp_path):
    from ai_quota_swap.ledger.store import LedgerStore

    # The first scan makes the ledger: the stamp, taken while there was
    # none, changes with ledger.db (the scan's WAL is folded in and gone).
    stamp = menubar_data.ledger_stamp(tmp_path)
    assert menubar_data.read_cost(tmp_path, today=TODAY) is None
    _write_ledger(tmp_path, [("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s1", *_toks(1_000))])
    assert not (tmp_path / "ledger.db-wal").exists()
    assert menubar_data.ledger_stamp(tmp_path) != stamp
    # A scan that commits while it still holds the ledger open (or while
    # another reader does): the commit is in ledger.db-wal alone.
    menubar_data.read_cost(tmp_path, today=TODAY)
    store = LedgerStore(tmp_path / "ledger.db")
    try:
        stamp = menubar_data.ledger_stamp(tmp_path)
        db = (tmp_path / "ledger.db").stat()
        store.begin()
        store.add_buckets([("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s2", *_toks(2_000))])
        store.commit()
        assert (tmp_path / "ledger.db").stat().st_mtime_ns == db.st_mtime_ns  # not in ledger.db yet
        assert menubar_data.ledger_stamp(tmp_path) != stamp
        assert menubar_data.read_cost(tmp_path, today=TODAY).today == pytest.approx(_price(3_000))
    finally:
        store.close()


def test_one_cost_read_is_one_snapshot_of_the_ledger(tmp_path, monkeypatch):
    # The caption's totals and the columns are two queries. A scan that
    # commits between them changes neither: the columns add up to the
    # caption they sit under, and the next read has the new usage in both.
    _write_ledger(tmp_path, [("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s1", *_toks(1_000_000))])
    real = menubar_data.cost_days
    commits = []

    def scan_commits_first(*a, **kw):
        if not commits:
            _write_ledger(tmp_path, [("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s2", *_toks(2_000_000))])
            commits.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(menubar_data, "cost_days", scan_commits_first)
    cost = menubar_data.read_cost(tmp_path, today=TODAY)
    assert commits
    columns = sum(c for d in cost.days for _a, c in d.costs)
    assert cost.week == pytest.approx(_price(1_000_000)) and columns == pytest.approx(cost.week)
    cost = menubar_data.read_cost(tmp_path, today=TODAY)
    columns = sum(c for d in cost.days for _a, c in d.costs)
    assert cost.week == pytest.approx(_price(3_000_000)) and columns == pytest.approx(cost.week)


def test_with_the_chart_off_the_days_are_not_read(tmp_path, monkeypatch):
    _write_ledger(tmp_path, [("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s1", *_toks(1_000_000))])
    calls = []
    real = menubar_data.cost_days
    monkeypatch.setattr(menubar_data, "cost_days", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    off = menubar_data.read_cost(tmp_path, today=TODAY, days=False)
    assert calls == [] and off.days == () and off.today == pytest.approx(_price(1_000_000))
    on = menubar_data.read_cost(tmp_path, today=TODAY)
    assert calls == [1] and on.days and (on.today, on.week) == (off.today, off.week)
    # The cache: a read without the days never stands in for one with
    # them, though the ledger has not changed; otherwise it reads only when
    # the stamp changes, or when asked to after a scan.
    cache = menubar_data.CostCache()
    calls.clear()
    assert cache.read(tmp_path, days=False, today=TODAY).days == () and calls == []
    assert cache.read(tmp_path, days=False, today=TODAY).days == () and calls == []
    assert cache.read(tmp_path, days=True, today=TODAY).days and calls == [1]
    assert cache.read(tmp_path, days=True, today=TODAY).days and calls == [1]
    assert cache.read(tmp_path, days=True, again=True, today=TODAY).days and calls == [1, 1]
    _write_ledger(tmp_path, [("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s2", *_toks(1_000))])
    assert cache.read(tmp_path, days=True, today=TODAY).today == pytest.approx(_price(1_001_000))
    assert calls == [1, 1, 1]
    assert cache.read(tmp_path, days=False, today=TODAY).days == () and calls == [1, 1, 1]


@pytest.fixture
def local_zone(monkeypatch):
    """Run in another local time zone: ``set_zone("Asia/Kolkata")``."""
    import time

    def set_zone(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield set_zone
    monkeypatch.undo()
    time.tzset()


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset is POSIX-only")
@pytest.mark.parametrize("zone, today, week_hours, today_hours", [
    # Half an hour off UTC: the hour buckets straddle midnight.
    ("Asia/Kolkata", _dt.date(2026, 11, 10), 169, 25),
    # A whole-hour zone across a change: 1 November 2026 is 25 hours long.
    ("America/New_York", _dt.date(2026, 11, 3), 169, 24),
])
def test_the_columns_count_what_the_caption_counts(tmp_path, local_zone, zone, today, week_hours, today_hours):
    from ai_quota_swap.ledger.report import day_bounds

    local_zone(zone)
    since_hour, until_hour = day_bounds(today - _dt.timedelta(days=6), today)
    _write_ledger(tmp_path, [
        ("claude", h, "a@example.com|", MODEL, "p", f"s{h}", *_toks(1_000_000))
        for h in range(since_hour - 3, until_hour + 3)  # and a few hours either side
    ])
    cost = menubar_data.read_cost(tmp_path, today=today)
    hour = _price(1_000_000)
    assert cost.week == pytest.approx(week_hours * hour)
    assert cost.today == pytest.approx(today_hours * hour)
    columns = [sum(c for _a, c in d.costs) for d in cost.days]
    assert sum(columns) == pytest.approx(cost.week)
    assert columns[-1] == pytest.approx(cost.today)
    chart = build_chart(cost)
    assert chart.columns[-1].total == pytest.approx(cost.today)
    assert sum(c.total for c in chart.columns) == pytest.approx(cost.week)


def test_read_cost_keeps_the_totals_when_the_days_cannot_be_read(tmp_path, monkeypatch, caplog):
    _write_ledger(tmp_path, [("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s", *_toks(1_000))])

    def broken(*_a, **_kw):
        raise OSError("unreadable")

    monkeypatch.setattr(menubar_data, "cost_days", broken)
    with caplog.at_level(logging.DEBUG, logger="ai-quota-swap"):
        cost = menubar_data.read_cost(tmp_path, today=TODAY)
    assert cost.days == () and cost.today == pytest.approx(_price(1_000))
    assert any("cost by day could not be read" in r.getMessage() for r in caplog.records)
    assert build_chart(cost) is None


def test_read_cost_by_day_opens_everything_read_only(tmp_path, monkeypatch):
    _write_ledger(tmp_path, [("claude", _hour(TODAY), "a@example.com|", MODEL, "p", "s", *_toks(1_000))])
    before = sorted(p.name for p in tmp_path.iterdir())
    uris = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        uris.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    cost = menubar_data.read_cost(tmp_path, today=TODAY)
    assert cost.days and uris and all(u.endswith("?mode=ro") for u in uris)
    # Nothing of qswap's is made (no registry, no log, no codex directory);
    # SQLite keeps its own -wal and -shm beside a WAL ledger for any reader.
    after = {p.name for p in tmp_path.iterdir()} - {"ledger.db-wal", "ledger.db-shm"}
    assert sorted(after) == before


# --- the app: the chart item, or the text row ----------------------------------------------------


class _FakeNSItem:
    def __init__(self):
        self.view = None

    def setView_(self, view):
        self.view = view


class _FakeRumps:
    class MenuItem:
        def __init__(self, title, callback=None):
            self.title = title
            self._menuitem = _FakeNSItem()


def test_the_app_draws_the_chart_or_falls_back_to_the_text_row(monkeypatch):
    from ai_quota_swap import menubar_app

    row = Row("Today $1.00 · 7 days $7.00 (estimated)", chart=build_chart(_cost({A1: (1,) * 7})))
    log = logging.getLogger("ai-quota-swap")
    made = []
    view_module = types.ModuleType("ai_quota_swap.menubar_chart_view")
    view_module.make_view = lambda chart: made.append(chart) or "a view"
    monkeypatch.setitem(sys.modules, "ai_quota_swap.menubar_chart_view", view_module)
    item = menubar_app._chart_item(_FakeRumps, row, log)
    assert made == [row.chart] and item._menuitem.view == "a view" and item.title == row.text

    def broken(chart):
        raise RuntimeError("no AppKit view today")

    view_module.make_view = broken
    assert menubar_app._chart_item(_FakeRumps, row, log) is None  # _item then draws the text row
    monkeypatch.setitem(sys.modules, "ai_quota_swap.menubar_chart_view", None)  # cannot import
    assert menubar_app._chart_item(_FakeRumps, row, log) is None


#: Each on/off row of Settings and the setting its click flips.
TOGGLE_ROWS = {
    "Show account name in menu bar": "show_account_name",
    "Show model limits in title": "title_scoped",
    "Show usage chart": "usage_chart",
}


@pytest.mark.parametrize("text", list(TOGGLE_ROWS))
def test_each_on_off_setting_flips_only_itself_saves_it_and_the_menu_follows(tmp_path, text):
    # Each click goes where the menu's other on/off settings go:
    # menubar.toggle_menu_setting with the row's own action, which the
    # app's handler for that action calls with the file it saves to.
    import dataclasses
    import inspect

    from ai_quota_swap import menubar_app

    assert set(TOGGLE_ROWS.values()) == set(menubar.SETTING_TOGGLES.values())
    name = TOGGLE_ROWS[text]
    path = tmp_path / "menubar_settings.json"
    settings = MenuBarSettings()
    cost = _cost({A1: (5,) * 7})
    saved = []

    def save(s):
        s.save(path)
        saved.append(getattr(s, name))

    def values(s):
        return {f.name: getattr(s, f.name) for f in dataclasses.fields(s) if f.name != "extra"}

    for _click in range(2):
        before = values(settings)
        row = menubar.build_menu(_cost_inp(cost, settings=settings)).find(text)
        assert row.checked is before[name]
        menubar.toggle_menu_setting(row.action, settings, save=save)
        assert values(settings) == {**before, name: not before[name]}  # only its own setting
        assert saved[-1] is (not before[name]) and MenuBarSettings.load(path) == settings
        menu = menubar.build_menu(_cost_inp(cost, settings=settings))
        assert menu.find(text).checked is (not before[name])
        if name == "show_account_name":
            assert ("personal" in menu.title) is settings.show_account_name
        if name == "usage_chart":
            assert (menu.section("cost").rows[0].chart is not None) is settings.usage_chart
    # The app's handler for the row's action calls that same function, then
    # draws the menu again.
    source = inspect.getsource(menubar_app.run_app)
    handler = "_do_" + row.action.kind.replace("-", "_")
    assert f"def {handler}(self, a: Action):\n            self._toggle_setting(a)\n" in source
    body = source[source.index("def _toggle_setting"):source.index("def _do_toggle_name")]
    assert "toggle_menu_setting(a, self.settings, save=lambda s: s.save(settings_path))" in body
    assert "self.rebuild_menu()" in body
    assert row.action.kind in menubar.ACTION_KINDS


class _AppItem:
    def __init__(self, title, callback=None):
        self.title, self.callback, self.state = title, callback, 0
        self.children, self._menuitem = [], _FakeNSItem()

    def add(self, item):
        self.children.append(item)

    def __setitem__(self, key, item):
        self.children.append(item)


class _AppMenu:
    def __init__(self):
        self.items = []
        self._menu = types.SimpleNamespace(itemArray=lambda: [])

    def clear(self):
        self.items = []

    def add(self, item):
        self.items.append(item)

    def __setitem__(self, key, item):
        self.items.append(item)


def _start_app(switcher, monkeypatch):
    """The real MenuBarApp over a fake rumps and no AppKit: its handlers,
    its reads and its drawing of rows, with nothing shown anywhere. Waits
    for its first background read."""
    from unittest.mock import MagicMock

    from ai_quota_swap import menubar_app

    # No AppKit view: the chart row is a plain item here.
    view_module = types.ModuleType("ai_quota_swap.menubar_chart_view")
    view_module.make_view = lambda chart: (_ for _ in ()).throw(RuntimeError("no view in tests"))
    monkeypatch.setitem(sys.modules, "ai_quota_swap.menubar_chart_view", view_module)
    made = []

    class App:
        def __init__(self, name, quit_button=None):
            self.menu, self.title = _AppMenu(), None

        def run(self):
            made.append(self)

    class Timer:
        def __init__(self, callback, interval):
            self.interval = interval

        def start(self):
            pass

        def stop(self):
            pass

    rumps = types.SimpleNamespace(
        App=App, MenuItem=_AppItem, Timer=Timer, notification=lambda *a, **k: None,
        alert=lambda *a, **k: 0, quit_application=lambda: None,
        rumps=types.SimpleNamespace(NSApp=types.SimpleNamespace()),
    )
    assert menubar_app.run_app(switcher, rumps, MagicMock()) == 0
    app = made[0]
    _wait_for_read(app)
    return app


def _wait_for_read(app):
    import time

    deadline = time.monotonic() + 30
    while app._refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not app._refreshing


def _switcher(temp_home):
    from ai_quota_swap.models import Platform
    from ai_quota_swap.switcher import ClaudeAccountSwitcher

    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


@pytest.mark.skipif(sys.platform == "win32", reason="the menu bar app is macOS's; its fakes are POSIX paths")
def test_the_app_reads_the_days_only_for_the_chart_and_turning_it_on_shows_it(temp_home, monkeypatch):
    s = _switcher(temp_home)
    root = s.backup_dir
    _write_ledger(root, [("claude", _hour(_dt.date.today()), "a@example.com|", MODEL, "p", "s1", *_toks(1_000))])
    MenuBarSettings(usage_chart=False).save(root / "menubar_settings.json")
    calls = []
    real = menubar_data.cost_days
    monkeypatch.setattr(menubar_data, "cost_days", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    app = _start_app(s, monkeypatch)
    # Off: the cost read leaves the days out, and the menu has no chart.
    assert app._cost is not None and app._cost.days == () and calls == []
    stamp = menubar_data.ledger_stamp(root)
    app.dispatch(menubar.Action("toggle-chart"))
    # On: read again at once, with the days, though the ledger is as it was.
    _wait_for_read(app)
    assert menubar_data.ledger_stamp(root) == stamp
    assert calls == [1] and app._cost.days
    app.on_sync_tick(None)
    assert menubar.build_menu(app._menu_input()).section("cost").rows[0].chart is not None
    # A refresh with nothing changed reads nothing.
    app.refresh_async()
    _wait_for_read(app)
    assert calls == [1]
    # Off again: no days read.
    app.dispatch(menubar.Action("toggle-chart"))
    _wait_for_read(app)
    assert calls == [1] and app._cost.days == ()


@pytest.mark.skipif(sys.platform == "win32", reason="the menu bar app is macOS's; its fakes are POSIX paths")
def test_the_apps_on_off_rows_flip_their_own_setting_save_it_and_redraw(temp_home, monkeypatch):
    s = _switcher(temp_home)
    path = s.backup_dir / "menubar_settings.json"
    app = _start_app(s, monkeypatch)

    def settings_rows():
        settings = next(i for i in app.menu.items if i is not None and i.title == "Settings")
        return {c.title: c.state for c in settings.children if c is not None and c.title in TOGGLE_ROWS}

    for text, name in TOGGLE_ROWS.items():
        for _click in range(2):
            before = json.loads(path.read_text()) if path.exists() else MenuBarSettings().to_json()
            click = menubar.build_menu(app._menu_input()).find(text).action
            app.dispatch(click)
            after = json.loads(path.read_text())
            assert after == {**before, name: not before[name]}, text
            assert getattr(app.settings, name) is after[name]
            assert settings_rows()[text] == int(after[name])
            _wait_for_read(app)


# --- the off-screen renderer's scenario path ----------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the demo sandbox's guards are POSIX-only")
@pytest.mark.parametrize("scenario", ["usage", "showcase"])
def test_the_renderer_reads_a_scenario_inside_the_demo_sandbox(scenario):
    # scripts/demo/menubar_chart.py builds a scenario's chart in a child
    # process under the demo guards and hands back only its description.
    import os
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "demo" / "menubar_chart.py"), "--describe", scenario],
        capture_output=True, text=True, timeout=120, cwd=repo,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    chart = data["chart"]
    legend = [(e["name"], menubar_chart.whole_dollars(e["amount"])) for e in chart["legend"]]
    assert len(chart["columns"]) == 7 and chart["columns"][-1]["today"]
    assert any(r["checked"] for r in data["rows"])  # the rows that set the menu's width
    if scenario == "usage":
        assert chart["caption"].endswith("7 days $180.55 (estimated)")  # qswap usage's total
        # Codex is in the menu: only its entries say their provider.
        assert [name for name, _amount in legend] == ["#1 personal", "#2 work", "#3 night-shift", "other"]
    else:
        # The week the legend tests above use is this scenario's.
        mirror = build_chart(_cost(SHOWCASE), both=True)
        assert chart["caption"] == mirror.caption
        assert legend == [(e.name, e.amount_text) for e in mirror.legend]
        assert [round(sum(s["amount"] for s in c["segments"]), 4) for c in chart["columns"]] == [
            round(c.total, 4) for c in mirror.columns
        ]
