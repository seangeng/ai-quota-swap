"""The token ledger: readers, dedupe, incremental scan, attribution, pricing,
and report grouping. All transcripts are synthetic (tests/ledger_fixtures.py)
and live under the test's isolated home."""

from __future__ import annotations

import bisect
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from ai_quota_swap import paths
from ai_quota_swap.ledger import readers, scanner
from ai_quota_swap.ledger.activations import ActivationLog, record_activation
from ai_quota_swap.ledger.pricing import PriceTable, normalize_model
from ai_quota_swap.ledger.report import (
    Row,
    build_report,
    day_bounds,
    fit_label,
    render_text,
    report_to_json,
)
from ai_quota_swap.ledger.scanner import Scanner, default_config
from ai_quota_swap.ledger.store import DB_FILENAME, LedgerStore
from tests.ledger_fixtures import (
    claude_line,
    codex_count,
    codex_meta,
    codex_record,
    codex_turn,
    owner_line,
    rollout_name,
    user_line,
    write_lines,
)

# 2026-11-09 12:00:00 UTC; every synthetic event sits on 2026-11-09/10 local.
T0 = 1_794_225_600.0
DAY = date.fromtimestamp(T0)
THREAD_A = "00000000-0000-4000-8000-00000000000a"
THREAD_B = "00000000-0000-4000-8000-00000000000b"
THREAD_C = "00000000-0000-4000-8000-00000000000c"


class Env:
    def __init__(self) -> None:
        self.home = Path.home()
        self.data = paths.get_backup_root()
        self.projects = paths.get_claude_config_home() / "projects"
        self.codex = paths.get_codex_home()
        self.registry: dict | None = None
        self.codex_registry: dict | None = None

    def transcript(self, name: str = "sess-1.jsonl", slug: str = "-work-demo-app") -> Path:
        return self.projects / slug / name

    def rollout(self, thread: str, *, archived: bool = False, zst: bool = False) -> Path:
        base = self.codex / ("archived_sessions" if archived else "sessions/2026/11/09")
        name = rollout_name(thread) + (".zst" if zst else "")
        return base / name

    def store(self) -> LedgerStore:
        return LedgerStore(self.data / DB_FILENAME)

    def scan(self, providers=("claude", "codex"), since=None, **kw) -> scanner.ScanStats:
        store = self.store()
        try:
            return Scanner(store, default_config(self.data, self.registry), **kw).run(
                providers, since=since
            )
        finally:
            store.close()

    def sums(self, provider: str | None = None) -> dict:
        store = self.store()
        try:
            sql = (
                "SELECT coalesce(sum(input),0), coalesce(sum(output),0), "
                "coalesce(sum(cache_read),0), coalesce(sum(cache_write_5m),0), "
                "coalesce(sum(cache_write_1h),0), coalesce(sum(reasoning),0), "
                "coalesce(sum(requests),0) FROM buckets"
            )
            args: tuple = ()
            if provider:
                sql += " WHERE provider = ?"
                args = (provider,)
            row = store.conn.execute(sql, args).fetchone()
        finally:
            store.close()
        keys = ("input", "output", "cache_read", "w5", "w1", "reasoning", "requests")
        return dict(zip(keys, row))

    def accounts(self) -> dict[str, int]:
        store = self.store()
        try:
            return dict(
                store.conn.execute(
                    "SELECT account, sum(requests) FROM buckets GROUP BY account"
                ).fetchall()
            )
        finally:
            store.close()

    def report(self, by: str = "day", **kw):
        store = self.store()
        try:
            return build_report(
                store,
                since=kw.pop("since", DAY),
                until=kw.pop("until", date.fromtimestamp(T0 + 86400)),
                by=by,
                registries={"claude": self.registry, "codex": self.codex_registry},
                logs={
                    "claude": ActivationLog.load(self.data),
                    "codex": ActivationLog.load(self.data / "codex"),
                },
                prices=PriceTable.load(self.data),
                **kw,
            )
        finally:
            store.close()


@pytest.fixture
def env() -> Env:
    return Env()


def _registry(*accounts: tuple[int, str, str]) -> dict:
    return {
        "accounts": {
            str(n): {"email": email, "uuid": uuid, "organizationUuid": "", "organizationName": ""}
            for n, email, uuid in accounts
        },
        "sequence": [n for n, _e, _u in accounts],
    }


# -- dedupe --------------------------------------------------------------------


class TestClaudeDedupe:
    def test_duplicate_lines_of_one_response_count_once_with_the_final_output(self, env):
        # One response, three content blocks: the output count grows to its
        # final value on the last line; input and cache counts repeat.
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0, out=3, stop=None),
            user_line(T0 + 1),
            claude_line(mid="msg_1", ts=T0 + 2, out=3, stop=None),
            claude_line(mid="msg_1", ts=T0 + 3, out=40, stop="tool_use"),
        ])
        env.scan()
        assert env.sums() == {
            "input": 10, "output": 40, "cache_read": 100, "w5": 20, "w1": 0,
            "reasoning": 0, "requests": 1,
        }

    def test_resumed_session_replay_in_a_second_file_is_not_counted_twice(self, env):
        write_lines(env.transcript("sess-1.jsonl"), [
            claude_line(mid="msg_1", ts=T0, session="sess-1"),
            claude_line(mid="msg_2", ts=T0 + 60, session="sess-1"),
        ])
        # The resumed session copies both responses into its own file.
        write_lines(env.transcript("sess-2.jsonl"), [
            claude_line(mid="msg_1", ts=T0, session="sess-2"),
            claude_line(mid="msg_2", ts=T0 + 60, session="sess-2"),
            claude_line(mid="msg_3", ts=T0 + 3600, session="sess-2"),
        ])
        env.scan()
        assert env.sums()["requests"] == 3
        assert env.sums()["input"] == 30

    def test_replay_scanned_in_a_later_scan_is_not_counted_twice(self, env):
        write_lines(env.transcript("sess-1.jsonl"), [claude_line(mid="msg_1", ts=T0)])
        env.scan()
        write_lines(env.transcript("sess-2.jsonl"), [
            claude_line(mid="msg_1", ts=T0, session="sess-2"),
            claude_line(mid="msg_9", ts=T0 + 5, session="sess-2"),
        ])
        env.scan()
        assert env.sums()["requests"] == 2

    def test_final_output_written_after_a_scan_adds_only_the_difference(self, env):
        path = write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0, out=3, stop=None)])
        env.scan()
        write_lines(path, [claude_line(mid="msg_1", ts=T0 + 4, out=90)], append=True)
        env.scan()
        s = env.sums()
        assert (s["input"], s["output"], s["cache_read"], s["requests"]) == (10, 90, 100, 1)

    def test_rescans_are_stable(self, env):
        write_lines(env.transcript(), [
            claude_line(mid=f"msg_{i}", ts=T0 + i) for i in range(30)
        ])
        env.scan()
        first = env.sums()
        env.scan()
        env.scan()
        assert env.sums() == first
        store = env.store()
        store.reset()
        store.close()
        env.scan()
        assert env.sums() == first

    def test_sidechain_transcripts_are_separate_requests(self, env):
        write_lines(env.transcript(), [claude_line(mid="msg_main", ts=T0)])
        write_lines(env.transcript("agent-x1.jsonl", slug="-work-demo-app/sess-1/subagents"), [
            claude_line(mid="msg_sub", ts=T0 + 1, sidechain=True),
        ])
        env.scan()
        assert env.sums()["requests"] == 2

    def test_synthetic_error_placeholders_and_zero_usage_are_skipped(self, env):
        write_lines(env.transcript(), [
            claude_line(mid="msg_err", ts=T0, model="<synthetic>", inp=0, out=0, cr=0, w5=0),
            claude_line(mid="msg_zero", ts=T0, inp=0, out=0, cr=0, w5=0),
            "not json at all",
            claude_line(mid="msg_ok", ts=T0),
        ])
        env.scan()
        assert env.sums()["requests"] == 1

    def test_missing_request_id_falls_back_to_the_message_id(self, env):
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0, rid="", out=1),
            claude_line(mid="msg_1", ts=T0, rid="", out=7),
        ])
        env.scan()
        assert (env.sums()["requests"], env.sums()["output"]) == (1, 7)

    def test_one_hour_cache_writes_are_kept_apart(self, env):
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0, w5=5, w1=7)])
        env.scan()
        assert (env.sums()["w5"], env.sums()["w1"]) == (5, 7)


# -- incremental scan ---------------------------------------------------------


