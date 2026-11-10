"""The shared auto-switch engine over a real CodexSwitcher (M2b).

Each engine stage is driven against Codex's realities with synthetic logins,
a temp ``CODEX_HOME``, a faked usage fetch and a faked consume call; nothing
here can reach the network (the suite blocks it) or start ``codex``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_quota_swap import autoswitch
from ai_quota_swap.autoswitch import (
    MAX_AUTO_RESETS_PER_DAY,
    AutoSwitchEngine,
    ErrorEvent,
    NoSwitchEvent,
    QuarantineEvent,
    ResetEvent,
    SwitchEvent,
    TickOutcome,
    record_reset_claim,
    reset_claims_today,
)
from ai_quota_swap.codex import processes as codex_processes
from ai_quota_swap.codex import usage as codex_usage
from ai_quota_swap.codex.processes import CodexProcess
from ai_quota_swap.codex.switcher import CodexSwitcher
from ai_quota_swap.codex.usage import UsageOutcome, credits_status, normalize_usage
from ai_quota_swap.exceptions import SwitchError
from ai_quota_swap.json_output import USAGE_TOKEN_EXPIRED, USAGE_UNKNOWN_UNTIL_USED
from ai_quota_swap.resets import ClaimResult
from ai_quota_swap.settings import AutoSwitchSettings, set_setting
from tests.codex_helpers import (
    DAY,
    LOGIN_A,
    LOGIN_B,
    LOGIN_C,
    FakeFetch,
    make_login,
    usage_payload,
    window,
    write_live,
)

WEEK_S = 604_800
FIVE_H_S = 18_000


class FakeClock:
    """Wall-clock-anchored fake: JWT ``exp`` values and reset times built from
    ``time.time()`` stay meaningful, while the engine's clock is ours."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def credit(cid: str = "rlrc_6f1b0d8e", *, expires_in: float | None = 60 * DAY, now: float) -> dict:
    expires = None
    if expires_in is not None:
        from datetime import datetime, timezone

        expires = datetime.fromtimestamp(now + expires_in, tz=timezone.utc).isoformat()
    return {
        "id": cid, "reset_type": "codex_rate_limits", "status": "available",
        "expires_at": expires, "title": "Full reset", "is_supported_by_plan": True,
    }


def reading(
    weekly: float,
    *,
    five_hour: float | None = None,
    credits: list[dict] | None = None,
    allowed: bool | None = None,
    now: float,
    weekly_reset_in: float = 3 * DAY,
) -> dict:
    """A normalized Codex reading at ``now``, with its credit list when given."""
    primary = window(FIVE_H_S, five_hour, 2 * 3600, now=now) if five_hour is not None else None
    weekly_w = window(WEEK_S, weekly, weekly_reset_in, now=now)
    payload = usage_payload(
        primary or weekly_w,
        weekly_w if primary else None,
        allowed=allowed if allowed is not None else max(weekly, five_hour or 0) < 100,
        credits=len(credits) if credits is not None else None,
    )
    usage = normalize_usage(payload, now=now)
    if credits:
        usage["resets"] = credits_status({"credits": credits}, usage)
    return usage


class Harness:
    def __init__(self, env, monkeypatch, **settings):
        self.env = env
        self.clock = FakeClock()
        self.sw = CodexSwitcher()
        self.sw._usage_store.clock = self.clock
        self.fetch = FakeFetch()
        monkeypatch.setattr(codex_usage, "fetch_usage", self.fetch)
        self.consumed: list[tuple[str, str, str]] = []
        self.consume_answer = ClaimResult("reset")
        # What the fetch answers for an account once a credit was spent on it.
        self.after_claim: dict[str, dict] = {}
        monkeypatch.setattr(codex_usage, "consume_reset_credit", self._consume)
        self.events: list = []
        self.settings = AutoSwitchSettings(**{"threshold": 99.0, **settings})
        self.engine = self.make_engine()

    def make_engine(self, **kw) -> AutoSwitchEngine:
        return AutoSwitchEngine(self.sw, self.settings, self.events.append, clock=self.clock, **kw)

    def _consume(self, access_token, account_id, credit_id, request_id, **_kw):
        self.consumed.append((account_id, credit_id, request_id))
        if self.consume_answer.ok and account_id in self.after_claim:
            self.answer(account_id, self.after_claim[account_id])
        return self.consume_answer

    def capture(self, login: dict, **kw) -> bytes:
        raw = make_login(**login, **kw)
        write_live(self.env.codex_home, raw)
        self.sw.add_account()
        return raw

    def make_live(self, raw: bytes) -> None:
        write_live(self.env.codex_home, raw)

    def answer(self, account_id: str, usage: dict) -> None:
        self.fetch.answers[account_id] = UsageOutcome(usage=usage)

    def tick(self) -> TickOutcome:
        return self.engine.tick()

    def of(self, cls) -> list:
        return [e for e in self.events if isinstance(e, cls)]

    def reasons(self) -> list[str]:
        return [e.reason for e in self.of(NoSwitchEvent)]

    def state(self) -> dict:
        path = self.sw.backup_dir / "autoswitch_state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def live_email(self) -> str:
        from ai_quota_swap.codex import auth as codex_auth

        return codex_auth.parse_login((self.env.codex_home / "auth.json").read_bytes()).email


