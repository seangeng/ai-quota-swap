"""``qswap codex auto``, ``qswap codex reset``, ``qswap codex login`` and the
Codex observer in ``qswap usage``, end to end through ``cli.main`` (M2b).

Synthetic logins, a temp ``CODEX_HOME``, a faked usage fetch, a faked consume
call and a faked ``codex login`` runner: no request leaves the process and the
real ``codex`` never runs.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_quota_swap import cli, paths
from ai_quota_swap.codex import processes as codex_processes
from ai_quota_swap.codex import usage as codex_usage
from ai_quota_swap.codex.processes import LoginRun
from ai_quota_swap.codex.switcher import LOGIN_HOME_PREFIX, CodexSwitcher
from ai_quota_swap.codex.usage import UsageOutcome, credits_status, normalize_usage
from ai_quota_swap.ledger.activations import ActivationLog
from ai_quota_swap.resets import ClaimResult
from ai_quota_swap.settings import set_setting
from tests.codex_helpers import (
    DAY,
    LOGIN_A,
    LOGIN_B,
    LOGIN_C,
    FakeFetch,
    make_login,
    usage_payload,
    window,
    write_live,
)

#: The real runner, taken before the suite's autouse fake replaces it.
REAL_RUN_CODEX_LOGIN = codex_processes.run_codex_login

CREDIT_ID = "rlrc_01JC9ZK3M2Q8V7X4T6N5B0WYHD"


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    monkeypatch.setattr("ai_quota_swap.update_check.check_for_update", lambda v: None)


def _real_path_lookup(monkeypatch) -> None:
    """These tests put a stand-in ``codex`` on the real PATH (or empty it), so
    they need the real lookup back, not conftest's fixed answer."""
    import shutil

    from ai_quota_swap import clis

    monkeypatch.setattr(clis, "find_cli", shutil.which)


def run(argv: list[str], capsys, *, answer: str | None = None, tty: bool = False) -> tuple[int, str, str]:
    with patch.object(sys, "argv", ["qswap", *argv]), \
         patch.object(sys.stdin, "isatty", return_value=tty), \
         patch("os.geteuid", return_value=1000, create=True):
        try:
            if answer is None:
                cli.main()
            else:
                with patch("builtins.input", return_value=answer), \
                     patch("ai_quota_swap.prompts.interactive", return_value=tty):
                    cli.main()
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    out, err = capsys.readouterr()
    return code, out, err


def _reading(weekly: float, *, credit: bool = False, summary: bool = False) -> dict:
    """A weekly-only reading; ``credit``: one available credit; ``summary``:
    the response carries the credit summary even with none left."""
    now = time.time()
    count = 1 if credit else (0 if summary else None)
    usage = normalize_usage(usage_payload(window(604_800, weekly, 3 * DAY), credits=count))
    if credit:
        expires = datetime.fromtimestamp(now + 30 * DAY, tz=timezone.utc).isoformat()
        usage["resets"] = credits_status({"credits": [{
            "id": CREDIT_ID, "status": "available", "expires_at": expires, "title": "Full reset",
        }]}, usage)
    return usage


@pytest.fixture
def env(temp_home: Path, tmp_path: Path, monkeypatch) -> SimpleNamespace:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fetch = FakeFetch()
    monkeypatch.setattr(codex_usage, "fetch_usage", fetch)
    ns = SimpleNamespace(home=temp_home, codex_home=codex_home.resolve(), fetch=fetch,
                         consumed=[], answer=ClaimResult("reset"), root=paths.get_backup_root())

    def consume(access_token, account_id, credit_id, request_id, **_kw):
        ns.consumed.append((account_id, credit_id, request_id))
        if isinstance(ns.answer, BaseException):
            raise ns.answer
        if ns.answer.ok:
            fetch.answers[account_id] = UsageOutcome(usage=_reading(0, summary=True))
        return ns.answer

    monkeypatch.setattr(codex_usage, "consume_reset_credit", consume)
    return ns


@pytest.fixture
def two(env, capsys):
    """B is account 1; A is account 2 and live, at its limit with one credit."""
    write_live(env.codex_home, make_login(**LOGIN_B))
    assert run(["codex", "add"], capsys)[0] == 0
    env.live = make_login(**LOGIN_A)
    write_live(env.codex_home, env.live)
    assert run(["codex", "add"], capsys)[0] == 0
    env.fetch.answers["acct-a"] = UsageOutcome(usage=_reading(100, credit=True))
    env.fetch.answers["acct-b"] = UsageOutcome(usage=_reading(10))
    return env


