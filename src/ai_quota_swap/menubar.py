"""macOS menu bar app for ai-quota-swap (``qswap menubar``): what it shows.

The menu bar is a shell over both providers' switchers and the shared
auto-switch engine; it never re-implements account, usage or auto-switch
logic. It is split so that everything it says can be tested without
``rumps`` (an optional extra, macOS only):

- this module: the settings file, the typed menu description and
  :func:`build_menu`, the one pure function that turns both providers'
  state into the complete menu (title, sections, rows with their text,
  checked/disabled/attention state and an :class:`Action`); the title; the
  health row; the dialogs' words. Pure: no I/O, no clock unless passed in.
- ``menubar_notify``: notification text and de-duplication.
- ``menubar_engines``: hosting one engine per provider in its own thread,
  and noticing active-account changes made outside the menu bar.
- ``menubar_data``: the blocking reads (snapshots, the ledger, the log),
  always run off the main thread.
- ``menubar_app``: the ``rumps`` glue, which only draws a :class:`Menu` and
  maps each :class:`Action` to a handler.

Every word comes from :mod:`ai_quota_swap.display`, :mod:`ai_quota_swap.labels`
and :mod:`ai_quota_swap.autolog`, so a state reads the same here as in
``qswap list``, the dashboard and the auto log (docs/design/ux.md).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import plistlib
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from ai_quota_swap import display, launch_agent, oauth
from ai_quota_swap.exceptions import (
    ClaudeSwitchError,
    KeyHelperUnavailableError,
    ProviderError,
    SessionsRunningError,
)
from ai_quota_swap.fsutil import atomic_write_text
from ai_quota_swap.labels import account_label, is_placeholder, shown_email
from ai_quota_swap.menubar_engines import (  # noqa: F401  (re-exported)
    EngineStoppedEvent,
    engine_exit_applies,
)
from ai_quota_swap.menubar_notify import (  # noqa: F401  (re-exported)
    ENGINE_ERROR_REPEAT_S,
    NOTIFY_TITLE,
    should_surface_engine_error,
)
from ai_quota_swap.printer import warning
from ai_quota_swap.settings import RESET_AUTO_MODES, ResetSettings
from ai_quota_swap.switcher import SENTINEL_NOTES, USAGE_API_KEY, USAGE_RELOGIN_REQUIRED  # noqa: F401

ICON = "⇄"
PROVIDERS: tuple[str, ...] = ("claude", "codex")
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
#: Codex switches restart its background server, so its line sits higher.
CODEX_THRESHOLD_CHOICES: tuple[int, ...] = (90, 95, 98, 99)
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
#: What the title follows: Claude Code's active account, Codex's, or whichever
#: active account is closest to its limit.
TITLE_FOLLOWS_CHOICES: tuple[str, ...] = ("claude", "codex", "closest")
SWITCH_HISTORY_LIMIT = 10
NOTIFICATION_BUNDLE_ID = "com.ai-quota-swap.menubar"
#: Who runs an engine the menu bar hosts, as its heartbeat says.
ENGINE_HOST_LABEL = "qswap menubar"

_logger = logging.getLogger("ai-quota-swap")


def ensure_notification_identity(
    executable: Path | None = None,
    *,
    platform: str = sys.platform,
) -> Path | None:
    """Ensure rumps can resolve a bundle identifier for notifications.

    Fallback for runs outside the wrapper bundle (``ai_quota_swap.app_bundle``),
    whose Info.plist already carries the identifier: a bare command-line
    interpreter has no app bundle, so rumps looks for an ``Info.plist`` beside
    it. uv/pipx reinstalls can recreate that environment; repair the tiny
    plist on every launch when needed.
    """
    if platform != "darwin":
        return None
    path = (executable or Path(sys.executable)).parent / "Info.plist"
    data: dict = {}
    try:
        if path.exists():
            try:
                loaded = plistlib.loads(path.read_bytes())
            except Exception:
                loaded = None  # unreadable/corrupt — rebuild from scratch
            if isinstance(loaded, dict):
                data = loaded
        changed = False
        if not data.get("CFBundleIdentifier"):
            data["CFBundleIdentifier"] = NOTIFICATION_BUNDLE_ID
            changed = True
        if not data.get("CFBundleName"):
            data["CFBundleName"] = "AI Quota Swap"
            changed = True
        if changed or not path.exists():
            # atomic: an interrupted write must not leave a half-written plist
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(plistlib.dumps(data))
            os.replace(tmp, path)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        _logger.warning("Could not prepare menu-bar notification identity: %s", exc)
        return None
    return path


# -- settings --------------------------------------------------------------------


_CHOICES = {"title_pct": TITLE_PCT_CHOICES, "title_follows": TITLE_FOLLOWS_CHOICES}


@dataclass
class MenuBarSettings:
    """The menu bar's own preferences, ``menubar_settings.json`` in the data
    directory.

    Only display preferences and the auto-switch toggles live here.
    Auto-switch *policy* (thresholds, cooldown, ...) is core config, read and
    written through ``ai_quota_swap.settings`` (``autoswitch.*`` and
    ``codex.threshold``), so the CLI and the menu bar share one source.

    ``auto_switch_enabled`` is Claude Code's toggle; it keeps its name because
    older builds and ``qswap menubar --auto-switch`` read and write it.
    ``codex_auto_switch_enabled`` is Codex's, off unless turned on.

    The CLI and the running menu bar both write this file, and a build that
    does not know a key must not drop it: keys this build does not know are
    kept in ``extra`` and written back unchanged.
    """

    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    title_scoped: bool = False  # append per-model weekly limits (e.g. Fable) to the title
    refresh_interval: int = 60
    auto_switch_enabled: bool = False
    codex_auto_switch_enabled: bool = False
    title_follows: str = "claude"  # one of TITLE_FOLLOWS_CHOICES
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def _known(cls) -> list[str]:
        return [f.name for f in fields(cls) if f.name != "extra"]

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        A value of the wrong type (or a choice this build does not offer)
        keeps that field's default; unknown keys are kept for :meth:`save`. A
        missing or unparseable file yields all defaults, as before: Claude
        Code's auto-switch off unless the file turned it on, Codex's off.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs: dict = {}
        known = cls._known()
        for name in known:
            if name not in raw:
                continue
            value, default = raw[name], getattr(defaults, name)
            if isinstance(default, bool) or isinstance(value, bool):
                ok = isinstance(value, bool) and isinstance(default, bool)
            else:
                ok = isinstance(value, type(default))
            if ok and name in _CHOICES and value not in _CHOICES[name]:
                ok = False
            if ok:
                kwargs[name] = value
        kwargs["extra"] = {k: v for k, v in raw.items() if k not in known}
        return cls(**kwargs)

    def to_json(self) -> dict:
        data = dict(self.extra)
        data.update({k: v for k, v in asdict(self).items() if k != "extra"})
        return data

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories.

        Atomic, because the CLI and the running menu bar both write this file
        and :meth:`load` reads a torn write as all-defaults, which would turn
        auto-switch off without anyone touching the menu.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(self.to_json(), indent=2), mode=0o600)

    def auto_enabled(self, provider: str) -> bool:
        """Whether this provider's auto-switch toggle is on."""
        if provider == "codex":
            return self.codex_auto_switch_enabled
        return self.auto_switch_enabled

    def set_auto_enabled(self, provider: str, value: bool) -> None:
        if provider == "codex":
            self.codex_auto_switch_enabled = value
        else:
            self.auto_switch_enabled = value


# -- usage windows (the shape oauth.build_usage_result and UsageEntry.last_good use) --


def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Surfaces the binding window's utilization for display. Spend is excluded —
    it isn't a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def closest_pct(usage: dict | str | None, now: float) -> float | None:
    """How close an account is to a limit: its highest window, per-model
    weekly limits included (a model at 100% is at a limit), weekly windows
    rolled forward when their reset has passed."""
    if not isinstance(usage, dict):
        return None
    windows = [usage.get("five_hour"), _rolled_weekly_window(usage.get("seven_day"), now)]
    windows += [_rolled_weekly_window(w, now) for w in usage.get("scoped") or ()]
    pcts = [
        float(w["pct"]) for w in windows
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float))
    ]
    if isinstance(usage.get("rejected"), dict):
        pcts.append(100.0)
    return max(pcts) if pcts else None


def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad."""
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
    return float("inf")


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Time until a usage window resets, computed live from ``resets_at``.

    The cached usage dict's ``countdown`` string is frozen at fetch time, so a
    stale (e.g. last-known-good) entry would show a wrong remaining time.
    Deriving it from the absolute ``resets_at`` keeps it correct between and
    without refetches. ``None`` when there's no ``resets_at`` or it passed.
    """
    ts = _resets_at_ts(window)
    if ts == float("inf"):
        return None
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    return oauth.format_countdown(remaining)


_WEEKLY_PERIOD_S = 7 * 86400  # weekly limits reset on a fixed 7-day cadence


