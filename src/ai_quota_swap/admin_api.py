"""Anthropic's Admin API: the reads qswap makes with an organization's Admin key.

Every request below was written from Anthropic's published reference, read on
2026-11-10. Where the reference and docs/design/api-accounts.md disagreed, the
reference won and the design file was corrected.

``GET /v1/organizations/me`` (the key check in ``qswap api org add``)
    https://platform.claude.com/docs/en/api/organization/retrieve, read
    2026-11-10. Answers ``{id, type: "organization", name}``.

``GET /v1/organizations/api_keys`` (linking accounts to key ids)
    https://platform.claude.com/docs/en/api/organization/api_keys/list, read
    2026-11-10 (reached from https://platform.claude.com/docs/en/api/admin-api).
    Query: ``limit`` (1 to 1000, default 20), ``after_id``/``before_id``
    cursors, optional ``status`` and ``workspace_id`` filters. Answer:
    ``data[]`` of ``{id, name, partial_key_hint, status, scope, created_at,
    expires_at, ...}``, then ``has_more``, ``first_id``, ``last_id``; the next
    page is ``after_id=<last_id>``. ``partial_key_hint`` is "Partially
    redacted hint for the API key", string or null; the reference's example is
    ``"sk-ant-api03-R2D...igAA"``. Under version 2026-10-21 a key's workspace
    is ``scope.workspace_id`` (``workspace_id`` is gone).

``GET /v1/organizations/usage_report/messages`` (tokens per key, model, day)
    https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-messages-usage-report,
    now served at
    https://platform.claude.com/docs/en/api/beta/organization/usage_report/retrieve_messages,
    read 2026-11-10. Query: ``starting_at`` (RFC 3339, required),
    ``ending_at``, ``bucket_width`` ``1d|1h|1m`` (default ``1d``),
    ``group_by[]`` (``api_key_id``, ``model``, ``speed``, ``workspace_id``,
    ``account_id``, ``service_account_id``, ``service_tier``,
    ``context_window``, ``inference_geo``), ``api_key_ids[]`` and other
    filters, ``limit`` (for ``1d``: default 7, maximum 31 buckets), ``page``.
    Answer: ``data[]`` of buckets, oldest first and including empty ones,
    each ``{starting_at, ending_at, results[]}``; a result carries
    ``uncached_input_tokens``, ``cache_creation.ephemeral_5m_input_tokens``,
    ``cache_creation.ephemeral_1h_input_tokens``, ``cache_read_input_tokens``,
    ``output_tokens``, ``server_tool_use.web_search_requests`` and each
    grouped dimension (null when not grouped; ``api_key_id`` is also null for
    Console usage, ``workspace_id`` for the default workspace). Then
    ``has_more`` and ``next_page``, passed back as ``page``.

``GET /v1/organizations/cost_report`` (billed cost for ``usage --billed``)
    https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-cost-report,
    now served at
    https://platform.claude.com/docs/en/api/beta/organization/cost_report/retrieve,
    read 2026-11-10. Query: ``starting_at``, ``ending_at``, ``bucket_width``
    ``1d`` only, ``group_by[]`` ``description`` and/or ``workspace_id`` (not
    by API key), ``limit`` (default 7, maximum 31), ``page``. A result's
    ``amount`` is a decimal string in the currency's lowest unit, cents for
    USD (``"123.45"`` is $1.23), and ``currency`` is always ``"USD"``;
    grouping by ``description`` adds ``model``, ``cost_type`` and
    ``token_type``. Priority Tier costs are not in this report.

Guide: https://platform.claude.com/docs/en/build-with-claude/usage-cost-api,
now https://platform.claude.com/docs/en/manage-claude/usage-cost-api, read
2026-11-10. Requests send the Admin key in ``x-api-key`` and an
``anthropic-version`` header; 2026-10-21 is the current version
(https://platform.claude.com/docs/en/api/versioning). Buckets snap to UTC
boundaries. Data "typically appears within 5 minutes of API request
completion, though delays might occasionally be longer". Polling "once per
minute for sustained use" is supported; dashboards should cache, which this
module does for ``CACHE_TTL_S``. Errors (https://platform.claude.com/docs/en/api/errors):
401 ``authentication_error``, 403 ``permission_error``, 429
``rate_limit_error`` with a ``retry-after`` header when there is one, 500,
504 and 529.

These two reports are what Anthropic now calls the legacy Usage and Cost API.
It "remains supported". The newer Usage and Cost Analytics API
(``/v1/organizations/analytics/usage`` and ``/cost``, beta) can split cost by
API key, but its data "typically refreshes within 4 hours of the underlying
usage and can take up to 24 hours", which is too late for a budget brake.

Rules this module keeps:

- Only GET is ever sent with an Admin key (:func:`request` and the transport
  both refuse anything else, before any I/O).
- The key goes in one request header and nowhere else: not in a URL, an
  exception message, a log line or the cache file. :func:`redact` scrubs any
  ``sk-ant-`` token from text that came back from the server.
- Answers are cached under ``<data root>/cache/admin_api.json`` for
  ``CACHE_TTL_S``; a failure backs the organization off (429 honours
  ``retry-after``), and a stale answer up to ``STALE_MAX_S`` old is served in
  the meantime.
- The functions surfaces call (:func:`cached_report`, :func:`api_key_list`)
  return errors in their result; they never raise into ``list`` or the engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol

from ai_quota_swap import __version__
from ai_quota_swap.fsutil import atomic_write_text

_logger = logging.getLogger("ai-quota-swap")

BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2026-10-21"

ORG_INFO_PATH = "/v1/organizations/me"
API_KEYS_PATH = "/v1/organizations/api_keys"
USAGE_REPORT_PATH = "/v1/organizations/usage_report/messages"
COST_REPORT_PATH = "/v1/organizations/cost_report"

REQUEST_TIMEOUT_S = 8.0
# A report answer is reused for five minutes: the reports lag by about that
# much, so asking sooner learns nothing new.
CACHE_TTL_S = 300.0
# After a failure, an answer up to an hour old is still shown (marked stale)
# before the row falls back to the local estimate.
STALE_MAX_S = 3600.0
# The key listing changes rarely: refreshed daily, or early when a managed
# key appears that the last listing was never checked against.
KEY_LIST_TTL_S = 24 * 3600.0
KEY_LIST_MIN_REFRESH_S = 600.0
# Report buckets per page for daily buckets (the reference's maximum), keys
# per page for the listing (its maximum), and a page cap per read.
DAY_BUCKET_LIMIT = 31
KEY_PAGE_LIMIT = 1000
MAX_PAGES = 20
MAX_BODY_BYTES = 8 * 1024 * 1024
# Backoff after a failure: 1, 2, 4 ... minutes up to 30; a rejected key waits
# 30 minutes (it will not fix itself); a 429 waits its retry-after, capped.
BACKOFF_BASE_S = 60.0
BACKOFF_MAX_S = 1800.0
AUTH_BACKOFF_S = 1800.0
RETRY_AFTER_MAX_S = 3600.0

CACHE_FILENAME = "admin_api.json"
CACHE_SCHEMA_VERSION = 1
# Report entries older than this are dropped on the next write.
_CACHE_PRUNE_S = 2 * 24 * 3600.0

_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")

# Short, human notes per error kind, for a row's note and one-line outcomes.
ERROR_NOTES = {
    "auth": "Admin key rejected",
    "forbidden": "Admin key may not read usage reports",
    "rate-limited": "Admin API rate limited",
    "server": "Admin API error",
    "http": "Admin API refused the request",
    "timeout": "Admin API timed out",
    "network": "Admin API unreachable",
    "malformed": "Admin API answer unreadable",
    "key-unreadable": "stored Admin key unreadable",
    "not-read": "Admin API not read yet",
}


def redact(text: object) -> str:
    """``text`` with every ``sk-ant-`` token replaced: for anything that came
    back from the server or a subprocess before it reaches a message or log."""
    return _SECRET_RE.sub("sk-ant-[redacted]", str(text))


class AdminApiError(Exception):
    """A failed Admin API read. ``kind`` is one of :data:`ERROR_NOTES`'s keys.

    The message is built from the status code and the server's error type
    and message, redacted; it never carries the key.
    """

    def __init__(
        self,
        kind: str,
        message: str = "",
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        super().__init__(redact(message or ERROR_NOTES.get(kind, kind)))

    @property
    def note(self) -> str:
        """The short note a row or a one-line outcome shows."""
        base = ERROR_NOTES.get(self.kind, "Admin API error")
        if self.kind in ("server", "http") and self.status:
            return f"{base} (HTTP {self.status})"
        return base


# -- transport ----------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, headers: dict[str, str], timeout: float
    ) -> Response: ...


def _urllib_send(
    method: str, url: str, headers: dict[str, str], timeout: float
) -> Response:
    """One HTTP request through urllib. GET only, refused before any I/O."""
    if method != "GET":
        raise ValueError("qswap only sends GET requests with an Admin key")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY_BYTES)
            return Response(
                resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BODY_BYTES)
        except Exception:
            body = b""
        hdrs = e.headers.items() if e.headers is not None else ()
        return Response(e.code, {k.lower(): v for k, v in hdrs}, body)


# Tests replace this with a fake; nothing else does.
send: Transport = _urllib_send


def build_url(path: str, params: list[tuple[str, str]] | None = None) -> str:
    """The request URL. Brackets and colons stay literal, as the reference's
    examples write them (``group_by[]=model``, ``starting_at=...T00:00:00Z``)."""
    if not params:
        return BASE_URL + path
    return BASE_URL + path + "?" + urllib.parse.urlencode(params, safe="[]:")


def _headers(admin_key: str) -> dict[str, str]:
    return {
        "x-api-key": admin_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "accept": "application/json",
        "user-agent": f"ai-quota-swap/{__version__}",
    }


def _retry_after(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _error_detail(body: bytes) -> str:
    """``type: message`` from the server's error envelope, redacted and short."""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    err = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(err, dict):
        return ""
    kind = err.get("type") if isinstance(err.get("type"), str) else ""
    msg = err.get("message") if isinstance(err.get("message"), str) else ""
    detail = ": ".join(part for part in (kind, msg) if part)
    return redact(detail)[:200]