def _state(env) -> dict:
    path = env.root / "codex" / "autoswitch_state.json"
    return json.loads(path.read_text()) if path.exists() else {}


# -- qswap codex auto ------------------------------------------------------------------


class _CapturingEngine:
    instances: list = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False, state_path=None, clock=None):
        self.switcher, self.settings, self.dry_run = switcher, settings, dry_run
        type(self).instances.append(self)

    def tick(self):
        from ai_quota_swap.autoswitch import TickOutcome

        return TickOutcome.NO_ACTION


class TestCodexAuto:
    def _settings(self, argv, capsys):
        _CapturingEngine.instances = []
        with patch("ai_quota_swap.autoswitch.AutoSwitchEngine", _CapturingEngine):
            code, _, err = run(["codex", "auto", "--once", *argv], capsys)
        assert code == 2, err
        engine = _CapturingEngine.instances[-1]
        assert isinstance(engine.switcher, CodexSwitcher)
        return engine.settings

    def test_codex_threshold_is_the_default_line(self, two, capsys):
        assert self._settings([], capsys).threshold == 99.0

    def test_settings_come_from_the_shared_root(self, two, capsys):
        set_setting(two.root, "codex.threshold", "95")
        set_setting(two.root, "autoswitch.threshold", "70")
        set_setting(two.root, "autoswitch.cooldownSeconds", "42")
        # Nothing is read from beside Codex's own data.
        (two.root / "codex" / "settings.json").write_text(json.dumps(
            {"codex": {"threshold": 60}, "autoswitch": {"cooldownSeconds": 7}}))
        settings = self._settings([], capsys)
        assert (settings.threshold, settings.cooldown_seconds) == (95.0, 42.0)
        assert self._settings(["--threshold", "80"], capsys).threshold == 80.0

    def test_claude_auto_keeps_autoswitch_threshold(self, two, capsys):
        set_setting(two.root, "codex.threshold", "95")
        _CapturingEngine.instances = []
        with patch("ai_quota_swap.autoswitch.AutoSwitchEngine", _CapturingEngine):
            run(["auto", "--once"], capsys)
        assert _CapturingEngine.instances[-1].settings.threshold == 90.0

    def test_help(self, env, capsys):
        code, out, _ = run(["codex", "auto", "--help"], capsys)
        assert code == 0
        assert out.startswith("usage: qswap codex auto")
        assert "codex.threshold" in out and "--include-api-key-accounts" not in out

    def test_one_tick_end_to_end_in_json(self, two, capsys):
        # A is out and #1 has room: last-resort keeps the credit, the engine switches.
        code, out, _ = run(["codex", "auto", "--once", "--json"], capsys)
        assert code == 0
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        assert [e["event"] for e in events] == ["poll", "switch"]
        assert events[0]["windowsPct"]["2"] == {"7d": 100.0}
        assert events[1]["trigger"] == "at-limit"
        assert events[1]["to"] == {"number": 1, "email": "b@example.com"}
        assert two.consumed == []
        assert run(["codex", "status", "--json"], capsys)[1].count('"number": 1') == 1

    def test_one_tick_spends_a_credit_with_nowhere_to_go(self, two, capsys):
        two.fetch.answers["acct-b"] = UsageOutcome(usage=_reading(100))
        code, out, _ = run(["codex", "auto", "--once"], capsys)
        assert code == 2
        assert "Used a weekly reset on #2 a@example.com · 7d reached 100%, no account" in out
        assert "last-resort" not in out  # the reason in words
        assert [c[:2] for c in two.consumed] == [("acct-a", CREDIT_ID)]
        assert codex_usage.is_uuid4(two.consumed[0][2])
        assert _state(two)["resetClaims"]["2"]["auto"] is True
        assert not (two.root / "autoswitch_state.json").exists()  # Claude's state untouched

    def test_loop_banner_names_codex(self, two, capsys, monkeypatch):
        from ai_quota_swap.autoswitch import AutoSwitchEngine

        monkeypatch.setattr(AutoSwitchEngine, "run_loop", lambda self: 0)
        code, out, _ = run(["codex", "auto", "--dry-run"], capsys)
        assert code == 0
        assert "Codex auto-switch dry run: would switch at 99% · every 60s" in out


