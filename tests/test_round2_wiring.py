"""Round 2 of pull request #13: the pieces the switching side (decisions B1
to B9) and the engine side (A1 to A16) left for each other, wired together
and tested across the seam (pr13-round2-decisions.md).

Everything runs on the suite's fake home, fake Keychain and faked network.
The engine fleet is tests/test_key_helper_live.py's: #1 a@example.com (the
live login), #2 ci (an API key with a budget), #3 c@example.com.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_quota_swap import api_spend, claude_settings, menubar
from ai_quota_swap.autoswitch import ErrorEvent, NoSwitchEvent, TickOutcome
from ai_quota_swap.switcher import USAGE_API_KEY
from ai_quota_swap.usage_store import UsageEntry
from tests.test_autoswitch import _usage
from tests.test_key_helper_live import (
    POSIX_ONLY,
    pin_key_helper_platform,
    _REAL_CHECK_COMMAND,
    _REAL_COMMAND_MISSING,
    _REAL_RUN_CHECK,
    EXHAUSTED,
    QSWAP,
    _fits,
    _fleet,
    _harness,
    _profile,
    _settings,
)


#: How the messages name Claude Code's user settings: "~/.claude/settings.json",
#: with this platform's separator.
WHERE = str(Path("~") / ".claude" / "settings.json")


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    """The installed command every line names, taken to exist and answer."""
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)
    monkeypatch.setattr(claude_settings, "check_command", lambda command, **kw: None)
    pin_key_helper_platform(monkeypatch)


def _line(home: Path) -> str | None:
    path = _settings(home)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("apiKeyHelper")
    except ValueError:
        return None


def _holds(h) -> list[NoSwitchEvent]:
    return [e for e in h.events if isinstance(e, NoSwitchEvent)]


def _notified(h) -> list[str]:
    from ai_quota_swap.menubar_notify import Notifier

    notifier = Notifier()
    out = []
    for event in h.events:
        note = notifier.event("claude", event, both=False, now=0.0)
        if note is not None:
            out.append(note.body)
    return out


def _engine_on_its_key(temp_home):
    """The engine entered #2 ci at the limit."""
    h = _harness(temp_home)
    assert h.tick_with_usage(EXHAUSTED) is TickOutcome.SWITCHED
    assert h.switcher.key_entered_by() == "engine"
    return h


# == B6 and B9: the engine and a line the settings would defeat =========================


class TestTheEngineAndADefeatedLine:
    """B6: the engine treats the env-block refusal like key-helper-unavailable;
    B9: its words call the line the key line and end in the next step."""

    def test_it_holds_and_says_so_in_the_key_lines_words(self, temp_home):
        from ai_quota_swap import autolog

        h = _harness(temp_home)
        _settings(temp_home).write_text(
            json.dumps({"env": {"ANTHROPIC_API_KEY": "do-not-print"}}), encoding="utf-8"
        )
        before = _settings(temp_home).read_bytes()
        for _ in range(3):
            assert h.tick_with_usage(EXHAUSTED) is TickOutcome.BLOCKED
            h.clock.advance(60)
        held = _holds(h)[-1]
        assert held.reason == "key-helper-unavailable"
        text = held.human()
        assert "key line" in text and "key helper" not in text
        assert text.endswith("qswap doctor")
        fragment, _tone = autolog.no_switch_fragment(held)
        assert "key line" in fragment and "helper" not in fragment
        assert "key helper" not in held.detail
        assert _settings(temp_home).read_bytes() == before
        assert h.switcher.current_account_number() == "1"
        said = [n for n in _notified(h) if "key line" in n]
        assert len(said) == 1

    def test_a_refusal_at_the_switch_itself_is_worded_the_same(self, temp_home, monkeypatch):
        """The line became defeated between the decision and the switch."""
        from ai_quota_swap.exceptions import KeyHelperUnavailableError

        h = _harness(temp_home)
        monkeypatch.setattr(h.switcher, "key_helper_available", lambda: True)

        def refuse(*a, **k):
            raise KeyHelperUnavailableError(
                "not switched", number="2",
                reason="~/.claude/settings.json sets ANTHROPIC_API_KEY in its env block",
                log_out_sessions=False,
            )

        monkeypatch.setattr(h.switcher, "switch_to", refuse)
        assert h.tick_with_usage(EXHAUSTED) is TickOutcome.BLOCKED
        held = _holds(h)[-1]
        assert held.reason == "key-helper-unavailable"
        assert "key helper" not in held.detail and "key line" in held.detail


