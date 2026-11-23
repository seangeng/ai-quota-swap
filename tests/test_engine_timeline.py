"""Acceptance timelines for the auto-switch engine (PR #13, round 4).

One long scripted day per test, on a fleet of four subscriptions and three
API keys, with made-up names:

- four subscriptions: #1 maya@example.com (the live login, active, holding
  one weekly reset), #2 work, #3 night, #4 side;
- three API keys: #5 credit (a monthly budget of $1,200 and a credit of
  $1,187.03 recorded today), #6 build and #7 spare (a budget of $200 each);
- settings: resets.auto active, resets.threshold 100, autoswitch.threshold 98,
  autoswitch.includeApiKeyAccounts true (everything else the default: a
  cooldown of 300 s, the `best` strategy).

The real engine runs tick by tick on the suite's fake clock, one tick a
minute. Before each tick the sessions work for a minute on whichever account
is active: a subscription's windows fill, an API key's spend lands in this
machine's transcripts. What the engine reads is built by the real code
wherever the suite allows it: the keys' spend readings come from
``api_spend.readings`` over a real ledger database (``ledger.db``), priced by
the real price table, with tokens of a model that has no price charged at the
dearest rate. The fakes are the seams the suite already uses: the ledger
scan (``scan_recent``; here it moves the transcript lines written so far
into the ledger and stamps the fake clock), the subscriptions' usage
endpoint (``usage_entries_by_account``, ``refresh_usage``) and the reset
claim (``claim_usage_reset``). The real claim (``resets.claim``) is made to
fail the test if anything reaches it. T7 goes further (issue #17): the real
scan over real session files, at the fake clock (its ``now``), and the
keys read through the switcher's ``_api_spend_readings`` with Anthropic's
Admin API faked, so an organization's key's figure ages on the fake clock.

Assertions run after every tick. The expectations are docs/auto-switch.md,
docs/api-accounts.md, docs/resets.md and the design's "As built"
(docs/design/api-key-sessions.md). Two expectations were corrected when the
file joined the suite: a key left because its scan could not cover the
period names that cause rather than an age (BRAKE_WORDS, USER_WORDS), and
the rows check after a failed scan also reads the holds that follow a key's
brake (``key-left``), so that it checks something on every timeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_quota_swap import api_spend, autoswitch, claude_settings, oauth, resets
from ai_quota_swap.autoswitch import (
    IDLE_HOLD_MAX_S,
    OFFLINE_RETRY_S,
    REACHABILITY_TTL_S,
    RETURN_ROOM_PCT,
    SPEND_SCAN_INTERVAL_S,
    SPEND_SCAN_MIN_GAP_S,
    AutoSwitchEngine,
    NoSwitchEvent,
    ResetEvent,
    SwitchEvent,
    TickOutcome,
    RepeatFailureFilter,
    kept_resets_now,
    preview_reset_plan,
)
from ai_quota_swap.json_output import (
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from ai_quota_swap.ledger import usage as ledger_usage
from ai_quota_swap.ledger.store import DB_FILENAME, LAST_SCAN_META, LedgerStore
from ai_quota_swap.resets import ClaimResult
from ai_quota_swap.settings import AutoSwitchSettings, load_engine_settings, set_setting
from ai_quota_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _iso_at

QSWAP = "/opt/qswap/bin/qswap"
KEY = "sk-ant-api03-" + "tl" * 20  # made up; never printed
UTC = timezone.utc
MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0
#: The morning the timelines start, and when #5's credit was recorded today.
T0 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC).timestamp()
CREDIT_AT = datetime(2026, 11, 11, 7, 30, tzinfo=UTC).timestamp()
LINE = 98.0          # autoswitch.threshold
COOLDOWN = 300.0     # autoswitch.cooldownSeconds (the default)

SUBS = {
    "1": ("maya@example.com", ""),
    "2": ("maya.work@example.com", "work"),
    "3": ("maya.night@example.com", "night"),
    "4": ("maya.side@example.com", "side"),
}
KEYS = {
    "5": ("key5@token.local", "credit"),
    "6": ("key6@token.local", "build"),
    "7": ("key7@token.local", "spare"),
}
PRICED = "claude-opus-5-5"      # $4 per million input tokens in the price table
UNPRICED = "claude-fable-5-3"   # not in the table: the dearest input rate, $16
RATE = {PRICED: 4.0, UNPRICED: 16.0}


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)


@pytest.fixture(autouse=True)
def _no_real_claim(monkeypatch):
    """Only the world's fake claim may run; the real one fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("the real usage-reset claim was reached")

    monkeypatch.setattr(resets, "claim", refuse)


# == The world ===============================================================================


def _weekly_grant(gid: str, now: float) -> dict:
    """One weekly reset, usable now, good for 120 days (far from its expiry
    runway, so only the normal trigger can spend it)."""
    return {
        "id": gid, "label": "", "resets_left": 1, "resets_total": 1, "starts_at": None,
        "ends_at": _iso_at(now + 120 * DAY), "clears": ["seven_day"], "blocking": [],
        "paused": False, "usable_now": True, "use_requires_limit": False,
        "gives_back_weekly": True, "type": "full", "unavailable_reason": None,
        "percent_used": {},
    }


@dataclass
class Sub:
    """One subscription's two windows and its resets."""

    h5: float
    d7: float
    h5_reset: float
    d7_reset: float
    grants: list = field(default_factory=list)
    fresh_window: bool = False  # the 5h window rolled over and has not been used since

    def usage(self, now: float) -> dict:
        h5_reset = now + 5 * HOUR if self.fresh_window else self.h5_reset
        return {
            "five_hour": {"pct": round(self.h5, 3), "resets_at": _iso_at(h5_reset)},
            "seven_day": {"pct": round(self.d7, 3), "resets_at": _iso_at(self.d7_reset)},
            "resets": {
                "eligible": True, "ineligible_reason": None, "at_limit": False,
                "exhausted": [],
                "next_grant_id": next(
                    (g["id"] for g in self.grants if g["resets_left"] > 0), None
                ),
                "weekly_resets_at": _iso_at(self.d7_reset), "cooldown_until": None,
                "grants": [dict(g) for g in self.grants],
            },
        }

    @property
    def room(self) -> float:
        return 100.0 - max(self.h5, self.d7)

    def resets_left(self) -> int:
        return sum(g["resets_left"] for g in self.grants)


