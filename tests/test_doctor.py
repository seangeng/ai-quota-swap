"""``qswap doctor``: a read-only self-check (src/ai_quota_swap/doctor.py).

Every test runs on a fake machine rooted at ``tmp_path``: the PATH lookup,
``--version`` runs, platform, clock, ``launchctl``, Keychain, install
location and network are fakes, and the files doctor reads live under the
temporary home. Nothing here depends on the platform the suite runs on.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from ai_quota_swap import autoswitch, doctor, launch_agent, printer
from ai_quota_swap.models import Platform
from tests.codex_helpers import make_login

NOW = 1_800_000_000.0
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUNNING = "\tstate = running\n\tpid = 4242\n\tlast exit code = (never exited)\n"


class Refused(Exception):
    """Stands in for the demo harness's refusal: deliberately not an OSError."""


@pytest.fixture(autouse=True)
def _eighty_columns(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")


def tilde(*parts: str) -> str:
    """How doctor spells a path under the home directory on this platform."""
    return str(Path("~", *parts))


class Machine:
    """A fake machine: a home under ``tmp_path`` and fakes for every probe."""

    def __init__(self, tmp_path: Path, platform: Platform = Platform.LINUX):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.data = self.home / ".ai-quota-swap"
        self.codex_home = self.home / ".codex"
        self.platform = platform
        self.bins: dict[str, str] = {}
        self.versions: dict[str, str] = {}
        self.modes: dict[Path, int] = {}
        self.keychain_error: Exception | None = None
        self.keyring_login = False
        self.launchctl: tuple[int, str] | Exception = (113, "")
        self.launchctl_calls = 0
        self.reach_calls: list[str] = []
        self.reach_errors: dict[str, Exception] = {}
        self.install_paths = [str(self.home / ".local" / "share" / "uv" / "tools" / "aqs")]
        self.now = NOW
        self.environ: dict[str, str] = {}
        self.sessions: list = []

    # -- setup ------------------------------------------------------------------

    def install(self, *names: str) -> Machine:
        versions = {"claude": "2.1.4 (Claude Code)\n", "codex": "codex-cli 0.167.0\n"}
        for name in names:
            path = str(self.home / "bin" / name)
            self.bins[name] = path
            self.versions[path] = versions[name]
        return self

    def claude_login(self, email: str = "maya@example.com", org: str = "") -> Machine:
        account = {"emailAddress": email, "accountUuid": "uuid-maya"}
        if org:
            account["organizationUuid"] = org
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": account}))
        return self

    def claude_managed(self, email: str = "maya@example.com", org: str = "",
                       alias: str | None = "personal") -> Machine:
        record = {"email": email, "uuid": "uuid-maya", "organizationUuid": org}
        if alias:
            record["alias"] = alias
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "sequence.json").write_text(json.dumps(
            {"activeAccountNumber": 1, "sequence": [1], "accounts": {"1": record}}
        ))
        return self

    def codex_login(self, raw: bytes | None = None) -> Machine:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "auth.json").write_bytes(raw or make_login("maya@example.com"))
        return self

    def codex_managed(self, alias: str | None = "work") -> Machine:
        record = {"email": "maya@example.com", "uuid": "user-a", "organizationUuid": "acct-a"}
        if alias:
            record["alias"] = alias
        codex_dir = self.data / "codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "sequence.json").write_text(json.dumps(
            {"activeAccountNumber": 1, "sequence": [1], "accounts": {"1": record}}
        ))
        return self

    def codex_config(self, text: str) -> Machine:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(text)
        return self

    def menubar_plist(self, program: str | None = None, *, login: bool = True) -> Machine:
        path = (launch_agent.plist_path(home=self.home) if login
                else launch_agent.session_plist_path(backup_dir=self.data))
        path.parent.mkdir(parents=True, exist_ok=True)
        program = program or str(self.home / ".local" / "bin" / "qswap")
        path.write_bytes(plistlib.dumps({
            "Label": launch_agent.LABEL,
            "ProgramArguments": [program, "menubar", "--foreground"],
        }))
        return self

    def heartbeat(self, *, last_ago: float, next_in: float, codex: bool = False,
                  dry_run: bool = False) -> Machine:
        directory = self.data / "codex" if codex else self.data
        directory.mkdir(parents=True, exist_ok=True)
        autoswitch.write_heartbeat(
            directory, mode="loop", dry_run=dry_run, last_tick_at=self.now - last_ago,
            next_tick_at=self.now + next_in, started_at=self.now - 3600, pid=999,
        )
        return self

    def fleet(self) -> Machine:
        """Both providers installed, logged in and managed; macOS menu bar running."""
        self.platform = Platform.MACOS
        self.install("claude", "codex").claude_login().claude_managed()
        self.codex_login().codex_managed()
        self.menubar_plist()
        self.launchctl = (0, RUNNING)
        return self.heartbeat(last_ago=120, next_in=60)

    # -- probes -------------------------------------------------------------------

    def _version(self, path: str) -> str | None:
        return self.versions.get(path)

    def _keychain(self) -> None:
        if self.keychain_error is not None:
            raise self.keychain_error

    def _launchctl(self) -> tuple[int, str]:
        self.launchctl_calls += 1
        if isinstance(self.launchctl, Exception):
            raise self.launchctl
        return self.launchctl

    def _reach(self, url: str) -> str:
        self.reach_calls.append(url)
        if url in self.reach_errors:
            raise self.reach_errors[url]
        return "HTTP 404"

    def probes(self) -> doctor.Probes:
        return replace(
            doctor.default_probes(),
            platform=lambda: self.platform,
            now=lambda: self.now,
            home=lambda: self.home,
            claude_config_path=lambda: self.home / ".claude.json",
            codex_home=lambda: self.codex_home,
            data_dir=lambda: self.data,
            which=lambda name: self.bins.get(name),
            cli_version=self._version,
            dir_mode=lambda path: self.modes.get(Path(path), 0o700),
            access=lambda path, mode: True,
            keychain_answers=self._keychain,
            codex_keyring_login=lambda home: self.keyring_login,
            launchctl_print=self._launchctl,
            install_paths=lambda: list(self.install_paths),
            install_info=lambda: {
                "python": "3.12.4", "method": "uv",
                "identity": {"version": "1.2.0", "source": None}, "editable": False,
            },
            reach=self._reach,
            environ=lambda: dict(self.environ),
            claude_settings_path=lambda: self.home / ".claude" / "settings.json",
            claude_sessions=lambda: list(self.sessions),
        )

    # -- running ------------------------------------------------------------------

    def run(self, capsys, *argv: str) -> tuple[int, str]:
        try:
            doctor.doctor_command(list(argv), probes=self.probes())
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        out, _err = capsys.readouterr()
        return code, out

    def checks(self, *, online: bool = False) -> dict[str, doctor.Check]:
        return {c.id: c for c in doctor.run_checks(self.probes(), online=online)}


