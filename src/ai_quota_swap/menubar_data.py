"""The menu bar's blocking reads: snapshots of both providers, the ledger's
cost totals and each day's cost by account, the switch history, the
engines' heartbeats.

Everything here touches files, the Keychain or the network (through the
usage store's paced read path), so the app calls it from a background
thread only, never from the ``rumps`` main loop. What comes back is plain
data for :func:`ai_quota_swap.menubar.build_menu`.

The cost row reads ``ledger.db`` read-only and never scans: a scan reads
session files and can take seconds on a large history, so the app runs one
(:func:`scan_ledger`) in the background every :data:`COST_SCAN_INTERVAL_S`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ai_quota_swap.exceptions import ProviderError
from ai_quota_swap.menubar import (
    AccountState,
    CostSummary,
    ProviderState,
    SwitchRecord,
    error_text,
    parse_switch_history,
)
from ai_quota_swap.menubar_chart import CostAccount, CostDay

_logger = logging.getLogger("ai-quota-swap")

#: How often the menu bar brings the ledger up to date. The cost row is an
#: estimate over a day and a week, so a quarter of an hour of lag changes
#: nothing anyone acts on, while a scan walks every session directory and,
#: on a large history or after a long sleep, reads for seconds: often enough
#: to stay current through a working day, rarely enough to stay out of the
#: way of the work it measures. ``qswap usage`` scans on demand meanwhile,
#: and the row picks up any scan as soon as the ledger file changes.
COST_SCAN_INTERVAL_S = 15 * 60
#: The first scan waits this long after launch, out of the way of login.
COST_FIRST_SCAN_DELAY_S = 60.0
#: The switch history reads only the end of the log.
HISTORY_TAIL_BYTES = 512 * 1024


@dataclass(frozen=True)
class Gathered:
    """One background read of everything the menu shows."""

    claude: ProviderState
    codex: ProviderState
    history: tuple[SwitchRecord, ...] = ()
    heartbeats: dict = field(default_factory=dict)
    taken_at: float = 0.0


def _plan_label(record: dict | None, now: float) -> str:
    try:
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        return ClaudeAccountSwitcher._plan_text(record, now)[0]
    except Exception:
        return ""


def provider_lines(data_root: Path | None, provider: str):
    """A provider's switch lines from the settings its engine runs on
    (``autoswitch.threshold`` / ``weeklyThreshold``; ``codex.threshold`` for
    Codex). Empty lines when the settings cannot be read."""
    from ai_quota_swap.display import Lines
    from ai_quota_swap.settings import load_engine_settings

    if data_root is None:
        return Lines()
    try:
        settings = load_engine_settings(Path(data_root), provider)
    except Exception:
        return Lines()
    return Lines(five_hour=settings.threshold, weekly=settings.weekly_threshold)


def _account_lines(acc, lines):
    """An account's own threshold replaces the provider's 5h line, as in
    ``qswap list``."""
    from ai_quota_swap.display import Lines

    own = getattr(getattr(acc, "policy", None), "threshold", None)
    if own is None:
        return lines
    return Lines(five_hour=own, weekly=lines.weekly)


def account_states(
    snapshot, provider: str, set_aside: dict[str, str], *, now: float, lines=None,
) -> tuple[AccountState, ...]:
    """An ``AccountsSnapshot``'s accounts as :class:`AccountState` rows;
    ``lines`` are the provider's switch lines."""
    from ai_quota_swap.display import Lines

    from ai_quota_swap.labels import account_label

    lines = lines or Lines()
    under = getattr(snapshot, "underneath_number", None)
    over = ""
    if under is not None:
        acc_under = next((a for a in snapshot.accounts if str(a.number) == str(under)), None)
        if acc_under is not None:
            over = account_label(
                acc_under.number, alias=acc_under.alias or None, email=acc_under.email,
                long=False,
            )
    return tuple(
        AccountState(
            provider=provider,
            number=str(acc.number),
            email=acc.email,
            alias=acc.alias or "",
            active=bool(acc.is_active),
            disabled=bool(acc.disabled),
            kind=acc.kind,
            entry=acc.usage,
            set_aside=set_aside.get(str(acc.number)),
            login_expires_at=acc.login_expires_at,
            plan=_plan_label(acc.subscription_plan, now),
            org=(acc.org_name or "") if provider == "claude" else "",
            lines=_account_lines(acc, lines),
            underneath=bool(getattr(acc, "underneath", False)),
            over=over if acc.is_active else "",
            by_engine=bool(acc.is_active)
            and getattr(snapshot, "key_entered_by", None) == "engine",
        )
        for acc in snapshot.accounts
    )


