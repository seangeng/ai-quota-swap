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
"""

from __future__ import annotations

import bisect
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FILENAME = "activations.jsonl"
SOURCES = ("switch", "observed")
HOWS = ("login", "helper", "saved-key")

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
    """Epoch seconds from an ISO-8601 string or a number; None when unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
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


@dataclass(frozen=True)
class Activation:
    ts: float
    account: str  # identity_key
    number: int | None
    source: str
    how: str = ""


class ActivationLog:
    """The parsed log, ready for interval lookups."""

    def __init__(self, activations: list[Activation]):
        self.activations = sorted(activations, key=lambda a: a.ts)
        self._times = [a.ts for a in self.activations]

    @classmethod
    def load(cls, root: Path) -> ActivationLog:
        """Read ``<root>/activations.jsonl``; a missing or torn file reads as empty."""
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
        return cls(out)

    def __len__(self) -> int:
        return len(self.activations)

    @property
    def first_ts(self) -> float | None:
        return self._times[0] if self._times else None

    def account_at(self, ts: float) -> str:
        """The account live at ``ts``; ``""`` (unattributed) before the first line."""
        i = bisect.bisect_right(self._times, ts) - 1
        return self.activations[i].account if i >= 0 else ""

    def saved_key_periods(self) -> bool:
        """Whether any line activated an API key as a saved key."""
        return any(a.how == "saved-key" for a in self.activations)

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
        hows: list[str] = []
        starts: list[int] = []
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

    def numbers(self) -> dict[str, int]:
        """Last known account number per identity, for accounts since removed."""
        out: dict[str, int] = {}
        for a in self.activations:
            if a.number is not None:
                out[a.account] = a.number
        return out
