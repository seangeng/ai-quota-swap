"""API-key accounts and running Claude Code sessions (docs/design/api-key-sessions.md).

Part 1 of the design: activating an API-key account keeps the login item
(only ``claudeAiOauth`` leaves it, so MCP server sign-ins survive), leaving one
never rewrites its saved key, and the active credential of an API-key account
is the saved key. Everything runs on the in-memory Keychain fake and the
temporary home; the macOS paths run on every platform by setting the
switcher's platform.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_quota_swap import macos_keychain
from ai_quota_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
)
from ai_quota_swap.models import Platform
from ai_quota_swap.paths import get_credentials_path, get_global_config_path
from ai_quota_swap.switcher import ClaudeAccountSwitcher

KEY_5 = "sk-ant-api03-" + "k5k5k5k5k5" * 4
KEY_6 = "sk-ant-api03-" + "k6k6k6k6k6" * 4
SETUP_TOKEN = "sk-ant-oat01-" + "p1p1p1p1p1" * 4
MCP = {"plugin:slack:slack|abc": {"serverName": "slack", "accessToken": "mcp-1"}}
MCP_LATER = {
    **MCP,
    "plugin:figma:figma|def": {"serverName": "figma", "accessToken": "mcp-2"},
}
MAYA = "maya@example.com"
EMAIL_5 = "api-key-5@token.local"
EMAIL_6 = "api-key-6@token.local"


def _switcher(platform: Platform) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = platform
    s._setup_directories()
    s._init_sequence_file()
    return s


def _fleet(platform: Platform = Platform.MACOS) -> ClaudeAccountSwitcher:
    """#1 personal (a subscription login) is active with MCP sign-ins in its
    live login; #5 ci and #6 ops are API-key accounts."""
    s = _switcher(platform)
    s.add_account_from_token(SETUP_TOKEN, email=MAYA, slot=1)
    s.add_account_from_token(KEY_5, slot=5)
    s.add_account_from_token(KEY_6, slot=6)
    s.set_alias("1", "personal")
    s.set_alias("5", "ci")
    s.set_alias("6", "ops")
    login = json.loads(s._read_account_credentials("1", MAYA))
    s._write_credentials(json.dumps({**login, "mcpOAuth": MCP}))
    get_global_config_path().write_text(json.dumps({
        "oauthAccount": {"emailAddress": MAYA, "accountUuid": "", "organizationUuid": None},
        "projects": {"/work/app": {"allowedTools": []}},
        "mcpServers": {"slack": {}},
    }), encoding="utf-8")
    data = s._get_sequence_data()
    data["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, data)
    return s


def _acct() -> str:
    return macos_keychain.keychain_account_name()


def _login_item(store) -> dict | None:
    raw = store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())
    return None if raw is None else json.loads(raw)


def _config() -> dict:
    return json.loads(get_global_config_path().read_text(encoding="utf-8"))


def _prev(s: ClaudeAccountSwitcher, num: str, email: str) -> str:
    return s._store._read_previous_backup(num, email)


# -- activation ------------------------------------------------------------------


class TestActivation:
    def test_login_item_keeps_mcp_and_loses_the_login(self, temp_home, block_real_keychain):
        s = _fleet()
        approved_before = {"approved": ["x" * 20], "rejected": []}
        cfg = _config()
        cfg["customApiKeyResponses"] = approved_before
        get_global_config_path().write_text(json.dumps(cfg), encoding="utf-8")

        s.switch_to("5", json_output=True, log_out_sessions=True)

        item = _login_item(block_real_keychain)
        assert item is not None, "the login item is never left missing"
        assert "claudeAiOauth" not in item
        assert item["mcpOAuth"] == MCP
        assert block_real_keychain.get_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()
        ) == KEY_5
        cfg = _config()
        assert cfg["customApiKeyResponses"] == approved_before
        assert "primaryApiKey" not in cfg
        assert cfg["projects"] == {"/work/app": {"allowedTools": []}}

    def test_login_item_is_never_absent_during_the_write(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        """No step of the activation deletes the item: Claude Code keeps its
        cached login while the item is missing and recreates it holding only
        MCP sign-ins later, at a moment nobody chose."""
        s = _fleet()
        deletes: list[str] = []
        real_delete = macos_keychain.delete_password

        def spy(service, account):
            deletes.append(service)
            real_delete(service, account)

        monkeypatch.setattr(macos_keychain, "delete_password", spy)
        s.switch_to("5", json_output=True, log_out_sessions=True)
        assert CLAUDE_CODE_KEYCHAIN_SERVICE not in deletes

    def test_an_absent_login_item_is_written_without_a_login(
        self, temp_home, block_real_keychain
    ):
        s = _switcher(Platform.MACOS)
        s._write_credentials(KEY_5)
        assert _login_item(block_real_keychain) == {}

    def test_file_store_keeps_mcp_and_loses_the_login(self, temp_home):
        """Linux and Windows: the same rule on ``.credentials.json``."""
        s = _fleet(Platform.LINUX)
        s.switch_to("5", json_output=True, log_out_sessions=True)

        stored = json.loads(get_credentials_path().read_text(encoding="utf-8"))
        assert stored == {"mcpOAuth": MCP}
        cfg = _config()
        assert cfg["primaryApiKey"] == KEY_5
        assert "customApiKeyResponses" not in cfg


# -- reading the active credential --------------------------------------------------


class TestActiveCredential:
    def test_mcp_only_item_with_a_saved_key_reads_as_the_key(
        self, temp_home, block_real_keychain
    ):
        s = _switcher(Platform.MACOS)
        block_real_keychain.set_password(
            CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(), json.dumps({"mcpOAuth": MCP})
        )
        block_real_keychain.set_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct(), KEY_5
        )
        assert s._read_credentials() == KEY_5

    def test_mcp_only_item_without_a_key_reads_as_itself(
        self, temp_home, block_real_keychain
    ):
        s = _switcher(Platform.MACOS)
        item = json.dumps({"mcpOAuth": MCP})
        block_real_keychain.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(), item)
        assert s._read_credentials() == item

    def test_a_login_still_wins_over_a_saved_key(self, temp_home, block_real_keychain):
        s = _switcher(Platform.MACOS)
        login = json.dumps({"claudeAiOauth": {"accessToken": "a"}, "mcpOAuth": MCP})
        block_real_keychain.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(), login)
        block_real_keychain.set_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct(), KEY_5
        )
        assert s._read_credentials() == login

    def test_file_store_mcp_only_reads_as_the_key(self, temp_home):
        s = _switcher(Platform.LINUX)
        path = get_credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpOAuth": MCP}), encoding="utf-8")
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": KEY_5}), encoding="utf-8"
        )
        assert s._read_credentials() == KEY_5


