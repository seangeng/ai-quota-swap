"""Claude Code's user settings file, and the one key qswap writes in it.

docs/design/api-key-sessions.md, part 2: an API-key account is made active by
adding ``"apiKeyHelper": "<qswap> key-helper N"`` to Claude Code's user
settings (``<config home>/settings.json``, where the config home is
``CLAUDE_CONFIG_DIR`` or ``~/.claude``). Every Claude Code process that reads
that file moves to the key within a few seconds, running or new, and moves
back to its claude.ai login when the key is removed. This module reads that
file and edits that one key, and nothing else in it.

Reading gives a :class:`HelperState`:

``no-file``
    The file does not exist.
``none``
    The file is a JSON object without ``apiKeyHelper``.
``ours``
    ``apiKeyHelper`` is qswap's line for account ``number``. The line is
    recognised by its structure (a ``qswap`` or ``ai-quota-swap`` command, or
    ``python -m ai_quota_swap``, then ``key-helper`` and a number), not by an
    exact path, so a reinstall that moved the command still reads as ours.
``foreign``
    ``apiKeyHelper`` is someone else's. Its value is never shown: a helper
    command may carry a key.
``invalid``
    The file is not a JSON object (a parse error, or another JSON value).

Writing changes that one key and keeps everything else as it was: the other
keys and their order, the indentation, the trailing newline and the file
mode. The change is made in the text itself (one member added, replaced or
removed), and the result is checked by parsing it; only if the text edit does
not give the expected object is the file written again in its own indentation.
A write is atomic (a temporary file renamed over the target), goes through a
symbolic link rather than over it, and re-reads the file immediately before
the rename: Claude Code writes this file too, so when the file changed since
it was read the edit starts again from the new contents. Nothing is ever
written to a file that cannot be parsed or holds another helper.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: The key qswap owns in Claude Code's settings.
HELPER_KEY = "apiKeyHelper"
#: The hidden verb every helper line runs.
HELPER_VERB = "key-helper"
#: Names the installed command goes by.
COMMAND_NAMES = ("qswap", "ai-quota-swap")
#: How often a write is tried again when the file keeps changing under it.
WRITE_ATTEMPTS = 5

NO_FILE, NONE, OURS, FOREIGN, INVALID = "no-file", "none", "ours", "foreign", "invalid"


class SettingsError(Exception):
    """qswap did not change the settings file. ``reason`` is what follows the
    file's path in a message ("is not valid JSON (...)"); ``kind`` is the state that stopped
    it (``invalid``, ``foreign``, ``write``, ``changing``)."""

    def __init__(self, reason: str, *, kind: str, path: Path | None = None):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.path = path


@dataclass(frozen=True)
class HelperState:
    """What the settings file says about ``apiKeyHelper`` (see the module)."""

    kind: str
    path: Path
    number: str | None = None
    error: str | None = None
    #: qswap's own line (``ours`` only; a foreign helper's value is never kept).
    command: str | None = None

    @property
    def stale_command(self) -> bool:
        """qswap's line runs a command that is not there any more (qswap was
        reinstalled elsewhere): Claude Code would fail to get the key."""
        return self.ours and command_missing(self.command)

    @property
    def ours(self) -> bool:
        return self.kind == OURS

    @property
    def installable(self) -> bool:
        """Whether qswap may write its line into this file."""
        return self.kind in (NO_FILE, NONE, OURS)

    def reason(self) -> str | None:
        """Why qswap cannot write its line here (what follows the path in a
        message); None when it can."""
        if self.kind == FOREIGN:
            return "already sets another apiKeyHelper"
        if self.kind == INVALID:
            return f"is not valid JSON ({self.error or 'parse error'})"
        return None

    def shown_path(self) -> str:
        """The path as messages show it (``~/.claude/settings.json``)."""
        return shown_path(self.path)


def shown_path(path: Path) -> str:
    """``path`` with the home directory spelled ``~``."""
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def settings_path() -> Path:
    """Claude Code's user settings file: ``<config home>/settings.json``."""
    from ai_quota_swap.paths import get_claude_config_home

    return get_claude_config_home() / "settings.json"


# -- recognising qswap's line -------------------------------------------------------

_LINE_RE = re.compile(
    r"""^\s*(?P<exe>"[^"]+"|'[^']+'|\S+)(?P<mod>\s+-m\s+ai_quota_swap)?"""
    r"""\s+key-helper\s+(?P<num>\d+)\s*$"""
)


