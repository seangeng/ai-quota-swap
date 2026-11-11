"""The auto-switch engine and real money: when it may enter an API key, the
spend figure and its freshness, the latch, free resets before a paid key,
the way back, and getting off a key at its limit (PR #13, round 2, decisions
A1 to A16 in the orchestrator's notes).

Everything runs on the suite's fake home and fake Keychain. The fleet is
#1 a@example.com (the live login), #2 ci (an API key), #3 c@example.com.
"""

from __future__ import annotations

import json

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
    settings.setdefault("include_api_key_accounts", True)
    h = EngineHarness(temp_home, **settings)
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


EXHAUSTED = {"1": _usage(100), "2": "api key", "3": _usage(100)}


def _enter(h) -> None:
    """The engine enters #2 at the limit."""
    assert _tick(h, EXHAUSTED) is TickOutcome.SWITCHED
    assert h.switcher.key_entered_by() == "engine"


def _notes(h) -> list[str]:
    from ai_quota_swap.menubar_notify import Notifier

    notifier = Notifier()
    return [
        n.body for n in (notifier.event("claude", e, both=False, now=0.0) for e in h.events)
        if n is not None
    ]


# == A2: unattended entry needs a limit ======================================================


class TestEntryNeedsALimit:
    def test_a_key_with_neither_a_budget_nor_a_credit_is_never_entered(self, temp_home):
        h = _fleet(temp_home, limit=None)
        for _ in range(4):
            assert _tick(h, EXHAUSTED, {"2": _reading(h, limit=None)}) is TickOutcome.BLOCKED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None
        held = [e for e in _holds(h) if e.reason == "key-no-limit"]
        assert len(held) == 4
        assert held[0].human() == (
            "Not switching to an API key: #2 ci has no budget or credit; set one "
            "with: qswap budget 2 AMOUNT"
        )
        # Said once per episode.
        assert [n for n in _notes(h) if "no budget or credit" in n] == [
            "Not switching to an API key: #2 ci has no budget or credit; set one "
            "with: qswap budget 2 AMOUNT"
        ]

    @pytest.mark.parametrize("limit", ["budget", "credit"])
    def test_a_budget_or_a_credit_lets_it_in(self, temp_home, limit):
        h = _fleet(temp_home, limit=limit)
        credit = (
            {"amount": 100.0, "used": 10.0, "left": 90.0, "spent": False, "expired": False,
             "since": "2026-11-01T00:00:00Z", "expires": None, "source": "ledger"}
            if limit == "credit" else None
        )
        reading = _reading(h, limit=500.0 if limit == "budget" else None, credit=credit)
        assert _tick(h, EXHAUSTED, {"2": reading}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"

    def test_by_hand_such_a_key_is_still_allowed(self, temp_home):
        h = _fleet(temp_home, limit=None)
        h.switcher.switch_to("2", json_output=True)
        assert h.switcher.current_account_number() == "2"
        # The user's key without a limit is kept while subscriptions have room.
        assert _tick(h, {"1": _usage(10), "2": "api key", "3": _usage(10)},
                     {"2": None}) is TickOutcome.NO_ACTION
        assert h.switcher.current_account_number() == "2"


# == A4: no fresh figure counts as at the limit ==============================================


class TestNoFreshFigure:
    @pytest.mark.parametrize("reading", ["none", "no-figure", "stale"])
    def test_not_entered(self, temp_home, reading):
        h = _fleet(temp_home)
        value = {"none": None, "no-figure": _reading(h, used=None),
                 "stale": _reading(h, age=21 * 60)}[reading]
        assert _tick(h, EXHAUSTED, {"2": value}) is TickOutcome.BLOCKED
        assert h.switcher.current_account_number() == "1"
        held = [e for e in _holds(h) if e.reason == "spend-unknown"]
        assert held and held[0].human() == (
            "Not switching to #2 ci: its spend could not be measured for 20 "
            "minutes; check it with: qswap usage"
        )

    def test_a_figure_19_minutes_old_is_fresh(self, temp_home):
        h = _fleet(temp_home)
        assert _tick(h, EXHAUSTED, {"2": _reading(h, age=19 * 60)}) is TickOutcome.SWITCHED

    @pytest.mark.parametrize("by", ["engine", "user"])
    def test_an_active_key_is_left(self, temp_home, by):
        h = _fleet(temp_home, cooldown_seconds=3600)
        if by == "engine":
            _enter(h)
        else:
            h.switcher.switch_to("2", json_output=True)
        h.clock.advance(60)  # inside the cooldown: leaving for spend-unknown ignores it
        outcome = _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(40)},
                        {"2": _reading(h, age=25 * 60)})
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        assert _switches(h)[-1].human() == (
            "Switched #2 ci → #3 c@example.com · its spend could not be measured "
            "for 20 minutes"
        )

    def test_with_no_room_it_goes_back_underneath(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        assert _tick(h, EXHAUSTED, {"2": None}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None

    def test_a_user_key_without_a_limit_is_kept(self, temp_home):
        h = _fleet(temp_home, limit=None)
        h.switcher.switch_to("2", json_output=True)
        assert _tick(h, EXHAUSTED, {"2": None}) is TickOutcome.NO_ACTION
        assert h.switcher.current_account_number() == "2"

    def test_an_engine_key_whose_limit_was_removed_is_left(self, temp_home):
        """The engine would not have entered it; with nothing to hold it to,
        it does not keep sessions on it either (the side that stops spending)."""
        h = _fleet(temp_home)
        _enter(h)
        api_spend.set_budget(h.switcher, "2", None)
        assert _tick(h, EXHAUSTED, {"2": _reading(h, limit=None)}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert "it has no budget or credit" in _switches(h)[-1].human()

    def test_an_older_way_key_with_a_stale_figure_is_left(self, temp_home):
        h = _fleet(temp_home, include_api_key_accounts=False)
        h.make_live("key2@token.local", 2)
        assert _tick(h, {"1": _usage(10), "2": "api key", "3": _usage(100)},
                     {"2": _reading(h, age=3600)}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert "its spend could not be measured" in _switches(h)[-1].human()


# == A3: the engine keeps the figure fresh itself ============================================


class TestTheEngineScans:
    def test_before_it_enters_a_key(self, temp_home, scans):
        h = _fleet(temp_home)
        _enter(h)
        assert len(scans) == 1

    def test_every_5_minutes_while_a_limited_key_is_active(self, temp_home, scans):
        h = _fleet(temp_home)
        _enter(h)
        for _ in range(10):  # ten minutes, one tick a minute
            h.clock.advance(60)
            h.engine = h._make_engine()  # cron --once: a fresh engine each time
            _tick(h, EXHAUSTED)
        assert len(scans) == 1 + 2

    @pytest.mark.parametrize("how", ["older-way", "user"])
    def test_on_a_key_active_another_way_too(self, temp_home, scans, how):
        h = _fleet(temp_home)
        if how == "older-way":
            h.make_live("key2@token.local", 2)
        else:
            h.switcher.switch_to("2", json_output=True)
        _tick(h, {"1": _usage(10), "2": "api key", "3": _usage(10)})
        assert len(scans) == 1

    def test_not_for_a_key_without_a_limit(self, temp_home, scans):
        h = _fleet(temp_home, limit=None)
        h.switcher.switch_to("2", json_output=True)
        _tick(h, {"1": _usage(10), "2": "api key", "3": _usage(10)}, {"2": None})
        assert len(scans) == 0

    def test_a_dry_run_writes_no_ledger(self, temp_home, scans):
        h = _fleet(temp_home)
        h.engine = h._make_engine(dry_run=True)
        _tick(h, EXHAUSTED)
        assert len(scans) == 0

    def test_a_failed_scan_costs_nothing_but_the_figure(self, temp_home, monkeypatch):
        from ai_quota_swap.ledger import usage as ledger_usage

        def boom(*a, **k):
            raise OSError("disk")

        monkeypatch.setattr(ledger_usage, "scan_recent", boom)
        h = _fleet(temp_home)
        assert _tick(h, EXHAUSTED, {"2": _reading(h, age=30 * 60)}) is TickOutcome.BLOCKED
        assert h.switcher.current_account_number() == "1"


class TestTheRealScan:
    """The reviewer's case, end to end: no menu bar, a $10 credit, $40 of
    transcripts on the key after the engine entered it. The engine's own
    scan finds it and leaves the key (only the subscriptions' usage is faked)."""

    def test_spend_on_the_key_is_found_and_the_key_is_left(self, temp_home, scans, monkeypatch):
        import time

        from ai_quota_swap import paths
        from ai_quota_swap.ledger import usage as ledger_usage
        from tests.ledger_fixtures import claude_line, write_lines

        monkeypatch.setattr(ledger_usage, "scan_recent", scans.real)
        h = _fleet(temp_home, limit=None)
        h.clock.now = time.time()
        api_spend.set_credit(h.switcher, "2", 10, now=h.clock.now - 60)
        real = h.switcher.usage_entries_by_account

        def collect(fetch=None, scheduled=False):
            entries = real(fetch=set())  # the real spend reading, no network
            entries.update({"1": _entry_for(_usage(100), h.clock.now),
                            "3": _entry_for(_usage(100), h.clock.now)})
            return entries

        monkeypatch.setattr(h.switcher, "usage_entries_by_account", collect)
        assert h.engine.tick() is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        assert (h.switcher.backup_dir / "ledger.db").exists()

        projects = paths.get_claude_config_home() / "projects" / "-work-demo"
        write_lines(projects / "sess-1.jsonl", [
            claude_line(mid="m1", ts=time.time() + 1, inp=10_000_000, out=0, cr=0, w5=0),
        ])
        h.clock.advance(5 * 60)
        assert h.engine.tick() is TickOutcome.SWITCHED
        assert h.switcher.helper_account() is None
        assert _switches(h)[-1].limit_reason == "credit-spent"
        assert "credit spent" in _switches(h)[-1].human()


# == A4 on the rows: they say when the limit is not enforced =================================

NOW = 1_794_000_000.0


def _row(used=14.0, limit=20.0, *, as_of=NOW, source="ledger", credit=None, note=None):
    pct = None if used is None or limit is None else round(used / limit * 100, 2)
    reading = {
        "spend": {"used": used, "limit": limit, "pct": pct, "currency": "USD"},
        "today": None if used is None else 1.0, "source": source if used is not None else None,
        "as_of": as_of, "unpriced_tokens": 0, "month": "2026-11",
    }
    if credit is not None:
        reading["credit"] = credit
    if note:
        reading["note"] = note
    return reading


def _credit(left=1150.0, amount=1187.0):
    return {"amount": amount, "used": None if left is None else amount - left, "left": left,
            "spent": left is not None and left <= 0, "expired": False,
            "since": "2026-11-01T00:00:00Z", "expires": None, "source": "ledger"}


class TestRowsSayTheLimitIsNotEnforced:
    def test_a_fresh_budget_figure_reads_as_before(self):
        reading = _row()
        assert api_spend.spend_text(reading, now=NOW + 60) == (
            "API key · $1.00 today · $14.00 this month of $20 (70%) · estimate"
        )
        assert not api_spend.limit_not_enforced(reading, NOW + 60)

    def test_a_stale_budget_figure(self):
        reading = _row()
        now = NOW + 25 * 60
        assert api_spend.spend_text(reading, now=now) == (
            "API key · $1.00 today · $14.00 this month of $20 (70%) · estimate · "
            "limit not enforced: no spend figure"
        )
        assert api_spend.detail_text(reading, now) == (
            "estimate as of the last qswap usage, 25m ago; auto-switch stays off "
            "this key until its spend is measured: qswap usage"
        )
        # The menu row's room goes to the reason, which its limit must not cut.
        assert api_spend.menu_text(reading, now=now) == (
            "$14.00/$20 (70%) est. · limit not enforced"
        )

    def test_a_budget_with_no_figure(self):
        reading = _row(used=None, limit=500.0, as_of=None)
        assert api_spend.spend_text(reading, now=NOW) == (
            "API key · budget $500 a month · limit not enforced: no spend figure"
        )
        assert api_spend.detail_text(reading, NOW) == (
            "auto-switch stays off this key until its spend is measured: qswap usage"
        )
        assert api_spend.menu_text(reading, now=NOW) == "budget $500 · limit not enforced"

    def test_a_credit_with_no_figure(self):
        reading = _row(used=None, limit=None, as_of=None, credit=_credit(left=None))
        assert api_spend.spend_text(reading, now=NOW) == (
            "API key · $1,187 credit · limit not enforced: no spend figure"
        )
        assert api_spend.menu_text(reading, now=NOW) == "$1,187 credit · limit not enforced"

    def test_a_stale_credit(self):
        reading = _row(limit=None, credit=_credit())
        assert api_spend.spend_text(reading, now=NOW + 3600) == (
            "API key · $1,150 of $1,187 credit left · estimate · limit not enforced: "
            "no spend figure"
        )

    def test_a_reached_limit_is_enforced_whatever_the_age(self):
        reading = _row(used=30.0, limit=20.0)
        assert not api_spend.limit_not_enforced(reading, NOW + 86400)
        assert "limit not enforced" not in api_spend.spend_text(reading, now=NOW + 86400)

    def test_no_limit_no_words(self):
        reading = _row(limit=None)
        assert not api_spend.limit_not_enforced(reading, NOW + 86400)
        assert "limit not enforced" not in api_spend.spend_text(reading, now=NOW + 86400)


# == A5: a limit that was reached stays reached ==============================================


def _over(h, **kw):
    return _reading(h, used=600.0, source="admin-api", **kw)


def _lower(h, **kw):
    return _reading(h, used=300.0, source="ledger", **kw)


class TestTheLatch:
    def test_a_lower_figure_from_a_worse_source_does_not_let_it_in(self, temp_home):
        h = _fleet(temp_home)
        assert _tick(h, EXHAUSTED, {"2": _over(h)}) is TickOutcome.BLOCKED
        h.clock.advance(60)
        for _ in range(3):
            assert _tick(h, EXHAUSTED, {"2": _lower(h)}) is TickOutcome.BLOCKED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert h.state()["limitLatch"]["2"]["reason"] == "budget"

    def test_an_active_key_is_left_and_stays_left(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        h.clock.advance(60)
        assert _tick(h, EXHAUSTED, {"2": _over(h)}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        h.clock.advance(3600)
        assert _tick(h, EXHAUSTED, {"2": _lower(h)}) is TickOutcome.BLOCKED
        assert h.switcher.current_account_number() == "1"

    def test_a_fresh_engine_reads_the_latch(self, temp_home):
        h = _fleet(temp_home)
        _tick(h, EXHAUSTED, {"2": _over(h)})
        h.engine = h._make_engine()
        assert _tick(h, EXHAUSTED, {"2": _lower(h)}) is TickOutcome.BLOCKED

    def test_the_next_month_clears_it(self, temp_home):
        h = _fleet(temp_home)
        _tick(h, EXHAUSTED, {"2": _over(h)})
        assert _tick(h, EXHAUSTED, {"2": _lower(h, month="2026-12")}) is TickOutcome.SWITCHED

    @pytest.mark.parametrize("amount", [500, 800])
    def test_setting_the_budget_again_clears_it(self, temp_home, amount):
        h = _fleet(temp_home)
        _tick(h, EXHAUSTED, {"2": _over(h)})
        api_spend.set_budget(h.switcher, "2", amount)
        assert "2" not in h.state().get("limitLatch", {})
        assert _tick(h, EXHAUSTED, {"2": _lower(h, limit=float(amount))}) is (
            TickOutcome.SWITCHED
        )

    def test_a_spent_credit_stays_spent(self, temp_home):
        h = _fleet(temp_home, limit="credit")
        since = "2026-11-01T00:00:00Z"

        def credit(left):
            return {"amount": 100.0, "used": 100.0 - left, "left": left,
                    "spent": left <= 0, "expired": False, "since": since,
                    "expires": None, "source": "admin-api"}

        spent = _reading(h, limit=None, source="admin-api", credit=credit(-5.0))
        assert _tick(h, EXHAUSTED, {"2": spent}) is TickOutcome.BLOCKED
        lower = _reading(h, limit=None, credit=credit(60.0))
        assert _tick(h, EXHAUSTED, {"2": lower}) is TickOutcome.BLOCKED
        # A credit recorded again is another credit.
        since = "2026-11-12T00:00:00Z"
        api_spend.set_credit(h.switcher, "2", 100)
        assert _tick(h, EXHAUSTED, {"2": _reading(h, limit=None, credit=credit(60.0))}) is (
            TickOutcome.SWITCHED
        )

    def test_a_dry_run_latches_in_memory_only(self, temp_home):
        h = _fleet(temp_home)
        h.engine = h._make_engine(dry_run=True)
        _tick(h, EXHAUSTED, {"2": _over(h)})
        assert _tick(h, EXHAUSTED, {"2": _lower(h)}) is TickOutcome.BLOCKED
        assert "limitLatch" not in h.state()


# == A7: free before paid ====================================================================

from unittest.mock import patch  # noqa: E402

from ai_quota_swap.resets import ClaimResult  # noqa: E402
from tests.test_resets import _grant, _usage as _rusage  # noqa: E402


def _week(**kw):
    return _grant("g-week", clears=("seven_day",), **kw)


def _tick_resets(h, usage, *, result=None, after=None, readings=None):
    """A tick on reset-carrying usage; a claim answers ``result`` (a reset
    by default) and the next read gives ``after``."""
    claims = []
    state = {"claimed": False}

    def claim(num, grant_id, request_id):
        claims.append(num)
        state["claimed"] = True
        return result or ClaimResult("reset", resets_left=0, cleared=("seven_day",))

    def refresh(nums):
        return {n: _entry_for(usage[n], h.clock.now) for n in nums if n in usage}

    def entries(fetch=None, scheduled=False):
        current = after if state["claimed"] and after else usage
        out = {}
        for num, value in current.items():
            out[num] = (_key((readings or {}).get(num, _reading(h))) if value == "api key"
                        else _entry_for(value, h.clock.now))
        return out

    with (
        patch.object(h.switcher, "usage_entries_by_account", side_effect=entries),
        patch.object(h.switcher, "refresh_usage", side_effect=refresh),
        patch.object(h.switcher, "claim_usage_reset", side_effect=claim),
    ):
        return h.engine.tick(), claims


class TestFreeBeforePaid:
    def test_a_tick_that_claimed_a_reset_enters_no_key(self, temp_home):
        h = _fleet(temp_home)
        usage = {"1": _rusage(d7=100, grants=[_week()]), "2": "api key", "3": _rusage(d7=100)}
        outcome, claims = _tick_resets(h, usage, result=ClaimResult("error", detail="network"))
        assert claims == ["1"]
        assert outcome is not TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert _holds(h)[-1].reason == "reset-pending"
        assert "reset" in _holds(h)[-1].human()

    def test_a_claim_in_flight_holds_the_key_back(self, temp_home):
        h = _fleet(temp_home)
        path = h.switcher.backup_dir / "autoswitch_state.json"
        path.write_text(json.dumps({"schemaVersion": 1, "resetClaims": {"1": {
            "at": h.clock.now - 30, "grantId": "g-week", "result": "pending",
            "settled": False, "auto": True, "requestId": "r1",
        }}}))
        usage = {"1": _rusage(d7=100, grants=[_week()]), "2": "api key", "3": _rusage(d7=100)}
        outcome, claims = _tick_resets(h, usage)
        assert claims == []
        assert outcome is not TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"

    @pytest.mark.parametrize("auto", ["last-resort", "active"])
    def test_on_a_key_it_entered_a_reset_underneath_is_claimed_and_it_returns(
        self, temp_home, auto
    ):
        from ai_quota_swap.settings import set_setting

        h = _fleet(temp_home)
        set_setting(h.switcher.backup_dir, "resets.auto", auto)
        _enter(h)
        h.clock.advance(10 * 60)  # past the cooldown since the key was entered
        usage = {"1": _rusage(d7=100, grants=[_week()]), "2": "api key", "3": _rusage(d7=100)}
        after = {"1": _rusage(d7=0, grants=[_week(left=0)]), "2": "api key",
                 "3": _rusage(d7=100)}
        outcome, claims = _tick_resets(h, usage, after=after)
        assert claims == ["1"]
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None

    def test_on_a_key_the_user_entered_nothing_changes(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.switch_to("2", json_output=True)
        usage = {"1": _rusage(d7=100, grants=[_week()]), "2": "api key", "3": _rusage(d7=100)}
        outcome, claims = _tick_resets(h, usage)
        assert claims == []
        assert h.switcher.current_account_number() == "2"

    def test_with_resets_auto_off_the_key_is_entered_and_the_reset_named(self, temp_home):
        from ai_quota_swap.settings import set_setting

        h = _fleet(temp_home)
        set_setting(h.switcher.backup_dir, "resets.auto", "off")
        usage = {"1": _rusage(d7=100, grants=[_week()]), "2": "api key", "3": _rusage(d7=100)}
        outcome, claims = _tick_resets(h, usage)
        assert claims == []
        assert outcome is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        assert _switches(h)[-1].human() == (
            "Switched #1 a@example.com → #2 ci · 7d limit reached; #1 a@example.com "
            "holds 1 weekly reset: qswap reset 1"
        )


# == A8: the way back needs real room and a minimum stay =====================================


class TestTheWayBack:
    def test_not_before_the_cooldown_since_the_key_was_entered(self, temp_home):
        h = _fleet(temp_home)  # cooldown 300 s
        _enter(h)
        h.clock.advance(60)
        roomy = {"1": _usage(100), "2": "api key", "3": _usage(10)}
        assert _tick(h, roomy) is TickOutcome.NO_ACTION
        assert h.switcher.current_account_number() == "2"
        h.clock.advance(250)
        assert _tick(h, roomy) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        assert _switches(h)[-1].trigger == "key-return"

    def test_under_its_line_but_with_less_than_5_points_of_room_it_waits(self, temp_home):
        from ai_quota_swap.autoswitch import RETURN_ROOM_PCT

        assert RETURN_ROOM_PCT == 5.0
        h = _fleet(temp_home, threshold=98.0)
        _enter(h)
        h.clock.advance(600)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(96)}) is (
            TickOutcome.NO_ACTION
        )
        assert h.switcher.current_account_number() == "2"
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(95)}) is (
            TickOutcome.SWITCHED
        )

    def test_every_window_needs_the_room(self, temp_home):
        h = _fleet(temp_home, threshold=98.0)
        _enter(h)
        h.clock.advance(600)
        weekly_full = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 96.0}}
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": weekly_full}) is (
            TickOutcome.NO_ACTION
        )

    def test_at_the_limit_both_are_ignored(self, temp_home):
        h = _fleet(temp_home, threshold=98.0, cooldown_seconds=3600)
        _enter(h)
        h.clock.advance(60)
        over = _reading(h, used=600.0)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(97)}, {"2": over}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "3"


