"""The auto-switch engine, round 4 of PR #13: what the verification of round
3 found (the orchestrator's notes, pr13-round3-decisions.md and the round 4
findings).

Everything runs on the suite's fake home and fake Keychain; resets are faked
and never claimed. The fleet is #1 a@example.com (the live login), #2 ci and
#4 (API keys with a budget), #3 c@example.com, as in test_engine_money.py.
"""

from __future__ import annotations

import json

import pytest

from ai_quota_swap import api_spend, claude_settings
from ai_quota_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import _usage
from tests.test_engine_money import _fleet, _reading, _settings_line, _switches, _tick

QSWAP = "/opt/qswap/bin/qswap"


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)


@pytest.fixture(autouse=True)
def scans(monkeypatch):
    """The engine's ledger scan, counted by its keyword arguments."""
    calls: list = []
    from ai_quota_swap.ledger import usage as ledger_usage

    monkeypatch.setattr(ledger_usage, "scan_recent", lambda *a, **k: calls.append(k),
                        raising=False)
    return calls


def _state(h) -> dict:
    path = h.switcher.backup_dir / "autoswitch_state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _to(event) -> str | None:
    number = (event.to_ref or {}).get("number")
    return None if number is None else str(number)


ALL_OUT = {"1": _usage(100), "2": "api key", "3": _usage(100), "4": "api key"}


# == One key's brake is not the way to another key ==========================================


