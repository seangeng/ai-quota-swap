"""Codex quota: the ``/wham/usage`` read, normalized into qswap's reading shape.

The request is the one Codex's own clients make (aqs-notes ``codex-protocol.md``
§5): ``GET https://chatgpt.com/backend-api/wham/usage`` with the login's access
token and ``ChatGPT-Account-ID``. It answers a plain ``ai-quota-swap/<version>``
User-Agent. Never sends ``x-openai-codex-luna-reserve``, which the source
reserves for clients that can apply Reserve.

Normalization (docs/design/codex.md, "Usage reading"):

- Windows are identified by ``limit_window_seconds``, never by position: a Pro
  account reports only a weekly window, as ``primary_window``. 18,000 s ±5% is
  ``five_hour``, 604,800 s ±5% is ``seven_day``; any other length becomes a
  ``scoped`` entry named ``daily``, ``monthly`` or ``<N>h``.
- ``additional_rate_limits[]`` become ``scoped`` entries named by
  ``limit_name``.
- ``rate_limit.allowed == false`` with no window at 100% adds ``rejected``, so
  the account reads as exhausted.
- ``rate_limit_reset_credits.available_count > 0`` triggers one more GET for
  the credit list, mapped into ``resets.grants[]``.

The output follows the reading contract every consumer relies on (seam map
§4.6): a window's ``pct`` key is always present, a ``scoped`` entry's ``pct`` is
always a number, ``resets_at`` is a tz-aware ISO string, and no scoped name
collides with ``5h``, ``7d`` or ``rejected``.

Spending a credit is :func:`consume_reset_credit` (docs/design/codex.md,
"Reset credits"): ``POST …/rate-limit-reset-credits/consume`` with
``{redeem_request_id, credit_id}``, the request id a UUID v4 that the caller
stores before the request and reuses on retry.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ai_quota_swap import __version__, oauth

if TYPE_CHECKING:
    from ai_quota_swap.resets import ClaimResult

_logger = logging.getLogger("ai-quota-swap")

BASE_URL = "https://chatgpt.com/backend-api"
USAGE_URL = f"{BASE_URL}/wham/usage"
CREDITS_URL = f"{BASE_URL}/wham/rate-limit-reset-credits"
CONSUME_URL = f"{CREDITS_URL}/consume"
TIMEOUT_S = 10.0
#: Same budget as the Claude claim: a timeout is an unknown outcome, retried
#: later with the same request id.
CLAIM_TIMEOUT_S = 25.0

#: Longest credit id qswap accepts. OpenAI does not document the format; the
#: ids seen so far are short, and anything near this is not an id.
CREDIT_ID_MAX_LEN = 200

#: The consume answer's ``code`` → qswap's claim result (design table).
#: ``already_redeemed`` counts as done; anything else is an ``error``.
CONSUME_RESULTS = {
    "reset": "reset",
    "already_redeemed": "already_used",
    "nothing_to_reset": "ineligible",
    "no_credit": "unavailable",
}

FIVE_HOUR_S = 18_000
SEVEN_DAY_S = 604_800
DAY_S = 86_400
MONTH_S = 2_592_000  # Codex's own "monthly": 43,200 minutes
WINDOW_TOLERANCE = 0.05

#: Names a scoped entry may never take: ``relevant_windows`` labels and
#: label-keyed dicts downstream use them for the core windows.
_RESERVED_NAMES = {"5h", "7d", "rejected"}

#: Key under which the response's ``plan_type`` rides in a stored reading.
#: Consumers ignore unknown keys, and JSON output drops them.
PLAN_KEY = "plan_type"


class InvalidUsageResponse(ValueError):
    """The usage answer was not the shape Codex's own client parses."""


def user_agent() -> str:
    return f"ai-quota-swap/{__version__}"


def _headers(access_token: str, account_id: str) -> dict[str, str]:
    """The auth headers Codex's own clients send to the ChatGPT backend."""
    return {
        "Authorization": f"Bearer {access_token}",
        "ChatGPT-Account-ID": account_id,
        "User-Agent": user_agent(),
        "Accept": "application/json",
    }