class TestIncrementalScan:
    def test_append_reads_only_the_new_lines(self, env):
        path = write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0)])
        env.scan()
        write_lines(path, [claude_line(mid="msg_2", ts=T0 + 10)], append=True)
        stats = env.scan()
        assert stats.events == 1
        assert stats.bytes_read == len(claude_line(mid="msg_2", ts=T0 + 10)) + 1
        assert env.sums()["requests"] == 2

    def test_crlf_lines_are_counted_and_resumed_at_the_right_byte(self, env):
        # Claude Code writes "\n" everywhere, but a transcript copied through
        # a tool that translates line endings arrives with "\r\n". The cursor
        # is a byte offset, so it must land after the "\r\n" and an append
        # must read only the new line.
        path = env.transcript()
        path.parent.mkdir(parents=True)
        first = claude_line(mid="msg_1", ts=T0)
        second = claude_line(mid="msg_2", ts=T0 + 10)
        path.write_bytes((first + "\r\n").encode())
        assert env.scan().bytes_read == len(first) + 2
        assert env.sums()["requests"] == 1
        with open(path, "ab") as f:
            f.write((second + "\r\n").encode())
        stats = env.scan()
        assert stats.events == 1
        assert stats.bytes_read == len(second) + 2
        assert env.sums()["requests"] == 2
        assert env.scan().bytes_read == 0

    def test_unchanged_files_are_not_read_again(self, env):
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0)])
        env.scan()
        stats = env.scan()
        assert (stats.files_seen, stats.files_read, stats.bytes_read) == (1, 0, 0)

    def test_a_line_still_being_written_waits_for_its_newline(self, env):
        path = env.transcript()
        path.parent.mkdir(parents=True)
        full = claude_line(mid="msg_1", ts=T0)
        path.write_text(full[:40])
        env.scan()
        assert env.sums()["requests"] == 0
        path.write_text(full + "\n")
        env.scan()
        assert env.sums()["requests"] == 1

    def test_truncated_file_is_read_again_from_the_start(self, env):
        path = write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0), claude_line(mid="msg_2", ts=T0 + 1),
        ])
        env.scan()
        # Rewritten shorter, in place: an old response plus a new one.
        with open(path, "w") as f:
            f.write(claude_line(mid="msg_3", ts=T0 + 2) + "\n")
        env.scan()
        assert env.sums()["requests"] == 3

    def test_rotated_file_with_a_new_inode_is_read_from_the_start(self, env):
        path = write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0)])
        env.scan()
        tmp = path.with_suffix(".new")
        write_lines(tmp, [
            claude_line(mid="msg_1", ts=T0),
            claude_line(mid="msg_2", ts=T0 + 1),
            claude_line(mid="msg_3", ts=T0 + 2),
        ])
        os.replace(tmp, path)
        env.scan()
        assert env.sums()["requests"] == 3

    def test_interrupted_scan_resumes_and_matches_an_uninterrupted_one(self, env, monkeypatch):
        for n in range(3):
            write_lines(env.transcript(f"sess-{n}.jsonl"), [
                claude_line(mid=f"msg_{n}_{i}", ts=T0 + i, session=f"sess-{n}", out=i + 1)
                for i in range(25)
            ])
        expected_requests = 75
        real = readers.parse_claude_line
        calls = {"n": 0}

        def flaky(raw):
            calls["n"] += 1
            if calls["n"] == 40:
                raise KeyboardInterrupt
            return real(raw)

        monkeypatch.setattr(scanner, "parse_claude_line", flaky)
        with pytest.raises(KeyboardInterrupt):
            env.scan(batch_events=7)
        partial = env.sums()["requests"]
        assert 0 < partial < expected_requests  # some batches were committed
        monkeypatch.setattr(scanner, "parse_claude_line", real)
        env.scan(batch_events=7)
        resumed = env.sums()
        store = env.store()
        store.reset()
        store.close()
        env.scan()
        assert resumed == env.sums()
        assert resumed["requests"] == expected_requests

    def test_scan_reads_only_under_the_isolated_roots(self, env, tmp_path):
        """Every file the scanner opens or lists sits under this test's home."""
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0)])
        write_lines(env.rollout(THREAD_A), [
            codex_meta(T0, THREAD_A), codex_turn(T0), codex_record(T0 + 1, THREAD_A, "resp-1"),
        ])
        prof = env.data / "sessions" / "2-b_example.com" / "projects" / "-work-x"
        write_lines(prof / "sess-p.jsonl", [claude_line(mid="msg_p", ts=T0, session="sess-p")])
        store = env.store()  # imports and schema setup happen before recording
        cfg = default_config(env.data, _registry((2, "b@example.com", "uuid-b")))
        seen: list[str] = []
        recording = {"on": False}

        def hook(event, args):
            if not recording["on"]:
                return
            if event in ("open", "os.scandir", "os.listdir", "os.stat"):
                target = args[0] if args else None
                if isinstance(target, (str, bytes, os.PathLike)):
                    seen.append(os.fsdecode(target))

        sys.addaudithook(hook)
        recording["on"] = True
        try:
            stats = Scanner(store, cfg).run()
        finally:
            recording["on"] = False
            store.close()
        assert stats.events == 3
        assert seen, "the audit hook saw nothing; the check proves nothing"
        # Two isolated roots: the test's home, and the per-test CODEX_HOME that
        # the harness points Codex at (a sibling temp dir, not under the home).
        roots = [os.path.realpath(env.home), os.path.realpath(paths.get_codex_home())]
        outside = [
            p for p in seen
            if not any(
                os.path.realpath(p) == r or os.path.realpath(p).startswith(r + os.sep)
                for r in roots
            )
        ]
        assert outside == []


# -- Codex --------------------------------------------------------------------


