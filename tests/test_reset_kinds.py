"""Usage reset kinds: a Fable reset clears only Fable usage, a weekly reset
clears all weekly usage, and the planner, the displays and the JSON treat
them as different things (resets.grant_scope and the planner rules).

Pure planner tests run on the fixed clock of tests/test_resets.py; the CLI
tests drive ``qswap reset`` over a seeded switcher with faked readings and a
faked claim, on the real clock (the command reads it). Nothing here reaches
a server.
"""

from __future__ import annotations

import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from ai_quota_swap import cli, resets
from ai_quota_swap.autoswitch import ResetEvent
from ai_quota_swap.resets import ClaimResult, ResetPlan, plan_reset
from ai_quota_swap.usage_store import UsageEntry
from tests.test_autoswitch import _iso_at
from tests.test_resets import (
    ACTIVE_100,
    DAY,
    LAST_RESORT,
    NOW,
    ON_LIMIT,
    RAW_STATUS,
    _grant,
    _usage,
    _views,
    h,  # noqa: F401  (fixture)
)

FABLE_KEY = "seven_day_overage_included"


def fable(gid: str = "fb", **kw) -> dict:
    """A Fable reset as the live server sends it: only the Fable window,
    ``gives_back_weekly``, and (misleadingly) ``type: "full"``."""
    return _grant(gid, clears=[FABLE_KEY], gives_back=True, type="full", **kw)


def weekly(gid: str = "wk", *, lists_fable: bool = True, **kw) -> dict:
    clears = ["seven_day", FABLE_KEY] if lists_fable else ["seven_day"]
    return _grant(gid, clears=clears, gives_back=False, type="weekly", **kw)


def full(gid: str = "fu", *, lists_fable: bool = False, **kw) -> dict:
    clears = ["five_hour", "seven_day"] + ([FABLE_KEY] if lists_fable else [])
    return _grant(gid, clears=clears, gives_back=False, type="full", **kw)


def session(gid: str = "ss", **kw) -> dict:
    return _grant(gid, clears=["five_hour"], gives_back=False, type="session", **kw)


MODES = [LAST_RESORT, ON_LIMIT, ACTIVE_100]


def _one(usage: dict, settings, **kw):
    return plan_reset(_views({"1": usage}), current="1", settings=settings, now=NOW, **kw)


# -- step 2: the scope model ---------------------------------------------------------


