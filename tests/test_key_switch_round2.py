"""Round 2 of pull request #13 (the API-key switch), the switching side:
decisions B1, B2 (the active key's line repaired), B5 to B9 of
pr13-round2-decisions.md. Everything runs on the temporary home, the
in-memory Keychain fake and faked network, as tests/test_key_helper_live.py
does, whose fleet and helpers these tests share.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import claude_settings, display
from ai_quota_swap.exceptions import SwitchError
from ai_quota_swap.models import Platform
from ai_quota_swap.paths import get_global_config_path
from tests.test_key_helper_live import (
    pin_key_helper_platform,
    KEY_5,
    KEY_6,
    MAYA,
    QSWAP,
    SAM,
    _activate,
    _fits,
    _fleet,
    _main,
    _profile,
    _settings,
    _switcher,
)


#: How the messages name Claude Code's user settings: "~/.claude/settings.json",
#: with this platform's separator.
WHERE = str(Path("~") / ".claude" / "settings.json")


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    """The installed command every line names, taken to exist and answer
    (as in tests/test_key_helper_live.py)."""
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)
    monkeypatch.setattr(claude_settings, "check_command", lambda command, **kw: None)
    pin_key_helper_platform(monkeypatch)


@pytest.fixture
def alive(monkeypatch):
    pids: set[int] = set()
    monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive", lambda pid: pid in pids)
    return pids


@pytest.fixture
def run(monkeypatch, capsys):
    """``qswap ARGS`` through cli.main on ``switcher``: (exit, out, err)."""
    from ai_quota_swap import cli

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


def _line(home: Path) -> str | None:
    """qswap's line in the user settings, or None."""
    path = _settings(home)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("apiKeyHelper")
    except ValueError:
        return None


def _keys_only() -> object:
    """API-key accounts #5 ci and #6 ops, and no claude.ai login at all."""
    s = _switcher(Platform.MACOS)
    s.add_account_from_token(KEY_5, slot=5)
    s.add_account_from_token(KEY_6, slot=6)
    s.set_alias("5", "ci")
    s.set_alias("6", "ops")
    get_global_config_path().write_text("{}", encoding="utf-8")
    return s


def _no_login_with_subscriptions_disabled():
    """The fleet after /logout, its two subscriptions disabled."""
    s = _fleet()
    s.set_account_disabled("1", True)
    s.set_account_disabled("7", True)
    _activate(s)
    get_global_config_path().write_text("{}", encoding="utf-8")
    return s


# == B1: a rotation from a key decides first, then moves ===========================


