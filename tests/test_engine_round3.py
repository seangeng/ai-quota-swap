"""The auto-switch engine and real money, round 3 of PR #13 (decisions EM2,
EM3, EM4, EM6, EM8, EM9, EM10, EM11, DS3 and DS4 in the orchestrator's notes).

Everything runs on the suite's fake home and fake Keychain; resets are faked
and never claimed. The fleet is #1 a@example.com (the live login), #2 ci (an
API key), #3 c@example.com, as in test_engine_money.py.
"""

from __future__ import annotations

import json

import pytest

from ai_quota_swap import api_spend, claude_settings
from ai_quota_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import _usage
from tests.test_engine_money import (
    EXHAUSTED,
    _enter,
    _fleet,
    _holds,
    _notes,
    _reading,
    _settings_line,
    _switches,
    _tick,
)

QSWAP = "/opt/qswap/bin/qswap"


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)


@pytest.fixture(autouse=True)
def scans(monkeypatch):
    """The engine's ledger scan, counted (no real scan unless a test asks)."""
    calls: list = []
    from ai_quota_swap.ledger import usage as ledger_usage

    monkeypatch.setattr(ledger_usage, "scan_recent", lambda *a, **k: calls.append(k),
                        raising=False)
    return calls


def _state(h) -> dict:
    path = h.switcher.backup_dir / "autoswitch_state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_state(h, state: dict) -> None:
    path = h.switcher.backup_dir / "autoswitch_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")


# == EM8: one latch per kind ==================================================================


def _credit(left: float, *, amount: float = 1000.0, since: str = "2026-11-01T00:00:00Z",
            source: str = "admin-api") -> dict:
    return {"amount": amount, "used": amount - left, "left": left, "spent": left <= 0,
            "expired": False, "since": since, "expires": None, "source": source}


def _budget_over(h, **kw):
    return _reading(h, used=600.0, source="admin-api", **kw)


def _budget_lower(h, **kw):
    return _reading(h, used=300.0, source="ledger", **kw)


