"""Tests for usage resets (resets.py) and their engine/CLI integration."""

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import clis
from ai_quota_swap import oauth, resets
from ai_quota_swap.autoswitch import (
    MAX_AUTO_RESETS_PER_DAY,
    ResetEvent,
    SwitchEvent,
    TickOutcome,
    record_reset_claim,
    reset_claim_settled,
    reset_request_id,
)
from ai_quota_swap.resets import AccountView, ClaimResult, plan_reset
from ai_quota_swap.settings import ResetSettings, load_reset_settings, set_setting
from ai_quota_swap.usage_store import UsageEntry

from tests.test_autoswitch import EngineHarness, _iso_at

NOW = 1_000_000.0
DAY = 86400.0


# -- fixtures ------------------------------------------------------------------

# The cedar_ember block exactly as the usage endpoint answered a Max account
# (Claude Code surface), trimmed to two grants.
RAW_STATUS = {
    "eligible": True,
    "ineligible_reason": None,
    "at_limit": False,
    "exhausted": [],
    "grants": [
        {
            "id": "launch-2610-fable",
            "label": "New model launch: one Fable reset for Max, Team Premium and Enterprise Premium",
            "resets_total": 1,
            "resets_left": 1,
            "starts_at": "2026-11-09T18:00:00+00:00",
            "ends_at": "2027-03-09T07:59:00+00:00",
            "clears": ["seven_day_overage_included"],
            "paused": False,
            "usable_now": True,
            "use_requires_limit": False,
            "percent_used": {"seven_day_overage_included": 7, "seven_day": 30},
            "blocking": [],
            "arm": None,
            "type": "full",
            "unavailable_reason": None,
            "gives_back_weekly": True,
        },
        {
            "id": "launch-2610-fable-12mo",
            "label": "New model launch: one more Fable reset at 12 months' tenure",
            "resets_total": 1,
            "resets_left": 1,
            "starts_at": "2026-11-09T18:00:00+00:00",
            "ends_at": "2027-03-09T07:59:00+00:00",
            "clears": ["seven_day_overage_included"],
            "paused": False,
            "usable_now": True,
            "use_requires_limit": False,
            "percent_used": {"seven_day_overage_included": 7, "seven_day": 30},
            "blocking": [],
            "type": "full",
            "unavailable_reason": None,
            "gives_back_weekly": True,
        },
    ],
    "next_grant_id": "launch-2610-fable",
    "weekly_resets_at": "2026-11-15T08:00:00+00:00",
    "cooldown_until": None,
    "event_props": {"surface": "claude_code_cli", "tier": "claude_max_20x"},
}


def _grant(
    gid: str = "g-fable",
    *,
    clears=("seven_day_overage_included",),
    left: int = 1,
    ends: float | None = NOW + 120 * DAY,
    usable: bool = True,
    gives_back: bool = True,
    percent: dict | None = None,
    **extra,
) -> dict:
    g = {
        "id": gid,
        "label": "",
        "resets_left": left,
        "resets_total": max(left, 1),
        "starts_at": None,
        "ends_at": _iso_at(ends) if ends is not None else None,
        "clears": list(clears),
        "blocking": [],
        "paused": False,
        "usable_now": usable,
        "use_requires_limit": False,
        "gives_back_weekly": gives_back,
        "type": "full",
        "unavailable_reason": None,
        "percent_used": dict(percent or {}),
    }
    g.update(extra)
    return g


def _usage(
    *,
    h5: float | None = None,
    d7: float = 30.0,
    fable: float | None = None,
    share: float | None = None,
    reset_in: float = 5 * DAY,
    grants: list[dict] | None = None,
    next_id: str | None = None,
    eligible: bool = True,
    cooldown: float | None = None,
) -> dict:
    resets_at = _iso_at(NOW + reset_in)
    u: dict = {"seven_day": {"pct": d7, "resets_at": resets_at}}
    if h5 is not None:
        u["five_hour"] = {"pct": h5, "resets_at": _iso_at(NOW + 3 * 3600)}
    if fable is not None:
        scoped = {"name": "Fable", "pct": fable, "resets_at": resets_at}
        if share is not None:
            scoped["share_of_weekly"] = share
        u["scoped"] = [scoped]
    if grants is not None:
        u["resets"] = {
            "eligible": eligible,
            "ineligible_reason": None if eligible else "tier",
            "at_limit": False,
            "exhausted": [],
            "next_grant_id": next_id,
            "weekly_resets_at": resets_at,
            "cooldown_until": _iso_at(cooldown) if cooldown else None,
            "grants": grants,
        }
    return u


def _views(usage: dict[str, dict], *, threshold: float = 90.0, can_claim=None) -> list[AccountView]:
    allowed = set(usage) if can_claim is None else set(can_claim)
    return [
        AccountView(num=n, usage=u, threshold=threshold, can_claim=n in allowed)
        for n, u in usage.items()
    ]


LAST_RESORT = ResetSettings()
ON_LIMIT = ResetSettings(auto="on-limit")


# -- status parsing -------------------------------------------------------------


