"""A setup-token kept as the backup of a subscription login (experimental).

docs/design/backup-token.md. A Claude subscription login ends about a month
after its sign-in; ``claude setup-token`` prints a token for the same
subscription that lasts about a year. ``qswap add-token --backup-for N``
stores such a token as an ordinary setup-token account whose record names
its login: ``backupFor`` (the login's account number), ``backupForEmail``
and ``backupForOrganization`` (who that login is, so a link that ends up
naming another account is dropped), and ``backupAdded`` (when the token was
added, ISO 8601). While the login works the token stands by: it is not a
candidate and is never read. Once the login needs re-login for an auth
reason, the token stands in for it.

Pure: plain data in, plain data out, no I/O. The switcher reads the registry
and the usage store and asks these functions; the engine asks the switcher.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

#: Record keys on the backup token's account.
BACKUP_FOR = "backupFor"
BACKUP_FOR_EMAIL = "backupForEmail"
BACKUP_FOR_ORG = "backupForOrganization"
BACKUP_ADDED = "backupAdded"
LINK_KEYS = (BACKUP_FOR, BACKUP_FOR_EMAIL, BACKUP_FOR_ORG, BACKUP_ADDED)

#: How long a setup-token works, as ``claude setup-token`` says ("about a
#: year"). Nothing in the token says so; the date shown is an estimate.
TOKEN_LIFETIME_S = 365 * 86400.0
#: From this close to the end, every view and ``qswap doctor`` say to make a
#: new token.
RENEW_WITHIN_S = 30 * 86400.0

#: The identity check (``compare``). Two reset times of one window agree
#: within ``RESET_TOLERANCE_S``: the usage endpoint stamps a reset with
#: sub-second jitter and the rate-limit headers carry whole epoch seconds.
#: Windows seem to start on the hour, so two accounts first used in the same
#: hour share their resets: the figures must agree too, within
#: ``PCT_TOLERANCE`` points, and a window at or under ``NEAR_ZERO_PCT`` on
#: both sides says nothing (an unused account and a barely used one look
#: alike).
RESET_TOLERANCE_S = 10.0
PCT_TOLERANCE = 5.0
NEAR_ZERO_PCT = 1.0

#: The token's states. ``standing-by``: its login works (or is disabled) and
#: it is not the active account, so it is never read and never a candidate.
#: ``standing-in``: its login needs re-login; it takes the login's place.
#: ``in-use``: it is the active account while its login works (switched to
#: by hand, or auto-switch has not moved back yet).
STANDING_BY = "standing-by"
STANDING_IN = "standing-in"
IN_USE = "in-use"
STATES = (STANDING_BY, STANDING_IN, IN_USE)


def _accounts(data: Mapping | None) -> Mapping:
    accounts = (data or {}).get("accounts")
    return accounts if isinstance(accounts, Mapping) else {}


def _order(num: str) -> tuple[int, object]:
    return (0, int(num)) if num.isdigit() else (1, num)


def is_login(record: object) -> bool:
    """Whether a record is a subscription login a backup can name: not an
    API key, not a token's account (``add-token`` stores those without an
    organization; every subscription login has one), and no backup itself."""
    return (
        isinstance(record, Mapping)
        and record.get("kind") != "api_key"
        and bool(record.get("organizationUuid"))
        and parent_of(record) is None
    )


def names_login(record: Mapping, login: object) -> bool:
    """Whether a token's record names this login record: the email and
    organization stored with the link are the login's."""
    if not isinstance(login, Mapping):
        return False
    return (
        record.get(BACKUP_FOR_EMAIL) == login.get("email")
        and (record.get(BACKUP_FOR_ORG) or "") == (login.get("organizationUuid") or "")
    )


def _candidates(data: Mapping | None) -> dict[str, str]:
    """Every record whose link names a managed subscription login that is
    the account it was made for: token -> login."""
    accounts = _accounts(data)
    out: dict[str, str] = {}
    for num, record in accounts.items():
        parent = parent_of(record)
        if parent is None or parent == str(num):
            continue
        login = accounts.get(parent)
        if is_login(login) and names_login(record, login):
            out[str(num)] = parent
    return out


def _added_key(record: object) -> float:
    added = added_at(record)
    return added if added is not None else float("-inf")


def links(data: Mapping | None) -> dict[str, str]:
    """Backup token's account number -> its login's, from a loaded
    ``sequence.json``. Left out: a link whose login is not managed, is not
    a subscription login, or is not the account the link was made for (its
    email and organization); and a second backup of one login, of which the
    one added last is kept. Each such token is an ordinary setup-token
    account (``qswap doctor`` warns about it)."""
    accounts = _accounts(data)
    best: dict[str, str] = {}
    for token, parent in sorted(_candidates(data).items(), key=lambda kv: _order(kv[0])):
        kept = best.get(parent)
        if kept is None or _added_key(accounts.get(token)) > _added_key(accounts.get(kept)):
            best[parent] = token
    return {token: parent for parent, token in best.items()}