@pytest.fixture
def machine(tmp_path):
    return Machine(tmp_path)


def snapshot(root: Path) -> dict[str, tuple]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            out[path] = (stat.S_ISDIR(st.st_mode), st.st_size, st.st_mtime_ns)
    return out


# -- first run ----------------------------------------------------------------------


class TestNothingSetUp:
    def test_every_problem_has_a_next_step_and_it_exits_1(self, machine, capsys):
        code, out = machine.run(capsys)

        assert code == 1  # Claude Code missing, Codex missing too: a failure
        assert "  fail  Claude Code was not found (claude is not on PATH)" in out
        assert "        fix: curl -fsSL https://claude.ai/install.sh | bash" in out
        assert "  warn  Claude Code is not logged in" in out
        assert "        fix: claude, then /login, then qswap add" in out
        assert "  skip  Codex was not found; it is optional" in out
        assert f"{tilde('.ai-quota-swap')} (not created yet)" in out
        assert out.rstrip().endswith("1 warning, 1 failure")

        payload = json.loads(machine.run(capsys, "--json")[1])
        problems = [c for c in payload["checks"] if c["status"] in ("warn", "fail")]
        assert problems
        assert all(c["fix"] for c in problems), problems

    def test_it_writes_nothing(self, machine, capsys):
        before = snapshot(machine.root)
        machine.run(capsys)
        machine.run(capsys, "--json")
        machine.run(capsys, "--online")
        assert snapshot(machine.root) == before
        assert not machine.data.exists()

    def test_windows_gets_the_windows_installer(self, machine):
        machine.platform = Platform.WINDOWS
        assert machine.checks()["claude.cli"].fix == "irm https://claude.ai/install.ps1 | iex"

    def test_codex_alone_is_a_working_setup(self, machine):
        machine.install("codex")
        check = machine.checks()["claude.cli"]
        assert check.status == "warn"
        assert check.fix


# -- the happy path ---------------------------------------------------------------------


class TestFleet:
    def test_everything_passes(self, machine, capsys):
        machine.fleet()
        before = snapshot(machine.root)

        code, out = machine.run(capsys)

        assert code == 0, out
        for line in (
            "Claude Code\n  ok    Claude Code 2.1.4 on PATH\n"
            "  ok    logged in as maya@example.com\n"
            "  ok    managed by qswap as #1 personal\n",
            "  ok    Codex 0.167.0 on PATH\n",
            "  ok    managed by qswap as #1 work\n",
            "  ok    private to you (mode 700)\n",
            "  ok    saved logins are kept in the login Keychain, which is responding\n",
            "  ok    set up · starts at login\n  ok    running (pid 4242)\n",
            "  ok    Claude Code: running (pid 999) · last ticked 2m ago\n",
            "  ok    Python 3.12.4\n",
            "  ok    ai-quota-swap 1.2.0 · installed with uv tool\n",
        ):
            assert line in out, line
        assert "fix:" not in out
        assert out.rstrip().endswith("no warnings, no failures")
        assert snapshot(machine.root) == before

    def test_unmanaged_login_says_qswap_add(self, machine, capsys):
        machine.install("claude").claude_login()
        _code, out = machine.run(capsys)
        assert (
            "Claude Code\n"
            "  ok    Claude Code 2.1.4 on PATH\n"
            "  ok    logged in as maya@example.com\n"
            "  warn  that login is not managed by qswap\n"
            "        fix: qswap add\n"
        ) in out

    def test_same_email_in_another_org_is_not_managed(self, machine):
        machine.install("claude").claude_login(org="org-b").claude_managed(org="org-a")
        assert machine.checks()["claude.managed"].status == "warn"

    def test_unmanaged_codex_login_says_qswap_codex_add(self, machine):
        machine.install("claude", "codex").codex_login()
        check = machine.checks()["codex.managed"]
        assert (check.status, check.fix) == ("warn", "qswap codex add")

    def test_codex_not_logged_in(self, machine):
        machine.install("codex")
        check = machine.checks()["codex.login"]
        assert (check.status, check.fix) == ("warn", "codex login, then qswap codex add")

    def test_codex_api_key_login_is_named(self, machine):
        machine.install("codex").codex_login(make_login(auth_mode="apikey", api_key="sk-test"))
        check = machine.checks()["codex.login"]
        assert check.status == "warn"
        assert "API-key login" in check.summary