class TestParseStatus:
    def test_real_block_normalizes(self):
        status = resets.parse_status(RAW_STATUS)
        assert status["eligible"] is True
        assert status["next_grant_id"] == "launch-2610-fable"
        assert [g["id"] for g in status["grants"]] == [
            "launch-2610-fable", "launch-2610-fable-12mo",
        ]
        g = status["grants"][0]
        assert g["clears"] == ["seven_day_overage_included"]
        assert g["gives_back_weekly"] is True
        assert g["usable_now"] is True
        assert g["use_requires_limit"] is False
        assert g["percent_used"] == {"seven_day_overage_included": 7.0, "seven_day": 30.0}
        assert "event_props" not in status and "arm" not in g

    def test_ineligible_block_is_still_a_status(self):
        status = resets.parse_status({"eligible": False, "ineligible_reason": "surface", "grants": []})
        assert status == {
            "eligible": False, "ineligible_reason": "surface", "at_limit": False,
            "exhausted": [], "next_grant_id": None, "weekly_resets_at": None,
            "cooldown_until": None, "grants": [],
        }

    @pytest.mark.parametrize("block", [None, [], "x", {}, {"eligible": "yes"}])
    def test_non_status_is_none(self, block):
        assert resets.parse_status(block) is None

    def test_malformed_grants_dropped_one_by_one(self):
        block = dict(RAW_STATUS, grants=[
            {"id": "BAD ID", "resets_left": 1},
            {"id": "ok", "resets_left": -1},
            {"id": "ok2", "resets_left": True},
            RAW_STATUS["grants"][1],
        ])
        status = resets.parse_status(block)
        assert [g["id"] for g in status["grants"]] == ["launch-2610-fable-12mo"]
        # The server's next pick no longer exists among parsed grants.
        assert status["next_grant_id"] is None

    def test_missing_use_requires_limit_is_conservative(self):
        raw = dict(RAW_STATUS["grants"][0])
        del raw["use_requires_limit"]
        status = resets.parse_status(dict(RAW_STATUS, grants=[raw]))
        assert status["grants"][0]["use_requires_limit"] is True

    def test_unknown_window_keys_dropped(self):
        raw = dict(RAW_STATUS["grants"][0], clears=["seven_day", "mystery"])
        status = resets.parse_status(dict(RAW_STATUS, grants=[raw]))
        assert status["grants"][0]["clears"] == ["seven_day"]


class TestBuildUsageResult:
    def _data(self, **extra):
        data = {
            "five_hour": None,
            "seven_day": {"utilization": 30.0, "resets_at": "2026-11-15T08:00:00+00:00"},
            "limits": [
                {"kind": "weekly_all", "percent": 30, "resets_at": "2026-11-15T08:00:00+00:00", "scope": None},
                {"kind": "weekly_scoped", "percent": 7, "resets_at": "2026-11-15T08:00:00+00:00",
                 "scope": {"model": {"display_name": "Fable"}}},
            ],
            "weekly_scoped_shares": [
                {"limit_index": 1, "used_percent_of_weekly": 3, "allowance_percent_of_weekly": 50},
            ],
        }
        data.update(extra)
        return data

    def test_resets_and_share_carried(self):
        result = oauth.build_usage_result(self._data(cedar_ember=RAW_STATUS))
        assert result["resets"]["next_grant_id"] == "launch-2610-fable"
        assert result["scoped"][0]["share_of_weekly"] == 3.0

    def test_absent_block_adds_nothing(self):
        result = oauth.build_usage_result(self._data())
        assert "resets" not in result

    def test_block_alone_is_not_a_reading(self):
        assert oauth.build_usage_result({"cedar_ember": RAW_STATUS}) is None


class TestUsageRequest:
    def _capture(self, **kwargs):
        seen = {}

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["agent"] = req.get_header("User-agent")
            return _Resp(b"{}")

        with patch("urllib.request.urlopen", fake_urlopen):
            oauth.request_usage_data("tok", **kwargs)
        return seen

    def test_plain_request_unchanged(self):
        seen = self._capture()
        assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
        assert seen["agent"] == "ai-quota-swap/1.0"

    def test_resets_read_uses_cli_surface(self, monkeypatch):
        monkeypatch.setattr(resets, "_cli_version_cache", None)
        monkeypatch.setenv("QSWAP_CLAUDE_CLI_VERSION", "2.1.400")
        seen = self._capture(resets=True)
        assert seen["url"] == "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
        assert seen["agent"] == "claude-cli/2.1.400 (external, cli)"


class TestCliVersion:
    def test_native_install_binary_name(self, monkeypatch, tmp_path):
        monkeypatch.setattr(resets, "_cli_version_cache", None)
        monkeypatch.delenv("QSWAP_CLAUDE_CLI_VERSION", raising=False)
        versions = tmp_path / "versions"
        versions.mkdir()
        binary = versions / "2.1.500"
        binary.write_text("")
        link = tmp_path / "claude"
        link.symlink_to(binary)
        monkeypatch.setattr(clis, "find_cli", lambda _name: str(link))
        assert resets.detect_cli_version() == "2.1.500"

    def test_fallback(self, monkeypatch, tmp_path):
        monkeypatch.setattr(resets, "_cli_version_cache", None)
        monkeypatch.delenv("QSWAP_CLAUDE_CLI_VERSION", raising=False)
        monkeypatch.setattr(clis, "find_cli", lambda _name: None)
        monkeypatch.setattr(resets.Path, "home", lambda: tmp_path)
        assert resets.detect_cli_version() == resets.FALLBACK_CLI_VERSION


# -- claim ----------------------------------------------------------------------