@pytest.fixture
def env(temp_home: Path, tmp_path: Path, monkeypatch) -> SimpleNamespace:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return SimpleNamespace(home=temp_home, codex_home=codex_home.resolve())


@pytest.fixture
def h(env, monkeypatch) -> Harness:
    return Harness(env, monkeypatch)


@pytest.fixture
def pair(h) -> Harness:
    """B saved as account 1; A saved as account 2 and live."""
    h.b = h.capture(LOGIN_B)
    h.a = h.capture(LOGIN_A)
    return h


# -- the engine host ----------------------------------------------------------------


class TestEngineHost:
    def test_state_and_settings_roots(self, h):
        """Engine state is per provider; settings come from the shared root."""
        assert h.engine.state_path == h.sw.backup_dir / "autoswitch_state.json"
        assert h.sw.backup_dir == h.sw.data_root / "codex"
        assert autoswitch._state_lock_for(h.engine.state_path).lock_path.parent == h.sw.backup_dir

    def test_module_helpers_reach_only_the_switchers_root(self, pair):
        record_reset_claim(pair.sw.backup_dir, "2", {"at": pair.clock.now, "result": "reset", "auto": True})
        assert (pair.sw.backup_dir / "autoswitch_state.json").exists()
        assert not (pair.sw.data_root / "autoswitch_state.json").exists()
        assert autoswitch.preview_reset_plan(pair.sw, pair.settings, {}) is None


# -- engine stages against Codex ------------------------------------------------------


class TestExpiredSavedLogin:
    """A saved login whose access token expired reads "usage unknown until
    used": never an engine target, never an error, never a fetch loop."""

    def test_never_a_target_and_never_an_error(self, h):
        h.b = h.capture(LOGIN_B, exp=time.time() - 3600)
        h.a = h.capture(LOGIN_A)
        h.answer("acct-a", reading(100, now=h.clock.now))
        for _ in range(4):  # past unhealthy_ticks: still no failover onto it
            assert h.tick() is TickOutcome.BLOCKED
            h.clock.advance(61)
        assert h.of(SwitchEvent) == [] and h.of(ErrorEvent) == [] and h.of(QuarantineEvent) == []
        assert set(h.reasons()) == {"no-comparison"}
        assert "quarantine" not in h.state()
        assert {c[1] for c in h.fetch.calls} == {"acct-a"}  # B is never asked
        entries = h.sw.usage_entries_by_account(fetch=set())
        assert entries["1"].sentinel == USAGE_UNKNOWN_UNTIL_USED
        assert h.live_email() == "a@example.com"

    def test_a_readable_peer_is_chosen_instead(self, h):
        h.capture(LOGIN_B, exp=time.time() - 3600)
        h.capture(LOGIN_C)
        a = h.capture(LOGIN_A)
        h.answer("acct-a", reading(100, now=h.clock.now))
        h.answer("acct-c", reading(20, now=h.clock.now))
        assert h.tick() is TickOutcome.SWITCHED
        (switch,) = h.of(SwitchEvent)
        assert switch.to_ref == {"number": 2, "email": "c@example.com"}
        assert switch.trigger == "at-limit"
        assert h.live_email() == "c@example.com"
        assert (h.sw.credentials_dir / "account-3.json").read_bytes() == a


