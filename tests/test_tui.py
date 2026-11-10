"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against fake switchers that
implement exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows, ``check_switch_target``, ``set_aside_accounts``) — one for Claude Code
and one for Codex. No scraping, no real credentials, no network, no ledger
scan (``ledger=False`` unless a test stubs the ledger read).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_quota_swap import display
from ai_quota_swap.autoswitch import NoSwitchEvent, SwitchEvent
from ai_quota_swap.exceptions import ConfigError, SwitchError
from ai_quota_swap.json_output import (
    USAGE_API_KEY,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from ai_quota_swap.models import AccountPolicy, AccountSnapshot, AccountsSnapshot
from ai_quota_swap.switcher import ClaudeAccountSwitcher
from ai_quota_swap.tui import data as tui_data
from ai_quota_swap.usage_store import UsageEntry


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def make_entry(
    pct5: float | None = 25.0,
    pct7: float | None = 10.0,
    *,
    sentinel: str | None = None,
    age_s: float = 5.0,
    scoped: list[tuple[str, float]] | None = None,
    spend: dict | None = None,
    error: str | None = None,
) -> UsageEntry:
    """``pct5``/``pct7`` of None omit that window (e.g. annual plans lack 7d)."""
    if sentinel is not None:
        return UsageEntry(sentinel=sentinel)
    if error is not None and pct5 is None and pct7 is None:
        return UsageEntry(last_error=error, fetched_at=None)
    last_good: dict = {}
    if pct5 is not None:
        last_good["five_hour"] = {"pct": pct5, "resets_at": _iso_in(7200)}
    if pct7 is not None:
        last_good["seven_day"] = {"pct": pct7, "resets_at": _iso_in(86400 * 3)}
    if scoped is not None:
        last_good["scoped"] = [
            {"name": name, "pct": pct, "resets_at": _iso_in(86400 * 2)}
            for name, pct in scoped
        ]
    if spend is not None:
        last_good["spend"] = spend
    return UsageEntry(
        last_good=last_good,
        fetched_at=time.time() - age_s,
        age_s=age_s,
        last_error=error,
    )


def make_account(
    number: int | str,
    *,
    active: bool = False,
    switchable: bool = True,
    kind: str = "oauth",
    entry: UsageEntry | None = None,
    email: str | None = None,
    alias: str = "",
    disabled: bool = False,
    org_name: str = "",
    policy: AccountPolicy | None = None,
    login_expires_at: float | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=str(number),
        email=email or f"user{number}@example.com",
        org_name=org_name,
        org_uuid="",
        is_active=active,
        kind=kind,
        switchable=switchable,
        usage=entry if entry is not None else make_entry(),
        alias=alias,
        disabled=disabled,
        policy=policy or AccountPolicy(),
        login_expires_at=login_expires_at,
    )


def make_usage_at(
    fetched_at: float | None,
    pct: float = 25.0,
    *,
    sentinel: str | None = None,
) -> UsageEntry:
    return UsageEntry(
        sentinel=sentinel,
        last_good={"five_hour": {"pct": pct, "resets_at": _iso_in(7200)}},
        fetched_at=fetched_at,
        age_s=(time.time() - fetched_at) if fetched_at is not None else None,
    )


class FakeSwitcher:
    """Structured-surface stand-in for ClaudeAccountSwitcher."""

    provider_id = "claude"

    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        self._accounts = list(accounts)
        self.backup_dir = backup_dir
        self.active = next(
            (a.number for a in accounts if a.is_active), None
        )
        self.calls: list[tuple] = []
        self.fetch_sets: list[set[str] | None] = []
        self.plan_refreshes = 0
        self.set_aside: dict[str, str] = {}
        self.live_email: str | None = None
        self.refuse: dict[str, str] = {}  # number -> why it needs re-login
        self.snapshot_error: Exception | None = None
        self.switch_error: Exception | None = None

    # -- surface the TUI consumes ------------------------------------------

    def refresh_subscription_plans(self) -> None:
        self.plan_refreshes += 1

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return AccountsSnapshot(
            active_number=self.active,
            accounts=tuple(self._accounts),
            taken_at=time.time(),
        )

    def set_aside_accounts(self) -> dict[str, str]:
        return dict(self.set_aside)

    def live_login_email(self) -> str | None:
        return self.live_email

    def current_account_number(self) -> str | None:
        return self.active

    def check_switch_target(self, number: str, *, anyway: bool = False) -> list[str]:
        self.calls.append(("check", str(number), anyway))
        why = self.refuse.get(str(number))
        if why is None:
            return []
        if not anyway:
            raise SwitchError(
                f"#{number} {display.NEEDS_RELOGIN} ({why}); not switched.\n"
                "Log in to it in Claude Code (/login), then run: qswap add\n"
                f"To switch to it anyway: qswap switch {number} --anyway"
            )
        return [f"#{number} {display.NEEDS_RELOGIN} ({why}); switching anyway"]

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict:
        self.calls.append(("switch_to", str(identifier)))
        if self.switch_error is not None:
            raise self.switch_error
        old = self.active
        self.active = str(identifier)
        self._accounts = [
            dataclasses.replace(a, is_active=(a.number == self.active))
            for a in self._accounts
        ]
        return {
            "switched": True,
            "from": {"number": int(old) if old else None, "email": ""},
            "to": {
                "number": int(identifier),
                "email": f"user{identifier}@example.com",
            },
            "reason": "requested",
        }

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict:
        self.calls.append(("switch", strategy))
        return {"switched": False, "from": None, "to": None, "reason": "no-better-target",
                "message": "Already on the account with the most quota left"}

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        self.calls.append(("remove", str(identifier), assume_yes))
        self._accounts = [a for a in self._accounts if a.number != str(identifier)]
        print(f"Removed #{identifier}")

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        self.calls.append(("set_disabled", str(identifier), disabled))
        self._accounts = [
            dataclasses.replace(a, disabled=disabled)
            if a.number == str(identifier)
            else a
            for a in self._accounts
        ]
        verb = "Disabled" if disabled else "Enabled"
        print(f"{verb} #{identifier}")

    def add_account(self, slot: int | None = None, assume_yes: bool = False) -> None:
        self.calls.append(("add", slot, assume_yes))
        print("Added #9 fresh@example.com")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        self.calls.append(("add_token", token, email, slot, assume_yes))
        print(f"Added #{slot or 9}")

    def set_poll_policy_inputs(
        self, threshold: float, weekly_threshold: float | None, models: tuple[str, ...]
    ) -> None:
        self._poll_inputs_override = (threshold, weekly_threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None


class FakeCodexSwitcher(FakeSwitcher):
    """The Codex provider's stand-in: its own accounts, its own numbers."""

    provider_id = "codex"

    def __init__(self, accounts: list[AccountSnapshot], data_root: Path):
        super().__init__(accounts, data_root / "codex")
        self.data_root = data_root
        self.logins: list[tuple] = []

    def managed_count(self) -> int:
        return len(self._accounts)

    def login_new_account(self, codex_args=(), *, slot=None, alias=None, assume_yes=False):
        self.logins.append((tuple(codex_args), slot, alias))
        return "3"


class BlockingSnapshotSwitcher(FakeSwitcher):
    """Fake switcher with independently gated normal/store snapshot lanes."""

    def __init__(
        self,
        normal_account: AccountSnapshot,
        store_account: AccountSnapshot,
        backup_dir: Path,
    ):
        super().__init__([normal_account], backup_dir)
        self.normal_account = normal_account
        self.store_account = store_account
        self.normal_started = threading.Event()
        self.normal_release = threading.Event()
        self.normal_done = threading.Event()
        self.store_started = threading.Event()
        self.store_release = threading.Event()
        self.store_done = threading.Event()
        self.block_store = False

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        if fetch is None:
            self.normal_started.set()
            self.normal_release.wait(timeout=2)
            self.normal_done.set()
            account = self.normal_account
        else:
            self.store_started.set()
            if self.block_store:
                self.store_release.wait(timeout=2)
            self.store_done.set()
            account = self.store_account
        return AccountsSnapshot(
            active_number=account.number,
            accounts=(account,),
            taken_at=time.time(),
        )


def make_app(fake: FakeSwitcher, codex: FakeSwitcher | None = None, **kwargs):
    from ai_quota_swap.tui.app import QswapApp

    kwargs.setdefault("ledger", False)
    return QswapApp(fake, codex=codex, **kwargs)


def fleet_fakes(tmp_path: Path) -> tuple[FakeSwitcher, FakeCodexSwitcher]:
    """Both providers, the demo's ``fleet`` in miniature."""
    claude = FakeSwitcher(
        [
            make_account(1, active=True, alias="personal", email="maya@example.com",
                         entry=make_entry(46.0, 38.0, scoped=[("Fable", 52.0)])),
            make_account(2, alias="work", email="maya@northwind.example.com",
                         org_name="Northwind Labs", entry=make_entry(12.0, 21.0)),
            make_account(3, alias="night-shift", email="ops@example.net",
                         entry=make_entry(93.0, 81.0)),
            make_account(4, alias="side", email="sam@example.org", disabled=True,
                         entry=make_entry(0.0, 9.0)),
        ],
        tmp_path,
    )
    codex = FakeCodexSwitcher(
        [
            make_account(1, active=True, email="maya@example.com", entry=make_entry(22.0, 47.0)),
            make_account(2, alias="work", email="maya@northwind.example.com",
                         entry=make_entry(8.0, 15.0)),
        ],
        tmp_path,
    )
    return claude, codex


async def settle(pilot) -> None:
    """Let thread workers finish and their UI updates apply.

    The (fake) auto engine worker deliberately runs until its screen stops
    it, so waiting on it would block; wait on everything else.
    """
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
    if pending:
        await app.workers.wait_for_complete(pending)
    await pilot.pause()
    await pilot.pause()


async def wait_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


def menu_ids(app) -> list[str]:
    from textual.widgets import ListView

    from ai_quota_swap.tui.widgets import MenuItem

    menu = app.screen.query_one("#menu", ListView)
    return [item.action_id for item in menu.query(MenuItem)]


def menu_labels(app) -> list[str]:
    from textual.widgets import ListView, Static

    from ai_quota_swap.tui.widgets import MenuItem

    menu = app.screen.query_one("#menu", ListView)
    return [item.query_one(Static).render().plain for item in menu.query(MenuItem)]


async def menu_select(pilot, action_id: str) -> None:
    """Drive the dashboard menu: highlight the entry by id, press Enter."""
    from textual.widgets import ListView

    from ai_quota_swap.tui.widgets import MenuItem

    menu = pilot.app.screen.query_one("#menu", ListView)
    items = list(menu.query(MenuItem))
    menu.index = next(
        i for i, item in enumerate(items) if item.action_id == action_id
    )
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def panel_text(app) -> str:
    from ai_quota_swap.tui.widgets import AccountsPanel

    return app.screen.query_one("#accounts-panel", AccountsPanel).render().plain


def view_for(acc_list, provider="claude", **kwargs) -> tui_data.ProviderView:
    active = next((a.number for a in acc_list if a.is_active), None)
    snap = AccountsSnapshot(active_number=active, accounts=tuple(acc_list), taken_at=time.time())
    kwargs.setdefault("lines", display.Lines(five_hour=90.0))
    return tui_data.ProviderView(provider, snap, **kwargs)


# ---------------------------------------------------------------------------
# A card is the command line's account block (decision: one renderer for words)
# ---------------------------------------------------------------------------


class TestBlocks:
    """``block_for`` builds the block ``qswap list`` prints, from a snapshot."""

    def _block(self, acc, **view):
        return tui_data.block_for(view_for([acc], **view), acc)

    def test_header_facts_in_the_command_lines_order(self):
        acc = make_account(2, active=True, alias="work", email="maya@example.com",
                           org_name="Northwind Labs", disabled=True)
        block = self._block(acc)
        assert display.header_parts(block, width=200, indent="").plain() == (
            "#2 work (maya@example.com) · active · disabled · Northwind Labs"
        )

    def test_needs_relogin_from_the_sentinel_or_the_engine(self):
        dead = make_account(2, entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED))
        assert self._block(dead).state == "needs-relogin"
        held = make_account(4, entry=make_entry(5.0, 30.0))
        block = self._block(held, set_aside={"4": "invalid_grant"})
        assert block.state == "needs-relogin"
        text = " ".join(t for t, _r in display.state_parts(block))
        assert text == (
            "needs re-login (its saved login was refused): "
            "log in to it in Claude Code (/login), then run: qswap add"
        )

    def test_codex_relogin_step_and_note(self):
        from ai_quota_swap.codex.switcher import USAGE_UNKNOWN_UNTIL_USED

        held = make_account(2, entry=make_entry(5.0, 30.0))
        block = self._block(held, provider="codex", set_aside={"2": "invalid_grant"})
        assert "qswap codex login" in display.state_parts(block)[0][0]
        unknown = make_account(2, entry=UsageEntry(sentinel=USAGE_UNKNOWN_UNTIL_USED))
        block = self._block(unknown, provider="codex")
        assert block.notes[0].startswith("usage unknown until used:")

    def test_unavailable_and_stale_words(self):
        failing = make_account(3, entry=make_entry(None, None, error="http-503"))
        assert display.state_parts(self._block(failing))[0][0] == (
            "usage unavailable (HTTP 503), retrying"
        )
        stale = make_account(5, entry=make_entry(40.0, 55.0, age_s=3 * 3600 + 300, error="timeout"))
        parts = [t for t, _r in display.state_parts(self._block(stale))]
        assert parts == ["as of 3h 5m ago · usage unavailable (timed out), retrying"]

    def test_extra_usage_is_labelled_extra(self):
        acc = make_account(2, active=True, entry=make_entry(
            12.0, 21.0, spend={"pct": 18.0, "used": 18.4, "limit": 100.0, "resets_at": _iso_in(86400 * 20)},
        ))
        labels = [w.label for w in self._block(acc).windows]
        assert labels[0] == "extra" and "$$" not in labels

    def test_severity_follows_the_accounts_own_line(self):
        acc = make_account(2, active=True, entry=make_entry(80.0, 10.0),
                           policy=AccountPolicy(threshold=75.0))
        five = self._block(acc).windows[0]
        assert five.line == 75.0 and display.severity(five.pct, five.line) == "warn"

    def test_a_session_threshold_stands_in_for_the_global_line_only(self):
        own = make_account(2, policy=AccountPolicy(threshold=60.0))
        plain = make_account(3)
        view = view_for([own, plain])
        assert tui_data.block_for(view, own, threshold=95.0).windows[0].line == 60.0
        assert tui_data.block_for(view, plain, threshold=95.0).windows[0].line == 95.0

    def test_api_key_spend_as_the_command_line_prints_it(self):
        entry = UsageEntry(sentinel=USAGE_API_KEY, api_spend={
            "spend": {"used": 14.0, "limit": 20.0, "pct": 70.0}, "today": 10.0,
        })
        acc = make_account(3, kind="api_key", entry=entry)
        block = self._block(acc)
        assert block.state == "api-key"
        assert block.notes[0] == "$10.00 today · $14.00 this month of $20 (70%)"

    def test_a_login_due_soon_is_a_row_of_its_own(self):
        acc = make_account(1, active=True, login_expires_at=time.time() + 5 * 86400 + 12 * 3600 + 30)
        login = [w for w in self._block(acc).windows if w.label == "login"]
        assert login and login[0].text.endswith("in 5d 12h")


class TestCards:
    def _card(self, acc, width=100, **view):
        from ai_quota_swap.tui.theme import Palette
        from ai_quota_swap.tui.widgets import card_text

        block = tui_data.block_for(view_for([acc], **view), acc)
        return card_text(block, width, Palette.DARK).plain

    def test_held_resets_read_as_a_sentence(self):
        from ai_quota_swap.tui.widgets import card_lines
        from ai_quota_swap.tui.theme import Palette

        far = (datetime.now(timezone.utc) + timedelta(days=40)).isoformat()
        entry = make_entry(46.0, 38.0)
        entry.last_good["resets"] = {"eligible": True, "grants": [
            {"id": "g", "label": "", "resets_left": 1, "resets_total": 1, "starts_at": None,
             "ends_at": far, "clears": ["seven_day_overage_included"], "blocking": [],
             "paused": False},
        ]}
        acc = make_account(1, active=True, entry=entry)
        lines = card_lines(tui_data.block_for(view_for([acc]), acc), 100, Palette.DARK)
        held = [ln for ln in lines if ln.text.plain.strip().startswith("Holds")]
        assert held and held[0].optional
        assert "resets:" not in "\n".join(ln.text.plain for ln in lines)

    def test_a_relogin_card_says_the_way_out_first(self):
        acc = make_account(2, active=True, entry=make_entry(5.0, 30.0))
        lines = self._card(acc, set_aside={"2": "invalid_grant"}).splitlines()
        assert lines[1].strip().startswith("needs re-login")

    def test_bars_line_up_across_cards_with_an_extra_row(self):
        from ai_quota_swap.tui.widgets import _BAR_FILLED, _BAR_EMPTY

        plain = make_account(1, active=True, entry=make_entry(50.0, 50.0))
        extra = make_account(2, entry=make_entry(50.0, 50.0, spend={
            "pct": 18.0, "used": 18.4, "limit": 100.0, "resets_at": _iso_in(86400 * 20)}))

        def bar_starts(acc):
            rows = self._card(acc).splitlines()[1:]
            return {r.split()[0]: min(r.find(_BAR_FILLED), r.find(_BAR_EMPTY))
                    for r in rows if (_BAR_FILLED in r or _BAR_EMPTY in r)}

        a, b = bar_starts(plain), bar_starts(extra)
        assert a["5h"] == b["5h"] == b["extra"]

    def test_a_row_ahead_of_pace_keeps_its_reset_clock(self):
        week = {"pct": 90.0, "resets_at": _iso_in(86400 * 5)}  # 90% with 5 of 7 days left
        entry = UsageEntry(last_good={"five_hour": {"pct": 10.0, "resets_at": _iso_in(3600)},
                                      "seven_day": week},
                           fetched_at=time.time(), age_s=1.0)
        acc = make_account(1, active=True, entry=entry)
        for width in (100, 80):
            row = next(r for r in self._card(acc, width).splitlines() if r.strip().startswith("7d"))
            assert "ahead of pace" in row and "resets " in row
            assert len(row) <= width

    def test_the_weekly_rows_tick_at_the_weekly_line(self):
        from ai_quota_swap.tui.widgets import _BAR_TICK

        acc = make_account(1, active=True, entry=make_entry(50.0, 50.0, scoped=[("Fable", 50.0)]))

        def ticks(weekly):
            plain = self._card(acc, lines=display.Lines(five_hour=85.0, weekly=weekly))
            rows = {ln.split()[0]: ln for ln in plain.splitlines()[1:]}
            return {label: ln.index(_BAR_TICK) for label, ln in rows.items()}

        with_weekly = ticks(97.0)
        assert with_weekly["7d"] > with_weekly["5h"]
        assert with_weekly["Fable"] == with_weekly["7d"]
        without = ticks(None)
        assert without["5h"] == without["7d"] == without["Fable"] == with_weekly["5h"]

    def test_light_palette(self):
        from ai_quota_swap.tui.theme import ACCENT_LIGHT, QSWAP_LIGHT, Palette
        from ai_quota_swap.tui.widgets import card_text

        acc = make_account(1, active=True, alias="dev", entry=make_entry(pct5=95.0))
        text = card_text(tui_data.block_for(view_for([acc]), acc), 100, Palette.from_theme(QSWAP_LIGHT))
        assert any(ACCENT_LIGHT in str(span.style) for span in text.spans)

    def test_an_accounts_own_line_shows_in_its_header(self):
        from ai_quota_swap.tui.theme import Palette
        from ai_quota_swap.tui.widgets import account_extra, card_text

        acc = make_account(2, active=True, alias="dev", policy=AccountPolicy(threshold=82.5))
        block = tui_data.block_for(view_for([acc]), acc)
        header = card_text(block, 100, Palette.DARK, extra=account_extra(acc)).plain.splitlines()[0]
        assert header.endswith("· switch at 82.5%")
        none = make_account(2, active=True, alias="dev")
        assert account_extra(none) == []


class TestRows:
    """One-line rows: nothing wraps; the email gives way, the figures stay."""

    def _row(self, acc, width, **view):
        from ai_quota_swap.tui.theme import Palette
        from ai_quota_swap.tui.widgets import row_text

        return row_text(tui_data.block_for(view_for([acc], **view), acc), width, Palette.DARK).plain

    @pytest.mark.parametrize("width", [60, 76, 96])
    def test_never_wider_than_the_panel(self, width):
        acc = make_account(2, alias="night-shift-backup", email="operations-team@northwind.example.com",
                           org_name="Northwind Labs International",
                           entry=make_entry(93.0, 81.0, scoped=[("Fable", 97.0)]))
        row = self._row(acc, width)
        assert len(row) <= width
        assert "5h 93%" in row and "Fable 97%" in row  # the figures are kept
        assert "night-shift-backup" in row  # the alias outlasts the email

    def test_states_in_the_command_lines_words(self):
        failing = make_account(3, entry=make_entry(None, None, error="http-503"))
        assert self._row(failing, 100).endswith("usage unavailable (HTTP 503), retrying")
        stale = make_account(5, entry=make_entry(40.0, 55.0, age_s=3 * 3600 + 300))
        assert self._row(stale, 100).endswith("5h 40% · 7d 55% · as of 3h 5m ago")
        dead = make_account(2, entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED))
        assert self._row(dead, 100).endswith("needs re-login")
        unread = make_account(6, entry=UsageEntry())
        assert self._row(unread, 100).endswith("usage not read yet")

    def test_a_limit_is_said_in_words(self):
        acc = make_account(1, entry=make_entry(100.0, 64.0))
        assert self._row(acc, 100).endswith("5h 100% · 7d 64% · limit reached")

    def test_a_due_login_ends_the_row_and_goes_first_when_narrow(self):
        acc = make_account(2, entry=make_entry(10.0, 10.0),
                           login_expires_at=time.time() + 5 * 86400 + 12 * 3600 + 30)
        assert self._row(acc, 100).endswith("login expires in 5d 12h")
        assert "login" not in self._row(acc, 40)