# == B5: the engine's tick and the one reconcile ========================================


class TestTheEngineTickReadsTheReconcile:
    def test_unreadable_settings_with_a_record_hold_the_engine(self, temp_home):
        """B5 (F52): the file cannot be parsed while key_helper.json names the
        engine's key. The engine holds with the sentence every surface says;
        it neither errors every tick nor changes the file."""
        h = _engine_on_its_key(temp_home)
        _settings(temp_home).write_text(
            f'{{"apiKeyHelper": "{QSWAP} key-helper 2",}}\n', encoding="utf-8"
        )
        before = _settings(temp_home).read_bytes()
        h.events.clear()
        for usage in ({"1": _usage(100), "2": "api key", "3": _usage(20)}, EXHAUSTED):
            h.clock.advance(600)
            assert h.tick_with_usage(usage) is TickOutcome.BLOCKED
        assert not [e for e in h.events if isinstance(e, ErrorEvent)]
        held = _holds(h)[-1]
        assert held.reason == "settings-unreadable"
        text = " ".join(held.human().split())
        assert "qswap last made #2 ci the active API key" in text
        assert f"Fix the JSON in {WHERE}" in text
        assert _settings(temp_home).read_bytes() == before
        said = [n for n in _notified(h) if "qswap last made" in n]
        assert len(said) == 1

    def test_unreadable_settings_with_no_record_do_not_hold_a_login_switch(self, temp_home):
        """With no key of qswap's on record, a login switch touches nothing
        of the settings: the engine goes on as before."""
        h = _harness(temp_home)
        _settings(temp_home).write_text("{not json", encoding="utf-8")
        assert h.tick_with_usage(
            {"1": _usage(100), "2": "api key", "3": _usage(20)}
        ) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"

    def test_a_tick_gives_a_linked_run_profile_its_copy(self, temp_home):
        """B5 (F9): a run profile linked while the engine's line is in (a
        launch in between) is repaired by the next tick, not only by the
        next switch."""
        h = _engine_on_its_key(temp_home)
        d = _profile(h.switcher, "3", "c@example.com")
        assert (d / "settings.json").is_symlink()
        assert len(h.switcher.key_findings().linked) == 1
        h.clock.advance(60)
        h.tick_with_usage(EXHAUSTED)
        assert not (d / "settings.json").is_symlink()
        assert "apiKeyHelper" not in (d / "settings.json").read_text(encoding="utf-8")
        assert h.switcher.key_findings().linked == ()

    def test_a_dry_run_repairs_nothing(self, temp_home):
        h = _engine_on_its_key(temp_home)
        d = _profile(h.switcher, "3", "c@example.com")
        h.engine = h._make_engine(dry_run=True)
        h.clock.advance(60)
        h.tick_with_usage(EXHAUSTED)
        assert (d / "settings.json").is_symlink()

    def test_a_record_and_a_line_written_meanwhile_are_not_taken_for_lost(
        self, temp_home, monkeypatch
    ):
        """The repair re-reads under the lock: a switch that wrote its record
        and then its line between the tick's look and its repair is not
        turned into a tombstone (which would mislabel the line as come back)."""
        s = _fleet()
        real = s.key_findings
        calls = {"n": 0}

        def stale_then_real():
            calls["n"] += 1
            if calls["n"] == 1:
                # The first look: the record is written, the line not yet.
                s._write_key_state({"number": "5", "enteredBy": "user"})
                f = real()
                claude_settings.install("5")
                return f
            return real()

        monkeypatch.setattr(s, "key_findings", stale_then_real)
        s.reconcile_key_helper()
        assert s._key_state().get("number") == "5"
        monkeypatch.setattr(s, "key_findings", real)
        assert s.key_findings().came_back is False


# == F26 and B7 on the engine's way back underneath =====================================


