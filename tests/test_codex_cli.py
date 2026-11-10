"""``qswap codex <verb>`` in text and JSON, and the Codex section of ``qswap list``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_quota_swap import cli, codex_cli
from ai_quota_swap.codex import processes as codex_processes
from ai_quota_swap.codex import usage as codex_usage
from ai_quota_swap.codex.processes import RestartResult
from ai_quota_swap.codex.usage import UsageOutcome
from tests.codex_helpers import (
    DAY,
    LOGIN_A,
    LOGIN_B,
    FakeFetch,
    make_login,
    usage_payload,
    window,
    write_live,
)


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    monkeypatch.setattr("ai_quota_swap.update_check.check_for_update", lambda v: None)


@pytest.fixture
def env(temp_home: Path, tmp_path: Path, monkeypatch) -> SimpleNamespace:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fetch = FakeFetch({
        "acct-a": UsageOutcome(usage=codex_usage.normalize_usage(
            usage_payload(window(604_800, 42, 4 * DAY)))),
        "acct-b": UsageOutcome(usage=codex_usage.normalize_usage(
            usage_payload(window(18_000, 12, 7000), window(604_800, 55, 2 * DAY), plan="plus"))),
    })
    monkeypatch.setattr(codex_usage, "fetch_usage", fetch)
    return SimpleNamespace(home=temp_home, codex_home=codex_home.resolve(), fetch=fetch)


def run(argv: list[str], capsys, *, answer: str | None = None) -> tuple[int, str, str]:
    with patch.object(sys, "argv", ["qswap", *argv]):
        try:
            if answer is None:
                cli.main()
            else:
                with patch("builtins.input", return_value=answer), patch("ai_quota_swap.prompts.interactive", return_value=True):
                    cli.main()
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def two(env, capsys):
    """B is account 1, A is account 2 and live."""
    write_live(env.codex_home, make_login(**LOGIN_B, plan="plus"))
    assert run(["codex", "add"], capsys)[0] == 0
    write_live(env.codex_home, make_login(**LOGIN_A))
    assert run(["codex", "add"], capsys)[0] == 0
    return env


class TestVerbs:
    def test_add(self, env, capsys):
        write_live(env.codex_home, make_login(**LOGIN_A))
        code, out, _ = run(["codex", "add", "--alias", "main"], capsys)
        assert code == 0
        assert "Added #1 main (a@example.com)" in out
        code, out, _ = run(["codex", "add"], capsys)
        assert "Updated the saved login of #1 main (a@example.com)" in out

    def test_add_without_a_login_is_one_line(self, env, capsys):
        code, out, err = run(["codex", "add"], capsys)
        assert code == 1
        # One message, wrapped to 80 columns, with the next step.
        assert err.startswith("Error: No Codex login found")
        assert err.count("Error:") == 1
        # Wrapped at 80 columns; only a path too long for any line runs over.
        assert all(len(line) <= 80 or " " not in line.strip() for line in err.splitlines())
        assert "qswap codex add" in " ".join(err.split())

    def test_add_help_explains_the_second_account_and_warns_about_logout(self, env, capsys):
        code, out, _ = run(["codex", "add", "--help"], capsys)
        assert code == 0
        assert "qswap codex login" in " ".join(out.split())
        help_map = run(["codex", "help"], capsys)[1]
        assert 'CODEX_HOME="$tmp" codex login' in help_map
        assert 'CODEX_HOME="$tmp" qswap codex add' in help_map
        assert "codex logout" in out
        assert "signs the current login out" in " ".join(help_map.split())

    def test_list_text(self, two, capsys):
        code, out, _ = run(["codex", "list"], capsys)
        assert code == 0
        lines = out.splitlines()
        assert lines[0] == "Codex accounts"
        assert lines[1] == "  #1 b@example.com · Plus"
        assert lines[2].startswith("     ├ 5h      12%  resets ")
        assert lines[3].startswith("     └ 7d      55%  resets ")
        assert "  #2 a@example.com · active · Pro" in lines
        assert any(line.startswith("     └ 7d      42%") for line in lines)

    def test_list_json(self, two, capsys):
        code, out, _ = run(["codex", "list", "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert set(payload) == {"schemaVersion", "activeAccountNumber", "accounts"}
        assert payload["schemaVersion"] == 1
        assert payload["activeAccountNumber"] == 2
        first, second = payload["accounts"]
        assert first["number"] == 1 and first["active"] is False
        assert first["organizationUuid"] == "acct-b"
        assert first["isOrganization"] is False
        assert first["usageStatus"] == "ok"
        assert first["usage"]["fiveHour"]["pct"] == 12.0
        assert first["usage"]["sevenDay"]["pct"] == 55.0
        assert first["plan"]["label"] == "Plus"
        assert second["active"] is True and "fiveHour" not in second["usage"]

    def test_list_with_nothing_managed(self, env, capsys):
        code, out, _ = run(["codex", "list"], capsys)
        assert code == 0
        assert "No Codex accounts are managed yet." in out

    def test_status_text_and_json(self, two, capsys):
        code, out, _ = run(["codex", "status"], capsys)
        assert code == 0
        assert out.splitlines()[0] == "Codex"
        assert out.splitlines()[1] == "  #2 a@example.com · active · Pro"
        assert "2 Codex accounts are managed." in out
        code, out, _ = run(["codex", "status", "--json"], capsys)
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["active"]["number"] == 2
        assert payload["active"]["managed"] is True
        assert payload["active"]["usageStatus"] == "ok"
        assert payload["totalManagedAccounts"] == 2

    def test_status_without_a_login(self, env, capsys):
        out = run(["codex", "status"], capsys)[1]
        assert out.splitlines()[0] == "No Codex accounts are managed yet."
        assert "Codex is not logged in. Run codex login, then: qswap codex add" in out
        assert json.loads(run(["codex", "status", "--json"], capsys)[1]) == {
            "schemaVersion": 1, "active": None,
        }

    def test_status_of_an_unmanaged_login(self, env, capsys):
        write_live(env.codex_home, make_login(**LOGIN_A))
        assert "Codex is logged in as a@example.com. Add it with: qswap codex add" in run(
            ["codex", "status"], capsys)[1]
        assert json.loads(run(["codex", "status", "--json"], capsys)[1])["active"] == {
            "email": "a@example.com", "managed": False,
        }

    def test_switch_text(self, two, capsys, monkeypatch):
        monkeypatch.setattr(codex_processes, "background_server_pid", lambda home: 4242)
        monkeypatch.setattr(codex_processes, "restart_background_server",
                            lambda home, timeout=30: RestartResult(True))
        code, out, _ = run(["codex", "switch", "1"], capsys)
        assert code == 0
        lines = out.splitlines()
        assert lines[0] == "Switched #2 a@example.com → #1 b@example.com"
        assert lines[1] == "  #1 b@example.com · active · Plus"
        assert lines[-1] == (
            "Restarted Codex's background server (pid 4242); new sessions use this account."
        )
        assert all(len(line) <= 80 for line in lines)

    def test_switch_json_and_rotation(self, two, capsys):
        code, out, _ = run(["codex", "switch", "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["switched"] is True
        assert payload["strategy"] == "rotation"
        assert payload["to"] == {"number": 1, "email": "b@example.com"}
        code, out, _ = run(["codex", "switch", "2", "--json"], capsys)
        assert json.loads(out)["strategy"] == "direct"

    def test_switch_errors_use_the_envelope(self, two, capsys):
        code, out, err = run(["codex", "switch", "9", "--json"], capsys)
        assert code == 1
        assert json.loads(out) == {
            "schemaVersion": 1,
            "error": {
                "type": "AccountNotFoundError",
                "message": "#9 does not exist; qswap codex list shows the accounts",
            },
        }
        assert err == ""

    def test_switch_refuses_an_unmanaged_login(self, two, capsys):
        write_live(two.codex_home, make_login(email="c@example.com", user_id="user-c", account_id="acct-c"))
        code, _, err = run(["codex", "switch", "1"], capsys)
        assert code == 1
        assert "not managed by qswap" in err and "--force" in err
        code, out, _ = run(["codex", "switch", "1", "--force", "--json"], capsys)
        assert code == 0 and json.loads(out)["switched"] is True

    def test_force_needs_an_account(self, two, capsys):
        code, _, err = run(["codex", "switch", "--force"], capsys)
        assert code == 64
        assert "--force can only be used with 'switch ACCOUNT'" in err

    def test_remove(self, two, capsys):
        def no_codex(*a, **k):
            raise AssertionError("remove started a process")

        with patch("ai_quota_swap.spawn.run", no_codex), patch("subprocess.run", no_codex):
            code, out, err = run(["codex", "remove", "1"], capsys, answer="y")
        assert code == 0
        assert "Removed #1 b@example.com" in out
        assert "codex logout" in out + err
        code, out, _ = run(["codex", "remove", "--help"], capsys)
        assert "codex logout" in out

    def test_alias(self, two, capsys):
        assert "Named #1 work (b@example.com)" in run(["codex", "alias", "1", "Work"], capsys)[1]
        out = run(["codex", "alias"], capsys)[1]
        assert out.splitlines() == ["Aliases", "  #1 work (b@example.com)"]
        assert run(["codex", "switch", "work", "--json"], capsys)[0] == 0
        assert "Removed the alias of #1 b@example.com" in run(
            ["codex", "alias", "1", "--unset"], capsys)[1]
        assert "No aliases set. Name an account with: qswap codex alias N NAME" in run(
            ["codex", "alias"], capsys)[1]
        assert run(["codex", "alias", "1"], capsys)[0] == 64

    def test_disable_and_enable(self, two, capsys):
        assert "Disabled #1 b@example.com" in run(["codex", "disable", "1"], capsys)[1]
        assert "Enabled #1 b@example.com" in run(["codex", "enable", "1"], capsys)[1]

    def test_threshold(self, two, capsys):
        assert "Set the switch line of #1 b@example.com to 95%" in run(
            ["codex", "threshold", "1", "95"], capsys)[1]
        out = run(["codex", "threshold"], capsys)[1]
        assert "  #1 b@example.com  95%" in out
        assert "default (codex.threshold)" in out
        assert "Used only by qswap codex auto" in out
        code, _, err = run(["codex", "threshold", "1", "10"], capsys)
        assert code == 1 and err.startswith("Error: ") and "50" in err

    def test_priority(self, two, capsys):
        assert "Set the priority of #2 a@example.com to 3 (used by qswap codex auto)" in run(
            ["codex", "priority", "2", "3"], capsys)[1]
        out = run(["codex", "priority"], capsys)[1]
        assert "  #2 a@example.com  3" in out
        assert run(["codex", "priority", "2", "x"], capsys)[0] == 1

    def test_keyring_mode_stops_every_verb_with_one_line(self, two, capsys):
        (two.codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        for argv in (["codex", "list"], ["codex", "alias"], ["codex", "remove", "1"],
                     ["codex", "threshold"]):
            code, out, err = run(argv, capsys, answer="y")
            assert code == 1, argv
            # One message (wrapped to 80 columns) that says what to do.
            assert err.count("Error:") == 1, argv
            assert all(
                len(line) <= 80 or " " not in line.strip() for line in err.splitlines()
            ), argv
            assert "file storage only" in " ".join(err.split())
            assert "codex login" in " ".join(err.split())
        code, out, _ = run(["codex", "status", "--json"], capsys)
        assert code == 1 and json.loads(out)["error"]["type"] == "CodexLoginError"


class TestUnifiedList:
    def test_no_codex_section_and_no_request_without_codex_accounts(self, env, capsys):
        code, out, _ = run(["list"], capsys)
        assert code == 0
        assert "Codex" not in out
        payload = json.loads(run(["list", "--json"], capsys)[1])
        assert "providers" not in payload
        assert env.fetch.calls == []

    def test_text_section_after_the_claude_accounts(self, two, capsys):
        code, out, _ = run(["list"], capsys)
        assert code == 0
        lines = out.splitlines()
        start = lines.index("Codex accounts")
        assert lines.index("  No Claude Code accounts are managed yet.") < start
        assert "  #2 a@example.com · active · Pro" in lines[start:]

    def test_json_adds_providers_codex_and_keeps_the_top_level(self, two, capsys):
        payload = json.loads(run(["list", "--json"], capsys)[1])
        assert payload["schemaVersion"] == 1
        assert payload["activeAccountNumber"] is None
        assert payload["accounts"] == []
        codex = payload["providers"]["codex"]
        assert codex["activeAccountNumber"] == 2
        assert [row["number"] for row in codex["accounts"]] == [1, 2]
        codex_only = json.loads(run(["codex", "list", "--json"], capsys)[1])
        assert codex["accounts"] == codex_only["accounts"]

    def test_a_codex_problem_never_fails_the_claude_list(self, two, capsys):
        (two.codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        code, out, err = run(["list"], capsys)
        assert code == 0
        section = out[out.index("Codex accounts"):].splitlines()
        assert "Codex accounts unavailable" in section[1]
        assert "file storage only" in " ".join(" ".join(section[1:]).split())
        payload = json.loads(run(["list", "--json"], capsys)[1])
        assert payload["providers"]["codex"]["error"]["type"] == "CodexLoginError"
        assert payload["providers"]["codex"]["accounts"] == []

    def test_import_usage_ignores_the_providers_key(self, two, capsys, tmp_path):
        doc = tmp_path / "usage.json"
        doc.write_text(run(["list", "--json"], capsys)[1])
        code, _, err = run(["import-usage", str(doc)], capsys)
        assert code == 0
        assert "Done: 0 adopted" in err

    def test_managed_check_reads_only_the_registry(self, env, monkeypatch):
        monkeypatch.setattr("ai_quota_swap.codex.switcher.CodexSwitcher.__init__",
                            lambda *a, **k: pytest.fail("built a switcher"))
        assert codex_cli.codex_accounts_managed() is False
        assert codex_cli.extend_unified_list({"accounts": []}, json_output=True) == {"accounts": []}