class TestCodex:
    def test_records_are_counted_with_cached_input_split_out(self, env):
        write_lines(env.rollout(THREAD_A), [
            codex_meta(T0, THREAD_A),
            codex_turn(T0, "gpt-6.1-sol"),
            codex_record(T0 + 1, THREAD_A, "resp-1", inp=1000, cached=800, out=50, reasoning=20),
            codex_count(T0 + 1, (1000, 800, 50), (1000, 800, 50)),
            codex_record(T0 + 2, THREAD_A, "resp-2", inp=500, cached=0, out=10, reasoning=0),
        ])
        env.scan()
        s = env.sums("codex")
        # token_count lines are not added on top of the records.
        assert (s["input"], s["cache_read"], s["output"], s["reasoning"], s["requests"]) == (
            700, 800, 60, 20, 2,
        )
        store = env.store()
        models = {r[0] for r in store.conn.execute("SELECT model FROM buckets")}
        store.close()
        assert models == {"gpt-6.1-sol"}

    def test_a_thread_in_sessions_and_archived_sessions_counts_once(self, env):
        lines = [codex_meta(T0, THREAD_A), codex_turn(T0),
                 codex_record(T0 + 1, THREAD_A, "resp-1")]
        write_lines(env.rollout(THREAD_A), lines)
        write_lines(env.rollout(THREAD_A, archived=True), lines)
        env.scan()
        assert env.sums("codex")["requests"] == 1

    def test_archiving_moves_the_cursor_with_the_thread(self, env):
        live = write_lines(env.rollout(THREAD_A), [
            codex_meta(T0, THREAD_A), codex_turn(T0), codex_record(T0 + 1, THREAD_A, "resp-1"),
        ])
        env.scan()
        archived = env.rollout(THREAD_A, archived=True)
        archived.parent.mkdir(parents=True)
        os.replace(live, archived)
        write_lines(archived, [codex_record(T0 + 9, THREAD_A, "resp-2")], append=True)
        stats = env.scan()
        assert env.sums("codex")["requests"] == 2
        # Resumed at the old offset: only the appended line was read.
        assert stats.bytes_read == len(codex_record(T0 + 9, THREAD_A, "resp-2")) + 1

    def test_a_crlf_rollout_resumes_at_the_right_byte_with_its_reader_state(self, env):
        # Same as the Claude case: "\r\n" endings must not shift the cursor,
        # and the model read before it still applies after it.
        path = env.rollout(THREAD_A)
        path.parent.mkdir(parents=True)
        head = [codex_meta(T0, THREAD_A), codex_turn(T0, "gpt-5.4"),
                codex_record(T0 + 1, THREAD_A, "resp-1")]
        path.write_bytes("".join(line + "\r\n" for line in head).encode())
        env.scan()
        tail = codex_record(T0 + 9, THREAD_A, "resp-2")
        with open(path, "ab") as f:
            f.write((tail + "\r\n").encode())
        stats = env.scan()
        assert stats.bytes_read == len(tail) + 2
        store = env.store()
        rows = store.conn.execute(
            "SELECT model, sum(requests) FROM buckets WHERE provider = 'codex' GROUP BY 1"
        ).fetchall()
        store.close()
        assert rows == [("gpt-5.4", 2)]

    @pytest.mark.skipif(not scanner.ZSTD_AVAILABLE, reason="needs zstd support")
    def test_compressed_in_place_is_not_counted_again(self, env):
        lines = [codex_meta(T0, THREAD_A), codex_turn(T0),
                 codex_record(T0 + 1, THREAD_A, "resp-1")]
        live = write_lines(env.rollout(THREAD_A), lines)
        env.scan()
        data = live.read_bytes()
        # The scanner's own zstd: the stdlib's from 3.14, `zstandard` before.
        env.rollout(THREAD_A, zst=True).write_bytes(scanner._zstd.compress(data))
        live.unlink()
        env.scan()
        assert env.sums("codex")["requests"] == 1
        # A compressed file read from scratch counts the same.
        store = env.store()
        store.reset()
        store.close()
        env.scan()
        assert env.sums("codex")["requests"] == 1

    def test_compressed_files_are_skipped_without_zstd(self, env, monkeypatch):
        monkeypatch.setattr(scanner, "ZSTD_AVAILABLE", False)
        env.rollout(THREAD_A, zst=True).parent.mkdir(parents=True)
        env.rollout(THREAD_A, zst=True).write_bytes(b"\x28\xb5\x2f\xfd not really")
        stats = env.scan()
        assert stats.skipped_compressed == 1
        assert env.sums("codex")["requests"] == 0

    def test_broken_symlinks_are_tolerated(self, env):
        target = env.home / "unmounted-volume" / rollout_name(THREAD_A)
        link = env.rollout(THREAD_A)
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        write_lines(env.rollout(THREAD_B), [
            codex_meta(T0, THREAD_B), codex_turn(T0), codex_record(T0, THREAD_B, "resp-b"),
        ])
        stats = env.scan()
        assert stats.missing == 1
        assert env.sums("codex")["requests"] == 1

    @pytest.mark.parametrize("when", ["before-its-stat", "before-its-read"])
    def test_a_file_deleted_after_the_listing_is_vanished_not_missing(
        self, env, monkeypatch, when
    ):
        """Decision R4-2: an entry that no longer exists at all when the scan
        fails to read it was deleted between the listing and the read. It is
        counted as vanished, not as an unreadable (missing) entry."""
        gone = write_lines(env.transcript("gone.jsonl"), [claude_line(mid="m-1", ts=T0)])
        write_lines(env.transcript("kept.jsonl"), [claude_line(mid="m-2", ts=T0)])
        real_stat_all = scanner._stat_all

        def stat_all(paths):
            if when == "before-its-stat":
                gone.unlink()
                return real_stat_all(paths)
            stats = real_stat_all(paths)
            gone.unlink()
            return stats

        monkeypatch.setattr(scanner, "_stat_all", stat_all)
        stats = env.scan(("claude",))
        assert (stats.vanished, stats.missing, stats.missing_paths) == (1, 0, [])
        assert (stats.to_json()["vanished"], stats.to_json()["missing"]) == (1, 0)
        assert env.sums("claude")["requests"] == 1

    def test_a_broken_link_is_missing_not_vanished(self, env):
        link = env.transcript("linked.jsonl")
        link.parent.mkdir(parents=True)
        link.symlink_to(env.home / "unmounted-volume" / "linked.jsonl")
        stats = env.scan(("claude",))
        assert (stats.missing, stats.vanished) == (1, 0)
        assert stats.to_json()["vanished"] == 0

    @pytest.mark.skipif(
        os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
        reason="mode 000 keeps only a POSIX non-root reader out",
    )
    def test_a_file_it_may_not_read_is_missing_not_vanished(self, env):
        locked = write_lines(env.transcript("locked.jsonl"), [claude_line(mid="m-1", ts=T0)])
        locked.chmod(0)
        try:
            stats = env.scan(("claude",))
        finally:
            locked.chmod(0o600)
        assert (stats.missing, stats.vanished) == (1, 0)

    @pytest.mark.skipif(
        os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
        reason="a directory's search bit keeps only a POSIX non-root reader out",
    )
    def test_a_file_in_a_directory_it_may_not_enter_is_missing_not_vanished(self, env):
        """Decision R4-2: a permission error stays missing. A directory the
        scan can list but not enter (mode 0600, as after a recursive chmod
        600) still holds its files: they are unreadable, not deleted."""
        write_lines(env.transcript("kept.jsonl"), [claude_line(mid="m-1", ts=T0)])
        held = write_lines(
            env.transcript("held.jsonl", slug="-work-other"), [claude_line(mid="m-2", ts=T0)]
        )
        held.parent.chmod(0o600)
        try:
            stats = env.scan(("claude",))
        finally:
            held.parent.chmod(0o700)
        assert (stats.missing, stats.vanished) == (1, 0)
        assert stats.missing_paths == [str(held)]
        assert env.sums("claude")["requests"] == 1

    def test_symlinked_rollouts_are_read(self, env):
        real = write_lines(env.home / "archive" / rollout_name(THREAD_A), [
            codex_meta(T0, THREAD_A), codex_turn(T0), codex_record(T0, THREAD_A, "resp-1"),
        ])
        link = env.rollout(THREAD_A)
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        env.scan()
        assert env.sums("codex")["requests"] == 1

    def test_token_count_fallback_counts_deltas_and_never_goes_negative(self, env):
        # An old file: no token_usage_record lines at all.
        write_lines(env.rollout(THREAD_A), [
            codex_meta(T0, THREAD_A, version="0.40.0", ordinal=None),
            codex_turn(T0, "gpt-5.4"),
            codex_count(T0 + 1, (100, 0, 10), (100, 0, 10)),
            codex_count(T0 + 2, (100, 0, 10), (100, 0, 10)),  # repeated totals
            codex_count(T0 + 3, (250, 50, 30), (150, 50, 20)),
            # The total goes down: a new context. Count that response's own
            # usage, not a negative difference.
            codex_count(T0 + 4, (40, 0, 5), (40, 0, 5)),
        ])
        env.scan()
        s = env.sums("codex")
        # fresh input: 100 + (150 - 50 cached) + 40 ; cached 50 ; output 10 + 20 + 5
        assert (s["input"], s["cache_read"], s["output"], s["requests"]) == (240, 50, 35, 3)
        env.scan()
        assert env.sums("codex")["requests"] == 3

    def test_fallback_skips_a_forks_inherited_prefix(self, env):
        root = THREAD_A
        parent = [
            codex_meta(T0, root, version="0.40.0", ordinal=None),
            codex_turn(T0, "gpt-5.4"),
            codex_count(T0 + 1, (100, 0, 10), (100, 0, 10)),
            codex_count(T0 + 2, (300, 0, 30), (200, 0, 20)),
        ]
        write_lines(env.rollout(root), parent)
        # The child starts with its own session_meta, then replays the
        # parent's (re-timestamped), then does its own work.
        child = [
            codex_meta(T0 + 50, THREAD_B, session=root, version="0.40.0", ordinal=None),
            codex_meta(T0 + 50, root, version="0.40.0", ordinal=None),
            codex_count(T0 + 51, (100, 0, 10), (100, 0, 10)),
            codex_count(T0 + 52, (300, 0, 30), (200, 0, 20)),
            codex_turn(T0 + 53, "gpt-5.4"),
            codex_count(T0 + 54, (360, 0, 36), (60, 0, 6)),
        ]
        write_lines(env.rollout(THREAD_B), child)
        env.scan()
        s = env.sums("codex")
        assert (s["input"], s["output"], s["requests"]) == (360, 36, 3)

    def test_fallback_uses_ordinals_when_present(self, env):
        write_lines(env.rollout(THREAD_B), [
            codex_meta(T0, THREAD_B, session=THREAD_A, version="0.90.0", ordinal=0, start_ordinal=5),
            codex_meta(T0, THREAD_A, version="0.90.0", ordinal=1),
            codex_turn(T0, "gpt-5.4", ordinal=2),  # the parent's turn, inherited
            codex_count(T0 + 1, (100, 0, 10), (100, 0, 10), ordinal=3),
            codex_count(T0 + 2, (300, 0, 30), (200, 0, 20), ordinal=4),
            codex_turn(T0 + 3, "gpt-5.4", ordinal=5),
            codex_count(T0 + 4, (360, 0, 36), (60, 0, 6), ordinal=6),
        ])
        env.scan()
        s = env.sums("codex")
        assert (s["input"], s["output"], s["requests"]) == (60, 6, 1)

    def test_a_record_writing_forks_inherited_token_counts_are_not_counted(self, env):
        write_lines(env.rollout(THREAD_B), [
            codex_meta(T0, THREAD_B, session=THREAD_A, ordinal=0, start_ordinal=4),
            codex_meta(T0, THREAD_A, ordinal=1),
            codex_count(T0 + 1, (100, 0, 10), (100, 0, 10), ordinal=2),
            codex_count(T0 + 2, (300, 0, 30), (200, 0, 20), ordinal=3),
            codex_turn(T0 + 3, ordinal=4),
            codex_record(T0 + 4, THREAD_B, "resp-child", inp=60, cached=0, out=6, ordinal=5),
            codex_count(T0 + 4, (360, 0, 36), (60, 0, 6), ordinal=6),
        ])
        env.scan()
        s = env.sums("codex")
        assert (s["input"], s["output"], s["requests"]) == (60, 6, 1)

    def test_token_counts_after_the_first_record_are_never_counted(self, env):
        path = write_lines(env.rollout(THREAD_A), [
            codex_meta(T0, THREAD_A), codex_turn(T0),
            codex_record(T0 + 1, THREAD_A, "resp-1", inp=100, cached=0, out=10),
        ])
        env.scan()
        # A later scan sees only the token_count: the saved reader state
        # remembers that this thread writes records.
        write_lines(path, [codex_count(T0 + 1, (100, 0, 10), (100, 0, 10))], append=True)
        env.scan()
        assert env.sums("codex")["requests"] == 1


# -- attribution --------------------------------------------------------------


class TestAttribution:
    def test_usage_before_the_first_activation_is_unattributed(self, env):
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0 + 100)
        write_lines(env.transcript(), [
            claude_line(mid="msg_early", ts=T0), claude_line(mid="msg_late", ts=T0 + 200),
        ])
        env.scan()
        assert env.accounts() == {"": 1, "a@example.com|": 1}

    def test_usage_follows_a_switch(self, env):
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        record_activation(env.data, number=2, email="b@example.com", org="org-b", now=T0 + 100)
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0 + 10),
            claude_line(mid="msg_2", ts=T0 + 99),
            claude_line(mid="msg_3", ts=T0 + 100),
            claude_line(mid="msg_4", ts=T0 + 500),
        ])
        env.scan()
        assert env.accounts() == {"a@example.com|": 2, "b@example.com|org-b": 2}

    def test_codex_reads_its_own_activation_log(self, env):
        record_activation(env.data / "codex", number=1, email="c@example.com", org="", now=T0)
        write_lines(env.rollout(THREAD_A), [
            codex_meta(T0, THREAD_A), codex_turn(T0), codex_record(T0 + 1, THREAD_A, "resp-1"),
        ])
        env.scan()
        assert env.accounts() == {"c@example.com|": 1}

    def test_desktop_app_sessions_are_kept_apart(self, env):
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0 + 5, entrypoint="claude-desktop"),
        ])
        env.scan()
        assert env.accounts() == {"desktop": 1}

    def test_run_profile_session_in_the_shared_tree_goes_to_its_owner(self, env):
        env.registry = _registry((1, "a@example.com", "uuid-a"), (2, "b@example.com", "uuid-b"))
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        # The default login is account 1 throughout; this session was started
        # from `qswap run 2`, so Claude Code stamped account 2 as its owner.
        write_lines(env.transcript("sess-run.jsonl"), [
            owner_line("sess-run", "uuid-b"),
            claude_line(mid="msg_r1", ts=T0 + 10, session="sess-run"),
            claude_line(mid="msg_r2", ts=T0 + 20, session="sess-run"),
        ])
        write_lines(env.transcript("agent-r.jsonl", slug="-work-demo-app/sess-run/subagents"), [
            claude_line(mid="msg_r3", ts=T0 + 30, session="sess-run", sidechain=True),
        ])
        write_lines(env.transcript("sess-default.jsonl"), [
            owner_line("sess-default", "uuid-a"),
            claude_line(mid="msg_d1", ts=T0 + 15, session="sess-default"),
        ])
        env.scan()
        rows = {r.label: r.totals.requests for r in env.report("account").rows}
        assert rows == {"#2 b@example.com": 3, "#1 a@example.com": 1}

    def test_default_session_spanning_a_switch_follows_the_switch(self, env):
        env.registry = _registry((1, "a@example.com", "uuid-a"), (2, "b@example.com", "uuid-b"))
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        record_activation(env.data, number=2, email="b@example.com", org="", now=T0 + 100)
        # Started on account 1 (its owner), continued after the switch to 2.
        write_lines(env.transcript("sess-long.jsonl"), [
            owner_line("sess-long", "uuid-a"),
            claude_line(mid="msg_1", ts=T0 + 10, session="sess-long"),
            claude_line(mid="msg_2", ts=T0 + 200, session="sess-long"),
        ])
        env.scan()
        rows = {r.label: r.totals.requests for r in env.report("account").rows}
        assert rows == {"#1 a@example.com": 1, "#2 b@example.com": 1}

    def test_session_started_before_the_first_activation_stays_unattributed(self, env):
        env.registry = _registry((2, "b@example.com", "uuid-b"))
        record_activation(env.data, number=2, email="b@example.com", org="", now=T0 + 100)
        write_lines(env.transcript("sess-old.jsonl"), [
            owner_line("sess-old", "uuid-b"),
            claude_line(mid="msg_1", ts=T0 + 10, session="sess-old"),
        ])
        env.scan()
        rows = {r.label: r.totals.requests for r in env.report("account").rows}
        assert rows == {"unattributed": 1}

    def test_unshared_run_profile_history_is_attributed_by_its_directory(self, env):
        env.registry = _registry((1, "a@example.com", "uuid-a"), (2, "b@example.com", "uuid-b"))
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        prof = env.data / "sessions" / "2-b_example.com" / "projects" / "-work-demo-app"
        write_lines(prof / "sess-p.jsonl", [claude_line(mid="msg_p", ts=T0 + 5, session="sess-p")])
        env.scan()
        assert env.accounts() == {"b@example.com|": 1}

    def test_an_account_qswap_does_not_manage_is_labelled_so(self, env):
        env.registry = _registry((1, "a@example.com", "uuid-a"))
        record_activation(env.data, number=None, email="x@example.com", org="", now=T0)
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0 + 5)])
        env.scan()
        rep = env.report("account")
        assert [r.label for r in rep.rows] == ["x@example.com (not managed)"]
        assert rep.rows[0].extra["account"] == {"number": None, "email": "x@example.com"}

    def test_linked_run_profile_history_is_not_scanned_twice(self, env):
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0)])
        prof = env.data / "sessions" / "2-b_example.com"
        prof.mkdir(parents=True)
        (prof / "projects").symlink_to(env.projects)
        stats = env.scan()
        assert stats.files_seen == 1
        assert env.sums()["requests"] == 1