def _rolled_weekly_window(window: dict | None, now: float) -> dict | None:
    """A weekly window with a passed reset advanced to its next 7-day boundary.

    Weekly limits reset on a fixed weekly cadence, so once the stored
    ``resets_at`` is in the past we know the window rolled over — the stored pct
    belongs to a window that no longer exists. Return a copy reflecting the reset
    state (``pct`` 0, ``resets_at`` advanced to the next future boundary) so the
    menu bar shows the reset from the static schedule alone, without waiting to
    spend tokens on a fresh fetch. Missing/future/unparseable windows are
    returned unchanged.
    """
    if not isinstance(window, dict):
        return window
    ts = _resets_at_ts(window)
    if ts == float("inf") or ts > now:
        return window
    missed = int((now - ts) // _WEEKLY_PERIOD_S) + 1
    new_ts = ts + missed * _WEEKLY_PERIOD_S
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
    rolled.pop("countdown", None)  # recomputed live from the rolled resets_at
    rolled.pop("clock", None)
    return rolled


def rolled_usage(usage: dict | None, now: float) -> dict | None:
    """``usage`` with every weekly window (7d and per-model) rolled forward
    past a reset that has passed (:func:`_rolled_weekly_window`)."""
    if not isinstance(usage, dict):
        return usage
    out = dict(usage)
    if isinstance(usage.get("seven_day"), dict):
        out["seven_day"] = _rolled_weekly_window(usage["seven_day"], now)
    if usage.get("scoped"):
        out["scoped"] = [_rolled_weekly_window(w, now) for w in usage["scoped"]]
    return out


MENU_NOTE_LIMIT = 40

# A reading older than this many display intervals is called old (the health
# row's "as of", an account row's "as of"), but never sooner than
# STALE_USAGE_FLOOR_S: while an engine runs, the display reads store-only and
# the engine polls on its own cadence (an alternate every 5 to 10 minutes),
# so three short display intervals would flag every quiet account.
STALE_REFRESH_INTERVALS = 3
STALE_USAGE_FLOOR_S = 15 * 60


def stale_after(refresh_interval: int) -> float:
    """Age past which a reading says ``as of …`` in the menu."""
    return float(max(STALE_REFRESH_INTERVALS * refresh_interval, STALE_USAGE_FLOOR_S))


def _pct(value: float) -> str:
    """A whole percentage that never rounds up onto the limit (autolog's rule)."""
    from ai_quota_swap.autolog import pct_text

    return pct_text(float(value))


def menu_note(note: str, limit: int = MENU_NOTE_LIMIT) -> str:
    """The state half of a sentinel note, sized for a menu row.

    ``SENTINEL_NOTES`` carry a remedy after an em dash ("needs re-login — log
    in to it in Claude Code (/login), then run: qswap add"), which ``qswap
    list`` has the width for. A menu is as wide as its widest row, so one
    expired account otherwise doubles the menu and pushes every other row's
    numbers to the far edge. The row keeps the state; the full note travels
    as the row's tooltip.
    """
    state = note.split(" — ", 1)[0].strip()
    if len(state) > limit:
        state = state[: limit - 1].rstrip() + "…"
    return state


def _set_tooltip(item, text: str) -> None:
    """Attach ``text`` as the row's hover tooltip; NSMenuItem carries one natively."""
    ns_item = getattr(item, "_menuitem", None)
    if ns_item is not None and hasattr(ns_item, "setToolTip_"):
        ns_item.setToolTip_(text)


#: The widest an account row gets; :func:`display.fit` cuts a longer one
#: (the tooltip carries the whole block).
ROW_LIMIT = 72


def usage_segments(
    usage: dict, now: float, lines: display.Lines | None = None
) -> tuple[list[str], bool, bool]:
    """A reading's windows as compact row segments: bare percentages (``5h
    46% · 7d 38% · Fable 52%``), ``extra 18%`` for extra-usage spend, and a
    refused setup-token probe first as ``limit reached`` (the account is at
    its limit until the refusal ends, however low the windows read).

    A countdown appears only once the account is blocked, on a window at its
    limit (100%, or refused): ``5h 100% (resets in 1h 12m)``. With several at
    the limit, only the one that resets last carries it, since that is when
    the account is usable again. Weekly windows whose reset passed are rolled
    forward. Also returns whether any window is at its limit, and whether any
    is at or past its switch line (``lines``)."""
    lines = lines or display.Lines()
    entries: list[tuple[str, dict | None, bool]] = []  # text, window, at its limit
    past_line = False
    rejected = usage.get("rejected")
    if isinstance(rejected, dict):
        entries.append(("limit reached", rejected, True))
    windows = [("5h", usage.get("five_hour")), ("7d", _rolled_weekly_window(usage.get("seven_day"), now))]
    for window in usage.get("scoped") or []:
        window = _rolled_weekly_window(window, now)
        if isinstance(window, dict) and window.get("name"):
            windows.append((str(window["name"]), window))
    for label, window in windows:
        if not isinstance(window, dict) or not isinstance(window.get("pct"), (int, float)):
            continue
        pct = window["pct"]
        past_line = past_line or display.severity(pct, lines.for_label(label)) != "ok"
        entries.append((f"{label} {_pct(pct)}%", window, pct >= 100))
    blocked = [(i, _resets_at_ts(w)) for i, (_t, w, at) in enumerate(entries) if at]
    future = [(ts, i) for i, ts in blocked if ts != float("inf") and ts > now]
    last = max(future)[1] if future else None
    parts = []
    for i, (text, window, _at) in enumerate(entries):
        if i == last:
            countdown = _live_countdown(window, now)
            if countdown:
                text += f" (resets in {countdown})"
        parts.append(text)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"extra {_pct(spend['pct'])}%")
    return parts, bool(blocked), past_line or bool(blocked)


def usage_summary(
    usage: dict | str | None, now: float | None = None, lines: display.Lines | None = None
) -> str:
    """One-line usage summary of a reading (:func:`usage_segments`). A string
    is a state note, cut to its state; no reading at all is ``usage
    unavailable``."""
    if isinstance(usage, str):
        return menu_note(usage)
    if usage is None:
        return display.USAGE_UNAVAILABLE
    parts, _limit, _past = usage_segments(usage, time.time() if now is None else now, lines)
    return " · ".join(parts) if parts else display.USAGE_UNAVAILABLE


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker. A
    placeholder name (``setup-token-13``) is shown whole: it is one word."""
    local = email.split("@", 1)[0]
    if len(local) > limit and not is_placeholder(email):
        return local[: limit - 1] + "*"
    return local


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
    now: float | None = None,
    alias: str | None = None,
    *,
    attention: bool = False,
) -> str:
    """The menu bar title from one active account and the title settings.

    ``⇄``, then, joined by `` · ``: the account's alias or the part of its
    email before ``@`` (12 characters, longer ends in ``*``), ``limit
    reached`` for a refused setup-token probe, the 5h and/or 7d percentage,
    and with ``title_scoped`` each per-model window (``Fable 52%``).
    ``attention`` prefixes a warning sign: auto-switch is enabled but not
    running, which the closed menu would otherwise never show.
    """
    icon = f"⚠ {ICON}" if attention else ICON
    if active_email is None:
        return icon
    if now is None:
        now = time.time()
    segments: list[str] = []
    if settings.show_account_name:
        segments.append(alias if alias else _local_part(active_email))
    if isinstance(active_usage, dict) and isinstance(active_usage.get("rejected"), dict):
        segments.append("limit reached")
    if settings.title_pct in ("5h", "both"):
        p = _window_pct(active_usage, "five_hour")
        if p is not None:
            segments.append(f"{_pct(p)}%")
    if settings.title_pct in ("7d", "both"):
        seven = active_usage.get("seven_day") if isinstance(active_usage, dict) else None
        seven = _rolled_weekly_window(seven, now)  # reflect a passed weekly reset
        p = seven["pct"] if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)) else None
        if p is not None:
            segments.append(f"{_pct(p)}%")
    if settings.title_scoped and isinstance(active_usage, dict):
        # Per-model weekly limits (e.g. Fable), same roll-forward as the rows;
        # named so several scoped models stay distinguishable.
        for window in active_usage.get("scoped") or []:
            window = _rolled_weekly_window(window, now)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                segments.append(f"{window['name']} {_pct(window['pct'])}%")
    if not segments:
        return icon
    return f"{icon} " + " · ".join(segments)


def format_usage_log(who: str, usage: dict | str | None) -> str | None:
    """A log line of an account's 5h and 7d windows: ``usage #1 personal:
    5h 35% (resets 06:59) · 7d 55%``.

    Uses each window's absolute reset ``clock`` rather than a live countdown,
    since log lines are already timestamped. Returns ``None`` when no numeric
    window is available (sentinels, ``None``, or spend-only) so callers can
    skip logging nothing.
    """
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = _window_pct(usage, key)
        if pct is None:
            continue
        window = usage.get(key)  # a dict — _window_pct found a numeric pct in it
        clock = window.get("clock") if isinstance(window, dict) else None
        seg = f"{label} {_pct(pct)}%"
        if clock:
            seg += f" (resets {clock})"
        parts.append(seg)
    if not parts:
        return None
    return f"usage {who}: " + " · ".join(parts)


def _usage_log_key(usage: dict | str | None) -> tuple[float | None, float | None]:
    """De-dupe key for usage logging: the (5h, 7d) percentages only.

    Reset clocks change every refresh; keying on the percentages means an idle
    account isn't re-logged every cycle.
    """
    return (_window_pct(usage, "five_hour"), _window_pct(usage, "seven_day"))


# -- switch history (from the log) ---------------------------------------------------


_SWITCH_LOG_RES = (
    ("claude", re.compile(r"Switched from account (\d+) to (\d+)")),
    ("codex", re.compile(r"Switched Codex from account (\w+) to (\d+)")),
)


@dataclass(frozen=True)
class SwitchRecord:
    """One switch the log recorded: provider, from and to account numbers
    (``source`` None when there was no managed account before), and the
    minute it happened (``YYYY-MM-DD HH:MM``)."""

    provider: str
    source: str | None
    target: str
    stamp: str


def parse_switch_history(log_text: str, limit: int = SWITCH_HISTORY_LIMIT) -> list[SwitchRecord]:
    """Recent switches from the log, most recent first.

    Reads the lines both switchers log (``Switched from account X to Y``,
    ``Switched Codex from account X to Y``) and pairs each with its
    timestamp, trimmed to the minute. Unparseable lines are skipped.
    """
    out: list[SwitchRecord] = []
    for line in log_text.splitlines():
        for provider, pattern in _SWITCH_LOG_RES:
            m = pattern.search(line)
            if m:
                break
        else:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]  # "YYYY-MM-DD HH:MM"
        source = m.group(1) if m.group(1).isdigit() else None
        out.append(SwitchRecord(provider, source, m.group(2), stamp))
    return out[-limit:][::-1]


# -- what the menu is built from -------------------------------------------------------


@dataclass(frozen=True)
class AccountState:
    """One managed account as the menu shows it.

    ``entry`` is the account's stored reading (a ``UsageEntry`` or a
    :class:`display.Reading`), None before the first read. ``set_aside`` is
    the engine's reason for holding it out until its owner logs in again.
    ``plan`` is its plan label already in words (``Max 20x``).
    """

    provider: str
    number: str
    email: str
    alias: str = ""
    active: bool = False
    disabled: bool = False
    kind: str = "oauth"
    entry: object | None = None
    set_aside: str | None = None
    login_expires_at: float | None = None
    plan: str = ""
    org: str = ""
    #: Its switch lines (its own threshold, else the provider's): a window
    #: at or past one shows when it resets.
    lines: display.Lines = display.Lines()
    #: The subscription logged in under an active key; on that key, the
    #: account underneath (``#1 personal``). docs/design/api-key-sessions.md.
    underneath: bool = False
    over: str = ""
    #: On the active key: auto-switch entered it (decision B8).
    by_engine: bool = False

    def label(self, *, provider_prefix: bool = False, long: bool = True) -> str:
        """``#2 work (maya@example.com)``; ``Claude #2 work (…)`` with the prefix."""
        return account_label(
            self.number, alias=self.alias or None, email=self.email,
            provider=self.provider if provider_prefix else None, long=long,
        )

    @property
    def needs_relogin(self) -> bool:
        return self.set_aside is not None or getattr(self.entry, "sentinel", None) == USAGE_RELOGIN_REQUIRED

    def usage(self) -> dict | None:
        last_good = getattr(self.entry, "last_good", None)
        return last_good if isinstance(last_good, dict) else None