def request(
    method: str,
    path: str,
    params: list[tuple[str, str]] | None,
    admin_key: str,
    *,
    timeout: float = REQUEST_TIMEOUT_S,
) -> dict:
    """One Admin API request; the decoded JSON object. Raises :class:`AdminApiError`.

    ``method`` must be ``"GET"``: anything else raises ``ValueError`` before a
    connection is opened. It is a parameter only so that this refusal sits
    in the one function every request goes through.
    """
    if method != "GET":
        raise ValueError("qswap only sends GET requests with an Admin key")
    url = build_url(path, params)
    try:
        resp = send("GET", url, _headers(admin_key), timeout)
    except TimeoutError:
        raise AdminApiError("timeout", f"no answer within {timeout:g}s") from None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError):
            raise AdminApiError("timeout", f"no answer within {timeout:g}s") from None
        raise AdminApiError("network", redact(reason)) from None
    except (OSError, ConnectionError) as e:
        raise AdminApiError("network", redact(e)) from None
    except Exception as e:  # http.client.HTTPException and kin
        raise AdminApiError("network", f"{type(e).__name__}: {redact(e)}") from None
    status = resp.status
    detail = _error_detail(resp.body) if status >= 400 else ""
    suffix = f": {detail}" if detail else ""
    if status == 401:
        raise AdminApiError("auth", f"HTTP 401{suffix}", status=401)
    if status == 403:
        raise AdminApiError("forbidden", f"HTTP 403{suffix}", status=403)
    if status == 429:
        raise AdminApiError(
            "rate-limited", f"HTTP 429{suffix}", status=429,
            retry_after=_retry_after(resp.headers),
        )
    if status >= 500:
        raise AdminApiError("server", f"HTTP {status}{suffix}", status=status)
    if not 200 <= status < 300:
        raise AdminApiError("http", f"HTTP {status}{suffix}", status=status)
    try:
        doc = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AdminApiError("malformed", "the answer is not JSON") from None
    if not isinstance(doc, dict):
        raise AdminApiError("malformed", "the answer is not a JSON object")
    _logger.debug("Admin API GET %s -> %s", path, status)
    return doc