# -- pricing --------------------------------------------------------------------


class TestPricing:
    @pytest.mark.parametrize(
        "model, entry",
        [
            ("claude-opus-5-5", "claude-opus-5-5"),
            ("claude-opus-4-20250514", "claude-opus-4"),
            ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
            ("claude-haiku-4-5@20251001", "claude-haiku-4-5"),
            ("us.anthropic.claude-opus-4-5-20251101-v1:0", "claude-opus-4-5"),
            ("claude-opus-5-5[1m]", "claude-opus-5-5"),
            ("gpt-5-2025-08-07", "gpt-5"),
            ("gpt-6.1-sol", "gpt-6.1-sol"),
        ],
    )
    def test_prefix_match_takes_snapshots_and_the_longest_entry(self, model, entry):
        found = PriceTable().lookup(model)
        assert found is not None and found[0] == entry

    @pytest.mark.parametrize(
        "model", ["claude-opus-4-9", "gpt-5.3-codex-spark", "codex-auto-review", "unknown"]
    )
    def test_other_versions_and_variants_are_not_priced(self, model):
        assert PriceTable().lookup(model) is None

    def test_cost_uses_every_token_class(self):
        _key, price = PriceTable().lookup("claude-opus-5-5")
        # input $4, output $20, cache read $0.20, 5m write $5, 1h write $8.
        assert price.cost(1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000) == pytest.approx(
            4 + 20 + 0.20 + 5 + 8
        )

    def test_fast_mode_uses_the_fast_table(self):
        key, price = PriceTable().lookup("claude-opus-5-5/fast")
        assert price.input == 8 and price.output == 40
        assert PriceTable().lookup("claude-sonnet-5-5/fast") is None

    def test_override_file_adds_replaces_and_removes(self, env):
        env.data.mkdir(parents=True, exist_ok=True)
        (env.data / "pricing.json").write_text(json.dumps({
            "$comment": "USD per million tokens",
            "models": {
                "gpt-5.3-codex-spark": {"input": 1, "output": 2},
                "claude-opus-5-5": {"input": 3, "output": 4, "cache_read": 0.5},
                "claude-sonnet-5-5": None,
                "codex-*": {"input": 7, "output": 8},
            },
        }))
        table = PriceTable.load(env.data)
        assert table.lookup("gpt-5.3-codex-spark")[1].cache_read == 1  # defaults to input
        assert table.lookup("claude-opus-5-5")[1].output == 4
        assert table.lookup("claude-sonnet-5-5") is None
        assert table.lookup("codex-auto-review")[0] == "codex-*"
        assert table.warnings == []

    def test_bad_override_file_warns_and_keeps_the_bundled_table(self, env):
        env.data.mkdir(parents=True, exist_ok=True)
        (env.data / "pricing.json").write_text("{not json")
        table = PriceTable.load(env.data)
        assert table.lookup("claude-opus-5-5") is not None
        assert len(table.warnings) == 1
        (env.data / "pricing.json").write_text(json.dumps({"x-model": {"input": "free"}}))
        table = PriceTable.load(env.data)
        assert table.lookup("x-model") is None
        assert "ignored" in table.warnings[0]

    def test_unknown_model_shows_tokens_and_na_and_is_named(self, env):
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0, model="claude-zeta-9"),
            claude_line(mid="msg_2", ts=T0, model="claude-opus-5-5"),
        ])
        env.scan()
        rep = env.report("model")
        rows = {r.key: r for r in rep.rows}
        assert rows["claude-zeta-9"].totals.cost_or_none() is None
        assert rows["claude-zeta-9"].totals.tokens == 135
        assert rows["claude-opus-5-5"].totals.cost > 0
        assert list(rep.unpriced_models) == ["claude-zeta-9"]
        text = render_text(rep)
        assert "No price for: claude-zeta-9" in text
        assert " n/a" in text
        # A day mixing priced and unpriced models marks its partial cost.
        day = env.report("day")
        assert any("+" in line for line in render_text(day).splitlines() if "2026" in line)

    def test_normalize_model(self):
        assert normalize_model("  Claude-Opus-5-5[1M] ") == "claude-opus-5-5"


# -- report grouping ---------------------------------------------------------------


@pytest.fixture
def mixed(env) -> Env:
    env.registry = _registry((1, "a@example.com", "uuid-a"))
    record_activation(env.data, number=1, email="a@example.com", org="", now=T0 + 3600)
    write_lines(env.transcript("sess-1.jsonl"), [
        claude_line(mid="msg_1", ts=T0, session="sess-1", cwd="/work/demo-app"),
        claude_line(mid="msg_2", ts=T0 + 7200, session="sess-1", model="claude-sonnet-5-5"),
    ])
    write_lines(env.transcript("sess-2.jsonl", slug="-work-other"), [
        claude_line(mid="msg_3", ts=T0 + 86400, session="sess-2", cwd="/work/other"),
    ])
    write_lines(env.rollout(THREAD_A), [
        codex_meta(T0, THREAD_A, cwd="/work/demo-app"),
        codex_turn(T0, "gpt-6.1-sol"),
        codex_record(T0 + 60, THREAD_A, "resp-1"),
    ])
    env.scan()
    return env


class TestReportGrouping:
    def test_by_day_lists_every_day_in_the_range(self, mixed):
        rep = mixed.report("day", since=DAY, until=date.fromtimestamp(T0 + 3 * 86400))
        assert [r.key for r in rep.rows] == [
            date.fromtimestamp(T0 + d * 86400) for d in range(4)
        ] or len(rep.rows) == 4
        assert sum(r.totals.requests for r in rep.rows) == 4
        assert rep.total.requests == 4

    def test_by_model(self, mixed):
        rows = {r.key: r.totals.requests for r in mixed.report("model").rows}
        assert rows == {"claude-opus-5-5": 2, "claude-sonnet-5-5": 1, "gpt-6.1-sol": 1}

    def test_by_account(self, mixed):
        rep = mixed.report("account")
        rows = {r.label: r.totals.requests for r in rep.rows}
        # Claude and Codex usage both appear, so the account names its provider.
        assert rows == {"Claude #1 a@example.com": 2, "unattributed": 2}
        assert rep.unattributed_share == pytest.approx(
            rep.unattributed_tokens / rep.total.tokens
        )

    def test_by_project(self, mixed):
        rows = {r.key: r.totals.requests for r in mixed.report("project").rows}
        assert rows == {"/work/demo-app": 3, "/work/other": 1}

    def test_by_provider(self, mixed):
        rows = {r.label: r.totals.requests for r in mixed.report("provider").rows}
        assert rows == {"Claude Code": 3, "Codex": 1}

    def test_by_session(self, mixed):
        rows = {r.key: r.totals.requests for r in mixed.report("session").rows}
        assert rows == {("claude", "sess-1"): 2, ("claude", "sess-2"): 1, ("codex", THREAD_A): 1}

    def test_provider_filter(self, mixed):
        rep = mixed.report("model", provider="codex")
        assert [r.key for r in rep.rows] == ["gpt-6.1-sol"]

    def test_range_excludes_other_days(self, mixed):
        rep = mixed.report("day", since=DAY, until=DAY)
        assert rep.total.requests == sum(
            1 for ts in (T0, T0 + 7200, T0 + 60) if date.fromtimestamp(ts) == DAY
        )

    def test_json_shape(self, mixed):
        doc = report_to_json(mixed.report("account"))
        assert doc["schemaVersion"] == 1
        assert doc["by"] == "account"
        assert doc["estimate"] is True
        keys = {r["key"] for r in doc["rows"]}
        assert keys == {None, "a@example.com|"}
        row = next(r for r in doc["rows"] if r["key"])
        assert row["account"] == {"number": 1, "email": "a@example.com"}
        for field in ("input", "output", "cacheRead", "cacheWrite5m", "cacheWrite1h",
                      "reasoning", "requests", "totalTokens", "costUsd", "unpricedTokens"):
            assert field in doc["total"]
        assert doc["pricing"]["fetched"] == "2026-11-10"
        assert 0 < doc["unattributedShare"] < 1

    def test_text_rendering(self, mixed):
        text = render_text(mixed.report("day"))
        assert "Token usage" in text and "by day" in text
        assert "Total" in text
        assert "estimates at API list prices" in text
        assert datetime.fromtimestamp(T0).strftime("%Y-%m-%d") in text


