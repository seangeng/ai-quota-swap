"""`qswap usage` end to end through `cli.main`, and the activation log writers
(never raising, and the switcher's two call sites)."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import cli, paths
from ai_quota_swap.ledger import activations
from ai_quota_swap.ledger.report import home_path
from ai_quota_swap.ledger.activations import (
    ActivationLog,
    observe_live_login,
    record_activation,
)
from tests.ledger_fixtures import (
    claude_line,
    codex_meta,
    codex_record,
    codex_turn,
    rollout_name,
    write_lines,
)

THREAD = "00000000-0000-4000-8000-0000000000c1"
THREAD_OLD = "00000000-0000-4000-8000-0000000000c2"


def run_cli(*args: str, capsys) -> tuple[int, str, str]:
    with patch.object(sys, "argv", ["qswap", "usage", *args]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def history() -> dict:
    """A small Claude Code and Codex history from the last two days."""
    now = time.time()
    projects = paths.get_claude_config_home() / "projects" / "-work-demo-app"
    write_lines(projects / "sess-1.jsonl", [
        claude_line(mid="msg_1", ts=now - 3600, out=100),
        claude_line(mid="msg_2", ts=now - 86400, model="claude-sonnet-5-5"),
        claude_line(mid="msg_3", ts=now - 120, model="claude-zeta-9"),
    ])
    rollout = paths.get_codex_home() / "sessions" / "2026" / "11" / "09" / rollout_name(THREAD)
    write_lines(rollout, [
        codex_meta(now - 600, THREAD),
        codex_turn(now - 600, "gpt-6.1-sol"),
        codex_record(now - 590, THREAD, "resp-1"),
    ])
    return {"now": now, "projects": projects, "rollout": rollout}


@pytest.fixture
def old_history(history) -> dict:
    """``history`` plus a transcript and a rollout last changed 30 days ago."""
    then = history["now"] - 30 * 86400
    old = write_lines(history["projects"] / "sess-old.jsonl", [
        claude_line(mid="msg_old", ts=then, session="sess-old"),
    ])
    os.utime(old, (then, then))
    rollout = history["rollout"].parent / rollout_name(THREAD_OLD)
    write_lines(rollout, [
        codex_meta(then, THREAD_OLD),
        codex_turn(then, "gpt-6.1-sol"),
        codex_record(then + 5, THREAD_OLD, "resp-old"),
    ])
    os.utime(rollout, (then + 5, then + 5))
    return history


class TestUsageCommand:
    def test_default_text_report(self, history, capsys):
        code, out, err = run_cli(capsys=capsys)
        assert code == 0, err
        assert "Token usage" in out and "by day" in out
        assert out.count(date.today().strftime("%Y-%m-%d")) >= 1
        assert "Total" in out
        assert "estimates at API list prices" in out
        assert "No price for: claude-zeta-9" in out
        assert "not in the ledger yet" not in out  # nothing older was left out
        assert "\033" not in out  # not a terminal: plain text
        assert err == ""  # no progress line off a TTY

    def test_json_report(self, history, capsys):
        code, out, _err = run_cli("--json", "--by", "provider", capsys=capsys)
        assert code == 0
        doc = json.loads(out)
        assert doc["schemaVersion"] == 1
        assert doc["by"] == "provider"
        rows = {r["key"]: r for r in doc["rows"]}
        assert rows["claude"]["requests"] == 3
        assert rows["codex"]["requests"] == 1
        assert doc["total"]["requests"] == 4
        assert doc["scan"]["newEvents"] == 4
        assert doc["unpricedModels"] == [{"model": "claude-zeta-9", "tokens": 135}]
        assert doc["estimate"] is True

    @pytest.mark.parametrize("by", ["day", "model", "account", "project", "provider", "session"])
    def test_every_grouping_renders(self, history, capsys, by):
        code, out, _ = run_cli("--by", by, capsys=capsys)
        assert code == 0
        assert f"by {by}" in out

    def test_provider_filter_and_days(self, history, capsys):
        code, out, _ = run_cli("--json", "--provider", "codex", "--days", "1", capsys=capsys)
        doc = json.loads(out)
        assert code == 0 and doc["provider"] == "codex"
        assert doc["total"]["requests"] == 1
        assert len(doc["rows"]) == 1

    def test_since_until(self, history, capsys):
        today = date.today().isoformat()
        code, out, _ = run_cli("--json", "--since", today, "--until", today, capsys=capsys)
        doc = json.loads(out)
        assert (doc["since"], doc["until"]) == (today, today)

    def test_no_scan_reports_the_ledger_as_it_is(self, history, capsys):
        code, out, _ = run_cli("--json", "--no-scan", capsys=capsys)
        assert json.loads(out)["total"]["requests"] == 0
        assert "scan" not in json.loads(out)
        run_cli("--json", capsys=capsys)
        write_lines(history["projects"] / "sess-1.jsonl",
                    [claude_line(mid="msg_new", ts=history["now"] - 60)], append=True)
        code, out, _ = run_cli("--json", "--no-scan", capsys=capsys)
        assert json.loads(out)["total"]["requests"] == 4

    def test_rescan_rebuilds(self, history, capsys):
        run_cli("--json", capsys=capsys)
        code, out, _ = run_cli("--json", "--rescan", capsys=capsys)
        doc = json.loads(out)
        assert doc["scan"]["newEvents"] == 4
        assert doc["total"]["requests"] == 4

    @pytest.mark.parametrize(
        "args, message",
        [
            (["--until", "2026-11-01"], "--until needs --since"),
            (["--since", "2026-11-05", "--until", "2026-11-01"], "--until is before --since"),
            (["--days", "0"], "--days must be at least 1"),
            (["--days", "3", "--since", "2026-11-01"], "not allowed with"),
            (["--no-scan", "--rescan"], "not allowed with"),
            (["--all", "--rescan"], "not allowed with"),
            (["--all", "--no-scan"], "not allowed with"),
            (["--since", "11/05/2026"], "not a date"),
            (["--by", "week"], "invalid choice"),
        ],
    )
    def test_bad_flags(self, capsys, args, message):
        code, _out, err = run_cli(*args, capsys=capsys)
        assert code == 64
        assert message in err

    def test_ledger_error_is_a_json_envelope(self, capsys):
        root = paths.get_backup_root()
        (root / "ledger.db").mkdir(parents=True)  # a directory where the file goes
        code, out, _ = run_cli("--json", capsys=capsys)
        assert code == 1
        doc = json.loads(out)
        assert doc["schemaVersion"] == 1
        assert doc["error"]["type"] == "OperationalError"

    def test_interrupt_exits_130_with_a_resume_hint(self, history, capsys):
        with patch("ai_quota_swap.ledger.usage.Scanner.run", side_effect=KeyboardInterrupt):
            code, _out, err = run_cli(capsys=capsys)
        assert code == 130
        assert "run qswap usage again to continue" in err

    def test_older_files_are_left_out_and_the_report_says_so(self, old_history, capsys):
        code, out, _ = run_cli(capsys=capsys)
        assert code == 0
        # The report's last line, wrapped to 80 columns.
        assert out.rstrip("\n").splitlines()[-2:] == [
            "2 older session files are not in the ledger yet. Use a wider --days or --since",
            "to include the ones you need, or --all for all of them.",
        ]
        code, out, _ = run_cli("--json", capsys=capsys)
        doc = json.loads(out)
        assert doc["total"]["requests"] == 4
        assert (doc["scan"]["filesSkipped"], doc["scan"]["olderFilesNotInLedger"]) == (2, 2)
        assert isinstance(doc["scan"]["modifiedSince"], str)
        # A wider request reads them...
        code, out, _ = run_cli("--json", "--days", "40", capsys=capsys)
        doc = json.loads(out)
        assert doc["total"]["requests"] == 6
        assert (doc["scan"]["filesSkipped"], doc["scan"]["olderFilesNotInLedger"]) == (0, 0)
        # ...and the default report no longer mentions them.
        code, out, _ = run_cli(capsys=capsys)
        assert "not in the ledger yet" not in out
        assert json.loads(run_cli("--json", capsys=capsys)[1])["scan"]["filesSkipped"] == 2

    def test_all_reads_everything_without_rebuilding(self, old_history, capsys):
        run_cli("--json", capsys=capsys)  # a first, narrow run
        code, out, _ = run_cli("--json", "--all", capsys=capsys)
        doc = json.loads(out)
        assert code == 0
        assert doc["scan"]["modifiedSince"] is None
        assert (doc["scan"]["filesSkipped"], doc["scan"]["olderFilesNotInLedger"]) == (0, 0)
        assert doc["scan"]["newEvents"] == 2  # only the two old files were new
        assert doc["total"]["requests"] == 4  # the report still covers the last 7 days
        code, out, _ = run_cli("--json", "--no-scan", "--days", "40", capsys=capsys)
        assert json.loads(out)["total"]["requests"] == 6

    def test_rescan_reads_everything(self, old_history, capsys):
        code, out, _ = run_cli("--json", "--rescan", capsys=capsys)
        doc = json.loads(out)
        assert doc["scan"]["newEvents"] == 6
        assert (doc["scan"]["filesSkipped"], doc["scan"]["modifiedSince"]) == (0, None)

    def test_help_lists_usage(self, capsys):
        with patch.object(sys, "argv", ["qswap", "help"]), pytest.raises(SystemExit):
            cli.main()
        assert "usage                 tokens and estimated cost" in capsys.readouterr().out

    def test_every_line_fits_80_columns(self, history, capsys):
        for by in ("day", "model", "account", "project", "provider", "session"):
            code, out, _ = run_cli("--by", by, capsys=capsys)
            assert code == 0
            assert [line for line in out.splitlines() if len(line) > 80] == [], by

    def test_usage_observes_the_live_login_first(self, history, capsys):
        home = Path.home()
        (home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"},
        }))
        run_cli("--json", capsys=capsys)
        log = ActivationLog.load(paths.get_backup_root())
        assert [a.account for a in log.activations] == ["a@example.com|"]
        assert log.activations[0].source == "observed"


class TestUsageEmptyStates:
    """No session files, or none in the range: a sentence and the next step."""

    def test_first_run_with_no_session_files_says_where_it_looked(self, capsys):
        code, out, err = run_cli(capsys=capsys)
        assert code == 0, err
        lines = out.splitlines()
        assert all(len(line) <= 80 for line in lines)
        text = " ".join(lines)
        assert text.startswith("No Claude Code or Codex session files were found in ")
        # The test's Codex home is outside ~ and longer than a line, so it is
        # broken across lines: compare without the breaks.
        squeezed = "".join(out.split())
        for d in (
            paths.get_claude_config_home() / "projects",
            paths.get_codex_home() / "sessions",
            paths.get_codex_home() / "archived_sessions",
        ):
            assert home_path(d) in squeezed
        # Home is spelled ~ (the Claude config home is under it), with this
        # platform's separator: ~/ on POSIX, ~\ on Windows (report.home_path).
        tilde = home_path(Path.home() / "x")[:-1]
        assert tilde == ("~\\" if os.sep == "\\" else "~/")
        assert tilde in text
        assert text.endswith("Use Claude Code or Codex, then run qswap usage again.")
        assert "Token usage" not in out and "$0.00" not in out and "Total" not in out

    def test_first_run_json_keeps_its_shape(self, history, capsys):
        full = json.loads(run_cli("--json", capsys=capsys)[1])
        for f in (history["projects"] / "sess-1.jsonl", history["rollout"]):
            f.unlink()
        code, out, _ = run_cli("--json", "--rescan", capsys=capsys)
        empty = json.loads(out)
        assert code == 0
        assert set(empty) == set(full)
        assert len(empty["rows"]) == 7 and empty["total"]["requests"] == 0
        assert empty["scan"]["filesSeen"] == 0

    def test_no_codex_files_with_provider_codex(self, capsys):
        projects = paths.get_claude_config_home() / "projects" / "-work-demo-app"
        write_lines(projects / "sess-1.jsonl", [claude_line(mid="msg_1", ts=time.time() - 60)])
        code, out, _ = run_cli("--provider", "codex", capsys=capsys)
        assert code == 0
        assert all(len(line) <= 80 for line in out.splitlines())
        codex = paths.get_codex_home()
        expected = (
            f"No Codex session files were found in {home_path(codex / 'sessions')} or "
            f"{home_path(codex / 'archived_sessions')}. "
            "Use Codex, then run qswap usage --provider codex again."
        )
        # Compared without whitespace: the test's long Codex home is broken
        # across lines.
        assert "".join(out.split()) == "".join(expected.split())
        assert "~/.claude" not in out  # only the directories this run read
        # Claude Code's own report is a table.
        code, out, _ = run_cli("--provider", "claude", capsys=capsys)
        assert "Token usage" in out

    def test_a_range_with_no_usage_says_so(self, history, capsys):
        today = date.today()
        since, until = today - timedelta(days=400), today - timedelta(days=394)
        code, out, _ = run_cli("--since", since.isoformat(), "--until", until.isoformat(),
                               capsys=capsys)
        assert code == 0
        assert out == (
            f"No usage between {since.isoformat()} and {until.isoformat()}.\n"
            "To look further back: qswap usage --days 30\n"
        )

    def test_an_empty_ledger_without_a_scan(self, history, capsys):
        code, out, _ = run_cli("--no-scan", "--days", "1", capsys=capsys)
        assert code == 0
        assert out.splitlines() == [
            f"No usage on {date.today().isoformat()} in the ledger.",
            "Run the command again without --no-scan to read new session files.",
        ]


# -- the activation log writers -------------------------------------------------


class TestActivationWriters:
    def test_record_appends_one_line(self, tmp_path):
        assert record_activation(tmp_path, number=2, email="b@example.com", org="o", now=100.5)
        line = json.loads((tmp_path / "activations.jsonl").read_text())
        assert line == {
            "ts": "1970-01-01T00:01:40.500Z", "number": 2, "email": "b@example.com",
            "org": "o", "source": "switch",
        }
        if sys.platform != "win32":  # Windows keeps privacy in ACLs, not mode bits
            assert ((tmp_path / "activations.jsonl").stat().st_mode & 0o777) == 0o600

    def test_observe_writes_only_on_change(self, tmp_path):
        assert observe_live_login(tmp_path, number=1, email="a@example.com", org="")
        assert not observe_live_login(tmp_path, number=1, email="a@example.com", org="")
        activations._last_seen.clear()  # a fresh process reads the tail
        assert not observe_live_login(tmp_path, number=1, email="a@example.com", org="")
        assert observe_live_login(tmp_path, number=2, email="b@example.com", org="")
        sources = [a.source for a in ActivationLog.load(tmp_path).activations]
        assert sources == ["observed", "observed"]

    def test_observe_sees_another_process_append(self, tmp_path):
        observe_live_login(tmp_path, number=1, email="a@example.com", org="")
        with open(tmp_path / "activations.jsonl", "a") as f:
            f.write(json.dumps({"ts": "2026-11-10T00:00:00Z", "number": 2,
                                "email": "b@example.com", "org": "", "source": "switch"}) + "\n")
        assert observe_live_login(tmp_path, number=1, email="a@example.com", org="")

    @pytest.mark.parametrize("writer", ["record", "observe"])
    def test_writers_never_raise(self, tmp_path, writer):
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("a file where the data root should be")
        call = record_activation if writer == "record" else observe_live_login
        assert call(blocked, number=1, email="a@example.com", org="") is False
        with patch("ai_quota_swap.ledger.activations.os.open", side_effect=OSError("disk full")):
            assert call(tmp_path, number=1, email="a@example.com", org="") is False
        with patch("ai_quota_swap.ledger.activations.json.dumps", side_effect=TypeError("boom")):
            assert call(tmp_path, number=1, email="a@example.com", org="") is False
        assert call(tmp_path, number="x", email="", org=None) is False

    def test_torn_and_junk_lines_are_skipped(self, tmp_path):
        (tmp_path / "activations.jsonl").write_text(
            '{"ts": "2026-11-10T00:00:00Z", "email": "a@example.com", "org": ""}\n'
            "junk\n"
            '{"ts": 12, "email": ""}\n'
            '{"ts": "2026-11-10T01:00:00Z", "email": "b@ex'
        )
        log = ActivationLog.load(tmp_path)
        assert [a.account for a in log.activations] == ["a@example.com|"]
        assert observe_live_login(tmp_path, number=2, email="b@example.com", org="")

    def test_log_lookup(self, tmp_path):
        record_activation(tmp_path, number=1, email="a@example.com", org="", now=100)
        record_activation(tmp_path, number=2, email="b@example.com", org="", now=200)
        log = ActivationLog.load(tmp_path)
        assert log.account_at(99) == ""
        assert log.account_at(100) == "a@example.com|"
        assert log.account_at(199.9) == "a@example.com|"
        assert log.account_at(5000) == "b@example.com|"


class TestSwitcherCallSites:
    """The two places the Claude switcher writes the log."""

    def _harness(self, temp_home):
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        return h

    def _log(self, h):
        return ActivationLog.load(h.switcher.backup_dir).activations

    def test_switch_records_a_switch_line(self, temp_home):
        h = self._harness(temp_home)
        result = h.switcher.switch_to("2", json_output=True)
        assert result["switched"] is True
        log = self._log(h)
        assert [(a.account, a.source, a.number) for a in log] == [
            ("b@example.com|", "switch", 2)
        ]

    def test_direct_activation_records_a_switch_line(self, temp_home):
        h = self._harness(temp_home)
        (temp_home / ".claude.json").unlink()  # no live login: direct activation
        h.switcher.switch_to("2", json_output=True)
        assert [(a.account, a.source) for a in self._log(h)] == [("b@example.com|", "switch")]

    def test_account_list_observes_a_login_made_elsewhere(self, temp_home):
        h = self._harness(temp_home)
        h.switcher._build_accounts_info()
        h.switcher._build_accounts_info()
        assert [(a.account, a.source, a.number) for a in self._log(h)] == [
            ("a@example.com|", "observed", 1)
        ]
        h.make_live("b@example.com", 2)  # a /login outside qswap
        h.switcher._build_accounts_info()
        assert [a.account for a in self._log(h)] == ["a@example.com|", "b@example.com|"]

    def test_no_observation_inside_a_run_session_shell(self, temp_home, monkeypatch):
        h = self._harness(temp_home)
        profile = h.switcher.backup_dir / "sessions" / "2-b_example.com"
        profile.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        h.switcher._note_live_login("2", ("b@example.com", ""))
        assert self._log(h) == []

    def test_a_failing_writer_never_reaches_the_switch(self, temp_home):
        h = self._harness(temp_home)
        with patch(
            "ai_quota_swap.ledger.activations.record_activation",
            side_effect=RuntimeError("boom"),
        ), patch(
            "ai_quota_swap.ledger.activations.observe_live_login",
            side_effect=RuntimeError("boom"),
        ):
            result = h.switcher.switch_to("2", json_output=True)
            h.switcher._build_accounts_info()
        assert result["switched"] is True
        assert h.switcher._get_sequence_data()["activeAccountNumber"] == 2

    def test_the_writer_takes_no_lock(self, temp_home):
        h = self._harness(temp_home)
        with patch("ai_quota_swap.switcher.FileLock") as lock:
            h.switcher._record_activation("2", "b@example.com", "")
            h.switcher._note_live_login("1", ("a@example.com", ""))
        lock.assert_not_called()
        assert len(self._log(h)) == 2
        assert not os.path.exists(str(h.switcher.backup_dir / "activations.jsonl.lock"))


class TestProgress:
    def test_progress_only_on_a_terminal(self):
        import io

        from ai_quota_swap.ledger.scanner import ScanStats, tty_progress

        class Stream(io.StringIO):
            def __init__(self, tty: bool):
                super().__init__()
                self._tty = tty

            def isatty(self) -> bool:
                return self._tty

        assert tty_progress(Stream(False)) is None
        out = Stream(True)
        show = tty_progress(out)
        show(ScanStats(files_seen=3, bytes_read=2_000_000, events=5), 10)
        assert "3/10 files" in out.getvalue() and "5 new responses" in out.getvalue()
