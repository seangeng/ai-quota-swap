"""Tests for the ``re-login in …`` note: when it shows, how it reads, and
how it fits in every view that lists accounts (CLI list/status, TUI dashboard
card and mini rows, TUI auto-view Next-best rows, the menu bar label)."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.cells import cell_len

from ai_quota_swap import oauth
from ai_quota_swap.json_output import USAGE_API_KEY, USAGE_NO_CREDENTIALS, USAGE_RELOGIN_REQUIRED
from ai_quota_swap.models import AccountSnapshot
from ai_quota_swap.switcher import ClaudeAccountSwitcher, _usage_entry_lines
from ai_quota_swap.tui import widgets
from ai_quota_swap.usage_store import UsageEntry

from tests.test_tui import FakeSwitcher, fake_engine, make_account, make_app, make_entry

NOW = 1_000_000.0
DAY = 86400
DUE = 5 * DAY + 12 * 3600 + 30  # "5d 12h" (with 30 s for the render to happen in)
FAR = 23 * DAY  # a fresh login: nothing to say for weeks


def _replace_login(acc: AccountSnapshot, login_expires_at: float | None) -> AccountSnapshot:
    return dataclasses.replace(acc, login_expires_at=login_expires_at)


async def _workers_done(pilot) -> None:
    """Wait for the thread workers only (the engine worker runs until its screen
    stops it); the caller then pushes its screen, or pauses once."""
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
    if pending:
        await app.workers.wait_for_complete(pending)


# ---------------------------------------------------------------------------
# The rule: oauth.login_time_left / format_login_expiry
# ---------------------------------------------------------------------------


class TestLoginTimeLeft:
    def test_nothing_to_say_without_a_stamp(self):
        assert oauth.login_time_left(None, False, NOW) is None

    def test_nothing_to_say_further_out_than_a_week(self):
        assert oauth.login_time_left(NOW + 7 * DAY + 1, False, NOW) is None
        assert oauth.login_time_left(NOW + FAR, False, NOW) is None

    def test_due_inside_a_week(self):
        assert oauth.login_time_left(NOW + 7 * DAY, False, NOW) == 7 * DAY
        assert oauth.login_time_left(NOW + 3600, False, NOW) == 3600

    def test_past_reads_as_zero(self):
        assert oauth.login_time_left(NOW - 10, False, NOW) == 0.0
        assert oauth.login_time_left(NOW, False, NOW) == 0.0

    def test_quarantined_says_nothing_even_when_past(self):
        # The usage sentinel already puts "re-login needed" on that row.
        assert oauth.login_time_left(NOW - 10, True, NOW) is None
        assert oauth.login_time_left(NOW + 3600, True, NOW) is None

    @pytest.mark.parametrize("stamp", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_stamp_reads_as_unknown(self, stamp):
        creds = json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": stamp}})
        expires_at = oauth.login_expires_at_epoch(creds)
        assert expires_at is None
        assert oauth.format_login_expiry(expires_at, False, NOW) is None


class TestFormatLoginExpiry:
    def test_reads_in_the_reset_counters_grammar(self):
        assert oauth.format_login_expiry(NOW + DUE, False, NOW) == "re-login in 5d 12h"
        assert oauth.format_login_expiry(NOW + 3600 + 59 * 60, False, NOW) == "re-login in 1h 59m"
        assert oauth.format_login_expiry(NOW + 45 * 60, False, NOW) == "re-login in 45m"

    def test_same_countdown_as_a_reset(self):
        # One formatter behind both: a window resetting in 5d 12h and a login
        # expiring in 5d 12h show the same figure.
        assert oauth.format_countdown(DUE) == "5d 12h"
        future = datetime.now(timezone.utc) + timedelta(seconds=DUE + 30)
        assert oauth.format_reset(future.isoformat())[0] == "5d 12h"

    def test_short_keeps_the_first_unit(self):
        assert oauth.format_login_expiry(NOW + DUE, False, NOW, short=True) == "re-login in 5d"
        assert oauth.format_login_expiry(NOW + 3600 + 59 * 60, False, NOW, short=True) == "re-login in 1h"
        assert oauth.format_login_expiry(NOW + 45 * 60, False, NOW, short=True) == "re-login in 45m"

    def test_past(self):
        assert oauth.format_login_expiry(NOW - 10, False, NOW) == "needs re-login"
        assert oauth.format_login_expiry(NOW, False, NOW, short=True) == "needs re-login"

    def test_silent_when_there_is_nothing_to_say(self):
        assert oauth.format_login_expiry(None, False, NOW) is None
        assert oauth.format_login_expiry(NOW + FAR, False, NOW) is None
        assert oauth.format_login_expiry(NOW - 10, True, NOW) is None


# ---------------------------------------------------------------------------
# CLI list block: `switcher._usage_entry_lines`, and via `list_accounts`
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


_MEASURED = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 50.0}}


class TestCliList:
    def test_no_row_while_the_login_is_not_due(self):
        now = time.time()
        entry = UsageEntry(last_good=_MEASURED, fetched_at=now)
        fresh = _usage_entry_lines(entry, now + FAR)
        assert fresh == _usage_entry_lines(entry, None)  # exactly the block without a stamp
        assert not any("login" in line for line in fresh)
        assert any("5h:  10%" in line for line in fresh)  # label column unwidened

    def test_due_row_shares_the_reset_columns(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        # `list_accounts` sources the non-active slot's creds through
        # `_read_account_credentials` (patched below): that is the slot whose
        # 5h/7d rows the login row is compared against.
        expires_ms = int((time.time() + DUE) * 1000)
        backup_creds = json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-backup", "refreshTokenExpiresAt": expires_ms}}
        )
        usage_response = {
            "five_hour": {"utilization": 10.0, "resets_at": _iso_in(7200)},
            "seven_day": {"utilization": 50.0, "resets_at": _iso_in(3 * DAY)},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(usage_response).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("ai_quota_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            switcher.list_accounts()

        lines = capsys.readouterr().out.splitlines()
        line_7d = next(line for line in lines if "7d " in line and "resets" in line)
        (line_login,) = [line for line in lines if "login " in line and "expires" in line]
        assert line_login.lstrip().startswith("└ login ")
        assert line_login.endswith("in 5d 12h")
        # Clock under clock; both say "· in" before the countdown.
        assert line_login.index("expires ") + len("expires ") == line_7d.index("resets ") + len("resets ")
        assert " · in " in line_login and " · in " in line_7d

    def test_past_row(self):
        now = time.time()
        lines = _usage_entry_lines(UsageEntry(last_good=_MEASURED, fetched_at=now), now - 60)
        (line,) = [line for line in lines if "login:" in line]
        assert line.startswith("└ ") and "expired " in line and line.endswith("needs re-login")

    def test_under_a_sentinel_and_a_missing_measurement(self):
        now = time.time()
        no_creds = _usage_entry_lines(UsageEntry(sentinel=USAGE_NO_CREDENTIALS), now + DUE)
        assert no_creds[1].startswith("└ login:") and no_creds[1].endswith("in 5d 12h")
        unavailable = _usage_entry_lines(UsageEntry(last_error="timeout"), now + DUE)
        assert unavailable[0].startswith("usage unavailable")
        assert unavailable[1].startswith("└ login:")

    def test_quarantined_block_says_it_once(self):
        now = time.time()
        lines = _usage_entry_lines(UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED), now - 60)
        assert lines[0].startswith("needs re-login")
        assert len(lines) == 1

    def test_age_note_stays_on_the_last_usage_row(self):
        now = time.time()
        entry = UsageEntry(last_good=_MEASURED, fetched_at=now - 3 * 3600, age_s=3 * 3600)
        with_note = _usage_entry_lines(entry, now + DUE)
        assert "ago" in with_note[-2] and "7d:" in with_note[-2]
        assert "login:" in with_note[-1] and "ago" not in with_note[-1]


# ---------------------------------------------------------------------------
# TUI dashboard: the card's login row, the one-line rows, the fit
# ---------------------------------------------------------------------------


PLAN = {"plan": {"tier": "default_claude_max_20x"}, "fetchedAt": time.time()}


def _drawn(acc: AccountSnapshot, width: int = 100, *, row: bool = False, now: float | None = None) -> str:
    """The dashboard's card, or its one-line row, for one Claude account."""
    from ai_quota_swap.models import AccountsSnapshot
    from ai_quota_swap.tui import data
    from ai_quota_swap.tui.theme import Palette

    view = data.ProviderView("claude", AccountsSnapshot(None, (acc,), time.time()))
    block = data.block_for(view, acc, now=now)
    if row:
        return widgets.row_text(block, width, Palette.DARK).plain
    return widgets.card_text(block, width, Palette.DARK).plain


