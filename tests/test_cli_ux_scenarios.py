"""The UX rules on the demo harness's scenarios (POSIX: the sandbox guards are).

Each run is the real CLI against a synthetic store in its own guarded
subprocess (scripts/demo/run.py), so a dead login, a failing usage endpoint
and an account the engine set aside are real states, not mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("the demo harness's sandbox guards are POSIX-only", allow_module_level=True)

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "scripts" / "demo" / "run.py"


def qswap(scenario: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN), "--width", "80", scenario, "--", *args],
        capture_output=True, text=True, timeout=120, cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _fits(text: str) -> bool:
    return all(len(line) <= 80 for line in text.splitlines())


class TestSwitchTargets:
    def test_a_dead_login_is_refused_with_the_way_out(self):
        r = qswap("trouble", "switch", "2", "++", "list", "--json")
        out = r.stdout + r.stderr
        assert "Error: #2 work needs re-login (its saved login was refused); not" in out
        assert "then run: qswap add" in out
        assert "To switch to it anyway: qswap switch 2 --force" in out
        listed = json.loads(r.stdout[r.stdout.index("{"):])
        assert listed["activeAccountNumber"] == 1  # nothing changed

    def test_an_account_the_engine_set_aside_is_refused_too(self):
        r = qswap("trouble", "switch", "4")
        assert r.returncode == 1
        assert "#4 side needs re-login (its saved login was refused)" in r.stdout + r.stderr

    def test_force_switches_with_a_warning(self):
        r = qswap("trouble", "switch", "2", "--force")
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("Warning: #2 work needs re-login")
        assert "switching anyway (--force)" in " ".join(r.stdout.split())
        assert "Switched #1 personal → #2 work" in r.stdout

    @pytest.mark.parametrize("target, words", [
        ("3", "#3 night-shift: usage unavailable (HTTP 503); switching anyway"),
        ("5", "#5 spare: usage as of 3h"),
    ])
    def test_unreadable_or_stale_switches_with_one_warning(self, target, words):
        r = qswap("trouble", "switch", target)
        assert r.returncode == 0, r.stderr
        first = r.stdout.splitlines()[0]
        assert first.startswith("Warning: ") and words in first
        assert f"→ #{target}" in r.stdout

    def test_json_carries_the_warning_and_the_refusal(self):
        r = qswap("trouble", "switch", "3", "--json")
        payload = json.loads(r.stdout)
        assert payload["switched"] is True
        assert any("usage unavailable" in w for w in payload["warnings"])
        r = qswap("trouble", "switch", "2", "--json")
        assert r.returncode == 1
        assert json.loads(r.stdout)["error"]["type"] == "SwitchError"

    def test_codex_usage_unknown_until_used_switches_with_a_note(self):
        r = qswap("trouble", "codex", "switch", "2")
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("Warning: #2 work: usage unknown until used")


class TestSetAsideIsVisible:
    def test_list_status_and_json(self):
        r = qswap("trouble", "list", "++", "list", "--json")
        text, _, raw = r.stdout.partition("\n{")
        block = text[text.index("  #4 side"):].split("\n\n")[0]
        assert "needs re-login (its saved login was refused)" in block
        rows = {a["number"]: a for a in json.loads("{" + raw)["accounts"]}
        assert rows[4]["needsRelogin"] is True and rows[4]["setAside"] == {"reason": "invalid_grant"}
        assert rows[2]["needsRelogin"] is True and "setAside" not in rows[2]
        assert rows[1]["needsRelogin"] is False
        assert _fits(text)

    def test_the_auto_log_names_it_as_the_reason(self):
        r = qswap("trouble", "auto", "--once")
        assert r.returncode == 3
        assert "no candidate has readable usage" not in r.stdout
        assert "#4 side         needs re-login" in r.stdout
        assert _fits(r.stdout)


class TestOutputShapes:
    def test_status_covers_both_providers(self):
        r = qswap("fleet", "status")
        lines = r.stdout.splitlines()
        assert lines[0] == "Claude Code" and "Codex" in lines
        assert "5 Claude Code accounts and 2 Codex accounts are managed." in lines
        assert lines[-1] == "Auto-switch is not running. Start it with: qswap auto"

    def test_list_block(self):
        r = qswap("fleet", "list")
        assert "     Holds 1 Fable reset · usable once a limit is reached · use by Dec 28" in r.stdout
        assert "├ extra   18%" in r.stdout
        assert "  #5 ci (api-key-5@token.local) · API key" in r.stdout
        assert "Account-" not in r.stdout and "$$" not in r.stdout and "resets:" not in r.stdout
        assert _fits(r.stdout)

    def test_reset_json_has_a_schema_version_and_errors_are_envelopes(self):
        r = qswap("fleet", "reset", "--json")
        assert json.loads(r.stdout)["schemaVersion"] == 1
        r = qswap("fleet", "reset", "9", "--json")
        assert r.returncode == 1
        assert json.loads(r.stdout)["error"]["type"] == "AccountNotFoundError"
        r = qswap("fleet", "codex", "reset", "--json")
        assert json.loads(r.stdout)["schemaVersion"] == 1

    def test_first_run_usage_says_why_it_is_empty(self):
        r = qswap("empty", "usage")
        assert r.returncode == 0
        assert r.stdout.startswith("No Claude Code or Codex session files were found")
        assert "$0.00" not in r.stdout

    def test_three_ticks_of_auto(self):
        r = qswap("autoswitch", "auto")
        lines = r.stdout.splitlines()
        ticks = [line for line in lines if line[:2].isdigit()]
        assert ticks[0].endswith("· no switch needed")
        assert ticks[-1].endswith("Switched #1 personal → #2 work · 5h reached 91%")
        assert _fits(r.stdout)
