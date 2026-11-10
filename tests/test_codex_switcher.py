"""CodexSwitcher: registry, capture, removal, the switch procedure, usage, engine members.

Everything Codex-side is synthetic: login records built from fake claims, a
temp ``CODEX_HOME``, a faked usage fetch, faked process detection and restart.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_quota_swap import locking, oauth
from ai_quota_swap.codex import auth as codex_auth
from ai_quota_swap.codex import processes as codex_processes
from ai_quota_swap.codex import usage as codex_usage
from ai_quota_swap.codex.processes import CodexProcess, RestartResult
from ai_quota_swap.codex.switcher import CodexSwitcher
from ai_quota_swap.codex.usage import UsageOutcome
from ai_quota_swap.exceptions import (
    AccountNotFoundError,
    CodexLoginError,
    ConfigError,
    CredentialFormatError,
    FeatureUnavailableError,
    RetryableSwitchError,
    SwitchError,
    ValidationError,
)
from ai_quota_swap.json_output import (
    USAGE_NO_CREDENTIALS,
    USAGE_TOKEN_EXPIRED,
    USAGE_UNKNOWN_UNTIL_USED,
)
from ai_quota_swap.ledger.activations import ActivationLog, identity_key
from ai_quota_swap.settings import set_setting
from tests.codex_helpers import (
    LOGIN_A,
    LOGIN_B,
    LOGIN_C,
    FakeFetch,
    make_login,
    weekly_usage,
    write_live,
)


@pytest.fixture
def env(temp_home: Path, tmp_path: Path, monkeypatch) -> SimpleNamespace:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return SimpleNamespace(home=temp_home, codex_home=codex_home.resolve())


@pytest.fixture
def sw(env) -> CodexSwitcher:
    return CodexSwitcher()


@pytest.fixture
def fetch(monkeypatch) -> FakeFetch:
    fake = FakeFetch({
        "acct-a": UsageOutcome(usage=weekly_usage(40)),
        "acct-b": UsageOutcome(usage=weekly_usage(10)),
        "acct-c": UsageOutcome(usage=weekly_usage(70)),
    })
    monkeypatch.setattr(codex_usage, "fetch_usage", fake)
    return fake


def live_path(env) -> Path:
    return env.codex_home / "auth.json"


def capture(sw: CodexSwitcher, env, raw: bytes, **kwargs) -> None:
    write_live(env.codex_home, raw)
    sw.add_account(**kwargs)


@pytest.fixture
def two(sw, env, fetch, capsys):
    """B saved as 1, A saved as 2 and live."""
    b = make_login(**LOGIN_B)
    a = make_login(**LOGIN_A)
    capture(sw, env, b)
    capture(sw, env, a)
    capsys.readouterr()
    return SimpleNamespace(a=a, b=b)


def registry(sw) -> dict:
    return json.loads(sw.sequence_file.read_text())


# -- capture ----------------------------------------------------------------------


class TestAdd:
    def test_first_add_saves_the_record_byte_for_byte(self, sw, env, capsys):
        raw = make_login(**LOGIN_A)
        capture(sw, env, raw)
        out = capsys.readouterr().out
        assert "Added #1 a@example.com" in out
        assert "qswap codex login" in out  # how to add the next one safely
        saved = sw.credentials_dir / "account-1.json"
        assert saved.read_bytes() == raw
        if sys.platform != "win32":
            assert stat.S_IMODE(saved.stat().st_mode) == 0o600
            assert stat.S_IMODE(sw.credentials_dir.stat().st_mode) == 0o700
        data = registry(sw)
        assert data["sequence"] == [1]
        assert data["activeAccountNumber"] == 1
        record = data["accounts"]["1"]
        assert record["email"] == "a@example.com"
        assert record["uuid"] == "user-a"
        assert record["organizationUuid"] == "acct-a"

    def test_adding_a_managed_login_again_updates_it(self, sw, env, capsys):
        capture(sw, env, make_login(**LOGIN_A))
        renewed = make_login(**LOGIN_A, access_suffix="renewed")
        capture(sw, env, renewed)
        out = capsys.readouterr().out
        assert "Updated the saved login of #1 a@example.com" in out
        data = registry(sw)
        assert list(data["accounts"]) == ["1"] and data["sequence"] == [1]
        assert (sw.credentials_dir / "account-1.json").read_bytes() == renewed

    def test_a_second_account_gets_the_next_number(self, sw, env):
        capture(sw, env, make_login(**LOGIN_A))
        capture(sw, env, make_login(**LOGIN_B))
        assert registry(sw)["sequence"] == [1, 2]
        assert sw.account_email("2") == "b@example.com"

    def test_one_person_in_two_workspaces_is_two_accounts(self, sw, env):
        capture(sw, env, make_login(email="a@example.com", user_id="user-a", account_id="acct-personal"))
        capture(sw, env, make_login(email="a@example.com", user_id="user-a", account_id="acct-team", plan="team"))
        assert registry(sw)["sequence"] == [1, 2]
        with pytest.raises(ConfigError, match="ambiguous"):
            sw.resolve_account("a@example.com")

    def test_add_with_alias(self, sw, env):
        capture(sw, env, make_login(**LOGIN_A), alias="Work")
        assert registry(sw)["accounts"]["1"]["alias"] == "work"
        with pytest.raises(ConfigError, match="already used"):
            capture(sw, env, make_login(**LOGIN_B), alias="work")
        with pytest.raises(ValidationError):
            capture(sw, env, make_login(**LOGIN_B), alias="123")

    def test_add_into_an_occupied_slot_asks_first(self, sw, env, capsys):
        capture(sw, env, make_login(**LOGIN_A))
        b = make_login(**LOGIN_B)
        write_live(env.codex_home, b)
        with patch("builtins.input", return_value="n"), patch("ai_quota_swap.prompts.interactive", return_value=True):
            sw.add_account(slot=1)
        assert "Cancelled" in capsys.readouterr().out
        assert sw.account_email("1") == "a@example.com"
        with patch("builtins.input", return_value="y"), patch("ai_quota_swap.prompts.interactive", return_value=True):
            sw.add_account(slot=1)
        assert sw.account_email("1") == "b@example.com"
        assert (sw.credentials_dir / "account-1.json").read_bytes() == b

    def test_add_to_another_slot_moves_the_account_with_its_settings(self, sw, env):
        capture(sw, env, make_login(**LOGIN_A))
        sw.set_alias("1", "main")
        sw.set_account_disabled("1", True)
        sw.add_account(slot=5)
        data = registry(sw)
        assert data["sequence"] == [5]
        assert data["accounts"]["5"]["alias"] == "main"
        assert data["accounts"]["5"]["disabled"] is True
        assert not (sw.credentials_dir / "account-1.json").exists()

    def test_no_login(self, sw):
        with pytest.raises(CodexLoginError, match="No Codex login found"):
            sw.add_account()

    def test_api_key_login_is_refused(self, sw, env):
        with pytest.raises(CodexLoginError, match="API key"):
            capture(sw, env, make_login(**LOGIN_A, legacy=True, api_key="sk-test-not-real"))
        assert not sw.sequence_file.exists()

    def test_keyring_mode_is_refused(self, sw, env):
        (env.codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        with pytest.raises(CodexLoginError, match="file storage only"):
            capture(sw, env, make_login(**LOGIN_A))


# -- registry ---------------------------------------------------------------------


class TestRegistry:
    def test_remove_deletes_only_the_saved_copy_and_never_runs_codex(self, two, sw, env, capsys):
        def no_subprocess(*a, **k):
            raise AssertionError("remove must not start any process")

        before_live = live_path(env).read_bytes()
        with patch("builtins.input", return_value="y"), patch("ai_quota_swap.prompts.interactive", return_value=True), \
             patch("subprocess.run", no_subprocess), \
             patch("subprocess.Popen", no_subprocess), \
             patch("ai_quota_swap.spawn.run", no_subprocess), \
             patch("os.system", no_subprocess):
            sw.remove_account("2")
        captured = capsys.readouterr()
        assert "Removed #2 a@example.com" in captured.out
        assert "is the live Codex login" in captured.err + captured.out
        assert "codex logout" in captured.err + captured.out
        assert not (sw.credentials_dir / "account-2.json").exists()
        assert live_path(env).read_bytes() == before_live  # the live login is untouched
        assert registry(sw)["sequence"] == [1]

    def test_remove_can_be_cancelled(self, two, sw, capsys):
        with patch("builtins.input", return_value="n"), patch("ai_quota_swap.prompts.interactive", return_value=True):
            sw.remove_account("1")
        assert "Cancelled" in capsys.readouterr().out
        assert registry(sw)["sequence"] == [1, 2]

    def test_resolve_by_number_alias_and_email(self, two, sw):
        sw.set_alias("1", "spare")
        assert sw.resolve_account("1")[0] == "1"
        assert sw.resolve_account("SPARE")[0] == "1"
        assert sw.resolve_account("a@example.com") == ("2", "a@example.com", "acct-a")
        with pytest.raises(AccountNotFoundError):
            sw.resolve_account("9")
        with pytest.raises(AccountNotFoundError):
            sw.resolve_account("nobody@example.com")

    def test_aliases(self, two, sw):
        assert sw.set_alias("2", "Main") == ("2", "main")
        assert sw.list_aliases() == [("2", "main", "a@example.com")]
        with pytest.raises(ConfigError, match="already used"):
            sw.set_alias("1", "main")
        assert sw.unset_alias("main") == "2"
        assert sw.list_aliases() == []
        assert sw.unset_alias("2") == "2"  # idempotent

    def test_policy(self, two, sw, capsys):
        sw.set_account_threshold("1", "85")
        sw.set_account_priority("2", "7")
        policies = sw.account_policies()
        assert policies["1"].threshold == 85.0
        assert policies["2"].priority == 7
        with pytest.raises(ValueError):
            sw.set_account_threshold("1", "20")
        sw.set_account_threshold("1", None)
        assert sw.account_policies()["1"].threshold is None
        out = capsys.readouterr().out
        assert "Set the switch line of #1 b@example.com to 85%" in out
        assert "Cleared the switch line of #1 b@example.com" in out

    def test_disable_and_enable(self, two, sw, capsys):
        sw.set_account_disabled("1", True)
        assert sw.disabled_account_numbers() == ["1"]
        assert sw.switchable_account_numbers() == ["2"]
        sw.set_account_disabled("1", True)
        sw.set_account_disabled("1", False)
        out = capsys.readouterr().out
        assert "Disabled #1 b@example.com" in out
        assert "already disabled" in out
        assert "Enabled #1" in out
        assert sw.switchable_account_numbers() == ["1", "2"]

    def test_a_missing_saved_copy_is_not_switchable(self, two, sw):
        (sw.credentials_dir / "account-1.json").unlink()
        assert sw.switchable_account_numbers() == ["2"]

    def test_registry_writes_take_the_provider_lock(self, two, sw):
        held = []
        real = locking.FileLock.acquire

        def acquire(self, timeout=None):
            held.append(Path(self.lock_path).name)
            return real(self, timeout)

        with patch.object(locking.FileLock, "acquire", acquire):
            sw.set_alias("1", "x")
            sw.set_account_disabled("1", True)
        assert held.count(".lock") == 2

    def test_nothing_managed(self, sw):
        with pytest.raises(ConfigError, match="No Codex accounts are managed yet"):
            sw.switch_to("1")
        assert sw.account_policies() == {}
        assert sw.current_account_number() is None


# -- the switch procedure ----------------------------------------------------------


class _Recorder:
    """Records lock acquire/release and the auth write, in order."""

    def __init__(self):
        self.events: list[str] = []


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    real_acquire = locking.FileLock.acquire
    real_release = locking.FileLock.release

    def acquire(self, timeout=None):
        ok = real_acquire(self, timeout)
        rec.events.append(f"acquire {Path(self.lock_path).name}")
        return ok

    def release(self):
        if self._locked:
            rec.events.append(f"release {Path(self.lock_path).name}")
        real_release(self)

    real_write = codex_auth.write_auth_bytes

    def write(path, data):
        rec.events.append("write auth.json")
        real_write(path, data)

    monkeypatch.setattr(locking.FileLock, "acquire", acquire)
    monkeypatch.setattr(locking.FileLock, "release", release)
    monkeypatch.setattr(codex_auth, "write_auth_bytes", write)
    return rec


class TestSwitchTo:
    def test_writes_the_saved_record_byte_for_byte(self, two, sw, env, capsys):
        sw.switch_to("1")
        assert live_path(env).read_bytes() == two.b
        if sys.platform != "win32":
            assert stat.S_IMODE(live_path(env).stat().st_mode) == 0o600
        out = capsys.readouterr().out
        assert "Switched #2 a@example.com → #1 b@example.com" in out
        assert sw.current_account_number() == "1"

    def test_locks_are_taken_in_order_around_the_write(self, two, sw, env, recorder):
        sw.switch_to("1")
        switch_events = recorder.events[recorder.events.index("acquire .lock"):]
        assert switch_events[:7] == [
            "acquire .lock",
            "acquire auth.lock",
            "acquire auth.json.lock",
            "write auth.json",
            "release auth.json.lock",
            "release auth.lock",
            "release .lock",
        ]
        assert (env.codex_home / "auth.lock").exists()
        assert (env.codex_home / "auth.json.lock").exists()

    def test_the_outgoing_login_is_saved_first_with_codexs_renewal(self, two, sw, env):
        renewed = make_login(**LOGIN_A, access_suffix="renewed-by-codex")
        write_live(env.codex_home, renewed)  # Codex refreshed account 2's tokens
        sw.switch_to("1")
        assert (sw.credentials_dir / "account-2.json").read_bytes() == renewed
        sw.switch_to("2")
        assert live_path(env).read_bytes() == renewed

    def test_a_symlinked_auth_json_stays_a_symlink(self, two, sw, env, tmp_path):
        store = tmp_path / "dotfiles"
        store.mkdir()
        target = store / "codex-auth.json"
        target.write_bytes(live_path(env).read_bytes())
        live_path(env).unlink()
        live_path(env).symlink_to(target)
        sw.switch_to("1")
        assert live_path(env).is_symlink()
        assert target.read_bytes() == two.b
        assert (store / "codex-auth.json.lock").exists()

    def test_an_unmanaged_live_login_is_refused_without_force(self, two, sw, env):
        stranger = make_login(**LOGIN_C)
        write_live(env.codex_home, stranger)
        with pytest.raises(SwitchError, match="not managed by qswap"):
            sw.switch_to("1")
        assert live_path(env).read_bytes() == stranger

    def test_force_overwrites_an_unmanaged_login(self, two, sw, env):
        write_live(env.codex_home, make_login(**LOGIN_C))
        result = sw.switch_to("1", json_output=True, force=True)
        assert live_path(env).read_bytes() == two.b
        assert result["switched"] is True
        assert result["from"] == {"number": None, "email": "c@example.com"}
        assert any("unmanaged" in w for w in result["warnings"])

    def test_an_unreadable_live_file_is_refused_without_force(self, two, sw, env):
        write_live(env.codex_home, json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}).encode())
        with pytest.raises(SwitchError, match="not managed"):
            sw.switch_to("1")

    def test_near_expiry_with_a_running_codex_is_refused(self, sw, env, fetch, monkeypatch):
        capture(sw, env, make_login(**LOGIN_B))
        soon = make_login(**LOGIN_A, exp=time.time() + 300)
        capture(sw, env, soon)
        monkeypatch.setattr(codex_processes, "list_codex_processes",
                            lambda: [CodexProcess(321, codex_processes.CLI, env.codex_home)])
        with pytest.raises(RetryableSwitchError, match="try again") as exc:
            sw.switch_to("1")
        assert "321" in str(exc.value)
        assert live_path(env).read_bytes() == soon

    def test_near_expiry_with_unknown_process_home_is_refused(self, sw, env, fetch, monkeypatch):
        capture(sw, env, make_login(**LOGIN_B))
        capture(sw, env, make_login(**LOGIN_A, exp=time.time() + 300))
        monkeypatch.setattr(codex_processes, "list_codex_processes",
                            lambda: [CodexProcess(321, codex_processes.CLI, None)])
        with pytest.raises(RetryableSwitchError):
            sw.switch_to("1")

    def test_near_expiry_with_a_background_server_is_refused(self, sw, env, fetch, monkeypatch):
        capture(sw, env, make_login(**LOGIN_B))
        capture(sw, env, make_login(**LOGIN_A, exp=time.time() + 300))
        monkeypatch.setattr(codex_processes, "background_server_pid", lambda home: 999)
        with pytest.raises(RetryableSwitchError):
            sw.switch_to("1")

    def test_near_expiry_with_nothing_running_goes_ahead(self, sw, env, fetch, monkeypatch, tmp_path):
        capture(sw, env, make_login(**LOGIN_B))
        capture(sw, env, make_login(**LOGIN_A, exp=time.time() + 300))
        monkeypatch.setattr(codex_processes, "list_codex_processes",
                            lambda: [CodexProcess(5, codex_processes.CLI, tmp_path / "other-home")])
        sw.switch_to("1")
        assert sw.current_account_number() == "1"

    def test_verification_failure_is_reported(self, two, sw, env, monkeypatch):
        real_write = codex_auth.write_auth_bytes

        def clobbered(path, data):
            real_write(path, make_login(**LOGIN_C))  # a 0.164 Codex writing back

        monkeypatch.setattr(codex_auth, "write_auth_bytes", clobbered)
        with pytest.raises(SwitchError, match="changed right after the switch"):
            sw.switch_to("1")

    def test_already_active_is_a_no_op(self, two, sw, env, recorder, capsys):
        assert sw.switch_to("2", json_output=True) == {
            "schemaVersion": 1, "switched": False,
            "from": {"number": 2, "email": "a@example.com"},
            "to": {"number": 2, "email": "a@example.com"},
            "strategy": "direct", "reason": "already-active",
            "message": "Already on Account-2 (a@example.com)", "warnings": [],
        }
        assert "write auth.json" not in recorder.events

    def test_json_result(self, two, sw):
        result = sw.switch_to("b@example.com", json_output=True)
        assert result == {
            "schemaVersion": 1, "switched": True,
            "from": {"number": 2, "email": "a@example.com"},
            "to": {"number": 1, "email": "b@example.com"},
            "strategy": "direct", "reason": "switched",
            "message": "Switched to Account-1 (b@example.com)", "warnings": [],
        }

    def test_a_saved_copy_replaced_mid_switch_is_refused(self, two, sw, env, monkeypatch):
        real_read = CodexSwitcher._read_saved
        reads = {"n": 0}

        def read(self, num):
            reads["n"] += 1
            if num == "1" and reads["n"] > 1:
                return make_login(**LOGIN_C)  # replaced between the two reads
            return real_read(self, num)

        monkeypatch.setattr(CodexSwitcher, "_read_saved", read)
        before = live_path(env).read_bytes()
        with pytest.raises(SwitchError, match="changed while switching"):
            sw.switch_to("1")
        assert live_path(env).read_bytes() == before

    def test_a_missing_saved_copy_cannot_be_switched_to(self, two, sw):
        (sw.credentials_dir / "account-1.json").unlink()
        with pytest.raises(SwitchError, match="no saved login"):
            sw.switch_to("1")

    def test_keyring_mode_is_refused(self, two, sw, env):
        (env.codex_home / "config.toml").write_text('cli_auth_credentials_store = "auto"\n')
        with pytest.raises(CodexLoginError):
            sw.switch_to("1")

    def test_a_switch_writes_an_activation_line_the_ledger_reads(self, two, sw):
        sw.switch_to("1")
        log = ActivationLog.load(sw.backup_dir)
        switches = [a for a in log.activations if a.source == "switch"]
        assert len(switches) == 1
        assert switches[0].account == identity_key("b@example.com", "acct-b")
        assert switches[0].number == 1
        assert log.account_at(time.time() + 1) == identity_key("b@example.com", "acct-b")

    def test_listing_observes_a_login_changed_outside_qswap(self, two, sw, fetch):
        sw.switch_to("1")
        write_live(codex_auth.codex_home(), two.a)  # e.g. a restore by hand
        sw.list_payload()
        last = ActivationLog.load(sw.backup_dir).activations[-1]
        assert (last.source, last.account) == ("observed", identity_key("a@example.com", "acct-a"))


class TestHolders:
    def test_background_server_is_restarted(self, two, sw, env, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(codex_processes, "background_server_pid", lambda home: 4242)
        monkeypatch.setattr(codex_processes, "restart_background_server",
                            lambda home, timeout=30: calls.append(home) or RestartResult(True))
        result = sw.switch_to("1", json_output=True)
        assert calls == [env.codex_home]
        assert result["backgroundServer"] == {"pid": 4242, "restarted": True}
        assert result["warnings"] == []

    def test_restart_is_skipped_when_the_setting_is_off(self, two, sw, env, monkeypatch):
        set_setting(sw.data_root, "codex.restartDaemon", "false")
        monkeypatch.setattr(codex_processes, "background_server_pid", lambda home: 4242)
        result = sw.switch_to("1", json_output=True)  # the conftest fake fails on a restart
        assert result["backgroundServer"] == {
            "pid": 4242, "restarted": False, "reason": "codex.restartDaemon is off",
        }
        assert any("codex app-server daemon restart" in w for w in result["warnings"])

    @pytest.mark.parametrize("detail", ["timed out after 30s", "exit code 1"])
    def test_a_failed_restart_is_a_warning_not_an_error(self, two, sw, env, monkeypatch, detail):
        monkeypatch.setattr(codex_processes, "background_server_pid", lambda home: 4242)
        monkeypatch.setattr(codex_processes, "restart_background_server",
                            lambda home, timeout=30: RestartResult(False, detail))
        result = sw.switch_to("1", json_output=True)
        assert result["switched"] is True
        assert live_path(env).read_bytes() == two.b
        assert result["backgroundServer"]["restarted"] is False
        assert any(detail in w for w in result["warnings"])

    def test_no_restart_without_a_background_server(self, two, sw):
        result = sw.switch_to("1", json_output=True)
        assert "backgroundServer" not in result

    def test_desktop_app_warning_on_the_default_home(self, temp_home, monkeypatch, fetch):
        monkeypatch.delenv("CODEX_HOME", raising=False)
        home = temp_home / ".codex"
        home.mkdir()
        sw = CodexSwitcher()
        write_live(home, make_login(**LOGIN_B))
        sw.add_account()
        write_live(home, make_login(**LOGIN_A))
        sw.add_account()
        monkeypatch.setattr(codex_processes, "list_codex_processes",
                            lambda: [CodexProcess(77, codex_processes.DESKTOP, None)])
        result = sw.switch_to("1", json_output=True)
        assert any("desktop app" in w for w in result["warnings"])

    def test_no_desktop_warning_for_another_home(self, two, sw, monkeypatch):
        monkeypatch.setattr(codex_processes, "list_codex_processes",
                            lambda: [CodexProcess(77, codex_processes.DESKTOP, None)])
        result = sw.switch_to("1", json_output=True)
        assert not any("desktop" in w for w in result["warnings"])

    def test_other_codex_processes_are_warned_about(self, two, sw, env, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(codex_processes, "list_codex_processes", lambda: [
            CodexProcess(11, codex_processes.CLI, env.codex_home),
            CodexProcess(12, codex_processes.CLI, None),
            CodexProcess(13, codex_processes.CLI, tmp_path / "elsewhere"),
        ])
        result = sw.switch_to("1", json_output=True)
        assert result["warnings"] == [
            "2 running codex processes (pids 11, 12) stay on the old account until they exit"
        ]

    def test_one_other_process_reads_in_the_singular(self, two, sw, env, monkeypatch):
        monkeypatch.setattr(codex_processes, "list_codex_processes", lambda: [
            CodexProcess(11, codex_processes.CLI, env.codex_home),
        ])
        result = sw.switch_to("1", json_output=True)
        assert result["warnings"] == [
            "1 running codex process (pid 11) stays on the old account until it exits"
        ]


class TestRotation:
    def test_plain_switch_rotates(self, two, sw, env):
        result = sw.switch(json_output=True)
        assert result["strategy"] == "rotation"
        assert result["to"] == {"number": 1, "email": "b@example.com"}
        assert sw.switch(json_output=True)["to"]["number"] == 2

    def test_rotation_skips_disabled_and_unsaved_accounts(self, sw, env, fetch, capsys):
        for login in (LOGIN_B, LOGIN_C, LOGIN_A):
            capture(sw, env, make_login(**login))
        sw.set_account_disabled("1", True)
        (sw.credentials_dir / "account-2.json").unlink()
        result = sw.switch(json_output=True)
        assert result["reason"] == "no-valid-target"
        assert result["warnings"] == [
            "Skipped Account-1 (disabled)", "Skipped Account-2 (no saved login)",
        ]

    def test_only_one_account(self, sw, env, fetch):
        capture(sw, env, make_login(**LOGIN_A))
        assert sw.switch(json_output=True)["reason"] == "only-one-account"

    def test_unmanaged_live_login_in_json_mode(self, two, sw, env):
        write_live(env.codex_home, make_login(**LOGIN_C))
        result = sw.switch(json_output=True)
        assert result["reason"] == "unmanaged-account"
        assert result["switched"] is False

    def test_no_live_login_starts_from_the_recorded_account(self, two, sw, env):
        live_path(env).unlink()
        result = sw.switch(json_output=True)
        assert result["to"]["number"] == 2
        assert result["from"] is None

    def test_strategies_are_not_available_yet(self, two, sw):
        with pytest.raises(FeatureUnavailableError):
            sw.switch(strategy="best")


# -- usage ------------------------------------------------------------------------


class TestUsage:
    def test_live_token_for_the_active_account_saved_token_for_the_rest(self, two, sw, fetch):
        entries = sw.usage_entries_by_account()
        live = codex_auth.parse_login(two.a).access_token
        saved = codex_auth.parse_login(two.b).access_token
        assert sorted(fetch.calls) == sorted([(live, "acct-a"), (saved, "acct-b")])
        assert entries["2"].last_good["seven_day"]["pct"] == 40.0
        assert entries["1"].last_good["seven_day"]["pct"] == 10.0

    def test_a_second_list_inside_the_interval_makes_no_request(self, two, sw, fetch, capsys):
        sw.list_accounts()
        assert len(fetch.calls) == 2
        sw.list_accounts()
        sw.list_payload()
        sw.accounts_snapshot()
        assert len(fetch.calls) == 2

    def test_fetch_restricts_what_may_be_fetched(self, two, sw, fetch):
        sw.usage_entries_by_account(fetch=set())
        assert fetch.calls == []
        sw.usage_entries_by_account(fetch={"1"})
        assert [c[1] for c in fetch.calls] == ["acct-b"]

    def test_refresh_usage_fetches_now(self, two, sw, fetch):
        sw.usage_entries_by_account()
        sw.refresh_usage({"2"})
        assert [c[1] for c in fetch.calls].count("acct-a") == 2

    def test_401_on_the_live_login_is_token_expired(self, two, sw, fetch):
        fetch.answers["acct-a"] = UsageOutcome(error="http-401")
        entries = sw.usage_entries_by_account()
        assert entries["2"].sentinel == USAGE_TOKEN_EXPIRED
        calls = len(fetch.calls)
        # The refused token is stamped: no new request until it changes.
        sw.refresh_usage({"2"})
        assert len(fetch.calls) == calls
        assert sw.usage_entries_by_account()["2"].sentinel == USAGE_TOKEN_EXPIRED

    def test_401_on_a_saved_login_is_unknown_until_used(self, two, sw, fetch):
        fetch.answers["acct-b"] = UsageOutcome(error="http-401")
        assert sw.usage_entries_by_account()["1"].sentinel == USAGE_UNKNOWN_UNTIL_USED

    def test_an_expired_saved_token_is_not_fetched(self, sw, env, fetch):
        capture(sw, env, make_login(**LOGIN_B, exp=time.time() - 60))
        capture(sw, env, make_login(**LOGIN_A))
        entries = sw.usage_entries_by_account()
        assert entries["1"].sentinel == USAGE_UNKNOWN_UNTIL_USED
        assert [c[1] for c in fetch.calls] == ["acct-a"]

    def test_an_expired_live_token_is_token_expired_without_a_request(self, sw, env, fetch):
        capture(sw, env, make_login(**LOGIN_A, exp=time.time() - 60))
        assert sw.usage_entries_by_account()["1"].sentinel == USAGE_TOKEN_EXPIRED
        assert fetch.calls == []

    def test_a_missing_saved_copy(self, two, sw, fetch):
        (sw.credentials_dir / "account-1.json").unlink()
        assert sw.usage_entries_by_account()["1"].sentinel == USAGE_NO_CREDENTIALS

    def test_errors_back_off_and_keep_the_last_reading(self, two, sw, fetch):
        sw.usage_entries_by_account()
        fetch.answers["acct-b"] = UsageOutcome(error="bad-response")
        entries = sw.refresh_usage({"1"})
        assert entries["1"].last_error == "bad-response"
        assert entries["1"].last_good["seven_day"]["pct"] == 10.0
        calls = len(fetch.calls)
        sw.refresh_usage({"1"})  # in backoff now
        assert len(fetch.calls) == calls

    def test_plan_label_from_usage_then_from_the_id_token(self, sw, env, fetch):
        capture(sw, env, make_login(**LOGIN_A, plan="plus"))
        snap = sw.accounts_snapshot(fetch=set())
        assert snap.accounts[0].subscription_plan["plan"] == {"tier": "plus", "planDisplayName": "Plus"}
        snap = sw.accounts_snapshot()
        assert snap.accounts[0].subscription_plan["plan"]["tier"] == "pro"  # the response's

    def test_snapshot(self, two, sw, fetch):
        snap = sw.accounts_snapshot()
        assert snap.active_number == "2"
        assert [a.number for a in snap.accounts] == ["1", "2"]
        assert snap.accounts[1].is_active and snap.accounts[1].kind == "oauth"
        assert snap.accounts[0].org_uuid == "acct-b"


# -- engine host members ----------------------------------------------------------


class TestEngineMembers:
    def test_identity(self, two, sw):
        assert sw.current_account_number() == "2"
        assert sw.has_live_login() is True
        assert sw.account_identity("1") == {
            "email": "b@example.com", "organizationUuid": "acct-b", "uuid": "user-b",
        }
        assert sw.account_kind_for("1") == "oauth"

    def test_unmanaged_live_login_is_none_but_live(self, two, sw, env):
        write_live(env.codex_home, make_login(**LOGIN_C))
        assert sw.current_account_number() is None
        assert sw.has_live_login() is True

    def test_credential_expiry(self, sw):
        exp = time.time() + 1000
        assert sw.credential_expires_at_ms(make_login(**LOGIN_A, exp=exp).decode()) == pytest.approx(exp * 1000)
        for bad in ("", "nope", "{}", json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "k"})):
            with pytest.raises(CredentialFormatError):
                sw.credential_expires_at_ms(bad)

    def test_fingerprint_follows_the_refresh_token(self, sw):
        first = make_login(**LOGIN_A, refresh="rt-1", access_suffix="x").decode()
        same_rt = make_login(**LOGIN_A, refresh="rt-1", access_suffix="y").decode()
        rotated = make_login(**LOGIN_A, refresh="rt-2").decode()
        assert sw.credential_fingerprint(first) == sw.credential_fingerprint(same_rt)
        assert sw.credential_fingerprint(first) != sw.credential_fingerprint(rotated)
        assert sw.credential_fingerprint("") is None
        assert sw.credential_fingerprint("garbage").startswith("sha256:")

    def test_consume_backup_grant_never_touches_the_network(self, two, sw):
        """The global network block would fail this test on any request."""
        snapshot = sw.read_account_credentials("1", "b@example.com")
        outcome = sw.consume_backup_grant("1", "b@example.com", snapshot)
        assert outcome.error is None and outcome.credentials == two.b.decode()
        assert sw.consume_backup_grant("9", "x@example.com", "").error == "transient"
        assert sw.consume_backup_grant("9", "x@example.com", "junk").error == "invalid_grant"

    def test_the_engine_treats_an_expired_codex_target_as_ready(self, sw, env, fetch):
        """_freshen_target reads expiry through the host; for a Codex target
        whose access token expired while saved it reaches the consume gate,
        which answers ready without refreshing."""
        from ai_quota_swap.autoswitch import AutoSwitchEngine
        from ai_quota_swap.settings import AutoSwitchSettings

        capture(sw, env, make_login(**LOGIN_B, exp=time.time() - 3600))
        capture(sw, env, make_login(**LOGIN_A))
        before = sw.read_account_credentials("1", "b@example.com")
        engine = AutoSwitchEngine(sw, AutoSwitchSettings(), lambda e: None,
                                  state_path=sw.backup_dir / "autoswitch_state.json")
        with patch.object(oauth, "try_refresh_oauth_credentials",
                          side_effect=AssertionError("refreshed a Codex login")):
            assert engine._freshen_target("1", "b@example.com") == "ok"
        assert sw.read_account_credentials("1", "b@example.com") == before

    def test_reset_settings_and_poll_inputs(self, sw):
        set_setting(sw.data_root, "resets.threshold", "80")
        assert sw.reset_settings().threshold == 80.0
        sw.set_poll_policy_inputs(70.0, None, ("x",))
        assert sw._poll_policy_inputs() == (70.0, None, ("x",))
        sw.clear_poll_policy_inputs()
        assert sw._poll_policy_inputs()[2] == ()

    def test_backfill_fills_only_an_empty_uuid(self, two, sw):
        data = registry(sw)
        data["accounts"]["1"]["uuid"] = ""
        sw.sequence_file.write_text(json.dumps(data))
        sw.backfill_account_uuid("1", "user-b")
        sw.backfill_account_uuid("1", "someone-else")
        assert registry(sw)["accounts"]["1"]["uuid"] == "user-b"


class TestRotationSkipsNeedsRelogin:
    """A bare `qswap codex switch` never lands on an account the engine set
    aside until it is logged in again; it says so in one line."""

    def test_skips_it_and_stops_with_the_next_step(self, two, sw, env, capsys):
        before = live_path(env).read_bytes()
        with patch.object(type(sw), "set_aside_accounts", return_value={"1": "invalid_grant"}):
            sw.switch()
        out = capsys.readouterr().out
        assert "Skipped #1 b@example.com: needs re-login." in out
        assert "No other account can take over, so Codex stays on #2 a@example.com." in out
        assert "qswap codex login" in out
        assert live_path(env).read_bytes() == before  # nothing changed

    def test_json_lists_it_in_warnings(self, two, sw, env, capsys):
        with patch.object(type(sw), "set_aside_accounts", return_value={"1": "invalid_grant"}):
            payload = sw.switch(json_output=True)
        assert payload["switched"] is False
        assert payload["warnings"] == ["Skipped Account-1 (needs re-login)"]

    def test_without_it_rotation_proceeds(self, two, sw, env, capsys):
        with patch.object(type(sw), "set_aside_accounts", return_value={}):
            sw.switch()
        assert sw.current_account_number() == "1"