class TestCardAndRow:
    def test_nothing_while_the_login_is_not_due(self):
        acc = make_account(2, email="a@example.com", alias="dev")
        fresh = _replace_login(acc, time.time() + FAR)
        assert _drawn(fresh) == _drawn(acc)
        assert _drawn(fresh, row=True) == _drawn(acc, row=True)

    def test_the_card_has_a_login_row_and_the_row_ends_with_it(self):
        now = time.time()
        acc = _replace_login(
            dataclasses.replace(make_account(2, email="a@example.com", alias="dev"), subscription_plan=PLAN),
            now + 5 * 3600 + 7 * 60 + 30,
        )
        login = [ln for ln in _drawn(acc, now=now).splitlines() if ln.strip().startswith("login")]
        assert len(login) == 1 and login[0].rstrip().endswith("· in 5h 7m")
        assert _drawn(acc, row=True, now=now) == (
            "#2 dev (a@example.com) · Max 20x   5h 25% · 7d 10% · login expires in 5h 7m"
        )

    def test_quarantined_says_it_once(self):
        now = time.time()
        acc = _replace_login(
            make_account(1, active=True, entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED)), now - 60
        )
        card = _drawn(acc, now=now)
        assert card.count("needs re-login") == 1
        assert "login  expired" not in card
        assert _drawn(acc, row=True, now=now).count("re-login") == 1

    def test_api_key_slot_shows_nothing(self):
        acc = make_account(3, kind="api_key", entry=make_entry(sentinel=USAGE_API_KEY))
        assert "login" not in _drawn(acc, row=True)
        assert "login" not in _drawn(acc)

    def test_past_stamp_without_the_sentinel_yet(self):
        now = time.time()
        acc = _replace_login(make_account(2), now - 60)
        assert _drawn(acc, row=True, now=now).endswith(" · login expired")
        assert "needs re-login" in _drawn(acc, now=now)

    def test_a_narrow_row_keeps_its_figures_before_the_login_note(self):
        now = time.time()
        acc = _replace_login(make_account(2, email="developer2@example.com"), now + DUE)
        row = _drawn(acc, 44, row=True, now=now)
        assert cell_len(row) <= 44
        assert "5h 25% · 7d 10%" in row and "login" not in row