def request_json(url: str, access_token: str, account_id: str, *, timeout: float = TIMEOUT_S) -> object:
    """One authenticated GET against the ChatGPT backend; the parsed JSON body."""
    request = urllib.request.Request(
        url, headers=_headers(access_token, account_id), method="GET"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


@dataclass(frozen=True)
class UsageOutcome:
    """One fetch: a reading (which may be None), or an error kind.

    ``error`` uses the same kinds as the Claude path (``http-401``,
    ``http-429``, ``timeout``, ``network``, ``bad-response``), so the usage
    store's backoff and 429 handling apply unchanged.
    """

    usage: dict | None = None
    error: str | None = None
    retry_after_s: float | None = None


def fetch_usage(
    access_token: str, account_id: str, *, read_credits: bool = True
) -> UsageOutcome:
    """Read one account's usage (and, when it holds any, its reset credits).

    ``read_credits`` is the shared ``resets.enabled`` setting: off, no credit
    list is read and the reading carries no ``resets`` status, the way the
    Claude read then sends the plain usage request.
    """
    try:
        data = request_json(USAGE_URL, access_token, account_id)
        usage = normalize_usage(data)
    except InvalidUsageResponse as e:
        _logger.warning("Codex usage fetch failed: bad-response")
        _logger.debug("Codex usage response rejected: %s", e)
        return UsageOutcome(error="bad-response")
    except Exception as e:  # HTTP, network, timeout, JSON: classified below
        kind, retry_after = oauth._classify_usage_error(e)
        _logger.warning(
            "Codex usage fetch failed: %s%s", kind,
            "" if retry_after is None else f", retry-after {retry_after:.0f}s",
        )
        _logger.debug("Codex usage fetch failure detail: %r", e)
        return UsageOutcome(error=kind, retry_after_s=retry_after)

    if usage is not None and not read_credits:
        usage.pop("resets", None)
    elif usage is not None and _credit_count(data) > 0:
        try:
            listing = request_json(CREDITS_URL, access_token, account_id)
            usage["resets"] = credits_status(listing, usage)
        except Exception as e:
            # The reading stands without its credit list; the next poll retries.
            _logger.warning("Codex reset-credit list fetch failed: %s", type(e).__name__)
            _logger.debug("Codex reset-credit list failure detail: %r", e)
    return UsageOutcome(usage=usage)


# -- normalization ----------------------------------------------------------------


def _within(seconds: float, target: float) -> bool:
    return abs(seconds - target) <= target * WINDOW_TOLERANCE


def window_label(seconds: float | None) -> str:
    """Short name for a window length: ``5h``, ``weekly``, ``daily``, ``monthly``, ``<N>h``."""
    if seconds is None:
        return "usage"
    if _within(seconds, FIVE_HOUR_S):
        return "5h"
    if _within(seconds, SEVEN_DAY_S):
        return "weekly"
    if _within(seconds, DAY_S):
        return "daily"
    if _within(seconds, MONTH_S):
        return "monthly"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))}m"
    return f"{round(seconds / 3600, 1):g}h"