class TestNoOtherKeyAfterABrake:
    """The high finding of the round 4 timelines: at one key's limit the
    engine went back to the account underneath, still at 100%, and entered
    another key on the next check. A10 says never key to key, in every
    branch; the side that stops spending is to wait for a subscription."""

    @pytest.mark.parametrize("brake", ["budget", "no-limit", "spend-unknown"])
    def test_left_at_its_limit_then_no_other_key(self, temp_home, brake):
        h = _fleet(temp_home, keys=(2, 4))
        assert _tick(h, ALL_OUT) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        if brake == "no-limit":
            api_spend.set_budget(h.switcher, "2", None)
            two = _reading(h, limit=None)
        elif brake == "budget":
            two = _reading(h, used=600.0)
        else:
            two = _reading(h, used=10.0, age=3600.0)
        h.clock.advance(60)
        assert _tick(h, ALL_OUT, {"2": two}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None
        assert _state(h)["keyLeft"]["account"] == "2"
        before = len(h.events)
        for _ in range(10):
            h.clock.advance(60)
            _tick(h, ALL_OUT, {"2": two, "4": _reading(h, used=1.0)})
            assert h.switcher.current_account_number() == "1"
        assert not [s for s in _switches(h)[1:] if _to(s) == "4"]
        held = [e for e in h.events[before:] if isinstance(e, NoSwitchEvent)
                and e.reason == "key-left"]
        # The hold is said, naming the key left and why, and the other key
        # the user may move to by hand.
        assert held, [e.reason for e in h.events[before:] if isinstance(e, NoSwitchEvent)]
        text = held[0].human()
        assert text.startswith("Not switching to another API key: auto-switch left #2 ci")
        assert "until a subscription has room" in text
        assert text.endswith("To use #4 key4 now: qswap switch 4")
        # The rows under it say why #4 is not used (ux.md: every view).
        from ai_quota_swap.autolog import account_facts

        rows = {v.number: v for v in held[0].context.accounts}
        assert account_facts(rows["4"])[0] == "API key, not used until a subscription has room"

    def test_the_key_it_left_comes_back_when_its_figure_is_fresh_again(self, temp_home):
        h = _fleet(temp_home, keys=(2, 4))
        _tick(h, ALL_OUT)
        h.clock.advance(60)
        _tick(h, ALL_OUT, {"2": _reading(h, used=10.0, age=3600.0)})
        assert h.switcher.current_account_number() == "1"
        h.clock.advance(60)
        assert _tick(h, ALL_OUT, {"2": _reading(h), "4": _reading(h, used=1.0)}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "2"

    def test_a_subscription_with_room_ends_it(self, temp_home):
        h = _fleet(temp_home, keys=(2, 4))
        _tick(h, ALL_OUT)
        h.clock.advance(60)
        over = _reading(h, used=600.0)
        _tick(h, ALL_OUT, {"2": over})
        assert "keyLeft" in _state(h)
        h.clock.advance(60)
        _tick(h, {**ALL_OUT, "1": _usage(40)}, {"2": over})
        assert "keyLeft" not in _state(h)
        h.clock.advance(3600)
        assert _tick(h, ALL_OUT, {"2": over, "4": _reading(h, used=1.0)}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "4"

    def test_a_dry_run_says_the_same_and_writes_nothing(self, temp_home):
        h = _fleet(temp_home, keys=(2, 4))
        _tick(h, ALL_OUT)
        h.clock.advance(60)
        over = _reading(h, used=600.0)
        _tick(h, ALL_OUT, {"2": over})
        state = _state(h)
        h.engine = h._make_engine(dry_run=True)
        before = len(h.events)
        h.clock.advance(60)
        _tick(h, ALL_OUT, {"2": over, "4": _reading(h, used=1.0)})
        assert not [e for e in h.events[before:] if isinstance(e, SwitchEvent)]
        assert [e.reason for e in h.events[before:] if isinstance(e, NoSwitchEvent)
                and e.reason == "key-left"]
        assert _state(h) == state


# == EM11: a running engine follows autoswitch.includeApiKeyAccounts =========================


EXHAUSTED = {"1": _usage(100), "2": "api key", "3": _usage(100)}


def _engine_from_the_file(h, *, flag: bool | None = None):
    """An engine built as `qswap auto` builds it: the settings file, then
    the command line's flags (``--include-api-key-accounts`` when ``flag``)."""
    from argparse import Namespace

    from ai_quota_swap.settings import load_engine_settings, merged_with_cli, set_setting

    root = h.switcher.backup_dir
    set_setting(root, "autoswitch.includeApiKeyAccounts", "true")
    settings = load_engine_settings(root)
    if flag is not None:
        settings = merged_with_cli(settings, Namespace(include_api_key_accounts=flag))
    h.settings = settings
    h.engine = h._make_engine()


class TestKeysTurnedOffWhileTheEngineRuns:
    """EM11 worked only for an engine built after the setting changed: a
    running `qswap auto` loop and the menu bar's engine read it once."""

    def test_the_running_engine_leaves_its_key(self, temp_home):
        from ai_quota_swap.settings import set_setting

        h = _fleet(temp_home)
        _engine_from_the_file(h)
        assert _tick(h, EXHAUSTED) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        set_setting(h.switcher.backup_dir, "autoswitch.includeApiKeyAccounts", "false")
        h.clock.advance(60)
        assert _tick(h, EXHAUSTED) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert _switches(h)[-1].limit_reason == "keys-off"
        for _ in range(5):
            h.clock.advance(60)
            _tick(h, EXHAUSTED)
        assert h.switcher.current_account_number() == "1"
        # Turned on again, the same engine uses keys again.
        set_setting(h.switcher.backup_dir, "autoswitch.includeApiKeyAccounts", "true")
        h.clock.advance(60)
        assert _tick(h, EXHAUSTED) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"

    def test_an_engine_started_with_the_flag_keeps_it(self, temp_home):
        from ai_quota_swap.settings import set_setting

        h = _fleet(temp_home)
        _engine_from_the_file(h, flag=True)
        set_setting(h.switcher.backup_dir, "autoswitch.includeApiKeyAccounts", "false")
        assert _tick(h, EXHAUSTED) is TickOutcome.SWITCHED
        h.clock.advance(60)
        _tick(h, EXHAUSTED)
        assert h.switcher.current_account_number() == "2"

    def test_the_menu_bars_engine_follows_it_too(self, temp_home):
        from ai_quota_swap.settings import load_engine_settings, set_setting

        h = _fleet(temp_home)
        root = h.switcher.backup_dir
        set_setting(root, "autoswitch.includeApiKeyAccounts", "false")
        h.settings = load_engine_settings(root, "claude")
        h.engine = h._make_engine()
        _tick(h, EXHAUSTED)
        assert h.switcher.current_account_number() == "1"
        set_setting(root, "autoswitch.includeApiKeyAccounts", "true")
        h.clock.advance(60)
        assert _tick(h, EXHAUSTED) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"


# == EM9: the wait for an unreadable subscription ends ======================================


UNREADABLE_3 = {"1": _usage(100), "2": "api key", "3": None}


class TestTheUnreadableHoldEnds:
    """A gap longer than ONCE_WATCH_S between checks that want the key
    started a new episode each time, so with checks over 15 minutes apart
    the key was never entered; and a check that could not read the active
    account ended the episode."""

    @pytest.mark.parametrize("gap", [901, 1200, 1800, 3600])
    def test_checks_far_apart_go_ahead_on_the_second(self, temp_home, gap):
        h = _fleet(temp_home, cooldown_seconds=300)
        h.engine = h._make_engine()  # qswap auto --once from cron
        assert _tick(h, UNREADABLE_3) is TickOutcome.BLOCKED
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)][-1] == (
            "usage-unreadable-hold"
        )
        h.clock.advance(gap)
        h.engine = h._make_engine()
        assert _tick(h, UNREADABLE_3) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"

    def test_one_loop_engine_with_an_hour_between_checks(self, temp_home):
        h = _fleet(temp_home, cooldown_seconds=300, interval_seconds=3600)
        assert _tick(h, UNREADABLE_3) is TickOutcome.BLOCKED
        h.clock.advance(3600)
        assert _tick(h, UNREADABLE_3) is TickOutcome.SWITCHED

    def test_a_check_that_cannot_read_the_active_account_does_not_restart_it(self, temp_home):
        h = _fleet(temp_home, cooldown_seconds=300)
        unknown = {"1": None, "2": "api key", "3": None}
        entered_at = None
        for i in range(12):
            _tick(h, unknown if i % 3 == 2 else UNREADABLE_3)
            if h.switcher.current_account_number() == "2":
                entered_at = i
                break
            h.clock.advance(60)
        assert entered_at is not None and entered_at <= 6, entered_at


# == EM4: a reset auto mode would spend, deferred for want of a reading =====================


from unittest.mock import patch  # noqa: E402

from ai_quota_swap import resets  # noqa: E402
from ai_quota_swap.settings import ResetSettings  # noqa: E402
from tests.test_autoswitch import _entry_for  # noqa: E402
from tests.test_engine_money import _key, _tick_resets  # noqa: E402
from tests.test_resets import _grant, _usage as _rusage  # noqa: E402


def _week(**kw):
    return _grant("g-week", clears=("seven_day",), **kw)


def _tick_unread_claim(h, usage):
    """A tick on stored readings 5 minutes old where the re-read before a
    claim gives nothing (the usage endpoint does not answer for any account)."""
    claims: list = []

    def entries(fetch=None, scheduled=False):
        return {n: (_key(_reading(h)) if v == "api key"
                    else _entry_for(v, h.clock.now - 300))
                for n, v in usage.items()}

    with (
        patch.object(h.switcher, "usage_entries_by_account", side_effect=entries),
        patch.object(h.switcher, "refresh_usage", side_effect=lambda nums: {}),
        patch.object(h.switcher, "claim_usage_reset",
                     side_effect=lambda *a: claims.append(a)),
    ):
        return h.engine.tick(), claims


class TestADeferredResetHoldsTheKey:
    """The re-check's case: the planner wants #1's weekly reset (5 days to
    go) and defers it because #1 could not be read again. The engine must
    not enter the paid key in the same check: it holds, bounded by one
    cooldown like the unreadable hold, and says why."""

    USAGE = {"1": _rusage(d7=100, grants=[_week()]), "2": "api key", "3": _rusage(d7=100)}

    def test_held_for_one_cooldown_then_the_key(self, temp_home):
        h = _fleet(temp_home, cooldown_seconds=300)
        for _ in range(5):  # 0, 60, ..., 240 s
            outcome, claims = _tick_unread_claim(h, self.USAGE)
            assert outcome is not TickOutcome.SWITCHED and claims == []
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)
                and e.reason == "reset-pending"]
        assert held and held[0].account == "1"
        assert held[0].human() == (
            "Not switching to an API key yet: auto mode would spend a usage reset on "
            "#1 a@example.com, whose usage could not be read again this check; waiting "
            "up to 5m for it"
        )
        outcome, _ = _tick_unread_claim(h, self.USAGE)  # 300 s: one cooldown
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"