# == Flapping: many ticks, one engine and two with different lines ==========================

from ai_quota_swap.autoswitch import AutoSwitchEngine  # noqa: E402
from ai_quota_swap.settings import AutoSwitchSettings  # noqa: E402


def _engines(h, *lines: float) -> list[AutoSwitchEngine]:
    """The harness's engine, plus one more per extra line (a qswap auto with
    --threshold beside the menu bar's), all on one switcher and state file."""
    out = [h.engine]
    for line in lines:
        settings = AutoSwitchSettings(threshold=line, include_api_key_accounts=True)
        out.append(AutoSwitchEngine(h.switcher, settings, h.events.append, clock=h.clock))
    return out


def _run(h, engines, readings, ticks: int, step: float = 60.0) -> list[str]:
    """``ticks`` minutes; each minute every engine ticks once on
    ``readings(minute)``. Returns where each switch went."""
    moves = []
    for minute in range(ticks):
        usage = readings(minute)
        for engine in engines:
            before = len(_switches(h))
            entries = {
                num: _key(_reading(h)) if value == "api key" else _entry_for(value, h.clock.now)
                for num, value in usage.items()
            }
            with patch.object(h.switcher, "usage_entries_by_account", return_value=entries):
                engine.tick()
            moves.extend(str((e.to_ref or {}).get("number")) for e in _switches(h)[before:])
        h.clock.advance(step)
    return moves


