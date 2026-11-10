"""Synthetic Codex fixtures: login records with made-up JWTs, usage payloads.

Every token here is built in the test from fake claims and a junk signature;
no real email, id or token appears anywhere.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from ai_quota_swap.codex import auth as codex_auth
from ai_quota_swap.codex.usage import UsageOutcome

AUTH = codex_auth.AUTH_CLAIMS
PROFILE = codex_auth.PROFILE_CLAIMS
DAY = 86_400


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(claims: dict) -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.bm90LWEtc2lnbmF0dXJl"


def make_login(
    email: str = "a@example.com",
    user_id: str = "user-a",
    account_id: str = "acct-a",
    *,
    plan: str = "pro",
    exp: float | None = None,
    refresh: str | None = None,
    access_suffix: str = "",
    legacy: bool = False,
    auth_mode: str = "chatgpt",
    api_key: str | None = None,
    last_refresh: str | None = "2026-11-01T00:00:00.000000Z",
    id_token_claims: dict | None = None,
) -> bytes:
    """A login record, deliberately formatted (key order, indent, trailing
    newline) so that a re-serialization would show up as a byte difference."""
    now = time.time()
    access_claims = {
        "client_id": "app_test",
        AUTH: {
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "chatgpt_plan_type": plan,
        },
        PROFILE: {"email": email, "email_verified": True},
        "iat": now - DAY,
        "exp": exp if exp is not None else now + 9 * DAY,
        "jti": f"jti-{access_suffix}",
    }
    id_claims = id_token_claims if id_token_claims is not None else {
        "email": email,
        AUTH: {
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "chatgpt_plan_type": plan,
        },
        "exp": now + 3600,
    }
    record: dict = {}
    if not legacy:
        record["auth_mode"] = auth_mode
    record["OPENAI_API_KEY"] = api_key
    record["tokens"] = {
        "id_token": make_jwt(id_claims),
        "access_token": make_jwt(access_claims),
        "refresh_token": refresh if refresh is not None else f"rt-{user_id}-{access_suffix}",
        "account_id": account_id,
    }
    if last_refresh is not None:
        record["last_refresh"] = last_refresh
    return (json.dumps(record, indent=4) + "\n").encode("utf-8")


LOGIN_A = dict(email="a@example.com", user_id="user-a", account_id="acct-a")
LOGIN_B = dict(email="b@example.com", user_id="user-b", account_id="acct-b")
LOGIN_C = dict(email="c@example.com", user_id="user-c", account_id="acct-c")


def write_live(codex_home: Path, raw: bytes) -> Path:
    path = codex_home / "auth.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def window(seconds: int, pct: float, reset_in: float = 3600.0, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "used_percent": pct,
        "limit_window_seconds": seconds,
        "reset_after_seconds": int(reset_in),
        "reset_at": int(now + reset_in),
    }


def usage_payload(
    primary: dict | None = None,
    secondary: dict | None = None,
    *,
    allowed: bool = True,
    plan: str = "pro",
    additional: list | None = None,
    credits: int | None = None,
) -> dict:
    payload: dict = {
        "plan_type": plan,
        "rate_limit": {
            "allowed": allowed,
            "limit_reached": not allowed,
            "primary_window": primary,
            "secondary_window": secondary,
        },
        "additional_rate_limits": additional,
        "credits": None,
        "email": "ignored@example.com",
    }
    if credits is not None:
        payload["rate_limit_reset_credits"] = {
            "available_count": credits,
            "applicable_available_count": credits,
        }
    return payload


class FakeFetch:
    """Stands in for ``codex.usage.fetch_usage``: answers per account id and
    records every call."""

    def __init__(self, answers: dict[str, UsageOutcome] | None = None):
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []
        self.read_credits: list[bool] = []

    def __call__(self, access_token: str, account_id: str, *, read_credits: bool = True) -> UsageOutcome:
        self.calls.append((access_token, account_id))
        self.read_credits.append(read_credits)
        return self.answers.get(account_id, UsageOutcome(error="network"))


def weekly_usage(pct: float = 42.0, reset_in: float = 3 * DAY) -> dict:
    """A normalized reading with one weekly window (the Pro shape)."""
    from ai_quota_swap.codex.usage import normalize_usage

    return normalize_usage(usage_payload(window(604_800, pct, reset_in)))