class TestKeptResetWhileAClaimSettles:
    def test_the_note_says_auto_mode_waits_for_the_claim(self):
        """#1 (active) at 100% with its last claim still settling; #3 holds
        a weekly reset auto mode spends once the claim has settled. The note
        must not say it would not make #3 an account to switch to."""
        usage = {"1": _rusage(d7=100), "3": _rusage(d7=100, grants=[_week()])}
        views = [
            resets.AccountView(num="1", usage=usage["1"], threshold=90.0,
                               can_claim=False, settling=True),
            resets.AccountView(num="3", usage=usage["3"], threshold=90.0),
        ]
        (kept,) = resets.kept_resets(views, current="1", settings=ResetSettings(),
                                     now=1_000_000.0)
        assert kept.account == "3"
        assert (kept.reason, kept.words) == (
            "settling", "auto mode waits for the active account's last reset claim to settle"
        )


# == resets.auto active: the account's own reset before its switch line =======================


def _active_mode(h, *, threshold: str = "100") -> None:
    from ai_quota_swap.settings import set_setting

    set_setting(h.switcher.backup_dir, "resets.auto", "active")
    set_setting(h.switcher.backup_dir, "resets.threshold", threshold)


class TestActiveModeSpendsRatherThanSwitchingAway:
    """resets.md: `active` spends the active account's own reset "rather
    than switching away", paired with resets.threshold 100. With a switch
    line of 98 the engine left at 98 and the reset was not spent until the
    rotation came back to #1 at its limit, hours and switches later."""

    def _usage(self, d7: float, *, grants=True, h5: float = 10.0) -> dict:
        return {"1": _rusage(h5=h5, d7=d7, reset_in=3 * 86400,
                             grants=[_week()] if grants else None),
                "2": "api key", "3": _rusage(h5=10, d7=30)}

    def test_it_stays_at_the_line_and_spends_the_reset_at_100(self, temp_home):
        h = _fleet(temp_home, threshold=98.0)
        _active_mode(h)
        for d7 in (98.0, 99.0, 99.5):
            outcome, claims = _tick_resets(h, self._usage(d7))
            assert outcome is TickOutcome.NO_ACTION and claims == []
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert not _switches(h)
        held = [e for e in h.events if isinstance(e, NoSwitchEvent) and e.reason == "reset-ahead"]
        assert held and held[0].human() == (
            "No switch yet: auto mode spends #1 a@example.com's own weekly reset when 7d "
            "reaches 100%, rather than switching away (resets.auto active)"
        )
        from ai_quota_swap import autolog

        lines: list[str] = []
        log = autolog.AutoLog(lines.append, clock=lambda: "12:00:00", width=80)
        for event in h.events:
            log(event)
        log.flush()
        text = [line for chunk in lines for line in chunk.splitlines()]
        assert "its own weekly reset comes first, at 7d 100%" in " ".join(
            " ".join(text).split()
        ), text
        assert all(len(line) <= 80 for line in text), text
        after = self._usage(0.0)
        outcome, claims = _tick_resets(h, self._usage(100.0), after=after)
        assert claims == ["1"] and h.switcher.current_account_number() == "1"
        assert not _switches(h)

    def test_a_window_its_reset_does_not_clear_still_switches(self, temp_home):
        h = _fleet(temp_home, threshold=98.0)
        _active_mode(h)
        outcome, _ = _tick_resets(h, self._usage(50.0, h5=98.0))
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"

    def test_another_mode_still_switches_at_the_line(self, temp_home):
        from ai_quota_swap.settings import set_setting

        h = _fleet(temp_home, threshold=98.0)
        set_setting(h.switcher.backup_dir, "resets.threshold", "100")
        outcome, _ = _tick_resets(h, self._usage(98.0))
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"

    def test_a_reset_its_settings_keep_does_not_hold_it(self, temp_home):
        """Its 7d window refills in 10 hours (under resets.minHoursLeft):
        auto mode would not spend it, so the line stands."""
        h = _fleet(temp_home, threshold=98.0)
        _active_mode(h)
        usage = self._usage(98.0)
        usage["1"] = _rusage(h5=10, d7=98.0, reset_in=10 * 3600, grants=[_week()])
        outcome, _ = _tick_resets(h, usage)
        assert outcome is TickOutcome.SWITCHED


