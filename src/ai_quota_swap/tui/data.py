"""Data service for the TUI: snapshots of both providers, blocking actions,
and the account blocks every screen draws.

The TUI never parses printed CLI output. Each provider's switcher hands over
an ``AccountsSnapshot`` (``accounts_snapshot``, one collect pass); a
:class:`FleetSource` takes one per provider and adds what the views need to
say an account's state in ``qswap list``'s words: the accounts the engine set
aside, the live login that is not managed, the switch lines. A card is a
:class:`display.AccountBlock` built from that (:func:`block_for`), so the
dashboard and the command line share one set of words.

Everything here that touches a switcher is blocking (file locks, keychain
subprocesses, network) and must be called from a thread worker, never the UI
event loop. The block builders are pure.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ai_quota_swap import display, printer
from ai_quota_swap.exceptions import ProviderError
from ai_quota_swap.json_output import USAGE_RELOGIN_REQUIRED
from ai_quota_swap.models import AccountSnapshot, AccountsSnapshot
from ai_quota_swap.snapshot_source import SnapshotSource

#: Providers in display order (``qswap list``: Claude Code first).
PROVIDER_ORDER = ("claude", "codex")


# ---------------------------------------------------------------------------
# Blocking actions (switch/add/remove) run captured, off the UI thread
# ---------------------------------------------------------------------------


@dataclass
class ActionResult:
    """Outcome of a captured switcher action."""

    ok: bool
    output: str  # captured stdout+stderr, ANSI-colored (render with Text.from_ansi)
    payload: dict | None = None  # structured result for json-capable actions
    error: str | None = None  # the provider error's text, as `Error: …` would show it
    error_type: str | None = None  # that error's class name (SessionsRunningError, ...)
    error_fields: dict | None = None  # its facts beyond the text (error.envelope_fields)

    @property
    def lines(self) -> list[str]:
        """Non-empty output lines, ANSI-stripped."""
        from rich.text import Text

        out = []
        for line in self.output.splitlines():
            plain = Text.from_ansi(line).plain.rstrip()
            if plain.strip():
                out.append(plain)
        return out

    @property
    def first_line(self) -> str:
        """First non-empty output line, ANSI-stripped — notification material."""
        lines = self.lines
        return lines[0].strip() if lines else ""


def run_action(fn: Callable[[], dict | None]) -> ActionResult:
    """Run a switcher action capturing stdout+stderr (color forced on).

    A :class:`ProviderError` (every provider's error base) becomes a failed
    result whose output is the line the command line prints, ``Error: …``.
    ``sys.stdin`` is swapped for an empty stream so an unexpected ``input()``
    raises ``EOFError`` instead of freezing the app. The redirect is
    process-global for the duration — fine here because the TUI owns the
    terminal and nothing else prints while it runs.
    """
    buf = io.StringIO()
    payload: dict | None = None
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO()
    try:
        with printer.force_color(), contextlib.redirect_stdout(
            buf
        ), contextlib.redirect_stderr(buf):
            try:
                payload = fn()
            except ProviderError as e:
                print(f"Error: {e}")
                fields = getattr(e, "envelope_fields", None)
                return ActionResult(
                    False, buf.getvalue(), error=str(e), error_type=type(e).__name__,
                    error_fields=dict(fields) if isinstance(fields, dict) else None,
                )
            except EOFError:
                print("Error: interactive input is not available here.")
                return ActionResult(
                    False, buf.getvalue(), error="interactive input is not available here."
                )
    finally:
        sys.stdin = saved_stdin
    return ActionResult(
        True, buf.getvalue(), payload if isinstance(payload, dict) else None
    )


# ---------------------------------------------------------------------------
# Both providers in one snapshot
# ---------------------------------------------------------------------------


def _empty_snapshot() -> AccountsSnapshot:
    return AccountsSnapshot(active_number=None, accounts=(), taken_at=time.time())


@dataclass(frozen=True)
class ProviderView:
    """One provider as the dashboard shows it.

    ``live_email`` is the provider's live login when qswap does not manage
    it: no managed account is active, or a key is active through the key
    line over such a login (``None`` otherwise, or when there is none;
    :func:`unmanaged_login_email`); ``cli_found`` whether
    its command is on PATH, read only when it has neither accounts nor a
    login. ``set_aside`` holds the accounts the engine will not use until
    their owner logs in again (number → reason). ``lines`` are the
    provider's switch lines; ``error`` says why it could not be read.
    """

    provider: str
    snapshot: AccountsSnapshot = field(default_factory=_empty_snapshot)
    set_aside: Mapping[str, str] = field(default_factory=dict)
    live_email: str | None = None
    cli_found: bool | None = None
    lines: display.Lines = display.Lines()
    include_api_key_accounts: bool = False
    error: str | None = None

    @property
    def accounts(self) -> tuple[AccountSnapshot, ...]:
        return self.snapshot.accounts

    @property
    def active_number(self) -> str | None:
        return self.snapshot.active_number

    @property
    def name(self) -> str:
        return display.provider_name(self.provider)

    def account(self, number: str) -> AccountSnapshot | None:
        return next((a for a in self.accounts if a.number == str(number)), None)


@dataclass(frozen=True)
class Fleet:
    """Every provider's view from one refresh, in display order."""

    views: tuple[ProviderView, ...]
    taken_at: float

    def view(self, provider: str) -> ProviderView | None:
        return next((v for v in self.views if v.provider == provider), None)

    @property
    def managed(self) -> bool:
        """Whether any provider manages an account."""
        return any(v.accounts for v in self.views)

    @property
    def sectioned(self) -> bool:
        """Both providers have accounts: views show a heading over each."""
        return sum(1 for v in self.views if v.accounts) > 1

    def rows(self) -> Iterator[tuple[ProviderView, AccountSnapshot]]:
        """Every account in display order: provider by provider, slot order."""
        for v in self.views:
            for acc in v.accounts:
                yield v, acc

    def find(self, provider: str, number: str) -> tuple[ProviderView, AccountSnapshot] | None:
        v = self.view(provider)
        acc = v.account(number) if v is not None else None
        return (v, acc) if v is not None and acc is not None else None

    def label(self, provider: str, number: str, *, long: bool = True) -> str:
        """``#2 work``, ``Claude #2 work (maya@…)`` when both providers show:
        an account named outside its section carries its provider."""
        found = self.find(provider, number)
        if found is None:
            return display.account_label(number, provider=provider if self.sectioned else None)
        _v, acc = found
        return display.account_label(
            acc.number, alias=acc.alias or None, email=acc.email,
            provider=provider if self.sectioned else None, long=long,
        )


def settings_root(switcher: object) -> Path:
    """Where ``settings.json`` lives for this switcher (``cli._settings_root``)."""
    root = getattr(switcher, "data_root", None)
    return root if isinstance(root, Path) else Path(getattr(switcher, "backup_dir"))


def provider_lines(provider: str, root: Path) -> tuple[display.Lines, bool]:
    """A provider's switch lines from ``settings.json`` (Codex: its own
    ``codex.threshold``), and whether its engine may land on API-key accounts."""
    from ai_quota_swap.settings import load_engine_settings

    try:
        loaded = load_engine_settings(root, provider)
    except Exception:
        return display.Lines(), False
    return (
        display.Lines(five_hour=loaded.threshold, weekly=loaded.weekly_threshold),
        bool(loaded.include_api_key_accounts),
    )


class FleetSource:
    """One :class:`Fleet` per call: each provider's snapshot, paced by its
    own usage store (:class:`SnapshotSource`), and the facts around it.

    A provider that raises a :class:`ProviderError` comes back with its
    ``error`` set; it never takes the other down. A Codex switcher with no
    managed account is not asked for a snapshot (``qswap list`` does not
    build one either), only for its live login.
    """

    #: How long the set-aside list is reused. Reading it checks each set-aside
    #: account's saved login (a Keychain call on macOS), which a dashboard
    #: polling every few seconds should not repeat each time.
    SET_ASIDE_TTL_S = 30.0

    def __init__(self, switchers: Mapping[str, object]) -> None:
        self.switchers = {p: s for p, s in switchers.items() if s is not None}
        self._sources = {p: SnapshotSource(s) for p, s in self.switchers.items()}  # type: ignore[arg-type]
        self._cli_found: dict[str, bool | None] = {}
        self._set_aside: dict[str, tuple[float, dict]] = {}

    def invalidate(self) -> None:
        """Forget what is reused between passes (after an action)."""
        self._set_aside.clear()

    def _set_aside_for(self, provider: str, switcher: object) -> dict:
        now = time.monotonic()
        hit = self._set_aside.get(provider)
        if hit is not None and now - hit[0] < self.SET_ASIDE_TTL_S:
            return hit[1]
        value = dict(_quiet(switcher, "set_aside_accounts", {}) or {})
        self._set_aside[provider] = (now, value)
        return value

    def take(self, *, full: bool = False, store_only: bool = False) -> Fleet:
        views = tuple(
            self._take_one(p, self.switchers[p], full=full, store_only=store_only)
            for p in PROVIDER_ORDER
            if p in self.switchers
        )
        return Fleet(views=views, taken_at=time.time())

    def refresh_plans(self) -> None:
        for source in self._sources.values():
            source.refresh_plans()

    def _take_one(self, provider: str, switcher: object, *, full: bool, store_only: bool) -> ProviderView:
        lines, include_api = provider_lines(provider, settings_root(switcher))
        try:
            if provider != "claude" and not _managed_count(switcher):
                snap = _empty_snapshot()
            else:
                snap = self._sources[provider].take(full=full, store_only=store_only)
        except ProviderError as e:
            return ProviderView(provider, lines=lines, error=str(e))
        set_aside = self._set_aside_for(provider, switcher) if snap.accounts else {}
        live = unmanaged_login_email(switcher, snap)
        found = None
        if not snap.accounts and not live:
            if provider not in self._cli_found:
                self._cli_found[provider] = _cli_found(provider)
            found = self._cli_found[provider]
        return ProviderView(
            provider, snap, set_aside=dict(set_aside), live_email=live,
            cli_found=found, lines=lines, include_api_key_accounts=include_api,
        )


def _key_is_active(snap: AccountsSnapshot) -> bool:
    active = next((a for a in snap.accounts if a.number == snap.active_number), None)
    return active is not None and getattr(active, "kind", None) == "api_key"


def unmanaged_login_email(switcher: object, snap: AccountsSnapshot) -> str | None:
    """The live login's email when qswap does not manage it: with no
    managed account active, or under a key with no managed subscription
    underneath (decision B7, F27). None otherwise."""
    if snap.active_number is not None and not (
        _key_is_active(snap) and snap.underneath_number is None
    ):
        return None
    live = _quiet(switcher, "live_login_email", None)
    if not live or any(a.email == live for a in snap.accounts):
        return None
    return live


def login_email(view: "ProviderView") -> str | None:
    """The login an "add the current login" would save: under a key, the
    login underneath it (managed or not), never the key's own address;
    otherwise the active account's, or the unmanaged live login's."""
    snap = view.snapshot
    if snap.underneath_number is not None:
        acc = view.account(snap.underneath_number)
        return acc.email if acc else None
    if view.live_email:
        return view.live_email
    if view.active_number is not None and not _key_is_active(snap):
        acc = view.account(view.active_number)
        return acc.email if acc else None
    return None


def _managed_count(switcher: object) -> int:
    count = getattr(switcher, "managed_count", None)
    if count is None:
        return 1  # a switcher that cannot say: ask it for its snapshot
    try:
        return int(count())
    except ProviderError:
        raise
    except Exception:
        return 0


def _quiet(switcher: object, member: str, default):
    """``switcher.member()``, or ``default`` when it lacks it or fails: a
    display fact, never worth failing a refresh over."""
    fn = getattr(switcher, member, None)
    if fn is None:
        return default
    try:
        return fn()
    except Exception:
        return default


def _cli_found(provider: str) -> bool | None:
    try:
        from ai_quota_swap import clis

        return clis.cli_found(provider)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# An account's block: the command line's words for one snapshot row
# ---------------------------------------------------------------------------


def _sentinel_notes(provider: str) -> dict[str, str]:
    if provider == "codex":
        from ai_quota_swap.codex.switcher import SENTINEL_NOTES
    else:
        from ai_quota_swap.switcher import SENTINEL_NOTES
    return {k: v.replace(" — ", ": ") for k, v in SENTINEL_NOTES.items()}


def _api_lines(reading: dict) -> list[str]:
    """An API-key account's spend lines, as ``qswap list`` prints them."""
    from ai_quota_swap import api_spend

    text = api_spend.spend_text(reading)
    text = text.removeprefix("API key · ").removeprefix("API key")
    out = [text] if text else []
    detail = api_spend.detail_text(reading)
    if detail:
        out.append(detail)
    return out


def block_for(
    view: ProviderView,
    acc: AccountSnapshot,
    *,
    now: float | None = None,
    show_provider: bool = False,
    threshold: float | None = None,
) -> display.AccountBlock:
    """``acc``'s :class:`display.AccountBlock`, built as ``qswap list`` builds
    it (``ClaudeAccountSwitcher._account_block``, ``CodexSwitcher.account_block``)
    from the snapshot instead of the registry. ``threshold`` stands in for the
    provider's global line (a session adjustment in the auto view); an
    account's own line still wins."""
    from ai_quota_swap.switcher import _USAGE_AGE_NOTE_S, ClaudeAccountSwitcher

    now = time.time() if now is None else now
    base = threshold if threshold is not None else view.lines.five_hour
    line = acc.policy.threshold if acc.policy.threshold is not None else base
    plan, plan_warning = ClaudeAccountSwitcher._plan_text(acc.subscription_plan, now)
    held_out = view.set_aside.get(acc.number)
    login_row = None
    api_lines = None
    if view.provider == "claude":
        relogin = held_out is not None or acc.usage.sentinel == USAGE_RELOGIN_REQUIRED
        login_row = ClaudeAccountSwitcher._login_window(acc.login_expires_at, relogin)
        api_lines = _api_lines
    over = ""
    under = getattr(view.snapshot, "underneath_number", None)
    if acc.is_active and under is not None:
        below = view.account(under)
        if below is not None:
            over = display.account_label(
                below.number, alias=below.alias or None, email=below.email, long=False,
            )
    return display.block_from_entry(
        acc.number, acc.usage,
        email=acc.email, alias=acc.alias, provider=view.provider,
        show_provider=show_provider, active=acc.is_active, disabled=acc.disabled,
        underneath=bool(getattr(acc, "underneath", False)), over=over,
        by_engine=getattr(view.snapshot, "key_entered_by", None) == "engine",
        plan=plan, plan_warning=plan_warning,
        org=acc.org_name if view.provider == "claude" else "",
        kind="api_key" if acc.kind == "api_key" else "oauth",
        lines=display.Lines(five_hour=line, weekly=view.lines.weekly),
        set_aside=held_out, sentinel_notes=_sentinel_notes(view.provider),
        login_row=login_row, now=now, stale_after_s=_USAGE_AGE_NOTE_S,
        api_lines=api_lines,
    )


def over_budget(acc: AccountSnapshot) -> bool:
    """An API-key account at or past its monthly budget, or with its credit
    spent or expired."""
    from ai_quota_swap import api_spend

    return api_spend.limit_reason(getattr(acc.usage, "api_spend", None)) is not None


# ---------------------------------------------------------------------------
# The ledger: the cost line and the usage screen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostLine:
    """Today's and the last seven days' estimated cost, from the ledger."""

    today: float
    week: float
    unpriced: bool = False

    def text(self) -> str:
        plus = "+" if self.unpriced else ""
        return (
            f"Today ${self.today:,.2f}{plus} · 7 days ${self.week:,.2f}{plus} · "
            "estimated at API prices"
        )


def usage_report(
    data_root: Path,
    *,
    days: int = 7,
    by: str = "day",
    scan: bool,
    registry: dict | None = None,
    today: date | None = None,
):
    """``qswap usage --days N --by GROUP`` as a :class:`UsageResult`
    (``ledger.usage.run_usage``); ``scan=False`` reports what the ledger
    already holds. Blocking: a worker calls it. Without a ledger file and no
    scan there is nothing to read, and nothing is created: None."""
    from ai_quota_swap.ledger.store import DB_FILENAME
    from ai_quota_swap.ledger.usage import resolve_range, run_usage

    if not scan and not (Path(data_root) / DB_FILENAME).exists():
        return None
    since, until = resolve_range(days=days, since=None, until=None, today=today)
    return run_usage(
        data_root, since=since, until=until, by=by, scan=scan,
        registry=registry, progress=False,
    )


def cost_line(result) -> CostLine | None:
    """The cost line from a by-day report of the last seven days; None when
    the ledger holds nothing for them."""
    if result is None:
        return None
    report = result.report
    if report.empty:
        return None
    today_row = next((r for r in report.rows if r.key == report.until), None)
    today_cost = today_row.totals.cost if today_row is not None else 0.0
    return CostLine(
        today=today_cost, week=report.total.cost,
        unpriced=bool(report.total.unpriced_tokens),
    )


def last_days(n: int, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    return today - timedelta(days=n - 1), today


def format_duration(seconds: float) -> str:
    """Compact duration: "45s", "12m", "2h 13m", "3d 4h"."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(s // 3600, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def clock_stamp() -> str:
    """HH:MM:SS local-time stamp for the event log."""
    return time.strftime("%H:%M:%S")


__all__ = [
    "PROVIDER_ORDER",
    "ActionResult",
    "CostLine",
    "Fleet",
    "FleetSource",
    "ProviderView",
    "SnapshotSource",
    "block_for",
    "clock_stamp",
    "cost_line",
    "format_duration",
    "over_budget",
    "provider_lines",
    "run_action",
    "settings_root",
    "usage_report",
]
