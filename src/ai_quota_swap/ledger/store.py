"""The ledger's SQLite store.

Tables (see docs/design/ledger.md):

- ``files``: one scan cursor per source. For Claude Code the key is the
  transcript's path; for Codex it is ``codex-thread:<thread id>``, because a
  Codex rollout moves (``sessions/`` to ``archived_sessions/``) and changes
  format (``.jsonl`` to ``.jsonl.zst``) over its life. ``offset`` counts bytes
  of the (decompressed) stream; ``state`` is the reader's JSON context.
- ``seen``: a 64-bit hash of each dedupe key, plus the output-token count
  that was counted for it (Claude Code writes a message's partial count
  first and the final count later; the difference is added when it shows up).
- ``buckets``: token sums per provider, UTC epoch hour, account, model,
  project and session. Rows are added to, never rewritten.
- ``sessions``: per session, the account that started it as Claude Code
  records it (``session-owner``), and its first counted event time. Used at
  report time to attribute ``qswap run`` sessions to their profile's account.
- ``meta``: schema version and scan bookkeeping.

Costs are not stored: they are computed when a report is printed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
DB_FILENAME = "ledger.db"
# ``meta`` key: when ``qswap usage`` last finished a scan (epoch seconds), so
# a spend figure read without scanning can say how old it is.
LAST_SCAN_META = "last_scan_at"

TOKEN_FIELDS = (
    "input",
    "output",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "reasoning",
    "requests",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    inode INTEGER,
    size INTEGER,
    mtime_ns INTEGER,
    offset INTEGER NOT NULL DEFAULT 0,
    state TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen(
    key INTEGER PRIMARY KEY,
    out INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS buckets(
    provider TEXT NOT NULL,
    hour INTEGER NOT NULL,
    account TEXT NOT NULL,
    model TEXT NOT NULL,
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(provider, hour, account, model, project, session)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS buckets_hour ON buckets(hour);
CREATE TABLE IF NOT EXISTS sessions(
    provider TEXT NOT NULL,
    session TEXT NOT NULL,
    owner TEXT,
    first_ts REAL,
    PRIMARY KEY(provider, session)
) WITHOUT ROWID;
"""

_UPSERT_BUCKET = (
    "INSERT INTO buckets(provider, hour, account, model, project, session, "
    + ", ".join(TOKEN_FIELDS)
    + ") VALUES (?, ?, ?, ?, ?, ?, "
    + ", ".join("?" for _ in TOKEN_FIELDS)
    + ") ON CONFLICT(provider, hour, account, model, project, session) DO UPDATE SET "
    + ", ".join(f"{f} = {f} + excluded.{f}" for f in TOKEN_FIELDS)
)


def key_hash(*parts: str) -> int:
    """A signed 64-bit hash of a dedupe key (SQLite INTEGER range)."""
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class LedgerStore:
    """Thin wrapper over the SQLite file. One instance per scan or report."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        version = None
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            version = int(row[0]) if row else None
        except sqlite3.OperationalError:
            version = None  # no meta table yet
        if version is not None and version != SCHEMA_VERSION:
            # A ledger is a cache of files that are still on disk: an
            # incompatible one is rebuilt rather than migrated.
            self._drop_all()
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def _drop_all(self) -> None:
        for table in ("files", "seen", "buckets", "sessions", "meta"):
            self.conn.execute(f"DROP TABLE IF EXISTS {table}")

    def reset(self) -> None:
        """Empty every table (``--rescan``)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._drop_all()
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # -- meta -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )

    # -- cursors --------------------------------------------------------

    def cursors(self, provider: str) -> dict[str, tuple]:
        """``{path: (inode, size, mtime_ns, offset, state)}`` for a provider."""
        return {
            row[0]: row[1:]
            for row in self.conn.execute(
                "SELECT path, inode, size, mtime_ns, offset, state FROM files WHERE provider = ?",
                (provider,),
            )
        }

    def put_cursor(
        self,
        path: str,
        provider: str,
        inode: int | None,
        size: int | None,
        mtime_ns: int | None,
        offset: int,
        state: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO files(path, provider, inode, size, mtime_ns, offset, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (path, provider, inode, size, mtime_ns, offset, state),
        )

    # -- dedupe ---------------------------------------------------------

    def seen_many(self, keys: list[int]) -> dict[int, int]:
        """Stored ``{key: out}`` for those of ``keys`` already counted."""
        out: dict[int, int] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            for k, o in self.conn.execute(
                f"SELECT key, out FROM seen WHERE key IN ({marks})", chunk
            ):
                out[k] = o
        return out

    def put_seen(self, rows: list[tuple[int, int]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO seen(key, out) VALUES (?, ?)", rows
        )

    # -- buckets --------------------------------------------------------

    def add_buckets(self, rows: list[tuple]) -> None:
        """Rows: ``(provider, hour, account, model, project, session, *TOKEN_FIELDS)``."""
        self.conn.executemany(_UPSERT_BUCKET, rows)

    def note_sessions(self, rows: list[tuple[str, str, str | None, float | None]]) -> None:
        """``(provider, session, owner, first_ts)``: keeps the first owner and
        the earliest counted event time."""
        self.conn.executemany(
            "INSERT INTO sessions(provider, session, owner, first_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider, session) DO UPDATE SET "
            "owner = COALESCE(sessions.owner, excluded.owner), "
            "first_ts = CASE WHEN sessions.first_ts IS NULL THEN excluded.first_ts "
            "WHEN excluded.first_ts IS NULL THEN sessions.first_ts "
            "ELSE MIN(sessions.first_ts, excluded.first_ts) END",
            rows,
        )

    def query_buckets(
        self, *, since_hour: int | None, until_hour: int | None, provider: str | None
    ) -> list[tuple]:
        sql = (
            "SELECT provider, hour, account, model, project, session, "
            + ", ".join(TOKEN_FIELDS)
            + " FROM buckets WHERE 1=1"
        )
        args: list = []
        if since_hour is not None:
            sql += " AND hour >= ?"
            args.append(since_hour)
        if until_hour is not None:
            sql += " AND hour < ?"
            args.append(until_hour)
        if provider is not None:
            sql += " AND provider = ?"
            args.append(provider)
        return list(self.conn.execute(sql, args))

    def sessions(self, provider: str) -> dict[str, tuple[str | None, float | None]]:
        return {
            row[0]: (row[1], row[2])
            for row in self.conn.execute(
                "SELECT session, owner, first_ts FROM sessions WHERE provider = ?",
                (provider,),
            )
        }

    # -- transactions ---------------------------------------------------

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")