class TestRotationFromAKeyDecidesFirst:
    @pytest.mark.parametrize("strategy", [None, "best", "next-available"])
    def test_keys_only_nothing_to_rotate_to_keeps_the_line(self, temp_home, strategy):
        s = _keys_only()
        _activate(s)
        extra = {"strategy": strategy} if strategy else {}
        with patch.object(s, "_usage_by_account", return_value={}):
            result = s.switch(json_output=True, **extra)
        assert result["switched"] is False
        assert result["reason"] == "api-key-only"
        assert result["from"]["number"] == 5 and result["to"]["number"] == 5
        assert "you stay on #5 ci" in result["message"]
        assert "qswap switch 6" in result["message"]
        assert _line(temp_home) == f"{QSWAP} key-helper 5"
        assert s.current_account_number() == "5"

    def test_keys_only_text_says_the_next_step_and_exits_0(self, temp_home, run):
        s = _keys_only()
        _activate(s)
        code, out, err = run(s, "switch")
        assert code == 0, err
        text = " ".join(out.split())
        assert "No subscription can take over, so you stay on #5 ci." in text
        assert "qswap switch 6" in text
        _fits(out)
        assert _line(temp_home) == f"{QSWAP} key-helper 5"

    def test_after_logout_with_subscriptions_disabled_the_line_stays(self, temp_home):
        s = _no_login_with_subscriptions_disabled()
        result = s.switch(json_output=True)
        assert result["switched"] is False
        assert "qswap enable 1" in result["message"] or "qswap enable 7" in result["message"]
        assert _line(temp_home) == f"{QSWAP} key-helper 5"

    def test_after_logout_a_subscription_is_switched_to(self, temp_home):
        s = _fleet()
        _activate(s)
        get_global_config_path().write_text("{}", encoding="utf-8")
        result = s.switch(json_output=True)
        assert result["switched"] is True
        assert result["from"]["number"] == 5
        assert result["to"]["number"] in (1, 7)
        assert _line(temp_home) is None

    def test_a_key_over_an_unmanaged_login_keeps_the_line(self, temp_home):
        s = _fleet()
        _activate(s)
        get_global_config_path().write_text(json.dumps({
            "oauthAccount": {"emailAddress": "new@example.com", "accountUuid": "",
                             "organizationUuid": None},
        }), encoding="utf-8")
        result = s.switch(json_output=True)
        assert result["switched"] is False and result["reason"] == "unmanaged-account"
        assert result["from"] == result["to"]
        assert result["to"]["number"] == 5
        assert "new@example.com" in result["message"] and "qswap add" in result["message"]
        assert _line(temp_home) == f"{QSWAP} key-helper 5"

    def test_a_key_over_an_unmanaged_login_in_text_adds_it_and_stays(
        self, temp_home, capsys
    ):
        s = _fleet()
        _activate(s)
        get_global_config_path().write_text(json.dumps({
            "oauthAccount": {"emailAddress": "new@example.com", "accountUuid": "",
                             "organizationUuid": None},
        }), encoding="utf-8")
        capsys.readouterr()
        with patch.object(s, "_reject_foreign_credential_capture", side_effect=lambda c, *a: c):
            s.switch()
        out = " ".join(capsys.readouterr().out.split())
        assert _line(temp_home) == f"{QSWAP} key-helper 5"
        assert "#5 ci is still the active API key" in out
        assert "qswap switch 8" in out

    def test_a_key_over_a_saved_key_with_nothing_to_rotate_to_stays(self, temp_home):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        s.switch_to("6", json_output=True)
        s.set_account_disabled("1", True)
        s.set_account_disabled("7", True)
        result = s.switch(json_output=True)
        assert result["switched"] is False and result["reason"] == "api-key-only"
        assert result["to"]["number"] == 6
        assert "you stay on #6 ops" in result["message"]
        assert _line(temp_home) == f"{QSWAP} key-helper 6"
        assert s.current_account_number() == "6"

    def test_a_rotation_that_fails_after_the_line_puts_it_back(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        s = _fleet()
        _activate(s)
        get_global_config_path().write_text("{}", encoding="utf-8")
        before = (_line(temp_home), s._key_state())

        def boom(*a, **k):
            raise SwitchError("Switch failed and was rolled back: disk full")

        monkeypatch.setattr(s, "_perform_switch", boom)
        with pytest.raises(SwitchError, match="disk full"):
            s.switch(json_output=True)
        assert (_line(temp_home), s._key_state()) == before
        assert s.current_account_number() == "5"

    @pytest.mark.parametrize("how", ["named", "rotation"])
    def test_ctrl_c_during_the_login_switch_puts_the_line_back(
        self, temp_home, block_real_keychain, monkeypatch, how
    ):
        """F6: an interrupt is not an Exception, and still moves no session."""
        s = _fleet()
        _activate(s)
        if how == "rotation":
            get_global_config_path().write_text("{}", encoding="utf-8")
        before = (_line(temp_home), s._key_state())

        def interrupted(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(s, "_perform_switch", interrupted)
        with pytest.raises(KeyboardInterrupt):
            if how == "named":
                s.switch_to("7", json_output=True)
            else:
                s.switch(json_output=True)
        assert (_line(temp_home), s._key_state()) == before

    @pytest.mark.parametrize("how", ["named", "rotation", "log-out-sessions"])
    @pytest.mark.parametrize("where", ["restore_run_profiles", "login_account_number", "label"])
    def test_ctrl_c_after_the_line_came_out_puts_it_back(
        self, temp_home, block_real_keychain, monkeypatch, how, where
    ):
        """The re-check's case: an interrupt between taking the line out and
        the login switch (restoring the run profiles, reading the login
        underneath, a label) left the line out and the login unchanged:
        sessions on neither the key nor the account asked for."""
        s = _fleet()
        _activate(s)
        if how == "rotation":
            get_global_config_path().write_text("{}", encoding="utf-8")
        before = (_line(temp_home), s._key_state())
        real_remove = claude_settings.remove
        removed = {"done": False}

        def remove(*a, **k):
            removed["done"] = True
            return real_remove(*a, **k)

        monkeypatch.setattr(claude_settings, "remove", remove)
        real = getattr(s, where)

        def interrupted(*a, **k):
            if removed["done"]:
                raise KeyboardInterrupt
            return real(*a, **k)

        monkeypatch.setattr(s, where, interrupted)
        with pytest.raises(KeyboardInterrupt):
            if how == "named":
                s.switch_to("7", json_output=True)
            elif how == "rotation":
                s.switch(json_output=True)
            else:
                s.switch_to("6", json_output=True, log_out_sessions=True)
        monkeypatch.setattr(s, where, real)
        assert removed["done"]
        assert (_line(temp_home), s._key_state()) == before
        assert s.current_account_number() == "5"

    def test_a_put_back_that_half_fails_leaves_no_stale_copy(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        """F6: one profile cannot get its copy when the line goes back; the
        profile that did get one is given its sharing back."""
        from ai_quota_swap.session import SETTINGS_COPY_MARKER, SessionManager

        s = _fleet()
        good = _profile(s, "7", SAM)
        bad = _profile(s, "1", MAYA)
        _activate(s)
        (s.configs_dir / f".claude-config-7-{SAM}.json").unlink()
        real = SessionManager._write_settings_copy
        calls = {"n": 0}

        def copy(self, src, dest, session_dir):
            calls["n"] += 1
            if Path(session_dir).name == Path(bad).name:
                return False
            return real(self, src, dest, session_dir)

        # Only the put-back runs isolation from here on.
        monkeypatch.setattr(SessionManager, "_write_settings_copy", copy)
        with pytest.raises(SwitchError, match="could not be put back"):
            s.switch_to("7", json_output=True)
        assert calls["n"] >= 1
        assert _line(temp_home) is None
        assert s.stale_profile_copies() == []
        assert not (good / SETTINGS_COPY_MARKER).exists()
        assert (good / "settings.json").is_symlink()


# == B7: qswap add while a key is active ============================================


class TestAddWhileOnAKey:
    def test_add_says_the_key_is_still_active_and_keeps_the_active_number(
        self, temp_home, capsys
    ):
        """F61: a /login as a new account under the key, then qswap add."""
        s = _fleet()
        _activate(s)
        get_global_config_path().write_text(json.dumps({
            "oauthAccount": {"emailAddress": "new@example.com", "accountUuid": "",
                             "organizationUuid": None},
        }), encoding="utf-8")
        capsys.readouterr()
        with patch.object(s, "_reject_foreign_credential_capture", side_effect=lambda c, *a: c):
            s.add_account()
        out = capsys.readouterr().out
        assert out.splitlines()[0] == "Added #8 new@example.com"
        assert " ".join(out.split()).endswith(
            "#5 ci is still the active API key; Claude Code sessions use it, not this "
            "login. To use this login: qswap switch 8"
        )
        _fits(out)
        assert s._get_sequence_data()["activeAccountNumber"] == 5
        assert s.helper_account() == "5"

    def test_updating_the_login_underneath_says_so_too(self, temp_home, capsys):
        s = _fleet()
        _activate(s)
        capsys.readouterr()
        with patch.object(s, "_reject_foreign_credential_capture", side_effect=lambda c, *a: c):
            s.add_account()
        out = " ".join(capsys.readouterr().out.split())
        assert "Updated the saved login of #1 personal" in out
        assert "To use this login: qswap switch 1" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 5


# == B5: one reconcile, both directions ==============================================


def _key_record(s) -> dict:
    if not s.key_state_path.exists():
        return {}
    return json.loads(s.key_state_path.read_text(encoding="utf-8"))


def _notes(s) -> str:
    return " ".join(" ".join(s.key_helper_notes()).split())


def _check(check_id: str):
    from ai_quota_swap import doctor

    return {c.id: c for c in doctor.run_checks()}.get(check_id)


class TestUnknownIsNotMissing:
    """F11: a read that fails is "unknown"; nothing is repaired or removed."""

    def _damage(self, s):
        raw = s.sequence_file.read_bytes()
        s.sequence_file.write_bytes(raw[: len(raw) // 2])

    def test_a_damaged_account_list_removes_no_line(self, temp_home, run):
        s = _fleet()
        d = _profile(s)
        _activate(s)
        before = (_settings(temp_home).read_bytes(), s.key_state_path.read_bytes())
        self._damage(s)
        for argv in (["switch"], ["switch", "--strategy", "best"], ["switch", "1"]):
            code, out, err = run(s, *argv)
            assert code == 1, argv
            assert "Removed the key line" not in out + err
            assert (_settings(temp_home).read_bytes(), s.key_state_path.read_bytes()) == before
        assert not (d / "settings.json").is_symlink()

    def test_the_problem_is_not_said_to_be_a_missing_account(self, temp_home):
        s = _fleet()
        _activate(s)
        self._damage(s)
        assert s.helper_problem() is None
        assert s.reconcile_key_helper() == []
        assert _line(temp_home) == f"{QSWAP} key-helper 5"


class TestTombstones:
    """F10: leaving keeps a tombstone; a line that comes back inherits who
    had entered it, and every surface says so with the way out."""

    def test_leaving_keeps_a_tombstone(self, temp_home):
        s = _fleet()
        _activate(s)
        s.switch_to("1", json_output=True)
        record = _key_record(s)
        assert "number" not in record
        assert record["left"]["5"]["enteredBy"] == "user"
        assert isinstance(record["left"]["5"]["at"], float)

    def _engine_key_came_back(self, temp_home):
        from tests.test_autoswitch import _usage
        from tests.test_key_helper_live import EXHAUSTED, _harness

        h = _harness(temp_home)
        h.tick_with_usage(EXHAUSTED)
        assert h.switcher.key_entered_by() == "engine"
        old = _settings(temp_home).read_bytes()
        h.clock.now += 3600
        h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)})
        assert h.switcher.helper_account() is None
        _settings(temp_home).write_bytes(old)  # another program wrote it back
        return h

    def test_a_line_that_comes_back_keeps_the_engines_name(self, temp_home):
        from tests.test_autoswitch import _usage

        h = self._engine_key_came_back(temp_home)
        s = h.switcher
        assert s.current_account_number() == "2"
        assert s.key_entered_by() == "engine"
        assert s.key_findings().came_back is True
        notes = _notes(s)
        assert "came back" in notes and "qswap switch 3" in notes
        h.clock.now += 3600
        h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)})
        assert s.helper_account() is None  # the engine leaves its key again

    def test_doctor_and_the_menu_bar_say_it_came_back(self, temp_home):
        h = self._engine_key_came_back(temp_home)
        check = _check("claude.key_helper")
        assert check.status == "warn" and "came back" in check.summary
        assert check.fix == "qswap switch 3"
        snap = h.switcher.accounts_snapshot(fetch=set())
        assert "came back" in " ".join(snap.key_notes)

    def test_an_interrupted_key_to_key_switch_does_not_say_the_line_came_back(
        self, temp_home, monkeypatch
    ):
        """The re-check's case: #5 active, qswap switch 6 interrupted while
        the line was being written. The record named #6 with a tombstone for
        #5, so the surfaces said #5's line came back, which it had not."""
        s = _fleet()
        _activate(s)
        real = claude_settings.install

        def interrupted(number, *a, **k):
            if str(number) == "6":
                raise KeyboardInterrupt
            return real(number, *a, **k)

        monkeypatch.setattr(claude_settings, "install", interrupted)
        with pytest.raises(KeyboardInterrupt):
            s.switch_to("6", json_output=True)
        monkeypatch.setattr(claude_settings, "install", real)
        assert _line(temp_home) == f"{QSWAP} key-helper 5"
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by, f.lost) == ("5", False, "user", None)
        assert s.key_entered_by() == "user"
        assert "came back" not in _notes(s)
        assert "no longer in" not in _notes(s)
        s.reconcile_key_helper()  # the record is #5's again
        assert _key_record(s)["number"] == "5" and "replacing" not in _key_record(s)
        # And the next switch to #6 goes through as usual.
        s.switch_to("6", json_output=True)
        assert _key_record(s)["number"] == "6" and "5" in _key_record(s)["left"]

    def test_removing_a_line_that_cannot_give_a_key_keeps_the_other_tombstones(
        self, temp_home
    ):
        """The re-check's case: {left: {6: engine}}, and a line for #9 (no
        such account). Removing it deleted key_helper.json whole, so #6's
        line coming back later read as the user's."""
        s = _fleet()
        s._write_key_state({"left": {"6": {"at": time.time(), "enteredBy": "engine"}}})
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 9"}), encoding="utf-8"
        )
        s.switch_to("7", json_output=True)
        assert _key_record(s)["left"]["6"]["enteredBy"] == "engine"
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 6"}), encoding="utf-8"
        )
        assert s.key_findings().came_back is True
        assert s.key_entered_by() == "engine"

    def test_every_reader_agrees_on_who_entered_a_line_that_came_back(self, temp_home):
        """The re-check's case: auto-switch entered #5, the user switched to
        #6, then an old copy with #5's line was written back. key_findings
        said engine, key_entered_by (the engine, the row, JSON) said user."""
        s = _fleet()
        s.switch_to("5", json_output=True, entered_by="engine")
        old = _settings(temp_home).read_bytes()
        s.switch_to("6", json_output=True)
        _settings(temp_home).write_bytes(old)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", True, "engine")
        assert s.key_entered_by() == "engine"

    def test_a_line_with_neither_a_record_nor_a_tombstone_is_the_users(self, temp_home):
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 6"}), encoding="utf-8"
        )
        assert s.key_entered_by() == "user"
        assert s.key_findings().came_back is False
        assert "came back" not in _notes(s)


