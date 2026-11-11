"""The incremental scanner: transcript files in, buckets out.

A scan walks the roots, stats every file once (in parallel), reads each file
from its stored cursor, turns lines into events (readers.py), and writes them
in batches. Each batch is one SQLite transaction that adds to the buckets,
records the dedupe keys, and moves the cursors of the files it read, so an
interrupted scan (Ctrl-C, a crash) loses at most the batch in flight and the
next scan resumes exactly where the last committed batch stopped.

Scan window
    A report over ``[since, until]`` only needs files that can hold an event
    at or after ``since``. A line is written when its event happens, so a
    file's modification time is at or after every timestamp in it: a file
    last changed more than ``MTIME_MARGIN`` before ``since`` is left alone.
    It is not opened and gets no cursor, so a later, wider scan reads it.
    Files are read newest first, so an interrupted first scan keeps the most
    recent usage, and the files a narrow scan reads are exactly the newest
    part of what a wide scan reads, in the same order.

Roots come from ``paths.py``: nothing here spells a home directory.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ai_quota_swap import paths
from ai_quota_swap.ledger.activations import ActivationLog, identity_key
from ai_quota_swap.ledger.readers import (
    Event,
    OwnerNote,
    new_codex_state,
    parse_claude_line,
    parse_codex_line,
)
from ai_quota_swap.ledger.store import LedgerStore

try:  # Python 3.14+
    from compression import zstd as _zstd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the interpreter
    try:
        import zstandard as _zstd  # type: ignore[import-not-found,no-redef]
    except ImportError:
        _zstd = None

ZSTD_AVAILABLE = _zstd is not None

BATCH_EVENTS = 20_000
BATCH_BYTES = 64 * 1024 * 1024
_CHUNK = 1024 * 1024

# How long before the first event a report needs a file may have been last
# changed and still be read. Absorbs clock skew between the machine that
# wrote a line and the server that stamps a network share's mtimes, and the
# report's rounding of local days to UTC hours.
MTIME_MARGIN = 86_400
#: How many unreadable entries a scan keeps by name (``ScanStats.missing_paths``).
MISSING_PATHS_KEPT = 3

_THREAD_ID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?:\.zst)?$"
)


@dataclass
class ScanStats:
    files_seen: int = 0
    files_read: int = 0
    bytes_read: int = 0
    events: int = 0  # new events counted (after dedupe)
    duplicates: int = 0
    missing: int = 0  # unreadable entries, such as symlinks to an unmounted volume
    # The first few of them, to name in a message (not in the JSON).
    missing_paths: list = field(default_factory=list)
    # Entries gone when the scan came to read them (deleted between the
    # listing and the read): not unreadable, and not part of ``missing``.
    vanished: int = 0
    skipped_compressed: int = 0  # .zst files with no zstd support
    # The scan window: files last changed before ``modified_since`` (epoch
    # seconds; None when every file was in scope) were not opened.
    modified_since: float | None = None
    files_skipped: int = 0  # Claude files and Codex threads outside the window
    older_not_in_ledger: int = 0  # of those, the ones the ledger does not hold yet
    seconds: float = 0.0
    interrupted: bool = False

    def to_json(self) -> dict:
        return {
            "filesSeen": self.files_seen,
            "filesRead": self.files_read,
            "bytesRead": self.bytes_read,
            "newEvents": self.events,
            "duplicates": self.duplicates,
            "missing": self.missing,
            "vanished": self.vanished,
            "skippedCompressed": self.skipped_compressed,
            "modifiedSince": (
                datetime.fromtimestamp(self.modified_since).astimezone().isoformat(
                    timespec="seconds"
                )
                if self.modified_since is not None
                else None
            ),
            "filesSkipped": self.files_skipped,
            "olderFilesNotInLedger": self.older_not_in_ledger,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class ClaudeRoot:
    path: Path
    account: str | None = None  # set for a run profile's own (unshared) history


@dataclass
class ScanConfig:
    data_root: Path
    claude_roots: list[ClaudeRoot] = field(default_factory=list)
    codex_roots: list[Path] = field(default_factory=list)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


def default_config(data_root: Path, registry: dict | None = None) -> ScanConfig:
    """Roots for this machine, resolved through ``paths``.

    Claude Code: the config home's ``projects/`` (``CLAUDE_CONFIG_DIR`` or
    ``~/.claude``), the default home's if different, and each ``qswap run``
    profile's ``projects/`` that is a real directory rather than a link into
    the shared tree; those files belong to the profile's account. Codex:
    ``$CODEX_HOME/sessions`` and ``$CODEX_HOME/archived_sessions``.
    """
    roots: list[ClaudeRoot] = []
    for home in (paths.get_claude_config_home(), paths.get_default_claude_config_home()):
        p = home / "projects"
        if not any(_same_dir(p, r.path) for r in roots):
            roots.append(ClaudeRoot(p))
    accounts = (registry or {}).get("accounts") or {}
    sessions = Path(data_root) / "sessions"
    try:
        entries = sorted(sessions.iterdir())
    except OSError:
        entries = []
    for prof in entries:
        proj = prof / "projects"
        if proj.is_symlink() or not proj.is_dir():
            continue
        if any(_same_dir(proj, r.path) for r in roots):
            continue
        num = prof.name.split("-", 1)[0]
        acct = accounts.get(num) if num.isdigit() else None
        account = (
            identity_key(acct.get("email", ""), acct.get("organizationUuid") or "")
            if isinstance(acct, dict) and acct.get("email")
            else None
        )
        roots.append(ClaudeRoot(proj, account))
    codex_home = paths.get_codex_home()
    return ScanConfig(
        data_root=Path(data_root),
        claude_roots=roots,
        codex_roots=[codex_home / "sessions", codex_home / "archived_sessions"],
    )


def _walk(root: Path, accept: Callable[[str], bool]) -> Iterator[str]:
    """Files under ``root`` whose name passes ``accept``. Per-file symlinks are
    listed (and followed when read); symlinked directories are not entered,
    so a link loop cannot hang the scan."""
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                    continue
            except OSError:
                continue
            if accept(e.name):
                yield e.path


def _stat(path: str) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError:
        return None


def _stat_all(paths: list[str]) -> dict[str, os.stat_result | None]:
    """``stat`` many files at once. On a network share each call is a round
    trip, so they run in parallel: an incremental scan over ~1,400 linked
    rollouts drops from seconds to well under one."""
    if len(paths) < 64:
        return {p: _stat(p) for p in paths}
    with ThreadPoolExecutor(max_workers=16) as pool:
        return dict(zip(paths, pool.map(_stat, paths)))


def _ino(st: os.stat_result) -> int:
    """The inode as a signed 64-bit value: network filesystems report inodes
    above 2**63, which SQLite's INTEGER cannot hold."""
    ino = st.st_ino & 0xFFFFFFFFFFFFFFFF
    return ino - (1 << 64) if ino >= (1 << 63) else ino