def duplicates(data: Mapping | None) -> dict[str, str]:
    """Tokens that name a login another backup already covers: token ->
    the backup in use for that login."""
    kept = links(data)
    by_parent = backups_by_parent(kept)
    return {
        token: by_parent[parent]
        for token, parent in _candidates(data).items()
        if token not in kept
    }


def parent_of(record: object) -> str | None:
    """The login number a record names as its parent, or None."""
    if not isinstance(record, Mapping):
        return None
    raw = record.get(BACKUP_FOR)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return str(raw)
    if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
        return str(int(raw))
    return None


def dangling(data: Mapping | None) -> dict[str, str]:
    """Records whose link names no login it can use (not managed, not a
    subscription login, or another account than the one it was made for):
    number -> the number they name. Second backups are :func:`duplicates`."""
    good = _candidates(data)
    out = {}
    for num, record in _accounts(data).items():
        parent = parent_of(record)
        if parent is not None and str(num) not in good:
            out[str(num)] = parent
    return out


def backups_by_parent(link_map: Mapping[str, str]) -> dict[str, str]:
    """Login number -> its backup token's, the inverse of :func:`links`."""
    return {parent: token for token, parent in link_map.items()}


def renumber(data: dict, moves: Mapping[str, str]) -> None:
    """Follow accounts that changed number (``swap``, ``move``): every link
    to a number in ``moves`` names the new one. In place."""
    if not moves:
        return
    for record in _accounts(data).values():
        parent = parent_of(record)
        if parent is not None and parent in moves:
            record[BACKUP_FOR] = str(moves[parent])


def unlink(data: dict, parent: str) -> list[str]:
    """Drop every link to ``parent`` (its account is gone or replaced by
    another): the tokens stay, as ordinary setup-token accounts. In place;
    the numbers of the tokens unlinked."""
    out = []
    for num, record in _accounts(data).items():
        if parent_of(record) == str(parent):
            for key in LINK_KEYS:
                record.pop(key, None)
            out.append(str(num))
    return sorted(out, key=_order)


def parse_iso(value: object) -> float | None:
    """Epoch seconds of an ISO-8601 instant, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def iso(epoch: float | None) -> str | None:
    """``2027-11-23T09:00:00Z`` for an epoch, None for None."""
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def added_at(record: object) -> float | None:
    """When the token was added (``backupAdded``, else the record's
    ``added``), as epoch seconds."""
    if not isinstance(record, Mapping):
        return None
    return parse_iso(record.get(BACKUP_ADDED)) or parse_iso(record.get("added"))


def ends_at(record: object) -> float | None:
    """About when the token stops working: a year after it was added."""
    added = added_at(record)
    return None if added is None else added + TOKEN_LIFETIME_S


def due(ends: float | None, now: float) -> bool:
    """Whether a token ending at ``ends`` should be made again now."""
    return ends is not None and ends - now <= RENEW_WITHIN_S


def link_fields(parent: str, login: Mapping, added: str) -> dict:
    """The keys a token's record carries to name ``login`` (in ``parent``)."""
    return {
        BACKUP_FOR: str(parent),
        BACKUP_FOR_EMAIL: login.get("email", ""),
        BACKUP_FOR_ORG: login.get("organizationUuid", "") or "",
        BACKUP_ADDED: added,
    }


def state(*, parent_ended: bool, active: bool, standing_by_read: bool = False) -> str:
    """The token's state. ``standing_by_read``: the collector left it unread
    as standing by this pass, which the views follow so that one pass never
    shows a state its reading disagrees with."""
    if standing_by_read:
        return STANDING_BY
    if parent_ended:
        return STANDING_IN
    return IN_USE if active else STANDING_BY


@dataclass(frozen=True)
class Link:
    """One backup token as the views show it: its account number, its
    login's, its state and its dates (epoch seconds). ``covers``: it can
    take its login's place now (standing in, not disabled, its token saved),
    which is what the login's row says."""

    number: str
    parent: str
    state: str
    added_at: float | None = None
    ends_at: float | None = None
    covers: bool = False

    def to_json(self) -> dict:
        """The ``backupFor`` field of the token's ``--json`` row."""
        return {
            "accountNumber": int(self.parent) if self.parent.isdigit() else self.parent,
            "state": self.state,
            "added": iso(self.added_at),
            "endsAbout": iso(self.ends_at),
        }