# -- leaving an API-key account -------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("platform", [Platform.MACOS, Platform.LINUX])
    def test_key_period_and_back_keeps_the_key_and_the_mcp_logins(
        self, temp_home, block_real_keychain, platform
    ):
        s = _fleet(platform)
        config_5 = s._read_account_config("5", EMAIL_5)

        s.switch_to("5", json_output=True, log_out_sessions=True)
        # While the key is active Claude Code saves another MCP sign-in into
        # the login item, which has no login in it.
        if platform == Platform.MACOS:
            block_real_keychain.set_password(
                CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(), json.dumps({"mcpOAuth": MCP_LATER})
            )
        else:
            get_credentials_path().write_text(
                json.dumps({"mcpOAuth": MCP_LATER}), encoding="utf-8"
            )

        result = s.switch_to("1", json_output=True)

        assert result["switched"] is True
        # #5 ci: its key, previous copy and config backup are untouched.
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        assert _prev(s, "5", EMAIL_5) == ""
        assert s._read_account_config("5", EMAIL_5) == config_5
        # #1 personal is active with its own login plus the live MCP sign-ins.
        assert s.current_account_number() == "1"
        if platform == Platform.MACOS:
            live = _login_item(block_real_keychain)
            assert block_real_keychain.get_password(
                CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()
            ) is None
        else:
            live = json.loads(get_credentials_path().read_text(encoding="utf-8"))
            assert "primaryApiKey" not in _config()
        assert live["claudeAiOauth"]["accessToken"] == SETUP_TOKEN
        assert live["mcpOAuth"] == MCP_LATER
        assert _config()["projects"] == {"/work/app": {"allowedTools": []}}

    def test_switching_away_with_an_mcp_only_item_never_rewrites_the_key(
        self, temp_home, block_real_keychain
    ):
        """The incident: #5 ci active, the login item recreated holding only
        MCP sign-ins, then a switch to #1. The key stays the saved
        credential; nothing is pushed into its previous copy."""
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        block_real_keychain.set_password(
            CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(), json.dumps({"mcpOAuth": MCP})
        )
        before = s._read_account_config("5", EMAIL_5)
        s.switch_to("1", json_output=True)
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        assert _prev(s, "5", EMAIL_5) == ""
        assert s._read_account_config("5", EMAIL_5) == before
        assert s.list_unclaimed_credentials() == {}

    def test_key_to_key_keeps_both_keys(self, temp_home, block_real_keychain):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        s.switch_to("6", json_output=True, log_out_sessions=True)
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        assert s._read_account_credentials("6", EMAIL_6) == KEY_6
        assert block_real_keychain.get_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()
        ) == KEY_6
        assert _login_item(block_real_keychain) == {"mcpOAuth": MCP}


class TestSelfHeal:
    def _damage(self, s, store):
        """What the bug left: the saved credential is the MCP-only item and
        the key sits in the previous copy."""
        user = s._store._backup_username("5", EMAIL_5)
        store.set_password(SECURITY_SERVICE, user, json.dumps({"mcpOAuth": MCP}))
        store.set_password(SECURITY_SERVICE, f"{user}.prev", KEY_5)

    def test_switching_to_a_damaged_account_restores_its_key(
        self, temp_home, block_real_keychain, caplog
    ):
        s = _fleet()
        self._damage(s, block_real_keychain)
        with caplog.at_level("WARNING", logger=s._logger.name):
            s.switch_to("5", json_output=True, log_out_sessions=True)
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        assert block_real_keychain.get_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()
        ) == KEY_5
        lines = [r.getMessage() for r in caplog.records if "previous copy" in r.getMessage()]
        assert lines == [
            "Restored account 5's API key from its previous copy "
            "(the saved credential was not a key)"
        ]

    def test_an_intact_account_is_left_alone(self, temp_home, block_real_keychain):
        s = _fleet()
        assert s._heal_api_key_backup("5", EMAIL_5) is False
        assert _prev(s, "5", EMAIL_5) == ""

    def test_an_oauth_account_is_never_healed(self, temp_home, block_real_keychain):
        s = _fleet()
        user = s._store._backup_username("1", MAYA)
        block_real_keychain.set_password(SECURITY_SERVICE, f"{user}.prev", KEY_5)
        assert s._heal_api_key_backup("1", MAYA) is False
        assert json.loads(s._read_account_credentials("1", MAYA))["claudeAiOauth"]


def test_fleet_paths_are_inside_the_temp_home(temp_home):
    assert Path(get_global_config_path()).is_relative_to(temp_home)


# -- running sessions --------------------------------------------------------------

import sys  # noqa: E402
import time  # noqa: E402
from unittest.mock import patch  # noqa: E402

from ai_quota_swap import cli, display, printer  # noqa: E402
from ai_quota_swap.exceptions import SessionsRunningError  # noqa: E402


def _write_sessions(home: Path, pids, *, started_at: float | None = None, cwd="/work/app"):
    """Claude Code session records on the shared login (``~/.claude/sessions``)."""
    directory = home / ".claude" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = int((started_at if started_at is not None else time.time()) * 1000)
    for pid in pids:
        (directory / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": f"s-{pid}", "cwd": f"{cwd}{pid}",
            "startedAt": stamp, "kind": "interactive", "entrypoint": "cli",
        }), encoding="utf-8")