class TestClaim:
    ORG = "00000000-0000-4000-8000-00000000000a"

    def _run(self, response=None, error=None):
        seen = {}

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["body"] = json.loads(req.data)
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            if error is not None:
                raise error
            return _Resp(json.dumps(response).encode())

        with patch("urllib.request.urlopen", fake_urlopen):
            result = resets.claim("tok", self.ORG, "launch-2610-fable", "req123")
        return result, seen

    def test_wire_format(self):
        result, seen = self._run({
            "result": "reset", "reason": None, "reset": True, "resets_left": 0,
            "cleared": ["seven_day_overage_included"],
            "weekly_resets_at": "2026-11-15T08:00:00+00:00",
        })
        assert seen["url"] == f"https://api.anthropic.com/api/organizations/{self.ORG}/reset_rate_limits"
        assert seen["method"] == "POST"
        assert seen["body"] == {
            "program": "cedar_ember", "grant_id": "launch-2610-fable", "request_id": "req123",
        }
        assert seen["headers"]["authorization"] == "Bearer tok"
        assert seen["headers"]["anthropic-beta"] == oauth.OAUTH_BETA_HEADER
        assert seen["headers"]["user-agent"].startswith("claude-cli/")
        assert result.ok and result.settled
        assert result.resets_left == 0
        assert result.cleared == ("seven_day_overage_included",)

    def test_unknown_grant_answer(self):
        # The live server's answer for a grant id that does not exist.
        result, _ = self._run({
            "result": "ineligible", "reason": "unknown_grant", "reset": False,
            "grant_id": "x", "resets_left": None, "cleared": [],
        })
        assert not result.ok and result.settled
        assert result.reason == "unknown_grant"

    def test_unrecognized_result_is_unavailable(self):
        result, _ = self._run({"result": "something-new"})
        assert result.result == "unavailable" and not result.settled

    @pytest.mark.parametrize("code,expected", [(429, "rate_limited"), (401, "auth_error"), (403, "auth_error"), (500, "error")])
    def test_http_errors(self, code, expected):
        err = urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))
        result, _ = self._run(error=err)
        assert result.result == expected and not result.settled

    def test_network_error(self):
        result, _ = self._run(error=urllib.error.URLError("down"))
        assert result.result == "error" and result.detail == "network"

    @pytest.mark.parametrize("grant,req,org", [
        ("BAD ID", "r", ORG), ("ok", "bad id!", ORG), ("ok", "r", ""), ("ok", "r", "../x"),
    ])
    def test_malformed_ids_never_sent(self, grant, req, org):
        with patch("urllib.request.urlopen") as urlopen:
            result = resets.claim("tok", org, grant, req)
        urlopen.assert_not_called()
        assert result.result == "error"


# -- display helpers -------------------------------------------------------------


class TestDisplay:
    def test_summary_counts_and_expiry(self):
        status = resets.parse_status(RAW_STATUS)
        now = resets.parse_ts("2026-11-10T00:00:00+00:00")
        assert resets.summary(status, now).startswith("2 Fable · use by Mar ")
        assert resets.summary(status, now, counted=False).startswith("Fable · use by Mar ")
        assert resets.total_left(status, now) == 2

    def test_summary_none_without_resets(self):
        assert resets.summary(None, NOW) is None
        assert resets.summary({"eligible": True, "grants": [_grant(left=0)]}, NOW) is None
        assert resets.summary({"eligible": True, "grants": [_grant(ends=NOW - 1)]}, NOW) is None

    def test_kinds(self):
        assert resets.grant_kind(_grant(clears=["five_hour"])) == "session"
        assert resets.grant_kind(_grant(clears=["seven_day"])) == "weekly"
        assert resets.grant_kind(_grant(clears=["five_hour", "seven_day"])) == "full"
        assert resets.grant_kind(_grant()) == "Fable"

    def test_unusable_reasons(self):
        assert resets.grant_unusable_reason(_grant(), NOW) is None
        assert resets.grant_unusable_reason(_grant(paused=True), NOW) == "paused"
        assert resets.grant_unusable_reason(_grant(blocking=["seven_day"]), NOW) == "blocked by 7d"
        assert resets.grant_unusable_reason(_grant(usable=False, use_requires_limit=True), NOW) == "needs a limit hit first"


# -- planner ---------------------------------------------------------------------


