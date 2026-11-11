"""Detect running Claude Code instances.

Reads session PID files (~/.claude/sessions/{pid}.json) and IDE lockfiles
(~/.claude/ide/{port}.lock) to determine which Claude Code instances are
currently running. Uses the same mechanism Claude Code itself uses internally.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ai_quota_swap import spawn
from ai_quota_swap.paths import get_claude_config_home

logger = logging.getLogger(__name__)

# A session record names a pid, and the OS recycles pids: once the claude
# that wrote the record is gone, the number can belong to anything. The
# record also carries claude's reading of its own start (``procStart``): on
# Linux the ``/proc/<pid>/stat`` start time in clock ticks since boot, fixed
# for the process's lifetime, and elsewhere ``ps -o lstart``, a wall-clock
# time. Only the latter needs slack, for the small clock steps that move a
# ``ps`` start time on some platforms.
PID_REUSE_SLACK_S = 120

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class ClaudeSession:
    """A running Claude Code session from ~/.claude/sessions/{pid}.json."""

    pid: int
    session_id: str
    cwd: str
    started_at: int  # epoch milliseconds
    kind: str  # "interactive", "bg", "daemon", "daemon-worker"
    entrypoint: str  # "cli", "claude-vscode", "claude-desktop", "sdk-cli", "mcp"
    status: str | None = None  # "busy", "idle", "waiting"


@dataclass
class IdeInstance:
    """A running IDE instance from ~/.claude/ide/{port}.lock."""

    port: int  # from filename
    pid: int
    ide_name: str  # "Visual Studio Code", "Cursor", "Windsurf"
    workspace_folders: list[str] = field(default_factory=list)


def get_claude_dir() -> Path:
    """Return the Claude config directory, respecting CLAUDE_CONFIG_DIR."""
    return get_claude_config_home()


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Cross-platform:
    - macOS/Linux/WSL: os.kill(pid, 0), and a zombie (a process that has
      exited and waits only for its parent to reap it) is not running
    - Windows: ctypes OpenProcess and GetExitCodeProcess
    """
    if pid <= 1:
        return False

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
    except PermissionError:
        # EPERM means the process exists but we lack permission
        pass
    except OSError:
        return False
    # os.kill succeeds on a zombie, and ps still reports its start time, so
    # its record would otherwise read as a live session until it is reaped.
    return process_state(pid) != "Z"


def process_state(pid: int) -> str | None:
    """The process's state letter (``R``, ``S``, ``Z`` for a zombie, ...), or
    None when unknowable. Linux reads field 3 of ``/proc/<pid>/stat``, other
    POSIX systems ``ps -o stat=``; Windows has no such state. Claude Code
    tells a zombie apart the same way."""
    if sys.platform == "win32":
        return None
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        fields = text.rpartition(")")[2].split()
        return fields[0][:1] if fields else None
    text = _ps(pid, "stat")
    return text[:1] if text else None


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# GetExitCodeProcess's answer for a process that has not exited.
STILL_ACTIVE = 259


