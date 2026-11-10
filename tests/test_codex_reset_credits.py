"""Codex usage reset credits (M2b): the consume call, credit and request ids,
the shared planner on Codex readings, and ``CodexSwitcher.claim_usage_reset``.

Every request is faked at ``urllib.request.urlopen``; the suite's network block
would fail any test that got past it.
"""

from __future__ import annotations

import http.client
import io
import json
import time
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_quota_swap import __version__, resets
from ai_quota_swap.autoswitch import reset_request_id
from ai_quota_swap.codex import usage as codex_usage
from ai_quota_swap.codex.switcher import CodexSwitcher
from ai_quota_swap.codex.usage import UsageOutcome, credits_status, normalize_usage
from ai_quota_swap.resets import AccountView, plan_reset
from ai_quota_swap.settings import ResetSettings, set_setting
from tests.codex_helpers import (
    DAY,
    LOGIN_A,
    LOGIN_B,
    FakeFetch,
    make_login,
    usage_payload,
    window,
    write_live,
)

NOW = 1_800_000_000.0
REQ = "1b4e28ba-2fa1-4d2b-883f-0016d3cca427"  # a UUID v4
UUID_CREDIT = "6f1b0d8e-3c2a-4f5e-9d7b-0a1b2c3d4e5f"
PREFIXED_CREDIT = "rlrc_01JC9ZK3M2Q8V7X4T6N5B0WYHD"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: bytes = b"{}"):
    import email.message

    return urllib.error.HTTPError(codex_usage.CONSUME_URL, code, "x", email.message.Message(), io.BytesIO(body))


def _consume(answer, *, credit_id=UUID_CREDIT, request_id=REQ):
    seen = []

    def fake(request, timeout=None):
        seen.append((request, timeout))
        if isinstance(answer, BaseException):
            raise answer
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return _Response(body)

    with patch.object(codex_usage.urllib.request, "urlopen", fake):
        result = codex_usage.consume_reset_credit("tok-a", "acct-a", credit_id, request_id)
    return result, seen


class TestConsumeRequest:
    def test_wire_format(self):
        result, seen = _consume({"code": "reset", "windows_reset": 2})
        ((request, timeout),) = seen
        assert request.full_url == "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
        assert request.get_method() == "POST"
        assert {k.lower(): v for k, v in request.header_items()} == {
            "authorization": "Bearer tok-a",
            "chatgpt-account-id": "acct-a",
            "user-agent": f"ai-quota-swap/{__version__}",
            "accept": "application/json",
            "content-type": "application/json",
        }
        assert json.loads(request.data) == {"redeem_request_id": REQ, "credit_id": UUID_CREDIT}
        assert timeout == codex_usage.CLAIM_TIMEOUT_S
        assert all("luna" not in k.lower() for k, _ in request.header_items())
        assert (result.result, result.ok, result.settled) == ("reset", True, True)
        assert result.detail == "2 windows reset"

    @pytest.mark.parametrize("code, expected, settled", [
        ("reset", "reset", True),
        ("already_redeemed", "already_used", True),
        ("nothing_to_reset", "ineligible", True),
        ("no_credit", "unavailable", False),
    ])
    def test_the_design_table(self, code, expected, settled):
        result, _ = _consume({"code": code, "windows_reset": 0})
        assert result.result == expected
        assert result.settled is settled
        assert result.reason == (None if code == "reset" else code)

    @pytest.mark.parametrize("answer", [
        {"code": "something_new"}, {"windows_reset": 2}, {"code": 3}, [], b"null", b"<html>",
    ])
    def test_anything_else_is_an_error_to_retry(self, answer):
        result, _ = _consume(answer)
        assert result.result == "error" and not result.settled

    @pytest.mark.parametrize("exc, expected, detail", [
        (_http_error(401), "auth_error", "http-401"),
        (_http_error(403), "auth_error", "http-403"),
        (_http_error(429), "rate_limited", "http-429"),
        (_http_error(500), "error", "http-500"),
        (_http_error(404), "error", "http-404"),
        (urllib.error.URLError("down"), "error", "network"),
        (TimeoutError(), "error", "network"),
        (http.client.IncompleteRead(b""), "error", "IncompleteRead"),
    ])
    def test_transport_failures_never_raise(self, exc, expected, detail):
        result, _ = _consume(exc)
        assert (result.result, result.detail) == (expected, detail)
        assert not result.settled

    @pytest.mark.parametrize("status, code, expected", [
        (400, "no_credit", "unavailable"),
        (409, "already_redeemed", "already_used"),
        (422, "nothing_to_reset", "ineligible"),
    ])
    def test_a_refusal_sent_with_a_4xx_is_read_from_the_body(self, status, code, expected):
        body = json.dumps({"code": code}).encode()
        result, _ = _consume(_http_error(status, body))
        assert (result.result, result.reason, result.detail) == (expected, code, f"http-{status}")

    @pytest.mark.parametrize("exc", [
        _http_error(400, b'{"code": "reset", "windows_reset": 2}'),  # a 4xx is never a success
        _http_error(500, b'{"code": "no_credit"}'),                  # a 5xx is never definite
        _http_error(400, b"not json"),
        _http_error(400, b'{"code": "something_new"}'),
    ])
    def test_other_http_errors_stay_unknown_outcomes(self, exc):
        result, _ = _consume(exc)
        assert result.result == "error" and not result.settled

    @pytest.mark.parametrize("credit_id, request_id", [
        ("", REQ),
        ("has space", REQ),
        ("tab\there", REQ),
        ("ctl\x07", REQ),
        ("x" * 201, REQ),
        (UUID_CREDIT, "1b4e28ba2fa14d2b883f0016d3cca427"),       # Claude's id shape, not a UUID v4 string
        (UUID_CREDIT, "6ba7b810-9dad-11d1-80b4-00c04fd430c8"),   # a UUID v1
        (UUID_CREDIT, REQ.upper()),                       # not canonical
        (UUID_CREDIT, "{" + REQ + "}"),
    ])
    def test_malformed_ids_are_never_sent(self, credit_id, request_id):
        result, seen = _consume({"code": "reset"}, credit_id=credit_id, request_id=request_id)
        assert seen == []
        assert result.result == "error" and result.detail == "malformed credit or request id"