# -- Codex storage --------------------------------------------------------------------------


class TestCodexStorage:
    @pytest.mark.parametrize("mode", ["keyring", "auto", "ephemeral"])
    def test_a_login_outside_auth_json_fails_with_the_config_fix(self, machine, capsys, mode):
        machine.install("claude", "codex").codex_config(
            f'model = "gpt-5"\ncli_auth_credentials_store = "{mode}"\n'
        )
        check = machine.checks()["codex.storage"]
        assert check.status == "fail"
        assert f'cli_auth_credentials_store = "{mode}"' in check.summary
        assert check.fix == (
            f'set cli_auth_credentials_store = "file" in {tilde(".codex", "config.toml")}, '
            "then codex login"
        )
        assert "codex.login" not in machine.checks()
        assert machine.run(capsys)[0] == 1

    def test_file_mode_with_only_a_keyring_entry_fails(self, machine):
        machine.install("codex").codex_config('cli_auth_credentials_store = "file"\n')
        machine.keyring_login = True
        check = machine.checks()["codex.storage"]
        assert (check.status, check.fix) == ("fail", "codex login")

    def test_file_storage_passes(self, machine):
        machine.install("codex").codex_login()
        assert machine.checks()["codex.storage"].status == "pass"

    def test_unreadable_config_warns(self, machine):
        machine.install("codex").codex_config("this is = = not toml")
        machine.keyring_login = True  # one storage line even with a stray keyring entry
        checks = doctor.run_checks(machine.probes())
        storage = [c for c in checks if c.id == "codex.storage"]
        assert len(storage) == 1
        assert storage[0].status == "warn"
        assert "not valid TOML" in storage[0].summary


# -- menu bar --------------------------------------------------------------------------------


class TestMenuBarLocation:
    @pytest.mark.parametrize("folder", ["Documents", "Desktop", "Downloads"])
    def test_an_install_in_a_protected_folder_fails(self, machine, capsys, folder):
        machine.platform = Platform.MACOS
        machine.install_paths = [str(machine.home / folder / "aqs" / ".venv")]

        check = machine.checks()["menubar.location"]

        assert check.status == "fail"
        assert f"~/{folder}" in check.summary
        assert f"macOS keeps background services out of ~/{folder}" in check.summary
        assert check.fix == "uv tool install git+https://github.com/seangeng/ai-quota-swap"
        code, out = machine.run(capsys)
        assert code == 1
        assert "fix: uv tool install git+https://github.com/seangeng/ai-quota-swap" in out

    def test_with_the_menu_bar_set_up_the_fix_restarts_it(self, machine):
        machine.platform = Platform.MACOS
        machine.menubar_plist()
        machine.launchctl = (0, RUNNING)
        machine.install_paths = [str(machine.home / "Documents" / "aqs" / ".venv")]
        check = machine.checks()["menubar.location"]
        assert check.fix.endswith(", then qswap menubar")

    def test_a_plist_pointing_into_documents_fails(self, machine):
        machine.platform = Platform.MACOS
        machine.menubar_plist(str(machine.home / "Documents" / "aqs" / "bin" / "qswap"))
        machine.launchctl = (0, RUNNING)
        check = machine.checks()["menubar.location"]
        assert (check.status, check.fix) == ("fail", "qswap menubar")
        assert "menu bar service runs" in check.summary

    def test_case_does_not_hide_it(self, machine):
        machine.platform = Platform.MACOS
        machine.install_paths = [str(machine.home / "documents" / "aqs")]
        assert machine.checks()["menubar.location"].status == "fail"

    def test_an_install_elsewhere_passes(self, machine):
        machine.platform = Platform.MACOS
        check = machine.checks()["menubar.location"]
        assert check.status == "pass"
        assert check.summary == "installed outside ~/Documents, ~/Desktop and ~/Downloads"