@pytest.fixture
def alive(monkeypatch):
    """The pids that count as running processes (everything else is gone)."""
    pids: set[int] = set()
    monkeypatch.setattr(
        "ai_quota_swap.process_detection.is_pid_alive", lambda pid: pid in pids
    )
    return pids


def _main(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["qswap", *argv]):
        try:
            cli.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


@pytest.fixture
def run(monkeypatch, capsys):
    """``qswap ARGV`` against a switcher, the post-switch listing left out
    (its usage reads are not what these tests are about)."""
    monkeypatch.setattr("ai_quota_swap.update_check.check_for_update", lambda v: None)
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    for name in display.OVERRIDE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def go(switcher, *argv: str) -> tuple[int, str, str]:
        capsys.readouterr()
        # The pre-switch usage check would read a setup-token login's usage,
        # which it cannot: not what these tests are about.
        with patch.object(cli, "ClaudeAccountSwitcher", return_value=switcher), \
             patch.object(switcher, "list_accounts"), \
             patch.object(switcher, "target_readiness", return_value=(None, "")):
            code = _main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return go


REFUSAL = """\
Error: not switched. #5 ci is an API-key account, and 6 Claude Code sessions
are running on the shared login. A running session cannot move to an API key:
about 30 seconds after the switch each one stops with "Not logged in" until
you switch back or restart it.
Sessions started after the switch use the key.
To switch anyway:
  qswap switch 5 --log-out-sessions
"""

PIDS = (4101, 4102, 4103, 4104, 4105, 4106)


def _state(s, store) -> tuple:
    return (
        dict(store.data),
        get_global_config_path().read_bytes(),
        s.sequence_file.read_bytes(),
    )