class TestPanelFit:
    """The dashboard's panel within ``max_rows``: what goes first."""

    def _lines(self, max_rows, width=76):
        from ai_quota_swap.tui.theme import Palette
        from ai_quota_swap.tui.widgets import fit_pieces, panel_pieces

        claude, codex = fleet_fakes(Path("."))
        fleet = tui_data.Fleet(
            views=(view_for(claude._accounts), view_for(codex._accounts, "codex")),
            taken_at=time.time(),
        )
        pieces = panel_pieces(fleet, width, Palette.DARK, None)
        return [line.plain for line in fit_pieces(pieces, max_rows, Palette.DARK)]

    def test_everything_when_it_fits(self):
        lines = self._lines(None)
        assert lines[0] == "Claude Code accounts"
        assert "Codex accounts" in lines
        assert sum(1 for ln in lines if ln.startswith("#")) == 6

    def test_a_crowded_panel_keeps_every_account(self):
        full = len(self._lines(None))
        lines = self._lines(full - 3)
        assert len(lines) <= full - 3
        assert sum(1 for ln in lines if ln.startswith("#")) == 6
        assert "" not in lines  # the blank between sections went first

    def test_a_tiny_panel_folds_rows_and_says_where_to_look(self):
        lines = self._lines(7)
        assert len(lines) <= 7
        assert any("more · w shows every account" in ln for ln in lines)

    @pytest.mark.parametrize("width", [76, 96])
    def test_no_line_wider_than_the_panel(self, width):
        assert all(len(ln) <= width for ln in self._lines(None, width))


