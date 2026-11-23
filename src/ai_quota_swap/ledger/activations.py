"""The activation log: which account was live when.

Transcripts do not say which account produced them, so each provider's data
root carries an append-only ``activations.jsonl``. One JSON object per line::

    {"ts": "2026-11-10T14:12:26.123Z", "number": 2, "email": "a@example.com",
     "org": "", "source": "switch", "how": "login"}

``source`` is ``"switch"`` when qswap switched the live login, and
``"observed"`` when qswap noticed that the live login differs from the last
line (a manual ``/login``, another tool). An event belongs to the account whose
interval contains its timestamp; usage before the first line is unattributed.

``how`` (docs/design/api-key-sessions.md) says how the account was made
active: ``login`` (the live claude.ai login), ``helper`` (an API key through
qswap's ``apiKeyHelper`` line, which every session follows within seconds) or
``saved-key`` (an API key saved for Claude Code, which only sessions started
after the activation use). Lines written before the field existed have none.

The writers here are called from the switch path and from a cheap observation
point, so they must never raise into the caller and never block: no locks, one
``O_APPEND`` write of one short line. Two processes racing can at worst write
two lines for the same account, which reads the same.

Launch records
    ``qswap run N`` on an API-key account runs one Claude Code process on the
    shared profile with the key, while the activation log names the login
    every other session uses. So each such launch appends a line to
    ``key_runs.jsonl`` beside the log::

        {"pid": 4242, "number": "5", "ts": 1794225600.0,
         "session": "6f1c...", "email": "api-key-5@token.local", "org": ""}

    ``session`` is the Claude Code session id the launch gave claude
    (``--session-id``), which every transcript line of that conversation
    carries: an event of that session at or after ``ts`` belongs to the
    account the line names (:meth:`ActivationLog.launch_account`), whatever
    the log or the session's owner stamp says. A launch that let claude pick
    its session (``--resume``, ``--continue``) records none, and its usage is
    attributed as before. No key or token is ever written; the account is
    named as the log names it, so a later ``qswap swap`` cannot move the
    usage. Lines from before the field existed carry only ``pid``,
    ``number`` and ``ts`` (``remove`` names the running ones).

    A record is dropped once Claude Code would have removed the transcript
    it explains (``cleanupPeriodDays``, 30 days by default, counted from the
    launch, plus a day) and its process is gone, when the next launch is
    recorded (:func:`record_launch`). That rewrite, unlike the appends
    above, runs under the account lock, which a launch also holds while it
    appends (when it can get it within a second; else it only appends).
    The ledger keeps what the record gave each event it counted
    (scanner.py), so dropping it changes no report.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FILENAME = "activations.jsonl"
SOURCES = ("switch", "observed")
HOWS = ("login", "helper", "saved-key")
#: ``qswap run N`` sessions on an API key: one JSON line per launch (module
#: docstring, "Launch records").
LAUNCHES_FILENAME = "key_runs.jsonl"
#: How many days Claude Code keeps a transcript after its last change when
#: its settings do not say (``cleanupPeriodDays``).
TRANSCRIPT_DAYS = 30
#: How long a lock for dropping old launch records is waited for. A launch
#: that cannot get it appends its record and leaves the old ones for later.
LAUNCH_LOCK_WAIT_S = 1.0

# Per-process memory of the last line seen per log file, keyed by path:
# ((size, mtime_ns), (email, org)). Lets the observation point skip the tail
# read while the file is unchanged, which matters for the TUI and menu bar
# that rebuild the account list every few seconds.
_last_seen: dict[str, tuple[tuple[int, int], tuple[str, str]]] = {}

_TAIL_BYTES = 8192


def log_path(root: Path) -> Path:
    """The activation log inside a provider data root."""
    return Path(root) / FILENAME


def identity_key(email: str, org: str | None) -> str:
    """The account key stored in the ledger: ``email|org`` (org may be empty)."""
    return f"{email}|{org or ''}"


def _iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_ts(value: object) -> float | None:
    """Epoch seconds from an ISO-8601 string or a number; None when unusable:
    a number that is not finite (``NaN`` and ``Infinity``, which JSON readers
    accept) or too large for a float, so a line carrying one is skipped like
    any other unreadable line."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
        except OverflowError:
            return None
        return ts if math.isfinite(ts) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _append(path: Path, record: dict) -> None:
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _tail_identity(path: Path) -> tuple[str, str] | None:
    """(email, org) of the last complete line, or None when there is none."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            chunk = f.read()
    except OSError:
        return None
    for raw in reversed(chunk.splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue  # a torn first line of the tail window, or junk
        if isinstance(obj, dict) and isinstance(obj.get("email"), str):
            return obj["email"], obj.get("org") or ""
    return None


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def record_activation(
    root: Path,
    *,
    number: int | str | None,
    email: str,
    org: str | None,
    source: str = "switch",
    now: float | None = None,
    how: str | None = None,
) -> bool:
    """Append one activation line. Returns True when a line was written.

    Never raises: the switch this rides on has already committed, and a full
    disk or a read-only data root must not turn into a failed switch.
    """
    try:
        if not email:
            return False
        path = log_path(root)
        try:
            num = int(number) if number is not None else None
        except (TypeError, ValueError):
            num = None
        line = {
            "ts": _iso(time.time() if now is None else now),
            "number": num,
            "email": email,
            "org": org or "",
            "source": source if source in SOURCES else "switch",
        }
        if how in HOWS:
            line["how"] = how
        _append(path, line)
        key = _stat_key(path)
        if key is not None:
            _last_seen[str(path)] = (key, (email, org or ""))
        return True
    except Exception:
        return False


def observe_live_login(
    root: Path,
    *,
    number: int | str | None,
    email: str,
    org: str | None,
    now: float | None = None,
    how: str | None = None,
) -> bool:
    """Append an ``observed`` line when the live login differs from the last line.

    Cheap when nothing changed: one ``stat`` against an in-process memory of
    the last line. Never raises. Returns True when a line was written.
    """
    try:
        if not email:
            return False
        path = log_path(root)
        ident = (email, org or "")
        key = _stat_key(path)
        cached = _last_seen.get(str(path))
        if key is not None and cached is not None and cached[0] == key:
            last = cached[1]
        else:
            last = _tail_identity(path) if key is not None else None
            if key is not None and last is not None:
                _last_seen[str(path)] = (key, last)
        if last == ident:
            return False
        return record_activation(
            root, number=number, email=email, org=org, source="observed", now=now, how=how
        )
    except Exception:
        return False


# -- launch records ------------------------------------------------------------


def launches_path(root: Path) -> Path:
    """The launch records inside a provider data root."""
    return Path(root) / LAUNCHES_FILENAME


def record_launch(
    root: Path,
    *,
    pid: int,
    number: int | str,
    email: str,
    org: str | None,
    session: str | None,
    keep_days: float = TRANSCRIPT_DAYS,
    lock: Path | None = None,
    now: float | None = None,
    alive=None,
) -> bool:
    """Append the record of a ``qswap run N`` launch on a key (the module
    docstring), then drop the records past ``keep_days`` (:func:`prune_launches`).
    Both run under ``lock`` when it is given and free within
    ``LAUNCH_LOCK_WAIT_S``; otherwise the record is appended alone, and the
    old ones wait for the next launch. ``alive`` stands in for the pid check
    (tests). Returns True when the record was written. Never raises."""
    try:
        record: dict = {
            "pid": int(pid), "number": str(number),
            "ts": time.time() if now is None else float(now),
        }
        if session:
            record.update(session=str(session), email=email or "", org=org or "")
        path = launches_path(root)
        held = None
        if lock is not None:
            from ai_quota_swap.locking import FileLock

            held = FileLock(Path(lock), timeout=LAUNCH_LOCK_WAIT_S)
            if not held.acquire():
                held = None
        try:
            _append(path, record)
            if held is not None:
                prune_launches(root, keep_days=keep_days, now=record["ts"], alive=alive)
        finally:
            if held is not None:
                held.release()
        return True
    except Exception:
        return False


def prune_launches(
    root: Path, *, keep_days: float = TRANSCRIPT_DAYS, now: float | None = None, alive=None
) -> int:
    """Drop the launch records older than ``keep_days`` plus one day whose
    process is gone, and lines that cannot be read. Returns how many lines
    went. The file is replaced only when one goes; the caller holds the lock
    every writer takes (:func:`record_launch`). Never raises."""
    try:
        path = launches_path(root)
        try:
            raw = path.read_bytes()
        except OSError:
            return 0
        if alive is None:
            from ai_quota_swap.process_detection import is_pid_alive

            alive = is_pid_alive
        cutoff = (time.time() if now is None else now) - (max(0.0, keep_days) + 1) * 86400
        kept: list[bytes] = []
        dropped = 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                dropped += 1
                continue
            ts = parse_ts(obj.get("ts"))
            if ts is None or ts < cutoff:
                pid = obj.get("pid")
                if not (isinstance(pid, int) and not isinstance(pid, bool) and alive(pid)):
                    dropped += 1
                    continue
            kept.append(line + b"\n")
        if dropped:
            from ai_quota_swap.fsutil import atomic_write_bytes

            atomic_write_bytes(path, b"".join(kept))
        return dropped
    except Exception:
        return 0


@dataclass(frozen=True)
class Launch:
    """A launch record the ledger can use: one that names its session."""

    ts: float
    session: str
    account: str  # identity_key
    number: int | None


def load_launches(root: Path) -> list[Launch]:
    """The launch records under ``root`` that name a session and an
    account; a missing or torn file reads as empty."""
    out: list[Launch] = []
    try:
        with open(launches_path(root), "rb") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = parse_ts(obj.get("ts"))
                session, email = obj.get("session"), obj.get("email")
                if ts is None or not isinstance(session, str) or not session:
                    continue
                if not isinstance(email, str) or not email:
                    continue
                num, org = str(obj.get("number")), obj.get("org")
                out.append(Launch(
                    ts=ts, session=session,
                    account=identity_key(email, org if isinstance(org, str) else ""),
                    number=int(num) if num.isascii() and num.isdigit() else None,
                ))
    except OSError:
        pass
    return out


@dataclass(frozen=True)
class Activation:
    ts: float
    account: str  # identity_key
    number: int | None
    source: str
    how: str = ""


class ActivationLog:
    """The parsed log, ready for interval lookups, with the launch records
    of the same root (the module docstring)."""

    def __init__(self, activations: list[Activation], launches: list[Launch] | tuple = ()):
        self.activations = sorted(activations, key=lambda a: a.ts)
        self._times = [a.ts for a in self.activations]
        # Worked out from the whole log once, on first use (_periods).
        self._period_cache: tuple[list[str], list[int]] | None = None
        self._has_saved_key: bool | None = None
        # session -> its launch records, oldest first.
        self._launches: dict[str, list[Launch]] = {}
        for launch in sorted(launches, key=lambda x: x.ts):
            self._launches.setdefault(launch.session, []).append(launch)

    def launch_account(self, session: str, ts: float) -> str | None:
        """The account a launch record gives an event of ``session`` at
        ``ts``: the latest record's launched at or before ``ts``; None when
        no record names the session, or the event came before its launch."""
        found = None
        for launch in self._launches.get(session, ()) if session else ():
            if launch.ts > ts:
                break
            found = launch.account
        return found

    def launched(self, session: str) -> bool:
        """Whether a launch record names ``session`` (its usage then follows
        the record, not the owner stamp or the saved-key rule)."""
        return bool(session) and session in self._launches

    @classmethod
    def load(cls, root: Path) -> ActivationLog:
        """Read ``<root>/activations.jsonl`` and the launch records beside it;
        a missing or torn file reads as empty."""
        out: list[Activation] = []
        try:
            with open(log_path(root), "rb") as f:
                for raw in f:
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    ts = parse_ts(obj.get("ts"))
                    email = obj.get("email")
                    if ts is None or not isinstance(email, str) or not email:
                        continue
                    num = obj.get("number")
                    out.append(
                        Activation(
                            ts=ts,
                            account=identity_key(email, obj.get("org") or ""),
                            number=num if isinstance(num, int) else None,
                            source=str(obj.get("source") or ""),
                            how=str(obj.get("how") or ""),
                        )
                    )
        except OSError:
            pass
        return cls(out, load_launches(root))

    def __len__(self) -> int:
        return len(self.activations)

    @property
    def first_ts(self) -> float | None:
        return self._times[0] if self._times else None

    def account_at(self, ts: float) -> str:
        """The account live at ``ts``; ``""`` (unattributed) before the first line."""
        i = bisect.bisect_right(self._times, ts) - 1
        return self.activations[i].account if i >= 0 else ""

    def _periods(self) -> tuple[list[str], list[int]]:
        """For each line, the ``how`` in force from it and the line its
        period began at: a line with none continues the one before it when
        that is the same account. One walk of the log, kept: reports ask
        this for every bucket and every ``qswap run`` session."""
        if self._period_cache is None:
            acts = self.activations
            hows: list[str] = []
            starts: list[int] = []
            for i, a in enumerate(acts):
                how, start = a.how, i
                if not how and i > 0 and acts[i - 1].account == a.account:
                    how, start = hows[i - 1], starts[i - 1]
                hows.append(how)
                starts.append(start)
            self._period_cache = (hows, starts)
        return self._period_cache

    def _hows(self) -> list[str]:
        """The ``how`` in force from each line: a line with none continues
        the one before it when that is the same account."""
        return self._periods()[0]

    def helper_underneath(self, ts: float) -> str | None:
        """The login under the API key active through qswap's key line at
        ``ts``: the account in force before the run of helper lines that
        ``ts`` falls in began (a switch from one key to another keeps the
        same login under it). None when no key was active through the line
        at ``ts``, or nothing was in force before it."""
        i = bisect.bisect_right(self._times, ts) - 1
        if i < 0:
            return None
        hows = self._hows()
        if hows[i] != "helper":
            return None
        while i >= 0 and hows[i] == "helper":
            i -= 1
        return self.activations[i].account if i >= 0 else None

    def saved_key_periods(self) -> bool:
        """Whether any line activated an API key as a saved key."""
        if self._has_saved_key is None:
            self._has_saved_key = any(a.how == "saved-key" for a in self.activations)
        return self._has_saved_key

    def saved_key_owner(
        self, account: str, hour: int, session_start: float | None
    ) -> str | None:
        """The account usage of ``account`` in UTC epoch ``hour`` belongs to
        instead, under the saved-key rule (docs/design/api-key-sessions.md):
        in a ``saved-key`` period only sessions started after the activation
        used the key, so a session whose first event is before it stays with
        the account live before. None when the rule does not apply.

        Each line starts its own period, also within a run of lines for one
        account: the ``how`` in force at the bucket's time decides (a line
        with none continues the one before it). A bucket is an hour, so the
        rule applies only when every line of ``account`` in force during the
        hour is a saved-key one: any part of the hour under the helper line
        (or a login) keeps the usage on the key, the side that counts the
        key's spend high. The session is compared with the earliest of those
        periods, and its usage goes to the account in force just before that
        period, unless that is ``account`` itself (a helper line before the
        saved-key one: the session was on the key all along)."""
        if session_start is None:
            return None
        lo, hi = hour * 3600, (hour + 1) * 3600
        acts = self.activations
        hows, starts = self._periods()
        # The lines in force during the hour: each line from the last one at
        # or before its start (the next line comes after the start) to the
        # last one before its end.
        first_line = max(0, bisect.bisect_right(self._times, lo) - 1)
        end_line = bisect.bisect_left(self._times, hi)
        in_force = [i for i in range(first_line, end_line) if acts[i].account == account]
        if not in_force or any(hows[i] != "saved-key" for i in in_force):
            return None
        first = min(starts[i] for i in in_force)
        if session_start >= acts[first].ts:
            return None
        before = acts[first - 1].account if first > 0 else ""
        return None if before == account else before

    def numbers(self) -> dict[str, int]:
        """Last known account number per identity, for accounts since removed."""
        out: dict[str, int] = {}
        for a in self.activations:
            if a.number is not None:
                out[a.account] = a.number
        return out