class TestMenuBarService:
    def test_not_set_up_is_a_pass_with_a_hint(self, machine):
        machine.platform = Platform.MACOS
        check = machine.checks()["menubar.service"]
        assert (check.status, check.summary) == ("pass", "not set up")
        assert "qswap menubar" in check.hint
        assert machine.launchctl_calls == 0  # nothing to ask launchd about

    def test_start_at_login_off(self, machine):
        machine.platform = Platform.MACOS
        machine.menubar_plist(login=False)
        machine.launchctl = (0, RUNNING)
        assert "Start at login is off" in machine.checks()["menubar.service"].summary

    def test_a_crash_reports_the_exit_code_and_fails(self, machine, capsys):
        machine.platform = Platform.MACOS
        machine.menubar_plist()
        machine.launchctl = (0, "\tstate = not running\n\tlast exit code = 78: EX_CONFIG\n")
        check = machine.checks()["menubar.running"]
        assert check.status == "fail"
        assert "last exited with code 78" in check.summary
        assert check.fix == "qswap menubar"
        assert "com.aiquotaswap.menubar.err" in check.hint
        assert machine.run(capsys)[0] == 1

    def test_a_signal_fails(self, machine):
        machine.platform = Platform.MACOS
        machine.menubar_plist()
        machine.launchctl = (0, "\tstate = not running\n\tlast terminating signal = Killed: 9\n")
        assert machine.checks()["menubar.running"].status == "fail"

    def test_a_quit_menu_bar_passes(self, machine):
        machine.platform = Platform.MACOS
        machine.menubar_plist()
        machine.launchctl = (0, "\tstate = not running\n\tlast exit code = 0\n")
        check = machine.checks()["menubar.running"]
        assert check.status == "pass"
        assert "quit" in check.summary

    def test_not_loaded_warns(self, machine):
        machine.platform = Platform.MACOS
        machine.menubar_plist()
        machine.launchctl = (113, "")
        check = machine.checks()["menubar.running"]
        assert (check.status, check.fix) == ("warn", "qswap menubar")

    def test_a_refused_launchctl_reads_as_unknown(self, machine, capsys):
        machine.platform = Platform.MACOS
        machine.install("claude").menubar_plist()
        machine.launchctl = Refused("subprocess.Popen refused")
        check = machine.checks()["menubar.running"]
        assert check.status == "warn"
        assert "could not ask launchd" in check.summary
        assert machine.run(capsys)[0] == 0


class TestMenuBarOffMacOS:
    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.WINDOWS, Platform.WSL])
    def test_no_menu_bar_checks_at_all(self, machine, capsys, platform):
        machine.platform = platform
        machine.menubar_plist()  # a stray plist changes nothing off macOS
        machine.install_paths = [str(machine.home / "Documents" / "aqs")]
        machine.launchctl = AssertionError("launchctl must not run off macOS")

        checks = machine.checks()
        _code, out = machine.run(capsys)

        assert not [c for c in checks.values() if c.group == "menubar"]
        assert "Menu bar" not in out
        assert machine.launchctl_calls == 0

    def test_a_menu_bar_settings_file_gets_one_warning(self, machine):
        machine.data.mkdir()
        (machine.data / "menubar_settings.json").write_text("{}")
        menubar = [c for c in machine.checks().values() if c.group == "menubar"]
        assert [(c.id, c.status) for c in menubar] == [("menubar.platform", "warn")]
        assert "macOS-only" in menubar[0].summary
        assert menubar[0].fix


# -- auto-switch -------------------------------------------------------------------------------


class TestAutoSwitch:
    def test_a_running_engine(self, machine):
        machine.heartbeat(last_ago=120, next_in=60)
        check = machine.checks()["auto.claude"]
        assert check.status == "pass"
        assert check.summary == "Claude Code: running (pid 999) · last ticked 2m ago"
        assert check.hint is None

    def test_a_dry_run_engine_says_so(self, machine):
        machine.heartbeat(last_ago=30, next_in=30, dry_run=True)
        assert "dry run" in machine.checks()["auto.claude"].summary

    def test_a_stale_heartbeat_is_not_running_but_still_a_pass(self, machine):
        machine.heartbeat(last_ago=3720, next_in=-3600)
        check = machine.checks()["auto.claude"]
        assert check.status == "pass"
        assert check.summary == "Claude Code: not running · last ticked 1h 2m ago"
        assert check.hint == "start one with qswap auto"

    def test_no_heartbeat_at_all(self, machine):
        check = machine.checks()["auto.claude"]
        assert (check.status, check.summary) == ("pass", "Claude Code: not running")

    def test_on_macos_the_hint_names_the_menu_bar_too(self, machine):
        machine.platform = Platform.MACOS
        hint = machine.checks()["auto.claude"].hint
        assert hint == "start one with qswap auto or qswap menubar --auto-switch"

    def test_codex_engine_has_its_own_line(self, machine):
        machine.install("codex").heartbeat(last_ago=60, next_in=60, codex=True)
        checks = machine.checks()
        assert checks["auto.codex"].summary.startswith("Codex: running")
        assert checks["auto.claude"].summary == "Claude Code: not running"

    def test_no_codex_line_without_codex(self, machine):
        assert "auto.codex" not in machine.checks()


# -- data directory --------------------------------------------------------------------------