class TestScope:
    def test_the_live_fable_grant_is_a_model_reset(self):
        # type "full" on the wire, but it clears only the Fable window and
        # gives back weekly: Claude Code calls it a Fable reset, and so does qswap.
        status = resets.parse_status(RAW_STATUS)
        g = status["grants"][0]
        assert g["type"] == "full"
        assert resets.grant_scope(g) == "model"
        assert resets.grant_kind(g) == "Fable"
        assert resets.reset_name(g) == "Fable reset"
        assert resets.clears_phrase(g) == "clears Fable usage only"

    @pytest.mark.parametrize("grant, scope, name, phrase", [
        (weekly(), "weekly", "weekly reset", "clears all weekly usage"),
        (weekly(lists_fable=False), "weekly", "weekly reset", "clears all weekly usage"),
        (full(), "full", "full reset", "clears all usage"),
        (session(), "session", "5-hour reset", "clears 5-hour usage only"),
        (_grant(clears=["seven_day_opus"], gives_back=False, type=None), "model", "Opus reset",
         "clears Opus usage only"),
        (_grant(clears=[FABLE_KEY, "seven_day_opus"]), "model", "Fable+Opus reset",
         "clears Fable and Opus usage only"),
    ])
    def test_each_kind(self, grant, scope, name, phrase):
        assert resets.grant_scope(grant) == scope
        assert resets.reset_name(grant) == name
        assert resets.clears_phrase(grant) == phrase

    @pytest.mark.parametrize("clears, gives_back, type_, scope", [
        # gives_back_weekly agrees with per-model windows only ...
        ([FABLE_KEY], True, None, "model"),
        # ... and is moot on a grant that clears the weekly window itself.
        (["seven_day"], True, "full", "weekly"),
        (["five_hour"], True, "full", "session"),
        # type "full" is no evidence: the windows decide (Codex weekly-only plan).
        (["seven_day"], False, "full", "weekly"),
        (["five_hour"], False, "full", "session"),
        # A declared weekly type on model-only windows reads as weekly (broader).
        ([FABLE_KEY], False, "weekly", "weekly"),
        # Declared narrower than the windows: the windows (broader) win.
        (["five_hour", "seven_day"], False, "session", "full"),
        (["five_hour", "seven_day"], False, "weekly", "full"),
        # Contradictions the model has no reading for are unknown.
        (["seven_day"], False, "session", "unknown"),
        (["five_hour"], False, "weekly", "unknown"),
        ([FABLE_KEY], False, "session", "unknown"),
        # Shapes outside the model.
        (["five_hour", FABLE_KEY], False, None, "unknown"),
        (["seven_day_cowork"], False, None, "unknown"),
        ([], False, "full", "unknown"),
    ])
    def test_declared_kind_is_cross_checked_against_clears(self, clears, gives_back, type_, scope):
        g = _grant(clears=clears, gives_back=gives_back, type=type_)
        assert resets.grant_scope(g) == scope

    def test_an_unknown_shape_is_shown_as_reset_with_its_windows(self):
        g = _grant(clears=["five_hour", FABLE_KEY], gives_back=False, type=None)
        assert resets.reset_name(g) == "reset"
        assert resets.grant_kind(g) == "reset"
        assert resets.clears_phrase(g) == "clears 5h, Fable"

    def test_unknown_window_keys_are_kept_and_named(self):
        raw = dict(RAW_STATUS["grants"][0], clears=["seven_day", "mystery", "\x1b[31m", 7],
                   gives_back_weekly=False, type="weekly")
        g = resets.parse_status(dict(RAW_STATUS, grants=[raw]))["grants"][0]
        assert g["clears"] == ["seven_day"]
        assert g["clears_unknown"] == ["mystery", "?"]
        assert resets.grant_scope(g) == "weekly"
        assert resets.clears_phrase(g) == "clears all weekly usage (also lists mystery, ?)"
        only = resets.parse_status(dict(RAW_STATUS, grants=[dict(raw, clears=["mystery"])]))
        assert resets.reset_name(only["grants"][0]) == "reset"
        assert resets.clears_phrase(only["grants"][0]) == "clears mystery"

    def test_json_adds_scope_and_unknown_windows(self):
        status = resets.parse_status(RAW_STATUS)
        out = resets.status_to_json(status, NOW)
        g = out["grants"][0]
        assert (g["kind"], g["scope"], g["clearsUnknown"]) == ("Fable", "model", [])
        # Nothing existing was renamed or dropped.
        assert {"id", "label", "kind", "resetsLeft", "clears", "givesBackWeekly",
                "unusableReason", "percentUsed"} <= set(g)


# -- step 3: planner rules ------------------------------------------------------------


class TestRule1OnlyForAWindowItClears:
    @pytest.mark.parametrize("settings", MODES)
    def test_a_fable_reset_is_never_spent_for_the_weekly_limit(self, settings):
        # 7d at 100; the give-back (60 points) would even bring it under the
        # line. Claude Code offers a Fable reset at the weekly wall ("frees up
        # only what Fable used this week"); qswap never spends one there.
        usage = _usage(d7=100, fable=10, share=60, grants=[fable()])
        assert _one(usage, settings) is None
        assert _one(usage, replace(settings, weekly_for_model_limit=True)) is None

    @pytest.mark.parametrize("settings", MODES)
    def test_a_fable_reset_is_never_spent_for_the_five_hour_limit(self, settings):
        usage = _usage(h5=100, d7=30, fable=10, grants=[fable()])
        assert _one(usage, replace(settings, min_hours_left=0)) is None

    @pytest.mark.parametrize("settings", MODES)
    def test_a_weekly_reset_is_never_spent_for_the_five_hour_limit(self, settings):
        usage = _usage(h5=100, d7=30, fable=10, grants=[weekly()])
        assert _one(usage, replace(settings, min_hours_left=0)) is None

    @pytest.mark.parametrize("settings", MODES)
    def test_a_five_hour_reset_is_never_spent_for_the_weekly_limit(self, settings):
        usage = _usage(h5=10, d7=100, grants=[session()])
        assert _one(usage, settings) is None