class TestPlannerLastResort:
    def test_active_fable_maxed_unwatched_window_is_stuck(self):
        # The engine does not watch Fable (autoswitch.model unset), so no
        # switch will ever relieve it: the active account's own reset is used.
        usage = {
            "1": _usage(fable=100, d7=60, grants=[_grant(percent={"seven_day_overage_included": 100})]),
            "2": _usage(fable=10, d7=10),
        }
        plan = plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW)
        assert plan is not None
        assert (plan.account, plan.grant_id, plan.reason, plan.window) == ("1", "g-fable", "last-resort", "Fable")
        assert plan.pct == 100

    def test_watched_window_with_room_elsewhere_keeps_the_reset(self):
        usage = {
            "1": _usage(fable=100, d7=60, grants=[_grant()]),
            "2": _usage(fable=10, d7=10),
        }
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, watched=("Fable",), now=NOW) is None

    def test_watched_window_all_full_is_stuck(self):
        usage = {
            "1": _usage(fable=100, d7=60, grants=[_grant()]),
            "2": _usage(fable=95, d7=10),
            "3": "token-expired",  # unknown usage is no room
        }
        plan = plan_reset(_views(usage), current="1", settings=LAST_RESORT, watched=("Fable",), now=NOW)
        assert plan is not None and plan.account == "1"

    def test_below_line_does_nothing(self):
        usage = {"1": _usage(fable=80, grants=[_grant()])}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_window_refilling_soon_is_skipped(self):
        usage = {"1": _usage(fable=100, reset_in=10 * 3600, grants=[_grant()])}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None
        relaxed = replace(LAST_RESORT, min_hours_left=6)
        assert plan_reset(_views(usage), current="1", settings=relaxed, now=NOW) is not None

    def test_weekly_still_full_after_model_reset_is_useless(self):
        usage = {"1": _usage(fable=100, d7=98, grants=[_grant()])}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_give_back_credits_the_model_share(self):
        # 7d 98% but Fable used 40 points of it: the reset gives them back.
        usage = {"1": _usage(fable=100, d7=98, share=40, grants=[_grant()])}
        plan = plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW)
        assert plan is not None
        assert plan.projected == {"7d": 58.0, "Fable": 0.0}

    def test_no_give_back_without_the_flag(self):
        usage = {"1": _usage(fable=100, d7=98, share=40, grants=[_grant(gives_back=False)])}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_stuck_active_without_grant_refills_a_landing_peer(self):
        # Active #1 is out of 7d and holds nothing; #2 is stuck on Fable but a
        # Fable reset (with give-back) makes it a valid switch target.
        usage = {
            "1": _usage(fable=50, d7=99),
            "2": _usage(fable=100, d7=70, share=45, grants=[_grant()]),
        }
        plan = plan_reset(
            _views(usage), current="1", settings=LAST_RESORT, watched=("Fable",), now=NOW
        )
        assert plan is not None and plan.account == "2" and plan.reason == "last-resort"

    def test_refilled_peer_must_land(self):
        # Same, but #2's weekly stays over the switch line: refilling it would
        # not give the engine anywhere to go.
        usage = {
            "1": _usage(fable=50, d7=99),
            "2": _usage(fable=100, d7=94, share=1, grants=[_grant()]),
        }
        assert plan_reset(
            _views(usage), current="1", settings=LAST_RESORT, watched=("Fable",), now=NOW
        ) is None

    def test_settling_active_blocks_a_peer_refill(self):
        # Active #1 was just refilled but its re-read failed: the stale 99%
        # must not read as stuck and spend #2's reset too.
        usage = {
            "1": _usage(fable=50, d7=99),
            "2": _usage(fable=100, d7=70, share=45, grants=[_grant()]),
        }
        views = [
            AccountView("1", usage["1"], 90.0, can_claim=False, settling=True),
            AccountView("2", usage["2"], 90.0),
        ]
        assert plan_reset(views, current="1", settings=LAST_RESORT, watched=("Fable",), now=NOW) is None

    def test_settling_peer_counts_as_room(self):
        # #2 was just refilled (stale reading still full): the active's own
        # reset waits for #2's reading instead of being spent alongside.
        usage = {
            "1": _usage(fable=100, d7=60, grants=[_grant()]),
            "2": _usage(fable=100, d7=60),
        }
        views = [
            AccountView("1", usage["1"], 90.0),
            AccountView("2", usage["2"], 90.0, can_claim=False, settling=True),
        ]
        assert plan_reset(views, current="1", settings=LAST_RESORT, watched=("Fable",), now=NOW) is None

    def test_views_from_settling_never_claims(self):
        views = resets.views_from(
            {"1": {}, "2": {}}, claimable=["1", "2"], thresholds={"1": 90.0, "2": 90.0},
            weekly_threshold=None, accounts=["1", "2"], settling=["2"],
        )
        assert [(v.can_claim, v.settling) for v in views] == [(True, False), (False, True)]

    def test_cannot_claim_accounts_never_spend(self):
        usage = {"1": _usage(fable=100, grants=[_grant()])}
        assert plan_reset(_views(usage, can_claim=()), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_off(self):
        usage = {"1": _usage(fable=100, grants=[_grant()])}
        assert plan_reset(_views(usage), current="1", settings=replace(LAST_RESORT, auto="off"), now=NOW) is None


class TestPlannerOnLimit:
    def test_inactive_account_at_limit_is_refilled(self):
        usage = {
            "1": _usage(fable=10, d7=20),
            "2": _usage(fable=100, d7=60, grants=[_grant()]),
        }
        plan = plan_reset(_views(usage), current="1", settings=ON_LIMIT, now=NOW)
        assert plan is not None and (plan.account, plan.reason) == ("2", "on-limit")

    def test_active_preferred_then_most_time_left(self):
        usage = {
            "1": _usage(fable=100, reset_in=2 * DAY, grants=[_grant()]),
            "2": _usage(fable=100, reset_in=6 * DAY, grants=[_grant()]),
            "3": _usage(fable=100, reset_in=4 * DAY, grants=[_grant()]),
        }
        assert plan_reset(_views(usage), current="1", settings=ON_LIMIT, now=NOW).account == "1"
        assert plan_reset(_views(usage), current="9", settings=ON_LIMIT, now=NOW).account == "2"


ACTIVE_100 = ResetSettings(auto="active", threshold=100.0)


class TestPlannerActive:
    def test_active_at_limit_spends_even_with_room_elsewhere(self):
        usage = {
            "1": _usage(fable=100, d7=60, grants=[_grant()]),
            "2": _usage(fable=0, d7=0),
        }
        plan = plan_reset(_views(usage), current="1", settings=ACTIVE_100, watched=("Fable",), now=NOW)
        assert plan is not None and (plan.account, plan.reason) == ("1", "active-limit")

    def test_below_the_limit_waits(self):
        usage = {"1": _usage(fable=99, grants=[_grant()])}
        assert plan_reset(_views(usage), current="1", settings=ACTIVE_100, now=NOW) is None

    def test_never_an_inactive_account(self):
        usage = {
            "1": _usage(fable=10, d7=10),
            "2": _usage(fable=100, d7=60, grants=[_grant()]),
        }
        assert plan_reset(_views(usage), current="1", settings=ACTIVE_100, now=NOW) is None

    def test_still_needs_a_useful_refill(self):
        # Fable at 100 but the all-models weekly is at 100 too, with no share
        # to give back: the reset would not unblock the account.
        usage = {"1": _usage(fable=100, d7=100, grants=[_grant()])}
        assert plan_reset(_views(usage), current="1", settings=ACTIVE_100, now=NOW) is None


class TestPlannerGrants:
    def test_next_grant_first_then_soonest_expiry(self):
        grants = [
            _grant("late", ends=NOW + 100 * DAY),
            _grant("soon", ends=NOW + 60 * DAY),
            _grant("next", ends=NOW + 110 * DAY),
        ]
        usage = {"1": _usage(fable=100, grants=grants, next_id="next")}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW).grant_id == "next"
        usage = {"1": _usage(fable=100, grants=grants)}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW).grant_id == "soon"

    @pytest.mark.parametrize("grant", [
        _grant(usable=False),
        _grant(paused=True),
        _grant(blocking=["seven_day"]),
        _grant(unavailable_reason="cooldown"),
        _grant(left=0),
        _grant(ends=NOW - 60),
        _grant(clears=["seven_day_cowork"]),
    ])
    def test_unusable_grants_skipped(self, grant):
        usage = {"1": _usage(fable=100, grants=[grant])}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_server_cooldown_skips_account(self):
        usage = {"1": _usage(fable=100, grants=[_grant()], cooldown=NOW + 3600)}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_ineligible_skips_account(self):
        usage = {"1": _usage(fable=100, grants=[_grant()], eligible=False)}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_server_percent_preferred_over_window(self):
        usage = {"1": _usage(fable=50, grants=[_grant(percent={"seven_day_overage_included": 99})])}
        plan = plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW)
        assert plan is not None and plan.pct == 99