class TestDataDirectory:
    def test_open_permissions_warn_with_chmod(self, machine):
        machine.data.mkdir()
        machine.modes[machine.data] = 0o755
        check = machine.checks()["data.permissions"]
        assert check.status == "warn"
        assert check.fix == f"chmod 700 {tilde('.ai-quota-swap')}"

    def test_no_permission_check_on_windows(self, machine):
        machine.platform = Platform.WINDOWS
        machine.data.mkdir()
        machine.modes[machine.data] = 0o777
        assert "data.permissions" not in machine.checks()

    def test_a_file_where_the_directory_should_be_fails(self, machine):
        machine.data.write_text("oops")
        check = machine.checks()["data.dir"]
        assert check.status == "fail"
        assert check.fix

    def test_files_off_macos(self, machine):
        summary = machine.checks()["data.store"].summary
        creds = tilde(".ai-quota-swap", "credentials")
        assert summary == f"saved logins are kept in files in {creds}"

    def test_a_keychain_that_does_not_answer_warns(self, machine):
        machine.platform = Platform.MACOS
        machine.keychain_error = Refused("security find-generic-password failed (rc=51)")
        check = machine.checks()["data.store"]
        assert check.status == "warn"
        assert "(security exited 51)" in check.summary
        assert check.fix == "security unlock-keychain"
        assert "fall back to files" in check.summary

    def test_an_unreadable_account_list_warns(self, machine):
        machine.install("claude").claude_login()
        machine.data.mkdir()
        (machine.data / "sequence.json").write_text("{not json")
        check = machine.checks()["claude.managed"]
        assert check.status == "warn"
        assert "sequence.json" in check.fix


# -- network -----------------------------------------------------------------------------------


class TestNetwork:
    def test_offline_by_default(self, machine, capsys):
        _code, out = machine.run(capsys)
        assert machine.reach_calls == []
        assert "Network checks skipped; run qswap doctor --online to include them." in out
        assert not [c for c in machine.checks().values() if c.group == "network"]

    def test_online_reaches_both_hosts(self, machine, capsys):
        machine.install("claude").claude_login().claude_managed()
        code, out = machine.run(capsys, "--online")
        assert machine.reach_calls == [doctor.ANTHROPIC_URL, doctor.CHATGPT_URL]
        assert "api.anthropic.com answers (HTTP 404)" in out
        assert "Network checks skipped" not in out
        assert code == 0
        payload = json.loads(machine.run(capsys, "--json", "--online")[1])
        assert payload["online"] is True

    def test_an_unreachable_api_fails(self, machine):
        machine.install("claude")
        machine.reach_errors[doctor.ANTHROPIC_URL] = TimeoutError("timed out")
        check = machine.checks(online=True)["network.anthropic"]
        assert check.status == "fail"
        assert check.summary == "cannot reach api.anthropic.com (timed out)"
        assert check.fix

    def test_chatgpt_only_warns_without_codex(self, machine):
        machine.reach_errors[doctor.CHATGPT_URL] = OSError("no route")
        assert machine.checks(online=True)["network.chatgpt"].status == "warn"
        machine.install("codex")
        assert machine.checks(online=True)["network.chatgpt"].status == "fail"


# -- JSON, exit codes, layout ------------------------------------------------------------------


class TestJson:
    def test_shape(self, machine, capsys):
        machine.fleet()
        code, out = machine.run(capsys, "--json")
        payload = json.loads(out)

        assert code == 0
        assert set(payload) == {"schemaVersion", "checks", "counts", "online"}
        assert payload["schemaVersion"] == 1
        assert payload["online"] is False
        ids = [c["id"] for c in payload["checks"]]
        assert len(ids) == len(set(ids))
        groups = {g for g, _ in doctor.GROUPS}
        for check in payload["checks"]:
            assert set(check) == {"id", "group", "status", "summary", "fix"}
            assert check["status"] in ("pass", "warn", "fail", "skip")
            assert check["group"] in groups
            assert check["id"].startswith(check["group"] + ".")
        assert set(payload["counts"]) == {"pass", "warn", "fail", "skip"}
        assert sum(payload["counts"].values()) == len(ids)
        assert {"claude.cli", "claude.login", "claude.managed", "codex.storage",
                "data.dir", "data.store", "menubar.location", "auto.claude",
                "install.python", "install.method"} <= set(ids)

    def test_failures_exit_1_under_json_too(self, machine, capsys):
        code, out = machine.run(capsys, "--json")
        assert code == 1
        assert json.loads(out)["counts"]["fail"] == 1