class TestRule2NarrowestFirst:
    def test_a_fable_reset_before_a_weekly_one_that_would_also_clear_it(self):
        # Fable at 100 and 7d at 96: the weekly reset (the server's next
        # pick, expiring first) would clear both; the Fable reset gives back
        # 40 points and is enough. The narrow one is spent.
        grants = [weekly("wk", ends=NOW + 30 * DAY), fable("fb", ends=NOW + 100 * DAY)]
        usage = _usage(fable=100, d7=96, share=40, grants=grants, next_id="wk")
        for settings in (LAST_RESORT, ON_LIMIT):
            plan = _one(usage, settings)
            assert plan is not None
            assert (plan.grant_id, plan.scope, plan.window) == ("fb", "model", "Fable")
            assert plan.projected == {"7d": 56.0, "Fable": 0.0}

    def test_scope_comes_before_the_refilled_windows_clock(self):
        # Before: (active, most hours left, rank) let the weekly reset win
        # because its 7d window had more of the week left than Fable's.
        usage = _usage(fable=100, d7=96, share=40, reset_in=5 * DAY,
                       grants=[weekly("wk"), fable("fb")], next_id="wk")
        usage["scoped"][0]["resets_at"] = _iso_at(NOW + 2 * DAY)
        plan = _one(usage, ON_LIMIT)
        assert plan is not None and plan.grant_id == "fb"
        assert plan.hours_left == pytest.approx(48.0)

    def test_the_ranking_key(self):
        usage = _usage(fable=100, d7=96, share=40, grants=[weekly("wk"), fable("fb")], next_id="wk")
        view = _views({"1": usage})[0]
        cands = resets._evaluate(view, ON_LIMIT, NOW)
        assert [(c.grant["id"], c.scope) for c in cands] == [("fb", "model"), ("wk", "weekly")]
        # (breadth, not the server's next pick, ends, windows cleared)
        assert cands[0].rank[:2] == (0, True) and cands[1].rank[:2] == (1, False)

    def test_without_the_narrow_reset_being_enough_the_weekly_is_spent(self):
        # No share reported: the Fable reset leaves 7d at 96, so only the
        # weekly reset (which lists Fable) makes the account usable.
        usage = _usage(fable=100, d7=96, grants=[weekly("wk"), fable("fb")])
        plan = _one(usage, ON_LIMIT)
        assert plan is not None and (plan.grant_id, plan.window) == ("wk", "7d")


