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
Adding a provider: give it an account dataclass with a ``provider`` field, a
seeder in ``seed.SEEDERS``, and routes in ``fakeapi.FakeAnthropic``. Scenario
accounts are a flat tuple, so a mixed Claude and Codex fleet is one list
(:class:`ClaudeAccount` and :class:`CodexAccount` number their slots
independently, as each provider's registry does).
"""

from __future__ import annotations

import base64
import hashlib
import json
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
class UsageFailure:
    """How an account's usage endpoint fails: an HTTP status, or ``"timeout"``.

    ``from_clock``: the warm-up read (before the scenario clock) still
    succeeded, so the store keeps that older reading and the row shows it
    with its age: a stale reading.
    """

    status: int | str = 503
    from_clock: bool = False


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
    # An Anthropic API key (``qswap add-token sk-ant-...``) instead of a login:
    # no quota windows, never polled on the usage endpoint.
    api_key: bool = False
    # A monthly budget in dollars (``qswap budget N AMOUNT``), API keys only.
    budget: float | None = None
    # Access-token lifetime from the clock; negative means already expired.
    token_expires_in: timedelta = timedelta(hours=8)
    # The token endpoint answers ``invalid_grant`` for this login's refresh
    # token: a dead login that only a re-login fixes.
    refresh_dead: bool = False
    usage_failure: UsageFailure | None = None
    # Seed an auto-switch quarantine entry with this reason.
    quarantine: str | None = None
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

    @property
    def api_key_secret(self) -> str:
        return f"sk-ant-api03-demo{self.slot}-not-a-real-key"


# -- Codex ---------------------------------------------------------------------

# The claim namespaces in a ChatGPT login's JWTs (``codex.auth``).
CODEX_AUTH_CLAIMS = "https://api.openai.com/auth"
CODEX_PROFILE_CLAIMS = "https://api.openai.com/profile"
CODEX_FIVE_HOUR_S = 18_000
CODEX_WEEK_S = 604_800


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt(claims: dict) -> str:
    """An unsigned-looking JWT carrying ``claims`` (junk signature, never checked)."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, sort_keys=True).encode())
    payload = _b64url(json.dumps(claims, sort_keys=True).encode())
    return f"{header}.{payload}.ZGVtby1ub3QtYS1zaWduYXR1cmU"


@dataclass(frozen=True)
class CodexCredit:
    """One Codex usage reset credit (``/wham/rate-limit-reset-credits``)."""

    id: str
    title: str = ""
    status: str = "available"
    expires_in: timedelta | None = timedelta(days=30)
    supported: bool = True


@dataclass(frozen=True)
class CodexAccount:
    """A ChatGPT login for Codex CLI, as ``auth.json`` holds it."""

    slot: int
    email: str
    plan: str = "plus"  # the usage answer's ``plan_type``
    alias: str = ""
    org_name: str = ""  # a workspace's name; personal plans have none
    five_hour: Window | None = None
    seven_day: Window | None = None
    credits: tuple[CodexCredit, ...] = ()
    allowed: bool = True  # rate_limit.allowed
    disabled: bool = False
    priority: int | None = None
    threshold: float | None = None
    # Access-token lifetime from the clock; negative means already expired.
    token_expires_in: timedelta = timedelta(days=9)
    provider: str = "codex"

    @property
    def user_id(self) -> str:
        return "user-" + _uuid("codex-user", self.email).replace("-", "")[:20]

    @property
    def account_id(self) -> str:
        return _uuid("codex-account", self.email, self.org_name or self.plan)

    def access_token(self, clock: datetime) -> str:
        epoch = clock.timestamp()
        return _jwt({
            "client_id": "app_demo",
            CODEX_AUTH_CLAIMS: {
                "chatgpt_account_id": self.account_id,
                "chatgpt_user_id": self.user_id,
                "chatgpt_plan_type": self.plan,
            },
            CODEX_PROFILE_CLAIMS: {"email": self.email, "email_verified": True},
            "iat": int(epoch - 86_400),
            "exp": int(epoch + self.token_expires_in.total_seconds()),
            "jti": f"demo-codex-{self.slot}",
        })

    def login_record(self, clock: datetime) -> bytes:
        """The ``auth.json`` bytes Codex would have written for this login."""
        epoch = clock.timestamp()
        id_token = _jwt({
            "email": self.email,
            CODEX_AUTH_CLAIMS: {
                "chatgpt_account_id": self.account_id,
                "chatgpt_user_id": self.user_id,
                "chatgpt_plan_type": self.plan,
            },
            "exp": int(epoch + 3600),
        })
        record = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": id_token,
                "access_token": self.access_token(clock),
                "refresh_token": f"demo-codex-refresh-{self.slot}",
                "account_id": self.account_id,
            },
            "last_refresh": (clock - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        }
        return (json.dumps(record, indent=2) + "\n").encode("utf-8")


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
    # The same for Codex accounts (Codex's own activation log).
    codex_switches: tuple[tuple[int, int, int], ...] = ()
    daily_scale: float = 1.0
    # Read the history into ``ledger.db`` while seeding, as an earlier
    # ``qswap usage`` run would have: API-key spend on ``list`` rows and the
    # menu bar's cost row read the ledger without scanning it.
    prescan: bool = False


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
    accounts: tuple[ClaudeAccount | CodexAccount, ...]
    # The managed Claude account whose login is live; None for no managed one.
    active: int | None
    settings: dict = field(default_factory=dict)
    ledger: Ledger | None = None
    auto: AutoRun | None = None
    # Grant ids already spent before the run starts (the "after" states).
    spent: tuple[tuple[int, str], ...] = ()
    # Read every account's usage this long before the clock, as an earlier
    # qswap run (the menu bar, a `list`) would have: the store then holds a
    # previous reading, so the poll planner sees movement from the first tick.
    warmup: timedelta | None = None
    # A live Claude Code login that qswap does not manage (a first run, or a
    # `/login` since the last `qswap add`). Its slot is only the fake API's
    # key for its tokens.
    live: ClaudeAccount | None = None
    # The managed Codex account whose login is in Codex's auth.json.
    codex_active: int | None = None
    # A live Codex login that qswap does not manage.
    codex_live: CodexAccount | None = None
    # What a faked `codex login` leaves behind for `qswap codex login`; None
    # behaves as a machine without the codex CLI.
    codex_login: CodexAccount | None = None
    notes: str = ""

    @property
    def epoch(self) -> float:
        return self.clock.timestamp()

    @property
    def claude_accounts(self) -> tuple[ClaudeAccount, ...]:
        return tuple(a for a in self.accounts if a.provider == "claude")

    @property
    def codex_accounts(self) -> tuple[CodexAccount, ...]:
        return tuple(a for a in self.accounts if a.provider == "codex")

    def account(self, slot: int) -> ClaudeAccount:
        return next(a for a in self.claude_accounts if a.slot == slot)

    def codex_account(self, slot: int) -> CodexAccount:
        return next(a for a in self.codex_accounts if a.slot == slot)

    def claude_logins(self) -> tuple[ClaudeAccount, ...]:
        """Every Claude login the fake API knows: managed ones and the unmanaged live one."""
        return self.claude_accounts + ((self.live,) if self.live is not None else ())

    def codex_logins(self) -> tuple[CodexAccount, ...]:
        """Every Codex login the fake API knows."""
        extra = tuple(a for a in (self.codex_live, self.codex_login) if a is not None)
        return self.codex_accounts + extra


