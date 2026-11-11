"""Auto-switch engine: poll usage, switch accounts before they hit rate limits.

``AutoSwitchEngine`` is UI-agnostic — no printing, no argparse, no TUI
imports. It composes a :class:`ClaudeAccountSwitcher`, evaluates a threshold
policy each :meth:`~AutoSwitchEngine.tick`, and reports everything through
typed events handed to an ``on_event`` callback; the CLI renders them as
human lines or JSONL, and any future frontend (TUI dashboard, menubar) can
consume the same stream.

Policy in one paragraph: when any of the active account's usage windows (5h,
7d, and the per-model windows named by ``settings.model``) reaches its own
switch line — that account's **effective threshold** (its per-account
override where one is set, otherwise ``settings.threshold``), or
``settings.weekly_threshold`` for the windows that reset weekly once that is
set — switch to the candidate with the most headroom measured against its
own lines (see ``lines.py``; with one line it is plain ``100 - max pct``) —
proactively, so the old account is
still valid while a running Claude Code picks the new one up (this is what
makes the macOS ~30s Keychain cache latency harmless). Candidates must sit
below every one of their lines and beat the active account by
``hysteresis_pct`` points so two accounts hovering at a line never
ping-pong, and a ``cooldown_seconds`` floor bounds the switch rate (bypassed
only when the active account is hard at its limit). Physical questions —
exhausted, at its limit, when does quota return — read the raw percentages. Before
activation the target's token is *freshened* (refreshed if it expires within
10 minutes — twice Claude Code's refresh buffer, so a running Claude Code's
under-lock re-read sees a fresh token and aborts its own refresh); a target
whose refresh token is dead gets quarantined instead of activated. When the
active account's own usage becomes unreadable for ``unhealthy_ticks``
consecutive ticks, the engine fails over to any healthy candidate.

Cooldown and quarantine persist in ``<switcher.backup_dir>/autoswitch_state.json``
(so cron-driven ``qswap auto --once`` ticks behave across processes), mutated
read-modify-write under a dedicated file lock. Each provider's switcher has its
own ``backup_dir`` (Codex: ``<data root>/codex``), so cooldown, quarantine,
health counters and the daily reset brake are per provider.
"""

from __future__ import annotations

import enum
import json
import logging
import math
import random
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from ai_quota_swap import oauth, poll_policy
from ai_quota_swap.exceptions import (
    CredentialFormatError,
    ProviderError,
    RetryableSwitchError,
    SessionsRunningError,
)
from ai_quota_swap.json_output import (
    SCHEMA_VERSION,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from ai_quota_swap.lines import BindingWindow, binding_window, window_line
from ai_quota_swap.locking import FileLock
from ai_quota_swap.models import AccountPolicy
from ai_quota_swap.poll_policy import (
    ESCALATION_MARGIN_PCT,
    RESET_SLACK_S,
    binding_pct,
)
from ai_quota_swap.settings import AutoSwitchSettings, atomic_write_json, parse_model_names
from ai_quota_swap.switcher import ClaudeAccountSwitcher
from ai_quota_swap.usage_store import STALE_OK_S, due_candidate, plan_oversleeps_interval

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

_logger = logging.getLogger("ai-quota-swap")

# Systemic freshen refusals, MOST ACTIONABLE FIRST. Deterministic conditions
# that every candidate hits identically, so the tick reports one of them —
# and the order decides which, because reporting the wrong one is how a cause
# needing a human hides behind one that clears itself. store-unmirrored and
# invalid_client stay until somebody unsets an env var or fixes a client
# registration, and stash-unreadable until they unlock a keychain, fix a mode,
# or purge the row; consume-busy is gone by the next pass. stash-unreadable is
# the one that is per-SLOT rather than global, which costs nothing here: this
# message is only ever emitted when NO candidate freshened, so naming the real
# cause of the only slot that had one beats "(network?)".
_SYSTEMIC_MESSAGES = {
    "store-unmirrored": "CLAUDE_SECURESTORAGE_CONFIG_DIR is set — unset it or "
                        "run qswap from a normal shell",
    "invalid_client": "qswap's OAuth client was rejected — systemic, not this "
                      "account",
    "stash-unreadable": "a stashed successor is unreadable — unlock the "
                        "keychain or fix the file, then retry; "
                        "`qswap unclaimed` inspects it",
    "consume-busy": "another qswap surface holds the slot — retries next pass",
}
# Insertion order IS the precedence order, so the remedy and its rank cannot
# drift apart.
_SYSTEMIC_STATUSES = tuple(_SYSTEMIC_MESSAGES)

# Freshen targets whose access token expires within this window: twice Claude
# Code's own 5-minute refresh buffer, so its post-lock "abort refresh if not
# expired" re-read holds with margin after our swap.
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# Sleep caps around a known quota reset (RESET_SLACK_S lives in poll_policy
# with the rest of the cadence numbers). Recheck at the exhausted-account poll
# cadence: providers can grant quota before the previously reported reset, and
# a long engine sleep must not suppress the fetch that discovers it.
MAX_SLEEP_S = poll_policy.EXHAUSTED_INTERVAL_S
NO_RESET_FALLBACK_S = 300.0

# Idle-hold cap (elapsed, not ticks — the hold itself slows the cadence to
# NO_RESET_FALLBACK_S): an owned-and-expired token normally means Claude Code
# is idle and will self-heal on next use, but a *dead* refresh token with an
# active user would look identical forever, so after this long the engine
# falls back to normal unhealthy counting.
IDLE_HOLD_MAX_S = 30 * 60.0

# Margin on the reset axis for the no-return bar's release
# (`_left_account_recovered`): the account we left counts as recovered only
# once its binding reset has moved at least this much nearer. Five minutes is
# comfortably longer than one poll cycle, so measurement jitter on a reset
# timestamp cannot release the bar.
RECOVERY_HYSTERESIS_S = 300.0

# How near a peer's binding reset must be for the failover release in
# `_left_account_recovered` to trust it when the active's own reset reads
# unknown. A 5h window can be up to 5h from resetting, so 4h keeps most of a
# five-hour cycle inside it.
RECOVERY_HORIZON_S = 4 * 3600.0

# Anti-flap margin when every account is over its own switch lines, as a
# RATIO rather than percentage points: strictly-more is no margin at all — one
# point moves the engine, the target burns it back, and it ping-pongs. The cap
# below lets the reverse margin reopen after ordinary burn, so the no-return
# bar (`_no_return_account`), not this ratio, keeps the move one-way.
#
# CAPPED at SPENT_HEADROOM_PCT points (`_all_above_margin_met`). Uncapped, the
# ratio's margin grows with the active's headroom, so with the threshold at 80
# an active holding 10 points refused a peer holding 19: the engine stayed at
# 90% while an 81% account sat idle, and both lie above the line, where the
# only question left is which account has the most room. Below the cap the
# ratio is the smaller margin and still decides.
HORIZON_HEADROOM_RATIO = 2.0

# A headroom edge smaller than this is noise: a point is under ten minutes of
# work, less than two poll intervals. It caps the anti-flap margin above, and
# the no-return bar's release asks the account we left to have gained at least
# this much.
SPENT_HEADROOM_PCT = 3.0


def _all_above_margin_met(candidate_headroom: float, active_headroom: float) -> bool:
    """Does a candidate beat the active by enough to move, every account over
    its own switch lines?

    The margin is ``HORIZON_HEADROOM_RATIO`` times the active's headroom, capped
    at ``SPENT_HEADROOM_PCT`` points. Either alone fails a measured case:

        no margin       active 5 pts, peer 6: moves, burns the point back,
                        and moves again (four switches in 35 minutes)
        ratio alone     active 10 pts, peer 19: holds a 90% account while an
                        81% one sits idle

    Both sides are RAW headroom (``100 - max pct``), never line headroom: past
    its lines an account's line headroom is its room rescaled by how much room
    its line reserved, and a ratio against a line every account has already
    crossed measures the policy, not the quota (the reason the ratio and
    ``SPENT_HEADROOM_PCT`` stay raw everywhere else).

    The cap leaves the move one-way: the return leg is the no-return bar's call
    (`_no_return_account`), and burn on the new active opens that bar only once
    the active is down to a sliver (`_left_account_recovered`).

    The no-return bar's release (`_no_return_account`) reuses this margin in
    every regime, so a recovered account that clears it is never still barred.
    """
    required = min(
        active_headroom * HORIZON_HEADROOM_RATIO,
        active_headroom + SPENT_HEADROOM_PCT,
    )
    return candidate_headroom >= required


# Adaptive scheduling: the baseline request volume is O(1) per tick — the
# active account plus ONE due candidate (stalest data first) — instead of
# every account in parallel, and the per-account cadence itself (movement,
# threshold distance, urgent mode, 429 recovery) lives in poll_policy, is
# persisted in the usage store by whichever collector fetched, and is shared
# by every surface. The engine escalates to a full candidate refresh only
# when a switch could actually be near: one of the active account's windows
# within ESCALATION_MARGIN_PCT of its own switch line, or active usage unknown
# (failover needs fresh candidate data). The consume-first trigger can fire outside
# that escalation band; there it decides provisionally on the stored
# snapshot and escalates at commit time, when a switch would actually fire
# (the two-phase commit in _tick_inner).


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def pct_label(value: float) -> str:
    """A percentage for display, as configured: 85.555555 stays itself
    (never a rounded "85.5556") and 99.9 never becomes a lying "100" the
    way ``.0f`` renders it. Ten significant digits still absorb IEEE float
    noise (~15th digit) in computed utilizations (100.0 - headroom).
    Displayed comparisons must format BOTH sides with this helper — mixing
    formatters can render an impossible "85.5556% < 85.555555%"."""
    return f"{value:.10g}"


def _below_line_detail(
    binding: BindingWindow | None, active_headroom: float, threshold: float
) -> str:
    """The below-threshold hold, quoting the comparison the engine made: the
    binding window's own pct against its own line ("7d 93% < 97%"), so the
    detail cannot read as a contradiction once the lines differ. The
    account-wide form is only for an account with no readable window, which
    a readable headroom rules out."""
    if binding is None:
        return f"{pct_label(100.0 - active_headroom)}% < {pct_label(threshold)}%"
    return f"{binding.label} {pct_label(binding.pct)}% < {pct_label(binding.line)}%"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _human_only(default=None, *, factory=None):
    """A field for human text only: never in ``to_json()``, never compared
    (an event built by hand still equals the engine's), never in ``repr``."""
    if factory is not None:
        return field(default_factory=factory, kw_only=True, compare=False, repr=False)
    return field(default=default, kw_only=True, compare=False, repr=False)


@dataclass(frozen=True)
class AccountView:
    """How the engine saw one account on one tick, for human text only
    (``autolog``); never part of any ``to_json()``.

    ``state`` is one of:

    - ``"ok"``: a reading the engine decides on (``windows``; ``age_s`` when
      it is older than ``STALE_OK_S`` but still trusted);
    - ``"stale"``: only a reading too old to decide on (``windows``,
      ``age_s``, ``error`` the fetch error since);
    - ``"unavailable"``: no reading; ``error`` is the last fetch error;
    - ``"not-read"``: never read;
    - ``"needs-relogin"``: set aside until its owner logs in again
      (``set_aside`` is the engine's reason) or its login was refused;
    - ``"sentinel"``: another provider state (``sentinel``);
    - ``"api-key"``: an API-key account (``used``: the engine may land on it
      as a last resort);
    - ``"disabled"``: held out of rotation by its owner;
    - ``"no-login"``: no saved login to switch to.

    ``line`` / ``weekly_line`` are its switch lines this tick.
    """

    number: str
    email: str = ""
    alias: str = ""
    state: str = "ok"
    windows: tuple[tuple[str, float], ...] = ()
    line: float | None = None
    weekly_line: float | None = None
    age_s: float | None = None
    error: str | None = None
    set_aside: str | None = None
    sentinel: str | None = None
    used: bool = False


@dataclass(frozen=True)
class EventContext:
    """What an event's human text needs beyond its JSON: the provider, the
    accounts' aliases, and how the engine saw every account on the tick that
    emitted it. Attached by the engine to every event (``context``); never
    part of ``to_json()``."""

    provider: str = "claude"
    names: Mapping[str, str] = field(default_factory=dict)  # account number -> alias
    accounts: tuple[AccountView, ...] = ()
    active: str | None = None  # the active account's number on that tick


@dataclass(frozen=True)
class AutoSwitchEvent:
    """Base event. ``to_json()`` payloads are additive: consumers must ignore
    unknown ``event`` kinds and unknown fields.

    ``human()`` is one self-contained sentence (the menu bar shows it as a
    notification, the dashboard as a log line); its words live in
    :mod:`ai_quota_swap.autolog`. ``context`` and every other field marked
    human-only feed that text and are never serialized."""

    kind: ClassVar[str] = "event"
    ts: str = field(default_factory=_now_iso, kw_only=True)
    context: EventContext | None = _human_only()

    def _fields(self) -> dict:
        return {}

    def to_json(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "event": self.kind,
            "ts": self.ts,
            **self._fields(),
        }

    def human(self) -> str:  # pragma: no cover - overridden
        return self.kind


@dataclass(frozen=True)
class PollEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "poll"
    active: dict | None  # account_ref shape, or None
    headroom: dict[str, float | None]  # account number → headroom pct (None=unknown)
    threshold: float
    # account number → last fetch-error cause ("http-429", "timeout", ...) for
    # accounts whose usage is unknown this tick. Additive field.
    fetch_errors: dict[str, str] = field(default_factory=dict)
    # account number → ordered window label → utilization pct ("5h", "7d",
    # then scoped model display names). Additive field: the binding pct alone
    # (e.g. "89%") hides which window binds — #115 was reported off that
    # ambiguity.
    windows: dict[str, dict[str, float]] = field(default_factory=dict)
    # The switch line of the weekly windows (7d and per-model) when one is
    # configured; ``threshold`` stays the 5h window's. Additive field, emitted
    # only when set. Every pct above stays RAW: the lines are applied where
    # the engine compares, never to the numbers it logs.
    weekly_threshold: float | None = None

    def _fields(self) -> dict:
        fields = {
            "active": self.active,
            "headroomPct": self.headroom,
            "threshold": self.threshold,
        }
        if self.fetch_errors:
            fields["fetchErrors"] = self.fetch_errors
        if self.windows:
            fields["windowsPct"] = self.windows
        if self.weekly_threshold is not None:
            fields["weeklyThreshold"] = self.weekly_threshold
        return fields

    def human(self) -> str:
        """``#1 personal at 5h 86% (switch at 90%); others: #2 work at 5h
        12% · 7d 21%; …``. The active account's binding window is named and
        quoted against ITS line (the engine's trigger rule): a bare pct hid
        which window bound (#115), and "93% (switch at 85%)" reads as a
        contradiction once the weekly window has a line of its own."""
        from ai_quota_swap import autolog

        return autolog.poll_sentence(self)


@dataclass(frozen=True)
class SwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "switch"
    trigger: str  # "proactive" | "at-limit" | "failover" | "consume-first" | "priority"
    from_ref: dict | None
    to_ref: dict | None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    # Human-only: what the reason words quote. ``binding`` is the active
    # account's binding window when the engine decided ("5h reached 91%");
    # ``over_budget`` an API-key account at its budget; ``priority_up``
    # whether the target outranks the active (False: same tier, sooner
    # weekly reset).
    binding: BindingWindow | None = _human_only()
    over_budget: bool = _human_only(False)
    priority_up: bool | None = _human_only()

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "from": self.from_ref,
            "to": self.to_ref,
            "warnings": self.warnings,
            "dryRun": self.dry_run,
        }

    def human(self) -> str:
        """``Switched #1 personal → #2 work · 5h reached 91%``."""
        from ai_quota_swap import autolog

        return autolog.switch_sentence(self)


@dataclass(frozen=True)
class NoSwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "no-switch"
    reason: str
    detail: str = ""
    # Human-only facts behind ``reason``: the active account's binding window
    # (below-threshold), the account that could not be re-read (stale-usage),
    # and the cooldown still to run (cooldown). The accounts behind a blocked
    # tick are in ``context.accounts``.
    binding: BindingWindow | None = _human_only()
    account: str | None = _human_only()
    wait_s: float | None = _human_only()
    # api-key-sessions: how many Claude Code sessions ran on the shared login.
    sessions: int | None = _human_only()

    def _fields(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}

    def human(self) -> str:
        """Why the engine stayed, in words; a blocked tick names each other
        account's reason and the next step."""
        from ai_quota_swap import autolog

        return autolog.no_switch_sentence(self)


def _api_key_sessions_event(number: str, sessions: int) -> NoSwitchEvent:
    """The engine stayed rather than land on the API-key account ``number``
    while ``sessions`` Claude Code sessions run on the shared login."""
    return NoSwitchEvent(
        reason="api-key-sessions",
        detail=(
            f"account {number} is an API-key account and {sessions} running "
            "sessions could not follow"
        ),
        account=str(number),
        sessions=sessions,
    )