class TestOneLatchPerKind:
    def test_recording_a_credit_keeps_a_reached_budget(self, temp_home):
        """The re-check's case: the budget reached per Anthropic ($600 of
        $500) and latched; then qswap credit 2 1000. The reading back on the
        ledger's $300 of $500 must not let the key in: this month's budget
        was reached."""
        h = _fleet(temp_home)
        assert _tick(h, EXHAUSTED, {"2": _budget_over(h)}) is TickOutcome.BLOCKED
        api_spend.set_credit(h.switcher, "2", 1000, now=h.clock.now)
        h.clock.advance(60)
        lower = _budget_lower(h, credit=_credit(990.0, source="ledger"))
        for _ in range(3):
            assert _tick(h, EXHAUSTED, {"2": lower}) is TickOutcome.BLOCKED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert _settings_line(temp_home) is None
        assert _state(h)["limitLatch"]["2"]["budget"]["reason"] == "budget"

    def test_setting_a_budget_keeps_a_spent_credit(self, temp_home):
        h = _fleet(temp_home, limit="credit")
        spent = _reading(h, limit=None, source="admin-api", credit=_credit(-5.0, amount=100.0))
        assert _tick(h, EXHAUSTED, {"2": spent}) is TickOutcome.BLOCKED
        api_spend.set_budget(h.switcher, "2", 500)
        lower = _reading(h, used=10.0, credit=_credit(60.0, amount=100.0, source="ledger"))
        for _ in range(2):
            assert _tick(h, EXHAUSTED, {"2": lower}) is TickOutcome.BLOCKED
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "1"
        assert _state(h)["limitLatch"]["2"]["credit"]["reason"] == "credit-spent"

    def test_each_command_clears_its_own_kind_and_either_keeps_the_key_out(self, temp_home):
        h = _fleet(temp_home)
        api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now)
        both = _budget_over(h, credit=_credit(-5.0, amount=100.0))
        assert _tick(h, EXHAUSTED, {"2": both}) is TickOutcome.BLOCKED
        assert set(_state(h)["limitLatch"]["2"]) == {"budget", "credit"}
        lower = _budget_lower(h, credit=_credit(60.0, amount=100.0, source="ledger"))

        api_spend.set_budget(h.switcher, "2", 500)  # the budget set again
        assert set(_state(h)["limitLatch"]["2"]) == {"credit"}
        assert _tick(h, EXHAUSTED, {"2": lower}) is TickOutcome.BLOCKED

        # The credit recorded again is another credit: nothing holds the key now.
        api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now + 86400)
        assert "2" not in _state(h).get("limitLatch", {})
        renewed = _budget_lower(h, credit=_credit(
            60.0, amount=100.0, source="ledger",
            since=_iso(h.clock.now + 86400),
        ))
        assert _tick(h, EXHAUSTED, {"2": renewed}) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"

    def test_the_month_rollover_clears_the_budget_latch_only(self, temp_home):
        h = _fleet(temp_home)
        api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now)
        both = _budget_over(h, credit=_credit(-5.0, amount=100.0))
        _tick(h, EXHAUSTED, {"2": both})
        december = _budget_lower(h, month="2026-12",
                                 credit=_credit(60.0, amount=100.0, source="ledger"))
        assert _tick(h, EXHAUSTED, {"2": december}) is TickOutcome.BLOCKED
        assert set(_state(h)["limitLatch"]["2"]) == {"credit"}
        assert [e.limit_reason for e in _holds(h) if e.reason == "limit-latched"][-1] == (
            "credit-spent"
        )

    @pytest.mark.parametrize("reason,clears,keeps", [
        ("budget", "budget", "credit"),
        ("credit-spent", "credit", "budget"),
    ])
    def test_a_state_file_in_the_old_shape_is_read_as_its_reason(
        self, temp_home, reason, clears, keeps,
    ):
        """Written by the round-2 engine: ``limitLatch[N]`` is one latch,
        ``{"reason": ...}``. It holds the key out, the other kind's command
        leaves it, and its own command clears it."""
        h = _fleet(temp_home)
        api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now)
        record = h.switcher._get_sequence_data()["accounts"]["2"]
        since = record["credit"]["since"]
        old = (
            {"reason": "budget", "month": "2026-11", "limit": 500.0}
            if reason == "budget"
            else {"reason": "credit-spent", "credit": f"{int(_ts(since))}|100.0"}
        )
        _write_state(h, {"schemaVersion": 1, "limitLatch": {"2": old}})
        lower = _budget_lower(h, credit=_credit(60.0, amount=100.0, source="ledger",
                                                 since=since))
        assert _tick(h, EXHAUSTED, {"2": lower}) is TickOutcome.BLOCKED
        held = [e for e in _holds(h) if e.reason == "limit-latched"]
        assert held and held[-1].limit_reason == reason
        # The other kind's command leaves it as it was.
        if keeps == "credit":
            api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now)
        else:
            api_spend.set_budget(h.switcher, "2", 500)
        assert _tick(h, EXHAUSTED, {"2": _budget_lower(h, credit=_credit(
            60.0, amount=100.0, source="ledger",
            since=h.switcher._get_sequence_data()["accounts"]["2"]["credit"]["since"],
        ))}) is TickOutcome.BLOCKED
        # Its own command clears it.
        if clears == "budget":
            api_spend.set_budget(h.switcher, "2", 500)
        else:
            api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now)
        assert "2" not in _state(h).get("limitLatch", {})

    def test_the_reading_carries_either_latch(self, temp_home):
        """Every surface reads the latch from the reading (``latched``): a
        credit latch next to a budget set again is still on the row."""
        from datetime import datetime, timezone

        from ai_quota_swap.ledger.activations import parse_ts

        h = _fleet(temp_home, limit=None)
        api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now)
        since = h.switcher._get_sequence_data()["accounts"]["2"]["credit"]["since"]
        _write_state(h, {"schemaVersion": 1, "limitLatch": {"2": {
            "credit": {"reason": "credit-spent",
                       "credit": f"{int(parse_ts(since))}|100.0"},
        }}})
        now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc).timestamp()
        infos = {
            str(i[0]): i for i in h.switcher._build_accounts_info()
            if h.switcher._account_kind(str(i[0])) == "api_key"
        }
        reading = api_spend.readings(h.switcher, infos, fetch=set(), now=now, env={})["2"]
        assert api_spend.latch_of(reading) == "credit-spent"