def _base_name(token: str) -> str:
    token = token.strip().strip("\"'")
    name = re.split(r"[\\/]", token)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


def parse_line(value: object) -> str | None:
    """The account number a helper command is qswap's line for, or None."""
    if not isinstance(value, str):
        return None
    m = _LINE_RE.match(value)
    if not m:
        return None
    base = _base_name(m.group("exe"))
    if m.group("mod"):
        if not base.startswith("python") and base != "py":
            return None
    elif base not in COMMAND_NAMES:
        return None
    return str(int(m.group("num")))


def command_missing(command: str | None) -> bool:
    """Whether qswap's line names a program by absolute path that does not
    exist. A bare name (found on PATH by the shell) is not checked."""
    if not command:
        return False
    m = _LINE_RE.match(command)
    if not m:
        return False
    exe = m.group("exe").strip().strip("\"'")
    if not (os.path.isabs(exe) or re.match(r"^[A-Za-z]:[\\/]", exe)):
        return False
    return not os.path.exists(exe)


def _quote(path: str) -> str:
    if re.search(r"""[\s"'$`\\!*?&;|<>()]""", path) and not (
        sys.platform == "win32" and "\\" in path and not re.search(r"\s", path)
    ):
        return '"' + path.replace('"', '\\"') + '"'
    return path


def qswap_executable() -> list[str]:
    """The command that runs this qswap, as argv: the installed ``qswap``
    script by absolute path, or this interpreter with ``-m ai_quota_swap``."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and _base_name(argv0) in COMMAND_NAMES:
        found = shutil.which(argv0) if not os.path.isabs(argv0) else argv0
        if found and os.path.isfile(found):
            return [os.path.abspath(found)]
    for name in COMMAND_NAMES:
        beside = Path(sys.executable).parent / (name + (".exe" if sys.platform == "win32" else ""))
        if beside.is_file():
            return [str(beside)]
    for name in COMMAND_NAMES:
        found = shutil.which(name)
        if found:
            return [os.path.abspath(found)]
    return [sys.executable, "-m", "ai_quota_swap"]


def helper_command(number: str | int) -> str:
    """qswap's line for account ``number``: ``/abs/path/qswap key-helper 5``."""
    parts = [_quote(p) for p in qswap_executable()]
    return " ".join([*parts, HELPER_VERB, str(int(number))])


# -- reading ------------------------------------------------------------------------


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8")
    return text[1:] if text.startswith("﻿") else text


def _parse(text: str) -> tuple[dict | None, str | None]:
    if not text.strip():
        return {}, None
    try:
        obj = json.loads(text)
    except ValueError as e:
        return None, f"line {getattr(e, 'lineno', '?')}: {getattr(e, 'msg', 'parse error')}"
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    return obj, None


def state_of_text(text: str | None, path: Path) -> HelperState:
    """The :class:`HelperState` of a file's text (None: no file)."""
    if text is None:
        return HelperState(NO_FILE, path)
    obj, error = _parse(text)
    if obj is None:
        return HelperState(INVALID, path, error=error)
    if _top_level_count(text, HELPER_KEY) > 1:
        return HelperState(INVALID, path, error=f"{HELPER_KEY} appears twice")
    if HELPER_KEY not in obj:
        return HelperState(NONE, path)
    number = parse_line(obj[HELPER_KEY])
    if number is None:
        return HelperState(FOREIGN, path)
    return HelperState(OURS, path, number=number, command=obj[HELPER_KEY])


def _read(path: Path) -> str | None:
    try:
        return _decode(path.read_bytes())
    except FileNotFoundError:
        return None


def read_state(path: Path | None = None) -> HelperState:
    """The state of Claude Code's user settings (or of ``path``). A file that
    exists but cannot be read is ``invalid``. Never raises."""
    path = settings_path() if path is None else path
    try:
        text = _read(path)
    except (OSError, UnicodeDecodeError) as e:
        return HelperState(INVALID, path, error=f"could not be read: {type(e).__name__}")
    return state_of_text(text, path)


# -- the text editor ------------------------------------------------------------------


_WS = " \t\r\n"


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in _WS:
        i += 1
    return i


