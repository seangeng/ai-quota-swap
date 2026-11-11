"""``resets.beforeSwitch`` (docs/resets.md, "Before switching").

Off (the default), auto-switch moves at its switch line and auto mode spends
the active account's reset only when that account reaches
``resets.threshold`` anyway. On, under ``resets.auto active`` or ``on-limit``,
the engine stays past the line while the active account holds a reset of its
own that auto mode spends at ``resets.threshold`` for the window over the line,
and spends it there (``reset-ahead``). A reset any guard keeps, and one whose
claim failed or was refused, never holds the switch.

The fleet is test_engine_money.py's: #1 a@example.com (the live login,
holding one weekly reset), #2 ci (an API key with a budget), #3 c@example.com
with room. The switch line is 98 and ``resets.threshold`` 100. Resets are
faked and never claimed.
"""

from __future__ import annotations

import pytest

from ai_quota_swap import autolog, claude_settings
from ai_quota_swap.autoswitch import (
    MAX_AUTO_RESETS_PER_DAY,
    NoSwitchEvent,
    TickOutcome,
    _mutate_state_file,
)
from ai_quota_swap.resets import ClaimResult
from ai_quota_swap.settings import ResetSettings, load_reset_settings, set_setting
from tests.test_engine_money import _fleet, _switches, _tick_resets
from tests.test_resets import _grant
from tests.test_resets import _usage as _rusage

QSWAP = "/opt/qswap/bin/qswap"


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)


@pytest.fixture(autouse=True)
def _no_ledger_scan(monkeypatch):
    from ai_quota_swap.ledger import usage as ledger_usage

    monkeypatch.setattr(ledger_usage, "scan_recent", lambda *a, **k: None, raising=False)


def _week(**kw):
    return _grant("g-week", clears=("seven_day",), **kw)


def _five_hour(**kw):
    return _grant("g-5h", clears=("five_hour",), **kw)


def _usage(d7: float, *, h5: float = 10.0, reset_in: float = 3 * 86400, grants=None) -> dict:
    return {
        "1": _rusage(h5=h5, d7=d7, reset_in=reset_in,
                     grants=[_week()] if grants is None else grants),
        "2": "api key",
        "3": _rusage(h5=10, d7=30),
    }


def _settings(h, *, auto: str = "active", before_switch: bool | None = True,
              threshold: str = "100") -> None:
    root = h.switcher.backup_dir
    set_setting(root, "resets.auto", auto)
    set_setting(root, "resets.threshold", threshold)
    if before_switch is not None:
        set_setting(root, "resets.beforeSwitch", "true" if before_switch else "false")


def _held(h) -> list[NoSwitchEvent]:
    return [e for e in h.events if isinstance(e, NoSwitchEvent) and e.reason == "reset-ahead"]


def _log_text(events) -> list[str]:
    lines: list[str] = []
    log = autolog.AutoLog(lines.append, clock=lambda: "12:00:00", width=80)
    for event in events:
        log(event)
    log.flush()
    return [line for chunk in lines for line in chunk.splitlines()]


# -- the setting -----------------------------------------------------------------------


def test_the_setting_is_off_by_default_and_read_from_settings_json(tmp_path):
    assert ResetSettings().before_switch is False
    assert load_reset_settings(tmp_path).before_switch is False
    set_setting(tmp_path, "resets.beforeSwitch", "true")
    assert load_reset_settings(tmp_path).before_switch is True
    set_setting(tmp_path, "resets.beforeSwitch", "no")
    assert load_reset_settings(tmp_path).before_switch is False


# -- with it on ------------------------------------------------------------------------