class TestARecordWithNoLine:
    """F13, F5: a record whose line is gone is found, said and cleared."""

    def test_entering_writes_the_record_before_the_line(self, temp_home, monkeypatch):
        s = _fleet()
        real = claude_settings.install
        seen = []

        def install(number, *a, **k):
            seen.append(s._key_state().get("enteredBy"))
            return real(number, *a, **k)

        monkeypatch.setattr(claude_settings, "install", install)
        s.switch_to("5", json_output=True, entered_by="engine")
        assert seen == ["engine"]

    def test_a_failed_install_puts_the_old_record_back(self, temp_home, monkeypatch):
        s = _fleet()
        _activate(s)
        s.switch_to("1", json_output=True)
        before = s.key_state_path.read_bytes()

        def fail(*a, **k):
            raise claude_settings.SettingsError("could not be written (EIO)", kind="write")

        monkeypatch.setattr(claude_settings, "install", fail)
        from ai_quota_swap.exceptions import KeyHelperUnavailableError

        with pytest.raises(KeyHelperUnavailableError):
            s.switch_to("6", json_output=True)
        assert s.key_state_path.read_bytes() == before

    def test_a_line_removed_by_hand_is_said_and_cleared(self, temp_home, run):
        s = _fleet()
        _activate(s, entered_by="engine")
        claude_settings.remove(path=_settings(temp_home), remove_file_if_empty=True)
        f = s.key_findings()
        assert f.lost == "5" and f.stale_active == "5"
        notes = _notes(s)
        assert "#5 ci" in notes and "no longer in" in notes
        assert "qswap switch 5" in notes
        check = _check("claude.key_helper")
        assert check is not None and check.status == "warn"
        code, out, err = run(s, "switch", "1", "--json")
        assert code == 0, err
        payload = json.loads(out)
        assert payload["reason"] == "already-active"
        assert any("#5" in w for w in payload["warnings"])
        record = _key_record(s)
        assert "number" not in record and record["left"]["5"]["enteredBy"] == "engine"
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        assert s.key_helper_notes() == []
        # A line for #5 that turns up later is the engine's, come back.
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 5"}), encoding="utf-8"
        )
        assert s.key_entered_by() == "engine"

    def test_with_no_login_underneath_doctor_names_no_placeholder(self, temp_home):
        """End-to-end run R6: the line deleted by hand on a machine with no
        claude.ai login. The activeAccountNumber warning said 'fix: qswap
        switch N', right after the record's warning named qswap switch 5."""
        from ai_quota_swap import doctor

        s = _fleet()
        _activate(s)
        claude_settings.remove(path=_settings(temp_home), remove_file_if_empty=True)
        get_global_config_path().write_text("{}", encoding="utf-8")
        checks = doctor.run_checks()
        active = [c for c in checks if c.id == "claude.active_number"]
        assert active and active[0].fix == "qswap switch 5"
        assert all(c.fix != "qswap switch N" for c in checks if c.fix), [
            (c.id, c.fix) for c in checks
        ]


class TestTheActiveNumberFollowsTheLine:
    """F25: every path that removes the line sets activeAccountNumber back."""

    def test_removing_the_active_key_sets_it_back(self, temp_home, block_real_keychain):
        s = _fleet()
        s.switch_to("7", json_output=True)
        _activate(s)
        s.remove_account("5", assume_yes=True)
        assert s._get_sequence_data()["activeAccountNumber"] == 7
        result = s.switch(json_output=True)
        assert result["switched"] is True and result["to"]["number"] == 1

    def test_the_unmanaged_leave_sets_it_back(self, temp_home):
        s = _fleet()
        _activate(s)
        get_global_config_path().write_text(json.dumps({
            "oauthAccount": {"emailAddress": "new@example.com", "accountUuid": "",
                             "organizationUuid": None},
        }), encoding="utf-8")
        assert s.leave_key_for_unmanaged_login() is not None
        assert s._get_sequence_data()["activeAccountNumber"] is None

    def test_removing_the_login_underneath_warns(self, temp_home, capsys, block_real_keychain):
        s = _fleet()
        _activate(s)
        capsys.readouterr()
        s.remove_account("1", assume_yes=True)
        out = " ".join(capsys.readouterr().out.split())
        assert "#1 personal (maya@example.com) is logged in underneath #5 ci" in out

    def test_removing_an_account_forgets_its_tombstone(self, temp_home, block_real_keychain):
        s = _fleet()
        _activate(s)
        s.switch_to("1", json_output=True)
        assert "5" in _key_record(s)["left"]
        s.remove_account("5", assume_yes=True)
        assert "5" not in (_key_record(s).get("left") or {})