@dataclass(frozen=True)
class QuarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-quarantined"
    number: str
    email: str
    reason: str
    # The provider's own recovery step for the human line (not in JSON).
    # Empty: Claude Code's, logging in to it again and re-adding it.
    recover: str = ""

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        """``#4 side needs re-login: its saved login was refused. Log in to
        it in Claude Code (/login), then run: qswap add``."""
        from ai_quota_swap import autolog

        return autolog.quarantine_sentence(self)


@dataclass(frozen=True)
class UnquarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-unquarantined"
    number: str
    email: str
    reason: str = "credentials-replaced"

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        """``#4 side is back in rotation (login replaced)``."""
        from ai_quota_swap import autolog

        return autolog.unquarantine_sentence(self)


@dataclass(frozen=True)
class AllExhaustedEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "all-exhausted"
    earliest_reset_at: str | None

    def _fields(self) -> dict:
        return {"earliestResetAt": self.earliest_reset_at}

    def human(self) -> str:
        """``All accounts are at their limit; the first resets 15:32 · in 1h 12m``."""
        from ai_quota_swap import autolog

        return autolog.exhausted_sentence(self)


@dataclass(frozen=True)
class SleepEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "sleep"
    seconds: float
    until: str

    def _fields(self) -> dict:
        return {"seconds": round(self.seconds, 1), "until": self.until}

    def human(self) -> str:
        """``Sleeping 12m, until 14:32``."""
        from ai_quota_swap import autolog

        return autolog.sleep_sentence(self)


@dataclass(frozen=True)
class ErrorEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "error"
    message: str
    transient: bool = True

    def _fields(self) -> dict:
        return {"message": self.message, "transient": self.transient}

    def human(self) -> str:
        return f"Error: {self.message}" + (" (will retry)" if self.transient else "")


@dataclass(frozen=True)
class ConfigWarningEvent(AutoSwitchEvent):
    """A configuration value is syntactically fine but provably inert (e.g.
    an ``autoswitch.model`` name no account reports). Not an error: the
    engine keeps running on the axes that do exist."""

    kind: ClassVar[str] = "config-warning"
    message: str

    def _fields(self) -> dict:
        return {"message": self.message}

    def human(self) -> str:
        return f"Warning: {self.message}"


@dataclass(frozen=True)
class BudgetWarningEvent(AutoSwitchEvent):
    """An API-key account reached ``api_spend.BUDGET_WARN_FRACTION`` of its
    monthly budget. Emitted once per account per UTC month. Additive kind."""

    kind: ClassVar[str] = "budget-warning"
    account: dict  # account_ref shape
    used: float
    limit: float
    month: str  # "YYYY-MM", UTC
    source: str | None = None  # "admin-api" | "ledger"

    @property
    def pct(self) -> float:
        return self.used / self.limit * 100.0 if self.limit else 0.0

    def _fields(self) -> dict:
        return {
            "account": self.account,
            "usedUsd": round(self.used, 2),
            "budgetUsd": self.limit,
            "pct": round(self.pct, 1),
            "month": self.month,
            "source": self.source,
        }

    def human(self) -> str:
        """``#5 ci has spent $410.00 of its $500.00 monthly budget (82%)``."""
        from ai_quota_swap import autolog

        return autolog.budget_sentence(self)


@dataclass(frozen=True)
class ResetEvent(AutoSwitchEvent):
    """A usage reset the engine spent (or, dry-run, would spend).

    ``plan`` is :meth:`ai_quota_swap.resets.ResetPlan.to_json`; ``result`` the
    server's answer (:meth:`ai_quota_swap.resets.ClaimResult.to_json`), None in
    dry-run.
    """

    kind: ClassVar[str] = "limit-reset"
    account: dict
    plan: dict
    result: dict | None = None
    dry_run: bool = False

    def _fields(self) -> dict:
        return {
            "account": self.account,
            "plan": self.plan,
            "scope": self.plan.get("scope"),
            "result": self.result,
            "dryRun": self.dry_run,
        }

    @property
    def used(self) -> bool:
        return bool(self.result and self.result.get("result") == "reset")

    def human(self) -> str:
        """``Used a Fable reset on #1 personal · Fable reached 100%, 3d
        before it resets on its own · 1 left on this grant``."""
        from ai_quota_swap import autolog

        return autolog.reset_sentence(self)


# A claim's record keeps the account out of the planner until a reading taken
# after it arrives AND this long has passed: no second spend on the pre-claim
# numbers, and no tight retry loop when the server keeps refusing.
RESET_SUCCESS_GAP_S = 30 * 60.0
RESET_RETRY_GAP_S = 10 * 60.0
# The server said no (cooldown, already used, not at a limit, ineligible):
# its fresh reading usually explains why, but never ask again sooner.
RESET_REFUSED_GAP_S = 60 * 60.0
# A planned reset is re-read first; a reading older than this is no basis.
RESET_FRESH_READ_S = 120.0
# Results that may have spent a reset (or whose outcome is unknown): they
# count toward the daily brake. auth_error / rate_limited / the decided
# refusals cannot have spent one.
_BRAKE_RESULTS = frozenset({"reset", "already_used", "unavailable", "error", "pending"})
# Runaway brake across the fleet: banked resets cannot be bought back.
MAX_AUTO_RESETS_PER_DAY = 3


def _state_lock_for(state_path: Path) -> FileLock:
    return FileLock(state_path.parent / ".autoswitch_state.lock")


def _read_state_file(state_path: Path) -> dict:
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _mutate_state_file(state_path: Path, mutator: Callable[[dict], None]) -> dict:
    with _state_lock_for(state_path):
        state = _read_state_file(state_path)
        state["schemaVersion"] = STATE_SCHEMA_VERSION
        mutator(state)
        atomic_write_json(state_path, state)
        return state


# -- heartbeat ------------------------------------------------------------------
#
# Where a running engine says it is alive, for `qswap status` and `qswap
# doctor`. A file of its own, never the state file: it is presence, not a
# decision input, so even a dry-run engine writes it, and nothing reads it to
# decide anything. Written after every tick (best-effort; a failed write never
# touches the tick).

HEARTBEAT_FILENAME = "autoswitch_heartbeat.json"
# How late a loop engine may be past its announced next tick and still count
# as running (a slow tick, a laptop waking up).
HEARTBEAT_GRACE_S = 120.0


def write_heartbeat(
    backup_dir: Path,
    *,
    mode: str,
    dry_run: bool,
    last_tick_at: float,
    next_tick_at: float | None,
    started_at: float | None = None,
    host: str = "qswap auto",
    pid: int | None = None,
) -> None:
    """Record that an engine just ticked. ``mode`` is ``"loop"`` or
    ``"once"``; ``next_tick_at`` is when a loop expects to tick again."""
    import os

    beat = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "pid": os.getpid() if pid is None else pid,
        "host": host,
        "mode": mode,
        "dryRun": bool(dry_run),
        "startedAt": started_at,
        "lastTickAt": last_tick_at,
        "nextTickAt": next_tick_at,
    }
    try:
        atomic_write_json(Path(backup_dir) / HEARTBEAT_FILENAME, beat)
    except Exception as e:  # presence only; never break a tick over it
        _logger.debug("heartbeat write failed: %r", e)


def read_heartbeat(backup_dir: Path) -> dict | None:
    """The last heartbeat any engine wrote under ``backup_dir``, or None."""
    beat = _read_state_file(Path(backup_dir) / HEARTBEAT_FILENAME)
    if not isinstance(beat.get("lastTickAt"), (int, float)):
        return None
    return beat


def engine_running(beat: dict | None, now: float, *, grace_s: float = HEARTBEAT_GRACE_S) -> bool:
    """Whether a heartbeat comes from a loop engine that is still ticking:
    its announced next tick is not more than ``grace_s`` overdue."""
    if not beat or beat.get("mode") != "loop":
        return False
    next_at = beat.get("nextTickAt")
    if not isinstance(next_at, (int, float)):
        return False
    return now <= next_at + grace_s


def set_aside_accounts(
    backup_dir: Path,
    *,
    identity: Callable[[str], tuple[str, str | None] | None] | None = None,
) -> dict[str, str]:
    """Accounts the engine holds out until their owner logs in again
    (account number → reason, e.g. ``"invalid_grant"``). Read-only; every
    view uses this to say ``needs re-login`` for them.

    ``identity(number)`` gives the slot's ``(email, refresh-token
    fingerprint)`` now; an entry whose account or login has been replaced
    since is left out, by the same rule the engine releases it on its next
    tick (``_release_recovered_quarantines``).
    """
    state = _read_state_file(Path(backup_dir) / STATE_FILENAME)
    quarantine = state.get("quarantine")
    if not isinstance(quarantine, dict):
        return {}
    out = {}
    for number, entry in quarantine.items():
        if not isinstance(entry, dict):
            continue
        if identity is not None:
            try:
                now_id = identity(str(number))
            except Exception:
                now_id = None
            if now_id is None:
                continue
            email_now, fingerprint = now_id
            if not email_now or email_now != entry.get("email"):
                continue
            if fingerprint != entry.get("refreshTokenFingerprint"):
                continue
        out[str(number)] = str(entry.get("reason") or "")
    return out


def record_reset_claim(
    backup_dir: Path,
    number: str,
    record: dict,
    *,
    state_path: Path | None = None,
) -> None:
    """Persist one claim (manual or automatic) so every engine sees it.

    ``resetClaims[number]`` is the latest attempt per account (its request id
    is reused to retry an unconfirmed claim idempotently); successful claims
    also append to ``resetLog`` for the daily brake and for ``qswap reset``.
    """
    path = state_path or (backup_dir / STATE_FILENAME)

    def apply(state: dict) -> None:
        claims = state.get("resetClaims")
        if not isinstance(claims, dict):
            claims = state["resetClaims"] = {}
        claims[str(number)] = record
        if record.get("result") in _BRAKE_RESULTS and record.get("result") != "pending":
            log = state.get("resetLog")
            if not isinstance(log, list):
                log = []
            log.append({
                "at": record.get("at"),
                "account": str(number),
                "grantId": record.get("grantId"),
                "result": record.get("result"),
                "auto": bool(record.get("auto")),
            })
            state["resetLog"] = log[-50:]

    _mutate_state_file(path, apply)


def reset_claims_today(state: dict, now: float, *, auto_only: bool = True) -> int:
    """Claims in the last 24h that may have spent a reset: logged results in
    ``_BRAKE_RESULTS`` plus claims still pending (a crash between the POST
    and its record leaves one), so an unknown outcome counts as spent."""
    def recent(item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("at"), (int, float))
            and now - item["at"] < 86400
            and (bool(item.get("auto")) or not auto_only)
        )

    log = state.get("resetLog")
    logged = sum(
        1 for item in (log if isinstance(log, list) else ())
        if recent(item) and item.get("result", "reset") in _BRAKE_RESULTS
    )
    claims = state.get("resetClaims")
    pending = sum(
        1 for item in (claims.values() if isinstance(claims, dict) else ())
        if recent(item) and item.get("result") == "pending"
    )
    return logged + pending


def reset_claim_settled(record: object, fetched_at: float | None, now: float) -> bool:
    """Whether an account's last claim no longer holds it out of planning."""
    if not isinstance(record, dict):
        return True
    at = record.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True
    if fetched_at is None or fetched_at <= at:
        return False
    result = record.get("result")
    if result == "reset":
        gap = RESET_SUCCESS_GAP_S
    elif result in ("already_used", "not_limited", "cooldown", "ineligible"):
        gap = RESET_REFUSED_GAP_S
    else:
        gap = RESET_RETRY_GAP_S
    return now - at >= gap


def preview_reset_plan(
    switcher: ClaudeAccountSwitcher,
    settings: AutoSwitchSettings,
    entries: Mapping[str, object],
    *,
    now: float | None = None,
    reset_settings=None,
):
    """The reset the engine's policy would spend right now on ``entries``
    (``usage_entries_by_account()``), or None — for ``qswap reset`` to show.
    Same inputs as :meth:`AutoSwitchEngine._maybe_use_reset`, no side effects.
    ``reset_settings`` replaces the configured ``resets.*`` (to ask what a
    different setting would do)."""
    from ai_quota_swap import resets

    now = time.time() if now is None else now
    rs = reset_settings if reset_settings is not None else switcher.reset_settings()
    if not rs.enabled:
        return None
    state = _read_state_file(switcher.backup_dir / STATE_FILENAME)
    if reset_claims_today(state, now) >= MAX_AUTO_RESETS_PER_DAY:
        return None
    quarantined = set(state.get("quarantine") or {}) if isinstance(state.get("quarantine"), dict) else set()
    claims = state.get("resetClaims") if isinstance(state.get("resetClaims"), dict) else {}
    current = switcher.current_account_number()
    policies = switcher.account_policies()
    thresholds = ThresholdMap(
        {n: p.threshold for n, p in policies.items() if p.threshold is not None},
        settings.threshold,
    )
    rotation = [
        n for n in switcher.switchable_account_numbers()
        if n not in quarantined and switcher.account_kind_for(n) != "api_key"
    ]
    accounts = rotation if current in rotation or current is None else [current, *rotation]
    usage = {
        num: entry.decision_value() for num, entry in entries.items()  # type: ignore[union-attr]
    }
    settling = [
        n for n in accounts
        if not reset_claim_settled(claims.get(n), getattr(entries.get(n), "fetched_at", None), now)
    ]
    views = resets.views_from(
        usage,
        claimable=rotation,
        thresholds=thresholds,
        weekly_threshold=settings.weekly_threshold,
        accounts=accounts,
        settling=settling,
    )
    return resets.plan_reset(
        views, current=current, settings=rs,
        watched=parse_model_names(settings.model), now=now,
    )


def _new_reset_request_id() -> str:
    """Claude Code's request id: a random UUID as 32 hex digits."""
    import uuid

    return uuid.uuid4().hex


def reset_request_id(record: object, grant_id: str, host: object = None) -> str:
    """The idempotency key for claiming ``grant_id``: the previous attempt's
    when that attempt never got a decided answer, else a fresh one.

    The id's format is the provider's choice. A ``host`` (the switcher) may
    define ``new_reset_request_id()`` and ``is_reset_request_id(value)``;
    Codex does, for the UUID v4 its consume endpoint expects, and a stored id
    that fails the check is replaced rather than reused. Without them
    (Claude Code) a fresh id is 32 hex digits and any stored string is reused.
    """
    new_id = getattr(host, "new_reset_request_id", None) or _new_reset_request_id
    valid = getattr(host, "is_reset_request_id", None)
    previous = record.get("requestId") if isinstance(record, dict) else None
    if (
        isinstance(record, dict)
        and record.get("grantId") == grant_id
        and not record.get("settled")
        and isinstance(previous, str)
        and (valid is None or valid(previous))
    ):
        return previous
    return new_id()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TickOutcome(enum.Enum):
    """Outcome of one evaluation tick; values double as --once exit codes."""

    SWITCHED = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3  # wanted to switch but no viable target / all exhausted


def _window_pcts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> dict[str, float]:
    """Ordered window label → pct: "5h", "7d", then configured scoped names.

    Deliberately restricted to the windows the *decision* reads (same
    ``models`` filter): showing an unconfigured scoped window at 100% next
    to a switch onto that account would look like a bug, when the engine
    correctly ignored it. Full per-model usage lives in ``qswap list``.
    """
    return {
        name: pct for name, pct, _ in oauth.relevant_windows(usage, models)
    }


# Reset math moved to poll_policy with the cadence numbers; aliased for the
# engine's sleep scheduling and the test suite.
_limiting_reset_ts = poll_policy.limiting_reset_ts
_earliest_future_reset_ts = poll_policy.earliest_future_reset_ts
_parse_reset_ts = poll_policy.parse_reset_ts


def _seven_day_reset_ts(usage: dict | str | None, now: float) -> float | None:
    """Epoch of an account's 7-day (weekly) window reset, or None if unknown
    or already past.

    The consume-first strategy ranks by this — the weekly window is the
    perishable quota (the 5-hour one recycles too fast to be worth planning
    around). A stale snapshot can carry a ``resets_at`` that has since
    elapsed; treated as a real instant it would sort the *just-rolled-over*
    account (the least perishable quota of all) as "soonest", so past ==
    unknown. Plain ``ts <= now``: RESET_SLACK_S is poll-scheduling lag
    tolerance, not ranking input — padding here would turn a genuinely
    imminent reset into a false reset-unknown hold.
    """
    if isinstance(usage, dict):
        window = usage.get("seven_day")
        if isinstance(window, dict):
            ts = _parse_reset_ts(window.get("resets_at"))
            if ts is not None and ts > now:
                return ts
    return None