class TestPlannerExpiry:
    def test_runway_scales_with_resets_held(self):
        # Three resets expiring together in 15 days: 3 × 7d = 21d of runway,
        # so they count as expiring and are used from 50% even though nothing
        # is stuck (last-resort would otherwise keep them).
        grants = [_grant(f"g{i}", ends=NOW + 15 * DAY) for i in range(3)]
        usage = {
            "1": _usage(fable=60, grants=grants),
            "2": _usage(fable=0, d7=0),
        }
        plan = plan_reset(_views(usage), current="1", settings=LAST_RESORT, watched=("Fable",), now=NOW)
        assert plan is not None and plan.reason == "expiring"

    def test_single_reset_outside_runway_is_kept(self):
        usage = {"1": _usage(fable=60, grants=[_grant(ends=NOW + 15 * DAY)])}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_expiry_guard_off(self):
        grants = [_grant(f"g{i}", ends=NOW + 2 * DAY) for i in range(3)]
        usage = {"1": _usage(fable=60, grants=grants)}
        settings = replace(LAST_RESORT, expiry_days=0)
        assert plan_reset(_views(usage), current="1", settings=settings, now=NOW) is None

    def test_expiring_below_its_line_is_kept(self):
        grants = [_grant(ends=NOW + 2 * DAY)]
        usage = {"1": _usage(fable=40, grants=grants)}
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None


# -- settings ------------------------------------------------------------------


class TestResetSettings:
    def test_defaults(self, tmp_path):
        assert load_reset_settings(tmp_path) == ResetSettings()

    def test_set_and_load(self, tmp_path):
        set_setting(tmp_path, "resets.auto", "on-limit")
        set_setting(tmp_path, "resets.threshold", "98")
        set_setting(tmp_path, "resets.enabled", "false")
        loaded = load_reset_settings(tmp_path)
        assert (loaded.auto, loaded.threshold, loaded.enabled) == ("on-limit", 98.0, False)

    def test_bad_values_fall_back_field_by_field(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "resets": {"auto": "always", "threshold": 500, "minHoursLeft": "x", "enabled": "no"},
        }))
        loaded = load_reset_settings(tmp_path)
        assert loaded.auto == "last-resort"
        assert loaded.threshold == 100.0  # clamped
        assert loaded.min_hours_left == 24.0
        assert loaded.enabled is False


# -- claim bookkeeping -----------------------------------------------------------


class TestClaimRecords:
    def test_settled_needs_a_later_reading_and_a_gap(self):
        rec = {"at": NOW, "result": "reset"}
        assert reset_claim_settled(None, None, NOW)
        assert not reset_claim_settled(rec, NOW - 1, NOW + 3600)  # reading predates claim
        assert not reset_claim_settled(rec, NOW + 5, NOW + 60)  # too soon after success
        assert reset_claim_settled(rec, NOW + 5, NOW + 3600)
        failed = {"at": NOW, "result": "error"}
        assert reset_claim_settled(failed, NOW + 5, NOW + 601)

    def test_request_id_reused_only_for_unconfirmed_same_grant(self):
        pending = {"grantId": "g", "requestId": "keep", "settled": False}
        assert reset_request_id(pending, "g") == "keep"
        assert reset_request_id(pending, "other") != "keep"
        assert reset_request_id({**pending, "settled": True}, "g") != "keep"
        assert resets.REQUEST_ID_RE.match(reset_request_id(None, "g"))

    def test_record_and_log(self, tmp_path):
        record_reset_claim(tmp_path, "2", {"at": NOW, "grantId": "g", "result": "reset", "auto": True})
        record_reset_claim(tmp_path, "3", {"at": NOW, "grantId": "g", "result": "cooldown", "auto": True})
        state = json.loads((tmp_path / "autoswitch_state.json").read_text())
        assert set(state["resetClaims"]) == {"2", "3"}
        assert state["resetLog"] == [
            {"at": NOW, "account": "2", "grantId": "g", "result": "reset", "auto": True},
        ]


# -- engine integration ----------------------------------------------------------


def _engine_usage(**kw) -> dict:
    """Engine-harness usage at the harness's clock (NOW == FakeClock default)."""
    return _usage(**kw)


@pytest.fixture
def h(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home)
    for n, email in ((1, "a@example.com"), (2, "b@example.com")):
        h.seed(n, email)
    h.make_live("a@example.com", 1)
    data = h.switcher._get_sequence_data()
    for n in ("1", "2"):
        data["accounts"][n]["organizationUuid"] = f"org-{n}-00000000"
    h.switcher._write_json(h.switcher.sequence_file, data)
    # The live login must carry the same organization, or the composite
    # (email, org) key reads it as an unmanaged login.
    (h.temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": "a@example.com",
            "accountUuid": "uuid-1",
            "organizationUuid": "org-1-00000000",
        },
    }))
    return h


def _entries(usage: dict, now: float) -> dict[str, UsageEntry]:
    return {n: UsageEntry(last_good=u, fetched_at=now, age_s=0.0) for n, u in usage.items()}