# -- qswap codex reset -------------------------------------------------------------------


class TestCodexReset:
    def test_list(self, two, capsys):
        code, out, _ = run(["codex", "reset"], capsys)
        assert code == 0
        assert out.startswith("Reset credits\nAuto: last-resort")
        assert "#2 a@example.com · active" in out
        assert "1 reset credit · clears all weekly usage" in out
        assert "usable now · use by" in out
        assert f"id {CREDIT_ID}" in out
        assert "#1 b@example.com\n     └ no reset credits reported" in out
        # #1 has room, so last-resort would not spend now.
        assert "Auto would not use a reset credit right now." in out
        assert "Spent automatically in the last 24h: 0 of 3." in out
        assert all(len(line) <= 80 for line in out.splitlines())

    def test_list_json(self, two, capsys):
        code, out, _ = run(["codex", "reset", "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["auto"]["mode"] == "last-resort" and payload["plan"] is None
        by_num = {a["number"]: a for a in payload["accounts"]}
        assert by_num[2]["resets"]["grants"][0]["id"] == CREDIT_ID
        assert by_num[2]["resets"]["resetsLeft"] == 1

    def test_spend_with_yes(self, two, capsys):
        code, out, err = run(["codex", "reset", "2", "--yes"], capsys)
        assert code == 0, err
        assert "Used a reset credit on #2 a@example.com: 7d reset" in out
        assert "· 0 credits left" in out
        ((account_id, credit_id, request_id),) = two.consumed
        assert (account_id, credit_id) == ("acct-a", CREDIT_ID)
        assert codex_usage.is_uuid4(request_id)
        state = _state(two)
        assert state["resetClaims"]["2"] | {"at": 0} == {
            "at": 0, "grantId": CREDIT_ID, "requestId": request_id, "reason": "manual",
            "auto": False, "result": "reset", "settled": True, "detail": None,
        }
        assert state["resetLog"][-1]["result"] == "reset"
        assert (two.codex_home / "auth.json").read_bytes() == two.live

    def test_spend_json(self, two, capsys):
        code, out, _ = run(["codex", "reset", "a@example.com", "--yes", "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["account"] == {"number": 2, "email": "a@example.com"}
        assert payload["grantId"] == CREDIT_ID and payload["result"]["result"] == "reset"

    def test_asks_first_and_can_be_kept(self, two, capsys):
        code, out, _ = run(["codex", "reset", "2"], capsys, answer="n", tty=True)
        assert code == 0
        assert "#2 a@example.com · reset credit" in out
        assert f"id {CREDIT_ID}" in out
        assert "Resets 7d (now 100%) to 0%" in out
        assert "Kept; nothing was spent." in out
        assert two.consumed == []

    def test_refuses_a_non_tty_without_yes(self, two, capsys):
        code, _, err = run(["codex", "reset", "2"], capsys)
        assert code == 1
        assert err.strip() == (
            "Error: qswap codex reset needs --yes when there is no terminal to ask on"
        )
        assert two.consumed == []

    def test_ctrl_c_mid_request_keeps_the_pending_record(self, two, capsys):
        two.answer = KeyboardInterrupt()
        code, out, _ = run(["codex", "reset", "2", "--yes"], capsys)
        assert code == 130
        assert ("Interrupted mid-claim; the reset credit may have been used. "
                "qswap codex reset shows where things stand.") in out
        pending = _state(two)["resetClaims"]["2"]
        assert pending["result"] == "pending" and pending["settled"] is False
        two.answer = ClaimResult("reset")
        run(["codex", "reset", "2", "--yes"], capsys)
        assert two.consumed[1][2] == two.consumed[0][2] == pending["requestId"]

    def test_a_failed_spend(self, two, capsys):
        # No decided answer: the credit may have been spent, so it is never
        # reported as not used.
        two.answer = ClaimResult("error", detail="network")
        code, _, err = run(["codex", "reset", "2", "--yes"], capsys)
        assert code == 1
        err = " ".join(err.split())
        assert "The reset credit may have been used: error (network). qswap codex reset shows where things stand." in err
        assert "not used" not in err

    def test_a_refused_spend_says_not_used(self, two, capsys):
        two.answer = ClaimResult("ineligible", reason="nothing_to_reset")
        code, _, err = run(["codex", "reset", "2", "--yes"], capsys)
        assert code == 1
        assert "Credit not used: ineligible (nothing to reset)" in err

    def test_no_credit_left_is_a_decided_nothing_spent(self, two, capsys):
        two.answer = ClaimResult("unavailable", reason="no_credit")
        code, _, err = run(["codex", "reset", "2", "--yes"], capsys)
        assert code == 1
        err = " ".join(err.split())
        assert "Credit not used: no reset credit left; nothing was spent" in err
        assert "may have been used" not in err

    def test_a_retry_answered_already_redeemed_was_used(self, two, capsys):
        # The first attempt timed out; the retry reuses its request id, and
        # the server's "already redeemed" means the first one went through.
        two.answer = ClaimResult("error", detail="network")
        code, _, err = run(["codex", "reset", "2", "--yes"], capsys)
        assert code == 1 and "may have been used" in " ".join(err.split())
        two.answer = ClaimResult("already_used", reason="already_redeemed")
        code, out, err = run(["codex", "reset", "2", "--yes"], capsys)
        assert two.consumed[0][2] == two.consumed[1][2]  # the same request id
        assert code == 0
        assert " ".join(out.split()) == (
            "A reset credit on #2 a@example.com was already used: an earlier attempt went through. "
            "Run qswap codex reset to see where things stand."
        )
        assert "not used" not in out + err
        assert _state(two)["resetLog"][-1]["result"] == "already_used"  # the brake counts it

    def test_ctrl_c_while_printing_a_spent_credit_says_it_was_used(self, two, capsys, monkeypatch):
        from ai_quota_swap.codex.switcher import CodexSwitcher

        real = CodexSwitcher.usage_entries_by_account

        def interrupt_after_the_spend(self, *a, **kw):
            if two.consumed:
                raise KeyboardInterrupt()
            return real(self, *a, **kw)

        monkeypatch.setattr(CodexSwitcher, "usage_entries_by_account", interrupt_after_the_spend)
        code, out, _err = run(["codex", "reset", "2", "--yes"], capsys)
        assert len(two.consumed) == 1 and _state(two)["resetClaims"]["2"]["result"] == "reset"
        assert code == 0
        assert "Used a reset credit on #2 a@example.com." in out and "nothing was spent" not in out

    def test_an_error_while_printing_a_spent_credit_says_it_was_used(self, two, capsys, monkeypatch):
        from ai_quota_swap.codex.switcher import CodexSwitcher
        from ai_quota_swap.exceptions import ConfigError

        real = CodexSwitcher.usage_entries_by_account

        def fail_after_the_spend(self, *a, **kw):
            if two.consumed:
                raise ConfigError("sequence.json could not be read")
            return real(self, *a, **kw)

        monkeypatch.setattr(CodexSwitcher, "usage_entries_by_account", fail_after_the_spend)
        code, out, err = run(["codex", "reset", "2", "--yes"], capsys)
        assert len(two.consumed) == 1
        assert code == 0
        assert "Used a reset credit on #2 a@example.com." in out and "sequence.json could not be read" in err

    def test_an_unknown_credit(self, two, capsys):
        code, _, err = run(["codex", "reset", "2", "--grant", "nope", "--yes"], capsys)
        assert code == 1
        assert "has no reset credit 'nope' (qswap codex reset lists them)" in " ".join(err.split())

    def test_an_account_without_credits(self, two, capsys):
        code, _, err = run(["codex", "reset", "1", "--yes"], capsys)
        assert code == 1
        assert "#1 b@example.com reports no reset credits; nothing to spend" in err

    def test_help(self, env, capsys):
        code, out, _ = run(["codex", "reset", "--help"], capsys)
        assert code == 0 and out.startswith("usage: qswap codex reset")
        assert "List Codex reset credits" in out


# -- qswap codex login -------------------------------------------------------------------


class _Runner:
    """Stands in for ``codex login``: writes the given record (or nothing)
    into the home it is handed, and records how it was called."""

    def __init__(self, raw: bytes | None, returncode: int | None = 0, detail: str = "", raises=None):
        self.raw, self.returncode, self.detail, self.raises = raw, returncode, detail, raises
        self.calls: list[tuple[Path, list[str], int, str | None]] = []

    def __call__(self, home: Path, args: list[str]) -> LoginRun:
        self.calls.append((home, list(args), stat.S_IMODE(home.stat().st_mode), os.environ.get("CODEX_HOME")))
        assert home.is_dir() and not any(home.iterdir()), "the login home must start empty"
        if self.raw is not None:
            (home / "auth.json").write_bytes(self.raw)
        if self.raises is not None:
            raise self.raises
        return LoginRun(self.returncode, self.detail)


@pytest.fixture
def one(env, capsys):
    """A is account 1 and live."""
    env.live = make_login(**LOGIN_A)
    write_live(env.codex_home, env.live)
    assert run(["codex", "add"], capsys)[0] == 0
    env.before = sorted(p.name for p in env.codex_home.iterdir())
    return env


def _login(env, monkeypatch, capsys, runner, *args):
    monkeypatch.setattr(codex_processes, "run_codex_login", runner)
    return run(["codex", "login", *args], capsys)


def _registry(env) -> dict:
    return json.loads((env.root / "codex" / "sequence.json").read_text())


def _login_homes(env) -> list[str]:
    return sorted(p.name for p in (env.root / "codex").iterdir() if p.name.startswith(LOGIN_HOME_PREFIX))


class TestCodexLogin:
    def test_logs_a_further_account_in_and_saves_it(self, one, monkeypatch, capsys):
        new = make_login(**LOGIN_B)
        runner = _Runner(new)
        code, out, err = _login(one, monkeypatch, capsys, runner, "--alias", "work", "--", "--device-auth")
        assert code == 0, err
        assert "Added #2 work (b@example.com)" in out
        ((home, args, mode, own_codex_home),) = runner.calls
        assert args == ["--device-auth"]
        assert home.parent == one.root / "codex" and home.name.startswith(LOGIN_HOME_PREFIX)
        if sys.platform != "win32":
            assert mode == 0o700
        assert own_codex_home == str(one.codex_home)  # only the child's CODEX_HOME names the temp home
        assert not home.exists() and _login_homes(one) == []
        saved = one.root / "codex" / "credentials" / "account-2.json"
        assert saved.read_bytes() == new
        data = _registry(one)
        assert data["accounts"]["2"]["alias"] == "work"
        assert data["activeAccountNumber"] == 1  # the live account did not change
        # Codex's own home is untouched.
        assert (one.codex_home / "auth.json").read_bytes() == one.live
        assert sorted(p.name for p in one.codex_home.iterdir()) == one.before
        assert CodexSwitcher().current_account_number() == "1"

    def test_an_account_already_saved_is_updated(self, one, monkeypatch, capsys):
        renewed = make_login(**LOGIN_A, access_suffix="fresh-login")
        code, out, _ = _login(one, monkeypatch, capsys, _Runner(renewed))
        assert code == 0
        assert "Updated the saved login of #1 a@example.com" in out
        assert (one.root / "codex" / "credentials" / "account-1.json").read_bytes() == renewed
        assert list(_registry(one)["accounts"]) == ["1"]
        assert (one.codex_home / "auth.json").read_bytes() == one.live

    @pytest.mark.parametrize("runner, message", [
        (_Runner(None, returncode=1), "codex login failed (exit code 1); nothing was saved."),
        (_Runner(make_login(**LOGIN_B), returncode=2), "codex login failed (exit code 2); nothing was saved."),
        (_Runner(None, returncode=None, detail="codex is not on PATH"),
         "Could not run codex login (codex is not on PATH); nothing was saved."),
        (_Runner(None), "codex login left no auth.json, so there was nothing to save. Codex may "
                        "be set to keep logins in the OS keyring (cli_auth_credentials_store), "
                        "which qswap does not support."),
        (_Runner(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test"}).encode()),
         "The new login is an API-key login; qswap manages ChatGPT logins only; nothing was saved."),
    ], ids=["fails", "fails-after-writing", "no-codex", "no-auth-json", "api-key"])
    def test_nothing_changes_when_it_does_not_work(self, one, monkeypatch, capsys, runner, message):
        before = (one.root / "codex" / "sequence.json").read_bytes()
        code, _, err = _login(one, monkeypatch, capsys, runner)
        assert code == 1
        assert " ".join(err.split()) == f"Error: {message}"
        assert (one.root / "codex" / "sequence.json").read_bytes() == before
        assert _login_homes(one) == []
        assert (one.codex_home / "auth.json").read_bytes() == one.live

    def test_ctrl_c_cleans_up(self, one, monkeypatch, capsys):
        runner = _Runner(make_login(**LOGIN_B), raises=KeyboardInterrupt())
        code, out, _ = _login(one, monkeypatch, capsys, runner)
        assert code == 130
        assert "codex login was interrupted; nothing was saved." in out
        assert _login_homes(one) == [] and list(_registry(one)["accounts"]) == ["1"]

    def test_leftovers_older_than_a_day_are_swept(self, one, monkeypatch, capsys):
        codex_dir = one.root / "codex"
        old, young = codex_dir / f"{LOGIN_HOME_PREFIX}old", codex_dir / f"{LOGIN_HOME_PREFIX}young"
        for leftover in (old, young):
            leftover.mkdir()
            (leftover / "auth.json").write_bytes(make_login(**LOGIN_C))
        two_days_ago = time.time() - 2 * DAY
        os.utime(old, (two_days_ago, two_days_ago))
        _login(one, monkeypatch, capsys, _Runner(make_login(**LOGIN_B)))
        assert not old.exists()
        assert young.exists()  # may belong to a login running in another terminal

    def test_a_keyring_home_is_refused_before_codex_runs(self, one, monkeypatch, capsys):
        (one.codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        runner = _Runner(make_login(**LOGIN_B))
        code, _, err = _login(one, monkeypatch, capsys, runner)
        assert code == 1 and "file storage only" in err and runner.calls == []

    def test_help_and_add_help_point_at_it(self, env, capsys):
        code, out, _ = run(["codex", "login", "--help"], capsys)
        assert code == 0 and "temporary Codex" in out and "--device-auth" in out
        code, out, _ = run(["codex", "add", "--help"], capsys)
        assert code == 0 and "qswap codex login" in " ".join(out.split())
        code, out, _ = run(["codex", "help"], capsys)
        assert 'CODEX_HOME="$tmp" codex login' in out  # the manual fallback stays

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stand-in for codex")
    def test_the_real_runner_sets_codex_home_for_the_child_only(self, env, tmp_path, monkeypatch):
        """The runner itself, against a stand-in ``codex`` on PATH (a shell
        script, never the real CLI): argv, the child's CODEX_HOME, exit code."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "codex"
        fake.write_text('#!/bin/sh\necho "$@" > "$CODEX_HOME/argv"\nexit 3\n')
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        _real_path_lookup(monkeypatch)
        home = tmp_path / "login-home"
        home.mkdir()
        outcome = REAL_RUN_CODEX_LOGIN(home, ["--device-auth"])
        assert outcome == LoginRun(3)
        assert (home / "argv").read_text().strip() == "login --device-auth"
        assert os.environ["CODEX_HOME"] == str(env.codex_home)

    def test_the_real_runner_without_codex_on_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))
        _real_path_lookup(monkeypatch)
        assert REAL_RUN_CODEX_LOGIN(tmp_path, []) == LoginRun(None, "codex is not on PATH")


# -- qswap usage: the Codex observer --------------------------------------------------


def _usage(capsys) -> int:
    return run(["usage", "--json", "--no-scan"], capsys)[0]


class TestUsageObservesCodex:
    def test_a_login_changed_outside_qswap_is_noticed(self, two, capsys):
        codex_dir = two.root / "codex"
        before = len(ActivationLog.load(codex_dir).activations)
        write_live(two.codex_home, make_login(**LOGIN_B))  # e.g. switched by another tool
        assert _usage(capsys) == 0
        log = ActivationLog.load(codex_dir)
        assert len(log.activations) == before + 1
        latest = log.activations[-1]
        assert (latest.account, latest.number, latest.source) == ("b@example.com|acct-b", 1, "observed")

    def test_nothing_runs_when_no_codex_account_is_managed(self, env, capsys):
        with patch("ai_quota_swap.codex.switcher.CodexSwitcher",
                   side_effect=AssertionError("built a Codex switcher")):
            assert _usage(capsys) == 0
        assert not (env.root / "codex").exists()

    def test_a_codex_problem_never_fails_the_report(self, two, capsys):
        (two.codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        assert _usage(capsys) == 0