# == EM2: a key whose link to an organization is not known ===================================


from tests.admin_fakes import (  # noqa: E402
    ADMIN_KEY,
    FakeAnthropic,
    error_response,
    json_response,
    key_item,
    keys_page,
)
from tests.test_engine_round3 import ORG_NOW, _ledger_spend, _link  # noqa: E402


@pytest.fixture
def admin(monkeypatch):
    """Anthropic's Admin API, faked: it lists ``listed`` (None: it answers
    500 to every request), and the usage report always fails."""
    from ai_quota_swap import admin_api

    admin_api._memory.clear()
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    state = {"listed": None}

    def listing(query):
        if state["listed"] is None:
            return error_response(500, "api_error")
        return json_response(keys_page(state["listed"]))

    fake = FakeAnthropic({
        "/v1/organizations/api_keys": listing,
        "/v1/organizations/usage_report/messages": lambda q: error_response(500, "api_error"),
    })
    monkeypatch.setattr(admin_api, "send", fake)
    fake.state = state
    return fake


def _key2_reading(h, env: dict) -> dict:
    infos = {str(i[0]): i for i in h.switcher._build_accounts_info() if str(i[0]) == "2"}
    return api_spend.readings(h.switcher, infos, fetch=None, now=ORG_NOW, env=env)["2"]