class TestActiveTokenExpiredWhileIdle:
    def test_idle_hold_not_failover(self, h):
        h.capture(LOGIN_B)
        h.capture(LOGIN_A, exp=time.time() - 60)
        h.answer("acct-b", reading(10, now=h.clock.now))
        for _ in range(5):
            assert h.tick() is TickOutcome.NO_ACTION
            assert h.engine._idle_hold_slow
            assert h.engine._next_delay(TickOutcome.NO_ACTION) >= autoswitch.NO_RESET_FALLBACK_S
            h.clock.advance(300)
        assert h.of(SwitchEvent) == [] and h.of(ErrorEvent) == []
        idle = h.of(NoSwitchEvent)[-1]
        assert idle.reason == "active-idle"
        assert idle.detail == "token expired while Codex is idle; resumes on next use"
        assert h.sw.usage_entries_by_account(fetch=set())["2"].sentinel == USAGE_TOKEN_EXPIRED
        assert h.live_email() == "a@example.com"
        assert "acct-a" not in {c[1] for c in h.fetch.calls}  # the expired token is never sent


class TestRetryableSwitch:
    """``switch_to`` refusing with RetryableSwitchError (the live token is
    about to be renewed by a running Codex) is "try again next tick"."""

    @pytest.fixture
    def near(self, h, monkeypatch):
        h.capture(LOGIN_B)
        h.capture(LOGIN_A, exp=time.time() + 300)  # inside the 10-minute guard
        h.answer("acct-a", reading(100, now=h.clock.now))
        h.answer("acct-b", reading(10, now=h.clock.now))
        running = [CodexProcess(4242, codex_processes.CLI, h.env.codex_home)]
        monkeypatch.setattr(codex_processes, "list_codex_processes", lambda: running)
        h.running = running
        return h

    def test_deferred_not_an_error_and_not_a_quarantine(self, near):
        assert near.tick() is TickOutcome.NO_ACTION
        deferred = near.of(NoSwitchEvent)[-1]
        assert deferred.reason == "switch-deferred"
        assert "about to expire" in deferred.detail and "4242" in deferred.detail
        assert deferred.to_json()["event"] == "no-switch"
        assert near.of(ErrorEvent) == [] and near.of(QuarantineEvent) == []
        state = near.state()
        assert "quarantine" not in state and "lastSwitchAt" not in state
        assert near.live_email() == "a@example.com"

    def test_the_next_tick_switches_once_codex_is_done(self, near):
        near.tick()
        near.running.clear()
        near.clock.advance(61)
        assert near.tick() is TickOutcome.SWITCHED
        assert near.live_email() == "b@example.com"

    def test_any_other_switch_error_is_still_an_error_event(self, pair, monkeypatch):
        pair.answer("acct-a", reading(100, now=pair.clock.now))
        pair.answer("acct-b", reading(10, now=pair.clock.now))

        def boom(*a, **k):
            raise SwitchError("disk full")

        monkeypatch.setattr(pair.sw, "switch_to", boom)
        assert pair.tick() is TickOutcome.ERROR
        assert [e.message for e in pair.of(ErrorEvent)] == ["disk full"]


class TestSingleAccount:
    def test_runs_never_switches_and_reaches_the_reset_step(self, h):
        h.capture(LOGIN_A)
        cr = credit(now=h.clock.now)
        h.answer("acct-a", reading(100, credits=[cr], now=h.clock.now))
        h.after_claim["acct-a"] = reading(0, credits=[], now=h.clock.now)
        assert h.tick() is TickOutcome.NO_ACTION  # refilled in the same tick: below the line
        assert [c[:2] for c in h.consumed] == [("acct-a", cr["id"])]
        (event,) = h.of(ResetEvent)
        assert event.used and event.plan["reason"] == "last-resort" and event.plan["window"] == "7d"
        assert event.plan["projected"] == {"7d": 0.0}
        assert h.of(SwitchEvent) == []

    def test_without_a_credit_it_waits(self, h):
        h.capture(LOGIN_A)
        h.answer("acct-a", reading(100, now=h.clock.now))
        assert h.tick() is TickOutcome.BLOCKED
        assert h.reasons() == ["no-candidates"]
        assert h.of(SwitchEvent) == [] and h.of(ErrorEvent) == [] and h.consumed == []

    def test_below_every_line_nothing_happens(self, h):
        h.capture(LOGIN_A)
        h.answer("acct-a", reading(90, credits=[credit(now=h.clock.now)], now=h.clock.now))
        assert h.tick() is TickOutcome.NO_ACTION
        assert h.reasons() == ["below-threshold"] and h.consumed == []

    def test_the_reset_line_is_resets_threshold_not_the_switch_line(self, h):
        """resets.* mean what they mean for Claude Code: with nowhere to
        switch, last-resort spends once the active account reaches
        resets.threshold (95), even below codex.threshold (99)."""
        h.capture(LOGIN_A)
        h.answer("acct-a", reading(96, credits=[credit(now=h.clock.now)], now=h.clock.now))
        h.after_claim["acct-a"] = reading(0, credits=[], now=h.clock.now)
        assert h.tick() is TickOutcome.NO_ACTION
        assert len(h.consumed) == 1
        set_setting(h.sw.data_root, "resets.threshold", "100")
        h.clock.advance(2 * 3600)
        h.answer("acct-a", reading(97, credits=[credit("rlrc_second", now=h.clock.now)], now=h.clock.now))
        h.tick()
        assert len(h.consumed) == 1