class TestEngine:
    def _tick(
        self, h, before: dict, after: dict | None = None, result: ClaimResult | None = None,
        *, on_refresh=None, stale_refresh: bool = False,
    ):
        calls = []
        state = {"claimed": False}

        def entries_by_account(*a, **k):
            return _entries(after if state["claimed"] and after else before, h.clock.now)

        def claim(num, grant_id, request_id):
            calls.append((num, grant_id, request_id))
            state["claimed"] = True
            return result or ClaimResult("reset", resets_left=0, cleared=("seven_day_overage_included",))

        def refresh(nums):
            if on_refresh is not None:
                on_refresh()
            fresh = _entries(before, h.clock.now)
            if stale_refresh:
                fresh = _entries(before, h.clock.now - 3600)
            return fresh

        with (
            patch.object(h.switcher, "usage_entries_by_account", side_effect=entries_by_account),
            patch.object(h.switcher, "refresh_usage", side_effect=refresh),
            patch.object(h.switcher, "claim_usage_reset", side_effect=claim),
        ):
            outcome = h.engine.tick()
        return outcome, calls

    def test_stuck_active_uses_reset_then_stays(self, h):
        before = {
            "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant()]),
            "2": _engine_usage(fable=0, d7=93),
        }
        after = {
            "1": _engine_usage(fable=0, d7=55, grants=[_grant(left=0)]),
            "2": _engine_usage(fable=0, d7=93),
        }
        outcome, calls = self._tick(h, before, after)
        assert [c[:2] for c in calls] == [("1", "g-fable")]
        event = next(e for e in h.events if isinstance(e, ResetEvent))
        assert event.used and event.plan["reason"] == "last-resort"
        assert "Used a Fable reset on #1 a@example.com" in event.human()
        # The refill lands in the same tick: below its line, so no switch.
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        state = h.state()
        assert state["resetClaims"]["1"]["result"] == "reset"
        assert state["resetLog"][0]["account"] == "1"

    def test_no_second_claim_on_the_pre_claim_reading(self, h):
        before = {
            "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant()]),
            "2": _engine_usage(fable=0, d7=93),
        }
        _, calls = self._tick(h, before, result=ClaimResult("unavailable"))
        assert len(calls) == 1
        h.clock.advance(60)
        # Same stale reading (fetched before the claim): held out.
        with (
            patch.object(h.switcher, "usage_entries_by_account",
                         return_value=_entries(before, h.clock.now - 120)),
            patch.object(h.switcher, "refresh_usage",
                         return_value=_entries(before, h.clock.now - 120)),
            patch.object(h.switcher, "claim_usage_reset") as claim,
        ):
            h.engine.tick()
        claim.assert_not_called()

    def test_unconfirmed_claim_retries_with_same_request_id(self, h):
        before = {
            "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant()]),
            "2": _engine_usage(fable=0, d7=93),
        }
        _, first = self._tick(h, before, result=ClaimResult("error", detail="network"))
        h.clock.advance(11 * 60)
        _, second = self._tick(h, before)
        assert first[0][2] == second[0][2]

    def test_dry_run_never_claims(self, h):
        h.engine = h._make_engine(dry_run=True)
        before = {
            "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant()]),
            "2": _engine_usage(fable=0, d7=93),
        }
        _, calls = self._tick(h, before)
        assert calls == []
        event = next(e for e in h.events if isinstance(e, ResetEvent))
        assert event.dry_run and event.human().startswith("Would use a Fable reset")
        assert event.human().endswith("(dry run)")
        assert "resetClaims" not in h.state()

    def _active_reset_fleet(self, h):
        # `resets.auto active` spends a reset on a stuck active account even
        # with a healthy peer to switch to.
        set_setting(h.switcher.backup_dir, "resets.auto", "active")
        before = {
            "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant()]),
            "2": _engine_usage(fable=0, d7=10),
        }
        after = {
            "1": _engine_usage(fable=0, d7=55, grants=[_grant(left=0)]),
            "2": _engine_usage(fable=0, d7=10),
        }
        return before, after

    def test_a_dry_run_that_would_use_a_reset_does_not_also_switch(self, h):
        h.engine = h._make_engine(dry_run=True)
        before, _after = self._active_reset_fleet(h)
        outcome, calls = self._tick(h, before)
        assert calls == []
        resets_planned = [e for e in h.events if isinstance(e, ResetEvent)]
        assert len(resets_planned) == 1 and resets_planned[0].dry_run
        # The tick ends at the planned reset, as a real one that refilled
        # the active account would stay on it.
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_the_same_tick_live_uses_the_reset_and_stays(self, h):
        before, after = self._active_reset_fleet(h)
        outcome, calls = self._tick(h, before, after)
        assert [c[:2] for c in calls] == [("1", "g-fable")]
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_a_dry_run_with_no_reset_to_plan_still_decides(self, h):
        # Nothing to plan: the dry-run tick goes on to its switch decision.
        h.engine = h._make_engine(dry_run=True)
        before = {
            "1": _engine_usage(d7=95),
            "2": _engine_usage(d7=10),
        }
        outcome, _calls = self._tick(h, before)
        assert not any(isinstance(e, ResetEvent) for e in h.events)
        assert any(isinstance(e, SwitchEvent) for e in h.events)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 1  # a dry run switches nothing

    def test_switch_preferred_when_it_helps(self, h):
        # Active 7d at 95% holds a weekly reset, but #2 has room: last-resort
        # keeps the reset and the engine switches.
        before = {
            "1": _engine_usage(d7=95, grants=[_grant(clears=["seven_day"], percent={"seven_day": 95})]),
            "2": _engine_usage(d7=10),
        }
        outcome, calls = self._tick(h, before)
        assert calls == []
        assert outcome is TickOutcome.SWITCHED
        assert any(isinstance(e, SwitchEvent) for e in h.events)

    def test_daily_brake(self, h):
        for i in range(MAX_AUTO_RESETS_PER_DAY):
            record_reset_claim(
                h.switcher.backup_dir, "2",
                {"at": h.clock.now - 60 * (i + 1), "grantId": "g", "result": "reset", "auto": True},
            )
        before = {
            "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant()]),
            "2": _engine_usage(fable=0, d7=93),
        }
        _, calls = self._tick(h, before)
        assert calls == []

    def test_disabled_by_setting(self, h):
        set_setting(h.switcher.backup_dir, "resets.auto", "off")
        before = {
            "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant()]),
            "2": _engine_usage(fable=0, d7=93),
        }
        _, calls = self._tick(h, before)
        assert calls == []

    def test_reset_event_json(self):
        event = ResetEvent(
            account={"number": 1, "email": "a"},
            plan={"reason": "on-limit", "window": "Fable", "pct": 100.0, "hoursLeft": 30.0, "grantKind": "Fable"},
            result=ClaimResult("cooldown", reason="cooldown").to_json(),
        )
        payload = event.to_json()
        assert payload["event"] == "limit-reset"
        assert payload["result"]["result"] == "cooldown"
        assert "Did not use a Fable reset on #1 a: cooldown" in event.human()