class TestCreditIds:
    @pytest.mark.parametrize("cid", [
        UUID_CREDIT, PREFIXED_CREDIT, "credit-2f1c", "CR_ABC.def:01", "x" * 200, "rc|2026|01",
    ])
    def test_loose_validation_accepts_plausible_ids(self, cid):
        assert codex_usage.valid_credit_id(cid)

    def test_anthropics_pattern_would_have_dropped_some(self):
        # Why Codex credits never go through resets.GRANT_ID_RE.
        assert not resets.GRANT_ID_RE.match(PREFIXED_CREDIT)
        assert not resets.GRANT_ID_RE.match("x" * 41)

    @pytest.mark.parametrize("cid", ["", " ", "a b", "a\nb", "\x00", "x" * 201, None, 7])
    def test_what_cannot_be_an_id(self, cid):
        assert not codex_usage.valid_credit_id(cid)

    def test_the_mapping_keeps_uuid_and_prefixed_ids_and_drops_the_rest(self, caplog):
        reading = normalize_usage(usage_payload(window(604_800, 99, now=NOW)), now=NOW)
        listing = {"credits": [
            {"id": UUID_CREDIT, "status": "available", "expires_at": None},
            {"id": PREFIXED_CREDIT, "status": "available", "expires_at": None},
            {"id": "bad id", "status": "available", "expires_at": None},
        ]}
        with caplog.at_level("WARNING"):
            status = credits_status(listing, reading)
        assert [g["id"] for g in status["grants"]] == [UUID_CREDIT, PREFIXED_CREDIT]
        assert "id is not usable" in caplog.text and "bad id" not in caplog.text

    def test_an_epoch_expiry_is_read_too(self):
        reading = normalize_usage(usage_payload(window(604_800, 99, now=NOW)), now=NOW)
        status = credits_status({"credits": [
            {"id": "c1", "status": "available", "expires_at": NOW + DAY},
            {"id": "c2", "status": "available", "expires_at": "2027-01-01T00:00:00Z"},
            {"id": "c3", "status": "available", "expires_at": True},
        ]}, reading)
        ends = {g["id"]: g["ends_at"] for g in status["grants"]}
        assert resets.parse_ts(ends["c1"]) == pytest.approx(NOW + DAY)
        assert ends["c2"] == "2027-01-01T00:00:00Z"
        assert ends["c3"] is None


