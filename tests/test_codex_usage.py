"""``/wham/usage`` normalization into the reading contract, and the fetch itself."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime
from unittest.mock import patch

import pytest

from ai_quota_swap import __version__, json_output, oauth, resets
from ai_quota_swap.codex import usage as codex_usage
from ai_quota_swap.codex.usage import InvalidUsageResponse, normalize_usage
from tests.codex_helpers import DAY, usage_payload, window

NOW = 1_800_000_000.0


def _aware(iso: str) -> bool:
    return datetime.fromisoformat(iso).tzinfo is not None


def _contract(reading: dict) -> None:
    """What every consumer relies on (seam map §4.6)."""
    for key in ("five_hour", "seven_day"):
        if key in reading:
            assert "pct" in reading[key]
            if "resets_at" in reading[key]:
                assert _aware(reading[key]["resets_at"])
    names = set()
    for entry in reading.get("scoped", []):
        assert isinstance(entry["name"], str) and entry["name"]
        assert isinstance(entry["pct"], (int, float))
        assert entry["name"].lower() not in {"5h", "7d", "rejected"}
        assert entry["name"].lower() not in names
        names.add(entry["name"].lower())
        if "resets_at" in entry:
            assert _aware(entry["resets_at"])
    # Every consumer path accepts it.
    oauth.relevant_windows(reading, ("all",))
    oauth.account_headroom(reading)
    json_output.usage_to_json(reading, NOW)


class TestWindows:
    def test_weekly_only_primary_is_seven_day(self):
        """The real Pro shape: one weekly window, reported as primary."""
        reading = normalize_usage(
            usage_payload(window(604_800, 37, reset_in=3 * DAY, now=NOW)), now=NOW
        )
        _contract(reading)
        assert "five_hour" not in reading
        assert reading["seven_day"]["pct"] == 37.0
        assert reading["seven_day"]["resets_at"] == datetime.fromtimestamp(
            int(NOW + 3 * DAY), tz=__import__("datetime").timezone.utc
        ).isoformat()
        assert "scoped" not in reading
        assert reading["plan_type"] == "pro"

    def test_five_hour_primary_and_weekly_secondary(self):
        reading = normalize_usage(usage_payload(
            window(18_000, 12, reset_in=3600, now=NOW),
            window(604_800, 55, reset_in=DAY, now=NOW),
            plan="plus",
        ), now=NOW)
        _contract(reading)
        assert reading["five_hour"]["pct"] == 12.0
        assert reading["seven_day"]["pct"] == 55.0
        assert [label for label, _, _ in oauth.relevant_windows(reading)] == ["5h", "7d"]

    def test_positions_do_not_matter(self):
        reading = normalize_usage(usage_payload(
            window(604_800, 55, now=NOW), window(18_000, 12, now=NOW)
        ), now=NOW)
        assert reading["five_hour"]["pct"] == 12.0
        assert reading["seven_day"]["pct"] == 55.0

    @pytest.mark.parametrize("seconds, name", [
        (86_400, "daily"),
        (2_592_000, "monthly"),
        (36_000, "10h"),
        (17_000, "4.7h"),       # just outside 5h ±5%
        (1_800, "30m"),
    ])
    def test_odd_window_lengths_become_scoped(self, seconds, name):
        reading = normalize_usage(usage_payload(window(seconds, 20, now=NOW)), now=NOW)
        _contract(reading)
        assert reading["scoped"] == [
            {"name": name, "pct": 20.0, **{k: reading["scoped"][0][k] for k in ("resets_at", "countdown", "clock")}}
        ]
        assert "five_hour" not in reading and "seven_day" not in reading

    def test_tolerance_is_five_percent(self):
        reading = normalize_usage(usage_payload(window(18_900, 1, now=NOW), window(574_560, 2, now=NOW)), now=NOW)
        assert reading["five_hour"]["pct"] == 1.0
        assert reading["seven_day"]["pct"] == 2.0

    def test_two_windows_of_one_length_never_collide(self):
        reading = normalize_usage(usage_payload(
            window(18_000, 10, now=NOW), window(18_000, 20, now=NOW)
        ), now=NOW)
        _contract(reading)
        assert reading["five_hour"]["pct"] == 10.0
        assert reading["scoped"][0]["pct"] == 20.0

    def test_null_used_percent_keeps_a_core_window_with_unknown_pct(self):
        w = window(604_800, 0, now=NOW)
        w["used_percent"] = None
        reading = normalize_usage(usage_payload(w), now=NOW)
        _contract(reading)
        assert reading["seven_day"]["pct"] is None

    def test_reset_after_seconds_when_reset_at_is_missing(self):
        w = window(604_800, 5, reset_in=100, now=NOW)
        del w["reset_at"]
        reading = normalize_usage(usage_payload(w), now=NOW)
        assert reading["seven_day"]["resets_at"] == datetime.fromtimestamp(
            NOW + 100, tz=__import__("datetime").timezone.utc
        ).isoformat()


class TestAdditionalLimits:
    def test_each_limit_is_a_scoped_entry_named_by_limit_name(self):
        reading = normalize_usage(usage_payload(
            window(604_800, 10, now=NOW),
            additional=[
                {"limit_name": "GPT-5-Codex-Mini", "metered_feature": "codex_mini",
                 "rate_limit": {"allowed": True, "primary_window": window(604_800, 70, now=NOW)}},
                {"limit_name": "Spark", "metered_feature": "spark",
                 "rate_limit": {"allowed": True,
                                "primary_window": window(18_000, 30, now=NOW),
                                "secondary_window": window(604_800, 40, now=NOW)}},
            ],
        ), now=NOW)
        _contract(reading)
        assert [(s["name"], s["pct"]) for s in reading["scoped"]] == [
            ("GPT-5-Codex-Mini", 70.0), ("Spark 5h", 30.0), ("Spark weekly", 40.0),
        ]
        # Scoped windows gate decisions only when named, as for Claude models.
        assert oauth.account_headroom(reading) == 90.0
        assert oauth.account_headroom(reading, ("all",)) == 30.0

    @pytest.mark.parametrize("bad", ["5h", "7D", "Rejected"])
    def test_reserved_names_are_renamed(self, bad):
        reading = normalize_usage(usage_payload(
            window(604_800, 10, now=NOW),
            additional=[{"limit_name": bad, "rate_limit": {"primary_window": window(604_800, 1, now=NOW)}}],
        ), now=NOW)
        _contract(reading)
        assert reading["scoped"][0]["name"] == f"{bad} (codex)"

    def test_duplicate_names_are_made_distinct(self):
        reading = normalize_usage(usage_payload(
            window(86_400, 10, now=NOW),
            additional=[
                {"limit_name": "daily", "rate_limit": {"primary_window": window(604_800, 1, now=NOW)}},
                {"limit_name": "Daily", "rate_limit": {"primary_window": window(604_800, 2, now=NOW)}},
            ],
        ), now=NOW)
        _contract(reading)
        assert [s["name"] for s in reading["scoped"]] == ["daily", "daily (2)", "Daily (3)"]

    def test_a_limit_without_figures_adds_nothing(self):
        reading = normalize_usage(usage_payload(
            window(604_800, 10, now=NOW),
            additional=[{"limit_name": "x", "rate_limit": None}],
        ), now=NOW)
        assert "scoped" not in reading


class TestRejected:
    def test_not_allowed_without_a_full_window_reads_as_exhausted(self):
        reading = normalize_usage(usage_payload(
            window(18_000, 40, reset_in=600, now=NOW),
            window(604_800, 60, reset_in=DAY, now=NOW),
            allowed=False,
        ), now=NOW)
        _contract(reading)
        assert reading["rejected"] == {
            "resets_at": reading["five_hour"]["resets_at"], "retry_after_s": None,
        }
        assert oauth.account_headroom(reading) == 0.0
        assert ("rejected", 100.0, reading["five_hour"]["resets_at"]) in oauth.relevant_windows(reading)

    def test_not_allowed_with_a_window_at_100_needs_no_extra_marker(self):
        reading = normalize_usage(usage_payload(window(604_800, 100, now=NOW), allowed=False), now=NOW)
        assert "rejected" not in reading
        assert oauth.account_headroom(reading) == 0.0

    def test_not_allowed_without_any_window(self):
        reading = normalize_usage(usage_payload(allowed=False), now=NOW)
        assert reading["rejected"] == {"resets_at": None, "retry_after_s": None}

    def test_rejected_installs_no_store_backoff(self):
        from ai_quota_swap.usage_store import _rejection_backoff_until

        reading = normalize_usage(usage_payload(window(604_800, 50, now=NOW), allowed=False), now=NOW)
        assert _rejection_backoff_until(reading, NOW) is None


class TestEmptyAndMalformed:
    def test_no_windows_is_no_reading(self):
        assert normalize_usage(usage_payload(), now=NOW) is None
        assert normalize_usage({"plan_type": "pro", "rate_limit": None}, now=NOW) is None

    @pytest.mark.parametrize("data", [
        [],
        "x",
        {"rate_limit": "x"},
        {"rate_limit": {"primary_window": "x"}},
        {"rate_limit": {"primary_window": {"used_percent": "high", "limit_window_seconds": 1}}},
        {"rate_limit": {"primary_window": {"used_percent": True, "limit_window_seconds": 1}}},
        {"additional_rate_limits": {}},
        {"additional_rate_limits": ["x"]},
        {"rate_limit": {"primary_window": {"used_percent": 1, "limit_window_seconds": 1, "reset_at": 1e30}}},
    ])
    def test_malformed_shapes_raise(self, data):
        with pytest.raises(InvalidUsageResponse):
            normalize_usage(data, now=NOW)


class TestCredits:
    LISTING = {
        "credits": [
            {"id": "credit_late", "reset_type": "codex_rate_limits", "status": "available",
             "granted_at": "2026-10-01T00:00:00Z", "expires_at": "2026-12-31T00:00:00Z",
             "title": "Full reset", "is_supported_by_plan": True},
            {"id": "credit_early", "status": "available",
             "expires_at": "2026-11-20T00:00:00Z", "title": "Referral reset"},
            {"id": "credit_used", "status": "redeemed", "expires_at": None, "title": "Old"},
            {"id": "credit_busy", "status": "redeeming", "expires_at": None, "title": "Busy"},
            {"id": "credit_plan", "status": "available", "expires_at": None,
             "is_supported_by_plan": False, "title": "Elsewhere"},
            {"status": "available"},
            "junk",
        ],
        "available_count": 3,
    }

    def test_credit_list_maps_into_grants(self):
        reading = normalize_usage(usage_payload(
            window(18_000, 100, now=NOW), window(604_800, 70, now=NOW)
        ), now=NOW)
        status = codex_usage.credits_status(self.LISTING, reading)
        grants = {g["id"]: g for g in status["grants"]}
        assert [g["id"] for g in status["grants"]] == [
            "credit_early", "credit_late", "credit_used", "credit_busy", "credit_plan",
        ]
        late = grants["credit_late"]
        assert late["label"] == "Full reset"
        assert late["ends_at"] == "2026-12-31T00:00:00Z"
        assert late["resets_left"] == 1
        assert late["clears"] == ["five_hour", "seven_day"]
        assert late["type"] == "full"
        assert late["usable_now"] is True
        assert grants["credit_used"]["resets_left"] == 0
        assert grants["credit_busy"]["unavailable_reason"] == "redeeming"
        assert grants["credit_plan"]["usable_now"] is False
        assert status["next_grant_id"] == "credit_early"
        assert status["exhausted"] == ["five_hour"]
        assert status["at_limit"] is True
        # The shared readers accept it.
        now = datetime.fromisoformat("2026-11-15T00:00:00+00:00").timestamp()
        assert resets.total_left(status, now) == 3
        assert resets.summary(status, now).startswith("3 full")
        out = resets.status_to_json(status, now)
        assert out["resetsLeft"] == 3 and out["grants"][0]["kind"] == "full"

    def test_weekly_only_reading_clears_the_weekly_window(self):
        reading = normalize_usage(usage_payload(window(604_800, 99, now=NOW)), now=NOW)
        status = codex_usage.credits_status(self.LISTING, reading)
        assert status["grants"][0]["clears"] == ["seven_day"]
        # Every credit is a "Full reset (Weekly + 5 hr)"; on a weekly-only
        # plan it can clear only the weekly window, so it acts as weekly.
        assert status["grants"][0]["type"] == "full"
        assert resets.grant_scope(status["grants"][0]) == "weekly"
        assert status["weekly_resets_at"] == reading["seven_day"]["resets_at"]

    def test_both_windows_make_every_credit_a_full_reset(self):
        reading = normalize_usage(usage_payload(
            window(18_000, 10, now=NOW), window(604_800, 70, now=NOW)
        ), now=NOW)
        status = codex_usage.credits_status(self.LISTING, reading)
        assert {g["type"] for g in status["grants"]} == {"full"}
        assert {resets.grant_scope(g) for g in status["grants"]} == {"full"}
        assert resets.reset_name(status["grants"][0]) == "full reset"
        assert resets.clears_phrase(status["grants"][0]) == "clears all usage"

    def test_zero_credits_is_an_empty_status_without_a_request(self):
        reading = normalize_usage(usage_payload(window(604_800, 10, now=NOW), credits=0), now=NOW)
        assert reading["resets"]["grants"] == []
        assert reading["resets"]["eligible"] is True


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, headers: dict | None = None):
    import email.message

    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(codex_usage.USAGE_URL, code, "x", msg, io.BytesIO(b"{}"))


class TestFetch:
    def _urlopen(self, routes: dict):
        seen = []

        def fake(request, timeout=None):
            seen.append(request)
            answer = routes[request.full_url]
            if isinstance(answer, Exception):
                raise answer
            return _Response(json.dumps(answer).encode())

        return fake, seen

    def test_the_exact_request(self):
        fake, seen = self._urlopen({codex_usage.USAGE_URL: usage_payload(window(604_800, 5))})
        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            outcome = codex_usage.fetch_usage("tok-a", "acct-a")
        assert outcome.error is None and outcome.usage["seven_day"]["pct"] == 5.0
        (request,) = seen
        assert request.full_url == "https://chatgpt.com/backend-api/wham/usage"
        assert request.get_method() == "GET"
        headers = {k.lower(): v for k, v in request.header_items()}
        assert headers == {
            "authorization": "Bearer tok-a",
            "chatgpt-account-id": "acct-a",
            "user-agent": f"ai-quota-swap/{__version__}",
            "accept": "application/json",
        }

    def test_credits_are_listed_only_when_some_are_available(self):
        fake, seen = self._urlopen({
            codex_usage.USAGE_URL: usage_payload(window(604_800, 99), credits=1),
            codex_usage.CREDITS_URL: TestCredits.LISTING,
        })
        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            outcome = codex_usage.fetch_usage("tok", "acct")
        assert [r.full_url for r in seen] == [codex_usage.USAGE_URL, codex_usage.CREDITS_URL]
        assert len(outcome.usage["resets"]["grants"]) == 5
        assert {k.lower(): v for k, v in seen[1].header_items()}["chatgpt-account-id"] == "acct"

    def test_a_failed_credit_list_keeps_the_reading(self):
        fake, _ = self._urlopen({
            codex_usage.USAGE_URL: usage_payload(window(604_800, 99), credits=2),
            codex_usage.CREDITS_URL: _http_error(500),
        })
        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            outcome = codex_usage.fetch_usage("tok", "acct")
        assert outcome.error is None
        assert "resets" not in outcome.usage

    @pytest.mark.parametrize("answer, kind, retry", [
        (_http_error(401), "http-401", None),
        (_http_error(429, {"Retry-After": "120"}), "http-429", 120.0),
        (urllib.error.URLError("down"), "network", None),
        (TimeoutError(), "timeout", None),
    ])
    def test_failures_keep_the_shared_error_kinds(self, answer, kind, retry):
        fake, _ = self._urlopen({codex_usage.USAGE_URL: answer})
        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            outcome = codex_usage.fetch_usage("tok", "acct")
        assert (outcome.usage, outcome.error, outcome.retry_after_s) == (None, kind, retry)

    def test_malformed_json_is_a_bad_response(self):
        def fake(request, timeout=None):
            return _Response(b"<html>")

        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            assert codex_usage.fetch_usage("tok", "acct").error == "bad-response"

    def test_a_malformed_shape_is_a_bad_response(self):
        fake, _ = self._urlopen({codex_usage.USAGE_URL: {"rate_limit": "nope"}})
        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            assert codex_usage.fetch_usage("tok", "acct").error == "bad-response"

    def test_never_sends_the_reserve_header(self):
        fake, seen = self._urlopen({codex_usage.USAGE_URL: usage_payload(window(604_800, 5))})
        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            codex_usage.fetch_usage("tok", "acct")
        assert all("luna" not in k.lower() for k, _ in seen[0].header_items())


def test_window_label():
    assert codex_usage.window_label(None) == "usage"
    assert codex_usage.window_label(18_000) == "5h"
    assert codex_usage.window_label(604_800) == "weekly"
    assert codex_usage.window_label(90_000) == "daily"