def get_report_pages(
    path: str, params: list[tuple[str, str]], admin_key: str
) -> list[dict]:
    """Every bucket of a usage or cost report, following ``next_page``."""
    buckets: list[dict] = []
    page: str | None = None
    for _ in range(MAX_PAGES):
        query = list(params) + ([("page", page)] if page else [])
        doc = request("GET", path, query, admin_key)
        data = doc.get("data")
        if not isinstance(data, list) or not all(isinstance(b, dict) for b in data):
            raise AdminApiError("malformed", "report has no data list")
        buckets.extend(data)
        has_more = doc.get("has_more")
        page = doc.get("next_page")
        if has_more is not True:
            return buckets
        if not isinstance(page, str) or not page:
            raise AdminApiError("malformed", "has_more without next_page")
    raise AdminApiError("malformed", f"more than {MAX_PAGES} pages")


def get_api_keys(admin_key: str) -> list[dict]:
    """Every API key of the organization, following ``last_id``. Each item is
    reduced to ``{id, name, hint, status, workspaceId}``."""
    keys: list[dict] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        query = [("limit", str(KEY_PAGE_LIMIT))]
        if after:
            query.append(("after_id", after))
        doc = request("GET", API_KEYS_PATH, query, admin_key)
        data = doc.get("data")
        if not isinstance(data, list):
            raise AdminApiError("malformed", "key listing has no data list")
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise AdminApiError("malformed", "key listing item without an id")
            hint = item.get("partial_key_hint")
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            keys.append({
                "id": item["id"],
                "name": item.get("name") if isinstance(item.get("name"), str) else "",
                "hint": hint if isinstance(hint, str) else None,
                "status": item.get("status") if isinstance(item.get("status"), str) else "",
                "workspaceId": scope.get("workspace_id")
                if isinstance(scope.get("workspace_id"), str) else None,
            })
        after = doc.get("last_id")
        if doc.get("has_more") is not True:
            return keys
        if not isinstance(after, str) or not after:
            raise AdminApiError("malformed", "has_more without last_id")
    raise AdminApiError("malformed", f"more than {MAX_PAGES} pages")