class TestRequestIds:
    def test_codex_gets_a_stored_uuid4_reused_on_retry(self, temp_home):
        sw = CodexSwitcher()
        fresh = reset_request_id(None, "c1", sw)
        assert codex_usage.is_uuid4(fresh)
        pending = {"grantId": "c1", "requestId": fresh, "settled": False}
        assert reset_request_id(pending, "c1", sw) == fresh
        assert reset_request_id(pending, "c2", sw) != fresh
        assert reset_request_id({**pending, "settled": True}, "c1", sw) != fresh

    def test_a_stored_id_codex_would_refuse_is_replaced(self, temp_home):
        sw = CodexSwitcher()
        stale = {"grantId": "c1", "requestId": uuid.uuid4().hex, "settled": False}
        replaced = reset_request_id(stale, "c1", sw)
        assert replaced != stale["requestId"] and codex_usage.is_uuid4(replaced)

    def test_claude_ids_are_unchanged(self, temp_home):
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        claude = ClaudeAccountSwitcher()
        for host in (None, claude):
            fresh = reset_request_id(None, "g", host)
            assert len(fresh) == 32 and int(fresh, 16) >= 0 and "-" not in fresh
            assert resets.REQUEST_ID_RE.match(fresh)
            # Any stored string is reused, as before.
            assert reset_request_id({"grantId": "g", "requestId": "keep", "settled": False}, "g", host) == "keep"


# -- the shared planner on Codex readings ------------------------------------------------


def _codex(weekly: float, five_hour: float | None = None, *, credit=None, allowed=None,
           weekly_in: float = 3 * DAY) -> dict:
    primary = window(18_000, five_hour, 2 * 3600, now=NOW) if five_hour is not None else None
    week = window(604_800, weekly, weekly_in, now=NOW)
    usage = normalize_usage(usage_payload(
        primary or week, week if primary else None,
        allowed=allowed if allowed is not None else max(weekly, five_hour or 0) < 100,
        credits=1,
    ), now=NOW)
    listing = {"credits": [credit or {"id": "c1", "status": "available", "expires_at": None}]}
    usage["resets"] = credits_status(listing, usage)
    return usage


def _plan(usage: dict, *, auto: str = "last-resort", **kw):
    view = AccountView(num="1", usage=usage, threshold=99.0)
    return plan_reset([view], current="1", settings=ResetSettings(auto=auto, **kw), now=NOW)


class TestPlannerOnCodex:
    def test_weekly_only_at_the_limit(self):
        plan = _plan(_codex(100))
        assert plan is not None
        assert (plan.window, plan.grant_kind, plan.projected) == ("7d", "weekly", {"7d": 0.0})
        assert plan.hours_left == pytest.approx(72.0, abs=0.1)

    def test_five_hour_and_weekly_are_both_cleared(self):
        plan = _plan(_codex(97, 60))
        assert plan is not None and plan.grant_kind == "full"
        assert plan.projected == {"5h": 0.0, "7d": 0.0}

    def test_a_weekly_reset_due_soon_is_kept(self):
        assert _plan(_codex(100, weekly_in=6 * 3600)) is None  # min_hours_left 24

    def test_a_credit_about_to_expire_is_spent_from_the_expiry_line(self):
        soon = datetime.fromtimestamp(NOW + 2 * DAY, tz=timezone.utc).isoformat()
        plan = _plan(_codex(60, credit={"id": "c1", "status": "available", "expires_at": soon}))
        assert plan is not None and plan.reason == "expiring"

    def test_a_refusal_with_no_window_at_its_limit_is_not_refilled(self):
        """allowed=false with every window below 100 reads as ``rejected``;
        a credit resets windows, not that refusal (workspace credits, a spend
        cap), so the planner keeps it."""
        usage = _codex(97, allowed=False)
        assert "rejected" in usage
        assert _plan(usage) is None

    def test_an_unsupported_credit_is_not_planned(self):
        assert _plan(_codex(100, credit={"id": "c1", "status": "available",
                                         "is_supported_by_plan": False})) is None

    def test_off_never(self):
        assert _plan(_codex(100), auto="off") is None


# -- the switcher's claim ------------------------------------------------------------------


@pytest.fixture
def env(temp_home: Path, tmp_path: Path, monkeypatch) -> SimpleNamespace:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fetch = FakeFetch()
    monkeypatch.setattr(codex_usage, "fetch_usage", fetch)
    calls = []

    def consume(access_token, account_id, credit_id, request_id, **_kw):
        calls.append((access_token, account_id, credit_id, request_id))
        return env.answer

    monkeypatch.setattr(codex_usage, "consume_reset_credit", consume)
    env = SimpleNamespace(codex_home=codex_home.resolve(), fetch=fetch, consumed=calls,
                          answer=resets.ClaimResult("reset"))
    return env