@dataclass(frozen=True)
class ProviderState:
    """One provider as the last background read saw it.

    ``live_email`` is the provider's live login, managed or not; it is
    unmanaged when no account is active. ``cli_found`` is whether its command
    is on PATH (None: not checked). ``error`` is why it could not be read (a
    keyring-mode Codex home, an unreadable registry), in the command line's
    words. ``managed`` counts its accounts even when they could not be read.
    """

    provider: str
    accounts: tuple[AccountState, ...] = ()
    live_email: str | None = None
    cli_found: bool | None = None
    error: str | None = None
    managed: int | None = None
    #: What every surface says when the key helper line names an account
    #: that cannot give a key, or run profiles kept a stale settings copy.
    key_notes: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return self.managed if self.managed is not None else len(self.accounts)

    @property
    def active(self) -> AccountState | None:
        return next((a for a in self.accounts if a.active), None)

    @property
    def unmanaged_live(self) -> str | None:
        """The live login's email when qswap does not manage it, also under
        an active key with no managed subscription underneath (decision B7,
        F27); unknown, so None, while the provider could not be read."""
        if not self.live_email or self.error:
            return None
        active = self.active
        if active is not None:
            if active.kind != "api_key" or any(a.underneath for a in self.accounts):
                return None
        if any(a.email == self.live_email for a in self.accounts):
            return None
        return self.live_email


#: An engine's state, per provider: off (toggle off), running, stopped (the
#: toggle is on and the engine is not running: it failed to start or its
#: thread died; a click restarts it), elsewhere (the toggle is off and another
#: process's engine is running for it, e.g. ``qswap auto``).
ENGINE_STATES = ("off", "running", "stopped", "elsewhere")


@dataclass(frozen=True)
class EngineState:
    provider: str
    state: str = "off"
    failure: str | None = None
    host: str | None = None  # who runs it, for "elsewhere"


@dataclass(frozen=True)
class CostSummary:
    """Today's and the last seven days' estimated cost from the ledger, both
    providers together (``qswap usage``'s totals)."""

    today: float
    week: float
    today_unpriced: int = 0
    week_unpriced: int = 0

    def text(self) -> str:
        """``Today $12.40 · 7 days $179.72 (estimated)``."""
        from ai_quota_swap.ledger.report import Totals, fmt_cost

        today = fmt_cost(Totals(cost=self.today, unpriced_tokens=self.today_unpriced))
        week = fmt_cost(Totals(cost=self.week, unpriced_tokens=self.week_unpriced))
        return f"Today {today} · 7 days {week} (estimated)"


@dataclass(frozen=True)
class MenuInput:
    """Everything :func:`build_menu` reads."""

    claude: ProviderState
    codex: ProviderState
    settings: MenuBarSettings
    now: float
    engines: Mapping[str, EngineState] = field(default_factory=dict)
    cost: CostSummary | None = None
    history: tuple[SwitchRecord, ...] = ()
    start_at_login: bool = False
    thresholds: Mapping[str, float] = field(default_factory=dict)
    can_add_token: bool = True
    #: The shared ``resets`` section of settings.json (both providers').
    resets: ResetSettings = field(default_factory=ResetSettings)
    #: Accounts with a *Use a reset* claim in flight, as ``(provider, number)``.
    resets_busy: frozenset = frozenset()

    def provider(self, provider: str) -> ProviderState:
        return self.codex if provider == "codex" else self.claude

    @property
    def codex_shown(self) -> bool:
        """Codex gets a section (and every Codex control) once it manages an account."""
        return self.codex.count > 0

    def engine(self, provider: str) -> EngineState:
        return self.engines.get(provider) or EngineState(provider)


# -- the menu description ---------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """What a click does. ``kind`` names the handler; the rest are its
    arguments (a provider, an account number, a chosen value)."""

    kind: str
    provider: str | None = None
    number: str | None = None
    value: object = None


@dataclass(frozen=True)
class Row:
    """One menu row. A row without an action is information (drawn greyed
    out); ``children`` make it a submenu; ``checked`` draws a check mark;
    ``attention`` marks a state that needs the user (needs re-login, a limit
    reached, an engine that stopped)."""

    text: str
    action: Action | None = None
    checked: bool = False
    attention: bool = False
    tooltip: str = ""
    children: tuple["Row", ...] = ()
    separator: bool = False
    heading: bool = False

    @property
    def disabled(self) -> bool:
        return self.action is None and not self.children and not self.separator


SEPARATOR = Row("", separator=True)


@dataclass(frozen=True)
class Section:
    """Rows drawn together, separated from the next section by a line.
    ``heading`` (when set) is drawn first, greyed out."""

    key: str
    rows: tuple[Row, ...]
    heading: str | None = None


@dataclass(frozen=True)
class Menu:
    title: str
    sections: tuple[Section, ...]

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)

    def rows(self) -> list[Row]:
        """Every top-level row in drawing order, headings and separators included."""
        out: list[Row] = []
        for i, section in enumerate(self.sections):
            if i:
                out.append(SEPARATOR)
            if section.heading:
                out.append(Row(section.heading, heading=True))
            out.extend(section.rows)
        return out

    def find(self, text: str) -> Row | None:
        """The first row anywhere whose text starts with ``text``."""
        def walk(rows: Sequence[Row]) -> Row | None:
            for row in rows:
                if not row.separator and row.text.startswith(text):
                    return row
                hit = walk(row.children)
                if hit is not None:
                    return hit
            return None

        return walk(self.rows())

    def outline(self, *, actions: bool = False) -> str:
        """The menu as indented text: ``✓`` checked, ``⚠`` attention, ``▸`` a
        submenu, ``(greyed)`` information, ``───`` a separator."""
        lines = [f"title: {self.title}"]

        def walk(rows: Sequence[Row], depth: int) -> None:
            pad = "    " * depth
            for row in rows:
                if row.separator:
                    lines.append(f"{pad}────")
                    continue
                mark = "✓ " if row.checked else "  "
                flag = "⚠ " if row.attention else ""
                text = f"{pad}{mark}{flag}{row.text}"
                if row.children:
                    text += " ▸"
                elif row.heading:
                    text += "  (heading)"
                elif row.disabled:
                    text += "  (greyed)"
                if actions and row.action is not None:
                    a = row.action
                    args = " ".join(str(x) for x in (a.provider, a.number, a.value) if x is not None)
                    text += f"  [{a.kind}{' ' + args if args else ''}]"
                lines.append(text)
                walk(row.children, depth + 1)

        walk(self.rows(), 0)
        return "\n".join(lines)


# -- an account row ------------------------------------------------------------------


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _reset_strings(now: float):
    """``(countdown, clock)`` for a window, from ``now`` (never the wall clock)."""

    def cell(window: dict) -> tuple[str, str] | None:
        ts = _resets_at_ts(window)
        if ts == float("inf"):
            return None
        clock = oauth.reset_clock_string(
            datetime.fromtimestamp(ts, tz=timezone.utc), datetime.fromtimestamp(now, tz=timezone.utc)
        )
        return oauth.format_countdown(max(0.0, ts - now)), clock

    return cell


def _api_lines(reading: dict) -> list[str]:
    """An API-key account's spend, as ``qswap list`` prints it."""
    from ai_quota_swap import api_spend

    text = api_spend.spend_text(reading).removeprefix("API key · ").removeprefix("API key")
    out = [text] if text else []
    detail = api_spend.detail_text(reading)
    if detail:
        out.append(detail)
    return out