def _windows(h5: float, d7: float = 0.0) -> dict:
    return {"five_hour": {"pct": h5}, "seven_day": {"pct": d7}}


class TestNoFlapping:
    @pytest.mark.parametrize("p1, p3", [
        (85, 85), (91, 85), (95, 89.9), (99, 92), (100, 100), (100, 89.9), (100, 97.9),
        (92, 100), (100, 50),
    ])
    @pytest.mark.parametrize("lines", [(), (80.0,)], ids=["one-engine", "two-engines"])
    def test_constant_readings_move_sessions_at_most_once(self, temp_home, p1, p3, lines):
        h = _fleet(temp_home)
        engines = _engines(h, *lines)
        moves = _run(h, engines, lambda m: {"1": _windows(p1), "2": "api key",
                                            "3": _windows(p3)}, ticks=90)
        assert len(moves) <= 1, moves
        if p1 < 100:
            assert "2" not in moves  # never a key with room left on the active

    @pytest.mark.parametrize("lines", [(), (80.0,), (98.0,)],
                             ids=["one-engine", "two-engines", "two-engines-high"])
    def test_a_window_that_fills_and_resets(self, temp_home, lines):
        """#1 climbs half a point a minute to 100, stays a while, resets to
        10 after its five hours; #3 sits at its limit. One move to the key
        when #1 is full, one move back after the reset, per window."""
        h = _fleet(temp_home)
        engines = _engines(h, *lines)

        def readings(minute):
            cycle = minute % 300
            p1 = min(100.0, 80.0 + 0.5 * cycle)
            return {"1": _windows(p1), "2": "api key", "3": _windows(100)}

        moves = _run(h, engines, readings, ticks=900)  # three windows
        assert moves.count("2") <= 3 and len(moves) <= 6, moves

    @pytest.mark.parametrize("lines", [(), (80.0,)], ids=["one-engine", "two-engines"])
    def test_a_weekly_window_a_sliver_under_the_line(self, temp_home, lines):
        """The reviewer's monotone case: the 5h window full, the 7d window at
        89.9 then creeping up. Back once the 5h window resets, never onto
        the key again for the tenth of a point."""
        h = _fleet(temp_home)
        engines = _engines(h, *lines)

        def readings(minute):
            h5 = 100.0 if minute < 30 else 10.0
            d7 = 89.9 + 0.01 * minute
            return {"1": _windows(h5, d7), "2": "api key", "3": _windows(100, 100)}

        moves = _run(h, engines, readings, ticks=120)
        assert moves[:1] == ["2"] and moves.count("2") == 1 and len(moves) <= 2, moves

    def test_a_subscription_hovering_at_its_line_does_not_bounce_through_the_key(
        self, temp_home
    ):
        """On the engine's key, #3 drifts between 88 and 92 around its line
        of 90; #1 is full. Sessions go to #3 once and stay there."""
        import math

        h = _fleet(temp_home)
        _enter(h)
        engines = _engines(h, 80.0)
        moves = _run(h, engines, lambda m: {
            "1": _windows(100), "2": "api key",
            "3": _windows(90 + 2 * math.sin(m / 7)),
        }, ticks=240)
        assert moves.count("2") == 0 and len(moves) <= 1, moves

    def test_readings_alternating_between_full_and_room_are_bounded_by_the_cooldown(
        self, temp_home
    ):
        """The reviewer's alternating case (100 / 89.9 every minute) switched
        every tick. Back from the key at most once per cooldown now."""
        h = _fleet(temp_home)
        moves = _run(h, [h.engine], lambda m: {
            "1": _windows(100 if m % 2 == 0 else 89.9), "2": "api key", "3": _windows(100),
        }, ticks=60)
        returns = [m for m in moves if m != "2"]
        assert len(returns) <= 60 * 60 / h.settings.cooldown_seconds, moves