def _held(cursor: tuple | None, path: str, st: os.stat_result) -> bool:
    """Whether the ledger already holds all of ``path`` as ``st`` describes it,
    judged from the stored cursor without opening the file: the cursor marks
    this file read to its end and the file is unchanged since; or, for a Codex
    thread now in another file (moved to ``archived_sessions/``, compressed),
    the thread was read to the end of a copy changed no earlier than this one."""
    if cursor is None:
        return False
    c_inode, c_size, c_mtime, _offset, c_state = cursor
    if c_size is None or c_mtime is None:
        return False  # read only partway
    if c_inode == _ino(st) and c_size == st.st_size and c_mtime == st.st_mtime_ns:
        return True
    if not c_state:
        return False
    try:
        saved = json.loads(c_state)
    except ValueError:
        return False
    return isinstance(saved, dict) and saved.get("file") != path and st.st_mtime_ns <= c_mtime


class _Batch:
    def __init__(self) -> None:
        self.events: list[tuple[str, Event]] = []
        # session -> (owner, early): early when no event of the session came
        # before the owner line in its file.
        self.owners: dict[str, tuple[str, int]] = {}
        self.cursors: dict[str, tuple] = {}
        self.bytes = 0

    def __len__(self) -> int:
        return len(self.events)


class Scanner:
    """One scan over the configured roots into a ``LedgerStore``."""

    def __init__(
        self,
        store: LedgerStore,
        config: ScanConfig,
        *,
        progress: Callable[[ScanStats, int], None] | None = None,
        batch_events: int = BATCH_EVENTS,
        batch_bytes: int = BATCH_BYTES,
    ):
        self.store = store
        self.config = config
        self.progress = progress
        self.batch_events = batch_events
        self.batch_bytes = batch_bytes
        self.stats = ScanStats()
        self._log_roots = {"claude": config.data_root, "codex": config.data_root / "codex"}
        self._log_stamps: dict[str, tuple | None] = {}
        self.logs: dict[str, ActivationLog] = {}
        self._refresh_logs()
        self._batch = _Batch()
        self._last_progress = 0.0
        self._total_files = 0
        # Claude sessions with a counted event in the ledger, read when a
        # file is resumed mid-way (an owner line there came late).
        self._counted: set[str] | None = None

    def _refresh_logs(self) -> None:
        """(Re)load an activation log whose file changed, so a switch made
        while a long scan runs attributes the events after it correctly."""
        for provider, root in self._log_roots.items():
            st = _stat(str(root / "activations.jsonl"))
            stamp = (st.st_size, st.st_mtime_ns) if st is not None else None
            if provider not in self.logs or stamp != self._log_stamps.get(provider):
                self.logs[provider] = ActivationLog.load(root)
                self._log_stamps[provider] = stamp

    # -- public -------------------------------------------------------------

    def run(
        self, providers: tuple[str, ...] = ("claude", "codex"), *, since: float | None = None
    ) -> ScanStats:
        """Bring the store up to date with the files under the roots.

        ``since`` is the earliest event time (epoch seconds) the caller will
        report on. Files last changed more than ``MTIME_MARGIN`` before it are
        left alone, with no cursor, for a later, wider scan; ``stats`` counts
        them. ``None`` scans every file. Either way the files are read newest
        first.
        """
        start = time.monotonic()
        cutoff_ns: int | None = None
        if since is not None:
            self.stats.modified_since = since - MTIME_MARGIN
            cutoff_ns = int(self.stats.modified_since * 1_000_000_000)
        try:
            claude_files: list[tuple[str, str | None]] = []
            codex_threads: dict[str, list[str]] = {}
            if "claude" in providers:
                for root in self.config.claude_roots:
                    for path in _walk(root.path, lambda n: n.endswith(".jsonl")):
                        claude_files.append((path, root.account))
            if "codex" in providers:
                for root in self.config.codex_roots:
                    for path in _walk(
                        root,
                        lambda n: n.startswith("rollout-")
                        and (n.endswith(".jsonl") or n.endswith(".jsonl.zst")),
                    ):
                        m = _THREAD_ID.search(os.path.basename(path))
                        key = f"codex-thread:{m.group(1)}" if m else f"codex-file:{path}"
                        codex_threads.setdefault(key, []).append(path)
            # One stat per file, in parallel; it follows symlinks, so the
            # mtime is the target's: the bytes a read would see.
            stats = _stat_all(
                [p for p, _a in claude_files] + [p for c in codex_threads.values() for p in c]
            )
            claude_cursors = self.store.cursors("claude") if claude_files else {}
            codex_cursors = self.store.cursors("codex") if codex_threads else {}
            # What to read: (mtime, cursor key, provider, path, stat, files,
            # account); files counts a Codex thread's copies. Newest first.
            work: list[tuple[int, str, str, str, os.stat_result, int, str | None]] = []
            for path, account in claude_files:
                st = stats[path]
                if st is None:
                    self.stats.files_seen += 1
                    self._missing(path)
                    continue
                if not self._outside_window(st, cutoff_ns, claude_cursors.get(path), path):
                    work.append((st.st_mtime_ns, path, "claude", path, st, 1, account))
            for key, candidates in codex_threads.items():
                chosen = self._choose(candidates, stats)
                if chosen is None:
                    self.stats.files_seen += len(candidates)
                    continue
                path, st = chosen
                if not self._outside_window(st, cutoff_ns, codex_cursors.get(key), path):
                    work.append((st.st_mtime_ns, key, "codex", path, st, len(candidates), None))
            work.sort(key=lambda w: (-w[0], w[1]))
            self._total_files = self.stats.files_seen + sum(w[5] for w in work)
            for _mtime, key, provider, path, st, files, account in work:
                if provider == "claude":
                    self._claude_file(path, account, claude_cursors.get(key), st)
                else:
                    self._codex_thread(key, path, st, files, codex_cursors.get(key))
            self._flush()
        except KeyboardInterrupt:
            self.stats.interrupted = True
            self.store.rollback()
            raise
        finally:
            self.stats.seconds = time.monotonic() - start
        return self.stats

    def _missing(self, path: str) -> None:
        """Count an entry that could not be read, keeping the first few. One
        that no longer exists at all, not even as a link, was deleted after
        the listing: it counts as vanished instead (decision R4-2). A broken
        link, a permission error or any other read error stays missing. Only
        an lstat that says "no such entry" counts as gone: ``lexists`` is also
        false when a directory on the path cannot be entered, and those files
        are still there."""
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            self.stats.vanished += 1
            return
        except OSError:
            pass
        self.stats.missing += 1
        if len(self.stats.missing_paths) < MISSING_PATHS_KEPT:
            self.stats.missing_paths.append(path)

    def _outside_window(
        self, st: os.stat_result, cutoff_ns: int | None, cursor: tuple | None, path: str
    ) -> bool:
        """True for a file last changed before the window: count it and leave
        it unread, with its cursor (if any) as it was."""
        if cutoff_ns is None or st.st_mtime_ns >= cutoff_ns:
            return False
        self.stats.files_skipped += 1
        if not _held(cursor, path, st):
            self.stats.older_not_in_ledger += 1
        return True

    # -- Claude Code ----------------------------------------------------------

    def _claude_file(
        self, path: str, account: str | None, cursor: tuple | None, st: os.stat_result
    ) -> None:
        self.stats.files_seen += 1
        inode, size, mtime = _ino(st), st.st_size, st.st_mtime_ns
        offset = 0
        if cursor is not None:
            c_inode, c_size, c_mtime, c_offset, _state = cursor
            if c_inode == inode and c_size == size and c_mtime == mtime:
                return  # unchanged
            if c_inode == inode and size >= (c_offset or 0):
                offset = c_offset or 0
        try:
            f = open(path, "rb")
        except OSError:
            self._missing(path)
            return
        self.stats.files_read += 1
        with f:
            if offset:
                f.seek(offset - 1)
                if f.read(1) != b"\n":
                    offset = 0  # rewritten in place: read it again; dedupe absorbs it
                f.seek(offset)
            pos = offset
            # Sessions with an event earlier in this file: an owner line
            # after one came late (store.py, owner_early).
            with_events: set[str] = set()
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a line still being written: leave it for next time
                pos += len(raw)
                self.stats.bytes_read += len(raw)
                self._batch.bytes += len(raw)
                item = parse_claude_line(raw)
                if item is None:
                    pass
                elif isinstance(item, OwnerNote):
                    if item.session not in self._batch.owners:
                        early = item.session not in with_events and not (
                            offset and item.session in self._counted_sessions()
                        )
                        self._batch.owners[item.session] = (item.owner, int(early))
                else:
                    if account is not None:
                        item.account = account
                    if item.session:
                        with_events.add(item.session)
                    self._batch.events.append(("claude", item))
                if self._batch_full():
                    # Mid-file: no size, so the next scan never mistakes the
                    # file for fully read and resumes from ``pos``.
                    self._batch.cursors[path] = ("claude", inode, None, mtime, pos, None)
                    self._flush()
                    self._maybe_progress()
            self._batch.cursors[path] = ("claude", inode, size, mtime, pos, None)
        self._maybe_progress()

    def _counted_sessions(self) -> set[str]:
        if self._counted is None:
            self._counted = self.store.counted_sessions("claude")
        return self._counted

    # -- Codex ----------------------------------------------------------------

    @staticmethod
    def _pick(
        candidates: list[str], stats: dict[str, os.stat_result | None]
    ) -> list[tuple[str, os.stat_result]]:
        """Readable candidates for one thread, best first: a live ``.jsonl``
        before its compressed copy, ``sessions/`` before ``archived_sessions/``."""
        found = []
        for p in candidates:
            st = stats[p] if p in stats else _stat(p)
            if st is not None:
                found.append((p, st))
        found.sort(
            key=lambda x: (
                x[0].endswith(".zst"),
                f"{os.sep}archived_sessions{os.sep}" in x[0],
                -x[1].st_size,
            )
        )
        return found

    def _choose(
        self, candidates: list[str], stats: dict[str, os.stat_result | None]
    ) -> tuple[str, os.stat_result] | None:
        """The file a thread is read from, or None when no copy is readable
        (counted as missing, or as compressed with no zstd support)."""
        found = self._pick(candidates, stats)
        readable = {p for p, _s in found}
        for p in candidates:
            if p not in readable:
                self._missing(p)
        if found and not ZSTD_AVAILABLE:
            self.stats.skipped_compressed += sum(1 for p, _s in found if p.endswith(".zst"))
            found = [x for x in found if not x[0].endswith(".zst")]
        return found[0] if found else None

    def _codex_thread(
        self, key: str, path: str, st: os.stat_result, files: int, cursor: tuple | None
    ) -> None:
        self.stats.files_seen += files
        inode, size, mtime = _ino(st), st.st_size, st.st_mtime_ns
        state: dict = new_codex_state()
        offset = 0
        if cursor is not None:
            c_inode, c_size, c_mtime, c_offset, c_state = cursor
            try:
                saved = json.loads(c_state) if c_state else None
            except ValueError:
                saved = None
            same_file = isinstance(saved, dict) and saved.get("file") == path
            if same_file and c_inode == inode and c_size == size and c_mtime == mtime:
                return  # unchanged
            if isinstance(saved, dict) and c_offset:
                state = {**new_codex_state(), **saved.get("reader", {})}
                offset = c_offset
                if same_file and not path.endswith(".zst") and c_inode == inode and size < offset:
                    state, offset = new_codex_state(), 0  # truncated: start over
        stream = self._open_codex(path)
        if stream is None:
            self._missing(path)
            return
        self.stats.files_read += 1
        with stream:
            pos = 0
            if offset:
                pos = self._skip_to(stream, path, offset)
                if pos != offset:
                    # The new file does not continue the old one (rewritten,
                    # or a different copy): read it from the start. Dedupe
                    # keeps already-counted responses from counting twice.
                    stream.close()
                    stream = self._open_codex(path)
                    if stream is None:
                        self._missing(path)
                        return
                    state, pos = new_codex_state(), 0
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                pos += len(raw)
                self.stats.bytes_read += len(raw)
                self._batch.bytes += len(raw)
                ev = parse_codex_line(raw, state)
                if ev is not None:
                    self._batch.events.append(("codex", ev))
                if self._batch_full():
                    self._batch.cursors[key] = (
                        "codex", inode, None, mtime, pos,
                        json.dumps({"file": path, "reader": state}),
                    )
                    self._flush()
                    self._maybe_progress()
            self._batch.cursors[key] = (
                "codex", inode, size, mtime, pos, json.dumps({"file": path, "reader": state}),
            )
        self._maybe_progress()

    @staticmethod
    def _open_codex(path: str):
        try:
            if path.endswith(".zst"):
                f = _zstd.open(path, "rb")  # type: ignore[union-attr]
                if getattr(_zstd, "__name__", "") == "compression.zstd":
                    return f  # already a buffered, line-iterable reader
                return io.BufferedReader(f, buffer_size=_CHUNK)
            return open(path, "rb")
        except Exception:
            return None

    @staticmethod
    def _skip_to(stream, path: str, offset: int) -> int:
        """Advance ``stream`` by ``offset`` bytes when the byte before it ends a
        line; return the position reached (``offset`` on success)."""
        if not path.endswith(".zst"):
            try:
                stream.seek(offset - 1)
                ok = stream.read(1) == b"\n"
            except OSError:
                return -1
            return offset if ok else -1
        left, last = offset, b""
        try:
            while left > 0:
                chunk = stream.read(min(_CHUNK, left))
                if not chunk:
                    return -1
                left -= len(chunk)
                last = chunk[-1:]
        except Exception:
            return -1
        return offset if last == b"\n" else -1

    # -- batches --------------------------------------------------------------

    def _batch_full(self) -> bool:
        return len(self._batch) >= self.batch_events or self._batch.bytes >= self.batch_bytes

    def _flush(self) -> None:
        batch = self._batch
        self._batch = _Batch()
        if not batch.events and not batch.cursors and not batch.owners:
            return
        # Within a batch the copy with the most output tokens wins (Claude
        # Code's last line of a response carries the final count).
        best: dict[int, tuple[str, Event]] = {}
        for provider, ev in batch.events:
            have = best.get(ev.key)
            if have is None or ev.output >= have[1].output:
                best[ev.key] = (provider, ev)
        self._refresh_logs()
        store = self.store
        store.begin()
        try:
            stored = store.seen_many(list(best))
            buckets: dict[tuple, list[int]] = {}
            seen_rows: list[tuple[int, int]] = []
            first_ts: dict[str, float] = {}
            for key, (provider, ev) in best.items():
                prior = stored.get(key)
                if prior is None:
                    tokens = [
                        ev.input, ev.output, ev.cache_read, ev.cache_write_5m,
                        ev.cache_write_1h, ev.reasoning, 1,
                    ]
                    self.stats.events += 1
                    if provider == "claude" and ev.session:
                        t = first_ts.get(ev.session)
                        first_ts[ev.session] = ev.ts if t is None else min(t, ev.ts)
                elif ev.output > prior:
                    # The final count of a response stored mid-stream.
                    tokens = [0, ev.output - prior, 0, 0, 0, 0, 0]
                    self.stats.duplicates += 1
                else:
                    self.stats.duplicates += 1
                    continue
                seen_rows.append((key, ev.output))
                account = ev.account
                if account is None:
                    account = self.logs[provider].account_at(ev.ts)
                bkey = (
                    provider, int(ev.ts // 3600), account, ev.model, ev.project, ev.session,
                )
                acc = buckets.get(bkey)
                if acc is None:
                    buckets[bkey] = tokens
                else:
                    for i, v in enumerate(tokens):
                        acc[i] += v
            self.stats.duplicates += len(batch.events) - len(best)
            if seen_rows:
                store.put_seen(seen_rows)
            if buckets:
                store.add_buckets([(*k, *v) for k, v in buckets.items()])
            sessions = {s: [o, None, early] for s, (o, early) in batch.owners.items()}
            for s, t in first_ts.items():
                sessions.setdefault(s, [None, None, None])[1] = t
            if sessions:
                store.note_sessions(
                    [("claude", s, o, t, early) for s, (o, t, early) in sessions.items()]
                )
                if self._counted is not None:
                    self._counted.update(first_ts)
            for path, (provider, inode, size, mtime, pos, state) in batch.cursors.items():
                store.put_cursor(path, provider, inode, size, mtime, pos, state)
            store.commit()
        except BaseException:
            store.rollback()
            raise

    def _maybe_progress(self) -> None:
        if self.progress is None:
            return
        now = time.monotonic()
        if now - self._last_progress >= 0.2:
            self._last_progress = now
            self.progress(self.stats, self._total_files)


def tty_progress(stream=None) -> Callable[[ScanStats, int], None] | None:
    """A one-line progress printer for a terminal; None when not a TTY."""
    out = stream or sys.stderr
    if not getattr(out, "isatty", lambda: False)():
        return None

    def show(stats: ScanStats, total: int) -> None:
        mb = stats.bytes_read / 1_000_000
        out.write(
            f"\r\033[2KScanning transcripts: {stats.files_seen}/{total} files, "
            f"{mb:,.0f} MB, {stats.events:,} new responses"
        )
        out.flush()

    return show


def clear_progress(stream=None) -> None:
    out = stream or sys.stderr
    if getattr(out, "isatty", lambda: False)():
        out.write("\r\033[2K")
        out.flush()