class TestRule3NoBroadResetForANarrowLimit:
    @pytest.mark.parametrize("settings", MODES)
    @pytest.mark.parametrize("grant", [weekly(), full(lists_fable=True)])
    def test_kept_when_only_fable_is_at_its_limit(self, settings, grant):
        usage = _usage(fable=100, d7=31, grants=[grant])
        assert _one(usage, settings) is None

    @pytest.mark.parametrize("settings", MODES)
    def test_the_setting_allows_it(self, settings):
        usage = _usage(fable=100, d7=31, grants=[weekly()])
        plan = _one(usage, replace(settings, weekly_for_model_limit=True))
        assert plan is not None
        assert (plan.grant_id, plan.scope, plan.window) == ("wk", "weekly", "Fable")
        assert plan.projected == {"7d": 0.0, "Fable": 0.0}

    def test_spent_when_the_all_models_weekly_is_the_reason(self):
        usage = _usage(fable=100, d7=97, grants=[weekly()])
        plan = _one(usage, LAST_RESORT)
        assert plan is not None and (plan.window, plan.scope) == ("7d", "weekly")

    def test_a_full_reset_spent_for_the_five_hour_window(self):
        usage = _usage(h5=100, d7=30, grants=[full()])
        plan = _one(usage, replace(ON_LIMIT, min_hours_left=0))
        assert plan is not None and (plan.window, plan.scope) == ("5h", "full")

    def test_kept_by_scope_names_what_auto_would_do(self):
        usage = _usage(fable=100, d7=31, grants=[weekly()])
        real = _one(usage, LAST_RESORT)
        widened = _one(usage, replace(LAST_RESORT, weekly_for_model_limit=True))
        kept = resets.kept_by_scope(real, widened)
        assert kept is widened
        assert resets.kept_note(kept) == (
            "Auto keeps the weekly reset on #1: only Fable is at its limit (100%), "
            "and a weekly reset clears all weekly usage "
            "(resets.weeklyForModelLimit true would spend it)."
        )

    def test_kept_by_scope_is_silent_otherwise(self):
        model = _one(_usage(fable=100, d7=31, grants=[fable()]), LAST_RESORT)
        on_7d = _one(_usage(fable=10, d7=97, grants=[weekly()]), LAST_RESORT)
        assert model is not None and on_7d is not None
        assert resets.kept_by_scope(None, model) is None  # a Fable reset is not "kept"
        assert resets.kept_by_scope(None, on_7d) is None  # 7d is an all-usage reason
        assert resets.kept_by_scope(on_7d, on_7d) is None  # auto spends something
        assert resets.kept_by_scope(None, None) is None

    def test_manual_trade_note(self):
        usage = _usage(fable=100, d7=31, grants=[weekly()])
        assert resets.trade_note(weekly(), usage, 95.0) == (
            "This weekly reset clears all weekly usage; only Fable is at its limit (7d is at 31%)."
        )
        with_5h = _usage(h5=10, fable=100, d7=31)
        assert resets.trade_note(full(), with_5h, 95.0) == (
            "This full reset clears all usage; only Fable is at its limit (5h is at 10%, 7d is at 31%)."
        )
        assert resets.trade_note(fable(), usage, 95.0) is None  # narrow
        assert resets.trade_note(weekly(), _usage(fable=100, d7=97), 95.0) is None  # 7d too
        assert resets.trade_note(weekly(), _usage(fable=50, d7=31), 95.0) is None  # nothing

    def test_manual_order_offers_the_narrowest_useful_reset(self):
        grants = [weekly("wk"), fable("fb")]
        status = _usage(grants=grants, next_id="wk")["resets"]
        pick = lambda usage: resets.manual_order(status, usage, 95.0, NOW)[0]["id"]  # noqa: E731
        assert pick(_usage(fable=100, d7=31)) == "fb"  # only Fable at its limit
        assert pick(_usage(fable=20, d7=99)) == "wk"  # the Fable reset does not clear 7d
        assert pick(_usage(fable=20, d7=30)) == "fb"  # nothing at the line: narrowest


class TestRule4UseItOrLoseIt:
    def test_an_expiring_weekly_reset_is_measured_on_the_all_models_weekly(self):
        soon = NOW + 2 * DAY
        assert _one(_usage(fable=60, d7=30, grants=[weekly(ends=soon)]), LAST_RESORT) is None
        plan = _one(_usage(fable=10, d7=60, grants=[weekly(ends=soon)]), LAST_RESORT)
        assert plan is not None and (plan.reason, plan.window) == ("expiring", "7d")

    def test_an_expiring_fable_reset_is_measured_on_fable(self):
        soon = NOW + 2 * DAY
        assert _one(_usage(fable=10, d7=60, grants=[fable(ends=soon)]), LAST_RESORT) is None
        plan = _one(_usage(fable=60, d7=30, grants=[fable(ends=soon)]), LAST_RESORT)
        assert plan is not None and (plan.reason, plan.window) == ("expiring", "Fable")

    def test_the_sooner_deadline_wins_over_scope_when_expiring(self):
        grants = [fable("fb", ends=NOW + 5 * DAY), weekly("wk", ends=NOW + 2 * DAY)]
        plan = _one(_usage(fable=60, d7=60, grants=grants), LAST_RESORT)
        assert plan is not None and (plan.reason, plan.grant_id) == ("expiring", "wk")

    def test_at_the_line_scope_still_comes_first(self):
        grants = [fable("fb", ends=NOW + 5 * DAY), weekly("wk", ends=NOW + 2 * DAY)]
        plan = _one(_usage(fable=100, d7=96, share=40, grants=grants), LAST_RESORT)
        assert plan is not None and plan.grant_id == "fb"


