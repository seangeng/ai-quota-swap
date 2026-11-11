"""Spend and budgets for API-key accounts (docs/design/api-accounts.md).

An API-key account has no quota windows. Its usage entry keeps the
``"api key"`` sentinel, so the engine never ranks it by windows and the
collector never polls it on the subscription usage endpoint; this module adds
a *reading* beside the sentinel (``UsageEntry.api_spend``)::

    {"spend": {"used": 212.40, "limit": 500.0, "pct": 42.48,
               "currency": "USD", "resets_at": "2026-12-01T00:00:00Z"},
     "today": 4.12, "source": "admin-api", "month": "2026-11",
     "as_of": 1762780000.0, "unpriced_tokens": 0,
     "org": "acme", "api_key_id": "apikey_01...", "note": "..."}

``source`` is ``admin-api`` when the account is linked to a key in a
registered organization and Anthropic's usage report could be read (tokens per
key, priced with the bundled table), ``ledger`` when the figure comes from
this machine's transcripts (an estimate), and None when there is no figure
(``used`` is None then). Months and days are UTC, as in Anthropic's reports.

Linking. The key listing gives each key a ``partial_key_hint`` such as
``sk-ant-api03-R2D...igAA``. A managed key matches a hint when it starts with
everything before the ``...`` (which must begin ``sk-ant-``) and ends with
everything after it (at least four characters), with something in between.
An account links to a key only when exactly one listed key, across every
registered organization, matches it, and no other managed account matches
that same key; otherwise nothing is linked and the reading says why. The link
(``apiKeyId``, ``apiOrg``, ``apiKeyHint``) is stored on the account record
and re-checked against the account's current key on every pass, without the
network.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap import admin_api
from ai_quota_swap.admin_api import AdminApiError, AdminCache, ReportResult
from ai_quota_swap.admin_orgs import Org, OrgRegistry

SOURCE_ADMIN = "admin-api"
SOURCE_LEDGER = "ledger"
BUDGET_MAX = 10_000_000.0
# The engine warns once per account per UTC month at this share of a budget.
BUDGET_WARN_FRACTION = 0.8
LINK_FIELDS = ("apiKeyId", "apiOrg", "apiKeyHint")
MIN_HINT_TAIL = 4
# A ledger figure older than this gets an "as of" line.
LEDGER_STALE_S = 3600.0
#: The note of a reading whose ledger exists but could not be read (an
#: unknown schema version, no meta table, a sqlite or OS error): its spend is
#: unknown, never zero. "No ledger file yet" is not this: nothing was ever
#: scanned, so nothing is recorded.
LEDGER_UNREADABLE = "this machine's usage ledger could not be read; run: qswap usage"
_HINT_SEPARATORS = ("...", "…")
_KEY_PREFIX = "sk-ant-"


# -- budgets --------------------------------------------------------------------


def normalize_budget(value: object) -> float:
    """A budget in dollars from ``500``, ``"49.50"``, ``"$1,200"``. Raises ValueError."""
    message = (
        f"a budget is an amount in US dollars above 0 and at most "
        f"${BUDGET_MAX:,.0f}, for example 500 or 49.50"
    )
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.startswith("$"):
            text = text[1:]
        try:
            amount = float(text)
        except ValueError:
            raise ValueError(message) from None
    elif isinstance(value, (int, float)):
        amount = float(value)
    else:
        raise ValueError(message)
    if not math.isfinite(amount) or amount <= 0 or amount > BUDGET_MAX:
        raise ValueError(message)
    return round(amount, 2)


def budget_from_record(record: dict | None) -> float | None:
    """A stored budget, or None. Degrades, never raises: a hand-edited value
    that is not a usable amount reads as no budget."""
    if not isinstance(record, dict) or record.get("budget") is None:
        return None
    try:
        return normalize_budget(record["budget"])
    except ValueError:
        return None


# -- credit ---------------------------------------------------------------------
#
# docs/design/api-key-sessions.md, "Credit": ``qswap credit N AMOUNT [--expires
# DATE]`` records a balance for an API-key account as of now. Spend measured
# from then on counts down from it (Anthropic's figures when the account is
# linked to a registered organization, the ledger otherwise). At zero, or after
# the expiry date, the account is at its limit like at its budget: the engine
# leaves it and will not move to it. It is qswap's own count, not Anthropic's
# balance, which no key can read.


def normalize_credit(value: object) -> float:
    """A credit in dollars from ``1187``, ``"1,187.50"``, ``"$1187"``. Raises ValueError."""
    try:
        return normalize_budget(value)
    except ValueError:
        raise ValueError(
            f"a credit is an amount in US dollars above 0 and at most "
            f"${BUDGET_MAX:,.0f}, for example 1187 or 49.50"
        ) from None


def parse_expiry(value: str) -> str:
    """``YYYY-MM-DD`` (the last UTC day the credit can be used). Raises ValueError."""
    from datetime import date

    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise ValueError(
            f"--expires takes a date as YYYY-MM-DD, for example 2026-12-31 (got {value!r})"
        ) from None


def credit_from_record(record: dict | None) -> dict | None:
    """``{"amount", "since", "expires"}`` (``since`` in epoch seconds,
    ``expires`` a date or None), or None. Degrades, never raises."""
    from ai_quota_swap.ledger.activations import parse_ts

    if not isinstance(record, dict):
        return None
    raw = record.get("credit")
    if not isinstance(raw, dict):
        return None
    try:
        amount = normalize_credit(raw.get("amount"))
    except ValueError:
        return None
    since = parse_ts(raw.get("since"))
    if since is None:
        return None
    expires = raw.get("expires")
    try:
        expires = parse_expiry(expires) if expires else None
    except ValueError:
        expires = None
    return {"amount": amount, "since": since, "expires": expires}


def credit_expired(expires: str | None, now: float) -> bool:
    """Whether ``now`` is after the expiry date (the whole UTC day counts)."""
    if not expires:
        return False
    from datetime import date, datetime, timedelta, timezone

    day = date.fromisoformat(expires)
    end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    return now >= end.timestamp()


def credit_reading(credit: dict, used: float | None, source: str | None, now: float) -> dict:
    """The ``credit`` part of a reading: what is left of it."""
    from ai_quota_swap.admin_api import iso_z
    from datetime import datetime, timezone

    left = None if used is None else round(credit["amount"] - used, 4)
    return {
        "amount": credit["amount"],
        "used": None if used is None else round(used, 4),
        "left": left,
        "since": iso_z(datetime.fromtimestamp(credit["since"], tz=timezone.utc)),
        "expires": credit["expires"],
        "expired": credit_expired(credit["expires"], now),
        "spent": left is not None and left <= 0,
        "source": source,
    }


def limit_reason(reading: object) -> str | None:
    """Why an API-key account is at its limit: ``credit-expired``,
    ``credit-spent`` or ``budget``; None when it is not (or nothing can
    tell). Whichever is reached first applies."""
    if not isinstance(reading, dict):
        return None
    credit = reading.get("credit")
    if isinstance(credit, dict):
        if credit.get("expired"):
            return "credit-expired"
        if credit.get("spent"):
            return "credit-spent"
    if over_budget(reading):
        return "budget"
    return None


class CreditError(ValueError):
    """A refused ``qswap credit`` change; the message is for the user."""


def set_credit(
    switcher, identifier: str, amount: object | None, *, expires: str | None = None,
    now: float | None = None,
) -> tuple[str, str, dict | None, bool]:
    """Record (``amount``) or remove (None) one API-key account's credit, as
    of ``now``. Stored on the account record; validated before any write.
    Returns ``(number, email, credit, changed)``."""
    from ai_quota_swap.admin_api import iso_z
    from ai_quota_swap.models import get_timestamp
    from datetime import datetime, timezone

    now = time.time() if now is None else now
    value = None
    if amount is not None:
        value = {
            "amount": normalize_credit(amount),
            "since": iso_z(datetime.fromtimestamp(now, tz=timezone.utc)),
        }
        if expires:
            value["expires"] = parse_expiry(expires)
    elif expires:
        raise CreditError("--expires goes with an amount: qswap credit N AMOUNT --expires DATE")
    num, email, data, record = switcher._resolve_for_policy_write(identifier)
    if record.get("kind") != "api_key":
        raise CreditError(
            f"#{num} {email} is a subscription account; a credit applies to "
            "API-key accounts"
        )
    if value is None:
        if "credit" not in record:
            return num, email, None, False
        record.pop("credit", None)
    else:
        record["credit"] = value
    data["lastUpdated"] = get_timestamp()
    switcher._write_json(switcher.sequence_file, data)
    return num, email, credit_from_record(record), True


# -- linking --------------------------------------------------------------------


def parse_hint(hint: object) -> tuple[str, str] | None:
    """``(head, tail)`` of a ``partial_key_hint``, or None when it cannot link."""
    if not isinstance(hint, str):
        return None
    for sep in _HINT_SEPARATORS:
        if hint.count(sep) == 1:
            head, tail = hint.split(sep)
            break
    else:
        return None
    if not head.startswith(_KEY_PREFIX) or len(head) <= len(_KEY_PREFIX):
        return None
    if len(tail) < MIN_HINT_TAIL:
        return None
    return head, tail


def hint_matches(hint: object, key: str) -> bool:
    """Whether the managed ``key`` is the key ``hint`` describes."""
    parts = parse_hint(hint)
    if parts is None or not key:
        return False
    head, tail = parts
    return (
        key.startswith(head)
        and key.endswith(tail)
        and len(key) > len(head) + len(tail)
    )


def key_fingerprint(key: str) -> str:
    """A one-way tag for a managed key, so the cache can remember which keys a
    listing was checked against without holding the keys."""
    return hashlib.sha256(("qswap-link\0" + key).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Link:
    org: str
    key_id: str
    hint: str
    key_name: str = ""


@dataclass
class LinkResult:
    links: dict[str, Link]
    notes: dict[str, str]


def resolve_links(
    orgs: list[Org],
    keys: dict[str, str],
    records: dict,
    *,
    now: float,
    network: bool,
    cache: AdminCache,
    force: bool = False,
) -> LinkResult:
    """Which key id each managed key is, per the module docstring. Lists an
    organization's keys only when an account needs it (see
    :func:`admin_api.api_key_list`). Never raises."""
    org_by_name = {o.name: o for o in orgs}
    links: dict[str, Link] = {}
    notes: dict[str, str] = {}
    pending: list[str] = []
    for num, key in keys.items():
        rec = records.get(num) if isinstance(records.get(num), dict) else {}
        org_name, key_id, hint = rec.get("apiOrg"), rec.get("apiKeyId"), rec.get("apiKeyHint")
        org = org_by_name.get(org_name) if isinstance(org_name, str) else None
        if (
            not force
            and org is not None
            and org.persistent
            and isinstance(key_id, str)
            and key_id
            and hint_matches(hint, key)
        ):
            links[num] = Link(org.name, key_id, hint)
        else:
            pending.append(num)
    unseen = {key_fingerprint(keys[n]) for n in pending if keys[n]}
    listings: dict[str, admin_api.KeyList] = {}
    refreshed = False
    for org in orgs:
        listing = admin_api.api_key_list(
            org,
            now=now,
            network=network and (bool(pending) or force),
            cache=cache,
            unseen=unseen,
            force=force,
        )
        listings[org.name] = listing
        refreshed = refreshed or listing.refreshed
    names = {
        (org_name, item.get("id")): item.get("name") or ""
        for org_name, listing in listings.items()
        for item in (listing.keys or [])
        if isinstance(item, dict)
    }
    for num, link in list(links.items()):
        links[num] = Link(link.org, link.key_id, link.hint, names.get((link.org, link.key_id), ""))
    # A refreshed listing re-decides every account, so a key archived or a
    # second key with the same hint is noticed; otherwise only the accounts
    # without a valid stored link are decided.
    targets = list(keys) if (refreshed or force) else pending
    matches: dict[str, list[tuple[str, dict]]] = {}
    for num in targets:
        found: list[tuple[str, dict]] = []
        for org in orgs:
            for item in listings[org.name].keys or []:
                if isinstance(item, dict) and hint_matches(item.get("hint"), keys[num]):
                    found.append((org.name, item))
        matches[num] = found
    kept = {(l.org, l.key_id) for n, l in links.items() if n not in targets}
    claims = Counter(
        (m[0][0], m[0][1].get("id")) for m in matches.values() if len(m) == 1
    )
    errors = [l.error for l in listings.values() if l.error is not None]
    listed_any = any(l.keys is not None for l in listings.values())
    for num in targets:
        found = matches[num]
        links.pop(num, None)
        if not found:
            if errors and not listed_any:
                notes[num] = errors[0].note
            elif not listed_any:
                notes[num] = "API keys not listed yet"
            else:
                notes[num] = "key not found in a registered organization"
        elif len(found) > 1:
            notes[num] = f"{len(found)} listed API keys share this key's hint; not linked"
        else:
            org_name, item = found[0]
            ident = (org_name, item.get("id"))
            if claims[ident] > 1 or ident in kept:
                notes[num] = "another managed account matches the same API key; not linked"
            else:
                links[num] = Link(org_name, item["id"], item.get("hint") or "", item.get("name") or "")
    return LinkResult(links, notes)


def persist_links(switcher, desired: dict[str, Link | None]) -> bool:
    """Write changed links onto the account records, under the registry lock.

    Only API-key records are touched, and only when a field changes, so a
    steady state writes nothing. Skipped (the links still apply for this pass)
    when the lock is busy. Returns whether ``sequence.json`` was written.
    """
    from ai_quota_swap.locking import FileLock
    from ai_quota_swap.models import get_timestamp

    data = switcher._get_sequence_data() or {}
    if not _links_differ(data, desired):
        return False
    lock = FileLock(switcher.lock_file, timeout=2.0)
    if not lock.acquire():
        return False
    try:
        data = switcher._get_sequence_data()
        if not data or not _links_differ(data, desired):
            return False
        accounts = data.get("accounts", {})
        for num, link in desired.items():
            rec = accounts.get(num)
            if not isinstance(rec, dict) or rec.get("kind") != "api_key":
                continue
            if link is None:
                for k in LINK_FIELDS:
                    rec.pop(k, None)
            else:
                rec.update({"apiKeyId": link.key_id, "apiOrg": link.org, "apiKeyHint": link.hint})
        data["lastUpdated"] = get_timestamp()
        switcher._write_json(switcher.sequence_file, data)
        return True
    finally:
        lock.release()


def _links_differ(data: dict, desired: dict[str, Link | None]) -> bool:
    accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
    for num, link in desired.items():
        rec = accounts.get(num)
        if not isinstance(rec, dict) or rec.get("kind") != "api_key":
            continue
        if link is None:
            if any(k in rec for k in LINK_FIELDS):
                return True
        elif (rec.get("apiKeyId"), rec.get("apiOrg"), rec.get("apiKeyHint")) != (
            link.key_id, link.org, link.hint
        ):
            return True
    return False


def unlink_org(switcher, org_name: str) -> list[str]:
    """Drop every link to ``org_name`` (``qswap api org remove``)."""
    data = switcher._get_sequence_data() or {}
    nums = [
        num for num, rec in data.get("accounts", {}).items()
        if isinstance(rec, dict) and rec.get("apiOrg") == org_name
    ]
    if nums:
        persist_links(switcher, {num: None for num in nums})
    return nums


# -- readings ---------------------------------------------------------------------


def _blank(source: str | None, used: float | None, today: float | None,
           unpriced: int, as_of: float | None) -> dict:
    return {
        "spend": {
            "used": None if used is None else round(used, 4),
            "limit": None,
            "pct": None,
            "currency": "USD",
        },
        "today": None if today is None else round(today, 4),
        "source": source,
        "as_of": as_of,
        "unpriced_tokens": unpriced,
    }


def _price_admin(
    days: dict[str, list[tuple[str, str | None, tuple[int, ...]]]],
    prices,
    today_label: str,
    month_label: str = "",
    since_label: str | None = None,
) -> tuple[float, float, int, float]:
    """``(month, today, unpriced tokens, since)`` from a report's daily
    buckets: this month from ``month_label`` (the report may start earlier
    for a credit), and from ``since_label`` for a credit (Anthropic's figures
    are whole UTC days, so the credit's first day counts in full)."""
    from ai_quota_swap.ledger.pricing import FAST_SUFFIX

    month = today = since = 0.0
    unpriced = 0
    for day, rows in days.items():
        for model, speed, toks in rows:
            found = prices.lookup(model + FAST_SUFFIX if speed == "fast" else model)
            if found is None:
                if day >= month_label:
                    unpriced += sum(toks)
                continue
            cost = found[1].cost(*toks)
            if day >= month_label:
                month += cost
            if day == today_label:
                today += cost
            if since_label is not None and day >= since_label:
                since += cost
    return month, today, unpriced, since


def readings(
    switcher,
    infos: dict[str, tuple],
    *,
    fetch: set[str] | None = None,
    now: float | None = None,
    env: dict | None = None,
) -> dict[str, dict]:
    """The reading for each API-key account in ``infos`` (``{num: account
    info tuple}`` as ``_build_accounts_info`` builds them). An account with no
    figure, no budget and no registered organization gets no reading, so it
    renders exactly as before.

    ``fetch`` follows the collector: None or a non-empty set allows Admin API
    reads (each organization at most once per ``CACHE_TTL_S``); an empty set
    allows none. Never raises :class:`AdminApiError`; the caller guards the rest.
    """
    from ai_quota_swap.ledger import spend as ledger_spend
    from ai_quota_swap.ledger.pricing import PriceTable
    from ai_quota_swap.ledger.store import DB_FILENAME

    if not infos:
        return {}
    now = time.time() if now is None else now
    root = Path(switcher.backup_dir)
    data = switcher._get_sequence_data() or {}
    records = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    registry = OrgRegistry(root, platform=getattr(switcher, "platform", None))
    orgs = registry.active_orgs(env)
    month, next_month, day, _next_day = admin_api.utc_month_bounds(now)
    network = fetch is None or bool(fetch)
    keys = {str(num): (info[5] or "").strip() for num, info in infos.items()}
    links: dict[str, Link] = {}
    notes: dict[str, str] = {}
    cache = AdminCache(root)
    if orgs:
        result = resolve_links(orgs, keys, records, now=now, network=network, cache=cache)
        links, notes = result.links, result.notes
        persistent = {o.name for o in orgs if o.persistent}
        desired: dict[str, Link | None] = {}
        for num in keys:
            link = links.get(num)
            desired[num] = link if link is not None and link.org in persistent else None
        persist_links(switcher, desired)
    org_by_name = {o.name: o for o in orgs}
    by_org: dict[str, list[str]] = defaultdict(list)
    for num, link in links.items():
        by_org[link.org].append(num)
    credits = {
        str(num): credit_from_record(records.get(str(num)) if isinstance(records.get(str(num)), dict) else None)
        for num in infos
    }
    reports: dict[str, tuple[ReportResult, dict | None]] = {}
    for org_name, nums in by_org.items():
        # A credit recorded before this month widens the report back to its day.
        starts = [
            credits[n]["since"] for n in nums
            if credits.get(n) is not None and credits[n]["since"] < month.timestamp()
        ]
        res = admin_api.month_usage_by_key(
            org_by_name[org_name], [links[n].key_id for n in nums],
            now=now, network=network, cache=cache,
            start=min(starts) if starts else None,
        )
        parsed = None
        if res.buckets is not None:
            try:
                parsed = admin_api.tokens_by_key_day(res.buckets)
            except AdminApiError as e:
                res = ReportResult(None, None, e)
        reports[org_name] = (res, parsed)

    prices = None
    today_label = day.date().isoformat()
    out: dict[str, dict] = {}
    for num, info in infos.items():
        num = str(num)
        record = records.get(num) if isinstance(records.get(num), dict) else {}
        budget = budget_from_record(record)
        credit = credits.get(num)
        credit_used: float | None = None
        link = links.get(num)
        reading: dict | None = None
        note = notes.get(num)
        if link is not None:
            res, parsed = reports[link.org]
            if parsed is not None:
                prices = prices or PriceTable.load(root)
                since_label = (
                    admin_api.utc_day_label(credit["since"]) if credit is not None else None
                )
                cost, today_cost, unpriced, since_cost = _price_admin(
                    parsed.get(link.key_id, {}), prices, today_label,
                    month_label=month.date().isoformat(), since_label=since_label,
                )
                if credit is not None:
                    credit_used = since_cost
                reading = _blank(SOURCE_ADMIN, cost, today_cost, unpriced, res.fetched_at)
                reading["org"] = link.org
                reading["api_key_id"] = link.key_id
                if link.key_name:
                    reading["api_key_name"] = link.key_name
                if res.stale and res.error is not None and res.error.kind != "not-read":
                    note = res.error.note
            else:
                error = res.error
                note = error.note if error is not None else "Admin API: no data"
        if reading is None:
            ls = ledger_spend.account_spend(
                root, info[1], info[3] or "",
                month_start=month.timestamp(), day_start=day.timestamp(),
                registry=data,
                since_start=credit["since"] if credit is not None else None,
            )
            if ls is not None:
                reading = _blank(SOURCE_LEDGER, ls.month, ls.today, ls.unpriced_tokens, ls.last_scan_at)
                if credit is not None:
                    credit_used = ls.since
            elif not note and (root / DB_FILENAME).exists():
                note = LEDGER_UNREADABLE
        if reading is None:
            if budget is None and credit is None and not orgs and not note:
                continue
            reading = _blank(None, None, None, 0, None)
        if credit is not None:
            reading["credit"] = credit_reading(credit, credit_used, reading.get("source"), now)
        spend = reading["spend"]
        spend["resets_at"] = admin_api.iso_z(next_month)
        if budget is not None:
            spend["limit"] = budget
            if spend["used"] is not None:
                spend["pct"] = round(spend["used"] / budget * 100.0, 2)
        reading["month"] = month.strftime("%Y-%m")
        if note:
            reading["note"] = note
        out[num] = reading
    return out


def budget_state(reading: object) -> tuple[float, float] | None:
    """``(used, limit)`` when the reading has both, else None (no budget, or
    no figure to hold it against: nothing is enforced then)."""
    if not isinstance(reading, dict):
        return None
    spend = reading.get("spend")
    if not isinstance(spend, dict):
        return None
    used, limit = spend.get("used"), spend.get("limit")
    if (
        isinstance(used, (int, float)) and not isinstance(used, bool)
        and isinstance(limit, (int, float)) and not isinstance(limit, bool)
        and limit > 0
    ):
        return float(used), float(limit)
    return None


def over_budget(reading: object) -> bool:
    state = budget_state(reading)
    return state is not None and state[0] >= state[1]


# -- text -----------------------------------------------------------------------


def money(value: float) -> str:
    return f"${value:,.2f}"


def budget_money(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "just now" if seconds < 60 else "1m ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def spend_unknown(reading: object) -> bool:
    """Whether the ledger exists but could not be read (:data:`LEDGER_UNREADABLE`)."""
    return isinstance(reading, dict) and reading.get("source") is None and (
        reading.get("note") == LEDGER_UNREADABLE
    )


def nothing_recorded(reading: object) -> bool:
    """Whether the figure qswap has for an API-key account shows no usage:
    this machine's estimate is zero today and this month with no unpriced
    tokens, or there is no figure yet only because nothing was ever scanned
    (no ledger file, no organization in the way: no note). An Admin API
    figure is never "nothing recorded": its zeros are Anthropic's, and are
    shown as they are; nor is a ledger that could not be read
    (:func:`spend_unknown`). No reading at all (no budget, no organization,
    no ledger file) counts too."""
    if reading is None:
        return True
    if not isinstance(reading, dict):
        return False
    source = reading.get("source")
    spend = reading.get("spend") if isinstance(reading.get("spend"), dict) else {}
    used = spend.get("used")
    if source == SOURCE_LEDGER:
        return used == 0 and not reading.get("today") and not reading.get("unpriced_tokens")
    return source is None and used is None and not reading.get("note")


def _nothing_spent(limit: float | None) -> str:
    """The budget form of nothing recorded: ``$0 of $500 this month``."""
    return f"$0 of {budget_money(limit)} this month" if limit is not None else ""


def credit_money(value: float) -> str:
    """Whole dollars for a credit, rounded down so the count never shows
    more than is left: ``$1,150``."""
    return f"${math.floor(max(value, 0.0) + 1e-9):,}"


def _day_words(iso: str) -> str:
    """``Dec 31`` (with the year when it is not this year's)."""
    from datetime import date

    try:
        day = date.fromisoformat(iso)
    except ValueError:
        return iso
    text = f"{day.strftime('%b')} {day.day}"
    if day.year != date.today().year:
        text += f" {day.year}"
    return text


def credit_text(credit: dict) -> str:
    """``$1,150 of $1,187 credit left``; ``credit expired Dec 31``; ``credit
    spent ($0 of $1,187 left)``; ``$1,187 credit, no spend figure yet``."""
    amount = budget_money(credit["amount"])
    if credit.get("expired"):
        return f"credit expired {_day_words(credit['expires'])}"
    if credit.get("spent"):
        return f"credit spent ($0 of {amount} left)"
    left = credit.get("left")
    if left is None:
        return f"{amount} credit, no spend figure yet"
    return f"{credit_money(left)} of {amount} credit left"


def spend_text(reading: dict) -> str:
    """``API key · $4.12 today · $212.40 this month of $500 (42%)``, then
    ``· estimate`` when the figure is the local one. With nothing recorded:
    ``API key · no usage recorded on this machine``, after ``$0 of $500 this
    month`` when there is a budget. With a credit: ``API key · $1,150 of
    $1,187 credit left``, and the month against its budget after it when one
    is set too."""
    from ai_quota_swap.display import API_KEY_NO_USAGE

    credit = reading.get("credit")
    if isinstance(credit, dict):
        parts = ["API key", credit_text(credit)]
        spend = reading.get("spend") or {}
        used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
        if limit is not None and used is not None and pct is not None:
            tag = f"{pct:.0f}%" + (", over budget" if pct >= 100 else "")
            parts.append(f"{money(used)} of {budget_money(limit)} this month ({tag})")
        if reading.get("source") == SOURCE_LEDGER and credit.get("left") is not None:
            parts.append("estimate")
        return " · ".join(parts)

    spend = reading.get("spend") or {}
    used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
    parts = ["API key"]
    if spend_unknown(reading):
        from ai_quota_swap.display import API_KEY_SPEND_UNKNOWN

        parts.append(f"{API_KEY_SPEND_UNKNOWN}: {LEDGER_UNREADABLE}")
        return " · ".join(parts)
    if nothing_recorded(reading):
        if limit is not None:
            parts.append(_nothing_spent(limit))
        parts.append(API_KEY_NO_USAGE)
        return " · ".join(parts)
    if used is None:
        parts.append("no spend data yet")
        if limit is not None:
            parts.append(f"budget {budget_money(limit)} a month")
        return " · ".join(parts)
    today = reading.get("today")
    if today is not None:
        parts.append(f"{money(today)} today")
    plus = "+" if reading.get("unpriced_tokens") else ""
    month = f"{money(used)}{plus} this month"
    if limit is not None and pct is not None:
        tag = f"{pct:.0f}%" + (", over budget" if pct >= 100 else "")
        month += f" of {budget_money(limit)} ({tag})"
    parts.append(month)
    if reading.get("source") == SOURCE_LEDGER:
        parts.append("estimate")
    return " · ".join(parts)


def detail_text(reading: dict, now: float | None = None) -> str | None:
    """A second line when there is more to say: why the figure is not
    Anthropic's, how old it is, or how to get one; for a credit, since when it
    counts and when it expires."""
    now = time.time() if now is None else now
    base = _detail_text(reading, now)
    credit = reading.get("credit")
    if not isinstance(credit, dict) or credit.get("expired"):
        return base
    since = str(credit.get("since") or "")[:10]
    words = f"credit counted from {_day_words(since)}" if since else ""
    if credit.get("expires"):
        words += f", expires {_day_words(credit['expires'])}"
    if not words:
        return base
    return f"{base} · {words}" if base else words


def _detail_text(reading: dict, now: float) -> str | None:
    note = reading.get("note")
    source = reading.get("source")
    as_of = reading.get("as_of")
    empty = nothing_recorded(reading)
    if source == SOURCE_LEDGER:
        old = isinstance(as_of, (int, float)) and now - as_of > LEDGER_STALE_S
        if note:
            # "no usage recorded on this machine" already says where from.
            return note if empty else f"{note}; estimate from this machine's transcripts"
        if old:
            what = "as of" if empty else "estimate as of"
            return f"{what} the last qswap usage, {_age(now - as_of)}"
        return None
    if empty or spend_unknown(reading):
        # Nothing scanned yet reads as nothing recorded, as it will after
        # the first scan of an unused key: the same state, the same words.
        # An unreadable ledger says so on the line above already.
        return None
    if source == SOURCE_ADMIN:
        if note:
            when = f", figures from {_age(now - as_of)}" if isinstance(as_of, (int, float)) else ""
            return f"{note}{when}"
        return None
    if note:
        return note
    return "run qswap usage for an estimate, or register the organization: qswap api org add"


def short_text(reading: dict) -> str:
    """For a one-line row: ``API key · $212.40 (42%)``."""
    spend = reading.get("spend") or {}
    used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
    if used is None:
        return "API key · no spend data"
    text = f"API key · {money(used)}"
    text += f" ({pct:.0f}%)" if limit is not None and pct is not None else " this month"
    if reading.get("source") == SOURCE_LEDGER:
        text += " est."
    return text


def menu_text(reading: dict) -> str:
    """For a menu bar row, within its note width: ``$4.12 today ·
    $212.40/$500 (42%)``; ``(104%, over budget)`` past the budget, as
    :func:`spend_text` says it. Nothing recorded with a budget: ``$0 of $500
    this month``."""
    from ai_quota_swap.display import API_KEY_NO_USAGE_SHORT

    credit = reading.get("credit")
    if isinstance(credit, dict):
        return credit_text(credit)
    spend = reading.get("spend") or {}
    used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
    if spend_unknown(reading):
        from ai_quota_swap.display import API_KEY_SPEND_UNKNOWN

        return API_KEY_SPEND_UNKNOWN
    if nothing_recorded(reading):
        return _nothing_spent(limit) or API_KEY_NO_USAGE_SHORT
    if used is None:
        return "API key · no spend data"
    today = reading.get("today")
    text = f"{money(today)} today · " if today is not None else ""
    text += money(used)
    if limit is not None and pct is not None:
        tag = f"{pct:.0f}%" + (", over budget" if pct >= 100 else "")
        text += f"/{budget_money(limit)} ({tag})"
    else:
        text += " this month"
    if reading.get("source") == SOURCE_LEDGER:
        text += " est."
    return text


# -- budget command ---------------------------------------------------------------


class BudgetError(ValueError):
    """A refused ``qswap budget`` change; the message is for the user."""


def set_budget(switcher, identifier: str, amount: object | None) -> tuple[str, str, float | None, bool]:
    """Set (``amount``) or remove (None) one API-key account's monthly budget.

    Stored like ``threshold``: a ``budget`` key on the account record, popped
    rather than written as null. Validated before any write. Returns
    ``(number, email, budget, changed)``.
    """
    from ai_quota_swap.models import get_timestamp

    normalized = None if amount is None else normalize_budget(amount)
    num, email, data, record = switcher._resolve_for_policy_write(identifier)
    if record.get("kind") != "api_key":
        raise BudgetError(
            f"#{num} {email} is a subscription account; budgets apply to "
            "API-key accounts"
        )
    current = budget_from_record(record)
    if normalized == current and (normalized is not None or "budget" not in record):
        return num, email, normalized, False
    if normalized is None:
        record.pop("budget", None)
    else:
        record["budget"] = normalized
    data["lastUpdated"] = get_timestamp()
    switcher._write_json(switcher.sequence_file, data)
    return num, email, normalized, True
