"""Named, deterministic demo states.

A scenario is data: a fixed clock, a set of invented accounts (plan, usage
windows, usage resets, alias, disabled), settings, and optionally a synthetic
session history for ``qswap usage``. ``seed.py`` writes it into a temp home
with the tool's own writers, and ``fakeapi.py`` answers the tool's HTTP calls
from it, so what a capture shows is the real rendering of this state.

Every name, email, organization, project path and number here is invented.
Emails use the reserved ``example.com``/``example.org``/``example.net``
domains; projects live under ``/work/``.

Adding a scenario: build a :class:`Scenario` and add it to ``SCENARIOS``.
Adding a provider (Codex, once ``qswap codex`` manages accounts): give it an
account dataclass with a ``provider`` field, a seeder in ``seed.SEEDERS``, and
routes in ``fakeapi.FakeAnthropic`` (or a sibling fake for its API). Scenario
accounts are a flat tuple, so a mixed Claude and Codex fleet is one list.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from fakeapi import iso, window


def _uuid(*parts: str) -> str:
    """A stable, obviously synthetic UUID derived from ``parts``."""
    digest = hashlib.sha256("|".join(("qswap-demo", *parts)).encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


H = timedelta(hours=1)
M = timedelta(minutes=1)
D = timedelta(days=1)


@dataclass(frozen=True)
class Ramp:
    """Utilization that climbs with scenario time: ``start + per_min * minutes``,
    capped. Used by ``autoswitch`` so the active account walks into its limit."""

    start: float
    per_min: float = 0.0
    cap: float = 100.0

    def at(self, elapsed_s: float) -> float:
        return max(0.0, min(self.cap, self.start + self.per_min * elapsed_s / 60.0))


def pct_at(value: float | Ramp, elapsed_s: float) -> float:
    return value.at(elapsed_s) if isinstance(value, Ramp) else float(value)


@dataclass(frozen=True)
class Window:
    pct: float | Ramp
    resets_in: timedelta


@dataclass(frozen=True)
class ModelWindow:
    """A per-model weekly window (``weekly_scoped`` in the usage API)."""

    model: str  # display name: "Fable", "Opus", "Sonnet"
    pct: float | Ramp
    resets_in: timedelta
    share_of_weekly: float | None = None  # pts of the 7d window this model used


@dataclass(frozen=True)
class ExtraUsage:
    used: float  # dollars
    limit: float
    resets_in: timedelta = 20 * D


@dataclass(frozen=True)
class Grant:
    """One usage-reset grant (the ``cedar_ember`` block)."""

    id: str
    clears: tuple[str, ...]
    label: str = ""
    resets_left: int = 1
    resets_total: int | None = None
    ends_in: timedelta = 30 * D
    usable_now: bool = True
    use_requires_limit: bool = True
    gives_back_weekly: bool = False
    type: str | None = None


@dataclass(frozen=True)
class Resets:
    eligible: bool = True
    ineligible_reason: str | None = None
    grants: tuple[Grant, ...] = ()


@dataclass(frozen=True)
class Plan:
    tier: str
    organization_type: str
    display_name: str | None
    billing_type: str = "stripe_subscription"
    subscription_status: str = "active"


PLANS: dict[str, Plan] = {
    "pro": Plan("default_claude_ai", "claude_pro", "Pro"),
    "max5": Plan("default_claude_max_5x", "claude_max", "Max"),
    "max20": Plan("default_claude_max_20x", "claude_max", "Max"),
    "ultra": Plan("default_claude_max_20x", "claude_max", "Ultra"),
    "team": Plan("default_claude_max_5x", "claude_team", "Team"),
    "enterprise": Plan("default_claude_zero", "claude_enterprise", None),
}


@dataclass(frozen=True)
class ClaudeAccount:
    slot: int
    email: str
    plan: str = "max20"
    alias: str = ""
    org_name: str = ""  # "" is a personal account
    five_hour: Window | None = None
    seven_day: Window | None = None
    models: tuple[ModelWindow, ...] = ()
    extra_usage: ExtraUsage | None = None
    resets: Resets | None = None
    disabled: bool = False
    priority: int | None = None
    threshold: float | None = None
    provider: str = "claude"

    @property
    def account_uuid(self) -> str:
        return _uuid("account", self.email)

    @property
    def org_uuid(self) -> str:
        return _uuid("org", self.org_name or self.email)

    def access_token(self, generation: int = 0) -> str:
        return f"demo-access-{self.slot}-{generation}"

    def refresh_token(self, generation: int = 0) -> str:
        return f"demo-refresh-{self.slot}-{generation}"


@dataclass(frozen=True)
class LedgerProject:
    path: str
    weight: float
    models: tuple[tuple[str, float], ...]  # (model id, share)


@dataclass(frozen=True)
class Ledger:
    """A synthetic session history for ``qswap usage``."""

    days: int = 14
    seed: int = 7
    projects: tuple[LedgerProject, ...] = ()
    # Which slot was live, as (days ago, hour UTC, slot): the activation log.
    switches: tuple[tuple[int, int, int], ...] = ()
    codex_projects: tuple[LedgerProject, ...] = ()
    codex_email: str = ""
    daily_scale: float = 1.0


@dataclass(frozen=True)
class AutoRun:
    """How a demo run drives ``qswap auto`` (and the TUI's auto view).

    The engine sleeps ``interval`` seconds between ticks. Demo runs compress
    that: the scenario clock jumps by the sleep, real time waits
    ``1/compression`` of it, so a five-minute episode records in seconds with
    true timestamps in the log. ``ticks`` stops the CLI loop after that many.
    """

    ticks: int = 5
    compression: float = 20.0


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    clock: datetime
    accounts: tuple[ClaudeAccount, ...]
    active: int
    settings: dict = field(default_factory=dict)
    ledger: Ledger | None = None
    auto: AutoRun | None = None
    # Grant ids already spent before the run starts (the "after" states).
    spent: tuple[tuple[int, str], ...] = ()
    # Read every account's usage this long before the clock, as an earlier
    # qswap run (the menu bar, a `list`) would have: the store then holds a
    # previous reading, so the poll planner sees movement from the first tick.
    warmup: timedelta | None = None
    notes: str = ""

    @property
    def epoch(self) -> float:
        return self.clock.timestamp()

    def account(self, slot: int) -> ClaudeAccount:
        return next(a for a in self.accounts if a.slot == slot)


# -- live state: what the fake API answers ------------------------------------


class LiveState:
    """Mutable view of a scenario while a demo process runs.

    Tracks token generations (a refresh rotates them), grants spent, and the
    windows a spent grant cleared, and evaluates ramps against the clock.
    """

    def __init__(self, scenario: Scenario, clock) -> None:
        self.scenario = scenario
        self.clock = clock  # callable -> epoch seconds (the frozen clock)
        self.generation: dict[int, int] = {a.slot: 0 for a in scenario.accounts}
        self.spent: set[tuple[int, str]] = set(scenario.spent)
        # slot -> {window key: (cleared at elapsed seconds, value before)}
        self.cleared: dict[int, dict[str, float]] = {}
        for slot, gid in scenario.spent:
            grant = self._grant(slot, gid)
            if grant is not None:
                self._apply_clear(slot, grant, at=0.0)

    def elapsed(self) -> float:
        return self.clock() - self.scenario.epoch

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc)

    def account_for_token(self, token: str):
        for a in self.scenario.accounts:
            if token == a.access_token(self.generation[a.slot]):
                return a
        return None

    def _grant(self, slot: int, gid: str) -> Grant | None:
        acct = self.scenario.account(slot)
        if acct.resets is None:
            return None
        return next((g for g in acct.resets.grants if g.id == gid), None)

    def _resets_at(self, delta: timedelta) -> datetime:
        # Half a minute of slack, so the seconds a run takes never tip a
        # countdown into the next minute: captures stay identical run to run.
        return self.scenario.clock + delta + timedelta(seconds=30)

    def _window_pct(self, slot: int, key: str, value: float | Ramp) -> float:
        cleared_at = self.cleared.get(slot, {}).get(key)
        if cleared_at is None:
            return pct_at(value, self.elapsed())
        if isinstance(value, Ramp):
            # A refilled window keeps climbing at the same rate from zero.
            return max(0.0, Ramp(0.0, value.per_min, value.cap).at(self.elapsed() - cleared_at))
        return 0.0

    def usage_payload(self, a: ClaudeAccount, *, resets: bool) -> dict:
        out: dict = {}
        if a.five_hour is not None:
            out["five_hour"] = window(
                self._window_pct(a.slot, "five_hour", a.five_hour.pct),
                self._resets_at(a.five_hour.resets_in),
            )
        else:
            out["five_hour"] = None
        if a.seven_day is not None:
            seven = self._window_pct(a.slot, "seven_day", a.seven_day.pct)
            # A spent grant that gives weekly back refunds what its model used.
            for gid_slot, gid in self.spent:
                g = self._grant(gid_slot, gid) if gid_slot == a.slot else None
                if g is not None and g.gives_back_weekly and "seven_day" not in g.clears:
                    for m in a.models:
                        if _model_key(m.model) in g.clears and m.share_of_weekly:
                            seven = max(0.0, seven - m.share_of_weekly)
            out["seven_day"] = window(seven, self._resets_at(a.seven_day.resets_in))
        limits, shares = [], []
        for i, m in enumerate(a.models):
            key = _model_key(m.model)
            pct = self._window_pct(a.slot, key, m.pct)
            limits.append({
                "kind": "weekly_scoped",
                "scope": {"model": {"display_name": m.model}},
                "percent": round(pct, 1),
                "resets_at": iso(self._resets_at(m.resets_in)),
            })
            share = m.share_of_weekly
            if share is not None and key in self.cleared.get(a.slot, {}):
                share = 0.0
            if share is not None:
                shares.append({"limit_index": i, "used_percent_of_weekly": share})
        if limits:
            out["limits"] = limits
        if shares:
            out["weekly_scoped_shares"] = shares
        if a.extra_usage is not None:
            eu = a.extra_usage
            out["extra_usage"] = {
                "is_enabled": True,
                "used_credits": round(eu.used * 100),
                "monthly_limit": round(eu.limit * 100),
                "utilization": round(100 * eu.used / eu.limit, 1),
                "currency": "USD",
                "resets_at": iso(self._resets_at(eu.resets_in)),
            }
        if resets and a.resets is not None:
            out["cedar_ember"] = self._resets_block(a)
        return out

    def _resets_block(self, a: ClaudeAccount) -> dict:
        r = a.resets
        assert r is not None
        grants = []
        exhausted = []
        for key, value in self._current_windows(a).items():
            if value >= 100.0:
                exhausted.append(key)
        for g in r.grants:
            left = g.resets_left - (1 if (a.slot, g.id) in self.spent else 0)
            percent_used = {k: v for k, v in self._current_windows(a).items() if k in g.clears}
            at_limit = any(percent_used.get(k, 0.0) >= 100.0 for k in g.clears)
            grants.append({
                "id": g.id,
                "label": g.label,
                "resets_left": max(0, left),
                "resets_total": g.resets_total or g.resets_left,
                "starts_at": iso(self.scenario.clock - 20 * D),
                "ends_at": iso(self.scenario.clock + g.ends_in),
                "clears": list(g.clears),
                "blocking": [],
                "paused": False,
                "usable_now": g.usable_now and left > 0 and (at_limit or not g.use_requires_limit),
                "use_requires_limit": g.use_requires_limit,
                "gives_back_weekly": g.gives_back_weekly,
                "type": g.type,
                "percent_used": percent_used,
            })
        usable = [g for g in grants if g["resets_left"] > 0]
        block = {
            "eligible": r.eligible,
            "ineligible_reason": r.ineligible_reason,
            "at_limit": bool(exhausted),
            "exhausted": exhausted,
            "next_grant_id": usable[0]["id"] if usable else None,
            "weekly_resets_at": iso(self._resets_at(a.seven_day.resets_in)) if a.seven_day else None,
            "cooldown_until": None,
            "grants": grants if r.eligible else [],
        }
        return block

    def _current_windows(self, a: ClaudeAccount) -> dict[str, float]:
        out = {}
        if a.five_hour is not None:
            out["five_hour"] = self._window_pct(a.slot, "five_hour", a.five_hour.pct)
        if a.seven_day is not None:
            out["seven_day"] = self._window_pct(a.slot, "seven_day", a.seven_day.pct)
        for m in a.models:
            out[_model_key(m.model)] = self._window_pct(a.slot, _model_key(m.model), m.pct)
        return out

    def profile_payload(self, a: ClaudeAccount) -> dict:
        plan = PLANS[a.plan]
        org = {
            "uuid": a.org_uuid,
            "name": a.org_name or f"{a.email}'s Organization",
            "rate_limit_tier": plan.tier,
            "organization_type": plan.organization_type,
            "billing_type": plan.billing_type,
            "subscription_status": plan.subscription_status,
        }
        if plan.display_name:
            org["plan_display_name"] = plan.display_name
        return {
            "account": {"uuid": a.account_uuid, "email": a.email, "display_name": a.alias or None},
            "organization": org,
        }

    def refresh(self, refresh_token: str) -> dict | None:
        for a in self.scenario.accounts:
            gen = self.generation[a.slot]
            if refresh_token == a.refresh_token(gen):
                self.generation[a.slot] = gen + 1
                return {
                    "access_token": a.access_token(gen + 1),
                    "refresh_token": a.refresh_token(gen + 1),
                    "expires_in": 8 * 3600,
                    "scope": "user:inference user:profile user:sessions:claude_code",
                    "account": {"uuid": a.account_uuid, "email_address": a.email},
                    "organization": {"uuid": a.org_uuid},
                }
        return None

    def _apply_clear(self, slot: int, grant: Grant, *, at: float) -> None:
        self.cleared.setdefault(slot, {}).update({k: at for k in grant.clears})

    def claim(self, a: ClaudeAccount, grant_id: str) -> dict:
        grant = self._grant(a.slot, grant_id)
        if grant is None:
            return {"result": "ineligible", "reason": "unknown_grant"}
        if (a.slot, grant_id) in self.spent and grant.resets_left <= 1:
            return {"result": "already_used"}
        self.spent.add((a.slot, grant_id))
        self._apply_clear(a.slot, grant, at=self.elapsed())
        return {
            "result": "reset",
            "resets_left": max(0, grant.resets_left - 1),
            "cleared": list(grant.clears),
            "weekly_resets_at": iso(self._resets_at(a.seven_day.resets_in)) if a.seven_day else None,
        }


_MODEL_KEYS = {"fable": "seven_day_overage_included", "opus": "seven_day_opus", "sonnet": "seven_day_sonnet"}


def _model_key(model: str) -> str:
    return _MODEL_KEYS.get(model.lower(), f"seven_day_{model.lower()}")


# -- the scenarios ------------------------------------------------------------

CLOCK = datetime(2026, 11, 10, 14, 20, tzinfo=timezone.utc)

# Grants shaped like the launch grants Claude Code offers: one Fable reset
# (clears the per-model window and gives back its share of the weekly one),
# and a session reset that refills the 5-hour window.
FABLE_RESET = Grant(
    id="launch-2611-fable",
    label="One Fable reset for Max",
    clears=("seven_day_overage_included",),
    gives_back_weekly=True,
    type="weekly",
    ends_in=48 * D,
)
SESSION_RESET = Grant(
    id="tenure-12m-session",
    label="One more at 12 months' tenure",
    clears=("five_hour",),
    type="session",
    ends_in=20 * D,
)


def _fleet_accounts() -> tuple[ClaudeAccount, ...]:
    return (
        ClaudeAccount(
            slot=1,
            email="maya@example.com",
            alias="personal",
            plan="max20",
            five_hour=Window(46, 2 * H + 35 * M),
            seven_day=Window(38, 3 * D + 6 * H),
            models=(ModelWindow("Fable", 52, 3 * D + 6 * H, share_of_weekly=14),),
            resets=Resets(grants=(FABLE_RESET,)),
        ),
        ClaudeAccount(
            slot=2,
            email="maya@northwind.example.com",
            alias="work",
            org_name="Northwind Labs",
            plan="team",
            five_hour=Window(12, 4 * H + 10 * M),
            seven_day=Window(21, 5 * D + 2 * H),
            extra_usage=ExtraUsage(used=18.40, limit=100.0),
        ),
        ClaudeAccount(
            slot=3,
            email="ops@example.net",
            alias="night-shift",
            plan="max5",
            five_hour=Window(93, 38 * M),
            seven_day=Window(81, 1 * D + 19 * H),
            models=(ModelWindow("Fable", 97, 1 * D + 19 * H, share_of_weekly=31),),
            resets=Resets(grants=(SESSION_RESET,)),
        ),
        ClaudeAccount(
            slot=4,
            email="sam@example.org",
            alias="side",
            plan="pro",
            five_hour=Window(0, 5 * H),
            seven_day=Window(9, 6 * D + 1 * H),
            disabled=True,
            resets=Resets(eligible=False, ineligible_reason="plan"),
        ),
    )


_DARK = {"ui": {"theme": "dark"}, "updates": {"check": False}}

FLEET = Scenario(
    name="fleet",
    title="Four Claude accounts on different plans",
    clock=CLOCK,
    accounts=_fleet_accounts(),
    active=1,
    settings=_DARK,
    notes="One near its limit (3), one holding resets (1, 3), one disabled (4).",
)


def _autoswitch_accounts() -> tuple[ClaudeAccount, ...]:
    a1, a2, a3, _a4 = _fleet_accounts()
    return (
        # 78% at the warm-up read, then 86%, 88.5%, 91%: rising inside 15
        # points of its line puts the poll planner on a one-minute cadence,
        # so each tick sees a fresh reading until the switch.
        replace(a1, five_hour=Window(Ramp(86.0, 2.5, cap=92.0), 1 * H + 50 * M)),
        replace(a2, five_hour=Window(12, 4 * H + 10 * M)),
        a3,
    )


AUTOSWITCH = Scenario(
    name="autoswitch",
    title="The active account walks into its limit; auto-switch moves on",
    clock=CLOCK,
    accounts=_autoswitch_accounts(),
    active=1,
    settings={**_DARK, "autoswitch": {"threshold": 90, "intervalSeconds": 60, "cooldownSeconds": 60}},
    auto=AutoRun(ticks=3, compression=20.0),
    warmup=3 * M,
)


def _reset_accounts() -> tuple[ClaudeAccount, ...]:
    a1, a2, a3, a4 = _fleet_accounts()
    return (
        replace(
            a1,
            five_hour=Window(31, 3 * H + 5 * M),
            seven_day=Window(74, 3 * D + 6 * H),
            models=(ModelWindow("Fable", 100, 3 * D + 6 * H, share_of_weekly=22),),
            resets=Resets(grants=(FABLE_RESET, replace(SESSION_RESET, usable_now=True))),
        ),
        replace(a2, resets=Resets(eligible=False, ineligible_reason="plan")),
        replace(a3, resets=Resets(grants=())),
        a4,
    )


RESET = Scenario(
    name="reset",
    title="A per-model window at 100% with a usage reset ready",
    clock=CLOCK,
    accounts=_reset_accounts(),
    active=1,
    settings={**_DARK, "autoswitch": {"model": "Fable"}, "resets": {"auto": "active"}},
    auto=AutoRun(ticks=3, compression=20.0),
)

RESET_AFTER = replace(
    RESET,
    name="reset-after",
    title="The same account after its Fable reset was spent",
    spent=((1, FABLE_RESET.id),),
)


USAGE = Scenario(
    name="usage",
    title="Two weeks of Claude Code and Codex sessions",
    clock=CLOCK,
    accounts=_fleet_accounts(),
    active=1,
    settings=_DARK,
    ledger=Ledger(
        days=14,
        projects=(
            LedgerProject("/work/checkout-api", 0.38, (
                ("claude-opus-5-5", 0.70), ("claude-sonnet-5-5", 0.25), ("claude-haiku-5-5", 0.05),
            )),
            LedgerProject("/work/billing-dashboard", 0.24, (
                ("claude-sonnet-5-5", 0.65), ("claude-opus-5-5", 0.30), ("claude-haiku-5-5", 0.05),
            )),
            LedgerProject("/work/mobile-app", 0.16, (
                ("claude-fable-5-2", 0.55), ("claude-sonnet-5-5", 0.45),
            )),
            LedgerProject("/work/infra-terraform", 0.10, (
                ("claude-sonnet-5-5", 0.80), ("claude-haiku-5-5", 0.20),
            )),
        ),
        switches=tuple(
            (d, h, slot)
            for d in range(15, -1, -1)
            for h, slot in ((8, 1), (13, 2), (19, 1) if d % 3 else (19, 3))
        ),
        codex_projects=(
            LedgerProject("/work/checkout-api", 0.08, (("gpt-6.1-sol", 0.8), ("gpt-6.1-terra", 0.2))),
            LedgerProject("/work/data-pipeline", 0.04, (("gpt-6.1-sol", 1.0),)),
        ),
        codex_email="maya@example.com",
        daily_scale=2.0,
    ),
)


SCENARIOS: dict[str, Scenario] = {
    s.name: s for s in (FLEET, AUTOSWITCH, RESET, RESET_AFTER, USAGE)
}


def get(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise SystemExit(
            f"unknown scenario {name!r}; known: {', '.join(sorted(SCENARIOS))}"
        ) from None