class TestItHoldsTheSwitch:
    @pytest.mark.parametrize("auto", ["active", "on-limit"])
    def test_it_stays_past_the_line_and_spends_the_reset_at_resets_threshold(
        self, temp_home, auto
    ):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h, auto=auto)
        for d7 in (98.0, 99.0, 99.5):
            outcome, claims = _tick_resets(h, _usage(d7))
            assert outcome is TickOutcome.NO_ACTION and claims == []
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert not _switches(h)
        held = _held(h)
        assert len(held) == 3
        assert held[0].to_json()["reason"] == "reset-ahead"
        assert held[0].human() == (
            "No switch yet: auto mode spends #1 a@example.com's own weekly reset when 7d "
            "reaches 100%, rather than switching away (resets.beforeSwitch)"
        )
        text = _log_text(h.events)
        assert "its own weekly reset comes first, at 7d 100%" in " ".join(
            " ".join(text).split()
        ), text
        assert all(len(line) <= 80 for line in text), text
        outcome, claims = _tick_resets(h, _usage(100.0), after=_usage(0.0))
        assert claims == ["1"] and h.switcher.current_account_number() == "1"
        assert not _switches(h)

    @pytest.mark.parametrize("auto", ["active", "on-limit"])
    def test_a_window_its_reset_does_not_clear_still_switches(self, temp_home, auto):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h, auto=auto)
        outcome, _ = _tick_resets(h, _usage(50.0, h5=98.0))
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        assert not _held(h)

    @pytest.mark.parametrize("auto", ["active", "on-limit"])
    def test_a_reset_that_would_leave_another_window_over_its_line_does_not_hold(
        self, temp_home, auto
    ):
        """7d over the line, and 5h at the line too: the weekly reset leaves 5h
        where it is, so the engine would leave anyway."""
        h = _fleet(temp_home, threshold=98.0)
        _settings(h, auto=auto)
        outcome, _ = _tick_resets(h, _usage(99.0, h5=98.0))
        assert outcome is TickOutcome.SWITCHED and not _held(h)


class TestTheSettingOffIsMain:
    @pytest.mark.parametrize("auto", ["active", "on-limit"])
    @pytest.mark.parametrize("before_switch", [None, False])
    def test_it_moves_at_the_line(self, temp_home, auto, before_switch):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h, auto=auto, before_switch=before_switch)
        outcome, claims = _tick_resets(h, _usage(98.0))
        assert outcome is TickOutcome.SWITCHED and claims == []
        assert h.switcher.current_account_number() == "3"
        assert not _held(h)

    @pytest.mark.parametrize("auto", ["last-resort", "off"])
    def test_other_modes_ignore_it(self, temp_home, auto):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h, auto=auto)
        outcome, claims = _tick_resets(h, _usage(98.0))
        assert outcome is TickOutcome.SWITCHED and claims == []
        assert h.switcher.current_account_number() == "3"
        assert not _held(h)

    def test_last_resort_ignores_it_with_nowhere_to_move(self, temp_home):
        """No subscription has room, so last-resort mode would spend #1's own
        reset at resets.threshold: still no hold, since the setting does not
        apply to that mode."""
        h = _fleet(temp_home, threshold=98.0)
        _settings(h, auto="last-resort")
        usage = {**_usage(98.0), "3": _rusage(h5=10, d7=99)}
        outcome, claims = _tick_resets(h, usage)
        assert outcome is not TickOutcome.SWITCHED and claims == []
        assert not _held(h)


# -- a reset a guard keeps does not hold it ----------------------------------------------


def _record(h, **fields) -> None:
    def apply(state: dict) -> None:
        state.update(fields)

    _mutate_state_file(h.engine.state_path, apply)


class TestAKeptResetDoesNotHold:
    def test_too_little_time_left_in_the_window(self, temp_home):
        """Its 7d window refills in 10 hours, under resets.minHoursLeft."""
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        outcome, _ = _tick_resets(h, _usage(98.0, reset_in=10 * 3600))
        assert outcome is TickOutcome.SWITCHED and not _held(h)

    def test_todays_limit_on_automatic_resets(self, temp_home):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        now = h.clock.now
        _record(h, resetLog=[
            {"at": now - 3600 * (i + 1), "account": "3", "grantId": f"g-{i}",
             "result": "reset", "auto": True}
            for i in range(MAX_AUTO_RESETS_PER_DAY)
        ])
        outcome, _ = _tick_resets(h, _usage(98.0))
        assert outcome is TickOutcome.SWITCHED and not _held(h)

    def test_a_claim_still_settling(self, temp_home):
        """A claim a minute ago that no reading has followed yet: #1's
        figures may be from before it."""
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        _record(h, resetClaims={"1": {
            "at": h.clock.now - 60, "grantId": "g-other", "requestId": "r-1",
            "result": "reset", "settled": True, "auto": True,
        }})
        outcome, _ = _tick_resets(h, _usage(98.0))
        assert outcome is TickOutcome.SWITCHED and not _held(h)

    @pytest.mark.parametrize("grant", [_five_hour, lambda: _grant("g-fable")],
                             ids=["five-hour", "fable"])
    def test_a_reset_of_another_kind(self, temp_home, grant):
        """A five-hour or a Fable reset does nothing for the 7d window over
        the line."""
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        outcome, _ = _tick_resets(h, _usage(98.0, grants=[grant()]))
        assert outcome is TickOutcome.SWITCHED and not _held(h)