# == A9: at the limit the engine always gets off the key ===================================


class TestOffTheKeyAtItsLimit:
    @pytest.mark.parametrize("status", ["skip-live-session", "transient", "store-unmirrored"])
    def test_a_subscription_that_cannot_be_activated_sends_it_back_underneath(
        self, temp_home, monkeypatch, status
    ):
        h = _fleet(temp_home)
        h.switcher.switch_to("2", json_output=True)
        monkeypatch.setattr(h.engine, "_freshen_target", lambda num, email: status)
        over = _reading(h, used=600.0)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(10)}, {"2": over}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None
        assert _switches(h)[-1].human() == (
            "Switched #2 ci → #1 a@example.com · monthly budget reached (estimate); no "
            "subscription has room, so back to the account underneath"
        )

    def test_with_no_login_at_all_it_stays_and_says_so_once(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.switch_to("2", json_output=True)
        (temp_home / ".claude.json").write_text("{}", encoding="utf-8")
        over = _reading(h, used=600.0)
        for _ in range(3):
            assert _tick(h, EXHAUSTED, {"2": over}) is TickOutcome.BLOCKED
            h.clock.advance(60)
        assert h.switcher.helper_account() == "2"
        held = [e for e in _holds(h) if e.reason == "stuck-on-key"]
        assert len(held) == 3
        assert held[0].human() == (
            "Staying on #2 ci past its monthly budget: no login is under the key to "
            "go back to. Leave it with: qswap switch 1"
        )
        assert len([n for n in _notes(h) if n.startswith("Staying on #2 ci")]) == 1


# == A11: an unmanaged login underneath is never overwritten ==============================


class TestAnUnmanagedLoginUnderneath:
    def test_an_engine_key_stays_and_says_so(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        h.make_live("new@example.com", 9)  # /login as an account qswap does not manage
        h.clock.advance(600)
        live = (temp_home / ".claude.json").read_bytes()
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(10)}) is (
            TickOutcome.NO_ACTION
        )
        assert h.switcher.helper_account() == "2"
        assert (temp_home / ".claude.json").read_bytes() == live
        assert _holds(h)[-1].reason == "unmanaged-active-account"

    def test_at_the_limit_only_the_line_comes_out(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        h.make_live("new@example.com", 9)
        live = (temp_home / ".claude.json").read_bytes()
        over = _reading(h, used=600.0)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(10)}, {"2": over}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.helper_account() is None
        assert (temp_home / ".claude.json").read_bytes() == live
        assert _switches(h)[-1].to_ref == {"number": None, "email": "new@example.com"}


# == A12: a slot that cannot give a key is skipped =========================================


class TestASlotWithoutAKey:
    def _fleet(self, temp_home):
        h = _fleet(temp_home, keys=(2, 4))
        blob = json.dumps({"mcpOAuth": {"x": {"accessToken": "m"}}})
        # No key in the previous copy either, so nothing heals the slot.
        h.switcher._write_account_credentials("2", "key2@token.local", blob, retain=False)
        return h

    def test_the_next_key_is_tried(self, temp_home):
        h = self._fleet(temp_home)
        usage = {"1": _usage(100), "2": "api key", "3": _usage(100), "4": "api key"}
        assert _tick(h, usage) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "4"
        skipped = [e for e in _holds(h) if e.reason == "key-unusable"]
        assert skipped and skipped[0].account == "2"
        assert "is not an API key" in skipped[0].human()

    def test_with_no_other_key_it_holds_and_says_so_once(self, temp_home):
        h = self._fleet(temp_home)
        usage = {"1": _usage(100), "2": "api key", "3": _usage(100)}
        for _ in range(3):
            assert _tick(h, usage) is TickOutcome.BLOCKED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert not [e for e in h.events if getattr(e, "kind", "") == "error"]
        assert len([n for n in _notes(h) if "is not an API key" in n]) == 1


