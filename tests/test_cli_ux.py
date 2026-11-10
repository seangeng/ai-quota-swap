"""The command line's UX rules (docs/design/ux.md), as behaviour.

Portable: in-process, temp home, no harness. The scenario-level checks (a
dead login refused by `switch`, the engine's set-aside account in `list`)
are in test_cli_ux_scenarios.py, which needs the POSIX demo sandbox.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_quota_swap import cli, helptext
from ai_quota_swap.autoswitch import (
    HEARTBEAT_FILENAME,
    engine_running,
    read_heartbeat,
    set_aside_accounts,
    write_heartbeat,
)


def _main(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["qswap", *argv]):
        try:
            cli.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("ai_quota_swap.update_check.check_for_update", lambda v: None)
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)


class TestPurge:
    def _mock(self, purged):
        switcher = MagicMock()
        switcher.purge.return_value = purged
        return switcher

    def test_cancelling_changes_nothing_not_even_the_menu_bar(self, monkeypatch):
        """Regression: a cancelled purge used to uninstall the menu bar anyway."""
        monkeypatch.setattr(sys, "platform", "darwin")
        uninstall = MagicMock()
        monkeypatch.setattr("ai_quota_swap.launch_agent.uninstall", uninstall)
        with patch.object(cli, "ClaudeAccountSwitcher", return_value=self._mock(False)):
            assert _main(["purge"]) == 0
        uninstall.assert_not_called()

    def test_a_purge_that_ran_removes_the_menu_bar(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        uninstall = MagicMock(return_value={"was_loaded": False, "removed": []})
        monkeypatch.setattr("ai_quota_swap.launch_agent.uninstall", uninstall)
        with patch.object(cli, "ClaudeAccountSwitcher", return_value=self._mock(True)):
            assert _main(["purge", "--yes"]) == 0
        uninstall.assert_called_once()

    def test_answering_no_returns_false_and_keeps_the_data(self, temp_home, capsys):
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        with patch("ai_quota_swap.prompts.interactive", return_value=True), \
             patch("ai_quota_swap.prompts.read_line", return_value="n"):
            assert switcher.purge() is False
        assert switcher.backup_dir.exists()
        assert "Cancelled; nothing was removed." in capsys.readouterr().out

    def test_off_a_terminal_it_names_yes_and_removes_nothing(self, temp_home, capsys):
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        ClaudeAccountSwitcher()._setup_directories()
        assert _main(["purge"]) == 1
        out, err = capsys.readouterr()
        assert err.strip() == "Error: qswap purge needs --yes when there is no terminal to ask on"
        assert out == ""  # not even the list of what it would remove
        assert ClaudeAccountSwitcher().backup_dir.exists()


class TestNoPromptOffATerminal:
    def _two(self, temp_home, sample_sequence_data):
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    def test_remove_stops_with_one_line(self, temp_home, sample_sequence_data, capsys):
        switcher = self._two(temp_home, sample_sequence_data)
        assert _main(["remove", "2"]) == 1
        assert capsys.readouterr().err.strip() == (
            "Error: qswap remove needs --yes when there is no terminal to ask on"
        )
        assert "2" in switcher._get_sequence_data()["accounts"]

    def test_remove_with_yes_or_after_other_options(self, temp_home, sample_sequence_data, capsys):
        switcher = self._two(temp_home, sample_sequence_data)
        with patch.object(type(switcher), "_delete_account_files"):
            assert _main(["remove", "--yes", "2"]) == 0
        assert "2" not in switcher._get_sequence_data()["accounts"]
        assert "Removed #2" in capsys.readouterr().out

    def test_add_onto_a_taken_slot_needs_yes(self, temp_home, mock_claude_config,
                                              sample_sequence_data, capsys):
        from ai_quota_swap.exceptions import ConfirmationRequiredError

        switcher = self._two(temp_home, sample_sequence_data)
        email = sample_sequence_data["accounts"]["2"]["email"]
        with pytest.raises(ConfirmationRequiredError, match="qswap add --slot 2 needs --yes"):
            switcher.add_account(slot=2)
        assert switcher._get_sequence_data()["accounts"]["2"]["email"] == email

    def test_add_token_without_a_token_off_a_terminal(self, temp_home, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert _main(["add-token"]) == 1
        err = capsys.readouterr().err
        assert "no token given" in err and "qswap add-token -" in " ".join(err.split())
        assert "Traceback" not in err


class TestReadOnlyFirstRun:
    def test_list_with_an_unmanaged_login_never_offers_to_add(
        self, temp_home, mock_claude_config, capsys
    ):
        with patch("builtins.input", side_effect=AssertionError("list asked a question")):
            assert _main(["list"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines() == [
            "No accounts are managed yet.",
            "Claude Code is logged in as test@example.com. Add it with: qswap add",
        ]
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        assert not ClaudeAccountSwitcher().sequence_file.exists()  # nothing written

    def test_status_says_the_same(self, temp_home, mock_claude_config, capsys):
        assert _main(["status"]) == 0
        assert capsys.readouterr().out.splitlines() == [
            "No accounts are managed yet.",
            "Claude Code is logged in as test@example.com. Add it with: qswap add",
        ]

    def test_switch_with_nothing_managed_points_at_add(self, temp_home, mock_claude_config, capsys):
        assert _main(["switch"]) == 1
        assert capsys.readouterr().err.strip() == (
            "Error: No accounts are managed yet. Add the current login with: qswap add"
        )


class TestHelpForEveryVerb:
    VERBS = sorted({*helptext.LEGACY_VERB_HELP, *cli._OWN_PARSER_VERBS} - {"help"})
    CANONICAL = {"resets": "reset", "rm": "remove", "ls": "list", "update": "upgrade"}

    @pytest.mark.parametrize("verb", VERBS)
    def test_verb_help(self, verb, temp_home, capsys):
        assert _main([verb, "--help"]) == 0
        own = capsys.readouterr().out
        assert own.startswith(f"usage: qswap {self.CANONICAL.get(verb, verb)}")
        assert "examples:" in own
        assert all(len(line) <= 80 for line in own.splitlines()), verb
        assert _main(["help", verb]) == 0
        assert capsys.readouterr().out == own

    @pytest.mark.parametrize("verb", [
        "add", "login", "list", "status", "switch", "remove", "disable", "enable",
        "alias", "threshold", "priority", "auto", "reset",
    ])
    def test_codex_verb_help(self, verb, temp_home, capsys):
        assert _main(["codex", verb, "--help"]) == 0
        own = capsys.readouterr().out
        assert own.startswith(f"usage: qswap codex {verb}")
        assert "examples:" in own
        assert all(len(line) <= 80 for line in own.splitlines()), verb
        _main(["codex", "help", verb])
        assert capsys.readouterr().out == own

    def test_the_legacy_flag_spellings_still_work(self, temp_home, capsys):
        assert _main(["--list"]) == 0
        assert "No accounts are managed yet." in capsys.readouterr().out


class TestUsageErrors:
    @pytest.mark.parametrize("argv, message", [
        (["frobnicate"], "unknown command 'frobnicate'; qswap help lists them"),
        (["remove"], "remove needs an account (number, email or alias)"),
        (["export"], "export needs a file to write"),
        (["codex", "nosuch"], "unknown command 'nosuch'; qswap codex help lists them"),
        (["auto", "--bogus"], "unrecognized arguments: --bogus"),
    ])
    def test_exit_64_naming_the_verb_not_a_hidden_flag(self, argv, message, temp_home, capsys):
        assert _main(argv) == 64
        err = " ".join(capsys.readouterr().err.split())
        assert message in err
        assert "--remove-account" not in err and "--export" not in err

    def test_under_json_a_usage_error_is_the_envelope(self, temp_home, capsys):
        assert _main(["priority", "2", "--json"]) == 64
        assert json.loads(capsys.readouterr().out) == {
            "schemaVersion": 1,
            "error": {"type": "UsageError", "message": "TIER is required (or pass --unset)"},
        }


class TestPriorityJson:
    def test_list_and_set(self, temp_home, sample_sequence_data, capsys):
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        assert _main(["priority", "2", "5", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "schemaVersion": 1,
            "account": {"number": 2, "email": sample_sequence_data["accounts"]["2"]["email"]},
            "priority": 5,
        }
        assert _main(["priority", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed["schemaVersion"] == 1
        assert {a["number"]: a["priority"] for a in listed["accounts"]}[2] == 5
        assert _main(["priority", "2", "x", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["type"] == "ValidationError"


class TestAutoFlagsOutOfRange:
    def test_a_clamped_flag_says_so(self, temp_home, capsys):
        engine = MagicMock()
        engine.tick.return_value = MagicMock(value=2)
        with patch("ai_quota_swap.autoswitch.AutoSwitchEngine", return_value=engine):
            assert _main(["auto", "--once", "--threshold", "20", "--interval", "5"]) == 2
        out = capsys.readouterr().out
        assert "Warning: --threshold 20 is out of range; using 50%" in out
        assert "Warning: --interval 5 is out of range; using 15s" in out


class TestHeartbeat:
    def test_written_read_and_judged(self, tmp_path):
        assert read_heartbeat(tmp_path) is None
        write_heartbeat(tmp_path, mode="loop", dry_run=False, last_tick_at=1000.0,
                        next_tick_at=1060.0)
        beat = read_heartbeat(tmp_path)
        assert beat["mode"] == "loop" and beat["lastTickAt"] == 1000.0
        assert engine_running(beat, 1100.0)
        assert not engine_running(beat, 1060.0 + 121)
        write_heartbeat(tmp_path, mode="once", dry_run=False, last_tick_at=1000.0,
                        next_tick_at=None)
        assert not engine_running(read_heartbeat(tmp_path), 1000.0)

    def test_status_says_whether_auto_switch_runs(self, temp_home, monkeypatch):
        from ai_quota_swap import paths

        root = paths.get_backup_root()
        root.mkdir(parents=True, exist_ok=True)
        assert cli._auto_switch_line(cli._auto_switch_state()) == (
            "Auto-switch is not running. Start it with: qswap auto"
        )
        now = time.time()
        write_heartbeat(root, mode="loop", dry_run=True, last_tick_at=now - 30,
                        next_tick_at=now + 30)
        line = cli._auto_switch_line(cli._auto_switch_state())
        assert line.startswith("Auto-switch is running for Claude Code in dry-run mode")
        assert "last check" in line
        assert (root / HEARTBEAT_FILENAME).exists()

    def test_a_loop_engine_writes_it(self, tmp_path):
        from ai_quota_swap.autoswitch import AutoSwitchEngine
        from ai_quota_swap.settings import AutoSwitchSettings

        switcher = MagicMock()
        switcher.backup_dir = tmp_path
        switcher.switchable_account_numbers.return_value = []
        switcher.account_policies.return_value = {}
        engine = AutoSwitchEngine(switcher, AutoSwitchSettings(), lambda e: None, dry_run=True,
                                  state_path=tmp_path / "state.json")
        ticks = []

        def tick():
            ticks.append(1)
            engine.stop()
            from ai_quota_swap.autoswitch import TickOutcome

            return TickOutcome.NO_ACTION

        engine.tick = tick
        engine._next_delay = lambda outcome: 0.0
        engine.run_loop()
        beat = read_heartbeat(tmp_path)
        assert beat is not None and beat["mode"] == "loop" and beat["dryRun"] is True


class TestSetAside:
    def test_read_only_and_released_by_a_replaced_login(self, tmp_path):
        (tmp_path / "autoswitch_state.json").write_text(json.dumps({"quarantine": {
            "4": {"email": "s@example.org", "reason": "invalid_grant",
                  "refreshTokenFingerprint": "fp-dead"},
        }}))
        assert set_aside_accounts(tmp_path) == {"4": "invalid_grant"}
        same = lambda n: ("s@example.org", "fp-dead")  # noqa: E731
        replaced = lambda n: ("s@example.org", "fp-new")  # noqa: E731
        assert set_aside_accounts(tmp_path, identity=same) == {"4": "invalid_grant"}
        assert set_aside_accounts(tmp_path, identity=replaced) == {}

    def test_codex_switch_refuses_an_account_the_engine_set_aside(self):
        from ai_quota_swap.codex.switcher import CodexSwitcher
        from ai_quota_swap.exceptions import SwitchError

        switcher = MagicMock(spec=CodexSwitcher)
        switcher.label.return_value = "#2 work"
        switcher.set_aside_accounts.return_value = {"2": "invalid_grant"}
        with pytest.raises(SwitchError) as exc:
            CodexSwitcher.check_switch_target(switcher, "2")
        assert "needs re-login" in str(exc.value)
        assert "qswap codex switch 2 --force" in str(exc.value)
        notes = CodexSwitcher.check_switch_target(switcher, "2", force=True)
        assert notes == ["#2 work needs re-login (its saved login was refused); "
                         "switching anyway (--force)"]


def test_status_json_gains_codex_and_auto_switch(temp_home, monkeypatch, capsys):
    switcher = MagicMock()
    switcher.managed_count.return_value = 1
    switcher.status.return_value = {"schemaVersion": 1, "active": None}
    with patch.object(cli, "ClaudeAccountSwitcher", return_value=switcher):
        assert _main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == 1 and payload["active"] is None
    assert payload["autoSwitch"] == {
        "running": False, "providers": [], "dryRun": False, "lastTickAt": None,
    }


def test_main_help_is_a_map_that_fits_a_screen(capsys):
    assert _main(["--help"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) <= 52 and max(map(len, lines)) <= 80
    assert "Setup and maintenance" in lines and "doctor" in " ".join(lines)


def test_the_shared_module_has_no_io():
    """display.py is imported by the dashboard and the menu bar: plain functions."""
    source = Path(cli.__file__).with_name("display.py").read_text(encoding="utf-8")
    for forbidden in ("open(", "print(", "subprocess", "urlopen", "time.time("):
        assert forbidden not in source, forbidden