class TestALinkThatCannotBeDecided:
    """The re-check's EM2 gap: with an organization active (ANTHROPIC_ADMIN_KEY,
    or a registered one never listed) and the Admin API down, the key's link
    is not known, and this machine's ledger figure was held to the limit."""

    @pytest.mark.parametrize("how", ["env", "registered"])
    def test_this_machines_figure_does_not_hold_it(self, temp_home, admin, how):
        h = _fleet(temp_home)
        api_spend.set_budget(h.switcher, "2", 20)
        _ledger_spend(h, 4.0, scanned_at=ORG_NOW)
        env = {"ANTHROPIC_ADMIN_KEY": ADMIN_KEY} if how == "env" else {}
        if how == "registered":
            _link(h)
        reading = _key2_reading(h, env)
        assert reading["source"] == api_spend.SOURCE_LEDGER
        assert api_spend.machine_only(reading)
        assert not api_spend.figure_fresh(reading, ORG_NOW)
        assert api_spend.engine_limit_reason(reading, now=ORG_NOW, limited=True) == (
            api_spend.SPEND_UNKNOWN
        )
        text = api_spend.spend_text(reading, now=ORG_NOW)
        assert text.endswith("· this machine only · limit not enforced: no spend figure"), text
        h.clock.now = ORG_NOW
        assert _tick(h, EXHAUSTED, {"2": reading}) is TickOutcome.BLOCKED
        assert h.switcher.current_account_number() == "1"

    def test_a_listing_without_the_key_keeps_the_ledger_figure(self, temp_home, admin):
        h = _fleet(temp_home)
        api_spend.set_budget(h.switcher, "2", 20)
        _ledger_spend(h, 4.0, scanned_at=ORG_NOW)
        _link(h)
        admin.state["listed"] = [key_item("apikey_O", "other", "sk-ant-api03-" + "zz" * 20)]
        reading = _key2_reading(h, {})
        assert not api_spend.machine_only(reading)
        assert api_spend.figure_fresh(reading, ORG_NOW)


# == EM3: a scan that cannot cover the period, in words ======================================


class _Stats:
    def __init__(self, missing=0, paths=()):
        self.missing = missing
        self.missing_paths = list(paths)
        self.older_not_in_ledger = 0
        self.modified_since = None