class TestProfilesStillLinkedUnderTheLine:
    """F9: a run profile still linked while the line is in is found, said and
    given its copy by the next switch."""

    def test_a_hand_written_line_over_a_linked_profile(self, temp_home, run):
        s = _fleet()
        d = _profile(s)
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 5", "model": "opus"}),
            encoding="utf-8",
        )
        assert (d / "settings.json").is_symlink()
        assert len(s.key_findings().linked) == 1
        notes = _notes(s)
        assert "still shares" in notes and "qswap switch 5" in notes
        check = _check("claude.profile_settings")
        assert check.status == "fail" and check.fix == "qswap switch 5"
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        assert "Already on #5 ci" in out
        assert not (d / "settings.json").is_symlink()
        assert "apiKeyHelper" not in (d / "settings.json").read_text(encoding="utf-8")
        assert s.key_findings().linked == ()

    def test_two_profiles_get_their_own_copies(self, temp_home, run):
        """The re-check's slip: "Gave 2 qswap run profiles its own copy"."""
        s = _fleet()
        _profile(s, "7", SAM)
        _profile(s, "1", MAYA)
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 5", "model": "opus"}),
            encoding="utf-8",
        )
        assert len(s.key_findings().linked) == 2
        assert s.reconcile_key_helper() == [
            "Gave 2 qswap run profiles their own copies of the settings."
        ]

    def test_a_link_the_manifest_does_not_list_still_counts(self, temp_home):
        """A first launch killed before its manifest was written."""
        from ai_quota_swap.session import SHARE_MANIFEST, SessionManager

        s = _fleet()
        d = _profile(s)
        (d / SHARE_MANIFEST).unlink()
        _activate(s)
        assert not (d / "settings.json").is_symlink()
        # A launch now sees the line and copies, manifest or not.
        dest = d / "settings.json"
        dest.unlink()
        dest.symlink_to(_settings(temp_home))
        (d / SHARE_MANIFEST).unlink(missing_ok=True)
        SessionManager(s)._sync_items(d, ("settings.json",), scope=("settings.json",))
        assert not dest.is_symlink()
        assert "apiKeyHelper" not in dest.read_text(encoding="utf-8")


class TestUnparseableSettingsWithARecord:
    """F52, F5: one sentence everywhere, and fixing the file as the step."""

    def _break(self, s, home):
        _activate(s)
        _settings(home).write_text(
            f'{{"apiKeyHelper": "{QSWAP} key-helper 5",}}\n', encoding="utf-8"
        )

    def test_every_surface_says_the_same(self, temp_home, capsys):
        s = _fleet()
        self._break(s, temp_home)
        notes = _notes(s)
        assert f"{WHERE} cannot be read" in notes
        assert "qswap last made #5 ci the active API key" in notes
        assert f"Fix the JSON in {WHERE}" in notes
        status = " ".join(" ".join(s.status_lines()).split())
        assert "qswap last made #5 ci the active API key" in status
        capsys.readouterr()
        s.list_accounts()
        assert "qswap last made #5 ci" in " ".join(capsys.readouterr().out.split())
        snap = s.accounts_snapshot(fetch=set())
        assert "qswap last made #5 ci" in " ".join(snap.key_notes)
        check = _check("claude.key_helper")
        assert check.status == "fail" and "qswap last made #5 ci" in check.summary
        assert check.fix == f"fix the JSON in {WHERE}"

    @pytest.mark.parametrize("op", ["swap", "move", "add-token"])
    def test_the_guards_name_the_file(self, temp_home, op):
        from ai_quota_swap.exceptions import ValidationError

        s = _fleet()
        self._break(s, temp_home)
        with pytest.raises(ValidationError) as caught:
            if op == "swap":
                s.swap_accounts("5", "6")
            elif op == "move":
                s.move_account("5", "9")
            else:
                s.add_account_from_token(
                    "sk-ant-oat01-" + "z9" * 20, email="new@example.com", slot=5,
                    assume_yes=True,
                )
        text = " ".join(str(caught.value).split())
        assert f"Fix the JSON in {WHERE}" in text
        assert "qswap switch 1" not in text

    @pytest.mark.parametrize("argv", [["switch", "1"], ["switch"], ["switch", "7"]])
    def test_a_switch_is_refused_with_the_same_words(self, temp_home, run, argv):
        s = _fleet()
        self._break(s, temp_home)
        code, out, err = run(s, *argv)
        assert code == 1
        text = " ".join(err.split())
        assert "qswap last made #5 ci the active API key" in text
        assert f"Fix the JSON in {WHERE}" in text
        assert "Already on" not in out

    def test_a_file_that_cannot_be_read_is_not_told_to_fix_its_json(
        self, temp_home, run, monkeypatch
    ):
        """The re-check's case: the settings file with mode 000 (read
        refused, not unparseable). Every surface named fixing the JSON."""
        s = _fleet()
        _activate(s)
        path = _settings(temp_home)
        real = Path.read_bytes

        def refused(self, *a, **k):
            if self == path:
                raise PermissionError(13, "Permission denied")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_bytes", refused)
        notes = _notes(s)
        assert "qswap last made #5 ci the active API key" in notes
        assert f"Make {WHERE} readable, then retry." in notes
        assert "Fix the JSON" not in notes
        code, out, err = run(s, "switch", "1")
        assert code == 1
        text = " ".join(err.split())
        assert f"Make {WHERE} readable, then retry." in text and "Fix the JSON" not in text
        check = _check("claude.key_helper")
        assert check.status == "fail" and check.fix == f"make {WHERE} readable"
        assert "not valid JSON" not in " ".join(c.summary for c in [check])


# == B2: qswap switch N to the active key checks and repairs its line ================


