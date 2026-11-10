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
  the credit list, mapped into ``resets.grants[]`` (read side only).

The output follows the reading contract every consumer relies on (seam map
§4.6): a window's ``pct`` key is always present, a ``scoped`` entry's ``pct`` is
always a number, ``resets_at`` is a tz-aware ISO string, and no scoped name
collides with ``5h``, ``7d`` or ``rejected``.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from ai_quota_swap import __version__, oauth

_logger = logging.getLogger("ai-quota-swap")

BASE_URL = "https://chatgpt.com/backend-api"
USAGE_URL = f"{BASE_URL}/wham/usage"
CREDITS_URL = f"{BASE_URL}/wham/rate-limit-reset-credits"
TIMEOUT_S = 10.0

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


def request_json(url: str, access_token: str, account_id: str, *, timeout: float = TIMEOUT_S) -> object:
    """One authenticated GET against the ChatGPT backend; the parsed JSON body."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-ID": account_id,
            "User-Agent": user_agent(),
            "Accept": "application/json",
        },
        method="GET",
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


def fetch_usage(access_token: str, account_id: str) -> UsageOutcome:
    """Read one account's usage (and, when it holds any, its reset credits)."""
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

    if usage is not None and _credit_count(data) > 0:
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


def credits_status(listing: object, usage: dict) -> dict:
    """The reset-credit list, in the shape ``resets.parse_status`` returns.

    Each credit becomes one grant: ``id`` the credit id, ``ends_at`` its
    ``expires_at``, one use left while it is ``available``, ``clears`` the core
    windows this reading has, ``label`` the credit's title. Spending one is the
    next milestone; this is display and planning input only.
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
        if not isinstance(credit_id, str) or not credit_id:
            continue
        status = raw.get("status")
        available = status == "available"
        supported = raw.get("is_supported_by_plan") is not False
        reason = None
        if not supported:
            reason = "not_supported_by_plan"
        elif not available and status == "redeeming":
            reason = "redeeming"
        expires = raw.get("expires_at")
        title = raw.get("title")
        grants.append({
            "id": credit_id,
            "label": title if isinstance(title, str) else "",
            "resets_left": 1 if available else 0,
            "resets_total": 1,
            "starts_at": None,
            "ends_at": expires if isinstance(expires, str) and expires else None,
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
