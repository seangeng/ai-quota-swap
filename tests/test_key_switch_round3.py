"""Round 3 of pull request #13 (the API-key switch), the switching side and
the surfaces: decisions SS3, SS4, SS9 with DS16, DS7 (points 3 and 4), DS11,
DS17 and E1 of pr13-round3-decisions.md. Everything runs on the temporary
home, the in-memory Keychain fake and faked network, as
tests/test_key_helper_live.py does, whose fleet and helpers these tests share.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_quota_swap import claude_settings, key_activation
from tests.test_key_helper_live import (
    QSWAP,
    pin_key_helper_platform,
    _fleet,
    _settings,
)


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    """The installed command every line names, taken to exist and answer
    (as in tests/test_key_helper_live.py)."""
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)
    monkeypatch.setattr(claude_settings, "check_command", lambda command, **kw: None)
    pin_key_helper_platform(monkeypatch)


def _line(home: Path) -> str | None:
    path = _settings(home)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("apiKeyHelper")
    except ValueError:
        return None


def _notes(s) -> str:
    return " ".join(" ".join(s.key_helper_notes()).split())


def _key_record(s) -> dict:
    if not s.key_state_path.exists():
        return {}
    return json.loads(s.key_state_path.read_text(encoding="utf-8"))


def _write_line(home: Path, number: int | str, *, extra: str = "") -> None:
    _settings(home).write_text(
        json.dumps({"apiKeyHelper": f"{QSWAP} key-helper {number}{extra}"}),
        encoding="utf-8",
    )


# == SS3: a tombstone counts for 24 hours ===========================================


def _age_tombstone(s, number: str, at: object) -> None:
    record = _key_record(s)
    record["left"][number]["at"] = at
    s._write_key_state(record)


class TestATombstoneCountsForADay:
    def _left_by_the_engine(self, temp_home):
        s = _fleet()
        s.switch_to("5", json_output=True, entered_by="engine")
        s.switch_to("1", json_output=True)
        assert _key_record(s)["left"]["5"]["enteredBy"] == "engine"
        return s

    def test_within_a_day_the_line_came_back(self, temp_home):
        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", time.time() - 23 * 3600)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", True, "engine")
        assert s.key_entered_by() == "engine"

    @pytest.mark.parametrize("at", [
        "old", None, "yesterday", True, float("nan"),
    ])
    def test_a_tombstone_with_no_readable_time_is_old(self, temp_home, at):
        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", at)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", False, "user")
        assert s.key_entered_by() == "user"

    def test_after_a_day_a_line_for_that_number_is_the_users(self, temp_home):
        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", time.time() - 24 * 3600 - 60)
        _write_line(temp_home, 5)
        f = s.key_findings()
        assert (f.key, f.came_back, f.entered_by) == ("5", False, "user")
        assert s.key_entered_by() == "user"
        assert "came back" not in _notes(s)

    def test_doctor_does_not_say_an_old_one_came_back(self, temp_home):
        from ai_quota_swap import doctor

        s = self._left_by_the_engine(temp_home)
        _age_tombstone(s, "5", time.time() - 30 * 24 * 3600)
        _write_line(temp_home, 5)
        check = {c.id: c for c in doctor.run_checks()}["claude.key_helper"]
        assert "came back" not in check.summary
        assert check.status == "pass"

    def test_the_engine_leaves_a_hand_written_line_alone(self, temp_home):
        """The re-check's case: auto-switch entered #2 and left it; thirty days
        later the user writes #2's line by hand, and the next tick took it
        out again as auto-switch's own."""
        from ai_quota_swap.autoswitch import TickOutcome
        from tests.test_autoswitch import _usage
        from tests.test_key_helper_live import EXHAUSTED, _harness

        h = _harness(temp_home)
        h.tick_with_usage(EXHAUSTED)
        assert h.switcher.key_entered_by() == "engine"
        h.clock.now += 3600
        h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)})
        s = h.switcher
        assert s.helper_account() is None
        assert _key_record(s)["left"]["2"]["enteredBy"] == "engine"
        _age_tombstone(s, "2", time.time() - 30 * 24 * 3600)
        written = claude_settings.helper_command(2)
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": written}), encoding="utf-8"
        )
        assert s.key_entered_by() == "user"
        h.clock.now += 3600
        outcome = h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)})
        assert outcome is not TickOutcome.SWITCHED
        assert s.helper_account() == "2"
        assert json.loads(_settings(temp_home).read_text())["apiKeyHelper"] == written

    def test_the_rule_reads_the_age_from_at(self):
        now = 1_000_000.0
        record = {"left": {"5": {"at": now - 3600, "enteredBy": "engine"}}}
        assert key_activation.entered_by_of(record, "5", now=now) == ("engine", True)
        assert key_activation.entered_by_of(record, "5", now=now + 24 * 3600) == (
            "user", False,
        )


