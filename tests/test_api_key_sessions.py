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
from ai_quota_swap.exceptions import (  # noqa: E402
    KeyHelperUnavailableError,
    SessionsRunningError,
)


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


# Part 2 (docs/design/api-key-sessions.md) moves a key in through Claude
# Code's apiKeyHelper, which running sessions follow, so nothing is refused for
# them there. The refusal these tests pin is what is left of part 1's guard:
# the key helper cannot be added (another helper is set), so the only way is
# the saved key, which running sessions cannot use.
FOREIGN_HELPER = '{"apiKeyHelper": "/opt/vault/print-key"}'


def _no_helper(home: Path) -> None:
    """Make the key helper unavailable: another apiKeyHelper is set."""
    (home / ".claude" / "settings.json").write_text(FOREIGN_HELPER, encoding="utf-8")


REFUSAL = """\
Error: not switched. qswap cannot add the key helper for #5 ci to the Claude
Code settings: ~/.claude/settings.json already sets another apiKeyHelper.
Without it, the key can only be saved for Claude Code, which a running session
does not pick up. 6 Claude Code sessions are running on the shared login:
about 30 seconds after the switch each one stops with "Not logged in" until
you switch back or restart it.
Sessions started after the switch use the key.
To switch anyway:
  qswap switch 5 --log-out-sessions
""".replace(
    # Messages spell the path with the platform's separator (the same length,
    # so the wrapping is the same).
    "~/.claude/settings.json", str(Path("~", ".claude", "settings.json")),
)

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
        _no_helper(temp_home)
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
        _no_helper(temp_home)
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        before = _state(s, block_real_keychain)

        code, out, _err = run(s, "switch", "5", "--json")

        assert code == 1
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["error"]["type"] == "KeyHelperUnavailableError"
        assert payload["error"]["sessions"] == 6
        assert payload["error"]["message"] == REFUSAL.removeprefix("Error: ").rstrip("\n")
        assert _state(s, block_real_keychain) == before

    def test_one_session_reads_in_the_singular(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        _no_helper(temp_home)
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
        """Through the key helper (part 2): no refusal, and the words say
        the sessions follow it."""
        s = _fleet()
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        assert out.splitlines() == [
            "Switched #1 personal → #5 ci (API key)",
            "Running Claude Code sessions move to the key within a few seconds. #1 personal",
            "stays logged in underneath. claude.ai connectors are unavailable while a key is",
            "in use.",
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
        _no_helper(temp_home)
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

    # Part 2: rotation moves among subscriptions only, whether or not
    # sessions run; an API-key account is entered by naming it.

    def test_rotation_skips_api_key_accounts_while_sessions_run(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = self._fleet_with_second_login()
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        code, out, err = run(s, "switch")
        assert code == 0, err
        assert "Skipped #5" not in out and "Skipped #6" not in out
        assert s.current_account_number() == "7"

    def test_json_rotation_names_the_skip(self, temp_home, block_real_keychain, alive, run):
        s = self._fleet_with_second_login()
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        code, out, _err = run(s, "switch", "--json")
        assert code == 0
        payload = json.loads(out)
        assert not any("Account-5" in w for w in payload["warnings"])
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
        assert "use an API-key account by naming it: qswap switch 5" in " ".join(out.split())

    def test_rotation_lands_on_a_key_with_no_sessions(
        self, temp_home, block_real_keychain, alive, run
    ):
        """Not any more: with no sessions running, rotation still stays among
        subscriptions."""
        s = _fleet()
        code, out, _err = run(s, "switch")
        assert code == 0
        assert s.current_account_number() == "1"
        assert "qswap switch 5" in out


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
        _no_helper(temp_home)
        _write_sessions(temp_home, [4102], started_at=time.time() + 5)
        alive.add(4102)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("6")
        text = str(caught.value).replace("\n", " ")
        assert "Not logged in" not in text
        assert "started while #5 ci was active keep using its key" in text
        assert isinstance(caught.value, KeyHelperUnavailableError)


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
        h.switcher._write_account_credentials("2", "key@token.local", "sk-ant-api03-" + "kk" * 20)
        return h

    EXHAUSTED = {"1": _usage(100), "2": "api key", "3": _usage(100)}

    def test_never_lands_on_a_key_while_sessions_run_and_notifies_once(
        self, temp_home, alive
    ):
        """Part 2: through the key helper the engine moves onto a key while
        sessions run (tests/test_key_helper_live.py). Where the helper cannot
        be added it never takes the saved-key path by itself, and says so
        once."""
        h = self._harness(temp_home)
        _no_helper(temp_home)
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
            if isinstance(e, NoSwitchEvent) and e.reason == "key-helper-unavailable"
        ]
        assert len(held) == 3
        assert held[0].to_json()["reason"] == "key-helper-unavailable"
        assert held[0].human() == (
            "Not switching to #2 ci: qswap cannot add its key helper to Claude "
            "Code's settings. qswap doctor says why"
        )
        for event in h.events:
            note = notifier.event("claude", event, both=False, now=0.0)
            if note is not None:
                notes.append(note.body)
        # The hold once, and (every subscription at its limit) the
        # all-exhausted notice once, as with API-key accounts left out.
        assert notes[0] == held[0].human()
        assert len(notes) == 2 and "at their limit" in notes[1]

    def test_a_new_episode_notifies_again(self, temp_home, alive):
        h = self._harness(temp_home)
        _no_helper(temp_home)
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        notifier = Notifier()
        h.tick_with_usage(self.EXHAUSTED)
        h.tick_with_usage({"1": _usage(10), "2": "api key", "3": _usage(100)})
        h.tick_with_usage(self.EXHAUSTED)
        notes = [
            n.body for n in (notifier.event("claude", e, both=False, now=0.0) for e in h.events)
            if n is not None
        ]
        assert len([n for n in notes if n.startswith("Not switching to")]) == 2

    def test_with_no_sessions_the_key_is_still_the_last_resort(self, temp_home, alive):
        h = self._harness(temp_home)
        _write_sessions(temp_home, [4101])  # a stale record: its process is gone
        assert h.tick_with_usage(self.EXHAUSTED) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_a_refusal_at_switch_time_is_the_same_decision(self, temp_home, alive):
        """Another helper set between the decision and the switch (part 2:
        sessions no longer bar the move, the helper's availability does)."""
        h = self._harness(temp_home)
        _no_helper(temp_home)
        calls = {"n": 0}

        def available_at_the_decision():
            calls["n"] += 1
            return calls["n"] == 1

        with patch.object(h.switcher, "key_helper_available", available_at_the_decision):
            outcome = h.tick_with_usage(self.EXHAUSTED)
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)][-1] == (
            "key-helper-unavailable"
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
        _no_helper(temp_home)
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("5", json_output=True)
        title, message, button = menubar.api_key_switch_question(caught.value)
        assert title == "Use the saved key instead?"
        assert "6 Claude Code sessions are running" in message
        assert button == "Switch and log out sessions"

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

    # Part 2: through the key helper session records do not matter (the
    # sessions follow it). Where the helper cannot be added, the refusal
    # still counts them, and "could not tell" is still never "nobody there".

    def _refused(self, s) -> SessionsRunningError:
        _no_helper(Path.home())
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
        assert "could not read the Claude Code session records" in str(error).replace("\n", " ")
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
        _no_helper(temp_home)
        code, out, err = run(s, "switch", "5")
        assert code == 1
        assert "Switched" not in out
        assert err.startswith(
            "Error: not switched. qswap cannot add the key helper for #5 ci to the Claude\n"
        )
        assert "could not read the Claude Code session records" in " ".join(err.split())
        assert "qswap switch 5 --log-out-sessions" in err
        # Every record is named, each path whole on a line of its own (the
        # one kind of line allowed past 80 columns).
        lines = err.splitlines()
        for pid in PIDS:
            assert f"  {directory / f'{pid}.json'}" in lines
        assert all(len(line) <= 80 for line in lines if not line.startswith("  "))
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
        _no_helper(temp_home)
        code, out, _err = run(s, "switch", "5", "--json")
        assert code == 1
        error = json.loads(out)["error"]
        assert error["type"] == "KeyHelperUnavailableError"
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
        assert "Skipped #5" not in out  # not in rotation at all now
        assert s.current_account_number() == "7"

    def test_the_engine_holds(self, temp_home, alive):
        """Part 2: the hold is the key helper's, not the session count's
        (unreadable records do not stop a move through the helper)."""
        h = TestEngine()._harness(temp_home)
        self._torn(temp_home)
        alive.add(4101)
        _no_helper(temp_home)
        assert h.tick_with_usage(TestEngine.EXHAUSTED) is TickOutcome.BLOCKED
        assert h.active_number() == 1
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert held.reason == "key-helper-unavailable"

    def test_the_engine_names_the_record_on_a_host_without_a_helper(
        self, temp_home, alive, monkeypatch,
    ):
        """Without a key helper the engine keeps part 1's hold, and its
        sentence names the record it could not read."""
        h = TestEngine()._harness(temp_home)
        self._torn(temp_home)
        alive.add(4101)
        monkeypatch.setattr(h.engine, "_key_helper_ready", lambda: None)
        assert h.tick_with_usage(TestEngine.EXHAUSTED) is TickOutcome.BLOCKED
        assert h.active_number() == 1
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert held.reason == "api-key-sessions"
        record = temp_home / ".claude" / "sessions" / "4101.json"
        assert held.human().splitlines() == [
            "Not switching to #2 ci: it is an API-key account and running sessions "
            "could not be counted",
            "This session record could not be read:",
            f"  {record}",
            "If Claude Code process 4101 is gone, delete the file.",
        ]

    def test_the_engine_holds_when_the_reader_raises(self, temp_home, alive, monkeypatch):
        """A provider without a key helper still holds when sessions cannot
        be counted (the engine's part 1 rule, kept for such hosts)."""
        h = TestEngine()._harness(temp_home)

        def boom():
            raise RuntimeError("detection broke")

        monkeypatch.setattr(h.switcher, "shared_login_blocking", boom)
        monkeypatch.setattr(h.engine, "_key_helper_ready", lambda: None)
        assert h.tick_with_usage(TestEngine.EXHAUSTED) is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_the_menu_bar_asks_without_a_count(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        self._torn(temp_home)
        alive.add(4101)
        title, message, button = menubar.api_key_switch_question(self._refused(s))
        assert title == "Use the saved key instead?"
        assert "could not read the Claude Code session records" in message
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

    # Part 2: a rotation never lands on a key, so with only keys left it
    # names the way to use one (by name), sessions or not.

    def test_it_names_the_flag_not_enable(self, temp_home, block_real_keychain, alive, run):
        s = _fleet()
        self._logged_out(s, [5, 6], 5)
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        code, out, err = run(s, "switch")
        assert code == 1
        assert "Skipped #5" not in out
        assert "qswap enable" not in err
        assert err.splitlines()[-1] == "  qswap switch 5"
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
        assert err.splitlines()[-1] == "  qswap switch 5"

    def test_with_no_sessions_it_lands_on_the_key(
        self, temp_home, block_real_keychain, alive, run
    ):
        """Not any more: a key is entered by naming it."""
        s = _fleet()
        self._logged_out(s, [5, 6], 5)
        code, _out, err = run(s, "switch")
        assert code == 1
        assert err.splitlines()[-1] == "  qswap switch 5"
        s.switch_to("5", json_output=True)
        assert s.current_account_number() == "5"


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
        # Part 2: the key goes in through the helper line, which logs no one
        # out, and the login in the item is left where it is.
        result = s.switch_to("5", json_output=True, force=True)
        assert result["how"] == "helper"
        assert _has_login(block_real_keychain)
        with pytest.raises(SessionsRunningError):
            _no_helper(temp_home)
            s.switch_to("6", json_output=True)


class TestKeyToKeyQuestion:
    def test_menu_bar_does_not_ask_to_log_anyone_out(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _no_helper(temp_home)
        _write_sessions(temp_home, [4102], started_at=time.time() + 5)
        alive.add(4102)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("6")
        assert caught.value.from_key is True
        title, _message, button = menubar.api_key_switch_question(caught.value)
        assert title == "Use the saved key instead?"
        assert button == "Switch anyway"
        assert "log out" not in (title + button).lower()

    def test_the_flag_help_does_not_promise_a_logout_for_every_session(self, capsys):
        from ai_quota_swap import helptext

        text = helptext._switch().format_help()
        assert "sessions running on a subscription stop with" in " ".join(text.split())


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


# -- second review: credentials --------------------------------------------------------

import os  # noqa: E402

from ai_quota_swap import autolog, transfer  # noqa: E402
from ai_quota_swap.locking import FileLock  # noqa: E402

KEY_NEW = "sk-ant-api03-" + "nwnwnwnwnw" * 4


def _managed_read_fails(monkeypatch, store):
    """The Keychain read of the saved key ("Claude Code") fails; every other
    item still answers."""
    real_get = store.get_password

    def get_password(service, account):
        if service == CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE:
            raise macos_keychain.KeychainError("managed item read timed out")
        return real_get(service, account)

    monkeypatch.setattr(macos_keychain, "get_password", get_password)


def _export_one(s, capsys, account="5") -> dict:
    capsys.readouterr()
    transfer.export_accounts(s, "-", account=account)
    return json.loads(capsys.readouterr().out)["accounts"][0]


class TestActiveKeyReadFailsClosed:
    """Finding 1: the login item holds only MCP sign-ins while a key is
    active; when the saved key cannot be read, that item is never the
    active credential."""

    def test_a_failed_key_read_is_a_failed_read(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        assert _login_item(block_real_keychain) == {"mcpOAuth": MCP}
        _managed_read_fails(monkeypatch, block_real_keychain)
        active = s._store._read_active_credentials()
        assert active.value is None
        assert active.keychain_unavailable is True
        assert active.degraded is True

    def test_no_saved_key_still_reads_the_item(self, temp_home, block_real_keychain):
        """Finding no key is not a failed read (a machine with no key)."""
        s = _switcher(Platform.MACOS)
        item = json.dumps({"mcpOAuth": MCP})
        block_real_keychain.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct(), item)
        assert s._store._read_active_credentials().value == item

    def test_file_store_with_an_unreadable_config_is_a_failed_read(self, temp_home):
        s = _switcher(Platform.LINUX)
        path = get_credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpOAuth": MCP}), encoding="utf-8")
        get_global_config_path().write_text("{torn", encoding="utf-8")
        assert s._store._read_active_credentials().value is None

    def test_export_of_the_active_key_when_its_read_fails(
        self, temp_home, block_real_keychain, monkeypatch, capsys
    ):
        """The reviewer's probe: the export carries #5's saved key, tagged."""
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _managed_read_fails(monkeypatch, block_real_keychain)
        entry = _export_one(s, capsys)
        assert entry.get("kind") == "api_key"
        assert entry["credentials"] == KEY_5

    def test_export_takes_the_kind_from_the_record(
        self, temp_home, block_real_keychain, monkeypatch, capsys
    ):
        """Whatever the live store answers, an active API-key account exports
        its saved key."""
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        monkeypatch.setattr(s, "_read_credentials", lambda: json.dumps({"mcpOAuth": MCP}))
        entry = _export_one(s, capsys)
        assert (entry.get("kind"), entry["credentials"]) == ("api_key", KEY_5)

    def test_an_api_key_account_with_no_key_is_never_exported_as_one(
        self, temp_home, block_real_keychain, capsys
    ):
        s = _fleet()
        user = s._store._backup_username("5", EMAIL_5)
        block_real_keychain.set_password(SECURITY_SERVICE, user, json.dumps({"mcpOAuth": MCP}))
        capsys.readouterr()
        transfer.export_accounts(s, "-")
        out, err = capsys.readouterr()
        numbers = [a["number"] for a in json.loads(out)["accounts"]]
        assert 5 not in numbers and 6 in numbers
        assert (
            "Skipping #5 api-key-5@token.local: it has no saved API key; add it again "
            "with: qswap add-token KEY --slot 5"
        ) in err


def _archive(tmp_path, *entries) -> str:
    path = tmp_path / "archive.json"
    path.write_text(json.dumps({
        "version": 1, "encrypted": False, "activeAccountNumber": None,
        "accounts": list(entries),
    }), encoding="utf-8")
    return str(path)


def _entry(email: str, credentials, *, number: int, kind: str | None = None) -> dict:
    entry = {
        "number": number, "email": email, "uuid": "", "organizationUuid": "",
        "credentials": credentials,
        "config": {"oauthAccount": {"emailAddress": email}},
    }
    if kind:
        entry["kind"] = kind
    return entry


class TestImportKeepsKinds:
    """Finding 2: import --force never writes a non-key over an API-key
    account, nor a key over a subscription, and never drops ``kind``."""

    def test_an_mcp_only_object_is_skipped(
        self, temp_home, block_real_keychain, tmp_path, capsys
    ):
        """The reviewer's probe: what an older qswap exported for a damaged #5."""
        s = _fleet()
        capsys.readouterr()
        transfer.import_accounts(
            s, _archive(tmp_path, _entry(EMAIL_5, {"mcpOAuth": MCP}, number=5)), force=True,
        )
        err = capsys.readouterr().err
        assert "Skipped #5 ci: the export holds no login or API key for it" in err.splitlines()
        assert "Done: 0 imported, 0 overwritten, 1 skipped" in err
        assert s._get_sequence_data()["accounts"]["5"].get("kind") == "api_key"
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        assert _prev(s, "5", EMAIL_5) == ""

    def test_a_login_is_never_written_over_an_api_key_account(
        self, temp_home, block_real_keychain, tmp_path, capsys
    ):
        s = _fleet()
        login = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}
        capsys.readouterr()
        transfer.import_accounts(s, _archive(tmp_path, _entry(EMAIL_5, login, number=5)),
                                 force=True)
        err = capsys.readouterr().err
        assert (
            "Skipped #5 ci: it is an API-key account and the export holds a login for "
            "it. To replace it anyway, remove it first: qswap remove 5"
        ) in err
        assert s._get_sequence_data()["accounts"]["5"].get("kind") == "api_key"
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5

    def test_a_key_is_never_written_over_a_subscription(
        self, temp_home, block_real_keychain, tmp_path, capsys
    ):
        s = _fleet()
        before = s._read_account_credentials("1", MAYA)
        capsys.readouterr()
        transfer.import_accounts(
            s, _archive(tmp_path, _entry(MAYA, KEY_NEW, number=1, kind="api_key")), force=True,
        )
        err = capsys.readouterr().err
        assert (
            "Skipped #1 personal: it is a subscription account and the export holds an "
            "API key for it. To replace it anyway, remove it first: qswap remove 1"
        ) in err
        assert "kind" not in s._get_sequence_data()["accounts"]["1"]
        assert s._read_account_credentials("1", MAYA) == before

    def test_a_new_key_for_an_api_key_account_keeps_its_kind(
        self, temp_home, block_real_keychain, tmp_path
    ):
        s = _fleet()
        transfer.import_accounts(
            s, _archive(tmp_path, _entry(EMAIL_5, KEY_NEW, number=5, kind="api_key")),
            force=True,
        )
        assert s._get_sequence_data()["accounts"]["5"].get("kind") == "api_key"
        assert s._read_account_credentials("5", EMAIL_5) == KEY_NEW

    def test_the_oldest_login_shape_is_still_imported(
        self, temp_home, block_real_keychain, tmp_path
    ):
        """Top-level tokens, from before ``claudeAiOauth``, are a login."""
        s = _fleet()
        legacy = {"accessToken": "tok-legacy", "refreshToken": "rt-legacy"}
        transfer.import_accounts(
            s, _archive(tmp_path, _entry("old@example.com", legacy, number=8)),
        )
        assert json.loads(s._read_account_credentials("8", "old@example.com")) == legacy


class TestHealUnderTheLock:
    """Finding 3: the heal writes only over the value it read, and export
    and add-token write under the account lock."""

    _damage = TestSelfHeal._damage

    def test_a_key_saved_meanwhile_is_never_overwritten(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        """The reviewer's interleaving: another qswap saves a key into the
        slot after the heal has read it."""
        s = _fleet()
        self._damage(s, block_real_keychain)
        real_prev = s._store._read_previous_backup

        def prev_then_a_concurrent_write(num, email):
            value = real_prev(num, email)
            s._store._write_account_credentials("5", EMAIL_5, KEY_NEW)
            return value

        monkeypatch.setattr(s._store, "_read_previous_backup", prev_then_a_concurrent_write)
        assert s._heal_api_key_backup("5", EMAIL_5) is False
        assert s._read_account_credentials("5", EMAIL_5) == KEY_NEW

    def test_export_heals_under_the_account_lock(
        self, temp_home, block_real_keychain, monkeypatch, capsys
    ):
        s = _fleet()
        self._damage(s, block_real_keychain)
        real_heal = s._heal_api_key_backup
        held = []

        def heal(num, email):
            probe = FileLock(s.lock_file)
            free = probe.acquire(timeout=0)
            if free:
                probe.release()
            held.append(not free)
            return real_heal(num, email)

        monkeypatch.setattr(s, "_heal_api_key_backup", heal)
        entry = _export_one(s, capsys)
        assert held == [True]
        assert entry["credentials"] == KEY_5

    def test_add_token_writes_under_the_account_lock(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        s = _fleet()
        real_write = s._store._write_account_credentials
        held = []

        def write(num, email, credentials, **kwargs):
            probe = FileLock(s.lock_file)
            free = probe.acquire(timeout=0)
            if free:
                probe.release()
            held.append(not free)
            return real_write(num, email, credentials, **kwargs)

        monkeypatch.setattr(s._store, "_write_account_credentials", write)
        s.add_account_from_token(KEY_NEW, slot=5, assume_yes=True)  # the same slot
        s.add_account_from_token(KEY_NEW, slot=9, assume_yes=True)  # a new one
        s.add_account_from_token(KEY_NEW, email=EMAIL_6)  # #6 refreshed in place
        assert held == [True, True, True]
        assert s._read_account_credentials("5", EMAIL_5) == KEY_NEW
        assert s._read_account_credentials("6", EMAIL_6) == KEY_NEW


# -- second review: guard and engine ----------------------------------------------------


def _torn_record(home: Path, pid: int) -> Path:
    directory = home / ".claude" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    path.write_text('{"pid": %d, "sessionId": "s-' % pid, encoding="utf-8")
    return path


class TestStaleUnreadableRecords:
    """Finding 4: a record that cannot be read blocks only while its process
    may be running, and every surface names it with what to do."""

    def test_a_torn_record_of_a_process_that_is_gone_is_skipped(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _torn_record(temp_home, 4242)  # 4242 is not running
        assert process_detection.scan_sessions() == ([], 0)
        code, _out, err = run(s, "switch", "5")
        assert code == 0, err
        assert s.current_account_number() == "5"

    def test_a_record_of_an_unknown_shape_and_a_dead_pid_is_skipped(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        directory = temp_home / ".claude" / "sessions"
        directory.mkdir(parents=True)
        (directory / "4243.json").write_text(json.dumps({"pid": "4243"}), encoding="utf-8")
        assert process_detection.scan_sessions() == ([], 0)
        assert s.switch_to("5", json_output=True)["switched"] is True

    def test_a_record_not_named_after_a_pid_still_refuses(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        directory = temp_home / ".claude" / "sessions"
        directory.mkdir(parents=True)
        (directory / "notes.json").write_text("{torn", encoding="utf-8")
        assert process_detection.scan_session_records() == ([], [directory / "notes.json"])
        _no_helper(temp_home)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("5", json_output=True)
        text = str(caught.value)
        assert f"  {directory / 'notes.json'}" in text.splitlines()
        assert "If that process is gone, delete the file." in text.replace("\n", " ")

    def test_the_refusal_names_the_record_and_what_to_do(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        record = _torn_record(temp_home, 4101)
        alive.add(4101)
        _no_helper(temp_home)
        code, out, err = run(s, "switch", "5")
        assert code == 1
        lines = err.splitlines()
        at = lines.index("This session record could not be read:")
        assert lines[at:] == [
            "This session record could not be read:",
            f"  {record}",
            "If Claude Code process 4101 is gone, delete the file.",
            "To switch anyway:",
            "  qswap switch 5 --log-out-sessions",
        ]
        code, out, _err = run(s, "switch", "5", "--json")
        error = json.loads(out)["error"]
        assert error["unreadableRecords"] == [str(record)]
        assert "fromKey" not in error

    def test_the_menu_bar_question_keeps_the_path_on_its_own_line(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        record = _torn_record(temp_home, 4101)
        alive.add(4101)
        _no_helper(temp_home)
        with pytest.raises(SessionsRunningError) as caught:
            s.switch_to("5", json_output=True)
        _title, message, _button = menubar.api_key_switch_question(caught.value)
        lines = message.splitlines()
        assert lines[0].startswith("qswap cannot add the key helper for #5 ci")
        assert "could not read the Claude Code session records" in " ".join(message.split())
        assert lines[-3:] == [
            "This session record could not be read:",
            f"  {record}",
            "If Claude Code process 4101 is gone, delete the file.",
        ]

    def test_the_engine_log_prints_the_path_whole(self, temp_home, alive, monkeypatch):
        """On a host without a key helper (the part 1 session rule)."""
        h = TestEngine()._harness(temp_home)
        record = _torn_record(temp_home, 4101)
        alive.add(4101)
        monkeypatch.setattr(h.engine, "_key_helper_ready", lambda: None)
        h.tick_with_usage(TestEngine.EXHAUSTED)
        lines: list[str] = []
        log = autolog.AutoLog(lines.append, clock=lambda: "12:00:00", width=80)
        for event in h.events:
            log(event)
        log.flush()
        assert any(line.strip() == str(record) for line in lines)
        assert any(
            line.strip() == "If Claude Code process 4101 is gone, delete the file."
            for line in lines
        )
        assert all(len(line) <= 80 for line in lines if line.strip() != str(record))

    def test_a_dead_torn_record_no_longer_holds_the_engine(self, temp_home, alive):
        """The reviewer's probe: six hourly ticks used to hold for good."""
        h = TestEngine()._harness(temp_home)
        _torn_record(temp_home, 4242)
        assert h.tick_with_usage(TestEngine.EXHAUSTED) is TickOutcome.SWITCHED
        assert h.active_number() == 2


R1_OFFSET_S = 3600


class TestHoldKeepsTheAllExhaustedHandling:
    """Finding 5: held off a key with every subscription at its limit, the
    engine still says so, sleeps until the first reset and notifies it."""

    def _fleet(self, temp_home, keys: bool):
        from tests.test_autoswitch import _iso_at

        h = EngineHarness(temp_home, include_api_key_accounts=keys)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        h.switcher._write_account_credentials("2", "key@token.local", "sk-ant-api03-" + "kk" * 20)
        reset = _iso_at(h.clock.now + R1_OFFSET_S)
        return h, {"1": _usage(100, reset), "2": "api key", "3": _usage(100, reset)}

    def _outcome(self, h, usage):
        outcome = h.tick_with_usage(usage)
        notifier = Notifier()
        notes = [
            n.body for n in (notifier.event("claude", e, both=False, now=0.0) for e in h.events)
            if n is not None
        ]
        return outcome, h.kinds(), h.engine._sleep_until_ts, notes

    @staticmethod
    def _hold(h, temp_home, monkeypatch, why):
        if why == "no-helper":
            _no_helper(temp_home)  # the key helper cannot be added
        elif why == "sessions":
            # A host without a key helper: the part 1 session rule.
            monkeypatch.setattr(h.engine, "_key_helper_ready", lambda: None)

    @pytest.mark.parametrize("keys, why", [
        (True, "no-helper"), (True, "sessions"), (False, None),
    ], ids=["held-no-helper", "held-for-sessions", "keys-left-out"])
    def test_held_matches_keys_left_out(self, temp_home, alive, monkeypatch, keys, why):
        from ai_quota_swap.autoswitch import RESET_SLACK_S

        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        h, usage = self._fleet(temp_home, keys=keys)
        self._hold(h, temp_home, monkeypatch, why)
        reset = h.clock.now + R1_OFFSET_S
        outcome, kinds, sleep, notes = self._outcome(h, usage)

        assert outcome is TickOutcome.BLOCKED
        assert kinds == (
            ["poll", "no-switch", "all-exhausted"] if keys else ["poll", "all-exhausted"]
        )
        assert sleep == reset + RESET_SLACK_S
        assert h.engine._next_delay(outcome) > 60
        if keys:
            assert notes[0].startswith("Not switching to #2")
            notes = notes[1:]
        assert len(notes) == 1 and "first resets" in notes[0]

    @pytest.mark.parametrize("why", ["no-helper", "sessions"])
    def test_a_candidate_with_room_to_come_keeps_the_normal_cadence(
        self, temp_home, alive, monkeypatch, why
    ):
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        h, usage = self._fleet(temp_home, keys=True)
        self._hold(h, temp_home, monkeypatch, why)
        usage["3"] = None  # unreadable this tick: it may have room
        outcome, kinds, sleep, _notes = self._outcome(h, usage)
        assert outcome is TickOutcome.BLOCKED
        assert kinds == ["poll", "no-switch"]
        assert sleep is None


class TestKeyToKeyListsEveryKey:
    """Finding 6: a key-to-key switch lists every running session started on
    any other API key, under the key it keeps."""

    def test_a_session_from_an_earlier_key_is_listed_under_it(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        s.add_account_from_token("sk-ant-api03-" + "k7k7k7k7k7" * 4, slot=7)
        t = time.time()
        with patch("time.time", return_value=t - 300):
            s.switch_to("6", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4201], started_at=t - 200)  # on #6 ops
        alive.add(4201)
        with patch("time.time", return_value=t - 100):
            s.switch_to("5", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4202], started_at=t - 50)  # on #5 ci
        alive.add(4202)

        code, out, err = run(s, "switch", "7", "--log-out-sessions")

        assert code == 0, err
        assert out.splitlines() == [
            "Switched #5 ci → #7 api-key-7 (API key)",
            "These sessions were started while #5 ci was active and keep using its key until",
            "you restart them:",
            "  pid 4202 · app4202",
            "These sessions were started while #6 ops was active and keep using its key until",
            "you restart them:",
            "  pid 4201 · app4201",
            "Claude Code sessions started from now on use the key.",
        ]

    def test_sessions_on_the_target_key_are_not_listed(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        t = time.time()
        with patch("time.time", return_value=t - 300):
            s.switch_to("6", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4201], started_at=t - 200)  # on #6 ops
        alive.add(4201)
        with patch("time.time", return_value=t - 100):
            s.switch_to("5", json_output=True, log_out_sessions=True)
        code, out, _err = run(s, "switch", "6", "--log-out-sessions")
        assert code == 0
        assert out.splitlines()[1:] == ["Claude Code sessions started from now on use the key."]


class TestZombies:
    """Finding 7: a process that has exited and waits to be reaped is gone."""

    def test_a_zombie_is_not_alive(self):
        with patch("ai_quota_swap.process_detection.sys.platform", "darwin"), \
             patch("os.kill", return_value=None), \
             patch("ai_quota_swap.process_detection._ps", return_value="Z+") as ps:
            assert process_detection.is_pid_alive(4242) is False
        ps.assert_called_once_with(4242, "stat")

    def test_a_sleeping_process_is_alive(self):
        with patch("ai_quota_swap.process_detection.sys.platform", "darwin"), \
             patch("os.kill", return_value=None), \
             patch("ai_quota_swap.process_detection._ps", return_value="Ss"):
            assert process_detection.is_pid_alive(4242) is True

    def test_an_unknown_state_is_alive(self):
        with patch("ai_quota_swap.process_detection.sys.platform", "darwin"), \
             patch("os.kill", side_effect=PermissionError("not ours")), \
             patch("ai_quota_swap.process_detection._ps", return_value=None):
            assert process_detection.is_pid_alive(4242) is True

    @pytest.mark.parametrize("text, state", [
        ("4242 (claude) Z 1 4242 4242 0 -1", "Z"),
        ("4242 (my (odd) name) S 1 4242", "S"),
        ("", None),
    ])
    def test_linux_reads_proc_stat(self, text, state):
        with patch("ai_quota_swap.process_detection.sys.platform", "linux"), \
             patch.object(pathlib.Path, "read_text", return_value=text):
            assert process_detection.process_state(4242) == state

    def test_windows_has_no_state(self):
        with patch("ai_quota_swap.process_detection.sys.platform", "win32"):
            assert process_detection.process_state(4242) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="zombies are POSIX")
    def test_a_real_unreaped_child_does_not_block_a_key_switch(
        self, temp_home, block_real_keychain
    ):
        import subprocess

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.monotonic() + 10
            while process_detection.process_state(child.pid) != "Z":
                assert time.monotonic() < deadline, "the child never became a zombie"
                time.sleep(0.05)
            _write_sessions(temp_home, [child.pid])
            assert process_detection.scan_sessions() == ([], 0)
            assert _fleet().switch_to("5", json_output=True)["switched"] is True
        finally:
            child.wait()


class _FakeKernel32:
    """The Windows calls the process checks make, on any platform."""

    def __init__(self, *, opens=True, exit_code=259, created=None):
        self.opens, self.exit_code, self.created = opens, exit_code, created
        self.closed = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - the Windows name
        return 44 if self.opens else 0

    def GetExitCodeProcess(self, handle, code):  # noqa: N802
        code.contents.value = self.exit_code
        return 1

    def GetProcessTimes(self, handle, created, exited, kernel, user):  # noqa: N802
        if self.created is None:
            return 0
        created.contents.value = self.created
        return 1

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return 1


def _filetime(epoch_s: float) -> int:
    return int((epoch_s + 11_644_473_600) * 10_000_000)


class TestWindowsRecycledPids:
    """Finding 8: on Windows the record's ``procStartFt`` is compared with the
    process's creation time, so a recycled pid is not a session."""

    STARTED = 1_788_000_000.0

    def _record(self, home: Path, pid: int, stamp: str) -> None:
        directory = home / ".claude" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": "w", "cwd": "C:/w", "startedAt": 1,
            "kind": "interactive", "entrypoint": "cli", "procStartFt": stamp,
        }), encoding="utf-8")

    @pytest.mark.parametrize("created, counted", [
        (STARTED, True),              # the recorded process
        (STARTED + 0.5, True),        # within the rounding slack
        (STARTED + 3600, False),      # created later: a recycled pid
        (STARTED - 3600, True),       # older than the record: not a stranger
        (None, True),                 # cannot tell
    ])
    def test_the_creation_time_decides(self, temp_home, created, counted):
        self._record(temp_home, 4444, str(_filetime(self.STARTED)))
        stamp = _filetime(created) if created is not None else None
        with patch("ai_quota_swap.process_detection.sys.platform", "win32"), \
             patch("ai_quota_swap.process_detection.is_pid_alive", return_value=True), \
             patch("ai_quota_swap.process_detection.process_creation_filetime",
                   return_value=stamp):
            sessions, unreadable = process_detection.scan_sessions()
        assert ([s.pid for s in sessions], unreadable) == ([4444] if counted else [], 0)

    def test_a_stamp_that_is_not_a_filetime_passes(self):
        with patch("ai_quota_swap.process_detection.sys.platform", "win32"), \
             patch("ai_quota_swap.process_detection.process_creation_filetime",
                   return_value=_filetime(self.STARTED + 86400)):
            assert process_detection.pid_matches_record(4444, "11485") is True
            assert process_detection.pid_matches_record(4444, "900000000000000000") is True

    def test_the_api_calls_through_a_fake_kernel32(self):
        fake = _FakeKernel32(created=_filetime(self.STARTED))
        with patch("ai_quota_swap.process_detection.sys.platform", "win32"), \
             patch("ai_quota_swap.process_detection._kernel32", return_value=fake):
            assert process_detection._is_pid_alive_windows(4444) is True
            assert process_detection.process_creation_filetime(4444) == _filetime(self.STARTED)
            fake.exit_code = 0  # exited, a handle still open somewhere
            assert process_detection._is_pid_alive_windows(4444) is False
            fake.opens = False
            assert process_detection._is_pid_alive_windows(4444) is False
            assert process_detection.process_creation_filetime(4444) is None
        assert fake.closed == [44, 44, 44]

    def test_off_windows_there_is_no_creation_time(self):
        with patch("ai_quota_swap.process_detection.sys.platform", "linux"):
            assert process_detection.process_creation_filetime(os.getpid()) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="the real Windows API")
    def test_the_real_windows_api(self):
        import subprocess

        created = process_detection.process_creation_filetime(os.getpid())
        assert created is not None
        started = created / 10_000_000 - 11_644_473_600
        assert started <= time.time() + 1
        assert process_detection._is_pid_alive_windows(os.getpid()) is True
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        assert process_detection._is_pid_alive_windows(child.pid) is False


class TestRotationJsonReason:
    """Finding 9: a rotation left with only API keys says so in JSON. Part 2:
    a rotation never lands on a key, so the reason is api-key-only and the
    message names the switch that goes to one."""

    def test_the_reason_and_message_name_the_key(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        code, out, _err = run(s, "switch", "--json")
        assert code == 0
        payload = json.loads(out)
        assert payload["switched"] is False
        assert payload["reason"] == "api-key-only"
        assert payload["message"] == (
            "No other account can take over, so you stay on #1 personal. To use an "
            "API-key account, name it: qswap switch 5"
        )
        assert not any("Skipped" in w for w in payload["warnings"])

    def test_no_valid_target_is_unchanged(self, temp_home, block_real_keychain, alive, run):
        s = _fleet()
        for number in ("5", "6"):
            data = s._get_sequence_data()
            data["accounts"][number]["disabled"] = True
            s._write_json(s.sequence_file, data)
        code, out, _err = run(s, "switch", "--json")
        assert json.loads(out)["reason"] == "no-valid-target"


# -- second review: merged state --------------------------------------------------------


class TestReactivatingTheActiveKeyOnTheFileStore:
    """Finding 10: with no .credentials.json (an older qswap deleted it),
    writing the active key again changes nothing in the login store."""

    def test_force_on_the_active_key_is_not_refused(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet(Platform.LINUX)
        s.switch_to("5", json_output=True, log_out_sessions=True)
        get_credentials_path().unlink()
        _write_sessions(temp_home, PIDS)
        alive.update(PIDS)
        code, _out, err = run(s, "switch", "5", "--force")
        assert code == 0, err
        assert not get_credentials_path().exists()
        assert _config()["primaryApiKey"] == KEY_5

    def test_on_macos_an_absent_item_is_still_refused(
        self, temp_home, block_real_keychain, alive
    ):
        """There the item would be written as {}, which logs sessions out."""
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        block_real_keychain.delete_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        # Part 2: the switch goes through the key helper and writes no item.
        assert s.switch_to("5", json_output=True, force=True)["how"] == "helper"
        assert block_real_keychain.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct()) is None
        # Where the helper cannot be added, it is refused.
        _no_helper(temp_home)
        with pytest.raises(SessionsRunningError):
            s.switch_to("5", json_output=True, force=True)


class TestReactivatingWithANewKey:
    """Finding 11: loading a changed key for the active account says that
    running sessions keep the previous one, and lists them."""

    def _imported_new_key(self, s, tmp_path):
        path = tmp_path / "export.json"
        transfer.export_accounts(s, str(path), account="5")
        envelope = json.loads(path.read_text())
        envelope["accounts"][0]["credentials"] = KEY_NEW
        path.write_text(json.dumps(envelope))
        transfer.import_accounts(s, str(path), force=True)

    def test_running_sessions_are_listed_with_the_previous_key(
        self, temp_home, block_real_keychain, alive, run, tmp_path
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        self._imported_new_key(s, tmp_path)
        _write_sessions(temp_home, [4101, 4102], started_at=time.time() + 5)
        alive.update({4101, 4102})

        code, out, err = run(s, "switch", "5", "--force", "--log-out-sessions")

        assert code == 0, err
        assert out.splitlines()[1:] == [
            "These sessions were started while #5 ci was active and keep using its previous",
            "key until you restart them:",
            "  pid 4101 · app4101",
            "  pid 4102 · app4102",
            "Claude Code sessions started from now on use the key.",
        ]
        assert block_real_keychain.get_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()
        ) == KEY_NEW
        code, out, _err = run(s, "switch", "5", "--force", "--log-out-sessions", "--json")
        assert json.loads(out)["followup"] == ["Claude Code sessions started from now on use the key."]

    def test_the_same_key_again_says_only_the_usual(
        self, temp_home, block_real_keychain, alive, run
    ):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        _write_sessions(temp_home, [4101], started_at=time.time() + 5)
        alive.add(4101)
        code, out, _err = run(s, "switch", "5", "--force", "--log-out-sessions", "--json")
        assert code == 0
        assert json.loads(out)["followup"] == ["Claude Code sessions started from now on use the key."]


class _ActiveKeyRefusedFake(FakeSwitcher):
    """#5 ci, an API key, is active; the switcher refuses with the
    ``from_key`` it would give (False for re-activating #5 itself)."""

    def __init__(self, *args, from_key: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.from_key = from_key

    def account_kind_for(self, number):
        return "api_key" if str(number) in ("5", "6") else "oauth"

    def switch_to(self, identifier, json_output=False, force=False, log_out_sessions=False):
        if not log_out_sessions:
            raise SessionsRunningError(
                display.api_key_refusal(
                    "#5 ci", 5, 6, from_key="#6 ops" if self.from_key else None,
                ),
                number=str(identifier), sessions=6, from_key=self.from_key,
            )
        return super().switch_to(identifier, json_output=json_output, force=force)


@pytest.mark.asyncio
class TestDashboardFollowsTheRefusal:
    """Finding 12: the dialog's title and button come from the refusal's own
    ``fromKey``, not from whether the active account is a key."""

    async def _dialog(self, tmp_path, *, from_key: bool):
        from textual.widgets import Static

        from ai_quota_swap.tui.modals import ConfirmModal

        fake = _ActiveKeyRefusedFake(
            [make_account(1), make_account(5, alias="ci", active=True)], tmp_path,
            from_key=from_key,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("claude", "5")
            await settle(pilot)
            assert isinstance(app.screen, ConfirmModal)
            body = app.screen.query_one(".modal-body", Static).render().plain
            return app.screen._title, app.screen._yes_label, body

    async def test_re_activating_the_active_key(self, tmp_path):
        title, button, body = await self._dialog(tmp_path, from_key=False)
        assert title == "Log out 6 running sessions?"
        assert button == "Switch and log out sessions"
        assert 'stops with "Not logged in"' in body

    async def test_a_key_to_key_switch(self, tmp_path):
        title, button, body = await self._dialog(tmp_path, from_key=True)
        assert title == "Switch keys with 6 sessions running?"
        assert button == "Switch anyway"
        assert "Not logged in" not in body

    async def test_the_envelope_carries_from_key(self):
        error = SessionsRunningError("x", number="6", sessions=1, from_key=True)
        assert error.envelope_fields == {"sessions": 1, "fromKey": True}