def _quiet(fn, default=None):
    try:
        return fn()
    except Exception:
        _logger.debug("menu bar read failed", exc_info=True)
        return default


def claude_state(switcher, source, *, full: bool, store_only: bool, now: float) -> ProviderState:
    """Claude Code's accounts through the paced read path (``SnapshotSource``),
    the engine's set-aside list, and the live login. A provider error is the
    state's ``error``; anything else propagates (the app keeps the last read)."""
    from ai_quota_swap import clis

    live = _quiet(switcher.live_login_email)
    try:
        snap = source.take(full=full, store_only=store_only)
    except ProviderError as e:
        return ProviderState("claude", live_email=live, error=error_text(e))
    set_aside = _quiet(switcher.set_aside_accounts, {}) or {}
    lines = provider_lines(getattr(switcher, "backup_dir", None), "claude")
    accounts = account_states(snap, "claude", set_aside, now=now, lines=lines)
    found = None if accounts or live else _quiet(lambda: clis.cli_found("claude"))
    notes = tuple(getattr(snap, "key_notes", ()) or ())
    return ProviderState("claude", accounts, live_email=live, cli_found=found, key_notes=notes)


def codex_registry_count(data_root: Path) -> int:
    """How many Codex accounts are managed, from its registry file alone
    (no switcher, nothing created)."""
    try:
        data = json.loads((Path(data_root) / "codex" / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    accounts = data.get("accounts") if isinstance(data, dict) else None
    return len(accounts) if isinstance(accounts, dict) else 0


class CodexReader:
    """Codex's side of a read. The switcher (and its paced read path) is
    built the first time Codex manages an account, and shared with the
    Codex engine, as the Claude switcher is with Claude Code's."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.switcher = None
        self.source = None

    def ensure(self):
        if self.switcher is None:
            from ai_quota_swap.codex.switcher import CodexSwitcher
            from ai_quota_swap.snapshot_source import SnapshotSource

            self.switcher = CodexSwitcher(data_root=self.data_root, configure_logging=False)
            self.source = SnapshotSource(self.switcher)
        return self.switcher

    def state(self, *, full: bool, store_only: bool, now: float) -> ProviderState:
        from ai_quota_swap import codex_cli

        live = codex_cli.live_codex_email()
        count = codex_registry_count(self.data_root)
        if not count:
            return ProviderState("codex", live_email=live, managed=0)
        switcher = self.ensure()
        try:
            switcher.check_storage()
            snap = self.source.take(full=full, store_only=store_only)
        except ProviderError as e:
            return ProviderState("codex", live_email=live, managed=count, error=error_text(e))
        set_aside = _quiet(switcher.set_aside_accounts, {}) or {}
        lines = provider_lines(self.data_root, "codex")
        return ProviderState(
            "codex", account_states(snap, "codex", set_aside, now=now, lines=lines),
            live_email=live, managed=count,
        )


def heartbeats(data_root: Path) -> dict:
    """Each provider's last engine heartbeat (``qswap auto`` or a menu bar)."""
    from ai_quota_swap.autoswitch import read_heartbeat

    out = {}
    for provider, root in (("claude", Path(data_root)), ("codex", Path(data_root) / "codex")):
        beat = _quiet(lambda r=root: read_heartbeat(r))
        if beat is not None:
            out[provider] = beat
    return out


def read_history(log_path: Path) -> tuple[SwitchRecord, ...]:
    """Recent switches from the end of the log."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - HISTORY_TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(parse_switch_history(text))


# -- cost --------------------------------------------------------------------------


def ledger_stamp(data_root: Path) -> tuple:
    """Changes whenever the ledger does (a scan here or by ``qswap usage``)."""
    out = []
    for name in ("ledger.db", "ledger.db-wal"):
        try:
            st = os.stat(Path(data_root) / name)
            out.append((st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


def read_cost(data_root: Path, *, today: date | None = None) -> CostSummary | None:
    """Today's and the last seven local days' estimated cost, both providers,
    from ``ledger.db`` as it is: opened read-only (a running scan is never
    blocked, a missing ledger is not created), priced at read time with the
    same table and ``pricing.json`` as ``qswap usage``, over the same local
    days. None when there is no ledger or it holds nothing yet.

    It also reads each of those days by account for the chart
    (:func:`cost_days`); a failure there leaves the days empty and the
    totals as they are."""
    from ai_quota_swap.ledger.pricing import PriceTable
    from ai_quota_swap.ledger.report import day_bounds
    from ai_quota_swap.ledger.store import DB_FILENAME, SCHEMA_VERSION, LedgerStore

    db = Path(data_root) / DB_FILENAME
    if not db.exists():
        return None
    today = today or date.today()
    since_hour, until_hour = day_bounds(today - timedelta(days=6), today)
    today_hour = day_bounds(today, today)[0]
    try:
        store = LedgerStore.read_only(db, timeout=1.0)
    except sqlite3.Error:
        return None
    conn = store.conn
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None or str(row[0]) != str(SCHEMA_VERSION):
            return None
        if conn.execute("SELECT 1 FROM buckets LIMIT 1").fetchone() is None:
            return None
        rows = conn.execute(
            "SELECT hour >= ?, model, SUM(input), SUM(output), SUM(cache_read), "
            "SUM(cache_write_5m), SUM(cache_write_1h) FROM buckets "
            "WHERE hour >= ? AND hour < ? GROUP BY 1, 2",
            (today_hour, since_hour, until_hour),
        ).fetchall()
        prices = PriceTable.load(Path(data_root))
        try:
            days = cost_days(store, Path(data_root), today=today, prices=prices)
        except Exception:
            _logger.debug("menu bar: the cost by day could not be read", exc_info=True)
            days = ()
    except sqlite3.Error:
        return None
    finally:
        store.close()
    totals = {"today": [0.0, 0], "week": [0.0, 0]}
    for is_today, model, *toks in rows:
        toks = [int(t or 0) for t in toks]
        found = prices.lookup(model)
        buckets = ("week", "today") if is_today else ("week",)
        for name in buckets:
            if found is None:
                totals[name][1] += sum(toks)
            else:
                totals[name][0] += found[1].cost(*toks)
    return CostSummary(
        today=round(totals["today"][0], 6), week=round(totals["week"][0], 6),
        today_unpriced=totals["today"][1], week_unpriced=totals["week"][1],
        days=days,
    )


def cost_days(store, data_root: Path, *, today: date, prices) -> tuple[CostDay, ...]:
    """The seven local days ending ``today``, oldest first, each day's
    estimated cost by account: the accounts ``qswap usage --by account``
    gives that usage (``ledger.report.attribute``, the same activation logs
    and registries), priced as it prices them. Usage with no account is
    its own entry, ``unattributed``. Reads files only; ``store`` is the
    read-only ledger."""
    from ai_quota_swap.ledger.report import Accounts, account_group, attribute, day_bounds, local_day
    from ai_quota_swap.ledger.usage import _read_json, report_inputs

    first = today - timedelta(days=6)
    logs, registries = report_inputs(data_root, _read_json(Path(data_root) / "sequence.json"))
    accounts = Accounts(registries, logs)
    since_hour, until_hour = day_bounds(first, today)
    attributed = attribute(
        store, since_hour=since_hour, until_hour=until_hour, provider=None,
        accounts=accounts, logs=logs,
    )
    per_day: dict[date, dict[tuple, float]] = {}
    for prov, hour, account, model, _project, _session, toks in attributed.rows:
        found = prices.lookup(model)
        if found is None:
            continue  # unpriced: in the caption's "+", not in a column
        group = account_group(prov, account)
        day = per_day.setdefault(local_day(hour), {})
        day[group] = day.get(group, 0.0) + found[1].cost(*toks[:5])
    who: dict[tuple, CostAccount] = {}
    for group in {g for day in per_day.values() for g in day}:
        prov, key = group
        name = accounts.describe(key, prov)[0]
        info = accounts.by_key.get((prov or "", key))
        who[group] = CostAccount(prov, key, info["number"] if info else None, name)
    return tuple(
        CostDay(d, tuple(
            (who[g], round(cost, 6))
            for g, cost in sorted(per_day.get(d, {}).items(), key=lambda item: (item[0][0] or "", item[0][1]))
        ))
        for d in (first + timedelta(days=i) for i in range(7))
    )


def scan_ledger(data_root: Path, *, registry: dict | None = None, today: date | None = None) -> None:
    """Bring the ledger up to date for the last seven days: the scan ``qswap
    usage`` runs (incremental, resumable, safe beside another scan: every
    event is counted once). Blocking; the app runs it in a thread."""
    from ai_quota_swap.ledger.usage import resolve_range, run_usage

    since, until = resolve_range(days=7, since=None, until=None, today=today)
    run_usage(
        Path(data_root), since=since, until=until, by="day", scan=True,
        progress=False, registry=registry,
    )