# == SS4: a line that names another data directory is another program's line ======


RULES = {"linux": "LINUX", "macos": "MACOS", "windows": "WINDOWS"}


def _on(monkeypatch, temp_home, rules: str, *, data_dir: str = "default"):
    """The fleet on ``rules``'s platform rules: ``data_dir`` ``default`` (the
    data directory a line without --data-dir names) or ``other`` (this qswap
    runs with another one: XDG_DATA_HOME on Linux; elsewhere a data directory
    put in place, as a future option would). Returns (switcher, its data
    directory, the default data directory)."""
    from ai_quota_swap import key_helper, paths
    from ai_quota_swap.models import Platform

    platform = getattr(Platform, RULES[rules])
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))
    monkeypatch.setattr(key_helper, "_platform", lambda: rules)
    default = paths.get_backup_root()
    if data_dir == "other":
        if rules == "linux":
            monkeypatch.setenv("XDG_DATA_HOME", str(temp_home / "xdg"))
        else:
            elsewhere = temp_home / "elsewhere" / "ai-quota-swap"
            monkeypatch.setattr("ai_quota_swap.switcher.get_backup_root", lambda: elsewhere)
            monkeypatch.setattr(paths, "get_backup_root", lambda: elsewhere)
    s = _fleet(platform)
    assert (Path(s.backup_dir) == default) is (data_dir == "default")
    return s, Path(s.backup_dir), default


def _other_line(number: int, other: Path | None) -> str:
    """Another qswap's line: naming ``other`` with --data-dir, or none."""
    tail = "" if other is None else f" --data-dir {claude_settings._quote(str(other))}"
    return f"{QSWAP} key-helper {number}{tail}"


def _put(home: Path, line: str) -> bytes:
    raw = json.dumps({"model": "opus", "apiKeyHelper": line}, indent=2).encode() + b"\n"
    _settings(home).parent.mkdir(parents=True, exist_ok=True)
    _settings(home).write_bytes(raw)
    return raw


@pytest.mark.parametrize("rules", list(RULES))
class TestAnotherDataDirectorysLine:
    def test_a_line_naming_this_data_directory_is_this_qswaps(
        self, temp_home, monkeypatch, rules
    ):
        s, mine, _default = _on(monkeypatch, temp_home, rules)
        _put(temp_home, _other_line(5, mine))
        assert s.helper_account() == "5"
        f = s.key_findings()
        assert f.key == "5" and f.other_dir is None

    def test_a_line_naming_another_data_directory_is_not(
        self, temp_home, monkeypatch, rules
    ):
        s, _mine, _default = _on(monkeypatch, temp_home, rules)
        other = temp_home / "other qswap" / "ai-quota-swap"
        _put(temp_home, _other_line(5, other))
        assert s.helper_account() is None
        assert s.current_account_number() == "1"
        f = s.key_findings()
        assert f.key is None and f.problem is None
        assert f.other_dir == str(other)
        assert not s.key_helper_available()

    def test_a_line_naming_none_belongs_to_the_default_data_directory(
        self, temp_home, monkeypatch, rules
    ):
        s, _mine, default = _on(monkeypatch, temp_home, rules)
        _put(temp_home, _other_line(5, None))
        assert s.helper_account() == "5"
        assert s.key_findings().other_dir is None

    def test_with_another_data_directory_a_line_naming_none_is_the_defaults(
        self, temp_home, monkeypatch, rules
    ):
        s, mine, default = _on(monkeypatch, temp_home, rules, data_dir="other")
        _put(temp_home, _other_line(5, None))
        assert s.helper_account() is None
        f = s.key_findings()
        assert f.key is None and f.other_dir == str(default)
        # And this qswap's own line names its directory, so it reads as its own.
        s.switch_to("1", json_output=True)
        raw = _put(temp_home, _other_line(5, None))
        _settings(temp_home).write_bytes(raw.replace(b'"apiKeyHelper"', b'"x"'))
        s.switch_to("6", json_output=True)
        assert s.helper_account() == "6"
        line = claude_settings.read_line(_line(temp_home))
        assert line is not None and Path(line.data_dir) == mine

    def test_a_line_for_a_number_this_qswap_does_not_have_is_not_removed(
        self, temp_home, monkeypatch, rules
    ):
        s, _mine, _default = _on(monkeypatch, temp_home, rules)
        raw = _put(temp_home, _other_line(9, temp_home / "other"))
        assert s.helper_problem() is None
        assert s.reconcile_key_helper() == []
        s.switch_to("7", json_output=True)
        assert _settings(temp_home).read_bytes() == raw


