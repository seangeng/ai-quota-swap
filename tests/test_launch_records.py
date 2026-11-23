"""Issue #21: the launch record of a ``qswap run N`` session on an API key.

``qswap run N`` on a key starts one Claude Code process on the shared
profile, with the key in ``--settings``, while every other session stays on
the login the activation log names. The launch gives claude a session id
(``--session-id``) and records it with the account (``key_runs.jsonl``), so
the ledger gives that session's usage to the key, ahead of the time and
owner-stamp rules. Everything is synthetic: transcripts from
tests/ledger_fixtures.py, accounts at ``example.com`` and ``token.local``,
claude faked at exec.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from ai_quota_swap import claude_settings, clis
from ai_quota_swap.ledger import activations
from ai_quota_swap.ledger.activations import (
    ActivationLog,
    Launch,
    load_launches,
    prune_launches,
    record_activation,
    record_launch,
)
from ai_quota_swap.locking import FileLock
from ai_quota_swap.session import SessionManager, key_run_session
from tests.ledger_fixtures import claude_line, owner_line, user_line, write_lines
from tests.test_ledger import T0, Env, _registry

KEY_5 = "sk-ant-api03-" + "k5k5k5k5k5" * 4
EMAIL_5 = "api-key-5@token.local"
SID = "6f1c2a3b-0000-4000-8000-000000000005"
DAY_S = 86_400


# == the session id a launch gives claude ==================================================


class TestTheSessionId:
    def test_a_launch_gets_a_fresh_id(self):
        extra, session = key_run_session([])
        assert extra == ["--session-id", session]
        assert str(uuid.UUID(session)) == session
        assert key_run_session([])[1] != session
        extra, session = key_run_session(["-p", "fix the bug -r", "--model", "opus"])
        assert extra == ["--session-id", session]

    @pytest.mark.parametrize("args", [
        ["--resume"], ["--resume", "abc"], ["--resume=abc"], ["-r"], ["-r", "abc"],
        ["--continue"], ["-c"], ["-pc"], ["--fork-session", "--resume", "abc"],
        ["--from-pr", "12"], ["--teleport"], ["--bg"], ["--background"],
    ])
    def test_an_option_that_picks_the_conversation_gets_none(self, args):
        """claude refuses --session-id beside --resume or --continue, and the
        conversation such a launch opens is not known when it starts."""
        assert key_run_session(args) == ([], None)

    def test_an_id_of_the_users_own_is_recorded_as_it_is(self):
        assert key_run_session(["--session-id", SID]) == ([], SID)
        assert key_run_session([f"--session-id={SID}", "-p", "hi"]) == ([], SID)
        assert key_run_session(["--session-id", "not-a-uuid"]) == ([], None)
        assert key_run_session(["--session-id"]) == ([], None)


# == qswap run N writes the record =========================================================


def _keyed_switcher():
    from tests.api_helpers import make_switcher, seed_api_key, seed_oauth

    s = make_switcher()
    seed_oauth(s, 1, "maya@example.com")
    seed_api_key(s, 5, KEY_5)
    return s


def _launch(s, monkeypatch, args: list[str]) -> list[str]:
    monkeypatch.setattr(clis, "find_cli", lambda name: "/fake/claude")
    monkeypatch.setattr(claude_settings, "qswap_executable", lambda: ["/opt/qswap/bin/qswap"])
    mgr = SessionManager(s)
    seen: dict = {}

    def fake_exec(claude_bin, argv, env):
        seen["args"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(mgr, "_exec", fake_exec)
    with pytest.raises(SystemExit):
        mgr.run("5", args, share=True)
    return seen["args"]


def _records(s) -> list[dict]:
    path = Path(s.backup_dir) / activations.LAUNCHES_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestTheLaunchRecord:
    def test_run_on_a_key_names_its_session(self, temp_home, monkeypatch):
        s = _keyed_switcher()
        before = time.time()
        args = _launch(s, monkeypatch, ["-p", "hello"])
        assert args[0] == "--settings"
        assert args[2] == "--session-id"
        session = args[3]
        assert args[4:] == ["-p", "hello"]
        (record,) = _records(s)
        assert record == {
            "pid": os.getpid(), "number": "5", "ts": record["ts"], "session": session,
            "email": EMAIL_5, "org": "",
        }
        assert before <= record["ts"] <= time.time()
        # No key and no token: the account, the time and the session id.
        text = (Path(s.backup_dir) / activations.LAUNCHES_FILENAME).read_text(encoding="utf-8")
        assert "sk-ant" not in text
        assert [launch.session for launch in load_launches(s.backup_dir)] == [session]

    def test_a_resume_is_recorded_without_a_session(self, temp_home, monkeypatch):
        s = _keyed_switcher()
        args = _launch(s, monkeypatch, ["--resume", "abc"])
        assert args[2:] == ["--resume", "abc"]
        (record,) = _records(s)
        assert set(record) == {"pid", "number", "ts"}
        assert load_launches(s.backup_dir) == []
        monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive", lambda pid: True)
        assert s.key_run_pids("5") == [os.getpid()]  # remove still names it

    def test_records_past_claude_codes_retention_go_at_the_next_launch(
        self, temp_home, monkeypatch
    ):
        s = _keyed_switcher()
        claude_settings.settings_path().write_text(
            json.dumps({"cleanupPeriodDays": 7}), encoding="utf-8"
        )
        now = time.time()
        path = Path(s.backup_dir) / activations.LAUNCHES_FILENAME
        old = {"pid": 4101, "number": "5", "ts": now - 9 * DAY_S, "session": SID,
               "email": EMAIL_5, "org": ""}
        recent = {**old, "pid": 4102, "ts": now - 7 * DAY_S}
        write_lines(path, [json.dumps(old), json.dumps(recent)])
        monkeypatch.setattr("ai_quota_swap.process_detection.is_pid_alive", lambda pid: False)
        _launch(s, monkeypatch, [])
        assert [r["pid"] for r in _records(s)] == [4102, os.getpid()]


# == keeping and dropping records ==========================================================


def _write(root: Path, *records) -> Path:
    path = root / activations.LAUNCHES_FILENAME
    write_lines(path, [r if isinstance(r, str) else json.dumps(r) for r in records])
    return path


class TestRetention:
    """A record is kept while Claude Code keeps the transcript it explains
    (cleanupPeriodDays from the launch, plus a day) and while its process
    runs; the ledger keeps what it gave each event."""

    NOW = T0 + 40 * DAY_S

    def test_old_records_of_finished_launches_go(self, tmp_path):
        old = {"pid": 11, "number": "5", "ts": self.NOW - 32 * DAY_S, "session": SID,
               "email": EMAIL_5}
        running = {**old, "pid": 12}
        recent = {**old, "pid": 13, "ts": self.NOW - 30 * DAY_S}
        before_the_field = {"pid": 14, "number": "5", "ts": self.NOW - 60 * DAY_S}
        path = _write(tmp_path, old, running, recent, before_the_field, '{"pid": 15, "num')
        dropped = prune_launches(tmp_path, keep_days=30, now=self.NOW, alive=lambda pid: pid == 12)
        assert dropped == 3
        assert [json.loads(line)["pid"] for line in path.read_text().splitlines()] == [12, 13]

    def test_nothing_to_drop_leaves_the_file_alone(self, tmp_path):
        record = {"pid": 11, "number": "5", "ts": self.NOW - DAY_S, "session": SID,
                  "email": EMAIL_5}
        path = _write(tmp_path, record)
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
        assert prune_launches(tmp_path, keep_days=30, now=self.NOW, alive=lambda pid: False) == 0
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == before
        assert prune_launches(tmp_path / "none", keep_days=30, now=self.NOW) == 0

    def test_a_launch_drops_them_under_the_lock(self, tmp_path):
        old = {"pid": 11, "number": "5", "ts": self.NOW - 40 * DAY_S}
        _write(tmp_path, old)
        assert record_launch(
            tmp_path, pid=21, number=5, email=EMAIL_5, org="", session=SID, keep_days=30,
            lock=tmp_path / ".lock", now=self.NOW, alive=lambda pid: False,
        )
        assert [r.ts for r in load_launches(tmp_path)] == [self.NOW]
        assert len((tmp_path / activations.LAUNCHES_FILENAME).read_text().splitlines()) == 1

    def test_a_launch_that_cannot_get_the_lock_only_appends(self, tmp_path, monkeypatch):
        monkeypatch.setattr(activations, "LAUNCH_LOCK_WAIT_S", 0.05)
        old = {"pid": 11, "number": "5", "ts": self.NOW - 40 * DAY_S}
        path = _write(tmp_path, old)
        held = FileLock(tmp_path / ".lock")
        assert held.acquire()
        try:
            assert record_launch(
                tmp_path, pid=21, number=5, email=EMAIL_5, org="", session=SID, keep_days=30,
                lock=tmp_path / ".lock", now=self.NOW, alive=lambda pid: False,
            )
        finally:
            held.release()
        assert [json.loads(line)["pid"] for line in path.read_text().splitlines()] == [11, 21]

    def test_without_a_lock_a_launch_only_appends(self, tmp_path):
        old = {"pid": 11, "number": "5", "ts": self.NOW - 40 * DAY_S}
        path = _write(tmp_path, old)
        record_launch(tmp_path, pid=21, number=5, email=EMAIL_5, org="", session=None,
                      now=self.NOW)
        assert len(path.read_text().splitlines()) == 2

    def test_it_never_raises(self, tmp_path):
        blocked = tmp_path / "file"
        blocked.write_text("", encoding="utf-8")
        assert record_launch(blocked, pid=1, number=5, email=EMAIL_5, org="", session=SID) is False

    @pytest.mark.parametrize(("settings", "days"), [
        (None, 30), ({}, 30), ({"cleanupPeriodDays": 7}, 7), ({"cleanupPeriodDays": 0}, 0),
        ({"cleanupPeriodDays": 99999}, 99999), ({"cleanupPeriodDays": -1}, 30),
        ({"cleanupPeriodDays": "7"}, 30), ({"cleanupPeriodDays": True}, 30), ("{not json", 30),
        ("﻿" + json.dumps({"cleanupPeriodDays": 14}), 14),
    ])
    def test_claude_codes_retention_is_read_from_its_settings(self, tmp_path, settings, days):
        path = tmp_path / "settings.json"
        if settings is not None:
            path.write_text(settings if isinstance(settings, str) else json.dumps(settings),
                            encoding="utf-8")
        assert claude_settings.transcript_days(path) == days


# == the ledger gives the session to the key ===============================================


def _world() -> Env:
    """#1 and #2 subscriptions, #1 the login throughout; #5 an API key."""
    env = Env()
    env.registry = _registry((1, "a@example.com", "uuid-a"), (2, "b@example.com", "uuid-b"))
    env.registry["accounts"]["5"] = {"email": EMAIL_5, "organizationUuid": "", "kind": "api_key"}
    record_activation(env.data, number=1, email="a@example.com", org="", now=T0, how="login")
    return env