def organization_info(admin_key: str) -> dict:
    """``{id, name}`` of the organization the key belongs to. Raises."""
    doc = request("GET", ORG_INFO_PATH, None, admin_key)
    org_id, name = doc.get("id"), doc.get("name")
    if not isinstance(org_id, str) or not org_id:
        raise AdminApiError("malformed", "organization info has no id")
    return {"id": org_id, "name": name if isinstance(name, str) else ""}


# -- time ---------------------------------------------------------------------


def iso_z(dt: datetime) -> str:
    """RFC 3339 in UTC with a ``Z`` and whole seconds."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_month_bounds(now: float) -> tuple[datetime, datetime, datetime, datetime]:
    """``(month start, next month start, day start, next day start)`` in UTC."""
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    return month, next_month, day, day + timedelta(days=1)


# -- report parsing -----------------------------------------------------------


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdminApiError("malformed", "a token count is not a number")
    if value < 0:
        raise AdminApiError("malformed", "a token count is negative")
    return int(value)


def result_tokens(result: dict) -> tuple[int, int, int, int, int]:
    """``(input, output, cache_read, cache_write_5m, cache_write_1h)`` of one
    usage-report result, in the ledger's order (``Price.cost``'s arguments)."""
    cache = result.get("cache_creation")
    if cache is None:
        cache = {}
    if not isinstance(cache, dict):
        raise AdminApiError("malformed", "cache_creation is not an object")
    return (
        _count(result.get("uncached_input_tokens", 0)),
        _count(result.get("output_tokens", 0)),
        _count(result.get("cache_read_input_tokens", 0)),
        _count(cache.get("ephemeral_5m_input_tokens", 0)),
        _count(cache.get("ephemeral_1h_input_tokens", 0)),
    )


def bucket_day(bucket: dict) -> str:
    """The UTC date (``YYYY-MM-DD``) a daily bucket starts on."""
    start = bucket.get("starting_at")
    if not isinstance(start, str):
        raise AdminApiError("malformed", "bucket without starting_at")
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        raise AdminApiError("malformed", "bucket starting_at is not a time") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def bucket_results(bucket: dict) -> list[dict]:
    results = bucket.get("results")
    if results is None:
        return []
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        raise AdminApiError("malformed", "bucket results is not a list")
    return results


def amount_usd(result: dict) -> float:
    """A cost-report amount in dollars: the reference gives a decimal string
    in cents (``"123.45"`` is $1.23)."""
    raw = result.get("amount")
    currency = result.get("currency", "USD")
    if currency != "USD":
        raise AdminApiError("malformed", f"unexpected currency {currency!r}")
    try:
        cents = Decimal(str(raw)) if isinstance(raw, (str, int, float)) else None
    except InvalidOperation:
        cents = None
    if cents is None or not cents.is_finite():
        raise AdminApiError("malformed", "a cost amount is not a decimal string")
    return float(cents / 100)


# -- cache --------------------------------------------------------------------


class _Org(Protocol):
    name: str
    persistent: bool

    def admin_key(self) -> str: ...
    def cache_id(self) -> str: ...


# Answers for organizations that live only in the environment
# (``ANTHROPIC_ADMIN_KEY``) are kept in this process, never on disk.
_memory: dict[str, dict] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


class AdminCache:
    """The answer cache: ``cache/admin_api.json`` for registered organizations,
    process memory for the environment one. Holds report buckets, key
    listings and backoff, never a key."""

    def __init__(self, root: Path | None):
        self.path = Path(root) / "cache" / CACHE_FILENAME if root is not None else None

    def _load(self, org: _Org) -> dict:
        if not org.persistent or self.path is None:
            return _memory.setdefault(org.cache_id(), {})
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(doc, dict) or doc.get("schemaVersion") != CACHE_SCHEMA_VERSION:
            return {}
        orgs = doc.get("orgs")
        entry = orgs.get(org.name) if isinstance(orgs, dict) else None
        return entry if isinstance(entry, dict) else {}

    def read(self, org: _Org) -> dict:
        return self._load(org)

    def update(self, org: _Org, mutate: Callable[[dict], None], now: float) -> None:
        """Read-modify-write one organization's section. A failed write is
        logged and dropped: the cache is an optimisation."""
        if not org.persistent or self.path is None:
            mutate(_memory.setdefault(org.cache_id(), {}))
            return
        try:
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                doc = {}
            except (OSError, ValueError, UnicodeDecodeError):
                doc = {}
            if not isinstance(doc, dict) or doc.get("schemaVersion") != CACHE_SCHEMA_VERSION:
                doc = {"schemaVersion": CACHE_SCHEMA_VERSION, "orgs": {}}
            orgs = doc.setdefault("orgs", {})
            if not isinstance(orgs, dict):
                orgs = doc["orgs"] = {}
            section = orgs.get(org.name)
            if not isinstance(section, dict):
                section = orgs[org.name] = {}
            mutate(section)
            _prune(section, now)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, json.dumps(doc, separators=(",", ":")))
        except OSError as e:
            _logger.debug("Could not write the Admin API cache: %s", redact(e))

    def forget(self, name: str) -> None:
        """Drop one organization's section (``qswap api org remove``/``add``)."""
        if self.path is None:
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return
        orgs = doc.get("orgs") if isinstance(doc, dict) else None
        if isinstance(orgs, dict) and name in orgs:
            del orgs[name]
            try:
                atomic_write_text(self.path, json.dumps(doc, separators=(",", ":")))
            except OSError as e:
                _logger.debug("Could not write the Admin API cache: %s", redact(e))