def _another_qswaps_line(temp_home, monkeypatch) -> tuple[object, bytes, Path]:
    s, _mine, _default = _on(monkeypatch, temp_home, "macos")
    other = temp_home / "other" / "ai-quota-swap"
    return s, _put(temp_home, _other_line(5, other)), other


class TestAnotherQswapsLineComesThroughEveryCommand:
    """Byte for byte: switch to a subscription, to a key (refused), add,
    remove, doctor, the engine's reconcile."""

    def test_a_switch_to_a_subscription_leaves_it_and_says_so(
        self, temp_home, monkeypatch, capsys
    ):
        s, raw, other = _another_qswaps_line(temp_home, monkeypatch)
        capsys.readouterr()
        s.switch_to("7")
        out = " ".join(capsys.readouterr().out.split())
        assert _settings(temp_home).read_bytes() == raw
        assert "another qswap" in out and claude_settings.shown_path(other) in out
        assert "until" in out and "removed" in out
        result = s.switch_to("1", json_output=True)
        assert result["switched"] is True
        assert any("another qswap" in line for line in result.get("followup", []))
        assert _settings(temp_home).read_bytes() == raw

    def test_a_rotation_leaves_it(self, temp_home, monkeypatch):
        s, raw, _other = _another_qswaps_line(temp_home, monkeypatch)
        from unittest.mock import patch

        with patch.object(s, "_usage_by_account", return_value={}):
            s.switch(json_output=True)
        assert _settings(temp_home).read_bytes() == raw

    @pytest.mark.parametrize("flag", [False, True])
    def test_a_switch_to_a_key_is_refused_as_for_another_helper(
        self, temp_home, monkeypatch, flag
    ):
        from ai_quota_swap.exceptions import KeyHelperUnavailableError

        s, raw, other = _another_qswaps_line(temp_home, monkeypatch)
        record = s.key_state_path.read_bytes() if s.key_state_path.exists() else None
        with pytest.raises(KeyHelperUnavailableError) as caught:
            s.switch_to("5", json_output=True, log_out_sessions=flag)
        text = " ".join(str(caught.value).split())
        assert "another apiKeyHelper" in text
        assert "another qswap" in text and claude_settings.shown_path(other) in text
        assert "Remove apiKeyHelper from" in text and "qswap switch 5" in text
        assert "--log-out-sessions" not in text.replace("with --log-out-sessions", "")
        assert caught.value.log_out_sessions is False
        assert _settings(temp_home).read_bytes() == raw
        assert (s.key_state_path.read_bytes() if s.key_state_path.exists() else None) == record

    def test_add_and_remove_leave_it(self, temp_home, monkeypatch, block_real_keychain):
        s, raw, _other = _another_qswaps_line(temp_home, monkeypatch)
        s.add_account_from_token("sk-ant-api03-" + "k8k8k8k8k8" * 4, slot=8)
        s.add_account(assume_yes=True)
        assert _settings(temp_home).read_bytes() == raw
        s.remove_account("5", assume_yes=True)
        s.remove_account("8", assume_yes=True)
        assert _settings(temp_home).read_bytes() == raw

    def test_doctor_names_the_other_data_directory(self, temp_home, monkeypatch):
        from ai_quota_swap import doctor

        s, raw, other = _another_qswaps_line(temp_home, monkeypatch)
        checks = doctor.run_checks()
        assert _settings(temp_home).read_bytes() == raw
        found = [c for c in checks if "another qswap" in c.summary]
        assert len(found) == 1 and found[0].status == "warn"
        assert found[0].path == doctor._abbr(other, temp_home)
        assert all(c.id != "claude.key_helper" or c.status != "pass" for c in checks)
        assert not any("active API key: #5" in c.summary for c in checks)

    def test_list_and_status_report_it_with_the_directory(self, temp_home, monkeypatch):
        s, raw, other = _another_qswaps_line(temp_home, monkeypatch)
        notes = _notes(s)
        assert "another qswap" in notes and claude_settings.shown_path(other) in notes
        assert "#5 ci" not in notes
        codes = [n["code"] for n in s.key_notes()]
        assert codes == ["other-data-dir"]
        assert s.active_view() == (None, None)

    def test_the_engines_reconcile_leaves_it(self, temp_home, monkeypatch):
        from ai_quota_swap.autoswitch import AutoSwitchEngine, AutoSwitchSettings

        s, raw, _other = _another_qswaps_line(temp_home, monkeypatch)
        engine = AutoSwitchEngine(s, AutoSwitchSettings(), lambda e: None)
        engine._reconcile_key_line()
        assert s.reconcile_key_helper() == []
        assert _settings(temp_home).read_bytes() == raw

    def test_the_engine_does_not_enter_a_key_over_it(self, temp_home, monkeypatch):
        """Linux rules, the engine's harness: the line names another data
        directory, the subscriptions are at their limit, and the engine
        neither enters #2 nor touches the line."""
        from tests.test_key_helper_live import EXHAUSTED, _harness

        h = _harness(temp_home)
        other = temp_home / "other" / "ai-quota-swap"
        raw = _put(temp_home, _other_line(2, other))
        h.tick_with_usage(EXHAUSTED)
        assert _settings(temp_home).read_bytes() == raw
        assert h.switcher.helper_account() is None


