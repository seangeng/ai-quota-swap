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
    chart = build_chart(_cost(SEVEN))
    names = [e.name for e in chart.legend]
    # By seven-day amount: #1 $101.70, #2 $66.80, Codex #1 $26.60, #3 $24.30.
    assert names == ["#1 personal", "#2 work", "#1 maya@example.com", "#3 night-shift", "other"]
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
    chart = build_chart(_cost(SEVEN))
    colours = {s.key: s.colour for c in chart.columns for s in c.segments}
    # Claude Code from blue in order, Codex from pink.
    assert colours["claude#1"] == menubar_chart.ACCOUNT_COLOURS.index("blue")
    assert colours["claude#2"] == menubar_chart.ACCOUNT_COLOURS.index("teal")
    assert colours["claude#3"] == menubar_chart.ACCOUNT_COLOURS.index("indigo")
    assert colours["codex#1"] == menubar_chart.ACCOUNT_COLOURS.index("pink")
    assert "red" not in menubar_chart.ACCOUNT_COLOURS
    # The same in every column.
    for column in chart.columns:
        for seg in column.segments:
            assert seg.colour == colours[seg.key]
    # A later week where #2 overtakes #1: neither changes colour.
    later = build_chart(_cost({A1: (1,) * 7, A2: (9,) * 7}))
    assert [e.key for e in later.legend] == ["claude#2", "claude#1"]
    assert {e.key: e.colour for e in later.legend} == {"claude#1": colours["claude#1"], "claude#2": colours["claude#2"]}


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


def test_names_read_as_the_rows_read_and_the_provider_only_to_tell_two_apart():
    chart = build_chart(_cost({A1: (5,) * 7, X1: (4,) * 7}))
    assert [e.name for e in chart.legend] == ["#1 personal", "#1 maya@example.com"]
    chart = build_chart(_cost({A2: (5,) * 7, X2: (4,) * 7}))
    assert [e.name for e in chart.legend] == ["Claude #2 work", "Codex #2 work"]
    assert [e.head for e in chart.legend] == [len("Claude #2"), len("Codex #2")]
    # The rows' names win over the ones the read carried (an alias set since).
    chart = build_chart(_cost({A2: (5,) * 7}), names={("claude", "2"): "#2 renamed"})
    assert chart.legend[0].name == "#2 renamed"


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


def test_fit_names_cuts_the_longest_first_and_keeps_numbers():
    names = ["#1 personal", "#2 a-very-long-account-name", "other"]
    heads = [2, 2, 0]
    out = fit_names(names, heads, lambda ns: sum(map(len, ns)) <= 30)
    assert out == ["#1 personal", "#2 a-very-lon…", "other"]
    # Tighter: both cut to the same length, down to the number and 3 letters.
    out = fit_names(names, heads, lambda ns: sum(map(len, ns)) <= 20)
    assert out == ["#1 per…", "#2 a-v…", "other"]
    # Tighter still: the end of the legend goes to its number first, and
    # the room that frees goes back to the others.
    out = fit_names(names, heads, lambda ns: sum(map(len, ns)) <= 15)
    assert out == ["#1 pers…", "#2", "other"]
    # Nothing fits: as short as it goes; "other" is never cut.
    assert fit_names(names, heads, lambda ns: False) == ["#1", "#2", "other"]
    # A cut never ends in a dot before the ellipsis.
    assert fit_names(["jordan.lee@example.org"], [0], lambda ns: len(ns[0]) <= 8) == ["jordan…"]


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
    assert [e.name for e in menu.section("cost").rows[0].chart.legend] == ["#1 personal", "#2 work"]


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

    stamp, cost = menubar_data.ledger_stamp(root), menubar_data.read_cost(root, today=TODAY)
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


def test_the_app_has_a_handler_that_saves_the_setting():
    import inspect

    from ai_quota_swap import menubar_app

    source = inspect.getsource(menubar_app)
    assert (
        "def _do_toggle_chart(self, _a: Action):\n"
        "            self.settings.usage_chart = not self.settings.usage_chart\n"
        "            self._save_and_rebuild()"
    ) in source



# --- the off-screen renderer's scenario path ----------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the demo sandbox's guards are POSIX-only")
def test_the_renderer_reads_a_scenario_inside_the_demo_sandbox():
    # scripts/demo/menubar_chart.py builds a scenario's chart in a child
    # process under the demo guards and hands back only its description.
    import os
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / "demo" / "menubar_chart.py"), "--describe", "usage"],
        capture_output=True, text=True, timeout=120, cwd=repo,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    chart = data["chart"]
    assert chart["caption"].endswith("7 days $180.55 (estimated)")  # qswap usage's total
    assert [e["name"] for e in chart["legend"]] == ["#1 personal", "#2 work", "#3 night-shift", "other"]
    assert len(chart["columns"]) == 7 and chart["columns"][-1]["today"]
    assert any(r["checked"] for r in data["rows"])  # the rows that set the menu's width