def _prune(section: dict, now: float) -> None:
    reports = section.get("reports")
    if isinstance(reports, dict):
        for key in [
            k for k, v in reports.items()
            if not isinstance(v, dict)
            or not isinstance(v.get("fetchedAt"), (int, float))
            or now - v["fetchedAt"] > _CACHE_PRUNE_S
        ]:
            del reports[key]


def report_cache_key(path: str, params: list[tuple[str, str]]) -> str:
    canonical = json.dumps([path, sorted(params)], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _backoff_until(section: dict) -> tuple[float | None, str | None]:
    backoff = section.get("backoff")
    if not isinstance(backoff, dict):
        return None, None
    until = backoff.get("until")
    kind = backoff.get("kind")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return None, None
    return float(until), kind if isinstance(kind, str) else None


def _next_backoff(section: dict, error: AdminApiError, now: float) -> dict:
    previous = section.get("backoff") if isinstance(section.get("backoff"), dict) else {}
    failures = previous.get("failures", 0)
    failures = failures + 1 if isinstance(failures, int) and failures >= 0 else 1
    if error.kind in ("auth", "forbidden"):
        delay = AUTH_BACKOFF_S
    elif error.kind == "rate-limited" and error.retry_after is not None:
        delay = min(max(error.retry_after, BACKOFF_BASE_S), RETRY_AFTER_MAX_S)
    else:
        delay = min(BACKOFF_BASE_S * (2 ** (failures - 1)), BACKOFF_MAX_S)
    return {"until": now + delay, "failures": failures, "kind": error.kind}


def _record_failure(cache: AdminCache, org: _Org, error: AdminApiError, now: float) -> None:
    def mutate(section: dict) -> None:
        section["backoff"] = _next_backoff(section, error, now)

    cache.update(org, mutate, now)
    _logger.debug("Admin API read for %s failed: %s (%s)", org.name, error.kind, error)


def _admin_key(org: _Org) -> str:
    try:
        key = org.admin_key()
    except Exception as e:
        raise AdminApiError(
            "key-unreadable", f"the stored Admin key could not be read ({type(e).__name__})"
        ) from None
    if not key:
        raise AdminApiError("key-unreadable", "no Admin key is stored")
    return key


@dataclass
class ReportResult:
    """A report read through the cache. ``buckets`` is None when there is
    nothing to show; ``error`` says why a fresh read did not happen."""

    buckets: list[dict] | None
    fetched_at: float | None
    error: AdminApiError | None = None
    stale: bool = False


def cached_report(
    org: _Org,
    path: str,
    params: list[tuple[str, str]],
    *,
    now: float,
    network: bool,
    cache: AdminCache,
    ttl: float = CACHE_TTL_S,
    stale_max: float = STALE_MAX_S,
) -> ReportResult:
    """A report's buckets: the cached answer while it is under ``ttl`` old,
    else a fresh read when ``network`` allows and the organization is not
    backing off, else a stale answer up to ``stale_max`` old. Never raises."""
    key = report_cache_key(path, params)
    with _lock_for(f"{org.cache_id()}|{key}"):
        section = cache.read(org)
        reports = section.get("reports") if isinstance(section.get("reports"), dict) else {}
        entry = reports.get(key)
        cached = None
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("fetchedAt"), (int, float))
            and isinstance(entry.get("data"), list)
        ):
            cached = entry
        if cached is not None and 0 <= now - cached["fetchedAt"] <= ttl:
            return ReportResult(cached["data"], float(cached["fetchedAt"]))
        error: AdminApiError | None = None
        until, kind = _backoff_until(section)
        if until is not None and now < until:
            error = AdminApiError(kind or "server", "backing off after a failure")
        elif network:
            try:
                buckets = get_report_pages(path, params, _admin_key(org))
            except AdminApiError as e:
                error = e
                if e.kind != "key-unreadable":
                    _record_failure(cache, org, e, now)
            except Exception as e:  # never into list or the engine
                error = AdminApiError("malformed", f"{type(e).__name__}")
                _record_failure(cache, org, error, now)
            else:
                def store(section: dict) -> None:
                    section.setdefault("reports", {})
                    if not isinstance(section["reports"], dict):
                        section["reports"] = {}
                    section["reports"][key] = {"fetchedAt": now, "data": buckets}
                    section.pop("backoff", None)

                cache.update(org, store, now)
                return ReportResult(buckets, now)
        else:
            error = AdminApiError("not-read", "not read in this pass")
        if cached is not None and 0 <= now - cached["fetchedAt"] <= stale_max:
            return ReportResult(cached["data"], float(cached["fetchedAt"]), error, stale=True)
        return ReportResult(None, None, error)