# -- text output: 80 columns, labels, empty states ------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

LONG_ALIAS = "the-overnight-batch-runner-for-the-platform-team"
LONG_EMAIL = "someone.with.a.very.long.name@subdomain.example-corporation.com"
LONG_CWD = "/work/" + "deeply/nested/" * 5 + "a-project-with-a-rather-long-name"
LONG_MODEL = "claude-zeta-9-experimental-preview-20261231"


def _plain_lines(text: str) -> list[str]:
    return _ANSI.sub("", text).splitlines()


def _codex_rollout(env: Env, thread: str, ts: float, *, cwd: str = "/work/demo-app") -> None:
    write_lines(env.rollout(thread), [
        codex_meta(ts, thread, cwd=cwd), codex_turn(ts, "gpt-6.1-sol"),
        codex_record(ts + 1, thread, f"resp-{thread[-1]}"),
    ])


@pytest.fixture
def two_providers(env) -> Env:
    """Claude #1 (no alias) and #2 ``work``, Codex #2 ``work``: two accounts
    numbered 2, one per provider, each with usage."""
    env.registry = {"accounts": {
        "1": {"email": "a@example.com", "uuid": "uuid-a", "organizationUuid": ""},
        "2": {"email": "w@example.com", "uuid": "uuid-w", "organizationUuid": "org-w",
              "alias": "work"},
    }}
    env.codex_registry = {"accounts": {
        "2": {"email": "w@example.com", "organizationUuid": "ws-1", "alias": "work"},
    }}
    record_activation(env.data, number=2, email="w@example.com", org="org-w", now=T0)
    record_activation(env.data, number=1, email="a@example.com", org="", now=T0 + 100)
    record_activation(env.data / "codex", number=2, email="w@example.com", org="ws-1", now=T0)
    write_lines(env.transcript(), [
        claude_line(mid="msg_1", ts=T0 + 10), claude_line(mid="msg_2", ts=T0 + 200),
    ])
    _codex_rollout(env, THREAD_A, T0 + 5)
    env.scan()
    return env


class TestTextReport:
    def test_the_footnote_wraps_to_80_columns(self, mixed):
        lines = _plain_lines(render_text(mixed.report("day")))
        assert all(len(line) <= 80 for line in lines)
        at = lines.index(
            "Costs are estimates at API list prices (Anthropic and OpenAI pricing pages,"
        )
        assert lines[at + 1] == "2026-11-10). Subscription usage is not billed this way."

    def test_accounts_name_their_provider_when_both_appear(self, two_providers):
        rep = two_providers.report("account")
        assert sorted(r.label for r in rep.rows) == [
            "Claude #1 a@example.com", "Claude #2 work", "Codex #2 work",
        ]
        text = render_text(rep)
        assert "Account-" not in text
        assert all(len(line) <= 80 for line in _plain_lines(text))
        rows = {r["label"]: r for r in report_to_json(rep)["rows"]}
        assert rows["Codex #2 work"]["provider"] == "codex"
        assert rows["Codex #2 work"]["key"] == "w@example.com|ws-1"
        assert rows["Claude #2 work"]["provider"] == "claude"
        assert rows["Claude #2 work"]["account"] == {"number": 2, "email": "w@example.com"}

    def test_one_providers_view_leaves_the_provider_out(self, two_providers):
        assert [r.label for r in two_providers.report("account", provider="codex").rows] == [
            "#2 work"
        ]
        assert sorted(
            r.label for r in two_providers.report("account", provider="claude").rows
        ) == ["#1 a@example.com", "#2 work"]

    def test_a_key_both_providers_use_stays_two_rows(self, env):
        env.registry = _registry((1, "a@example.com", "uuid-a"))
        env.codex_registry = {"accounts": {"1": {"email": "a@example.com"}}}
        record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
        record_activation(env.data / "codex", number=1, email="a@example.com", org="", now=T0)
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0 + 10)])
        _codex_rollout(env, THREAD_A, T0 + 5)
        env.scan()
        rep = env.report("account")
        assert {r.label: r.totals.requests for r in rep.rows} == {
            "Claude #1 a@example.com": 1, "Codex #1 a@example.com": 1,
        }
        doc = report_to_json(rep)
        assert sorted((r["provider"], r["key"]) for r in doc["rows"]) == [
            ("claude", "a@example.com|"), ("codex", "a@example.com|"),
        ]

    def test_unattributed_usage_of_both_providers_is_one_row(self, env):
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0 + 10)])
        _codex_rollout(env, THREAD_A, T0 + 5)
        env.scan()
        rep = env.report("account")
        assert [(r.label, r.totals.requests) for r in rep.rows] == [("unattributed", 2)]
        assert report_to_json(rep)["rows"][0]["provider"] is None

    def test_the_desktop_app_and_an_unmanaged_login_are_labelled_clearly(self, env):
        record_activation(env.data, number=None, email="x@example.com", org="", now=T0)
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0 + 5),
            claude_line(mid="msg_2", ts=T0 + 6, entrypoint="claude-desktop"),
        ])
        _codex_rollout(env, THREAD_A, T0 + 5)
        env.scan()
        labels = {r.label for r in env.report("account").rows}
        assert labels == {"Claude x@example.com (not managed)", "Claude desktop app",
                          "unattributed"}

    def test_a_long_unmanaged_email_is_cut_but_keeps_not_managed(self, env):
        record_activation(env.data, number=None, email=LONG_EMAIL, org="", now=T0)
        write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0 + 5)])
        env.scan()
        lines = _plain_lines(render_text(env.report("account")))
        row = next(line for line in lines if "not managed" in line)
        assert len(row) <= 80
        assert "…" in row and "someone.with" in row
        assert row.split("  ")[1].endswith(" (not managed)")

    def test_session_rows_fit_and_cut_the_project(self, env):
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0, session="sess-long-project", cwd=LONG_CWD),
            claude_line(mid="msg_2", ts=T0, session="sess-short", cwd="/work/demo-app"),
        ])
        env.scan()
        lines = _plain_lines(render_text(env.report("session")))
        assert all(len(line) <= 80 for line in lines)
        assert lines[2].startswith("  Session   Project ")
        long_row = next(line for line in lines if line.startswith("  sess-lon"))
        assert long_row.startswith("  sess-lon  …")
        assert "  sess-sho  /work/demo-app " in "\n".join(lines)

    def test_a_cut_path_starts_at_a_directory_when_one_is_in_reach(self):
        row = Row(key="p", label="/work/clients/northwind/checkout-api", cut="start")
        assert fit_label(row, 20) == "…/checkout-api"
        assert fit_label(row, 80) == row.label
        session = Row(key="s", label="80f2eb9b  /work/a-very-long-project-name",
                      head="80f2eb9b  ", cut="start")
        cut = fit_label(session, 20)
        assert cut.startswith("80f2eb9b  …") and len(cut) == 20

    def test_a_cut_account_label_keeps_its_start_and_tail(self):
        row = Row(key="k", label=f"Claude {LONG_EMAIL} (not managed)", tail=" (not managed)")
        cut = fit_label(row, 27)
        assert len(cut) == 27
        assert cut == "Claude someo… (not managed)"
        assert fit_label(Row(key="k", label="Claude #2 " + LONG_ALIAS), 27) == (
            "Claude #2 the-overnight-ba…"
        )

    @pytest.mark.parametrize("by", ["day", "model", "account", "project", "provider", "session"])
    def test_every_line_fits_80_columns_with_long_names(self, env, by):
        env.registry = {"accounts": {
            "1": {"email": LONG_EMAIL, "uuid": "uuid-l", "organizationUuid": "",
                  "alias": LONG_ALIAS},
            "2": {"email": "b" * 60 + "@example.com", "uuid": "uuid-b",
                  "organizationUuid": ""},
        }}
        env.codex_registry = {"accounts": {"1": {"email": LONG_EMAIL, "alias": LONG_ALIAS}}}
        record_activation(env.data, number=1, email=LONG_EMAIL, org="", now=T0)
        record_activation(env.data, number=2, email="b" * 60 + "@example.com", org="",
                          now=T0 + 50)
        record_activation(env.data, number=None, email="c" * 70 + "@example.com", org="",
                          now=T0 + 100)
        record_activation(env.data / "codex", number=1, email=LONG_EMAIL, org="", now=T0)
        write_lines(env.transcript(), [
            claude_line(mid="msg_1", ts=T0 + 10, cwd=LONG_CWD, session="s" * 40),
            claude_line(mid="msg_2", ts=T0 + 60, model=LONG_MODEL, cwd=LONG_CWD + "-2"),
            claude_line(mid="msg_3", ts=T0 + 110, cwd="/work/demo-app"),
        ])
        _codex_rollout(env, THREAD_A, T0 + 5, cwd=LONG_CWD)
        env.scan()
        text = render_text(
            env.report(by),
            notes=["A note that is long enough to need wrapping at eighty columns, "
                   "as the scan's notes about skipped or unreadable files are."],
            last_line="1,234 older session files are not in the ledger yet. Use a wider "
                      "--days or --since to include the ones you need, or --all for all.",
        )
        lines = _plain_lines(text)
        assert lines[0].startswith("Token usage")
        too_wide = [line for line in lines if len(line) > 80]
        assert too_wide == []
        assert any("No price for: " + LONG_MODEL in line for line in lines)


