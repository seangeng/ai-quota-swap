"""Round 3 of PR #13, what the two tracks left for each other: the reset auto
mode keeps, named on every surface while a key the engine entered is active
(EM4); ``keyNotes`` in ``list --json`` and ``status --json`` (DS13); and what
watches the key, said the same on every surface (DS3, DS4).

Everything runs on the suite's fake home and fake Keychain; resets are faked
and never claimed. The engine's fleet is #1 a@example.com (the live login),
#2 ci (an API key with a $500 budget), #3 c@example.com, as in
tests/test_engine_money.py.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from ai_quota_swap import claude_settings, key_activation
from tests.test_autoswitch import _entry_for
from tests.test_engine_money import (
    EXHAUSTED,
    _enter,
    _fleet,
    _key,
    _reading,
    _tick_resets,
)
from tests.test_resets import _grant, _usage as _rusage

QSWAP = "/opt/qswap/bin/qswap"


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)
    monkeypatch.setattr(claude_settings, "check_command", lambda command, **kw: None)


def _week(**kw):
    return _grant("g-week", clears=("seven_day",), **kw)


def _ten_hours_left() -> dict:
    """#1 and #3 at 100% on 7d with 10 hours to go; #1 holds a weekly reset,
    which the defaults (minHoursLeft 24) keep."""
    return {"1": _rusage(d7=100, reset_in=10 * 3600, grants=[_week()]), "2": "api key",
            "3": _rusage(d7=100, reset_in=10 * 3600)}


KEPT = (
    "#1 a@example.com holds 1 weekly reset auto mode keeps (its 7d window "
    "refills in 10h); to spend it instead: qswap reset 1"
)


def _stored(h, monkeypatch, usage: dict, readings: dict | None = None) -> None:
    """What every surface reads back from the usage store: ``usage`` (an API
    key's entry carries ``readings[N]``, a fresh figure under its budget by
    default)."""

    def collect(infos, fetch=None, scheduled=False):
        out = {}
        for info in infos:
            num = str(info[0])
            value = usage.get(num)
            out[num] = (
                _key((readings or {}).get(num, _reading(h))) if value == "api key"
                else _entry_for(value, h.clock.now)
            )
        return out

    monkeypatch.setattr(h.switcher, "_collect_usage_entries", collect)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _surfaces(s, capsys) -> dict[str, str]:
    """The text of list, status, the dashboard (the snapshot's key notes), the
    menu bar's provider state and its key row (the row's tooltip)."""
    from ai_quota_swap import menubar, menubar_data

    class Source:
        def take(self, *, full, store_only):
            return s.accounts_snapshot(fetch=set())

    capsys.readouterr()
    s.list_accounts()
    out, err = capsys.readouterr()
    snap = s.accounts_snapshot(fetch=set())
    state = menubar_data.claude_state(s, Source(), full=False, store_only=True, now=time.time())
    rows = menubar._provider_rows(
        type("I", (), {"settings": type("S", (), {"refresh_interval": 60})(),
                       "now": time.time()})(), state,
    )
    key_row = [r.tooltip for r in rows if r.attention and r.tooltip]
    return {
        "list": _flat(out + " " + err),
        "status": _flat(" ".join(s.status_lines())),
        "dashboard": _flat(" ".join(snap.key_notes)),
        "menu bar": _flat(" ".join(state.key_notes)),
        "menu bar row": _flat(" ".join(key_row)),
    }


def _json_notes(s) -> dict[str, list[dict] | None]:
    return {
        "list": s.list_accounts(json_output=True).get("keyNotes"),
        "status": s.status(json_output=True).get("keyNotes"),
    }


def _beat(h, *, mode: str, dry_run: bool = False, ago: float = 0.0,
          host: str = "qswap auto") -> None:
    from ai_quota_swap.autoswitch import write_heartbeat

    at = time.time() - ago
    write_heartbeat(h.switcher.backup_dir, mode=mode, dry_run=dry_run, last_tick_at=at,
                    next_tick_at=at + 60 if mode == "loop" else None, started_at=at,
                    host=host)


def _entered_with_a_kept_reset(h, monkeypatch) -> None:
    """The engine enters #2 while #1 holds a reset the defaults keep; the
    store then holds that reading, as it does after the tick."""
    from ai_quota_swap.autoswitch import TickOutcome

    outcome, claims = _tick_resets(h, _ten_hours_left())
    assert claims == [] and outcome is TickOutcome.SWITCHED
    assert h.switcher.helper_account() == "2"
    assert h.switcher.key_entered_by() == "engine"
    _stored(h, monkeypatch, _ten_hours_left())
    _beat(h, mode="loop")  # an engine watches: the reset is the only note


# == EM4: a reset auto mode keeps is named while an engine-entered key is active =============


class TestTheKeptResetOnEverySurface:
    def test_status_list_the_dashboard_and_the_menu_bar_name_it(self, temp_home, monkeypatch,
                                                              capsys):
        h = _fleet(temp_home)
        _entered_with_a_kept_reset(h, monkeypatch)
        for surface, text in _surfaces(h.switcher, capsys).items():
            assert KEPT in text, surface
        assert [n for n in h.switcher.key_notes() if n["code"] == "reset-kept"] == [{
            "code": "reset-kept", "account": 1, "text": KEPT, "fix": "qswap reset 1",
        }]

    def test_one_note_per_account(self, temp_home, monkeypatch):
        from ai_quota_swap.settings import set_setting

        h = _fleet(temp_home)
        _entered_with_a_kept_reset(h, monkeypatch)
        set_setting(h.switcher.backup_dir, "resets.auto", "off")
        both = {**_ten_hours_left(), "3": _rusage(d7=100, reset_in=10 * 3600, grants=[_week()])}
        _stored(h, monkeypatch, both)
        notes = [n for n in h.switcher.key_notes() if n["code"] == "reset-kept"]
        assert [(n["account"], n["fix"]) for n in notes] == [
            (1, "qswap reset 1"), (3, "qswap reset 3"),
        ]
        assert "(auto mode is off)" in notes[0]["text"]

    def test_it_lasts_only_while_it_is_true(self, temp_home, monkeypatch, capsys):
        """The reset spent by hand (no grant left): the note goes."""
        h = _fleet(temp_home)
        _entered_with_a_kept_reset(h, monkeypatch)
        spent = {**_ten_hours_left(), "1": _rusage(d7=100, reset_in=10 * 3600)}
        _stored(h, monkeypatch, spent)
        for surface, text in _surfaces(h.switcher, capsys).items():
            assert "auto mode keeps" not in text, surface

    def test_a_key_the_user_entered_names_none(self, temp_home, monkeypatch):
        """On a key the user entered the planner keeps its usual rules (A7);
        the note is about what auto-switch chose."""
        h = _fleet(temp_home)
        _entered_with_a_kept_reset(h, monkeypatch)
        record = json.loads(h.switcher.key_state_path.read_text(encoding="utf-8"))
        record["enteredBy"] = "user"
        h.switcher._write_key_state(record)
        assert h.switcher.key_entered_by() == "user"
        assert "reset-kept" not in [n["code"] for n in h.switcher.key_notes()]

    def test_on_a_subscription_there_is_no_such_note(self, temp_home, monkeypatch):
        h = _fleet(temp_home)
        _stored(h, monkeypatch, _ten_hours_left())
        assert h.switcher.helper_account() is None
        assert h.switcher.key_notes() == []

    def test_the_notes_fit_80_columns(self, temp_home, monkeypatch):
        h = _fleet(temp_home)
        _entered_with_a_kept_reset(h, monkeypatch)
        lines = h.switcher.key_helper_notes()
        assert any("auto mode keeps" in line for line in lines)
        for line in lines:
            assert len(line) <= 80, line
        for line in h.switcher.status_lines():
            assert len(line) <= 80, line


# == DS13: keyNotes in list --json and status --json =========================================


DS13_CODES = (
    "settings-unreadable", "line-came-back", "line-lost", "unwatched", "profiles-linked",
    "profiles-stale", "env-outranks", "other-helper", "other-data-dir", "reset-kept",
    "limit-latched",
)


class TestKeyNotesInJson:
    def test_one_entry_per_note_the_text_view_prints(self, temp_home, monkeypatch):
        """Nothing watches the key auto-switch entered, and #1 holds a reset
        auto mode keeps: two notes in the text, two entries, in its order."""
        h = _fleet(temp_home)
        from ai_quota_swap.autoswitch import HEARTBEAT_FILENAME

        _entered_with_a_kept_reset(h, monkeypatch)
        (h.switcher.backup_dir / HEARTBEAT_FILENAME).unlink()
        text = [n["code"] for n in h.switcher.key_notes()]
        assert text == ["unwatched", "reset-kept"]
        expected = [
            {"code": "unwatched", "account": 2, "fix": "qswap switch 1"},
            {"code": "reset-kept", "account": 1, "fix": "qswap reset 1"},
        ]
        for command, notes in _json_notes(h.switcher).items():
            assert notes == expected, command

    def test_a_clean_state_has_no_key_notes(self, temp_home, monkeypatch):
        h = _fleet(temp_home)
        _stored(h, monkeypatch, EXHAUSTED)
        for command, notes in _json_notes(h.switcher).items():
            assert notes is None, command

    @pytest.mark.parametrize("reason,fix", [
        ("budget", "qswap budget 2 AMOUNT"),
        ("credit-spent", "qswap credit 2 AMOUNT"),
    ])
    def test_a_latched_limit_is_one_entry_where_its_row_is_printed(
        self, temp_home, monkeypatch, capsys, reason, fix,
    ):
        """The row of #2 says its limit was reached earlier and still counts;
        list prints every row, status only the active account's."""
        h = _fleet(temp_home)
        if reason == "credit-spent":
            from ai_quota_swap import api_spend

            api_spend.set_credit(h.switcher, "2", 100, now=h.clock.now)
        reading = _reading(h, used=300.0)
        if reason == "credit-spent":
            reading["credit"] = {"amount": 100.0, "used": 60.0, "left": 40.0, "spent": False,
                                 "expired": False, "since": "2026-11-01T00:00:00Z",
                                 "expires": None, "source": "ledger"}
        reading["latched"] = {"reason": reason, "account": "2"}
        _stored(h, monkeypatch, EXHAUSTED, {"2": reading})
        capsys.readouterr()
        h.switcher.list_accounts()
        out = _flat(capsys.readouterr().out)
        assert f"until {'the month ends or ' if reason == 'budget' else ''}it" in out
        assert fix in out
        listed = h.switcher.list_accounts(json_output=True)["keyNotes"]
        assert listed == [{"code": "limit-latched", "account": 2, "fix": fix}]
        # status prints #1's block only: no row of #2, so no entry.
        assert h.switcher.status(json_output=True).get("keyNotes") is None

        # With #2 the active key, status prints its row, and the entry.
        h.switcher.switch_to("2", json_output=True)
        assert h.switcher.helper_account() == "2"
        status = h.switcher.status(json_output=True)["keyNotes"]
        assert {"code": "limit-latched", "account": 2, "fix": fix} in status

    def test_the_switching_sides_notes(self, temp_home, monkeypatch):
        """The notes the switching track made (another program's helper, a
        variable that outranks the line, a record whose line is gone) each
        come through with the fix the text names first."""
        h = _fleet(temp_home)
        _stored(h, monkeypatch, EXHAUSTED)
        settings = temp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"apiKeyHelper": "/opt/vault/print-key",
                                        "env": {"ANTHROPIC_API_KEY": "x"}}), encoding="utf-8")
        where = str(Path("~") / ".claude" / "settings.json")
        expected = [
            {"code": "other-helper", "account": None, "fix": f"Remove apiKeyHelper from {where}"},
            {"code": "env-outranks", "account": None,
             "fix": f"Remove ANTHROPIC_API_KEY from the env block of {where}"},
        ]
        assert [{k: n[k] for k in ("code", "account", "fix")}
                for n in h.switcher.key_notes()] == expected
        for command, notes in _json_notes(h.switcher).items():
            assert notes == expected, command

    def test_the_codes(self):
        """Every code DS13 lists is one the notes can give, and each code the
        notes can give is named in docs/json-output.md."""
        for code in DS13_CODES:
            assert code in key_activation.KEY_NOTE_CODES, code
        doc = (Path(__file__).parent.parent / "docs" / "json-output.md").read_text(
            encoding="utf-8"
        )
        for code in key_activation.KEY_NOTE_CODES:
            assert f"`{code}`" in doc, code
        assert re.search(r'"keyNotes": \[', doc), "the page carries one example"

    def test_the_pages_example_is_what_list_prints(self, temp_home, monkeypatch):
        """docs/json-output.md's keyNotes example, against the payload of the
        state it describes, printed as the CLI prints it; ``...`` marks lines
        left out."""
        from ai_quota_swap.autoswitch import HEARTBEAT_FILENAME

        h = _fleet(temp_home)
        _entered_with_a_kept_reset(h, monkeypatch)
        (h.switcher.backup_dir / HEARTBEAT_FILENAME).unlink()
        printed = json.dumps(h.switcher.list_accounts(json_output=True), indent=2)
        doc = (Path(__file__).parent.parent / "docs" / "json-output.md").read_text(
            encoding="utf-8"
        )
        block = re.search(r"```\n\$ qswap list --json\n(\{\n.*?\n\})\n```",
                          doc[doc.index("| `limit-latched` |"):], re.S).group(1)
        for chunk in block.split("\n...\n"):
            assert chunk in printed, chunk