# -- list output -------------------------------------------------------------------


class TestListRow:
    def test_resets_row_under_usage(self):
        import time

        from ai_quota_swap.switcher import _usage_entry_lines

        # The row reads the real clock: grants must outlive it.
        far = time.time() + 120 * DAY
        usage = _usage(fable=6, grants=[_grant("a", ends=far), _grant("b", ends=far)])
        lines = _usage_entry_lines(UsageEntry(last_good=usage, fetched_at=None, age_s=0.0))
        text = "\n".join(lines)
        assert "resets:" in text
        assert "2    Fable · use by" in text

    def test_tui_card_says_it_as_a_sentence(self):
        import time

        from ai_quota_swap import display

        far = time.time() + 120 * DAY
        status = _usage(grants=[_grant("a", ends=far), _grant("b", ends=far, left=2)])["resets"]
        line = display.held_line(status, time.time())
        assert line.startswith("Holds 3 Fable resets")
        assert display.held_line(None, time.time()) == ""
    def test_no_row_without_resets(self):
        from ai_quota_swap.switcher import _usage_entry_lines

        usage = _usage(fable=6)
        lines = _usage_entry_lines(UsageEntry(last_good=usage, fetched_at=None, age_s=0.0))
        assert "resets:" not in "\n".join(lines)


# -- review regressions -----------------------------------------------------------


class TestReviewRegressions:
    def test_unwatched_window_never_refills_a_peer(self):
        # Active #1 is at Fable 100% (unwatched) with no grant: the engine
        # will not leave it for Fable, so refilling #2 would buy nothing.
        usage = {
            "1": _usage(fable=100, d7=40),
            "2": _usage(fable=100, d7=30, grants=[_grant()]),
        }
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    @pytest.mark.parametrize("settings", [LAST_RESORT, ACTIVE_100])
    def test_no_spend_when_the_engine_would_switch_anyway(self, settings):
        # Fable 100% (unwatched) but 7d 92% is over the 90% switch line and
        # #2 has room: the engine leaves either way, so keep the reset.
        usage = {
            "1": _usage(fable=100, d7=92, grants=[_grant()]),
            "2": _usage(d7=10),
        }
        assert plan_reset(_views(usage), current="1", settings=settings, now=NOW) is None

    def test_active_spends_when_it_would_stay(self):
        usage = {
            "1": _usage(fable=100, d7=60, grants=[_grant()]),
            "2": _usage(d7=10),
        }
        plan = plan_reset(_views(usage), current="1", settings=ACTIVE_100, now=NOW)
        assert plan is not None and plan.account == "1"

    def test_setup_token_peer_counts_as_room(self):
        header_peer = {
            "source": oauth.USAGE_SOURCE_HEADERS,
            "five_hour": {"pct": 5.0},
            "seven_day": {"pct": 10.0},
        }
        usage = {
            "1": _usage(d7=100, grants=[_grant(clears=["seven_day"], percent={"seven_day": 100})]),
            "2": header_peer,
        }
        assert plan_reset(_views(usage), current="1", settings=LAST_RESORT, now=NOW) is None

    def test_min_hours_judged_per_window(self):
        full = _grant(
            clears=["five_hour", "seven_day"],
            percent={"five_hour": 100, "seven_day": 97},
        )
        usage = {"1": _usage(h5=100, d7=97, grants=[full])}
        plan = plan_reset(_views(usage), current="1", settings=ON_LIMIT, now=NOW)
        assert plan is not None and plan.window == "7d"

    @pytest.mark.parametrize("extra", [
        {"weekly_scoped_shares": True},
        {"weekly_scoped_shares": 5},
        {"cedar_ember": {"eligible": True, "grants": 5}},
        {"cedar_ember": {"eligible": True, "grants": True}},
    ])
    def test_malformed_blocks_keep_the_reading(self, extra):
        data = {"seven_day": {"utilization": 30.0, "resets_at": "2026-11-15T08:00:00+00:00"}, **extra}
        result = oauth.build_usage_result(data)
        assert result["seven_day"]["pct"] == 30.0

    def test_claim_never_raises(self):
        import http.client

        def boom(req, timeout=None):
            raise http.client.IncompleteRead(b"")

        with patch("urllib.request.urlopen", boom):
            result = resets.claim("tok", "00000000-0000-4000-8000-00000000000a", "g", "r")
        assert result.result == "error" and not result.settled

    def test_far_future_expiry_displays(self):
        status = {"eligible": True, "grants": [_grant(ends=None, ends_at="9999-12-31T23:59:59Z")]}
        status["grants"][0]["ends_at"] = "9999-12-31T23:59:59Z"
        assert resets.summary(status, NOW) is not None

    def test_brake_counts_unknown_outcomes(self):
        from ai_quota_swap.autoswitch import reset_claims_today

        state = {
            "resetLog": [
                {"at": NOW - 10, "account": "1", "result": "error", "auto": True},
                {"at": NOW - 20, "account": "2", "result": "reset", "auto": True},
                {"at": NOW - 30, "account": "3", "auto": True},  # older entry: a reset
            ],
            "resetClaims": {"4": {"at": NOW - 5, "result": "pending", "auto": True}},
        }
        assert reset_claims_today(state, NOW) == 4

    def test_refusal_holds_longer(self):
        rec = {"at": NOW, "result": "cooldown"}
        assert not reset_claim_settled(rec, NOW + 5, NOW + 11 * 60)
        assert reset_claim_settled(rec, NOW + 5, NOW + 61 * 60)