def ends_words(ends: float | None) -> str:
    """``about Nov 23, 2027``, or ``""``."""
    from ai_quota_swap.resets import use_by  # the local date, as "use by" says it

    when = use_by(ends, year=True)
    return f"about {when}" if when else ""


# -- the identity check --------------------------------------------------------------

#: The windows compared, with their words.
_WINDOWS = (("five_hour", "5-hour"), ("seven_day", "weekly"))

#: An organization id as the API writes one elsewhere. A header in another
#: form is not compared: its form is not known from a real response.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass(frozen=True)
class Match:
    """What :func:`compare` found. ``verdict`` is ``same`` (the weekly
    window agrees and nothing disagrees), ``other`` (the token is another
    account's) or ``unknown`` (it cannot be told); ``why`` says it in words;
    ``agreed`` names the windows that agreed."""

    verdict: str
    why: str = ""
    agreed: tuple[str, ...] = ()


def _window(usage: object, key: str) -> Mapping | None:
    if not isinstance(usage, Mapping):
        return None
    window = usage.get(key)
    return window if isinstance(window, Mapping) else None


def _reset(usage: object, key: str) -> float | None:
    window = _window(usage, key)
    return parse_iso(window.get("resets_at")) if window is not None else None


def _pct(usage: object, key: str) -> float | None:
    window = _window(usage, key)
    pct = window.get("pct") if window is not None else None
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return float(pct)


def nothing_to_compare(parent: object, *, now: float) -> str:
    """Why a login's reading cannot make a match, or ``""`` when it can.
    A match needs its weekly window: running (its reset ahead) and used
    (over :data:`NEAR_ZERO_PCT`). Checked before any request is sent."""
    reset = _reset(parent, "seven_day")
    pct = _pct(parent, "seven_day")
    if reset is None or pct is None:
        return "there is no weekly usage reading of the login to compare with"
    if reset <= now:
        return "the login's weekly window has ended since its last reading"
    if pct <= NEAR_ZERO_PCT:
        return "the login's weekly window shows no use yet"
    return ""


def organization_differs(parent_org: str, token_org: str | None) -> bool:
    """Whether the response names, in the form of an organization id,
    another organization than the login's."""
    if not token_org or not parent_org or not _UUID.match(token_org.strip()):
        return False
    return token_org.strip().lower() != parent_org.strip().lower()


def compare(
    parent: object,
    token: object,
    *,
    now: float,
    fresh: bool,
    parent_org: str = "",
    token_org: str | None = None,
) -> Match:
    """Whether a token's reading (``token``, normalized as
    ``oauth.build_usage_result`` returns it) is the same account as its
    login's reading (``parent``).

    One account has one 5-hour and one weekly window: the same reset times
    and the same figures. A window counts while its reset on the login's
    side is ahead (an ended window says nothing about the next, and the
    token's request may have started one), and not when both sides are at
    or under :data:`NEAR_ZERO_PCT`. ``same`` needs the weekly window to
    agree. A disagreement is ``other`` only against a ``fresh`` reading of
    the login, taken for this check; against an older one a reset spent
    since, or a slow clock, could explain it, so it is ``unknown``. An
    organization header (:func:`organization_differs`) that names another
    organization makes a match ``unknown`` and a disagreement ``other``; one
    that matches proves nothing alone (a team's members share one).
    """
    agreed: list[str] = []
    disagreed = ""
    for key, words in _WINDOWS:
        mine, theirs = _reset(parent, key), _reset(token, key)
        mine_pct, theirs_pct = _pct(parent, key), _pct(token, key)
        if mine is None or mine <= now or theirs is None or mine_pct is None or theirs_pct is None:
            continue
        if mine_pct <= NEAR_ZERO_PCT and theirs_pct <= NEAR_ZERO_PCT:
            continue  # unused on both sides: no evidence either way
        if abs(mine - theirs) > RESET_TOLERANCE_S:
            disagreed = disagreed or f"its {words} window resets at another time"
        elif abs(mine_pct - theirs_pct) > PCT_TOLERANCE:
            disagreed = disagreed or (
                f"its {words} window is {theirs_pct:.0f}% used, the login's {mine_pct:.0f}%"
            )
        else:
            agreed.append(words)
    org = organization_differs(parent_org, token_org)
    if disagreed:
        if fresh or org:
            return Match("other", disagreed + ("; it names another organization" if org else ""))
        return Match("unknown", f"{disagreed}, but the login's reading is not from now")
    if org:
        return Match("unknown", "the response names another organization than the login's")
    if "weekly" in agreed:
        return Match("same", agreed=tuple(agreed))
    return Match(
        "unknown",
        nothing_to_compare(parent, now=now) or "the check request carried no weekly figures",
    )
