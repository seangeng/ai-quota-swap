"""Switching, the settings file and the surfaces, round 4 of PR #13: what
the verification of round 3 found (the orchestrator's notes,
pr13-round3-decisions.md SS3, SS4, SS9, DS11, DS13, DS17 and E1).

Everything runs on the suite's fake home and fake Keychain, with made-up
names; the key line's command is fixed as in test_key_helper_live.py.
"""

from __future__ import annotations

import time

import pytest

from ai_quota_swap import claude_settings
from tests.test_key_helper_live import QSWAP, _fleet, pin_key_helper_platform
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


# == SS4: doctor's line for another qswap's key line ===========================================


from tests.test_key_switch_round3 import RULES, _on, _other_line, _put  # noqa: E402


class TestDoctorsLineForAnotherQswap:
    """The summary held two apostrophes ("qswap's ... That qswap's"); the
    printer took the text between them for one quoted word and cut it at 80
    columns with an ellipsis."""

    @pytest.mark.parametrize("rules", list(RULES))
    def test_it_is_whole_at_80_columns(self, temp_home, monkeypatch, rules):
        from ai_quota_swap import doctor

        _on(monkeypatch, temp_home, rules)
        other = temp_home / "other" / "ai-quota-swap"
        _put(temp_home, _other_line(5, other))
        checks = [c for c in doctor.run_checks() if "another qswap" in c.summary]
        lines = doctor.render_text(checks, online=False, width=80)
        text = " ".join(" ".join(lines).split())
        assert "…" not in text, lines
        assert "uses the key of that qswap instead of the active account" in text
        for line in lines:
            if not line.strip().startswith(("~", "/", "qswap ")) and ":\\" not in line:
                assert len(line) <= 80, line


# == DS13: keyNotes for a key active the older way =============================================


class TestStatusJsonForAKeyActiveTheOlderWay:
    def test_the_latched_row_status_prints_has_its_entry(self, temp_home, monkeypatch, capsys):
        """#2 ci (latched budget) made active with --log-out-sessions: status
        prints its row with the latch note, so status --json carries the
        entry, as list --json does."""
        from tests.test_engine_money import EXHAUSTED, _fleet as _engine_fleet, _reading
        from tests.test_round3_wiring import _flat, _stored

        h = _engine_fleet(temp_home)
        reading = _reading(h, used=300.0)
        reading["latched"] = {"reason": "budget", "account": "2"}
        _stored(h, monkeypatch, EXHAUSTED, {"2": reading})
        h.switcher.switch_to("2", json_output=True, log_out_sessions=True)
        assert h.switcher.helper_account() is None
        assert h.switcher.current_account_number() == "2"
        capsys.readouterr()
        h.switcher.status()
        assert "qswap budget 2 AMOUNT" in _flat(capsys.readouterr().out)
        entry = {"code": "limit-latched", "account": 2, "fix": "qswap budget 2 AMOUNT"}
        assert entry in h.switcher.status(json_output=True).get("keyNotes", [])


# == DS17: add-token without --slot while the settings cannot be read =========================


from tests import test_key_switch_round3 as _r3  # noqa: E402
from tests.test_key_switch_round3 import NEW_KEY, WHERE  # noqa: E402