class TestThemeColours:
    def test_command_line_colours_become_the_themes(self):
        from ai_quota_swap import printer
        from ai_quota_swap.tui.theme import QSWAP_LIGHT, Palette, from_printer

        light = Palette.from_theme(QSWAP_LIGHT)
        with printer.force_color():
            line = printer.yellowed("warn") + printer.accent("go")
        styles = [str(s.style) for s in from_printer(line, light).spans]
        assert styles == [light.sev_warn, light.accent]


class TestCostLine:
    def test_today_and_seven_days_from_a_by_day_report(self):
        from ai_quota_swap.ledger.report import Report, Row, Totals
        from ai_quota_swap.ledger.pricing import PriceTable

        today = datetime.now().date()
        rows = [Row(key=today - timedelta(days=1), label="", totals=Totals(input=1, cost=5.0)),
                Row(key=today, label="", totals=Totals(input=1, cost=2.5))]
        report = Report(by="day", since=today - timedelta(days=6), until=today, provider=None,
                        rows=rows, total=Totals(input=2, requests=2, cost=7.5),
                        unpriced_models={}, unattributed_tokens=0, profile_sessions=0,
                        prices=PriceTable(), attribution_starts={})
        line = tui_data.cost_line(type("R", (), {"report": report})())
        assert line.text() == "Today $2.50 · 7 days $7.50 · estimated at API prices"

    def test_nothing_in_the_ledger_hides_it(self):
        assert tui_data.cost_line(None) is None

    def test_no_ledger_file_and_no_scan_reads_nothing(self, tmp_path):
        assert tui_data.usage_report(tmp_path, scan=False) is None
        assert not (tmp_path / "ledger.db").exists()