def account_block(acc: AccountState, *, now: float, stale_after_s: float, show_provider: bool = False):
    """The account's :class:`display.AccountBlock`: the state ``qswap list``
    classifies it into (needs re-login, unavailable, API key, not read, a
    provider note, or a reading), its windows and held resets. The menu row
    is a line of it and the row's tooltip is all of it."""
    entry = acc.entry
    reading = None
    if entry is not None:
        fetched_at = getattr(entry, "fetched_at", None)
        age = now - fetched_at if isinstance(fetched_at, (int, float)) else getattr(entry, "age_s", None)
        reading = display.Reading(
            sentinel=getattr(entry, "sentinel", None),
            last_good=rolled_usage(getattr(entry, "last_good", None), now),
            fetched_at=fetched_at,
            age_s=age,
            last_error=getattr(entry, "last_error", None),
            backoff_until=getattr(entry, "backoff_until", None),
            api_spend=getattr(entry, "api_spend", None),
        )
    if acc.provider == "codex":
        from ai_quota_swap.codex.switcher import SENTINEL_NOTES as notes
    else:
        notes = SENTINEL_NOTES
    return display.block_from_entry(
        acc.number, reading,
        email=acc.email, alias=acc.alias, provider=acc.provider, show_provider=show_provider,
        active=acc.active, disabled=acc.disabled, plan=acc.plan, org=acc.org,
        kind=acc.kind, lines=acc.lines, set_aside=acc.set_aside, sentinel_notes=notes,
        underneath=acc.underneath, over=acc.over, by_engine=acc.by_engine,
        now=now, stale_after_s=stale_after_s, reset_strings=_reset_strings(now),
        api_lines=_api_lines,
    )


def block_text(block) -> str:
    """A block as plain text, the way ``qswap list`` prints it (the tooltip)."""
    lines = display.render_block(block, width=display.WIDTH, indent="")
    return "\n".join(_ANSI.sub("", line).replace(" — ", ": ").rstrip() for line in lines)


def account_row(
    acc: AccountState, *, now: float, stale_after_s: float, action: Action | None = None
) -> Row:
    """One account's row, short enough to scan: ``#1 personal · 5h 46% · 7d
    38% · Fable 52% · 1 Fable reset``.

    The label is the short one; the windows are bare percentages, with a
    countdown only on a window at its limit (``5h 100% (resets in 1h 12m)``;
    one, on the window that resets last); held resets come last. A window at
    or past the account's switch line makes the row orange (``attention``). A state replaces the
    percentages, in ``qswap list``'s words: ``needs re-login``, ``usage
    unavailable (HTTP 503), retrying``, ``usage not read yet``, ``API key ·
    $4.12 today · …`` (with a budget, the compact ``$212.40/$500 (42%)``); an
    old reading adds ``as of 3h ago``. A stored login nearing its expiry ends
    the row (``re-login in 5d 12h``). The row fits :data:`ROW_LIMIT`
    (:func:`fit_row`); the tooltip is the whole block.
    """
    from ai_quota_swap import api_spend, resets

    block = account_block(acc, now=now, stale_after_s=stale_after_s)
    parts = [acc.label(long=False)]
    attention = False
    if acc.underneath:
        parts.append(display.UNDERNEATH)
    if acc.disabled:
        parts.append("disabled")
    state = block.state
    if state == "needs-relogin":
        parts.append(display.NEEDS_RELOGIN)
        attention = True
    elif state == "unavailable":
        retrying = block.error not in ("store-unmirrored", "invalid_client", "setup-token")
        parts.append(display.usage_unavailable(block.error, retry_in_s=block.retry_in_s, retrying=retrying))
    elif state == "api-key":
        reading = getattr(acc.entry, "api_spend", None)
        parts.append("API key")
        over = display.over_fact(acc.over) if acc.over else None
        by = display.BY_ENGINE if acc.by_engine else None
        if api_spend.has_limit(reading):
            # With a budget or a credit, the compact form: the figure (and
            # "over budget", "credit spent", "limit not enforced") must never
            # be what the row limit cuts. Short of room, "over #1 personal"
            # gives way first (the #1 row says "underneath"), then today's
            # figure, then "by auto-switch" (with no engine running, the note
            # row says it in full); the tooltip keeps them all.
            # Last, a credit that follows a budget reached gives way whole,
            # never cut mid-figure.
            full = api_spend.menu_text(reading, now=now)
            short = api_spend.menu_text(reading, now=now, today=False)
            bare = api_spend.menu_text(reading, now=now, today=False, credit=False)
            tries = ([over, by, full], [by, full], [by, short], [short], [bare])
        else:
            summary = display.api_key_summary(block)
            tries = ([over, by, summary], [by, summary], [summary])
        whole = not acc.alias and is_placeholder(acc.email)
        for extra in tries:
            facts = [x for x in extra if x]
            if _row_fits([*parts, *facts], whole_name=whole):
                break
        parts.extend(facts)
        # A limit reached, or one not enforced (no fresh figure): the engine
        # treats both as at the limit and leaves the key (decision A4).
        attention = api_spend.limit_reason(reading) is not None or (
            api_spend.limit_not_enforced(reading, now)
        )
    elif state == "not-read":
        parts.append(display.NOT_READ_YET)
    elif state == "note":
        parts.append(menu_note(block.notes[0]) if block.notes else display.USAGE_UNAVAILABLE)
    else:
        usage = rolled_usage(acc.usage(), now) or {}
        segments, _at_limit, past_line = usage_segments(usage, now, acc.lines)
        parts.extend(segments or [display.USAGE_UNAVAILABLE])
        attention = past_line
        if block.stale_s is not None:
            stale = display.as_of(block.stale_s)
            if block.error:
                stale += f" ({display.error_label(block.error)})"
            parts.append(stale)
        held = display.held_compact(resets.counts_by_kind(usage.get("resets"), now), acc.provider)
        if held:
            parts.append(held)
    if state != "needs-relogin":
        note = oauth.format_login_expiry(acc.login_expires_at, False, now)
        if note:
            parts.append(note)
            attention = attention or note == display.NEEDS_RELOGIN
    return Row(
        fit_row(parts, whole_name=not acc.alias and is_placeholder(acc.email)),
        action=action, checked=acc.active,
        attention=attention, tooltip=block_text(block),
    )


#: The shortest an account label gets when a row is too long: ``#5 api…``.
LABEL_FLOOR = 7


def _row_fits(parts: Sequence[str], limit: int = ROW_LIMIT, *, whole_name: bool = False) -> bool:
    """Whether :func:`fit_row` fits ``parts`` without cutting the end: the
    label may give way (down to :data:`LABEL_FLOOR`, or to its number)."""
    if not parts:
        return True
    label = parts[0]
    if whole_name:
        shortest = label.rsplit(" ", 1)[0] if " " in label else label
    else:
        shortest = label[: min(len(label), LABEL_FLOOR)]
    return len(" · ".join([shortest, *parts[1:]])) <= limit


def fit_row(parts: Sequence[str], limit: int = ROW_LIMIT, *, whole_name: bool = False) -> str:
    """``parts`` joined with `` · `` within ``limit`` columns. The label
    (``parts[0]``) gives way first, down to :data:`LABEL_FLOOR` (its number
    stays, the full name is in the tooltip); only then is the end cut. Both
    cuts are :func:`display.fit`'s. ``whole_name``: the label's name is one
    word that must not be cut (a placeholder such as ``api-key-5``), so it
    stays whole or goes altogether, leaving the number."""
    text = " · ".join(parts)
    if len(text) <= limit or not parts:
        return text
    over = len(text) - limit
    label = parts[0]
    if whole_name:
        number = label.rsplit(" ", 1)[0] if " " in label else label
        return display.fit(" · ".join([number, *parts[1:]]), limit)
    room = max(LABEL_FLOOR, len(label) - over)
    if room < len(label):
        text = " · ".join([display.fit(label, room), *parts[1:]])
    return display.fit(text, limit)


# -- the title ------------------------------------------------------------------------


def title_provider(inp: MenuInput) -> str:
    """Which provider's active account the title shows (``title_follows``).

    ``closest`` picks the active account nearest a limit; a tie or two
    unknown readings keep Claude Code. Following Codex while Codex has no
    active account falls back to Claude Code, and the other way round.
    """
    follows = inp.settings.title_follows if inp.codex_shown else "claude"
    claude, codex = inp.claude.active, inp.codex.active
    if follows == "codex":
        return "codex" if codex is not None or claude is None else "claude"
    if follows == "closest":
        if claude is None:
            return "codex" if codex is not None else "claude"
        if codex is None:
            return "claude"
        a = closest_pct(_title_usage(claude), inp.now)
        b = closest_pct(_title_usage(codex), inp.now)
        if b is not None and (a is None or b > a):
            return "codex"
        return "claude"
    return "claude" if claude is not None or codex is None else "codex"


def _title_usage(acc: AccountState) -> dict | None:
    """The reading the title quotes: none for an account in a provider
    state (needs re-login, token expired, API key, ...), whose numbers are
    not the account's live standing."""
    if acc.set_aside is not None or getattr(acc.entry, "sentinel", None):
        return None
    return acc.usage()