@dataclass
class KeyList:
    """An organization's API keys as last listed, or why there are none."""

    keys: list[dict] | None
    fetched_at: float | None
    seen: set[str] = field(default_factory=set)
    error: AdminApiError | None = None
    refreshed: bool = False


def api_key_list(
    org: _Org,
    *,
    now: float,
    network: bool,
    cache: AdminCache,
    unseen: set[str] = frozenset(),
    force: bool = False,
) -> KeyList:
    """The organization's key listing, refreshed when older than
    ``KEY_LIST_TTL_S``, when ``unseen`` names a managed key this listing was
    never checked against (at most every ``KEY_LIST_MIN_REFRESH_S``), or when
    ``force``. ``unseen`` holds key fingerprints (never keys); a refresh marks
    them seen. Never raises."""
    with _lock_for(f"{org.cache_id()}|keys"):
        section = cache.read(org)
        stored = section.get("apiKeys") if isinstance(section.get("apiKeys"), dict) else {}
        keys = stored.get("keys") if isinstance(stored.get("keys"), list) else None
        fetched = stored.get("fetchedAt") if isinstance(stored.get("fetchedAt"), (int, float)) else None
        seen = set(s for s in stored.get("seen", []) if isinstance(s, str)) if isinstance(stored.get("seen"), list) else set()
        age = None if fetched is None else now - fetched
        due = (
            force
            or keys is None
            or age is None
            or age > KEY_LIST_TTL_S
            or age < 0
            or (bool(set(unseen) - seen) and age > KEY_LIST_MIN_REFRESH_S)
        )
        if not due or not network:
            return KeyList(keys, fetched, seen)
        until, kind = _backoff_until(section)
        if until is not None and now < until and not force:
            return KeyList(keys, fetched, seen, AdminApiError(kind or "server", "backing off"))
        try:
            listed = get_api_keys(_admin_key(org))
        except AdminApiError as e:
            if e.kind != "key-unreadable":
                _record_failure(cache, org, e, now)
            return KeyList(keys, fetched, seen, e)
        except Exception as e:
            error = AdminApiError("malformed", f"{type(e).__name__}")
            _record_failure(cache, org, error, now)
            return KeyList(keys, fetched, seen, error)
        new_seen = seen | set(unseen)

        def store(section: dict) -> None:
            section["apiKeys"] = {
                "fetchedAt": now,
                "keys": listed,
                "seen": sorted(new_seen),
            }
            section.pop("backoff", None)

        cache.update(org, store, now)
        return KeyList(listed, now, new_seen, refreshed=True)