class TestGuard:
    def test_refusal_changes_nothing_and_says_what_happens(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        before = _state(s, block_real_keychain)

        code, out, err = run(s, "switch", "5")

        assert code == 1
        assert err == REFUSAL
        assert "Switched" not in out
        assert _state(s, block_real_keychain) == before

    def test_json_refusal_is_the_error_envelope_with_the_count(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        before = _state(s, block_real_keychain)

        code, out, _err = run(s, "switch", "5", "--json")

        assert code == 1
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["error"]["type"] == "SessionsRunningError"
        assert payload["error"]["sessions"] == 6
        assert payload["error"]["message"] == REFUSAL.removeprefix("Error: ").rstrip("\n")
        assert _state(s, block_real_keychain) == before

    def test_one_session_reads_in_the_singular(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("5")
        text = str(caught.value)
        assert "and 1 Claude Code session\nis running" in text or (
            "1 Claude Code session is running" in text.replace("\n", " ")
        )
        assert "it stops" in text.replace("\n", " ")
        assert caught.value.sessions == 1

    def test_with_the_flag_it_switches_and_says_the_sessions_stop(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)

        code, out, err = run(s, "switch", "5", "--log-out-sessions")

        assert code == 0, err
        assert out.splitlines() == [
            "Switched #1 personal → #5 ci (API key)",
            '6 running sessions will stop with "Not logged in" within about 30 seconds.',
            "Restart a session to put it on the key. Sessions started from now on use it.",
        ]
        assert s.current_account_number() == "5"
        item = _login_item(block_real_keychain)
        assert item == {"mcpOAuth": MCP}
        assert block_real_keychain.get_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()
        ) == KEY_5
        assert "customApiKeyResponses" not in _config()

    def test_json_switch_carries_the_followup(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        code, out, _err = run(s, "switch", "5", "--log-out-sessions", "--json")
        assert code == 0
        payload = json.loads(out)
        assert payload["switched"] is True
        assert payload["followup"][0].startswith("6 running sessions will stop")

    def test_no_sessions_no_refusal(self, temp_home, block_real_keychain, alive, run):
        s = _fleet()
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        assert out.splitlines() == [
            "Switched #1 personal → #5 ci (API key)",
            "Claude Code sessions started from now on use the key.",
        ]
        assert "within 30 seconds" not in out and "next message" not in out

    def test_a_stale_session_record_does_not_count(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, [4101, 4102])  # both processes are gone
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        assert "Not logged in" not in out

    def test_a_run_profile_session_does_not_count(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        # A qswap run profile is its own CLAUDE_CONFIG_DIR, with its own
        # sessions directory.
        records = s._session_dir("1", MAYA) / "sessions"
        records.mkdir(parents=True, exist_ok=True)
        (records / "4101.json").write_text(json.dumps({
            "pid": 4101, "sessionId": "s", "cwd": "/w", "startedAt": 1,
            "kind": "interactive", "entrypoint": "cli",
        }), encoding="utf-8")
        alive.add(4101)
        code, _out, err = run(s, "switch", "5")
        assert code == 0, err

    def test_the_flag_is_refused_for_a_subscription_account(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        before = _state(s, block_real_keychain)
        code, _out, err = run(s, "switch", "1", "--log-out-sessions")
        assert code == 64
        assert "--log-out-sessions is only for switching to an API-key account" in err
        assert _state(s, block_real_keychain) == before

    def test_the_flag_needs_an_account(self, temp_home, run):
        s = _fleet()
        code, _out, err = run(s, "switch", "--log-out-sessions")
        assert code == 64
        assert "--log-out-sessions can only be used with 'switch ACCOUNT'" in err

    def test_the_flag_is_independent_of_force_and_anyway(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        for flags in (["--force"], ["--anyway"], ["--anyway", "--force"]):
            code, _out, err = run(s, "switch", "5", *flags)
            assert code == 1, flags
            assert "--log-out-sessions" in err
        assert s.current_account_number() == "1"


class TestRotation:
    def _fleet_with_second_login(self):
        s = _fleet()
        s.add_account_from_token(SETUP_TOKEN.replace("p1", "q2"), email="sam@example.com", slot=7)
        return s

    def test_rotation_skips_api_key_accounts_while_sessions_run(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = self._fleet_with_second_login()
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        code, out, err = run(s, "switch")
        assert code == 0, err
        lines = out.splitlines()
        assert lines[:2] == [
            "Skipped #5 ci: an API-key account, and 2 Claude Code sessions are running.",
            "Skipped #6 ops: an API-key account, and 2 Claude Code sessions are running.",
        ]
        assert s.current_account_number() == "7"

    def test_json_rotation_names_the_skip(self, temp_home, block_real_keychain, alive, run):
        s = self._fleet_with_second_login()
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        code, out, _err = run(s, "switch", "--json")
        assert code == 0
        payload = json.loads(out)
        assert "Skipped Account-5 (API-key account, 2 sessions running)" in payload["warnings"]
        assert payload["to"]["number"] == 7

    def test_rotation_with_nothing_left_stays_and_says_how(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        code, out, _err = run(s, "switch")
        assert code == 0
        assert s.current_account_number() == "1"
        assert "qswap switch 5 --log-out-sessions" in out

    def test_rotation_lands_on_a_key_with_no_sessions(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        code, out, _err = run(s, "switch")
        assert code == 0
        assert s.current_account_number() == "5"
        assert "Claude Code sessions started from now on use the key." in out


class TestLeavingAKey:
    def test_back_to_a_subscription_names_the_sessions_started_on_the_key(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, [4101], started_at=time.time() - 3600)
        alive.add(4101)
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4102], started_at=time.time() + 5)
        alive.add(4102)

        code, out, err = run(s, "switch", "1")

        assert code == 0, err
        assert out.splitlines() == [
            "Switched #5 ci → #1 personal (maya@example.com)",
            "Sessions started while #5 ci was active move to #1 personal within about 30",
            "seconds.",
        ]

    def test_back_to_a_subscription_with_no_key_sessions_says_the_usual(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, [4101], started_at=time.time() - 3600)
        alive.add(4101)
        s.switch_to("5", json_output=True, log_out_sessions=True)
        code, out, _err = run(s, "switch", "1")
        assert code == 0
        assert out.splitlines()[0] == "Switched #5 ci → #1 personal (maya@example.com)"
        assert "Sessions started while" not in out

    def test_key_to_key_lists_the_sessions_that_keep_the_old_key(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, [4101], started_at=time.time() - 3600)
        alive.add(4101)
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4102, 4103], started_at=time.time() + 5)
        alive.update({4102, 4103})

        code, out, err = run(s, "switch", "6", "--log-out-sessions")

        assert code == 0, err
        assert out.splitlines() == [
            "Switched #5 ci → #6 ops (API key)",
            "These sessions were started while #5 ci was active and keep using its key until",
            "you restart them:",
            "  pid 4102 · app4102",
            "  pid 4103 · app4103",
            "Claude Code sessions started from now on use the key.",
        ]

    def test_key_to_key_refusal_does_not_claim_a_logout(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4102], started_at=time.time() + 5)
        alive.add(4102)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("6")
        text = str(caught.value).replace("\n", " ")
        assert "Not logged in" not in text
        assert "started while #5 ci was active keep using its key" in text


class TestEnvironment:
    def test_switch_warns_about_an_exported_key_by_name_only(
        self, temp_home, block_real_keychain, alive, run, monkeypatch
    ):
        s = _fleet()
        secret = "sk-ant-api03-" + "s3cr3t" * 7
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        assert (
            "Warning: ANTHROPIC_API_KEY is set; claude started here ignores the switch."
            in out
        )
        assert secret not in out + err

    def test_no_variable_no_warning(self, temp_home, block_real_keychain, alive, run):
        s = _fleet()
        _code, out, _err = run(s, "switch", "5")
        assert "ignores the switch" not in out


class TestWidths:
    LONG = "#12 a-rather-long-alias-name"

    def test_every_new_message_fits_80_columns(self):
        texts = [
            "Error: " + display.api_key_refusal(self.LONG, 12, 16),
            "Error: " + display.api_key_refusal(self.LONG, 12, 1, from_key="#11 another-long-alias"),
            # Printed through say(), which wraps like every other message.
            printer.wrap_message(
                "Skipped #12 a-rather-long-alias-name: "
                + display.api_key_skip_reason(16) + "."
            ),
            # A notification's sentence; where a terminal shows it, it is wrapped.
            "\n".join(display.wrap(display.api_key_engine_sentence("#15 long-alias", 16))),
            display.override_warning(["ANTHROPIC_API_KEY"]),
            *display.api_key_followup(
                target=self.LONG, target_is_key=True, source="#1 personal",
                source_is_key=False, running=16,
            ),
            *display.api_key_followup(
                target=self.LONG, target_is_key=True, source="#11 another-long-alias",
                source_is_key=True, running=3,
                started_on_key=[(40123, "/Users/someone/dev/project")],
            ),
            *display.api_key_followup(
                target="#1 personal", target_is_key=False, source=self.LONG,
                source_is_key=True, running=3, started_on_key=[(1, "/x")],
            ),
        ]
        for text in texts:
            for line in text.splitlines():
                if line.startswith("  qswap "):
                    continue  # a command to copy stays whole
                assert len(line) <= 80, line


# -- the auto-switch engine ------------------------------------------------------------

from ai_quota_swap.autoswitch import NoSwitchEvent, TickOutcome  # noqa: E402
from ai_quota_swap.menubar_notify import Notifier  # noqa: E402
from tests.test_autoswitch import EngineHarness, _usage  # noqa: E402


class TestEngine:
    def _harness(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        data["accounts"]["2"]["alias"] = "ci"
        h.switcher._write_json(h.switcher.sequence_file, data)
        return h

    EXHAUSTED = {"1": _usage(100), "2": "api key", "3": _usage(100)}

    def test_never_lands_on_a_key_while_sessions_run_and_notifies_once(
        self, temp_home, alive
    ):
        h = self._harness(temp_home)
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        notifier = Notifier()
        notes = []
        for _ in range(3):
            outcome = h.tick_with_usage(self.EXHAUSTED)
            assert outcome is TickOutcome.BLOCKED
            assert h.active_number() == 1
        held = [
            e for e in h.events
            if isinstance(e, NoSwitchEvent) and e.reason == "api-key-sessions"
        ]
        assert len(held) == 3
        assert held[0].to_json()["reason"] == "api-key-sessions"
        assert held[0].human() == (
            "Not switching to #2 ci: it is an API-key account and 6 running "
            "sessions could not follow"
        )
        for event in h.events:
            note = notifier.event("claude", event, both=False, now=0.0)
            if note is not None:
                notes.append(note.body)
        assert notes == [held[0].human()]

    def test_a_new_episode_notifies_again(self, temp_home, alive):
        h = self._harness(temp_home)
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        notifier = Notifier()
        h.tick_with_usage(self.EXHAUSTED)
        h.tick_with_usage({"1": _usage(10), "2": "api key", "3": _usage(100)})
        h.tick_with_usage(self.EXHAUSTED)
        notes = [
            n for n in (notifier.event("claude", e, both=False, now=0.0) for e in h.events)
            if n is not None
        ]
        assert len(notes) == 2

    def test_with_no_sessions_the_key_is_still_the_last_resort(self, temp_home, alive):
        h = self._harness(temp_home)
        _write_sessions(temp_home, [4101])  # a stale record: its process is gone
        assert h.tick_with_usage(self.EXHAUSTED) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_a_refusal_at_switch_time_is_the_same_decision(self, temp_home, alive):
        """Sessions that start between the decision and the switch."""
        h = self._harness(temp_home)
        real = h.switcher.shared_login_scan
        calls = {"n": 0}

        def sessions_after_the_decision():
            calls["n"] += 1
            if calls["n"] == 1:
                return [], 0
            _write_sessions(temp_home, [4101])
            alive.add(4101)
            return real()

        with patch.object(h.switcher, "shared_login_scan", sessions_after_the_decision):
            outcome = h.tick_with_usage(self.EXHAUSTED)
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)][-1] == (
            "api-key-sessions"
        )


# -- the menu bar ------------------------------------------------------------------

from ai_quota_swap import menubar  # noqa: E402


class TestMenuBar:
    def _refusal(self, sessions=6):
        return SessionsRunningError(
            display.api_key_refusal("#5 ci", 5, sessions), number="5", sessions=sessions,
        )

    def test_a_refused_key_switch_asks_with_the_same_explanation(self):
        title, message, button = menubar.api_key_switch_question(self._refusal())
        assert title == "Log out 6 running sessions?"
        assert button == "Switch and log out sessions"
        assert message == (
            "#5 ci is an API-key account, and 6 Claude Code sessions are running on "
            "the shared login. A running session cannot move to an API key: about 30 "
            'seconds after the switch each one stops with "Not logged in" until you '
            "switch back or restart it. Sessions started after the switch use the key."
        )
        assert "--log-out-sessions" not in message

    def test_other_errors_ask_nothing(self):
        from ai_quota_swap.exceptions import SwitchError

        assert menubar.api_key_switch_question(SwitchError("no")) is None
        assert menubar.api_key_switch_question(None) is None

    def test_the_real_refusal_becomes_the_question(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("5", json_output=True)
        title, _message, _button = menubar.api_key_switch_question(caught.value)
        assert title == "Log out 6 running sessions?"

    def test_followup_for_a_key_never_says_the_generic_line(self):
        result = {"switched": True, "followup": [
            '6 running sessions will stop with "Not logged in" within about 30 seconds.',
            "Restart a session to put it on the key. Sessions started from now on use it.",
        ]}
        text = menubar.manual_switch_followup("claude", result, backend="keychain")
        assert "picks up the new account" not in text
        assert text.startswith("6 running sessions will stop")
        # A subscription switch keeps the usual words.
        assert "picks up the new account" in menubar.manual_switch_followup("claude", {})


# -- the dashboard -------------------------------------------------------------------

from tests.test_tui import FakeSwitcher, make_account, make_app, settle  # noqa: E402


class _KeyFake(FakeSwitcher):
    """Account 5 is an API-key account and six sessions are running."""

    def switch_to(self, identifier, json_output=False, force=False, log_out_sessions=False):
        if str(identifier) == "5" and not log_out_sessions:
            self.calls.append(("refused", "5"))
            raise SessionsRunningError(
                display.api_key_refusal("#5 ci", 5, 6), number="5", sessions=6,
            )
        self.calls.append(("log-out", str(identifier), log_out_sessions))
        return super().switch_to(identifier, json_output=json_output, force=force)


@pytest.mark.asyncio
class TestDashboard:
    async def test_refused_with_an_explicit_log_out_choice(self, tmp_path):
        from textual.widgets import Static

        from ai_quota_swap.tui.modals import ConfirmModal

        fake = _KeyFake([make_account(1, active=True), make_account(5, alias="ci")], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("claude", "5")
            await settle(pilot)
            assert isinstance(app.screen, ConfirmModal)
            assert app.screen._title == "Log out 6 running sessions?"
            assert app.screen._yes_label == "Switch and log out sessions"
            body = app.screen.query_one(".modal-body", Static).render().plain
            assert body.startswith("#5 ci is an API-key account, and 6 Claude Code")
            await pilot.press("enter")  # the focus starts on Cancel
            await settle(pilot)
            assert ("log-out", "5", True) not in fake.calls
            app.do_switch("claude", "5")
            await settle(pilot)
            await pilot.press("y")
            await settle(pilot)
            assert ("log-out", "5", True) in fake.calls
            assert fake.active == "5"


# -- review fixes --------------------------------------------------------------------

import pathlib  # noqa: E402

from ai_quota_swap import process_detection  # noqa: E402
from ai_quota_swap.exceptions import ValidationError  # noqa: E402


def _has_login(store) -> bool:
    item = _login_item(store)
    return bool(item) and "claudeAiOauth" in item


class TestAddNeverSavesALoginlessStore:
    """``qswap add`` while an API-key account is active, with a variable that
    makes the capture read the login item directly: that item holds only MCP
    sign-ins and must never be saved over an account."""

    CASES = [
        pytest.param(Platform.MACOS, "CLAUDE_SECURESTORAGE_CONFIG_DIR", False,
                     id="mac-securestorage-empty"),
        pytest.param(Platform.LINUX, "CLAUDE_SECURESTORAGE_CONFIG_DIR", False,
                     id="linux-securestorage-empty"),
        pytest.param(Platform.LINUX, "CLAUDE_CONFIG_DIR", False, id="linux-config-dir"),
        pytest.param(Platform.MACOS, "CLAUDE_CONFIG_DIR", True, id="mac-config-dir-with-file"),
    ]

    @pytest.mark.parametrize("platform,var,seed_file", CASES)
    @pytest.mark.parametrize("slot", [None, 5, 7], ids=["refresh", "same-slot", "move-slot"])
    def test_add_refuses_a_store_with_no_login(
        self, temp_home, block_real_keychain, monkeypatch, platform, var, seed_file, slot
    ):
        # The capture's profile reader asks the host platform, not the
        # switcher's: pin it so the macOS cases read the Keychain fake on
        # every CI host.
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: platform))
        if var == "CLAUDE_CONFIG_DIR":
            # It moves .claude.json too, so it is set for the whole test.
            monkeypatch.setenv(var, str(temp_home / ".claude"))
        if seed_file:
            # A plaintext copy an earlier fallback left beside the Keychain.
            path = get_credentials_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"claudeAiOauth": {"accessToken": "old"}, "mcpOAuth": MCP}
            ), encoding="utf-8")
        s = _fleet(platform)
        s.switch_to("5", json_output=True, log_out_sessions=True)
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        if var == "CLAUDE_SECURESTORAGE_CONFIG_DIR":
            monkeypatch.setenv(var, "")

        with pytest.raises(ValidationError, match="holds no Claude login"):
            s.add_account(slot=slot, assume_yes=True)

        data = s._get_sequence_data()
        assert data["accounts"]["5"]["email"] == EMAIL_5
        assert data["accounts"]["5"].get("kind") == "api_key"
        assert "7" not in data["accounts"]
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        assert _prev(s, "5", EMAIL_5) == ""

    def test_a_real_login_is_still_added(self, temp_home, block_real_keychain):
        s = _fleet()
        s._reject_live_api_key_capture(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))