def menu_title(inp: MenuInput, *, attention: bool = False) -> str:
    acc = inp.provider(title_provider(inp)).active
    if acc is None:
        return format_title(None, None, inp.settings, inp.now, attention=attention)
    return format_title(
        acc.email, _title_usage(acc), inp.settings, inp.now,
        alias=acc.alias or None, attention=attention,
    )


# -- the health row ---------------------------------------------------------------------


def _engine_word(state: EngineState) -> str:
    if state.state == "running":
        return "running"
    if state.state == "stopped":
        return "stopped"
    if state.state == "elsewhere":
        return f"running in {state.host or 'another process'}"
    return "off"


def engines_text(states: Sequence[EngineState], *, both: bool) -> tuple[str, bool]:
    """The auto-switch half of the health row, and whether it needs a click.

    One provider: ``Auto-switch off`` / ``Auto-switch running`` / ``⚠
    Auto-switch stopped, click to restart`` / ``Auto-switch running in qswap
    auto``. Both: ``Auto-switch running for Claude Code and Codex`` when they
    agree, else each named: ``Auto-switch: Claude Code running, Codex off``.
    """
    shown = [s for s in states if both or s.provider == "claude"]
    attention = any(s.state == "stopped" for s in shown)
    words = [_engine_word(s) for s in shown]
    names = [display.provider_name(s.provider) for s in shown]
    if len(set(words)) == 1:
        word = words[0]
        who = f" for {' and '.join(names)}" if both and word != "off" else ""
        if word == "stopped":
            return f"⚠ Auto-switch stopped{who}, click to restart", True
        return f"Auto-switch {word}{who}", False
    text = "Auto-switch: " + ", ".join(f"{n} {w}" for n, w in zip(names, words))
    if attention:
        return f"⚠ {text}, click to restart", True
    return text, False


def usage_age(inp: MenuInput) -> float | None:
    """Seconds since the oldest active account's reading, or None.

    The health row dates the measurement, not the display's last look at the
    store: a store-only refresh (an engine paces fetching while it runs)
    would otherwise call a two-hour-old reading "just now".
    """
    ages = []
    for provider in ("claude", "codex") if inp.codex_shown else ("claude",):
        acc = inp.provider(provider).active
        fetched_at = getattr(acc.entry, "fetched_at", None) if acc is not None else None
        if isinstance(fetched_at, (int, float)):
            ages.append(max(0.0, inp.now - fetched_at))
    return max(ages) if ages else None


def usage_age_text(age: float | None, refresh_interval: int) -> str:
    """``usage read 2m ago``, ``usage as of 2h 0m ago`` once old, or ``no
    usage reading yet``."""
    if age is None:
        return "no usage reading yet"
    if age > stale_after(refresh_interval):
        return f"usage {display.as_of(age)}"
    return f"usage read {display.ago(age)}"


def health_row(inp: MenuInput) -> Row:
    """The menu's first row: each engine in words, then how old the active
    readings are. A stopped engine makes it clickable (restart)."""
    states = [inp.engine(p) for p in PROVIDERS]
    text, attention = engines_text(states, both=inp.codex_shown)
    text += f" · {usage_age_text(usage_age(inp), inp.settings.refresh_interval)}"
    failures = [
        f"{display.provider_name(s.provider)}: {s.failure}" if inp.codex_shown else s.failure
        for s in states if s.state == "stopped" and s.failure
    ]
    return Row(
        text,
        action=Action("restart-engines") if attention else None,
        attention=attention,
        tooltip="\n".join(f for f in failures if f),
    )


def health_label(
    *,
    auto_enabled: bool,
    engine_running: bool,
    usage_age: float | None,
    refresh_interval: int,
) -> tuple[str, bool]:
    """The health row for Claude Code alone: ``(label, attention)``."""
    if not auto_enabled:
        state = "off"
    else:
        state = "running" if engine_running else "stopped"
    text, attention = engines_text([EngineState("claude", state)], both=False)
    return f"{text} · {usage_age_text(usage_age, refresh_interval)}", attention


# -- sections ----------------------------------------------------------------------------


def _first_run_rows(inp: MenuInput) -> list[Row]:
    """Nothing managed by either provider: why, and the next step for each
    login this machine has (``qswap list``'s first run). A line naming a live
    login adds it when clicked."""
    rows: list[Row] = []
    claude, codex = inp.claude, inp.codex
    for i, line in enumerate(display.nothing_managed_lines(
        "claude", live_email=claude.live_email, cli_found=claude.cli_found,
    )):
        action = Action("add-current", "claude") if i and claude.live_email else None
        rows.append(Row(line, action=action))
    if codex.live_email:
        for line in display.nothing_managed_lines("codex", live_email=codex.live_email, sentence=False):
            rows.append(Row(line, action=Action("add-current", "codex")))
    return rows


def _provider_rows(inp: MenuInput, state: ProviderState) -> list[Row]:
    provider = state.provider
    name = display.provider_name(provider)
    if state.error:
        return [Row(f"{name} could not be read: {state.error}")]
    if not state.accounts:
        rows = [Row(f"No {name} accounts are managed yet.")]
        for line in display.nothing_managed_lines(provider, live_email=state.live_email, sentence=False):
            action = Action("add-current", provider) if state.live_email else None
            rows.append(Row(line, action=action))
        return rows
    stale = stale_after(inp.settings.refresh_interval)
    rows = [
        account_row(acc, now=inp.now, stale_after_s=stale, action=Action("switch", provider, acc.number))
        for acc in state.accounts
    ]
    if state.key_notes:
        # A key helper line that cannot give a key, or stale run-profile
        # copies: one row, the whole sentence (and its repair) in the tooltip.
        text = " ".join(line.strip() for line in state.key_notes)
        rows.append(Row(display.fit(text, ROW_LIMIT), attention=True, tooltip=text))
    return rows


def _strategy_rows(inp: MenuInput) -> list[Row]:
    suffix = " (Claude Code)" if inp.codex_shown else ""
    return [
        Row(f"Rotate to next{suffix}", action=Action("rotate", "claude", value=None)),
        Row(f"Switch to best{suffix}", action=Action("rotate", "claude", value="best")),
        Row(f"Next available{suffix}", action=Action("rotate", "claude", value="next-available")),
    ]


def _all_accounts(inp: MenuInput) -> list[AccountState]:
    accounts = list(inp.claude.accounts)
    if inp.codex_shown:
        accounts += list(inp.codex.accounts)
    return accounts


def _add_menu(inp: MenuInput) -> Row:
    rows: list[Row] = []
    for state in (inp.claude, inp.codex):
        email = state.unmanaged_live
        if email:
            rows.append(Row(
                f"Add the current {display.provider_name(state.provider)} login ({email})",
                action=Action("add-current", state.provider),
            ))
    if inp.can_add_token:
        rows.append(Row("Add from a setup-token or API key…", action=Action("add-token", "claude")))
    if inp.codex_shown:
        rows.append(Row("Log in another Codex account…", action=Action("codex-login-help", "codex")))
    return Row("Add account", children=tuple(rows))


def _accounts_submenu(inp: MenuInput, title: str, kind: str) -> Row:
    prefix = inp.codex_shown
    rows: list[Row] = []
    for acc in _all_accounts(inp):
        label = acc.label(provider_prefix=prefix)
        if kind == "toggle-disabled":
            # A check mark here means "held out of rotation", not "active".
            rows.append(Row(label, action=Action(kind, acc.provider, acc.number, not acc.disabled),
                            checked=acc.disabled))
        else:
            rows.append(Row(label, action=Action(kind, acc.provider, acc.number)))
    if not rows:
        rows.append(Row(display.NOTHING_MANAGED))
    return Row(title, children=tuple(rows))


def history_rows(inp: MenuInput) -> list[Row]:
    """``#3 ops → #1 personal · 2026-06-27 02:06``, newest first; provider-
    prefixed (``Codex #1 → #2 work``) when both providers are shown."""
    labels = {(a.provider, a.number): a for a in inp.claude.accounts + inp.codex.accounts}
    rows = []
    for rec in inp.history:
        if rec.provider == "codex" and not inp.codex_shown:
            continue
        prefix = inp.codex_shown

        def name(number: str | None, *, show: bool) -> str:
            acc = labels.get((rec.provider, number))
            if acc is not None:
                return acc.label(provider_prefix=show, long=False)
            return account_label(number, provider=rec.provider if show else None)

        dst = name(rec.target, show=prefix and rec.source is None)
        if rec.source is None:
            text = f"to {dst}"
        else:
            text = f"{name(rec.source, show=prefix)} → {dst}"
        rows.append(Row(f"{text} · {rec.stamp}"))
    return rows


def _history_menu(inp: MenuInput) -> Row:
    rows = history_rows(inp) or [Row("No switches logged yet")]
    rows += [SEPARATOR, Row("Open full log…", action=Action("open-log"))]
    return Row("Switch history", children=tuple(rows))


_TITLE_PCT_LABELS = {
    "off": "None",
    "5h": "5-hour window (5h)",
    "7d": "Weekly window (7d)",
    "both": "Both (5h · 7d)",
}
_TITLE_FOLLOWS_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "closest": "Whichever is closest to its limit",
}
_INTERVAL_LABELS = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}


def _threshold_menu(inp: MenuInput, provider: str, title: str) -> Row:
    choices = CODEX_THRESHOLD_CHOICES if provider == "codex" else AUTO_THRESHOLD_CHOICES
    current = inp.thresholds.get(provider)
    return Row(title, children=tuple(
        Row(f"{pct}%", action=Action("threshold", provider, value=pct),
            checked=current is not None and float(current) == float(pct))
        for pct in choices
    ))