class World:
    """The accounts as they really are, minute by minute.

    ``r5``/``r7`` are the points a minute of work adds to the active
    subscription's windows; ``key_rate`` the dollars a minute of work costs on
    the active key (``key_model``). ``hooks`` run after the work, each
    ``hook(world, t, active)``, for scripted events (a heavy turn, a model with
    no price, a scan that starts failing)."""

    def __init__(self, h: EngineHarness, subs: dict[str, Sub], *, r5: float = 1.6,
                 r7: float = 0.1, key_rate: float = 0.40):
        self.h = h
        self.subs = subs
        self.r5, self.r7, self.key_rate = r5, r7, key_rate
        self.key_model = PRICED
        self.hooks: list = []
        self.lines: list[tuple[float, str, str, int]] = []     # every transcript line
        self.pending: list[tuple[float, str, str, int]] = []   # not yet in the ledger
        self.scan_mode = "ok"   # "ok", "raise" or "unreadable" (files it cannot read)
        self.scans: list[tuple[float, float | None]] = []      # (when, from)
        self.claims: list[tuple[float, str, str]] = []
        self.infos = {
            n: (int(n), email, "", "", False, KEY, alias) for n, (email, alias) in KEYS.items()
        }
        self.t = -1

    @property
    def now(self) -> float:
        return self.h.clock.now

    # -- work -----------------------------------------------------------------------------

    def work(self, t: int) -> None:
        self.t = t
        now = self.now
        for sub in self.subs.values():
            if now >= sub.h5_reset and not sub.fresh_window:
                sub.h5, sub.fresh_window = 0.0, True
            if now >= sub.d7_reset:
                sub.d7, sub.d7_reset = 0.0, sub.d7_reset + 7 * DAY
        active = self.h.switcher.current_account_number()
        if active in self.subs and (self.r5 or self.r7):
            sub = self.subs[active]
            if sub.fresh_window:
                sub.fresh_window, sub.h5_reset = False, now + 5 * HOUR
            sub.h5 = min(100.0, sub.h5 + self.r5)
            sub.d7 = min(100.0, sub.d7 + self.r7)
        elif active in KEYS and self.key_rate:
            self.spend(active, self.key_rate, self.key_model)
        for hook in self.hooks:
            hook(self, t, active)

    def spend(self, num: str, dollars: float, model: str = PRICED, *,
              at: float | None = None) -> None:
        """``dollars`` of work on the key ``num``, written to its transcripts."""
        tokens = int(round(dollars / RATE[model] * 1_000_000))
        line = (self.now if at is None else at, num, model, tokens)
        self.lines.append(line)
        self.pending.append(line)

    def dollars(self, num: str, *, since: float | None = None, scanned: bool = True) -> float:
        """What the key ``num`` has spent this month (or since ``since``, by
        the hour as the ledger counts a credit), all of it or only what a
        scan has put in the ledger."""
        pending = set(map(id, self.pending)) if scanned else set()
        month = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
        start = month if since is None else int(since // HOUR) * HOUR
        return sum(
            tokens / 1_000_000 * RATE[model]
            for line in self.lines
            for ts, n, model, tokens in (line,)
            if n == num and ts >= start and id(line) not in pending
        )

    # -- what the engine reads ------------------------------------------------------------

    def ingest(self, root: Path) -> None:
        """The transcript lines written so far go into the ledger, stamped now."""
        store = LedgerStore(Path(root) / DB_FILENAME)
        try:
            rows = [
                ("claude", int(ts // HOUR), f"{KEYS[n][0]}|", model, "/work/demo",
                 f"sess-{n}", tokens, 0, 0, 0, 0, 0, 1)
                for ts, n, model, tokens in self.pending
            ]
            if rows:
                store.add_buckets(rows)
            store.set_meta(LAST_SCAN_META, f"{self.now:.3f}")
        finally:
            store.close()
        self.pending = []

    def scan(self, data_root, *, registry=None, start=None, **_kw):
        """The engine's ledger scan (``ledger.usage.scan_recent``)."""
        self.scans.append((self.now, start))
        if self.scan_mode == "raise":
            raise OSError("the disk that holds the session files went away")
        self.ingest(Path(data_root))
        return SimpleNamespace(missing=2 if self.scan_mode == "unreadable" else 0)

    def readings(self) -> dict[str, dict]:
        return api_spend.readings(self.h.switcher, dict(self.infos), fetch=set(),
                                  now=self.now, env={})

    def sub_entry(self, num: str) -> UsageEntry:
        return UsageEntry(last_good=self.subs[num].usage(self.now), fetched_at=self.now,
                          age_s=0.0)

    def entries(self, fetch=None, scheduled=False) -> dict[str, UsageEntry]:
        out = {n: self.sub_entry(n) for n in self.subs}
        readings = self.readings()
        for n in KEYS:
            out[n] = UsageEntry(sentinel="api key", api_spend=readings.get(n))
        return out

    def refresh(self, nums) -> dict[str, UsageEntry]:
        return {n: self.sub_entry(n) for n in nums if n in self.subs}

    def claim(self, num: str, grant_id: str, request_id: str) -> ClaimResult:
        """The fake claim: the grant's window refills at once."""
        self.claims.append((self.now, num, grant_id))
        sub = self.subs[num]
        grant = next(g for g in sub.grants if g["id"] == grant_id)
        assert grant["resets_left"] > 0, f"t{self.t}: claimed a used grant {grant_id}"
        grant["resets_left"] -= 1
        if "seven_day" in grant["clears"]:
            sub.d7 = 0.0
        if "five_hour" in grant["clears"]:
            sub.h5 = 0.0
        return ClaimResult("reset", resets_left=grant["resets_left"],
                           cleared=tuple(grant["clears"]))

    # -- the rules, from the world's side -------------------------------------------------

    def passes_return_rule(self, num: str) -> bool:
        """A8 and EM9: readable, under its own lines, and at least
        RETURN_ROOM_PCT points of room in every window."""
        sub = self.subs[num]
        return sub.room >= RETURN_ROOM_PCT and max(sub.h5, sub.d7) < LINE

    def takers(self) -> list[str]:
        return [n for n in self.subs if self.passes_return_rule(n)]

    def all_out(self) -> bool:
        return all(sub.room <= 0 for sub in self.subs.values())


def _owner(temp_home, monkeypatch, subs: dict[str, Sub], *, now: float = T0,
           live: str = "1", credit_expires: str | None = None, **world_kw):
    """The owner's fleet and settings on the suite's fake home."""
    h = EngineHarness(temp_home)
    h.clock.now = now
    for n, (email, _alias) in {**SUBS, **KEYS}.items():
        h.seed(int(n), email)
    h.make_live(SUBS[live][0], int(live))
    data = h.switcher._get_sequence_data()
    for n, (_email, alias) in {**SUBS, **KEYS}.items():
        if alias:
            data["accounts"][n]["alias"] = alias
        if n in KEYS:
            data["accounts"][n]["kind"] = "api_key"
    if live != "1":
        data["activeAccountNumber"] = int(live)
    h.switcher._write_json(h.switcher.sequence_file, data)
    for n, (email, _alias) in KEYS.items():
        h.switcher._write_account_credentials(n, email, KEY)
    api_spend.set_budget(h.switcher, "5", "$1,200")
    api_spend.set_credit(h.switcher, "5", "$1,187.03", now=CREDIT_AT, expires=credit_expires)
    api_spend.set_budget(h.switcher, "6", "200")
    api_spend.set_budget(h.switcher, "7", "200")
    root = h.switcher.backup_dir
    for key, value in (
        ("autoswitch.threshold", "98"),
        ("autoswitch.includeApiKeyAccounts", "true"),
        ("resets.auto", "active"),
        ("resets.threshold", "100"),
    ):
        set_setting(root, key, value)
    h.settings = load_engine_settings(root)
    assert (h.settings.threshold, h.settings.include_api_key_accounts) == (98.0, True)
    assert h.settings.cooldown_seconds == COOLDOWN
    h.engine = h._make_engine()
    world = World(h, subs, **world_kw)
    monkeypatch.setattr(ledger_usage, "scan_recent", world.scan)
    return h, world


# == Driving it ==============================================================================


def _num(ref) -> str | None:
    number = (ref or {}).get("number")
    return None if number is None else str(number)


@dataclass
class Tick:
    t: int
    now: float
    before: str | None          # the active account before the tick
    before_by: str | None       # who entered it, when it was a key through the line
    outcomes: list
    events: list
    active: str | None
    scans: int

    @property
    def switches(self) -> list[SwitchEvent]:
        return [e for e in self.events if isinstance(e, SwitchEvent)]

    @property
    def holds(self) -> list[NoSwitchEvent]:
        return [e for e in self.events if isinstance(e, NoSwitchEvent)]

    def held(self, reason: str) -> bool:
        return any(e.reason == reason for e in self.holds)


@dataclass
class Move:
    t: int
    frm: str | None
    to: str | None
    trigger: str
    reason: str | None
    text: str


class Run:
    """Ticks the engines one minute at a time against the world."""

    def __init__(self, h: EngineHarness, world: World, *, engines=None):
        self.h, self.world = h, world
        self._engines = engines
        self.ticks: list[Tick] = []
        self.moves: list[Move] = []
        self.entered_at: dict[str, float] = {}   # key -> when the engine last entered it
        self._patches = [
            patch.object(h.switcher, "usage_entries_by_account", side_effect=world.entries),
            patch.object(h.switcher, "refresh_usage", side_effect=world.refresh),
            patch.object(h.switcher, "claim_usage_reset", side_effect=world.claim),
        ]

    @property
    def engines(self) -> list[AutoSwitchEngine]:
        return self._engines if self._engines is not None else [self.h.engine]

    def __enter__(self) -> Run:
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc) -> None:
        for p in reversed(self._patches):
            p.stop()

    def step(self) -> Tick:
        h, world = self.h, self.world
        t = len(self.ticks)
        if t:
            h.clock.advance(MINUTE)
        before = h.switcher.current_account_number()
        before_by = h.switcher.key_entered_by() if h.switcher.helper_account() else None
        world.work(t)
        first_event, first_scan = len(h.events), len(world.scans)
        outcomes = [engine.tick() for engine in self.engines]
        tick = Tick(
            t=t, now=world.now, before=before, before_by=before_by, outcomes=outcomes,
            events=h.events[first_event:], active=h.switcher.current_account_number(),
            scans=len(world.scans) - first_scan,
        )
        self.ticks.append(tick)
        for s in tick.switches:
            move = Move(t, _num(s.from_ref), _num(s.to_ref), s.trigger, s.limit_reason,
                        s.human())
            self.moves.append(move)
            if move.to in KEYS:
                self.entered_at[move.to] = world.now
        _always(self, tick)
        return tick

    def trace(self, last: int = 12) -> str:
        """The moves so far, for an assertion's message."""
        return "\n".join(f"  t{m.t} {m.frm}->{m.to} [{m.trigger}] {m.text}"
                         for m in self.moves[-last:])

    def entries_now(self) -> list[Move]:
        return [m for m in self.moves if m.to in KEYS]


def _settings_line(temp_home) -> str | None:
    path = temp_home / ".claude" / "settings.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("apiKeyHelper")


def _always(run: Run, tick: Tick) -> None:
    """What holds after every tick of every timeline."""
    h, world = run.h, run.world
    where = f"t{tick.t} ({_clock(tick.now)})"
    errors = [e.human() for e in tick.events if getattr(e, "kind", "") == "error"]
    assert not errors, f"{where}: the engine reported an error: {errors}"
    for s in tick.switches:
        frm, to = _num(s.from_ref), _num(s.to_ref)
        # A10: never from one key to another, in any branch.
        assert not (frm in KEYS and to in KEYS), f"{where}: key to key: {s.human()}"
        if to in KEYS:
            # A1 and EM9: a key only at the limit, and never while a
            # subscription passes the return rule.
            assert s.trigger == "at-limit", f"{where}: entered a key on {s.trigger}"
            assert not world.takers(), (
                f"{where}: entered {to} while {world.takers()} could take the sessions"
            )
            # A3: the spend was measured before the move.
            assert tick.scans >= 1, f"{where}: entered {to} without a ledger scan"
    # The key line is in exactly while a key is the active account.
    line = _settings_line(h.temp_home)
    if tick.active in KEYS:
        assert h.switcher.helper_account() == tick.active, f"{where}: line {line!r}"
        assert line is not None and f"key-helper {tick.active}" in line, f"{where}: {line!r}"
    else:
        assert line is None and h.switcher.helper_account() is None, (
            f"{where}: on {tick.active} with the key line {line!r} still in"
        )
    # Leaving a key the engine entered for a subscription with room, once
    # the cooldown has run (A8): never sooner, and never later than the
    # first tick it may.
    if tick.before in KEYS and tick.before_by == "engine":
        since = tick.now - run.entered_at.get(tick.before, -1e18)
        returned = [s for s in tick.switches if s.trigger == "key-return"]
        if returned:
            assert since >= COOLDOWN, f"{where}: back {since:.0f}s after entering"
            assert world.passes_return_rule(_num(returned[0].to_ref)), (
                f"{where}: back to {_num(returned[0].to_ref)} without its room"
            )


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M")


def _must_return(run: Run, tick: Tick) -> None:
    """On a key the engine entered, with the cooldown run and a subscription
    passing the return rule, the engine goes back this tick (A8)."""
    if tick.before in KEYS and tick.before_by == "engine":
        since = tick.now - run.entered_at.get(tick.before, -1e18)
        if since >= COOLDOWN and run.world.takers() and not tick.switches:
            raise AssertionError(
                f"t{tick.t}: stayed on {tick.before} though {run.world.takers()} pass "
                f"the return rule {since:.0f}s after the entry\n{run.trace()}"
            )


def _sub(h5: float, d7: float, h5_in: float, d7_in: float, *grants: str,
         now: float = T0) -> Sub:
    return Sub(h5=h5, d7=d7, h5_reset=now + h5_in, d7_reset=now + d7_in,
               grants=[_weekly_grant(g, now) for g in grants])


# == T1: the normal day =======================================================================


def _t1_world(temp_home, monkeypatch):
    subs = {
        # #1's 5h window opened an hour ago and is nearly unused; its 7d
        # window is nearly full with three days left in it.
        "1": _sub(0.0, 97.0, 4 * HOUR, 3 * DAY, "g-maya"),
        "2": _sub(0.0, 30.0, 5 * HOUR, 5 * DAY),
        "3": _sub(0.0, 40.0, 5 * HOUR, 6 * DAY),
        "4": _sub(0.0, 50.0, 5 * HOUR, 2 * DAY),
    }
    return _owner(temp_home, monkeypatch, subs, r5=1.6, r7=0.05)


def _t1_rest_of_the_day(run: Run, start: int, ticks: int,
                        moves=(("1", "2"), ("2", "3"), ("3", "4"), ("4", "1"))) -> None:
    """After the reset: #1 fills again and the engine moves through #2, #3
    and #4 as each reaches its line, never to a key."""
    world = run.world
    expected = iter(moves)
    want = next(expected)
    for _ in range(start, ticks):
        before = run.h.switcher.current_account_number()
        was = world.subs[before].h5 if before in world.subs else None
        tick = run.step()
        where = f"t{tick.t}"
        assert tick.active in SUBS, f"{where}: on {tick.active}\n{run.trace()}"
        assert not world.claims[1:], f"{where}: a second reset was spent: {world.claims}"
        if tick.switches:
            s = tick.switches[0]
            assert len(tick.switches) == 1
            assert (_num(s.from_ref), _num(s.to_ref)) == want, (
                f"{where}: {s.human()}, expected {want}\n{run.trace()}"
            )
            assert s.trigger == "proactive" and "5h reached" in s.human(), s.human()
            # At its line, the first tick it is there.
            assert (was or 0.0) < LINE <= world.subs[before].h5, (s.human(), was)
            want = next(expected, None)
        elif before in world.subs:
            assert world.subs[before].h5 < LINE or want is None, (
                f"{where}: {before} at 5h {world.subs[before].h5} and no move"
            )
    assert want is None, f"the day ended before every move; still expected {want}"


class TestT1TheNormalDay:
    def test_it_moves_at_the_line_and_spends_the_reset_with_nowhere_to_move(
        self, temp_home, monkeypatch
    ):
        """#1's 7d window climbs a twentieth of a point a minute from 97,
        with three days left in it. resets.threshold (100) is above the switch
        line (98), so the order is the one resets.md and auto-switch.md state:
        auto-switch moves to a subscription with room at the line, and keeps
        the reset; auto mode spends it when #1 is the active account at 100
        with nowhere to move."""
        h, world = _t1_world(temp_home, monkeypatch)
        seen: dict[int, tuple[str | None, float, list[str]]] = {}

        def before_the_check(w, t, active):
            seen[t] = (active, round(w.subs["1"].d7, 3), w.takers())

        world.hooks.append(before_the_check)
        with Run(h, world) as run:
            for _ in range(19):
                tick = run.step()
                assert tick.active == "1" and not tick.switches, run.trace()
                assert world.subs["1"].d7 < LINE
            tick = run.step()
            assert seen[tick.t] == ("1", LINE, ["2", "3", "4"]), seen[tick.t]
            assert [(m.frm, m.to, m.trigger, m.text) for m in run.moves] == [(
                "1", "2", "proactive", "Switched #1 maya@example.com → #2 work · 7d reached 98%",
            )]
            while not world.claims:
                assert tick.t < 400, f"no reset by t{tick.t}\n{run.trace()}"
                tick = run.step()
            assert world.claims == [(tick.now, "1", "g-maya")]
            assert seen[tick.t] == ("1", 100.0, []), seen[tick.t]
            assert world.subs["1"].d7 == 0.0 and not tick.switches
            assert tick.t > 200 and not run.entries_now(), run.trace()
            _t1_rest_of_the_day(run, tick.t + 1, tick.t + 190, moves=(
                ("1", "2"), ("2", "3"), ("3", "4"),
            ))

    def test_a_heavy_turn_to_100_spends_the_reset_then_the_rotation(
        self, temp_home, monkeypatch
    ):
        """The same morning, but a heavy turn takes #1's 7d window from 97.5
        to 100 inside one minute, so the engine first sees it at its limit."""
        h, world = _t1_world(temp_home, monkeypatch)

        def heavy_turn(w, t, active):
            if t == 10:
                w.subs["1"].d7 = 100.0

        world.hooks.append(heavy_turn)
        with Run(h, world) as run:
            for t in range(11):
                tick = run.step()
                assert tick.active == "1" and not tick.switches, run.trace()
                assert bool(world.claims) == (t >= 10), (t, world.claims)
            assert world.claims == [(T0 + 10 * MINUTE, "1", "g-maya")]
            assert world.subs["1"].d7 == 0.0 and world.subs["1"].resets_left() == 0
            assert tick.outcomes == [TickOutcome.NO_ACTION]
            used = [e for e in tick.events if isinstance(e, ResetEvent)]
            assert used and used[0].account["number"] == 1
            _t1_rest_of_the_day(run, 11, 300)
        assert not run.entries_now()
        assert len(run.ticks) == 300


# == T1 with resets.beforeSwitch ==============================================================


def _t1_day(home: Path, monkeypatch, *, before_switch: bool, ticks: int = 300) -> dict:
    """T1's day for ``ticks`` checks in a home of its own: the claims (by
    tick), the moves, the checks held for the active account's own reset, and
    what the planner with resets.beforeSwitch off would spend at each check's
    reading (taken after the work and before the engine reads it)."""
    home.mkdir()
    (home / ".claude").mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    with patch("pathlib.Path.home", return_value=home), patch.dict(os.environ, env):
        h, world = _t1_world(home, monkeypatch)
        if before_switch:
            set_setting(h.switcher.backup_dir, "resets.beforeSwitch", "true")
        off = replace(h.switcher.reset_settings(), before_switch=False)
        planned: dict[int, tuple[str, str] | None] = {}

        def plan_with_it_off(w, t, active):
            subs = {n: w.sub_entry(n) for n in w.subs}
            plan = preview_reset_plan(h.switcher, h.settings, subs, now=w.now,
                                      reset_settings=off)
            planned[t] = None if plan is None else (plan.account, plan.grant_id)

        world.hooks.append(plan_with_it_off)
        with Run(h, world) as run:
            for _ in range(ticks):
                run.step()
    return {
        "claims": [(round((now - T0) / MINUTE), num, gid) for now, num, gid in world.claims],
        "moves": [(m.t, m.frm, m.to) for m in run.moves],
        "held": [t.t for t in run.ticks if t.held("reset-ahead")],
        "planned": planned,
        "keys": run.entries_now(),
    }


class TestT1WithResetsBeforeSwitch:
    def test_it_stays_past_the_line_spends_the_reset_at_100_then_the_rotation(
        self, temp_home, monkeypatch
    ):
        """T1's morning with resets.beforeSwitch on: #1 stays past its line
        (98) while its 7d window climbs, auto mode spends its weekly reset
        when 7d reaches resets.threshold (100), and the rotation then runs as
        on any day: #1, #2, #3 and #4 each to its 5h line."""
        h, world = _t1_world(temp_home, monkeypatch)
        set_setting(h.switcher.backup_dir, "resets.beforeSwitch", "true")
        seen: dict[int, tuple[str | None, float, list[str]]] = {}

        def before_the_check(w, t, active):
            seen[t] = (active, round(w.subs["1"].d7, 3), w.takers())

        world.hooks.append(before_the_check)
        with Run(h, world) as run:
            for _ in range(19):
                tick = run.step()
                assert tick.active == "1" and not tick.switches, run.trace()
                assert world.subs["1"].d7 < LINE
            held = []
            while not world.claims:
                tick = run.step()
                assert tick.t < 80, f"no reset by t{tick.t}\n{run.trace()}"
                assert tick.active == "1" and not tick.switches, run.trace()
                if not world.claims:
                    assert [e.reason for e in tick.holds] == ["reset-ahead"], tick.holds
                    held.append((tick.t, seen[tick.t][1]))
            # Held from the first check at the line to the last under 100.
            assert held[0] == (19, LINE) and held[-1] == (58, 99.95) and len(held) == 40
            first = next(e for e in run.ticks[19].holds if e.reason == "reset-ahead")
            assert first.human() == (
                "No switch yet: auto mode spends #1 maya@example.com's own weekly reset "
                "when 7d reaches 100%, rather than switching away (resets.beforeSwitch)"
            )
            # Spent at resets.threshold, while the others still had room.
            assert world.claims == [(tick.now, "1", "g-maya")] and tick.t == 59
            assert seen[tick.t] == ("1", 100.0, ["2", "3", "4"]), seen[tick.t]
            assert world.subs["1"].d7 == 0.0 and not tick.switches
            _t1_rest_of_the_day(run, tick.t + 1, 300)
        assert not run.entries_now()
        assert len(run.ticks) == 300

    def test_off_and_on_over_the_same_300_checks(self, tmp_path, monkeypatch):
        """The same day twice. With the setting on the engine spends no reset
        the planner would keep with it off at that check's reading, and
        makes no more switches."""
        off = _t1_day(tmp_path / "off", monkeypatch, before_switch=False)
        on = _t1_day(tmp_path / "on", monkeypatch, before_switch=True)

        assert not off["held"] and on["held"] == list(range(19, 59))
        # The same reset, spent at resets.threshold either way: with it off
        # when the rotation comes back to #1 with nowhere to move, with it
        # on before #1 ever leaves.
        assert off["claims"] == [(248, "1", "g-maya")]
        assert on["claims"] == [(59, "1", "g-maya")]
        # Each one where the planner with the setting off spends it, at the
        # reading of that check: the setting moves no reset earlier.
        for day in (off, on):
            for t, num, gid in day["claims"]:
                assert day["planned"][t] == (num, gid), (t, num, gid, day["planned"][t])
        # Fewer switches, and no API key either way.
        assert on["moves"] == [(61, "1", "2"), (123, "2", "3"), (185, "3", "4"), (247, "4", "1")]
        assert len(off["moves"]) == 8 and off["moves"][0] == (19, "1", "2")
        assert len(on["moves"]) <= len(off["moves"])
        assert not on["keys"] and not off["keys"]


# == T2: all four out =========================================================================


def _t2_subs(d7_first: float = 62.0) -> dict[str, Sub]:
    """Every subscription at its 5h limit, their windows refilling over the
    next four hours; the weekly windows have room."""
    return {
        "1": _sub(100.0, d7_first, 2 * HOUR, 4 * DAY, "g-maya"),
        "2": _sub(100.0, 48.0, 2.5 * HOUR, 5 * DAY),
        "3": _sub(100.0, 55.0, 3.5 * HOUR, 6 * DAY),
        "4": _sub(100.0, 40.0, 4 * HOUR, 2 * DAY),
    }


def _key_scans(run: Run) -> list[int]:
    return [t.t for t in run.ticks if t.scans]


class TestT2AllFourOut:
    def test_one_key_rescanned_and_back_when_a_window_refills(self, temp_home, monkeypatch):
        h, world = _owner(temp_home, monkeypatch, _t2_subs())
        on_key_since = None
        with Run(h, world) as run:
            for _ in range(440):
                tick = run.step()
                where = f"t{tick.t} ({_clock(tick.now)})"
                _must_return(run, tick)
                entered = [s for s in tick.switches if _num(s.to_ref) in KEYS]
                if entered:
                    s = entered[0]
                    assert len(tick.switches) == 1, f"{where}: {run.trace()}"
                    assert world.all_out(), f"{where}: {s.human()}"
                    # The first key in rotation order with a limit and a
                    # fresh figure under it (no order among keys is
                    # documented; see the write-up).
                    assert _num(s.to_ref) == "5", s.human()
                    assert s.human() == (
                        f"Switched {_label(_num(s.from_ref))} → #5 credit · 5h limit reached"
                    ), s.human()
                    # #1's weekly reset cannot help a 5h limit: nothing kept, nothing said.
                    assert s.reset_notes == () and s.resets_kept == ()
                    assert "resetsKept" not in s.to_json()
                    on_key_since = tick.t
                if tick.active in KEYS:
                    assert tick.active == "5", f"{where}: on {tick.active}"
                    # A3: a scan every 5 minutes while the key is active.
                    assert tick.scans == (1 if (tick.t - on_key_since) % 5 == 0 else 0), (
                        f"{where}: {tick.scans} scans, {tick.t - on_key_since} min on the key"
                    )
                    # The figure the engine judges is the ledger's, as of the last scan.
                    reading = world.readings()["5"]
                    assert reading["credit"]["used"] == pytest.approx(
                        world.dollars("5", since=CREDIT_AT), abs=1e-6
                    )
                    assert world.now - reading["as_of"] < SPEND_SCAN_INTERVAL_S
                    if not entered:
                        assert tick.held("no-subscription-room") or tick.switches, where
                elif not entered and not any(_num(s.from_ref) in KEYS for s in tick.switches):
                    assert tick.scans == 0, f"{where}: a scan with no key active or wanted"
        assert not world.claims, world.claims
        entries = run.entries_now()
        assert [m.to for m in entries] == ["5", "5"], run.trace(20)
        assert [m.t for m in entries] == [0, 372], run.trace(20)
        returns = [m for m in run.moves if m.frm in KEYS]
        assert [(m.t, m.to, m.trigger) for m in returns] == [
            (120, "1", "key-return"), (421, "1", "key-return"),
        ], run.trace(20)
        assert returns[0].text == (
            "Switched #5 credit → #1 maya@example.com · a subscription has room again"
        )
        _no_flap(run)
        # Said in the menu bar too, once per move onto the key.
        entered_notes = [n for n in _notes(h) if "→ #5 credit" in n]
        assert entered_notes == [
            "Switched #1 maya@example.com → #5 credit · 5h limit reached",
            "Switched #4 side → #5 credit · 5h limit reached",
        ], entered_notes
        # The spend rose tick by tick and was measured every 5 minutes.
        assert world.dollars("5", scanned=False) == pytest.approx(0.40 * (120 + 49), abs=0.01)
        assert len(run.ticks) == 440


def _label(num: str | None) -> str:
    email, alias = {**SUBS, **KEYS}[num]
    return f"#{num} {alias}" if alias else f"#{num} {email}"


def _notes(h) -> list[str]:
    """The menu bar's notifications for every event so far."""
    from ai_quota_swap.menubar_notify import Notifier

    notifier = Notifier()
    return [
        n.body for n in (notifier.event("claude", e, both=False, now=0.0) for e in h.events)
        if n is not None
    ]


def _log_lines(h) -> list[str]:
    """The auto log (qswap auto's text output) for every event so far."""
    from ai_quota_swap import autolog

    lines: list[str] = []
    log = autolog.AutoLog(lines.append, clock=lambda: "12:00:00", width=80)
    for event in h.events:
        log(event)
    log.flush()
    return [line for text in lines for line in text.splitlines()]


def _no_flap(run: Run) -> None:
    """No key entered and left again inside one cooldown; no key to key."""
    for i, move in enumerate(run.moves):
        if move.to in KEYS:
            later = [m for m in run.moves[i + 1:] if m.frm == move.to]
            if later and later[0].trigger == "key-return":
                gap = (later[0].t - move.t) * MINUTE
                assert gap >= COOLDOWN, f"t{move.t}: in and out of {move.to} in {gap:.0f}s"
        assert not (move.frm in KEYS and move.to in KEYS), move.text


# == T3: the brakes ===========================================================================


def _out_for_hours() -> dict[str, Sub]:
    """No subscription has room for the next three hours."""
    return {
        "1": _sub(100.0, 70.0, 3 * HOUR, 4 * DAY, "g-maya"),
        "2": _sub(100.0, 48.0, 3.5 * HOUR, 5 * DAY),
        "3": _sub(100.0, 55.0, 4 * HOUR, 6 * DAY),
        "4": _sub(100.0, 40.0, 4.5 * HOUR, 2 * DAY),
    }


#: Each brake: the key it is on, what the world does, and the reason the
#: engine must give. ``at`` is the tick the condition starts in the world.
BRAKES = {
    "budget": "6",
    "stale": "6",
    "scan-raises": "6",
    "unpriced-model": "6",
    "credit-spent": "5",
    "credit-expired": "5",
}


def _brake_world(temp_home, monkeypatch, brake: str):
    now = T0
    expires = None
    if brake == "credit-expired":
        # The credit's last usable day is today; the timeline starts late tonight.
        now = datetime(2026, 11, 11, 23, 40, tzinfo=UTC).timestamp()
        expires = "2026-11-11"
    subs = {n: replace(s, h5_reset=s.h5_reset - T0 + now, d7_reset=s.d7_reset - T0 + now)
            for n, s in _out_for_hours().items()}
    h, world = _owner(temp_home, monkeypatch, subs, now=now, credit_expires=expires)
    month_early = datetime(2026, 11, 4, 15, tzinfo=UTC).timestamp()
    if BRAKES[brake] == "6":
        # #5's credit was spent earlier today (by hand), so the engine's
        # first key this morning is #6.
        world.spend("5", 1190.0, at=CREDIT_AT + 20 * MINUTE)
        world.spend("6", 150.0 if brake in ("budget", "unpriced-model") else 20.0,
                    at=month_early)
    else:
        world.spend("5", 1150.0 if brake == "credit-spent" else 40.0,
                    at=CREDIT_AT + 20 * MINUTE)
    world.spend("7", 10.0, at=month_early)
    world.ingest(h.switcher.backup_dir)  # what earlier scans already put in the ledger
    return h, world


def _brake_script(world: World, brake: str) -> None:
    """What the world does in each brake's timeline."""
    if brake == "budget":
        world.key_rate = 1.20
    elif brake == "credit-spent":
        world.key_rate = 3.00
    elif brake in ("stale", "scan-raises"):
        def scans_fail(w, t, active):
            # From t20 no scan succeeds; from t100 they work again.
            if t == 20:
                w.scan_mode = "unreadable" if brake == "stale" else "raise"
            elif t == 100:
                w.scan_mode = "ok"
        world.hooks.append(scans_fail)
    elif brake == "unpriced-model":
        def no_price(w, t, active):
            if t >= 12 and active == "6":
                # 600k input tokens a minute of a model the price table does
                # not know: $9.60 at the dearest input rate, $0 without A6.
                w.spend("6", 9.60, UNPRICED)
        world.hooks.append(no_price)


class TestT3TheBrakes:
    @pytest.mark.parametrize("brake", list(BRAKES))
    def test_off_the_key_underneath_and_never_another_key(self, temp_home, monkeypatch, brake):
        """From "on the key, entered by the engine, no subscription has
        room": the engine leaves for the account underneath within one tick
        of the condition (as far as the engine can know it: the ledger's
        figure after a scan, a failed scan, the credit's date), says why,
        does not come back while the condition or its latch holds, and never
        moves to another key (A5, A9, A10, EM3; docs/api-accounts.md "At the
        limit ... Never for another key")."""
        h, world = _brake_world(temp_home, monkeypatch, brake)
        key = BRAKES[brake]
        _brake_script(world, brake)
        condition_at: int | None = None
        with Run(h, world) as run:
            tick = run.step()
            assert tick.active == key and h.switcher.key_entered_by() == "engine", run.trace()
            assert [m.to for m in run.moves] == [key]
            left_at = None
            for _ in range(1, 150):
                tick = run.step()
                where = f"t{tick.t} ({_clock(tick.now)})"
                holds = _brake_holds(world, brake, key, tick)
                if left_at is None:
                    if holds and condition_at is None:
                        condition_at = tick.t
                    if tick.active == key:
                        assert condition_at is None or tick.t - condition_at < 1, (
                            f"{where}: still on {key} a tick after the condition "
                            f"(t{condition_at})\n{run.trace()}"
                        )
                        continue
                    left_at = tick.t
                    s = tick.switches[0]
                    assert condition_at is not None, (
                        f"{where}: left {key} before the brake: {s.human()}"
                    )
                    assert tick.t - condition_at <= 1, f"{where}: condition since t{condition_at}"
                    # For the account underneath, and says why.
                    assert _num(s.to_ref) == "1" and s.trigger == "at-limit", s.human()
                    assert s.human() == (
                        f"Switched {_label(key)} → #1 maya@example.com · "
                        f"{BRAKE_WORDS[brake]}; no subscription has room, so back to "
                        "the account underneath"
                    ), s.human()
                    if brake == "unpriced-model":
                        # A6: the tokens with no price counted, at the dearest rate.
                        reading = world.readings()["6"]
                        assert reading["unpriced_tokens"] > 0
                        assert reading["spend"]["used"] == pytest.approx(world.dollars("6"))
                        assert "+ this month" in api_spend.spend_text(reading, now=world.now)
                        assert world.dollars("6") - sum(
                            tok / 1e6 * 16.0 for _ts, n, m, tok in world.lines
                            if n == "6" and m == UNPRICED
                        ) < 200.0, "without A6 the budget would not be reached yet"
                    continue
                entered = [m.to for m in run.moves if m.t == tick.t and m.to in KEYS]
                # Not back while the condition, or the latch it left (A5), holds.
                if holds or brake in LATCHED:
                    assert key not in entered, f"{where}: back on {key}\n{run.trace()}"
                # Never another key.
                others = [n for n in entered if n != key]
                assert not others, (
                    f"{where}: {tick.switches[0].human()}, {tick.t - left_at} min after "
                    f"leaving {key} at its limit; the subscriptions are still out and "
                    f"the engine moved to another key\n{run.trace()}"
                )
            assert left_at is not None, f"never left {key}\n{run.trace()}"
            if brake in ("stale", "scan-raises"):
                # No latch for spend-unknown (A5 latches budgets and credits):
                # once a scan succeeds again the same key is fine again.
                back = [m for m in run.moves if m.t >= 100 and m.to == key]
                assert back and back[0].t <= 101, run.trace()
        assert len(run.ticks) == 150

    @pytest.mark.parametrize("brake", ["stale", "scan-raises"])
    def test_the_rows_agree_with_the_engine_after_a_failed_scan(
        self, temp_home, monkeypatch, brake
    ):
        """A4: a key the engine will not use for want of a spend figure says
        "limit not enforced: no spend figure" on its row, "so the surfaces
        agree with the engine". Here the engine refuses every key on each
        tick a scan fails (EM3); the auto log's rows under that hold are
        what the user reads."""
        h, world = _brake_world(temp_home, monkeypatch, brake)
        _brake_script(world, brake)
        checked = 0
        with Run(h, world) as run:
            for _ in range(40):
                tick = run.step()
                # Every key is refused while the scans fail: for #6 its spend
                # is unknown, and once #6 is left the others wait for a
                # subscription with room (key-left).
                held = [e for e in tick.holds if e.reason in ("spend-unknown", "key-left")]
                if world.scan_mode == "ok" or not held or held[0].context is None:
                    continue
                rows = {v.number: v for v in held[0].context.accounts}
                for n in KEYS:
                    if n in rows and n != tick.active:
                        checked += 1
                        assert rows[n].key_hold is not None, (
                            f"t{tick.t}: the engine refuses #{n} ({held[0].reason}: "
                            f"{held[0].human()}) while its row says it is used as a "
                            "last resort"
                        )
        assert checked, "no tick refused the keys while the scans failed"


#: The two scan brakes name why the spend is not known, not an age: their
#: last good scan was minutes before (the write-up's finding 5).
BRAKE_WORDS = {
    "budget": "monthly budget reached (estimate)",
    "stale": "its spend is not known (2 session files on this machine could not be read)",
    "scan-raises": "its spend is not known (the scan of this machine's session files failed)",
    "unpriced-model": "monthly budget reached (estimate)",
    "credit-spent": "credit spent (estimate)",
    "credit-expired": "credit expired",
}
#: Brakes whose limit stays reached (A5): the key must not come back at all.
LATCHED = {"budget", "unpriced-model", "credit-spent", "credit-expired"}


def _brake_holds(world: World, brake: str, key: str, tick: Tick) -> bool:
    """Whether the brake's condition holds as the engine can know it this
    tick: the ledger's figure (after this tick's scan) at the limit, the
    scans failing, the credit's last day past."""
    if brake in ("budget", "unpriced-model"):
        return world.dollars(key) >= 200.0
    if brake == "credit-spent":
        return world.dollars(key, since=CREDIT_AT) >= 1187.03
    if brake == "credit-expired":
        return tick.now >= datetime(2026, 11, 12, tzinfo=UTC).timestamp()
    if brake == "scan-raises":
        return world.scan_mode == "raise"
    # stale: no scan has succeeded for 20 minutes (the scans since t20 found
    # session files they could not read); the engine counts the figure as
    # unknown from the first such scan (EM3).
    return world.scan_mode == "unreadable"


# == T4: the user's own key ====================================================================


class TestT4TheUsersKey:
    @pytest.mark.parametrize("brake", ["credit-spent", "budget", "spend-unknown"])
    def test_kept_while_a_subscription_has_room_left_at_its_limit(
        self, temp_home, monkeypatch, brake
    ):
        subs = {
            "1": _sub(100.0, 70.0, 3 * HOUR, 4 * DAY, "g-maya"),
            "2": _sub(10.0, 30.0, 4 * HOUR, 5 * DAY),       # room all along
            "3": _sub(100.0, 55.0, 4 * HOUR, 6 * DAY),
            "4": _sub(100.0, 40.0, 4.5 * HOUR, 2 * DAY),
        }
        h, world = _owner(temp_home, monkeypatch, subs)
        if brake == "budget":
            # $50 this month before the credit was recorded: the budget
            # ($1,200 this month) is reached with about $30 of credit left.
            world.spend("5", 50.0, at=datetime(2026, 11, 3, 10, tzinfo=UTC).timestamp())
        world.spend("5", 1100.0 if brake == "budget" else 1150.0, at=CREDIT_AT + 20 * MINUTE)
        world.ingest(h.switcher.backup_dir)

        def turns(w, t, active):
            if t == 60:
                if brake == "spend-unknown":
                    w.scan_mode = "raise"
                else:
                    w.key_rate = 3.00

        world.hooks.append(turns)
        h.switcher.switch_to("5", json_output=True)   # by hand
        assert h.switcher.key_entered_by() == "user"
        with Run(h, world) as run:
            left_at = None
            for _ in range(120):
                tick = run.step()
                where = f"t{tick.t} ({_clock(tick.now)})"
                limit = {
                    "credit-spent": world.dollars("5", since=CREDIT_AT) >= 1187.03,
                    "budget": world.dollars("5") >= 1200.0,
                    "spend-unknown": world.scan_mode == "raise" and tick.t >= 60
                    and any(t.scans for t in run.ticks[60:]),
                }[brake]
                if left_at is None and tick.active != "5":
                    left_at = tick.t
                    s = tick.switches[0]
                    assert limit, f"{where}: left the user's key with no limit: {s.human()}"
                    assert _num(s.to_ref) == "2", s.human()   # the subscription with room
                    assert s.human() == (
                        f"Switched #5 credit → #2 work · {USER_WORDS[brake]}"
                    ), s.human()
                elif left_at is None:
                    assert not limit, f"{where}: still on #5 at its limit\n{run.trace()}"
                    # A subscription with room is no reason to leave the user's key.
                    assert world.passes_return_rule("2")
                    assert tick.held("active-api-key"), [e.reason for e in tick.holds]
                    # A3: the engine keeps the figure fresh on the user's key too.
                    assert tick.scans == (1 if tick.t % 5 == 0 else 0), (tick.t, tick.scans)
                else:
                    assert "5" not in [m.to for m in run.moves if m.t > left_at], run.trace()
            assert left_at is not None, f"never left #5\n{run.trace()}"
        assert not world.claims


USER_WORDS = {
    "credit-spent": "credit spent (estimate)",
    "budget": "monthly budget reached (estimate)",
    "spend-unknown": "its spend is not known (the scan of this machine's session files failed)",
}


# == T5: settings changed mid-flight ===========================================================


class TestT5SettingsMidFlight:
    @pytest.mark.parametrize("how", ["running-engine", "restarted-engine"])
    def test_api_keys_turned_off_on_an_engine_key(self, temp_home, monkeypatch, how):
        """`qswap config set autoswitch.includeApiKeyAccounts false` half an
        hour into an engine-entered key. EM11 and docs/auto-switch.md: a key
        the engine moved to "is also left at once when you turn
        autoswitch.includeApiKeyAccounts off". ``running-engine``: the
        engine that entered the key keeps running (``qswap auto``, the menu
        bar); ``restarted-engine``: a fresh engine reads the settings (a
        cron ``--once``, a restart)."""
        h, world = _owner(temp_home, monkeypatch, _out_for_hours())
        with Run(h, world) as run:
            for _ in range(30):
                run.step()
            assert run.ticks[-1].active == "5" and h.switcher.key_entered_by() == "engine"
            set_setting(h.switcher.backup_dir, "autoswitch.includeApiKeyAccounts", "false")
            if how == "restarted-engine":
                h.settings = load_engine_settings(h.switcher.backup_dir)
                h.engine = h._make_engine()
            tick = run.step()
            assert tick.active == "1", (
                f"t{tick.t}: still on {tick.active} after API keys were turned off "
                f"({[e.reason for e in tick.holds]})"
            )
            s = tick.switches[0]
            assert s.limit_reason == "keys-off"
            assert s.human() == (
                "Switched #5 credit → #1 maya@example.com · API keys are turned off "
                "(autoswitch.includeApiKeyAccounts); no subscription has room, so back to "
                "the account underneath"
            )
            for _ in range(60):
                tick = run.step()
                assert tick.active in SUBS, f"t{tick.t}: on {tick.active}\n{run.trace()}"
            assert len([m for m in run.moves if m.reason == "keys-off"]) == 1

    @pytest.mark.parametrize("key", ["5", "6"])
    def test_the_budget_of_the_active_key_removed(self, temp_home, monkeypatch, key):
        """`qswap budget N off` on the key the engine entered. #6 has only
        the budget: it is left at once (A2/A4: the engine would not have
        entered it). #5 keeps its credit: it stays until the credit is
        removed too. Then, with the subscriptions still out, no other key."""
        h, world = _owner(temp_home, monkeypatch, _out_for_hours())
        if key == "6":
            world.spend("5", 1190.0, at=CREDIT_AT + 20 * MINUTE)   # #5 spent earlier
            world.ingest(h.switcher.backup_dir)
        with Run(h, world) as run:
            for _ in range(20):
                run.step()
            assert run.ticks[-1].active == key
            api_spend.set_budget(h.switcher, key, None)
            tick = run.step()
            if key == "5":
                assert tick.active == "5", "the credit still holds the key to a limit"
                for _ in range(10):
                    assert run.step().active == "5"
                api_spend.set_credit(h.switcher, "5", None)
                tick = run.step()
            assert tick.active == "1", f"t{tick.t}: {tick.active}\n{run.trace()}"
            s = tick.switches[0]
            assert s.human() == (
                f"Switched {_label(key)} → #1 maya@example.com · it has no budget or credit; "
                "no subscription has room, so back to the account underneath"
            ), s.human()
            left_at = tick.t
            for _ in range(60):
                tick = run.step()
                others = [m for m in run.moves if m.t > left_at and m.to in KEYS]
                assert not others, (
                    f"t{tick.t}: after leaving {key} (no limit) the engine moved to "
                    f"another key: {others[0].text}"
                )

    def test_a_second_engine_with_another_line_does_not_flap(self, temp_home, monkeypatch):
        """T2's day with a `qswap auto --threshold 90` running beside the
        owner's engine on the same accounts and state file, their ticks
        interleaved. #1's weekly window is at 92 when its 5h window refills:
        under the owner's line, over the other's."""
        h, world = _owner(temp_home, monkeypatch, _t2_subs(d7_first=92.0))
        other = AutoSwitchEngine(
            h.switcher, AutoSwitchSettings(threshold=90.0, include_api_key_accounts=True),
            h.events.append, clock=h.clock,
        )
        with Run(h, world, engines=[h.engine, other]) as run:
            for _ in range(440):
                tick = run.step()
                # One move a minute at most, from both engines together.
                assert len(tick.switches) <= 1, f"t{tick.t}: {run.trace()}"
        _no_flap(run)
        # No A to B and back to A inside one cooldown, among subscriptions too,
        # unless the return was forced (the account left at its limit).
        for a, b in zip(run.moves, run.moves[1:]):
            if a.frm == b.to and b.trigger != "at-limit":
                assert (b.t - a.t) * MINUTE >= COOLDOWN, f"{a.text} / {b.text}"
        entries = run.entries_now()
        assert entries and all(m.to == "5" for m in entries), run.trace(30)
        assert len(entries) <= 2, run.trace(30)


# == T6: a reset the planner keeps ============================================================


class TestT6AResetThePlannerKeeps:
    @pytest.mark.parametrize("active", ["1", "2"])
    def test_the_key_is_entered_and_the_kept_reset_named(self, temp_home, monkeypatch, active):
        """All four out; #2 holds a weekly reset and its 7d window refills in
        9 hours (under resets.minHoursLeft, 24). The engine enters the key,
        does not spend the reset, and names it with `qswap reset 2` (EM4).
        Its reason is the time left, with #2 active and with #1 active too,
        where auto mode would also spend only the active account's resets
        (decision R4-5: the time left is said first)."""
        long = active == "2"
        subs = {
            # On #2's day the others are out on their weekly windows too, so
            # nothing has room until #2's 7d window refills by itself.
            # #1: out on 5h (its weekly reset cannot help that), or on #2's
            # day out on 7d with room on 5h (its weekly reset would help).
            "1": _sub(50.0 if long else 100.0, 100.0 if long else 60.0, 3 * HOUR, 2 * DAY,
                      "g-maya"),
            "2": _sub(40.0, 100.0, 2 * HOUR, 9 * HOUR, "g-work"),
            "3": _sub(100.0, 100.0 if long else 50.0, 3.5 * HOUR, 1.5 * DAY),
            "4": _sub(100.0, 100.0 if long else 45.0, 4 * HOUR, 20 * HOUR),
        }
        h, world = _owner(temp_home, monkeypatch, subs, live=active)
        with Run(h, world) as run:
            tick = run.step()
            assert tick.active == "5", f"t0: {[e.reason for e in tick.holds]}"
            s = tick.switches[0]
            notes = {
                "1": "#1 maya@example.com holds 1 weekly reset auto mode keeps (auto mode "
                     "spends resets of the active account only); to spend it instead: "
                     "qswap reset 1",
                "2": "#2 work holds 1 weekly reset auto mode keeps (its 7d window refills "
                     "in 9h); to spend it instead: qswap reset 2",
            }
            expected = [notes["1"], notes["2"]] if long else [notes["2"]]
            assert list(s.reset_notes) == expected, s.reset_notes
            window = "7d" if long else "5h"
            assert s.human() == (
                f"Switched {_label(active)} → #5 credit · {window} limit reached; "
                + "; ".join(expected)
            ), s.human()
            kept = {entry["account"]: entry for entry in s.to_json()["resetsKept"]}
            assert kept[2]["fix"] == "qswap reset 2"
            assert kept[2]["reason"] == "min-hours-left"
            assert kept[2]["resets"] == [{"kind": "weekly", "count": 1}]
            # The auto log and the notification say the same (EM4).
            log = _log_lines(h)
            assert all(len(line) <= 80 for line in log), log
            assert "qswap reset 2" in " ".join(" ".join(log).split())
            assert _notes(h)[0] == s.human()
            back_at = None
            for _ in range(1, 600 if long else 220):
                tick = run.step()
                _must_return(run, tick)
                assert not world.claims, f"t{tick.t}: a reset was spent: {world.claims}"
                if tick.active in KEYS:
                    assert tick.active == "5"
                    # Every surface keeps the note while it is true (EM4).
                    if tick.t % 60 == 0:
                        surfaces = kept_resets_now(h.switcher, h.settings, world.entries(),
                                                   now=world.now)
                        assert "2" in [k.account for k in surfaces], tick.t
                elif back_at is None:
                    back_at = tick.t
            assert back_at is not None
            assert world.subs["2"].resets_left() == 1 and world.subs["1"].resets_left() == 1
            back = [m for m in run.moves if m.frm in KEYS][0]
            assert back.trigger == "key-return"
            if long:
                # Nine hours on the key, then #2's window refilled by itself.
                assert (back.t, back.to) == (540, "2"), run.trace()
        assert len(run.ticks) >= (560 if long else 200)


# == T7: an organization's key, on the real scan ==============================================
#
# Issue #17: the timelines above fake the ledger scan and read the keys with
# no network, so an organization's key, held to Anthropic's figure only
# (EM2), never had its figure age on the fake clock. Here the keys are read
# through the switcher's own path (``_api_spend_readings``, with the fake
# clock as ``now``), #6 is linked to an organization whose Admin API is
# faked, and the engine's scan is the real ``scan_recent`` over real session
# files, recorded at the fake clock (its ``now``).

#: Each key's own (made-up) key, so the organization's listing can name one.
OWN_KEYS = {n: "sk-ant-api03-" + tag * 20 for n, tag in (("5", "q5"), ("6", "w6"), ("7", "x7"))}
#: The organization's id for #6's key.
ORG_KEY_ID = "apikey_06"
#: The Admin API is down from this tick, and back from that one.
ADMIN_DOWN, ADMIN_UP = 30, 100
_REAL_SCAN = ledger_usage.scan_recent


class OrgWorld(World):
    """The owner's world with #6 in an organization: every minute of work
    is a line in a real session file, Anthropic's report holds #6's spend
    as it happens, and the Admin API can be down."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.infos = {
            n: (int(n), email, "", "", False, OWN_KEYS[n], alias) for n, (email, alias) in KEYS.items()
        }
        self.admin_up = True
        self.admin_answers: list[float] = []   # when the usage report answered

    def spend(self, num: str, dollars: float, model: str = PRICED, *,
              at: float | None = None) -> None:
        super().spend(num, dollars, model, at=at)
        from ai_quota_swap.session import session_dir_for
        from tests.ledger_fixtures import claude_line

        ts, _n, _m, tokens = self.lines[-1]
        # A qswap run profile's own projects: its lines belong to the key.
        path = (session_dir_for(self.h.switcher.backup_dir, num, KEYS[num][0])
                / "projects" / "-work-demo" / f"sess-{num}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(claude_line(mid=f"m-{num}-{len(self.lines)}", ts=ts, model=model, inp=tokens,
                                out=0, cr=0, w5=0, session=f"sess-{num}") + "\n")
        os.utime(path, (ts, ts))  # changed when its last line was written

    def scan(self, data_root, **kw):
        """The real scan, on the fake clock."""
        self.scans.append((self.now, kw.get("start")))
        stats = _REAL_SCAN(data_root, now=self.now, **kw)
        self.pending = []
        return stats

    def readings(self, fetch=frozenset()) -> dict[str, dict]:
        return self.h.switcher._api_spend_readings(dict(self.infos), fetch, now=self.now)

    def entries(self, fetch=None, scheduled=False) -> dict[str, UsageEntry]:
        out = {n: self.sub_entry(n) for n in self.subs}
        readings = self.readings(fetch)
        for n in KEYS:
            out[n] = UsageEntry(sentinel="api key", api_spend=readings.get(n))
        return out

    def usage_report(self, query):
        from tests.admin_fakes import bucket, error_response, json_response, report, usage_result

        if not self.admin_up:
            return error_response(500, "api_error")
        self.admin_answers.append(self.now)
        days: dict[str, int] = {}
        for ts, n, _model, tokens in self.lines:
            if n == "6" and ts <= self.now:
                day = datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
                days[day] = days.get(day, 0) + tokens
        return json_response(report([
            bucket(day, [usage_result(ORG_KEY_ID, PRICED, inp=tokens)])
            for day, tokens in sorted(days.items())
        ]))


def _org_world(temp_home, monkeypatch):
    from ai_quota_swap import admin_api
    from ai_quota_swap.admin_orgs import OrgRegistry
    from ai_quota_swap.models import Platform
    from tests.admin_fakes import ADMIN_KEY, FakeAnthropic, json_response, key_item, keys_page

    h, _plain = _owner(temp_home, monkeypatch, _out_for_hours())
    world = OrgWorld(h, _plain.subs)
    monkeypatch.setattr(ledger_usage, "scan_recent", world.scan)
    for n, (email, _alias) in KEYS.items():
        h.switcher._write_account_credentials(n, email, OWN_KEYS[n])
    admin_api._memory.clear()
    monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
    OrgRegistry(h.switcher.backup_dir, platform=Platform.LINUX).add(
        "acme", ADMIN_KEY, "org-1", "Acme",
    )
    monkeypatch.setattr(admin_api, "send", FakeAnthropic({
        "/v1/organizations/api_keys": [json_response(keys_page([
            key_item(ORG_KEY_ID, "build", OWN_KEYS["6"]),
        ]))],
        "/v1/organizations/usage_report/messages": world.usage_report,
    }))
    # #5's credit was spent by hand this morning, so the engine's key is #6;
    # #6 and #7 spent a little earlier in the month.
    month_early = datetime(2026, 11, 4, 15, tzinfo=UTC).timestamp()
    world.spend("5", 1190.0, at=CREDIT_AT + 20 * MINUTE)
    world.spend("6", 20.0, at=month_early)
    world.spend("7", 10.0, at=month_early)

    def admin_outage(w, t, active):
        if t == ADMIN_DOWN:
            w.admin_up = False
        elif t == ADMIN_UP:
            w.admin_up = True

    world.hooks.append(admin_outage)
    return h, world


class TestT7AnOrganizationsKey:
    def test_anthropics_figure_ages_on_the_fake_clock_and_the_real_scan_runs(
        self, temp_home, monkeypatch
    ):
        """EM2 on the timeline: #6 is held to Anthropic's figure only. The
        Admin API goes down at t30: the engine keeps the key while the last
        answer is no more than 20 minutes old, leaves it for the account
        underneath once it is older (spend-unknown, no latch), enters no
        other key meanwhile (A10: #7's rows say why), and comes back to #6
        when the Admin API answers again. The real scan runs on the fake
        clock throughout: its stamp is the engine's time, and once
        Anthropic's last answer is an hour old #6's row shows this machine's
        figure, which is every line written to its session files."""
        h, world = _org_world(temp_home, monkeypatch)
        with Run(h, world) as run:
            tick = run.step()
            assert tick.active == "6", f"t0: {[e.human() for e in tick.holds]}"
            reading = world.readings()["6"]
            assert reading["source"] == api_spend.SOURCE_ADMIN
            assert reading["spend"]["used"] == pytest.approx(world.dollars("6", scanned=False))
            # The real scan ran at the fake clock, over the real session files.
            store = LedgerStore(h.switcher.backup_dir / DB_FILENAME)
            try:
                assert float(store.get_meta(LAST_SCAN_META)) == world.now
            finally:
                store.close()
            seven = world.readings()["7"]
            assert seven["source"] == api_spend.SOURCE_LEDGER and seven["as_of"] == world.now
            assert seven["spend"]["used"] == pytest.approx(10.0)
            left_at = back_at = None
            for _ in range(1, 170):
                tick = run.step()
                where = f"t{tick.t} ({_clock(tick.now)})"
                last = max(world.admin_answers)
                age = world.now - last
                if left_at is None:
                    if tick.active == "6":
                        # Kept while Anthropic's last answer is fresh enough.
                        assert age <= api_spend.SPEND_FRESH_S, f"{where}: {age:.0f}s"
                        continue
                    left_at = tick.t
                    s = tick.switches[0]
                    # Within a tick of the figure going stale, as the engine knows it.
                    assert api_spend.SPEND_FRESH_S < age <= api_spend.SPEND_FRESH_S + MINUTE, (
                        f"{where}: left with Anthropic's figure {age:.0f}s old"
                    )
                    assert tick.t > ADMIN_DOWN
                    assert (_num(s.to_ref), s.trigger) == ("1", "at-limit"), s.human()
                    assert s.limit_reason == api_spend.SPEND_UNKNOWN, s.human()
                    assert s.human() == (
                        "Switched #6 build → #1 maya@example.com · its spend could not be "
                        "measured for 20 minutes; no subscription has room, so back to the "
                        "account underneath"
                    ), s.human()
                    continue
                if back_at is None:
                    entered = [m.to for m in run.moves if m.t == tick.t and m.to in KEYS]
                    assert "7" not in entered and "5" not in entered, f"{where}\n{run.trace()}"
                    if not world.admin_up:
                        assert tick.active == "1", f"{where}\n{run.trace()}"
                        # A10 on #7's rows (issue #14), and #6's own reason.
                        rows = world.readings()
                        assert rows["7"]["key_left"]["left"] == "6"
                        assert api_spend.spend_text(rows["7"], world.now).endswith(
                            api_spend.KEY_LEFT_WORDS)
                        assert api_spend.engine_limit_reason(
                            rows["6"], now=world.now, limited=True) == api_spend.SPEND_UNKNOWN
                    if world.now - last > admin_api_stale_max():
                        # An hour without an answer: this machine's figure,
                        # every line the real scans read, shown but never
                        # held to the limit.
                        six = world.readings()["6"]
                        assert six["source"] == api_spend.SOURCE_LEDGER, where
                        assert api_spend.machine_only(six)
                        assert six["spend"]["used"] == pytest.approx(world.dollars("6"), abs=1e-6)
                        assert world.now - six["as_of"] < SPEND_SCAN_MIN_GAP_S + MINUTE
                    if tick.active == "6":
                        back_at = tick.t
                        assert tick.switches[0].trigger == "at-limit"
                        # Back with the first answer after the outage (the
                        # organization backs off after each failure, so that
                        # can be a while after the Admin API is up again).
                        again = [a for a in world.admin_answers if a >= T0 + ADMIN_UP * MINUTE]
                        assert again and world.now - again[0] <= MINUTE, f"{where}\n{run.trace()}"
            assert left_at is not None, f"never left #6\n{run.trace()}"
            assert back_at is not None, f"never back on #6\n{run.trace()}"
        assert [m.to for m in run.entries_now()] == ["6", "6"], run.trace()
        assert not world.claims
        _no_flap(run)


def admin_api_stale_max() -> float:
    from ai_quota_swap import admin_api

    return admin_api.STALE_MAX_S


# == Offline nights, dead tokens, accounts that stop being read ============================
#
# These drive the real collector, usage store, backoff and token refresh: only
# the network is fake (``FakeNet``: the token endpoint, the usage endpoint and
# the engine's reachability check), so what the log says and how often each
# account is tried are the production code's.


NIGHT = {"1": "night1@example.com", "2": "night2@example.com", "3": "night3@example.com"}


class _Answer:
    """What ``urlopen`` hands back: a body and no headers."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()
        self.headers: dict = {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeNet:
    """The network the engine's reads and its reachability check meet.

    While ``up`` is false every request and every check fails the way a
    machine without DNS does. While up, the token endpoint renews any refresh
    token and the usage endpoint answers each account's ``pct`` (by the
    access token's prefix); anything else (the plan read) answers 404.
    ``requests`` records ``(now, account, what, ok)`` for every request."""

    def __init__(self, h: EngineHarness, monkeypatch, pct: dict[str, float]):
        self.h = h
        self.up = True
        self.pct = dict(pct)
        self.requests: list[tuple[float, str, str, bool]] = []
        self.probes: list[float] = []
        self.renewed = 0
        monkeypatch.setattr(oauth.urllib.request, "urlopen", self.urlopen)
        monkeypatch.setattr(autoswitch, "host_unreachable", self.probe)

    @staticmethod
    def _account(token: str) -> str:
        for num in ("1", "2", "3"):
            if token.startswith(f"sk-{num}"):
                return num
        return "1" if token.startswith("sk-live") else "?"

    def probe(self, host: str, port: int = 443) -> str | None:
        assert host == oauth.USAGE_HOST
        self.probes.append(self.h.clock.now)
        return None if self.up else "network (dns)"

    def urlopen(self, req, timeout=None):
        url = req.full_url
        if url == oauth.OAUTH_TOKEN_URL:
            token = json.loads(req.data.decode())["refresh_token"]
            num, what = ("1" if token.startswith("rt-live") else token[3:4]), "refresh"
        elif url.startswith(f"https://{oauth.USAGE_HOST}/api/oauth/usage"):
            token = req.get_header("Authorization").removeprefix("Bearer ")
            num, what = self._account(token), "usage"
        else:
            num, what = "-", "other"
        self.requests.append((self.h.clock.now, num, what, self.up))
        if not self.up:
            raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
        if what == "refresh":
            self.renewed += 1
            return _Answer({
                "access_token": f"sk-{num}-r{self.renewed}",
                "refresh_token": f"rt-{num}-r{self.renewed}",
                "expires_in": 8 * 3600,
            })
        if what == "usage":
            return _Answer({
                "five_hour": {"utilization": self.pct[num], "resets_at": _iso_at(T0 + 4 * HOUR)},
                "seven_day": {"utilization": 5.0, "resets_at": _iso_at(T0 + 5 * DAY)},
            })
        raise urllib.error.HTTPError(url, 404, "not here", hdrs=None, fp=None)

    def failed(self, num: str | None = None) -> list[tuple[float, str, str, bool]]:
        return [r for r in self.requests if not r[3] and (num is None or r[1] == num)]


def _night_fleet(temp_home, monkeypatch, *, accounts=("1", "2", "3"), expired=True):
    """Three subscriptions on the suite's fake home; #1 is the live login and,
    with ``expired``, its token ran out while Claude Code owned it (the live
    copy and the backup both, so the engine's own refresh must POST)."""
    monkeypatch.setattr("ai_quota_swap.switcher._FETCH_STAGGER_S", 0)
    h = EngineHarness(temp_home)
    h.clock.now = T0
    for num in accounts:
        h.seed(int(num), NIGHT[num], expires_at=1000 if (num == "1" and expired) else None)
    live = {"accessToken": "sk-live", "refreshToken": "rt-live"}
    if expired:
        live["expiresAt"] = 1000
    (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": live}))
    (temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": NIGHT["1"], "accountUuid": "uuid-1"},
    }))
    monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
    net = FakeNet(h, monkeypatch, {"1": 40.0, "2": 10.0, "3": 20.0})
    return h, net


def _lines(caplog, pattern: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if re.search(pattern, r.getMessage())]


class TestOfflineNight:
    """The owner's log: 12 hours without a network, 1,650 "dead refresh
    token?" warnings, the per-account failure line once a minute, and the
    failures counted toward failover. None of that should happen."""

    def test_twelve_hours_offline_then_back(self, temp_home, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="ai-quota-swap")
        h, net = _night_fleet(temp_home, monkeypatch)
        net.up = False
        for minute in range(12 * 60):
            outcome = h.engine.tick()
            assert outcome is TickOutcome.NO_ACTION, f"minute {minute}: {h.kinds()[-3:]}"
            assert h.engine._unhealthy_ticks == 0, f"minute {minute}"
            assert h.engine._next_delay(outcome) < 2 * MINUTE, "offline keeps the normal cadence"
            h.clock.advance(MINUTE)
        assert not [e for e in h.events if isinstance(e, SwitchEvent)]
        holds = [e for e in h.events if isinstance(e, NoSwitchEvent)]
        assert {e.reason for e in holds} == {"offline"}
        # Says since when: the first check of the night.
        assert holds[-1].since == _iso_at(T0)
        assert re.fullmatch(
            r"No switch: offline since [A-Z][a-z]{2} \d+ \d\d:\d\d \(DNS lookups fail\); "
            r"auto-switch waits for the network and does not fail over meanwhile",
            holds[-1].human(),
        ), holds[-1].human()
        assert holds[-1].detail == f"network (dns) since {_iso_at(T0)}"
        # The log: one line for the episode, one failure line per account, no
        # dead-token warning.
        assert _lines(caplog, "dead refresh token") == []
        assert _lines(caplog, r"^Offline") == [
            "Offline: Claude Code usage reads are failing (network (dns)); auto-switch "
            "waits for the network and does not fail over meanwhile"
        ]
        assert _lines(caplog, r"Usage fetch failed") == [
            "Usage fetch failed for account 2: network (dns)",
            "Usage fetch failed for account 3: network (dns)",
        ]
        assert _lines(caplog, "Back online") == []
        # The per-row backoff paces the night: never twice a minute.
        for num in ("1", "2", "3"):
            times = [t for t, n, _what, _ok in net.failed() if n == num]
            assert times, num
            assert all(b - a >= OFFLINE_RETRY_S for a, b in zip(times, times[1:])), num
            assert len(times) < 12 * 6 + 10, (num, len(times))  # about every 10 minutes
        # The reachability check: about once a minute at most.
        assert all(b - a >= REACHABILITY_TTL_S for a, b in zip(net.probes, net.probes[1:]))
        # A failed read is one request here: a usage request, or the refresh
        # of #1's expired token (the plan reads are not usage reads).
        failed_overnight = len([r for r in net.failed() if r[2] in ("usage", "refresh")])

        # The network comes back: the first check reads again at once.
        net.up = True
        back_at = h.clock.now
        h.events.clear()
        assert h.engine.tick() is TickOutcome.NO_ACTION
        (back,) = _lines(caplog, "Back online")
        assert back == (
            "Back online: Claude Code usage reads work again after 12h 0m offline; "
            f"{failed_overnight} reads failed meanwhile"
        )
        assert [r for r in net.requests if r[0] >= back_at and r[2] == "refresh"], "renewed"
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["below-threshold"]
        assert "offline" not in h.state()

        # And switching works again: #1 climbs past its line.
        net.pct["1"] = 95.0
        for _ in range(3):
            h.clock.advance(MINUTE)
            if h.engine.tick() is TickOutcome.SWITCHED:
                break
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert (switch.trigger, h.active_number()) == ("proactive", 2)

    def test_a_fresh_engine_per_check_writes_each_line_once(self, temp_home, monkeypatch, caplog):
        """`qswap auto --once` from cron builds a new engine every check: the
        episode is shared through the state file."""
        caplog.set_level(logging.INFO, logger="ai-quota-swap")
        h, net = _night_fleet(temp_home, monkeypatch)
        net.up = False
        for _ in range(90):
            h.engine = h._make_engine()
            assert h.engine.tick() is TickOutcome.NO_ACTION
            h.clock.advance(MINUTE)
        assert len(_lines(caplog, r"^Offline")) == 1
        assert h.state()["offline"]["detail"] == "network (dns)"
        net.up = True
        h.engine = h._make_engine()
        h.engine.tick()
        h.engine = h._make_engine()
        h.clock.advance(MINUTE)
        h.engine.tick()
        assert len(_lines(caplog, "Back online")) == 1
        assert "after 1h 30m offline" in _lines(caplog, "Back online")[0]

    def test_a_connection_without_working_reads_does_not_end_the_episode(
        self, temp_home, monkeypatch, caplog,
    ):
        """The host takes a TCP connection (a captive portal, say) but every
        read still fails: the episode stays open, each account is retried
        about once a minute, and the log does not flap."""
        caplog.set_level(logging.INFO, logger="ai-quota-swap")
        h, net = _night_fleet(temp_home, monkeypatch)
        net.up = False
        monkeypatch.setattr(autoswitch, "host_unreachable", lambda host, port=443: None)
        for _ in range(60):
            assert h.engine.tick() is TickOutcome.NO_ACTION
            h.clock.advance(MINUTE)
        assert len(_lines(caplog, r"^Offline")) == 1
        assert _lines(caplog, "Back online") == []
        for num in ("1", "2", "3"):
            times = [t for t, n, _what, _ok in net.failed() if n == num]
            assert all(b - a >= OFFLINE_RETRY_S for a, b in zip(times, times[1:])), num


class TestExpiredAndOwnedOneAccount:
    """One account, its token expired while Claude Code owns it, and nothing
    the engine can read to tell an idle machine from a dead token."""

    @pytest.fixture
    def h(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "solo@example.com")
        h.make_live("solo@example.com", 1)
        return h

    def test_unreachable_host_holds_as_offline(self, h, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger="ai-quota-swap")
        probes: list[float] = []

        def unreachable(host, port=443):
            probes.append(h.clock.now)
            return "network (dns)"

        monkeypatch.setattr(autoswitch, "host_unreachable", unreachable)
        for _ in range(int(2 * IDLE_HOLD_MAX_S / MINUTE)):
            assert h.tick_with_usage({"1": USAGE_TOKEN_EXPIRED}) is TickOutcome.NO_ACTION
            assert h.engine._unhealthy_ticks == 0
            assert h.engine._idle_hold_since is None
            h.clock.advance(MINUTE)
        assert {e.reason for e in h.events if isinstance(e, NoSwitchEvent)} == {"offline"}
        assert _lines(caplog, "dead refresh token") == []
        assert len(_lines(caplog, r"^Offline")) == 1
        assert len(probes) <= 2 * IDLE_HOLD_MAX_S / REACHABILITY_TTL_S + 1

    def test_reachable_host_is_the_old_path_with_one_warning(self, h, caplog):
        caplog.set_level(logging.INFO, logger="ai-quota-swap")
        reasons = []
        for _ in range(int(IDLE_HOLD_MAX_S / NO_RESET) + 12):
            outcome = h.tick_with_usage({"1": USAGE_TOKEN_EXPIRED})
            reasons.append([e.reason for e in h.events if isinstance(e, NoSwitchEvent)][-1])
            h.clock.advance(NO_RESET)
        # Held as idle for the cap, then counted toward failover, which with
        # no other account has nowhere to go.
        assert reasons[0] == "active-idle"
        assert reasons[: int(IDLE_HOLD_MAX_S / NO_RESET) + 1] == ["active-idle"] * (
            int(IDLE_HOLD_MAX_S / NO_RESET) + 1
        )
        tail = reasons[int(IDLE_HOLD_MAX_S / NO_RESET) + 1:]
        assert tail[:2] == ["active-usage-unknown", "active-usage-unknown"]
        assert set(tail[2:]) == {"no-candidates"}
        assert outcome is TickOutcome.BLOCKED
        assert _lines(caplog, "dead refresh token") == [
            "Active token of account 1 expired and owned for over 30 minutes while the usage "
            "host answers; resuming unhealthy counting (dead refresh token?)"
        ]
        assert _lines(caplog, r"^Offline") == []


NO_RESET = autoswitch.NO_RESET_FALLBACK_S


class TestDeadTokenWhileOnline:
    def test_still_fails_over_and_the_reads_outrank_the_check(self, temp_home, monkeypatch):
        """A refused login on the active account while the others read fine
        fails over after three checks, as before, even if the reachability
        check says the host is down: a read that worked is the better word."""
        h = EngineHarness(temp_home)
        for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
            h.seed(num, email)
        h.make_live("a@example.com", 1)
        monkeypatch.setattr(autoswitch, "host_unreachable", lambda host, port=443: "network (dns)")
        dead = {"1": USAGE_RELOGIN_REQUIRED, "2": _usage_now(h, 10), "3": _usage_now(h, 50)}
        assert h.tick_with_usage(dead) is TickOutcome.NO_ACTION
        assert h.tick_with_usage(dead) is TickOutcome.NO_ACTION
        assert h.tick_with_usage(dead) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert (switch.trigger, h.active_number()) == ("failover", 2)
        assert not [e for e in h.events if isinstance(e, NoSwitchEvent) and e.reason == "offline"]


def _usage_now(h: EngineHarness, pct: float) -> dict:
    return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}


class TestAccountsThatStopBeingRead:
    """One line when an account stops being readable for a reason that will
    not clear by itself, with the step that fixes it; one when it is read
    again. Account numbers only: no email reaches these lines."""

    @pytest.fixture
    def h(self, temp_home):
        h = EngineHarness(temp_home)
        for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
            h.seed(num, email)
        h.make_live("a@example.com", 1)
        return h

    def _tick(self, h, two: UsageEntry) -> None:
        now = h.clock.now
        healthy = UsageEntry(last_good=_usage_now(h, 10), fetched_at=now, age_s=0.0)
        h.tick_with_entries({"1": healthy, "2": two, "3": healthy})
        h.clock.advance(MINUTE)

    @pytest.mark.parametrize(
        ("entry", "line"),
        [
            (
                UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED, last_error="invalid_grant"),
                "Account 2 is no longer readable: its token refresh was refused "
                "(invalid_grant). To fix it, run: qswap login 2",
            ),
            (
                UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED, last_error="no_refresh_token"),
                "Account 2 is no longer readable: its saved login has no refresh token. "
                "To fix it, run: qswap login 2",
            ),
            (
                UsageEntry(
                    last_error="http-401", error_detail=oauth.REFUSED_AFTER_REFRESH,
                    consecutive_failures=1,
                ),
                "Account 2 is no longer readable: the usage endpoint refused a freshly "
                "renewed token (HTTP 401). To fix it, run: qswap login 2",
            ),
            (
                UsageEntry(sentinel=USAGE_KEYCHAIN_UNAVAILABLE),
                "Account 2 is no longer readable: the Keychain could not be read (locked "
                "or busy). Reads resume by themselves once the Keychain can be read",
            ),
            (
                UsageEntry(sentinel=USAGE_FOREIGN_CREDENTIAL),
                "Account 2 is no longer readable: the live login belongs to another "
                "account. Switching to it repairs it: qswap switch 2",
            ),
            (
                UsageEntry(
                    last_error="refresh-failed", error_detail="http-400",
                    login_expires_at=T0 - DAY,
                ),
                "Account 2 is no longer readable: its login reached its end date "
                f"({_iso_at(T0 - DAY)}). To fix it, run: qswap login 2",
            ),
            (
                UsageEntry(
                    sentinel=USAGE_RELOGIN_REQUIRED, last_error="invalid_grant",
                    login_expires_at=T0 - HOUR,
                ),
                "Account 2 is no longer readable: its token refresh was refused "
                f"(invalid_grant); its login reached its end date ({_iso_at(T0 - HOUR)}). "
                "To fix it, run: qswap login 2",
            ),
        ],
    )
    def test_one_line_each_way(self, h, caplog, entry, line):
        caplog.set_level(logging.INFO, logger="ai-quota-swap")
        h.clock.now = T0
        self._tick(h, UsageEntry(last_good=_usage_now(h, 10), fetched_at=h.clock.now, age_s=0.0))
        for _ in range(5):
            self._tick(h, entry)
        written = _lines(caplog, "readable")
        assert written == [line]
        assert not any("@example.com" in w for w in written)
        assert h.state()["unreadable"]["2"][0]
        # A fresh engine (a `--once` run) does not say it again.
        h.engine = h._make_engine()
        self._tick(h, entry)
        assert _lines(caplog, "readable") == [line]
        # Cleared but not read since: not yet.
        self._tick(h, UsageEntry(last_good=_usage_now(h, 10), fetched_at=T0, age_s=60.0))
        assert len(_lines(caplog, "readable")) == 1
        # Read again.
        self._tick(h, UsageEntry(last_good=_usage_now(h, 10), fetched_at=h.clock.now, age_s=0.0))
        assert _lines(caplog, "readable")[1:] == ["Account 2 is readable again"]
        assert "unreadable" not in h.state()

    @pytest.mark.parametrize(
        "entry",
        [
            UsageEntry(last_error="network", error_detail="network (dns)", consecutive_failures=4),
            UsageEntry(last_error="http-429", retry_after_s=None, consecutive_failures=1)
            if False else UsageEntry(last_error="http-429", consecutive_failures=1),
            UsageEntry(sentinel=USAGE_TOKEN_EXPIRED),
            UsageEntry(last_error="http-503", consecutive_failures=2, login_expires_at=None),
        ],
    )
    def test_states_that_clear_by_themselves_write_nothing(self, h, caplog, entry):
        caplog.set_level(logging.INFO, logger="ai-quota-swap")
        for _ in range(4):
            self._tick(h, entry)
        assert _lines(caplog, "readable") == []