class TestTheEngineBackUnderneath:
    def test_at_its_limit_with_the_login_gone_from_the_store_it_is_put_back(
        self, temp_home
    ):
        """~/.claude.json still names #1, but the login store holds only MCP
        sign-ins: going back by the line alone would leave every session
        logged out. The engine's way back restores #1's saved login."""
        h = _engine_on_its_key(temp_home)
        store = temp_home / ".claude" / ".credentials.json"
        store.write_text(json.dumps({"mcpOAuth": {"srv": {"accessToken": "m"}}}))
        h.key_readings["2"] = (600.0, 500.0)  # at its budget
        h.clock.advance(600)
        assert h.tick_with_usage(EXHAUSTED) is TickOutcome.SWITCHED
        assert _line(temp_home) is None
        live = json.loads(store.read_text())
        assert "claudeAiOauth" in live and "mcpOAuth" in live


# == A4 on the surfaces: the same severity as the engine's spend-unknown ================


def _stale_budget(age: float = 25 * 60) -> dict:
    return {
        "spend": {"used": 14.0, "limit": 20.0, "pct": 70.0, "currency": "USD"},
        "today": 1.0, "source": "ledger", "as_of": time.time() - age,
        "unpriced_tokens": 0, "month": "2026-11",
    }


class TestALimitNotEnforcedNeedsTheUser:
    """A4: with no fresh figure the engine treats the key as at its limit
    (it leaves it, whoever entered it); the rows say "limit not enforced"
    and mark it as needing the user, as they do a key at its budget."""

    def _acc(self, reading):
        return menubar.AccountState(
            provider="claude", number="5", email="ci@example.com", alias="ci", kind="api_key",
            entry=UsageEntry(sentinel=USAGE_API_KEY, api_spend=reading),
        )

    def test_the_menu_row_needs_attention(self):
        row = menubar.account_row(self._acc(_stale_budget()), now=time.time(), stale_after_s=600)
        assert "limit not enforced" in row.text
        assert row.attention

    def test_a_fresh_figure_under_the_budget_does_not(self):
        row = menubar.account_row(self._acc(_stale_budget(age=60)), now=time.time(),
                                  stale_after_s=600)
        assert "limit not enforced" not in row.text
        assert not row.attention

    def test_the_dashboard_row_warns_too(self):
        from ai_quota_swap.tui import data as tui_data

        assert tui_data.key_needs_attention(_key_snapshot(5, _stale_budget()))
        assert not tui_data.key_needs_attention(_key_snapshot(5, _stale_budget(age=60)))
        over = {**_stale_budget(age=60), "spend": {
            "used": 25.0, "limit": 20.0, "pct": 125.0, "currency": "USD"}}
        assert tui_data.key_needs_attention(_key_snapshot(5, over))


def _key_snapshot(number: int, reading: dict | None, **kw):
    from ai_quota_swap.models import AccountSnapshot

    return AccountSnapshot(
        number=str(number), email=f"key{number}@token.local", org_name="", org_uuid="",
        is_active=False, kind="api_key", switchable=True,
        usage=UsageEntry(sentinel=USAGE_API_KEY, api_spend=reading), **kw,
    )


def _sub_snapshot(number: int, pct: float, *, active: bool = False):
    from ai_quota_swap.models import AccountSnapshot
    from tests.test_autoswitch import _entry_for

    return AccountSnapshot(
        number=str(number), email=f"user{number}@example.com", org_name="", org_uuid="",
        is_active=active, kind="oauth", switchable=True,
        usage=_entry_for(_usage(pct), time.time()),
    )


class TestNextBestSaysWhyTheEngineWouldNotUseAKey:
    """ux.md: if the engine would skip an account, every view shows why. A2
    and A4: the engine enters only a key with a budget or credit and a fresh
    figure under it."""

    def test_each_key_says_whether_and_why_not(self):
        from ai_quota_swap.autoswitch import AutoSwitchSettings
        from ai_quota_swap.models import AccountsSnapshot
        from ai_quota_swap.tui import autoview
        from ai_quota_swap.tui import data as tui_data

        accounts = (
            _sub_snapshot(1, 100.0, active=True),
            _key_snapshot(5, None),
            _key_snapshot(6, _stale_budget()),
            _key_snapshot(8, _stale_budget(age=60)),
        )
        view = tui_data.ProviderView(provider="claude", snapshot=AccountsSnapshot(
            active_number="1", accounts=accounts, taken_at=time.time(),
        ))
        picked = autoview.next_best(view, AutoSwitchSettings(include_api_key_accounts=True))
        lines = {c.number: c.words for c in picked}
        assert lines["5"] == "API key · no budget or credit, not used"
        assert lines["6"] == f"API key · {api_spend.LIMIT_NOT_ENFORCED}"
        assert lines["8"] == "API key · only as a last resort"
        # The keys auto-switch would use come before those it would not.
        order = [c.number for c in picked]
        assert order.index("8") < order.index("5") and order.index("8") < order.index("6")