class TestEmptyReport:
    """A report with no usage is a sentence and a next step, not a zero table."""

    def test_a_range_with_no_usage_says_so_and_suggests_more_days(self, mixed):
        since, until = date(2026, 1, 1), date(2026, 1, 7)
        rep = mixed.report("day", since=since, until=until)
        assert rep.empty
        assert render_text(rep) == (
            "No usage between 2026-01-01 and 2026-01-07.\n"
            "To look further back: qswap usage --days 30"
        )

    def test_the_hint_keeps_the_provider_and_grouping(self, mixed):
        rep = mixed.report("account", since=date(2026, 1, 1), until=date(2026, 3, 1),
                           provider="codex")
        assert render_text(rep) == (
            "No Codex usage between 2026-01-01 and 2026-03-01.\n"
            "To look further back: qswap usage --provider codex --days 90 --by account"
        )

    def test_one_day_and_a_very_long_range(self, mixed):
        day = date(2026, 1, 1)
        assert render_text(mixed.report("day", since=day, until=day)).splitlines()[0] == (
            "No usage on 2026-01-01."
        )
        long = mixed.report("day", since=date(2024, 1, 1), until=date(2025, 6, 1))
        assert render_text(long) == "No usage between 2024-01-01 and 2025-06-01."

    def test_notes_and_the_last_line_still_show(self, mixed):
        rep = mixed.report("day", since=date(2026, 1, 1), until=date(2026, 1, 7))
        text = render_text(rep, notes=["Skipped 2 compressed Codex session files."],
                           last_line="1 older session file is not in the ledger yet.")
        assert text.splitlines()[-2:] == [
            "Skipped 2 compressed Codex session files.",
            "1 older session file is not in the ledger yet.",
        ]

    def test_no_session_files_names_the_directories(self, env):
        rep = env.report("day")
        rep.searched_dirs = ("~/.claude/projects", "~/.codex/sessions",
                             "~/.codex/archived_sessions")
        lines = render_text(rep).splitlines()
        assert lines == [
            "No Claude Code or Codex session files were found in ~/.claude/projects,",
            "~/.codex/sessions or ~/.codex/archived_sessions.",
            "Use Claude Code or Codex, then run qswap usage again.",
        ]

    def test_an_empty_ledger_without_a_scan(self, env):
        rep = env.report("day", since=DAY, until=DAY)
        rep.unscanned = True
        assert render_text(rep).splitlines() == [
            f"No usage on {DAY.isoformat()} in the ledger.",
            "Run the command again without --no-scan to read new session files.",
        ]

    def test_json_keeps_its_shape(self, env, mixed):
        empty = report_to_json(mixed.report("day", since=date(2026, 1, 1),
                                            until=date(2026, 1, 7)))
        full = report_to_json(mixed.report("day"))
        assert set(empty) == set(full)
        assert len(empty["rows"]) == 7 and empty["total"]["requests"] == 0


def test_a_switch_during_a_long_scan_is_picked_up(env, monkeypatch):
    """The activation log is re-read between batches."""
    for i in range(4):
        write_lines(env.transcript(f"sess-{i}.jsonl"), [
            claude_line(mid=f"msg_{i}", ts=T0 + 100 * i, session=f"sess-{i}")
        ])
    record_activation(env.data, number=1, email="a@example.com", org="", now=T0)
    real = readers.parse_claude_line
    seen = {"n": 0}

    def switch_midway(raw):
        seen["n"] += 1
        if seen["n"] == 1:  # after the scanner loaded the log
            record_activation(env.data, number=2, email="b@example.com", org="", now=T0 + 150)
        return real(raw)

    monkeypatch.setattr(scanner, "parse_claude_line", switch_midway)
    env.scan(batch_events=1)
    assert env.accounts() == {"a@example.com|": 2, "b@example.com|": 2}


def test_a_file_rewritten_in_place_to_a_longer_one_is_read_again(env):
    path = write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0)])
    env.scan()
    # Same inode, longer content whose old offset falls mid-line.
    with open(path, "w") as f:
        f.write(claude_line(mid="msg_2", ts=T0 + 1, cwd="/work/a-much-longer-project-path") + "\n")
        f.write(claude_line(mid="msg_3", ts=T0 + 2) + "\n")
    env.scan()
    assert env.sums()["requests"] == 3


def test_interrupted_codex_scan_resumes_with_its_reader_state(env, monkeypatch):
    """A cursor saved mid-file carries the model and project read before it."""
    lines = [codex_meta(T0, THREAD_A, cwd="/work/demo-app"), codex_turn(T0, "gpt-5.4")]
    lines += [codex_record(T0 + i, THREAD_A, f"resp-{i}", inp=10, cached=0, out=1) for i in range(12)]
    write_lines(env.rollout(THREAD_A), lines)
    real = readers.parse_codex_line
    calls = {"n": 0}

    def flaky(raw, state):
        calls["n"] += 1
        if calls["n"] == 9:
            raise KeyboardInterrupt
        return real(raw, state)

    monkeypatch.setattr(scanner, "parse_codex_line", flaky)
    with pytest.raises(KeyboardInterrupt):
        env.scan(batch_events=2)
    assert 0 < env.sums("codex")["requests"] < 12
    monkeypatch.setattr(scanner, "parse_codex_line", real)
    env.scan(batch_events=2)
    store = env.store()
    rows = store.conn.execute(
        "SELECT model, project, sum(requests) FROM buckets GROUP BY 1, 2"
    ).fetchall()
    store.close()
    assert rows == [("gpt-5.4", "/work/demo-app", 12)]


def test_many_rollouts_are_statted_in_parallel(env):
    threads = [f"00000000-0000-4000-8000-{i:012d}" for i in range(70)]
    for t in threads:
        write_lines(env.rollout(t), [codex_meta(T0, t), codex_turn(T0), codex_record(T0, t, "r")])
    stats = env.scan()
    assert (stats.files_seen, stats.events) == (70, 70)
    assert env.scan().files_read == 0


# -- scan window ----------------------------------------------------------------

D = 86_400
THREAD_D = "00000000-0000-4000-8000-00000000000d"
THREAD_E = "00000000-0000-4000-8000-00000000000e"
NARROW = DAY  # a report on DAY and the day after
WIDE = date.fromordinal(DAY.toordinal() - 40)


def _aged(path: Path, mtime: float) -> Path:
    os.utime(path, (mtime, mtime))
    return path


def _window(day: date) -> float:
    """The first event time a report from ``day`` needs, as run_usage computes it."""
    return day_bounds(day, day)[0] * 3600


def _cursor_keys(env) -> set[str]:
    store = env.store()
    try:
        return set(store.cursors("claude")) | set(store.cursors("codex"))
    finally:
        store.close()


def _sessions(env) -> set[str]:
    store = env.store()
    try:
        return {r[0] for r in store.conn.execute("SELECT DISTINCT session FROM buckets")}
    finally:
        store.close()


def _buckets(env) -> list[tuple]:
    """Every bucket, summed over sessions (see the window equivalence test)."""
    store = env.store()
    try:
        return store.conn.execute(
            "SELECT provider, hour, account, model, project, sum(input), sum(output), "
            "sum(cache_read), sum(cache_write_5m), sum(cache_write_1h), sum(reasoning), "
            "sum(requests) FROM buckets GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5"
        ).fetchall()
    finally:
        store.close()


def _tokens(totals) -> dict:
    doc = totals.to_json()
    doc.pop("costUsd")
    return doc


def _rollout_lines(thread: str, ts: float, *responses: str, model: str = "gpt-6.1-sol") -> list[str]:
    return [codex_meta(ts, thread), codex_turn(ts, model)] + [
        codex_record(ts + 1 + i, thread, r) for i, r in enumerate(responses)
    ]


def _history(env) -> None:
    """Session files of many ages, with the cross-file cases the window must
    survive: a Claude Code session resumed twice, each new file replaying
    earlier responses with their original timestamps; a sub-agent transcript;
    Codex threads live, archived and compressed; and an old-format fork whose
    child is recent and replays the totals of a parent that is not."""
    record_activation(env.data, number=1, email="a@example.com", org="", now=T0 - 10 * D)
    _aged(write_lines(env.transcript("sess-1.jsonl"), [
        claude_line(mid="msg_1", ts=T0 - 20 * D, session="sess-1", out=3, stop=None),
        claude_line(mid="msg_1", ts=T0 - 20 * D + 1, session="sess-1", out=40),
        claude_line(mid="msg_2", ts=T0 - 20 * D + 60, session="sess-1"),
    ]), T0 - 20 * D + 60)
    _aged(write_lines(env.transcript("sess-2.jsonl"), [
        claude_line(mid="msg_1", ts=T0 - 20 * D + 1, session="sess-2", out=40),
        claude_line(mid="msg_2", ts=T0 - 20 * D + 60, session="sess-2"),
        claude_line(mid="msg_3", ts=T0 - 3 * D, session="sess-2", model="claude-sonnet-5-5"),
    ]), T0 - 3 * D)
    _aged(write_lines(env.transcript("sess-3.jsonl"), [
        claude_line(mid="msg_3", ts=T0 - 3 * D, session="sess-3", model="claude-sonnet-5-5"),
        claude_line(mid="msg_4", ts=T0 + 600, session="sess-3", out=3, stop=None),
        claude_line(mid="msg_4", ts=T0 + 601, session="sess-3", out=90),
    ]), T0 + 601)
    sub = env.transcript("agent-1.jsonl", slug="-work-demo-app/sess-2/subagents")
    _aged(write_lines(sub, [
        claude_line(mid="msg_sub", ts=T0 - 9 * D, session="sess-2", sidechain=True),
    ]), T0 - 9 * D)
    _aged(write_lines(env.rollout(THREAD_A), _rollout_lines(THREAD_A, T0, "a1", "a2")), T0 + 2)
    _aged(
        write_lines(env.rollout(THREAD_B, archived=True),
                    _rollout_lines(THREAD_B, T0 - 15 * D, "b1", model="gpt-5.4")),
        T0 - 15 * D + 1,
    )
    lines_c = _rollout_lines(THREAD_C, T0 - 5 * D, "c1")
    if scanner.ZSTD_AVAILABLE:
        zst = env.rollout(THREAD_C, zst=True)
        zst.write_bytes(scanner._zstd.compress(("\n".join(lines_c) + "\n").encode()))
        _aged(zst, T0 - 5 * D + 1)
    else:
        _aged(write_lines(env.rollout(THREAD_C), lines_c), T0 - 5 * D + 1)
    _aged(write_lines(env.rollout(THREAD_D), [
        codex_meta(T0 - 12 * D, THREAD_D, version="0.40.0", ordinal=None),
        codex_turn(T0 - 12 * D, "gpt-5.4"),
        codex_count(T0 - 12 * D + 1, (100, 0, 10), (100, 0, 10)),
        codex_count(T0 - 12 * D + 2, (300, 0, 30), (200, 0, 20)),
    ]), T0 - 12 * D + 2)
    _aged(write_lines(env.rollout(THREAD_E), [
        codex_meta(T0 - 3600, THREAD_E, session=THREAD_D, version="0.40.0", ordinal=None),
        codex_meta(T0 - 3600, THREAD_D, version="0.40.0", ordinal=None),
        codex_count(T0 - 3599, (100, 0, 10), (100, 0, 10)),
        codex_count(T0 - 3598, (300, 0, 30), (200, 0, 20)),
        codex_turn(T0 - 3597, "gpt-5.4"),
        codex_count(T0 - 3596, (360, 0, 36), (60, 0, 6)),
    ]), T0 - 3596)