# -- usage resets: the automatic mode and spending one by hand ------------------------

#: When each ``resets.auto`` mode spends a reset by itself (settings.py,
#: README "Usage resets"). The submenu's choices are these, capitalized.
_RESET_AUTO_WHEN = {
    "active": "when the active account reaches its limit",
    "last-resort": "as a last resort, when no account can take over",
    "on-limit": "when any account reaches its limit",
}
RESET_AUTO_LABELS = {
    "off": "Off",
    **{mode: when[:1].upper() + when[1:] for mode, when in _RESET_AUTO_WHEN.items()},
}
RESETS_APPLY_TO = "Applies to Claude Code and Codex"
RESETS_OFF_NOTE = "Usage resets are not read (resets.enabled is off)"
WEEKLY_FOR_MODEL_LABEL = "Use a weekly reset for a single model's limit"
_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _resets_menu(inp: MenuInput) -> Row:
    """*Use resets automatically*: one checked choice per ``resets.auto``
    mode, then the ``resets.weeklyForModelLimit`` toggle. Each choice's
    tooltip is ``qswap reset``'s ``Auto:`` line for it."""
    rs = inp.resets
    # Shared by both providers; said only where Codex is part of the menu.
    rows: list[Row] = [Row(RESETS_APPLY_TO)] if inp.codex_shown else []
    if not rs.enabled:
        rows.append(Row(RESETS_OFF_NOTE))
    for mode in RESET_AUTO_MODES:
        rows.append(Row(
            RESET_AUTO_LABELS[mode], action=Action("resets-auto", value=mode),
            checked=rs.auto == mode, tooltip=display.reset_mode_line(mode, rs.threshold),
        ))
    rows.append(SEPARATOR)
    rows.append(Row(
        WEEKLY_FOR_MODEL_LABEL, action=Action("toggle-weekly-for-model"),
        checked=rs.weekly_for_model_limit,
        tooltip="resets.weeklyForModelLimit: automatic use may spend a weekly or full "
                "reset when only a per-model limit such as Fable's is reached",
    ))
    return Row("Use resets automatically", children=tuple(rows))


def resets_auto_confirmation(current: str, chosen: str, *, codex: bool = False) -> str | None:
    """The one sentence asked before automatic use turns on (``off`` to any
    other mode); None when nothing needs asking. ``codex``: Codex manages
    accounts too, so the sentence names its reset credits and the cap per
    provider."""
    from ai_quota_swap.autoswitch import MAX_AUTO_RESETS_PER_DAY

    when = _RESET_AUTO_WHEN.get(chosen)
    if current != "off" or when is None:
        return None
    cap = _NUMBER_WORDS.get(MAX_AUTO_RESETS_PER_DAY, str(MAX_AUTO_RESETS_PER_DAY))
    what = "a usage reset or Codex reset credit" if codex else "a usage reset"
    per = " per provider" if codex else ""
    return (
        f"Whenever auto-switch runs, it will spend {what} by itself {when}, "
        f"at most {cap} in 24 hours{per}."
    )


def change_resets_setting(action: Action, resets: ResetSettings, *, codex: bool, confirm, write) -> bool:
    """A *Use resets automatically* click. ``resets`` is the section as it is
    on disk at the click (not the menu's last read, so a value set to off in
    a terminal since still asks). From off to another mode, ``confirm(title,
    question)`` decides first; then ``write(key, value)`` (``settings.set_setting``)
    writes what ``qswap config set`` would. Returns whether it wrote."""
    if action.kind == "resets-auto":
        question = resets_auto_confirmation(resets.auto, str(action.value), codex=codex)
        if question and not confirm("Use resets automatically", question):
            return False
    key, value = resets_setting(action, resets)
    write(key, value)
    return True


def resets_setting(action: Action, resets: ResetSettings) -> tuple[str, str]:
    """The ``settings.json`` key a *Use resets automatically* click writes and
    the value, spelled as ``qswap config set KEY VALUE`` takes it."""
    if action.kind == "resets-auto":
        return "resets.auto", str(action.value)
    if action.kind == "toggle-weekly-for-model":
        return "resets.weeklyForModelLimit", "false" if resets.weekly_for_model_limit else "true"
    raise ValueError(f"not a resets setting: {action.kind}")


def _reset_noun(grant: dict, codex: bool) -> str:
    from ai_quota_swap import resets

    return "reset credit" if codex else resets.reset_name(grant)


def _account_reset_rows(acc: AccountState, inp: MenuInput) -> list[Row]:
    """One row per kind of reset ``acc`` holds: ``Claude #1 personal · Fable
    reset · clears Fable usage only``. Usable: clicking spends one (the
    usable grant of that kind that expires first). Otherwise greyed with
    why, in ``qswap reset``'s words; a kind qswap cannot name is greyed."""
    from ai_quota_swap import manual_reset, resets

    usage = acc.usage()
    status = usage.get("resets") if isinstance(usage, dict) else None
    grants = resets.unused_grants(status, inp.now)
    if not grants:
        return []
    codex = acc.provider == "codex"
    label = acc.label(provider_prefix=inp.codex_shown, long=False)
    kinds: dict[tuple[str, str], list[dict]] = {}
    for g in grants:
        kinds.setdefault((_reset_noun(g, codex), resets.clears_phrase(g)), []).append(g)

    def order(item):
        key, gs = item
        return (resets.SCOPE_BREADTH[resets.grant_scope(gs[0])], grants.index(gs[0]))

    busy = (acc.provider, acc.number) in inp.resets_busy
    # The account's number (and provider) stay when its name gives way.
    floor = len(acc.label(provider_prefix=inp.codex_shown, long=False)) - len(
        acc.alias or shown_email(acc.email)) + 2
    if not acc.alias and is_placeholder(acc.email):
        floor = len(label)  # a placeholder name is one word: never cut
    rows: list[Row] = []
    for (noun, phrase), gs in sorted(kinds.items(), key=order):
        full = f"{label} · {noun} · {phrase}"
        by_end = sorted(gs, key=lambda g: resets.parse_ts(g.get("ends_at")) or float("inf"))
        usable = [g for g in by_end if resets.grant_unusable_reason(g, inp.now) is None]
        command = f"{manual_reset.command_for(codex)} {acc.number} --grant {by_end[0]['id']}"
        reason = ""
        action = None
        tooltip = ""
        if busy:
            reason = "in progress"
        elif acc.needs_relogin:
            reason = display.NEEDS_RELOGIN
        elif not status.get("eligible"):
            reason = "not eligible"
        elif resets.grant_scope(gs[0]) == "unknown":
            tooltip = f"qswap cannot tell what it clears; to spend it anyway: {command}"
        elif usable:
            action = Action("use-reset", acc.provider, acc.number, usable[0]["id"])
        else:
            reason = display.grant_state(resets.grant_unusable_reason(by_end[0], inp.now))
        text = fit_reset_row(label, noun, phrase, reason, floor=floor)
        whole = f"{full} · {reason}" if reason else full
        rows.append(Row(text, action=action, tooltip=tooltip or (whole if text != whole else "")))
    return rows


def fit_reset_row(label: str, noun: str, phrase: str, reason: str = "", *,
                  floor: int = LABEL_FLOOR, limit: int = ROW_LIMIT) -> str:
    """A *Use a reset* row within ``limit`` columns, as an account row fits:
    the account's name gives way first (down to ``floor``, its number kept),
    then what the reset clears goes whole; the kind and, for a greyed row,
    the reason why are kept. Only then is the end cut."""
    tail = [reason] if reason else []
    for facts in ([noun, phrase, *tail], [noun, *tail]):
        rest = " · " + " · ".join(facts)
        room = limit - len(rest)
        if len(label) <= room:
            return label + rest
        if room >= floor:
            return display.fit(label, room) + rest
    return display.fit(display.fit(label, floor) + " · " + " · ".join([noun, *tail]), limit)


def use_reset_rows(inp: MenuInput) -> list[Row]:
    """*Use a reset*'s rows: every account's kinds, Claude Code's then
    Codex's, a separator between them when both have some."""
    groups = []
    for state in (inp.claude, inp.codex):
        if state.provider == "codex" and not inp.codex_shown:
            continue
        rows = [row for acc in state.accounts for row in _account_reset_rows(acc, inp)]
        if rows:
            groups.append(rows)
    out: list[Row] = []
    for i, rows in enumerate(groups):
        if i:
            out.append(SEPARATOR)
        out.extend(rows)
    return out


def _use_reset_menu(inp: MenuInput) -> Row | None:
    """*Use a reset*, shown only while some account holds one (and resets
    are read at all)."""
    if not inp.resets.enabled:
        return None
    rows = use_reset_rows(inp)
    return Row("Use a reset", children=tuple(rows)) if rows else None


def reset_dialog(choice, *, name: str) -> tuple[str, str]:
    """``(title, message)`` before spending ``choice``'s reset: the question
    ``qswap reset`` asks, and what it prints before asking, from the same
    freshly read choice (what the reset clears, the trade when a broad one
    meets a narrow limit, a reading that could not be refreshed, that it
    cannot be undone). ``name`` is the account's long label."""
    from ai_quota_swap import manual_reset

    text = manual_reset.describe(choice, name=name)
    lines = [_ANSI.sub("", line).strip() for line in text.splitlines()]
    return manual_reset.question(choice), "\n".join(line for line in lines if line)