class TestRepeatFailureFilter:
    @staticmethod
    def _record(tag=None) -> logging.LogRecord:
        record = logging.LogRecord("ai-quota-swap", logging.WARNING, __file__, 1, "x", None, None)
        if tag is not None:
            setattr(record, oauth.FAILURE_LINE, tag)
        return record

    def test_first_line_per_account_then_counted(self):
        quiet = RepeatFailureFilter("claude", logged={"3"})
        assert quiet.filter(self._record(("claude", "2"))) is True
        assert quiet.filter(self._record(("claude", "2"))) is False
        assert quiet.filter(self._record(("claude", "3"))) is False
        assert quiet.filter(self._record(("codex", "2"))) is True  # another provider's
        assert quiet.filter(self._record()) is True  # any other line
        assert quiet.held == 2

    def test_a_filter_nobody_touches_lets_everything_through(self, monkeypatch):
        quiet = RepeatFailureFilter("claude", logged={"2"})
        later = autoswitch.time.monotonic() + autoswitch.QUIET_STALE_S + 1
        monkeypatch.setattr(autoswitch.time, "monotonic", lambda: later)
        assert quiet.filter(self._record(("claude", "2"))) is True

    def test_stop_lets_the_lines_through_again(self, temp_home, monkeypatch, caplog):
        h, net = _night_fleet(temp_home, monkeypatch)
        net.up = False
        h.engine.tick()
        logger = logging.getLogger("ai-quota-swap")
        assert any(isinstance(f, RepeatFailureFilter) for f in logger.filters)
        h.engine.stop()
        assert not any(isinstance(f, RepeatFailureFilter) for f in logger.filters)


