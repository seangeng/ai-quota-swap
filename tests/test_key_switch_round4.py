"""Switching, the settings file and the surfaces, round 4 of PR #13: what
the verification of round 3 found (the orchestrator's notes,
pr13-round3-decisions.md SS3, SS4, SS9, DS11, DS13, DS17 and E1).

Everything runs on the suite's fake home and fake Keychain, with made-up
names; the key line's command is fixed as in test_key_helper_live.py.
"""

from __future__ import annotations

import json
import time

import pytest

from ai_quota_swap import claude_settings
from tests.test_key_helper_live import QSWAP, _fleet, _settings, pin_key_helper_platform
from tests.test_key_switch_round3 import _age_tombstone, _key_record, _write_line


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)
    monkeypatch.setattr(claude_settings, "check_command", lambda command, **kw: None)
    pin_key_helper_platform(monkeypatch)


# == SS3: a tombstone counts for 24 hours, and only from the past ============================


def _left_by_the_engine():
    s = _fleet()
    s.switch_to("5", json_output=True, entered_by="engine")
    s.switch_to("1", json_output=True)
    assert _key_record(s)["left"]["5"]["enteredBy"] == "engine"
    return s


class TestATombstoneCountsOnlyForItsDay:
    def test_leaving_a_hand_written_line_does_not_revive_an_old_tombstone(self, temp_home):
        """The re-check's case: auto-switch entered #5 and left it; a month
        later the user writes #5's line by hand and leaves it. The new
        tombstone is the user's, so a line written again later is the
        user's too, never auto-switch's."""
        s = _left_by_the_engine()
        _age_tombstone(s, "5", time.time() - 30 * 24 * 3600)
        _write_line(temp_home, 5)
        assert s.key_entered_by() == "user"
        s.switch_to("1", json_output=True)
        assert _key_record(s)["left"]["5"]["enteredBy"] == "user"
        _write_line(temp_home, 5)  # within the day: it came back, as the user's
        f = s.key_findings()
        assert (f.key, f.entered_by) == ("5", "user")
        assert s.key_entered_by() == "user"
        assert "Auto-switch entered" not in " ".join(s.key_helper_notes())

    def test_a_tombstone_dated_ahead_of_the_clock_is_old(self, temp_home):
        s = _left_by_the_engine()
        _age_tombstone(s, "5", time.time() + 40 * 24 * 3600)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", False, "user")

    def test_a_time_too_large_for_a_float_is_old_and_no_traceback(self, temp_home):
        s = _left_by_the_engine()
        _age_tombstone(s, "5", 10 ** 400)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", False, "user")
        assert isinstance(s.key_helper_notes(), list)

    def test_a_few_seconds_of_clock_skew_still_counts(self, temp_home):
        s = _left_by_the_engine()
        _age_tombstone(s, "5", time.time() + 30)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.came_back, f.entered_by) == (True, "engine")
