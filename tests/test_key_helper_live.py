"""Part 2 of docs/design/api-key-sessions.md: an API-key account made active
through qswap's ``apiKeyHelper`` line in Claude Code's user settings.

Everything runs on the temporary home, the in-memory Keychain fake and
faked network; the macOS paths run on every platform by setting the
switcher's platform. Sections follow the design: the key helper command (A),
the settings editor (B), who is active (C), switching (D), run profiles (E),
``qswap run`` on a key (F), the engine (G), credit (H), the ledger (I), the
surfaces and doctor (J).
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import (
    api_spend,
    claude_settings,
    cli,
    display,
    doctor,
    key_helper,
    macos_keychain,
    paths,
)
from ai_quota_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
)
from ai_quota_swap.exceptions import (
    KeyHelperUnavailableError,
    SessionError,
    SessionsRunningError,
    SwitchError,
    ValidationError,
)
from ai_quota_swap.models import Platform
from ai_quota_swap.paths import get_global_config_path
from ai_quota_swap.switcher import ClaudeAccountSwitcher

KEY_5 = "sk-ant-api03-" + "k5k5k5k5k5" * 4
KEY_6 = "sk-ant-api03-" + "k6k6k6k6k6" * 4
SETUP_TOKEN = "sk-ant-oat01-" + "p1p1p1p1p1" * 4
MCP = {"plugin:slack:slack|abc": {"serverName": "slack", "accessToken": "mcp-1"}}
MAYA = "maya@example.com"
SAM = "sam@example.com"
EMAIL_5 = "api-key-5@token.local"
EMAIL_6 = "api-key-6@token.local"
QSWAP = "/opt/qswap/bin/qswap"

#: The real functions, before the autouse fixtures below (and conftest's
#: _no_key_line_runs) replace them.
_REAL_COMMAND_MISSING = claude_settings.command_missing
_REAL_EXECUTABLE = claude_settings.qswap_executable
_REAL_CHECK_COMMAND = claude_settings.check_command
_REAL_RUN_CHECK = claude_settings._run_check

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


# -- the fleet ---------------------------------------------------------------------


def _switcher(platform: Platform) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = platform
    s._setup_directories()
    s._init_sequence_file()
    return s


def _fleet(platform: Platform = Platform.MACOS) -> ClaudeAccountSwitcher:
    """#1 personal (a subscription login, with MCP sign-ins) is the live login;
    #5 ci and #6 ops are API-key accounts; #7 is a second subscription."""
    s = _switcher(platform)
    s.add_account_from_token(SETUP_TOKEN, email=MAYA, slot=1)
    s.add_account_from_token(KEY_5, slot=5)
    s.add_account_from_token(KEY_6, slot=6)
    s.add_account_from_token(SETUP_TOKEN.replace("p1", "q2"), email=SAM, slot=7)
    s.set_alias("1", "personal")
    s.set_alias("5", "ci")
    s.set_alias("6", "ops")
    s.set_alias("7", "work")
    login = json.loads(s._read_account_credentials("1", MAYA))
    s._write_credentials(json.dumps({**login, "mcpOAuth": MCP}))
    get_global_config_path().write_text(json.dumps({
        "oauthAccount": {"emailAddress": MAYA, "accountUuid": "", "organizationUuid": None},
        "projects": {"/work/app": {"allowedTools": []}},
    }), encoding="utf-8")
    data = s._get_sequence_data()
    data["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, data)
    return s


def pin_key_helper_platform(monkeypatch) -> None:
    """The key helper's own platform, macOS as the fleet's is: on Linux the
    line also names the data directory (``--data-dir``, decision B3), which
    the lines these tests pin leave out. The tests of that rule set
    ``key_helper._platform`` themselves, so every runner checks both forms."""
    monkeypatch.setattr(key_helper, "_platform", lambda: "macos")


@pytest.fixture(autouse=True)
def _fixed_command(monkeypatch):
    """The installed command every helper line names, fixed for the tests
    (and taken to exist and to answer: TestMovedQswap is about one that does
    not exist, TestTheLineAnswers about one that does not answer), on the
    key helper's macOS (:func:`pin_key_helper_platform`)."""
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [QSWAP])
    monkeypatch.setattr(claude_settings, "command_missing", lambda command: False)
    monkeypatch.setattr(claude_settings, "check_command", lambda command, **kw: None)
    pin_key_helper_platform(monkeypatch)


@pytest.fixture
def alive(monkeypatch):
    pids: set[int] = set()
    monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive", lambda pid: pid in pids)
    return pids


def _write_sessions(home: Path, pids, *, started_at: float | None = None):
    directory = home / ".claude" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = int((started_at if started_at is not None else time.time()) * 1000)
    for pid in pids:
        (directory / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": f"s-{pid}", "cwd": f"/work/app{pid}",
            "startedAt": stamp, "kind": "interactive", "entrypoint": "cli",
        }), encoding="utf-8")


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _settings_obj(home: Path) -> dict:
    return json.loads(_settings(home).read_text(encoding="utf-8"))


def _settings_obj_any(home: Path) -> dict:
    """The settings object, read past a byte order mark."""
    return json.loads(_settings(home).read_bytes().decode("utf-8-sig"))


def _acct() -> str:
    return macos_keychain.keychain_account_name()


def _claude_state(store) -> tuple:
    """What a helper switch must never write: the login item, the saved-key
    item and ``~/.claude.json``."""
    return (
        store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct()),
        store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()),
        get_global_config_path().read_bytes(),
    )


def _main(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["qswap", *argv]):
        try:
            cli.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


@pytest.fixture
def run(monkeypatch, capsys):
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


def _fits(text: str) -> None:
    for line in text.splitlines():
        if line.strip().startswith("qswap "):
            continue  # a command to copy stays whole
        assert len(line) <= display.WIDTH, line


# == A. qswap key-helper N ================================================================


def _seed_data(root: Path, accounts: dict, creds: dict[str, str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": None, "sequence": [int(n) for n in accounts],
        "accounts": accounts,
    }), encoding="utf-8")
    (root / "credentials").mkdir(exist_ok=True)
    for name, value in (creds or {}).items():
        (root / "credentials" / name).write_text(
            base64.b64encode(value.encode()).decode(), encoding="utf-8"
        )


def _helper(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = key_helper.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestKeyHelper:
    def test_prints_the_key_and_one_newline_from_the_file(self, temp_home, monkeypatch):
        monkeypatch.setattr(key_helper, "_platform", lambda: "windows")
        root = key_helper.data_dir()
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})
        assert _helper(["5"]) == (0, KEY_5 + "\n", "")

    def test_prints_the_key_from_the_keychain(self, temp_home, monkeypatch, block_real_keychain):
        monkeypatch.setattr(key_helper, "_platform", lambda: "macos")
        root = key_helper.data_dir()
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}})
        block_real_keychain.set_password(SECURITY_SERVICE, f"account-5-{EMAIL_5}", KEY_5)
        assert _helper(["5"]) == (0, KEY_5 + "\n", "")

    @pytest.mark.parametrize("platform, name", [
        (Platform.MACOS, "macos"), (Platform.LINUX, "linux"),
    ])
    def test_reads_what_the_switcher_saved(
        self, temp_home, monkeypatch, block_real_keychain, platform, name
    ):
        """One platform per home: a second platform on the same home would
        migrate the first one's data directory (on Windows, with its log
        file still open)."""
        monkeypatch.setattr(key_helper, "_platform", lambda: name)
        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))
        s = _fleet(platform)
        assert key_helper.data_dir() == s.backup_dir
        assert _helper(["5"]) == (0, KEY_5 + "\n", "")

    def test_a_data_directory_from_xdg_is_named_in_the_line(
        self, temp_home, monkeypatch, tmp_path
    ):
        """Claude Code started from a desktop launcher may not have the
        XDG_DATA_HOME the terminal had: the line names the directory."""
        monkeypatch.setattr(key_helper, "_platform", lambda: "linux")
        xdg = tmp_path / "my data"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        root = key_helper.data_dir()
        assert root == xdg / "ai-quota-swap"
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})
        line = claude_settings.helper_command(5)
        quote = '"' if sys.platform == "win32" else "'"
        assert line == f"{QSWAP} key-helper 5 --data-dir {quote}{root}{quote}"
        assert claude_settings.parse_line(line) == "5"
        monkeypatch.delenv("XDG_DATA_HOME")
        assert _helper(["5"])[0] == 1  # the default directory has no accounts
        assert _helper(["5", "--data-dir", str(root)]) == (0, KEY_5 + "\n", "")

    def test_on_linux_the_line_always_names_the_data_directory(
        self, temp_home, monkeypatch, tmp_path
    ):
        """The other direction of the XDG case (F4): qswap ran without
        XDG_DATA_HOME, the session that runs the helper has one. The line
        names the directory in use, so the helper still finds the key."""
        monkeypatch.setattr(key_helper, "_platform", lambda: "linux")
        root = key_helper.data_dir()
        assert root == temp_home / ".local" / "share" / "ai-quota-swap"
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})
        line = claude_settings.helper_command(5)
        assert line == f"{QSWAP} key-helper 5 --data-dir {claude_settings._quote(str(root))}"
        args = line.split(" key-helper ", 1)[1]
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
        parts = claude_settings.read_line(line)
        assert parts is not None and parts.data_dir == str(root)
        assert _helper([args.split()[0], "--data-dir", parts.data_dir]) == (0, KEY_5 + "\n", "")

    @pytest.mark.parametrize("name", ["macos", "windows"])
    def test_elsewhere_the_data_directory_is_not_named(self, temp_home, monkeypatch, name):
        monkeypatch.setattr(key_helper, "_platform", lambda: name)
        assert claude_settings.helper_command(5) == f"{QSWAP} key-helper 5"

    def test_the_previous_copy_when_the_saved_one_is_not_a_key(self, temp_home, monkeypatch):
        monkeypatch.setattr(key_helper, "_platform", lambda: "linux")
        root = key_helper.data_dir()
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}}, {
            f".creds-5-{EMAIL_5}.enc": json.dumps({"mcpOAuth": MCP}),
            f".creds-5-{EMAIL_5}.enc.prev": KEY_5,
        })
        before = sorted(p.name for p in (root / "credentials").iterdir())
        assert _helper(["5"]) == (0, KEY_5 + "\n", "")
        assert sorted(p.name for p in (root / "credentials").iterdir()) == before

    @pytest.mark.parametrize("case", [
        "no-data", "missing", "subscription", "no-key", "not-a-key", "torn", "keychain",
    ])
    def test_every_failure_prints_nothing_on_stdout(self, temp_home, monkeypatch, case,
                                                     block_real_keychain):
        monkeypatch.setattr(key_helper, "_platform", lambda: "macos")
        root = key_helper.data_dir()
        accounts = {"5": {"email": EMAIL_5, "kind": "api_key"},
                    "1": {"email": MAYA}}
        if case == "missing":
            _seed_data(root, {"1": {"email": MAYA}})
        elif case == "subscription":
            _seed_data(root, accounts, {f".creds-1-{MAYA}.enc": '{"claudeAiOauth": {}}'})
        elif case == "no-key":
            _seed_data(root, accounts)
        elif case == "not-a-key":
            _seed_data(root, accounts, {f".creds-5-{EMAIL_5}.enc": '{"mcpOAuth": {}}'})
        elif case == "torn":
            root.mkdir(parents=True)
            (root / "sequence.json").write_text("{not json", encoding="utf-8")
        elif case == "keychain":
            _seed_data(root, accounts)

            def boom(service, account):
                raise macos_keychain.KeychainError("locked")

            monkeypatch.setattr(macos_keychain, "get_password", boom)
        number = "1" if case == "subscription" else "5"
        code, out, err = _helper([number])
        assert code != 0
        assert out == ""
        assert err.startswith("qswap key-helper: ") and err.count("\n") == 1
        assert KEY_5 not in err

    @pytest.mark.parametrize("argv", [
        [], ["x"], ["5", "6"], ["-5"], ["--check"], ["5", "--data-dir"],
        ["5", "--check", "--check"], ["5", "--data-dir", "/a", "--data-dir", "/b"],
        ["5", "--other"],
        # Digits str.isdigit() takes and int() does not, and other scripts'.
        ["\u00b2"], ["5\u00b2"], ["\u0665"], ["\uff15"],
    ])
    def test_a_bad_argument_is_a_usage_error_in_one_line(self, argv):
        """Claude Code shows the helper's standard error as it is: one line,
        exit 64 (E1)."""
        code, out, err = _helper(argv)
        assert (code, out) == (64, "")
        assert err.count("\n") == 1 and err.startswith("qswap key-helper: ")
        assert "usage: qswap key-helper N" in err
        assert len(err.rstrip("\n")) <= display.WIDTH

    def test_help(self):
        code, out, err = _helper(["--help"])
        assert code == 0 and err == ""
        assert out.startswith("usage: qswap key-helper N")
        assert "examples:" in out  # as every other verb's help
        assert "--check" in out
        # The exit status of each outcome is stated (E1).
        assert "exits 1" in out and "64" in out
        assert "exits 1" in key_helper.__doc__ and "64" in key_helper.__doc__

    def test_check_says_nothing_when_the_key_can_be_read(self, temp_home, monkeypatch):
        monkeypatch.setattr(key_helper, "_platform", lambda: "linux")
        root = key_helper.data_dir()
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})
        assert _helper(["5", "--check"]) == (0, "", "")
        # Any order after the number, as doctor appends it to the line.
        assert _helper(["5", "--data-dir", str(root), "--check"]) == (0, "", "")
        assert _helper(["5", "--check", "--data-dir", str(root)]) == (0, "", "")

    @pytest.mark.parametrize("case", ["missing", "no-key", "subscription"])
    def test_check_fails_in_one_line_when_the_key_cannot_be_read(
        self, temp_home, monkeypatch, case
    ):
        monkeypatch.setattr(key_helper, "_platform", lambda: "linux")
        root = key_helper.data_dir()
        accounts = {"5": {"email": EMAIL_5, "kind": "api_key"}, "1": {"email": MAYA}}
        if case == "missing":
            _seed_data(root, {"1": {"email": MAYA}})
        else:
            _seed_data(root, accounts, {f".creds-1-{MAYA}.enc": '{"claudeAiOauth": {}}'})
        number = "1" if case == "subscription" else "5"
        code, out, err = _helper([number, "--check"])
        assert (code, out) == (1, "")
        assert err.startswith("qswap key-helper: ") and err.count("\n") == 1

    def test_hidden_from_the_main_help_map(self):
        from ai_quota_swap import helptext

        assert "key-helper" not in helptext.MAIN_HELP

    def test_the_entry_point_answers_before_the_cli(self, temp_home, monkeypatch, capsys):
        from ai_quota_swap import entry

        monkeypatch.setattr(key_helper, "_platform", lambda: "linux")
        _seed_data(key_helper.data_dir(), {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})
        for main in (entry.main, cli.main):
            capsys.readouterr()
            with patch.object(sys, "argv", ["qswap", "key-helper", "5"]), \
                 pytest.raises(SystemExit) as caught:
                main()
            out, err = capsys.readouterr()
            assert caught.value.code == 0
            assert (out, err) == (KEY_5 + "\n", "")

    def test_writes_no_file(self, temp_home, monkeypatch):
        monkeypatch.setattr(key_helper, "_platform", lambda: "linux")
        root = key_helper.data_dir()
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})

        def tree():
            return sorted(
                (str(p.relative_to(temp_home)), p.stat().st_mtime_ns)
                for p in temp_home.rglob("*")
            )

        before = tree()
        _helper(["5"])
        _helper(["9"])
        assert tree() == before

    @pytest.mark.parametrize("platform, env, expected", [
        (Platform.LINUX, {}, ".local/share/ai-quota-swap"),
        (Platform.LINUX, {"XDG_DATA_HOME": "{home}/xdg"}, "xdg/ai-quota-swap"),
        (Platform.LINUX, {"XDG_DATA_HOME": "relative"}, ".local/share/ai-quota-swap"),
        (Platform.MACOS, {"XDG_DATA_HOME": "{home}/xdg"}, ".ai-quota-swap"),
        (Platform.WINDOWS, {}, ".ai-quota-swap"),
    ])
    def test_finds_the_data_directory_the_way_qswap_does(
        self, temp_home, monkeypatch, platform, env, expected
    ):
        names = {Platform.LINUX: "linux", Platform.MACOS: "macos", Platform.WINDOWS: "windows"}
        monkeypatch.setattr(key_helper, "_platform", lambda: names[platform])
        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))
        for name, value in env.items():
            monkeypatch.setenv(name, value.format(home=temp_home))
        assert key_helper.data_dir() == paths.get_backup_root()
        assert key_helper.data_dir() == temp_home.joinpath(*expected.split("/"))

    def test_imports_nothing_that_could_print_or_connect(self, tmp_path):
        """In a fresh interpreter with a minimal environment, from another
        directory: only the leaf modules load."""
        home = tmp_path / "home"
        home.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        script = textwrap.dedent("""
            import json, sys
            sys.argv = ["qswap", "key-helper", "5"]
            from ai_quota_swap import entry
            try:
                entry.main()
            except SystemExit as e:
                code = e.code
            mods = sorted(m for m in sys.modules if m.startswith("ai_quota_swap"))
            heavy = [m for m in ("urllib.request", "http.client", "ssl", "sqlite3",
                                 "textual", "rumps", "importlib.metadata", "email",
                                 "zipfile") if m in sys.modules]
            sys.stderr.write(json.dumps({"code": code, "mods": mods, "heavy": heavy}))
        """)
        env = {"HOME": str(home), "USERPROFILE": str(home), "PATH": os.defpath}
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        done = subprocess.run(
            [sys.executable, "-c", script], cwd=elsewhere, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert done.stdout == ""
        report = json.loads(done.stderr.splitlines()[-1][done.stderr.splitlines()[-1].index("{"):])
        assert report["code"] == 1
        assert set(report["mods"]) <= {
            "ai_quota_swap", "ai_quota_swap.entry", "ai_quota_swap.key_helper",
            "ai_quota_swap.macos_keychain", "ai_quota_swap.spawn",
        }
        assert report["heavy"] == []


# == B. the settings editor ===============================================================


class TestSettingsStates:
    def test_no_file(self, tmp_path):
        assert claude_settings.read_state(tmp_path / "settings.json").kind == "no-file"

    def test_no_helper(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text('{"model": "opus"}', encoding="utf-8")
        assert claude_settings.read_state(path).kind == "none"

    @pytest.mark.parametrize("line, number", [
        ("/opt/qswap/bin/qswap key-helper 5", "5"),
        ("/usr/local/bin/qswap key-helper 12", "12"),
        ('"/Users/a b/.local/bin/qswap" key-helper 5', "5"),
        ("C:\\Users\\a\\.local\\bin\\qswap.exe key-helper 3", "3"),
        ("/home/u/.local/bin/ai-quota-swap key-helper 4", "4"),
        ("/usr/bin/python3 -m ai_quota_swap key-helper 6", "6"),
        ("/usr/bin/python3 -P -m ai_quota_swap key-helper 6", "6"),
        ('"/a b/python3.12" -P -m ai_quota_swap key-helper 6 --data-dir /d', "6"),
        ("qswap key-helper 05", "5"),
    ])
    def test_qswaps_line_by_its_structure(self, tmp_path, line, number):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"apiKeyHelper": line}), encoding="utf-8")
        state = claude_settings.read_state(path)
        # Recognised by the same structure, a line naming another data
        # directory than this qswap's is another qswap's (decision SS4).
        kind = "other-qswap" if "--data-dir" in line else "ours"
        assert (state.kind, state.number) == (kind, number)

    @pytest.mark.parametrize("line", [
        "/opt/vault/print-key", "qswap key-helper", "qswap key-helper 5 extra",
        "/x/notqswap key-helper 5", "echo sk-ant-api03-zzzz", 42,
    ])
    def test_someone_elses_helper(self, tmp_path, line):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"apiKeyHelper": line}), encoding="utf-8")
        assert claude_settings.read_state(path).kind == "foreign"

    @pytest.mark.parametrize("text", [
        "{not json", "[1, 2]", '"x"',
        '{"apiKeyHelper": "qswap key-helper 5", "apiKeyHelper": "qswap key-helper 6"}',
    ])
    def test_not_valid(self, tmp_path, text):
        path = tmp_path / "settings.json"
        path.write_text(text, encoding="utf-8")
        assert claude_settings.read_state(path).kind == "invalid"

    def test_the_path_follows_claude_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        assert claude_settings.settings_path() == tmp_path / "cfg" / "settings.json"