class TestRule5ProjectionPerKind:
    def test_a_weekly_reset_zeroes_only_what_it_lists(self):
        plan = _one(_usage(fable=50, d7=97, grants=[weekly(lists_fable=False)]), LAST_RESORT)
        assert plan is not None and plan.projected == {"7d": 0.0, "Fable": 50.0}

    def test_a_weekly_reset_that_leaves_fable_blocked_is_not_spent(self):
        assert _one(_usage(fable=100, d7=97, grants=[weekly(lists_fable=False)]), LAST_RESORT) is None
        plan = _one(_usage(fable=100, d7=97, grants=[weekly(lists_fable=True)]), LAST_RESORT)
        assert plan is not None and plan.projected == {"7d": 0.0, "Fable": 0.0}

    def test_a_full_reset_keeps_a_window_it_does_not_list(self):
        plan = _one(_usage(h5=100, d7=50, fable=40, grants=[full()]), replace(ON_LIMIT, min_hours_left=0))
        assert plan is not None and plan.projected == {"5h": 0.0, "7d": 0.0, "Fable": 40.0}

    def test_a_model_reset_gives_back_its_share_and_leaves_5h(self):
        plan = _one(_usage(h5=50, fable=100, d7=60, share=20, grants=[fable()]), LAST_RESORT)
        assert plan is not None and plan.projected == {"5h": 50.0, "7d": 40.0, "Fable": 0.0}

    def test_the_account_must_be_usable_afterwards(self):
        # Fable refilled, but the 5-hour window stays over the line.
        assert _one(_usage(h5=99, fable=100, d7=60, grants=[fable()]), LAST_RESORT) is None


class TestUnknownNeverAutomatic:
    @pytest.mark.parametrize("settings", MODES)
    def test_unknown_shape(self, settings):
        g = _grant(clears=["five_hour", FABLE_KEY], gives_back=False, type=None)
        usage = _usage(h5=10, fable=100, d7=30, grants=[g])
        assert _one(usage, replace(settings, weekly_for_model_limit=True)) is None

    @pytest.mark.parametrize("settings", MODES)
    def test_a_window_qswap_does_not_know(self, settings):
        g = weekly(clears_unknown=["seven_day_newmodel"])
        assert _one(_usage(fable=10, d7=100, grants=[g]), settings) is None
        assert _one(_usage(fable=10, d7=100, grants=[weekly()]), settings) is not None


class TestRule6Codex:
    def test_a_full_credit_on_a_weekly_only_plan_acts_as_weekly(self):
        from tests.test_codex_reset_credits import _codex, _plan

        usage = _codex(100)
        assert usage["resets"]["grants"][0]["type"] == "full"
        plan = _plan(usage)
        assert plan is not None
        assert (plan.scope, plan.grant_kind, plan.noun) == ("weekly", "weekly", "weekly reset")

    def test_a_full_credit_with_both_windows_is_full(self):
        from tests.test_codex_reset_credits import _codex, _plan

        plan = _plan(_codex(97, 60))
        assert plan is not None and plan.scope == "full" and plan.noun == "full reset"

    def test_a_credit_is_not_spent_for_a_limit_it_does_not_clear(self):
        from tests.test_codex_reset_credits import NOW as CODEX_NOW, _codex, _plan

        usage = _codex(10)
        usage["scoped"] = [{"name": "daily", "pct": 100.0,
                            "resets_at": _iso_at(CODEX_NOW + 3 * DAY)}]
        assert _plan(usage) is None
        assert _plan(usage, auto="on-limit", weekly_for_model_limit=True) is None

    def test_the_confirmation_names_the_kind(self):
        from ai_quota_swap import codex_cli
        from tests.test_codex_reset_credits import NOW as CODEX_NOW, _codex

        usage = _codex(100)
        grant = usage["resets"]["grants"][0]
        text = codex_cli.describe_credit("2", "a@example.com", grant, usage, usage["resets"], CODEX_NOW)
        assert "  Weekly reset: clears all weekly usage" in text