class TestScanWindow:
    @pytest.mark.parametrize("wide", ["wider window", "all"])
    def test_a_narrow_scan_then_a_wider_one_matches_a_wide_scan_from_scratch(self, env, wide):
        _history(env)
        until = date.fromtimestamp(T0 + D)
        wide_since = _window(WIDE) if wide == "wider window" else None

        narrow = env.scan(since=_window(NARROW))
        # sess-3, the live thread and the fork child are read; six are not.
        assert (narrow.files_skipped, narrow.older_not_in_ledger) == (6, 6)
        narrow_window = env.report(since=NARROW, until=until).total
        env.scan(since=wide_since)
        stepwise = (_buckets(env), env.sums(), env.report(since=WIDE, until=until).total)

        store = env.store()
        store.reset()
        store.close()
        env.scan(since=wide_since)
        scratch = (_buckets(env), env.sums(), env.report(since=WIDE, until=until).total)

        # Claude: msg_1..4 and the sub-agent; Codex: a1, a2, b1, c1, and three
        # token_count deltas (two in the parent, one of the child's own).
        assert scratch[1]["requests"] == 12
        assert stepwise[0] == scratch[0]
        assert stepwise[1] == scratch[1]
        assert _tokens(stepwise[2]) == _tokens(scratch[2])
        assert stepwise[2].cost == pytest.approx(scratch[2].cost)
        # The narrow scan alone already held every event of its own window.
        assert _tokens(narrow_window) == _tokens(env.report(since=NARROW, until=until).total)
        assert narrow_window.requests > 0

    def test_left_out_files_get_no_cursor_and_a_wider_scan_reads_them(self, env):
        _aged(write_lines(env.transcript("sess-old.jsonl"), [
            claude_line(mid="msg_old", ts=T0 - 10 * D, session="sess-old"),
        ]), T0 - 10 * D)
        _aged(write_lines(env.rollout(THREAD_A), _rollout_lines(THREAD_A, T0 - 10 * D, "r")),
              T0 - 10 * D + 1)
        new = _aged(write_lines(env.transcript("sess-new.jsonl"), [
            claude_line(mid="msg_new", ts=T0, session="sess-new"),
        ]), T0 + 1)

        stats = env.scan(since=_window(NARROW))
        assert env.sums()["requests"] == 1
        assert (stats.files_seen, stats.files_read) == (1, 1)
        assert (stats.files_skipped, stats.older_not_in_ledger) == (2, 2)
        assert stats.modified_since == _window(NARROW) - scanner.MTIME_MARGIN
        assert _cursor_keys(env) == {str(new)}

        stats = env.scan(since=_window(WIDE))
        assert env.sums()["requests"] == 3
        assert (stats.files_read, stats.files_skipped) == (2, 0)
        stats = env.scan(since=_window(NARROW))
        assert (stats.files_read, stats.files_skipped, stats.older_not_in_ledger) == (0, 2, 0)

    def test_the_margin_is_one_day_before_the_window(self, env):
        assert scanner.MTIME_MARGIN == 86_400
        since = _window(NARROW)
        cutoff = since - scanner.MTIME_MARGIN
        _aged(write_lines(env.transcript("sess-at.jsonl"), [
            claude_line(mid="msg_at", ts=cutoff, session="sess-at"),
        ]), cutoff)
        _aged(write_lines(env.transcript("sess-before.jsonl"), [
            claude_line(mid="msg_before", ts=cutoff - 1, session="sess-before"),
        ]), cutoff - 1)
        stats = env.scan(since=since)
        assert (stats.files_read, stats.files_skipped) == (1, 1)
        assert _sessions(env) == {"sess-at"}

    def test_files_are_read_newest_first_across_both_providers(self, env, monkeypatch):
        a = _aged(write_lines(env.transcript("sess-a.jsonl"), [
            claude_line(mid="msg_a", ts=T0 - 3 * D, session="sess-a"),
        ]), T0 - 3 * D)
        b = _aged(write_lines(env.rollout(THREAD_A), _rollout_lines(THREAD_A, T0 - 2 * D, "r")),
                  T0 - 2 * D)
        c = _aged(write_lines(env.transcript("sess-c.jsonl"), [
            claude_line(mid="msg_c", ts=T0 - D, session="sess-c"),
        ]), T0 - D)
        d = _aged(write_lines(env.rollout(THREAD_B), _rollout_lines(THREAD_B, T0, "r")), T0)
        order: list[str] = []
        claude_file, codex_thread = Scanner._claude_file, Scanner._codex_thread

        def record_claude(self, path, *args):
            order.append(path)
            return claude_file(self, path, *args)

        def record_codex(self, key, path, *args):
            order.append(path)
            return codex_thread(self, key, path, *args)

        monkeypatch.setattr(Scanner, "_claude_file", record_claude)
        monkeypatch.setattr(Scanner, "_codex_thread", record_codex)
        env.scan()
        assert order == [str(d), str(c), str(b), str(a)]

    def test_an_interrupted_first_scan_keeps_the_newest_usage(self, env, monkeypatch):
        for i in range(4):
            _aged(write_lines(env.transcript(f"sess-{i}.jsonl"), [
                claude_line(mid=f"msg_{i}", ts=T0 - i * D, session=f"sess-{i}"),
            ]), T0 - i * D)
        real = readers.parse_claude_line
        calls = {"n": 0}

        def flaky(raw):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return real(raw)

        monkeypatch.setattr(scanner, "parse_claude_line", flaky)
        with pytest.raises(KeyboardInterrupt):
            env.scan(batch_events=1)
        assert _sessions(env) == {"sess-0", "sess-1"}

    @pytest.mark.skipif(os.utime not in os.supports_follow_symlinks, reason="needs lutimes")
    def test_a_symlinks_age_is_its_targets(self, env):
        share = env.home / "share"
        old = _aged(write_lines(share / rollout_name(THREAD_A),
                                _rollout_lines(THREAD_A, T0 - 10 * D, "old")), T0 - 10 * D)
        new = _aged(write_lines(share / rollout_name(THREAD_B),
                                _rollout_lines(THREAD_B, T0, "new")), T0 + 1)
        env.rollout(THREAD_A).parent.mkdir(parents=True)
        env.rollout(THREAD_A).symlink_to(old)  # a new link to an old file
        env.rollout(THREAD_B).symlink_to(new)
        os.utime(env.rollout(THREAD_B), (T0 - 30 * D, T0 - 30 * D), follow_symlinks=False)
        env.rollout(THREAD_C).symlink_to(share / "unmounted" / rollout_name(THREAD_C))
        stats = env.scan(since=_window(NARROW))
        assert _sessions(env) == {THREAD_B}
        assert env.sums("codex")["requests"] == 1
        assert (stats.files_read, stats.files_skipped, stats.missing) == (1, 1, 1)

    def test_a_thread_archived_while_old_still_counts_as_read(self, env):
        live = _aged(write_lines(env.rollout(THREAD_A), _rollout_lines(THREAD_A, T0 - 10 * D, "r")),
                     T0 - 10 * D)
        env.scan()
        archived = env.rollout(THREAD_A, archived=True)
        archived.parent.mkdir(parents=True)
        os.replace(live, archived)  # a rename keeps the inode and the mtime
        stats = env.scan(since=_window(NARROW))
        assert (stats.files_read, stats.files_skipped, stats.older_not_in_ledger) == (0, 1, 0)

    @pytest.mark.skipif(not scanner.ZSTD_AVAILABLE, reason="needs zstd support")
    def test_a_thread_compressed_while_old(self, env):
        live = _aged(write_lines(env.rollout(THREAD_A), _rollout_lines(THREAD_A, T0 - 10 * D, "r")),
                     T0 - 10 * D)
        env.scan()
        zst = env.rollout(THREAD_A, zst=True)
        zst.write_bytes(scanner._zstd.compress(live.read_bytes()))
        live.unlink()
        # Compressed keeping the file's time (the zstd tool's default): this
        # copy is no newer than what was read, so it is in the ledger.
        _aged(zst, T0 - 10 * D)
        stats = env.scan(since=_window(NARROW))
        assert (stats.files_skipped, stats.older_not_in_ledger) == (1, 0)
        # Compressed with a later time, still before the window: it could hold
        # more than was read, so it counts as not in the ledger until a wider
        # scan resumes it from the cursor.
        _aged(zst, T0 - 5 * D)
        stats = env.scan(since=_window(NARROW))
        assert (stats.files_skipped, stats.older_not_in_ledger) == (1, 1)
        stats = env.scan(since=_window(WIDE))
        assert (stats.files_read, stats.bytes_read) == (1, 0)
        assert env.sums("codex")["requests"] == 1
        stats = env.scan(since=_window(NARROW))
        assert (stats.files_skipped, stats.older_not_in_ledger) == (1, 0)

    def test_lines_behind_an_mtime_set_back_are_flagged_and_read_by_a_full_scan(self, env):
        """How the rule can miss events: a file written after its last scan and
        then given an older mtime (a tool that copies times from an older
        copy). The narrow scan cannot see the new line, but the file no longer
        matches its cursor, so it is reported as not in the ledger."""
        path = _aged(write_lines(env.transcript(), [claude_line(mid="msg_1", ts=T0 - 10 * D)]),
                     T0 - 10 * D)
        env.scan()
        write_lines(path, [claude_line(mid="msg_2", ts=T0 + 60)], append=True)
        _aged(path, T0 - 10 * D)
        stats = env.scan(since=_window(NARROW))
        assert env.sums()["requests"] == 1
        assert stats.older_not_in_ledger == 1
        env.scan()
        assert env.sums()["requests"] == 2