# == DS3 and DS4: what watches the key, the same on every surface =============================


class TestWhatWatchesOnEverySurface:
    @pytest.fixture
    def h(self, temp_home, monkeypatch):
        h = _fleet(temp_home)
        _enter(h)
        _stored(h, monkeypatch, EXHAUSTED)
        return h

    def test_a_dry_run_does_not_watch(self, h, capsys):
        _beat(h, mode="loop", dry_run=True, host="qswap dashboard")
        words = ("Auto-switch entered #2 ci and only a dry run is running, so nothing "
                 "watches its limit. To go back: qswap switch 1")
        for surface, text in _surfaces(h.switcher, capsys).items():
            assert words in text, surface
        for command, notes in _json_notes(h.switcher).items():
            assert notes == [{"code": "unwatched", "account": 2, "fix": "qswap switch 1"}], command

    def test_a_recent_once_tick_watches_and_says_when(self, h, capsys):
        _beat(h, mode="once", ago=4 * 60 + 5)
        words = "Auto-switch entered #2 ci and last checked it 4m ago (qswap auto --once)."
        for surface, text in _surfaces(h.switcher, capsys).items():
            assert words in text, surface
            assert "nothing watches" not in text, surface
        for command, notes in _json_notes(h.switcher).items():
            assert notes == [{"code": "watched-once", "account": 2, "fix": None}], command

    def test_an_old_once_tick_is_nothing_watching(self, h, capsys):
        _beat(h, mode="once", ago=20 * 60 + 5)
        words = ("Auto-switch entered #2 ci and is not running, so nothing watches its "
                 "limit; it last checked 20m ago. To go back: qswap switch 1")
        for surface, text in _surfaces(h.switcher, capsys).items():
            assert words in text, surface
        for command, notes in _json_notes(h.switcher).items():
            assert notes == [{"code": "unwatched", "account": 2, "fix": "qswap switch 1"}], command

    def test_a_loop_that_ticks_says_nothing(self, h, capsys):
        _beat(h, mode="loop")
        for surface, text in _surfaces(h.switcher, capsys).items():
            assert "Auto-switch entered" not in text, surface
        for command, notes in _json_notes(h.switcher).items():
            assert notes is None, command