def _ts(iso: str) -> float:
    from ai_quota_swap.ledger.activations import parse_ts

    return parse_ts(iso)


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    from ai_quota_swap.admin_api import iso_z

    return iso_z(datetime.fromtimestamp(ts, tz=timezone.utc))


# Re-exported for the classes below.
__all__ = ["NoSwitchEvent", "SwitchEvent", "_notes", "_usage", "_enter", "_switches"]


# == EM11: includeApiKeyAccounts turned off on a key the engine entered =======================


def _keys_off(h) -> None:
    from dataclasses import replace

    h.settings = replace(h.settings, include_api_key_accounts=False)
    h.engine.settings = h.settings


class TestKeysTurnedOff:
    def test_the_engine_leaves_its_key_for_the_account_underneath(self, temp_home):
        """The re-check's case: entered at the limit, then the setting is
        turned off while both subscriptions stay at 100%. It used to stay
        on the key for as long as no subscription had room."""
        h = _fleet(temp_home, cooldown_seconds=3600)
        _enter(h)
        _keys_off(h)
        h.clock.advance(60)  # inside the cooldown: leaving now ignores it
        assert _tick(h, EXHAUSTED) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert h.switcher.helper_account() is None
        assert _settings_line(temp_home) is None
        last = _switches(h)[-1]
        assert last.limit_reason == "keys-off"
        assert last.human() == (
            "Switched #2 ci → #1 a@example.com · API keys are turned off "
            "(autoswitch.includeApiKeyAccounts); no subscription has room, so back "
            "to the account underneath"
        )
        # Said once: the next checks stay on #1 and say nothing more of it.
        for _ in range(3):
            h.clock.advance(60)
            assert _tick(h, EXHAUSTED) is not TickOutcome.SWITCHED
        assert len([s for s in _switches(h) if s.limit_reason == "keys-off"]) == 1

    def test_with_room_on_a_subscription_it_goes_there_at_once(self, temp_home):
        h = _fleet(temp_home, cooldown_seconds=3600)
        _enter(h)
        _keys_off(h)
        h.clock.advance(60)
        assert _tick(h, {"1": _usage(100), "2": "api key", "3": _usage(40)}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "3"
        assert _settings_line(temp_home) is None

    def test_a_key_the_user_entered_is_not_touched(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.switch_to("2", json_output=True)
        _keys_off(h)
        for _ in range(3):
            assert _tick(h, EXHAUSTED) is TickOutcome.NO_ACTION
            h.clock.advance(60)
        assert h.switcher.current_account_number() == "2"
        assert _settings_line(temp_home) is not None

    def test_with_no_login_underneath_it_says_why_it_stays(self):
        event = NoSwitchEvent(reason="stuck-on-key", account="2", limit_reason="keys-off",
                              way_off="3")
        assert event.human() == (
            "Staying on #2 though API keys are turned off: no login is under the key to "
            "go back to. Leave it with: qswap switch 3"
        )


# == DS3 and DS4: what counts as an engine watching a key it entered ===========================


def _beat(h, *, mode: str, dry_run: bool = False, ago: float = 0.0,
          host: str = "qswap auto") -> None:
    import time

    from ai_quota_swap.autoswitch import write_heartbeat

    at = time.time() - ago
    write_heartbeat(h.switcher.backup_dir, mode=mode, dry_run=dry_run, last_tick_at=at,
                    next_tick_at=at + 60 if mode == "loop" else None, started_at=at,
                    host=host)


def _key_notes(h) -> str:
    return " ".join(" ".join(h.switcher.key_helper_notes()).split())


def _doctor_watch(h):
    from ai_quota_swap import doctor

    return {c.id: c for c in doctor.run_checks()}.get("claude.key_watch")


class TestWhatWatches:
    @pytest.mark.parametrize("mode,dry,ago,watching,by,dry_only,checked", [
        ("loop", False, 0, True, "loop", False, True),
        ("loop", True, 0, False, None, True, False),
        ("loop", False, 600, False, None, False, True),  # overdue: it stopped
        ("once", False, 4 * 60, True, "once", False, True),
        ("once", False, 15 * 60, True, "once", False, True),
        ("once", False, 16 * 60, False, None, False, True),
        ("once", True, 60, False, None, False, False),  # a dry --once checks nothing
    ])
    def test_the_rule(self, mode, dry, ago, watching, by, dry_only, checked):
        from ai_quota_swap.autoswitch import key_watch

        now = 1_800_000_000.0
        beat = {"mode": mode, "dryRun": dry, "lastTickAt": now - ago,
                "nextTickAt": now - ago + 60 if mode == "loop" else None, "host": "x"}
        watch = key_watch(beat, now)
        assert bool(watch) is watching
        assert watch.mode == by
        assert watch.dry_run_only is dry_only
        assert (watch.last_checked_at == now - ago) if checked else watch.last_checked_at is None
        assert not key_watch(None, now)

    def test_a_dry_run_engine_does_not_watch(self, temp_home):
        """The re-check's case: the dashboard's auto view starts every engine
        in dry run; its heartbeat used to silence the one line that says the
        budget is no longer enforced."""
        h = _fleet(temp_home)
        _enter(h)
        _beat(h, mode="loop", dry_run=True, host="qswap dashboard")
        assert (
            "Auto-switch entered #2 ci and only a dry run is running, so nothing "
            "watches its limit. To go back: qswap switch 1"
        ) in _key_notes(h)
        check = _doctor_watch(h)
        assert check is not None and check.status == "warn"
        assert check.summary == (
            "auto-switch entered #2 ci and only a dry run is running, so nothing "
            "watches its limit and sessions stay on the key"
        )
        from ai_quota_swap.autoswitch import unwatched_key, unwatched_key_notice

        assert unwatched_key(h.switcher)["engineRunning"] is False
        assert unwatched_key(h.switcher)["dryRunOnly"] is True
        assert unwatched_key_notice(h.switcher, host="qswap auto") == (
            "Sessions stay on #2 ci, which auto-switch entered; only a dry run is "
            "running, so nothing watches its limit now. To go back: qswap switch 1"
        )

    def test_a_recent_once_tick_watches_and_says_when(self, temp_home):
        """The documented cron setup: qswap auto --once every few minutes."""
        h = _fleet(temp_home)
        _enter(h)
        _beat(h, mode="once", ago=4 * 60 + 5)
        notes = _key_notes(h)
        assert "Auto-switch entered #2 ci and last checked it 4m ago (qswap auto --once)." in notes
        assert "nothing watches" not in notes
        assert _doctor_watch(h) is None
        from ai_quota_swap.autoswitch import unwatched_key, unwatched_key_notice

        key = unwatched_key(h.switcher)
        assert key["engineRunning"] is True and key["watchedBy"] == "once"
        # A loop stopping beside the cron (same host label) leaves it watched.
        assert unwatched_key_notice(h.switcher, host="qswap auto") is None

    def test_an_old_once_tick_is_nothing_watching_and_says_when(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        _beat(h, mode="once", ago=20 * 60 + 5)
        assert (
            "Auto-switch entered #2 ci and is not running, so nothing watches its "
            "limit; it last checked 20m ago. To go back: qswap switch 1"
        ) in _key_notes(h)
        check = _doctor_watch(h)
        assert check is not None and check.status == "warn"
        assert check.summary == (
            "auto-switch entered #2 ci and is not running, so nothing watches its "
            "limit and sessions stay on the key; it last checked 20m ago"
        )
        assert check.fix == "qswap switch 1"

    def test_a_running_loop_still_says_nothing(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        _beat(h, mode="loop")
        assert "Auto-switch entered" not in _key_notes(h)
        assert _doctor_watch(h) is None

    def test_the_notes_fit_80_columns(self, temp_home):
        h = _fleet(temp_home)
        _enter(h)
        for mode, dry, ago in (("loop", True, 0), ("once", False, 4 * 60), ("once", False, 3 * 3600)):
            _beat(h, mode=mode, dry_run=dry, ago=ago)
            for line in h.switcher.key_helper_notes():
                assert len(line) <= 80, line


# == EM10: a session stamped with the login underneath a key stays on the key =================


class TestTheLoginUnderneathIsNoRunProfile:
    def _world(self):
        from tests.test_ledger import Env, _registry

        env = Env()
        env.registry = _registry((1, "a@example.com", "uuid-a"), (2, "b@example.com", "uuid-b"))
        env.registry["accounts"]["5"] = {"email": "key5@token.local", "organizationUuid": "",
                                         "kind": "api_key"}
        return env

    def test_the_session_opened_as_the_subscription_ran_out(self):
        """The re-check's case: a session's file starts with the owner line
        of #1, the login under the key, then a prompt (nothing counted) just
        before the engine enters key #5; its requests go out on the key. A
        real run-profile session of #2 beside it keeps its stamp."""
        from ai_quota_swap.ledger import spend as ledger_spend
        from ai_quota_swap.ledger.activations import record_activation
        from tests.ledger_fixtures import claude_line, owner_line, user_line, write_lines
        from tests.test_ledger import T0

        env = self._world()
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0, how="login")
        record_activation(env.data, number=5, email="key5@token.local", org="", now=T0 + 600,
                          how="helper")
        write_lines(env.transcript("sess-edge.jsonl"), [
            owner_line("sess-edge", "uuid-a"),
            user_line(T0 + 590, session="sess-edge"),
            claude_line(mid="e1", ts=T0 + 1200, session="sess-edge", inp=1_000_000, out=0,
                        cr=0, w5=0),
        ])
        write_lines(env.transcript("sess-run.jsonl"), [
            owner_line("sess-run", "uuid-b"),
            claude_line(mid="r1", ts=T0 + 1300, session="sess-run", inp=1_000_000, out=0,
                        cr=0, w5=0),
        ])
        env.scan()
        spend = ledger_spend.account_spend(
            env.data, "key5@token.local", "", month_start=T0 - 3600, day_start=T0,
            registry=env.registry, since_start=T0,
        )
        assert spend.since == pytest.approx(4.0)
        rows = {r.label: r.totals.requests for r in env.report("account").rows}
        assert rows == {"#5 key5": 1, "#2 b@example.com": 1}

    def test_a_key_to_key_switch_keeps_the_same_login_underneath(self):
        from ai_quota_swap.ledger.activations import Activation, ActivationLog

        t = 1_794_225_600.0
        log = ActivationLog([
            Activation(ts=t, account="a@example.com|", number=1, source="switch", how="login"),
            Activation(ts=t + 600, account="key5@token.local|", number=5, source="switch",
                       how="helper"),
            Activation(ts=t + 900, account="key6@token.local|", number=6, source="switch",
                       how="helper"),
            Activation(ts=t + 950, account="key6@token.local|", number=6, source="observed"),
            Activation(ts=t + 3600, account="b@example.com|", number=2, source="switch",
                       how="login"),
        ])
        assert log.helper_underneath(t + 700) == "a@example.com|"
        assert log.helper_underneath(t + 1000) == "a@example.com|"
        assert log.helper_underneath(t + 300) is None  # a login, no key
        assert log.helper_underneath(t + 4000) is None
        assert log.helper_underneath(t - 10) is None
