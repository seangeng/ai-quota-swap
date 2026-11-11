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
        assert api_spend.menu_text(reading, now=now) == (
            "$1.00 today · $14.00/$20 (70%) est. · limit not enforced"
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