class TestEventText:
    def test_unmanaged_live_login_names_the_codex_command(self, pair):
        pair.make_live(make_login(**LOGIN_C))
        assert pair.tick() is TickOutcome.NO_ACTION
        (event,) = pair.of(NoSwitchEvent)
        assert event.reason == "unmanaged-active-account"
        assert event.detail == "run 'qswap codex add' to include it in rotation"

    def test_a_corrupt_saved_login_is_never_a_target(self, pair):
        """It reads as "no credentials", so it is never ranked, freshened or
        quarantined; the engine just has no target."""
        pair.answer("acct-a", reading(100, now=pair.clock.now))
        (pair.sw.credentials_dir / "account-1.json").write_text("{not a login")
        assert pair.tick() is TickOutcome.BLOCKED
        assert pair.of(SwitchEvent) == [] and pair.of(QuarantineEvent) == []
        assert pair.of(ErrorEvent) == []

    def test_quarantine_names_the_codex_recovery(self, pair):
        pair.engine._quarantine("1", "b@example.com", "invalid_grant")
        (event,) = pair.of(QuarantineEvent)
        assert event.reason == "invalid_grant"
        assert event.human().endswith("Log in to it again with 'qswap codex login' to recover.")
        assert set(event.to_json()) == {"schemaVersion", "event", "ts", "number", "email", "reason"}


# -- reset credits through the engine (equivalents of tests/test_resets.py) -----------


