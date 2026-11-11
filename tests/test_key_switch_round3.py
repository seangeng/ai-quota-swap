"""Round 3 of pull request #13 (the API-key switch), the switching side and
the surfaces: decisions SS3, SS4, SS9 with DS16, DS7 (points 3 and 4), DS11,
DS17 and E1 of pr13-round3-decisions.md. Everything runs on the temporary
home, the in-memory Keychain fake and faked network, as
tests/test_key_helper_live.py does, whose fleet and helpers these tests share.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_quota_swap import claude_settings, key_activation
from tests.test_key_helper_live import (
    QSWAP,
    pin_key_helper_platform,
    _activate,
    _fleet,
    _settings,
)


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    """The installed command every line names, taken to exist and answer
    (as in tests/test_key_helper_live.py)."""
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


def _notes(s) -> str:
    return " ".join(" ".join(s.key_helper_notes()).split())


def _key_record(s) -> dict:
    if not s.key_state_path.exists():
        return {}
    return json.loads(s.key_state_path.read_text(encoding="utf-8"))


def _write_line(home: Path, number: int | str, *, extra: str = "") -> None:
    _settings(home).write_text(
        json.dumps({"apiKeyHelper": f"{QSWAP} key-helper {number}{extra}"}),
        encoding="utf-8",
    )


# == SS3: a tombstone counts for 24 hours ===========================================


def _age_tombstone(s, number: str, at: object) -> None:
    record = _key_record(s)
    record["left"][number]["at"] = at
    s._write_key_state(record)


class TestATombstoneCountsForADay:
    def _left_by_the_engine(self, temp_home):
        s = _fleet()
        s.switch_to("5", json_output=True, entered_by="engine")
        s.switch_to("1", json_output=True)
        assert _key_record(s)["left"]["5"]["enteredBy"] == "engine"
        return s

    def test_within_a_day_the_line_came_back(self, temp_home):
        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", time.time() - 23 * 3600)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", True, "engine")
        assert s.key_entered_by() == "engine"

    @pytest.mark.parametrize("at", [
        "old", None, "yesterday", True, float("nan"),
    ])
    def test_a_tombstone_with_no_readable_time_is_old(self, temp_home, at):
        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", at)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", False, "user")
        assert s.key_entered_by() == "user"

    def test_after_a_day_a_line_for_that_number_is_the_users(self, temp_home):
        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", time.time() - 24 * 3600 - 60)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", False, "user")
        assert s.key_entered_by() == "user"
        assert "came back" not in _notes(s)

    def test_doctor_does_not_say_an_old_one_came_back(self, temp_home):
        from ai_quota_swap import doctor

        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", time.time() - 30 * 24 * 3600)
        _write_line(temp_home, 5)
        check = {c.id: c for c in doctor.run_checks()}["claude.key_helper"]
        assert "came back" not in check.summary
        assert check.status == "pass"

    def test_the_engine_leaves_a_hand_written_line_alone(self, temp_home):
        """The re-check's case: auto-switch entered #2 and left it; thirty days
        later the user writes #2's line by hand, and the next tick took it
        out again as auto-switch's own."""
        from ai_quota_swap.autoswitch import TickOutcome
        from tests.test_autoswitch import _usage
        from tests.test_key_helper_live import EXHAUSTED, _harness

        h = _harness(temp_home)
        h.tick_with_usage(EXHAUSTED)
        assert h.switcher.key_entered_by() == "engine"
        h.clock.now += 3600
        h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)})
        s = h.switcher
        assert s.helper_account() is None
        assert _key_record(s)["left"]["2"]["enteredBy"] == "engine"
        _age_tombstone(s, "2", time.time() - 30 * 24 * 3600)
        written = claude_settings.helper_command(2)
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": written}), encoding="utf-8"
        )
        assert s.key_entered_by() == "user"
        h.clock.now += 3600
        outcome = h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)})
        assert outcome is not TickOutcome.SWITCHED
        assert s.helper_account() == "2"
        assert json.loads(_settings(temp_home).read_text())["apiKeyHelper"] == written

    def test_the_rule_reads_the_age_from_at(self):
        now = 1_000_000.0
        record = {"left": {"5": {"at": now - 3600, "enteredBy": "engine"}}}
        assert key_activation.entered_by_of(record, "5", now=now) == ("engine", True)
        assert key_activation.entered_by_of(record, "5", now=now + 24 * 3600) == (
            "user", False,
        )