def _binding_recovery_ts(
    usage: dict | str | None,
    models: Sequence[str],
    now: float,
    *,
    threshold: float | None = None,
    weekly_threshold: float | None = None,
    scale: float | None = None,
) -> float:
    """When this account's *binding* window comes back, as a sort key.

    The binding window is the one holding the account back — the window with
    the least headroom against its own switch line (``lines.binding_window``),
    which with one line is simply the highest utilization among the windows
    that gate it (the same set ``account_headroom`` measures, so ranking and
    headroom can never disagree about which window matters). Its reset is the
    moment the account becomes useful again. With a weekly line both lines
    are required; without one the threshold cannot change the answer.

    Not the weekly window: with every account in the 90s the thing that
    decides where to go is which 5-hour window rolls over first, and that is
    routinely minutes away while the weekly one is days away.

    Returns ``inf`` when unknown or already past, so such accounts sort last
    rather than masquerading as "back immediately" — a stale ``resets_at``
    would otherwise rank a snapshot nobody has refreshed above a measured,
    genuinely imminent one.
    """
    # Pick the BINDING window first, then ask for its reset. Filtering on the
    # reset before the max lets a lower window win whenever the binding one's
    # reset is unknown or past — measured: 7d at 95% with no resets_at and 5h
    # at 40% resetting in an hour returned "back in an hour", which is the
    # opposite of what binds. An account whose binding window has no usable
    # reset is one we cannot schedule around, and inf sorts it last.
    windows = list(oauth.relevant_windows(usage, models))
    if not windows:
        return float("inf")
    if weekly_threshold is None:
        _label, _pct, resets_at = max(windows, key=lambda w: w[1])
    else:
        if threshold is None:
            raise ValueError("threshold is required alongside weekly_threshold")
        resets_at = binding_window(
            windows, threshold, weekly_threshold, scale=scale
        ).resets_at
    ts = _parse_reset_ts(resets_at)
    return ts if ts is not None and ts > now else float("inf")


class ThresholdMap(dict):
    """Effective switch threshold per account number — total by construction.

    Every account with a per-account override maps to that override; every
    other account, including one that is not in the fleet at all, reads the
    global ``autoswitch.threshold``. Totality lives here rather than at each
    of the ten call sites: they subscript directly (``thresholds[num]``), and
    a ``.get(num, default)`` convention would be one forgotten default away
    from a ``KeyError`` mid-tick, in a code path that runs unattended.
    """

    def __init__(self, overrides: Mapping[str, float], default: float) -> None:
        super().__init__(overrides)
        self.default = default

    def __missing__(self, key: str) -> float:
        return self.default


def _every_candidate_above_threshold(
    candidates: Sequence[str],
    over: dict[str, bool | None],
) -> bool:
    """Whether every measured candidate is at or over its own switch lines.

    An unmeasured candidate (``None`` = no readable window) does not block the
    verdict (it may be healthy, but it cannot be *chosen* either — the caller
    skips unreadable rows) as long as at least one candidate was measured.
    """
    # Each flag is that account's verdict against ITS OWN lines, so a fleet is
    # only "all above" when no account has room under the rule it is actually
    # governed by. One account with a higher line still sitting under it is a
    # real landing spot, and the all-above regime must not swallow it.
    measured = [over.get(n) for n in candidates if over.get(n) is not None]
    if not measured:
        return False
    return all(measured)


def _every_account_above_threshold(
    candidates: Sequence[str],
    over: dict[str, bool | None],
    active_over: bool | None,
) -> bool:
    """Whether the active account AND every measured candidate are at or over
    their own switch lines — the state where "land somewhere healthy" has no
    answer.

    Requires the active account's own verdict to be known (``None`` = no
    readable window): without it we do not know we are in this state, and
    guessing here would relax the landing rule on an ordinary tick. Reads the
    same per-window verdict as the trigger and the landing gate, so the three
    cannot disagree about what "above the threshold" means.
    """
    if not active_over:
        return False
    return _every_candidate_above_threshold(candidates, over)


def _ref(number: str, email: str) -> dict:
    return {"number": int(number), "email": email}


def _api_budget(entry: object) -> tuple[float, float, str, str | None] | None:
    """``(used, limit, month, source)`` of an API-key account with a budget
    and a spend figure (``UsageEntry.api_spend``), else None: an account with
    no budget, or nothing to hold it against, is left exactly as before."""
    from ai_quota_swap import api_spend

    reading = getattr(entry, "api_spend", None)
    state = api_spend.budget_state(reading)
    if state is None:
        return None
    month = reading.get("month") if isinstance(reading.get("month"), str) else ""
    source = reading.get("source") if isinstance(reading.get("source"), str) else None
    return state[0], state[1], month, source


def _over_budget(entry: object) -> bool:
    budget = _api_budget(entry)
    return budget is not None and budget[0] >= budget[1]


@dataclass(frozen=True)
class Candidates:
    """The accounts one tick may switch to, each list in rotation order.

    ``considered`` is every rotation account but the active one and those
    set aside; ``oauth`` the subscription logins among them; ``api_key`` the
    API-key accounts the settings let in, less any at its budget.
    """

    considered: tuple[str, ...]
    oauth: tuple[str, ...]
    api_key: tuple[str, ...]


def rotation_candidates(
    switchable: Sequence[str],
    current: str | None,
    *,
    set_aside: Collection[str],
    kind_of: Callable[[str], str],
    include_api_key_accounts: bool,
    entries: Mapping[str, object] | None = None,
) -> Candidates:
    """Which accounts a tick considers as switch targets: the engine's rule,
    pure, so the dashboard's "Next best" list asks the same question.

    ``switchable`` is the provider's rotation (``switchable_account_numbers``:
    a saved login, not disabled). Left out: ``current``; accounts in
    ``set_aside`` (the engine's quarantine: they need re-login); API-key
    accounts unless ``include_api_key_accounts``, and then any at its budget
    (``entries`` holds their spend readings). ``kind_of`` is
    ``account_kind_for``.
    """
    entries = entries or {}
    considered = tuple(n for n in switchable if n != current and n not in set_aside)
    kinds = {n: kind_of(n) for n in considered}
    oauth_accounts = tuple(n for n in considered if kinds[n] != "api_key")
    api_accounts = (
        tuple(
            n for n in considered
            if kinds[n] == "api_key" and not _over_budget(entries.get(n))
        )
        if include_api_key_accounts
        else ()
    )
    return Candidates(considered, oauth_accounts, api_accounts)


# `_maybe_use_reset`'s answer in a dry run that planned a reset: the tick ends.
_RESET_PLANNED = object()


def _headroom_by_account(
    usage: dict[str, dict | str | None], models: tuple[str, ...]
) -> dict[str, float | None]:
    """Per-account headroom derived from decision values."""
    return {
        num: oauth.account_headroom(
            value if isinstance(value, dict) else None, models
        )
        for num, value in usage.items()
    }