# -- live state: what the fake API answers ------------------------------------


class LiveState:
    """Mutable view of a scenario while a demo process runs.

    Tracks token generations (a refresh rotates them), grants spent, and the
    windows a spent grant cleared, and evaluates ramps against the clock.
    """

    def __init__(self, scenario: Scenario, clock) -> None:
        self.scenario = scenario
        self.clock = clock  # callable -> epoch seconds (the frozen clock)
        slots = [a.slot for a in scenario.claude_logins()]
        if len(slots) != len(set(slots)):
            raise ValueError(f"{scenario.name}: two Claude logins share a slot")
        codex_ids = [a.account_id for a in scenario.codex_logins()]
        if len(codex_ids) != len(set(codex_ids)):
            raise ValueError(f"{scenario.name}: two Codex logins share an account id")
        self.generation: dict[int, int] = {a.slot: 0 for a in scenario.claude_logins()}
        self.spent: set[tuple[int, str]] = set(scenario.spent)
        # slot -> {window key: (cleared at elapsed seconds, value before)}
        self.cleared: dict[int, dict[str, float]] = {}
        for slot, gid in scenario.spent:
            grant = self._grant(slot, gid)
            if grant is not None:
                self._apply_clear(slot, grant, at=0.0)
        # Codex: credits spent, and the windows a spent credit refilled.
        self.codex_spent: set[tuple[str, str]] = set()  # (account id, credit id)
        self.codex_cleared: dict[str, dict[str, float]] = {}

    def elapsed(self) -> float:
        return self.clock() - self.scenario.epoch

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc)

    def account_for_token(self, token: str):
        for a in self.scenario.claude_logins():
            if token == a.access_token(self.generation[a.slot]):
                return a
        return None

    def usage_failure(self, a: ClaudeAccount) -> UsageFailure | None:
        """The failure the usage endpoint answers for ``a`` right now, if any."""
        f = a.usage_failure
        if f is None or (f.from_clock and self.elapsed() < 0):
            return None
        return f

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
        for a in self.scenario.claude_logins():
            gen = self.generation[a.slot]
            if a.refresh_dead:
                continue  # the token endpoint answers invalid_grant
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

    # -- Codex (chatgpt.com/backend-api/wham) ------------------------------------

    def codex_account_for_token(self, token: str, account_id: str) -> CodexAccount | None:
        for a in self.scenario.codex_logins():
            if token == a.access_token(self.scenario.clock) and account_id == a.account_id:
                return a
        return None

    def _codex_pct(self, a: CodexAccount, key: str, value: float | Ramp) -> float:
        cleared_at = self.codex_cleared.get(a.account_id, {}).get(key)
        if cleared_at is None:
            return pct_at(value, self.elapsed())
        if isinstance(value, Ramp):
            return max(0.0, Ramp(0.0, value.per_min, value.cap).at(self.elapsed() - cleared_at))
        return 0.0

    def _codex_window(self, a: CodexAccount, key: str, w: Window, seconds: int) -> dict:
        reset_at = self._resets_at(w.resets_in).timestamp()
        return {
            "used_percent": round(self._codex_pct(a, key, w.pct), 1),
            "limit_window_seconds": seconds,
            "reset_after_seconds": int(reset_at - self.clock()),
            "reset_at": int(reset_at),
        }

    def _codex_available(self, a: CodexAccount) -> list[CodexCredit]:
        return [
            c for c in a.credits
            if c.status == "available" and (a.account_id, c.id) not in self.codex_spent
        ]

    def codex_usage_payload(self, a: CodexAccount) -> dict:
        windows = []
        if a.five_hour is not None:
            windows.append(self._codex_window(a, "five_hour", a.five_hour, CODEX_FIVE_HOUR_S))
        if a.seven_day is not None:
            windows.append(self._codex_window(a, "seven_day", a.seven_day, CODEX_WEEK_S))
        primary = windows[0] if windows else None
        secondary = windows[1] if len(windows) > 1 else None
        payload: dict = {
            "plan_type": a.plan,
            "rate_limit": {
                "allowed": a.allowed,
                "limit_reached": not a.allowed,
                "primary_window": primary,
                "secondary_window": secondary,
            },
            "additional_rate_limits": None,
            "credits": None,
            "email": a.email,
        }
        if a.credits:
            count = len(self._codex_available(a))
            payload["rate_limit_reset_credits"] = {
                "available_count": count,
                "applicable_available_count": count,
            }
        return payload

    def codex_credits_payload(self, a: CodexAccount) -> dict:
        credits = []
        for c in a.credits:
            spent = (a.account_id, c.id) in self.codex_spent
            credits.append({
                "id": c.id,
                "title": c.title,
                "status": "redeemed" if spent else c.status,
                "expires_at": iso(self.scenario.clock + c.expires_in) if c.expires_in else None,
                "is_supported_by_plan": c.supported,
            })
        return {"credits": credits}

    def codex_consume(self, a: CodexAccount, credit_id: str) -> dict:
        """The consume answer: ``reset`` refills the windows that hold usage."""
        if (a.account_id, credit_id) in self.codex_spent:
            return {"code": "already_redeemed"}
        if not any(c.id == credit_id for c in self._codex_available(a)):
            return {"code": "no_credit"}
        used = {
            key: self._codex_pct(a, key, w.pct)
            for key, w in (("five_hour", a.five_hour), ("seven_day", a.seven_day))
            if w is not None
        }
        refilled = [key for key, pct in used.items() if pct > 0]
        if not refilled:
            return {"code": "nothing_to_reset"}
        self.codex_spent.add((a.account_id, credit_id))
        self.codex_cleared.setdefault(a.account_id, {}).update(
            {key: self.elapsed() for key in refilled}
        )
        return {"code": "reset", "windows_reset": len(refilled)}


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
# A weekly reset: clears the all-models weekly window.
WEEKLY_RESET = Grant(
    id="tenure-12m-weekly",
    label="One weekly reset at 12 months' tenure",
    clears=("seven_day",),
    type="weekly",
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


CODEX_RESET_CREDIT = CodexCredit(
    id="rlrc_demo_2611_northwind",
    title="Rate limit reset",
    expires_in=12 * D,
)


def _fleet_extra() -> tuple[ClaudeAccount | CodexAccount, ...]:
    """The rest of the fleet: an API-key account and two Codex accounts."""
    return (
        # `qswap add-token sk-ant-api03-...` with no --email names it this way.
        ClaudeAccount(
            slot=5,
            email="api-key-5@token.local",
            alias="ci",
            api_key=True,
        ),
        CodexAccount(
            slot=1,
            email="maya@example.com",
            plan="plus",
            five_hour=Window(22, 3 * H + 40 * M),
            seven_day=Window(47, 4 * D + 3 * H),
        ),
        CodexAccount(
            slot=2,
            email="maya@northwind.example.com",
            alias="work",
            plan="team",
            org_name="Northwind Labs",
            five_hour=Window(8, 4 * H + 55 * M),
            seven_day=Window(15, 6 * D + 2 * H),
            credits=(CODEX_RESET_CREDIT,),
        ),
    )


# What `qswap codex login` logs in (through the faked `codex login`).
CODEX_NEW_LOGIN = CodexAccount(
    slot=3,
    email="ops@example.net",
    plan="pro",
    seven_day=Window(5, 6 * D + 20 * H),
)


_DARK = {"ui": {"theme": "dark"}, "updates": {"check": False}}

FLEET = Scenario(
    name="fleet",
    title="Four Claude accounts on different plans, an API key and two Codex accounts",
    clock=CLOCK,
    accounts=_fleet_accounts() + _fleet_extra(),
    active=1,
    codex_active=1,
    codex_login=CODEX_NEW_LOGIN,
    settings=_DARK,
    # Every account was read two minutes ago (inside the store's three-minute
    # serve window, so `list` shows the same numbers with no age note), as a
    # menu bar or an earlier `list` would have: `auto --once` then compares
    # real readings instead of accounts it has never fetched.
    warmup=2 * M,
    notes=(
        "Claude: one near its limit (3), one holding resets (1, 3), one disabled (4), "
        "an API key (5). Codex: 1 live at a moderate weekly figure, 2 saved with a "
        "reset credit."
    ),
)


# Claude Code projects for the session histories (``usage``, ``showcase``,
# ``autoswitch``).
_PROJECTS = (
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
)


def _autoswitch_accounts() -> tuple[ClaudeAccount, ...]:
    a1, a2, a3, _a4 = _fleet_accounts()
    return (
        # 76% at the warm-up read, then 86%, 88.5%, 91%: rising inside 15
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
    # Four minutes, past the store's three-minute serve window, so the first
    # check always reads afresh (86%). At exactly three minutes the seconds a
    # run takes decided whether it saw the warm-up reading instead.
    warmup=4 * M,
    # A week of sessions, so the menu bar's cost row is its seven-day chart
    # (the menu bar recording): mornings on #1, middays on #2, evenings on #1
    # (#3 every third day).
    ledger=Ledger(
        days=7,
        projects=_PROJECTS,
        switches=tuple(
            (d, h, slot)
            for d in range(7, -1, -1)
            for h, slot in ((8, 1), (12, 2), (17, 1 if d % 3 else 3))
        ),
    ),
)


def _reset_accounts() -> tuple[ClaudeAccount, ...]:
    a1, a2, a3, a4 = _fleet_accounts()
    return (
        replace(
            a1,
            five_hour=Window(31, 3 * H + 5 * M),
            seven_day=Window(74, 3 * D + 6 * H),
            models=(ModelWindow("Fable", 100, 3 * D + 6 * H, share_of_weekly=22),),
            # A Fable reset and a weekly reset: the planner spends the Fable
            # one, the narrowest that clears the limit reached.
            resets=Resets(grants=(FABLE_RESET, WEEKLY_RESET)),
        ),
        replace(a2, resets=Resets(eligible=False, ineligible_reason="plan")),
        replace(a3, resets=Resets(grants=())),
        a4,
    )


RESET = Scenario(
    name="reset",
    title="A per-model window at 100%, with a Fable reset and a weekly reset held",
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
    accounts=_fleet_accounts() + _fleet_extra(),
    active=1,
    codex_active=1,
    settings=_DARK,
    ledger=Ledger(
        days=14,
        projects=_PROJECTS,
        switches=tuple(
            (d, h, slot)
            for d in range(15, -1, -1)
            for h, slot in ((8, 1), (13, 2), (19, 1) if d % 3 else (19, 3))
        ),
        codex_projects=(
            LedgerProject("/work/checkout-api", 0.08, (("gpt-6.1-sol", 0.8), ("gpt-6.1-terra", 0.2))),
            LedgerProject("/work/data-pipeline", 0.04, (("gpt-6.1-sol", 1.0),)),
        ),
        # Codex account 2 (the workspace) mornings on weekdays, 1 otherwise.
        codex_switches=tuple(
            (d, h, slot)
            for d in range(15, -1, -1)
            for h, slot in ((9, 2), (17, 1))
        ),
        daily_scale=2.0,
    ),
)


# -- the first day of the menu bar: unused API keys -----------------------------

WEEKLY_RESET = Grant(
    id="launch-2611-weekly",
    label="One weekly reset",
    clears=("seven_day",),
    type="weekly",
    ends_in=40 * D,
)

NEW_KEYS = Scenario(
    name="new-keys",
    title="Three API keys just added without an email, beside an account holding resets",
    clock=CLOCK,
    accounts=(
        # A long address (the menu row cuts it), near its weekly limit, holding
        # resets of two kinds: three Fable resets on one grant and a weekly one.
        ClaudeAccount(
            slot=1,
            email="maya.lindqvist@northwind.example.com",
            plan="max20",
            seven_day=Window(98, 2 * D + 5 * H),
            models=(ModelWindow("Fable", 27, 2 * D + 5 * H, share_of_weekly=9),),
            resets=Resets(grants=(
                replace(FABLE_RESET, resets_left=3, resets_total=3), WEEKLY_RESET,
            )),
        ),
        # `qswap add-token sk-ant-api03-...` three times, no --email, never used.
        *(ClaudeAccount(slot=n, email=f"api-key-{n}@token.local", api_key=True) for n in (5, 6, 7)),
    ),
    active=1,
    settings=_DARK,
    warmup=2 * M,
    notes=(
        "The maintainer's first-day menu: #1 at 98% weekly holding 3 Fable resets and 1 "
        "weekly reset; #5-#7 API keys added without an email and never used (no ledger "
        "until the menu bar's first scan)."
    ),
)


# -- the README's hero: every kind of account, with history ----------------------


def _showcase_accounts() -> tuple[ClaudeAccount | CodexAccount, ...]:
    a1, a2, a3, a4 = _fleet_accounts()
    extra = _fleet_extra()
    return (
        a1,  # active, holds a Fable reset
        a2,  # Team, extra-usage spend
        a3,  # near its limit
        a4,  # disabled
        replace(extra[0], budget=40.0),  # API key with spend (from the ledger) and a budget
        *extra[1:],  # Codex: the active account, and a saved one holding a reset credit
    )


def _showcase_switches() -> tuple[tuple[int, int, int], ...]:
    """Which Claude account was active when: mornings on #1, middays on #2,
    evenings on #1 (#3 every third day), and on weekdays the API key #5 for
    CI runs at nine and mid-afternoon, today included."""
    out = []
    for d in range(15, -1, -1):
        weekday = d % 7 not in (2, 3)  # the clock is a Tuesday: 2 and 3 days ago are the weekend
        out.append((d, 8, 1))
        if weekday:
            out += [(d, 9, 5), (d, 10, 1)]
        out.append((d, 12, 2))
        if weekday:
            out.append((d, 15, 5))
        out.append((d, 17, 1 if d % 3 else 3))
    return tuple(out)


SHOWCASE = Scenario(
    name="showcase",
    title="Every kind of account, with two weeks of history: the README's hero",
    clock=CLOCK,
    accounts=_showcase_accounts(),
    active=1,
    codex_active=1,
    codex_login=CODEX_NEW_LOGIN,
    settings=_DARK,
    warmup=2 * M,
    ledger=Ledger(
        days=14,
        projects=USAGE.ledger.projects,
        switches=_showcase_switches(),
        codex_projects=USAGE.ledger.codex_projects,
        codex_switches=USAGE.ledger.codex_switches,
        daily_scale=1.0,
        prescan=True,
    ),
    notes=(
        "Claude: 1 active holding a Fable reset, 2 Team with extra usage, 3 near its "
        "limit, 4 disabled, 5 an API key with spend and a $40 budget. Codex: 1 active, "
        "2 holding a reset credit. Two weeks of sessions for the cost line and usage."
    ),
)


# -- an API key as the last resort ----------------------------------------------


def _last_resort_accounts() -> tuple[ClaudeAccount | CodexAccount, ...]:
    a1, a2, a3, a4, key, *codex = _showcase_accounts()
    return (
        # Every subscription at its limit, none holding a usage reset.
        replace(a1, five_hour=Window(100, 1 * H + 12 * M), models=(), resets=Resets(grants=())),
        replace(a2, five_hour=Window(100, 2 * H + 40 * M), extra_usage=None),
        replace(a3, five_hour=Window(100, 38 * M), models=(), resets=Resets(grants=())),
        a4,
        key,  # ci, with the $40 budget and the showcase's spend in the ledger
        *codex,
    )


LAST_RESORT = Scenario(
    name="last-resort",
    title="Every subscription at its limit; auto-switch may use the API key",
    clock=CLOCK,
    accounts=_last_resort_accounts(),
    active=1,
    codex_active=1,
    settings={**_DARK, "autoswitch": {"includeApiKeyAccounts": True}},
    warmup=2 * M,
    ledger=SHOWCASE.ledger,
    notes=(
        "Claude: 1, 2 and 3 at 100% of their 5-hour windows with no resets, 4 "
        "disabled, 5 an API key with a $40 budget and the showcase's spend. "
        "autoswitch.includeApiKeyAccounts is on."
    ),
)


# -- first run: nothing managed yet ---------------------------------------------

_FIRST_LOGIN = ClaudeAccount(
    slot=1,
    email="maya@example.com",
    plan="max20",
    five_hour=Window(18, 3 * H + 10 * M),
    seven_day=Window(26, 4 * D + 2 * H),
)
_FIRST_CODEX_LOGIN = CodexAccount(
    slot=1,
    email="maya@example.com",
    plan="plus",
    five_hour=Window(9, 4 * H + 30 * M),
    seven_day=Window(31, 5 * D + 1 * H),
)

EMPTY = Scenario(
    name="empty",
    title="First run: no Claude Code login, nothing managed",
    clock=CLOCK,
    accounts=(),
    active=None,
    settings={},  # no settings.json: every default, the update check included
    notes="A machine where Claude Code was never logged in and qswap never ran.",
)

EMPTY_CLAUDE = replace(
    EMPTY,
    name="empty-claude",
    title="First run: a live Claude Code login that qswap does not manage yet",
    live=_FIRST_LOGIN,
    notes="Claude Code is logged in as maya@example.com; qswap has never run.",
)

EMPTY_CLAUDE_CODEX = replace(
    EMPTY_CLAUDE,
    name="empty-claude-codex",
    title="First run: live Claude Code and Codex logins, nothing managed",
    codex_live=_FIRST_CODEX_LOGIN,
    notes="Claude Code and Codex are both logged in as maya@example.com; qswap has never run.",
)

# The quick start's second step: the first account saved with `qswap add`,
# then `/login` in Claude Code with the second, which qswap does not manage
# yet. `qswap add` saves it as #2, and `qswap switch 1` goes back.
QUICKSTART = Scenario(
    name="quickstart",
    title="One account saved; Claude Code now logged in to a second one",
    clock=CLOCK,
    accounts=(replace(_FIRST_LOGIN, alias="personal"),),
    active=1,
    live=ClaudeAccount(
        slot=2,
        email="maya@northwind.example.com",
        org_name="Northwind Labs",
        plan="team",
        five_hour=Window(71, 1 * H + 40 * M),
        seven_day=Window(44, 5 * D + 2 * H),
    ),
    settings=_DARK,
    notes=(
        "#1 personal saved (Max 20x, 5h 18%); Claude Code logged in as "
        "maya@northwind.example.com (Team, 5h 71%), not managed yet."
    ),
)


# -- failure states ----------------------------------------------------------------

TROUBLE = Scenario(
    name="trouble",
    title="Accounts in failure states",
    clock=CLOCK,
    accounts=(
        # Active, at 100% of its 5-hour window, eligible for resets but holding none.
        ClaudeAccount(
            slot=1,
            email="maya@example.com",
            alias="personal",
            plan="max20",
            five_hour=Window(100, 1 * H + 12 * M),
            seven_day=Window(64, 3 * D + 6 * H),
            resets=Resets(grants=()),
        ),
        # Its saved login is dead: the access token expired two days ago and
        # the token endpoint refuses the refresh token.
        ClaudeAccount(
            slot=2,
            email="maya@northwind.example.com",
            alias="work",
            org_name="Northwind Labs",
            plan="team",
            five_hour=Window(12, 4 * H + 10 * M),
            seven_day=Window(21, 5 * D + 2 * H),
            token_expires_in=-2 * D,
            refresh_dead=True,
        ),
        # Its usage endpoint answers 503, at the warm-up read and now.
        ClaudeAccount(
            slot=3,
            email="ops@example.net",
            alias="night-shift",
            plan="max5",
            five_hour=Window(35, 2 * H),
            seven_day=Window(48, 2 * D),
            usage_failure=UsageFailure(503),
        ),
        # Quarantined by the auto-switch engine (dead refresh token found while
        # freshening it as a switch target); its access token still works.
        ClaudeAccount(
            slot=4,
            email="sam@example.org",
            alias="side",
            plan="pro",
            five_hour=Window(5, 4 * H + 40 * M),
            seven_day=Window(30, 4 * D),
            token_expires_in=5 * H,
            refresh_dead=True,
            quarantine="invalid_grant",
        ),
        # Read fine three hours ago; every read since times out.
        ClaudeAccount(
            slot=5,
            email="lee@example.net",
            alias="spare",
            plan="max5",
            five_hour=Window(40, 2 * H + 30 * M),
            seven_day=Window(55, 3 * D),
            usage_failure=UsageFailure("timeout", from_clock=True),
        ),
        # Codex: the live account at 100% of its weekly window, no credits...
        CodexAccount(
            slot=1,
            email="maya@example.com",
            plan="plus",
            five_hour=Window(41, 2 * H + 5 * M),
            seven_day=Window(100, 1 * D + 7 * H),
            allowed=False,
        ),
        # ...and a saved one whose access token expired while it sat unused.
        CodexAccount(
            slot=2,
            email="maya@northwind.example.com",
            alias="work",
            plan="team",
            org_name="Northwind Labs",
            five_hour=Window(3, 4 * H),
            seven_day=Window(12, 6 * D),
            token_expires_in=-1 * D,
        ),
    ),
    active=1,
    codex_active=1,
    warmup=3 * H + 5 * M,  # past three hours, so the age reads "3h ago"
    settings=_DARK,
    notes=(
        "Claude: 1 at 100% with no resets, 2 dead login, 3 failing endpoint, "
        "4 quarantined, 5 stale reading. Codex: 1 at 100% weekly, 2 expired token."
    ),
)


SCENARIOS: dict[str, Scenario] = {
    s.name: s for s in (
        FLEET, AUTOSWITCH, RESET, RESET_AFTER, USAGE, SHOWCASE, NEW_KEYS, LAST_RESORT,
        EMPTY, EMPTY_CLAUDE, EMPTY_CLAUDE_CODEX, QUICKSTART, TROUBLE,
    )
}


def get(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise SystemExit(
            f"unknown scenario {name!r}; known: {', '.join(sorted(SCENARIOS))}"
        ) from None