class TestFleetSource:
    def test_both_providers_in_display_order(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        fleet = tui_data.FleetSource({"claude": claude, "codex": codex}).take()
        assert [v.provider for v in fleet.views] == ["claude", "codex"]
        assert fleet.sectioned
        assert fleet.label("codex", "2") == "Codex #2 work (maya@northwind.example.com)"

    def test_one_provider_with_accounts_has_no_headings(self, tmp_path):
        claude, _codex = fleet_fakes(tmp_path)
        empty = FakeCodexSwitcher([], tmp_path)
        fleet = tui_data.FleetSource({"claude": claude, "codex": empty}).take()
        assert not fleet.sectioned
        assert fleet.label("claude", "2") == "#2 work (maya@northwind.example.com)"
        assert empty.fetch_sets == []  # nothing managed: no snapshot asked for

    def test_a_provider_error_never_takes_the_other_down(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        codex.snapshot_error = ConfigError("Codex keeps its login in the OS keyring")
        fleet = tui_data.FleetSource({"claude": claude, "codex": codex}).take()
        assert fleet.view("codex").error == "Codex keeps its login in the OS keyring"
        assert len(fleet.view("claude").accounts) == 4

    def test_set_aside_and_live_login(self, tmp_path):
        claude = FakeSwitcher([make_account(1), make_account(2)], tmp_path)
        claude.active = None
        claude.live_email = "new@example.com"
        claude.set_aside = {"2": "invalid_grant"}
        view = tui_data.FleetSource({"claude": claude}).take().view("claude")
        assert view.live_email == "new@example.com"
        assert view.set_aside == {"2": "invalid_grant"}

    def test_codex_lines_come_from_its_own_threshold(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1, "autoswitch": {"threshold": 85}, "codex": {"threshold": 95},
        }))
        claude, codex = fleet_fakes(tmp_path)
        fleet = tui_data.FleetSource({"claude": claude, "codex": codex}).take()
        assert fleet.view("claude").lines.five_hour == 85.0
        assert fleet.view("codex").lines.five_hour == 95.0


