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