class TestEngineReview:
    BEFORE = {
        "1": _usage(fable=100, d7=60, share=40, grants=[_grant()]),
        "2": _usage(fable=0, d7=93),
    }

    def test_another_engines_claim_blocks_ours(self, h):
        # Engine A writes its pending claim after this tick's state snapshot
        # (simulated during our pre-claim re-read): the locked re-check sees it.
        def other_engine():
            record_reset_claim(
                h.switcher.backup_dir, "1",
                {"at": h.clock.now, "grantId": "g-fable", "requestId": "a" * 32,
                 "result": "pending", "settled": False, "auto": True},
            )

        _, calls = TestEngine()._tick(h, self.BEFORE, on_refresh=other_engine)
        assert calls == []
        assert h.state()["resetClaims"]["1"]["requestId"] == "a" * 32

    def test_no_fresh_reading_no_spend(self, h):
        _, calls = TestEngine()._tick(h, self.BEFORE, stale_refresh=True)
        assert calls == []

    def test_active_mode_end_to_end(self, h):
        set_setting(h.switcher.backup_dir, "resets.auto", "active")
        set_setting(h.switcher.backup_dir, "resets.threshold", "100")
        before = {
            "1": _usage(fable=100, d7=60, grants=[_grant()]),
            "2": _usage(fable=0, d7=10),  # room elsewhere: active still refills
        }
        after = {
            "1": _usage(fable=0, d7=20, grants=[_grant(left=0)]),
            "2": _usage(fable=0, d7=10),
        }
        outcome, calls = TestEngine()._tick(h, before, after)
        assert [c[:2] for c in calls] == [("1", "g-fable")]
        event = next(e for e in h.events if isinstance(e, ResetEvent))
        assert event.plan["reason"] == "active-limit"
        assert h.active_number() == 1


# -- the shared reservation decides exactly as the engine's own closure did --------------


def _old_engine_reserve(state_path, account, grant_id, reason, now, fetched_at, host):
    """The engine's reservation closure before it moved into
    ``reserve_reset_claim``, verbatim (autoswitch.py before 90c39d4)."""
    from ai_quota_swap.autoswitch import (
        MAX_AUTO_RESETS_PER_DAY,
        _mutate_state_file,
        reset_claim_settled,
        reset_claims_today,
        reset_request_id,
    )

    reserved: dict = {}

    def reserve(st: dict) -> None:
        if reset_claims_today(st, now) >= MAX_AUTO_RESETS_PER_DAY:
            return
        current_claims = st.get("resetClaims")
        if not isinstance(current_claims, dict):
            current_claims = st["resetClaims"] = {}
        previous = current_claims.get(account)
        if not reset_claim_settled(previous, fetched_at, now):
            return
        record = {
            "at": now,
            "grantId": grant_id,
            "requestId": reset_request_id(previous, grant_id, host),
            "reason": reason,
            "auto": True,
            "result": "pending",
            "settled": False,
        }
        current_claims[account] = record
        reserved.update(record)

    _mutate_state_file(state_path, reserve)
    return reserved or None


def test_the_shared_reservation_decides_and_writes_as_the_old_engine_closure(tmp_path):
    import itertools

    from ai_quota_swap import autoswitch as a

    class Host:
        def __init__(self):
            self.n = 0

        def new_reset_request_id(self):
            self.n += 1
            return f"fresh-{self.n}"

    def new_reserve(path, now, fetched_at, host):
        return a.reserve_reset_claim(
            path, "1", "G", at=now, reason="on-limit", auto=True, host=host,
            admit=lambda st, previous: (
                a.reset_claims_today(st, now) < a.MAX_AUTO_RESETS_PER_DAY
                and a.reset_claim_settled(previous, fetched_at, now)
            ),
        )

    prevs = [
        None,
        {"at": NOW - 60, "grantId": "G", "requestId": "R1", "result": "pending", "settled": False, "auto": False},
        {"at": NOW - 3600, "grantId": "G", "requestId": "R1", "result": "error", "settled": False, "auto": False},
        {"at": NOW - 3600, "grantId": "H", "requestId": "R2", "result": "error", "settled": False, "auto": True},
        {"at": NOW - 1200, "grantId": "G", "requestId": "R3", "result": "reset", "settled": True, "auto": True},
        {"at": NOW - 4000, "grantId": "G", "requestId": "R4", "result": "cooldown", "settled": True, "auto": False},
        {"at": "bad"},
        "notadict",
    ]
    logs = [None, [], [{"at": NOW - 10, "result": "reset", "auto": True}] * 3,
            [{"at": NOW - 10, "result": "reset", "auto": False}] * 3]
    others = [None, {"at": NOW - 10, "grantId": "X", "requestId": "Q", "result": "pending",
                     "settled": False, "auto": True}]
    cases = itertools.product(prevs, logs, ("dict", "missing", "list"), others,
                              (None, NOW - 5000, NOW - 1, NOW - 30))
    for n, (prev, log, shape, other, fetched) in enumerate(cases):
        st: dict = {"schemaVersion": a.STATE_SCHEMA_VERSION}
        if log is not None:
            st["resetLog"] = log
        if shape == "dict":
            st["resetClaims"] = {k: v for k, v in (("1", prev), ("2", other)) if v is not None}
        elif shape == "list":
            st["resetClaims"] = [prev]
        old_path, new_path = tmp_path / f"{n}-old" / "s.json", tmp_path / f"{n}-new" / "s.json"
        for path in (old_path, new_path):
            path.parent.mkdir()
            path.write_text(json.dumps(st))
        case = (prev, log, shape, other, fetched)
        assert _old_engine_reserve(old_path, "1", "G", "on-limit", NOW, fetched, Host()) == (
            new_reserve(new_path, NOW, fetched, Host())), case
        assert json.loads(old_path.read_text()) == json.loads(new_path.read_text()), case


def test_a_refused_reservation_adds_nothing_to_the_state_file(tmp_path):
    from ai_quota_swap import autoswitch as a

    path = tmp_path / "s.json"
    path.write_text(json.dumps({"resetLog": [{"at": NOW - 10, "result": "reset", "auto": True}] * 3}))
    assert a.reserve_reset_claim(path, "1", "G", at=NOW, reason="on-limit", auto=True,
                                 admit=lambda st, previous: False) is None
    assert "resetClaims" not in json.loads(path.read_text())
