"""A canned Anthropic (and ChatGPT, and PyPI) API for demo runs.

Installed as ``urllib.request.urlopen``: every usage, profile, token-refresh and
reset-claim request the package makes goes through that one function, so the
real request building and the real response parsing both run, and only the
transport is fake. Answers come from the loaded scenario, keyed on the bearer
token (each seeded account gets its own invented token).

Routes: Anthropic's usage, profile, token and reset-claim endpoints; Codex's
``chatgpt.com/backend-api/wham`` usage, reset-credit list and credit consume;
and PyPI's project JSON, which answers 404 as it does today (the package is
not on PyPI), so the passive update check behaves as it does for a user.

A request this module has no answer for is a guard violation, not a 404: it
means the code wanted to reach somewhere new, and the run should fail loudly
so the harness gets updated rather than quietly showing "usage unavailable".
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

USAGE_PATH = "/api/oauth/usage"
PROFILE_PATH = "/api/oauth/profile"
TOKEN_URL_HOST = "platform.claude.com"
API_HOST = "api.anthropic.com"
CHATGPT_HOST = "chatgpt.com"
CODEX_USAGE_PATH = "/backend-api/wham/usage"
CODEX_CREDITS_PATH = "/backend-api/wham/rate-limit-reset-credits"
CODEX_CONSUME_PATH = CODEX_CREDITS_PATH + "/consume"
PYPI_HOST = "pypi.org"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class _Response(io.BytesIO):
    """Enough of ``http.client.HTTPResponse`` for the package's callers."""

    def __init__(self, url: str, payload: object, status: int = 200) -> None:
        super().__init__(json.dumps(payload).encode())
        self.url = url
        self.status = status
        self.code = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _http_error(url: str, code: int, body: object) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    hdrs["Content-Type"] = "application/json"
    return urllib.error.HTTPError(url, code, "demo", hdrs, io.BytesIO(json.dumps(body).encode()))


class FakeAnthropic:
    """Routes requests to a :class:`scenarios.LiveState`."""

    def __init__(self, state, on_unknown=None) -> None:
        self.state = state
        self.on_unknown = on_unknown
        self.requests: list[tuple[str, str]] = []

    def urlopen(self, req, data=None, timeout=None, **_kw):
        if isinstance(req, str):
            req = urllib.request.Request(req, data=data)
        url = req.full_url
        method = req.get_method()
        parts = urlsplit(url)
        headers = {k.lower(): v for k, v in req.header_items()}
        token = (headers.get("authorization") or "").removeprefix("Bearer ").strip()
        self.requests.append((method, f"{parts.hostname}{parts.path}"))

        if parts.hostname == API_HOST and parts.path == USAGE_PATH and method == "GET":
            resets = "cedar_ember" in parse_qs(parts.query)
            account = self.state.account_for_token(token)
            if account is None:
                raise _http_error(url, 401, {"error": {"type": "authentication_error"}})
            failure = self.state.usage_failure(account)
            if failure is not None:
                if failure.status == "timeout":
                    raise TimeoutError("timed out (demo)")
                raise _http_error(url, int(failure.status), {"error": {"type": "api_error"}})
            return _Response(url, self.state.usage_payload(account, resets=resets))

        if parts.hostname == API_HOST and parts.path == PROFILE_PATH and method == "GET":
            account = self.state.account_for_token(token)
            if account is None:
                raise _http_error(url, 401, {"error": {"type": "authentication_error"}})
            return _Response(url, self.state.profile_payload(account))

        if parts.hostname == TOKEN_URL_HOST and parts.path == "/v1/oauth/token" and method == "POST":
            body = json.loads((req.data or b"{}").decode())
            refreshed = self.state.refresh(body.get("refresh_token", ""))
            if refreshed is None:
                raise _http_error(url, 400, {"error": "invalid_grant"})
            return _Response(url, refreshed)

        if (
            parts.hostname == API_HOST
            and parts.path.startswith("/api/organizations/")
            and parts.path.endswith("/reset_rate_limits")
            and method == "POST"
        ):
            account = self.state.account_for_token(token)
            if account is None:
                raise _http_error(url, 401, {"error": {"type": "authentication_error"}})
            body = json.loads((req.data or b"{}").decode())
            return _Response(url, self.state.claim(account, body.get("grant_id", "")))

        if parts.hostname == CHATGPT_HOST and parts.path in (
            CODEX_USAGE_PATH, CODEX_CREDITS_PATH, CODEX_CONSUME_PATH,
        ):
            account = self.state.codex_account_for_token(token, headers.get("chatgpt-account-id", ""))
            if account is None:
                raise _http_error(url, 401, {"detail": "Unauthorized"})
            if parts.path == CODEX_USAGE_PATH and method == "GET":
                return _Response(url, self.state.codex_usage_payload(account))
            if parts.path == CODEX_CREDITS_PATH and method == "GET":
                return _Response(url, self.state.codex_credits_payload(account))
            if parts.path == CODEX_CONSUME_PATH and method == "POST":
                body = json.loads((req.data or b"{}").decode())
                return _Response(url, self.state.codex_consume(account, body.get("credit_id", "")))

        if parts.hostname == PYPI_HOST and parts.path == "/pypi/ai-quota-swap/json" and method == "GET":
            raise _http_error(url, 404, {"message": "Not Found"})

        message =f"the demo API has no answer for {method} {parts.hostname}{parts.path}"
        if self.on_unknown is not None:
            self.on_unknown(message)
        raise urllib.error.URLError(message)


def window(pct: float, resets_at: datetime) -> dict:
    return {"utilization": round(pct, 1), "resets_at": iso(resets_at)}