class TestSettingsWrites:
    LINE = f"{QSWAP} key-helper 5"

    @pytest.mark.parametrize("before, after", [
        # The line goes first (decision B4): deleting it by hand leaves the
        # object valid, where a last member would leave a trailing comma.
        (
            '{\n  "model": "opus",\n  "hooks": {"Stop": []}\n}\n',
            '{\n  "apiKeyHelper": "LINE",\n  "model": "opus",\n  "hooks": {"Stop": []}\n}\n',
        ),
        (
            '{\n    "b": 1,\n    "a": 2\n}',
            '{\n    "apiKeyHelper": "LINE",\n    "b": 1,\n    "a": 2\n}',
        ),
        (
            '{\n\t"z": true\n}\n',
            '{\n\t"apiKeyHelper": "LINE",\n\t"z": true\n}\n',
        ),
        ('{"a":1}', '{"apiKeyHelper":"LINE","a":1}'),  # the file's own separators
        ('{"a": 1, "b": 2}', '{"apiKeyHelper": "LINE", "a": 1, "b": 2}'),
        ('{\n  "name": "café"\n}\n', '{\n  "apiKeyHelper": "LINE",\n  "name": "café"\n}\n'),
        ("{}", '{"apiKeyHelper": "LINE"}'),
    ])
    def test_adds_one_key_and_keeps_the_rest(self, tmp_path, before, after):
        path = tmp_path / "settings.json"
        path.write_text(before, encoding="utf-8")
        claude_settings.install(5, path=path, command=self.LINE)
        assert path.read_text(encoding="utf-8") == after.replace("LINE", self.LINE)
        claude_settings.remove(path=path)
        assert path.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("before", [
        '{\n  "model": "opus",\n  "hooks": {\n    "Stop": []\n  }\n}\n',
        '{\n    "b": 1\n}',
        '{\n\t"z": true,\n\t"y": [1, 2]\n}\n',
        '{\r\n  "a": 1,\r\n  "b": "x"\r\n}\r\n',
        '\n{\n  "a": 1\n}\n\n',
    ])
    def test_deleting_the_line_by_hand_leaves_the_file_as_it_was(self, tmp_path, before):
        """docs/api-accounts.md tells the reader to delete the apiKeyHelper
        line by hand (F49): in a file with one member per line that is one
        whole line, and what is left is the file as it was."""
        path = tmp_path / "settings.json"
        path.write_bytes(before.encode("utf-8"))
        claude_settings.install(5, path=path, command=self.LINE)
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        mine = [line for line in lines if '"apiKeyHelper"' in line]
        assert len(mine) == 1
        left = "".join(line for line in lines if line not in mine)
        assert left == before
        json.loads(left)

    FIRST_ON_THE_BRACE = [
        '{"model": "opus",\n "includeCoAuthoredBy": false,\n "env": {"A": "1"}}\n',
        '{"model": "opus",\n "hooks": {}}',
        '{ "model": "opus",\n  "hooks": {}\n}',
        '{"model": "opus"\n}\n',
        '{"a": 1, "b": 2,\r\n\t"c": 3}\r\n',
    ]

    @pytest.mark.parametrize("before", FIRST_ON_THE_BRACE)
    def test_a_first_member_on_the_brace_line_gets_a_line_of_its_own(self, tmp_path, before):
        """The re-check's case (B4): in a file of several lines whose first
        member shares the brace's line, the line went on the brace's line
        too, and deleting it by hand took the brace with it. It now goes on
        a line of its own; qswap's own removal puts the brace's line back as
        it was (the lead is recorded when the key is entered)."""
        path = tmp_path / "settings.json"
        path.write_bytes(before.encode("utf-8"))
        lead = claude_settings.brace_lead(path)  # what _enter_key records
        assert lead is not None
        claude_settings.install(5, path=path, command=self.LINE)
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        mine = [line for line in lines if '"apiKeyHelper"' in line]
        assert len(mine) == 1 and mine[0].strip() == f'"apiKeyHelper": "{self.LINE}",'
        left = "".join(line for line in lines if line not in mine)
        assert json.loads(left) == json.loads(before)
        claude_settings.install(6, path=path, command=f"{QSWAP} key-helper 6")
        claude_settings.remove(path=path, restore_lead=lead)
        assert path.read_bytes() == before.encode("utf-8")

    @pytest.mark.parametrize("before", FIRST_ON_THE_BRACE[:2])
    def test_the_brace_line_comes_back_after_the_key(self, temp_home, before):
        s = _fleet()
        _settings(temp_home).write_bytes(before.encode("utf-8"))
        _activate(s)
        s.switch_to("6", json_output=True)
        s.switch_to("1", json_output=True)
        assert _settings(temp_home).read_bytes() == before.encode("utf-8")

    @pytest.mark.parametrize("before", [
        '﻿{\n  "model": "opus"\n}\n',
        '﻿{\r\n  "a": 1\r\n}\r\n',
        '﻿{}',
        '﻿',
    ])
    def test_a_byte_order_mark_is_kept(self, tmp_path, before):
        """F14: the mark stays on every write, and the round trip is byte
        for byte."""
        path = tmp_path / "settings.json"
        path.write_bytes(before.encode("utf-8"))
        blank = claude_settings.blank_text(path)  # what _enter_key records
        claude_settings.install(5, path=path, command=self.LINE)
        assert path.read_bytes().startswith("﻿".encode("utf-8"))
        assert claude_settings.read_state(path).number == "5"
        claude_settings.install(6, path=path, command=f"{QSWAP} key-helper 6")
        assert path.read_bytes().startswith("﻿".encode("utf-8"))
        claude_settings.remove(path=path, restore_blank=blank)
        assert path.read_bytes() == before.encode("utf-8")

    @pytest.mark.parametrize("before", [
        "{}", "{}\n", "{\n}", "{\n}\n", "{ }", "{\n\n}", "{\n  \n}", "{\r\n}\r\n",
        "{}\n\n", "\n\n  {}  \n\n",
    ])
    def test_an_empty_object_in_any_spelling_round_trips(self, tmp_path, before):
        """F14: one line or several is decided from the text inside the
        braces, and the line comes out leaving that text as it was."""
        path = tmp_path / "settings.json"
        path.write_bytes(before.encode("utf-8"))
        claude_settings.install(5, path=path, command=self.LINE)
        assert json.loads(path.read_text(encoding="utf-8")) == {"apiKeyHelper": self.LINE}
        claude_settings.remove(path=path)
        assert path.read_bytes() == before.encode("utf-8")

    @pytest.mark.parametrize("before", ["{ }", "{\n\n}", "\n{}\n\n", "﻿{ }"])
    def test_an_empty_object_comes_back_after_the_key(self, temp_home, before):
        """The blank-file record, generalised: a file that was an empty
        object gets its own text back when the line comes out, even when
        Claude Code rewrote it in its own format meanwhile."""
        s = _fleet()
        _settings(temp_home).write_bytes(before.encode("utf-8"))
        _activate(s)
        bom = "﻿" if before.startswith("﻿") else ""
        line = _settings_obj_any(temp_home)["apiKeyHelper"]
        _settings(temp_home).write_bytes(
            (bom + json.dumps({"apiKeyHelper": line}, indent=2) + "\n").encode("utf-8")
        )
        s.switch_to("6", json_output=True)
        s.switch_to("1", json_output=True)
        assert _settings(temp_home).read_bytes() == before.encode("utf-8")

    def test_replaces_its_own_line_in_place(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            '{\n  "apiKeyHelper": "/old/path/qswap key-helper 5",\n  "model": "opus"\n}\n',
            encoding="utf-8",
        )
        claude_settings.install(6, path=path, command=f"{QSWAP} key-helper 6")
        assert path.read_text(encoding="utf-8") == (
            f'{{\n  "apiKeyHelper": "{QSWAP} key-helper 6",\n  "model": "opus"\n}}\n'
        )

    def test_removing_the_first_of_several_keys(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(
            f'{{\n  "apiKeyHelper": "{self.LINE}",\n  "model": "opus"\n}}\n', encoding="utf-8"
        )
        assert claude_settings.remove(path=path) is True
        assert path.read_text(encoding="utf-8") == '{\n  "model": "opus"\n}\n'

    def test_a_missing_file_is_created_and_removed_again(self, tmp_path):
        path = tmp_path / ".claude" / "settings.json"
        created, state = claude_settings.install(5, path=path, command=self.LINE)
        assert created and state.ours
        assert json.loads(path.read_text(encoding="utf-8")) == {"apiKeyHelper": self.LINE}
        claude_settings.remove(path=path, remove_file_if_empty=True)
        assert not path.exists()

    def test_a_created_file_that_gained_keys_is_kept(self, tmp_path):
        path = tmp_path / "settings.json"
        claude_settings.install(5, path=path, command=self.LINE)
        obj = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**obj, "model": "opus"}, indent=2) + "\n", encoding="utf-8")
        claude_settings.remove(path=path, remove_file_if_empty=True)
        assert json.loads(path.read_text(encoding="utf-8")) == {"model": "opus"}

    @POSIX_ONLY
    def test_keeps_the_file_mode(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text('{"a": 1}\n', encoding="utf-8")
        os.chmod(path, 0o640)
        claude_settings.install(5, path=path, command=self.LINE)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
        claude_settings.remove(path=path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640

    def test_writes_through_a_link(self, tmp_path):
        real = tmp_path / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"a": 1}\n', encoding="utf-8")
        link = tmp_path / "settings.json"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
        claude_settings.install(5, path=link, command=self.LINE)
        assert link.is_symlink()
        assert json.loads(real.read_text(encoding="utf-8"))["apiKeyHelper"] == self.LINE
        claude_settings.remove(path=link)
        assert link.is_symlink()
        assert json.loads(real.read_text(encoding="utf-8")) == {"a": 1}

    def test_a_change_made_while_writing_is_kept(self, tmp_path, monkeypatch):
        """Claude Code writes this file too: the editor re-reads right before
        the rename and starts again from what is there now."""
        path = tmp_path / "settings.json"
        path.write_text('{\n  "a": 1\n}\n', encoding="utf-8")
        real_read = claude_settings._read
        calls = {"n": 0}

        def read(p):
            calls["n"] += 1
            if calls["n"] == 2:  # the re-read before the first write
                p.write_text('{\n  "a": 1,\n  "theme": "dark"\n}\n', encoding="utf-8")
            return real_read(p)

        monkeypatch.setattr(claude_settings, "_read", read)
        claude_settings.install(5, path=path, command=self.LINE)
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "a": 1, "theme": "dark", "apiKeyHelper": self.LINE,
        }

    def test_a_file_that_keeps_changing_is_not_written(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        texts = iter(f'{{"a": {i}}}' for i in range(100))
        monkeypatch.setattr(claude_settings, "_read", lambda p: next(texts))
        monkeypatch.setattr(claude_settings.time, "sleep", lambda s: None)
        with pytest.raises(claude_settings.SettingsError) as caught:
            claude_settings.install(5, path=path, command=self.LINE)
        assert caught.value.kind == "changing"
        assert path.read_text(encoding="utf-8") == '{"a": 1}'

    @pytest.mark.parametrize("text, kind", [
        ("{not json", "invalid"),
        ('{"apiKeyHelper": "/opt/vault/print-key"}', "foreign"),
    ])
    def test_never_writes_a_file_it_cannot_parse_or_another_helper(self, tmp_path, text, kind):
        path = tmp_path / "settings.json"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(claude_settings.SettingsError) as caught:
            claude_settings.install(5, path=path, command=self.LINE)
        assert caught.value.kind == kind
        assert path.read_text(encoding="utf-8") == text

    def test_removal_leaves_another_helper_alone(self, tmp_path):
        path = tmp_path / "settings.json"
        text = '{"apiKeyHelper": "/opt/vault/print-key"}'
        path.write_text(text, encoding="utf-8")
        assert claude_settings.remove(path=path) is False
        assert path.read_text(encoding="utf-8") == text

    def test_the_copy_for_a_run_profile_has_no_helper(self):
        text = f'{{\n  "a": 1,\n  "apiKeyHelper": "{self.LINE}"\n}}\n'
        assert claude_settings.without_helper(text) == '{\n  "a": 1\n}\n'
        assert claude_settings.without_helper("{bad") == "{bad"

    def test_the_command_names_the_installed_qswap(self, monkeypatch, tmp_path):
        """The real resolver: started through a link (in a directory with a
        space) to this install's own console script, off PATH, the line
        names that link, quoted."""
        from ai_quota_swap import installed

        monkeypatch.setattr(claude_settings, "qswap_executable", _REAL_EXECUTABLE)
        monkeypatch.setattr(key_helper, "_platform", lambda: "macos")
        own = installed.own_scripts()
        if not own:
            pytest.skip("this environment has no qswap console script")
        exe = tmp_path / "bin with space" / own[0].name
        exe.parent.mkdir()
        try:
            exe.symlink_to(own[0])
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
        monkeypatch.setenv("PATH", os.defpath)
        monkeypatch.setattr(sys, "argv", [str(exe), "switch", "5"])
        line = claude_settings.helper_command(5)
        quote = '"' if sys.platform == "win32" else "'"
        assert line == f"{quote}{exe}{quote} key-helper 5"
        assert claude_settings.parse_line(line) == "5"

    @POSIX_ONLY
    @pytest.mark.parametrize("dirname", [
        "a$HOME b", "back`tick`", "it's here", "it's $HOME `x` \\z", 'a\\b "c"$d',
    ])
    def test_the_shell_runs_the_path_as_written(self, monkeypatch, tmp_path, dirname):
        """Claude Code runs the line through a shell: ``$``, backticks and
        backslashes in the path must reach it as they are, and the line must
        still read as qswap's, with the program found where it is."""
        exe = tmp_path / dirname / "qswap"
        exe.parent.mkdir()
        exe.write_text('#!/bin/sh\necho "ran $*"\n', encoding="utf-8")
        exe.chmod(0o755)
        monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [str(exe)])
        line = claude_settings.helper_command(5)
        done = subprocess.run(["/bin/sh", "-c", line], capture_output=True, text=True)
        assert (done.returncode, done.stdout) == (0, "ran key-helper 5\n"), done.stderr
        assert claude_settings.parse_line(line) == "5"
        assert _REAL_COMMAND_MISSING(line) is False
        exe.unlink()
        assert _REAL_COMMAND_MISSING(line) is True