class TestSwitchToTheActiveKeyRepairs:
    def _answers_unless_bare(self, monkeypatch):
        ran: list[str] = []

        def check(command, **kw):
            ran.append(command)
            if command.startswith("qswap "):
                return "runs qswap by name only, which a session without it on its PATH cannot find"
            return None

        monkeypatch.setattr(claude_settings, "check_command", check)
        return ran

    def test_a_line_that_does_not_answer_is_written_again(self, temp_home, run, monkeypatch):
        ran = self._answers_unless_bare(monkeypatch)
        s = _fleet()
        d = _profile(s)
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": "qswap key-helper 5", "model": "opus"}),
            encoding="utf-8",
        )
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        assert "Already on #5 ci (API key)" in out.splitlines()
        text = " ".join(out.split())
        assert "Gave 1 qswap run profile its own copy of the settings." in text
        assert "Wrote the key line for #5 ci again: it runs qswap by name only" in text
        _fits(out)
        assert _line(temp_home) == f"{QSWAP} key-helper 5"
        assert ran[-1] == f"{QSWAP} key-helper 5"  # checked again after the write
        # The record and the run profile too.
        record = _key_record(s)
        assert record["number"] == "5" and record["enteredBy"] == "user"
        assert not (d / "settings.json").is_symlink()

    def test_json_reports_the_repair(self, temp_home, monkeypatch):
        self._answers_unless_bare(monkeypatch)
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": "qswap key-helper 5"}), encoding="utf-8"
        )
        payload = s.switch_to("5", json_output=True)
        assert payload["switched"] is False and payload["reason"] == "already-active"
        assert any("Wrote the key line for #5 ci again" in w for w in payload["warnings"])

    def test_a_line_that_answers_is_left_alone(self, temp_home, monkeypatch):
        s = _fleet()
        _activate(s)
        before = _settings(temp_home).read_bytes()
        payload = s.switch_to("5", json_output=True)
        assert payload["warnings"] == []
        assert _settings(temp_home).read_bytes() == before

    def test_a_key_that_is_gone_is_said_with_the_helpers_next_step(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        from tests.test_key_helper_live import EMAIL_5, SECURITY_SERVICE

        s = _fleet()
        _activate(s)
        block_real_keychain.delete_password(SECURITY_SERVICE, f"account-5-{EMAIL_5}")
        for f in s.credentials_dir.glob(".creds-5-*"):
            f.unlink()
        monkeypatch.setattr(
            claude_settings, "check_command",
            lambda command, **kw: "fails (#5 has no saved key; save one: qswap add-token KEY --slot 5)",
        )
        with pytest.raises(SwitchError, match="has no saved key.*qswap add-token KEY --slot 5"):
            s.switch_to("5", json_output=True)
        assert _line(temp_home) == f"{QSWAP} key-helper 5"


# == B6: what defeats the line is checked when it is written ==========================


SECRET = "do-not-print-this-value"


def _settings_with(home: Path, obj: dict) -> bytes:
    _settings(home).write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    return _settings(home).read_bytes()


class TestTheEnvBlock:
    @pytest.mark.parametrize("name", [
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    ])
    def test_a_variable_that_outranks_the_line_refuses_the_switch(
        self, temp_home, run, name
    ):
        s = _fleet()
        value = SECRET if not name.startswith("CLAUDE_CODE_USE") else "1"
        before = _settings_with(temp_home, {"env": {name: value}, "model": "opus"})
        code, out, err = run(s, "switch", "5")
        assert code == 1
        text = " ".join(err.split())
        assert name in text and SECRET not in text
        assert f"Remove {name} from the env block of {WHERE}, then: qswap switch 5" in text
        assert "--log-out-sessions" not in text
        _fits(err)
        assert _settings(temp_home).read_bytes() == before
        assert s.current_account_number() == "1"
        code, out, _err = run(s, "switch", "5", "--json")
        payload = json.loads(out)
        assert payload["error"]["type"] == "KeyHelperUnavailableError"
        assert payload["error"]["logOutSessions"] is False
        assert SECRET not in out

    @pytest.mark.parametrize("names, joined", [
        (["ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK"],
         "ANTHROPIC_API_KEY and CLAUDE_CODE_USE_BEDROCK"),
        (["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_VERTEX"],
         "ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY and CLAUDE_CODE_USE_VERTEX"),
    ])
    def test_several_variables_one_verb_one_joiner(self, temp_home, run, names, joined):
        """The re-check's slips: "settings.json set A and B" (the verb
        followed the variables, not the file), and three ways of joining
        the names (refusal, doctor, JSON reason)."""
        s = _fleet()
        _settings_with(temp_home, {"env": {n: "1" for n in names}})
        code, out, err = run(s, "switch", "5")
        assert f"{WHERE} sets {joined} in its env block" in " ".join(err.split())
        code, out, _err = run(s, "switch", "5", "--json")
        assert f"sets {joined} in its env block" in json.loads(out)["error"]["keyHelper"]
        check = _check("claude.settings_env")
        assert f"{WHERE} sets {joined} in its env block" in check.summary
        assert check.fix == f"remove {joined} from the env block of {WHERE}"

    @pytest.mark.parametrize("value", ["", "0", "false"])
    def test_a_provider_switch_turned_off_is_no_refusal(self, temp_home, value):
        s = _fleet()
        _settings_with(temp_home, {"env": {"CLAUDE_CODE_USE_BEDROCK": value}})
        assert s.switch_to("5", json_output=True)["switched"] is True

    def test_the_engine_treats_it_as_the_helper_unavailable(self, temp_home):
        from ai_quota_swap.autoswitch import NoSwitchEvent, TickOutcome
        from tests.test_key_helper_live import EXHAUSTED, _harness

        h = _harness(temp_home)
        _settings_with(temp_home, {"env": {"ANTHROPIC_AUTH_TOKEN": SECRET}})
        assert h.switcher.key_helper_available() is False
        assert h.tick_with_usage(EXHAUSTED) is TickOutcome.BLOCKED
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert held.reason == "key-helper-unavailable"
        assert h.switcher.current_account_number() == "1"

    def test_a_base_url_goes_ahead_and_names_the_host(self, temp_home, run):
        s = _fleet()
        _settings_with(temp_home, {"env": {
            "ANTHROPIC_BASE_URL": f"https://user:{SECRET}@gateway.example.com/v1",
        }})
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        text = " ".join((out + err).split())
        assert "ANTHROPIC_BASE_URL" in text and "gateway.example.com" in text
        assert SECRET not in text
        assert s.current_account_number() == "5"
        s.switch_to("1", json_output=True)
        payload = s.switch_to("5", json_output=True)
        assert any("gateway.example.com" in w for w in payload["warnings"])

    def test_the_anthropic_api_is_not_warned_about(self, temp_home):
        s = _fleet()
        _settings_with(temp_home, {"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}})
        assert s.switch_to("5", json_output=True)["warnings"] == []


class TestAnotherHelper:
    """F47: another apiKeyHelper is what sessions use now; the one way
    forward is to remove it, and --log-out-sessions is not offered."""

    def test_the_refusal_says_sessions_use_it_and_names_one_way(
        self, temp_home, run, alive
    ):
        from tests.test_key_helper_live import _write_sessions

        s = _fleet()
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        before = _settings_with(temp_home, {"apiKeyHelper": "/opt/vault/print-key"})
        code, out, err = run(s, "switch", "5")
        assert code == 1
        text = " ".join(err.split())
        assert "already sets another apiKeyHelper" in text
        assert "every Claude Code session uses it now" in text
        assert "Not logged in" not in text
        assert "--log-out-sessions" not in text
        assert err.rstrip().endswith(
            f"Remove apiKeyHelper from {WHERE}, then: qswap switch 5"
        )
        _fits(err)
        assert _settings(temp_home).read_bytes() == before

    def test_log_out_sessions_is_refused_while_it_is_set(
        self, temp_home, block_real_keychain, run
    ):
        from tests.test_key_helper_live import _claude_state

        s = _fleet()
        _settings_with(temp_home, {"apiKeyHelper": "/opt/vault/print-key"})
        before = _claude_state(block_real_keychain)
        code, out, err = run(s, "switch", "5", "--log-out-sessions")
        assert code == 1
        text = " ".join(err.split())
        assert "outranks a saved key" in text
        assert f"Remove apiKeyHelper from {WHERE}, then: qswap switch 5" in text
        assert _claude_state(block_real_keychain) == before
        assert s.current_account_number() == "1"

    @pytest.mark.parametrize("env", [
        {"ANTHROPIC_AUTH_TOKEN": "t"},
        {"ANTHROPIC_API_KEY": "sk-ant-api03-other", "CLAUDE_CODE_USE_BEDROCK": "1"},
    ])
    def test_log_out_sessions_is_refused_for_a_variable_too(
        self, temp_home, block_real_keychain, run, env
    ):
        """The re-check's case: a credential or provider variable in the env
        block outranks a saved key as well, so the older way would log every
        session out and leave them on the variable (decision B6)."""
        from tests.test_key_helper_live import _claude_state

        s = _fleet()
        before_file = _settings_with(temp_home, {"env": env})
        before = _claude_state(block_real_keychain)
        code, out, err = run(s, "switch", "5", "--log-out-sessions")
        assert code == 1
        text = " ".join(err.split())
        names = " and ".join(sorted(env)) if len(env) > 1 else next(iter(env))
        assert "Claude Code obeys before any key line" in text, text
        assert names.split(" and ")[0] in text
        assert "--log-out-sessions" not in text
        assert "sk-ant-api03-other" not in out + err and "'t'" not in out + err
        assert _claude_state(block_real_keychain) == before
        assert _settings(temp_home).read_bytes() == before_file
        assert s.current_account_number() == "1"
        code, out, _err = run(s, "switch", "5", "--log-out-sessions", "--json")
        assert code == 1 and json.loads(out)["error"]["logOutSessions"] is False

    def test_the_menu_bar_does_not_ask_about_the_saved_key(self, temp_home):
        from ai_quota_swap import menubar
        from ai_quota_swap.exceptions import KeyHelperUnavailableError

        s = _fleet()
        _settings_with(temp_home, {"apiKeyHelper": "/opt/vault/print-key"})
        with pytest.raises(KeyHelperUnavailableError) as caught:
            _activate(s)
        assert menubar.api_key_switch_question(caught.value) is None
        _settings(temp_home).write_text("{not json", encoding="utf-8")
        with pytest.raises(KeyHelperUnavailableError) as caught:
            _activate(s)
        assert menubar.api_key_switch_question(caught.value) is not None

    def test_an_unparseable_file_names_the_fix_before_the_override(self, temp_home, run):
        s = _fleet()
        _settings(temp_home).write_text("{not json", encoding="utf-8")
        code, out, err = run(s, "switch", "5")
        assert code == 1
        text = " ".join(err.split())
        fix = text.index(f"Fix the JSON in {WHERE}, then: qswap switch 5")
        assert fix < text.index("qswap switch 5 --log-out-sessions")
        _fits(err)


class TestTheOAuthTokenVariable:
    """F62: the key line outranks CLAUDE_CODE_OAUTH_TOKEN."""

    def test_the_switch_onto_a_key_says_such_sessions_follow_it(
        self, temp_home, run, monkeypatch
    ):
        s = _fleet()
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
        with patch.object(display, "OVERRIDE_ENV_VARS", display.OVERRIDE_ENV_VARS):
            from ai_quota_swap import cli

            capture = []
            monkeypatch.setattr(cli, "warning", lambda msg, **k: capture.append(msg))
            code, out, err = run(s, "switch", "5")
        assert code == 0, err
        text = " ".join(" ".join(capture).split())
        assert "ignores the switch" not in text
        assert "CLAUDE_CODE_OAUTH_TOKEN" in text and "uses the key too" in text

    def test_doctor_says_such_sessions_follow_the_key(self, temp_home, monkeypatch):
        s = _fleet()
        _activate(s)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
        check = _check("claude.env")
        assert check is not None and "uses #5 ci" in check.summary
        assert "instead of the active account" not in check.summary


class TestDoctorReadsTheOtherLayers:
    def _probes(self, tmp_path, managed: Path):
        from dataclasses import replace as with_probe

        from ai_quota_swap import doctor

        return with_probe(
            doctor.default_probes(), cwd=lambda: tmp_path / "project",
            managed_settings_path=lambda: managed,
        )

    @pytest.mark.parametrize("where", ["managed", "project", "local"])
    def test_a_helper_or_variable_there_is_warned_about(self, temp_home, tmp_path, where):
        from ai_quota_swap import doctor

        _fleet()
        project = tmp_path / "project" / ".claude"
        project.mkdir(parents=True)
        managed = tmp_path / "managed-settings.json"
        target = {
            "managed": managed, "project": project / "settings.json",
            "local": project / "settings.local.json",
        }[where]
        target.write_text(json.dumps({"apiKeyHelper": "/opt/x", "env": {
            "ANTHROPIC_AUTH_TOKEN": SECRET}}), encoding="utf-8")
        checks = doctor.run_checks(self._probes(tmp_path, managed))
        found = [c for c in checks if c.id == "claude.other_settings"]
        assert len(found) == 1 and found[0].status == "warn"
        assert "apiKeyHelper" in found[0].summary
        assert "ANTHROPIC_AUTH_TOKEN" in found[0].summary
        assert SECRET not in json.dumps(doctor.to_json(checks, online=False))

    def test_the_path_is_never_broken_or_cut(self, temp_home, tmp_path):
        """The re-check's case: the managed file's path split at its space,
        and two long project paths cut to the same "…/billing-api/.claude…"
        so neither file was named in full (ux.md: a single path is never
        broken, and goes on a line of its own)."""
        from ai_quota_swap import doctor

        _fleet()
        deep = tmp_path / "dev" / "clients" / "northwind-labs" / "platform" / "services"
        project = deep / "billing-api" / ".claude"
        project.mkdir(parents=True)
        managed = tmp_path / "Library" / "Application Support" / "ClaudeCode" / (
            "managed-settings.json"
        )
        managed.parent.mkdir(parents=True)
        managed.write_text(json.dumps({"apiKeyHelper": "/opt/x"}), encoding="utf-8")
        (project / "settings.json").write_text(json.dumps({"apiKeyHelper": "/opt/x"}),
                                               encoding="utf-8")
        (project / "settings.local.json").write_text(
            json.dumps({"env": {"ANTHROPIC_API_KEY": SECRET}}), encoding="utf-8")
        from dataclasses import replace as with_probe

        probes = with_probe(doctor.default_probes(), cwd=lambda: deep / "billing-api",
                            managed_settings_path=lambda: managed)
        checks = [c for c in doctor.run_checks(probes) if c.id == "claude.other_settings"]
        assert len(checks) == 3
        lines = [line for c in checks for line in doctor._check_lines(c, 80)]
        shown_paths = {
            doctor._abbr(path, temp_home)
            for path in (managed, project / "settings.json", project / "settings.local.json")
        }
        for shown in shown_paths:
            assert any(line.strip() == shown for line in lines), (shown, lines)
        for line in lines:
            assert "…" not in line, line
            if line.strip() not in shown_paths:  # a path stays whole, even past 80
                assert len(line) <= 80, line
        summaries = " ".join(c.summary for c in checks)
        assert "managed settings" in summaries and "local settings" in summaries
        assert {c.to_json()["path"] for c in checks} == {
            doctor._abbr(p, temp_home)
            for p in (managed, project / "settings.json", project / "settings.local.json")
        }

    def test_the_user_settings_env_block_is_reported(self, temp_home):
        _fleet()
        _settings_with(temp_home, {"env": {"ANTHROPIC_API_KEY": SECRET}})
        check = _check("claude.settings_env")
        assert check is not None and check.status == "warn"
        assert "ANTHROPIC_API_KEY" in check.summary and SECRET not in check.summary


# == B7: changing accounts while a key is active =====================================


NEW_LOGIN = {"oauthAccount": {"emailAddress": "new@example.com", "accountUuid": "",
                              "organizationUuid": None}}


class TestAccountsChangeUnderAKey:
    def test_add_token_cannot_renumber_the_key_by_its_address(self, temp_home):
        """F2: the migrate path of add-token, by --email."""
        from ai_quota_swap.exceptions import ValidationError

        s = _fleet()
        s.add_account_from_token(KEY_6.replace("k6", "k8"), email="ci@corp.example", slot=8)
        s.switch_to("8", json_output=True)
        before = s.sequence_file.read_bytes()
        with pytest.raises(ValidationError) as caught:
            s.add_account_from_token(
                KEY_6.replace("k6", "k9"), email="ci@corp.example", slot=9, assume_yes=True,
            )
        text = " ".join(str(caught.value).split())
        assert "This would renumber #8" in text and "the active API key" in text
        assert "qswap switch 1" in text
        assert s.sequence_file.read_bytes() == before
        assert _line(temp_home) == f"{QSWAP} key-helper 8"

    @pytest.mark.parametrize("slot", [5, None])
    def test_a_new_key_for_the_active_account_says_when_sessions_take_it(
        self, temp_home, capsys, slot
    ):
        """F60: the line is the same, so Claude Code's cached key stands."""
        s = _fleet()
        _activate(s)
        capsys.readouterr()
        email = None if slot else "api-key-5@token.local"
        s.add_account_from_token(KEY_6.replace("k6", "k8"), slot=slot, email=email,
                                 assume_yes=True)
        out = capsys.readouterr().out
        text = " ".join(out.split())
        assert (
            "Running Claude Code sessions pick up the new key within 5 minutes, or at "
            "once after a request is rejected."
        ) in text
        _fits(out)

    def test_a_new_key_for_another_account_says_nothing_more(self, temp_home, capsys):
        s = _fleet()
        _activate(s)
        capsys.readouterr()
        s.add_account_from_token(KEY_6.replace("k6", "k8"), slot=6, assume_yes=True)
        assert "within 5 minutes" not in capsys.readouterr().out

    def test_leaving_for_a_login_the_store_no_longer_holds_switches_it_in(
        self, temp_home, block_real_keychain
    ):
        """F26: ~/.claude.json still names #1, but the login item has only
        MCP sign-ins: the line alone would leave every session logged out."""
        from tests.test_key_helper_live import MCP, _acct
        from ai_quota_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE

        s = _fleet()
        _activate(s)
        block_real_keychain.set_password(
            CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(), json.dumps({"mcpOAuth": MCP})
        )
        result = s.switch_to("1", json_output=True)
        assert result["switched"] is True
        live = json.loads(block_real_keychain.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct()))
        assert "claudeAiOauth" in live
        assert _line(temp_home) is None
        assert "within about 30 seconds" in " ".join(result["followup"])

    def test_leaving_for_the_login_underneath_still_touches_no_login(
        self, temp_home, block_real_keychain
    ):
        from tests.test_key_helper_live import _claude_state

        s = _fleet()
        _activate(s)
        before = _claude_state(block_real_keychain)
        s.switch_to("1", json_output=True)
        assert _claude_state(block_real_keychain) == before


class TestTheLoginUnderAKeyOnTheSurfaces:
    """F27: the login a surface offers to add is the one underneath."""

    def _view(self, s):
        from ai_quota_swap.tui import data as tui_data

        snap = s.accounts_snapshot(fetch=set())
        return tui_data.ProviderView(
            provider="claude", snapshot=snap,
            live_email=tui_data.unmanaged_login_email(s, snap),
        )

    def test_the_dashboard_offers_the_login_underneath(self, temp_home):
        from ai_quota_swap.tui import data as tui_data

        s = _fleet()
        _activate(s)
        view = self._view(s)
        assert tui_data.login_email(view) == MAYA
        assert view.live_email is None

    def test_an_unmanaged_login_under_a_key_is_offered_everywhere(self, temp_home):
        from ai_quota_swap import menubar, menubar_data
        from ai_quota_swap.tui import data as tui_data

        s = _fleet()
        _activate(s)
        get_global_config_path().write_text(json.dumps(NEW_LOGIN), encoding="utf-8")
        view = self._view(s)
        assert view.live_email == "new@example.com"
        assert tui_data.login_email(view) == "new@example.com"
        snap = s.accounts_snapshot(fetch=set())
        state = menubar.ProviderState(
            "claude", menubar_data.account_states(snap, "claude", {}, now=time.time()),
            live_email=s.live_login_email(),
        )
        assert state.unmanaged_live == "new@example.com"
        status = " ".join(" ".join(s.status_lines()).split())
        assert "new@example.com · underneath · not managed · add it with: qswap add" in status

    def test_a_managed_login_under_a_key_is_not_unmanaged(self, temp_home):
        from ai_quota_swap import menubar, menubar_data

        s = _fleet()
        _activate(s)
        snap = s.accounts_snapshot(fetch=set())
        state = menubar.ProviderState(
            "claude", menubar_data.account_states(snap, "claude", {}, now=time.time()),
            live_email=s.live_login_email(),
        )
        assert state.unmanaged_live is None


# == B8: who entered the key is shown =================================================


class TestWhoEnteredTheKey:
    """F33: auto-switch's key says so everywhere; with no engine running one
    line adds that nothing watches its limit, and how to go back."""

    def _engine_key(self, temp_home):
        from tests.test_key_helper_live import EXHAUSTED, _harness

        h = _harness(temp_home)
        h.tick_with_usage(EXHAUSTED)
        assert h.switcher.key_entered_by() == "engine"
        return h.switcher

    def _beat(self, s):
        from ai_quota_swap.autoswitch import write_heartbeat

        now = time.time()
        write_heartbeat(s.backup_dir, mode="loop", dry_run=False, last_tick_at=now,
                        next_tick_at=now + 60, started_at=now, host="test")

    def test_list_status_and_json_say_auto_switch_entered_it(self, temp_home, capsys):
        s = self._engine_key(temp_home)
        self._beat(s)
        capsys.readouterr()
        s.list_accounts(fetch=set())
        out = capsys.readouterr().out
        row = next(line for line in out.splitlines() if line.startswith("  #2 "))
        assert row.endswith("· by auto-switch")
        assert s.status_lines()[1].endswith("· by auto-switch")
        assert "nothing watches" not in out
        rows = {r["number"]: r for r in s.list_accounts(json_output=True, fetch=set())["accounts"]}
        assert rows[2]["enteredBy"] == "engine"
        assert s._build_status_payload()["active"]["enteredBy"] == "engine"

    def test_with_no_engine_running_one_line_says_nothing_watches(self, temp_home, capsys):
        s = self._engine_key(temp_home)
        notes = _notes(s)
        assert "Auto-switch entered #2 ci and is not running, so nothing watches its limit" in notes
        assert "To go back: qswap switch 1" in notes
        capsys.readouterr()
        s.list_accounts(fetch=set())
        out = capsys.readouterr().out
        assert "nothing watches its limit" in " ".join(out.split())
        _fits(out)
        snap = s.accounts_snapshot(fetch=set())
        assert snap.key_entered_by == "engine"
        assert "nothing watches its limit" in " ".join(snap.key_notes)
        check = _check("claude.key_watch")
        assert check is not None and check.status == "warn"
        assert check.fix == "qswap switch 1"

    def test_the_menu_bar_and_the_dashboard_say_it(self, temp_home):
        from ai_quota_swap import menubar, menubar_data
        from ai_quota_swap.tui import data as tui_data

        s = self._engine_key(temp_home)
        snap = s.accounts_snapshot(fetch=set())
        states = {a.number: a for a in menubar_data.account_states(snap, "claude", {}, now=time.time())}
        row = menubar.account_row(states["2"], now=time.time(), stale_after_s=600)
        assert "by auto-switch" in row.text
        view = tui_data.ProviderView(provider="claude", snapshot=snap)
        block = tui_data.block_for(view, view.account("2"))
        from ai_quota_swap import display as d

        assert "by auto-switch" in d.header_parts(block).plain()

    def test_a_key_the_user_chose_says_nothing_of_the_sort(self, temp_home, capsys):
        s = _fleet()
        _activate(s)
        capsys.readouterr()
        s.list_accounts()
        out = capsys.readouterr().out
        assert "auto-switch" not in out
        assert _check("claude.key_watch") is None
        rows = {r["number"]: r for r in s.list_accounts(json_output=True)["accounts"]}
        assert rows[5]["enteredBy"] == "user"


# == B9: words ========================================================================


class TestWords:
    def test_the_switch_names_remote_control_and_the_way_back(self, temp_home, run):
        """F57, F56 (3)."""
        s = _fleet()
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        text = " ".join(out.split())
        assert (
            "claude.ai connectors and Remote Control are unavailable while a key is in use."
        ) in text
        assert text.endswith("Go back with: qswap switch 1")
        _fits(out)

    def test_from_a_login_qswap_does_not_manage_it_is_named(self, temp_home, run):
        s = _fleet()
        get_global_config_path().write_text(json.dumps(NEW_LOGIN), encoding="utf-8")
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        text = " ".join(out.split())
        assert "new@example.com stays logged in underneath." in text
        assert "Go back with" not in text

    def test_key_to_key_names_the_way_back_too(self, temp_home, run):
        s = _fleet()
        _activate(s)
        code, out, err = run(s, "switch", "6")
        assert code == 0, err
        assert " ".join(out.split()).endswith("Go back with: qswap switch 1")

    def test_one_name_for_the_line(self, temp_home, run, capsys):
        """F56 (1): "key line", in the refusal, doctor and purge."""
        s = _fleet()
        _settings(temp_home).write_text("{not json", encoding="utf-8")
        code, out, err = run(s, "switch", "5")
        assert "qswap cannot add the key line for #5 ci" in " ".join(err.split())
        assert "key helper" not in err
        _settings(temp_home).unlink()
        _activate(s)
        check = _check("claude.key_helper")
        assert "through qswap's key line in" in check.summary
        _settings(temp_home).write_text("{broken", encoding="utf-8")
        capsys.readouterr()
        s.purge(assume_yes=True)
        captured = capsys.readouterr()
        text = " ".join((captured.out + captured.err).split())
        assert "could not check it for its key line" in text
        assert "apiKeyHelper line" not in text

    @pytest.mark.parametrize("case", ["swap", "remove", "adopt"])
    def test_the_long_messages_fit_80_columns(self, temp_home, run, case):
        """F51: the printed forms, not the strings alone."""
        s = _fleet()
        s.set_alias("5", "a-rather-long-alias-name")
        if case == "adopt":
            _activate(s, entered_by="engine")
            code, out, err = run(s, "switch", "5")
            assert code == 0, err
            assert "Auto-switch keeps it now" in " ".join(out.split())
        else:
            _activate(s)
            if case == "swap":
                code, out, err = run(s, "swap", "5", "6")
                assert "This would renumber" in err
            else:
                _settings(temp_home).write_text("{broken", encoding="utf-8")
                code, out, err = run(s, "remove", "5", "--yes")
                assert "The key line cannot be removed" in " ".join(err.split())
            assert code == 1
        for line in (out + err).splitlines():
            assert len(line) <= 80, line

    def test_help_key_helper_prints_its_help(self, temp_home, capsys):
        from ai_quota_swap import key_helper
        from tests.test_key_helper_live import _main

        capsys.readouterr()
        assert _main(["help", "key-helper"]) == 0
        assert capsys.readouterr().out == key_helper.HELP


class TestStatusNotesFit:
    def test_an_indented_note_still_fits_80_columns(self, temp_home):
        s = _fleet()
        s.set_alias("5", "xxx")  # wrapped at 80, its first line is 79 wide
        _activate(s, entered_by="engine")
        lines = s.status_lines()
        assert "nothing watches its limit" in " ".join(" ".join(lines).split())
        for line in lines:
            assert len(line) <= 80, line


class TestPrintedFormsFit:
    """The re-check's five lines past 80 columns, printed where they are
    printed (through cli.main): the 80-column tests built their strings in
    display.py and did not see these print sites."""

    def test_a_cleared_record(self, temp_home, run):
        s = _fleet()
        _activate(s)
        _settings(temp_home).write_text("{}", encoding="utf-8")  # the line deleted by hand
        code, out, err = run(s, "switch", "1")
        assert code == 0, err
        assert "Cleared the record of #5" in " ".join(out.split())
        _fits(out)

    def test_the_notice_about_an_unmanaged_login_underneath(self, temp_home, run, monkeypatch):
        s = _fleet()
        _activate(s)
        get_global_config_path().write_text(json.dumps({"oauthAccount": {
            "emailAddress": "maya.long-name-for-tests@example.com", "accountUuid": "",
            "organizationUuid": None,
        }}), encoding="utf-8")
        monkeypatch.setattr(s, "add_account", lambda *a, **k: None)
        code, out, err = run(s, "switch")
        assert "was not managed" in " ".join(out.split())
        _fits(out)

    @pytest.mark.parametrize("older_way", [False, True])
    def test_the_import_notes_for_a_replaced_active_key(
        self, temp_home, run, older_way, capsys
    ):
        s = _fleet()
        if older_way:
            s.switch_to("5", json_output=True, log_out_sessions=True)
        else:
            _activate(s)
        out_file = temp_home / "k.qswap"
        code, _out, err = run(s, "export", str(out_file), "--account", "5")
        assert code == 0, err
        env = json.loads(out_file.read_text())
        blob = json.dumps(env["accounts"][0]["credentials"]).replace(KEY_5, KEY_6)
        env["accounts"][0]["credentials"] = json.loads(blob)
        out_file.write_text(json.dumps(env))
        code, out, err = run(s, "import", str(out_file), "--force")
        assert code == 0, err
        assert "was replaced" in " ".join(err.split())
        _fits(err)
        if older_way:
            assert any(line.strip() == "qswap switch 5 --force" for line in err.splitlines())

    def test_the_credit_list(self, temp_home, run):
        from ai_quota_swap import api_spend

        s = _fleet()
        s.set_alias("5", "build-server-ci")
        api_spend.set_credit(s, "5", "1187.03")
        api_spend.set_budget(s, "5", 20)
        reading = {
            "spend": {"used": 25.22, "limit": 20.0, "pct": 126.1, "currency": "USD"},
            "today": 2.06, "source": "ledger", "month": "2026-11", "as_of": time.time(),
            "unpriced_tokens": 0,
            "credit": {"amount": 1187.03, "used": 25.22, "left": 1161.81, "spent": False,
                       "expired": False, "since": "2026-11-01T00:00:00Z", "expires": None,
                       "source": "ledger", "unpriced_tokens": 0},
        }
        with patch("ai_quota_swap.api_spend.readings", return_value={"5": reading}):
            code, out, err = run(s, "credit")
        assert code == 0, err
        assert "build-server-ci" in out
        _fits(out)


class TestRemovingAKeyThatRunSessionsHold:
    def test_the_warning_says_they_keep_the_key(self, temp_home, capsys, monkeypatch):
        """F56 (7): a session that holds a key keeps sending it when its
        helper stops giving one (Claude Code 2.1.322)."""
        s = _fleet()
        s.record_key_run("5", 4242)
        monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive", lambda pid: pid == 4242)
        capsys.readouterr()
        s.remove_account("5", assume_yes=True)
        out = capsys.readouterr().out
        text = " ".join(out.split())
        assert "1 session started with qswap run 5 (pid 4242) uses this key" in text
        assert "keeps using it after it is removed, until you exit it" in text
        assert "apiKeyHelper script is failing" not in text
        _fits(out)