def _kernel32():
    """Windows' kernel32, the seam the Windows process checks go through."""
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _is_pid_alive_windows(pid: int) -> bool:
    """Windows-specific PID liveness check using ctypes.

    OpenProcess also succeeds on a process that has exited while something
    still holds a handle to it, so the exit code decides: ``STILL_ACTIVE``
    is running. An exit code that cannot be read counts as running."""
    try:
        import ctypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.pointer(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def process_creation_filetime(pid: int) -> int | None:
    """When ``pid`` was created, as a Windows FILETIME (100 ns intervals
    since 1601, UTC), or None when unknowable (not Windows, no such process,
    no access). From GetProcessTimes, the clock Claude Code stamps into a
    record's ``procStartFt``."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            # A FILETIME is two 32-bit halves, low first: a little-endian
            # 64-bit integer.
            created, exited, kernel, user = (ctypes.c_ulonglong(0) for _ in range(4))
            if not kernel32.GetProcessTimes(
                handle, ctypes.pointer(created), ctypes.pointer(exited),
                ctypes.pointer(kernel), ctypes.pointer(user),
            ):
                return None
            return created.value or None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


# FILETIME counts from 1601; the epoch is this many seconds later.
_FILETIME_EPOCH_S = 11_644_473_600
# A FILETIME outside these years is not one (another unit, or garbage).
_FILETIME_PLAUSIBLE_S = (946_684_800, 4_102_444_800)  # 2000 .. 2100


def _filetime_seconds(value: int) -> float | None:
    """A FILETIME as epoch seconds, or None when it cannot be one."""
    seconds = value / 10_000_000 - _FILETIME_EPOCH_S
    low, high = _FILETIME_PLAUSIBLE_S
    return seconds if low <= seconds <= high else None


def _windows_start_matches(pid: int, stamp: str) -> bool:
    """Is the live process at ``pid`` the one whose record carries the
    FILETIME ``stamp``? Both come from the process's creation time, which
    never changes, so a process created later than the stamp (a second of
    slack for rounding) holds a recycled pid. Anything unknowable passes."""
    recorded = _filetime_seconds(int(stamp))
    if recorded is None:
        return True
    created_ft = process_creation_filetime(pid)
    created = _filetime_seconds(created_ft) if created_ft else None
    if created is None:
        return True
    return created <= recorded + 1


def _ps(pid: int, *columns: str) -> str | None:
    """``ps -o`` ``columns`` for ``pid``, or None when unknowable.

    POSIX only, under ``LC_ALL=C TZ=UTC`` like claude's own reading. Windows
    and every failure answer None: not knowing must never be read as "not
    the recorded process".
    """
    if sys.platform == "win32":
        return None
    try:
        proc = spawn.run(
            ["ps", "-o", ",".join(f"{c}=" for c in columns), "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    return text


def process_started_at(pid: int) -> int | None:
    """Epoch seconds at which ``pid`` started, or None when unknowable.

    Read the way claude stamps ``procStart`` into its record, ``ps -o
    lstart=`` under ``LC_ALL=C TZ=UTC``, so the two agree to the second for
    the same process.
    """
    text = _ps(pid, "lstart")
    if text is None:
        return None
    try:
        return _lstart_seconds(text)
    except ValueError:
        return None


def _lstart_seconds(text: str) -> int:
    """``Wed Sep  2 20:35:59 2026``, the ``ps -o lstart`` format under
    ``LC_ALL=C TZ=UTC``, as epoch seconds. Parsed by hand because ``strptime``
    reads month names in the process locale."""
    parts = text.split()
    if len(parts) != 5 or parts[1] not in _MONTHS:
        raise ValueError(text)
    _, month, day, clock, year = parts
    hours, minutes, seconds = (int(p) for p in clock.split(":"))
    return calendar.timegm(
        (int(year), _MONTHS.index(month) + 1, int(day), hours, minutes, seconds, 0, 0, 0)
    )


def process_start_ticks(pid: int) -> str | None:
    """``/proc/<pid>/stat``'s start time, in clock ticks since boot, or None
    when unreadable. Linux by construction: the file exists nowhere else."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    return _stat_start_ticks(text)


def _stat_start_ticks(text: str) -> str | None:
    """Field 22 of a ``/proc/<pid>/stat`` line, as the string claude stamps
    into ``procStart``. The command name sits in parentheses before the
    numeric fields and may itself contain spaces or parentheses, so the
    fields are counted from the last closing one."""
    _, _, rest = text.rpartition(")")
    fields = rest.split()
    if len(fields) <= 19 or not fields[19].isdigit():
        return None
    return fields[19]


def process_is_claude(pid: int) -> bool | None:
    """Does the process at ``pid`` look like a claude, or None when unknowable.

    Judged from ``ps -o comm=,args=``: the native binary and the symlink to
    it are named ``claude``, and an npm install runs ``cli.js`` out of a
    ``claude-code`` package directory.
    """
    text = _ps(pid, "comm", "args")
    if text is None:
        return None
    return "claude" in text.lower()


def pid_matches_record(pid: int, proc_start: str | None) -> bool:
    """Is the live process at ``pid`` the one that wrote a record stamped
    ``proc_start``, claude's reading of its own start?

    On Linux the stamp is the ``/proc/<pid>/stat`` start time in clock ticks
    since boot, which a process keeps for life and no two processes at one
    pid share, so equality is the whole test and no clock domain is
    involved. Elsewhere it is ``ps -o lstart``, a wall-clock time: a
    recycled pid belongs to a process that started after the recorded claude
    did, and only that direction disqualifies, and only a stranger. Some
    ``ps`` builds derive every start time from a boot time that moves with
    each wall-clock step (a WSL2 resume re-syncing the clock steps it by the
    whole sleep), so a live session can read as younger than its own record;
    a claude at the pid is kept either way, and a pid genuinely recycled by
    another claude lingers only for that process's lifetime, which is what
    happened before this check. On Windows the stamp is a FILETIME (Claude
    Code writes it as ``procStartFt``), compared with the process's creation
    time from GetProcessTimes. Everything unknowable (``ps`` or the Windows
    API unavailable, an unstamped or unparseable record) passes, because
    "cannot tell" must never turn a live session into "nobody there".
    """
    if not proc_start:
        return True
    if proc_start.isascii() and proc_start.isdigit():
        if sys.platform == "win32":
            return _windows_start_matches(pid, proc_start)
        ticks = process_start_ticks(pid)
        return ticks is None or ticks == proc_start
    try:
        recorded = _lstart_seconds(proc_start)
    except ValueError:
        return True
    started = process_started_at(pid)
    if started is None or started <= recorded + PID_REUSE_SLACK_S:
        return True
    return process_is_claude(pid) is not False


def scan_sessions(claude_dir: Path | None = None) -> tuple[list[ClaudeSession], int]:
    """Live sessions, and how many records could NOT be read.

    A record counts as live only when its pid is alive AND still belongs to
    the claude that wrote it (see ``pid_matches_record``): a crashed claude
    leaves its record behind, and the OS can hand the number to something
    else.

    Two kinds of caller read this directory and they need opposite things from
    an unparseable record:

    - A SCAN (a listing, a status display) wants it skipped. One bad file must
      not take out the whole listing.
    - A GUARD wants to know. ``0 live`` and ``0 readable`` are the same list,
      and only the first is safe to act on -- the callers gate ``_bootstrap``
      (which deletes a profile's Keychain entry and overwrites
      ``.credentials.json``) and account removal, so reading "could not tell"
      as "nobody there" runs them underneath a live instance.

    So the count is returned rather than swallowed, and ``list_sessions``
    below is the scan-shaped view that drops it.

    ``iterdir``, NOT ``glob`` behind an ``is_dir`` check: both of those
    suppress the ``OSError`` they hit, so a sessions directory that cannot be
    listed -- or one whose stat fails because the profile root cannot be
    traversed -- arrived at the GUARD as an empty directory rather than an
    unknown one. Such a directory contributes exactly 1, which makes the count
    a lower bound on the records at stake rather than a tally of them; every
    caller only asks whether it is zero.

    A MISSING directory is the one scan failure that still counts 0: a profile
    that has never been run holds no records, and refusing on it would block
    every guarded step against it permanently.

    A record that cannot be read is skipped, not counted, when its file name
    (Claude Code names each ``{pid}.json``) is a pid that is not running: a
    claude killed mid-write leaves such a record, and its process is gone.

    See :func:`scan_session_records` for the paths behind the count.
    """
    sessions, unreadable = scan_session_records(claude_dir)
    return sessions, len(unreadable)


def unreadable_session_records(claude_dir: Path | None = None) -> list[Path]:
    """The session records that could not be read (or the sessions
    directory, when it could not be listed), for a message that names them."""
    return scan_session_records(claude_dir)[1]


def _record_stamp(data: dict) -> str | None:
    """The record's reading of its process's start: ``procStart``, or on
    Windows ``procStartFt`` (a FILETIME), which Claude Code writes there."""
    stamp = data.get("procStart")
    if stamp is None and sys.platform == "win32":
        stamp = data.get("procStartFt")
    if isinstance(stamp, int) and not isinstance(stamp, bool):
        stamp = str(stamp)
    return stamp


def _named_process_is_gone(path: Path) -> bool:
    """Whether ``path`` is named after a pid (``4242.json``) that is not
    running. False when the name is not a pid or liveness cannot be read."""
    stem = path.stem
    if not (stem.isascii() and stem.isdigit()):
        return False
    try:
        return not is_pid_alive(int(stem))
    except Exception:
        return False


def scan_session_records(
    claude_dir: Path | None = None,
) -> tuple[list[ClaudeSession], list[Path]]:
    """:func:`scan_sessions` with the unreadable records named: the live
    sessions, and the path of every record that could not be read (the
    sessions directory itself when it could not be listed)."""
    sessions_dir = (claude_dir or get_claude_dir()) / "sessions"
    try:
        # ``match`` keeps the platform casing the old ``glob`` had (a
        # ``.JSON`` record still counts on Windows); ``endswith`` would not.
        paths = [p for p in sessions_dir.iterdir() if p.match("*.json")]
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        logger.debug("Cannot list session directory %s: %s", sessions_dir, exc)
        return [], [sessions_dir]

    sessions = []
    unreadable: list[Path] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data["pid"]
            if not is_pid_alive(pid):
                continue
            if not pid_matches_record(pid, _record_stamp(data)):
                logger.debug(
                    "Skipping session file %s: pid %s was recycled", path, pid
                )
                continue
            sessions.append(ClaudeSession(
                pid=pid,
                session_id=data.get("sessionId", ""),
                cwd=data.get("cwd", ""),
                started_at=data.get("startedAt", 0),
                kind=data.get("kind", ""),
                entrypoint=data.get("entrypoint", ""),
                status=data.get("status"),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            if _named_process_is_gone(path):
                logger.debug(
                    "Skipping session file %s: unreadable (%s), and its process is gone",
                    path, exc,
                )
                continue
            unreadable.append(path)
            logger.debug("Skipping session file %s: %s", path, exc)
    return sessions, unreadable


def list_sessions(claude_dir: Path | None = None) -> list[ClaudeSession]:
    """Live sessions. A record that cannot be read is SKIPPED.

    SCAN USE ONLY. The returned list cannot distinguish "no live sessions"
    from "no readable records", so anything gating a destructive step must
    call :func:`scan_sessions` and treat a non-zero count as live.
    """
    return scan_sessions(claude_dir)[0]


def list_ide_instances(claude_dir: Path | None = None) -> list[IdeInstance]:
    """Read IDE lockfiles and return only those with alive processes."""
    ide_dir = (claude_dir or get_claude_dir()) / "ide"
    if not ide_dir.is_dir():
        return []

    instances = []
    for path in ide_dir.glob("*.lock"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid is None or not is_pid_alive(pid):
                continue
            port = int(path.stem)
            instances.append(IdeInstance(
                port=port,
                pid=pid,
                ide_name=data.get("ideName", "Unknown IDE"),
                workspace_folders=data.get("workspaceFolders", []),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            logger.debug("Skipping IDE lockfile %s: %s", path, exc)
    return instances


def get_running_instances(
    claude_dir: Path | None = None,
) -> tuple[list[ClaudeSession], list[IdeInstance]]:
    """Return all running Claude Code sessions and IDE instances."""
    resolved = claude_dir or get_claude_dir()
    return list_sessions(resolved), list_ide_instances(resolved)