def _random_path(rng, alphabet: str, sep: str, head: str, tail: str) -> str:
    segments = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
        for _ in range(rng.randint(1, 3))
    ]
    return head + sep.join(segments) + sep + tail


#: Every character _quote treats specially on POSIX, both quotes together,
#: and some that are not special at all.
_POSIX_CHARS = "ab Z.-_é'\"$`\\!*?&;|<>()[]{}#~=\t\n%^,"
#: What a Windows path can hold (no double quote), with cmd.exe's specials.
_WINDOWS_CHARS = "ab Z.-_é'$`!&;()^%#~=[]{},"


class TestTheLineReadsBack:
    """Quoting is a round trip (F3, F19): every line _quote can write reads
    back as qswap's line for the same account, with the same program and
    data directory, and command_missing tests the path the line runs."""

    def _line(self, monkeypatch, program: str, data_dir: str | None, number: int = 5) -> str:
        monkeypatch.setattr(claude_settings, "qswap_executable", lambda: [program])
        monkeypatch.setattr(key_helper, "pinned_data_dir", lambda: data_dir)
        return claude_settings.helper_command(number)

    @POSIX_ONLY
    def test_posix_paths_with_every_special_character(self, monkeypatch, tmp_path):
        import random

        rng = random.Random(20261110)
        cases = [("/Users/o'brien/say \"hi\"/bin/qswap", "/d/it's \"x\"")] + [
            (
                _random_path(rng, _POSIX_CHARS, "/", "/", "qswap"),
                _random_path(rng, _POSIX_CHARS, "/", "/", "ai-quota-swap"),
            )
            for _ in range(300)
        ]
        for i, (program, data_dir) in enumerate(cases):
            line = self._line(monkeypatch, program, data_dir, number=i)
            parts = claude_settings.read_line(line)
            assert parts is not None, line
            assert (parts.program, parts.data_dir, parts.number) == (program, data_dir, str(i))
            assert claude_settings.parse_line(line) == str(i)
            if i % 20 == 0:
                # The shell reads the same words the parser does.
                done = subprocess.run(
                    ["/bin/sh", "-c", "printf '%s\\0' " + line], capture_output=True,
                )
                words = done.stdout.decode("utf-8").split("\0")[:-1]
                assert words == [program, "key-helper", str(i), "--data-dir", data_dir]

    @POSIX_ONLY
    def test_command_missing_tests_the_path_the_line_runs(self, monkeypatch, tmp_path):
        exe = tmp_path / "it's \"x\" $y" / "qswap"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        line = self._line(monkeypatch, str(exe), None)
        assert _REAL_COMMAND_MISSING(line) is False
        exe.unlink()
        assert _REAL_COMMAND_MISSING(line) is True

    def test_windows_paths_are_not_unescaped(self, monkeypatch):
        """cmd.exe takes nothing out of a double-quoted word: a UNC path and
        a backslash before $ keep every backslash."""
        import random

        monkeypatch.setattr(sys, "platform", "win32")
        rng = random.Random(1110)
        cases = [
            (r"\\fileserver\home$\jsmith\.local\bin\qswap.exe", None),
            (r"C:\Users\John Smith\$x\qswap.exe", r"C:\Users\John Smith\data$"),
        ] + [
            (
                _random_path(rng, _WINDOWS_CHARS, "\\", rng.choice(["C:\\", "\\\\srv\\s$\\"]),
                             "qswap.exe"),
                _random_path(rng, _WINDOWS_CHARS, "\\", "D:\\", "data"),
            )
            for _ in range(300)
        ]
        for program, data_dir in cases:
            line = self._line(monkeypatch, program, data_dir)
            parts = claude_settings.read_line(line)
            assert parts is not None, line
            assert (parts.program, parts.data_dir) == (program, data_dir)
            assert claude_settings.parse_line(line) == "5"
            seen: list[str] = []
            monkeypatch.setattr(claude_settings.os.path, "exists", lambda p: seen.append(p))
            assert _REAL_COMMAND_MISSING(line) is True
            assert seen == [program]
            monkeypatch.setattr(claude_settings.os.path, "exists", lambda p: p == program)
            assert _REAL_COMMAND_MISSING(line) is False
            monkeypatch.undo()
            monkeypatch.setattr(sys, "platform", "win32")

    @POSIX_ONLY
    @pytest.mark.parametrize("on_path", [True, False])
    def test_the_menu_bars_switch_writes_a_line_the_shell_runs(
        self, temp_home, monkeypatch, tmp_path, on_path
    ):
        """F21: the real resolver in the menu bar's process shape (the
        bundle's interpreter, argv[0] the package's __main__.py, the plist's
        PATH): the engine's switch writes this install's console script, the
        PATH link when the plist's PATH has it, and /bin/sh runs the line."""
        from ai_quota_swap import installed

        monkeypatch.setattr(claude_settings, "qswap_executable", _REAL_EXECUTABLE)
        monkeypatch.setattr(key_helper, "_platform", lambda: "macos")
        venv = tmp_path / "tools" / "ai-quota-swap"
        site = venv / "lib" / "python3.12" / "site-packages"
        info = site / "ai_quota_swap-0.1.0.dist-info"
        info.mkdir(parents=True)
        (info / "RECORD").write_text("../../../bin/qswap,sha256=x,1\n", encoding="utf-8")
        script = venv / "bin" / "qswap"
        script.parent.mkdir()
        script.write_text('#!/bin/sh\necho "ran $*"\n', encoding="utf-8")
        script.chmod(0o755)
        contents = tmp_path / "AI Quota Swap Menubar.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "lib").mkdir()
        (contents / "lib" / "python3.12").symlink_to(venv / "lib" / "python3.12")
        bundle_site = contents / "lib" / "python3.12" / "site-packages"
        link = temp_home / ".local" / "bin" / "qswap"
        link.parent.mkdir(parents=True)
        link.symlink_to(script)
        monkeypatch.setattr(installed, "_package_dir", lambda: bundle_site / "ai_quota_swap")
        monkeypatch.setattr(installed, "_interpreter_site_dirs", lambda: [bundle_site])
        monkeypatch.setattr(sys, "executable", str(contents / "MacOS" / "python"))
        monkeypatch.setattr(sys, "argv", [
            str(bundle_site / "ai_quota_swap" / "__main__.py"), "menubar", "--foreground",
        ])
        plist_path = "/usr/bin:/bin:/usr/sbin:/sbin"
        monkeypatch.setenv("PATH", f"{link.parent}:{plist_path}" if on_path else plist_path)
        s = _fleet()
        s.switch_to("5", json_output=True, entered_by="engine")
        line = _settings_obj(temp_home)["apiKeyHelper"]
        assert line == f"{link if on_path else script} key-helper 5"
        done = subprocess.run(["/bin/sh", "-c", line], cwd=tmp_path, capture_output=True,
                              text=True)
        assert (done.returncode, done.stdout) == (0, "ran key-helper 5\n")

    @POSIX_ONLY
    def test_the_interpreter_form_ignores_the_sessions_project_files(self, tmp_path):
        """F17: Claude Code runs the line from the session's project
        directory. With -P a project file named like a standard-library
        module is not imported; the bare -m form imports it."""
        root = tmp_path / "data"
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})
        project = tmp_path / "project"
        project.mkdir()
        for module in ("base64", "json", "calendar"):
            (project / f"{module}.py").write_text(
                "open('ran', 'w').write('x')\nraise SystemExit(3)\n", encoding="utf-8"
            )
        q = claude_settings._quote
        env = {"HOME": str(tmp_path / "home"), "PATH": os.defpath}
        for flags, answers in (("-P -m", True), ("-m", False)):
            line = f"{q(sys.executable)} {flags} ai_quota_swap key-helper 5 --data-dir {q(str(root))}"
            assert claude_settings.parse_line(line) == "5"
            done = subprocess.run(["/bin/sh", "-c", line], cwd=project, env=env,
                                  capture_output=True, text=True, timeout=60)
            if answers:
                assert (done.returncode, done.stdout) == (0, KEY_5 + "\n"), done.stderr
                assert not (project / "ran").exists()
            else:
                assert done.returncode != 0 and (project / "ran").exists()

    @pytest.mark.parametrize("line", [
        "/opt/qswap/bin/qswap key-helper 5 --data-dir /x;touch${IFS}/tmp/marker;:",
        "/opt/qswap/bin/qswap key-helper 5 --data-dir /x$(touch${IFS}/tmp/m)",
        "/opt/qswap/bin/qswap key-helper 5 --data-dir /x`touch${IFS}/tmp/m`",
        "/opt/qswap/bin/qswap key-helper 5 --data-dir /x&&id",
        "/opt/qswap/bin/qswap key-helper 5 --data-dir /x|id",
        "/x;id;/opt/qswap/bin/qswap key-helper 5",
        "/opt/$(id)/qswap key-helper 5",
        "/opt/qswap/bin/qswap key-helper 5 --data-dir /x>/tmp/out",
    ])
    def test_a_line_the_shell_reads_as_more_than_one_command_is_not_qswaps(
        self, monkeypatch, line
    ):
        """The re-check's case: the bare word took ; $( ) and backticks, so
        doctor and an already-active switch ran such a line with --check,
        and its second command with it. Only what _quote leaves bare reads
        as a bare word; anything else must be quoted to be qswap's."""
        for platform in ("linux", "win32"):
            monkeypatch.setattr(sys, "platform", platform)
            assert claude_settings.read_line(line) is None, (platform, line)
            assert claude_settings.parse_line(line) is None

    def test_a_line_qswap_could_not_read_back_is_never_written(self, monkeypatch, tmp_path):
        """A command that would not read as qswap's line for that account is
        refused before the write (F19): qswap could never remove it."""
        path = tmp_path / "settings.json"
        path.write_text('{"a": 1}\n', encoding="utf-8")
        for command in ("/opt/tool/print-key key-helper 5", f"{QSWAP} key-helper 6"):
            with pytest.raises(claude_settings.SettingsError) as caught:
                claude_settings.install(5, path=path, command=command)
            assert caught.value.kind == "command"
            assert path.read_text(encoding="utf-8") == '{"a": 1}\n'
        monkeypatch.setattr(claude_settings, "qswap_executable", lambda: ["/opt/tool/notqswap"])
        with pytest.raises(claude_settings.SettingsError):
            claude_settings.install(5, path=path)
        assert path.read_text(encoding="utf-8") == '{"a": 1}\n'

    def test_the_switch_is_refused_rather_than_writing_it(self, temp_home, monkeypatch):
        s = _fleet()
        monkeypatch.setattr(claude_settings, "qswap_executable", lambda: ["/opt/tool/notqswap"])
        with pytest.raises(KeyHelperUnavailableError):
            s.switch_to("5", json_output=True)
        assert not _settings(temp_home).exists()
        assert s.current_account_number() == "1"


# == C. who is active =====================================================================


def _activate(s, number="5", entered_by="user"):
    return s.switch_to(number, json_output=True, entered_by=entered_by)