# -- a claim that fails or is refused does not hold it -----------------------------------


class TestAFailedClaimDoesNotHold:
    @pytest.mark.parametrize("result", ["error", "ineligible", "unavailable", "cooldown"])
    def test_after_a_failed_claim_on_that_reset_the_next_check_switches(
        self, temp_home, result
    ):
        """The last claim on #1's weekly reset failed two hours ago (long
        settled): the line stands."""
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        _record(h, resetClaims={"1": {
            "at": h.clock.now - 2 * 3600, "grantId": "g-week", "requestId": "r-1",
            "result": result, "settled": True, "auto": True,
        }})
        outcome, claims = _tick_resets(h, _usage(98.0))
        assert outcome is TickOutcome.SWITCHED and claims == []
        assert h.switcher.current_account_number() == "3" and not _held(h)

    def test_a_failed_claim_on_another_reset_does_not_stop_it(self, temp_home):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        _record(h, resetClaims={"1": {
            "at": h.clock.now - 2 * 3600, "grantId": "g-fable", "requestId": "r-1",
            "result": "ineligible", "settled": True, "auto": True,
        }})
        outcome, _ = _tick_resets(h, _usage(98.0))
        assert outcome is TickOutcome.NO_ACTION and len(_held(h)) == 1

    @pytest.mark.parametrize("result", [
        ClaimResult("error", detail="network"), ClaimResult("ineligible"),
    ], ids=["fails", "refused"])
    def test_held_then_the_claim_at_resets_threshold_fails_and_it_switches(
        self, temp_home, result
    ):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        outcome, _ = _tick_resets(h, _usage(99.0))
        assert outcome is TickOutcome.NO_ACTION and len(_held(h)) == 1
        h.clock.advance(60)
        outcome, claims = _tick_resets(h, _usage(100.0), result=result)
        assert claims == ["1"]
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        assert len(_held(h)) == 1


# -- qswap config ----------------------------------------------------------------------


class TestConfig:
    def _run(self, argv, capsys):
        from tests.test_config_cli import _run

        return _run(argv, capsys)

    @pytest.mark.parametrize("auto", ["last-resort", "off"])
    def test_setting_it_where_it_has_no_effect_says_so_in_one_line(
        self, temp_home, capsys, auto
    ):
        if auto != "last-resort":
            assert self._run(["set", "resets.auto", auto], capsys)[0] == 0
        code, out, err = self._run(["set", "resets.beforeSwitch", "true"], capsys)
        assert code == 0
        assert out == "resets.beforeSwitch = true\n"
        assert err == f"No effect while resets.auto is {auto}: qswap config set resets.auto active\n"
        assert all(len(line) <= 80 for line in err.splitlines())

    @pytest.mark.parametrize("auto", ["active", "on-limit"])
    def test_no_note_where_it_applies(self, temp_home, capsys, auto):
        self._run(["set", "resets.auto", auto], capsys)
        code, out, err = self._run(["set", "resets.beforeSwitch", "true"], capsys)
        assert (code, out, err) == (0, "resets.beforeSwitch = true\n", "")

    def test_no_note_when_it_is_turned_off(self, temp_home, capsys):
        code, out, err = self._run(["set", "resets.beforeSwitch", "false"], capsys)
        assert (code, out, err) == (0, "resets.beforeSwitch = false\n", "")

    def test_list_and_get_show_it(self, temp_home, capsys):
        code, out, _ = self._run([], capsys)
        line = next(ln for ln in out.splitlines() if ln.startswith("resets.beforeSwitch"))
        assert line.split()[1] == "false" and "(default)" in line
        assert self._run(["get", "resets.beforeSwitch"], capsys)[1] == "false\n"
