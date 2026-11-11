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