# == Saved logins that end (autoswitch.watchLoginExpiry) =====================================

from ai_quota_swap.autoswitch import LOGIN_ENDING_S, LOGIN_LEAVE_S  # noqa: E402


def _reading(now: float, pct: float, *, weekly: float = 0.0, weekly_in: float = 5 * DAY,
             ends_in: float | None = None) -> UsageEntry:
    """A fresh reading; ``ends_in`` puts the saved login's end date that
    many seconds from ``now`` (negative: already past)."""
    usage = {
        "five_hour": {"pct": pct, "resets_at": _iso_at(now + 3 * HOUR)},
        "seven_day": {"pct": weekly, "resets_at": _iso_at(now + weekly_in)},
    }
    return UsageEntry(
        last_good=usage, fetched_at=now, age_s=0.0,
        login_expires_at=None if ends_in is None else now + ends_in,
    )


def _fleet(temp_home, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, **settings)
    for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        h.seed(num, email)
    h.make_live("a@example.com", 1)
    return h


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


class TestChoosingAroundAnEndingLogin:
    """Rule 1: a candidate whose login ends within LOGIN_ENDING_S (or has
    passed its end date) ranks behind every other, in every strategy, and is
    still taken when nothing else will do."""

    @staticmethod
    def _picks(h: EngineHarness, strategy: str, ending: bool) -> SwitchEvent:
        now = h.clock.now
        two_ends = 1 * HOUR if ending else None
        if strategy in ("best", "weekly-first"):
            entries = {
                "1": _reading(now, 95.0),
                # best's pick is #2 (most room); weekly-first's is #2 too (its
                # weekly window resets first).
                "2": _reading(now, 10.0, weekly_in=1 * DAY, ends_in=two_ends),
                "3": _reading(now, 30.0, weekly_in=3 * DAY),
            }
        else:
            # Below the line: consume-first moves to a sooner weekly reset,
            # priority-consume-first to a higher tier; both pick #2.
            entries = {
                "1": _reading(now, 40.0, weekly_in=6 * DAY),
                "2": _reading(now, 20.0, weekly_in=1 * DAY, ends_in=two_ends),
                "3": _reading(now, 20.0, weekly_in=2 * DAY),
            }
            if strategy == "priority-consume-first":
                h.switcher.set_account_priority("2", 3)
                h.switcher.set_account_priority("3", 2)
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED, [
            (e.kind, getattr(e, "reason", "")) for e in h.events
        ]
        return _switches(h)[-1]

    @pytest.mark.parametrize("ending", [False, True])
    @pytest.mark.parametrize(
        "strategy", ["best", "weekly-first", "consume-first", "priority-consume-first"]
    )
    def test_a_layer_above_every_strategy(self, temp_home, strategy, ending):
        h = _fleet(temp_home, strategy=strategy)
        assert self._picks(h, strategy, ending=ending).to_ref["number"] == (3 if ending else 2)

    def test_a_passed_end_date_ranks_last_too(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 95.0),
            "2": _reading(now, 10.0, ends_in=-DAY),
            "3": _reading(now, 30.0),
        }) is TickOutcome.SWITCHED
        assert _switches(h)[-1].to_ref["number"] == 3

    def test_still_taken_when_nothing_else_has_room(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        # At the limit, the only account with room has an hour of login left.
        assert h.tick_with_entries({
            "1": _reading(now, 100.0),
            "2": _reading(now, 10.0, ends_in=HOUR),
            "3": _reading(now, 100.0),
        }) is TickOutcome.SWITCHED
        assert (_switches(h)[-1].trigger, h.active_number()) == ("at-limit", 2)

    def test_a_move_at_the_line_takes_it_as_the_last_choice(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 95.0),
            "2": _reading(now, 10.0, ends_in=HOUR),
            "3": _reading(now, 96.0),
        }) is TickOutcome.SWITCHED
        assert (_switches(h)[-1].trigger, h.active_number()) == ("proactive", 2)

    def test_a_move_at_the_line_never_lands_on_a_login_about_to_end(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _reading(now, 95.0),
            "2": _reading(now, 10.0, ends_in=LOGIN_LEAVE_S - 60),
            "3": _reading(now, 96.0),
        })
        assert outcome is TickOutcome.BLOCKED
        assert _switches(h) == []

    def test_an_escape_may_land_on_one(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 100.0),
            "2": _reading(now, 10.0, ends_in=LOGIN_LEAVE_S - 60),
            "3": _reading(now, 100.0),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_accounts_without_an_end_date_are_unaffected(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 95.0), "2": _reading(now, 10.0), "3": _reading(now, 30.0),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_off_ranks_as_before(self, temp_home):
        h = _fleet(temp_home, watch_login_expiry=False)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 95.0),
            "2": _reading(now, 10.0, ends_in=LOGIN_LEAVE_S - 60),
            "3": _reading(now, 30.0),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2