class TestSnapshotSource:
    def _source(self, tmp_path: Path, accounts=None):
        fake = FakeSwitcher(
            accounts
            or [make_account(1, active=True), make_account(2)],
            tmp_path,
        )
        return fake, tui_data.SnapshotSource(fake)

    def test_every_pass_is_store_governed(self, tmp_path):
        # Pacing lives in the usage store (poll plans + freshness + atomic
        # reservation), so every take is the same on-demand pass `qswap list`
        # runs — including the user's explicit refresh, which cannot bypass
        # the store's per-account cadence.
        fake, source = self._source(tmp_path)
        source.take()
        source.take()
        source.take(full=True)
        assert fake.fetch_sets == [None, None, None]

    def test_store_only_never_fetches(self, tmp_path):
        fake, source = self._source(tmp_path)
        source.take(store_only=True)
        assert fake.fetch_sets == [set()]

    def test_expired_sentinel_retained_until_fetched_at_advances(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fresh_same_stamp = make_account(1, active=True, entry=make_usage_at(100.0))
        fresh_new_stamp = make_account(1, active=True, entry=make_usage_at(101.0))
        fake, source = self._source(tmp_path, [expired])

        assert source.take().accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_same_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_new_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_expired_sentinel_clears_on_superseding_sentinel(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        api_key = make_account(
            1,
            active=True,
            kind="api_key",
            entry=make_usage_at(None, sentinel=USAGE_API_KEY),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [api_key]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_API_KEY

    def test_expired_sentinel_clears_on_identity_replacement(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            email="old@example.com",
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        replacement = make_account(
            1,
            active=True,
            email="new@example.com",
            entry=make_usage_at(100.0),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [replacement]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_late_worker_fetched_at_regression_is_rejected(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        snap = source.take(store_only=True)
        usage = snap.accounts[0].usage
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0

    def test_late_expired_sentinel_cannot_replace_newer_usage(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, pct=10.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        usage = source.take(store_only=True).accounts[0].usage
        assert usage.sentinel is None
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0


class TestRunAction:
    def test_captures_output_and_payload(self):
        def fn():
            print("hello")
            return {"switched": True}

        result = tui_data.run_action(fn)
        assert result.ok and result.payload == {"switched": True}
        assert "hello" in result.output

    def test_switch_error_is_captured_not_raised(self):
        from ai_quota_swap.exceptions import ClaudeSwitchError

        def fn():
            raise ClaudeSwitchError("boom")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "boom" in result.output

    def test_unexpected_input_becomes_eoferror(self):
        def fn():
            input("should not block")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "interactive input" in result.output

    def test_first_line_strips_ansi(self):
        def fn():
            print("\x1b[1mBold headline\x1b[0m")

        assert tui_data.run_action(fn).first_line == "Bold headline"




# ---------------------------------------------------------------------------
# Pilot tests (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDashboard:
    async def test_panel_shows_active_full_and_others_on_one_line(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(47.0, 63.0)),
                make_account(2, entry=make_entry(92.0, 71.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            panel = panel_text(app)
            assert panel.splitlines()[0] == "#1 user1@example.com · active"
            assert "resets" in panel  # the active card is the full one
            row = next(ln for ln in panel.splitlines() if ln.startswith("#2"))
            assert row.endswith("5h 92% · 7d 71%")
            assert "━" not in row and "Claude Code accounts" not in panel  # one provider: no heading

    async def test_both_providers_in_sections(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 34)) as pilot:
            await settle(pilot)
            lines = panel_text(app).splitlines()
            assert lines[0] == "Claude Code accounts"
            codex_at = lines.index("Codex accounts")
            assert lines[codex_at + 1] == "#1 maya@example.com · active"
            assert any(ln.startswith("#2 work") and ln.endswith("5h 8% · 7d 15%")
                       for ln in lines[codex_at:])

    async def test_needs_relogin_and_set_aside_say_so_with_the_way_out(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, alias="work", entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED)),
                make_account(4, alias="side", entry=make_entry(5.0, 30.0)),
            ],
            tmp_path,
        )
        fake.set_aside = {"4": "invalid_grant"}
        app = make_app(fake)
        async with app.run_test(size=(110, 32)) as pilot:
            await settle(pilot)
            panel = panel_text(app)
            assert "needs re-login: log in to it in Claude Code (/login), then run: qswap add" in panel
            assert "needs re-login (its saved login was refused)" in panel

    async def test_disabled_marker_on_card_and_row(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1, active=True, disabled=True), make_account(2, disabled=True)],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert panel_text(app).count("· disabled") == 2

    async def test_root_menu_and_nesting(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            from ai_quota_swap.tui.dashboard import ROOT_MENU

            assert menu_ids(app) == [a for _l, a in ROOT_MENU]
            await menu_select(pilot, "add-menu")
            assert menu_ids(app) == [
                "heading", "add-login:claude", "add-token",
                "heading", "add-login:codex", "codex-login", "back",
            ]
            await pilot.press("escape")
            await pilot.pause()
            assert menu_ids(app)[0] == "switch"

    async def test_back_menu_entry_pops_submenu(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "back")
            assert menu_ids(app)[0] != "back"

    async def test_vim_keys_move_menu_cursor(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            menu = app.screen.query_one("#menu", ListView)
            assert menu.index == 0
            await pilot.press("j")
            assert menu.index == 1
            await pilot.press("k")
            assert menu.index == 0

    async def test_full_refresh_binding(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("f")
            await settle(pilot)
            assert fake.fetch_sets[-1] is None  # full on-demand pass

    async def test_palette_is_disabled(self, tmp_path):
        from ai_quota_swap.tui.app import QswapApp

        assert QswapApp.ENABLE_COMMAND_PALETTE is False

    async def test_cost_line_under_the_title(self, tmp_path):
        from ai_quota_swap.tui.app import QswapApp

        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        reads: list[bool] = []

        class Ledgered(QswapApp):
            def read_ledger(self, *, days=7, by="day", scan):
                reads.append(scan)
                return "result"

        from unittest.mock import patch

        line = tui_data.CostLine(today=12.4, week=179.72)
        with patch("ai_quota_swap.tui.app.cost_line", return_value=line):
            app = Ledgered(fake, ledger=True)
            async with app.run_test(size=(100, 32)) as pilot:
                await settle(pilot)
                from textual.widgets import Static

                cost = app.screen.query_one("#cost-line", Static)
                assert cost.display
                assert str(cost.render()) == "Today $12.40 · 7 days $179.72 · estimated at API prices"
        # Read what the ledger holds first, then scan in a worker.
        assert reads[:2] == [False, True]

    async def test_no_cost_line_without_ledger_data(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            from textual.widgets import Static

            assert not app.screen.query_one("#cost-line", Static).display

    async def test_at_80x24_nothing_wraps_and_the_menu_needs_no_scrollbar(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        claude._accounts.append(make_account(5, alias="ci", email="api-key-5@token.local",
                                             kind="api_key", entry=make_entry(sentinel=USAGE_API_KEY)))
        app = make_app(claude, codex)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            from textual.widgets import ListView

            from ai_quota_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one("#accounts-panel", AccountsPanel)
            lines = panel.render().plain.splitlines()
            assert all(len(ln) <= panel.size.width for ln in lines)
            assert sum(1 for ln in lines if ln.startswith("#")) == 7  # every account shows
            menu = app.screen.query_one("#menu", ListView)
            assert menu.size.height == len(menu_ids(app))
            assert menu.max_scroll_y == 0
            # The menu sits right under the accounts.
            assert menu.region.y <= panel.region.bottom + 1


@pytest.mark.asyncio
class TestFirstRun:
    async def test_live_login_is_the_first_action(self, tmp_path):
        fake = FakeSwitcher([], tmp_path)
        fake.live_email = "maya@example.com"
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert panel_text(app).splitlines() == display.nothing_managed_lines(
                "claude", live_email="maya@example.com",
            )
            assert menu_ids(app)[0] == "add-login:claude"
            assert menu_labels(app)[0] == "Add maya@example.com"
            await menu_select(pilot, "add-login:claude")
            await pilot.press("y")
            await settle(pilot)
            assert ("add", None, False) in fake.calls

    async def test_no_login_says_how_to_log_in_and_offers_only_what_works(self, tmp_path):
        fake = FakeSwitcher([], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            panel = panel_text(app)
            assert panel.startswith(display.NOTHING_MANAGED)
            assert "Log in with claude (/login), then run: qswap add" in panel or "not found on PATH" in panel
            ids = menu_ids(app)
            for useless in ("switch", "watch", "auto", "disable-menu", "remove-menu"):
                assert useless not in ids
            assert not any(i.startswith("add-login") for i in ids)
            await menu_select(pilot, "add-menu")
            assert "add-login:claude" not in menu_ids(app)
            assert "add-token" in menu_ids(app)
            # The keys for screens with nothing to show do nothing either.
            from ai_quota_swap.tui.dashboard import DashboardScreen

            await pilot.press("escape", "s", "w", "g")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)

    async def test_both_live_logins_offered_by_provider(self, tmp_path):
        claude = FakeSwitcher([], tmp_path)
        claude.live_email = "maya@example.com"
        codex = FakeCodexSwitcher([], tmp_path)
        codex.live_email = "maya@example.com"
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert menu_labels(app)[:2] == [
                "Add maya@example.com · Claude Code", "Add maya@example.com · Codex",
            ]
            assert "Codex is logged in as maya@example.com. Add it with: qswap codex add" in panel_text(app)
            await menu_select(pilot, "add-login:codex")
            await pilot.press("y")
            await settle(pilot)
            assert ("add", None, False) in codex.calls
            assert not any(c[0] == "add" for c in claude.calls)


@pytest.mark.asyncio
class TestSwitching:
    async def test_s_opens_the_picker_and_enter_switches(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from ai_quota_swap.tui.dashboard import DashboardScreen, SwitchScreen
            from ai_quota_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, SwitchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            assert [item.number for item in listview.query(AccountItem)] == ["1", "2"]
            assert listview.index == 0  # starts on the active account
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, DashboardScreen)
            assert app.snapshot.active_number == "2"

    async def test_picker_holds_both_providers_and_switches_the_right_one(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        notes: list[str] = []
        app.notify = lambda message, **kw: notes.append(str(message))  # type: ignore[method-assign]
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from ai_quota_swap.tui.widgets import AccountItem, HeadingItem

            listview = app.screen.query_one("#accounts", ListView)
            assert len(list(listview.query(HeadingItem))) == 2
            keys = [item.key for item in listview.query(AccountItem)]
            assert keys == [("claude", n) for n in "1234"] + [("codex", "1"), ("codex", "2")]
            # The cursor moves through every account in display order, over the headings.
            for _ in range(5):
                await pilot.press("down")
            assert listview.highlighted_child.key == ("codex", "2")
            await pilot.press("enter")
            await settle(pilot)
            assert ("switch_to", "2") in codex.calls
            assert not any(c[0] == "switch_to" for c in claude.calls)
            assert notes[-1].startswith("Switched to Codex #2 work")

    async def test_picker_escape_backs_out(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await pilot.press("enter")  # menu: Switch account…
            await pilot.pause()
            from ai_quota_swap.tui.dashboard import DashboardScreen, SwitchScreen

            assert isinstance(app.screen, SwitchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_best_pick_asks_the_highlighted_accounts_provider(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            for _ in range(4):
                await pilot.press("down")
            await pilot.press("b")
            await settle(pilot)
            assert ("switch", "best") in codex.calls
            assert not any(c[0] == "switch" for c in claude.calls)

    async def test_needs_relogin_is_refused_with_a_switch_anyway_choice(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2, alias="work")], tmp_path)
        fake.refuse = {"2": "its saved login was refused"}
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("claude", "2")
            await settle(pilot)
            from textual.widgets import Static

            from ai_quota_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            body = app.screen.query_one(".modal-body", Static).render().plain
            assert body == (
                "#2 needs re-login (its saved login was refused); not switched.\n"
                "Log in to it in Claude Code (/login), then run: qswap add"
            )
            assert not any(c[0] == "switch_to" for c in fake.calls)
            await pilot.press("enter")  # the focus starts on Cancel
            await settle(pilot)
            assert not any(c[0] == "switch_to" for c in fake.calls)
            app.do_switch("claude", "2")
            await settle(pilot)
            await pilot.press("y")
            await settle(pilot)
            assert ("check", "2", True) in fake.calls  # --anyway, not --force
            assert ("switch_to", "2") in fake.calls

    async def test_a_provider_error_shows_the_command_lines_text(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        fake.switch_error = SwitchError("Claude Code is running in /work; quit it first")
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("claude", "2")
            await settle(pilot)
            from ai_quota_swap.tui.modals import OutputModal

            assert isinstance(app.screen, OutputModal)
            assert "Error: Claude Code is running in /work; quit it first" in app.screen._output

    async def test_an_unexpected_failure_never_ends_in_a_traceback(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        fake.switch_error = RuntimeError("disk on fire")
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("claude", "2")
            await settle(pilot)
            from ai_quota_swap.tui.modals import OutputModal

            assert app.is_running
            assert isinstance(app.screen, OutputModal)
            assert "Error: disk on fire" in app.screen._output


@pytest.mark.asyncio
class TestAccountMenus:
    async def test_remove_menu_by_provider_and_routes_there(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            labels = menu_labels(app)
            assert labels[0] == "Claude Code accounts"
            assert "#2 work (maya@northwind.example.com)" in labels
            assert "Codex accounts" in labels
            await menu_select(pilot, "remove:codex:2")
            from textual.widgets import Static

            from ai_quota_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            body = app.screen.query_one(".modal-body", Static).render().plain
            assert body.startswith("Stop managing Codex #2 work (maya@northwind.example.com)?")
            await pilot.press("y")
            await settle(pilot)
            assert ("remove", "2", True) in codex.calls
            assert not any(c[0] == "remove" for c in claude.calls)

    async def test_remove_cancel_is_safe(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:claude:1")
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)

    async def test_modal_arrow_keys_choose_button(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:claude:2")
            await pilot.press("right", "enter")  # → Cancel
            await settle(pilot)
            assert not any(call[0] == "remove" for call in fake.calls)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("right", "left", "enter")
            await settle(pilot)
            assert ("remove", "2", True) in fake.calls

    async def test_labels_render_brackets_literally(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True, alias="red", email="[red]@example.com")], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remove-menu")
            assert any("[red]@example.com" in label for label in menu_labels(app))

    async def test_disable_toggles_without_confirm_and_says_so(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        notes: list[str] = []
        app.notify = lambda message, **kw: notes.append(str(message))  # type: ignore[method-assign]
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            labels = menu_labels(app)
            assert any(lb.startswith("#4 side (sam@example.org) · disabled") and lb.endswith("→ enable")
                       for lb in labels)
            assert any(lb.startswith("#2 work") and lb.endswith("→ disable") for lb in labels)
            await menu_select(pilot, "disable:codex:2")
            await settle(pilot)
            assert ("set_disabled", "2", True) in codex.calls
            assert menu_ids(app)[0] == "switch"  # back at the root
            assert notes[-1].startswith("Disabled Codex #2 work")

    async def test_add_token_passes_assume_yes(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "5"
            await pilot.click("#add")
            await settle(pilot)
            assert ("add_token", "sk-ant-oat01-test", None, 5, True) in fake.calls

    async def test_add_token_into_an_occupied_slot_asks_first(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "add-token")
            from textual.widgets import Input

            from ai_quota_swap.tui.modals import ConfirmModal

            app.screen.query_one("#token", Input).value = "sk-ant-oat01-test"
            app.screen.query_one("#slot", Input).value = "2"
            await pilot.click("#add")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")
            await settle(pilot)
            assert not any(call[0] == "add_token" for call in fake.calls)

    async def test_codex_login_shows_the_command_where_it_cannot_step_aside(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "add-menu")
            await menu_select(pilot, "codex-login")
            from ai_quota_swap.tui.modals import CommandModal

            assert isinstance(app.screen, CommandModal)  # the headless driver cannot suspend
            assert app.screen._command == "qswap codex login"
            assert codex.logins == []

    async def test_codex_login_steps_aside_and_comes_back(self, tmp_path, monkeypatch):
        import contextlib

        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        notes: list[str] = []
        app.notify = lambda message, **kw: notes.append(str(message))  # type: ignore[method-assign]
        suspended: list[bool] = []

        @contextlib.contextmanager
        def fake_suspend():
            suspended.append(True)
            yield

        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            monkeypatch.setattr(app, "can_suspend", lambda: True)
            monkeypatch.setattr(app, "suspend", fake_suspend)
            app.codex_login()
            await settle(pilot)
            assert suspended == [True]
            assert codex.logins == [((), None, None)]
            assert notes[-1] == "Added Codex #3."


@pytest.mark.asyncio
class TestWatchScreen:
    def _fake(self, tmp_path):
        return FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)

    async def test_w_opens_monitor_without_cursor(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import ListView

            from ai_quota_swap.tui.dashboard import WatchScreen
            from ai_quota_swap.tui.widgets import AccountItem

            assert isinstance(app.screen, WatchScreen)
            listview = app.screen.query_one("#accounts", ListView)
            assert len(list(listview.query(AccountItem))) == 2
            assert listview.index is None
            await pilot.press("enter")  # inert while just watching
            await settle(pilot)
            assert not any(call[0] == "switch_to" for call in app.switcher.calls)

    async def test_s_arms_selection_switch_stays_watching(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from textual.widgets import ListView

            from ai_quota_swap.tui.dashboard import WatchScreen

            listview = app.screen.query_one("#accounts", ListView)
            assert listview.index == 0
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake.calls
            assert isinstance(app.screen, WatchScreen)
            assert app.screen.query_one("#accounts", ListView).index is None
            assert app.snapshot.active_number == "2"

    async def test_escape_disarms_then_leaves(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            from textual.widgets import ListView

            from ai_quota_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            assert app.screen.query_one("#accounts", ListView).index is None
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert not any(call[0] == "switch_to" for call in fake.calls)

    async def test_watch_shows_both_providers(self, tmp_path):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "watch")
            from textual.widgets import ListView, Static

            from ai_quota_swap.tui.widgets import AccountItem, HeadingItem

            listview = app.screen.query_one("#accounts", ListView)
            headings = [h.query_one(Static).render().plain for h in listview.query(HeadingItem)]
            assert headings == ["Claude Code accounts", "Codex accounts"]
            assert len(list(listview.query(AccountItem))) == 6

    async def test_app_start_watch_stacks_over_dashboard(self, tmp_path):
        from ai_quota_swap.tui.app import QswapApp

        app = QswapApp(self._fake(tmp_path), start="watch", ledger=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            from ai_quota_swap.tui.dashboard import DashboardScreen, WatchScreen

            assert isinstance(app.screen, WatchScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)

    async def test_blocked_normal_allows_store_only_repaint_without_stale_overpaint(
        self, tmp_path
    ):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0
            assert fake.fetch_sets == [None, set()]

    async def test_late_normal_can_advance_usage_after_store_repaint(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        store = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        app = make_app(fake)

        async with app.run_test(size=(100, 40)) as pilot:
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 10.0

            fake.normal_release.set()
            await wait_event(fake.normal_done)
            await pilot.pause()
            assert app.snapshot.accounts[0].usage.last_good["five_hour"]["pct"] == 80.0

    async def test_repeated_ticks_keep_store_lane_single_flight(self, tmp_path):
        normal = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        store = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        fake = BlockingSnapshotSwitcher(normal, store, tmp_path)
        fake.block_store = True
        app = make_app(fake)

        async with app.run_test(size=(100, 40)):
            await wait_event(fake.normal_started)
            app._tick()
            await wait_event(fake.store_started)
            app._tick()
            app._tick()
            assert fake.fetch_sets == [None, set()]
            fake.store_release.set()
            fake.normal_release.set()
            await wait_event(fake.store_done)
            await wait_event(fake.normal_done)

    async def test_store_only_mode_launches_only_store_lane(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            fake.fetch_sets.clear()
            app.set_store_only(True)
            await settle(pilot)
            assert fake.fetch_sets == [set()]

    async def test_auto_mode_refreshes_plans_outside_the_snapshot(self, tmp_path):
        fake = self._fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert fake.plan_refreshes == 0
            app.set_store_only(True)
            await settle(pilot)
            assert fake.plan_refreshes >= 1

    async def test_watch_title_shows_snapshot_age_and_long_refresh(self, tmp_path):
        app = make_app(self._fake(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("w")
            await pilot.pause()
            from textual.widgets import Static

            title = app.screen.query_one("#list-title", Static)
            assert "snapshot" not in title.render().plain
            app.fleet = dataclasses.replace(
                app.fleet, taken_at=time.time() - app.SNAPSHOT_AGE_NOTE_S - 1.0
            )
            app._update_refresh_status()
            await pilot.pause()
            assert "snapshot 1m ago" in title.render().plain
            app._normal_refreshing = True
            app._normal_started_at = time.time() - app.POLL_INTERVAL_S - 1.0
            app._update_refresh_status()
            await pilot.pause()
            assert "refreshing" in title.render().plain


@pytest.mark.asyncio
class TestKeys:
    """Same key, same action, on every screen; every screen says how to go back."""

    async def test_escape_goes_back_and_q_quits_everywhere(self, tmp_path, fake_engine):
        from ai_quota_swap.tui.autoview import AutoScreen
        from ai_quota_swap.tui.dashboard import DashboardScreen, SwitchScreen, WatchScreen
        from ai_quota_swap.tui.usageview import UsageScreen

        for screen in (SwitchScreen, WatchScreen, AutoScreen, UsageScreen):
            keys = {}
            for binding in screen.BINDINGS:
                for key in binding.key.split(","):
                    keys[key] = (binding.action, binding.description, binding.show)
            assert keys["escape"][0] == "back" and keys["escape"][2], screen
            assert keys["q"][0] == "app.quit", screen
        dash = {b.key: b.action for b in DashboardScreen.BINDINGS}
        assert dash["q"] == "app.quit"
        # A key means one thing: these never mean "back" or anything else.
        for screen in (SwitchScreen, WatchScreen, AutoScreen, UsageScreen):
            for binding in screen.BINDINGS:
                for key in binding.key.split(","):
                    if key in ("w", "g", "u"):
                        pytest.fail(f"{screen.__name__} binds {key}")

    async def test_dashboard_shows_back_only_in_a_submenu(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            shown = {k for k, b in app.screen.active_bindings.items() if b.binding.show}
            assert "escape" not in shown and {"s", "w", "g", "u", "q"} <= shown
            await menu_select(pilot, "add-menu")
            shown = {k for k, b in app.screen.active_bindings.items() if b.binding.show}
            assert "escape" in shown


def fake_calls(app) -> list[tuple]:
    return app.switcher.calls


class _FakeEngine:
    """Stands in for AutoSwitchEngine: records construction, blocks until stop."""

    instances: list["_FakeEngine"] = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False, **kwargs):
        self.switcher = switcher
        self.settings = settings
        self.on_event = on_event
        self.dry_run = dry_run
        self.stopped = False
        self.applied_thresholds: list[float] = []
        self.wakes = 0
        self._stop = threading.Event()
        _FakeEngine.instances.append(self)

    def run_loop(self) -> int:
        self.on_event(NoSwitchEvent(reason="cooldown"))
        self._stop.wait(30)
        return 0

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def apply_threshold(self, threshold: float) -> None:
        self.settings = dataclasses.replace(self.settings, threshold=threshold)
        self.applied_thresholds.append(threshold)

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture
def fake_engine(monkeypatch):
    _FakeEngine.instances = []
    monkeypatch.setattr(
        "ai_quota_swap.tui.autoview.AutoSwitchEngine", _FakeEngine
    )
    return _FakeEngine


def _candidates(app, provider="claude") -> str:
    from textual.widgets import Static

    return app.screen.query_one(f"#candidates-{provider}", Static).render().plain


def _summary(app, provider="claude") -> str:
    from textual.widgets import Static

    return app.screen.query_one(f"#summary-{provider}", Static).render().plain


@pytest.mark.asyncio
class TestAutoScreen:
    async def _open(self, pilot):
        await settle(pilot)
        await pilot.press("g")
        await pilot.pause()

    async def test_opens_in_dry_run_and_store_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from textual.widgets import RichLog, Static

            from ai_quota_swap.tui.autoview import AutoScreen

            assert isinstance(app.screen, AutoScreen)
            assert len(fake_engine.instances) == 1
            assert fake_engine.instances[0].dry_run is True
            assert app._store_only is True
            assert not app.screen.query_one("#auto-tabs", Static).display  # one provider
            await settle(pilot)
            log = app.screen.query_one("#event-log", RichLog)
            assert log.lines
            assert not any(line.text.startswith("Claude") for line in log.lines)  # no tags

    async def test_one_engine_per_provider_switched_with_tab(self, tmp_path, fake_engine):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import RichLog, Static

            assert [e.switcher for e in fake_engine.instances] == [claude, codex]
            assert all(e.dry_run for e in fake_engine.instances)
            assert fake_engine.instances[1].settings.threshold == 99.0  # codex.threshold
            screen = app.screen
            assert screen.query_one("#auto-tabs", Static).display
            panels = {p.provider: p for p in screen.query("EnginePanel")}
            assert panels["claude"].display and not panels["codex"].display
            log = screen.query_one("#event-log", RichLog)
            texts = [line.text for line in log.lines]
            assert any(t.startswith("Claude ") for t in texts)
            assert any(t.startswith("Codex  ") for t in texts)
            await pilot.press("tab")
            await pilot.pause()
            assert panels["codex"].display and not panels["claude"].display
            # l goes live for the selected provider only.
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            live = [e for e in fake_engine.instances if not e.dry_run]
            assert [e.switcher for e in live] == [codex]
            assert fake_engine.instances[0].stopped is False

    async def test_wide_terminal_shows_both_panels(self, tmp_path, fake_engine):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(150, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            panels = list(app.screen.query("EnginePanel"))
            assert all(p.display for p in panels)
            assert not app.screen.query_one("#auto-tabs", Static).display

    async def test_go_live_requires_confirmation(self, tmp_path, fake_engine):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("l")
            await pilot.pause()
            from ai_quota_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped is True
            assert fake_engine.instances[1].dry_run is False

    async def test_back_stops_engines_and_restores_fetching(self, tmp_path, fake_engine):
        claude, codex = fleet_fakes(tmp_path)
        app = make_app(claude, codex)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await pilot.press("escape")
            await settle(pilot)
            from ai_quota_swap.tui.dashboard import DashboardScreen

            assert isinstance(app.screen, DashboardScreen)
            assert all(e.stopped for e in fake_engine.instances)
            assert app._store_only is False

    async def test_threshold_adjust_is_session_only(self, tmp_path, fake_engine):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            state = screen._states["claude"]
            await pilot.press("right")  # inert outside adjust mode
            await pilot.pause()
            assert state.settings.threshold == 90.0
            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            assert state.settings.threshold == 93.0
            assert app.session_threshold("claude") == 93.0  # the bars tick there
            engine = fake_engine.instances[0]
            assert engine.applied_thresholds == [91.0, 92.0, 93.0]
            assert "switch at 93% (this session)" in _summary(app)
            await pilot.press("enter")
            await pilot.pause()
            assert engine.wakes == 1
            assert not (tmp_path / "settings.json").exists()
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert fake_engine.instances[1].settings.threshold == 93.0
            await pilot.press("escape")
            await settle(pilot)
            assert app.session_threshold("claude") is None
            assert fake._poll_inputs_override is None

    async def test_threshold_adjust_escape_exits_mode_not_screen(self, tmp_path, fake_engine):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            from ai_quota_swap.tui.autoview import AutoScreen
            from ai_quota_swap.tui.dashboard import DashboardScreen

            await pilot.press("t")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, AutoScreen)
            assert fake_engine.instances[0].wakes == 0
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)

    async def test_threshold_clamps_and_keeps_meaningful_decimals(self, tmp_path, fake_engine):
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1, "autoswitch": {"threshold": 99.0},
        }))
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            screen = app.screen
            await pilot.press("t", "right", "right")
            await pilot.pause()
            assert screen._states["claude"].settings.threshold == 99.9
            assert "switch at 99.9% (this session)" in _summary(app)
            screen.action_threshold_step(-60.0)
            await pilot.pause()
            assert screen._states["claude"].settings.threshold == 50.0

    async def test_next_best_is_what_the_engine_would_consider(self, tmp_path, fake_engine, monkeypatch):
        from ai_quota_swap.tui import autoview

        calls: list = []
        real = autoview.rotation_candidates

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(autoview, "rotation_candidates", spy)
        over = UsageEntry(sentinel=USAGE_API_KEY, api_spend={"spend": {"used": 25.0, "limit": 20.0, "pct": 125.0}})
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(80.0, 10.0)),
                make_account(3, entry=make_entry(15.0, 5.0)),
                make_account(4, disabled=True, entry=make_entry(0.0, 0.0)),
                make_account(5, kind="api_key", entry=make_entry(sentinel=USAGE_API_KEY)),
                make_account(6, entry=make_entry(5.0, 5.0)),  # set aside
                make_account(7, entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED)),
                make_account(8, kind="api_key", entry=over),
            ],
            tmp_path,
        )
        fake.set_aside = {"6": "invalid_grant"}
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            plain = _candidates(app)
            assert plain.index("user3@example.com") < plain.index("user2@example.com")
            for left_out in ("user4@", "user5@", "user6@", "user7@", "user8@"):
                assert left_out not in plain
            assert calls  # the engine's rule, not a copy of it
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1, "autoswitch": {"includeApiKeyAccounts": True},
        }))
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            plain = _candidates(app)
            rows = plain.splitlines()
            assert rows[-1].startswith("  #5 user5@example.com") and "last resort" in rows[-1]
            assert "user8@" not in plain  # at its budget

    async def test_candidates_rank_by_policy_headroom_under_a_weekly_line(self, tmp_path, fake_engine):
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 85, "weeklyThreshold": 97},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(5.0, 93.0)),
                make_account(3, entry=make_entry(95.0, 47.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            plain = _candidates(app)
            assert plain.index("user2@example.com") < plain.index("user3@example.com")
            assert "7d  93% used (line 97%)" in plain
            assert "5h  95% used (line 85%)" in plain
            summary = _summary(app)
            assert "switch at 85%" in summary and "weekly 97%" in summary

    async def test_candidates_follow_a_session_threshold_adjust(self, tmp_path, fake_engine):
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 85, "weeklyThreshold": 97},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(83.0, 10.0)),
                make_account(3, entry=make_entry(5.0, 93.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            plain = _candidates(app)
            assert plain.index("user3@example.com") < plain.index("user2@example.com")
            assert "5h  83% used (line 85%)" in plain
            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            plain = _candidates(app)
            assert plain.index("user2@example.com") < plain.index("user3@example.com")
            assert "5h  83% used (line 88%)" in plain
            assert "7d  93% used (line 97%)" in plain

    async def test_candidates_show_no_line_without_a_weekly_one(self, tmp_path, fake_engine):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(42.0, 10.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            plain = _candidates(app)
            assert "5h  42% used" in plain and "(line" not in plain
            assert "weekly" not in _summary(app)

    async def test_candidates_ranking_honors_configured_model(self, tmp_path, fake_engine):
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1, "autoswitch": {"model": "Fable"},
        }))
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(10.0, 5.0, scoped=[("Fable", 95.0)])),
                make_account(3, entry=make_entry(50.0, 5.0, scoped=[("Fable", 20.0)])),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            plain = _candidates(app)
            assert plain.index("user3@example.com") < plain.index("user2@example.com")

    async def test_an_accounts_own_line_orders_the_list(self, tmp_path, fake_engine):
        """Account 2 at 5h 50% under its own 60% line scores 25 points;
        account 3 at 5h 55% under the global 90% line scores 45."""
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                make_account(2, entry=make_entry(50.0, 10.0), policy=AccountPolicy(threshold=60.0)),
                make_account(3, entry=make_entry(55.0, 10.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            plain = _candidates(app)
            assert plain.index("user3@example.com") < plain.index("user2@example.com")
            row2 = next(ln for ln in plain.splitlines() if "user2@" in ln)
            assert "5h  50% used (line 60%)" in row2


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from ai_quota_swap.tui.autoview import event_text

        assert event.human() in event_text(event).plain

    def test_event_text_uses_light_accent_for_switch(self):
        from ai_quota_swap.tui.autoview import event_text
        from ai_quota_swap.tui.theme import ACCENT_LIGHT, QSWAP_LIGHT, Palette

        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        text = event_text(event, palette=Palette.from_theme(QSWAP_LIGHT))
        assert any(ACCENT_LIGHT in str(s.style) for s in text.spans)


@pytest.mark.asyncio
class TestUsageScreen:
    def _app(self, tmp_path, reads, result=None):
        from ai_quota_swap.tui.app import QswapApp

        class Ledgered(QswapApp):
            def read_ledger(self, *, days=7, by="day", scan):
                reads.append((days, by, scan))
                return result

        return Ledgered(FakeSwitcher([make_account(1, active=True)], tmp_path), ledger=False)

    async def test_u_opens_the_last_seven_days_by_day_then_reads_new_files(self, tmp_path):
        reads: list = []
        app = self._app(tmp_path, reads)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            from ai_quota_swap.tui.usageview import UsageScreen

            assert isinstance(app.screen, UsageScreen)
            assert reads[:2] == [(7, "day", False), (7, "day", True)]

    async def test_tab_regroups_and_d_switches_the_span(self, tmp_path):
        reads: list = []
        app = self._app(tmp_path, reads)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            for expected in ("model", "account", "project", "provider", "day"):
                await pilot.press("tab")
                await settle(pilot)
                assert reads[-1][1] == expected
            await pilot.press("d")
            await settle(pilot)
            assert reads[-2:] == [(30, "day", False), (30, "day", True)]
            await pilot.press("d")
            await settle(pilot)
            assert reads[-1] == (7, "day", False)  # 7 days were read already: no second scan

    async def test_report_text_is_the_command_lines(self, tmp_path):
        from ai_quota_swap.ledger.pricing import PriceTable
        from ai_quota_swap.ledger.report import Report, Row, Totals
        from ai_quota_swap.ledger.usage import UsageResult

        today = datetime.now().date()
        report = Report(
            by="day", since=today, until=today, provider=None,
            rows=[Row(key=today, label=today.strftime("%a %Y-%m-%d"),
                      totals=Totals(input=1000, output=2000, requests=1, cost=1.25))],
            total=Totals(input=1000, output=2000, requests=1, cost=1.25),
            unpriced_models={}, unattributed_tokens=0, profile_sessions=0,
            prices=PriceTable(), attribution_starts={},
        )
        reads: list = []
        app = self._app(tmp_path, reads, UsageResult(report=report, scan=None))
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            from textual.widgets import Static

            body = app.screen.query_one("#usage-report", Static).render().plain
            assert "Total" in body and "$1.25" in body
            assert "Costs are estimates at API list prices" in body
            title = app.screen.query_one("#usage-title", Static).render().plain
            assert title == "Usage report · last 7 days · by day"

    async def test_scanning_says_so(self, tmp_path):
        from ai_quota_swap.tui.usageview import UsageScreen

        reads: list = []
        app = self._app(tmp_path, reads)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            screen = UsageScreen()
            screen._load = lambda: None  # type: ignore[method-assign]
            await app.push_screen(screen)
            screen.scanning = True
            screen._draw()
            await pilot.pause()
            from textual.widgets import Static

            assert "reading new session files" in screen.query_one("#usage-title", Static).render().plain


# ---------------------------------------------------------------------------
# accounts_snapshot on the real switcher
# ---------------------------------------------------------------------------


class TestAccountsSnapshot:
    def test_one_pass_snapshot(self, temp_home, mock_claude_config):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["sequence"] = [1, 2]
        data["accounts"] = {
            "1": {"email": "test@example.com", "uuid": "test-uuid-1234"},
            "2": {"email": "other@example.com", "uuid": "uuid-2"},
        }
        switcher._write_json(switcher.sequence_file, data)

        snap = switcher.accounts_snapshot(fetch=set())  # store-only: no network
        assert snap.active_number == "1"
        assert [acc.number for acc in snap.accounts] == ["1", "2"]
        active = snap.accounts[0]
        assert active.is_active and active.email == "test@example.com"
        assert all(acc.kind == "oauth" for acc in snap.accounts)
        # No stored credential backups: nothing is switchable, and usage is
        # sentinel'd rather than fetched.
        assert all(not acc.switchable for acc in snap.accounts)
        assert all(acc.usage.sentinel is not None for acc in snap.accounts)
        assert isinstance(snap.taken_at, float)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestBareInvocation:
    def test_bare_tty_launches_tui(self, monkeypatch, temp_home):
        import ai_quota_swap.cli as cli
        import ai_quota_swap.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["qswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched

    def test_bare_non_tty_keeps_usage_error(self, monkeypatch, temp_home, capsys):
        import ai_quota_swap.cli as cli
        import ai_quota_swap.tui as tui

        monkeypatch.setattr(sys, "argv", ["qswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(tui, "run", lambda *a, **k: pytest.fail("no dashboard off a terminal"))
        cli.main()  # a three-line hint, exit 0; never the dashboard
        out = capsys.readouterr().out
        assert "qswap help" in out and "qswap list" in out

    def test_qswap_watch_opens_tui_on_watch_page(self, monkeypatch, temp_home):
        import ai_quota_swap.cli as cli
        import ai_quota_swap.tui as tui

        launched = {}

        def fake_run(switcher, start="dashboard"):
            launched["start"] = start
            return 0

        monkeypatch.setattr(sys, "argv", ["qswap", "watch"])
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert launched["start"] == "watch"


# ---------------------------------------------------------------------------
# Theme wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestThemeWiring:
    async def test_mount_selects_light_theme_from_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "qswap-light"

    async def test_auto_setting_uses_detected_light(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from ai_quota_swap.tui.app import QswapApp
        app = QswapApp(fake, ledger=False, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "qswap-light"

    async def test_auto_setting_no_detection_falls_back_to_dark(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from ai_quota_swap.tui.app import QswapApp
        app = QswapApp(fake, ledger=False, detected=None)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "qswap-dark"

    async def test_toggle_cycles_dark_light_auto(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from ai_quota_swap.tui.app import QswapApp
        app = QswapApp(fake, ledger=False, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "qswap-dark"          # setting dark
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "qswap-light"          # → light
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "qswap-light"          # → auto, detected=light
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "auto"
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "qswap-dark"           # → back to dark

    async def test_theme_menu_marks_current_and_applies(self, tmp_path):
        from textual.widgets import ListView, Static

        from ai_quota_swap.tui.widgets import MenuItem

        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert app._theme_name == "auto"  # default
            await menu_select(pilot, "theme-menu")
            menu = app.screen.query_one("#menu", ListView)
            labels = [it.query_one(Static).render().plain for it in menu.query(MenuItem)]
            assert any("dark" in lbl for lbl in labels)
            assert any("light" in lbl for lbl in labels)
            current = next(lbl for lbl in labels if "auto" in lbl)
            assert "●" in current  # the current theme is marked
            await menu_select(pilot, "theme:light")
            assert app._theme_name == "light"
            assert app.theme == "qswap-light"