class AutoSwitchEngine:
    """Threshold-policy auto-switcher over one provider's switcher (an
    ``providers.EngineHost``: :class:`ClaudeAccountSwitcher` or ``CodexSwitcher``).

    ``on_event`` receives every :class:`AutoSwitchEvent`; exceptions it raises
    are not caught (a broken frontend should fail loudly in tests). ``clock``
    is wall time (persisted cooldown timestamps must survive processes).
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        settings: AutoSwitchSettings,
        on_event: Callable[[AutoSwitchEvent], None],
        *,
        dry_run: bool = False,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.switcher = switcher
        self.settings = settings
        # Model(s) whose per-model weekly limit also binds the switch decision
        # (empty = account-wide 5h/7d only). ``settings.model`` is a comma-
        # separated list ("Fable", "Opus,Sonnet", "all"); parse once here and
        # pass everywhere usage windows are read — decisions, cadence, and
        # reset scheduling must all see the same axes.
        self._models = parse_model_names(settings.model)
        # Poll plans written by the collector must key on the same lines/
        # models the engine decides with (CLI overrides included), not on
        # whatever the settings file happens to say.
        # The fleet MINIMUM, not the global: poll_policy's urgent mode fires
        # at `new_pct >= threshold - 15`, so the lowest line in the fleet is
        # the one that decides when collection must already be tight. Taking
        # the minimum can only make collection earlier, never lazier — the
        # safe direction when accounts disagree about where their line is.
        switcher.set_poll_policy_inputs(
            self._min_effective_threshold(), settings.weekly_threshold, self._models
        )
        self.on_event = on_event
        self.dry_run = dry_run
        self.state_path = state_path or (switcher.backup_dir / STATE_FILENAME)
        self.clock = clock
        self._stop = threading.Event()
        # Cuts the current inter-tick sleep short (a session threshold change
        # from the TUI should show a fresh decision now, not next interval).
        self._wake = threading.Event()
        self._unhealthy_ticks = 0
        self._health_account: str | None = None
        # Both set per tick: a known-reset sleep target, and whether a BLOCKED
        # outcome is static enough (truly exhausted / no candidates) to wait
        # longer than the normal interval.
        self._sleep_until_ts: float | None = None
        self._blocked_wait_long = False
        # Idle-hold: when the active token expired while Claude Code owns it
        # (and is therefore idle), crawl instead of counting unhealthy ticks.
        # ``_idle_hold_since`` survives across ticks (elapsed-time cap);
        # ``_idle_hold_slow`` is per-tick like ``_blocked_wait_long``.
        self._idle_hold_since: float | None = None
        self._idle_hold_slow = False
        # One-shot typo guard for ``autoswitch.model``: resolved (and possibly
        # warned) on the first tick where every relevant account has readable
        # usage — adaptive polling legitimately leaves gaps before that.
        self._model_check_done = not self._models
        # (account, month) budget warnings this engine has emitted; the state
        # file carries them across processes, this covers dry-run too.
        self._budget_warned: set[tuple[str, str]] = set()
        # Who runs this engine, for the heartbeat (`qswap status`, `doctor`).
        self.host_label = "qswap auto"
        self._started_at = clock()
        # What this tick's events need for their human text (aliases, how
        # each account stood), attached by `_emit`; and the facts a switch's
        # reason quotes. Neither is ever serialized or read by a decision.
        self._context: EventContext | None = None
        self._tick_cause: dict = {}

    # -- state file ---------------------------------------------------------

    def _state_lock(self) -> FileLock:
        return _state_lock_for(self.state_path)

    def _read_state(self) -> dict:
        return _read_state_file(self.state_path)

    def _mutate_state(self, mutator: Callable[[dict], None]) -> dict:
        """Read-modify-write the state file under its lock; returns new state.

        The lock prevents two concurrent engines (loop + cron ``--once``) from
        overwriting each other's quarantine/cooldown updates. Never called
        while any other lock is held.
        """
        return _mutate_state_file(self.state_path, mutator)

    # -- usage resets ---------------------------------------------------------

    def _maybe_use_reset(
        self,
        state: dict,
        current: str,
        entries: dict,
        usage: dict,
        thresholds: "ThresholdMap",
        quarantined: set[str],
    ) -> tuple[dict, dict, dict] | None:
        """Spend at most one usage reset this tick, if the policy wants one.

        Runs after collection and before the switch decision, so a refill
        lands in the same tick's decision: an active account brought back
        under its line stays, a refilled peer becomes a target. Returns the
        re-read ``(entries, usage, headroom)`` after a successful claim, None
        otherwise. Only rotation accounts (switchable, not quarantined, OAuth)
        ever spend; an account whose last claim has not been followed by a
        fresh reading is held out (``reset_claim_settled``), and the fleet
        stops after ``MAX_AUTO_RESETS_PER_DAY`` automatic resets a day.
        """
        from ai_quota_swap import resets

        # Nothing to plan unless some reading carries grants: checked first,
        # so a fleet without resets pays no settings read and no clock read
        # (the tick's clock reads are part of its decision snapshot).
        if not any(
            isinstance(u, dict)
            and isinstance(u.get("resets"), dict)
            and u["resets"].get("grants")
            for u in usage.values()
        ):
            return None
        rs = self.switcher.reset_settings()
        if not rs.enabled or rs.auto not in ("active", "last-resort", "on-limit"):
            return None
        now = self.clock()
        if reset_claims_today(state, now) >= MAX_AUTO_RESETS_PER_DAY:
            return None
        rotation = [
            n for n in self.switcher.switchable_account_numbers()
            if n not in quarantined and self.switcher.account_kind_for(n) != "api_key"
        ]
        accounts = rotation if current in rotation else [current, *rotation]

        def plan_on(entries_now: dict, usage_now: dict, claims: dict, at: float):
            settling = [
                n for n in accounts
                if not reset_claim_settled(
                    claims.get(n),
                    entries_now[n].fetched_at if n in entries_now else None,
                    at,
                )
            ]
            views = resets.views_from(
                usage_now,
                claimable=rotation,
                thresholds=thresholds,
                weekly_threshold=self.settings.weekly_threshold,
                accounts=accounts,
                settling=settling,
            )
            return resets.plan_reset(
                views, current=current, settings=rs, watched=self._models, now=at
            )

        claims = state.get("resetClaims")
        plan = plan_on(entries, usage, claims if isinstance(claims, dict) else {}, now)
        if plan is None:
            return None
        if self.dry_run:
            ref = _ref(plan.account, self.switcher.account_email(plan.account))
            self._emit(ResetEvent(account=ref, plan=plan.to_json(), dry_run=True))
            # What a real tick decides next rests on the refill, which a dry
            # run cannot see: the tick ends here rather than also report a
            # switch the reset would have made unnecessary.
            return _RESET_PLANNED

        # Re-read the account before spending: the stored reading can be
        # minutes old (an hour under extended trust), and a reset taken in
        # Claude Code or by another surface meanwhile is invisible in it.
        # Plan again on the fresh numbers; no fresh reading, no spend.
        target = plan.account
        entries = {**entries, **self.switcher.refresh_usage({target})}
        fresh = entries.get(target)
        now = self.clock()
        if fresh is None or fresh.fetched_at is None or now - fresh.fetched_at > RESET_FRESH_READ_S:
            _logger.info("usage reset on account %s deferred: no fresh reading", target)
            return None
        usage = {**usage, target: fresh.decision_value()}
        plan = plan_on(entries, usage, claims if isinstance(claims, dict) else {}, now)
        if plan is None:
            return None
        ref = _ref(plan.account, self.switcher.account_email(plan.account))
        fetched_at = entries[plan.account].fetched_at if plan.account in entries else None

        # Reserve under the state lock: re-check the brake and the account's
        # last claim against the CURRENT file (another engine — the menu
        # bar, the TUI, a cron --once — may have claimed since this tick's
        # snapshot), take the request id from that fresh record, and write
        # the pending record before any POST.
        reserved: dict = {}

        def reserve(st: dict) -> None:
            if reset_claims_today(st, now) >= MAX_AUTO_RESETS_PER_DAY:
                return
            current_claims = st.get("resetClaims")
            if not isinstance(current_claims, dict):
                current_claims = st["resetClaims"] = {}
            previous = current_claims.get(plan.account)
            if not reset_claim_settled(previous, fetched_at, now):
                return
            record = {
                "at": now,
                "grantId": plan.grant_id,
                "requestId": reset_request_id(previous, plan.grant_id, self.switcher),
                "reason": plan.reason,
                "auto": True,
                "result": "pending",
                "settled": False,
            }
            current_claims[plan.account] = record
            reserved.update(record)

        _mutate_state_file(self.state_path, reserve)
        if not reserved:
            return None
        result = self.switcher.claim_usage_reset(
            plan.account, plan.grant_id, reserved["requestId"]
        )
        try:
            record_reset_claim(
                self.switcher.backup_dir, plan.account,
                {
                    **reserved,
                    "result": result.result,
                    "settled": result.settled,
                    "detail": result.reason or result.detail,
                },
                state_path=self.state_path,
            )
        except Exception as e:  # the pending record still holds the request id
            _logger.warning("could not record usage reset claim: %r", e)
        self._emit(ResetEvent(account=ref, plan=plan.to_json(), result=result.to_json()))
        if not result.ok:
            return None
        # claim_usage_reset already re-read the claimed account; serve the
        # store for everyone else (no extra requests).
        entries = self.switcher.usage_entries_by_account(fetch=set())
        usage = {num: entry.decision_value() for num, entry in entries.items()}
        return entries, usage, _headroom_by_account(usage, self._models)

    # -- quarantine -----------------------------------------------------------

    def _quarantine(self, number: str, email: str, reason: str) -> None:
        creds = self.switcher.read_account_credentials(number, email)
        fingerprint = self.switcher.credential_fingerprint(creds) if creds else None

        def add(state: dict) -> None:
            state.setdefault("quarantine", {})[number] = {
                "email": email,
                "reason": reason,
                "at": _now_iso(),
                "refreshTokenFingerprint": fingerprint,
            }

        self._mutate_state(add)
        if self._context is not None and self._context.accounts:
            # The rest of this tick's text sees it as set aside too.
            self._context = replace(self._context, accounts=tuple(
                replace(v, state="needs-relogin", set_aside=reason) if v.number == number else v
                for v in self._context.accounts
            ))
        recover = getattr(self.switcher, "recover_hint", None)
        self._emit(QuarantineEvent(
            number=number, email=email, reason=reason,
            recover=recover if isinstance(recover, str) else "",
        ))

    def _release_recovered_quarantines(self, state: dict) -> dict:
        """Drop quarantine entries whose credential was replaced since.

        A changed refresh-token fingerprint (or a removed/re-added slot) means
        the user re-logged in and re-captured the account — the dead lineage
        is gone, so it re-enters rotation.
        """
        quarantine = state.get("quarantine")
        if not isinstance(quarantine, dict) or not quarantine:
            return state
        to_release: list[tuple[str, str, str]] = []
        for number, entry in quarantine.items():
            email_now = self.switcher.account_email(number)
            if not email_now or email_now != entry.get("email"):
                to_release.append(
                    (number, entry.get("email", ""), "account-replaced")
                )
                continue
            creds = self.switcher.read_account_credentials(number, email_now)
            fingerprint = (
                self.switcher.credential_fingerprint(creds) if creds else None
            )
            if fingerprint != entry.get("refreshTokenFingerprint"):
                to_release.append((number, email_now, "credentials-replaced"))
        if not to_release:
            return state

        def drop(s: dict) -> None:
            q = s.get("quarantine")
            if isinstance(q, dict):
                for number, _, _ in to_release:
                    q.pop(number, None)

        state = self._mutate_state(drop)
        for number, email, reason in to_release:
            self._emit(UnquarantineEvent(number=number, email=email, reason=reason))
        return state

    # -- freshening -----------------------------------------------------------

    def _freshen_target(self, number: str, email: str) -> str:
        """Ensure a candidate's stored token outlives Claude Code's 5-min
        refresh buffer before it gets activated.

        Returns ``"ok"``, ``"invalid_grant"`` (dead lineage — quarantine),
        ``"identity-conflict"`` (alive but authenticates as a different
        account — quarantine, do not activate), ``"transient"`` (network
        trouble — try again next tick) or ``"skip-live-session"``. Only ever
        touches the slot's *backup* store; the active credential belongs to
        Claude Code.
        """
        if self.switcher.account_kind_for(number) == "api_key":
            return "ok"  # API keys don't expire/refresh
        if self.switcher.live_session_pids_for(number, email):
            # A live `qswap run` session owns this account's token in its own
            # profile. Auto-activating it as the default login too would put
            # one rotating refresh token in two config dirs (the stale-copy
            # failure class) with nobody reading the warning — and its quota
            # is already being consumed by that session anyway. Manual
            # switch_to keeps its warn-and-proceed behavior; auto skips.
            return "skip-live-session"
        creds = self.switcher.read_account_credentials(number, email)
        if not creds:
            return "transient"
        try:
            expires_at = self.switcher.credential_expires_at_ms(creds)
        except CredentialFormatError:
            return "invalid_grant"
        now_ms = self.clock() * 1000
        near_expiry = (
            isinstance(expires_at, (int, float))
            and now_ms + FRESHEN_BUFFER_MS >= expires_at
        )
        if not near_expiry:
            return "ok"
        # The consume gate serializes every backup-rt POST (the recovery
        # branch in `_fetch_active_usage` is a second call site, under the
        # same per-slot consume lock):
        # it re-reads under the slot lock (our snapshot may be superseded),
        # consults the session profile for a newer generation, and persists
        # via fingerprint CAS — so a freshen racing the collector (or a
        # sibling surface) can no longer double-consume one grant.
        outcome = self.switcher.consume_backup_grant(number, email, creds)
        if outcome.error is None and outcome.credentials:
            # The gate already persisted the successor (or adopted a racing
            # writer's newer lineage) under its own lock.
            if self._note_token_identity(number, outcome.token_account):
                # The slot's stored credential authenticates as a *different*
                # account — activating it would put the user on the wrong
                # account with every gauge reading normal. Not a viable
                # target; the caller quarantines it (released automatically
                # once the credential is replaced by a re-add).
                return "identity-conflict"
            return "ok"
        if outcome.error in ("invalid_grant", "no_refresh_token"):
            return "invalid_grant"
        if outcome.error in _SYSTEMIC_STATUSES:
            # Deterministic conditions, not network trouble: every candidate
            # refuses identically and keeps refusing until something outside
            # this process changes — the shell for store-unmirrored (an
            # inherited CLAUDE_SECURESTORAGE_CONFIG_DIR), our OAuth client
            # registration for invalid_client. Reported distinctly so the tick
            # error names the real cause instead of "(network?)", which would
            # send the user to check a connection that is fine.
            return outcome.error
        return "transient"

    def _note_token_identity(
        self, number: str, token_account: dict | None
    ) -> bool:
        """Use the token endpoint's free identity to verify/backfill a slot.

        The refresh grant just ran against the slot's own stored credential,
        so ``token_account`` (when the server includes it) names who that
        credential really is. Returns True on a *conflict*: the credential
        authenticates under a different organization than the slot records
        (org compared first, whenever both sides record one), or as a
        different account uuid. An empty slot uuid (blank-uuid records from
        older versions, add-token placeholders) is backfilled — but only
        when no org conflict exists: a wrong-org credential is evidence the
        slot holds the wrong account, and backfilling *its* uuid would
        poison the slot's identity record (backfill never rewrites a
        non-empty uuid, so that corruption would be sticky).

        ``_parse_token_account`` already enforces a strict boundary, but this
        identity is opportunistic — re-check types here so malformed data can
        never break the freshen that carried it (the successor credential is
        already persisted by the time this runs).
        """
        if not isinstance(token_account, dict):
            return False
        ta_uuid = token_account.get("uuid")
        if not isinstance(ta_uuid, str) or not ta_uuid.strip():
            return False
        ta_uuid = ta_uuid.strip()
        slot_identity = self.switcher.account_identity(number)
        ta_org = token_account.get("organizationUuid")
        slot_org = slot_identity.get("organizationUuid") or ""
        if isinstance(ta_org, str) and ta_org and slot_org and ta_org != slot_org:
            return True
        if not slot_identity.get("uuid"):
            try:
                self.switcher.backfill_account_uuid(number, ta_uuid)
            except Exception as e:  # never let bookkeeping break a freshen
                _logger.debug("uuid backfill failed for account %s: %r", number, e)
            return False
        return slot_identity["uuid"] != ta_uuid

    # -- tick -----------------------------------------------------------------

    def tick(self) -> TickOutcome:
        """Evaluate once: poll usage, maybe switch. Never raises."""
        try:
            outcome = self._tick_inner()
            self._persist_health_counters(self._health_account)
            return outcome
        except ProviderError as e:
            self._emit(ErrorEvent(message=str(e), transient=True))
            return TickOutcome.ERROR
        except Exception as e:  # pragma: no cover - safety net
            self._emit(
                ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
            )
            return TickOutcome.ERROR

    def _resolve_thresholds(
        self, settings: AutoSwitchSettings | None = None,
        *, policies: Mapping[str, AccountPolicy] | None = None,
    ) -> ThresholdMap:
        """The effective threshold for every account, resolved once per tick.

        Resolution is ``policy(n).threshold if set else settings.threshold``.
        The result is passed DOWN as a parameter rather than looked up where
        it is needed: ``_rank_candidates`` is documented pure and runs twice
        per tick under the consume-first two-phase commit, so a store read
        inside it could decide phase 1 and phase 2 on different lines.

        The ``settings`` argument exists so a tick resolves against the same
        snapshot it gates with, even if ``apply_threshold()`` lands mid-tick.
        """
        effective = settings or self.settings
        if policies is None:
            policies = self.switcher.account_policies()
        return ThresholdMap(
            {
                num: policy.threshold
                for num, policy in policies.items()
                if policy.threshold is not None
            },
            effective.threshold,
        )

    def _min_effective_threshold(
        self, thresholds: ThresholdMap | None = None
    ) -> float:
        """The lowest effective threshold across switchable accounts.

        Used only for poll cadence (``set_poll_policy_inputs``), never for a
        switch decision — cadence is fleet-wide, so it has to be tight enough
        for whichever account crosses its own line first. With no overrides
        this is exactly ``settings.threshold``.

        ``thresholds`` lets a tick reuse the map it already resolved, so the
        cadence it pins and the lines it gates on come from one store read of
        one snapshot. Callers outside a tick (construction, ``apply_threshold``)
        omit it and resolve their own.
        """
        thresholds = thresholds if thresholds is not None else self._resolve_thresholds()
        values = [
            thresholds[num]
            for num in self.switcher.switchable_account_numbers()
        ]
        return min(values) if values else self.settings.threshold

    def _refresh_poll_policy_inputs(self, thresholds: ThresholdMap) -> None:
        """Re-pin poll cadence from this tick's threshold map.

        ``set_poll_policy_inputs`` used to be called in exactly two places,
        ``__init__`` and ``apply_threshold()``. Neither is on the path a
        *persisted* policy write takes: ``qswap threshold 1 50`` runs in a
        separate process from ``qswap auto``, so a running engine re-read the
        new line for its DECISION every tick (``_resolve_thresholds``) while
        its CADENCE stayed pinned to the minimum as it stood at boot.

        That defeats the reason the cadence is pinned to the fleet minimum at
        all. The minimum is chosen because ``poll_policy``'s urgent mode
        fires at ``new_pct >= threshold - 15``, so the fleet minimum can only
        make collection *earlier* — never lazier, and no crossing is missed.
        A stale 90 against a live 50 goes urgent at 75% instead of 35%, which
        is lazier in exactly the direction that reason rules out, on every
        tick until the process restarts.

        Called before ``_collect_scheduled_usage`` so the tick that discovers
        the change also plans on it. The pin itself is a bare attribute write
        (``ClaudeAccountSwitcher.set_poll_policy_inputs``); the only added
        per-tick cost is one ``switchable_account_numbers()`` read, which
        ``_tick_inner`` already performs for candidate construction.
        """
        self.switcher.set_poll_policy_inputs(
            self._min_effective_threshold(thresholds),
            self.settings.weekly_threshold,
            self._models,
        )

    def _load_health_counters(self, state: dict, current: str) -> None:
        """Seed the unreadable-active counters from the state file.

        They are persisted so a fresh engine per tick (``auto --once``) keeps
        counting toward failover instead of restarting at zero. Counters left
        by another account, or by a tick longer ago than twice the idle-hold cap,
        are dropped. Until the managed active account changes the in-memory values
        are authoritative, so dry-run (which never persists) and long gaps
        between loop ticks keep their progress.
        """
        ticks = state.get("unhealthyTicks")
        since = state.get("idleHoldSince")
        at = state.get("healthTickAt")
        fresh = (
            state.get("healthAccount") == current
            and isinstance(at, (int, float))
            and not isinstance(at, bool)
            and 0 <= self.clock() - at <= 2 * IDLE_HOLD_MAX_S
        )
        self._unhealthy_ticks = (
            ticks
            if fresh
            and isinstance(ticks, int)
            and not isinstance(ticks, bool)
            and ticks > 0
            else 0
        )
        self._idle_hold_since = (
            float(since)
            if fresh
            and isinstance(since, (int, float))
            and not isinstance(since, bool)
            else None
        )

    def _persist_health_counters(self, current: str | None) -> None:
        if self.dry_run:
            return
        ticks, since = self._unhealthy_ticks, self._idle_hold_since
        now = self.clock()

        def store(state: dict) -> None:
            if current is not None and (ticks or since is not None):
                state["unhealthyTicks"] = ticks
                state["idleHoldSince"] = since
                state["healthAccount"] = current
                state["healthTickAt"] = now
            else:
                for key in (
                    "unhealthyTicks", "idleHoldSince", "healthAccount",
                    "healthTickAt",
                ):
                    state.pop(key, None)

        stored = self._read_state()
        if not (ticks or since is not None) and "healthAccount" not in stored:
            return
        self._mutate_state(store)

    def _tick_inner(self) -> TickOutcome:
        self._sleep_until_ts = None
        self._blocked_wait_long = False
        self._idle_hold_slow = False
        self._tick_cause = {}
        self._context = EventContext(
            provider=self._provider_id(), names=self._alias_names()
        )
        settings = self.settings
        priority_mode = settings.strategy == "priority-consume-first"
        state = self._read_state()
        if not self.dry_run:
            # Dry-run must not write anything, so recovered quarantines are
            # only released (state mutation) on real ticks.
            state = self._release_recovered_quarantines(state)
        quarantined = set(
            state.get("quarantine", {})
            if isinstance(state.get("quarantine"), dict)
            else {}
        )

        current = self.switcher.current_account_number()
        # A tick with no managed active account keeps the current context;
        # a different account reseeds from the state file so its counts are
        # never inherited from the previous one.
        if current is not None and current != self._health_account:
            self._load_health_counters(state, current)
            self._health_account = current
        if current is None:
            self._emit(
                PollEvent(
                    active=None,
                    headroom={},
                    threshold=settings.threshold,
                    weekly_threshold=settings.weekly_threshold,
                )
            )
            add = self._add_command()
            if self.switcher.has_live_login():
                # Live login exists but qswap doesn't manage it: never act —
                # a switch would overwrite it without a backup.
                self._emit(
                    NoSwitchEvent(
                        reason="unmanaged-active-account",
                        detail=f"run '{add}' to include it in rotation",
                    )
                )
            else:
                self._emit(
                    NoSwitchEvent(
                        reason="no-active-account",
                        detail=f"log in and run '{add}' first",
                    )
                )
            return TickOutcome.NO_ACTION

        current_email = self.switcher.account_email(current)
        active_ref = _ref(current, current_email) if current_email else {
            "number": int(current),
            "email": "",
        }

        # One resolution per tick, threaded down as a parameter from here on.
        # Every gate below reads `thresholds[...]`; the map is total, so an
        # account absent from it (never sequenced, just removed) reads the
        # global rather than raising mid-tick.
        policies = self.switcher.account_policies()
        thresholds = self._resolve_thresholds(settings, policies=policies)
        # Cadence follows the same snapshot, and does so BEFORE collection is
        # planned below — see `_refresh_poll_policy_inputs`.
        self._refresh_poll_policy_inputs(thresholds)
        entries, usage, headroom = self._collect_scheduled_usage(
            current,
            quarantined,
            threshold=thresholds[current],
            weekly_threshold=settings.weekly_threshold,
        )
        self._context = replace(
            self._context or EventContext(),
            accounts=self._account_views(
                entries, usage, state, current, thresholds, settings
            ),
            active=current,
        )
        self._emit(
            PollEvent(
                active=active_ref,
                headroom=headroom,
                # The active account's own line: the number the CLI and TUI
                # render as "the threshold" must be the one the departure
                # gate below actually used, or the UI contradicts the engine.
                threshold=thresholds[current],
                weekly_threshold=settings.weekly_threshold,
                fetch_errors={
                    num: entry.last_error
                    for num, entry in entries.items()
                    if usage.get(num) is None and entry.last_error
                },
                windows={
                    num: pcts
                    for num, value in usage.items()
                    if (pcts := _window_pcts(
                        value if isinstance(value, dict) else None, self._models
                    ))
                },
            )
        )

        if not self._model_check_done:
            self._check_model_names(quarantined, usage)

        refilled = self._maybe_use_reset(
            state, current, entries, usage, thresholds, quarantined
        )
        if refilled is _RESET_PLANNED:
            return TickOutcome.NO_ACTION  # dry run: "would use a reset" ends the tick
        if refilled is not None:
            entries, usage, headroom = refilled

        # API-key budgets (docs/api-accounts.md): one warning per account per
        # month, and an active account at its budget is at its limit.
        self._warn_budgets(state, entries)
        active_over_budget = _over_budget(entries.get(current))

        if (
            self.switcher.account_kind_for(current) == "api_key"
            and not settings.include_api_key_accounts
            and not active_over_budget
        ):
            self._emit(
                NoSwitchEvent(
                    reason="active-api-key",
                    detail="API-key accounts have no quota to watch",
                )
            )
            return TickOutcome.NO_ACTION

        active_headroom = headroom.get(current)
        active_binding: BindingWindow | None = None
        if active_over_budget:
            # The budget is this account's limit: leave it like an exhausted
            # one (at-limit skips the cooldown and the landing hysteresis).
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            trigger = "at-limit"
        elif active_headroom is not None:
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            # Each window against its OWN line, the closest one decides (see
            # lines.py). A readable headroom means a readable window, so the
            # account-wide fallback is unreachable; kept as the exact old test.
            active_binding = self._binding(
                usage.get(current), settings, thresholds[current]
            )
            below = (
                not active_binding.over
                if active_binding is not None
                else (100.0 - active_headroom) < thresholds[current]
            )
            if below:
                if settings.strategy != "consume-first" and not priority_mode:
                    self._emit(
                        NoSwitchEvent(
                            reason="below-threshold",
                            # Both sides through pct_label: .0f utilization could
                            # display an impossible "100% < 99.9%".
                            detail=_below_line_detail(
                                active_binding,
                                active_headroom,
                                thresholds[current],
                            ),
                            binding=active_binding,
                        )
                    )
                    return TickOutcome.NO_ACTION
                # consume-first: below the threshold we still proactively move to
                # whichever account's weekly window resets soonest, to burn the
                # most-perishable quota first. Candidate selection decides whether
                # a sooner-resetting account with room actually exists.
                trigger = "priority" if priority_mode else "consume-first"
            else:
                # Exhaustion is physical: at-limit reads the raw headroom.
                trigger = "at-limit" if active_headroom <= 0 else "proactive"
        else:
            if usage.get(current) == USAGE_TOKEN_EXPIRED:
                # Expired and the refresh could not complete this pass (lock
                # contention, unattributable lineage, failed persist, or the
                # row's failure backoff gating the fetch). The locked-refresh
                # path retries on later passes — no quota burn, nothing to
                # switch for yet; crawl slowly instead of burning failover
                # ticks (Finding 2 of the usage-lapse investigation).
                now = self.clock()
                if self._idle_hold_since is None:
                    self._idle_hold_since = now
                if now - self._idle_hold_since <= IDLE_HOLD_MAX_S:
                    self._unhealthy_ticks = 0
                    self._idle_hold_slow = True
                    self._emit(
                        NoSwitchEvent(
                            reason="active-idle",
                            detail=(
                                f"token expired while {self._display_name()} "
                                "is idle; resumes on next use"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Held far longer than any idle nap should need — likely a
                # dead refresh token with an *active* user. Fall through to
                # normal unhealthy counting so failover can still happen.
                _logger.warning(
                    "Active token expired and owned for over %.0f minutes; "
                    "resuming unhealthy counting (dead refresh token?)",
                    IDLE_HOLD_MAX_S / 60,
                )
            else:
                self._idle_hold_since = None
            self._unhealthy_ticks += 1
            if self._unhealthy_ticks < settings.unhealthy_ticks:
                self._emit(
                    NoSwitchEvent(
                        reason="active-usage-unknown",
                        detail=(
                            f"{self._unhealthy_ticks}/{settings.unhealthy_ticks} "
                            "before failover"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            trigger = "failover"

        # What a switch this tick would quote as its reason (human text only).
        self._tick_cause = {
            "binding": active_binding,
            "over_budget": active_over_budget,
            "policies": policies,
        }

        if trigger in ("proactive", "consume-first", "priority") and self._in_cooldown(state):
            self._emit(NoSwitchEvent(reason="cooldown", wait_s=self._cooldown_left(state)))
            return TickOutcome.NO_ACTION

        # -- candidate selection ------------------------------------------
        switchable = self.switcher.switchable_account_numbers()
        # The no-return bar itself lives in `_rank` below: it is a statement
        # about the CHOICE, so it belongs where the choice is made rather than
        # in this census of what exists. See `_no_return_account` for the
        # incident, the scoping, and the release.
        picked = rotation_candidates(
            switchable,
            current,
            set_aside=quarantined,
            kind_of=self.switcher.account_kind_for,
            include_api_key_accounts=settings.include_api_key_accounts,
            entries=entries,  # an API-key account at its budget is not a target
        )
        candidates = list(picked.considered)
        oauth_candidates = list(picked.oauth)
        api_key_candidates = list(picked.api_key)
        if (
            trigger in ("consume-first", "priority")
            and not oauth_candidates
            and active_headroom is not None
        ):
            # Healthy below-threshold account with no OAuth peer to compare
            # against — the same state `best` reports as below-threshold
            # NO_ACTION before ever reaching candidate selection. API-key
            # candidates don't change the outcome: they have no weekly window
            # to consume, so a consume-first nudge never targets them. Keep
            # the exit-code contract identical across strategies: cron
            # wrappers keying on BLOCKED must not see false "blocked" from
            # the flag alone.
            self._emit(
                NoSwitchEvent(
                    reason="below-threshold",
                    detail=_below_line_detail(
                        active_binding, active_headroom, thresholds[current]
                    ),
                    binding=active_binding,
                )
            )
            return TickOutcome.NO_ACTION
        if not oauth_candidates and not api_key_candidates:
            # Won't change until the user adds/recovers an account — no point
            # re-polling at full cadence.
            self._blocked_wait_long = True
            self._emit(NoSwitchEvent(reason="no-candidates"))
            return TickOutcome.BLOCKED

        consume_first = settings.strategy == "consume-first"

        def _rank(**kw):
            """Rank with the no-return bar, and WITHOUT it if that empties AND
            the barred account is a different proposition from the one we left.

            Emptiness alone cannot be the release. On two accounts there is
            exactly one candidate, so barring it ALWAYS empties the list —
            measured, sweeping active x barred headroom x both reset shapes,
            `n=2 barred-rank EMPTY=320 NONEMPTY=0`. An emptiness-only release
            therefore fires every tick and the bar is inert at the fleet size
            the flap was reported on: pcts 92/92, resets 500h/400h, 60 ticks
            gave `[1, 2, 1, 2]` with the bar on and the identical `[1, 2, 1, 2]`
            with `lastSwitchFrom` popped every tick.

            "BARRING LEAVES NOTHING" AND "WE ARE FLAPPING" ARE DIFFERENT
            STATES, and at n=2 they are always the same state — which is how
            one swallowed the other. The ranking cannot separate them: it sees
            only the present, and both look like an empty list. What separates
            them is WHY the ranking flipped. Traced at each leg of that walk:

                t8   1->2   left 1 holding 4.0 pts, 500h out
                t20  2->1   account 1 still 4.0 pts, still 500h out
                t22  1->2   account 2 still 2.0 pts, still 400h out

            Every return won because the ACTIVE burned down, never because the
            target recovered. So the release asks the one question the ranking
            cannot: is the account we left better than when we left it?

            ON BOTH AXES AN ACCOUNT CAN IMPROVE ON, each with its own margin —
            ``SPENT_HEADROOM_PCT`` of headroom (below that an edge is under two
            poll intervals of work) or a binding reset ``RECOVERY_HYSTERESIS_S``
            sooner. An account's headroom rises only when a window rolls over
            and its binding reset only moves nearer when a nearer window
            starts binding, so both are real events rather than the boundary
            crossings burn manufactures for free.

            Emptiness still decides whether to ASK. Where the bar leaves a
            real alternative it simply applies, so a fleet with somewhere else
            to go is untouched by any of this.

            Cheap: the retry runs only when the barred list came back empty,
            which is the tick that was about to do nothing anyway.
            """
            if priority_mode:
                # Strict landing and a stable priority/reset ordering make the
                # legacy escape/no-return heuristics unnecessary. A recovered
                # higher tier must be able to reclaim work immediately after
                # the configured cooldown, even when it is the account we left.
                return self._rank_priority_candidates(
                    oauth_candidates=kw["oauth_candidates"],
                    usage=kw["usage"], headroom=kw["headroom"],
                    current=current, active_enabled=current in switchable,
                    settings=settings, now=kw["now"], thresholds=thresholds,
                    policies=policies,
                )
            # Recomputed per snapshot, never once per tick: the consume-first
            # two-phase commit replaces `headroom` and `active_headroom` and
            # re-ranks, and the ratio release consumes exactly those two
            # values. Computed once, the bar answered from a snapshot the
            # ranking had already thrown away — `left=20 active=30` bars,
            # `left=90 active=10` releases, and phase 2 is where that flips.
            recovered = self._left_account_recovered(
                state,
                kw["usage"],
                kw["headroom"],
                kw["active_headroom"],
                kw["settings"],
                kw["now"],
                kw["current"],
                thresholds=kw["thresholds"],
            )
            no_return = self._no_return_account(
                trigger,
                state,
                kw["headroom"],
                kw["active_headroom"],
                recovered,
                kw["settings"],
                kw["current"],
                thresholds=kw["thresholds"],
                usage=kw["usage"],
            )
            ranked = self._rank_candidates(no_return=no_return, **kw)
            if no_return is not None and not ranked[0] and recovered:
                unbarred = self._rank_candidates(no_return=None, **kw)
                if unbarred[0]:
                    return unbarred
            return ranked

        decided_now = self.clock()
        ordered, any_known, active_reset_ts = _rank(
            trigger=trigger,
            consume_first=consume_first,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            current=current,
            active_headroom=active_headroom,
            settings=settings,
            now=decided_now,
            thresholds=thresholds,
        )

        if (trigger == "consume-first" or priority_mode) and ordered:
            # Two-phase commit: the provisional pick may have ridden a
            # snapshot up to CANDIDATE_MAX_INTERVAL_S stale — consume-first
            # decides below the threshold, where the collector only escalates
            # inside the ESCALATION_MARGIN_PCT band (flat-traffic invariant).
            # A switch is imminent, so spend the fetches now and re-decide on
            # fresh data.
            # reserve() serves just-fetched accounts from the store, so this
            # is cheap in-tick and plan-bounded across ticks. The trigger is
            # deliberately NOT re-classified if the fresh active crossed the
            # threshold: a still-qualifying sooner target switches anyway,
            # and otherwise the next tick escalates normally and escapes.
            entries = self.switcher.usage_entries_by_account(
                fetch={current, *candidates}
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}
            headroom = _headroom_by_account(usage, self._models)
            active_headroom = headroom.get(current)
            decided_now = self.clock()
            ordered, any_known, active_reset_ts = _rank(
                trigger=trigger,
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=current,
                active_headroom=active_headroom,
                settings=settings,
                now=decided_now,
                thresholds=thresholds,
            )

        priority_active_ready = priority_mode and current in switchable and self._priority_ready(
            usage.get(current), headroom.get(current), settings, thresholds[current]
        )
        if (
            not ordered and api_key_candidates
            and trigger not in ("consume-first", "priority")
            and not priority_active_ready
        ):
            # Last resort when we must move: metered API-key accounts
            # (unmeasurable headroom). Never for a below-threshold consume-first
            # nudge — those API-key accounts have no weekly window to consume.
            # And never while Claude Code sessions run on the shared login: a
            # running session cannot move to a key and would stop with "Not
            # logged in" (docs/design/api-key-sessions.md).
            running = self._shared_session_count()
            if running:
                self._emit(_api_key_sessions_event(api_key_candidates[0], running))
                return TickOutcome.BLOCKED
            ordered = api_key_candidates

        if not ordered:
            if priority_active_ready:
                self._emit(NoSwitchEvent(
                    reason="already-consuming-priority",
                    detail="no usable account has higher priority or an earlier weekly reset in this tier",
                ))
                return TickOutcome.NO_ACTION
            if priority_mode:
                self._emit(NoSwitchEvent(
                    reason="no-qualifying-candidate",
                    detail="no candidate is below every switch line with readable usage; waiting for recovery",
                ))
                return TickOutcome.BLOCKED
            if not any_known:
                # No candidate readable this tick — true for every strategy,
                # and must not be dressed up as a consume-first hold.
                self._emit(
                    NoSwitchEvent(
                        reason="no-comparison",
                        detail="no candidate has readable usage",
                    )
                )
                return TickOutcome.BLOCKED
            if trigger == "consume-first":
                # Below the threshold and healthy: staying put is a correct
                # outcome, never a block. Distinguish *why* nothing qualified
                # so an opted-in user can see the strategy working (or inert).
                if active_reset_ts is None:
                    # The strictly-sooner filter skips every candidate when the
                    # active account's weekly reset is unknown — without this
                    # reason the strategy would look enabled while doing
                    # nothing, with no way to tell.
                    self._emit(
                        NoSwitchEvent(
                            reason="reset-unknown",
                            detail=(
                                "active account's weekly reset time is "
                                "unknown; consume-first is idle until it "
                                "is reported"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Covers both "everyone resets later" and "sooner ones have no
                # room" — don't claim the active account resets first when the
                # real story may be exhausted candidates.
                self._emit(
                    NoSwitchEvent(
                        reason="already-consuming-soonest",
                        detail="no sooner-resetting account with room to spare",
                    )
                )
                return TickOutcome.NO_ACTION
            # "All exhausted" (and its bounded reset-aware sleep) only when it's
            # literally true: every candidate's usage is known and at its
            # limit. A candidate that merely failed the proactive hysteresis
            # gate, or one whose usage is unreadable this tick, can become
            # viable at any moment — and the active account can hit 100% and
            # need the at-limit escape — so those keep the normal cadence.
            candidate_headrooms = [headroom.get(n) for n in oauth_candidates]
            truly_exhausted = all(
                h is not None and h <= 0 for h in candidate_headrooms
            )
            if not truly_exhausted:
                self._emit(
                    NoSwitchEvent(
                        reason="no-qualifying-candidate",
                        detail=(
                            "no candidate is below its switch lines and better "
                            "than the active account by the hysteresis "
                            "margin, or usage is unreadable this tick"
                        ),
                    )
                )
                return TickOutcome.BLOCKED
            self._blocked_wait_long = True
            earliest = self._earliest_recovery(usage)
            if earliest is not None:
                self._sleep_until_ts = earliest.timestamp() + RESET_SLACK_S
            self._emit(
                AllExhaustedEvent(
                    earliest_reset_at=(
                        earliest.isoformat().replace("+00:00", "Z")
                        if earliest
                        else None
                    )
                )
            )
            return TickOutcome.BLOCKED

        # -- freshen + switch ----------------------------------------------
        # The departure snapshot of the account we are leaving, taken from the
        # SAME `usage`/`headroom` the ranking just decided on — for
        # consume-first that is the phase-2 refetch, not the stale one.
        left_snapshot = (
            active_headroom,
            _binding_recovery_ts(
                usage.get(current),
                self._models,
                decided_now,
                threshold=thresholds[current],
                weekly_threshold=settings.weekly_threshold,
                scale=settings.threshold,
            ),
        )
        transient_failure = False
        systemic = ""
        for num in ordered:
            email = self.switcher.account_email(num)
            if trigger == "consume-first" or (priority_mode and num in oauth_candidates):
                # The phase-2 refetch is best-effort: the collector refuses
                # accounts in failure backoff or claimed by a concurrent
                # poller, which then serve their stored entries. Consume-first
                # is opportunistic, not an escape — never act on stale data
                # or slide to a worse-ranked target; hold and retry next tick.
                # Priority rotation also requires fresh target data for escapes,
                # but an unusable active account must not be stranded by one
                # stale candidate when a lower-ranked fresh target can take over.
                entry = entries.get(num)
                if entry is None or not entry.fresh(self.clock()):
                    self._emit(
                        NoSwitchEvent(
                            reason="stale-usage",
                            detail=(
                                f"account {num} usage could not be refreshed "
                                "this tick (backoff or a concurrent poller); "
                                "retrying"
                            ),
                            account=num,
                        )
                    )
                    if priority_mode and not priority_active_ready:
                        continue
                    return TickOutcome.NO_ACTION
            if self.dry_run:
                # Dry-run stops at the decision: no token refresh, no
                # quarantine writes — freshening is a mutation.
                return self._perform(num, email, trigger, left_snapshot)
            status = self._freshen_target(num, email)
            if status == "identity-conflict":
                # The slot's credential is alive but belongs to a different
                # account — switching onto it would silently run the wrong
                # account. Quarantine (auto-released once a re-add replaces
                # the credential).
                self._quarantine(num, email, "identity-conflict")
                continue
            if status == "invalid_grant":
                self._quarantine(num, email, "invalid_grant")
                continue
            if status == "transient":
                transient_failure = True
                continue
            if status in _SYSTEMIC_STATUSES:
                # ONE cause is reported, so it must be the one worth acting
                # on. Assigning unconditionally made it the LAST candidate's,
                # and `consume-busy` clears itself on the next pass while the
                # other two need a human — unset an env var, chase a rejected
                # client_id. So a busy slot sorting after an unmirrored one
                # named the harmless cause and hid the real one: exactly the
                # "reads as intermittent, nothing names it" trap these kinds
                # were split out of "transient" to escape.
                if not systemic or _SYSTEMIC_STATUSES.index(
                    status
                ) < _SYSTEMIC_STATUSES.index(systemic):
                    systemic = status
                continue
            if status == "skip-live-session":
                continue
            return self._perform(num, email, trigger, left_snapshot)

        if systemic or transient_failure:
            self._emit(
                ErrorEvent(
                    message="could not freshen: " + _SYSTEMIC_MESSAGES[systemic]
                    if systemic
                    else "could not freshen any candidate (network?)",
                    transient=True,
                )
            )
            return TickOutcome.ERROR
        self._emit(NoSwitchEvent(reason="no-viable-target"))
        return TickOutcome.BLOCKED

    def _no_return_account(
        self,
        trigger: str,
        state: dict,
        headroom: dict[str, float | None],
        active_headroom: float | None,
        recovered: bool,
        settings: AutoSwitchSettings,
        current: str | None = None,
        *,
        usage: dict[str, dict | str | None],
        # Optional so a caller with no per-account context (and every
        # pre-existing test that exercises this helper directly) keeps
        # today's exact semantics: one global line for every account. The
        # engine's own tick always passes the resolved map explicitly.
        thresholds: Mapping[str, float] | None = None,
    ) -> str | None:
        """The account this engine most recently left, while it is still barred.

        NEVER UNDO THE PREVIOUS MOVE. Each anti-flap margin is measured against
        the ACTIVE, and burn changes the active, so a burning pair can clear the
        margin in both directions. Measured, only the active burning:
        `[1, 2, 1, 2]` where one move was due.

        SCOPED like every sibling gate — `at-limit` and `failover` skip the
        anti-flap gates by design. Unscoped this stranded a 2-account fleet on
        an exhausted active with the peer at 0%.

        AND SCOPED TO THE ENGINE'S OWN LANDING (`lastSwitchTo == current`).
        The bar refuses to undo THIS ENGINE'S last move; once the user
        switches by hand the engine is no longer sitting where it put itself
        and that move is already undone, so the bar protects nothing and
        merely withholds the fleet's best account. Reproduced: engine 1 -> 2,
        user 2 -> 3 by hand, account 1 on 4 pts still barred against an
        active on 2, every reset far out, no release leg reachable — the
        `not recovered` return below fires before the ratio leg is read, and
        `recovered` is False because account 1 was 4 pts at departure too.
        The engine then holds the worse account until the at-limit escape.
        Both sides are `str`-normalised: `lastSwitchTo` is a `str` from
        `_perform` and `lastSwitchFrom` an `int` from `account_ref`. A state
        record written before `lastSwitchTo` existed has no such key and
        cannot prove the engine moved away, so it KEEPS the bar — the same
        conservative reading this module gives every other missing field, and
        the only one that does not silently drop the anti-flap bound for an
        upgrade cycle.

        RELEASED when the account we left now beats us by the margin the
        all-above ranking uses (`_all_above_margin_met`): that is not the flip
        this bars, it is a move the outbound leg would have made on its own
        merits — BUT ONLY IF IT HAS ACTUALLY RECOVERED. The ratio compares
        against the ACTIVE, and the active burns, so ungated it comes true on
        a target that has done nothing. Measured on the cited walk, the barred
        account held 4.0 pts at departure and 4.0 pts at every return; the
        ratio fired purely because the active fell to 2.0, which is the flap
        arriving through the release instead of through the ranking.
        `recovered` is that gate, computed once per snapshot by
        `_left_account_recovered` and shared with the leaves-nothing retry in
        `_rank` so the two cannot disagree.

        THE LEAVES-NOTHING RELEASE IS NOT HERE. It used to be, and it was
        rewritten twice for the same reason both times: this function cannot
        see the gates that decide. `lastSwitchFrom` is rewritten only by a
        successful switch — the one thing the bar prevents — so a bar that
        empties the ranking is permanent, and each attempt to predict emptiness
        landed one gate short of the ranking loop:

            all(n == barred ...)              "does another account EXIST"
            (headroom.get(n) or 0.0) > 0.0    "is another account not at its limit"

        The loop also applies each candidate's own lines and the all-above
        margin (`_all_above_margin_met`). A third account on ONE point clears
        both predicates above and neither of those, so the n=2 stall came back
        at n>=3.

        `_rank` now ASKS the ranking instead: it ranks with the bar, and
        re-ranks without it when the result is empty. That is exact by
        construction, and cannot be one gate behind because it is not a
        separate copy of the gates.

        EMPTINESS ALONE IS NOT THE RELEASE, though — see `_rank`. At n=2 the
        barred ranking is always empty, so an emptiness-only retry is a no-op
        at exactly the fleet size the flap was reported on. Both the retry and
        the ratio above are gated on `recovered`, which is the one question the
        ranking cannot answer: is this a different account from the one we
        left, or only a different active?
        """
        if thresholds is None:
            thresholds = ThresholdMap({}, settings.threshold)
        came_from = state.get("lastSwitchFrom")
        if trigger not in ("proactive", "consume-first") or came_from is None:
            return None
        # Only while we are still standing where that switch put us. A manual
        # switch away already undid the move, so there is nothing left to
        # refuse to undo. `str` on both sides: `lastSwitchTo` is written from
        # `_perform`'s `number: str`, `lastSwitchFrom` from `account_ref`'s
        # `number: int`, and an int/str mismatch here would disarm the bar
        # everywhere rather than only after a hand switch.
        landed_on = state.get("lastSwitchTo")
        if landed_on is not None and current is not None:
            if str(landed_on) != str(current):
                return None
        # No membership check on `oauth_candidates`: the loop compares
        # `num == no_return` while iterating that same list, so naming an
        # account that is not in it bars nothing. The check was a no-op and
        # nothing killed it under mutation.
        barred = str(came_from)
        if not recovered:
            return barred        # the ratio below burns true on its own; see above
        left_headroom = headroom.get(barred)
        if left_headroom is not None:
            if active_headroom is not None:
                if _all_above_margin_met(left_headroom, active_headroom):
                    return None               # beats us outright; not a flip
            elif settings is not None:
                # An unreadable active must not be silently scored as "the
                # peer does not beat it" -- same landing-eligible fallback
                # `_left_account_recovered` uses when it, too, has no active
                # to compare against: would the ranking accept the peer as a
                # landing spot, i.e. is it under every one of its own lines?
                left = self._binding(usage.get(barred), settings, thresholds[barred])
                if left is not None and not left.over:
                    return None
        return barred

    def _left_account_recovered(
        self,
        state: dict,
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
        current: str | None = None,
        *,
        # Optional so a caller with no per-account context (and every
        # pre-existing test that exercises this helper directly) keeps
        # today's exact semantics: one global line for every account. The
        # engine's own tick always passes the resolved map explicitly.
        thresholds: Mapping[str, float] | None = None,
    ) -> bool:
        """Is the account we left a better proposition than when we left it?

        This is the release the bar needs and the ranking cannot supply. A bar
        that leaves nothing is a stall, but "leaves nothing" is also what every
        flap looks like on two accounts, so lifting on emptiness alone lifts
        always. The distinction is not in the present state — it is between the
        present and the moment of departure, which is why `_perform` records
        that moment (`leftHeadroom` / `leftRecoveryAt`) alongside
        `lastSwitchFrom`.

        Measured on the walk this guard exists for, the barred account was
        IDENTICAL at every return — same headroom, same reset — and only the
        active had changed. That is the flap: the ranking flipped underneath a
        target that did nothing.

        FAILOVER FIRST, before any leg reads the active — checked ahead of
        dominance, because dominance-first starves this branch:
        `test_a_failover_departure_does_not_disarm_the_bar` broke on its
        FIRST tick the instant the active fell far enough for `4.0 > active
        x 2` to go true (measured at active=1.8, one fifth of a point past
        the boundary this branch's own sibling test already sits on).
        A `(None, None)` snapshot means severity was genuinely unmeasured at
        departure — there is no `leftHeadroom` to diff against and never was
        — so the two signals that do not depend on the active's LIVE state
        are (1) whether the peer, right now, would itself be a healthy place
        to land: under every one of its own switch lines (`not
        binding.over`), the same "would the ranking accept this as a landing
        spot" test the landing gate in `_rank_candidates` already runs on
        every candidate, reused rather than inventing a fresh
        constant; and (2), when the landing floor cannot answer, whether the
        peer's own binding reset is meaningfully sooner than the active's.
        The landing floor is the exact complement of
        `_every_account_above_threshold`, so it is UNSATISFIABLE whenever
        the
        fleet is all-spent — the recovery leg is what keeps the hold from
        becoming unconditional in exactly that regime. Neither leg can tell
        "genuinely recovered" from "was already this good" — there is
        nothing recorded to tell them apart. Measured which side the landing
        floor should land on: sweeping mutations of this same leg for the
        ordinary path showed an absolute floor is silently reintroducible
        with a green suite, so this is not free of that risk either — the
        difference is this constant is the user's own switch lines (the
        peer's effective threshold — its per-account override, else
        `settings.threshold` — and `settings.weekly_threshold`), not a
        hardcoded number, so a
        user's OWN policy decides how conservative the hold is, and it moves
        when they change it (pinned directly, below). Deliberately MORE
        conservative than the ordinary path below: a peer sitting at 4 points
        held through the whole walk that broke a bare dominance leg, with no
        upper bound short of the peer crossing the threshold itself OR its
        binding reset pulling meaningfully ahead of the active's. Bounded,
        not permanent — at-limit still escapes untouched
        (`_no_return_account` scopes this trigger out entirely), and the
        recovery leg means the bound is no longer just "the active reaches
        its own hard limit": a peer that resets first releases the hold on
        its own schedule, without the active ever needing to burn down to it.

        THE ORDINARY PATH HAS A REAL BASELINE (`leftHeadroom` is a number,
        not null), so it gets three legs, checked in this order, each with
        the margin its axis already uses. Burn cannot manufacture any of
        them: headroom rises only when a window rolls over, the ratio needs
        the active to lose more than half its remaining headroom AND clear
        an extra `SPENT_HEADROOM_PCT` on top, and the binding reset moves
        nearer only when a nearer window starts binding.

          dominance   `> active x HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT`
                      against the ACTIVE. A peer moved AWAY from for a reason
                      other than headroom (e.g. consume-first's reset
                      ordering) can dominate the active from the moment it
                      was left, and self-improvement against its own
                      departure baseline never fires for an account that had
                      nothing to improve on. The `+SPENT_HEADROOM_PCT` on top
                      of the bare ratio is what the bare ratio misses:
                      measured on this branch's own flap fleet (peer frozen
                      4.0 pts, active burning 98.0% -> 98.4%), it flips
                      true at active=1.8 pts purely because the active kept
                      burning; the same walk with the margin added stays
                      false through active=1.6 and only opens once the active
                      is down to a genuine sliver (`x < 0.5`), which is the
                      at-limit escape's territory, not a return worth calling
                      anti-flap. `test_a_burn_walk_never_returns_to_what_it_
                      left` still settles with the margin in place — it makes
                      the boundary harder to cross, not impossible.
          headroom    `+SPENT_HEADROOM_PCT` against the DEPARTURE baseline
                      — below that an edge is under two poll intervals.
          recovery    `-RECOVERY_HYSTERESIS_S` against the DEPARTURE
                      baseline — longer than one poll cycle, so jitter on a
                      reset timestamp is not a recovery.

        NO SNAPSHOT MEANS RELEASE. State written before this field existed, or
        by a switch that never recorded one, carries no evidence either way —
        and of the two failure modes the permanent proactive lockout is the
        worse one, because it is persisted and survives a restart and a week of
        wall clock. Absence of evidence releases. This only gates the
        proactive/consume-first return; `_no_return_account` scopes at-limit
        and failover out of the bar by design, so either trigger still
        escapes the account untouched, and the next successful switch
        overwrites the snapshot outright.
        """
        if thresholds is None:
            thresholds = ThresholdMap({}, settings.threshold)
        came_from = state.get("lastSwitchFrom")
        if came_from is None:
            # Unreachable through `_no_return_account`, the only caller: it
            # returns `None` at its own `came_from is None` check before
            # `recovered` is ever read. Kept `True` (not load-bearing) so a
            # future direct caller gets "no evidence, release" rather than a
            # silent hold.
            return True
        barred = str(came_from)
        if "leftHeadroom" not in state:
            return True          # pre-upgrade record: genuinely no evidence
        h = headroom.get(barred)
        left_headroom = state.get("leftHeadroom")
        left_recovery = state.get("leftRecoveryAt")
        # A `consume-first` departure can ALSO write (None, None) -- the
        # same shape a real failover writes -- whenever the phase-2 refetch's
        # active row is unmeasurable for headroom but still has a known
        # weekly reset (the split shape `oauth.
        # build_usage_result` emits for `utilization: null` plus a
        # `resets_at`). Inferring "failover" from the two nulls then ran the
        # more permissive failover legs (landing floor + recovery-only) on
        # what was really an ordinary departure -- reachable on 32%+ of
        # swept fleets, both directions. `leftTrigger` records the actual
        # trigger so this never has to guess again; a record written before
        # this field existed has no such key, so fall back to the old
        # two-null inference for it (unchanged behaviour for pre-upgrade
        # state).
        left_trigger = state.get("leftTrigger")
        is_failover_snapshot = (
            left_trigger == "failover"
            if left_trigger is not None
            else (left_headroom is None and left_recovery is None)
        )
        if is_failover_snapshot:
            # Failover: real departure, severity unmeasured at the time --
            # not absence of evidence, and there is no baseline to diff
            # against (that is exactly what "unmeasured" means), so this
            # cannot use the active's HEADROOM at all -- see docstring for
            # why reading the active's headroom here is wrong, and the walk
            # that proved it. Two legs, both read-only against CURRENT state
            # (no departure baseline exists to diff against):
            #
            #   landing   `not peer.over` -- under every one of its own
            #             lines: would the ranking accept this peer as a
            #             landing spot right now (the landing gate in
            #             `_rank_candidates`)?
            #   recovery  the peer's binding reset is meaningfully sooner
            #             than the ACTIVE's binding reset. Needed because
            #             `landing` is the exact complement of `_every_
            #             account_above_threshold`: whenever the fleet is
            #             all-spent, `landing` is unsatisfiable by
            #             construction, so without this leg the bar would
            #             hold until the active hit its limit. Releasing the
            #             bar does not choose the peer; the room ranking and
            #             its margin still decide.
            #
            # Burn cannot fake the recovery leg: a reset moves nearer only
            # when a nearer window starts binding, never as a side effect
            # of the active spending down -- the failure mode a bare
            # dominance leg has, guarded directly in the mutation table.
            peer = self._binding(usage.get(barred), settings, thresholds[barred])
            if peer is not None and not peer.over:
                return True
            peer_recovery_ts = _binding_recovery_ts(
                usage.get(barred),
                self._models,
                now,
                threshold=thresholds[barred],
                weekly_threshold=settings.weekly_threshold,
                scale=settings.threshold,
            )
            active_recovery_ts = _binding_recovery_ts(
                usage.get(current),
                self._models,
                now,
                threshold=thresholds[current],
                weekly_threshold=settings.weekly_threshold,
                scale=settings.threshold,
            )
            # The active's recovery must be a REAL measurement, not merely
            # "larger" -- `_binding_recovery_ts` returns `inf` for both
            # "never resets" and "we do not know" (no windows, no
            # `resets_at`, or a stale/past `resets_at`). Reading `inf` as
            # "never" here made `peer < inf - HYST` true for ANY finite
            # peer reset, releasing onto a peer arbitrarily far out on no
            # evidence. `math.isfinite` requires the active to have a
            # genuine, known reset before the comparison even runs --
            # unknown holds, exactly like unreadable already does on the
            # headroom axis.
            #
            # But two of the five `inf` states are ordinary shapes for an
            # active that is plainly alive and burning -- a `pct` reported
            # with no `resets_at`, or a `resets_at` already elapsed -- not
            # unknowns. Reading all five as "unknown, hold" pins the engine
            # on a near-spent active for up to a full window even when the
            # peer is back within `RECOVERY_HORIZON_S`. Requiring EITHER a known
            # active reset OR a peer inside that horizon keeps `isfinite`'s
            # intended release (a known active vs. an arbitrarily-far peer
            # still needs `isfinite`) while letting a near peer through
            # regardless of why the active's own reset reads `inf`.
            return (
                (
                    math.isfinite(active_recovery_ts)
                    or peer_recovery_ts - now <= RECOVERY_HORIZON_S
                )
                and peer_recovery_ts < active_recovery_ts - RECOVERY_HYSTERESIS_S
            )
        # Dominance over the ACTIVE, only reached once a real baseline is
        # confirmed to exist above -- a peer that was already miles ahead of
        # the active at departure (moved for a DIFFERENT reason -- e.g.
        # consume-first's reset ordering, not headroom) never "improves" on
        # its own baseline and would stall on self-improvement alone despite
        # dominating throughout.
        #
        # `active_headroom is None` means "we could not read the active this
        # tick" -- reachable through the consume-first two-phase commit,
        # which reassigns `active_headroom` from a fresh
        # refetch without re-classifying the trigger. That is a DIFFERENT
        # state from "readable, but does not dominate" and must not answer
        # the same way: fall back to the landing-eligible test the failover
        # branch above uses when IT has no baseline to compare against
        # either, rather than silently treating "unreadable" as "no
        # dominance".
        #
        # DEFENSIVE, not currently outcome-changing: measured exhaustively,
        # `active_headroom=None` on this path closes
        # BOTH of `_rank_candidates`'s gates before this leg is ever asked
        # (`_every_account_above_threshold` is False on a None active, and
        # the consume-first `active_reset_ts` gate is None too), so no
        # fleet shape has been found where this branch changes a `tick()`
        # outcome. Kept because the call IS reachable (confirmed via the
        # phase-2 refetch) and a future change to those gates could make it
        # live without anyone revisiting this function -- silently reading
        # None as "no dominance" would then be exactly the bug the
        # unreadable-active fallback was added for.
        if h is not None:
            if active_headroom is not None:
                if h > active_headroom * HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT:
                    return True
            else:
                peer = self._binding(
                    usage.get(barred), settings, thresholds[barred]
                )
                if peer is not None and not peer.over:
                    return True
        if (
            isinstance(left_headroom, (int, float))
            and h is not None
            and h >= min(left_headroom + SPENT_HEADROOM_PCT, 100.0)
        ):
            return True
        # `None` is the JSON-safe spelling of "unknown or already past", which
        # `_binding_recovery_ts` returns as `inf`: an account nobody can
        # schedule around. Moving off it onto a real reset IS the improvement.
        was = left_recovery if isinstance(left_recovery, (int, float)) else float("inf")
        return (
            _binding_recovery_ts(
                usage.get(barred),
                self._models,
                now,
                threshold=thresholds[barred],
                weekly_threshold=settings.weekly_threshold,
                scale=settings.threshold,
            )
            < was - RECOVERY_HYSTERESIS_S
        )

    def _priority_ready(
        self, value: dict | str | None, headroom: float | None,
        settings: AutoSwitchSettings, threshold: float,
    ) -> bool:
        """Priority rotation never lands at or above any configured line."""
        binding = self._binding(value, settings, threshold)
        return (
            headroom is not None and headroom > 0
            and binding is not None and not binding.over
        )

    def _rank_priority_candidates(
        self, *, oauth_candidates: list[str],
        usage: dict[str, dict | str | None], headroom: dict[str, float | None],
        current: str, active_enabled: bool, settings: AutoSwitchSettings,
        now: float, thresholds: Mapping[str, float],
        policies: Mapping[str, AccountPolicy],
    ) -> tuple[list[str], bool, float | None]:
        """Highest usable tier, then earliest weekly reset, with stable ties.

        Pure snapshot ranking, also used after the pre-switch refresh. Unknown
        reset times sort last within a tier; an eligible active account wins
        equal keys, so changing usage alone cannot cause ping-pong. Unlike the
        existing strategies, even at-limit/failover never selects an account
        at its switch line. Priorities do not bypass eligibility checks.
        """
        def key(num: str) -> tuple[int, float]:
            reset = _seven_day_reset_ts(usage.get(num), now)
            return (-policies.get(num, AccountPolicy()).priority,
                    reset if reset is not None else float("inf"))

        active_key = (
            key(current)
            if active_enabled and self._priority_ready(
                usage.get(current), headroom.get(current), settings, thresholds[current]
            ) else None
        )
        ordered = [
            num for num in oauth_candidates
            if self._priority_ready(usage.get(num), headroom.get(num), settings, thresholds[num])
            and (active_key is None or key(num) < active_key)
        ]
        ordered.sort(key=key)  # Sequence order breaks candidate ties.
        return (
            ordered,
            any(headroom.get(num) is not None for num in oauth_candidates),
            _seven_day_reset_ts(usage.get(current), now),
        )

    def _rank_candidates(
        self,
        *,
        trigger: str,
        consume_first: bool,
        oauth_candidates: list[str],
        no_return: str | None,
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        current: str,
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
        # See `_no_return_account`: optional, defaulting to the global line
        # for every account, so a caller without per-account context behaves
        # exactly as this function did before per-account thresholds existed.
        thresholds: Mapping[str, float] | None = None,
    ) -> tuple[list[str], bool, float | None]:
        """Filter and rank OAuth candidates for this tick's trigger.

        Returns ``(ordered, any_known, active_reset_ts)``. Pure — no emits,
        no state writes — so the consume-first two-phase commit can run it
        twice per tick: on the stored snapshot to decide provisionally, then
        on the escalated refetch to re-verify before switching.
        """
        if thresholds is None:
            thresholds = ThresholdMap({}, settings.threshold)
        # weekly-first is `best` with consume-first's ranking: the same
        # departure line, landing gates, hysteresis and escapes as `best`, but
        # a proactive move picks the eligible account whose weekly window
        # resets soonest, since its unused weekly quota is lost first.
        weekly_first = settings.strategy == "weekly-first"
        # consume-first ranks by soonest weekly reset; a proactive (below-
        # threshold) target must reset strictly sooner than where we are.
        active_reset_ts = (
            _seven_day_reset_ts(usage.get(current), now) if consume_first else None
        )
        # One binding window per account under THIS snapshot's lines — the
        # consume-first phase-2 refetch hands in fresh `usage`, so nothing the
        # trigger computed is reused here.
        bindings = {
            num: self._binding(usage.get(num), settings, thresholds[num])
            for num in (*oauth_candidates, current)
        }
        active_binding = bindings[current]
        # Policy headroom against the active's own lines, for the hysteresis
        # gate and the sort keys below; raw when it reports no window.
        active_lh = (
            active_binding.line_headroom
            if active_binding is not None
            else active_headroom
        )
        # When NOTHING is under its lines — the active account and every
        # candidate each at or over its own switch lines — "land somewhere
        # healthy" has no answer, and holding out for one costs the user the
        # session. Sitting still means burning the active account to 100% and
        # taking a hard limit with a peer that still has room never tried. So
        # in that state the landing rule is dropped and every strategy ranks
        # by the most room left before a limit: RAW headroom, `100 - max pct`
        # over the 5h, 7d and configured per-model windows, whatever lines
        # they carry.
        #
        # Not by soonest reset. Ranking a nearly spent account ahead of one
        # with room because its window rolls over first lands on it at 98-99%,
        # where it hits its limit within a poll or two, and holds the active
        # there too while a peer that resets sooner is preferred to one with
        # real room. Measured over 32h of a 28-account fleet: every one of 18
        # switches onto a target already at 95% or more came through that
        # ranking. A reset only breaks an exact room tie. When every
        # candidate is exhausted nothing qualifies, and the all-exhausted
        # sleep wakes at the earliest reset, where that account has the most
        # room.
        #
        # Not by line headroom either. Past its lines an account's line
        # headroom is its room rescaled by how much room its line reserved,
        # so with per-account lines (global 90) a 96% account on a 95 line
        # scores 8 against an 85% account on an 80 line's 7.5, and the
        # ranking lands on the one four points from a hard limit: the failure
        # above, through a different door. Every account here has crossed the
        # lines that make line headroom meaningful, and the anti-flap margin
        # is raw (`_all_above_margin_met`), so the key is raw too, keeping
        # the filter and the sort on one axis. The cost is that a weekly point
        # counts the same as a five-hour one, though it is more work; the
        # engine has no exchange rate between windows, and a line is a policy
        # on when to leave, not a measure of capacity.
        #
        # Deliberately narrow. It engages only when every measured OAuth
        # candidate is at/over its own lines, so a single peer still under
        # them wins the normal way. A proactive move needs the active over
        # its lines too; the escapes leave whatever the active reads, and
        # failover cannot read it at all, so for them the candidates alone
        # decide.
        over_by_account = {
            num: (b.over if b is not None else None) for num, b in bindings.items()
        }
        if trigger in ("at-limit", "failover"):
            all_above = _every_candidate_above_threshold(
                oauth_candidates, over_by_account
            )
        else:
            all_above = _every_account_above_threshold(
                oauth_candidates,
                over_by_account,
                active_binding.over if active_binding is not None else None,
            )

        qualifying: list[tuple[tuple, str]] = []
        any_known = False
        for num in oauth_candidates:
            h = headroom.get(num)
            if h is None:
                continue
            any_known = True          # it EXISTS and is readable either way
            if h <= 0:
                continue  # itself at its limit — never a target
            if num == no_return:
                continue  # the account we just left; see _no_return_account
            binding = bindings[num]
            # Policy headroom against the candidate's own lines, and whether
            # any of them is reached; raw `h` / the single line when it
            # reports no window (a readable headroom rules that out).
            lh = binding.line_headroom if binding is not None else h
            over = (
                binding.over
                if binding is not None
                else (100.0 - h) >= thresholds[num]
            )
            reset_ts = (
                _seven_day_reset_ts(usage.get(num), now)
                if consume_first or weekly_first
                else None
            )
            if trigger in ("proactive", "consume-first"):
                # Landing must be healthy: an account at/over any of its own
                # lines would re-trigger on the very next tick. At-limit and
                # failover are escapes that skip this whole block — any account
                # with real headroom beats a blocked or dead one.
                if over and not all_above:
                    continue
                if all_above:
                    # Checked before the strategies, because with nothing under
                    # its lines the strategy question is moot: consume-first
                    # exists to spend perishable WEEKLY quota, and every account
                    # here is about to be blocked on a window. Both strategies
                    # want the account with the most room, so both take this
                    # gate and the matching key below. (Ordering matters: `if
                    # consume_first` catching first filtered on weekly ordering
                    # while the key sorted on room, two different axes, and
                    # left consume-first users with no anti-flap guard at all.)
                    if not _all_above_margin_met(h, active_headroom or 0.0):
                        continue
                elif consume_first:
                    # Purely proactive on reset ordering: below the threshold,
                    # only move to accounts whose weekly window resets sooner
                    # than the active one (above the threshold we must move, so
                    # any healthy account qualifies and the sort picks soonest).
                    if trigger == "consume-first" and (
                        reset_ts is None
                        or active_reset_ts is None
                        or reset_ts >= active_reset_ts
                    ):
                        continue
                elif active_headroom is not None:
                    # best: the candidate must beat the active account by the
                    # full hysteresis margin (a one-way move like 99%→89%
                    # qualifies; near-line pairs can't flap back). Measured in
                    # policy headroom, so a 7d 93% candidate under a 97 line
                    # beats a 5h 95% active under 85 — raw headroom (7 vs 5)
                    # would park on the account about to hit its limit.
                    if lh - active_lh < settings.hysteresis_pct:
                        continue
            if all_above:
                # Every trigger, escapes included: at-limit and failover skip
                # the gate above, but an escape sorting by consume-first's
                # weekly key here lands on whichever spent account resets
                # first, and one sorting by line headroom on whichever sits on
                # the highest line. The reset breaks an exact room tie, where
                # sooner is plainly better than a lower slot number. It is the
                # reset of the window the room is measured on, the fullest one
                # (no lines passed), since its rollover is what gives the
                # account room again.
                key: tuple = (
                    -h,
                    _binding_recovery_ts(usage.get(num), self._models, now),
                )
            elif consume_first or (weekly_first and trigger == "proactive"):
                # Soonest weekly reset first (unknown resets sort last), most
                # policy headroom breaks ties, then sequence order. weekly-first
                # keeps `best`'s headroom order for the at-limit and failover
                # escapes, which may land on accounts over their lines.
                key = (reset_ts if reset_ts is not None else float("inf"), -lh)
            else:
                key = (-lh,)
            qualifying.append((key, num))
        # Ascending by the strategy's key; list order (sequence order) breaks ties.
        qualifying.sort(key=lambda t: t[0])
        return [num for _, num in qualifying], any_known, active_reset_ts

    # -- adaptive usage scheduling ---------------------------------------------

    def _collect_scheduled_usage(
        self,
        current: str,
        quarantined: set[str] = frozenset(),
        *,
        threshold: float | None = None,
        weekly_threshold: float | None = None,
    ) -> tuple[dict, dict[str, dict | str | None], dict[str, float | None]]:
        """Two-phase usage collection with an O(1) baseline.

        Phase A fetches the active account (when its persisted poll plan says
        it is due — poll_policy's urgent mode is what tightens that cadence
        near the band) plus ONE due candidate (the one with the stalest data
        — never-fetched first, then oldest fetch); everyone else is served
        from the usage store. Phase B refetches ALL candidates and recomputes
        before any switch decision when a switch could be near: one of the
        active account's windows within ``ESCALATION_MARGIN_PCT`` of its own
        switch line, or active usage unknown (failover must not run on stale
        candidate data).
        At-limit, proactive, and ordinary unknown-usage failover selection
        never runs on the pre-escalation snapshot — those triggers imply the
        escalation condition (the deliberate exception: an owned-and-expired
        active is excluded above, so a post-idle-hold failover can run
        without escalating). The consume-first trigger can fire outside the
        escalation band, so it instead decides *provisionally* on the stored
        snapshot and, only when a switch would fire, re-runs an escalated
        collection and re-verifies the choice in ``_tick_inner`` (two-phase
        commit), plus a per-target ``UsageEntry.fresh`` gate before
        performing.

        Stalest-first needs no rotation cursor: it reads the persisted store,
        so the loop and cron-driven ``--once`` runs schedule identically.
        Backoff (``backoffUntil``) is enforced by the collector even for the
        active account — a Retry-After must never be defeated — and during an
        idle-hold no candidate is polled at all (slow crawl for everything).
        Adapted cadences are persisted by the collector itself after each
        fetch (shared with every other surface), not by the engine.

        Returns ``(entries, usage, headroom)`` where ``usage`` carries
        decision values and ``headroom`` the derived headroom per account.
        """
        now = self.clock()
        # Quarantined accounts can never be switch targets, so spending the
        # single alternate poll slot (or an escalation fetch) on one is wasted.
        candidates = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n != current and n not in quarantined
        ]

        pre = self.switcher.usage_entries_by_account(fetch=set())
        plan: set[str] = set()
        active_pre = pre.get(current)
        # The active account is nominated when never fetched, poll-due per its
        # persisted plan, or (no plan yet) past the normal cadence floor. The
        # collector's reserve() honors due-ness even inside the serve TTL, so
        # an urgent plan (60s while burning near the band) actually fetches.
        # A candidate-style plan (slower than any active plan can be) left
        # over from a role change the switcher never saw (e.g. a manual
        # login) is overridden past the active age cap. Exhausted accounts
        # carry their own bounded plan and become due normally.
        stale_candidate_plan = (
            active_pre is not None
            and active_pre.age_s is not None
            and active_pre.age_s >= poll_policy.ACTIVE_MAX_INTERVAL_S
            and (active_pre.poll_interval_s or 0.0)
            > poll_policy.ACTIVE_MAX_INTERVAL_S
            and (binding_pct(active_pre.last_good, self._models) or 0.0) < 100.0
        )
        overslept_plan = (
            active_pre is not None
            and plan_oversleeps_interval(active_pre, now)
        )
        if (
            active_pre is None
            or active_pre.age_s is None
            or stale_candidate_plan
            or overslept_plan
            or (
                active_pre.next_poll_at is not None
                and now >= active_pre.next_poll_at
            )
            or (
                active_pre.next_poll_at is None
                and active_pre.age_s >= poll_policy.MIN_INTERVAL_S
            )
        ):
            plan.add(current)
        if self._idle_hold_since is None:
            pick = due_candidate(candidates, pre, now)
            if pick is None:
                disabled = [
                    n
                    for n in self.switcher.disabled_account_numbers()
                    if n != current and n not in quarantined
                ]
                pick = due_candidate(disabled, pre, now)
            if pick is not None:
                plan.add(pick)
        entries = self.switcher.usage_entries_by_account(
            fetch=plan,
            # A candidate-style plan on the active slot is deliberately
            # overridden after the active age cap; every other baseline
            # nomination preserves a valid future plan under the store lock.
            scheduled=not stale_candidate_plan,
        )
        usage = {num: entry.decision_value() for num, entry in entries.items()}

        active_value = usage.get(current)
        active_headroom = oauth.account_headroom(
            active_value if isinstance(active_value, dict) else None, self._models
        )
        # The caller's tick-snapshotted lines, so one tick fetches and decides
        # on the same values even if apply_threshold() lands mid-tick. No
        # threshold means "the engine's own" for BOTH lines: None is a legal
        # weekly value, so it cannot mean "not passed".
        if threshold is None:
            threshold = self.settings.threshold
            weekly_threshold = self.settings.weekly_threshold
        # Any window of the active within the margin of ITS OWN line: a 5h
        # window closing on its line under a flat, higher weekly window is a
        # switch that is near, whatever the binding max says. With one line
        # this is exactly "binding utilization within the margin".
        near_a_line = any(
            pct >= window_line(label, threshold, weekly_threshold) - ESCALATION_MARGIN_PCT
            for label, pct, _ in oauth.relevant_windows(
                active_value if isinstance(active_value, dict) else None, self._models
            )
        )
        escalate = bool(candidates) and (
            (active_headroom is None and active_value != USAGE_TOKEN_EXPIRED)
            or (active_headroom is not None and near_a_line)
        )
        if escalate:
            escalation_fetch = {current, *candidates}
            # Escalation may beat ordinary candidate plans to obtain a fresh
            # switch decision, but a decision-trusted exhausted row cannot be
            # a target. Preserve any wider post-429 plan instead of refetching
            # that token at the bounded all-exhausted wake cadence.
            for num in tuple(escalation_fetch):
                entry = entries.get(num)
                value = usage.get(num)
                planned_headroom = oauth.account_headroom(
                    value if isinstance(value, dict) else None, self._models
                )
                if (
                    entry is not None
                    and entry.next_poll_at is not None
                    and now < entry.next_poll_at
                    and (entry.poll_interval_s or 0.0)
                    > poll_policy.EXHAUSTED_INTERVAL_S
                    and planned_headroom is not None
                    and planned_headroom <= 0
                ):
                    escalation_fetch.remove(num)
            entries = self.switcher.usage_entries_by_account(
                fetch=escalation_fetch
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}

        headroom = _headroom_by_account(usage, self._models)
        return entries, usage, headroom

    def _perform(
        self,
        number: str,
        email: str,
        trigger: str,
        left: tuple[float | None, float],
    ) -> TickOutcome:
        cause = self._switch_cause(number)
        if self.dry_run:
            current = self.switcher.current_account_number()
            current_email = self.switcher.account_email(current) if current else ""
            self._emit(
                SwitchEvent(
                    trigger=trigger,
                    from_ref=_ref(current, current_email) if current else None,
                    to_ref=_ref(number, email),
                    dry_run=True,
                    **cause,
                )
            )
            return TickOutcome.SWITCHED

        # Hold the state lock across the whole recheck -> switch -> record
        # sequence so two concurrent engines (loop + cron --once) make one
        # serialized decision: the loser re-reads the winner's lastSwitchAt
        # and backs off instead of double-switching. No deadlock cycle: the
        # switch path (qswap FileLock + Claude Code locks) never takes the
        # state lock.
        with self._state_lock():
            state = self._read_state()
            if trigger in ("proactive", "consume-first", "priority") and self._in_cooldown(state):
                self._emit(
                    NoSwitchEvent(reason="cooldown", wait_s=self._cooldown_left(state))
                )
                return TickOutcome.NO_ACTION

            try:
                result = self.switcher.switch_to(number, json_output=True)
            except SessionsRunningError as e:
                # Sessions started between the decision and the switch: the
                # same refusal the decision makes, and nothing changed.
                self._emit(_api_key_sessions_event(number, e.sessions))
                return TickOutcome.BLOCKED
            except RetryableSwitchError as e:
                # Nothing changed, and the provider says a later try is safe
                # (Codex: a running Codex may be renewing the live token right
                # now). Not an error, and nothing against the target: hold,
                # and the next tick asks again. Claude Code never raises it.
                self._emit(NoSwitchEvent(reason="switch-deferred", detail=str(e)))
                return TickOutcome.NO_ACTION
            if not result or not result.get("switched"):
                self._emit(
                    NoSwitchEvent(
                        reason="already-active",
                        detail=(result or {}).get("reason", ""),
                    )
                )
                return TickOutcome.NO_ACTION

            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["lastSwitchAt"] = self.clock()
            state["lastSwitchTo"] = number
            # WHERE we came from, so the next tick can refuse to undo this,
            # and WHAT IT LOOKED LIKE, so that refusal has a release that burn
            # cannot fake. See `_left_account_recovered` for why the present
            # state alone cannot supply one. `inf` is stored as null: it is not
            # portable JSON, and every other reader of this file would have to
            # learn about it.
            state["lastSwitchFrom"] = (result.get("from") or {}).get("number")
            state["leftHeadroom"], recovery = left
            state["leftRecoveryAt"] = None if recovery == float("inf") else recovery
            # A `consume-first` phase-2 refetch can write the SAME (None,
            # None) shape a `failover` departure writes, whenever the
            # refetched active row has a `pct` but is otherwise unmeasurable
            # in the same tick its weekly reset is known --
            # `account_headroom` needs a numeric `pct`, `_seven_day_reset_ts`
            # needs only `resets_at`. Inferring the trigger from the two
            # nulls then runs the wrong legs. Record it directly so the
            # reader never has to guess.
            state["leftTrigger"] = trigger
            atomic_write_json(self.state_path, state)

        self._emit(
            SwitchEvent(
                trigger=trigger,
                from_ref=result.get("from"),
                to_ref=result.get("to"),
                warnings=result.get("warnings", []),
                **cause,
            )
        )
        return TickOutcome.SWITCHED

    # -- helpers --------------------------------------------------------------

    def _binding(
        self, value: dict | str | None, settings: AutoSwitchSettings, line: float
    ) -> BindingWindow | None:
        """The window binding this account under the tick's lines (see
        ``lines.binding_window``), or None when it reports no window. ``line``
        is the account's effective threshold from the tick's map. Reads
        only ``self._models`` and the ``settings`` it is handed, never
        ``self.settings``: a tick snapshots its settings once, and the ranking
        helpers are exercised directly on bare engines."""
        return binding_window(
            oauth.relevant_windows(
                value if isinstance(value, dict) else None, self._models
            ),
            line,
            settings.weekly_threshold,
            scale=settings.threshold,
        )

    def _display_name(self) -> str:
        """The provider's product name, for event text shown for every provider."""
        return getattr(self.switcher, "display_name", None) or "Claude Code"

    def _add_command(self) -> str:
        """The qswap command that captures the live login, for event text.
        A provider may name its own (``add_command``); Claude Code's is the
        default."""
        return getattr(self.switcher, "add_command", None) or "qswap --add-account"

    def _in_cooldown(self, state: dict) -> bool:
        last = state.get("lastSwitchAt")
        if not isinstance(last, (int, float)):
            return False
        return (self.clock() - last) < self.settings.cooldown_seconds

    def _cooldown_left(self, state: dict) -> float | None:
        """Seconds of cooldown still to run, for the no-switch text."""
        last = state.get("lastSwitchAt")
        if not isinstance(last, (int, float)):
            return None
        return max(0.0, self.settings.cooldown_seconds - (self.clock() - last))

    # -- human text ----------------------------------------------------------
    #
    # What the events' words need beyond their JSON: aliases, the provider,
    # and how each account stood this tick. Best-effort and read-only: a
    # failure here costs a label, never a tick, and no decision reads it.

    def _provider_id(self) -> str:
        provider = getattr(self.switcher, "provider_id", None)
        return provider if isinstance(provider, str) and provider else "claude"

    def _alias_names(self) -> dict[str, str]:
        """Account number -> alias, read once per tick."""
        lister = getattr(self.switcher, "list_aliases", None)
        if lister is None:
            return {}
        try:
            return {
                str(row[0]): str(row[1])
                for row in lister()
                if isinstance(row, (tuple, list)) and len(row) >= 2 and row[1]
            }
        except Exception as e:  # labels only; never cost the tick
            _logger.debug("could not read aliases for the auto log: %r", e)
            return {}

    def _switch_cause(self, number: str) -> dict:
        """The human-only facts a ``SwitchEvent`` quotes as its reason."""
        cause = self._tick_cause
        priority_up = None
        policies = cause.get("policies")
        current = self._context.active if self._context is not None else None
        if isinstance(policies, Mapping) and current is not None:
            mine = policies.get(current, AccountPolicy()).priority
            theirs = policies.get(number, AccountPolicy()).priority
            priority_up = theirs > mine
        return {
            "binding": cause.get("binding"),
            "over_budget": bool(cause.get("over_budget")),
            "priority_up": priority_up,
        }

    def _account_views(
        self,
        entries: Mapping[str, object],
        usage: Mapping[str, dict | str | None],
        state: dict,
        current: str,
        thresholds: Mapping[str, float],
        settings: AutoSwitchSettings,
    ) -> tuple[AccountView, ...]:
        """How every managed account stands on this tick, for human text:
        what the per-account comparison and a blocked tick's reasons say."""
        try:
            quarantine = state.get("quarantine")
            quarantine = quarantine if isinstance(quarantine, dict) else {}
            switchable = set(self.switcher.switchable_account_numbers())
            lister = getattr(self.switcher, "disabled_account_numbers", None)
            disabled = set(lister()) if lister is not None else set()
            names = self._context.names if self._context is not None else {}
            numbers = sorted(
                {str(n) for n in (*entries, *switchable, *disabled)},
                key=lambda n: (0, int(n)) if n.isdigit() else (1, n),
            )
            return tuple(
                self._account_view(
                    num, entries.get(num), usage.get(num), quarantine,
                    switchable, disabled, current, thresholds, settings, names,
                )
                for num in numbers
            )
        except Exception as e:  # labels only; never cost the tick
            _logger.debug("could not describe accounts for the auto log: %r", e)
            return ()

    def _account_view(
        self,
        num: str,
        entry: object,
        value: dict | str | None,
        quarantine: Mapping,
        switchable: set[str],
        disabled: set[str],
        current: str,
        thresholds: Mapping[str, float],
        settings: AutoSwitchSettings,
        names: Mapping[str, str],
    ) -> AccountView:
        base = AccountView(
            number=num,
            email=self.switcher.account_email(num) or "",
            alias=names.get(num, ""),
            line=thresholds[num],
            weekly_line=settings.weekly_threshold,
        )
        if num in quarantine:
            reason = quarantine[num].get("reason") if isinstance(quarantine[num], dict) else None
            return replace(base, state="needs-relogin", set_aside=str(reason) if reason else None)
        if value == USAGE_RELOGIN_REQUIRED:
            return replace(base, state="needs-relogin")
        if num != current:
            if num in disabled:
                return replace(base, state="disabled")
            if num not in switchable:
                return replace(base, state="no-login")
        if self.switcher.account_kind_for(num) == "api_key":
            return replace(base, state="api-key", used=bool(settings.include_api_key_accounts))
        age = getattr(entry, "age_s", None)
        if isinstance(value, dict):
            return replace(
                base,
                windows=tuple(_window_pcts(value, self._models).items()),
                age_s=age if isinstance(age, (int, float)) and age > STALE_OK_S else None,
            )
        if isinstance(value, str):
            return replace(base, state="sentinel", sentinel=value)
        error = getattr(entry, "last_error", None)
        last_good = getattr(entry, "last_good", None)
        if isinstance(last_good, dict):
            return replace(
                base,
                state="stale",
                windows=tuple(_window_pcts(last_good, self._models).items()),
                age_s=age if isinstance(age, (int, float)) else None,
                error=error,
            )
        if error:
            return replace(base, state="unavailable", error=error)
        return replace(base, state="not-read")

    def _warn_budgets(self, state: dict, entries: Mapping[str, object]) -> None:
        """One ``budget-warning`` per API-key account per UTC month, once its
        spend reaches ``BUDGET_WARN_FRACTION`` of its budget. Remembered in
        the state file (``budgetWarnings``: account number -> month) so a
        cron ``--once`` tick does not repeat it; dry-run writes nothing and
        remembers in memory only."""
        from ai_quota_swap.api_spend import BUDGET_WARN_FRACTION

        stored = state.get("budgetWarnings")
        stored = stored if isinstance(stored, dict) else {}
        due: list[tuple[str, float, float, str, str | None]] = []
        for num, entry in entries.items():
            budget = _api_budget(entry)
            if budget is None:
                continue
            used, limit, month, source = budget
            if not month or used < BUDGET_WARN_FRACTION * limit:
                continue
            if stored.get(num) == month or (num, month) in self._budget_warned:
                continue
            due.append((num, used, limit, month, source))
        if not due:
            return
        for num, used, limit, month, source in due:
            self._budget_warned.add((num, month))
            self._emit(
                BudgetWarningEvent(
                    account=_ref(num, self.switcher.account_email(num)),
                    used=used, limit=limit, month=month, source=source,
                )
            )
        if self.dry_run:
            return

        def remember(s: dict) -> None:
            warned = s.get("budgetWarnings")
            if not isinstance(warned, dict):
                warned = s["budgetWarnings"] = {}
            for num, _used, _limit, month, _source in due:
                warned[num] = month

        try:
            self._mutate_state(remember)
        except Exception as e:  # a warning must never cost the tick
            _logger.debug("Could not record a budget warning: %s", e)

    def _check_model_names(
        self, quarantined: set[str], usage: dict[str, dict | str | None]
    ) -> None:
        """One-shot ``autoswitch.model`` typo guard.

        A configured name that no account reports means the filter looks
        active while gating nothing. That's only provable once every
        relevant oauth account has readable usage this tick — adaptive
        polling legitimately leaves gaps before that — and never worth a
        forced refresh of its own.
        """
        wanted = {m.lower(): m for m in self._models if m.lower() != "all"}
        if not wanted:
            self._model_check_done = True  # bare "all" needs no name match
            return
        relevant = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n not in quarantined
            and self.switcher.account_kind_for(n) != "api_key"
        ]
        values = [usage.get(n) for n in relevant]
        readable = [v for v in values if isinstance(v, dict)]
        if not readable or len(readable) != len(values):
            return  # not every account observed yet — re-check next tick
        seen = {
            s["name"].lower()
            for v in readable
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        self._model_check_done = True
        missing = [name for low, name in wanted.items() if low not in seen]
        if missing:
            self._emit(
                ConfigWarningEvent(
                    message=(
                        f"autoswitch.model: {', '.join(missing)} matches no "
                        "account's usage windows — only the 5h/7d limits are "
                        "being watched for it (typo?)"
                    )
                )
            )

    def _earliest_recovery(
        self, usage: dict[str, dict | str | None]
    ) -> datetime | None:
        """Earliest moment any account becomes usable again (UTC), or None
        when that moment can't be proven.

        Per account that's the *latest* reset among its ≥100% relevant
        windows — an account blocked on both 5h and a scoped weekly limit
        isn't usable when the 5h rolls over — then the minimum across
        accounts, the active one included (its recovery also ends the
        blocked state). A blocked account whose exhausted windows carry no
        reset time at all could recover at any moment, so it makes the whole
        answer unprovable: return None and let the bounded blocked-cadence
        fallback re-check, rather than sleeping toward another account's
        later known reset."""
        earliest: float | None = None
        now = self.clock()
        for value in usage.values():
            if not isinstance(value, dict):
                continue
            blocked = [
                resets_at
                for _, pct, resets_at in oauth.relevant_windows(value, self._models)
                if pct >= 100.0
            ]
            if not blocked:
                continue  # not exhausted — doesn't gate the blocked state
            usable_at = _limiting_reset_ts(value, self._models)
            if usable_at is None or usable_at <= now:
                return None  # blocked with unprovable recovery — don't oversleep
            if earliest is None or usable_at < earliest:
                earliest = usable_at
        if earliest is None:
            return None
        return datetime.fromtimestamp(earliest, tz=timezone.utc)

    def _shared_session_count(self) -> int:
        """How many Claude Code sessions run on the provider's shared login;
        0 for a provider that cannot tell (Codex) or when reading fails."""
        reader = getattr(self.switcher, "shared_login_sessions", None)
        if not callable(reader):
            return 0
        try:
            return len(reader() or ())
        except Exception:
            return 0

    def _emit(self, event: AutoSwitchEvent) -> None:
        # Every event carries this tick's context for its human text (an
        # event that brought its own keeps it). Human-only: to_json() and
        # equality never see it.
        if self._context is not None and getattr(event, "context", True) is None:
            event = replace(event, context=self._context)
        self.on_event(event)

    # -- loop -------------------------------------------------------------------

    def stop(self) -> None:
        """Ask ``run_loop`` to exit; wakes it from any sleep. Safe to call
        before the loop starts — the stop is never cleared, so the loop
        exits immediately (engines are single-use)."""
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Cut the current inter-tick sleep short and tick now."""
        self._wake.set()

    def apply_threshold(self, threshold: float) -> None:
        """Session override from the TUI: retarget the trigger and poll
        cadence mid-run. Threshold only — the model axes (and their derived
        state) are fixed at construction. The frozen-settings swap is atomic
        and each tick snapshots ``self.settings`` once, so no locking."""
        self.settings = replace(self.settings, threshold=threshold)
        # Recomputed, not passed through: a per-account override may still be
        # lower than the new session global, and cadence follows the minimum.
        self.switcher.set_poll_policy_inputs(
            self._min_effective_threshold(),
            self.settings.weekly_threshold,
            self._models,
        )

    def _next_delay(self, outcome: TickOutcome) -> float:
        interval = self.settings.interval_seconds
        if outcome is TickOutcome.BLOCKED:
            if self._sleep_until_ts is not None:
                delay = self._sleep_until_ts - self.clock()
                return min(max(delay, interval), MAX_SLEEP_S)
            if self._blocked_wait_long:
                # Truly exhausted with no reset time known / no candidates.
                return max(interval, NO_RESET_FALLBACK_S)
            # Blocked on something that can resolve any tick (hysteresis,
            # unreadable usage) — keep the normal cadence so the at-limit
            # escape isn't missed.
        elif outcome is TickOutcome.NO_ACTION and self._idle_hold_slow:
            # Idle-hold: the CLI is idle on an expired token — nothing changes
            # until the user comes back, so crawl. Worst case protection
            # resumes one slow tick after they do.
            return max(interval, NO_RESET_FALLBACK_S)
        # ±10% jitter so multiple machines don't synchronize their API hits.
        return self._respect_poll_plan(interval * (0.9 + 0.2 * random.random()))

    def _respect_poll_plan(self, delay: float) -> float:
        """Shorten a normal-cadence sleep to the store's own next-poll time.

        The planner tightens the active row to URGENT_INTERVAL_S while it
        burns toward the threshold, but the loop always slept
        ``interval_seconds`` — so the plan ran late. Measured mid-episode: the
        row was due 112s ago while the engine still had minutes of sleep left.

        Only ever shortens, never below the planner's floor: the 429 budget
        lives in the plan, and this makes the loop obey it rather than
        override it. Best-effort — the unshortened delay is always safe.
        """
        try:
            current = self.switcher.current_account_number()
            if current is None:
                return delay
            entry = self.switcher.usage_entries_by_account(fetch=set()).get(current)
            if entry is None or entry.next_poll_at is None:
                return delay
            due_in = entry.next_poll_at - self.clock()
            # Clamp the DEADLINE, not the result. max(min(delay, due_in), U)
            # raises a delay that was ALREADY below U: at the configurable
            # floor of 15s it turns a 13.5s jittered sleep into 60s, and at
            # the 60s default it flattens the entire lower jitter half.
            # Bounding due_in instead keeps "only ever shortens" true at every
            # configured interval, and still refuses to poll faster than the
            # planner's own floor when the row is overdue.
            return min(delay, max(due_in, poll_policy.URGENT_INTERVAL_S))
        except Exception:
            return delay

    def run_loop(self) -> int:
        """Tick forever (until :meth:`stop`); a failing tick never kills it."""
        while True:
            # Clear at the top, not after the wait: a wake() racing a wait
            # timeout is then never lost — the tick right after this clear
            # already sees whatever settings that wake announced.
            self._wake.clear()
            if self._stop.is_set():
                return 0
            try:
                outcome = self.tick()
            except Exception as e:  # pragma: no cover - tick() already guards
                self._emit(
                    ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
                )
                outcome = TickOutcome.ERROR
            delay = self._next_delay(outcome)
            now = self.clock()
            write_heartbeat(
                self.switcher.backup_dir,
                mode="loop",
                dry_run=self.dry_run,
                last_tick_at=now,
                next_tick_at=now + delay,
                started_at=self._started_at,
                host=self.host_label,
            )
            if delay > self.settings.interval_seconds * 1.5:
                until = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self._emit(
                    SleepEvent(
                        seconds=delay,
                        until=until.isoformat(timespec="seconds").replace(
                            "+00:00", "Z"
                        ),
                    )
                )
            self._wake.wait(delay)
