"""Round 2 of pull request #13 (the API-key switch), the switching side:
decisions B1, B2 (the active key's line repaired), B5 to B9 of
pr13-round2-decisions.md. Everything runs on the temporary home, the
in-memory Keychain fake and faked network, as tests/test_key_helper_live.py
does, whose fleet and helpers these tests share.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import claude_settings, display
from ai_quota_swap.exceptions import SwitchError
from ai_quota_swap.models import Platform
from ai_quota_swap.paths import get_global_config_path
from tests.test_key_helper_live import (
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


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    """The installed command every line names, taken to exist and answer
    (as in tests/test_key_helper_live.py)."""
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)
    monkeypatch.setattr(claude_settings, "check_command", lambda command, **kw: None)


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
        assert "~/.claude/settings.json cannot be read" in notes
        assert "qswap last made #5 ci the active API key" in notes
        assert "Fix the JSON in ~/.claude/settings.json" in notes
        status = " ".join(" ".join(s.status_lines()).split())
        assert "qswap last made #5 ci the active API key" in status
        capsys.readouterr()
        s.list_accounts()
        assert "qswap last made #5 ci" in " ".join(capsys.readouterr().out.split())
        snap = s.accounts_snapshot(fetch=set())
        assert "qswap last made #5 ci" in " ".join(snap.key_notes)
        check = _check("claude.key_helper")
        assert check.status == "fail" and "qswap last made #5 ci" in check.summary
        assert check.fix == "fix the JSON in ~/.claude/settings.json"

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
        assert "Fix the JSON in ~/.claude/settings.json" in text
        assert "qswap switch 1" not in text

    @pytest.mark.parametrize("argv", [["switch", "1"], ["switch"], ["switch", "7"]])
    def test_a_switch_is_refused_with_the_same_words(self, temp_home, run, argv):
        s = _fleet()
        self._break(s, temp_home)
        code, out, err = run(s, *argv)
        assert code == 1
        text = " ".join(err.split())
        assert "qswap last made #5 ci the active API key" in text
        assert "Fix the JSON in ~/.claude/settings.json" in text
        assert "Already on" not in out


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