class TestWhoIsActive:
    def test_the_key_is_active_and_the_login_is_underneath(self, temp_home):
        s = _fleet()
        _activate(s)
        assert s.current_account_number() == "5"
        assert s.login_account_number() == "1"
        assert s.underneath_account_number() == "1"
        assert s.helper_account() == "5"
        assert s.active_view() == ("5", "1")
        assert s.active_email() == EMAIL_5

    def test_without_the_line_the_login_rule_holds(self, temp_home):
        s = _fleet()
        assert s.current_account_number() == "1"
        assert s.underneath_account_number() is None
        assert s.active_view() == (None, None)

    def test_a_line_written_by_hand_counts(self, temp_home):
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": "/other/place/qswap key-helper 6"}), encoding="utf-8"
        )
        assert s.current_account_number() == "6"
        assert s.key_entered_by() == "user"

    @pytest.mark.parametrize("number, why", [("9", "missing"), ("7", "not-api-key")])
    def test_a_line_for_no_key_is_reported_not_active(self, temp_home, number, why):
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper {number}"}), encoding="utf-8"
        )
        assert s.current_account_number() == "1"
        assert s.helper_problem() == {"number": number, "why": why}
        notes = " ".join(s.key_helper_notes())
        # An account that exists is named as everywhere else (DS7, round 3).
        named = "#7 work" if number == "7" else f"#{number}"
        assert f"make {named} the active API key" in notes
        assert "qswap switch 1" in notes

    def test_list_text(self, temp_home, capsys):
        s = _fleet()
        _activate(s)
        capsys.readouterr()
        s.list_accounts()
        out = capsys.readouterr().out
        lines = out.splitlines()
        key = next(line for line in lines if line.startswith("  #5 "))
        assert key == "  #5 ci · active · API key · over #1 personal"
        under = next(line for line in lines if line.startswith("  #1 "))
        assert "· underneath" in under and "active" not in under

    def test_list_json(self, temp_home):
        s = _fleet()
        _activate(s)
        payload = s.list_accounts(json_output=True)
        assert payload["activeAccountNumber"] == 5
        assert payload["underneathAccountNumber"] == 1
        rows = {row["number"]: row for row in payload["accounts"]}
        assert rows[5]["active"] is True and rows[5]["how"] == "helper"
        assert rows[1]["active"] is False and rows[1]["underneath"] is True

    def test_status_text_and_json(self, temp_home):
        s = _fleet()
        _activate(s)
        lines = s.status_lines()
        assert lines[1] == "  #5 ci · active · API key · over #1 personal"
        payload = s.status(json_output=True)
        assert payload["active"]["number"] == 5
        assert payload["active"]["how"] == "helper"
        assert payload["underneath"] == {"number": 1, "email": MAYA}

    def test_the_snapshot(self, temp_home):
        s = _fleet()
        _activate(s)
        snap = s.accounts_snapshot(fetch=set())
        assert snap.active_number == "5"
        assert snap.underneath_number == "1"
        by = {a.number: a for a in snap.accounts}
        assert by["5"].is_active and not by["5"].underneath
        assert by["1"].underneath and not by["1"].is_active

    def test_the_menu_bar_rows(self, temp_home):
        from ai_quota_swap import menubar, menubar_data

        s = _fleet()
        _activate(s)
        snap = s.accounts_snapshot(fetch=set())
        states = menubar_data.account_states(snap, "claude", {}, now=time.time())
        by = {a.number: a for a in states}
        assert by["5"].active and by["5"].over == "#1 personal"
        assert by["1"].underneath and not by["1"].active
        row = menubar.account_row(by["1"], now=time.time(), stale_after_s=600)
        assert row.text.startswith("#1 personal · underneath")
        assert not row.checked
        key_row = menubar.account_row(by["5"], now=time.time(), stale_after_s=600)
        assert key_row.checked and "over #1 personal" in key_row.text

    def test_the_dashboard_block(self, temp_home):
        from ai_quota_swap.tui import data as tui_data

        s = _fleet()
        _activate(s)
        snap = s.accounts_snapshot(fetch=set())
        view = tui_data.ProviderView(provider="claude", snapshot=snap)
        block = tui_data.block_for(view, view.account("5"))
        assert block.over == "#1 personal" and block.active
        assert tui_data.block_for(view, view.account("1")).underneath

    def test_the_engine_sees_the_key(self, temp_home):
        s = _fleet()
        _activate(s)
        from ai_quota_swap.autoswitch import AutoSwitchEngine

        engine = AutoSwitchEngine.__new__(AutoSwitchEngine)
        engine.switcher = s
        assert engine._helper_key_mode("5") == "user"
        assert engine._helper_key_mode("1") is None

    def test_the_activation_log_observes_the_key_not_the_login(self, temp_home):
        from ai_quota_swap.ledger.activations import ActivationLog

        s = _fleet()
        _activate(s)
        s._build_accounts_info()  # the observation point
        log = ActivationLog.load(s.backup_dir)
        last = log.activations[-1]
        assert last.number == 5 and last.how == "helper"

    def test_run_of_the_account_underneath_refuses_the_fast_path(self, temp_home, monkeypatch):
        from ai_quota_swap import clis
        from ai_quota_swap.session import SessionManager

        s = _fleet()
        _activate(s)
        monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/claude")
        mgr = SessionManager(s)
        monkeypatch.setattr(mgr, "_exec", lambda *a, **k: pytest.fail("exec"))
        with pytest.raises(SessionError) as caught:
            mgr.run("1", [], share=True)
        assert "underneath #5 ci, the active API key" in str(caught.value)
        assert "qswap switch 1" in str(caught.value)

    @pytest.mark.parametrize("where", ["run-profile", "other-home", "default-profile"])
    def test_run_of_the_account_underneath_refuses_with_config_dir_preset(
        self, temp_home, tmp_path, monkeypatch, where
    ):
        """The fast path drops CLAUDE_CONFIG_DIR, so the claude it starts
        reads ~/.claude/settings.json: the refusal reads that file too."""
        from ai_quota_swap import clis
        from ai_quota_swap.session import SessionManager, session_dir_for

        s = _fleet()
        _activate(s)
        if where == "run-profile":
            preset = session_dir_for(s.backup_dir, "7", SAM)
            preset.mkdir(parents=True, exist_ok=True)
            (preset / "settings.json").write_text("{}", encoding="utf-8")
        elif where == "other-home":
            preset = tmp_path / "other-home"
            preset.mkdir()
        else:
            preset = temp_home / ".claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(preset))
        monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/claude")
        mgr = SessionManager(s)
        monkeypatch.setattr(mgr, "_exec", lambda *a, **k: pytest.fail("exec"))
        with pytest.raises(SessionError) as caught:
            mgr.run("1", [], share=True)
        text = " ".join(str(caught.value).split())
        assert "underneath #5 ci, the active API key" in text
        assert "from a normal shell: qswap switch 1" in text

    def test_a_key_line_in_the_preset_directory_does_not_refuse(
        self, temp_home, tmp_path, monkeypatch
    ):
        """No key is active: a line in CLAUDE_CONFIG_DIR's own settings is not
        the one the launched claude reads, so the fast path goes ahead."""
        from ai_quota_swap import clis
        from ai_quota_swap.session import SessionManager

        s = _fleet()
        preset = tmp_path / "other-home"
        preset.mkdir()
        (preset / "settings.json").write_text(
            json.dumps({"apiKeyHelper": "/opt/qswap/bin/qswap key-helper 5"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(preset))
        monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/claude")
        mgr = SessionManager(s)
        seen = {}

        def fake_exec(claude_bin, claude_args, env):
            seen["env"] = env
            raise SystemExit(0)

        monkeypatch.setattr(mgr, "_exec", fake_exec)
        with pytest.raises(SystemExit):
            mgr.run("1", [], share=True)
        assert "CLAUDE_CONFIG_DIR" not in seen["env"]

    def test_doctor_reports_the_active_key(self, temp_home):
        s = _fleet()
        _activate(s)
        checks = {c.id: c for c in doctor.run_checks()}
        check = checks["claude.key_helper"]
        assert check.status == "pass"
        assert "active API key: #5 ci" in check.summary

    @pytest.mark.parametrize("number", ["9", "7"])
    def test_doctor_fails_a_line_for_no_key(self, temp_home, number):
        _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper {number}"}), encoding="utf-8"
        )
        checks = {c.id: c for c in doctor.run_checks()}
        check = checks["claude.key_helper"]
        assert check.status == "fail"
        assert check.fix == "qswap switch 1"

    def test_the_repair_removes_the_line(self, temp_home):
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 9", "a": 1}), encoding="utf-8"
        )
        s.switch_to("1", json_output=True)
        assert _settings_obj(temp_home) == {"a": 1}
        assert s.helper_problem() is None


# == D. switching =========================================================================


class TestSwitching:
    def test_to_a_key_writes_only_the_line(self, temp_home, block_real_keychain, alive):
        s = _fleet()
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        before = _claude_state(block_real_keychain)
        result = _activate(s)
        assert result["switched"] is True and result["how"] == "helper"
        assert _claude_state(block_real_keychain) == before
        assert _settings_obj(temp_home) == {"apiKeyHelper": f"{QSWAP} key-helper 5"}

    def test_the_message(self, temp_home, run, alive):
        s = _fleet()
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        code, out, err = run(s, "switch", "5")
        assert code == 0, err
        assert out.splitlines() == [
            "Switched #1 personal → #5 ci (API key)",
            "Running Claude Code sessions move to the key within a few seconds. #1 personal",
            "stays logged in underneath. claude.ai connectors and Remote Control are",
            "unavailable while a key is in use. Go back with: qswap switch 1",
        ]
        _fits(out)

    def test_a_slot_without_a_key_is_refused_and_no_line_is_written(self, temp_home, run):
        """The line would run a helper that prints nothing, and every Claude
        Code process reading the settings would lose its auth."""
        s = _fleet()
        s._write_account_credentials("5", EMAIL_5, json.dumps({"mcpOAuth": MCP}), retain=False)
        s._write_account_credentials("5", EMAIL_5, json.dumps({"mcpOAuth": MCP}))
        with pytest.raises(SwitchError, match="not an API key.*qswap add-token KEY --slot 5"):
            s.switch_to("5", json_output=True)
        assert not _settings(temp_home).exists()
        assert s.current_account_number() == "1"
        code, _out, err = run(s, "switch", "5")
        assert code == 1
        assert "#5 ci's saved credential is not an API key" in " ".join(err.split())
        _fits(err)
        assert not _settings(temp_home).exists()

    def test_a_previous_copy_with_the_key_still_heals_and_switches(self, temp_home):
        s = _fleet()
        s._write_account_credentials("5", EMAIL_5, json.dumps({"mcpOAuth": MCP}))  # key -> .prev
        assert s.switch_to("5", json_output=True)["switched"] is True
        assert _settings_obj(temp_home)["apiKeyHelper"].endswith("key-helper 5")
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5

    def test_the_engine_writes_no_line_for_a_slot_without_a_key(self, temp_home):
        h = _harness(temp_home)
        h.switcher._write_account_credentials(
            "2", "key@token.local", json.dumps({"mcpOAuth": MCP}), retain=False
        )
        assert h.tick_with_usage(EXHAUSTED) is not TickOutcome.SWITCHED
        assert not _settings(temp_home).exists()
        assert h.switcher.current_account_number() == "1"

    @staticmethod
    def _state(s, home):
        from ai_quota_swap.ledger.activations import ActivationLog

        settings = _settings(home)
        return {
            "helper": s.helper_account(),
            "current": s.current_account_number(),
            "login": s.login_account_number(),
            "settings": settings.read_text(encoding="utf-8") if settings.exists() else None,
            "key_state": s.key_state_path.exists(),
            "entered_by": s.key_entered_by(),
            "active_number": s._get_sequence_data().get("activeAccountNumber"),
            "last_activation": [
                (a.number, a.how) for a in ActivationLog.load(s.backup_dir).activations
            ][-1],
        }

    def test_a_failed_switch_away_from_a_key_moves_no_session(
        self, temp_home, block_real_keychain, run
    ):
        s = _fleet()
        _activate(s)
        (s.configs_dir / f".claude-config-7-{SAM}.json").unlink()
        before = self._state(s, temp_home)
        code, out, err = run(s, "switch", "7")
        assert code == 1
        assert "has no stored config backup" in err
        assert self._state(s, temp_home) == before

    def test_a_failed_switch_keeps_the_run_profiles_copies(self, temp_home, block_real_keychain):
        s = _fleet()
        d = _profile(s)
        _activate(s)
        assert not (d / "settings.json").is_symlink()
        (s.configs_dir / f".claude-config-7-{SAM}.json").unlink()
        before = self._state(s, temp_home)
        with pytest.raises(SwitchError, match="no stored config backup"):
            s.switch_to("7", json_output=True)
        assert not (d / "settings.json").is_symlink()
        assert self._state(s, temp_home) == before

    def test_a_failed_log_out_sessions_onto_another_key_moves_no_session(
        self, temp_home, block_real_keychain
    ):
        s = _fleet()
        _activate(s)
        (s.configs_dir / f".claude-config-6-{EMAIL_6}.json").unlink()
        before = self._state(s, temp_home)
        with pytest.raises(SwitchError):
            s.switch_to("6", json_output=True, log_out_sessions=True)
        assert self._state(s, temp_home) == before

    def test_a_failed_rotation_from_a_key_moves_no_session(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        s = _fleet()
        _activate(s)
        before = self._state(s, temp_home)

        def boom(*a, **k):
            raise SwitchError("Switch failed and was rolled back: disk full")

        monkeypatch.setattr(s, "_perform_switch", boom)
        with pytest.raises(SwitchError, match="disk full"):
            s._leave_key_to("7", emit_output=False)
        assert self._state(s, temp_home) == before

    def test_when_the_line_cannot_be_put_back_the_error_says_so(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        s = _fleet()
        _activate(s)
        (s.configs_dir / f".claude-config-7-{SAM}.json").unlink()

        def fail(*a, **k):
            raise claude_settings.SettingsError("could not be written (EIO)", kind="write")

        monkeypatch.setattr(claude_settings, "install", fail)
        with pytest.raises(SwitchError) as caught:
            s.switch_to("7", json_output=True)
        text = " ".join(str(caught.value).split())
        assert "no stored config backup" in text
        assert "#5 ci was left" in text and "running sessions are on #1 personal" in text
        assert text.endswith("To go back to the key: qswap switch 5")

    def test_key_to_key_replaces_the_line(self, temp_home, block_real_keychain, run):
        s = _fleet()
        _activate(s)
        before = _claude_state(block_real_keychain)
        code, out, _err = run(s, "switch", "6")
        assert code == 0
        assert _settings_obj(temp_home) == {"apiKeyHelper": f"{QSWAP} key-helper 6"}
        assert _claude_state(block_real_keychain) == before
        assert out.splitlines()[0] == "Switched #5 ci → #6 ops (API key)"
        assert "move from #5 ci to #6 ops within a few seconds" in " ".join(out.split())

    @pytest.mark.parametrize("blank", ["", "\n", "  \n\n"])
    def test_a_blank_settings_file_is_blank_again_after_the_key(self, temp_home, blank):
        """Claude Code reads a blank file as no settings; the round trip
        through a key (and from one key to another) leaves it as it was."""
        s = _fleet()
        _settings(temp_home).write_bytes(blank.encode("utf-8"))
        _activate(s)
        assert _settings_obj(temp_home)["apiKeyHelper"].endswith("key-helper 5")
        s.switch_to("6", json_output=True)
        s.switch_to("1", json_output=True)
        assert _settings(temp_home).read_bytes() == blank.encode("utf-8")

    def test_back_to_the_account_underneath_removes_the_line_only(
        self, temp_home, block_real_keychain, run
    ):
        s = _fleet()
        _activate(s)
        before = _claude_state(block_real_keychain)
        code, out, _err = run(s, "switch", "1")
        assert code == 0
        assert not _settings(temp_home).exists()  # qswap created it
        assert _claude_state(block_real_keychain) == before
        assert out.splitlines() == [
            "Switched #5 ci → #1 personal (maya@example.com)",
            "Running Claude Code sessions are back on #1 personal within a few seconds.",
        ]
        assert s.current_account_number() == "1"

    def test_to_another_subscription_is_the_line_then_the_login_switch(
        self, temp_home, block_real_keychain
    ):
        s = _fleet()
        _activate(s)
        result = s.switch_to("7", json_output=True)
        assert result["switched"] and result["from"]["number"] == 5
        assert result["to"]["number"] == 7
        assert not _settings(temp_home).exists()
        assert json.loads(get_global_config_path().read_text())["oauthAccount"]["emailAddress"] == SAM
        assert s.current_account_number() == "7"
        assert "within about 30 seconds" in " ".join(result["followup"])

    def test_already_on_the_key(self, temp_home, capsys):
        s = _fleet()
        _activate(s)
        payload = s.switch_to("5", json_output=True)
        assert payload["switched"] is False and payload["reason"] == "already-active"

    def test_the_users_settings_are_kept(self, temp_home):
        s = _fleet()
        original = '{\n  "model": "opus",\n  "permissions": {"allow": ["Bash"]}\n}\n'
        _settings(temp_home).write_text(original, encoding="utf-8")
        _activate(s)
        obj = _settings_obj(temp_home)
        assert obj["model"] == "opus" and obj["apiKeyHelper"].endswith("key-helper 5")
        s.switch_to("1", json_output=True)
        assert _settings(temp_home).read_text(encoding="utf-8") == original

    # Another apiKeyHelper is refused with no saved-key way (decision B6):
    # tests/test_key_switch_round2.py, TestAnotherHelper.
    @pytest.mark.parametrize("text, reason", [
        ("[]", "is not valid JSON (not a JSON object)"),
        ("{not json", "is not valid JSON"),
    ])
    def test_refused_when_the_line_cannot_be_installed(
        self, temp_home, block_real_keychain, alive, text, reason, run
    ):
        s = _fleet()
        _settings(temp_home).write_text(text, encoding="utf-8")
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        before = (_claude_state(block_real_keychain), s.sequence_file.read_bytes())
        code, out, err = run(s, "switch", "5")
        assert code == 1
        assert "Switched" not in out
        flat = " ".join(err.split())
        assert reason in flat
        assert "2 Claude Code sessions are running on the shared login" in flat
        assert err.rstrip().endswith("qswap switch 5 --log-out-sessions")
        _fits(err)
        assert (_claude_state(block_real_keychain), s.sequence_file.read_bytes()) == before
        assert _settings(temp_home).read_text(encoding="utf-8") == text
        code, out, _err = run(s, "switch", "5", "--json")
        payload = json.loads(out)
        assert payload["error"]["type"] == "KeyHelperUnavailableError"
        assert payload["error"]["sessions"] == 2
        assert reason in payload["error"]["keyHelper"]

    def test_the_saved_key_path_keeps_its_part_1_behaviour(
        self, temp_home, block_real_keychain, alive
    ):
        s = _fleet()
        _settings(temp_home).write_text("{not json", encoding="utf-8")
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        s.switch_to("5", json_output=True, log_out_sessions=True)
        item = json.loads(block_real_keychain.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct()))
        assert item == {"mcpOAuth": MCP}
        assert block_real_keychain.get_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()) == KEY_5

    def test_the_saved_key_path_takes_qswaps_line_out_first(self, temp_home, block_real_keychain):
        s = _fleet()
        _activate(s)
        s.switch_to("6", json_output=True, log_out_sessions=True)
        assert not _settings(temp_home).exists()
        assert block_real_keychain.get_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()) == KEY_6

    def test_an_account_without_a_saved_key_is_refused(self, temp_home, block_real_keychain):
        s = _fleet()
        block_real_keychain.delete_password(SECURITY_SERVICE, f"account-5-{EMAIL_5}")
        for f in s.credentials_dir.glob(".creds-5-*"):
            f.unlink()
        with pytest.raises(SwitchError, match="has no saved key"):
            _activate(s)
        assert not _settings(temp_home).exists()

    def test_the_activation_log_records_how(self, temp_home):
        from ai_quota_swap.ledger.activations import ActivationLog

        s = _fleet()
        _activate(s)
        s.switch_to("1", json_output=True)
        s.switch_to("6", json_output=True, log_out_sessions=True)
        hows = [(a.number, a.how) for a in ActivationLog.load(s.backup_dir).activations]
        assert hows[-3:] == [(5, "helper"), (1, "login"), (6, "saved-key")]


