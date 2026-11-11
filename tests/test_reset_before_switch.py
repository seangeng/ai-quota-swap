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

from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import autolog, claude_settings, oauth
from ai_quota_swap.autoswitch import (
    MAX_AUTO_RESETS_PER_DAY,
    NoSwitchEvent,
    TickOutcome,
    _mutate_state_file,
)
from ai_quota_swap.resets import ClaimResult
from ai_quota_swap.settings import ResetSettings, load_reset_settings, set_setting
from ai_quota_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _entry_for
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


class TestOnlyTheActiveAccountsOwnResetHolds:
    def test_another_accounts_reset_does_not_hold_under_on_limit(self, temp_home):
        """#1, active at 7d 98.5, holds only a five-hour reset; #3, at 7d 99,
        holds a weekly one. Under on-limit the planner would spend #3's once
        it reaches resets.threshold, but that reset is not #1's own: #1
        moves at its line, to #4."""
        h = _fleet(temp_home, threshold=98.0)
        h.seed(4, "d@example.com")
        _settings(h, auto="on-limit")
        usage = {
            **_usage(98.5, grants=[_five_hour()]),
            "3": _rusage(h5=10, d7=99, reset_in=3 * 86400, grants=[_week()]),
            "4": _rusage(h5=10, d7=20),
        }
        outcome, claims = _tick_resets(h, usage)
        assert outcome is TickOutcome.SWITCHED and claims == []
        assert h.switcher.current_account_number() == "4" and not _held(h)

    def test_a_reset_justified_by_another_window_does_not_hold(self, temp_home):
        """A weekly line of 90 under a switch line of 98: 7d at 97 binds
        (the furthest past its line), and 5h is at 98.5 with 3 hours left,
        over resets.minHoursLeft 1. #1's full reset clears both, but the
        planner would spend it for 5h, the fuller window, not for the 7d
        window the hold would name: #1 moves at its line."""
        h = _fleet(temp_home, threshold=98.0, weekly_threshold=90.0)
        _settings(h)
        set_setting(h.switcher.backup_dir, "resets.minHoursLeft", "1")
        full = _grant("g-full", clears=("five_hour", "seven_day"))
        outcome, claims = _tick_resets(h, _usage(97.0, h5=98.5, grants=[full]))
        assert outcome is TickOutcome.SWITCHED and claims == []
        assert h.switcher.current_account_number() == "3" and not _held(h)


class TestTheWordsQuoteTheResetLine:
    """The reset line is printed with the decimals it has, and the reading on
    a held check never reads as that line or above it: it has not reached it
    (with resets.threshold 100 the words are TestItHoldsTheSwitch's)."""

    @pytest.mark.parametrize(("line", "reset_line", "d7", "reading", "shown"), [
        (90.0, "95", 94.6, "94", "95"),
        (98.0, "99.5", 99.46, "99", "99.5"),
        (98.5, "99.5", 99.46, "99.4", "99.5"),
    ], ids=["94.6-under-95", "99.46-under-99.5", "99.46-under-99.5-on-a-98.5-line"])
    def test_the_fragment_the_sentence_and_the_row(
        self, temp_home, line, reset_line, d7, reading, shown
    ):
        h = _fleet(temp_home, threshold=line)
        _settings(h, threshold=reset_line)
        outcome, claims = _tick_resets(h, _usage(d7))
        assert outcome is TickOutcome.NO_ACTION and claims == []
        (held,) = _held(h)
        fragment = f"its own weekly reset comes first, at 7d {shown}%"
        assert autolog.no_switch_fragment(held) == (fragment, "routine")
        assert held.human() == (
            f"No switch yet: auto mode spends #1 a@example.com's own weekly reset when 7d "
            f"reaches {shown}%, rather than switching away (resets.beforeSwitch)"
        )
        assert held.to_json()["detail"] == (
            f"account 1's own weekly reset is spent when 7d reaches {shown}% "
            "(resets.beforeSwitch)"
        )
        text = _log_text(h.events)
        row = " ".join(" ".join(text).split())
        switch_at = f"{line:g}"
        assert (
            f"12:00:00 #1 a@example.com at 7d {reading}% (switch at {switch_at}%) · {fragment}"
        ) in row, text
        assert all(len(ln) <= 80 for ln in text), text


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


# -- a reset usable only once a limit is reached ------------------------------------------