def _seven(expires_at: float | None) -> list[AccountSnapshot]:
    """Seven `developerN@example.com` slots, the active one on a plan."""
    accounts = [
        _replace_login(
            make_account(n, active=n == 1, email=f"developer{n}@example.com", alias="work" if n in (1, 2) else ""),
            expires_at,
        )
        for n in range(1, 8)
    ]
    accounts[0] = dataclasses.replace(accounts[0], subscription_plan=PLAN)
    return accounts


async def _dashboard(tmp_path, accounts, size):
    """(panel lines, panel content size, fully visible menu items, menu items) at ``size``."""
    from textual.widgets import ListView

    from ai_quota_swap.tui.widgets import AccountsPanel, MenuItem

    app = make_app(FakeSwitcher(accounts, tmp_path))
    async with app.run_test(size=size) as pilot:
        await _workers_done(pilot)
        await pilot.pause()
        panel = app.screen.query_one(AccountsPanel)
        menu = app.screen.query_one("#menu", ListView)
        items = list(menu.query(MenuItem))
        shown = sum(1 for item in items if menu.region.contains_region(item.region))
        return panel.render().plain.splitlines(), panel.content_size, shown, len(items)


class TestDashboardFit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("size", [(80, 24), (88, 24), (120, 40)])
    async def test_nothing_wraps_and_the_menu_shows_whole(self, tmp_path, size):
        lines, panel, shown, items = await _dashboard(tmp_path, _seven(time.time() + DUE), size)
        assert all(cell_len(line) <= panel.width for line in lines)
        assert panel.height == len(lines)  # nothing wrapped
        assert shown == items

    @pytest.mark.asyncio
    async def test_the_note_shows_where_there_is_room(self, tmp_path):
        lines, _panel, _shown, _items = await _dashboard(tmp_path, _seven(time.time() + DUE), (120, 40))
        rows = [line for line in lines if line.startswith(("#2", "#3", "#4", "#5", "#6", "#7"))]
        assert len(rows) == 6
        assert all(line.endswith("login expires in 5d 12h") for line in rows)

    @pytest.mark.asyncio
    async def test_fresh_logins_render_exactly_as_without_the_field(self, tmp_path):
        fresh, *_ = await _dashboard(tmp_path, _seven(time.time() + FAR), (80, 24))
        bare, *_ = await _dashboard(tmp_path, _seven(None), (80, 24))
        assert fresh == bare