class TestRotation:
    def test_rotation_never_lands_on_a_key(self, temp_home):
        s = _fleet()
        result = s.switch(json_output=True)
        assert result["to"]["number"] == 7
        result = s.switch(json_output=True)
        assert result["to"]["number"] == 1
        assert not any("Account-5" in w or "Account-6" in w for w in result["warnings"])

    def test_with_only_keys_left_it_names_the_way(self, temp_home, capsys):
        s = _fleet()
        s.set_account_disabled("7", True)
        capsys.readouterr()
        s.switch()
        out = capsys.readouterr().out
        assert "use an API-key account by naming it: qswap switch 5" in " ".join(out.split())
        assert s.current_account_number() == "1"

    def test_rotation_on_a_key_returns_to_the_account_underneath(self, temp_home):
        s = _fleet()
        _activate(s)
        result = s.switch(json_output=True)
        assert (result["from"]["number"], result["to"]["number"]) == (5, 1)
        assert not _settings(temp_home).exists()

    def test_best_on_a_key_without_usage_goes_back_underneath(self, temp_home):
        s = _fleet()
        _activate(s)
        with patch.object(s, "_usage_by_account", return_value={}):
            result = s.switch(strategy="best", json_output=True)
        assert result["to"]["number"] == 1

    @pytest.mark.parametrize("strategy", [None, "best"])
    def test_rotation_over_a_key_saved_the_older_way_never_lands_on_it(
        self, temp_home, strategy
    ):
        """#6 through the line over #5 saved the older way: #5 is not a
        subscription underneath, so a rotation goes on to a subscription."""
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        s.switch_to("6", json_output=True)
        assert s.helper_account() == "6"
        assert s.underneath_account_number() is None
        extra = {"strategy": strategy} if strategy else {}
        with patch.object(s, "_usage_by_account", return_value={}):
            result = s.switch(json_output=True, **extra)
        assert result["switched"] is True
        assert result["to"]["number"] in (1, 7)
        assert s.account_kind_for(s.current_account_number()) != "api_key"
        assert s.helper_account() is None


class TestAccountChangesWhileOnAKey:
    def test_remove_the_active_key_takes_the_line_out_first(self, temp_home, block_real_keychain):
        s = _fleet()
        _activate(s)
        before = _claude_state(block_real_keychain)
        s.remove_account("5", assume_yes=True)
        assert not _settings(temp_home).exists()
        assert _claude_state(block_real_keychain) == before
        assert s.current_account_number() == "1"

    def test_remove_refuses_when_the_line_cannot_be_removed(self, temp_home):
        s = _fleet()
        _activate(s)
        _settings(temp_home).write_text("{broken", encoding="utf-8")
        with pytest.raises(SwitchError, match="key line cannot be removed"):
            s.remove_account("5", assume_yes=True)
        assert "5" in s._get_sequence_data()["accounts"]

    def test_removing_the_account_underneath_keeps_the_key(self, temp_home, block_real_keychain):
        s = _fleet()
        _activate(s)
        s.remove_account("1", assume_yes=True)
        assert s.helper_account() == "5"

    @pytest.mark.parametrize("op", ["swap", "move"])
    def test_swap_and_move_never_renumber_the_active_key(self, temp_home, op):
        s = _fleet()
        _activate(s)
        before = s.sequence_file.read_bytes()
        with pytest.raises(ValidationError, match="the active API key"):
            if op == "swap":
                s.swap_accounts("5", "7")
            else:
                s.move_account("5", "9")
        assert s.sequence_file.read_bytes() == before

    def test_swapping_others_is_fine(self, temp_home):
        s = _fleet()
        _activate(s)
        s.swap_accounts("6", "7")
        assert s.helper_account() == "5"

    def test_add_saves_the_login_underneath(self, temp_home, block_real_keychain):
        s = _fleet()
        _activate(s)
        with patch.object(s, "_reject_foreign_credential_capture", side_effect=lambda c, *a: c):
            s.add_account()
        stored = json.loads(s._read_account_credentials("1", MAYA))
        assert "claudeAiOauth" in stored
        assert s.helper_account() == "5"

    def test_add_into_the_slot_of_the_active_key_is_refused(self, temp_home):
        s = _fleet()
        _activate(s)
        with pytest.raises(ValidationError, match="the active API key"):
            s.add_account(slot=5)

    @pytest.mark.parametrize("op", ["swap", "move", "add"])
    def test_a_settings_file_that_cannot_be_parsed_still_guards_the_key(self, temp_home, op):
        """A trailing comma left in by hand: the line is still there, and
        key_helper.json still says which account it names."""
        s = _fleet()
        _activate(s)
        _settings(temp_home).write_text(
            f'{{"apiKeyHelper": "{QSWAP} key-helper 5",}}\n', encoding="utf-8"
        )
        before = s.sequence_file.read_bytes()
        with pytest.raises(ValidationError, match="the active API key"):
            if op == "swap":
                s.swap_accounts("5", "6")
            elif op == "move":
                s.move_account("5", "9")
            else:
                s.add_account(slot=5)
        assert s.sequence_file.read_bytes() == before

    def test_a_setup_token_over_the_active_key_is_refused(self, temp_home):
        s = _fleet()
        _activate(s)
        with pytest.raises(ValidationError, match="the active API key"):
            s.add_account_from_token(
                SETUP_TOKEN.replace("p1", "z9"), email="new@example.com", slot=5,
                assume_yes=True,
            )
        assert s._read_account_credentials("5", EMAIL_5) == KEY_5
        assert s.account_kind_for("5") == "api_key"
        assert s.helper_account() == "5"

    def test_purge_warns_about_a_line_it_cannot_check(self, temp_home, capsys):
        s = _fleet()
        _activate(s)
        _settings(temp_home).write_text("{broken", encoding="utf-8")
        assert s.purge(assume_yes=True) is True
        captured = capsys.readouterr()
        text = " ".join((captured.out + captured.err).split())
        assert "cannot be parsed, so qswap could not check it for its key line" in text

    def test_a_new_key_for_the_active_account_is_saved(self, temp_home, capsys):
        s = _fleet()
        _activate(s)
        s.add_account_from_token(KEY_6.replace("k6", "k8"), slot=5, assume_yes=True)
        assert s._read_account_credentials("5", EMAIL_5) == KEY_6.replace("k6", "k8")
        assert s.helper_account() == "5"

    def test_export_carries_the_key_and_the_login_underneath(self, temp_home, block_real_keychain):
        from ai_quota_swap.transfer import export_accounts

        s = _fleet()
        _activate(s)
        out = temp_home / "all.qswap"
        export_accounts(s, str(out))
        env = json.loads(out.read_text())
        by = {a["number"] if "number" in a else a.get("slot"): a for a in env["accounts"]}
        creds = {str(k): v["credentials"] for k, v in by.items()}
        assert creds["5"] == KEY_5
        assert "claudeAiOauth" in creds["1"]

    def test_import_of_a_new_key_for_the_active_account(self, temp_home, capsys):
        from ai_quota_swap.transfer import export_accounts, import_accounts

        s = _fleet(Platform.LINUX)
        _activate(s)
        out = temp_home / "k.qswap"
        export_accounts(s, str(out), account="5")
        env = json.loads(out.read_text())
        env["accounts"][0]["credentials"] = KEY_6.replace("k6", "k9")
        out.write_text(json.dumps(env))
        capsys.readouterr()
        import_accounts(s, str(out), force=True)
        err = " ".join(capsys.readouterr().err.split())
        assert "The saved key for #5 ci was replaced." in err
        assert "pick up the new key within 5 minutes, or at once after a request" in err
        assert s.helper_account() == "5"


# == E. run profiles ======================================================================


def _profile(s, number="7", email=SAM, *, link=True) -> Path:
    """A run profile sharing the user settings (``qswap run 7`` made it)."""
    from ai_quota_swap.session import SHARE_MANIFEST, session_dir_for

    d = session_dir_for(s.backup_dir, number, email)
    d.mkdir(parents=True, exist_ok=True)
    src = Path.home() / ".claude" / "settings.json"
    if not src.exists():
        src.write_text('{\n  "model": "opus"\n}\n', encoding="utf-8")
    dest = d / "settings.json"
    if link:
        try:
            dest.symlink_to(src)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
    else:
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (d / SHARE_MANIFEST).write_text(json.dumps({"items": ["settings.json"]}), encoding="utf-8")
    return d


