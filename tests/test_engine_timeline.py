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
scan (``scan_recent``, which reads transcripts on the wall clock; here it
moves the transcript lines written so far into the ledger and stamps the
fake clock), the subscriptions' usage endpoint (``usage_entries_by_account``,
``refresh_usage``) and the reset claim (``claim_usage_reset``). The real
claim (``resets.claim``) is made to fail the test if anything reaches it.

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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_quota_swap import api_spend, claude_settings, resets
from ai_quota_swap.autoswitch import (
    RETURN_ROOM_PCT,
    SPEND_SCAN_INTERVAL_S,
    AutoSwitchEngine,
    NoSwitchEvent,
    ResetEvent,
    SwitchEvent,
    TickOutcome,
    kept_resets_now,
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
