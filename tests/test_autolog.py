"""The auto-switch log in words (autolog.py) and the engine facts behind it.

Driven through the real engine where it matters (the scenarios the demo
harness shows: a walk into the switch line, a blocked tick with accounts
that need re-login, Codex), and through hand-built events for the layout
rules: one line per tick, 80 columns, one precision per comparison. The JSON
of every event is pinned: everything here is human text only.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_quota_swap import autolog, printer
from ai_quota_swap.autolog import AutoLog, banner, pct_pair, pct_text
from ai_quota_swap.autoswitch import (
    AccountView,
    AllExhaustedEvent,
    BudgetWarningEvent,
    ConfigWarningEvent,
    ErrorEvent,
    EventContext,
    NoSwitchEvent,
    PollEvent,
    QuarantineEvent,
    ResetEvent,
    SleepEvent,
    SwitchEvent,
    TickOutcome,
    UnquarantineEvent,
)
from ai_quota_swap.json_output import USAGE_RELOGIN_REQUIRED
from ai_quota_swap.lines import BindingWindow
from ai_quota_swap.settings import AutoSwitchSettings
from ai_quota_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness

H = 3600.0


def _u(five: float, seven: float) -> dict:
    return {"five_hour": {"pct": five}, "seven_day": {"pct": seven}}


class Lines(list):
    """What an AutoLog wrote, one entry per line."""

    def text(self) -> str:
        return "\n".join(self)


def _log(**kw) -> tuple[AutoLog, Lines]:
    out = Lines()
    return AutoLog(out.append, clock=lambda: "14:20:00", **kw), out


def _strip_ts(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "ts"}


# -- the autoswitch scenario through the real engine ---------------------------------


@pytest.fixture
def walk(temp_home: Path) -> EngineHarness:
    """The demo's ``autoswitch`` scenario: #1 personal walks 86 → 88.5 → 91
    on its 5h window against a 90 line; #2 work has room; #3 night-shift is
    over its line."""
    h = EngineHarness(temp_home, threshold=90.0, cooldown_seconds=60.0)
    h.seed(1, "maya@example.com")
    h.seed(2, "maya@northwind.example.com")
    h.seed(3, "ops@example.net")
    h.make_live("maya@example.com", 1)
    h.switcher.set_alias("1", "personal")
    h.switcher.set_alias("2", "work")
    h.switcher.set_alias("3", "night-shift")
    return h


def _walk(h: EngineHarness, log: AutoLog) -> list[TickOutcome]:
    h.engine.on_event = lambda e: (h.events.append(e), log(e))
    outcomes = []
    for active in (86.0, 88.5, 91.0):
        outcomes.append(h.tick_with_usage({
            "1": _u(active, 38.0), "2": _u(12.0, 21.0), "3": _u(93.0, 81.0),
        }))
        h.clock.advance(60)
    log.flush()
    return outcomes


class TestTheWalkToASwitch:
    def test_three_ticks_then_the_switch(self, walk):
        log, out = _log()
        assert _walk(walk, log) == [TickOutcome.NO_ACTION, TickOutcome.NO_ACTION, TickOutcome.SWITCHED]
        assert out == [
            # First tick: the comparison, once.
            "14:20:00  #1 personal at 5h 86% (switch at 90%) · no switch needed",
            "            #2 work         5h 12% · 7d 21%",
            "            #3 night-shift  5h 93% · 7d 81% · over its switch line",
            # A routine tick: one line, nothing under it.
            "14:20:00  #1 personal at 5h 88% (switch at 90%) · no switch needed",
            # The switch: the reason in words, and the comparison it was made on.
            "14:20:00  Switched #1 personal → #2 work · 5h reached 91%",
            "            #2 work         5h 12% · 7d 21%",
            "            #3 night-shift  5h 93% · 7d 81% · over its switch line",
        ]

    def test_verbose_shows_the_comparison_on_every_tick(self, walk):
        log, out = _log(verbose=True)
        _walk(walk, log)
        ticks = [line for line in out if line.startswith("14:20:00")]
        assert len(ticks) == 3
        assert sum(line.strip().startswith("#2 work") for line in out) == 3
        assert sum(line.strip().startswith("#3 night-shift") for line in out) == 3

    def test_dry_run(self, walk):
        walk.engine = walk._make_engine(dry_run=True)
        log, out = _log()
        _walk(walk, log)
        assert "14:20:00  Would switch #1 personal → #2 work · 5h reached 91% (dry run)" in out
        assert walk.active_number() == 1

    def test_the_events_say_it_too(self, walk):
        log, _ = _log()
        _walk(walk, log)
        poll = next(e for e in walk.events if isinstance(e, PollEvent))
        assert poll.human() == (
            "#1 personal at 5h 86% (switch at 90%); others: #2 work at 5h 12% · 7d 21%; "
            "#3 night-shift at 5h 93% · 7d 81% · over its switch line"
        )
        hold = next(e for e in walk.events if isinstance(e, NoSwitchEvent))
        assert hold.human() == "No switch needed: 5h at 86% (switch at 90%)"
        (switch,) = [e for e in walk.events if isinstance(e, SwitchEvent)]
        assert switch.human() == "Switched #1 personal → #2 work · 5h reached 91%"
        for event in walk.events:
            assert "\n" not in event.human()
            assert "Account-" not in event.human()

    def test_the_json_is_what_it_was(self, walk):
        log, _ = _log()
        _walk(walk, log)
        payloads = [_strip_ts(e.to_json()) for e in walk.events]
        win = {"2": {"5h": 12.0, "7d": 21.0}, "3": {"5h": 93.0, "7d": 81.0}}
        assert payloads == [
            {"schemaVersion": 1, "event": "poll", "active": {"number": 1, "email": "maya@example.com"},
             "headroomPct": {"1": 14.0, "2": 79.0, "3": 7.0}, "threshold": 90.0,
             "windowsPct": {"1": {"5h": 86.0, "7d": 38.0}, **win}},
            {"schemaVersion": 1, "event": "no-switch", "reason": "below-threshold", "detail": "5h 86% < 90%"},
            {"schemaVersion": 1, "event": "poll", "active": {"number": 1, "email": "maya@example.com"},
             "headroomPct": {"1": 11.5, "2": 79.0, "3": 7.0}, "threshold": 90.0,
             "windowsPct": {"1": {"5h": 88.5, "7d": 38.0}, **win}},
            {"schemaVersion": 1, "event": "no-switch", "reason": "below-threshold", "detail": "5h 88.5% < 90%"},
            {"schemaVersion": 1, "event": "poll", "active": {"number": 1, "email": "maya@example.com"},
             "headroomPct": {"1": 9.0, "2": 79.0, "3": 7.0}, "threshold": 90.0,
             "windowsPct": {"1": {"5h": 91.0, "7d": 38.0}, **win}},
            {"schemaVersion": 1, "event": "switch", "trigger": "proactive",
             "from": {"number": 1, "email": "maya@example.com"},
             "to": {"number": 2, "email": "maya@northwind.example.com"},
             "warnings": [], "dryRun": False},
        ]
        # The engine attached the human-only facts, and they serialize nowhere.
        for event in walk.events:
            assert event.context is not None and event.context.names["2"] == "work"
            bare = dataclasses.replace(event, context=None)
            assert bare.to_json() == event.to_json()
            assert bare == event  # human-only fields never take part in equality

    def test_a_cooldown_hold_says_how_long(self, walk):
        log, out = _log()
        _walk(walk, log)
        walk.clock.advance(-30)  # 30s after the switch, inside its 60s cooldown
        out.clear()
        walk.tick_with_usage({"2": _u(95.0, 21.0), "1": _u(10.0, 38.0), "3": _u(93.0, 81.0)})
        log.flush()
        hold = [e for e in walk.events if isinstance(e, NoSwitchEvent)][-1]
        assert hold.to_json()["reason"] == "cooldown" and hold.to_json()["detail"] == ""
        assert hold.human() == "No switch yet: cooling down after the last switch, 30s left"
        assert out[0] == "14:20:00  #2 work at 5h 95% (switch at 90%) · cooling down, 30s left"


# -- the trouble scenario: a blocked tick ----------------------------------------------


@pytest.fixture
def trouble(temp_home: Path) -> EngineHarness:
    """The demo's ``trouble`` fleet: #1 at its limit; #2's login refused;
    #3's endpoint answering 503; #4 set aside by the engine; #5 read three
    hours ago and timing out since."""
    h = EngineHarness(temp_home)
    for num, email in enumerate(
        ("maya@example.com", "maya@northwind.example.com", "ops@example.net",
         "sam@example.org", "lee@example.net"), start=1,
    ):
        h.seed(num, email)
    h.make_live("maya@example.com", 1)
    for num, alias in ((1, "personal"), (2, "work"), (3, "night-shift"), (4, "side"), (5, "spare")):
        h.switcher.set_alias(str(num), alias)
    h.engine._quarantine("4", "sam@example.org", "invalid_grant")
    h.events.clear()
    now = h.clock.now
    h.entries = {
        "1": UsageEntry(last_good=_u(100.0, 64.0), fetched_at=now, age_s=0.0),
        "2": UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED),
        "3": UsageEntry(last_error="http-503", last_attempt_at=now),
        "4": UsageEntry(last_good=_u(5.0, 30.0), fetched_at=now - 3 * H, age_s=3 * H),
        "5": UsageEntry(last_good=_u(40.0, 55.0), fetched_at=now - 3 * H - 300,
                        age_s=3 * H + 300, last_error="timeout", last_attempt_at=now),
    }
    return h


class TestABlockedTick:
    def test_the_json_keeps_its_reason_and_detail(self, trouble):
        assert trouble.tick_with_entries(trouble.entries) is TickOutcome.BLOCKED
        (hold,) = [e for e in trouble.events if isinstance(e, NoSwitchEvent)]
        assert _strip_ts(hold.to_json()) == {
            "schemaVersion": 1, "event": "no-switch",
            "reason": "no-comparison", "detail": "no candidate has readable usage",
        }

    def test_the_words_name_each_account_and_the_way_out(self, trouble):
        trouble.tick_with_entries(trouble.entries)
        (hold,) = [e for e in trouble.events if isinstance(e, NoSwitchEvent)]
        assert hold.human() == (
            "No account to switch to: #2 work and #4 side need re-login; "
            "#3 night-shift usage unavailable (HTTP 503), retrying; "
            "#5 spare as of 3h 5m ago (timed out). "
            "To use #2 work and #4 side again, log in to each in Claude Code (/login), "
            "then run: qswap add"
        )
        assert "no-comparison" not in hold.human()

    def test_the_log_shows_the_comparison_and_the_next_step(self, trouble):
        log, out = _log()
        trouble.engine.on_event = log
        trouble.tick_with_entries(trouble.entries)
        log.flush()
        assert out == [
            "14:20:00  #1 personal at 5h 100% (limit reached) · nothing to switch to",
            "            #2 work         needs re-login",
            "            #3 night-shift  usage unavailable (HTTP 503), retrying",
            "            #4 side         needs re-login",
            "            #5 spare        5h 40% · 7d 55% · as of 3h 5m ago (timed out)",
            "            To use #2 work and #4 side again, log in to each in Claude Code",
            "            (/login), then run: qswap add",
        ]

    def test_an_account_set_aside_mid_tick_reads_as_needing_re_login(self):
        """Freshening #2 finds its login dead: the line saying so prints when
        it happens, and the comparison after it says "needs re-login", not
        the numbers the poll had."""
        log, out = _log()
        log(PollEvent(
            active={"number": 1, "email": "maya@example.com"}, headroom={"1": 0.0, "2": 50.0},
            threshold=90.0, windows={"1": {"5h": 100.0}, "2": {"5h": 50.0}},
        ))
        log(QuarantineEvent(number="2", email="b@example.com", reason="invalid_grant"))
        log(NoSwitchEvent(reason="no-viable-target"))
        assert out == [
            "14:20:00  #2 b@example.com needs re-login: its saved login was refused. Log in",
            "          to it in Claude Code (/login), then run: qswap add",
            "14:20:00  #1 maya@example.com at 5h 100% (limit reached) · nothing to switch to",
            "            #2  needs re-login",
            "            To use #2 again, log in to it in Claude Code (/login), then run:",
            "            qswap add",
        ]


# -- Codex ----------------------------------------------------------------------------


@pytest.fixture
def codex(temp_home: Path, tmp_path: Path, monkeypatch):
    from tests.test_codex_engine import Harness

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    env = SimpleNamespace(home=temp_home, codex_home=codex_home.resolve())
    return Harness(env, monkeypatch)


class TestCodexWording:
    def test_needs_re_login_names_codex_login(self, codex):
        from tests.codex_helpers import LOGIN_A, LOGIN_B

        codex.capture(LOGIN_B)
        codex.capture(LOGIN_A)
        codex.engine._quarantine("1", "b@example.com", "invalid_grant")
        (event,) = [e for e in codex.events if isinstance(e, QuarantineEvent)]
        assert event.human() == (
            "#1 b@example.com needs re-login: its saved login was refused. "
            "Log in to it again: qswap codex login"
        )
        assert "qswap add" not in event.human()
        assert set(event.to_json()) == {"schemaVersion", "event", "ts", "number", "email", "reason"}

    def test_a_blocked_codex_tick(self, codex):
        from tests.codex_helpers import LOGIN_A, LOGIN_B
        from tests.test_codex_engine import reading

        codex.capture(LOGIN_B, exp=time.time() - 3600)  # usage unknown until used
        codex.capture(LOGIN_A)
        codex.sw.set_alias("1", "work")
        codex.answer("acct-a", reading(100, now=codex.clock.now))
        log, out = _log(provider="codex")
        codex.engine.on_event = lambda e: (codex.events.append(e), log(e))
        assert codex.tick() is TickOutcome.BLOCKED
        log.flush()
        (hold,) = [e for e in codex.events if isinstance(e, NoSwitchEvent)]
        assert hold.to_json()["reason"] == "no-comparison"
        assert hold.human() == (
            "No account to switch to: #1 work usage unknown until used. "
            "To use #1 work anyway, switch to it: qswap codex switch 1"
        )
        assert out[0] == "14:20:00  #2 a@example.com at 7d 100% (limit reached) · nothing to switch to"
        assert out[1] == "            #1 work  usage unknown until used"
        assert out[2] == "            To use #1 work anyway, switch to it: qswap codex switch 1"

    def test_codex_re_login_advice(self):
        views = (
            AccountView("1", email="a@example.com", state="ok", windows=(("7d", 100.0),), line=99.0),
            AccountView("2", email="b@example.com", alias="work", state="needs-relogin"),
        )
        event = NoSwitchEvent(
            reason="no-qualifying-candidate",
            context=EventContext(provider="codex", accounts=views, active="1"),
        )
        assert event.human() == (
            "No account to switch to: #2 work needs re-login. "
            "To use #2 work again, log in to it again: qswap codex login"
        )


# -- layout: 80 columns, one precision -------------------------------------------------


LONG_ALIAS = "the-account-with-an-alias-far-too-long-for-any-column"
LONG_EMAIL = "someone.with.a.very.long.address@subdomain.example-company.com"


def _long_context() -> EventContext:
    return EventContext(
        names={"1": LONG_ALIAS, "3": "x" * 70},
        accounts=(
            AccountView("1", email=LONG_EMAIL, alias=LONG_ALIAS, windows=(("5h", 95.0),), line=90.0),
            AccountView("2", email=LONG_EMAIL, state="ok", line=90.0,
                        windows=(("5h", 12.0), ("7d", 21.0), ("Fable", 30.0), ("Opus", 44.0))),
            AccountView("3", email=LONG_EMAIL, alias="x" * 70, state="needs-relogin"),
            AccountView("4", email=LONG_EMAIL, state="stale", windows=(("5h", 40.0), ("7d", 55.0)),
                        age_s=3 * H, error="refresh-failed"),
        ),
        active="1",
    )


class TestEightyColumns:
    def _events(self):
        ctx = _long_context()
        poll = PollEvent(
            active={"number": 1, "email": LONG_EMAIL},
            headroom={"1": 5.0, "2": 56.0, "3": None, "4": None},
            threshold=90.0, windows={"1": {"5h": 95.0}}, context=ctx,
        )
        return ctx, poll

    def test_a_switch_tick_with_long_labels(self):
        ctx, poll = self._events()
        log, out = _log()
        log(poll)
        log(SwitchEvent(trigger="proactive", from_ref={"number": 1, "email": LONG_EMAIL},
                        to_ref={"number": 2, "email": LONG_EMAIL}, context=ctx,
                        binding=BindingWindow("5h", 95.0, 90.0, None, -5.0),
                        warnings=["a warning long enough to need wrapping " * 4]))
        assert all(len(line) <= 80 for line in out), out
        assert out[0].startswith("14:20:00  Switched #1 ")
        assert "→ #2 " in out[0] and out[0].endswith("· 5h reached 95%")
        assert "…" in out[0]  # labels cut, the reason kept whole
        assert any("…" in line for line in out[1:])

    def test_a_blocked_tick_with_long_labels(self):
        ctx, poll = self._events()
        log, out = _log()
        log(poll)
        log(NoSwitchEvent(reason="no-comparison", context=ctx))
        assert all(len(line) <= 80 for line in out), out
        assert out[0].endswith("· nothing to switch to")

    def test_long_standalone_lines_wrap(self):
        log, out = _log()
        log(ErrorEvent(message="could not freshen: CLAUDE_SECURESTORAGE_CONFIG_DIR is set — "
                               "unset it or run qswap from a normal shell", transient=True))
        log(ConfigWarningEvent(message="autoswitch.model: Opus, Sonnet matches no account's usage "
                                       "windows — only the 5h/7d limits are being watched for it (typo?)"))
        assert all(len(line) <= 80 for line in out), out
        assert out[0].startswith("14:20:00  Error: could not freshen")
        assert out[1].startswith(" " * 10) and not out[1].startswith(" " * 11)


def _personal_poll(state: str = "ok", **view) -> PollEvent:
    """#1 personal at 5h 95%, or with no reading (``state``) and the view the
    engine would attach."""
    readable = state == "ok"
    ctx = EventContext(
        names={"1": "personal", "2": "night-shift"},
        accounts=(
            AccountView("1", email="maya@example.com", alias="personal", state=state,
                        windows=(("5h", 95.0),) if readable else (), line=90.0, **view),
            AccountView("2", email="ops@example.net", alias="night-shift",
                        windows=(("5h", 93.0), ("7d", 81.0)), line=90.0),
        ),
        active="1",
    )
    return PollEvent(
        active={"number": 1, "email": "maya@example.com"},
        headroom={"1": 5.0 if readable else None, "2": 7.0}, threshold=90.0,
        windows={"1": {"5h": 95.0}} if readable else {}, context=ctx,
    )


class TestOneLinePerTick:
    @pytest.mark.parametrize("reason, detail, extra, state, view", [
        ("below-threshold", "", {}, "ok", {}),
        ("cooldown", "", {"wait_s": 3540.0}, "ok", {}),
        ("already-consuming-soonest", "", {}, "ok", {}),
        ("reset-unknown", "", {}, "ok", {}),
        ("already-consuming-priority", "", {}, "ok", {}),
        ("no-qualifying-candidate", "", {}, "ok", {}),
        ("stale-usage", "", {"account": "2"}, "ok", {}),
        ("switch-deferred", "Codex is renewing the live token", {}, "ok", {}),
        ("already-active", "", {}, "ok", {}),
        ("active-usage-unknown", "1/3 before failover", {}, "unavailable", {"error": "http-503"}),
        ("active-idle", "token expired while Claude Code is idle; resumes on next use", {},
         "sentinel", {"sentinel": "token expired"}),
        ("active-api-key", "", {}, "api-key", {}),
    ])
    def test_each_decision_fits_one_line(self, reason, detail, extra, state, view):
        poll = _personal_poll(state, **view)
        log, out = _log()
        log(poll)
        log(NoSwitchEvent(reason=reason, detail=detail, context=poll.context, **extra))
        assert out[0].startswith("14:20:00  #1 personal ")
        assert len(out[0]) <= 80, out[0]
        assert not any(line.startswith(" " * 10) and not line.startswith(INDENT) for line in out), out
        assert reason not in out[0]

    def test_a_reset_spent_mid_tick_is_quoted_after_the_refill(self):
        """The poll read Fable 100%; a usage reset refilled it before the
        decision. The tick quotes the window the engine decided on, not a
        "limit reached" next to "no switch needed"."""
        ctx = EventContext(names={"1": "personal"})
        log, out = _log()
        log(PollEvent(active={"number": 1, "email": "maya@example.com"}, headroom={"1": 0.0},
                      threshold=90.0, windows={"1": {"7d": 74.0, "Fable": 100.0}}, context=ctx))
        log(ResetEvent(account={"number": 1, "email": "maya@example.com"}, context=ctx,
                       plan={"reason": "active-limit", "window": "Fable", "pct": 100.0,
                             "grantKind": "Fable", "scope": "model"},
                       result={"result": "reset", "resetsLeft": 0}))
        log(NoSwitchEvent(reason="below-threshold", detail="7d 52% < 90%", context=ctx,
                          binding=BindingWindow("7d", 52.0, 90.0, None, 38.0)))
        assert out[:2] == [  # its own line, wrapped under itself
            "14:20:00  Used a Fable reset on #1 personal · Fable reached 100% · 0 left on",
            "          this grant",
        ]
        assert out[2] == "14:20:00  #1 personal at 7d 52% (switch at 90%) · no switch needed"

    def test_the_unreadable_active_counts_down_to_failover(self):
        poll = _personal_poll("unavailable", error="http-503")
        log, out = _log()
        log(poll)
        log(NoSwitchEvent(reason="active-usage-unknown", detail="1/3 before failover", context=poll.context))
        assert out[0] == "14:20:00  #1 personal usage unavailable (HTTP 503) · failover in 2 more checks"
        assert out[1] == "            #2 night-shift  5h 93% · 7d 81% · over its switch line"


INDENT = " " * 12


class TestOnePrecision:
    def test_whole_percent_never_rounds_onto_the_line(self):
        assert pct_pair(86.0, 90.0) == ("86", "90")
        assert pct_pair(89.6, 90.0) == ("89", "90")  # not "90% (switch at 90%)"
        assert pct_pair(90.4, 90.0) == ("90", "90")  # at it: switch
        assert pct_text(99.6) == "99"                # never a false "100%"
        assert pct_text(100.0) == "100"

    def test_a_fractional_line_shows_one_decimal_on_both_sides(self):
        assert pct_pair(99.5, 99.9) == ("99.5", "99.9")
        assert pct_pair(99.86, 99.9) == ("99.8", "99.9")
        assert pct_pair(42.0, 99.9) == ("42.0", "99.9")

    def test_the_tick_line_at_threshold_99_9(self, temp_home):
        h = EngineHarness(temp_home, threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        log, out = _log()
        h.engine.on_event = lambda e: (h.events.append(e), log(e))
        h.tick_with_usage({"1": _u(99.86, 10.0), "2": _u(10.0, 10.0)})
        assert out[0] == "14:20:00  #1 a@example.com at 5h 99.8% (switch at 99.9%) · no switch needed"
        hold = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert hold.human() == "No switch needed: 5h at 99.8% (switch at 99.9%)"
        assert hold.to_json()["detail"] == "5h 99.86% < 99.9%"  # JSON unchanged

    def test_a_weekly_line_is_quoted_for_the_weekly_window(self):
        poll = PollEvent(active={"number": 1, "email": "a@example.com"}, headroom={"1": 7.0},
                         threshold=85.0, windows={"1": {"5h": 5.0, "7d": 93.0}}, weekly_threshold=97.0)
        log, out = _log()
        log(poll)
        log.flush()
        assert out[0] == "14:20:00  #1 a@example.com at 7d 93% (switch at 97%)"


# -- the stream ---------------------------------------------------------------------------


class TestTheStream:
    def _poll(self, pct: float = 50.0) -> PollEvent:
        return PollEvent(active={"number": 1, "email": "a@example.com"}, headroom={"1": 100 - pct},
                         threshold=90.0, windows={"1": {"5h": pct}})

    def test_flush_prints_a_poll_left_waiting(self):
        log, out = _log()
        log(self._poll())
        assert out == []
        log.flush()
        assert out == ["14:20:00  #1 a@example.com at 5h 50% (switch at 90%)"]
        log.flush()
        assert len(out) == 1

    def test_a_second_poll_flushes_the_first(self):
        log, out = _log()
        log(self._poll(40))
        log(self._poll(41))
        log.flush()
        assert [line[:40] for line in out] == [
            "14:20:00  #1 a@example.com at 5h 40% (sw",
            "14:20:00  #1 a@example.com at 5h 41% (sw",
        ]

    def test_an_error_ends_the_tick_with_its_status(self):
        log, out = _log()
        log(self._poll(95))
        log(ErrorEvent(message="could not freshen any candidate (network?)"))
        assert out[0] == "14:20:00  #1 a@example.com at 5h 95% (switch at 90%)"
        assert out[-1] == "14:20:00  Error: could not freshen any candidate (network?) (will retry)"

    def test_events_outside_a_tick_are_their_own_lines(self):
        log, out = _log()
        ctx = EventContext(names={"4": "side", "5": "ci"})
        log(SleepEvent(seconds=720, until="2026-11-10T14:32:00Z", ts="2026-11-10T14:20:00Z"))
        log(UnquarantineEvent(number="4", email="sam@example.org", context=ctx))
        log(BudgetWarningEvent(account={"number": 5, "email": "k@token.local"}, used=410.0,
                               limit=500.0, month="2026-11", context=ctx))
        assert out[0].startswith("14:20:00  Sleeping 12m, until ")
        assert out[1] == "14:20:00  #4 side is back in rotation (login replaced)"
        assert out[2] == "14:20:00  #5 ci has spent $410.00 of its $500.00 monthly budget (82%)"

    def test_the_stamp_is_the_events_own_time_by_default(self):
        out = Lines()
        log = AutoLog(out.append)
        log(ErrorEvent(message="boom", ts="2026-11-10T14:20:07Z"))
        from datetime import datetime, timezone

        local = datetime(2026, 11, 10, 14, 20, 7, tzinfo=timezone.utc).astimezone()
        assert out[0].startswith(local.strftime("%H:%M:%S") + "  Error: boom")

    def test_no_active_account(self):
        log, out = _log()
        log(PollEvent(active=None, headroom={}, threshold=90.0))
        log(NoSwitchEvent(reason="no-active-account", detail="log in and run 'qswap add' first"))
        assert out == [
            "14:20:00  No active account: log in to Claude Code (claude, then /login), then",
            "          run: qswap add",
        ]

    def test_colour_marks_meaning_and_the_words_carry_it_too(self):
        log, out = _log()
        with printer.force_color():
            log(self._poll(50))
            log(NoSwitchEvent(reason="below-threshold"))
            log(self._poll(95))
            log(SwitchEvent(trigger="proactive", from_ref={"number": 1, "email": "a@example.com"},
                            to_ref={"number": 2, "email": "b@example.com"}))
            log(QuarantineEvent(number="3", email="c@example.com", reason="invalid_grant"))
        dim, accent, yellow = printer._DIM, printer._pal("accent"), printer._pal("yellow")
        assert dim + "#1 a@example.com at 5h 50%" in out[0]           # routine: dim
        switch = next(line for line in out if "Switched" in line)
        assert accent + "Switched #1 a@example.com → #2 b@example.com" in switch
        relogin = next(line for line in out if "re-login" in line and "#3" in line)
        assert yellow in relogin and "needs re-login" in relogin


# -- every event's words ----------------------------------------------------------------


class TestEventWords:
    def test_switch_reasons(self):
        def why(trigger, **kw):
            return SwitchEvent(trigger=trigger, from_ref={"number": 1, "email": "a@x"},
                               to_ref={"number": 2, "email": "b@x"}, **kw).human()

        b = BindingWindow("7d", 97.4, 97.0, None, -0.4)
        assert why("proactive", binding=b).endswith("· 7d reached 97%")
        assert why("at-limit").endswith("· limit reached")
        assert why("at-limit", binding=BindingWindow("5h", 100.0, 90.0, None, -10)).endswith("· 5h limit reached")
        assert why("at-limit", over_budget=True).endswith("· monthly budget reached")
        assert why("failover").endswith("· usage unreadable")
        assert why("consume-first").endswith("· its weekly window resets sooner")
        assert why("priority", priority_up=True).endswith("· higher priority")
        assert why("priority", priority_up=False).endswith("· same priority, its weekly window resets sooner")
        for trigger in ("proactive", "at-limit", "failover"):
            assert trigger not in why(trigger)

    def test_all_exhausted_and_sleep(self):
        ev = AllExhaustedEvent(earliest_reset_at="2026-11-10T15:32:00Z", ts="2026-11-10T14:20:00Z")
        text = ev.human()
        assert text.startswith("All accounts are at their limit; the first resets ")
        assert text.endswith("· in 1h 12m")
        assert AllExhaustedEvent(earliest_reset_at=None).human() == (
            "All accounts are at their limit; no reset time is known yet"
        )

    def test_quarantine_and_release(self):
        ctx = EventContext(names={"4": "side"})
        assert QuarantineEvent(number="4", email="sam@example.org", reason="invalid_grant",
                               context=ctx).human() == (
            "#4 side needs re-login: its saved login was refused. "
            "Log in to it in Claude Code (/login), then run: qswap add"
        )
        assert QuarantineEvent(number="4", email="sam@example.org",
                               reason="identity-conflict").human().startswith(
            "#4 sam@example.org needs re-login: its saved login belongs to another account."
        )
        assert UnquarantineEvent(number="4", email="sam@example.org",
                                 reason="account-replaced").human() == (
            "#4 sam@example.org is back in rotation (account replaced)"
        )

    def test_resets(self):
        plan = {"reason": "on-limit", "window": "Fable", "pct": 100.0, "hoursLeft": 72.0,
                "grantKind": "Fable", "scope": "model"}
        ref = {"number": 1, "email": "maya@example.com"}
        ctx = EventContext(names={"1": "personal"})
        used = ResetEvent(account=ref, plan=plan, result={"result": "reset", "resetsLeft": 1}, context=ctx)
        assert used.human() == (
            "Used a Fable reset on #1 personal · Fable reached 100%, 3d before it resets "
            "on its own · 1 left on this grant"
        )
        dry = ResetEvent(account=ref, plan=plan, dry_run=True, context=ctx)
        assert dry.human().startswith("Would use a Fable reset on #1 personal · ")
        assert dry.human().endswith("(dry run)")
        refused = ResetEvent(account=ref, plan=plan, result={"result": "cooldown", "reason": "cooldown"})
        assert refused.human() == "Did not use a Fable reset on #1 maya@example.com: cooldown"

    @pytest.mark.parametrize("reason, detail, words", [
        ("unmanaged-active-account", "run 'qswap add' to include it in rotation",
         "No switch: the live Claude Code login is not managed yet. Add it with: qswap add"),
        ("active-api-key", "API-key accounts have no quota to watch",
         "No switch: the active account is an API key, which has no quota to watch"),
        ("active-usage-unknown", "1/3 before failover",
         "No switch yet: the active account's usage is unreadable; failover after 3 "
         "unreadable checks (1 so far)"),
        ("reset-unknown", "x",
         "No switch: the active account's weekly reset time is unknown; consume-first "
         "waits until it is reported"),
        ("switch-deferred", "Codex is renewing the live token",
         "Switch deferred: Codex is renewing the live token; trying again next check"),
    ])
    def test_no_switch_reasons_in_words(self, reason, detail, words):
        event = NoSwitchEvent(reason=reason, detail=detail)
        assert event.human() == words
        assert event.to_json()["reason"] == reason and event.to_json()["detail"] == detail

    def test_no_candidates_says_how_to_add_one(self):
        event = NoSwitchEvent(reason="no-candidates", context=EventContext(active="1"))
        assert event.human() == (
            "No account to switch to: no other account is in rotation. To add another "
            "account, log in to it in Claude Code (/login), then: qswap add"
        )
        disabled = NoSwitchEvent(reason="no-candidates", context=EventContext(
            active="1", accounts=(AccountView("1"), AccountView("4", alias="side", state="disabled")),
        ))
        assert disabled.human() == (
            "No account to switch to: #4 side is disabled. "
            "To put #4 side back in rotation, run: qswap enable 4"
        )


# -- the JSON of every event kind, pinned -------------------------------------------------


TS = "2026-11-10T14:20:00Z"
REF1 = {"number": 1, "email": "maya@example.com"}
REF2 = {"number": 2, "email": "maya@northwind.example.com"}
CTX = EventContext(provider="codex", names={"1": "personal", "2": "work"},
                   accounts=(AccountView("2", state="needs-relogin"),), active="1")
BIND = BindingWindow("5h", 91.0, 90.0, None, -1.0)

PINNED = [
    (PollEvent(active=REF1, headroom={"1": 9.0, "2": None}, threshold=90.0,
               fetch_errors={"2": "http-503"}, windows={"1": {"5h": 91.0}},
               weekly_threshold=97.0, ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "poll", "ts": TS, "active": REF1,
      "headroomPct": {"1": 9.0, "2": None}, "threshold": 90.0, "fetchErrors": {"2": "http-503"},
      "windowsPct": {"1": {"5h": 91.0}}, "weeklyThreshold": 97.0}),
    (SwitchEvent(trigger="proactive", from_ref=REF1, to_ref=REF2, ts=TS, context=CTX,
                 binding=BIND, over_budget=True, priority_up=True),
     {"schemaVersion": 1, "event": "switch", "ts": TS, "trigger": "proactive", "from": REF1,
      "to": REF2, "warnings": [], "dryRun": False}),
    (NoSwitchEvent(reason="no-comparison", detail="no candidate has readable usage", ts=TS,
                   context=CTX, binding=BIND, account="2", wait_s=30.0),
     {"schemaVersion": 1, "event": "no-switch", "ts": TS, "reason": "no-comparison",
      "detail": "no candidate has readable usage"}),
    (QuarantineEvent(number="2", email="b@x", reason="invalid_grant", recover="Log in.", ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "account-quarantined", "ts": TS, "number": "2", "email": "b@x",
      "reason": "invalid_grant"}),
    (UnquarantineEvent(number="2", email="b@x", ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "account-unquarantined", "ts": TS, "number": "2", "email": "b@x",
      "reason": "credentials-replaced"}),
    (AllExhaustedEvent(earliest_reset_at="2026-11-10T15:32:00Z", ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "all-exhausted", "ts": TS, "earliestResetAt": "2026-11-10T15:32:00Z"}),
    (SleepEvent(seconds=720.04, until="2026-11-10T14:32:00Z", ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "sleep", "ts": TS, "seconds": 720.0, "until": "2026-11-10T14:32:00Z"}),
    (ErrorEvent(message="boom", ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "error", "ts": TS, "message": "boom", "transient": True}),
    (ConfigWarningEvent(message="typo?", ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "config-warning", "ts": TS, "message": "typo?"}),
    (BudgetWarningEvent(account=REF2, used=410.004, limit=500.0, month="2026-11", source="ledger",
                        ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "budget-warning", "ts": TS, "account": REF2, "usedUsd": 410.0,
      "budgetUsd": 500.0, "pct": 82.0, "month": "2026-11", "source": "ledger"}),
    (ResetEvent(account=REF1, plan={"scope": "model", "reason": "on-limit"}, result={"result": "reset"},
                ts=TS, context=CTX),
     {"schemaVersion": 1, "event": "limit-reset", "ts": TS, "account": REF1,
      "plan": {"scope": "model", "reason": "on-limit"}, "scope": "model",
      "result": {"result": "reset"}, "dryRun": False}),
]


@pytest.mark.parametrize("event, payload", PINNED, ids=[type(e).__name__ for e, _ in PINNED])
def test_json_is_pinned_for_every_event_kind(event, payload):
    assert event.to_json() == payload
    json.dumps(event.to_json())  # still plain JSON
    # Stripped of everything human-only, the payload is the same.
    defaults = {
        f.name: (f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default)
        for f in dataclasses.fields(event)
        if not f.compare and f.name != "ts"
    }
    assert dataclasses.replace(event, **defaults).to_json() == payload
    assert "\n" not in event.human()


# -- the banner -----------------------------------------------------------------------------


class TestBanner:
    def test_default(self):
        assert banner(AutoSwitchSettings()) == (
            "Auto-switch running: switch at 90% · checks every 60s · Ctrl-C to stop"
        )

    def test_dry_run_codex_weekly_strategy_fit(self):
        settings = AutoSwitchSettings(threshold=99.9, weekly_threshold=97.5, strategy="consume-first")
        for provider in ("claude", "codex"):
            for dry in (False, True):
                text = banner(settings, dry_run=dry, provider=provider)
                assert len(text) <= 80, text
                assert "switch at 99.9% (weekly 97.5%)" in text
                assert ("dry run" in text) is dry
                assert text.startswith("Codex auto-switch" if provider == "codex" else "Auto-switch")
                assert "—" not in text and "threshold" not in text


def test_autolog_exports():
    assert set(autolog.__all__) == {"AutoLog", "banner", "pct_pair", "pct_text"}