class TestRunProfiles:
    def test_before_during_and_after(self, temp_home):
        from ai_quota_swap.session import SETTINGS_COPY_MARKER

        s = _fleet()
        d = _profile(s)
        _activate(s)
        dest = d / "settings.json"
        assert not dest.is_symlink()
        assert json.loads(dest.read_text(encoding="utf-8")) == {"model": "opus"}
        assert (d / SETTINGS_COPY_MARKER).exists()
        assert "apiKeyHelper" in _settings_obj(temp_home)
        s.switch_to("1", json_output=True)
        assert dest.is_symlink()
        assert not (d / SETTINGS_COPY_MARKER).exists()
        assert json.loads(dest.read_text(encoding="utf-8")) == {"model": "opus"}

    def test_a_profile_that_copies_keeps_a_copy(self, temp_home):
        from ai_quota_swap.session import SETTINGS_COPY_MARKER

        s = _fleet()
        d = _profile(s, link=False)
        _activate(s)
        assert "apiKeyHelper" not in json.loads((d / "settings.json").read_text())
        s.switch_to("1", json_output=True)
        assert not (d / SETTINGS_COPY_MARKER).exists()
        assert json.loads((d / "settings.json").read_text()) == {"model": "opus"}

    def test_a_profile_made_while_on_a_key_gets_the_copy(self, temp_home):
        from ai_quota_swap.session import SETTINGS_COPY_MARKER, SessionManager, session_dir_for

        s = _fleet()
        _settings(temp_home).write_text('{"model": "opus"}', encoding="utf-8")
        _activate(s)
        d = session_dir_for(s.backup_dir, "7", SAM)
        d.mkdir(parents=True)
        SessionManager(s)._sync_items(d, ("settings.json",), scope=("settings.json",))
        dest = d / "settings.json"
        assert not dest.is_symlink()
        assert json.loads(dest.read_text()) == {"model": "opus"}
        assert (d / SETTINGS_COPY_MARKER).exists()
        s.switch_to("1", json_output=True)
        # The next launch links it again.
        SessionManager(s)._sync_items(d, ("settings.json",), scope=("settings.json",))
        assert dest.is_symlink() or "apiKeyHelper" not in dest.read_text()

    def test_a_settings_file_with_a_byte_order_mark_is_copied_without_the_line(
        self, temp_home
    ):
        """F12: an editor saved the user settings with a byte order mark while
        the line is in. The copy a launch (or a switch) gives the profile
        still has no line."""
        from ai_quota_swap.session import SETTINGS_COPY_MARKER, SessionManager

        s = _fleet()
        d = _profile(s)
        _activate(s)
        raw = _settings(temp_home).read_bytes()
        _settings(temp_home).write_bytes("﻿".encode("utf-8") + raw)
        assert s.key_helper_state().number == "5"
        dest = d / "settings.json"
        SessionManager(s)._sync_items(d, ("settings.json",), scope=("settings.json",))
        assert not dest.is_symlink() and (d / SETTINGS_COPY_MARKER).exists()
        assert "apiKeyHelper" not in dest.read_bytes().decode("utf-8")
        assert json.loads(dest.read_bytes().decode("utf-8-sig")) == {"model": "opus"}
        dest.unlink()
        try:
            dest.symlink_to(_settings(temp_home))
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
        assert s.isolate_run_profiles() == []
        assert "apiKeyHelper" not in dest.read_bytes().decode("utf-8")

    def test_a_crash_between_the_steps_is_found_and_repaired(self, temp_home):
        s = _fleet()
        d = _profile(s)
        _activate(s)
        # The line went, the profile restore did not (a crash in between).
        claude_settings.remove(path=_settings(temp_home))
        assert s.stale_profile_copies() == [Path(os.path.realpath(d))]
        notes = " ".join(s.key_helper_notes())
        assert "qswap run profile keeps a copy" in notes and "qswap switch 1" in notes
        checks = {c.id: c for c in doctor.run_checks()}
        assert checks["claude.profile_settings"].status == "fail"
        assert checks["claude.profile_settings"].fix == "qswap switch 1"
        s.switch_to("1", json_output=True)
        assert (d / "settings.json").is_symlink()
        assert s.stale_profile_copies() == []

    def test_a_crash_before_the_line_is_repairable_too(self, temp_home, monkeypatch):
        s = _fleet()
        d = _profile(s)

        def fail(*a, **k):
            raise claude_settings.SettingsError("could not be written (EIO)", kind="write")

        monkeypatch.setattr(claude_settings, "install", fail)
        with pytest.raises(KeyHelperUnavailableError, match="could not be written"):
            _activate(s)
        assert (d / "settings.json").is_symlink()  # put back at once

    def test_a_profile_that_cannot_get_its_copy_stops_the_switch(self, temp_home, monkeypatch):
        """Fail closed: a profile left linked would follow the line onto the
        key, with nothing on any surface to say so."""
        from ai_quota_swap.session import SETTINGS_COPY_MARKER, SessionManager

        s = _fleet()
        d = _profile(s)
        other = _profile(s, "1", MAYA)
        real = SessionManager._write_settings_copy

        def copy(self, src, dest, session_dir):
            if Path(session_dir) == Path(os.path.realpath(d)):
                return False  # as when the profile cannot be written
            return real(self, src, dest, session_dir)

        monkeypatch.setattr(SessionManager, "_write_settings_copy", copy)
        with pytest.raises(KeyHelperUnavailableError) as caught:
            _activate(s)
        text = " ".join(str(caught.value).split())
        assert "a qswap run profile could not be given its own copy of the settings" in text
        assert "apiKeyHelper" not in _settings_obj(temp_home)
        assert s.helper_account() is None
        assert s.current_account_number() == "1"
        for p in (d, other):
            assert (p / "settings.json").is_symlink()
            assert not (p / SETTINGS_COPY_MARKER).exists()
        # The re-check: the refusal names the profile, on a line of its own,
        # and a fix before the override that logs sessions out.
        lines = str(caught.value).splitlines()
        shown = claude_settings.shown_path(Path(os.path.realpath(d)))
        assert f"  {shown}" in lines, lines
        assert "Make that profile writable, then: qswap switch 5" in lines
        assert lines.index("Make that profile writable, then: qswap switch 5") < lines.index(
            "To switch anyway:"
        )

    def test_isolation_that_raises_stops_the_switch(self, temp_home, monkeypatch):
        from ai_quota_swap.session import SessionManager

        s = _fleet()
        d = _profile(s)

        def boom(self):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(SessionManager, "isolate_settings", boom)
        with pytest.raises(KeyHelperUnavailableError, match="could not be given"):
            _activate(s)
        assert "apiKeyHelper" not in _settings_obj(temp_home)
        assert (d / "settings.json").is_symlink()

    def test_a_launch_that_relinks_between_the_steps_gets_the_copy(
        self, temp_home, monkeypatch
    ):
        """A qswap run launch between the two steps saw no line and put its
        link back; the pass after the line is in gives it the copy again."""
        s = _fleet()
        d = _profile(s)
        dest = d / "settings.json"
        real = claude_settings.install

        def install(number, *a, **k):
            dest.unlink()
            dest.symlink_to(Path.home() / ".claude" / "settings.json")
            return real(number, *a, **k)

        monkeypatch.setattr(claude_settings, "install", install)
        _activate(s)
        assert s.helper_account() == "5"
        assert not dest.is_symlink()
        assert "apiKeyHelper" not in json.loads(dest.read_text(encoding="utf-8"))

    def test_a_relinked_profile_that_cannot_be_copied_again_takes_the_line_back(
        self, temp_home, monkeypatch
    ):
        from ai_quota_swap.session import SessionManager

        s = _fleet()
        d = _profile(s)
        dest = d / "settings.json"
        real_install = claude_settings.install
        real_copy = SessionManager._write_settings_copy
        installed = []

        def install(number, *a, **k):
            dest.unlink()
            dest.symlink_to(Path.home() / ".claude" / "settings.json")
            installed.append(number)
            return real_install(number, *a, **k)

        def copy(self, src, dest_, session_dir):
            return False if installed else real_copy(self, src, dest_, session_dir)

        monkeypatch.setattr(claude_settings, "install", install)
        monkeypatch.setattr(SessionManager, "_write_settings_copy", copy)
        with pytest.raises(KeyHelperUnavailableError, match="could not be given"):
            _activate(s)
        assert installed == ["5"]
        assert "apiKeyHelper" not in _settings_obj(temp_home)
        assert s.helper_account() is None
        assert dest.is_symlink()

    def test_a_dangling_link_gets_a_copy_before_the_line_creates_its_target(
        self, temp_home
    ):
        """No user settings yet: the line would create the file the
        profile's link points at, so the profile gets an empty copy first."""
        s = _fleet()
        d = _profile(s)
        _settings(temp_home).unlink()
        dest = d / "settings.json"
        assert dest.is_symlink() and not dest.exists()
        _activate(s)
        assert "apiKeyHelper" in _settings_obj(temp_home)
        assert not dest.is_symlink()
        assert json.loads(dest.read_text(encoding="utf-8")) == {}
        s.switch_to("1", json_output=True)
        assert not _settings(temp_home).exists()  # qswap created it, and removed it
        assert not (dest.exists() and "apiKeyHelper" in dest.read_text(encoding="utf-8"))


# == F. qswap run on a key ================================================================


class TestRunOnAKey:
    def _mgr(self, s, monkeypatch):
        from ai_quota_swap import clis
        from ai_quota_swap.session import SessionManager

        monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/claude")
        mgr = SessionManager(s)
        seen: dict = {}

        def fake_exec(claude_bin, args, env):
            seen.update(bin=claude_bin, args=args, env=env)
            raise SystemExit(0)

        monkeypatch.setattr(mgr, "_exec", fake_exec)
        return mgr, seen

    def test_the_shared_profile_with_the_helper_in_settings(self, temp_home, monkeypatch, capsys):
        s = _fleet()
        mgr, seen = self._mgr(s, monkeypatch)
        capsys.readouterr()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        with pytest.raises(SystemExit):
            mgr.run("5", ["--resume", "abc"], share=True)
        assert seen["args"][0] == "--settings"
        assert json.loads(seen["args"][1]) == {"apiKeyHelper": f"{QSWAP} key-helper 5"}
        assert seen["args"][2:] == ["--resume", "abc"]
        assert "ANTHROPIC_API_KEY" not in seen["env"]
        out, err = capsys.readouterr()
        assert out == ""
        assert "Launching #5 ci (API key)" in err
        assert "Ignoring ANTHROPIC_API_KEY" in err
        assert not _settings(temp_home).exists()  # nothing global changed

    @pytest.mark.parametrize("extra", [["--settings", "{}"], ["--settings={}"]])
    def test_another_settings_flag_is_refused(self, temp_home, monkeypatch, extra):
        s = _fleet()
        mgr, seen = self._mgr(s, monkeypatch)
        with pytest.raises(SessionError, match="cannot take another --settings"):
            mgr.run("5", extra, share=True)
        assert not seen

    def test_a_run_profile_shell_runs_on_the_shared_profile(self, temp_home, monkeypatch):
        s = _fleet()
        mgr, seen = self._mgr(s, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(s.backup_dir / "sessions" / "7-x"))
        with pytest.raises(SystemExit):
            mgr.run("5", [], share=True)
        assert "CLAUDE_CONFIG_DIR" not in seen["env"]


# == G. the engine ========================================================================

from ai_quota_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome  # noqa: E402
from ai_quota_swap.usage_store import UsageEntry  # noqa: E402
from tests.test_autoswitch import EngineHarness, _usage  # noqa: E402


def _harness(temp_home, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, include_api_key_accounts=True, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "key@token.local")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    data = h.switcher._get_sequence_data()
    data["accounts"]["2"]["kind"] = "api_key"
    data["accounts"]["2"]["alias"] = "ci"
    h.switcher._write_json(h.switcher.sequence_file, data)
    h.switcher._write_account_credentials("2", "key@token.local", "sk-ant-api03-" + "kk" * 20)
    h.limit_key(2)  # the engine enters only a key with a budget or credit
    return h


EXHAUSTED = {"1": _usage(100), "2": "api key", "3": _usage(100)}


def _key_entry(reading: dict | None, now: float) -> UsageEntry:
    from tests.test_autoswitch import _entry_for

    return replace(_entry_for("api key", now), api_spend=reading)


class TestEngine:
    def test_moves_to_a_key_while_sessions_run(self, temp_home, alive):
        h = _harness(temp_home)
        _write_sessions(temp_home, [4101, 4102])
        alive.update({4101, 4102})
        assert h.tick_with_usage(EXHAUSTED) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"
        assert h.switcher.key_entered_by() == "engine"
        assert _settings_obj(temp_home)["apiKeyHelper"].endswith("key-helper 2")

    def test_does_not_take_the_saved_key_path_by_itself(self, temp_home, alive):
        h = _harness(temp_home)
        _settings(temp_home).write_text('{"apiKeyHelper": "/opt/vault/x"}', encoding="utf-8")
        assert h.tick_with_usage(EXHAUSTED) is TickOutcome.BLOCKED
        assert h.switcher.current_account_number() == "1"
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons[-1] == "key-helper-unavailable"
        assert "qswap doctor" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].human()

    def test_an_engine_key_is_left_once_a_subscription_has_room(self, temp_home):
        h = _harness(temp_home)
        h.tick_with_usage(EXHAUSTED)
        assert h.switcher.current_account_number() == "2"
        assert h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(100)}) is (
            TickOutcome.NO_ACTION
        )
        assert h.switcher.current_account_number() == "2"
        h.clock.advance(h.settings.cooldown_seconds)  # the minimum stay on the key
        assert h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)}) is (
            TickOutcome.SWITCHED
        )
        assert h.switcher.current_account_number() == "3"
        event = [e for e in h.events if isinstance(e, SwitchEvent)][-1]
        assert event.trigger == "key-return"
        assert event.human().endswith("a subscription has room again")
        assert not _settings(temp_home).exists()

    def test_switching_to_the_engine_key_makes_it_the_users(self, temp_home, capsys):
        h = _harness(temp_home)
        h.tick_with_usage(EXHAUSTED)
        assert h.switcher.key_entered_by() == "engine"
        capsys.readouterr()
        assert h.switcher.switch_to("2") is None
        out = " ".join(capsys.readouterr().out.split())
        assert "Already on #2 ci" in out
        assert "Auto-switch keeps it now" in out
        assert h.switcher.key_entered_by() == "user"
        assert h.tick_with_usage({"1": _usage(100), "2": "api key", "3": _usage(10)}) is (
            TickOutcome.NO_ACTION
        )
        assert h.switcher.current_account_number() == "2"
        payload = h.switcher.switch_to("2", json_output=True)
        assert payload["reason"] == "already-active"
        assert "Auto-switch" not in payload["message"]  # nothing changed this time

    def test_a_user_key_is_not_left_because_a_subscription_has_room(self, temp_home):
        h = _harness(temp_home)
        h.switcher.switch_to("2", json_output=True)
        assert h.switcher.key_entered_by() == "user"
        assert h.tick_with_usage({"1": _usage(10), "2": "api key", "3": _usage(5)}) is (
            TickOutcome.NO_ACTION
        )
        assert h.switcher.current_account_number() == "2"
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert held.reason == "active-api-key"
        assert "you chose the active API key" in held.human()
        # The re-check: the 20-minute rule (A4) is one of the ways off it.
        assert held.human().endswith(
            "auto-switch leaves it at its budget, when its credit is spent or expires, "
            "or when its spend cannot be measured for 20 minutes"
        )
        assert "only" not in display.KEY_KEPT
        assert display.KEY_KEPT == (
            "Auto-switch keeps it now, and leaves it at its budget, when its credit is "
            "spent or expires, or when its spend cannot be measured for 20 minutes."
        )
        assert "20 minutes" in held.detail

    @pytest.mark.parametrize("reading, words", [
        ({"spend": {"used": 600.0, "limit": 500.0}, "source": "ledger"},
         "monthly budget reached"),
        ({"spend": {"used": 1.0, "limit": None}, "source": "ledger",
          "credit": {"amount": 100.0, "left": 0.0, "spent": True, "expired": False}},
         "credit spent"),
        ({"spend": {"used": 1.0, "limit": None}, "source": "ledger",
          "credit": {"amount": 100.0, "left": 50.0, "spent": False, "expired": True,
                     "expires": "2026-01-31"}},
         "credit expired"),
    ])
    def test_at_its_limit_a_user_key_is_left_for_the_best_subscription(
        self, temp_home, reading, words
    ):
        h = _harness(temp_home)
        h.switcher.switch_to("2", json_output=True)
        from tests.test_autoswitch import _entry_for

        now = h.clock.now
        entries = {"1": _entry_for(_usage(40), now), "2": _key_entry(reading, now),
                   "3": _entry_for(_usage(10), now)}
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        event = [e for e in h.events if isinstance(e, SwitchEvent)][-1]
        assert words in event.human()

    def test_at_its_budget_with_no_room_it_goes_back_underneath_and_says_so(self, temp_home):
        h = _harness(temp_home)
        h.switcher.switch_to("2", json_output=True)
        from tests.test_autoswitch import _entry_for

        now = h.clock.now
        over = {"spend": {"used": 600.0, "limit": 500.0}, "source": "ledger"}
        entries = {"1": _entry_for(_usage(100), now), "2": _key_entry(over, now),
                   "3": _entry_for(_usage(100), now)}
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        switches = [e for e in h.events if isinstance(e, SwitchEvent)]
        assert len(switches) == 1
        assert switches[-1].human() == (
            "Switched #2 ci → #1 a@example.com · monthly budget reached (estimate); no "
            "subscription has room, so back to the account underneath"
        )

    def test_at_its_budget_it_leaves_for_an_unmanaged_login_underneath(self, temp_home):
        """No managed subscription has room and the login underneath is one
        qswap does not manage: the line comes out all the same, so sessions
        stop spending the key past its budget."""
        h = _harness(temp_home)
        h.make_live("x@example.com", 9)
        h.switcher.switch_to("2", json_output=True)
        assert h.switcher.helper_account() == "2"
        assert h.switcher.underneath_account_number() is None
        from tests.test_autoswitch import _entry_for

        now = h.clock.now
        over = {"spend": {"used": 600.0, "limit": 500.0}, "source": "ledger"}
        entries = {"1": _entry_for(_usage(100), now), "2": _key_entry(over, now),
                   "3": _entry_for(_usage(100), now)}
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        assert h.switcher.helper_account() is None
        assert "apiKeyHelper" not in (
            _settings_obj(temp_home) if _settings(temp_home).exists() else {}
        )
        switch = [e for e in h.events if isinstance(e, SwitchEvent)][-1]
        assert switch.human() == (
            "Switched #2 ci → x@example.com · monthly budget reached (estimate); no "
            "subscription has room, so back to the account underneath"
        )

    def test_at_its_budget_with_no_login_underneath_it_stays(self, temp_home):
        """With no login at all, removing the line would leave every session
        with nothing: the engine says so and stays."""
        h = _harness(temp_home)
        h.switcher.switch_to("2", json_output=True)
        (temp_home / ".claude.json").write_text("{}", encoding="utf-8")
        from tests.test_autoswitch import _entry_for

        now = h.clock.now
        over = {"spend": {"used": 600.0, "limit": 500.0}, "source": "ledger"}
        entries = {"1": _entry_for(_usage(100), now), "2": _key_entry(over, now),
                   "3": _entry_for(_usage(100), now)}
        assert h.tick_with_entries(entries) is TickOutcome.BLOCKED
        assert h.switcher.helper_account() == "2"
        held = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert held.reason == "stuck-on-key"  # said once per episode (the notifier)

    def test_will_not_move_to_a_key_whose_credit_is_spent(self, temp_home):
        h = _harness(temp_home)
        from tests.test_autoswitch import _entry_for

        now = h.clock.now
        spent = {"spend": {"used": 1.0, "limit": None}, "source": "ledger",
                 "credit": {"amount": 10.0, "left": 0.0, "spent": True, "expired": False}}
        entries = {"1": _entry_for(_usage(100), now), "2": _key_entry(spent, now),
                   "3": _entry_for(_usage(100), now)}
        assert h.tick_with_entries(entries) is TickOutcome.BLOCKED
        assert h.switcher.current_account_number() == "1"


