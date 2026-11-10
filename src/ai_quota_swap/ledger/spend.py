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
- prices tokens when read, with the same table and ``pricing.json``;
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

    def sessions(self, provider: str) -> dict[str, tuple[str | None, float | None]]:
        return {
            row[0]: (row[1], row[2])
            for row in self.conn.execute(
                "SELECT session, owner, first_ts FROM sessions WHERE provider = ?",
                (provider,),
            )
        }


def account_spend(
    data_root: Path,
    email: str,
    org: str,
    *,
    month_start: float,
    day_start: float,
    registry: dict | None = None,
) -> LedgerSpend | None:
    """Claude Code spend since ``month_start`` and ``day_start`` (epoch seconds,
    UTC boundaries) for the account ``email``/``org``, or None when there is
    no readable ledger. Never raises."""
    root = Path(data_root)
    db = root / DB_FILENAME
    db_key = _stat_key(db)
    if db_key is None:
        return None
    memo_key = (
        str(db), db_key, _stat_key(Path(str(db) + "-wal")),
        _stat_key(log_path(root)), _stat_key(root / OVERRIDE_FILENAME),
        email, org or "", int(month_start), int(day_start),
    )
    with _memo_lock:
        hit = _memo.get(memo_key)
    if hit is not None:
        return hit
    try:
        result = _read(root, db, email, org, month_start, day_start, registry)
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
) -> LedgerSpend | None:
    from ai_quota_swap.ledger.report import Accounts, _profile_sessions

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
        rows = conn.execute(
            "SELECT hour, model, session, input, output, cache_read, cache_write_5m, "
            "cache_write_1h FROM buckets WHERE provider = 'claude' AND hour >= ? "
            "AND account = ?",
            (month_hour, account),
        ).fetchall()
        moved: dict[str, str] = {}
        if rows:
            log = ActivationLog.load(root)
            accounts = Accounts({"claude": registry}, {"claude": log})
            moved = _profile_sessions(_Sessions(conn), accounts, log)  # type: ignore[arg-type]
    finally:
        conn.close()
    prices = PriceTable.load(root)
    month = today = 0.0
    unpriced = 0
    for hour, model, session, *toks in rows:
        owner = moved.get(session)
        if owner is not None and owner != account:
            continue
        found = prices.lookup(model)
        if found is None:
            unpriced += sum(toks)
            continue
        cost = found[1].cost(*toks)
        month += cost
        if hour >= day_hour:
            today += cost
    return LedgerSpend(
        month=round(month, 6), today=round(today, 6),
        unpriced_tokens=unpriced, last_scan_at=last_scan_at,
    )