class TestLeavingAnEndingLogin:
    """Rule 2: the active account is left LOGIN_LEAVE_S before its login
    ends (or once its end date has passed) for an account with room whose
    login lasts; with nowhere better to go it stays and says why."""

    def test_leaves_with_a_trigger_of_its_own(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 40.0, ends_in=12 * MINUTE),
            "2": _reading(now, 60.0),
            "3": _reading(now, 95.0),
        }) is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert (switch.trigger, h.active_number()) == ("login-expiry", 2)
        assert switch.to_json()["trigger"] == "login-expiry"
        assert switch.human() == (
            "Switched #1 a@example.com → #2 b@example.com · its login ends in 12m; "
            "qswap login 1 renews it"
        )
        assert h.state()["leftTrigger"] == "login-expiry"

    def test_the_words_for_the_time_left(self):
        from ai_quota_swap import autolog

        assert autolog.login_end_words(T0 + 12 * MINUTE, 12 * MINUTE) == "its login ends in 12m"
        assert autolog.login_end_words(T0 - HOUR, -HOUR, _iso_at(T0)).startswith(
            "its login reached its end date ("
        )
        assert autolog.login_end_words(None, None) == "its login is about to end"

    def test_a_passed_end_date_is_left_too(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 40.0, ends_in=-HOUR),
            "2": _reading(now, 60.0),
            "3": _reading(now, 95.0),
        }) is TickOutcome.SWITCHED
        text = _switches(h)[0].human()
        assert "· its login reached its end date (" in text, text
        assert text.endswith("; qswap login 1 renews it")

    def test_over_its_line_the_login_decides(self, temp_home):
        """Over its line with its login about to end: any healthy account
        whose login lasts will do, though it would not beat the active by the
        hysteresis margin at the line."""
        h = _fleet(temp_home)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 91.0, ends_in=5 * MINUTE),
            "2": _reading(now, 85.0),
            "3": _reading(now, 97.0),
        }) is TickOutcome.SWITCHED
        assert (_switches(h)[0].trigger, h.active_number()) == ("login-expiry", 2)

    def test_nowhere_better_to_go(self, temp_home):
        h = _fleet(temp_home)
        now = h.clock.now
        for _ in range(5):
            assert h.tick_with_entries({
                "1": _reading(now, 40.0, ends_in=12 * MINUTE),
                "2": _reading(now, 10.0, ends_in=LOGIN_ENDING_S - MINUTE),  # ending too
                "3": _reading(now, 95.0),                                   # over its line
            }) is TickOutcome.NO_ACTION
        assert _switches(h) == []
        hold = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert hold.reason == "login-ending"
        assert hold.human() == (
            "No switch: #1 a@example.com's login ends in 12m, and no account with room "
            "has a login that lasts longer; qswap login 1 renews it"
        )
        assert hold.to_json()["detail"].startswith("account 1's login ends at ")

    def test_only_account(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        assert h.tick_with_entries({"1": _reading(now, 40.0, ends_in=MINUTE)}) is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["login-ending"]

    def test_waits_out_the_cooldown(self, temp_home):
        h = _fleet(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now - 60))
        now = h.clock.now
        entries = {
            "1": _reading(now, 40.0, ends_in=12 * MINUTE),
            "2": _reading(now, 60.0),
            "3": _reading(now, 95.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["cooldown"]
        h.clock.advance(COOLDOWN)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 40.0, ends_in=12 * MINUTE - COOLDOWN),
            "2": _reading(now, 60.0),
            "3": _reading(now, 95.0),
        }) is TickOutcome.SWITCHED

    @pytest.mark.parametrize("strategy", ["best", "consume-first", "priority-consume-first"])
    def test_no_ping_pong(self, temp_home, strategy):
        """#1's login ends; it is left for #2, then for an hour #2 burns and
        #1 is the account every strategy would otherwise go back to: the most
        room, the sooner weekly reset, the higher tier. It never goes back."""
        h = _fleet(temp_home, strategy=strategy)
        h.switcher.set_account_disabled("3", True)
        if strategy == "priority-consume-first":
            h.switcher.set_account_priority("1", 5)
        start = h.clock.now
        ends = start + 10 * MINUTE
        two = 50.0
        for _minute in range(60):
            now = h.clock.now
            two = min(two + 1.0, 99.0) if h.active_number() == 2 else two
            h.tick_with_entries({
                "1": UsageEntry(
                    last_good=_reading(now, 5.0, weekly_in=1 * DAY).last_good,
                    fetched_at=now, age_s=0.0, login_expires_at=ends,
                ),
                "2": _reading(now, two, weekly_in=4 * DAY),
                "3": _reading(now, 95.0),
            })
            h.clock.advance(MINUTE)
        moves = [(s.from_ref["number"], s.to_ref["number"], s.trigger) for s in _switches(h)]
        assert moves == [(1, 2, "login-expiry")], moves

    def test_off_stays_as_before(self, temp_home):
        h = _fleet(temp_home, watch_login_expiry=False)
        now = h.clock.now
        assert h.tick_with_entries({
            "1": _reading(now, 40.0, ends_in=-HOUR),
            "2": _reading(now, 60.0),
            "3": _reading(now, 95.0),
        }) is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["below-threshold"]

    def test_the_setting_is_a_documented_autoswitch_key(self, temp_home):
        from ai_quota_swap.settings import SETTING_SPECS, load_settings

        spec = SETTING_SPECS["autoswitch.watchLoginExpiry"]
        assert (spec.kind, spec.default) == ("bool", True)
        root = temp_home / "settings-root"
        root.mkdir()
        set_setting(root, "autoswitch.watchLoginExpiry", "false")
        assert load_settings(root).watch_login_expiry is False


class TestTheCollectorCarriesTheEndDate:
    """The end date comes from the credential each pass already reads: the
    live store's for the active account, the backup's for the others."""

    def test_live_and_backup(self, temp_home, monkeypatch):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        backup = json.loads(h.switcher._read_account_credentials("2", "b@example.com"))
        backup["claudeAiOauth"]["refreshTokenExpiresAt"] = 1_799_000_000_000
        h.switcher._write_account_credentials("2", "b@example.com", json.dumps(backup))
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "refreshTokenExpiresAt": 1_798_000_000_000,
            },
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"},
        }))
        entries = h.switcher.usage_entries_by_account(fetch=set())
        assert entries["1"].login_expires_at == 1_798_000_000.0
        assert entries["2"].login_expires_at == 1_799_000_000.0