# == B8 with A16: one way back, the same everywhere =====================================


class TestTheWayBackFromAnUnwatchedKey:
    def _stop(self, h):
        """The engine stops: no heartbeat is live."""
        return h.switcher

    def test_over_a_managed_login_status_and_the_stopping_engine_agree(self, temp_home):
        from ai_quota_swap.autoswitch import unwatched_key_notice

        h = _engine_on_its_key(temp_home)
        s = self._stop(h)
        notes = " ".join(s.key_helper_notes())
        notice = unwatched_key_notice(s)
        assert "To go back: qswap switch 1" in notes
        assert notice.endswith("To go back: qswap switch 1")

    def test_over_a_login_qswap_does_not_manage_it_names_adding_it(self, temp_home):
        """Removing the line puts sessions on that login; a switch to a
        subscription would overwrite it (A11). The step is adding it."""
        from ai_quota_swap.autoswitch import unwatched_key, unwatched_key_notice

        h = _engine_on_its_key(temp_home)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "new@example.com", "accountUuid": "uuid-new"},
        }))
        s = self._stop(h)
        notes = " ".join(" ".join(s.key_helper_notes()).split())
        assert "qswap switch N" not in notes
        assert "qswap add" in notes
        assert unwatched_key(s)["back"] == "qswap add"
        assert unwatched_key_notice(s).endswith("qswap add")
        _fits("\n".join(s.key_helper_notes()))

    def test_doctor_gives_the_same_step(self, temp_home):
        from ai_quota_swap import doctor

        h = _engine_on_its_key(temp_home)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "new@example.com", "accountUuid": "uuid-new"},
        }))
        self._stop(h)
        checks = {c.id: c for c in doctor.run_checks()}
        assert checks["claude.key_watch"].fix == "qswap add"


# == B2: qswap switch N to the active key, with the line really run =====================


@POSIX_ONLY  # the stand-in qswap is a shell script
class TestSwitchToTheActiveKeyRunsTheLine:
    """B2 across the seam: the check runs the line through the shell (the
    real runner, a stand-in program), and the repair writes this install's
    line, which is run again."""

    @pytest.fixture(autouse=True)
    def _real(self, monkeypatch):
        monkeypatch.setattr(claude_settings, "command_missing", _REAL_COMMAND_MISSING)
        monkeypatch.setattr(claude_settings, "check_command", _REAL_CHECK_COMMAND)
        monkeypatch.setattr(claude_settings, "_run_check", _REAL_RUN_CHECK)

    def _program(self, where: Path, body: str) -> Path:
        exe = where / "bin" / "qswap"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        exe.chmod(0o755)
        return exe

    def test_a_line_that_fails_is_written_for_this_install(self, temp_home, tmp_path, monkeypatch):
        old = self._program(tmp_path / "old", 'echo "qswap key-helper: broken" >&2\nexit 1\n')
        new = self._program(tmp_path / "new", 'test "$3" = --check && exit 0\nexit 9\n')
        monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [str(new)])
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{old} key-helper 5"}), encoding="utf-8"
        )
        payload = s.switch_to("5", json_output=True)
        assert payload["switched"] is False and payload["reason"] == "already-active"
        assert any("Wrote the key line for #5 ci again: it fails" in w for w in payload["warnings"])
        assert _line(temp_home) == f"{new} key-helper 5"
        assert claude_settings.line_answers(s.key_helper_state()) is None


# == E2: what a line that cannot give a key does to sessions, the same everywhere =====