class TestTheAutoLogFits:
    """Every new line of the auto log fits 80 columns."""

    def _lines(self, h) -> list[str]:
        from ai_quota_swap import autolog

        lines: list[str] = []
        log = autolog.AutoLog(lines.append, clock=lambda: "12:00:00", width=80)
        for event in h.events:
            log(event)
        log.flush()
        return [line for text in lines for line in text.splitlines()]

    def test_the_holds_and_the_switches(self, temp_home):
        h = _fleet(temp_home, limit=None)
        _tick(h, EXHAUSTED, {"2": _reading(h, limit=None)})          # key-no-limit
        h2 = _fleet(temp_home / "x" if False else temp_home)
        _tick(h2, EXHAUSTED, {"2": _reading(h2, age=3600)})          # spend-unknown
        _tick(h2, EXHAUSTED)                                         # enter
        (temp_home / ".claude.json").write_text("{}", encoding="utf-8")
        _tick(h2, EXHAUSTED, {"2": _reading(h2, used=600.0)})        # stuck-on-key
        for h_ in (h, h2):
            lines = self._lines(h_)
            assert lines
            for line in lines:
                if line.strip().startswith("qswap "):
                    continue
                assert len(line) <= 80, line


# == A16: saying so when nothing watches the limit =========================================


class TestWhenTheEngineStops:
    def test_the_notice_for_a_key_the_engine_entered(self, temp_home):
        from ai_quota_swap.autoswitch import unwatched_key, unwatched_key_notice

        h = _fleet(temp_home)
        _enter(h)
        text = unwatched_key_notice(h.switcher)
        assert text == (
            "Sessions stay on #2 ci, which auto-switch entered; nothing watches its "
            "limit now. To go back: qswap switch 1"
        )
        assert unwatched_key(h.switcher) == {
            "number": "2", "enteredBy": "engine", "engineRunning": False,
            "underneath": "1", "back": "qswap switch 1",
        }
        assert h.switcher.key_entered_by() == "engine"  # kept for a restarted engine

    def test_none_for_a_key_the_user_chose_or_no_key(self, temp_home):
        from ai_quota_swap.autoswitch import unwatched_key_notice

        h = _fleet(temp_home)
        assert unwatched_key_notice(h.switcher) is None
        h.switcher.switch_to("2", json_output=True)
        assert unwatched_key_notice(h.switcher) is None

    def test_none_while_another_engine_runs(self, temp_home):
        from ai_quota_swap.autoswitch import unwatched_key_notice, write_heartbeat

        h = _fleet(temp_home)
        _enter(h)
        import time

        write_heartbeat(h.switcher.backup_dir, mode="loop", dry_run=False,
                        last_tick_at=time.time(), next_tick_at=time.time() + 60,
                        host="menu bar")
        assert unwatched_key_notice(h.switcher, host="qswap auto") is None
        assert unwatched_key_notice(h.switcher, host="menu bar") is not None

    def test_ctrl_c_on_qswap_auto_says_it(self, temp_home, monkeypatch, capsys):
        import sys

        from ai_quota_swap import cli
        from ai_quota_swap.autoswitch import AutoSwitchEngine
        from tests.test_key_helper_live import _fleet as live_fleet

        monkeypatch.setattr("ai_quota_swap.update_check.check_for_update", lambda v: None)
        monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
        s = live_fleet()
        s.switch_to("5", json_output=True, entered_by="engine")

        def interrupted(self):
            raise KeyboardInterrupt

        monkeypatch.setattr(AutoSwitchEngine, "run_loop", interrupted)
        monkeypatch.setattr(sys, "argv", ["qswap", "auto"])
        with pytest.raises(SystemExit) as stopped:
            cli.main()
        assert stopped.value.code == 130
        out = " ".join(capsys.readouterr().out.split())
        assert "Auto-switch stopped." in out
        assert "Sessions stay on #5 ci, which auto-switch entered; nothing watches its " \
               "limit now. To go back: qswap switch 1" in out
        assert s.helper_account() == "5"  # nothing moved on the way out

    def test_turning_it_off_in_the_menu_bar_notifies(self, temp_home):
        from ai_quota_swap import menubar_engines
        from ai_quota_swap.menubar_notify import Notifier

        h = _fleet(temp_home)
        _enter(h)
        events = []

        class Engine:
            switcher = h.switcher
            host_label = "menu bar"

            def run_loop(self):
                return 0

            def stop(self):
                pass

        sup = menubar_engines.EngineSupervisor(
            lambda provider, emit: Engine(), lambda p, e: events.append((p, e)),
            thread_factory=lambda **kw: type("T", (), {"start": lambda s: None,
                                                        "is_alive": lambda s: False,
                                                        "join": lambda s, t=None: None})(),
        )
        sup.start("claude")
        sup.restart("claude")  # a restart is no stop: nothing said
        assert not [e for _p, e in events if getattr(e, "kind", "") == "key-unwatched"]
        sup.apply({"claude": False})
        said = [e for _p, e in events if getattr(e, "kind", "") == "key-unwatched"]
        assert len(said) == 1
        note = Notifier().event("claude", said[0], both=False, now=0.0)
        assert note is not None and "nothing watches its limit now" in note.body
        sup.start("claude")
        assert sup.stop_all() == [said[0].human()]


# == A6: unpriced tokens never count as zero toward a limit =================================