@pytest.fixture
def run(monkeypatch, capsys):
    """``qswap ARGS`` through cli.main on a switcher: (exit, out, err)."""
    from unittest.mock import patch

    from ai_quota_swap import cli, display
    from tests.test_key_helper_live import _main

    monkeypatch.setattr("ai_quota_swap.update_check.check_for_update", lambda v: None)
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    for name in display.OVERRIDE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def go(switcher, *argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        with patch.object(cli, "ClaudeAccountSwitcher", return_value=switcher), \
             patch.object(switcher, "list_accounts"), \
             patch.object(switcher, "target_readiness", return_value=(None, "")):
            code = _main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return go


class TestAddTokenByLabelWhileTheSettingsCannotBeRead:
    """DS17's refusal when the key is replaced by the label its account
    already has, as a user rotating a key types it (no --slot)."""

    _base = _r3.TestAddTokenWhileTheSettingsCannotBeRead()

    def test_it_stops_with_the_shared_sentence_and_writes_nothing(self, temp_home, run):
        s, key = self._base._unreadable(temp_home)
        before = self._base._written(s, temp_home)
        code, out, err = run(s, "add-token", NEW_KEY, "--email", s.account_email("5"), "--yes")
        assert code == 1
        text = " ".join((out + err).split())
        assert f"{WHERE} cannot be read (line 1: " in text
        assert (
            "); qswap last made #5 ci the active API key there, and its key line "
            f"may still be in it. Fix the JSON in {WHERE}, then retry."
        ) in text
        assert "Added" not in text and "Updated" not in text
        assert self._base._written(s, temp_home) == before
        assert s._read_account_credentials("5", s.account_email("5")) == key

    def test_with_readable_settings_the_key_is_replaced_as_before(self, temp_home, run):
        s = _fleet()
        s.switch_to("5", json_output=True)
        code, out, err = run(s, "add-token", NEW_KEY, "--email", s.account_email("5"), "--yes")
        assert code == 0, err
        assert s._read_account_credentials("5", s.account_email("5")) == NEW_KEY


# == DS11: from one key to another over a login qswap does not manage =========================


class TestKeyToKeyOverAnUnmanagedLogin:
    def test_the_login_is_named_before_the_way_back(self, temp_home, run):
        """#5 ci is active over new@example.com, a login qswap does not
        manage; qswap switch 6 ended with "add that login first" and named
        no login."""
        from tests.test_key_switch_round3 import _login

        s = _fleet()
        _login("new@example.com")
        s.switch_to("5", json_output=True)
        code, out, err = run(s, "switch", "6")
        assert code == 0, err
        text = " ".join(out.split())
        assert "new@example.com stays logged in underneath." in text
        assert text.endswith("To go back, add that login first: qswap add")
        result = s.switch_to("5", json_output=True)
        assert "new@example.com stays logged in underneath." in " ".join(result["followup"])


# == E1: a lost record over a login qswap does not manage ======================================


class TestALostRecordOverAnUnmanagedLogin:
    """E1: the fix every surface names clears the record. Over a login
    qswap does not manage that fix is `qswap add`, which left the record
    and the warning in place."""

    def test_qswap_add_clears_it(self, temp_home):
        from ai_quota_swap import doctor
        from tests.test_key_switch_round3 import _login, _notes, _removed_by_hand

        s = _removed_by_hand(temp_home)
        _login("new@example.com")
        assert "To leave it: qswap add." in _notes(s)
        assert [n["fix"] for n in s.key_notes() if n["code"] == "line-lost"] == ["qswap add"]
        s.add_account(assume_yes=True)
        f = s.key_findings()
        assert (f.lost, f.stale_active) == (None, None)
        assert "no longer in" not in _notes(s)
        assert not [n for n in s.key_notes() if n["code"] == "line-lost"]
        lost = [c for c in doctor.run_checks() if "no longer in" in c.summary]
        assert not lost
        assert s.current_account_number() == s._find_account_slot(
            s._get_sequence_data(), "new@example.com", ""
        )


# == SS9: a credential in the env block beside an active key line ==============================


class TestACredentialBesideTheActiveKey:
    """The end-to-end run on Claude Code 2.1.322: with the line in and
    ANTHROPIC_AUTH_TOKEN added to the env block, sessions still run the
    helper and send #5's key with every request (x-api-key) beside the
    token (the bearer); with ANTHROPIC_BASE_URL too, to that host. The note
    said "so sessions do not use #5 ci", and doctor named no host."""

    def _over_the_key(self, temp_home, **env):
        from tests.test_key_switch_round3 import _with_env

        s = _fleet()
        s.switch_to("5", json_output=True)
        _with_env(temp_home, **env)
        return s

    def test_the_note_says_the_key_is_still_sent(self, temp_home):
        from tests.test_key_switch_round3 import WHERE, _notes

        s = self._over_the_key(temp_home, ANTHROPIC_AUTH_TOKEN="made-up")
        text = _notes(s)
        assert (
            f"{WHERE} sets ANTHROPIC_AUTH_TOKEN in its env block, which Claude Code obeys "
            "before any key line, yet sessions still send the key of #5 ci with every "
            "request, beside the credential that variable sets, so either may be billed. "
            f"Remove ANTHROPIC_AUTH_TOKEN from the env block of {WHERE}, then: qswap switch 5"
        ) in text
        assert "do not use #5 ci" not in text
        assert "made-up" not in text

    def test_with_a_base_url_it_names_the_host(self, temp_home):
        from ai_quota_swap import doctor
        from tests.test_key_switch_round3 import _notes

        s = self._over_the_key(temp_home, ANTHROPIC_AUTH_TOKEN="made-up",
                               ANTHROPIC_BASE_URL="https://gateway.example.com/v1")
        assert "with every request to gateway.example.com, beside" in _notes(s)
        env = {c.id: c for c in doctor.run_checks()}["claude.settings_env"]
        text = " ".join(env.summary.split())
        assert "the key of #5 ci with every request to gateway.example.com" in text
        assert "qswap will not switch" not in text
        assert env.fix.endswith("then: qswap switch 5")

    def test_another_provider_still_means_the_key_is_not_used(self, temp_home):
        from tests.test_key_switch_round3 import _notes

        s = self._over_the_key(temp_home, CLAUDE_CODE_USE_BEDROCK="1")
        assert "so sessions do not use #5 ci" in _notes(s)


# == R4-3: the refusals for a credential variable say what happens ============================


def _refusal(text: str) -> str:
    return " ".join(text.split())


class TestTheRefusalsForACredential:
    """Decision R4-3: Claude Code sends the key from the line beside a
    credential in the env block (the round 3 note), so the first-time
    refusal and the refusal of the switch to the key already active say
    that, in the tense that fits. A provider variable keeps "would not use"."""

    @pytest.mark.parametrize("name", ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"])
    def test_the_first_switch_says_the_key_would_be_sent_beside_it(
        self, temp_home, run, name
    ):
        from tests.test_key_switch_round3 import _with_env

        s = _fleet()
        _with_env(temp_home, **{name: "made-up"})
        code, out, err = run(s, "switch", "5")
        assert code == 1
        assert _refusal(err) == (
            f"Error: not switched. {WHERE} sets {name} in its env block, which Claude "
            "Code obeys before any key line, yet sessions would send the key of #5 ci "
            "with every request, beside the credential that variable sets, so either "
            f"could be billed. Remove {name} from the env block of {WHERE}, then: "
            "qswap switch 5"
        )
        assert all(len(line) <= 80 for line in err.splitlines()), err
        assert "made-up" not in out + err
        assert s.current_account_number() == "1"

    def test_with_a_base_url_the_first_switch_names_the_host(self, temp_home, run):
        from tests.test_key_switch_round3 import _with_env

        s = _fleet()
        _with_env(temp_home, ANTHROPIC_AUTH_TOKEN="made-up",
                  ANTHROPIC_BASE_URL="https://gateway.example.com/v1")
        code, _out, err = run(s, "switch", "5")
        assert code == 1
        assert (
            "yet sessions would send the key of #5 ci with every request to "
            "gateway.example.com, beside the credential that variable sets, so either "
            "could be billed."
        ) in _refusal(err)

    @pytest.mark.parametrize("name", ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"])
    def test_the_switch_to_the_active_key_says_it_is_still_sent(self, temp_home, run, name):
        from tests.test_key_switch_round3 import _with_env

        s = _fleet()
        s.switch_to("5", json_output=True)
        _with_env(temp_home, **{name: "made-up"})
        code, _out, err = run(s, "switch", "5")
        assert code == 1
        assert _refusal(err) == (
            f"Error: not switched. {WHERE} sets {name} in its env block, which Claude "
            "Code obeys before any key line, yet sessions still send the key of #5 ci "
            "with every request, beside the credential that variable sets, so either "
            f"may be billed. Remove {name} from the env block of {WHERE}, then: "
            "qswap switch 5"
        )
        assert all(len(line) <= 80 for line in err.splitlines()), err

    @pytest.mark.parametrize("active", [False, True])
    def test_a_provider_variable_keeps_would_not_use(self, temp_home, run, active):
        from tests.test_key_switch_round3 import _with_env

        s = _fleet()
        if active:
            s.switch_to("5", json_output=True)
        _with_env(temp_home, ANTHROPIC_AUTH_TOKEN="made-up", CLAUDE_CODE_USE_VERTEX="1")
        code, _out, err = run(s, "switch", "5")
        assert code == 1
        text = _refusal(err)
        assert (
            "which Claude Code obeys before any key line, so sessions would not use "
            "#5 ci."
        ) in text
        assert "billed" not in text