def _skip_string(text: str, i: int) -> int:
    """``i`` at an opening quote; the index after the closing one."""
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _skip_value(text: str, i: int) -> int:
    c = text[i]
    if c == '"':
        return _skip_string(text, i)
    if c in "{[":
        depth = 0
        while i < len(text):
            c = text[i]
            if c == '"':
                i = _skip_string(text, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise ValueError("unterminated value")
    j = i
    while j < len(text) and text[j] not in ",}]" + _WS:
        j += 1
    return j


@dataclass(frozen=True)
class _Member:
    key: str
    start: int  # the key's opening quote
    value_start: int
    end: int  # after the value


def _members(text: str) -> tuple[int, int, list[_Member]]:
    """``(open, close, members)`` of the top-level object: the index of its
    ``{`` and ``}``, and each member's span. Raises ValueError."""
    i = _skip_ws(text, 0)
    if i >= len(text) or text[i] != "{":
        raise ValueError("not an object")
    open_at = i
    i += 1
    out: list[_Member] = []
    while True:
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == "}":
            return open_at, i, out
        if i >= len(text) or text[i] != '"':
            raise ValueError("expected a key")
        key_start = i
        i = _skip_string(text, i)
        key = json.loads(text[key_start:i])
        i = _skip_ws(text, i)
        if text[i] != ":":
            raise ValueError("expected a colon")
        i = _skip_ws(text, i + 1)
        value_start = i
        i = _skip_value(text, i)
        out.append(_Member(key, key_start, value_start, i))
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == ",":
            i += 1
            continue
        if i < len(text) and text[i] == "}":
            return open_at, i, out
        raise ValueError("expected a comma")


def _top_level_count(text: str, key: str) -> int:
    try:
        return sum(1 for m in _members(text)[2] if m.key == key)
    except (ValueError, IndexError):
        return 0


def _indent_unit(text: str, members: list[_Member]) -> str | None:
    """The indentation of the top-level members, or None for a one-line file."""
    if not members:
        return None
    line_start = text.rfind("\n", 0, members[0].start)
    if line_start < 0:
        return None
    lead = text[line_start + 1 : members[0].start]
    return lead if lead.strip() == "" else None


def _detected_indent(text: str) -> str | int | None:
    try:
        unit = _indent_unit(text, _members(text)[2])
    except (ValueError, IndexError):
        return 2
    if unit is None:
        return None
    return unit if "\t" in unit else len(unit)


def _separator(text: str, members: list[_Member]) -> str:
    """``": "`` or ``":"``, as the file writes them."""
    for m in members:
        key_end = _skip_string(text, m.start)
        between = text[key_end : m.value_start]
        return between if between.strip() == ":" else ": "
    return ": "


def _edit_text(text: str, value: str | None) -> str:
    """``text`` with ``apiKeyHelper`` set to ``value`` (None: removed)."""
    open_at, close_at, members = _members(text)
    encoded = json.dumps(value, ensure_ascii=False) if value is not None else None
    found = [m for m in members if m.key == HELPER_KEY]
    if found:
        m = found[0]
        if encoded is not None:
            return text[: m.value_start] + encoded + text[m.end :]
        index = members.index(m)
        if index > 0:
            # Remove from the end of the previous value, which takes the
            # comma before this member with it.
            prev = members[index - 1]
            return text[: prev.end] + text[m.end :]
        if len(members) > 1:
            nxt = members[1]
            return text[: m.start] + text[nxt.start :]
        # The only member: leave the object empty, keeping what came around it.
        inner = text[open_at + 1 : close_at]
        newline = "\n" if "\n" in inner else ""
        return text[: open_at + 1] + newline + text[close_at:]
    if encoded is None:
        return text
    key = json.dumps(HELPER_KEY)
    sep = _separator(text, members)
    unit = _indent_unit(text, members)
    if not members:
        inner = text[open_at + 1 : close_at]
        if "\n" in inner or text.count("\n") > 1:
            return text[: open_at + 1] + f"\n  {key}{sep}{encoded}\n" + text[close_at:]
        return text[: open_at + 1] + f"{key}{sep}{encoded}" + text[close_at:]
    last = members[-1]
    if unit is None:
        return text[: last.end] + f", {key}{sep}{encoded}" + text[last.end :]
    return text[: last.end] + f",\n{unit}{key}{sep}{encoded}" + text[last.end :]


def edited(text: str | None, value: str | None) -> str | None:
    """The file's new text with ``apiKeyHelper`` set to ``value`` (None:
    removed); None when the file should not exist (it did not, and nothing is
    to be added). Raises :class:`SettingsError` when ``text`` cannot be parsed."""
    if text is None or not text.strip():
        if value is None:
            return text
        return json.dumps({HELPER_KEY: value}, indent=2, ensure_ascii=False) + "\n"
    obj, error = _parse(text)
    if obj is None:
        raise SettingsError(f"is not valid JSON ({error})", kind=INVALID)
    expected = dict(obj)
    if value is None:
        expected.pop(HELPER_KEY, None)
    else:
        expected[HELPER_KEY] = value
    try:
        out = _edit_text(text, value)
        if json.loads(out) == expected:
            return out
    except (ValueError, IndexError):
        pass
    # The text edit did not give the expected object: write the whole object
    # in the file's own indentation (key order is kept by the dict).
    indent = _detected_indent(text)
    out = json.dumps(expected, indent=indent, ensure_ascii=False)
    return out + ("\n" if text.endswith("\n") else "")


def without_helper(text: str) -> str:
    """``text`` with any ``apiKeyHelper`` removed (for a run profile's copy).
    Text that cannot be parsed is returned as it is."""
    try:
        out = edited(text, None)
    except SettingsError:
        return text
    return text if out is None else out


# -- writing --------------------------------------------------------------------------


def _target(path: Path) -> Path:
    from ai_quota_swap.fsutil import publish_target

    return publish_target(path)


def _mode_of(path: Path) -> int | None:
    try:
        return os.stat(path).st_mode & 0o7777
    except OSError:
        return None


def _publish(path: Path, text: str | None, mode: int | None) -> None:
    from ai_quota_swap.fsutil import atomic_write_text

    target = _target(path)
    if text is None:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, text, mode=0o600 if mode is None else mode)