class TestLayout:
    def _lines(self, machine, capsys, *argv) -> list[str]:
        with printer.force_color():
            _code, out = machine.run(capsys, *argv)
        return [ANSI.sub("", line) for line in out.splitlines()]

    @pytest.mark.parametrize("setup", ["nothing", "fleet", "trouble", "long-paths"])
    def test_every_line_fits_80_columns(self, machine, capsys, setup):
        if setup == "fleet":
            machine.fleet()
        elif setup == "trouble":
            machine.platform = Platform.MACOS
            machine.install("codex").codex_config('cli_auth_credentials_store = "keyring"\n')
            machine.install_paths = [str(machine.home / "Documents" / "a" / "b" / ".venv")]
            machine.menubar_plist()
            machine.launchctl = (0, "\tlast exit code = 78: EX_CONFIG\n")
            machine.keychain_error = Refused("x" * 200)
            machine.data.mkdir()
            machine.modes[machine.data] = 0o755
        elif setup == "long-paths":
            machine.data = machine.root / ("deep-" * 30) / "ai-quota-swap"
            machine.codex_home = machine.root / ("codex-" * 25)
            machine.install("claude", "codex").claude_login("a" * 70 + "@example.com")
            machine.reach_errors[doctor.ANTHROPIC_URL] = OSError("y" * 150)
        lines = self._lines(machine, capsys, "--online")
        lines += self._lines(machine, capsys)
        too_long = [line for line in lines if len(line) > 80]
        assert not too_long, too_long

    def test_marks_are_words_and_colour_never_stands_alone(self, machine, capsys):
        machine.install("claude").claude_login()
        lines = self._lines(machine, capsys)
        marked = [line for line in lines if line.startswith("  ") and not line.startswith("   ")]
        assert marked
        assert {line[2:6].strip() for line in marked} <= {"ok", "warn", "fail", "skip"}

    def test_no_exclamation_marks(self, machine, capsys):
        machine.fleet()
        out = "\n".join(self._lines(machine, capsys))
        machine.platform = Platform.LINUX
        out += "\n".join(self._lines(machine, capsys, "--online"))
        assert "!" not in out


class TestUsage:
    def test_a_bad_flag_exits_64(self, machine, capsys):
        code, _out = machine.run(capsys, "--bogus")
        assert code == 64

    def test_a_bad_flag_under_json_prints_the_envelope(self, machine, capsys):
        code, out = machine.run(capsys, "--json", "--bogus")
        assert code == 64
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["error"]["type"] == "UsageError"

    def test_help_is_short(self, machine, capsys):
        code, out = machine.run(capsys, "--help")
        lines = out.splitlines()
        assert code == 0
        assert lines[0].startswith("usage: qswap doctor")
        assert "--json" in out and "--online" in out
        assert "examples:" in out
        assert len(lines) <= 20
        assert all(len(line) <= 80 for line in lines)


# -- probes that fail ---------------------------------------------------------------------------


class TestProbesThatFail:
    def test_every_probe_refused_still_gives_a_report(self, machine, capsys):
        """The demo harness refuses spawns with a non-OSError; nothing may traceback."""
        machine.platform = Platform.MACOS
        machine.install("claude", "codex").claude_login().claude_managed()
        machine.menubar_plist()

        def refuse(*_args):
            raise Refused("refused")

        probes = replace(
            machine.probes(),
            cli_version=refuse, keychain_answers=refuse, launchctl_print=refuse,
            heartbeat=refuse, install_info=refuse, install_paths=refuse, dir_mode=refuse,
            codex_keyring_login=refuse, reach=refuse,
        )
        checks = {c.id: c for c in doctor.run_checks(probes, online=True)}

        assert checks["claude.cli"].summary == "Claude Code on PATH, version unknown"
        assert checks["claude.cli"].status == "pass"
        assert checks["codex.cli"].summary == "Codex on PATH, version unknown"
        assert checks["data.store"].status == "warn"
        assert checks["menubar.running"].status == "warn"
        assert checks["auto.claude"].summary == "Claude Code: not running"
        assert "install method unknown" in checks["install.method"].summary
        assert not [c for c in checks.values() if c.id.endswith(".error")]

    def test_paths_that_cannot_be_resolved(self, machine):
        def refuse():
            raise Refused("no home")

        probes = replace(machine.probes(), home=refuse, data_dir=refuse,
                         codex_home=refuse, claude_config_path=refuse)
        checks = {c.id: c for c in doctor.run_checks(probes)}
        assert checks["claude.login"].status == "warn"
        assert checks["data.dir"].status == "warn"

    def test_a_section_that_crashes_becomes_one_warning(self, machine, capsys, monkeypatch):
        def broken(probes, facts):
            raise KeyError("boom")

        sections = tuple(
            (group, broken if group == "data" else fn) for group, fn in doctor._SECTIONS
        )
        monkeypatch.setattr(doctor, "_SECTIONS", sections)
        checks = machine.checks()
        assert checks["data.error"].status == "warn"
        assert "claude.cli" in checks and "install.python" in checks


# -- pieces --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("output", "version"), [
    ("2.1.4 (Claude Code)\n", "2.1.4"),
    ("codex-cli 0.167.0\n", "0.167.0"),
    ("codex-cli 0.170.0-alpha.2", "0.170.0-alpha.2"),
    ("", None),
    (None, None),
    ("no digits here", None),
])
def test_parse_version(output, version):
    assert doctor.parse_version(output) == version


def test_parse_launchctl_print_reads_only_the_jobs_own_fields():
    text = (
        "gui/501/com.aiquotaswap.menubar = {\n"
        "\tactive count = 0\n"
        "\tstate = not running\n"
        "\tendpoints = {\n"
        "\t\tstate = active\n"
        "\t\tpid = 77\n"
        "\t}\n"
        "\tlast exit code = 78: EX_CONFIG\n"
        "}\n"
    )
    job = doctor.parse_launchctl_print(text)
    assert job == {"state": "not running", "pid": None, "last_exit": 78, "last_signal": None}
    assert doctor.parse_launchctl_print(RUNNING)["pid"] == 4242
    assert doctor.parse_launchctl_print(RUNNING)["last_exit"] is None