def _rt(raw: str) -> str:
    return json.loads(raw)["claudeAiOauth"]["refreshToken"]


class TestLeavingForTheLoginUnderneath:
    """Leaving an engine-entered key for the account underneath only removes
    the line, so that account's refresh token, shared with the live login,
    is never spent by a freshen whose result only the backup would get."""

    def _entered(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.clock.now = time.time()
        exp = int(h.clock.now * 1000) + 7 * 60 * 1000  # inside FRESHEN_BUFFER_MS
        h.seed(1, "a@example.com", expires_at=exp)
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com", expires_at=exp)
        h.make_live("a@example.com", 1)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        h.switcher._write_account_credentials("2", "key@token.local", "sk-ant-api03-" + "kk" * 20)
        h.limit_key(2)  # the engine enters only a key with a budget or credit
        h.switcher._write_credentials(
            h.switcher._read_account_credentials("1", "a@example.com")
        )
        assert h.tick_with_usage(EXHAUSTED) is TickOutcome.SWITCHED
        assert h.switcher.key_entered_by() == "engine"
        h.clock.advance(h.settings.cooldown_seconds)  # the minimum stay on the key
        return h, exp

    @staticmethod
    def _refresher(posted, exp):
        from ai_quota_swap import oauth

        def refresh(creds, *a, **k):
            posted.append(_rt(creds))
            return oauth.RefreshOutcome(json.dumps({"claudeAiOauth": {
                "accessToken": "sk-new", "refreshToken": "rt-new",
                "expiresAt": exp + 8 * 3600 * 1000,
            }}), None)
        return refresh

    def test_the_account_underneath_is_never_freshened(self, temp_home):
        h, exp = self._entered(temp_home)
        posted: list[str] = []
        with patch("ai_quota_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresher(posted, exp)):
            usage = {"1": _usage(10), "2": "api key", "3": _usage(100)}
            assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "1"
        assert h.switcher.helper_account() is None
        assert posted == []
        assert _rt(h.switcher._read_credentials()) == "rt-1"
        assert _rt(h.switcher._read_account_credentials("1", "a@example.com")) == "rt-1"

    def test_another_subscription_is_still_freshened_before_its_login_switch(
        self, temp_home
    ):
        h, exp = self._entered(temp_home)
        posted: list[str] = []
        with patch("ai_quota_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresher(posted, exp)):
            usage = {"1": _usage(100), "2": "api key", "3": _usage(10)}
            assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "3"
        assert posted == ["rt-3"]
        assert _rt(h.switcher._read_credentials()) == "rt-new"


# == H. credit ============================================================================


NOW = 1_794_000_000.0  # 2026-11-07, a fixed clock for the arithmetic


class TestCredit:
    def test_record_list_and_remove(self, temp_home, capsys, monkeypatch):
        from tests.api_helpers import pin_platform

        pin_platform(monkeypatch, Platform.LINUX)
        s = _fleet(Platform.LINUX)
        num, _email, credit, changed = api_spend.set_credit(s, "5", "$1,187", expires="2026-12-31",
                                                            now=NOW)
        assert (num, changed) == ("5", True)
        assert credit == {"amount": 1187.0, "since": NOW, "expires": "2026-12-31"}
        record = s._get_sequence_data()["accounts"]["5"]
        assert record["credit"]["amount"] == 1187.0
        assert api_spend.set_credit(s, "5", None)[2:] == (None, True)
        assert "credit" not in s._get_sequence_data()["accounts"]["5"]

    def test_a_subscription_has_no_credit(self, temp_home):
        s = _fleet()
        with pytest.raises(api_spend.CreditError, match="subscription account"):
            api_spend.set_credit(s, "1", 50)

    @pytest.mark.parametrize("value", ["0", "-5", "abc", "1e12"])
    def test_bad_amounts(self, value):
        with pytest.raises(ValueError):
            api_spend.normalize_credit(value)

    def test_bad_dates(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            api_spend.parse_expiry("31/12/2026")

    def test_the_arithmetic_and_the_row(self):
        c = {"amount": 1187.0, "since": NOW, "expires": None}
        reading = api_spend.credit_reading(c, 36.4, "admin-api", NOW + 60)
        assert reading["left"] == pytest.approx(1150.6)
        assert api_spend.credit_text(reading) == "$1,150 of $1,187 credit left"
        full = {"spend": {"used": 36.4, "limit": None}, "source": "admin-api", "credit": reading,
                "as_of": NOW + 60}
        assert api_spend.spend_text(full, now=NOW + 60) == "API key · $1,150 of $1,187 credit left"
        assert api_spend.limit_reason(full) is None

    def test_spent(self):
        c = {"amount": 100.0, "since": NOW, "expires": None}
        reading = api_spend.credit_reading(c, 100.0, "ledger", NOW)
        assert reading["spent"] and reading["left"] == 0
        assert api_spend.credit_text(reading) == "credit spent ($0 of $100 left)"
        assert api_spend.limit_reason({"credit": reading}) == "credit-spent"

    def test_expiry_is_after_the_whole_day(self):
        from datetime import datetime, timezone

        day = datetime.fromtimestamp(NOW, tz=timezone.utc).date()
        end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() + 86_400
        assert not api_spend.credit_expired(day.isoformat(), NOW)
        assert not api_spend.credit_expired(day.isoformat(), end - 1)
        assert api_spend.credit_expired(day.isoformat(), end)
        reading = api_spend.credit_reading(
            {"amount": 5.0, "since": NOW - 86_400 * 3, "expires": day.isoformat()},
            0.0, "ledger", end,
        )
        assert api_spend.limit_reason({"credit": reading}) == "credit-expired"
        assert api_spend.credit_text(reading) == f"credit expired {day.strftime('%b')} {day.day}"

    def test_a_budget_and_a_credit_whichever_comes_first(self):
        credit = api_spend.credit_reading({"amount": 1000.0, "since": NOW, "expires": None},
                                          600.0, "ledger", NOW)
        over_budget = {"spend": {"used": 600.0, "limit": 500.0, "pct": 120.0}, "credit": credit}
        assert api_spend.limit_reason(over_budget) == "budget"
        credit_first = {"spend": {"used": 50.0, "limit": 500.0},
                        "credit": api_spend.credit_reading(
                            {"amount": 40.0, "since": NOW, "expires": None}, 50.0, "ledger", NOW)}
        assert api_spend.limit_reason(credit_first) == "credit-spent"
        text = api_spend.spend_text({**over_budget, "source": "ledger"})
        assert text == ("API key · $400 of $1,000 credit left · $600.00 of $500 this month "
                        "(120%, over budget) · estimate")

    def test_the_admin_figures_count_from_the_credits_day(self):
        from ai_quota_swap.ledger.pricing import PriceTable

        prices = PriceTable()
        model = "claude-opus-5-5"
        days = {
            "2026-10-30": [(model, None, (1_000_000, 0, 0, 0, 0))],
            "2026-11-02": [(model, None, (1_000_000, 0, 0, 0, 0))],
            "2026-11-07": [(model, None, (2_000_000, 0, 0, 0, 0))],
        }
        month, today, _unpriced, since = api_spend._price_admin(
            days, prices, "2026-11-07", month_label="2026-11-01", since_label="2026-10-30",
        )
        one = prices.lookup(model)[1].cost(1_000_000, 0, 0, 0, 0)
        assert month == pytest.approx(3 * one)
        assert today == pytest.approx(2 * one)
        assert since == pytest.approx(4 * one)

    def test_an_earlier_credit_widens_the_report(self):
        from ai_quota_swap import admin_api

        from datetime import datetime, timezone

        start = NOW - 40 * 86_400
        params = dict(admin_api.month_usage_params(["k"], NOW, start=start))
        day = datetime.fromtimestamp(start, tz=timezone.utc).date().isoformat()
        assert params["starting_at"] == f"{day}T00:00:00Z"
        assert dict(admin_api.month_usage_params(["k"], NOW))["starting_at"] == (
            "2026-11-01T00:00:00Z"
        )

    def test_the_reading_from_the_ledger(self, temp_home, monkeypatch):
        from ai_quota_swap.ledger import spend as ledger_spend

        s = _fleet(Platform.LINUX)
        api_spend.set_credit(s, "5", 100, now=NOW - 3600)
        seen = {}

        def fake(root, email, org, **kw):
            seen.update(kw)
            return ledger_spend.LedgerSpend(month=30.0, today=2.0, unpriced_tokens=0,
                                            last_scan_at=NOW, since=12.5)

        monkeypatch.setattr(ledger_spend, "account_spend", fake)
        infos = {"5": next(i for i in s._build_accounts_info() if str(i[0]) == "5")}
        reading = api_spend.readings(s, infos, now=NOW)["5"]
        assert seen["since_start"] == NOW - 3600
        assert reading["credit"]["used"] == 12.5 and reading["credit"]["left"] == 87.5
        from ai_quota_swap.json_output import api_spend_to_json

        out = api_spend_to_json(reading)
        assert out["credit"]["left"] == 87.5 and out["credit"]["source"] == "ledger"
        assert out["used"] == 30.0  # the month figure is unchanged

    def test_the_command(self, temp_home, monkeypatch, capsys):
        from tests.api_helpers import pin_platform, run_main

        pin_platform(monkeypatch, Platform.LINUX)
        _fleet(Platform.LINUX)
        capsys.readouterr()
        run_main(["credit", "5", "1187", "--expires", "2026-12-31"])
        out = capsys.readouterr().out
        assert out.splitlines()[0] == (
            "Recorded $1,187 of credit for #5 ci, usable until 2026-12-31 UTC."
        )
        _fits(out)
        run_main(["credit", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["accounts"][0]["credit"]["amount"] == 1187.0
        run_main(["credit", "5", "off"])
        assert "Removed the credit for #5 ci" in capsys.readouterr().out
        with pytest.raises(SystemExit):
            run_main(["credit", "1", "5"])
        assert "subscription account" in capsys.readouterr().err


# == I. the ledger ========================================================================


class TestLedgerRule:
    def _store(self, tmp_path):
        from ai_quota_swap.ledger.store import LedgerStore

        return LedgerStore(tmp_path / "ledger.db")

    def test_saved_key_usage_goes_to_the_key_only_for_later_sessions(self, tmp_path):
        from datetime import date, datetime

        from ai_quota_swap.ledger.activations import Activation, ActivationLog
        from ai_quota_swap.ledger.report import build_report

        base = datetime(2026, 11, 3, 10).astimezone().timestamp()
        hour = int(base // 3600)
        log = ActivationLog([
            Activation(ts=base - 7200, account="maya@example.com|", number=1, source="switch",
                       how="login"),
            Activation(ts=base, account=f"{EMAIL_5}|", number=5, source="switch",
                       how="saved-key"),
        ])
        store = self._store(tmp_path)
        toks = (100, 10, 0, 0, 0, 0, 1)
        store.add_buckets([
            ("claude", hour, f"{EMAIL_5}|", "claude-opus-5-5", "/w", "old", *toks),
            ("claude", hour, f"{EMAIL_5}|", "claude-opus-5-5", "/w", "new", *toks),
        ])
        store.note_sessions([
            ("claude", "old", None, base - 3600), ("claude", "new", None, base + 60),
        ])
        store.conn.commit()
        registry = {"accounts": {
            "1": {"email": MAYA, "organizationUuid": ""},
            "5": {"email": EMAIL_5, "organizationUuid": "", "kind": "api_key"},
        }}
        report = build_report(
            store, since=date(2026, 11, 3), until=date(2026, 11, 3), by="account",
            registries={"claude": registry}, logs={"claude": log},
        )
        by = {row.key[1]: row.totals.tokens for row in report.rows}
        assert by == {f"{EMAIL_5}|": 110, "maya@example.com|": 110}
        assert report.approximate is True

    def test_a_helper_period_is_attributed_by_time(self, tmp_path):
        from datetime import date, datetime

        from ai_quota_swap.ledger.activations import Activation, ActivationLog
        from ai_quota_swap.ledger.report import build_report, report_to_json

        base = datetime(2026, 11, 3, 10).astimezone().timestamp()
        hour = int(base // 3600)
        log = ActivationLog([
            Activation(ts=base - 7200, account="maya@example.com|", number=1, source="switch",
                       how="login"),
            Activation(ts=base, account=f"{EMAIL_5}|", number=5, source="switch", how="helper"),
        ])
        store = self._store(tmp_path)
        store.add_buckets([("claude", hour, f"{EMAIL_5}|", "claude-opus-5-5", "/w", "old",
                            100, 10, 0, 0, 0, 0, 1)])
        store.note_sessions([("claude", "old", None, base - 3600)])
        store.conn.commit()
        report = build_report(store, since=date(2026, 11, 3), until=date(2026, 11, 3),
                              by="account", logs={"claude": log})
        assert [row.key[1] for row in report.rows] == [f"{EMAIL_5}|"]
        assert report.approximate is False
        assert report_to_json(report)["attributionApproximate"] is False

    def test_the_activation_line_carries_how(self, tmp_path):
        from ai_quota_swap.ledger.activations import ActivationLog, record_activation

        record_activation(tmp_path, number=5, email=EMAIL_5, org="", how="helper")
        record_activation(tmp_path, number=1, email=MAYA, org="", how="bogus")
        lines = (tmp_path / "activations.jsonl").read_text().splitlines()
        assert json.loads(lines[0])["how"] == "helper"
        assert "how" not in json.loads(lines[1])
        assert [a.how for a in ActivationLog.load(tmp_path).activations] == ["helper", ""]


# == J. surfaces, questions, doctor and words =============================================


class TestSurfaces:
    def test_the_menu_bar_asks_only_when_the_helper_cannot_be_added(
        self, temp_home, alive
    ):
        from ai_quota_swap import menubar

        s = _fleet()
        _write_sessions(temp_home, [4101])
        alive.add(4101)
        assert _activate(s)["switched"] is True  # no question at all
        s.switch_to("1", json_output=True)
        # (Another apiKeyHelper asks nothing either, decision B6: the saved
        # key would not be used; TestAnotherHelper in test_key_switch_round2.)
        _settings(temp_home).write_text("{not json", encoding="utf-8")
        with pytest.raises(KeyHelperUnavailableError) as caught:
            _activate(s)
        assert isinstance(caught.value, SessionsRunningError)
        title, message, button = menubar.api_key_switch_question(caught.value)
        assert title == "Use the saved key instead?"
        assert button == "Switch and log out sessions"
        assert "is not valid JSON" in message
        assert "--log-out-sessions" not in message

    def test_the_menu_bar_notices_a_change_of_the_settings_file(self, temp_home):
        from ai_quota_swap.menubar_engines import ActiveChangeWatcher

        s = _fleet()
        settings = _settings(temp_home)
        watcher = ActiveChangeWatcher(
            paths={"claude": lambda: (s._get_claude_config_path(), settings)},
            read_active={"claude": s.active_email},
        )
        assert watcher.poll({"claude": MAYA}) == []
        settings.write_text(json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 5"}),
                            encoding="utf-8")
        os.utime(settings, (time.time() + 5, time.time() + 5))
        assert watcher.poll({"claude": MAYA}) == ["claude"]

    def test_the_menu_shows_a_broken_line(self, temp_home):
        from ai_quota_swap import menubar, menubar_data

        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 9"}), encoding="utf-8"
        )
        snap = s.accounts_snapshot(fetch=set())
        state = menubar.ProviderState(
            "claude", menubar_data.account_states(snap, "claude", {}, now=time.time()),
            key_notes=snap.key_notes,
        )
        rows = menubar._provider_rows(
            type("I", (), {"settings": type("S", (), {"refresh_interval": 60})(),
                           "now": time.time()})(), state,
        )
        assert rows[-1].attention and "qswap switch 1" in rows[-1].tooltip

    def test_every_new_message_fits_80_columns(self):
        long = "#12 a-rather-long-alias-name"
        texts = [
            "Error: " + display.key_helper_refusal(
                long, 12, "~/.claude/settings.json already sets another apiKeyHelper", 16),
            "Error: " + display.key_helper_refusal(
                long, 12, "~/.claude/settings.json is not valid JSON (line 3: x)", 0),
            "Error: " + display.key_helper_refusal(
                long, 12, "~/.claude/settings.json could not be written (EACCES)",
                display.SESSIONS_UNKNOWN),
            "Error: " + display.key_helper_refusal(
                long, 12, "~/.claude/settings.json already sets another apiKeyHelper", 2,
                from_key="#11 another-long-alias"),
            *display.key_switch_followup(target=long, underneath="#13 yet-another-alias"),
            *display.key_switch_followup(target=long, underneath=None,
                                         from_key="#11 another-long-alias"),
            *display.key_leave_followup(long),
            *display.key_leave_followup(long, login_switch=True),
            *display.key_helper_problem("12", "missing", "qswap switch 1"),
            *display.key_helper_problem("12", "not-api-key", "qswap switch 1"),
            *display.stale_copies_line(3, "qswap switch 1"),
            key_helper.HELP,
            *display.key_helper_moved("12", "qswap switch 12"),
        ]
        from ai_quota_swap import api_cli

        texts.append("\n".join(display.wrap(api_cli.NOT_ANTHROPICS)))
        for text in texts:
            _fits(text)

    def test_the_connectors_sentence_is_said_on_every_move_onto_a_key(self):
        for lines in (
            display.key_switch_followup(target="#5 ci", underneath="#1 personal"),
            display.key_switch_followup(target="#6 ops", underneath=None, from_key="#5 ci"),
        ):
            assert display.KEY_CONNECTORS in " ".join(lines)


class TestMovedQswap:
    """A reinstall moved qswap: the line still reads as qswap's, but the
    command it runs is gone, and every session on the key would fail."""

    @pytest.fixture(autouse=True)
    def _real_check(self, monkeypatch):
        from ai_quota_swap import claude_settings as cs

        monkeypatch.setattr(cs, "command_missing", TestMovedQswap._missing)

    @staticmethod
    def _missing(command):
        return "/gone/" in (command or "")

    def test_reported_everywhere_and_repaired_by_a_switch(self, temp_home):
        s = _fleet()
        _settings(temp_home).write_text(
            json.dumps({"apiKeyHelper": "/gone/bin/qswap key-helper 5"}), encoding="utf-8"
        )
        assert s.current_account_number() == "5"
        notes = " ".join(s.key_helper_notes())
        assert "runs a qswap that is no longer there" in notes
        assert "qswap switch 5" in notes
        checks = {c.id: c for c in doctor.run_checks()}
        assert checks["claude.key_helper"].status == "fail"
        assert checks["claude.key_helper"].fix == "qswap switch 5"
        s.switch_to("5", json_output=True)
        assert _settings_obj(temp_home)["apiKeyHelper"] == f"{QSWAP} key-helper 5"
        assert s.key_helper_notes() == []

    def test_the_real_check(self, tmp_path):
        command_missing = _REAL_COMMAND_MISSING
        exe = tmp_path / "qswap"
        exe.write_text("", encoding="utf-8")
        assert command_missing(f"{exe} key-helper 5") is False
        assert command_missing(f"{tmp_path / 'nope' / 'qswap'} key-helper 5") is True
        assert command_missing("qswap key-helper 5") is False  # found on PATH


@POSIX_ONLY
class TestTheLineAnswers:
    """Decision B2 (F18): doctor runs the line's own command with --check,
    as Claude Code runs it (through the shell), and reports a line that
    cannot answer: a bare name, a path that is not an executable program,
    an interpreter that cannot import the package, a bare -m, a failure or
    no answer in time. Only a line parse_line reads as qswap's is run, and
    nothing it prints is shown but the helper's own one-line message."""

    @pytest.fixture(autouse=True)
    def _real(self, monkeypatch):
        monkeypatch.setattr(claude_settings, "command_missing", _REAL_COMMAND_MISSING)
        monkeypatch.setattr(claude_settings, "check_command", _REAL_CHECK_COMMAND)
        monkeypatch.setattr(claude_settings, "_run_check", _REAL_RUN_CHECK)

    def _program(self, tmp_path, body: str, name: str = "qswap", mode: int = 0o755) -> Path:
        exe = tmp_path / "bin" / name
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        exe.chmod(mode)
        return exe

    def _line(self, home: Path, command: str) -> None:
        _settings(home).write_text(json.dumps({"apiKeyHelper": command}), encoding="utf-8")

    def _check(self):
        return {c.id: c for c in doctor.run_checks()}["claude.key_helper"]

    def test_a_line_that_answers_passes_and_names_its_command(self, temp_home, tmp_path):
        # $1 key-helper, $2 the number, $3 --check: only that answers.
        exe = self._program(tmp_path, 'test "$3" = --check && exit 0\nexit 9\n')
        s = _fleet()
        self._line(temp_home, f"{exe} key-helper 5")
        check = self._check()
        assert check.status == "pass"
        assert check.summary.endswith(f", which runs {exe}")
        assert claude_settings.line_answers(s.key_helper_state()) is None

    def test_a_failure_is_said_in_the_helpers_own_words(self, temp_home, tmp_path):
        exe = self._program(tmp_path, (
            'echo "qswap key-helper: #5 has no saved key; save one: '
            'qswap add-token KEY --slot 5" >&2\nexit 1\n'
        ))
        s = _fleet()
        self._line(temp_home, f"{exe} key-helper 5")
        check = self._check()
        assert check.status == "fail"
        assert "but its line fails (#5 has no saved key; save one:" in check.summary
        assert check.fix == "qswap add-token KEY --slot 5"  # the helper's own next step
        assert "running ones keep the key they have" in check.summary
        assert claude_settings.line_answers(s.key_helper_state()).startswith("fails (#5")

    def test_a_failure_without_a_command_is_repaired_by_the_switch(self, temp_home, tmp_path):
        exe = self._program(
            tmp_path, 'echo "qswap key-helper: qswap manages no accounts on this machine" >&2\n'
                      "exit 1\n",
        )
        _fleet()
        self._line(temp_home, f"{exe} key-helper 5")
        check = self._check()
        assert "fails (qswap manages no accounts on this machine)" in check.summary
        assert check.fix == "qswap switch 5"

    def test_what_the_program_prints_is_never_shown(self, temp_home, tmp_path):
        exe = self._program(tmp_path, f'echo {KEY_5}\necho "{KEY_6}" >&2\nexit 2\n')
        _fleet()
        self._line(temp_home, f"{exe} key-helper 5")
        check = self._check()
        assert check.status == "fail" and "fails (exit 2)" in check.summary
        for text in (check.summary, check.hint or "", json.dumps(doctor.to_json(
            doctor.run_checks(), online=False))):
            assert KEY_5 not in text and KEY_6 not in text

    @pytest.mark.parametrize("case, words", [
        ("bare-name", "runs qswap by name only"),
        ("not-executable", "is not executable"),
        ("directory", "is not a program"),
    ])
    def test_a_line_that_cannot_run_is_reported(self, temp_home, tmp_path, monkeypatch,
                                                 case, words):
        ran: list[str] = []
        monkeypatch.setattr(claude_settings, "_run_check",
                            lambda command, **kw: ran.append(command) or (0, ""))
        if case == "bare-name":
            command = "qswap key-helper 5"
        elif case == "not-executable":
            command = f"{self._program(tmp_path, 'exit 0\n', mode=0o644)} key-helper 5"
        else:
            (tmp_path / "qswap").mkdir()
            command = f"{tmp_path / 'qswap'} key-helper 5"
        s = _fleet()
        self._line(temp_home, command)
        check = self._check()
        assert check.status == "fail" and check.fix == "qswap switch 5"
        assert words in check.summary
        assert words in claude_settings.line_answers(s.key_helper_state())
        assert ran == []

    def test_an_interpreter_that_cannot_import_the_package(self, temp_home, tmp_path):
        python = self._program(
            tmp_path, 'echo "python3: No module named ai_quota_swap" >&2\nexit 1\n',
            name="python3",
        )
        _fleet()
        self._line(temp_home, f"{python} -P -m ai_quota_swap key-helper 5")
        check = self._check()
        assert check.status == "fail" and "its line fails (exit 1)" in check.summary

    def test_a_bare_dash_m_line_is_reported_without_running_it(
        self, temp_home, monkeypatch
    ):
        ran: list[str] = []
        monkeypatch.setattr(claude_settings, "_run_check",
                            lambda command, **kw: ran.append(command) or (0, ""))
        _fleet()
        self._line(temp_home, f"{sys.executable} -m ai_quota_swap key-helper 5")
        check = self._check()
        assert check.status == "fail" and "Python with -m" in check.summary
        assert ran == []

    def test_a_line_that_does_not_answer_in_time(self, tmp_path):
        exe = self._program(tmp_path, "sleep 30\n")
        started = time.monotonic()
        reason = claude_settings.check_command(f"{exe} key-helper 5", timeout=0.3)
        assert reason == "did not answer within 0.3 seconds"
        assert time.monotonic() - started < 10  # the program and its children are gone

    def test_a_check_that_could_not_be_made_warns(self, temp_home, tmp_path):
        from dataclasses import replace as with_probe

        def boom(command):
            raise OSError("no shell")

        _fleet()
        self._line(temp_home, f"{self._program(tmp_path, 'exit 0\n')} key-helper 5")
        probes = with_probe(doctor.default_probes(), key_line_answers=boom)
        check = {c.id: c for c in doctor.run_checks(probes)}["claude.key_helper"]
        assert check.status == "warn" and "could not check" in check.summary
        assert check.fix == "qswap key-helper 5 --check"

    def test_only_qswaps_own_line_is_run(self, temp_home, monkeypatch):
        ran: list[str] = []
        monkeypatch.setattr(claude_settings, "_run_check",
                            lambda command, **kw: ran.append(command) or (0, ""))
        _fleet()
        self._line(temp_home, "/opt/vault/print-key")
        checks = {c.id: c for c in doctor.run_checks()}
        assert "claude.key_helper" not in checks
        assert ran == []
        state = claude_settings.read_state(_settings(temp_home))
        assert claude_settings.line_answers(state) == "is not a key line of qswap's"

    def test_this_installs_own_line_answers(self, temp_home, tmp_path, monkeypatch):
        """End to end with this install's console script: the line qswap
        writes, run the way doctor runs it. It names the data directory, so
        the child reads only the key file there (never a Keychain)."""
        from ai_quota_swap import installed

        if not installed.own_scripts():
            pytest.skip("this environment has no qswap console script")
        monkeypatch.setattr(claude_settings, "qswap_executable", _REAL_EXECUTABLE)
        root = tmp_path / "data"
        _seed_data(root, {"5": {"email": EMAIL_5, "kind": "api_key"}},
                   {f".creds-5-{EMAIL_5}.enc": KEY_5})
        monkeypatch.setattr(key_helper, "pinned_data_dir", lambda: root)
        assert claude_settings.check_command(claude_settings.helper_command(5)) is None
        reason = claude_settings.check_command(claude_settings.helper_command(9))
        assert reason == "fails (#9 does not exist; qswap list shows the accounts)"


class TestProblemEverywhere:
    def _broken(self, home):
        _settings(home).write_text(
            json.dumps({"apiKeyHelper": f"{QSWAP} key-helper 9"}), encoding="utf-8"
        )

    def test_list_and_status_say_it(self, temp_home, capsys):
        s = _fleet()
        self._broken(temp_home)
        capsys.readouterr()
        s.list_accounts()
        out = " ".join(capsys.readouterr().out.split())
        assert "make #9 the active API key, but #9 does not exist" in out
        assert "Remove the line: qswap switch 1" in out
        status = " ".join(" ".join(s.status_lines()).split())
        assert "make #9 the active API key" in status

    def test_json_says_it(self, temp_home):
        s = _fleet()
        self._broken(temp_home)
        expected = {"number": "9", "why": "missing", "fix": "qswap switch 1"}
        assert s.list_accounts(json_output=True)["keyHelperProblem"] == expected
        assert s.status(json_output=True)["keyHelperProblem"] == expected

    def test_the_snapshot_carries_it(self, temp_home):
        s = _fleet()
        self._broken(temp_home)
        snap = s.accounts_snapshot(fetch=set())
        assert snap.active_number == "1"
        assert "qswap switch 1" in " ".join(snap.key_notes)


class TestRemovingAKeyInUse:
    def test_remove_names_the_run_sessions_on_the_key(self, temp_home, capsys, monkeypatch):
        s = _fleet()
        s.record_key_run("5", 4242)
        s.record_key_run("6", 4343)
        monkeypatch.setattr(
            "ai_quota_swap.process_detection.is_pid_alive", lambda pid: pid in (4242, 4343)
        )
        assert s.key_run_pids("5") == [4242]
        capsys.readouterr()
        s.remove_account("5", assume_yes=True)
        out = " ".join(capsys.readouterr().out.split())
        assert "1 session started with qswap run 5 (pid 4242) uses this key" in out
        assert "keeps using it after it is removed" in out

    def test_run_records_its_pid(self, temp_home, monkeypatch):
        from ai_quota_swap import clis
        from ai_quota_swap.session import SessionManager

        s = _fleet()
        monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/claude")
        mgr = SessionManager(s)
        monkeypatch.setattr(mgr, "_exec", lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)))
        with pytest.raises(SystemExit):
            mgr.run("5", [], share=True)
        monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive", lambda pid: True)
        assert s.key_run_pids("5") == [os.getpid()]


class TestExactlyTheseFiles:
    """Each direction of a switch writes exactly the files it should: outside
    qswap's data directory, only Claude Code's settings file (and, for the
    login switch, the login itself)."""

    @staticmethod
    def _outside(home: Path, data: Path) -> dict[str, bytes]:
        out = {}
        for path in home.rglob("*"):
            if path.is_file() and data not in path.parents:
                out[str(path.relative_to(home))] = path.read_bytes()
        return out

    def _changed(self, before: dict, after: dict) -> set[str]:
        return {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}

    def test_each_direction(self, temp_home, block_real_keychain):
        s = _fleet(Platform.MACOS)
        settings = str(Path(".claude") / "settings.json")
        steps = [
            ("5", {settings}, False),   # subscription → key
            ("6", {settings}, False),   # key → key
            ("1", {settings}, False),   # key → the account underneath
            ("5", {settings}, False),
            ("7", {settings, ".claude.json"}, True),  # key → another subscription
        ]
        for target, expected, login_changes in steps:
            before = self._outside(temp_home, s.backup_dir)
            items = dict(block_real_keychain.data)
            s.switch_to(target, json_output=True)
            assert self._changed(before, self._outside(temp_home, s.backup_dir)) == expected, target
            changed_items = {k for k in items.keys() | block_real_keychain.data.keys()
                             if items.get(k) != block_real_keychain.data.get(k)}
            login_items = {k for k in changed_items if k[0] != SECURITY_SERVICE}
            if login_changes:
                assert login_items == {(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())}, target
            else:
                assert login_items == set(), target

    def test_from_a_saved_key_nothing_is_underneath(self, temp_home, block_real_keychain):
        s = _fleet()
        s.switch_to("5", json_output=True, log_out_sessions=True)
        result = s.switch_to("6", json_output=True)
        assert result["how"] == "helper"
        assert s.active_view() == ("6", None)
        assert "underneath" not in " ".join(result["followup"])