class TestUnreadableSessions:
    """"Could not tell" is never "nobody there": a session record or a
    sessions directory qswap cannot read refuses the switch onto a key."""

    def _torn(self, home: Path, pid: int = 4101) -> None:
        directory = home / ".claude" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pid}.json").write_text(
            '{"pid": %d, "sessionId": "s-41' % pid, encoding="utf-8"
        )

    def _refused(self, s) -> SessionsRunningError:
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("5", json_output=True)
        return caught.value

    def test_a_torn_record_refuses(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        self._torn(temp_home)
        alive.add(4101)
        assert process_detection.scan_sessions() == ([], 1)
        before = _state(s, block_real_keychain)
        error = self._refused(s)
        assert (error.sessions, error.unreadable) == (0, 1)
        assert "could not read Claude Code's session records" in str(error).replace("\n", " ")
        assert _state(s, block_real_keychain) == before
        assert _has_login(block_real_keychain)

    def test_records_of_an_unknown_shape_refuse(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        directory = temp_home / ".claude" / "sessions"
        directory.mkdir(parents=True)
        for pid in PIDS:
            (directory / f"{pid}.json").write_text(
                json.dumps({"processId": pid, "sessionId": f"s-{pid}"}), encoding="utf-8"
            )
        alive.update(PIDS)
        code, out, err = run(s, "switch", "5")
        assert code == 1
        assert "Switched" not in out
        assert err.startswith(
            "Error: not switched. #5 ci is an API-key account, and qswap could not read\n"
        )
        assert "qswap switch 5 --log-out-sessions" in err
        assert all(len(line) <= 80 for line in err.splitlines())
        assert s.current_account_number() == "1"

    def test_an_unlistable_directory_refuses(
        self, temp_home, block_real_keychain, alive, monkeypatch
    ):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        real_iterdir = pathlib.Path.iterdir

        def iterdir(self):
            if self.name == "sessions":
                raise PermissionError(13, "Permission denied")
            return real_iterdir(self)

        monkeypatch.setattr(pathlib.Path, "iterdir", iterdir)
        assert process_detection.scan_sessions() == ([], 1)
        error = self._refused(s)
        assert error.unreadable == 1
        assert _has_login(block_real_keychain)

    def test_a_reader_that_raises_refuses(
        self, temp_home, block_real_keychain, alive, monkeypatch
    ):
        s = _fleet()

        def boom(*_a, **_k):
            raise RuntimeError("detection broke")

        monkeypatch.setattr(process_detection, "scan_sessions", boom)
        error = self._refused(s)
        assert error.unreadable == 1
        assert _has_login(block_real_keychain)

    def test_json_envelope_counts_the_unreadable_records(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        self._torn(temp_home)
        alive.add(4101)
        code, out, _err = run(s, "switch", "5", "--json")
        assert code == 1
        error = json.loads(out)["error"]
        assert error["type"] == "SessionsRunningError"
        assert (error["sessions"], error["unreadable"]) == (0, 1)

    def test_readable_sessions_keep_their_count(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        _write_sessions(temp_home, [4101, 4102])
        self._torn(temp_home, 4103)
        alive.update({4101, 4102, 4103})
        error = self._refused(s)
        assert (error.sessions, error.unreadable) == (2, 1)
        assert "2 Claude Code sessions" in str(error).replace("\n", " ")

    def test_the_flag_still_goes_ahead(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        self._torn(temp_home)
        alive.add(4101)
        assert s.switch_to("5", json_output=True, log_out_sessions=True)["switched"]

    def test_rotation_skips_the_key(self, temp_home, block_real_keychain, alive, run):
        s = _fleet()
        s.add_account_from_token(SETUP_TOKEN.replace("p1", "q2"), email="sam@example.com", slot=7)
        self._torn(temp_home)
        alive.add(4101)
        code, out, err = run(s, "switch")
        assert code == 0, err
        assert out.splitlines()[0] == (
            "Skipped #5 ci: an API-key account, and running sessions could not be counted."
        )
        assert s.current_account_number() == "7"

    def test_the_engine_holds(self, temp_home, alive):
        h = TestEngine()._harness(temp_home)
        self._torn(temp_home)
        alive.add(4101)
        assert h.tick_with_usage(TestEngine.EXHAUSTED) is TickOutcome.BLOCKED
        assert h.active_number() == 1
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert held.reason == "api-key-sessions"
        assert held.human() == (
            "Not switching to #2 ci: it is an API-key account and running sessions "
            "could not be counted"
        )

    def test_the_engine_holds_when_the_reader_raises(self, temp_home, alive, monkeypatch):
        h = TestEngine()._harness(temp_home)

        def boom():
            raise RuntimeError("detection broke")

        monkeypatch.setattr(h.switcher, "shared_login_blocking", boom)
        assert h.tick_with_usage(TestEngine.EXHAUSTED) is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_the_menu_bar_asks_without_a_count(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        self._torn(temp_home)
        alive.add(4101)
        title, message, button = menubar.api_key_switch_question(self._refused(s))
        assert title == "Could not count the running sessions"
        assert message.startswith("#5 ci is an API-key account, and qswap could not read")
        assert button == "Switch and log out sessions"


class TestFreshMachineRotation:
    """No live login in ~/.claude.json (logged out), and only API-key
    accounts can take over while sessions run."""

    def _logged_out(self, s, sequence, active):
        cfg = _config()
        cfg.pop("oauthAccount", None)
        get_global_config_path().write_text(json.dumps(cfg), encoding="utf-8")
        data = s._get_sequence_data()
        data["sequence"] = sequence
        data["activeAccountNumber"] = active
        s._write_json(s.sequence_file, data)

    def test_it_names_the_flag_not_enable(self, temp_home, block_real_keychain, alive, run):
        s = _fleet()
        self._logged_out(s, [5, 6], 5)
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        code, out, err = run(s, "switch")
        assert code == 1
        assert "Skipped #5 ci: an API-key account, and 6 Claude Code sessions are running." in out
        assert "qswap enable" not in err
        assert err.splitlines()[-1] == "  qswap switch 5 --log-out-sessions"
        assert all(len(line) <= 80 for line in err.splitlines()), err
        assert s._get_sequence_data()["activeAccountNumber"] == 5

    def test_a_disabled_subscription_is_not_the_way_out(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        self._logged_out(s, [1, 5, 6], 5)
        data = s._get_sequence_data()
        data["accounts"]["1"]["disabled"] = True
        s._write_json(s.sequence_file, data)
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        code, _out, err = run(s, "switch")
        assert code == 1
        assert "qswap switch 5 --log-out-sessions" in err

    def test_with_no_sessions_it_lands_on_the_key(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        self._logged_out(s, [5, 6], 5)
        code, _out, err = run(s, "switch")
        assert code == 0, err
        assert s._get_sequence_data()["activeAccountNumber"] == 5


class TestReactivatingTheActiveKey:
    def test_force_on_the_active_key_logs_no_one_out(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        result = s.switch_to("5", json_output=True, force=True)
        assert result["to"]["number"] == 5

    def test_a_login_back_in_the_item_is_still_guarded(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        block_real_keychain.set_password(
            CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(),
            json.dumps({"claudeAiOauth": {"accessToken": "a"}, "mcpOAuth": MCP}),
        )
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        with pytest.raises(SessionsRunningError):
            s.switch_to("5", json_output=True, force=True)


class TestKeyToKeyQuestion:
    def test_menu_bar_does_not_ask_to_log_anyone_out(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4102], started_at=time.time() + 5)
        alive.add(4102)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("6")
        assert caught.value.from_key is True
        title, _message, button = menubar.api_key_switch_question(caught.value)
        assert title == "Switch keys with 1 session running?"
        assert button == "Switch anyway"
        assert "log out" not in (title + button).lower()

    def test_the_flag_help_does_not_promise_a_logout_for_every_session(self, capsys):
        from ai_quota_swap import helptext

        text = helptext._switch().format_help()
        assert "those on a subscription stop with" in " ".join(text.split())


class TestAmbiguousEmail:
    def test_the_flag_is_refused_when_no_match_is_a_key(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        data = s._get_sequence_data()
        data["accounts"]["3"] = {
            **data["accounts"]["1"], "organizationUuid": "org-other", "alias": "work",
        }
        data["sequence"].append(3)
        s._write_json(s.sequence_file, data)
        assert cli._is_api_key_target(s, MAYA) is False
        code, _out, err = run(s, "switch", MAYA, "--log-out-sessions")
        assert code == 64
        assert "--log-out-sessions is only for switching to an API-key account" in err


class TestSelfHealEverywhere:
    """A key an older qswap pushed into the previous copy comes back before
    anything moves or exports the account, and a heal that fails keeps it."""

    _damage = TestSelfHeal._damage

    def test_switching_away_from_a_damaged_active_key_restores_it(
        self, temp_home, block_real_keychain
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        self._damage(s, block_real_keychain)
        s.switch_to("1", json_output=True)
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5

    def test_moving_a_damaged_account_keeps_its_key(self, temp_home, block_real_keychain):
        s = _fleet()
        self._damage(s, block_real_keychain)
        s.move_account("5", "9")
        assert s._read_account_credentials("9", EMAIL_5) == KEY_5

    def test_swapping_a_damaged_account_keeps_its_key(self, temp_home, block_real_keychain):
        s = _fleet()
        self._damage(s, block_real_keychain)
        s.swap_accounts("5", "6")
        assert s._read_account_credentials("6", EMAIL_5) == KEY_5
        assert s._read_account_credentials("5", EMAIL_6) == KEY_6

    def test_moving_on_the_file_backend_keeps_the_key(self, temp_home):
        s = _fleet(Platform.LINUX)
        s._store._write_backup_enc("5", EMAIL_5, json.dumps({"mcpOAuth": MCP}))
        s._store._atomic_b64_write(s._store._prev_backup_path("5", EMAIL_5), KEY_5)
        s.move_account("5", "9")
        assert s._read_account_credentials("9", EMAIL_5) == KEY_5

    def test_exporting_a_damaged_account_carries_its_key(
        self, temp_home, block_real_keychain, capsys
    ):
        from ai_quota_swap import transfer

        s = _fleet()
        self._damage(s, block_real_keychain)
        capsys.readouterr()
        transfer.export_accounts(s, "-", account="5")
        entry = json.loads(capsys.readouterr().out)["accounts"][0]
        assert entry.get("kind") == "api_key"
        assert entry["credentials"] == KEY_5

    def test_a_failed_main_write_keeps_the_key_in_the_previous_copy(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        s = _fleet()
        self._damage(s, block_real_keychain)
        user = s._store._backup_username("5", EMAIL_5)
        real_set = block_real_keychain.set_password

        def set_password(service, account, password):
            if service == SECURITY_SERVICE and account == user:
                raise macos_keychain.KeychainError("main item write failed")
            real_set(service, account, password)

        def no_enc(*_a, **_k):
            raise OSError(28, "No space left on device")

        with monkeypatch.context() as m:
            m.setattr(macos_keychain, "set_password", set_password)
            m.setattr(s._store, "_write_backup_enc", no_enc)
            assert s._heal_api_key_backup("5", EMAIL_5) is False
        assert _prev(s, "5", EMAIL_5) == KEY_5
        # A fresh process (the failed one is in its Keychain cooldown) heals.
        again = ClaudeAccountSwitcher()
        again.platform = Platform.MACOS
        assert again._heal_api_key_backup("5", EMAIL_5) is True
        assert again._read_account_credentials("5", EMAIL_5) == KEY_5

    def test_an_interrupted_heal_keeps_the_key_in_the_previous_copy(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        s = _fleet()
        self._damage(s, block_real_keychain)
        user = s._store._backup_username("5", EMAIL_5)
        real_set = block_real_keychain.set_password

        def set_password(service, account, password):
            if service == SECURITY_SERVICE and account == user:
                raise KeyboardInterrupt
            real_set(service, account, password)

        with monkeypatch.context() as m:
            m.setattr(macos_keychain, "set_password", set_password)
            with pytest.raises(KeyboardInterrupt):
                s._heal_api_key_backup("5", EMAIL_5)
        assert _prev(s, "5", EMAIL_5) == KEY_5
        assert s._heal_api_key_backup("5", EMAIL_5) is True
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5

    def test_a_failed_file_write_keeps_the_key(self, temp_home, monkeypatch):
        s = _fleet(Platform.LINUX)
        s._store._write_backup_enc("5", EMAIL_5, json.dumps({"mcpOAuth": MCP}))
        s._store._atomic_b64_write(s._store._prev_backup_path("5", EMAIL_5), KEY_5)

        def no_enc(*_a, **_k):
            raise OSError(28, "No space left on device")

        with monkeypatch.context() as m:
            m.setattr(s._store, "_write_backup_enc", no_enc)
            assert s._heal_api_key_backup("5", EMAIL_5) is False
        assert _prev(s, "5", EMAIL_5) == KEY_5
        assert s._heal_api_key_backup("5", EMAIL_5) is True
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5


class TestPinnedBehaviours:
    """Reported behaviours that no other test would notice breaking."""

    def test_active_since_is_the_start_of_the_last_run(self, temp_home, block_real_keychain):
        from ai_quota_swap.ledger.activations import FILENAME

        s = _fleet()
        rows = [(1000.0, 5, EMAIL_5), (2000.0, 1, MAYA), (3000.0, 5, EMAIL_5),
                (3500.0, 5, EMAIL_5)]
        from datetime import datetime, timezone

        with open(s.backup_dir / FILENAME, "w", encoding="utf-8") as log:
            for ts, number, email in rows:
                stamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                log.write(json.dumps({
                    "ts": stamp.replace("+00:00", "Z"), "number": number,
                    "email": email, "org": "", "source": "switch",
                }) + "\n")
        assert s._key_active_since("5") == 3000.0
        assert s._key_active_since("1") is None

    def test_a_switch_ends_the_notification_episode(self):
        from ai_quota_swap.autoswitch import SwitchEvent, _api_key_sessions_event

        notifier = Notifier()
        held = _api_key_sessions_event("2", 6)
        switched = SwitchEvent(
            trigger="at-limit", from_ref={"number": 1, "email": "a@example.com"},
            to_ref={"number": 3, "email": "c@example.com"},
        )
        said = [
            notifier.event("claude", e, both=False, now=0.0)
            for e in (held, held, switched, held)
        ]
        assert [n is not None for n in said] == [True, False, True, True]

    def test_rotation_warns_about_an_exported_key(
        self, temp_home, block_real_keychain, alive, run, monkeypatch
    ):
        s = _fleet()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "z" * 40)
        _code, out, _err = run(s, "switch")
        assert (
            "Warning: ANTHROPIC_API_KEY is set; claude started here ignores the switch."
            in out
        )

    def test_the_menu_bar_asks_with_cancel_as_the_default(self):
        source = Path(menubar.__file__).with_name("menubar_app.py").read_text(encoding="utf-8")
        assert "if self._confirm_cancel_default(title, message, go):" in source