# -- the reads surfaces make ----------------------------------------------------


def month_usage_params(key_ids: list[str], now: float) -> list[tuple[str, str]]:
    """The usage-report query for this UTC month to date, one daily bucket per
    day, split by key, model and speed (fast mode has its own price).

    ``ending_at`` is the start of tomorrow (UTC): the reference keeps a bucket
    that ends at ``ending_at`` (its 7-day example runs from the 8th to the
    15th at midnight), so today's partial bucket is included.
    """
    month, _next_month, _day, next_day = utc_month_bounds(now)
    params = [
        ("starting_at", iso_z(month)),
        ("ending_at", iso_z(next_day)),
        ("bucket_width", "1d"),
        ("group_by[]", "api_key_id"),
        ("group_by[]", "model"),
        ("group_by[]", "speed"),
    ]
    params.extend(("api_key_ids[]", k) for k in sorted(set(key_ids)))
    params.append(("limit", str(DAY_BUCKET_LIMIT)))
    return params


def month_usage_by_key(
    org: _Org, key_ids: list[str], *, now: float, network: bool, cache: AdminCache
) -> ReportResult:
    """This UTC month's usage-report buckets for ``key_ids``. Never raises."""
    return cached_report(
        org, USAGE_REPORT_PATH, month_usage_params(key_ids, now),
        now=now, network=network, cache=cache,
    )


def tokens_by_key_day(
    buckets: list[dict],
) -> dict[str, dict[str, list[tuple[str, str | None, tuple[int, int, int, int, int]]]]]:
    """``{api_key_id: {day: [(model, speed, tokens), ...]}}`` from the month
    report. Results without a key id (Console usage) are skipped. Raises
    :class:`AdminApiError` ``malformed`` on a bad shape."""
    out: dict[str, dict[str, list]] = {}
    for bucket in buckets:
        day = bucket_day(bucket)
        for result in bucket_results(bucket):
            key_id = result.get("api_key_id")
            if not isinstance(key_id, str) or not key_id:
                continue
            model = result.get("model") if isinstance(result.get("model"), str) else ""
            speed = result.get("speed") if isinstance(result.get("speed"), str) else None
            out.setdefault(key_id, {}).setdefault(day, []).append(
                (model, speed, result_tokens(result))
            )
    return out
