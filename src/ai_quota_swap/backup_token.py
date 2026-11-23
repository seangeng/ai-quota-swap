"""A setup-token kept as the backup of a subscription login (experimental).

docs/design/backup-token.md. A Claude subscription login ends about a month
after its sign-in; ``claude setup-token`` prints a token for the same
subscription that lasts about a year. ``qswap add-token --backup-for N``
stores such a token as an ordinary setup-token account whose record names
its login: ``backupFor`` (the login's account number) and ``backupAdded``
(when the token was added, ISO 8601). While the login works the token stands
by: it is not a candidate and is never read. Once the login needs re-login
for an auth reason, the token stands in for it.

Pure: plain data in, plain data out, no I/O. The switcher reads the registry
and the usage store and asks these functions; the engine asks the switcher.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

#: Record keys on the backup token's account.
BACKUP_FOR = "backupFor"
BACKUP_ADDED = "backupAdded"

#: How long a setup-token works, as ``claude setup-token`` says ("about a
#: year"). Nothing in the token says so; the date shown is an estimate.
TOKEN_LIFETIME_S = 365 * 86400.0
#: From this close to the end, every view and ``qswap doctor`` say to make a
#: new token.
RENEW_WITHIN_S = 30 * 86400.0

#: The identity check (``compare``): two reset times of one window agree
#: within this. The usage endpoint stamps a reset with sub-second jitter and
#: the rate-limit headers carry whole epoch seconds; two accounts' windows
#: start when their own first request lands.
RESET_TOLERANCE_S = 120.0

#: The token's states. ``standing-by``: its login works and it is not the
#: active account, so it is never read and never a candidate.
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


def links(data: Mapping | None) -> dict[str, str]:
    """Backup token's account number -> its login's, from a loaded
    ``sequence.json``. A link whose login is not managed (or is itself a
    token, a key or the token's own number) is left out: the token is then
    an ordinary setup-token account (``qswap doctor`` warns about it)."""
    accounts = _accounts(data)
    out: dict[str, str] = {}
    for num, record in accounts.items():
        parent = parent_of(record)
        if parent is None or parent == str(num):
            continue
        target = accounts.get(parent)
        if not isinstance(target, Mapping) or target.get("kind") == "api_key":
            continue
        if parent_of(target) is not None:
            continue  # a token's backup is no link
        out[str(num)] = parent
    return out


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
    """Records that name a login that is not a usable parent (not managed,
    an API key, a token): number -> the number they name."""
    good = links(data)
    out = {}
    for num, record in _accounts(data).items():
        parent = parent_of(record)
        if parent is not None and str(num) not in good:
            out[str(num)] = parent
    return out


def backups_by_parent(link_map: Mapping[str, str]) -> dict[str, str]:
    """Login number -> its backup token's, the inverse of :func:`links`.
    One backup per login; a second record naming the same login (a hand
    edit) loses to the lower number."""
    out: dict[str, str] = {}
    for backup in sorted(link_map, key=_order):
        out.setdefault(link_map[backup], backup)
    return out


def _order(num: str) -> tuple[int, object]:
    return (0, int(num)) if num.isdigit() else (1, num)


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
            record.pop(BACKUP_FOR, None)
            record.pop(BACKUP_ADDED, None)
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
    login's, its state and its dates (epoch seconds)."""

    number: str
    parent: str
    state: str
    added_at: float | None = None
    ends_at: float | None = None

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


@dataclass(frozen=True)
class Match:
    """What :func:`compare` found. ``verdict`` is ``same`` (at least one
    window agrees and nothing disagrees), ``other`` (something disagrees) or
    ``unknown`` (nothing could be compared); ``why`` says it in words;
    ``agreed`` names the windows that agreed."""

    verdict: str
    why: str = ""
    agreed: tuple[str, ...] = ()


def _reset(usage: object, key: str) -> float | None:
    if not isinstance(usage, Mapping):
        return None
    window = usage.get(key)
    if not isinstance(window, Mapping):
        return None
    return parse_iso(window.get("resets_at"))


def compare(
    parent: object,
    token: object,
    *,
    now: float,
    parent_org: str = "",
    token_org: str | None = None,
    tolerance: float = RESET_TOLERANCE_S,
) -> Match:
    """Whether a token's reading (``token``, normalized as
    ``oauth.build_usage_result`` returns it) is the same account as a
    login's reading (``parent``, its last good one, of any age).

    One account has one 5-hour and one weekly window, so the same reset
    times. A window counts on the login's side only while its reset is
    still ahead (a window that has ended says nothing about the next one,
    and the token's request may have started a new one). An organization
    header (``token_org``) that names another organization than the login's
    refuses on its own; one that matches proves nothing alone (a team's
    members share an organization).
    """
    if token_org and parent_org and token_org.strip().lower() != parent_org.strip().lower():
        return Match("other", "it belongs to another organization")
    agreed: list[str] = []
    for key, words in _WINDOWS:
        mine = _reset(parent, key)
        theirs = _reset(token, key)
        if mine is None or mine <= now or theirs is None:
            continue
        if abs(mine - theirs) > tolerance:
            return Match("other", f"its {words} window resets at another time")
        agreed.append(words)
    if agreed:
        return Match("same", agreed=tuple(agreed))
    if not isinstance(parent, Mapping) or not any(
        _reset(parent, key) is not None for key, _ in _WINDOWS
    ):
        return Match("unknown", "there is no usage reading of the login to compare with")
    if not any((_reset(parent, key) or 0) > now for key, _ in _WINDOWS):
        return Match("unknown", "no usage window of the login is running")
    return Match("unknown", "the check request carried no reset times")
