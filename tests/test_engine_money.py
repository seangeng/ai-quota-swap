"""The auto-switch engine and real money: when it may enter an API key, the
spend figure and its freshness, the latch, free resets before a paid key,
the way back, and getting off a key at its limit (PR #13, round 2, decisions
A1 to A16 in the orchestrator's notes).

Everything runs on the suite's fake home and fake Keychain. The fleet is
#1 a@example.com (the live login), #2 ci (an API key), #3 c@example.com.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ai_quota_swap import api_spend, claude_settings
from ai_quota_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from ai_quota_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _entry_for, _usage

QSWAP = "/opt/qswap/bin/qswap"
KEY = "sk-ant-api03-" + "kk" * 20


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)


class _Scans(list):
    real = None


@pytest.fixture(autouse=True)
def scans(monkeypatch):
    """The engine's ledger scan, counted; a test that wants a real scan
    calls ``scans.real``."""
    calls = _Scans()
    from ai_quota_swap.ledger import usage as ledger_usage

    calls.real = getattr(ledger_usage, "scan_recent", None)
    monkeypatch.setattr(ledger_usage, "scan_recent", lambda *a, **k: calls.append(1),
                        raising=False)
    return calls


def _fleet(temp_home, *, limit: str | None = "budget", keys=(2,), **settings) -> EngineHarness:
    h = EngineHarness(temp_home, include_api_key_accounts=True, **settings)
    h.seed(1, "a@example.com")
    for n in keys:
        h.seed(n, f"key{n}@token.local")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    data = h.switcher._get_sequence_data()
    for n in keys:
        data["accounts"][str(n)]["kind"] = "api_key"
    data["accounts"]["2"]["alias"] = "ci"
    h.switcher._write_json(h.switcher.sequence_file, data)
    for n in keys:
        h.switcher._write_account_credentials(str(n), f"key{n}@token.local", KEY)
        if limit == "budget":
            api_spend.set_budget(h.switcher, str(n), 500)
        elif limit == "credit":
            api_spend.set_credit(h.switcher, str(n), 100, now=h.clock.now)
    return h


def _reading(h, used: float | None = 10.0, limit: float | None = 500.0, *,
             age: float = 0.0, source: str = "ledger", credit: dict | None = None,
             month: str = "2026-11") -> dict:
    pct = None if used is None or limit is None else round(used / limit * 100, 2)
    out = {
        "spend": {"used": used, "limit": limit, "pct": pct, "currency": "USD"},
        "today": None if used is None else 0.0,
        "source": source if used is not None else None,
        "month": month,
        "as_of": None if used is None else h.clock.now - age,
        "unpriced_tokens": 0,
    }
    if credit is not None:
        out["credit"] = credit
    return out


def _key(reading: dict | None) -> UsageEntry:
    return UsageEntry(sentinel="api key", api_spend=reading)


def _tick(h, usage: dict, readings: dict | None = None) -> TickOutcome:
    """One tick; ``readings`` gives the API keys' spend readings (a fresh
    figure well under the budget by default)."""
    entries = {}
    for num, value in usage.items():
        if value == "api key":
            entries[num] = _key((readings or {}).get(num, _reading(h)))
        else:
            entries[num] = _entry_for(value, h.clock.now)
    return h.tick_with_entries(entries)


def _switches(h) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


def _holds(h) -> list[NoSwitchEvent]:
    return [e for e in h.events if isinstance(e, NoSwitchEvent)]


def _settings_line(temp_home) -> str | None:
    path = temp_home / ".claude" / "settings.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("apiKeyHelper")


# == A1: the engine enters a key only at the limit ===========================================


class TestEntryOnlyAtTheLimit:
    def test_at_the_limit_it_enters(self, temp_home):
        h = _fleet(temp_home)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(100)}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "2"
        assert _switches(h)[-1].trigger == "at-limit"

    def test_never_on_the_proactive_trigger(self, temp_home):
        """#1 over its line with room left, #3 inside the hysteresis margin:
        the engine stays on #1 and uses its last points first."""
        h = _fleet(temp_home)
        for _ in range(5):
            outcome = _tick(h, {"1": _usage(92), "2": "api key", "3": _usage(85)})
            assert outcome is not TickOutcome.SWITCHED
            h.clock.advance(600)
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None

    def test_never_on_the_usage_unreadable_trigger(self, temp_home):
        h = _fleet(temp_home)
        for _ in range(6):
            outcome = _tick(h, {"1": None, "2": "api key", "3": None})
            assert outcome is not TickOutcome.SWITCHED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None

    def test_a_subscription_with_room_is_tried_first(self, temp_home):
        h = _fleet(temp_home)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(40)}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "3"


# == A10: never key to key ===================================================================


class TestNeverKeyToKey:
    def test_from_a_key_made_active_the_older_way(self, temp_home):
        """#2 saved the older way (no helper line) is active; #4 is another
        key; subscriptions are exhausted: three unreadable ticks never move
        the user onto #4."""
        h = _fleet(temp_home, keys=(2, 4))
        h.make_live("key2@token.local", 2)
        for _ in range(5):
            outcome = _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(100),
                                "4": "api key"})
            assert outcome is not TickOutcome.SWITCHED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "2"
        assert _settings_line(temp_home) is None