def _seed_ledger(root, account: str, rows):
    """``(epoch, model, input)`` buckets in ``root``'s ledger, scanned now."""
    from ai_quota_swap.ledger.store import DB_FILENAME, LAST_SCAN_META, LedgerStore

    store = LedgerStore(root / DB_FILENAME)
    store.add_buckets([
        ("claude", int(ts // 3600), account, model, "/p", f"s{i}", inp, 0, 0, 0, 0, 0, 1)
        for i, (ts, model, inp) in enumerate(rows)
    ])
    store.set_meta(LAST_SCAN_META, str(NOW))
    store.close()


class TestUnpricedTokens:
    def test_the_dearest_rate_per_kind_of_token(self):
        from ai_quota_swap.ledger.pricing import PriceTable

        dear = PriceTable().dearest()
        # Fable 5.2 fast: $16 in, $20 5m, $32 1h, $80 out; Opus 4.1: $1.50 hits.
        assert (dear.input, dear.cache_write_5m, dear.cache_write_1h, dear.cache_read,
                dear.output) == (16, 20, 32, 1.50, 80)

    def test_the_ledger_charges_them_at_it(self, tmp_path):
        from ai_quota_swap.ledger import spend as ledger_spend

        _seed_ledger(tmp_path, "ci@example.com|", [
            (NOW - 60, "claude-fable-5-3", 1_000_000),   # no price: $16 at the dearest
            (NOW - 60, "claude-opus-5-5", 1_000_000),    # $4
        ])
        month0 = NOW - 9 * 86400
        ls = ledger_spend.account_spend(tmp_path, "ci@example.com", "", month_start=month0,
                                        day_start=NOW - 3600, since_start=NOW - 7200)
        assert ls.month == pytest.approx(20.0) and ls.since == pytest.approx(20.0)
        assert ls.unpriced_tokens == 1_000_000 and ls.unpriced_since == 1_000_000

    def test_over_the_whole_credit_period(self, tmp_path):
        from ai_quota_swap.ledger import spend as ledger_spend

        month_start = NOW - 86400
        _seed_ledger(tmp_path, "ci@example.com|", [
            (NOW - 5 * 86400, "claude-fable-5-3", 1_000_000),  # before this month
        ])
        ls = ledger_spend.account_spend(tmp_path, "ci@example.com", "", month_start=month_start,
                                        day_start=NOW - 3600, since_start=NOW - 9 * 86400)
        assert ls.month == 0 and ls.unpriced_tokens == 0
        assert ls.since == pytest.approx(16.0) and ls.unpriced_since == 1_000_000

    def test_the_admin_figures_charge_them_at_it(self):
        from ai_quota_swap.ledger.pricing import PriceTable

        days = {"2026-11-07": [("claude-fable-5-3", None, (1_000_000, 0, 0, 0, 0)),
                               ("claude-sonnet-5-5", "fast", (0, 1_000_000, 0, 0, 0))],
                "2026-10-30": [("claude-fable-5-3", None, (1_000_000, 0, 0, 0, 0))]}
        priced = api_spend.price_admin(days, PriceTable(), "2026-11-07",
                                       month_label="2026-11-01", since_label="2026-10-30")
        assert priced.month == pytest.approx(16.0 + 80.0)
        assert priced.since == pytest.approx(16.0 + 80.0 + 16.0)
        assert priced.unpriced == 2_000_000 and priced.unpriced_since == 3_000_000

    def test_the_reviewers_case_reaches_the_credit(self):
        """110M tokens of a model with no price used to count $0 against a
        $1,187 credit; at the dearest input rate they spend it."""
        credit = api_spend.credit_reading({"amount": 1187.0, "since": NOW, "expires": None},
                                          110 * 16.0 + 4.0, "ledger", NOW,
                                          unpriced_tokens=111_000_000)
        assert credit["spent"] and credit["unpriced_tokens"] == 111_000_000
        assert api_spend.limit_reason({"credit": credit}) == "credit-spent"

    def test_the_credit_row_carries_the_mark_and_the_detail_explains_it(self):
        credit = api_spend.credit_reading({"amount": 1187.0, "since": NOW, "expires": None},
                                          37.0, "ledger", NOW, unpriced_tokens=1_200_000)
        reading = {"spend": {"used": 37.0, "limit": None}, "today": 0.0, "source": "ledger",
                   "as_of": NOW, "unpriced_tokens": 1_200_000, "credit": credit}
        assert api_spend.spend_text(reading, now=NOW) == (
            "API key · $1,150+ of $1,187 credit left · estimate"
        )
        assert api_spend.credit_text(credit) == "$1,150+ of $1,187 credit left"
        detail = api_spend.detail_text(reading, NOW)
        assert detail.startswith(
            "+ counts 1.2M tokens of models with no price at the dearest rate"
        )
        from ai_quota_swap.json_output import api_spend_to_json

        assert api_spend_to_json(reading)["credit"]["unpricedTokens"] == 1_200_000

    def test_a_month_row_explains_its_mark_too(self):
        reading = {"spend": {"used": 37.0, "limit": None}, "today": 0.0, "source": "ledger",
                   "as_of": NOW, "unpriced_tokens": 500}
        assert api_spend.spend_text(reading, now=NOW).endswith("$37.00+ this month · estimate")
        assert api_spend.detail_text(reading, NOW) == (
            "+ counts 500 tokens of models with no price at the dearest rate"
        )


class TestHaikuLongPrompts:
    """Anthropic's pricing page: Claude Haiku 5.5 prompts over 100,000 tokens
    (input plus cache reads and writes) pay the higher prices."""

    def test_the_reader_tags_a_long_prompt(self):
        from ai_quota_swap.ledger.readers import parse_claude_line
        from tests.ledger_fixtures import claude_line

        short = parse_claude_line(claude_line(mid="a", ts=NOW, model="claude-haiku-5-5",
                                              inp=10, cr=99_990, w5=0).encode() + b"\n")
        long = parse_claude_line(claude_line(mid="b", ts=NOW, model="claude-haiku-5-5",
                                             inp=10, cr=99_991, w5=0).encode() + b"\n")
        opus = parse_claude_line(claude_line(mid="c", ts=NOW, model="claude-opus-5-5",
                                             inp=10, cr=500_000).encode() + b"\n")
        assert short.model == "claude-haiku-5-5"
        assert long.model == "claude-haiku-5-5/long"
        assert opus.model == "claude-opus-5-5"

    def test_the_long_prompt_price(self):
        from ai_quota_swap.ledger.pricing import PriceTable

        key, price = PriceTable().lookup("claude-haiku-5-5-20261101/long")
        assert key == "claude-haiku-5-5/long"
        assert (price.input, price.cache_write_5m, price.cache_write_1h, price.cache_read,
                price.output) == (0.50, 0.625, 1, 0.05, 2.50)

    def test_the_report_names_it(self):
        from ai_quota_swap.ledger import report

        assert report.model_label("claude-haiku-5-5/long") == "claude-haiku-5-5 (prompt over 100K)"


# == A13: the ledger keeps a key's usage on the key ========================================


class TestTheLedgerKeepsTheKeysUsage:
    def _world(self):
        from tests.test_ledger import Env, _registry

        env = Env()
        env.registry = _registry((1, "a@example.com", "uuid-a"), (2, "b@example.com", "uuid-b"))
        env.registry["accounts"]["5"] = {"email": "key5@token.local", "organizationUuid": "",
                                         "kind": "api_key"}
        return env

    def _key_spend(self, env, since):
        from ai_quota_swap.ledger import spend as ledger_spend

        return ledger_spend.account_spend(
            env.data, "key5@token.local", "", month_start=since - 3600, day_start=since,
            registry=env.registry, since_start=since,
        )

    def test_a_late_owner_line_does_not_take_the_keys_usage_away(self, env_free=None):
        """The reviewer's case (F35): a session started while the key was
        active gets Claude Code's owner stamp only after the line is
        removed. Its key-period usage stays on the key."""
        from ai_quota_swap.ledger.activations import record_activation
        from tests.ledger_fixtures import claude_line, owner_line, user_line, write_lines
        from tests.test_ledger import T0

        env = self._world()
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0, how="login")
        record_activation(env.data, number=5, email="key5@token.local", org="", now=T0 + 600,
                          how="helper")
        path = write_lines(env.transcript("sess-k.jsonl"), [
            user_line(T0 + 700, session="sess-k"),
            claude_line(mid="k1", ts=T0 + 1200, session="sess-k", inp=1_000_000, out=0,
                        cr=0, w5=0),
        ])
        env.scan()
        assert self._key_spend(env, T0).since == pytest.approx(4.0)
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0 + 3600,
                          how="login")
        write_lines(path, [owner_line("sess-k", "uuid-a")], append=True)
        env.scan()
        assert self._key_spend(env, T0).since == pytest.approx(4.0)
        rows = {r.label: r.totals.requests for r in env.report("account").rows}
        assert rows.get("#5 key5") == 1

    def test_an_owner_line_first_is_still_a_run_profile_session(self):
        from ai_quota_swap.ledger.activations import record_activation
        from tests.ledger_fixtures import claude_line, owner_line, write_lines
        from tests.test_ledger import T0

        env = self._world()
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        write_lines(env.transcript("sess-run.jsonl"), [
            owner_line("sess-run", "uuid-b"),
            claude_line(mid="r1", ts=T0 + 10, session="sess-run"),
        ])
        env.scan()
        rows = {r.label: r.totals.requests for r in env.report("account").rows}
        assert rows == {"#2 b@example.com": 1}

    def test_an_older_ledger_keeps_its_attribution(self):
        """A ledger from before the column: its sessions with an owner stay
        run-profile sessions, read-only or after the next scan opens it."""
        import sqlite3

        from ai_quota_swap.ledger.activations import ActivationLog, record_activation
        from ai_quota_swap.ledger.report import Accounts, _profile_sessions
        from ai_quota_swap.ledger.store import DB_FILENAME, LedgerStore
        from tests.test_ledger import T0

        env = self._world()
        env.data.mkdir(parents=True, exist_ok=True)
        db = env.data / DB_FILENAME
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;"
            "INSERT INTO meta VALUES ('schema_version', '1');"
            "CREATE TABLE sessions(provider TEXT NOT NULL, session TEXT NOT NULL, owner TEXT,"
            " first_ts REAL, PRIMARY KEY(provider, session)) WITHOUT ROWID;"
            f"INSERT INTO sessions VALUES ('claude', 'sess-run', 'uuid-b|', {T0 + 10});"
        )
        conn.close()
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        log = ActivationLog.load(env.data)
        accounts = Accounts({"claude": env.registry}, {"claude": log})
        from ai_quota_swap.ledger.spend import _Sessions

        conn = sqlite3.connect(db)
        assert _profile_sessions(_Sessions(conn), accounts, log) == {"sess-run": "b@example.com|"}
        conn.close()
        store = LedgerStore(db)  # the migration: a column, the old rows unchanged
        try:
            assert _profile_sessions(store, accounts, log) == {"sess-run": "b@example.com|"}
        finally:
            store.close()

    def test_each_activation_line_starts_its_own_period(self):
        """F38: a helper line, then a saved-key line for the same key. Usage
        in the helper period stays on the key, for sessions started before it."""
        from ai_quota_swap.ledger.activations import Activation, ActivationLog

        t = 1_794_225_600.0
        log = ActivationLog([
            Activation(ts=t, account="a@example.com|", number=1, source="switch", how="login"),
            Activation(ts=t + 600, account="key5@token.local|", number=5, source="switch",
                       how="helper"),
            Activation(ts=t + 5400, account="key5@token.local|", number=5, source="switch",
                       how="saved-key"),
        ])
        helper_hour = int((t + 1200) // 3600)
        assert log.saved_key_owner("key5@token.local|", helper_hour, t + 300) is None
        # A whole hour of the saved-key period: a session from before the
        # helper line was on the key through the helper, so it stays there too.
        later = int((t + 3 * 3600) // 3600)
        assert log.saved_key_owner("key5@token.local|", later, t + 300) is None

    def test_a_saved_key_period_after_a_login_still_splits(self):
        from ai_quota_swap.ledger.activations import Activation, ActivationLog

        t = 1_794_225_600.0
        log = ActivationLog([
            Activation(ts=t, account="a@example.com|", number=1, source="switch", how="login"),
            Activation(ts=t + 600, account="key5@token.local|", number=5, source="switch",
                       how="saved-key"),
        ])
        hour = int((t + 2 * 3600) // 3600)
        assert log.saved_key_owner("key5@token.local|", hour, t + 300) == "a@example.com|"
        assert log.saved_key_owner("key5@token.local|", hour, t + 900) is None


# == A14: credit and budget survive import --force =========================================


class TestImportKeepsTheBrakes:
    def test_import_force_keeps_the_records_settings(self, temp_home):
        from ai_quota_swap.transfer import export_accounts, import_accounts
        from tests.test_transfer import _linux_switcher, _seed_account

        s = _linux_switcher(temp_home)
        _seed_account(s, 5, "ci@example.com")
        data = s._get_sequence_data()
        data["accounts"]["5"]["kind"] = "api_key"
        s._write_json(s.sequence_file, data)
        s._write_account_credentials("5", "ci@example.com", KEY)
        api_spend.set_budget(s, "5", 500)
        api_spend.set_credit(s, "5", 1187, expires="2027-12-31", now=NOW)
        before = dict(s._get_sequence_data()["accounts"]["5"])
        out = temp_home / "ci.qswap"
        export_accounts(s, str(out), account="5")
        import_accounts(s, str(out), force=True)
        after = s._get_sequence_data()["accounts"]["5"]
        assert after["budget"] == 500.0
        assert after["credit"] == before["credit"]
        assert api_spend.credit_from_record(after)["amount"] == 1187.0


# == A15: amounts and dates ==================================================================


class TestAmounts:
    @pytest.mark.parametrize("text, value", [
        ("1187", 1187.0), ("1187.03", 1187.03), ("$1,187.03", 1187.03), ("1,187", 1187.0),
        ("$1,200.50", 1200.5), ("0.01", 0.01), ("12,345,678", None),
    ])
    def test_accepted(self, text, value):
        if value is None:
            with pytest.raises(ValueError):
                api_spend.normalize_credit(text)  # over the range
            return
        assert api_spend.normalize_credit(text) == value
        assert api_spend.normalize_budget(text) == value

    @pytest.mark.parametrize("text", [
        ",187.03", "1187,03", "1.187,03", "1,18", "11,87", "0.004", "1187.035", "0", "0.00",
        "-5", "+1187", "1e3", "1_187", "１１８７", "", "$", "abc", "nan", "inf", "1,187.",
    ])
    def test_refused(self, text):
        with pytest.raises(api_spend.AmountError):
            api_spend.normalize_credit(text)

    def test_a_stored_value_is_rounded_before_the_range_check(self):
        assert api_spend.normalize_budget(49.999) == 50.0
        with pytest.raises(ValueError):
            api_spend.normalize_budget(0.004)


def _cmd(argv, capsys):
    from tests.api_helpers import run_main

    capsys.readouterr()
    try:
        run_main(argv)
        code = 0
    except SystemExit as e:
        code = e.code
    out, err = capsys.readouterr()
    return code, out, err


class TestTheCreditCommand:
    @pytest.fixture
    def fleet(self, temp_home, monkeypatch):
        from ai_quota_swap.models import Platform
        from tests.api_helpers import pin_platform
        from tests.test_key_helper_live import _fleet as live_fleet

        pin_platform(monkeypatch, Platform.LINUX)
        return live_fleet(Platform.LINUX)

    @pytest.mark.parametrize("argv", [
        ["credit", "5", ",187.03"], ["credit", "5", "0.004"], ["credit", "5", "0"],
        ["budget", "5", "1187,03"], ["credit", "5", "--expires", "2026-12-31"],
    ])
    def test_a_bad_amount_or_a_missing_one_is_a_usage_error(self, fleet, capsys, argv):
        code, _out, err = _cmd(argv, capsys)
        assert code == 64
        assert len(err.strip().splitlines()) == 1

    def test_an_expiry_date_that_has_passed_is_refused(self, fleet, capsys):
        code, _out, err = _cmd(["credit", "5", "100", "--expires", "2020-01-01"], capsys)
        assert code == 64
        assert err.strip() == (
            "Error: --expires 2020-01-01 is past (UTC); give the credit's last usable day"
        )
        assert "credit" not in fleet._get_sequence_data()["accounts"]["5"]

    def test_the_confirmation_says_from_when_spend_counts(self, fleet, capsys):
        code, out, _err = _cmd(["credit", "5", "1187", "--expires", "2099-12-31"], capsys)
        assert code == 0
        lines = out.splitlines()
        assert lines[0] == "Recorded $1,187 of credit for #5 ci, usable until 2099-12-31 UTC."
        assert lines[1].startswith("  Spend counts against it from ") and "UTC" in lines[1]
        for line in lines:
            assert len(line) <= 80, line

    def test_nothing_recorded_fits_80_columns(self, fleet, capsys):
        code, out, _err = _cmd(["credit"], capsys)
        assert code == 0
        for line in out.splitlines():
            assert len(line) <= 80 or line.strip().startswith("qswap "), line
        assert "qswap credit N AMOUNT" in out


class TestCreditWords:
    def _credit(self, amount, left, *, source="ledger", **kw):
        return {"amount": amount, "used": amount - left, "left": left, "spent": left <= 0,
                "expired": False, "since": "2026-11-10T14:40:00Z", "expires": None,
                "source": source, **kw}

    def test_both_sides_at_one_precision(self):
        assert api_spend.credit_text(self._credit(1187.03, 1150.03)) == (
            "$1,150.03 of $1,187.03 credit left"
        )
        assert api_spend.credit_text(self._credit(1187.0, 1150.6)) == (
            "$1,150 of $1,187 credit left"
        )

    def test_under_a_dollar_is_not_zero(self):
        assert api_spend.credit_text(self._credit(1187.0, 0.5)) == (
            "under $1 of $1,187 credit left"
        )

    def test_the_detail_line_says_from_when_it_counts(self):
        ledger = {"spend": {"used": 1.0, "limit": None}, "source": "ledger", "as_of": NOW,
                  "credit": self._credit(100.0, 99.0)}
        assert "credit counted from 14:00 UTC Nov 10" in api_spend.detail_text(ledger, NOW)
        admin = {"spend": {"used": 1.0, "limit": None}, "source": "admin-api", "as_of": NOW,
                 "credit": self._credit(100.0, 99.0, source="admin-api")}
        assert "credit counted from the start of Nov 10 UTC" in api_spend.detail_text(admin, NOW)

    def test_the_menu_row_says_estimate_and_names_the_budget(self):
        credit = self._credit(1187.03, 1150.03)
        reading = {"spend": {"used": 37.0, "limit": 30.0, "pct": 123.33}, "today": 1.0,
                   "source": "ledger", "as_of": NOW, "credit": credit, "unpriced_tokens": 0}
        assert api_spend.limit_reason(reading) == "budget"
        assert api_spend.menu_text(reading, now=NOW) == (
            "$37.00/$30 (123%, over budget) est. · $1,150.03 of $1,187.03 credit left"
        )
        fine = {**reading, "spend": {"used": 10.0, "limit": 30.0, "pct": 33.3}}
        assert api_spend.menu_text(fine, now=NOW) == "$1,150.03 of $1,187.03 credit left est."

    def test_the_engine_says_an_estimate_is_one(self):
        from ai_quota_swap.autolog import switch_reason

        event = SwitchEvent(trigger="at-limit", from_ref=None, to_ref=None,
                            over_budget=True, limit_reason="credit-spent",
                            limit_source="ledger")
        assert switch_reason(event) == "credit spent (estimate)"


class TestStatusJsonCarriesTheCredit:
    def test_the_active_key_has_its_credit_as_list_rows_do(self, temp_home):
        from tests.test_key_helper_live import _fleet as live_fleet

        s = live_fleet()
        api_spend.set_credit(s, "5", 1187, expires="2099-12-31", now=NOW)
        s.switch_to("5", json_output=True)
        active = s.status(json_output=True)["active"]
        assert active["credit"] == {
            "amount": 1187.0, "since": "2026-11-06T21:20:00Z", "expires": "2099-12-31",
        }


# == Every brake, every way the figure can fail ============================================


def _brake_reading(h, limit: str, *, spent: float, source: str = "ledger", age: float = 0.0):
    """A reading for a $10 budget or a $10 credit with ``spent`` counted."""
    if limit == "budget":
        return _reading(h, used=spent, limit=10.0, source=source, age=age)
    credit = {"amount": 10.0, "used": spent, "left": 10.0 - spent, "spent": spent >= 10.0,
              "expired": False, "since": "2026-11-01T00:00:00Z", "expires": None,
              "source": source}
    return _reading(h, used=spent, limit=None, source=source, age=age, credit=credit)


class TestEveryBrake:
    def _fleet(self, temp_home, limit):
        h = _fleet(temp_home, limit=None)
        if limit == "budget":
            api_spend.set_budget(h.switcher, "2", 10)
        else:
            api_spend.set_credit(h.switcher, "2", 10, now=h.clock.now)
        return h

    def _readings(self, h, limit, state):
        """The readings of two ticks in a row for ``state``."""
        if state == "missing":
            return [None, None]
        if state == "stale":
            return [_brake_reading(h, limit, spent=2.0, age=25 * 60)] * 2
        # degraded: Anthropic's figure over the limit, then the ledger's lower one
        return [_brake_reading(h, limit, spent=12.0, source="admin-api"),
                _brake_reading(h, limit, spent=3.0)]

    @pytest.mark.parametrize("limit", ["budget", "credit"])
    @pytest.mark.parametrize("state", ["missing", "stale", "degraded"])
    def test_never_entered(self, temp_home, limit, state):
        h = self._fleet(temp_home, limit)
        for reading in self._readings(h, limit, state):
            assert _tick(h, EXHAUSTED, {"2": reading}) is TickOutcome.BLOCKED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None

    @pytest.mark.parametrize("limit", ["budget", "credit"])
    @pytest.mark.parametrize("state", ["missing", "stale", "degraded"])
    @pytest.mark.parametrize("by", ["engine", "user"])
    def test_an_active_key_is_left_and_stays_left(self, temp_home, limit, state, by):
        h = self._fleet(temp_home, limit)
        if by == "engine":
            assert _tick(h, EXHAUSTED, {"2": _brake_reading(h, limit, spent=1.0)}) is (
                TickOutcome.SWITCHED
            )
        else:
            h.switcher.switch_to("2", json_output=True)
        first, second = self._readings(h, limit, state)
        h.clock.advance(60)
        assert _tick(h, EXHAUSTED, {"2": first}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        h.clock.advance(3600)
        assert _tick(h, EXHAUSTED, {"2": second}) is not TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"

    @pytest.mark.parametrize("limit", ["budget", "credit"])
    def test_an_unpriced_model_reaches_it(self, temp_home, monkeypatch, limit):
        """$4 of a priced model and 1M input tokens of one with no price, on
        a $10 limit: at the dearest rate ($16) the limit is passed. The
        engine reads it through the real spend reading."""
        import time

        h = _fleet(temp_home, limit=None)
        h.clock.now = time.time()
        if limit == "budget":
            api_spend.set_budget(h.switcher, "2", 10)
        else:
            api_spend.set_credit(h.switcher, "2", 10, now=h.clock.now - 3600)
        _seed_ledger(h.switcher.backup_dir, "key2@token.local|", [
            (h.clock.now - 60, "claude-opus-5-5", 1_000_000),
            (h.clock.now - 60, "claude-fable-5-3", 1_000_000),
        ])
        from ai_quota_swap.ledger.store import DB_FILENAME, LAST_SCAN_META, LedgerStore

        store = LedgerStore(h.switcher.backup_dir / DB_FILENAME)
        store.set_meta(LAST_SCAN_META, str(h.clock.now))
        store.close()
        real = h.switcher.usage_entries_by_account

        def collect(fetch=None, scheduled=False):
            entries = real(fetch=set())
            entries.update({"1": _entry_for(_usage(100), h.clock.now),
                            "3": _entry_for(_usage(40), h.clock.now)})
            return entries

        monkeypatch.setattr(h.switcher, "usage_entries_by_account", collect)
        reading = collect()["2"].api_spend
        expected = "budget" if limit == "budget" else "credit-spent"
        assert api_spend.limit_reason(reading) == expected
        assert reading["unpriced_tokens"] == 1_000_000
        h.switcher.switch_to("2", json_output=True)
        assert h.engine.tick() is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        monkeypatch.setattr(h.switcher, "usage_entries_by_account", lambda fetch=None,
                            scheduled=False: {**collect(), "3": _entry_for(_usage(100),
                                                                            h.clock.now)})
        h.clock.advance(3600)
        h.engine.tick()
        assert h.switcher.current_account_number() != "2"


class TestDryRunOverAnUnmanagedLogin:
    def test_it_says_what_a_real_run_would_do(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        h.make_live("new@example.com", 9)
        h.engine = h._make_engine(dry_run=True)
        over = _reading(h, used=600.0)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(10)}, {"2": over}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.helper_account() == "2"  # nothing changed
        event = _switches(h)[-1]
        assert event.dry_run and event.to_ref == {"number": None, "email": "new@example.com"}
        assert event.human().startswith("Would switch #2 ci → new@example.com")
