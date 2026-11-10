"""The activation log: which account was live when.

Transcripts do not say which account produced them, so each provider's data
root carries an append-only ``activations.jsonl``. One JSON object per line::

    {"ts": "2026-11-10T14:12:26.123Z", "number": 2, "email": "a@example.com",
     "org": "", "source": "switch"}

``source`` is ``"switch"`` when qswap switched the live login, and
``"observed"`` when qswap noticed that the live login differs from the last
line (a manual ``/login``, another tool). An event belongs to the account whose
interval contains its timestamp; usage before the first line is unattributed.

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
        _append(
            path,
            {
                "ts": _iso(time.time() if now is None else now),
                "number": num,
                "email": email,
                "org": org or "",
                "source": source if source in SOURCES else "switch",
            },
        )
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
            root, number=number, email=email, org=org, source="observed", now=now
        )
    except Exception:
        return False


@dataclass(frozen=True)
class Activation:
    ts: float
    account: str  # identity_key
    number: int | None
    source: str


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

    def numbers(self) -> dict[str, int]:
        """Last known account number per identity, for accounts since removed."""
        out: dict[str, int] = {}
        for a in self.activations:
            if a.number is not None:
                out[a.account] = a.number
        return out