class TestAResetUsableOnlyAtTheLimit:
    """A reset the server marks usable only once a limit is reached cannot be
    spent below the limit, so it never holds the switch: auto-switch moves at
    the line and the reset stays banked, as with the setting off
    (docs/resets.md, "Before switching"). Holding for it would run the
    account to its limit for a reset the server may still refuse there."""

    @pytest.mark.parametrize("auto", ["active", "on-limit"])
    @pytest.mark.parametrize("before_switch", [True, False])
    def test_it_does_not_hold_the_switch(self, temp_home, auto, before_switch):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h, auto=auto, before_switch=before_switch)
        grant = _week(usable=False, use_requires_limit=True)
        outcome, claims = _tick_resets(h, _usage(99.0, grants=[grant]))
        assert outcome is TickOutcome.SWITCHED and claims == []
        assert h.switcher.current_account_number() == "3" and not _held(h)

    def test_the_documents_say_so(self):
        from ai_quota_swap.display import grant_state
        from ai_quota_swap.resets import grant_unusable_reason

        state = grant_state(grant_unusable_reason(
            _week(usable=False, use_requires_limit=True), 1_000_000.0))
        section = _page("docs/resets.md").split("### Before switching")[1].split("\n## ")[0]
        assert f"`{state}`" in section, state
        assert "usable only once a limit is reached never holds" in _page("docs/settings.md")


# -- the hold needs a reading the reset could be spent on -------------------------------
#
# Auto mode spends a reset only on a reading of the account from the last two
# minutes ("A fresh reading first", docs/resets.md). A stored reading stays
# trusted for switch decisions long after the account stops answering (an
# hour after a failure, up to two after a 429), and a hold on it would keep
# the engine on an account that nothing can spend the reset on.


def _tick_entries(h, entries: dict) -> TickOutcome:
    """A tick on these store entries; a claim fails the test."""

    def claim(*args):
        raise AssertionError(f"claimed a reset: {args!r}")

    with (
        patch.object(h.switcher, "usage_entries_by_account",
                     side_effect=lambda fetch=None, scheduled=False: dict(entries)),
        patch.object(h.switcher, "refresh_usage",
                     side_effect=lambda nums: {n: entries[n] for n in nums if n in entries}),
        patch.object(h.switcher, "claim_usage_reset", side_effect=claim),
    ):
        return h.engine.tick()


class TestTheHoldNeedsAFreshReading:
    def _entries(self, h, **active) -> dict:
        usage = _usage(99.0)
        age = active.pop("age", 0.0)
        out = {n: _entry_for(v, h.clock.now) for n, v in usage.items()}
        out["1"] = UsageEntry(last_good=usage["1"], fetched_at=h.clock.now - age,
                              age_s=age, **active)
        return out

    @pytest.mark.parametrize("active", [
        {"age": 30.0, "consecutive_failures": 1, "last_error": "http-429",
         "trust_extended": True},
        {"age": 30.0, "consecutive_failures": 2, "last_error": "timeout",
         "trust_extended": True},
        {"age": 301.0, "trust_extended": True, "next_poll_at": 1_000_000.0 + 60},
        {"age": 3000.0, "consecutive_failures": 4, "last_error": "http-429",
         "trust_extended": True},
    ], ids=["429-since", "failure-since", "older-than-5-minutes", "an-hour-of-429s"])
    def test_a_reading_the_account_no_longer_gives_does_not_hold(self, temp_home, active):
        """Each of these readings is still trusted for the switch, so with
        the setting off the engine moves at the line on it: so does it with
        the setting on."""
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        assert _tick_entries(h, self._entries(h, **active)) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3" and not _held(h)

    @pytest.mark.parametrize("age", [0.0, 120.0, 299.0])
    def test_a_reading_from_the_last_five_minutes_holds(self, temp_home, age):
        h = _fleet(temp_home, threshold=98.0)
        _settings(h)
        entries = self._entries(h, age=age, next_poll_at=h.clock.now + 60)
        assert _tick_entries(h, entries) is TickOutcome.NO_ACTION
        assert h.switcher.current_account_number() == "1" and len(_held(h)) == 1


