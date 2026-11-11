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
from tests.test_engine_money import _key  # noqa: E402
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