def use_reset(inp: MenuInput, action: Action, *, confirm, run) -> bool:
    """A click on a *Use a reset* row: hand the spend to ``run(provider,
    number, grant_id, who=…, what=…, ask=…)``
    (:class:`menubar_engines.ResetRunner`), whose worker reads the account
    again and then calls ``ask(choice)``: ``confirm(title, message)`` (the
    app's dialog, Cancel by default) with :func:`reset_dialog`'s words.
    Nothing happens while that account's claim is in flight, or for a grant
    the menu no longer shows. Returns whether a spend started."""
    provider, number, grant_id = action.provider or "claude", str(action.number), str(action.value)
    if (provider, number) in inp.resets_busy:
        return False
    acc = next((a for a in inp.provider(provider).accounts if a.number == number), None)
    usage = acc.usage() if acc is not None else None
    status = usage.get("resets") if isinstance(usage, dict) else None
    grant = next((g for g in (status or {}).get("grants") or () if g.get("id") == grant_id), None)
    if grant is None:
        return False
    name = acc.label(provider_prefix=inp.codex_shown)

    def ask(choice) -> bool:
        return bool(confirm(*reset_dialog(choice, name=name)))

    who = acc.label(provider_prefix=inp.codex_shown, long=False)
    return bool(run(
        provider, number, grant_id, who=who,
        what=f"a {_reset_noun(grant, provider == 'codex')}", ask=ask,
    ))


def _settings_menu(inp: MenuInput) -> Row:
    s = inp.settings
    rows: list[Row] = [
        Row("Show account name in menu bar", action=Action("toggle-name"), checked=s.show_account_name),
    ]
    if inp.codex_shown:
        rows.append(Row("Title follows", children=tuple(
            Row(_TITLE_FOLLOWS_LABELS[c], action=Action("title-follows", value=c), checked=s.title_follows == c)
            for c in TITLE_FOLLOWS_CHOICES
        )))
    rows.append(Row("Title percentage", children=tuple(
        Row(_TITLE_PCT_LABELS[m], action=Action("title-pct", value=m), checked=s.title_pct == m)
        for m in TITLE_PCT_CHOICES
    )))
    rows.append(Row("Show model limits in title", action=Action("toggle-scoped"), checked=s.title_scoped))
    rows.append(Row("Refresh interval", children=tuple(
        Row(_INTERVAL_LABELS[secs], action=Action("interval", value=secs), checked=s.refresh_interval == secs)
        for secs in REFRESH_CHOICES
    )))
    rows.append(SEPARATOR)
    rows.append(Row("Auto-switch Claude Code", action=Action("toggle-auto", "claude"),
                    checked=s.auto_switch_enabled))
    if inp.codex_shown:
        rows.append(Row("Auto-switch Codex", action=Action("toggle-auto", "codex"),
                        checked=s.codex_auto_switch_enabled))
        rows.append(_threshold_menu(inp, "claude", "Claude Code threshold"))
        rows.append(_threshold_menu(inp, "codex", "Codex threshold"))
    else:
        rows.append(_threshold_menu(inp, "claude", "Auto-switch threshold"))
    rows.append(_resets_menu(inp))
    rows.append(SEPARATOR)
    # Checked = the plist sits in ~/Library/LaunchAgents, where launchd scans
    # at login; read from disk on every rebuild so a CLI change shows here.
    rows.append(Row("Start at login", action=Action("toggle-login"), checked=inp.start_at_login))
    return Row("Settings", children=tuple(rows))


def build_menu(inp: MenuInput) -> Menu:
    """The complete menu for one moment: title, sections, rows.

    With no Codex account managed the menu is Claude Code's alone, as it has
    always been. Once Codex manages one, each provider gets a section under a
    heading, the account submenus name the provider, and the settings gain
    Codex's toggle, its threshold and what the title follows.
    """
    health = health_row(inp)
    sections: list[Section] = [Section("health", (health,))]
    claude_n, codex_n = inp.claude.count, inp.codex.count
    if not claude_n and not codex_n and not inp.claude.error and not inp.codex.error:
        sections.append(Section("accounts", tuple(_first_run_rows(inp))))
    elif inp.codex_shown:
        sections.append(Section("claude", tuple(_provider_rows(inp, inp.claude)),
                                heading=display.provider_heading("claude", both=True)))
        sections.append(Section("codex", tuple(_provider_rows(inp, inp.codex)),
                                heading=display.provider_heading("codex", both=True)))
    else:
        sections.append(Section("claude", tuple(_provider_rows(inp, inp.claude))))
    if inp.cost is not None:
        sections.append(Section("cost", (Row(inp.cost.text()),)))
    if inp.claude.accounts:
        sections.append(Section("switch", tuple(_strategy_rows(inp))))
    refresh = "Refresh current credentials" + (" (Claude Code)" if inp.codex_shown else "")
    use_reset_menu = _use_reset_menu(inp)
    sections.append(Section("manage", tuple(row for row in (
        _add_menu(inp),
        _accounts_submenu(inp, "Disable / enable account", "toggle-disabled"),
        _accounts_submenu(inp, "Remove account", "remove"),
        use_reset_menu,
        Row(refresh, action=Action("refresh-creds", "claude")),
        _history_menu(inp),
    ) if row is not None)))
    sections.append(Section("app", (
        _settings_menu(inp),
        Row("Refresh now", action=Action("refresh-now")),
        Row("Quit", action=Action("quit")),
    )))
    return Menu(menu_title(inp, attention=health.attention), tuple(sections))


# -- dialogs -------------------------------------------------------------------------


def remove_question(provider: str, label: str) -> str:
    """The question ``qswap remove`` / ``qswap codex remove`` ask."""
    return display.remove_question(provider, label)


def remove_dialog(provider: str, label: str) -> tuple[str, str]:
    """``(title, message)`` of the remove confirmation. Codex's adds what the
    command line says after it: only qswap's copy goes, and why not to run
    ``codex logout``."""
    message = remove_question(provider, label)
    if provider == "codex":
        message += (
            "\n\nOnly qswap's saved copy is deleted; the login itself is untouched. "
            "Do not run `codex logout` to clean up: it signs the login out at "
            "OpenAI, and every saved copy of it stops working."
        )
    return "Remove account", message


def relogin_confirmation(refusal: str) -> str:
    """The command line's refusal to switch to an account that needs
    re-login, for a dialog whose button switches anyway: the explanation and
    the way out, without the ``--anyway`` line the button stands for."""
    lines = [line for line in refusal.splitlines() if not line.startswith("To switch to it anyway")]
    text = "\n".join(lines).strip()
    return text.replace("; not switched.", ".", 1)


#: The button that goes ahead with a switch to an API-key account while
#: Claude Code sessions are running (``--log-out-sessions``).
API_KEY_SWITCH_BUTTON = display.API_KEY_SWITCH_BUTTON
#: The question's title when the key helper cannot be added.
KEY_HELPER_QUESTION_TITLE = display.KEY_HELPER_QUESTION_TITLE


def api_key_switch_question(error: BaseException | None) -> tuple[str, str, str] | None:
    """What a manual switch asks before going ahead, or None when it does not.

    A switch from the menu to an API-key account while Claude Code sessions
    are running is refused by the switcher exactly as on the command line;
    here the same explanation becomes a question: ``(title, message, button)``
    where ``button`` (:data:`API_KEY_SWITCH_BUTTON`, or "Switch anyway" when
    the active account is a key too and no one is logged out) goes ahead and
    Cancel is the default. The message drops the "To switch anyway" lines the button
    stands for. Anything else is not a question (None), and neither is a
    refusal with no saved-key way (``error.log_out_sessions`` False)."""
    if not isinstance(error, SessionsRunningError):
        return None
    from_key = bool(getattr(error, "from_key", False))
    if isinstance(error, KeyHelperUnavailableError):
        if not getattr(error, "log_out_sessions", True):
            # Another apiKeyHelper, or a credential in the settings' env
            # block, defeats a saved key too: nothing to ask, the refusal
            # (an alert) names the one way forward (decision B6).
            return None
        # The key helper could not be added (a file that cannot be parsed
        # or written): the saved-key way is what the button does.
        return (
            KEY_HELPER_QUESTION_TITLE,
            display.api_key_question(str(error)),
            display.api_key_question_button(from_key=from_key),
        )
    return (
        display.api_key_question_title(error.sessions, from_key=from_key),
        display.api_key_question(str(error)),
        display.api_key_question_button(from_key=from_key),
    )


def codex_toggle_message(threshold: float | None, *, restart_daemon: bool = True) -> str:
    """The confirmation before Codex's auto-switch turns on."""
    from ai_quota_swap.autoswitch import pct_label

    line = f"{pct_label(threshold)}%" if threshold is not None else "its switch line"
    text = (
        f"The menu bar switches Codex to another account when the active one "
        f"reaches {line} (codex.threshold)."
    )
    if restart_daemon:
        text += (
            " A Codex switch restarts Codex's background server, which "
            "interrupts work in progress."
        )
    else:
        text += (
            " codex.restartDaemon is off, so after a switch Codex's background "
            "server keeps the old account until you restart it."
        )
    return text


TOKEN_DIALOG_TITLE = "Add an account from a token"
TOKEN_DIALOG_MESSAGE = (
    "Paste a Claude Code setup-token (sk-ant-oat01-…) or a Console API key "
    "(sk-ant-api03-…). The kind is detected. The email is optional: without "
    "one the account is named setup-token-N@token.local or "
    "api-key-N@token.local."
)