# -- step 4: what people see ------------------------------------------------------------


def _far() -> float:
    return time.time() + 120 * DAY


class TestCountsByKind:
    def test_summary_counts_each_kind(self):
        far = _far()
        status = _usage(grants=[fable("a", ends=far), fable("b", ends=far), weekly("w", ends=far)])["resets"]
        now = time.time()
        assert resets.counts_by_kind(status, now) == [("Fable", 2), ("weekly", 1)]
        assert resets.summary(status, now).startswith("2 Fable · 1 weekly · use by ")
        assert resets.summary(status, now, counted=False).startswith("2 Fable · 1 weekly · use by ")

    def test_narrowest_kind_first_and_five_hour_named(self):
        far = _far()
        status = _usage(grants=[full("f", ends=far), session("s", ends=far, left=2)])["resets"]
        assert resets.held_by_kind(status, time.time()) == "2 five-hour · 1 full"

    def test_list_row(self):
        from ai_quota_swap.switcher import _usage_entry_lines

        far = _far()
        usage = _usage(fable=6, grants=[fable("a", ends=far), fable("b", ends=far), weekly("w", ends=far)])
        text = "\n".join(_usage_entry_lines(UsageEntry(last_good=usage, fetched_at=None, age_s=0.0)))
        assert "3    2 Fable · 1 weekly · use by" in text

    def test_tui_badge(self):
        from ai_quota_swap.tui.widgets import _resets_badge

        far = _far()
        mixed = _usage(grants=[fable("a", ends=far, left=2), weekly("w", ends=far)])
        assert _resets_badge(mixed, time.time()) == "resets: 2 Fable · 1 weekly"
        one_kind = _usage(grants=[fable("a", ends=far), fable("b", ends=far)])
        assert _resets_badge(one_kind, time.time()) == "2 resets"

    def test_menu_bar_row(self):
        # Held resets by kind, compact and last (menubar.held_compact).
        from ai_quota_swap.display import Reading
        from ai_quota_swap.menubar import AccountState, account_row

        far = _far()
        now = time.time()

        def row(usage, **kw):
            acc = AccountState("claude", "1", "a@example.com",
                               entry=Reading(last_good=usage, fetched_at=now), **kw)
            return account_row(acc, now=now, stale_after_s=900).text

        mixed = _usage(grants=[fable("a", ends=far, left=2), weekly("w", ends=far)])
        assert row(mixed).endswith(" · 2 Fable · 1 weekly resets")
        assert row(_usage(grants=[fable("a", ends=far)])).endswith(" · 1 Fable reset")
        assert "reset" not in row(_usage())
        # The login note stays last.
        due = row(mixed, login_expires_at=now + 3600)
        assert due.index("2 Fable") < due.index("re-login")


class TestEventWording:
    def _event(self, scope, kind, *, dry_run=False, result="reset"):
        plan = {"reason": "last-resort", "window": "7d", "pct": 100.0, "hoursLeft": 30.0,
                "grantKind": kind, "scope": scope}
        return ResetEvent(
            account={"number": 1, "email": "a@example.com"}, plan=plan, dry_run=dry_run,
            result=None if dry_run else ClaimResult(result).to_json(),
        )

    @pytest.mark.parametrize("scope, kind, noun", [
        ("model", "Fable", "Fable reset"),
        ("weekly", "weekly", "weekly reset"),
        ("session", "session", "5-hour reset"),
        ("full", "full", "full reset"),
    ])
    def test_each_kind_named(self, scope, kind, noun):
        assert self._event(scope, kind).human().startswith(f"Used a {noun} on #1 a@example.com")
        assert self._event(scope, kind, dry_run=True).human().startswith(f"Would use a {noun} on")
        assert f"Did not use a {noun}" in self._event(scope, kind, result="cooldown").human()

    def test_json_carries_scope(self):
        payload = self._event("weekly", "weekly").to_json()
        assert payload["scope"] == "weekly" and payload["plan"]["scope"] == "weekly"

    def test_plan_json_is_additive(self):
        plan = _one(_usage(fable=100, d7=60, grants=[fable()]), LAST_RESORT)
        assert isinstance(plan, ResetPlan)
        out = plan.to_json()
        assert out["scope"] == "model" and out["grantKind"] == "Fable"
        assert {"account", "grantId", "reason", "window", "pct", "projected"} <= set(out)