class TestAnAccountThatStopsAnswering:
    """End to end through the real usage store: #1 is read once at 7d 99%
    with its own weekly reset, then every read of it answers 429. Its last
    reading stays trusted for up to two hours, but no reset can be spent on
    it, so the engine moves once #1 stops answering, as it does with the
    setting off at the first check."""

    @pytest.fixture(autouse=True)
    def _no_profile_probe(self):
        with patch("ai_quota_swap.oauth.fetch_oauth_profile", return_value=None):
            yield

    def _harness(self, temp_home, monkeypatch, *, before_switch: bool):
        monkeypatch.setattr("ai_quota_swap.switcher._FETCH_STAGGER_S", 0)
        h = EngineHarness(temp_home, threshold=98.0)
        for n, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
            h.seed(n, email)
        h.make_live("a@example.com", 1)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        _settings(h, before_switch=before_switch)
        return h

    def _run(self, h, checks: int) -> tuple[list[TickOutcome], list[str]]:
        usage = {
            "1": _rusage(h5=10, d7=99, reset_in=3 * 86400, grants=[_week()]),
            "2": _rusage(h5=10, d7=30),
            "3": _rusage(h5=10, d7=20),
        }
        reads: list[str] = []

        def fetch(num, email, creds, is_active=False, persist_credentials=None, **kw):
            reads.append(num)
            if num == "1" and reads.count("1") > 1:
                return oauth.UsageOutcome(None, error="http-429")
            return oauth.UsageOutcome(dict(usage[num]))

        def claim(*args):
            raise AssertionError(f"claimed a reset: {args!r}")

        outcomes = []
        with (
            patch("ai_quota_swap.oauth.try_fetch_usage_for_account", side_effect=fetch),
            patch.object(h.switcher, "claim_usage_reset", side_effect=claim),
        ):
            for _ in range(checks):
                outcomes.append(h.engine.tick())
                if outcomes[-1] is TickOutcome.SWITCHED:
                    break
                h.clock.advance(60)
        return outcomes, reads

    def test_with_it_off_the_first_check_switches(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, before_switch=False)
        outcomes, _ = self._run(h, 3)
        assert outcomes == [TickOutcome.SWITCHED] and not _held(h)

    def test_with_it_on_the_hold_ends_when_the_account_stops_answering(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch, before_switch=True)
        outcomes, reads = self._run(h, 15)
        assert outcomes[0] is TickOutcome.NO_ACTION and len(_held(h)) >= 1
        assert outcomes[-1] is TickOutcome.SWITCHED, (outcomes, reads)
        assert h.switcher.current_account_number() in ("2", "3")
        # Held only while #1's reading was its last good answer, then the
        # first check after the refused read switched.
        assert reads.count("1") >= 2
        assert len(outcomes) <= 6, outcomes
        (switch,) = [e for e in h.events if type(e).__name__ == "SwitchEvent"]
        assert switch.trigger == "proactive"


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


# -- the documents quote what the code prints ------------------------------------------

REPO = Path(__file__).resolve().parents[1]


def _page(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _documents_fleet(temp_home) -> EngineHarness:
    """The documents' accounts: #1 personal (active, holding one weekly
    reset), #2 work and #3 night-shift; a switch line of 98."""
    h = EngineHarness(temp_home, threshold=98.0)
    for n, email in ((1, "maya@example.com"), (2, "maya.work@example.com"),
                     (3, "ops@example.net")):
        h.seed(n, email)
    h.make_live("maya@example.com", 1)
    data = h.switcher._get_sequence_data()
    for n, alias in (("1", "personal"), ("2", "work"), ("3", "night-shift")):
        data["accounts"][n]["alias"] = alias
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def test_the_documents_quote_the_hold_as_the_code_prints_it(temp_home):
    h = _documents_fleet(temp_home)
    _settings(h)
    usage = {
        "1": _rusage(h5=10, d7=98, reset_in=3 * 86400, grants=[_week()]),
        "2": _rusage(h5=12, d7=21),
        "3": _rusage(h5=40, d7=60),
    }
    outcome, _ = _tick_resets(h, usage)
    assert outcome is TickOutcome.NO_ACTION
    lines: list[str] = []
    log = autolog.AutoLog(lines.append, clock=lambda: "14:20:00", width=80)
    for event in h.events:
        log(event)
    log.flush()
    block = "\n".join(line for chunk in lines for line in chunk.splitlines())
    assert f"```text\n{block}\n```" in _page("docs/resets.md"), block
    (held,) = _held(h)
    assert f"`{held.human()}`" in _page("docs/resets.md"), held.human()
    fragment, _tone = autolog.no_switch_fragment(held)
    assert f"`{fragment}`" in _page("docs/auto-switch.md"), fragment
    assert "`reset-ahead`" in _page("docs/json-output.md")


def test_the_documents_quote_the_config_note():
    from ai_quota_swap.cli import _config_set_note
    from ai_quota_swap.settings import setting_spec

    note = _config_set_note(Path("/nonexistent"), setting_spec("resets.beforeSwitch"), True)
    assert f"$ qswap config set resets.beforeSwitch true\nresets.beforeSwitch = true\n{note}\n" in (
        _page("docs/resets.md"))


def test_the_menu_bar_page_names_the_toggle():
    from ai_quota_swap import menubar

    page = _page("docs/menubar.md")
    assert f"*{menubar.BEFORE_SWITCH_LABEL}* is `resets.beforeSwitch`" in page
    assert f"| `resets.beforeSwitch` | `false` | *{menubar.BEFORE_SWITCH_LABEL}*" in page