# --- saved-key lookups: one walk of the log, not one per bucket -------------------------------


def _reference_saved_key_owner(log, account, hour, session_start):
    """ActivationLog.saved_key_owner as it was before it kept its periods:
    two walks of the whole log per call. The behaviour to keep."""
    if session_start is None:
        return None
    lo, hi = hour * 3600, (hour + 1) * 3600
    acts = log.activations
    hows, starts = [], []
    for i, a in enumerate(acts):
        how, start = a.how, i
        if not how and i > 0 and acts[i - 1].account == a.account:
            how, start = hows[i - 1], starts[i - 1]
        hows.append(how)
        starts.append(start)
    in_force = [
        i for i, a in enumerate(acts)
        if a.account == account and a.ts < hi and (i + 1 == len(acts) or acts[i + 1].ts > lo)
    ]
    if not in_force or any(hows[i] != "saved-key" for i in in_force):
        return None
    first = min(starts[i] for i in in_force)
    if session_start >= acts[first].ts:
        return None
    before = acts[first - 1].account if first > 0 else ""
    return None if before == account else before


_H = 490_000  # an hour number (2025-11-24); every line below is relative to it
_A, _B, _K = "a@example.com|", "b@example.com|", "key5@token.local|"


def _saved_key_log():
    from ai_quota_swap.ledger.activations import Activation

    base = _H * 3600
    lines = [
        (base - 7200, _A, "login"), (base - 1800, _K, "helper"),
        (base, _K, "saved-key"), (base, _K, ""),  # on the hour, and a tie
        (base + 1200, _A, "login"),
        (base + 3600, _K, "saved-key"), (base + 4500, _K, ""),
        (base + 7210, _B, "login"), (base + 9000, _K, "saved-key"),
        (base + 10800, _K, "helper"), (base + 11400, _K, "saved-key"),
        (base + 14399, _A, ""), (base + 18000, _K, "saved-key"), (base + 18000, _B, "saved-key"),
        (base + 21605, _K, ""), (base + 25200, _K, "saved-key"),
    ]
    return ActivationLog([Activation(ts, acc, None, "switch", how) for ts, acc, how in lines])


def test_saved_key_owner_is_what_it_was_on_every_hour_account_and_session_start():
    log = _saved_key_log()
    times = sorted({a.ts for a in log.activations} | {(_H + k) * 3600 for k in range(-3, 10)})
    starts = [None, float("-inf")] + [t + d for t in times for d in (-1, -0.5, 0, 0.5, 1)]
    checked = moved = 0
    for account in (_A, _B, _K, "nobody@example.com|"):
        for hour in range(_H - 3, _H + 10):
            for start in starts:
                want = _reference_saved_key_owner(log, account, hour, start)
                assert log.saved_key_owner(account, hour, start) == want, (account, hour, start)
                checked += 1
                moved += want is not None
    assert checked > 5000 and moved > 50  # the grid reaches the rule, not only its refusals


def test_hows_and_the_login_under_a_key_are_what_they_were():
    log = _saved_key_log()
    acts = log.activations
    hows = []
    for i, a in enumerate(acts):  # the walk _hows did on every call
        how = a.how
        if not how and i > 0 and acts[i - 1].account == a.account:
            how = hows[i - 1]
        hows.append(how)
    assert log._hows() == hows

    def underneath(ts):  # helper_underneath over that walk
        i = bisect.bisect_right([a.ts for a in acts], ts) - 1
        if i < 0 or hows[i] != "helper":
            return None
        while i >= 0 and hows[i] == "helper":
            i -= 1
        return acts[i].account if i >= 0 else None

    times = sorted({a.ts for a in acts} | {(_H + k) * 3600 for k in range(-3, 10)})
    found = [log.helper_underneath(t + d) for t in times for d in (-1, 0, 1)]
    assert found == [underneath(t + d) for t in times for d in (-1, 0, 1)]
    assert _A in found and _K in found  # the walk back reaches both kinds of line


class _CountingLines(list):
    """A log's lines that count every line read, by index or by walking."""

    visits = 0

    def __getitem__(self, i):
        out = super().__getitem__(i)
        _CountingLines.visits += len(out) if isinstance(i, slice) else 1
        return out

    def __iter__(self):
        for line in super().__iter__():
            _CountingLines.visits += 1
            yield line


def test_a_report_walks_the_activation_log_once_not_once_per_bucket(tmp_path):
    # A week of hourly buckets on a log with saved-key lines and `qswap run`
    # sessions: the attribution reads each line a few times in all, never
    # the whole log for every bucket (the menu bar's chart reads this on
    # every ledger change).
    from ai_quota_swap.ledger.activations import Activation
    from ai_quota_swap.ledger.report import Accounts, attribute, day_bounds

    today = date(2026, 11, 10)
    since_hour, until_hour = day_bounds(date(2026, 11, 4), today)
    accounts = {1: "a@example.com", 2: "b@example.com", 5: "key5@token.local"}
    lines = [
        Activation((since_hour - 48) * 3600 + i * 1700.0, f"{accounts[n]}|", n, "switch",
                   "saved-key" if n == 5 and i % 2 else "helper" if n == 5 else "login")
        for i, n in enumerate([1, 5, 2, 5, 1, 2] * 70)
    ]
    log = ActivationLog(lines)
    store = LedgerStore(tmp_path / DB_FILENAME)
    store.begin()
    buckets = [
        ("claude", h, f"{accounts[n]}|", "claude-opus-5", "p", f"s{h // 3}-{n}", 1000, 0, 0, 0, 0, 0, 1)
        for h in range(since_hour, until_hour) for n in (1, 2, 5)
    ]
    store.add_buckets(buckets)
    store.note_sessions([("claude", b[5], "uuid-2|" if b[5].endswith("-1") else None, b[1] * 3600 + 60.0, 1)
                         for b in buckets])
    store.commit()
    registry = {"accounts": {str(n): {"email": e, "uuid": f"uuid-{n}", "organizationUuid": ""}
                             for n, e in accounts.items()}}
    log.activations = _CountingLines(log.activations)
    _CountingLines.visits = 0
    try:
        out = attribute(store, since_hour=since_hour, until_hour=until_hour, provider=None,
                        accounts=Accounts({"claude": registry}, {"claude": log}), logs={"claude": log})
    finally:
        store.close()
    assert out.approximate and out.profile_sessions  # both rules were reached
    assert len(buckets) > 400 and len(log) > 400
    assert _CountingLines.visits <= 4 * len(log) + 8 * len(buckets), _CountingLines.visits


def test_a_log_line_whose_time_is_not_a_finite_number_is_skipped(tmp_path):
    # JSON readers accept NaN and Infinity, and an integer too large for a
    # float. qswap never writes them (a hand-edited line), and such a line
    # is skipped like any other unreadable one. Kept, a NaN compares false
    # with every time, so the log's binary search could take a saved-key
    # line as in force for any hour and move that hour's usage of the key.
    from ai_quota_swap import api_spend
    from ai_quota_swap.ledger.activations import parse_ts
    from ai_quota_swap.ledger.report import _in_saved_key_period

    (tmp_path / "activations.jsonl").write_text(
        '{"ts":NaN,"number":5,"email":"key5@token.local","org":"","source":"switch","how":"saved-key"}\n'
        '{"ts":"2025-11-24T17:33:25.584Z","number":2,"email":"omar@example.com","org":"","source":"switch",'
        '"how":"login"}\n'
    )
    log = ActivationLog.load(tmp_path)
    assert len(log) == 1 and log.first_ts == parse_ts("2025-11-24T17:33:25.584Z")
    hour = int(log.first_ts // 3600)
    assert log.saved_key_owner("key5@token.local|", hour, float("-inf")) is None
    assert not _in_saved_key_period(log, "key5@token.local|", hour)
    assert not log.saved_key_periods()
    for ts in ("Infinity", "-Infinity", "1" + "0" * 400):
        (tmp_path / "activations.jsonl").write_text(
            f'{{"ts":{ts},"number":1,"email":"maya@example.com","org":"","source":"switch"}}\n')
        assert len(ActivationLog.load(tmp_path)) == 0  # and no OverflowError
    assert [parse_ts(v) for v in (float("nan"), float("inf"), -float("inf"), 10 ** 400)] == [None] * 4
    assert parse_ts(1762000000) == 1762000000.0 and parse_ts(1762000000.5) == 1762000000.5
    # The other readers of these times want the same: a credit whose start
    # is not a time is no credit, rather than one that raises later.
    assert api_spend.credit_from_record({"credit": {"amount": 25, "since": float("nan")}}) is None
    assert api_spend.credit_counted_from({"since": float("inf")}, None) == ""