# -- qswap reset over a seeded switcher ---------------------------------------------------


def _live(*, d7: float, fable_pct: float, share: float | None = None, grants=None,
          next_id: str | None = None) -> dict:
    """A reading on the real clock (the command reads it)."""
    now = time.time()
    week = _iso_at(now + 5 * DAY)
    u: dict = {"seven_day": {"pct": d7, "resets_at": week}}
    scoped = {"name": "Fable", "pct": fable_pct, "resets_at": week}
    if share is not None:
        scoped["share_of_weekly"] = share
    u["scoped"] = [scoped]
    if grants is not None:
        u["resets"] = {
            "eligible": True, "ineligible_reason": None, "at_limit": False, "exhausted": [],
            "next_grant_id": next_id, "weekly_resets_at": week, "cooldown_until": None,
            "grants": grants,
        }
    return u


def _held() -> list[dict]:
    """One Fable and one weekly reset (the weekly listed first, as next)."""
    far = _far()
    return [weekly("wk", ends=far), fable("fb", ends=far)]


def _run(h, argv, usage, capsys, *, answer="n", tty=False):
    entries = {
        n: UsageEntry(last_good=u, fetched_at=time.time(), age_s=0.0) for n, u in usage.items()
    }
    claimed: list[tuple[str, str]] = []

    def claim(num, grant_id, request_id):
        claimed.append((num, grant_id))
        return ClaimResult("reset", resets_left=0, cleared=("seven_day",))

    with (
        patch.object(cli, "ClaudeAccountSwitcher", lambda debug=False: h.switcher),
        patch.object(h.switcher, "usage_entries_by_account", return_value=entries),
        patch.object(h.switcher, "refresh_usage", return_value=entries),
        patch.object(h.switcher, "refresh_account_usage", side_effect=entries.get),
        patch.object(h.switcher, "claim_usage_reset", side_effect=claim),
        patch("sys.stdin.isatty", return_value=tty),
        patch("ai_quota_swap.prompts.interactive", return_value=tty),
        patch("builtins.input", return_value=answer),
    ):
        try:
            cli._reset_command(argv)
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    out, err = capsys.readouterr()
    return code, out, err, claimed