# == SS9 and DS16: a key that is active but outranked, or blocked, is said ========


def _with_env(home: Path, **env: str) -> bytes:
    """The settings with an env block added beside what is there."""
    path = _settings(home)
    obj = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    obj["env"] = dict(env)
    raw = (json.dumps(obj, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return raw


def _state_files(s, home: Path) -> tuple:
    def read(p: Path):
        return p.read_bytes() if p.exists() else None

    return (read(_settings(home)), read(s.key_state_path), read(s.sequence_file))


@pytest.fixture
def run(monkeypatch, capsys):
    """``qswap ARGS`` through cli.main on ``switcher``: (exit, out, err)."""
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


WHERE = str(Path("~") / ".claude" / "settings.json")


class TestTheActiveKeyOutranked:
    """SS9: qswap switch N to the key already active checks the env block in
    the words of the first-time refusal, and writes nothing when it refuses."""

    def test_a_variable_that_outranks_the_line_is_refused(self, temp_home, run):
        s = _fleet()
        s.switch_to("5", json_output=True)
        _with_env(temp_home, ANTHROPIC_API_KEY="secret-value")
        before = _state_files(s, temp_home)
        code, out, err = run(s, "switch", "5")
        assert code == 1
        text = " ".join((out + err).split())
        assert "Already on" not in text
        assert (
            f"{WHERE} sets ANTHROPIC_API_KEY in its env block, which Claude Code obeys "
            "before any key line, so sessions would not use #5 ci."
        ) in text
        assert (
            f"Remove ANTHROPIC_API_KEY from the env block of {WHERE}, then: qswap switch 5"
        ) in text
        assert "secret-value" not in out + err
        assert _state_files(s, temp_home) == before

    def test_it_writes_nothing_even_where_it_would_repair(self, temp_home, monkeypatch):
        """The key auto-switch entered (the user's switch would adopt it) and
        a line that does not answer (the switch would write it again)."""
        from ai_quota_swap.exceptions import KeyHelperUnavailableError

        s = _fleet()
        s.switch_to("5", json_output=True, entered_by="engine")
        _with_env(temp_home, CLAUDE_CODE_USE_BEDROCK="1")
        monkeypatch.setattr(claude_settings, "check_command", lambda c, **kw: "fails (exit 1)")
        before = _state_files(s, temp_home)
        with pytest.raises(KeyHelperUnavailableError) as caught:
            s.switch_to("5", json_output=True)
        assert caught.value.log_out_sessions is False
        assert "CLAUDE_CODE_USE_BEDROCK" in str(caught.value)
        assert _state_files(s, temp_home) == before
        assert s.key_entered_by() == "engine"

    def test_the_json_refusal_is_the_first_times(self, temp_home, run):
        s = _fleet()
        s.switch_to("5", json_output=True)
        _with_env(temp_home, ANTHROPIC_AUTH_TOKEN="t")
        code, out, _err = run(s, "switch", "5", "--json")
        assert code == 1
        error = json.loads(out)["error"]
        assert error["logOutSessions"] is False
        assert error["keyHelper"] == f"{WHERE} sets ANTHROPIC_AUTH_TOKEN in its env block"

    def test_another_host_goes_ahead_with_the_warning(self, temp_home, run):
        s = _fleet()
        s.switch_to("5", json_output=True)
        _with_env(temp_home, ANTHROPIC_BASE_URL="https://proxy.example.com/v1")
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        text = " ".join((out + err).split())
        assert "Already on" in text
        assert (
            f"{WHERE} sets ANTHROPIC_BASE_URL, so Claude Code sends the key of #5 ci "
            "to proxy.example.com."
        ) in text
        result = s.switch_to("5", json_output=True)
        assert result["reason"] == "already-active"
        assert any("proxy.example.com" in w for w in result["warnings"])


class TestWhatOutranksTheLineIsSaidEverywhere:
    """DS16: list, status, the dashboard and the menu bar carry a note for a
    variable in the env block that outranks the key line, and for another
    program's apiKeyHelper, in the refusal's words and with its fix."""

    def _surfaces(self, s) -> list[str]:
        """The note as list, status, the dashboard and the menu bar say it."""
        from ai_quota_swap import menubar_data

        class Source:
            def take(self, *, full, store_only):
                return s.accounts_snapshot(fetch=set())

        snap = s.accounts_snapshot(fetch=set())
        menu = menubar_data.claude_state(
            s, Source(), full=False, store_only=True, now=time.time(),
        )
        return [
            " ".join(text.split()) for text in (
                " ".join(s.key_helper_notes()),
                " ".join(s.status_lines()),
                " ".join(snap.key_notes),
                " ".join(menu.key_notes),
            )
        ]

    def test_a_variable_over_the_active_key(self, temp_home):
        s = _fleet()
        s.switch_to("5", json_output=True)
        _with_env(temp_home, ANTHROPIC_API_KEY="x", CLAUDE_CODE_USE_VERTEX="1")
        names = "ANTHROPIC_API_KEY and CLAUDE_CODE_USE_VERTEX"
        for text in self._surfaces(s):
            assert (
                f"{WHERE} sets {names} in its env block, which Claude Code obeys before "
                "any key line, so sessions do not use #5 ci."
            ) in text
            assert f"Remove {names} from the env block of {WHERE}, then: qswap switch 5" in text
        notes = [n for n in s.key_notes() if n["code"] == "env-outranks"]
        assert notes and notes[0]["account"] == 5
        assert notes[0]["fix"] == (
            f"Remove {names} from the env block of {WHERE}, then: qswap switch 5"
        )

    def test_a_variable_with_no_key_active_blocks_every_key(self, temp_home):
        s = _fleet()
        _with_env(temp_home, ANTHROPIC_API_KEY="x")
        for text in self._surfaces(s):
            assert (
                f"{WHERE} sets ANTHROPIC_API_KEY in its env block, which Claude Code "
                "obeys before any key line, so qswap does not switch to an API key."
            ) in text
            assert f"Remove ANTHROPIC_API_KEY from the env block of {WHERE}" in text
        notes = [n for n in s.key_notes() if n["code"] == "env-outranks"]
        assert notes and notes[0]["account"] is None
        assert notes[0]["fix"] == f"Remove ANTHROPIC_API_KEY from the env block of {WHERE}"

    def test_with_no_api_key_account_there_is_no_key_note(self, temp_home):
        s = _fleet()
        s.remove_account("5", assume_yes=True)
        s.remove_account("6", assume_yes=True)
        _with_env(temp_home, ANTHROPIC_API_KEY="x")
        assert "env-outranks" not in [n["code"] for n in s.key_notes()]

    def test_another_programs_helper(self, temp_home):
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": "/opt/vault/print-key"}), encoding="utf-8"
        )
        for text in self._surfaces(s):
            assert (
                f"{WHERE} already sets another apiKeyHelper, and every Claude Code "
                "session uses it now, before any login or saved key, so qswap cannot "
                "add a key line beside it."
            ) in text
            assert f"Remove apiKeyHelper from {WHERE}" in text
        notes = [n for n in s.key_notes() if n["code"] == "other-helper"]
        assert notes and notes[0]["fix"] == f"Remove apiKeyHelper from {WHERE}"
        assert "/opt/vault" not in _notes(s)

    def test_doctor_keeps_its_checks(self, temp_home):
        from ai_quota_swap import doctor

        s = _fleet()
        s.switch_to("5", json_output=True)
        _with_env(temp_home, ANTHROPIC_API_KEY="x")
        checks = {c.id: c for c in doctor.run_checks()}
        assert checks["claude.settings_env"].status == "warn"
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": "/opt/vault/print-key"}), encoding="utf-8"
        )
        checks = {c.id: c for c in doctor.run_checks()}
        assert checks["claude.api_key_helper"].status == "warn"