E2_RUNNING = "running ones keep the key they have until the settings change"


class TestABrokenLineSaysWhatSessionsDo:
    """E2 (Claude Code 2.1.322): a session that holds a key keeps sending it
    while the helper fails, until a settings change or a rejected request;
    only new sessions get none. The notes in list, status, the dashboard and
    the menu bar say so as doctor does."""

    def test_a_line_naming_an_account_that_does_not_exist(self, temp_home):
        from ai_quota_swap import doctor

        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 9"}), encoding="utf-8"
        )
        notes = " ".join(" ".join(s.key_helper_notes()).split())
        assert "new Claude Code sessions fail to get a key" in notes
        assert E2_RUNNING in notes
        assert "Remove the line: qswap switch 1" in notes
        _fits("\n".join(s.key_helper_notes()))
        check = {c.id: c for c in doctor.run_checks()}["claude.key_helper"]
        assert E2_RUNNING in check.summary

    def test_a_line_that_runs_a_qswap_no_longer_there(self, temp_home, monkeypatch):
        from ai_quota_swap import doctor

        s = _fleet()
        from tests.test_key_helper_live import _activate

        _activate(s)
        monkeypatch.setattr(claude_settings, "command_missing", lambda command: True)
        notes = " ".join(" ".join(s.key_helper_notes()).split())
        assert "new Claude Code sessions fail to get the key" in notes
        assert E2_RUNNING in notes
        assert "Point it at this qswap: qswap switch 5" in notes
        _fits("\n".join(s.key_helper_notes()))
        check = {c.id: c for c in doctor.run_checks()}["claude.key_helper"]
        assert "new Claude Code sessions fail to get the key" in check.summary
        assert E2_RUNNING in check.summary


# == The active key's menu row: B8's "by auto-switch" beside A's figure ===============


def _key_row(reading, *, by_engine: bool, over: str = "#1 personal", alias: str = "ci"):
    acc = menubar.AccountState(
        provider="claude", number="2", email="ci@example.com", alias=alias, kind="api_key",
        active=True, entry=UsageEntry(sentinel=USAGE_API_KEY, api_spend=reading),
        over=over, by_engine=by_engine,
    )
    return menubar.account_row(acc, now=time.time(), stale_after_s=600)


def _budget(used: float, limit: float, *, age: float = 60) -> dict:
    return {
        "spend": {"used": used, "limit": limit, "pct": used / limit * 100, "currency": "USD"},
        "today": 4.12, "source": "ledger", "as_of": time.time() - age,
        "unpriced_tokens": 0, "month": "2026-11",
    }


class TestTheActiveKeysMenuRow:
    """The key's figure and its reason are never what the row limit cuts:
    "over #1 personal" gives way first (the #1 row says "underneath"), then
    today's figure, then "by auto-switch" (when no engine runs, the note row
    under the accounts says it in full)."""

    @pytest.mark.parametrize("reading, by_engine, whole, gone", [
        (_budget(212.40, 500.0), True, "$212.40/$500 (42%) est.", ("over #1",)),
        (_budget(520.0, 500.0), True, "$520.00/$500 (104%, over budget) est.", ("over #1",)),
        (_budget(212.40, 500.0), False, "$4.12 today · $212.40/$500 (42%) est.", ("over #1",)),
        (_budget(14.0, 20.0, age=1500), True, "$14.00/$20 (70%) est. · limit not enforced",
         ("over #1", "by auto-switch")),
    ])
    def test_the_figure_and_its_reason_stay_whole(self, reading, by_engine, whole, gone):
        row = _key_row(reading, by_engine=by_engine)
        assert len(row.text) <= menubar.ROW_LIMIT
        assert row.text.endswith(whole), row.text
        assert "…" not in row.text
        for fact in gone:
            assert fact not in row.text
        if by_engine and "by auto-switch" not in gone:
            assert "· by auto-switch ·" in row.text
        # The tooltip keeps everything.
        assert "over #1 personal" in row.tooltip or "#1 personal" in row.tooltip

    def test_a_row_that_fits_keeps_every_fact(self):
        row = _key_row(_budget(2.0, 50.0), by_engine=False, over="#1 me")
        assert "over #1 me" in row.text and "today" in row.text