def _change(path: Path, value: str | None, *, remove_if_empty: bool = False,
            want: str | None = None) -> tuple[str | None, str | None]:
    """Set (or remove) the helper, re-reading before the write and starting
    again when the file changed. Returns ``(before, after)`` texts."""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            before = _read(path)
        except (OSError, UnicodeDecodeError) as e:
            raise SettingsError(f"could not be read ({type(e).__name__})", kind=INVALID,
                                path=path) from e
        state = state_of_text(before, path)
        if state.kind == INVALID:
            raise SettingsError(f"is not valid JSON ({state.error})", kind=INVALID, path=path)
        if state.kind == FOREIGN:
            raise SettingsError("already sets another apiKeyHelper", kind=FOREIGN, path=path)
        if value is None and state.kind != OURS:
            return before, before
        if want is not None and value is None and state.number != want:
            return before, before
        after = edited(before, value)
        if value is None and remove_if_empty and after is not None:
            obj, _ = _parse(after)
            if obj == {}:
                after = None
        if after == before:
            return before, after
        mode = _mode_of(path)
        try:
            again = _read(path)
        except (OSError, UnicodeDecodeError):
            again = None if before is None else ""
        if again != before:
            time.sleep(0.01 * (attempt + 1))
            continue
        try:
            _publish(path, after, mode)
        except OSError as e:
            raise SettingsError(
                f"could not be written ({e.strerror or type(e).__name__})",
                kind="write", path=path,
            ) from e
        return before, after
    raise SettingsError("kept changing while qswap wrote it", kind="changing", path=path)


def install(number: str | int, *, path: Path | None = None,
            command: str | None = None) -> tuple[bool, HelperState]:
    """Put qswap's line for ``number`` in the settings (replacing qswap's line
    for another account). Returns ``(created, state)``: ``created`` when there
    was no file before. Raises :class:`SettingsError` and writes nothing when
    the file cannot be parsed or holds another helper."""
    path = settings_path() if path is None else path
    value = command if command is not None else helper_command(number)
    before, _after = _change(path, value)
    return before is None, read_state(path)


def remove(*, path: Path | None = None, remove_file_if_empty: bool = False,
           number: str | None = None) -> bool:
    """Take qswap's line out of the settings (only qswap's; ``number`` limits
    it to the line for that account). With ``remove_file_if_empty`` a file
    left holding nothing is removed (qswap created it). Returns whether
    anything changed. Raises :class:`SettingsError` when the file cannot be
    parsed; another helper is left alone (and is not an error)."""
    path = settings_path() if path is None else path
    try:
        before, after = _change(path, None, remove_if_empty=remove_file_if_empty, want=number)
    except SettingsError as e:
        if e.kind == FOREIGN:
            return False
        raise
    return before != after
