"""Two auto-switch engines on one data directory whose
``--include-api-key-accounts`` flags disagree (issue #15).

A flag pins ``autoswitch.includeApiKeyAccounts`` for its engine. Without a
rule the engine allowed keys enters one at the limit and the other leaves it
(``keys-off``), round after round. The side that spends no money wins, once:
the leave is recorded with the engine that made it, and an engine that may
use keys enters none while that engine still runs with keys off. It says why
once, as a config warning with both ways out.

The fleet is test_engine_money.py's: #1 a@example.com (the live login), #2 ci
(an API key with a budget), #3 c@example.com. Both engines are built as
``qswap auto`` builds them, the settings file first and then the flags, on
one switcher, one state file and the suite's fake clock.
"""

from __future__ import annotations

import json
import os
from argparse import Namespace

import pytest

from ai_quota_swap import claude_settings
from ai_quota_swap.autoswitch import (
    KEYS_OFF_HOLD_S,
    KEYS_OFF_LEFT,
    KEYS_OFF_SEEN_S,
    ONCE_WATCH_S,
    AutoSwitchEngine,
    ConfigWarningEvent,
    TickOutcome,
)
from ai_quota_swap.settings import load_engine_settings, merged_with_cli, set_setting
from tests.test_engine_money import EXHAUSTED, _fleet, _settings_line, _switches, _tick

QSWAP = "/opt/qswap/bin/qswap"


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)


@pytest.fixture(autouse=True)
def _no_ledger_scan(monkeypatch):
    from ai_quota_swap.ledger import usage as ledger_usage

    monkeypatch.setattr(ledger_usage, "scan_recent", lambda *a, **k: None, raising=False)


def _engine(h, *, flag: bool | None, host: str = "qswap auto",
            loop: bool = False) -> AutoSwitchEngine:
    """An engine on ``h``'s switcher and state file, built as `qswap auto`
    builds it (``flag``: ``--include-api-key-accounts`` or
    ``--no-include-api-key-accounts``; None: the settings file's value, as
    the menu bar's engine). Each starts a second after the one before, so
    their heartbeat identities differ."""
    h.clock.advance(1)
    settings = load_engine_settings(h.switcher.backup_dir)
    if flag is not None:
        settings = merged_with_cli(settings, Namespace(include_api_key_accounts=flag))
    engine = AutoSwitchEngine(h.switcher, settings, h.events.append, clock=h.clock)
    engine.host_label = host
    engine._looping = loop
    return engine


def _on(h, engine: AutoSwitchEngine, usage: dict = EXHAUSTED) -> TickOutcome:
    """One check of ``engine`` (the harness ticks ``h.engine``)."""
    h.engine = engine
    return _tick(h, usage)


def _state(h) -> dict:
    path = h.switcher.backup_dir / "autoswitch_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _warnings(h) -> list[ConfigWarningEvent]:
    return [e for e in h.events if isinstance(e, ConfigWarningEvent)]


def _key_entries(h) -> int:
    return len([s for s in _switches(h) if (s.to_ref or {}).get("number") == 2])


def _keys_off_leaves(h) -> int:
    return len([s for s in _switches(h) if s.limit_reason == "keys-off"])


def _two(h, *, keys_on: bool | None = True, keys_off: bool | None = False,
         off_loop: bool = False):
    """``on`` may use keys, ``off`` may not; ``on`` enters #2 at the limit
    and ``off`` leaves it (keys-off)."""
    set_setting(h.switcher.backup_dir, "autoswitch.includeApiKeyAccounts", "false")
    on = _engine(h, flag=keys_on)
    off = _engine(h, flag=keys_off, loop=off_loop)
    assert _on(h, on) is TickOutcome.SWITCHED
    assert h.switcher.current_account_number() == "2"
    h.clock.advance(30)
    assert _on(h, off) is TickOutcome.SWITCHED
    assert h.switcher.current_account_number() == "1"
    assert _switches(h)[-1].limit_reason == "keys-off"
    return on, off