@pytest.fixture
def sw(env) -> CodexSwitcher:
    sw = CodexSwitcher()
    write_live(env.codex_home, make_login(**LOGIN_B))
    sw.add_account()
    env.live = make_login(**LOGIN_A)
    write_live(env.codex_home, env.live)
    sw.add_account()
    return sw


def _token(raw: bytes) -> str:
    from ai_quota_swap.codex import auth as codex_auth

    return codex_auth.parse_login(raw).access_token


class TestSwitcherClaim:
    def test_the_active_account_uses_the_live_login_read_only(self, sw, env):
        renewed = make_login(**LOGIN_A, access_suffix="renewed-by-codex")
        write_live(env.codex_home, renewed)
        result = sw.claim_usage_reset("2", UUID_CREDIT, REQ)
        assert result.ok
        assert env.consumed == [(_token(renewed), "acct-a", UUID_CREDIT, REQ)]
        assert (env.codex_home / "auth.json").read_bytes() == renewed  # untouched

    def test_another_account_uses_its_saved_login(self, sw, env):
        saved = (sw.credentials_dir / "account-1.json").read_bytes()
        sw.claim_usage_reset("1", UUID_CREDIT, REQ)
        assert env.consumed[0][:2] == (_token(saved), "acct-b")
        assert (sw.credentials_dir / "account-1.json").read_bytes() == saved

    def test_an_expired_token_is_refused_never_refreshed(self, sw, env):
        expired = make_login(**LOGIN_B, exp=time.time() - 60)
        (sw.credentials_dir / "account-1.json").write_bytes(expired)
        with patch("ai_quota_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=AssertionError("refreshed a Codex login")):
            result = sw.claim_usage_reset("1", UUID_CREDIT, REQ)
        assert result.result == "auth_error" and "expired" in result.detail
        assert env.consumed == []

    def test_no_saved_login(self, sw, env):
        (sw.credentials_dir / "account-1.json").unlink()
        result = sw.claim_usage_reset("1", UUID_CREDIT, REQ)
        assert result.result == "auth_error" and env.consumed == []

    def test_a_keyring_home_never_raises(self, sw, env):
        (env.codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        result = sw.claim_usage_reset("2", UUID_CREDIT, REQ)
        assert result.result == "auth_error" and "file storage only" in result.detail
        assert env.consumed == []

    @pytest.mark.parametrize("answer, rereads", [
        (resets.ClaimResult("reset"), True),
        (resets.ClaimResult("already_used", reason="already_redeemed"), True),
        (resets.ClaimResult("ineligible", reason="nothing_to_reset"), True),
        (resets.ClaimResult("unavailable", reason="no_credit"), True),
        (resets.ClaimResult("error", detail="network"), False),
        (resets.ClaimResult("rate_limited", detail="http-429"), False),
    ])
    def test_a_decided_answer_rereads_the_account(self, sw, env, answer, rereads):
        env.fetch.answers["acct-b"] = UsageOutcome(usage=normalize_usage(
            usage_payload(window(604_800, 5))))
        env.answer = answer
        sw.claim_usage_reset("1", UUID_CREDIT, REQ)
        assert ("acct-b" in {c[1] for c in env.fetch.calls}) is rereads


class TestCreditListFollowsResetsEnabled:
    def test_off_reads_no_credit_list(self, sw, env):
        set_setting(sw.data_root, "resets.enabled", "false")
        sw.usage_entries_by_account()
        assert env.fetch.read_credits and not any(env.fetch.read_credits)

    def test_on_by_default(self, sw, env):
        sw.usage_entries_by_account()
        assert env.fetch.read_credits and all(env.fetch.read_credits)

    def test_fetch_usage_without_credits_drops_the_status(self):
        seen = []

        def fake(request, timeout=None):
            seen.append(request.full_url)
            return _Response(json.dumps(usage_payload(window(604_800, 99), credits=2)).encode())

        with patch.object(codex_usage.urllib.request, "urlopen", fake):
            outcome = codex_usage.fetch_usage("tok", "acct", read_credits=False)
        assert seen == [codex_usage.USAGE_URL]
        assert "resets" not in outcome.usage