def test_summary_line():
    assert doctor.summary_line({"warn": 2, "fail": 0}) == "2 warnings, no failures"
    assert doctor.summary_line({"warn": 1, "fail": 1}) == "1 warning, 1 failure"
    assert doctor.summary_line({}) == "no warnings, no failures"


def test_matchers_agree_with_the_switchers():
    """doctor keeps copies of the switchers' identity rules so it need not
    build a switcher; this holds the copies to the originals."""
    from ai_quota_swap.codex import auth as codex_auth
    from ai_quota_swap.codex.switcher import CodexSwitcher
    from ai_quota_swap.switcher import ClaudeAccountSwitcher

    registry = {"accounts": {
        "1": {"email": "a@example.com", "organizationUuid": ""},
        "2": {"email": "a@example.com", "organizationUuid": "org-1"},
        "3": {"email": "b@example.com"},
        "4": {"email": "c@example.com", "organizationUuid": None},
    }}
    for email in ("a@example.com", "b@example.com", "c@example.com", "z@example.com"):
        for org in ("", "org-1", "org-2"):
            assert doctor.claude_slot(registry, email, org) == \
                ClaudeAccountSwitcher._find_account_slot(registry, email, org), (email, org)

    codex_registry = {"accounts": {
        "1": {"email": "a@example.com", "uuid": "user-a", "organizationUuid": "acct-a"},
        "2": {"email": "b@example.com", "uuid": "", "organizationUuid": "acct-b"},
        "3": {"email": "a@example.com", "uuid": "user-a", "organizationUuid": "acct-c"},
    }}
    for email, user, acct in (
        ("a@example.com", "user-a", "acct-a"), ("b@example.com", "user-b", "acct-b"),
        ("a@example.com", "user-a", "acct-c"), ("x@example.com", "user-x", "acct-a"),
        ("a@example.com", "user-z", "acct-a"),
    ):
        login = codex_auth.parse_login(make_login(email, user, acct))
        assert doctor.codex_slot(codex_registry, user, acct, email) == \
            CodexSwitcher._slot_for_identity(codex_registry, login), (email, user, acct)


def test_under_protected_folder(tmp_path):
    home = tmp_path / "home"
    assert doctor.under_protected_folder(str(home / "Documents" / "x"), home) == "Documents"
    assert doctor.under_protected_folder(str(home / "Downloads"), home) == "Downloads"
    assert doctor.under_protected_folder(str(home / "DocumentsOld" / "x"), home) is None
    assert doctor.under_protected_folder(str(home / ".local" / "bin" / "qswap"), home) is None


def test_the_cli_dispatches_to_doctor(monkeypatch):
    seen = []
    monkeypatch.setattr(doctor, "doctor_command", lambda argv: seen.append(argv))
    monkeypatch.setattr("sys.argv", ["qswap", "doctor", "--json"])
    from ai_quota_swap import cli

    cli.main()
    assert seen == [["--json"]]


# -- API-key accounts and what overrides the active account -------------------------