class TestTheSideThatSpendsNoMoneyWins:
    def test_the_key_goes_in_and_out_once_and_it_is_said_once(self, temp_home):
        h = _fleet(temp_home)
        on, off = _two(h)
        for _ in range(10):
            h.clock.advance(60)
            assert _on(h, on) is not TickOutcome.SWITCHED
            h.clock.advance(1)
            assert _on(h, off) is not TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None
        assert (_key_entries(h), _keys_off_leaves(h)) == (1, 1)
        (warning,) = _warnings(h)
        assert warning.human() == (
            f"Warning: another auto-switch engine (qswap auto, pid {os.getpid()}) left "
            "#2 ci because API keys are off for it, so this one does not move to an "
            "API key while that one runs. Start both with the same "
            "--include-api-key-accounts flag, or set autoswitch.includeApiKeyAccounts "
            "in the settings file and drop the flags"
        )
        assert warning.to_json()["event"] == "config-warning"

    def test_the_leave_is_recorded_with_when_and_which_engine(self, temp_home):
        h = _fleet(temp_home)
        _on_engine, off = _two(h, off_loop=True)
        record = _state(h)[KEYS_OFF_LEFT]
        assert record["account"] == "2"
        assert record["at"] == record["seenAt"] == h.clock.now
        assert record["engine"] == {
            "pid": os.getpid(), "startedAt": off._started_at, "host": "qswap auto",
        }
        assert (record["loop"], record["pinned"], record["interval"]) == (True, True, 60.0)

    def test_the_engine_with_the_flag_on_holds_either_way_round(self, temp_home):
        """The menu bar's engine (the settings file: keys off) left the key
        `qswap auto --include-api-key-accounts` entered: the flag is on the
        side that may use keys."""
        h = _fleet(temp_home)
        on, off = _two(h, keys_on=True, keys_off=None)
        assert _state(h)[KEYS_OFF_LEFT]["pinned"] is False
        h.clock.advance(60)
        assert _on(h, on) is not TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert len(_warnings(h)) == 1

    def test_a_subscription_with_room_is_still_taken(self, temp_home):
        h = _fleet(temp_home)
        on, _off = _two(h)
        h.clock.advance(60)
        room = {**EXHAUSTED, "3": EXHAUSTED["3"] | {"five_hour": {"pct": 40.0}}}
        assert _on(h, on, room) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        assert not _warnings(h)


class TestKeysComeBack:
    def test_when_the_other_engine_stops_checking(self, temp_home):
        """The keys-off engine stops: once it has not been seen for the gap
        an engine that stopped leaves (15 minutes), keys are used again."""
        h = _fleet(temp_home)
        on, _off = _two(h)
        h.clock.advance(ONCE_WATCH_S - 60)
        assert _on(h, on) is not TickOutcome.SWITCHED
        h.clock.advance(61)
        assert _on(h, on) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        assert len(_warnings(h)) == 1

    def test_at_once_when_the_other_loops_process_is_gone(self, temp_home, monkeypatch):
        h = _fleet(temp_home)
        alive = {4242}
        monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive",
                            lambda pid: pid in alive)
        with monkeypatch.context() as m:
            m.setattr(os, "getpid", lambda: 4242)
            on, _off = _two(h, off_loop=True)
        h.clock.advance(60)
        assert _on(h, on) is not TickOutcome.SWITCHED
        alive.clear()
        h.clock.advance(60)
        assert _on(h, on) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"

    def test_a_once_tick_has_no_process_to_ask_about(self, temp_home, monkeypatch):
        """A `qswap auto --once` has exited by the next check: only the time
        since it was seen counts, never its process."""
        h = _fleet(temp_home)
        monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive",
                            lambda pid: False)
        with monkeypatch.context() as m:
            m.setattr(os, "getpid", lambda: 4242)
            on, _off = _two(h, off_loop=False)
        h.clock.advance(60)
        assert _on(h, on) is not TickOutcome.SWITCHED

    def test_a_cron_of_once_ticks_keeps_the_record_seen(self, temp_home):
        """`qswap auto --once --no-include-api-key-accounts` every 10 minutes:
        each tick is a new engine, and takes the record over."""
        h = _fleet(temp_home)
        on, _first = _two(h)
        for _ in range(4):
            h.clock.advance(10 * 60)
            assert _on(h, on) is not TickOutcome.SWITCHED
            assert _on(h, _engine(h, flag=False)) is not TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert _key_entries(h) == 1 and len(_warnings(h)) == 1
        # The cron stops: 15 minutes after its last tick, keys again.
        h.clock.advance(ONCE_WATCH_S + 1)
        assert _on(h, on) is TickOutcome.SWITCHED

    def test_when_the_record_ages_out(self, temp_home):
        """A day after the leave the record no longer holds, even with the
        keys-off engine still running: the key goes in once more, is left
        once more, and that is said once more."""
        h = _fleet(temp_home)
        on, off = _two(h)
        h.clock.advance(60)
        assert _on(h, on) is not TickOutcome.SWITCHED
        state_path = h.switcher.backup_dir / "autoswitch_state.json"
        state = _state(h)
        state[KEYS_OFF_LEFT]["at"] -= KEYS_OFF_HOLD_S + 1
        state_path.write_text(json.dumps(state), encoding="utf-8")
        h.clock.advance(60)
        assert _on(h, on) is TickOutcome.SWITCHED
        h.clock.advance(1)
        assert _on(h, off) is TickOutcome.SWITCHED
        h.clock.advance(60)
        assert _on(h, on) is not TickOutcome.SWITCHED
        assert (_key_entries(h), _keys_off_leaves(h), len(_warnings(h))) == (2, 2, 2)

    def test_a_record_dated_ahead_of_the_clock_does_not_hold(self, temp_home):
        h = _fleet(temp_home)
        on, _off = _two(h)
        state_path = h.switcher.backup_dir / "autoswitch_state.json"
        state = _state(h)
        state[KEYS_OFF_LEFT]["at"] += 3600
        state_path.write_text(json.dumps(state), encoding="utf-8")
        h.clock.advance(60)
        assert _on(h, on) is TickOutcome.SWITCHED


