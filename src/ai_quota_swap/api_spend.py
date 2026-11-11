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
import re
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
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


class AmountError(ValueError):
    """An amount or a date the command line cannot take: a usage error
    (exit 64, docs/design/ux.md). The message is for the user."""


#: What an amount may look like: ``1187``, ``1187.03``, ``$1,187.03``,
#: ``1,187``. Commas only between groups of three digits, the first group
#: starting with 1 to 9 (``0,187`` is a decimal comma, not $187), at most two
#: decimals, ASCII digits only.
_AMOUNT_RE = re.compile(r"\$?(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?")


def normalize_budget(value: object) -> float:
    """A budget in dollars from ``500``, ``"49.50"``, ``"$1,200"``. Text is
    read strictly (:data:`_AMOUNT_RE`); a stored number is rounded to cents
    before the range check. Raises :class:`AmountError`."""
    shown = repr(value.strip()) if isinstance(value, str) else repr(value)
    example = "give US dollars such as 500 or 1,187.03"
    if isinstance(value, bool):
        raise AmountError(f"{shown} is not an amount; {example}")
    if isinstance(value, str):
        text = value.strip()
        if not _AMOUNT_RE.fullmatch(text):
            raise AmountError(f"{shown} is not an amount; {example}")
        amount = float(text.lstrip("$").replace(",", ""))
    elif isinstance(value, (int, float)):
        amount = float(value)
    else:
        raise AmountError(f"{shown} is not an amount; {example}")
    if not math.isfinite(amount):
        raise AmountError(f"{shown} is not an amount; {example}")
    amount = round(amount, 2)
    if amount <= 0:
        raise AmountError(f"{shown} is not above $0; {example}")
    if amount > BUDGET_MAX:
        raise AmountError(f"{shown} is over the most qswap takes, ${BUDGET_MAX:,.0f}")
    return amount


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
    return normalize_budget(value)


