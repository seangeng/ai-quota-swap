"""``qswap usage``: scan, then report. The CLI's single entry into the ledger."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ai_quota_swap.ledger.activations import ActivationLog
from ai_quota_swap.ledger.pricing import PriceTable
from ai_quota_swap.ledger.report import Report, build_report, day_bounds, home_path
from ai_quota_swap.ledger.scanner import (
    ZSTD_AVAILABLE,
    ScanConfig,
    Scanner,
    ScanStats,
    clear_progress,
    default_config,
    tty_progress,
)
from ai_quota_swap.ledger.store import DB_FILENAME, LAST_SCAN_META, LedgerStore


@dataclass
class UsageResult:
    report: Report
    scan: ScanStats | None
    notes: list[str] = field(default_factory=list)
    # Set when the scan left older session files out of the ledger: the
    # text report's last line.
    older_history: str | None = None


def older_history_line(stats: ScanStats | None) -> str | None:
    """How many older session files the scan window left out, and how to add them."""
    n = stats.older_not_in_ledger if stats is not None else 0
    if not n:
        return None
    if n == 1:
        return (
            "1 older session file is not in the ledger yet. "
            "Use a wider --days or --since to include it, or --all."
        )
    return (
        f"{n:,} older session files are not in the ledger yet. "
        "Use a wider --days or --since to include the ones you need, or --all for all of them."
    )


def searched_dirs(config: ScanConfig, providers: tuple[str, ...]) -> tuple[str, ...]:
    """The directories a scan over ``providers`` looks in, home spelled ``~``."""
    dirs: list[Path] = []
    if "claude" in providers:
        dirs.extend(root.path for root in config.claude_roots)
    if "codex" in providers:
        dirs.extend(config.codex_roots)
    return tuple(home_path(d) for d in dirs)


def resolve_range(
    *, days: int | None, since: date | None, until: date | None, today: date | None = None
) -> tuple[date, date]:
    """The local-date range the flags ask for (default: the last 7 days)."""
    today = today or date.today()
    if since is not None:
        return since, until or today
    n = days if days is not None else 7
    return today - timedelta(days=n - 1), today


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_usage(
    data_root: Path,
    *,
    since: date,
    until: date,
    by: str = "day",
    provider: str | None = None,
    scan: bool = True,
    rescan: bool = False,
    scan_all: bool = False,
    registry: dict | None = None,
    progress: bool = True,
) -> UsageResult:
    """Bring the ledger up to date (unless ``scan`` is False) and build a report.

    The scan reads only the session files that can hold events from
    ``since`` on (see the scan window in scanner.py); ``scan_all`` and
    ``rescan`` read every file. ``registry`` is the Claude account registry
    (``sequence.json``); the Codex one is read from ``<data_root>/codex/``
    when it exists. KeyboardInterrupt propagates after the batch in flight is
    rolled back: what was committed stays, and the next scan resumes from
    there.
    """
    data_root = Path(data_root)
    store = LedgerStore(data_root / DB_FILENAME)
    searched: tuple[str, ...] | None = None
    try:
        stats: ScanStats | None = None
        if rescan:
            store.reset()
        if scan or rescan:
            providers = (provider,) if provider else ("claude", "codex")
            # The report's first hour, as build_report computes it.
            window = None if rescan or scan_all else day_bounds(since, until)[0] * 3600
            show = tty_progress() if progress else None
            config = default_config(data_root, registry)
            scanner = Scanner(store, config, progress=show)
            try:
                stats = scanner.run(providers, since=window)
            finally:
                if show is not None:
                    clear_progress()
            store.set_meta(LAST_SCAN_META, f"{time.time():.3f}")
            if not stats.files_seen and not stats.files_skipped:
                searched = searched_dirs(config, providers)
        logs = {
            "claude": ActivationLog.load(data_root),
            "codex": ActivationLog.load(data_root / "codex"),
        }
        registries = {
            "claude": registry,
            "codex": _read_json(data_root / "codex" / "sequence.json"),
        }
        report = build_report(
            store,
            since=since,
            until=until,
            by=by,
            provider=provider,
            registries=registries,
            logs=logs,
            prices=PriceTable.load(data_root),
        )
        report.searched_dirs = searched
        report.unscanned = stats is None
    finally:
        store.close()
    notes: list[str] = []
    if store.rebuilt:
        notes.append(
            f"The usage ledger ({DB_FILENAME}) could not be read as a database, so it was "
            "rebuilt from the session files."
        )
    if stats is not None:
        if stats.skipped_compressed:
            notes.append(
                f"Skipped {stats.skipped_compressed} compressed Codex session files "
                "(.jsonl.zst): reading them needs Python 3.14+ or the 'zstandard' package."
                if not ZSTD_AVAILABLE
                else f"Skipped {stats.skipped_compressed} compressed Codex session files."
            )
        if stats.missing:
            notes.append(
                f"{stats.missing} session files could not be read (for example, links "
                "to a volume that is not mounted); they are read once they are back."
            )
    return UsageResult(
        report=report, scan=stats, notes=notes, older_history=older_history_line(stats)
    )
