"""The engine's budget rules for API-key accounts (autoswitch.py):

- at or over its budget, an API-key account is not a candidate;
- an active API-key account at or over its budget is at its limit and the
  engine looks for another account;
- at 80% the engine emits one ``budget-warning`` per account per month;
- with no budget (or no figure to hold it against) nothing changes.
"""

from __future__ import annotations

import json

import pytest

from ai_quota_swap.autoswitch import (
    BudgetWarningEvent,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from ai_quota_swap.usage_store import UsageEntry

from tests.test_autoswitch import EngineHarness, _entry_for, _usage


def _reading(used: float | None, limit: float | None, month: str = "2026-11",
             source: str = "admin-api") -> dict:
    pct = None if used is None or limit is None else used / limit * 100
    return {
        "spend": {"used": used, "limit": limit, "pct": pct, "currency": "USD"},
        "today": 0.0 if used is not None else None,
        "source": source,
        "month": month,
    }


def _api(used: float | None, limit: float | None, **kw) -> UsageEntry:
    return UsageEntry(sentinel="api key", api_spend=_reading(used, limit, **kw))


def _mark_api_key(h: EngineHarness, *nums: int) -> None:
    data = h.switcher._get_sequence_data()
    for num in nums:
        data["accounts"][str(num)]["kind"] = "api_key"
    h.switcher._write_json(h.switcher.sequence_file, data)
    for num in nums:
        # An API-key account holds a key: the helper line is only written
        # for a slot whose saved credential is one.
        email = data["accounts"][str(num)]["email"]
        h.switcher._write_account_credentials(str(num), email, "sk-ant-api03-" + "kk" * 20)


def _entries(h: EngineHarness, spec: dict) -> dict:
    return {
        num: value if isinstance(value, UsageEntry) else _entry_for(value, h.clock.now)
        for num, value in spec.items()
    }


def _harness(temp_home, active: int, api_keys: tuple[int, ...], n: int = 3, **settings):
    h = EngineHarness(temp_home, **settings)
    emails = {i: (f"key{i}@token.local" if i in api_keys else f"u{i}@example.com")
              for i in range(1, n + 1)}
    for i, email in emails.items():
        h.seed(i, email)
    h.make_live(emails[active], active)
    _mark_api_key(h, *api_keys)
    return h


class TestCandidates:
    def test_over_budget_candidate_is_skipped(self, temp_home):
        h = _harness(temp_home, 1, (2,), include_api_key_accounts=True)
        outcome = h.tick_with_entries(_entries(h, {
            "1": _usage(100), "2": _api(510, 500), "3": _usage(100),
        }))
        # Before budgets, account 2 was the last resort here.
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_under_budget_candidate_is_still_the_last_resort(self, temp_home):
        h = _harness(temp_home, 1, (2,), include_api_key_accounts=True)
        outcome = h.tick_with_entries(_entries(h, {
            "1": _usage(100), "2": _api(100, 500), "3": _usage(100),
        }))
        assert outcome is TickOutcome.SWITCHED and h.active_number() == 2

    def test_exactly_at_budget_counts_as_over(self, temp_home):
        h = _harness(temp_home, 1, (2,), include_api_key_accounts=True)
        outcome = h.tick_with_entries(_entries(h, {
            "1": _usage(100), "2": _api(500, 500), "3": _usage(100),
        }))
        assert outcome is TickOutcome.BLOCKED


class TestActiveOverBudget:
    def test_switches_to_a_subscription_with_headroom(self, temp_home):
        h = _harness(temp_home, 1, (1,))
        outcome = h.tick_with_entries(_entries(h, {
            "1": _api(612.5, 500), "2": _usage(10), "3": _usage(60),
        }))
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        (switch,) = [e for e in h.events if isinstance(e, SwitchEvent)]
        assert switch.trigger == "at-limit"
        assert "active-api-key" not in [getattr(e, "reason", "") for e in h.events]

    def test_bypasses_the_cooldown(self, temp_home):
        h = _harness(temp_home, 1, (1,), cooldown_seconds=3600)
        state_path = h.switcher.backup_dir / "autoswitch_state.json"
        state_path.write_text(json.dumps({"schemaVersion": 1, "lastSwitchAt": h.clock.now - 10}))
        outcome = h.tick_with_entries(_entries(h, {"1": _api(500, 500), "2": _usage(10)}))
        assert outcome is TickOutcome.SWITCHED

    def test_never_lands_on_another_over_budget_key(self, temp_home):
        h = _harness(temp_home, 1, (1, 2), include_api_key_accounts=True)
        outcome = h.tick_with_entries(_entries(h, {
            "1": _api(600, 500), "2": _api(900, 800), "3": _usage(100),
        }))
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_nowhere_to_go_is_blocked_not_an_error(self, temp_home):
        h = _harness(temp_home, 1, (1,), n=2)
        outcome = h.tick_with_entries(_entries(h, {"1": _api(600, 500), "2": _usage(100)}))
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1


class TestNoBudgetNoChange:
    @pytest.mark.parametrize("entry", [
        UsageEntry(sentinel="api key"),
        _api(10_000, None),           # a figure, no budget
        _api(None, 500),              # a budget, no figure
    ])
    def test_active_api_key_still_idles(self, temp_home, entry):
        h = _harness(temp_home, 1, (1,))
        outcome = h.tick_with_entries(_entries(h, {"1": entry, "2": _usage(10)}))
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["active-api-key"]
        assert not [e for e in h.events if isinstance(e, BudgetWarningEvent)]

    def test_under_budget_active_still_idles(self, temp_home):
        h = _harness(temp_home, 1, (1,))
        outcome = h.tick_with_entries(_entries(h, {"1": _api(100, 500), "2": _usage(10)}))
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["active-api-key"]

    @pytest.mark.parametrize("entry", [UsageEntry(sentinel="api key"), _api(10_000, None)])
    def test_subscription_ticks_decide_as_before(self, temp_home, entry):
        """A budgetless API-key candidate changes nothing about a subscription
        tick: the same events, the same target, as with the bare sentinel."""
        h = _harness(temp_home, 1, (2,), include_api_key_accounts=True)
        outcome = h.tick_with_entries(_entries(h, {"1": _usage(95), "2": entry, "3": _usage(10)}))
        assert outcome is TickOutcome.SWITCHED and h.active_number() == 3
        assert h.kinds() == ["poll", "switch"]
        assert h.events[-1].trigger == "proactive"


class TestWarning:
    def test_once_per_month_and_persisted(self, temp_home):
        h = _harness(temp_home, 2, (1,))
        spec = {"1": _api(410, 500), "2": _usage(10), "3": _usage(10)}
        h.tick_with_entries(_entries(h, spec))
        warnings = [e for e in h.events if isinstance(e, BudgetWarningEvent)]
        assert len(warnings) == 1
        w = warnings[0]
        assert w.account == {"number": 1, "email": "key1@token.local"}
        assert w.to_json() | {"ts": None} == {
            "schemaVersion": 1, "event": "budget-warning", "ts": None,
            "account": {"number": 1, "email": "key1@token.local"},
            "usedUsd": 410.0, "budgetUsd": 500, "pct": 82.0, "month": "2026-11",
            "source": "admin-api",
        }
        assert "has spent $410.00 of its $500.00 monthly budget (82%)" in w.human()
        assert h.state()["budgetWarnings"] == {"1": "2026-11"}
        # Same month, same engine: quiet.
        h.events.clear()
        h.tick_with_entries(_entries(h, spec))
        assert not [e for e in h.events if isinstance(e, BudgetWarningEvent)]
        # A fresh engine (cron --once) reads the state file: quiet too.
        h.engine = h._make_engine()
        h.tick_with_entries(_entries(h, spec))
        assert not [e for e in h.events if isinstance(e, BudgetWarningEvent)]
        # Next month warns again.
        h.tick_with_entries(_entries(h, {**spec, "1": _api(450, 500, month="2026-12")}))
        assert [e.month for e in h.events if isinstance(e, BudgetWarningEvent)] == ["2026-12"]

    def test_below_eighty_percent_is_quiet(self, temp_home):
        h = _harness(temp_home, 2, (1,))
        h.tick_with_entries(_entries(h, {"1": _api(399.99, 500), "2": _usage(10)}))
        assert not [e for e in h.events if isinstance(e, BudgetWarningEvent)]
        assert "budgetWarnings" not in h.state()

    def test_dry_run_warns_once_and_writes_nothing(self, temp_home):
        h = _harness(temp_home, 2, (1,))
        h.engine = h._make_engine(dry_run=True)
        spec = {"1": _api(450, 500, source="ledger"), "2": _usage(10)}
        h.tick_with_entries(_entries(h, spec))
        h.tick_with_entries(_entries(h, spec))
        warnings = [e for e in h.events if isinstance(e, BudgetWarningEvent)]
        assert len(warnings) == 1 and warnings[0].human().endswith("(estimate)")
        assert "budgetWarnings" not in h.state()

    def test_an_over_budget_active_warns_and_leaves(self, temp_home):
        h = _harness(temp_home, 1, (1,))
        h.tick_with_entries(_entries(h, {"1": _api(520, 500), "2": _usage(10)}))
        assert h.kinds().index("budget-warning") < h.kinds().index("switch")
        assert h.active_number() == 2