def parse_token_entry(token: str, email: str = "") -> tuple[str, str | None]:
    """The add-from-token dialog's fields, checked as ``qswap add-token``
    checks them: a token is required, the email is optional. Pasting both
    into the token field (``TOKEN EMAIL``) works too. Raises ``ValueError``
    with the message to show."""
    token = (token or "").strip()
    email = (email or "").strip()
    if not email and len(token.split()) == 2:
        token, email = token.split()
    if not token:
        raise ValueError("Token cannot be empty")
    if len(token.split()) > 1:
        raise ValueError("Paste the token alone; put the email in its own field")
    if email and (len(email.split()) > 1 or "@" not in email):
        raise ValueError(f"Invalid email format: {email}")
    return token, email or None


def codex_login_help() -> str:
    """What "Log in another Codex account" shows: the command to run, since
    the menu bar cannot host Codex's sign-in."""
    return (
        f"{display.ANOTHER_ACCOUNT_STEPS['codex']}\n\n"
        "Run it in a terminal; the menu bar cannot sign in for you. The new "
        "account shows up here once it is saved."
    )


def error_text(exc: BaseException) -> str:
    """What an alert says for an error: a provider error in the command
    line's words; anything else with its type, and where the details are."""
    if isinstance(exc, ProviderError):
        return str(exc) or type(exc).__name__
    return (
        f"Something went wrong: {type(exc).__name__}: {exc}. "
        "The details are in the log (Switch history → Open full log…)."
    )


def guarded(fn, *, alert, log=None) -> bool:
    """Run a menu handler so that no error ever reaches ``rumps`` (which
    would end the app): a failure becomes an alert with :func:`error_text`,
    and anything but a provider error is also logged with its traceback.
    True when ``fn`` finished."""
    try:
        fn()
        return True
    except Exception as e:
        if log is not None and not isinstance(e, ProviderError):
            try:
                log(e)
            except Exception:
                pass
        try:
            alert(error_text(e))
        except Exception:
            _logger.debug("could not show an error", exc_info=True)
        return False


#: Every :attr:`Action.kind` a menu can carry; the app has a ``_do_<kind>``
#: handler (dashes as underscores) for each.
ACTION_KINDS = frozenset({
    "switch", "rotate", "add-current", "add-token", "codex-login-help", "remove",
    "toggle-disabled", "refresh-creds", "open-log", "restart-engines", "toggle-name",
    "title-follows", "title-pct", "toggle-scoped", "interval", "toggle-auto",
    "threshold", "toggle-login", "refresh-now", "quit",
    "resets-auto", "toggle-weekly-for-model", "use-reset",
})


def manual_switch_followup(provider: str, result: Mapping | None, *, backend: str | None = None) -> str:
    """What follows "Switched …" after a switch from the menu: when the new
    login takes effect, as the command line says it."""
    if provider == "codex":
        background = (result or {}).get("backgroundServer")
        if isinstance(background, Mapping) and background.get("restarted"):
            return (
                f"Restarted Codex's background server (pid {background.get('pid')}); "
                "new sessions use this account."
            )
        return "New codex sessions use this account."
    followup = (result or {}).get("followup") if isinstance(result, Mapping) else None
    if followup:
        # A switch with an API-key account on either side says what the
        # running sessions do; the lines below hold for subscriptions only.
        return " ".join(str(line) for line in followup)
    if backend == "file":
        return "Claude Code uses the new account from its next message."
    return "Claude Code picks up the new account within 30 seconds; restart it to apply now."


def wanted_engines(settings: MenuBarSettings, *, codex_accounts: int) -> dict[str, bool]:
    """Which providers' engines the menu bar hosts: each toggle that is on,
    and Codex's only while Codex manages an account."""
    return {
        "claude": settings.auto_switch_enabled,
        "codex": settings.codex_auto_switch_enabled and codex_accounts > 0,
    }


def engine_state(
    provider: str,
    *,
    enabled: bool,
    running: bool,
    failure: str | None = None,
    heartbeat: Mapping | None = None,
    now: float | None = None,
    pid: int | None = None,
) -> EngineState:
    """One provider's :class:`EngineState`: off, running, stopped, or, with
    the toggle off, running in another process (its heartbeat)."""
    if running:
        return EngineState(provider, "running")
    if enabled:
        return EngineState(provider, "stopped", failure=failure)
    if heartbeat is not None and now is not None:
        from ai_quota_swap.autoswitch import engine_running

        if engine_running(dict(heartbeat), now) and heartbeat.get("pid") != (os.getpid() if pid is None else pid):
            return EngineState(provider, "elsewhere", host=str(heartbeat.get("host") or "qswap auto"))
    return EngineState(provider, "off")


# -- the app ----------------------------------------------------------------------


# macOS 26 stopped drawing status items for processes launched through an
# exec trampoline, and a CPython *framework* build is exactly that: its
# ``bin/python3.x`` is a stub that posix_spawns into ``Python.app``
# (Mac/Tools/pythonw.c). Homebrew and python.org ship framework builds;
# uv-managed and most other interpreters do not.
#
# Measured on macOS 26.6.2 with rumps 0.4.0, same bare rumps app throughout:
#
#   Homebrew 3.14.6   sys._framework 'Python'   no status item
#   Homebrew 3.10.21  sys._framework 'Python'   no status item
#   uv 3.14.7         sys._framework ''         status item drawn
#   uv 3.13.15        sys._framework ''         status item drawn
#
# The interpreter version is not the variable; the build is.


MIN_AFFECTED_MACOS = 26


def _macos_major(mac_ver: str | None = None) -> int | None:
    """Major version of the running macOS, or None if it cannot be read."""
    raw = platform.mac_ver()[0] if mac_ver is None else mac_ver
    head = raw.split(".")[0]
    return int(head) if head.isdigit() else None


def framework_build_warning(
    framework=None, install_method=None, mac_ver: str | None = None
) -> str | None:
    """Text to show when this interpreter cannot draw a status item.

    Returns None wherever the menu bar is known to work. Both halves of the
    condition matter: the evidence is a framework build *on macOS 26*, and
    framework builds draw fine on earlier releases — gating on the build alone
    would nag every Homebrew user on macOS 14 or 15, on every launch and in
    the service log on every restart.

    Nothing here can fix the incompatibility. The point is that it fails
    silently, with a healthy process and empty logs, so it is worth one line.
    """
    fw = getattr(sys, "_framework", "") if framework is None else framework
    if not fw:
        return None

    major = _macos_major(mac_ver)
    if major is None or major < MIN_AFFECTED_MACOS:
        return None

    from ai_quota_swap.update_check import MENUBAR_INSTALL_SPEC

    if install_method is None:
        from ai_quota_swap.update_check import _detect_install_method

        install_method = _detect_install_method()

    if install_method == "uv":
        remedy = (
            f"  uv tool install --managed-python --force {MENUBAR_INSTALL_SPEC}"
        )
    elif install_method == "pipx":
        remedy = (
            "  Reinstall against a non-framework interpreter, e.g. one from "
            "`uv python install 3.13`:\n"
            f"  pipx install --force --python <that python> {MENUBAR_INSTALL_SPEC}"
        )
    else:
        remedy = (
            "  Reinstall against a non-framework interpreter "
            "(uv-managed ones are; Homebrew and python.org are not)."
        )

    return (
        "This is a framework build of Python, which on macOS 26 has been "
        "observed not to draw the menu bar icon: the process runs and logs "
        "nothing, but no status item appears.\n" + remedy
    )


def run(switcher) -> int:
    """Entry point for ``qswap menubar``. Blocks until the user quits."""
    try:
        import rumps  # lazy: optional dependency, imported only when launching
        import AppKit  # ships with rumps (pyobjc-framework-Cocoa), never fails alone
    except ImportError as e:
        # This module is import-safe without rumps by design, so the CLI's
        # guard around ``from ai_quota_swap.menubar import run`` can never see a
        # missing extra — the failure lands here at call time. Raise the
        # error type the CLI already renders cleanly instead of a traceback.
        from ai_quota_swap.update_check import MENUBAR_INSTALL_SPEC

        raise ClaudeSwitchError(
            "Menu bar mode requires 'rumps'. "
            f"Install with: pip install {MENUBAR_INSTALL_SPEC}"
        ) from e

    from ai_quota_swap import app_bundle

    # Hop into the wrapper .app before anything AppKit-side is set up, so the
    # status item's owner has a bundle identifier that menu bar managers can
    # key on. Returns only when staying in this interpreter (already bundled,
    # opted out, framework build, or the bundle could not be built).
    app_bundle.maybe_reexec(switcher.backup_dir)
    if not app_bundle.in_bundle():
        ensure_notification_identity()
    try:
        # Pid and build of this worker, so a later `qswap menubar` or
        # `qswap upgrade` can tell an instance of an old build from the
        # installed one. Never worth failing the start over.
        launch_agent.write_worker_stamp(switcher.backup_dir)
    except OSError:
        switcher._logger.debug("could not write the menu bar worker stamp", exc_info=True)
    _warn = framework_build_warning()
    if _warn:
        # stderr, not stdout: launchd sends stdout to the .log file where it
        # would sit in a block buffer for the life of the process, and the
        # install output points the user at the .err file anyway. stderr stays
        # line-buffered even when redirected, so it lands immediately.
        warning(_warn, file=sys.stderr)

    # rumps never sets an activation policy, so under a framework Python the
    # process launches as a regular app and parks a "Python" icon in the Dock
    # for as long as the menu bar runs. Accessory keeps the status item and
    # dialog windows but stays out of the Dock and the Cmd-Tab switcher.
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )

    from ai_quota_swap.menubar_app import run_app

    return run_app(switcher, rumps, AppKit)
