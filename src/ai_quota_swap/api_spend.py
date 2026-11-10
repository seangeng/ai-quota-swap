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
) -> tuple[float, float, int]:
    from ai_quota_swap.ledger.pricing import FAST_SUFFIX

    month = today = 0.0
    unpriced = 0
    for day, rows in days.items():
        for model, speed, toks in rows:
            found = prices.lookup(model + FAST_SUFFIX if speed == "fast" else model)
            if found is None:
                unpriced += sum(toks)
                continue
            cost = found[1].cost(*toks)
            month += cost
            if day == today_label:
                today += cost
    return month, today, unpriced


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
    reports: dict[str, tuple[ReportResult, dict | None]] = {}
    for org_name, nums in by_org.items():
        res = admin_api.month_usage_by_key(
            org_by_name[org_name], [links[n].key_id for n in nums],
            now=now, network=network, cache=cache,
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
        link = links.get(num)
        reading: dict | None = None
        note = notes.get(num)
        if link is not None:
            res, parsed = reports[link.org]
            if parsed is not None:
                prices = prices or PriceTable.load(root)
                cost, today_cost, unpriced = _price_admin(
                    parsed.get(link.key_id, {}), prices, today_label
                )
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
            )
            if ls is not None:
                reading = _blank(SOURCE_LEDGER, ls.month, ls.today, ls.unpriced_tokens, ls.last_scan_at)
        if reading is None:
            if budget is None and not orgs:
                continue
            reading = _blank(None, None, None, 0, None)
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


def spend_text(reading: dict) -> str:
    """``API key · $4.12 today · $212.40 this month of $500 (42%)``, then
    ``· estimate`` when the figure is the local one."""
    spend = reading.get("spend") or {}
    used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
    parts = ["API key"]
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
    Anthropic's, how old it is, or how to get one."""
    now = time.time() if now is None else now
    note = reading.get("note")
    source = reading.get("source")
    as_of = reading.get("as_of")
    if source == SOURCE_LEDGER:
        old = isinstance(as_of, (int, float)) and now - as_of > LEDGER_STALE_S
        if note:
            return f"{note}; estimate from this machine's transcripts"
        if old:
            return f"estimate as of the last qswap usage, {_age(now - as_of)}"
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
    :func:`spend_text` says it."""
    spend = reading.get("spend") or {}
    used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
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