_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_expiry(value: str) -> str:
    """``YYYY-MM-DD`` (the last UTC day the credit can be used). Raises
    :class:`AmountError`."""
    from datetime import date

    text = str(value).strip()
    try:
        if not _DATE_RE.fullmatch(text):
            raise ValueError(text)
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise AmountError(
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


def credit_reading(
    credit: dict, used: float | None, source: str | None, now: float,
    *, unpriced_tokens: int = 0,
) -> dict:
    """The ``credit`` part of a reading: what is left of it. ``used`` counts
    ``unpriced_tokens`` (tokens of models with no price, over the whole
    credit period) at the dearest rate."""
    from ai_quota_swap.admin_api import iso_z
    from datetime import datetime, timezone

    left = None if used is None else round(credit["amount"] - used, 4)
    return {
        "unpriced_tokens": int(unpriced_tokens or 0),
        "amount": credit["amount"],
        "used": None if used is None else round(used, 4),
        "left": left,
        "since": iso_z(datetime.fromtimestamp(credit["since"], tz=timezone.utc)),
        "expires": credit["expires"],
        "expired": credit_expired(credit["expires"], now),
        "spent": left is not None and left <= 0,
        "source": source,
    }


def limit_reason(reading: object, *, latched: bool = True) -> str | None:
    """Why an API-key account is at its limit: ``credit-expired``,
    ``credit-spent`` or ``budget``; None when it is not (or nothing can
    tell). Whichever is reached first applies. With ``latched`` (the
    surfaces), a limit auto-switch judged reached earlier and still holds
    (:func:`latch_of`) counts too, though the figure now reads lower; the
    engine's census asks for the reading's own figures only. The engine adds
    the freshness rule on top (:func:`engine_limit_reason`)."""
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
    if latched:
        return latch_of(reading)
    return None


# -- a limit that was reached stays reached (decision A5) ---------------------------
#
# The engine records in its state file (``limitLatch``) that a key reached its
# budget or credit; a later reading under it whose source got worse (the
# Admin API's figure replaced by the ledger's lower estimate) does not make
# the key a candidate again. ``readings`` copies a latch that still applies
# into the reading (``latched``), so every surface says what the engine does.

#: The limits a latch holds.
LATCH_REASONS = frozenset({"budget", "credit-spent"})
#: The kind of limit each latched reason belongs to. ``limitLatch[N]`` holds
#: one latch per kind (decision EM8): ``qswap budget`` clears only the budget
#: latch, ``qswap credit`` only the credit latch, and the key stays out while
#: either holds.
LATCH_KINDS = {"credit-spent": "credit", "budget": "budget"}
#: On a row whose latch holds while its figure reads under the limit.
LATCHED_WORDS = {
    "budget": "budget reached earlier",
    "credit-spent": "credit counted as spent",
}


def latch_entries(stored: object) -> dict[str, dict]:
    """``{kind: latch}`` from one account's ``limitLatch`` entry, in either
    shape: ``{"credit": {...}, "budget": {...}}``, or the one latch
    ``{"reason": ...}`` an older engine wrote, read as the kind its reason
    names. Credit first, as :func:`limit_reason` orders them. Never raises."""
    if not isinstance(stored, dict):
        return {}
    reason = stored.get("reason")
    if isinstance(reason, str):
        kind = LATCH_KINDS.get(reason)
        return {kind: dict(stored)} if kind is not None else {}
    out: dict[str, dict] = {}
    for reason, kind in LATCH_KINDS.items():
        latch = stored.get(kind)
        if isinstance(latch, dict) and latch.get("reason") == reason:
            out[kind] = dict(latch)
    return out


def latched_reasons(stored: object, reading: object, record: object, now: float) -> list[str]:
    """The latched limits of one account (``limitLatch[N]``) that still apply
    to ``reading``: the same month and budget, the same credit. Credit first."""
    return [
        latch["reason"]
        for latch in latch_entries(stored).values()
        if latch_identity(latch["reason"], reading, record, now) == latch
    ]


def latch_identity(reason: str, reading: object, record: object, now: float) -> dict | None:
    """What a latch for ``reason`` records, so that it lapses with the thing
    it was about: the budget's month and amount, or the credit's start and
    amount. None when the reading and record cannot tell."""
    from ai_quota_swap.ledger.activations import parse_ts
    from datetime import datetime, timezone

    record = record if isinstance(record, dict) else None
    reading = reading if isinstance(reading, dict) else {}
    if reason == "budget":
        month = reading.get("month")
        if not isinstance(month, str) or not month:
            month = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")
        limit = budget_from_record(record)
        if limit is None:
            spend = reading.get("spend") if isinstance(reading.get("spend"), dict) else {}
            limit = spend.get("limit")
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            return None
        return {"reason": reason, "month": month, "limit": float(limit)}
    if reason == "credit-spent":
        credit = credit_from_record(record)
        if credit is not None:
            since, amount = credit["since"], credit["amount"]
        else:
            shown = reading.get("credit") if isinstance(reading.get("credit"), dict) else {}
            since, amount = parse_ts(shown.get("since")), shown.get("amount")
        if since is None or not isinstance(amount, (int, float)):
            return None
        return {"reason": reason, "credit": f"{int(since)}|{float(amount)}"}
    return None


def reached_latches(reading: object) -> list[str]:
    """Every latched kind of limit the reading's own figures reach:
    ``credit-spent`` and ``budget`` (both, when both are). An expired credit
    is decided by its date and needs no latch."""
    if not isinstance(reading, dict):
        return []
    out = []
    credit = reading.get("credit")
    if isinstance(credit, dict) and credit.get("spent") and not credit.get("expired"):
        out.append("credit-spent")
    if over_budget(reading):
        out.append("budget")
    return out


def latch_of(reading: object) -> str | None:
    """The latched limit a reading carries (``budget``, ``credit-spent``), or None."""
    latched = reading.get("latched") if isinstance(reading, dict) else None
    reason = latched.get("reason") if isinstance(latched, dict) else None
    return reason if reason in LATCH_REASONS else None


def _only_latched(reading: dict) -> str | None:
    """The latch, when it is the only reason the reading is at its limit."""
    return latch_of(reading) if limit_reason(reading, latched=False) is None else None


def _latches(root: Path) -> dict:
    """The engine's ``limitLatch``, or ``{}``. Never raises."""
    try:
        from ai_quota_swap.autoswitch import STATE_FILENAME, _read_state_file

        latches = _read_state_file(root / STATE_FILENAME).get("limitLatch")
    except Exception:
        return {}
    return latches if isinstance(latches, dict) else {}


# -- the engine's freshness rule ----------------------------------------------------
#
# Auto-switch holds an API key to its budget or credit only with a spend
# figure no older than SPEND_FRESH_S: the Admin API's ``as_of``, or the
# ledger's last scan. Without one the key counts as at its limit
# (``spend-unknown``): the engine does not enter it, and leaves it when it is
# active, whoever entered it. The engine scans the ledger itself while such a
# key is active (autoswitch.SPEND_SCAN_INTERVAL_S), so the figure stays fresh
# when nothing else runs. The rows say "limit not enforced" in the same state.

SPEND_FRESH_S = 20 * 60.0
#: The engine's reason for a key with a budget or credit and no fresh figure.
SPEND_UNKNOWN = "spend-unknown"
#: That reason in words, wherever the engine says why it left or stayed off a key.
SPEND_UNKNOWN_WORDS = "its spend could not be measured for 20 minutes"
#: The engine's reason for leaving a key it entered that has neither (A2).
NO_LIMIT = "no-limit"
NO_LIMIT_WORDS = "it has no budget or credit"
#: On a row whose budget or credit has no fresh figure to hold it against.
LIMIT_NOT_ENFORCED = "limit not enforced: no spend figure"


def has_limit(reading: object, record: object = None) -> bool:
    """Whether an API-key account has a budget or a credit: on its account
    record, or in its reading (which carries both when the record has them)."""
    if budget_from_record(record if isinstance(record, dict) else None) is not None:
        return True
    if credit_from_record(record if isinstance(record, dict) else None) is not None:
        return True
    if not isinstance(reading, dict):
        return False
    spend = reading.get("spend")
    limit = spend.get("limit") if isinstance(spend, dict) else None
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
        return True
    return isinstance(reading.get("credit"), dict)


def figure_age(reading: object, now: float) -> float | None:
    """How old the spend figure behind a reading is, in seconds; None when
    it has none (no figure, a credit with nothing counted, or no time)."""
    if not isinstance(reading, dict):
        return None
    spend = reading.get("spend")
    used = spend.get("used") if isinstance(spend, dict) else None
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    credit = reading.get("credit")
    if isinstance(credit, dict) and credit.get("left") is None:
        return None
    as_of = reading.get("as_of")
    if not isinstance(as_of, (int, float)) or isinstance(as_of, bool):
        return None
    return max(0.0, now - float(as_of))


def figure_fresh(reading: object, now: float) -> bool:
    """Whether the reading has a spend figure no older than :data:`SPEND_FRESH_S`.
    For a key linked to an organization only Anthropic's figure counts
    (decision EM2): this machine's ledger misses the other machines on the
    key, so its figure is never fresh for one (:func:`machine_only`)."""
    if machine_only(reading):
        return False
    age = figure_age(reading, now)
    return age is not None and age <= SPEND_FRESH_S


def machine_only(reading: object) -> bool:
    """Whether the reading is for a key linked to an organization but its
    figure is not Anthropic's: this machine's ledger (or none), shown but
    never held against the key's limit."""
    return (
        isinstance(reading, dict) and bool(reading.get("org"))
        and reading.get("source") != SOURCE_ADMIN
    )


def engine_limit_reason(reading: object, *, now: float, limited: bool) -> str | None:
    """Why the engine treats an API key as at its limit: :func:`limit_reason`
    first, then :data:`SPEND_UNKNOWN` for a key with a budget or credit
    (``limited``) whose figure is missing or older than :data:`SPEND_FRESH_S`."""
    reason = limit_reason(reading)
    if reason is not None:
        return reason
    if limited and not figure_fresh(reading, now):
        return SPEND_UNKNOWN
    return None


def limit_not_enforced(reading: object, now: float | None = None) -> bool:
    """Whether a row should say :data:`LIMIT_NOT_ENFORCED`: a budget or
    credit not yet reached, with no fresh figure to hold it against."""
    now = time.time() if now is None else now
    return (
        has_limit(reading)
        and limit_reason(reading) is None
        and not figure_fresh(reading, now)
    )


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
            if credit_expired(value["expires"], now):
                # The date is the last UTC day the credit can be used.
                raise AmountError(
                    f"--expires {value['expires']} is past (UTC); give the credit's "
                    "last usable day"
                )
    elif expires:
        raise AmountError("--expires goes with an amount: qswap credit N AMOUNT --expires DATE")
    num, email, data, record = switcher._resolve_for_policy_write(identifier)
    if record.get("kind") != "api_key":
        raise CreditError(
            f"{switcher.label(num, email, long=False)} is a subscription account; "
            "a credit applies to API-key accounts"
        )
    if value is None:
        if "credit" not in record:
            return num, email, None, False
        record.pop("credit", None)
    else:
        record["credit"] = value
    data["lastUpdated"] = get_timestamp()
    switcher._write_json(switcher.sequence_file, data)
    _clear_latch(switcher, num, "credit")
    return num, email, credit_from_record(record), True


def _clear_latch(switcher, number: str, kind: str) -> None:
    """A budget (``kind`` ``budget``) or a credit (``credit``) set again:
    auto-switch forgets that the old one was reached, and only that one
    (autoswitch.clear_limit_latch). Best-effort."""
    try:
        from ai_quota_swap.autoswitch import clear_limit_latch

        clear_limit_latch(Path(switcher.backup_dir), number, kind=kind)
    except Exception:
        pass


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


@dataclass(frozen=True)
class AdminPriced:
    month: float
    today: float
    unpriced: int  # this month
    since: float
    unpriced_since: int  # since the credit's day


def price_admin(
    days: dict[str, list[tuple[str, str | None, tuple[int, ...]]]],
    prices,
    today_label: str,
    month_label: str = "",
    since_label: str | None = None,
) -> AdminPriced:
    """Spend from a report's daily buckets: this month from ``month_label``
    (the report may start earlier for a credit), today, and from
    ``since_label`` for a credit (Anthropic's figures are whole UTC days, so
    the credit's first day counts in full). Tokens of a model with no price
    count at the table's dearest rate for their kind, never as zero."""
    from ai_quota_swap.ledger.pricing import FAST_SUFFIX

    month = today = since = 0.0
    unpriced = unpriced_since = 0
    dearest = None
    for day, rows in days.items():
        for model, speed, toks in rows:
            found = prices.lookup(model + FAST_SUFFIX if speed == "fast" else model)
            if found is None:
                dearest = dearest or prices.dearest()
                cost = dearest.cost(*toks)
                if day >= month_label:
                    unpriced += sum(toks)
                if since_label is not None and day >= since_label:
                    unpriced_since += sum(toks)
            else:
                cost = found[1].cost(*toks)
            if day >= month_label:
                month += cost
            if day == today_label:
                today += cost
            if since_label is not None and day >= since_label:
                since += cost
    return AdminPriced(month, today, unpriced, since, unpriced_since)


def _price_admin(
    days: dict[str, list[tuple[str, str | None, tuple[int, ...]]]],
    prices,
    today_label: str,
    month_label: str = "",
    since_label: str | None = None,
) -> tuple[float, float, int, float]:
    """``(month, today, unpriced tokens, since)``: :func:`price_admin` as a tuple."""
    p = price_admin(days, prices, today_label, month_label=month_label, since_label=since_label)
    return p.month, p.today, p.unpriced, p.since


_fresher = threading.local()


@contextmanager
def admin_max_age(seconds: float):
    """Within this block, this thread's :func:`readings` that may use the
    network ask Anthropic again for any answer older than ``seconds``,
    whatever :data:`admin_api.CACHE_TTL_S` says for the other readers: the
    auto-switch engine reads a linked key's figure itself this way, before it
    enters the key and every 5 minutes while it is on one (decision EM2)."""
    previous = getattr(_fresher, "seconds", None)
    _fresher.seconds = float(seconds)
    try:
        yield
    finally:
        _fresher.seconds = previous


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
        fresher = getattr(_fresher, "seconds", None)
        res = admin_api.month_usage_by_key(
            org_by_name[org_name], [links[n].key_id for n in nums],
            now=now, network=network, cache=cache,
            start=min(starts) if starts else None,
            ttl=None if fresher is None else min(admin_api.CACHE_TTL_S, fresher),
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
    latches = _latches(root)
    out: dict[str, dict] = {}
    for num, info in infos.items():
        num = str(num)
        record = records.get(num) if isinstance(records.get(num), dict) else {}
        budget = budget_from_record(record)
        credit = credits.get(num)
        credit_used: float | None = None
        credit_unpriced = 0
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
                priced = price_admin(
                    parsed.get(link.key_id, {}), prices, today_label,
                    month_label=month.date().isoformat(), since_label=since_label,
                )
                if credit is not None:
                    credit_used = priced.since
                    credit_unpriced = priced.unpriced_since
                reading = _blank(
                    SOURCE_ADMIN, priced.month, priced.today, priced.unpriced, res.fetched_at
                )
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
                    credit_unpriced = ls.unpriced_since
            elif not note and (root / DB_FILENAME).exists():
                note = LEDGER_UNREADABLE
        if reading is None:
            if budget is None and credit is None and not orgs and not note:
                continue
            reading = _blank(None, None, None, 0, None)
        if link is not None and reading.get("source") != SOURCE_ADMIN:
            # Linked, but Anthropic's figure is missing or past STALE_MAX_S:
            # this machine's figure is shown, never held to the limit (EM2).
            reading["org"] = link.org
        if credit is not None:
            reading["credit"] = credit_reading(
                credit, credit_used, reading.get("source"), now,
                unpriced_tokens=credit_unpriced,
            )
        spend = reading["spend"]
        spend["resets_at"] = admin_api.iso_z(next_month)
        if budget is not None:
            spend["limit"] = budget
            if spend["used"] is not None:
                spend["pct"] = round(spend["used"] / budget * 100.0, 2)
        reading["month"] = month.strftime("%Y-%m")
        if note:
            reading["note"] = note
        held = latched_reasons(latches.get(num), reading, record, now)
        if held:
            reading["latched"] = {"reason": held[0], "account": num}
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


def credit_money(value: float, *, cents: bool = False) -> str:
    """What is left of a credit, rounded down so the count never shows more
    than is left: ``$1,150``, or ``$1,150.03`` with ``cents`` (the amount
    has cents: both sides at one precision). Something left under the
    smallest unit shown reads ``under $1`` (``under $0.01``), never ``$0``."""
    value = max(value, 0.0)
    if cents:
        shown = math.floor(value * 100 + 1e-6) / 100
        return "under $0.01" if value > 0 and shown == 0 else f"${shown:,.2f}"
    whole = math.floor(value + 1e-9)
    return "under $1" if value > 0 and whole == 0 else f"${whole:,}"


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
    spent ($0 of $1,187 left)``; ``$1,187 credit`` when nothing is counted
    yet (the row adds :data:`LIMIT_NOT_ENFORCED`)."""
    amount = budget_money(credit["amount"])
    cents = not float(credit["amount"]).is_integer()
    if credit.get("expired"):
        return f"credit expired {_day_words(credit['expires'])}"
    if credit.get("spent"):
        return f"credit spent ({'$0.00' if cents else '$0'} of {amount} left)"
    left = credit.get("left")
    if left is None:
        return f"{amount} credit"
    plus = "+" if credit.get("unpriced_tokens") else ""
    return f"{credit_money(left, cents=cents)}{plus} of {amount} credit left"


def _has_figure(reading: dict) -> bool:
    spend = reading.get("spend") if isinstance(reading.get("spend"), dict) else {}
    return spend.get("used") is not None


def spend_text(reading: dict, now: float | None = None) -> str:
    """``API key · $4.12 today · $212.40 this month of $500 (42%)``, then
    ``· estimate`` when the figure is the local one. With nothing recorded:
    ``API key · no usage recorded on this machine``, after ``$0 of $500 this
    month`` when there is a budget. With a credit: ``API key · $1,150 of
    $1,187 credit left``, and the month against its budget after it when one
    is set too. A budget or credit with no fresh figure ends in
    :data:`LIMIT_NOT_ENFORCED` (``now`` judges the age)."""
    from ai_quota_swap.display import API_KEY_NO_USAGE

    unenforced = limit_not_enforced(reading, now)
    tail = [LIMIT_NOT_ENFORCED] if unenforced else []
    if only := _only_latched(reading):
        tail.append(LATCHED_WORDS[only])
    credit = reading.get("credit")
    if isinstance(credit, dict):
        parts = ["API key", credit_text(credit)]
        spend = reading.get("spend") or {}
        used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
        if limit is not None and used is not None and pct is not None:
            tag = f"{pct:.0f}%" + (", over budget" if pct >= 100 else "")
            parts.append(f"{money(used)} of {budget_money(limit)} this month ({tag})")
        if reading.get("source") == SOURCE_LEDGER and credit.get("left") is not None:
            parts.append(_estimate_word(reading))
        return " · ".join(parts + tail)

    spend = reading.get("spend") or {}
    used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
    parts = ["API key"]
    if spend_unknown(reading):
        from ai_quota_swap.display import API_KEY_SPEND_UNKNOWN

        # The sentence already says the figure is unknown and what to run.
        parts.append(f"{API_KEY_SPEND_UNKNOWN}: {LEDGER_UNREADABLE}")
        return " · ".join(parts)
    if unenforced and not _has_figure(reading):
        # A budget and nothing to hold it against: no "$0 of $500", which
        # would read as measured.
        if limit is not None:
            parts.append(f"budget {budget_money(limit)} a month")
        return " · ".join(parts + tail)
    if nothing_recorded(reading):
        if limit is not None:
            parts.append(_nothing_spent(limit))
        parts.append(API_KEY_NO_USAGE)
        return " · ".join(parts + tail)
    if used is None:
        parts.append("no spend data yet")
        if limit is not None:
            parts.append(f"budget {budget_money(limit)} a month")
        return " · ".join(parts + tail)
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
        parts.append(_estimate_word(reading))
    return " · ".join(parts + tail)


def _estimate_word(reading: dict) -> str:
    """How a row marks this machine's figure: ``estimate``, or ``this
    machine only`` for a key linked to an organization (:func:`machine_only`)."""
    return "this machine only" if machine_only(reading) else "estimate"


#: The detail line's next step while the limit is not enforced. Auto-switch
#: scans before it moves to a key, so this says it measures, never that it
#: stays off (decision EM6).
UNENFORCED_NEXT = (
    "auto-switch measures the spend before it moves to this key; measure it now "
    "with: qswap usage"
)
#: The same for a key linked to an organization, held to Anthropic's figure only.
ORG_UNENFORCED_NEXT = (
    "auto-switch reads Anthropic's figure before it moves to this key; read it now "
    "with: qswap budget"
)


def credit_counted_from(credit: dict, source: str | None) -> str:
    """From when spend counts against a credit: the start of its UTC day for
    Anthropic's figures (daily buckets), the start of its hour for the
    ledger's (hourly): ``the start of Nov 10 UTC``, ``14:00 UTC Nov 10``."""
    from ai_quota_swap.ledger.activations import parse_ts
    from datetime import datetime, timezone

    since = parse_ts(credit.get("since"))
    if since is None:
        return ""
    at = datetime.fromtimestamp(since, tz=timezone.utc)
    day = _day_words(at.date().isoformat())
    if source == SOURCE_ADMIN:
        return f"the start of {day} UTC"
    return f"{at.hour:02d}:00 UTC {day}"


def detail_text(
    reading: dict, now: float | None = None, *, with_credit: bool = True,
) -> str | None:
    """A second line when there is more to say: why the figure is not
    Anthropic's, how old it is, or how to get one; for a credit, since when it
    counts and when it expires. While the limit is not enforced it says how
    old the figure is and what auto-switch does about it."""
    now = time.time() if now is None else now
    base = _detail_text(reading, now)
    unpriced = _unpriced_words(reading)
    if unpriced:
        base = f"{unpriced} · {base}" if base else unpriced
    latched = _latched_words(reading)
    if latched:
        base = f"{base} · {latched}" if base else latched
    if limit_not_enforced(reading, now) and not spend_unknown(reading):
        age = figure_age(reading, now) if _has_figure(reading) else None
        as_of = reading.get("as_of")
        org = machine_only(reading)  # why: Anthropic's figure, not the ledger's age
        if (
            not org and reading.get("source") == SOURCE_LEDGER
            and isinstance(as_of, (int, float))
            and (base is None or not base.startswith(("estimate as of", "as of")))
        ):
            what = "as of" if nothing_recorded(reading) else "estimate as of"
            base = f"{what} the last qswap usage, {_age(now - as_of)}"
        elif not org and age is None and reading.get("source") != SOURCE_ADMIN:
            base = None  # the row says there is no figure; the next step follows
        step = ORG_UNENFORCED_NEXT if org else UNENFORCED_NEXT
        base = f"{base}; {step}" if base else step
    credit = reading.get("credit")
    if not with_credit or not isinstance(credit, dict) or credit.get("expired"):
        return base
    since = credit_counted_from(credit, reading.get("source") or credit.get("source"))
    words = f"credit counted from {since}" if since else ""
    if credit.get("expires"):
        words += f", expires {_day_words(credit['expires'])}"
    if not words:
        return base
    return f"{base} · {words}" if base else words


def latched_fix(reading: object) -> str | None:
    """The command that clears the latch a row names (:func:`_latched_words`):
    ``qswap budget 5 AMOUNT`` or ``qswap credit 5 AMOUNT``; None when the row
    names none (no latch, or the figure itself is at the limit). For the
    ``limit-latched`` entry of ``keyNotes`` (decision DS13)."""
    if not isinstance(reading, dict):
        return None
    only = _only_latched(reading)
    if only is None:
        return None
    number = (reading.get("latched") or {}).get("account") or "N"
    return f"qswap {'budget' if only == 'budget' else 'credit'} {number} AMOUNT"


def _latched_words(reading: dict) -> str | None:
    """Why the row says :data:`LATCHED_WORDS`, and the command that clears it."""
    fix = latched_fix(reading)
    if fix is None:
        return None
    if _only_latched(reading) == "budget":
        return (
            "an earlier figure reached the budget; auto-switch counts it as reached "
            f"until the month ends or it is set again: {fix}"
        )
    return (
        "an earlier figure spent the credit; auto-switch counts it as spent until "
        f"it is recorded again: {fix}"
    )


def _unpriced_words(reading: dict) -> str | None:
    """What the row's ``+`` means, said once: the tokens of models with no
    price, which count at the dearest rate."""
    credit = reading.get("credit") if isinstance(reading.get("credit"), dict) else {}
    tokens = max(int(reading.get("unpriced_tokens") or 0), int(credit.get("unpriced_tokens") or 0))
    if not tokens:
        return None
    from ai_quota_swap.ledger.report import fmt_tokens

    return f"+ counts {fmt_tokens(tokens)} tokens of models with no price at the dearest rate"


def _detail_text(reading: dict, now: float) -> str | None:
    note = reading.get("note")
    source = reading.get("source")
    as_of = reading.get("as_of")
    empty = nothing_recorded(reading)
    if source == SOURCE_LEDGER:
        old = isinstance(as_of, (int, float)) and now - as_of > LEDGER_STALE_S
        if note:
            # "no usage recorded on this machine" already says where from.
            only = " only" if machine_only(reading) else ""
            return note if empty else f"{note}; estimate from this machine's transcripts{only}"
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


#: :data:`LIMIT_NOT_ENFORCED` for a menu row, whose tooltip says the rest.
LIMIT_NOT_ENFORCED_SHORT = "limit not enforced"


def menu_text(
    reading: dict, now: float | None = None, *, today: bool = True, credit: bool = True,
) -> str:
    """For a menu bar row, within its note width: ``$4.12 today ·
    $212.40/$500 (42%)``; ``(104%, over budget)`` past the budget, as
    :func:`spend_text` says it. Nothing recorded with a budget: ``$0 of $500
    this month``. A budget or credit with no fresh figure ends in ``limit
    not enforced``. ``today`` False leaves today's figure out, for a row
    with no room for it; ``credit`` False leaves out the credit that follows a
    monthly budget reached (the reason), so a row short of room never cuts
    the credit's figure."""
    unenforced = limit_not_enforced(reading, now) and not spend_unknown(reading)
    text = _menu_text(reading, unenforced, today=today, with_credit=credit)
    if only := _only_latched(reading):
        return f"{text} · {LATCHED_WORDS[only]}"
    return f"{text} · {LIMIT_NOT_ENFORCED_SHORT}" if unenforced else text


def _menu_text(
    reading: dict, unenforced: bool, *, today: bool = True, with_credit: bool = True,
) -> str:
    from ai_quota_swap.display import API_KEY_NO_USAGE_SHORT

    credit = reading.get("credit")
    if isinstance(credit, dict):
        text = credit_text(credit)
        if (
            reading.get("source") == SOURCE_LEDGER
            and credit.get("left") is not None
            and not credit.get("expired")
        ):
            text += " est."
        if limit_reason(reading) == "budget":
            # At its monthly budget with credit left: the budget is why.
            budget = {k: v for k, v in reading.items() if k != "credit"}
            budget["today"] = None  # the row's room goes to the reason
            if not with_credit:
                return _menu_text(budget, False)
            return f"{_menu_text(budget, False)} · {credit_text(credit)}"
        return text
    spend = reading.get("spend") or {}
    used, limit, pct = spend.get("used"), spend.get("limit"), spend.get("pct")
    if unenforced and used is None and limit is not None:
        return f"budget {budget_money(limit)}"
    if spend_unknown(reading):
        from ai_quota_swap.display import API_KEY_SPEND_UNKNOWN

        return API_KEY_SPEND_UNKNOWN
    if nothing_recorded(reading):
        return _nothing_spent(limit) or API_KEY_NO_USAGE_SHORT
    if used is None:
        return "API key · no spend data"
    # While the limit is not enforced the row's room goes to saying so.
    spent_today = None if unenforced or not today else reading.get("today")
    text = f"{money(spent_today)} today · " if spent_today is not None else ""
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
            f"{switcher.label(num, email, long=False)} is a subscription account; "
            "budgets apply to API-key accounts"
        )
    current = budget_from_record(record)
    if normalized == current and (normalized is not None or "budget" not in record):
        if normalized is not None:
            _clear_latch(switcher, num, "budget")  # set again: the old one is forgotten
        return num, email, normalized, False
    if normalized is None:
        record.pop("budget", None)
    else:
        record["budget"] = normalized
    data["lastUpdated"] = get_timestamp()
    switcher._write_json(switcher.sequence_file, data)
    _clear_latch(switcher, num, "budget")
    return num, email, normalized, True