def _failing_scan(monkeypatch, temp_home, how: str) -> str:
    """The engine's scan raises (``raises``) or finds two session files it
    cannot read (``unreadable``); returns the first one's path as shown."""
    from ai_quota_swap.ledger import usage as ledger_usage
    from ai_quota_swap.printer import abbreviate_path

    first = temp_home / ".claude" / "projects" / "demo" / "a.jsonl"
    second = temp_home / ".claude" / "projects" / "demo" / "b.jsonl"

    def scan(*a, **k):
        if how == "raises":
            raise OSError("the disk went away")
        return _Stats(missing=2, paths=[str(first), str(second)])

    monkeypatch.setattr(ledger_usage, "scan_recent", scan)
    return abbreviate_path(str(first))


class TestAScanThatCannotCoverThePeriod:
    """EM3 counts a scan that raised, or found session files it could not
    read, as no figure. The re-check: the words said "could not be measured
    for 20 minutes" seconds after the scan, named `qswap usage`, which does
    not cure it, the rows said the opposite, and the dry run would switch."""

    def test_the_hold_names_the_files_and_the_way_out(self, temp_home, monkeypatch):
        path = _failing_scan(monkeypatch, temp_home, "unreadable")
        h = _fleet(temp_home)
        assert _tick(h, EXHAUSTED, {"2": _reading(h, age=0)}) is TickOutcome.BLOCKED
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)
                and e.reason == "spend-unknown"]
        assert held[-1].human() == (
            "Not switching to #2 ci: its spend is not known (2 session files on this "
            f"machine could not be read); make them readable, or move them away (one is "
            f"{path})"
        )
        # The rows under the hold agree (A4): no "used as a last resort".
        from ai_quota_swap.autolog import account_facts

        rows = {v.number: v for v in held[-1].context.accounts}
        assert account_facts(rows["2"])[0] == "API key, limit not enforced: no spend figure"
        # Recorded for the other surfaces.
        scan = _state(h)["spendScan"]
        assert (scan["ok"], scan["unreadable"]) == (False, 2)

    def test_a_scan_that_raised_leaves_the_key_in_its_own_words(self, temp_home, monkeypatch):
        h = _fleet(temp_home, cooldown_seconds=3600)
        _tick(h, EXHAUSTED)
        assert h.switcher.current_account_number() == "2"
        _failing_scan(monkeypatch, temp_home, "raises")
        h.clock.advance(5 * 60)
        assert _tick(h, EXHAUSTED, {"2": _reading(h, age=0)}) is TickOutcome.SWITCHED
        assert _switches(h)[-1].human() == (
            "Switched #2 ci → #1 a@example.com · its spend is not known (the scan of this "
            "machine's session files failed); no subscription has room, so back to the "
            "account underneath"
        )
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)]
        h.clock.advance(60)
        _tick(h, EXHAUSTED, {"2": _reading(h, age=0)})
        texts = [e.human() for e in h.events[len(held):] if isinstance(e, NoSwitchEvent)]
        assert not any("20 minutes" in t or "qswap usage" in t for t in texts), texts

    def test_the_rows_say_the_limit_is_not_enforced(self, temp_home, monkeypatch):
        """list, status, the dashboard and the menu bar read the reading the
        collector builds: after the engine's failed scan it says so."""
        from ai_quota_swap.ledger.store import DB_FILENAME, LAST_SCAN_META, LedgerStore

        path = _failing_scan(monkeypatch, temp_home, "unreadable")
        h = _fleet(temp_home)
        _tick(h, EXHAUSTED, {"2": _reading(h, age=0)})
        store = LedgerStore(h.switcher.backup_dir / DB_FILENAME)
        store.set_meta(LAST_SCAN_META, str(h.clock.now))  # the ledger looks fresh
        store.close()
        infos = {str(i[0]): i for i in h.switcher._build_accounts_info() if str(i[0]) == "2"}
        reading = api_spend.readings(h.switcher, infos, fetch=set(), now=h.clock.now,
                                     env={})["2"]
        assert api_spend.limit_not_enforced(reading, h.clock.now)
        assert api_spend.spend_text(reading, now=h.clock.now).endswith(
            "· limit not enforced: no spend figure"
        )
        assert api_spend.detail_text(reading, h.clock.now) == (
            "2 session files on this machine could not be read; make them readable, or "
            f"move them away (one is {path})"
        )

    def test_the_dry_run_agrees_with_the_real_run(self, temp_home, monkeypatch):
        _failing_scan(monkeypatch, temp_home, "unreadable")
        h = _fleet(temp_home)
        _tick(h, EXHAUSTED, {"2": _reading(h, age=0)})
        h.engine = h._make_engine(dry_run=True)
        h.clock.advance(60)
        before = len(h.events)
        _tick(h, EXHAUSTED, {"2": _reading(h, age=0)})
        assert not [e for e in h.events[before:] if isinstance(e, SwitchEvent)]
        assert [e.reason for e in h.events[before:] if isinstance(e, NoSwitchEvent)] == [
            "spend-unknown"
        ]