class TestNothingChangesForOneEngine:
    def test_its_own_leave_never_holds_it(self, temp_home):
        """One engine following the settings file: keys turned off, it leaves
        its key; turned on again, it uses keys again on the next check."""
        h = _fleet(temp_home)
        root = h.switcher.backup_dir
        set_setting(root, "autoswitch.includeApiKeyAccounts", "true")
        engine = _engine(h, flag=None)
        assert _on(h, engine) is TickOutcome.SWITCHED
        set_setting(root, "autoswitch.includeApiKeyAccounts", "false")
        h.clock.advance(60)
        assert _on(h, engine) is TickOutcome.SWITCHED
        assert _switches(h)[-1].limit_reason == "keys-off"
        assert KEYS_OFF_LEFT in _state(h)
        set_setting(root, "autoswitch.includeApiKeyAccounts", "true")
        h.clock.advance(60)
        assert _on(h, engine) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        assert KEYS_OFF_LEFT not in _state(h)
        assert not _warnings(h)

    def test_the_same_with_flags_one_run_after_the_other(self, temp_home, monkeypatch):
        """`qswap auto --no-include-api-key-accounts` leaves the key and is
        stopped; `qswap auto --include-api-key-accounts`, started next, uses
        keys at once."""
        h = _fleet(temp_home)
        monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive",
                            lambda pid: False)
        first = _engine(h, flag=True)
        assert _on(h, first) is TickOutcome.SWITCHED
        with monkeypatch.context() as m:
            m.setattr(os, "getpid", lambda: 4242)
            off = _engine(h, flag=False, loop=True)
            h.clock.advance(60)
            assert _on(h, off) is TickOutcome.SWITCHED
        h.clock.advance(60)
        assert _on(h, _engine(h, flag=True)) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        assert not _warnings(h)

    def test_turning_keys_off_still_leaves_the_key_at_once(self, temp_home):
        """The leave itself never waits, whatever another engine did."""
        h = _fleet(temp_home, cooldown_seconds=3600)
        on, off = _two(h)
        assert _keys_off_leaves(h) == 1
        assert h.switcher.helper_account() is None

    def test_two_engines_that_follow_the_settings_file_agree(self, temp_home):
        """With no flag on either side both read one value: a leave made
        while it was off does not hold the other once it is on."""
        h = _fleet(temp_home)
        root = h.switcher.backup_dir
        set_setting(root, "autoswitch.includeApiKeyAccounts", "true")
        first, second = _engine(h, flag=None), _engine(h, flag=None)
        assert _on(h, first) is TickOutcome.SWITCHED
        set_setting(root, "autoswitch.includeApiKeyAccounts", "false")
        h.clock.advance(60)
        assert _on(h, second) is TickOutcome.SWITCHED
        assert _switches(h)[-1].limit_reason == "keys-off"
        set_setting(root, "autoswitch.includeApiKeyAccounts", "true")
        h.clock.advance(60)
        assert _on(h, first) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        assert not _warnings(h)


class TestADryRun:
    def test_it_says_what_a_real_run_would_do(self, temp_home):
        h = _fleet(temp_home)
        _two(h)
        dry = _engine(h, flag=True)
        dry.dry_run = True
        h.clock.advance(60)
        assert _on(h, dry) is not TickOutcome.SWITCHED
        assert _key_entries(h) == 1 and len(_warnings(h)) == 1

    def test_it_writes_nothing(self, temp_home):
        h = _fleet(temp_home)
        _two(h)
        before = _state(h)[KEYS_OFF_LEFT]
        dry = _engine(h, flag=False)
        dry.dry_run = True
        h.clock.advance(KEYS_OFF_SEEN_S + 60)
        _on(h, dry)
        dry.settings = merged_with_cli(dry.settings, Namespace(include_api_key_accounts=True))
        h.clock.advance(KEYS_OFF_HOLD_S)
        _on(h, dry)
        assert _state(h)[KEYS_OFF_LEFT] == before