class TestOverrides:
    """docs/design/api-key-sessions.md: three read-only checks, each with a fix."""

    def test_none_of_them_appear_on_a_plain_machine(self, machine):
        machine.install("claude").claude_login().claude_managed()
        ids = set(machine.checks())
        assert not ids & {"claude.api_key_helper", "claude.env", "claude.api_key_sessions"}

    def test_api_key_helper_in_user_settings(self, machine, capsys):
        machine.install("claude").claude_login().claude_managed()
        settings = machine.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"apiKeyHelper": "/usr/local/bin/key"}))
        check = machine.checks()["claude.api_key_helper"]
        assert check.status == "warn"
        assert "Claude Code uses it instead of the active account" in check.summary
        assert check.fix == f"remove apiKeyHelper from {tilde('.claude', 'settings.json')}"
        _code, out = machine.run(capsys)
        assert all(len(line) <= 80 for line in out.splitlines())

    def test_exported_variables_by_name_only(self, machine, capsys):
        machine.install("claude").claude_login().claude_managed()
        secret = "sk-ant-api03-" + "q" * 40
        machine.environ = {"ANTHROPIC_API_KEY": secret, "CLAUDE_CODE_OAUTH_TOKEN": "x" * 30,
                           "ANTHROPIC_AUTH_TOKEN": ""}
        check = machine.checks()["claude.env"]
        assert check.status == "warn"
        assert check.summary.startswith(
            "ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are set in this shell"
        )
        assert check.fix == "unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN"
        code, out = machine.run(capsys)
        assert secret not in out and "x" * 30 not in out
        payload = json.loads(machine.run(capsys, "--json")[1])
        assert secret not in json.dumps(payload)
        assert code == 0  # a warning, not a failure

    def _key_active(self, machine, *, activated_at: float):
        from types import SimpleNamespace

        machine.install("claude")
        (machine.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": "api-key-5@token.local", "accountUuid": "",
            "organizationUuid": None,
        }}))
        machine.data.mkdir(parents=True, exist_ok=True)
        (machine.data / "sequence.json").write_text(json.dumps({
            "activeAccountNumber": 5, "sequence": [1, 5], "accounts": {
                "1": {"email": "maya@example.com", "uuid": "u1", "organizationUuid": "",
                      "alias": "personal"},
                "5": {"email": "api-key-5@token.local", "uuid": "", "organizationUuid": "",
                      "alias": "ci", "kind": "api_key"},
            },
        }))
        from datetime import datetime, timezone

        def iso(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        (machine.data / "activations.jsonl").write_text(
            json.dumps({"ts": iso(activated_at - 3600), "number": 1,
                        "email": "maya@example.com", "org": "", "source": "switch"}) + "\n"
            + json.dumps({"ts": iso(activated_at), "number": 5,
                          "email": "api-key-5@token.local", "org": "", "source": "switch"}) + "\n"
        )
        machine.sessions = [
            SimpleNamespace(pid=11, cwd="/a", started_at=int((activated_at - 600) * 1000)),
            SimpleNamespace(pid=12, cwd="/b", started_at=int((activated_at - 60) * 1000)),
            SimpleNamespace(pid=13, cwd="/c", started_at=int((activated_at + 60) * 1000)),
        ]

    def test_api_key_active_with_older_sessions(self, machine, capsys):
        self._key_active(machine, activated_at=NOW - 300)
        check = machine.checks()["claude.api_key_sessions"]
        assert check.status == "warn"
        assert check.summary.startswith(
            "#5 ci (API key) is active and 2 Claude Code sessions that started on a "
            "subscription"
        )
        assert check.fix == "restart them to use the key, or switch back: qswap switch 1"
        _code, out = machine.run(capsys)
        assert all(len(line) <= 80 for line in out.splitlines())

    def test_sessions_started_on_an_earlier_key_are_not_logged_out(self, machine):
        """Key to key: #5 ci, then #6 ops. Sessions started while #5 was
        active keep using its key; only the one started on #1 personal is
        logged out, and the way back is #1, not the earlier key (which the
        guard would refuse while sessions run)."""
        from datetime import datetime, timezone

        def iso(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        self._key_active(machine, activated_at=NOW - 300)
        (machine.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": "api-key-6@token.local", "accountUuid": "",
            "organizationUuid": None,
        }}))
        registry = json.loads((machine.data / "sequence.json").read_text())
        registry["activeAccountNumber"] = 6
        registry["sequence"] = [1, 5, 6]
        registry["accounts"]["6"] = {
            "email": "api-key-6@token.local", "uuid": "", "organizationUuid": "",
            "alias": "ops", "kind": "api_key",
        }
        (machine.data / "sequence.json").write_text(json.dumps(registry))
        with (machine.data / "activations.jsonl").open("a") as log:
            log.write(json.dumps({"ts": iso(NOW - 100), "number": 6,
                                  "email": "api-key-6@token.local", "org": "",
                                  "source": "switch"}) + "\n")
        # 11 and 12 started on #1, 13 on #5; add one more on #5.
        from types import SimpleNamespace

        machine.sessions.append(
            SimpleNamespace(pid=14, cwd="/d", started_at=int((NOW - 200) * 1000))
        )
        check = machine.checks()["claude.api_key_sessions"]
        assert check.summary.startswith(
            "#6 ops (API key) is active and 2 Claude Code sessions that started on a "
            "subscription"
        )
        assert check.fix == "restart them to use the key, or switch back: qswap switch 1"

    def test_only_sessions_started_on_an_earlier_key_is_fine(self, machine):
        from datetime import datetime, timezone

        def iso(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        self._key_active(machine, activated_at=NOW - 300)
        (machine.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": "api-key-6@token.local", "accountUuid": "",
            "organizationUuid": None,
        }}))
        registry = json.loads((machine.data / "sequence.json").read_text())
        registry["accounts"]["6"] = {
            "email": "api-key-6@token.local", "uuid": "", "organizationUuid": "",
            "alias": "ops", "kind": "api_key",
        }
        (machine.data / "sequence.json").write_text(json.dumps(registry))
        with (machine.data / "activations.jsonl").open("a") as log:
            log.write(json.dumps({"ts": iso(NOW - 100), "number": 6,
                                  "email": "api-key-6@token.local", "org": "",
                                  "source": "switch"}) + "\n")
        machine.sessions = machine.sessions[2:]  # pid 13: started while #5 was active
        assert "claude.api_key_sessions" not in machine.checks()

    def test_api_key_active_with_only_newer_sessions_is_fine(self, machine):
        self._key_active(machine, activated_at=NOW - 300)
        machine.sessions = machine.sessions[2:]
        assert "claude.api_key_sessions" not in machine.checks()

    def test_it_writes_nothing(self, machine, capsys):
        self._key_active(machine, activated_at=NOW - 300)
        before = snapshot(machine.root)
        machine.run(capsys)
        assert snapshot(machine.root) == before
