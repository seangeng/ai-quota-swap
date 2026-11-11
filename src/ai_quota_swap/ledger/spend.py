"""This month's and today's spend for one account, read from the ledger as it is.

For ``qswap list``, the dashboard, the menu bar and the auto engine, which show
an API-key account's spend without scanning anything (``qswap usage`` brings
the ledger up to date). So the read:

- opens ``ledger.db`` read-only, so a running scan is never blocked or
  written to, and a missing ledger is not created;
- uses UTC month and day bounds, as Anthropic's reports do (``qswap usage``
  groups by local day; the ledger's buckets are UTC hours, so both are exact);
- attributes like ``qswap usage --by account``: by the activation log, except
  that a session Claude Code stamped as started on another account (a
  ``qswap run`` session sharing the default history) belongs to that account
  and is left out here. API-key accounts never own such a session (the stamp
  names an account uuid, which they do not have), so nothing is added;
- prices tokens when read, with the same table and ``pricing.json``; tokens
  of a model with no price are charged at the table's dearest rate for their
  kind (``PriceTable.dearest``), since these figures hold an API key to its
  budget and credit: an unknown model never counts as zero;
- remembers its answer in this process until the ledger, the activation log
  or the price file changes, so a dashboard repainting every few seconds
  costs one query per change.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap.ledger.activations import ActivationLog, identity_key, log_path
from ai_quota_swap.ledger.pricing import OVERRIDE_FILENAME, PriceTable
from ai_quota_swap.ledger.store import DB_FILENAME, LAST_SCAN_META, SCHEMA_VERSION


@dataclass(frozen=True)
class LedgerSpend:
    month: float
    today: float
    unpriced_tokens: int
    # When ``qswap usage`` last scanned (epoch seconds); None for a ledger
    # written before the time was recorded.
    last_scan_at: float | None
    # Spend since ``since_start`` (a credit's start), when one was asked for.
    since: float | None = None
    # Tokens with no price since ``since_start`` (counted at the dearest rate).
    unpriced_since: int = 0


_memo: dict[tuple, LedgerSpend] = {}
_memo_lock = threading.Lock()


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


class _Sessions:
    """The one ``LedgerStore`` method ``report._profile_sessions`` calls."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def sessions(self, provider: str) -> dict[str, tuple[str | None, float | None, int | None]]:
        try:
            rows = self.conn.execute(
                "SELECT session, owner, first_ts, owner_early FROM sessions WHERE provider = ?",
                (provider,),
            ).fetchall()
        except sqlite3.OperationalError:
            # A ledger from before owner_early, read without migrating it
            # (read-only): its owners read as early, as they always did.
            rows = [
                (*row, None)
                for row in self.conn.execute(
                    "SELECT session, owner, first_ts FROM sessions WHERE provider = ?",
                    (provider,),
                )
            ]
        return {row[0]: (row[1], row[2], row[3]) for row in rows}


def account_spend(
    data_root: Path,
    email: str,
    org: str,
    *,
    month_start: float,
    day_start: float,
    registry: dict | None = None,
    since_start: float | None = None,
) -> LedgerSpend | None:
    """Claude Code spend since ``month_start`` and ``day_start`` (epoch seconds,
    UTC boundaries) for the account ``email``/``org``, and since
    ``since_start`` (a credit's start, from its hour) when given; None when
    there is no readable ledger. Never raises."""
    root = Path(data_root)
    db = root / DB_FILENAME
    db_key = _stat_key(db)
    if db_key is None:
        return None
    memo_key = (
        str(db), db_key, _stat_key(Path(str(db) + "-wal")),
        _stat_key(log_path(root)), _stat_key(root / OVERRIDE_FILENAME),
        email, org or "", int(month_start), int(day_start),
        None if since_start is None else int(since_start),
    )
    with _memo_lock:
        hit = _memo.get(memo_key)
    if hit is not None:
        return hit
    try:
        result = _read(root, db, email, org, month_start, day_start, registry, since_start)
    except (sqlite3.Error, OSError, ValueError):
        return None
    if result is not None:
        with _memo_lock:
            if len(_memo) > 256:
                _memo.clear()
            _memo[memo_key] = result
    return result


def _read(
    root: Path,
    db: Path,
    email: str,
    org: str,
    month_start: float,
    day_start: float,
    registry: dict | None,
    since_start: float | None = None,
) -> LedgerSpend | None:
    from ai_quota_swap.ledger.report import Accounts, _profile_sessions, saved_key_moves

    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or str(row[0]) != str(SCHEMA_VERSION):
            return None
        scan = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (LAST_SCAN_META,)
        ).fetchone()
        try:
            last_scan_at = float(scan[0]) if scan is not None else None
        except (TypeError, ValueError):
            last_scan_at = None
        account = identity_key(email, org)
        month_hour = int(month_start // 3600)
        day_hour = int(day_start // 3600)
        since_hour = None if since_start is None else int(since_start // 3600)
        first_hour = month_hour if since_hour is None else min(month_hour, since_hour)
        rows = conn.execute(
            "SELECT hour, model, session, input, output, cache_read, cache_write_5m, "
            "cache_write_1h FROM buckets WHERE provider = 'claude' AND hour >= ? "
            "AND account = ?",
            (first_hour, account),
        ).fetchall()
        moved: dict[str, str] = {}
        log = None
        starts: dict[str, float | None] = {}
        if rows:
            log = ActivationLog.load(root)
            accounts = Accounts({"claude": registry}, {"claude": log})
            sessions = _Sessions(conn)
            moved = _profile_sessions(sessions, accounts, log)  # type: ignore[arg-type]
            if log.saved_key_periods():
                starts = {s: v[1] for s, v in sessions.sessions("claude").items()}
    finally:
        conn.close()
    prices = PriceTable.load(root)
    dearest = None
    month = today = since = 0.0
    unpriced = unpriced_since = 0
    for hour, model, session, *toks in rows:
        owner = moved.get(session)
        if owner is not None and owner != account:
            continue
        if log is not None and starts:
            # docs/design/api-key-sessions.md: in a saved-key period only the
            # sessions started after the activation used the key.
            elsewhere = saved_key_moves(log, hour, starts.get(session), account)
            if elsewhere is not None and elsewhere != account:
                continue
        found = prices.lookup(model)
        if found is None:
            dearest = dearest or prices.dearest()
            cost = dearest.cost(*toks)
            if hour >= month_hour:
                unpriced += sum(toks)
            if since_hour is not None and hour >= since_hour:
                unpriced_since += sum(toks)
        else:
            cost = found[1].cost(*toks)
        if hour >= month_hour:
            month += cost
            if hour >= day_hour:
                today += cost
        if since_hour is not None and hour >= since_hour:
            since += cost
    return LedgerSpend(
        month=round(month, 6), today=round(today, 6),
        unpriced_tokens=unpriced, last_scan_at=last_scan_at,
        since=None if since_hour is None else round(since, 6),
        unpriced_since=unpriced_since,
    )