# ---------------------------------------------------------------------------
# TUI auto view: Next-best rows
# ---------------------------------------------------------------------------


async def _candidates(tmp_path, accounts, size):
    from textual.widgets import Static

    from ai_quota_swap.tui.autoview import AutoScreen

    app = make_app(FakeSwitcher(accounts, tmp_path))
    async with app.run_test(size=size) as pilot:
        await _workers_done(pilot)
        await app.push_screen(AutoScreen())
        await pilot.pause()
        box = app.screen.query_one("#candidates-claude", Static)
        return box.render().plain, box.size.width


class TestNextBest:
    @pytest.mark.asyncio
    async def test_rows_fit_and_name_the_account_as_list_does(self, tmp_path, fake_engine):
        expires = time.time() + DUE
        accounts = [make_account(1, active=True, email="active@example.com")] + [
            _replace_login(make_account(n, email=f"{'x' * 27}{n}@example.com"), expires) for n in (2, 3)
        ]
        plain, width = await _candidates(tmp_path, accounts, (80, 24))
        rows = [line for line in plain.splitlines() if "% used" in line]
        assert len(rows) == 2
        assert all(cell_len(row) <= width for row in rows)
        assert all(row.lstrip().startswith("#") for row in rows)


# ---------------------------------------------------------------------------
# TUI switch screen: the account cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_screen_cards_stay_within_the_width(tmp_path):
    from ai_quota_swap.tui.dashboard import SwitchScreen
    from ai_quota_swap.tui.widgets import AccountCard

    acc = _replace_login(
        make_account(1, active=True, email="dev-account-1@a.io", alias="work"), time.time() + DUE
    )
    for size in ((100, 24), (76, 24)):
        app = make_app(FakeSwitcher([acc, make_account(2)], tmp_path))
        async with app.run_test(size=size) as pilot:
            await _workers_done(pilot)
            await app.push_screen(SwitchScreen())
            await pilot.pause()
            card = app.screen.query(AccountCard).first()
            lines = card.render().plain.splitlines()
            assert all(cell_len(line) <= card.size.width for line in lines)
            assert any(line.strip().startswith("login") and line.rstrip().endswith("in 5d 12h")
                       for line in lines)