def _number(value: object, what: str) -> float | None:
    """A JSON number as float; None for null/absent; malformed raises."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidUsageResponse(f"{what} is not a number: {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidUsageResponse(f"{what} is not finite")
    return float(value)


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class _Window:
    seconds: float | None
    pct: float | None
    resets_at: str | None

    def entry(self) -> dict:
        out: dict = {"pct": self.pct}
        if self.resets_at:
            out["resets_at"] = self.resets_at
            out["countdown"], out["clock"] = oauth.format_reset(self.resets_at)
        return out


def _window(raw: object, what: str, now: float) -> _Window | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidUsageResponse(f"{what} is not an object")
    seconds = _number(raw.get("limit_window_seconds"), f"{what}.limit_window_seconds")
    pct = _number(raw.get("used_percent"), f"{what}.used_percent")
    reset_at = _number(raw.get("reset_at"), f"{what}.reset_at")
    if reset_at is None:
        after = _number(raw.get("reset_after_seconds"), f"{what}.reset_after_seconds")
        reset_at = now + after if after is not None else None
    resets_at = None
    if reset_at is not None:
        try:
            resets_at = _iso(reset_at)
        except (OverflowError, OSError, ValueError) as e:
            raise InvalidUsageResponse(f"{what}.reset_at is out of range") from e
    return _Window(seconds, pct, resets_at)


def _unique_name(name: str, taken: set[str]) -> str:
    """``name``, made distinct from reserved and already used names (case-insensitively)."""
    base = name.strip() or "usage"
    candidate = base
    if candidate.lower() in _RESERVED_NAMES:
        candidate = f"{base} (codex)"
    n = 2
    while candidate.lower() in taken or candidate.lower() in _RESERVED_NAMES:
        candidate = f"{base} ({n})"
        n += 1
    taken.add(candidate.lower())
    return candidate


def _rate_limit(raw: object, what: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidUsageResponse(f"{what} is not an object")
    return raw


def normalize_usage(data: object, *, now: float | None = None) -> dict | None:
    """``/wham/usage`` JSON → qswap reading, or None when it reports no window.

    Raises :class:`InvalidUsageResponse` on a shape Codex's own client would
    reject; the fetch then records ``bad-response`` and keeps the last reading.
    """
    now = time.time() if now is None else now
    if not isinstance(data, dict):
        raise InvalidUsageResponse("usage response is not an object")
    result: dict = {}
    scoped: list[dict] = []
    taken: set[str] = set()

    rate_limit = _rate_limit(data.get("rate_limit"), "rate_limit")
    core: list[_Window] = []
    for key in ("primary_window", "secondary_window"):
        window = _window((rate_limit or {}).get(key), f"rate_limit.{key}", now)
        if window is None:
            continue
        core.append(window)
        if window.seconds is not None and _within(window.seconds, FIVE_HOUR_S) and "five_hour" not in result:
            result["five_hour"] = window.entry()
        elif window.seconds is not None and _within(window.seconds, SEVEN_DAY_S) and "seven_day" not in result:
            result["seven_day"] = window.entry()
        elif window.pct is not None:
            scoped.append({"name": _unique_name(window_label(window.seconds), taken), **window.entry()})

    extra = data.get("additional_rate_limits")
    if extra is not None and not isinstance(extra, list):
        raise InvalidUsageResponse("additional_rate_limits is not a list")
    for i, limit in enumerate(extra or ()):
        if not isinstance(limit, dict):
            raise InvalidUsageResponse(f"additional_rate_limits[{i}] is not an object")
        name = limit.get("limit_name") or limit.get("metered_feature") or "additional"
        name = name if isinstance(name, str) else "additional"
        inner = _rate_limit(limit.get("rate_limit"), f"additional_rate_limits[{i}].rate_limit")
        windows = [
            w for w in (
                _window((inner or {}).get(key), f"additional_rate_limits[{i}].{key}", now)
                for key in ("primary_window", "secondary_window")
            )
            if w is not None and w.pct is not None
        ]
        for w in windows:
            label = name if len(windows) == 1 else f"{name} {window_label(w.seconds)}"
            scoped.append({"name": _unique_name(label, taken), **w.entry()})

    if scoped:
        result["scoped"] = scoped

    if (
        isinstance(rate_limit, dict)
        and rate_limit.get("allowed") is False
        and not any(w.pct is not None and w.pct >= 100 for w in core)
    ):
        # The backend's authoritative "cannot work" bit, with no window that
        # says so: a synthetic 100% window keeps the account from reading as
        # healthy until the earliest reset it reported.
        resets = sorted(w.resets_at for w in core if w.resets_at)
        result["rejected"] = {
            "resets_at": resets[0] if resets else None,
            "retry_after_s": None,
        }

    if not result:
        return None
    plan = data.get("plan_type")
    if isinstance(plan, str) and plan and plan != "unknown":
        result[PLAN_KEY] = plan
    summary = data.get("rate_limit_reset_credits")
    if isinstance(summary, dict) and _credit_count(data) == 0:
        result["resets"] = credits_status({"credits": []}, result)
    return result


def _credit_count(data: object) -> int:
    summary = data.get("rate_limit_reset_credits") if isinstance(data, dict) else None
    count = summary.get("available_count") if isinstance(summary, dict) else None
    if isinstance(count, bool) or not isinstance(count, int):
        return 0
    return max(count, 0)


def valid_credit_id(value: object) -> bool:
    """Whether ``value`` can be a reset-credit id.

    OpenAI does not document the format, so this is deliberately loose (UUIDs,
    prefixed ids and the like all pass) and only refuses what cannot be an
    id: empty, longer than :data:`CREDIT_ID_MAX_LEN`, or containing whitespace
    or control characters. Anthropic's grant-id pattern
    (``resets.GRANT_ID_RE``) is never applied to Codex credits.
    """
    return (
        isinstance(value, str)
        and 0 < len(value) <= CREDIT_ID_MAX_LEN
        and value.isprintable()
        and not any(ch.isspace() for ch in value)
    )


def is_uuid4(value: object) -> bool:
    """Whether ``value`` is a UUID v4 in canonical form, the request id the
    consume endpoint expects (Codex sends ``Uuid::new_v4()``)."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def new_request_id() -> str:
    """A fresh consume request id (UUID v4, canonical form)."""
    return str(uuid.uuid4())