# == EM6: a dry run that would move to a key ===================================================


class TestTheDryRunsMoveToAKey:
    @pytest.mark.parametrize("source", ["ledger", "admin-api"])
    def test_it_says_a_real_run_measures_first(self, temp_home, source):
        h = _fleet(temp_home)
        h.engine = h._make_engine(dry_run=True)
        assert _tick(h, EXHAUSTED, {"2": _reading(h, source=source)}) is TickOutcome.SWITCHED
        text = _switches(h)[-1].human()
        if source == "ledger":
            assert text == (
                "Would switch #1 a@example.com → #2 ci · 5h limit reached (dry run); a "
                "real run measures the spend first, and may not move"
            )
        else:
            assert text == "Would switch #1 a@example.com → #2 ci · 5h limit reached (dry run)"


# == DS3: a dry run beside a real engine ======================================================


class TestADryRunBesideARealEngine:
    """There is one heartbeat file per data directory: a dry run that
    ticked after the real loop made every surface say nothing watches the
    key, while the real loop was watching."""

    def _beat(self, h, *, mode, dry_run, at, host="qswap auto"):
        from ai_quota_swap.autoswitch import write_heartbeat

        write_heartbeat(h.switcher.backup_dir, mode=mode, dry_run=dry_run, last_tick_at=at,
                        next_tick_at=at + 60 if mode == "loop" else None, started_at=at,
                        host=host)

    @pytest.mark.parametrize("dry_mode", ["loop", "once"])
    def test_the_real_loop_still_watches(self, temp_home, dry_mode):
        from ai_quota_swap.autoswitch import key_watch, read_heartbeat

        h = _fleet(temp_home)
        now = 1_800_000_000.0
        self._beat(h, mode="loop", dry_run=False, at=now - 20, host="menu bar")
        self._beat(h, mode=dry_mode, dry_run=True, at=now, host="dashboard")
        watch = key_watch(read_heartbeat(h.switcher.backup_dir, now=now), now)
        assert watch and watch.mode == "loop" and watch.last_checked_at == now - 20

    def test_with_the_real_loop_gone_only_the_dry_run_is_running(self, temp_home):
        from ai_quota_swap.autoswitch import key_watch, read_heartbeat

        h = _fleet(temp_home)
        now = 1_800_000_000.0
        self._beat(h, mode="loop", dry_run=False, at=now - 3600)
        self._beat(h, mode="loop", dry_run=True, at=now)
        watch = key_watch(read_heartbeat(h.switcher.backup_dir, now=now), now)
        assert not watch and watch.dry_run_only
        assert watch.last_checked_at == now - 3600

    def test_a_real_beat_replaces_both(self, temp_home):
        from ai_quota_swap.autoswitch import read_heartbeat

        h = _fleet(temp_home)
        now = 1_800_000_000.0
        self._beat(h, mode="loop", dry_run=False, at=now - 20)
        self._beat(h, mode="loop", dry_run=True, at=now - 10)
        self._beat(h, mode="once", dry_run=False, at=now)
        beat = read_heartbeat(h.switcher.backup_dir, now=now)
        assert beat["mode"] == "once" and "real" not in beat

    def test_status_names_the_real_loop(self, temp_home):
        from ai_quota_swap.autoswitch import read_heartbeat

        h = _fleet(temp_home)
        now = 1_800_000_000.0
        self._beat(h, mode="loop", dry_run=False, at=now - 20, host="menu bar")
        self._beat(h, mode="loop", dry_run=True, at=now, host="dashboard")
        beat = read_heartbeat(h.switcher.backup_dir, now=now)
        assert (beat["host"], beat["dryRun"]) == ("menu bar", False)