class TestEngineResetCredits:
    def setup_pair(self, h, *, active_pct=100.0, peer_pct=10.0, credits=None):
        h.capture(LOGIN_B)
        h.capture(LOGIN_A)
        h.cr = credit(now=h.clock.now)
        cr_list = [h.cr] if credits is None else credits
        h.answer("acct-a", reading(active_pct, credits=cr_list, now=h.clock.now))
        h.answer("acct-b", reading(peer_pct, now=h.clock.now))
        return h

    def test_spends_at_the_limit_in_active_mode(self, h):
        set_setting(h.sw.data_root, "resets.auto", "active")
        self.setup_pair(h)
        h.after_claim["acct-a"] = reading(0, credits=[], now=h.clock.now)
        assert h.tick() is TickOutcome.NO_ACTION
        ((account_id, credit_id, request_id),) = h.consumed
        assert (account_id, credit_id) == ("acct-a", h.cr["id"])
        assert codex_usage.is_uuid4(request_id)
        (event,) = h.of(ResetEvent)
        assert event.used and event.plan["reason"] == "active-limit"
        assert h.of(SwitchEvent) == [] and h.live_email() == "a@example.com"
        state = h.state()
        assert state["resetClaims"]["2"]["result"] == "reset"
        assert state["resetClaims"]["2"]["requestId"] == request_id
        assert state["resetLog"][-1]["account"] == "2"

    def test_no_spend_when_a_switch_lands_elsewhere(self, h):
        self.setup_pair(h)  # last-resort (default), and B has room
        assert h.tick() is TickOutcome.SWITCHED
        assert h.consumed == [] and h.of(ResetEvent) == []
        assert h.live_email() == "b@example.com"

    @pytest.mark.parametrize("answer", [ClaimResult("reset"), ClaimResult("unavailable", reason="no_credit")])
    def test_no_second_spend_on_a_stale_reading(self, h, answer):
        self.setup_pair(h, peer_pct=100.0)  # nowhere to go: last-resort spends
        h.consume_answer = answer  # and the server keeps reporting the old numbers
        h.tick()
        assert len(h.consumed) == 1
        for _ in range(3):
            h.clock.advance(61)
            h.tick()
        assert len(h.consumed) == 1

    def test_dry_run_spends_nothing(self, h):
        self.setup_pair(h, peer_pct=100.0)
        h.engine = h.make_engine(dry_run=True)
        h.tick()
        assert h.consumed == []
        (event,) = h.of(ResetEvent)
        assert event.dry_run and event.human().startswith("[dry-run] would use a weekly reset")
        assert "resetClaims" not in h.state()

    def test_unknown_outcome_counts_and_retries_with_the_same_request_id(self, h):
        self.setup_pair(h, peer_pct=100.0)
        h.consume_answer = ClaimResult("error", detail="network")
        h.tick()
        state = h.state()
        assert state["resetClaims"]["2"]["result"] == "error"
        assert reset_claims_today(state, h.clock.now) == 1  # counts against the cap
        h.clock.advance(5 * 60)
        h.tick()
        assert len(h.consumed) == 1  # inside the retry gap
        h.clock.advance(6 * 60)
        h.consume_answer = ClaimResult("reset")
        h.tick()
        assert len(h.consumed) == 2
        assert h.consumed[0][2] == h.consumed[1][2]
        assert codex_usage.is_uuid4(h.consumed[0][2])

    def test_a_settled_answer_gets_a_fresh_request_id(self, h):
        self.setup_pair(h, peer_pct=100.0)
        h.consume_answer = ClaimResult("ineligible", reason="nothing_to_reset")
        h.tick()
        h.clock.advance(61 * 60)
        h.tick()
        assert len(h.consumed) == 2 and h.consumed[0][2] != h.consumed[1][2]

    def test_two_engines_reserving_at_once_spend_once(self, h, monkeypatch):
        self.setup_pair(h, peer_pct=100.0)
        other_id = codex_usage.new_request_id()
        real_refresh = h.sw.refresh_usage

        def other_engine_claims_first(nums):
            # Another engine (menu bar, a cron --once) reserves between this
            # tick's snapshot and its locked reservation.
            record_reset_claim(h.sw.backup_dir, "2", {
                "at": h.clock.now, "grantId": h.cr["id"], "requestId": other_id,
                "result": "pending", "settled": False, "auto": True,
            })
            return real_refresh(nums)

        monkeypatch.setattr(h.sw, "refresh_usage", other_engine_claims_first)
        h.tick()
        assert h.consumed == []
        assert h.state()["resetClaims"]["2"]["requestId"] == other_id

    def test_a_second_engine_after_a_spend_does_not_spend_again(self, h):
        self.setup_pair(h, peer_pct=100.0)
        h.tick()
        second = h.make_engine()
        h.clock.advance(30)
        second.tick()
        assert len(h.consumed) == 1

    def test_daily_cap_is_per_provider(self, h):
        self.setup_pair(h, peer_pct=100.0)
        # Three automatic spends in Claude Code's state do not count for Codex.
        for i in range(MAX_AUTO_RESETS_PER_DAY):
            record_reset_claim(h.sw.data_root, "1", {
                "at": h.clock.now - 60 * (i + 1), "grantId": "g", "result": "reset", "auto": True,
            })
        h.tick()
        assert len(h.consumed) == 1

    def test_daily_cap_stops_codex_after_three(self, h):
        self.setup_pair(h, peer_pct=100.0)
        for i in range(MAX_AUTO_RESETS_PER_DAY):
            record_reset_claim(h.sw.backup_dir, "1", {
                "at": h.clock.now - 60 * (i + 1), "grantId": "g", "result": "reset", "auto": True,
            })
        h.tick()
        assert h.consumed == []

    def test_resets_settings_come_from_the_shared_root(self, h):
        self.setup_pair(h, peer_pct=100.0)
        # A settings.json beside Codex's own data is not read.
        (h.sw.backup_dir / "settings.json").write_text(json.dumps({"resets": {"auto": "off"}}))
        h.tick()
        assert len(h.consumed) == 1
        set_setting(h.sw.data_root, "resets.auto", "off")
        h.clock.advance(2 * 3600)
        h.events.clear()
        h.tick()
        assert len(h.consumed) == 1 and h.of(ResetEvent) == []

    def test_a_credit_on_an_expired_saved_login_is_not_planned(self, h):
        # The account cannot be read ("unknown until used"), so the planner has
        # no numbers to justify a spend on it.
        h.capture(LOGIN_B, exp=time.time() - 60)
        h.capture(LOGIN_A)
        h.answer("acct-a", reading(100, now=h.clock.now))
        h.tick()
        assert h.consumed == []