def _expiry(value: object) -> str | None:
    """A credit's ``expires_at`` as an ISO string: RFC 3339 as sent, or an
    epoch number converted; None for null, absent or unreadable."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        try:
            return _iso(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    return None


def credits_status(listing: object, usage: dict) -> dict:
    """The reset-credit list, in the shape ``resets.parse_status`` returns.

    Each credit becomes one grant: ``id`` the credit id, ``ends_at`` its
    ``expires_at``, one use left while it is ``available``, ``clears`` the core
    windows this reading has (a credit resets the core Codex limit; whether it
    also resets an additional limit is not known, so those are not listed),
    ``label`` the credit's title. A credit whose id cannot be an id
    (:func:`valid_credit_id`) is dropped with a log line.
    """
    clears = [key for key in ("five_hour", "seven_day") if key in usage]
    kind = {
        ("five_hour", "seven_day"): "full",
        ("seven_day",): "weekly",
        ("five_hour",): "session",
    }.get(tuple(clears))
    credits = listing.get("credits") if isinstance(listing, dict) else None
    grants: list[dict] = []
    for raw in credits if isinstance(credits, list) else ():
        if not isinstance(raw, dict):
            continue
        credit_id = raw.get("id")
        if not valid_credit_id(credit_id):
            if credit_id is not None:
                _logger.warning("Codex reset credit skipped: its id is not usable")
                _logger.debug("Codex reset credit id rejected: %r", credit_id)
            continue
        status = raw.get("status")
        available = status == "available"
        supported = raw.get("is_supported_by_plan") is not False
        reason = None
        if not supported:
            reason = "not_supported_by_plan"
        elif not available and status == "redeeming":
            reason = "redeeming"
        title = raw.get("title")
        grants.append({
            "id": credit_id,
            "label": title if isinstance(title, str) else "",
            "resets_left": 1 if available else 0,
            "resets_total": 1,
            "starts_at": None,
            "ends_at": _expiry(raw.get("expires_at")),
            "clears": list(clears),
            "blocking": [],
            "paused": False,
            "usable_now": available and supported,
            # The server answers nothing_to_reset rather than spending a credit
            # when no window needs it, so no limit has to be hit first.
            "use_requires_limit": False,
            "gives_back_weekly": False,
            "type": kind,
            "unavailable_reason": reason,
            "percent_used": {},
        })
    grants.sort(key=lambda g: (g["ends_at"] is None, g["ends_at"] or ""))
    exhausted = [
        key for key in clears
        if isinstance(usage[key].get("pct"), (int, float)) and usage[key]["pct"] >= 100
    ]
    weekly = usage.get("seven_day") or {}
    return {
        "eligible": True,
        "ineligible_reason": None,
        "at_limit": bool(exhausted) or "rejected" in usage,
        "exhausted": exhausted,
        "next_grant_id": next((g["id"] for g in grants if g["usable_now"]), None),
        "weekly_resets_at": weekly.get("resets_at"),
        "cooldown_until": None,
        "grants": grants,
    }


# -- spending a credit ------------------------------------------------------------


def consume_reset_credit(
    access_token: str,
    account_id: str,
    credit_id: str,
    request_id: str,
    *,
    timeout: float = CLAIM_TIMEOUT_S,
) -> ClaimResult:
    """Spend one reset credit on the account. Never raises.

    ``POST …/rate-limit-reset-credits/consume`` with the usage call's auth
    headers plus ``Content-Type: application/json`` and the body
    ``{"redeem_request_id": request_id, "credit_id": credit_id}``.
    ``request_id`` is the idempotency key: a retry with the same one cannot
    spend a second credit, so an unknown outcome (timeout, network, an answer
    without a known ``code``) is an ``error`` to retry with it. The answer's
    ``code`` maps by :data:`CONSUME_RESULTS`; HTTP 401/403 is ``auth_error``,
    429 ``rate_limited``, anything else ``error``. Malformed ids are refused
    here and never sent.
    """
    from ai_quota_swap.resets import ClaimResult

    if not valid_credit_id(credit_id) or not is_uuid4(request_id):
        return ClaimResult("error", detail="malformed credit or request id")
    if not access_token or not account_id:
        return ClaimResult("auth_error", detail="no access token")
    body = json.dumps({"redeem_request_id": request_id, "credit_id": credit_id}).encode()
    request = urllib.request.Request(
        CONSUME_URL,
        data=body,
        method="POST",
        headers={**_headers(access_token, account_id), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return ClaimResult("rate_limited", detail="http-429")
        if e.code in (401, 403):
            return ClaimResult("auth_error", detail=f"http-{e.code}")
        return ClaimResult("error", detail=f"http-{e.code}")
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        _logger.debug("Codex reset credit transport failure: %r", e)
        return ClaimResult("error", detail="network")
    except ValueError:
        return ClaimResult("error", detail="bad-response")
    except Exception as e:  # http.client.IncompleteRead, BadStatusLine, ...
        _logger.debug("Codex reset credit claim failed: %r", e)
        return ClaimResult("error", detail=type(e).__name__)
    if not isinstance(data, dict):
        return ClaimResult("error", detail="bad-response")
    code = data.get("code")
    result = CONSUME_RESULTS.get(code) if isinstance(code, str) else None
    if result is None:
        return ClaimResult("error", detail="unrecognized answer")
    windows = data.get("windows_reset")
    detail = (
        f"{windows} window{'s' if windows != 1 else ''} reset"
        if isinstance(windows, int) and not isinstance(windows, bool)
        else None
    )
    return ClaimResult(
        result=result,
        reason=None if result == "reset" else code,
        detail=detail,
    )