class TestResetCommand:
    """Account 1 (active) holds one Fable and one weekly reset; account 2
    holds none and has no room on 7d either."""

    PEER = {"2": _live(d7=99, fable_pct=10)}

    def _list(self, h, capsys, account1):
        code, out, err, _ = _run(h, [], {"1": account1, **self.PEER}, capsys)
        assert code == 0, err
        return out

    def test_grant_lines_name_the_kind_grouped_narrowest_first(self, h, capsys):
        out = self._list(h, capsys, _live(d7=31, fable_pct=100, grants=_held(), next_id="wk"))
        fable_line = "1 Fable reset · clears Fable usage only"
        weekly_line = "1 weekly reset · clears all weekly usage · next"
        assert fable_line in out and weekly_line in out
        assert out.index(fable_line) < out.index(weekly_line)
        assert "usable now · use by" in out and "id wk" in out
        assert all(len(line) <= 80 for line in out.splitlines())

    def test_a_only_fable_at_its_limit_uses_the_fable_reset(self, h, capsys):
        out = self._list(h, capsys, _live(d7=31, fable_pct=100, grants=_held(), next_id="wk"))
        assert "Auto would now use a Fable reset on #1 a@example.com (Fable at 100%" in out
        assert "Auto keeps" not in out

    def test_b_weekly_at_its_limit_uses_the_weekly_reset(self, h, capsys):
        out = self._list(h, capsys, _live(d7=100, fable_pct=20, grants=_held(), next_id="wk"))
        assert "Auto would now use a weekly reset on #1 a@example.com (7d at 100%" in out

    def test_c_both_at_their_limits(self, h, capsys):
        # No share reported: only the weekly reset (which lists Fable) frees both.
        out = self._list(h, capsys, _live(d7=100, fable_pct=100, grants=_held(), next_id="wk"))
        assert "Auto would now use a weekly reset on #1 a@example.com (7d at 100%" in out
        # With Fable's share known, giving it back is enough: the Fable reset.
        out = self._list(h, capsys, _live(d7=100, fable_pct=100, share=45, grants=_held(), next_id="wk"))
        assert "Auto would now use a Fable reset on #1 a@example.com (Fable at 100%" in out

    def test_a_kept_weekly_reset_is_explained(self, h, capsys):
        out = self._list(h, capsys, _live(d7=31, fable_pct=100, grants=[weekly("wk", ends=_far())]))
        assert "Auto would not use a reset right now." in out
        assert ("Auto keeps the weekly reset on #1: only Fable is at its limit (100%), "
                "and a weekly reset clears all weekly usage") in " ".join(out.split())

    def test_no_explanation_once_the_setting_allows_it(self, h, capsys):
        from ai_quota_swap.settings import set_setting

        set_setting(h.switcher.backup_dir, "resets.weeklyForModelLimit", "true")
        out = self._list(h, capsys, _live(d7=31, fable_pct=100, grants=[weekly("wk", ends=_far())]))
        assert "Auto would now use a weekly reset on #1 a@example.com (Fable at 100%" in out

    def test_json_carries_scope_and_the_setting(self, h, capsys):
        import json

        code, out, _, _ = _run(
            h, ["--json"], {"1": _live(d7=31, fable_pct=100, grants=_held()), **self.PEER}, capsys,
        )
        payload = json.loads(out)
        assert payload["auto"]["weeklyForModelLimit"] is False
        grants = {g["id"]: g for g in payload["accounts"][0]["resets"]["grants"]}
        assert (grants["fb"]["scope"], grants["wk"]["scope"]) == ("model", "weekly")
        assert payload["plan"]["scope"] == "model"

    def test_manual_weekly_says_what_it_trades(self, h, capsys):
        usage = {"1": _live(d7=31, fable_pct=100, grants=_held(), next_id="wk"), **self.PEER}
        code, out, _, claimed = _run(h, ["1", "--grant", "wk"], usage, capsys, answer="n", tty=True)
        assert code == 0 and claimed == []
        assert "#1 a@example.com · weekly reset" in out and "id wk" in out
        assert "  Clears all weekly usage" in out
        assert "This weekly reset clears all weekly usage; only Fable is at its limit (7d is at 31%)." in out
        assert "Kept; nothing was spent." in out

    def test_manual_default_is_the_narrowest_useful_reset(self, h, capsys):
        usage = {"1": _live(d7=31, fable_pct=100, grants=_held(), next_id="wk"), **self.PEER}
        code, out, err, claimed = _run(h, ["1", "--yes"], usage, capsys)
        assert code == 0, err
        assert claimed == [("1", "fb")]
        assert "Used a Fable reset on #1 a@example.com" in out

    def test_manual_weekly_is_not_restricted(self, h, capsys):
        usage = {"1": _live(d7=31, fable_pct=100, grants=_held(), next_id="wk"), **self.PEER}
        code, out, err, claimed = _run(h, ["1", "--grant", "wk", "--yes"], usage, capsys)
        assert code == 0, err
        assert claimed == [("1", "wk")]
        assert "Used a weekly reset on #1 a@example.com: 7d refilled" in out

    def test_manual_default_for_the_weekly_limit(self, h, capsys):
        usage = {"1": _live(d7=99, fable_pct=20, grants=_held(), next_id="fb"), **self.PEER}
        _, _, _, claimed = _run(h, ["1", "--yes"], usage, capsys)
        assert claimed == [("1", "wk")]