def _launched(env: Env, ts: float, session: str = SID) -> None:
    assert record_launch(env.data, pid=4242, number=5, email=EMAIL_5, org="", session=session,
                         now=ts)


def _by_account(env: Env) -> dict[str, int]:
    return {r.label: r.totals.requests for r in env.report("account").rows}


def _key_spend(env: Env) -> float:
    from ai_quota_swap.ledger import spend

    reading = spend.account_spend(env.data, EMAIL_5, "", month_start=T0 - 3600,
                                  day_start=T0 - 3600, registry=env.registry)
    return reading.month if reading is not None else 0.0


class TestTheLedger:
    def _transcripts(self, env: Env, *, owner: str | None = None) -> None:
        lines = [owner_line(SID, owner)] if owner else []
        write_lines(env.transcript(f"{SID}.jsonl"), [
            *lines,
            user_line(T0 + 150, session=SID),
            claude_line(mid="k1", ts=T0 + 200, session=SID, inp=1_000_000, out=0, cr=0, w5=0),
            claude_line(mid="k2", ts=T0 + 300, session=SID),
        ])
        write_lines(env.transcript(f"{SID}/subagents/agent-1.jsonl"), [
            claude_line(mid="k3", ts=T0 + 310, session=SID, sidechain=True),
        ])
        write_lines(env.transcript("sess-default.jsonl"), [
            claude_line(mid="d1", ts=T0 + 250, session="sess-default"),
        ])

    def test_without_a_record_the_session_follows_the_log(self):
        """Today's attribution, kept for sessions with no record: the login."""
        env = _world()
        self._transcripts(env)
        env.scan()
        assert _by_account(env) == {"#1 a@example.com": 4}
        assert _key_spend(env) == 0

    def test_the_record_gives_the_session_to_the_key(self):
        env = _world()
        _launched(env, T0 + 100)
        self._transcripts(env)
        env.scan()
        assert _by_account(env) == {"#5 api-key-5": 3, "#1 a@example.com": 1}
        assert _key_spend(env) > 4.0  # the million input tokens are the key's
        assert env.accounts() == {f"{EMAIL_5}|": 3, "a@example.com|": 1}

    def test_the_record_wins_over_an_owner_stamp(self):
        env = _world()
        _launched(env, T0 + 100)
        self._transcripts(env, owner="uuid-b")
        env.scan()
        assert _by_account(env) == {"#5 api-key-5": 3, "#1 a@example.com": 1}

    def test_an_event_before_the_launch_is_not_the_records(self):
        env = _world()
        _launched(env, T0 + 250)
        self._transcripts(env)
        env.scan()
        assert _by_account(env) == {"#1 a@example.com": 2, "#5 api-key-5": 2}

    def test_the_saved_key_rule_leaves_a_recorded_session_on_the_key(self):
        """#5 made active as a saved key after the launch: a default session
        that began before it stays with #1 (the saved-key rule); the run
        session, though it began before too, is the key's by its record."""
        env = _world()
        _launched(env, T0 + 100)
        record_activation(env.data, number=5, email=EMAIL_5, org="", now=T0 + 140, how="saved-key")
        write_lines(env.transcript("sess-early.jsonl"), [
            claude_line(mid="e0", ts=T0 + 120, session="sess-early"),
            claude_line(mid="e1", ts=T0 + 400, session="sess-early"),
        ])
        write_lines(env.transcript(f"{SID}.jsonl"), [
            claude_line(mid="k1", ts=T0 + 110, session=SID),
            claude_line(mid="k2", ts=T0 + 410, session=SID),
        ])
        env.scan()
        assert _by_account(env) == {"#1 a@example.com": 2, "#5 api-key-5": 2}
        assert _key_spend(env) > 0

    def test_the_ledger_keeps_it_after_the_record_goes(self):
        env = _world()
        _launched(env, T0 + 100)
        self._transcripts(env)
        env.scan()
        (env.data / activations.LAUNCHES_FILENAME).unlink()
        assert _by_account(env) == {"#5 api-key-5": 3, "#1 a@example.com": 1}

    def test_a_record_written_during_a_scan_is_read_before_its_events_count(self):
        from ai_quota_swap.ledger.scanner import Scanner, default_config

        env = _world()
        self._transcripts(env)
        store = env.store()
        try:
            scanner = Scanner(store, default_config(env.data, env.registry))
            _launched(env, T0 + 100)  # after the scanner loaded the logs
            scanner.run(("claude",))
        finally:
            store.close()
        assert _by_account(env) == {"#5 api-key-5": 3, "#1 a@example.com": 1}

    def test_a_record_naming_no_account_is_not_used(self):
        env = _world()
        assert record_launch(env.data, pid=1, number=5, email="", org="", session=SID, now=T0)
        self._transcripts(env)
        env.scan()
        assert _by_account(env) == {"#1 a@example.com": 4}


def test_the_latest_launch_before_an_event_names_its_account():
    log = ActivationLog([], [
        Launch(ts=T0 + 10, session=SID, account="x|", number=5),
        Launch(ts=T0, session=SID, account="y|", number=6),
    ])
    assert log.launch_account(SID, T0 - 1) is None
    assert log.launch_account(SID, T0 + 5) == "y|"
    assert log.launch_account(SID, T0 + 10) == "x|"
    assert log.launch_account("other", T0 + 10) is None
    assert log.launch_account("", T0 + 10) is None
    assert log.launched(SID) and not log.launched("other") and not log.launched("")


def test_codex_usage_is_not_touched():
    """Launch records sit in the Claude Code data root; Codex's log root has none."""
    env = _world()
    _launched(env, T0)
    assert not ActivationLog.load(env.data / "codex").launched(SID)
    assert ActivationLog.load(env.data).launched(SID)
