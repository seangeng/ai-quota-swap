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
    ``python -P -m ai_quota_swap``, or ``-m`` alone as older lines have it,
    then ``key-helper`` and a number), not by an exact path, so a reinstall
    that moved the command still reads as ours. Every line qswap writes reads
    back this way: the quoting is a round trip on each platform, and a line
    that would not read back is refused before it is written.
``foreign``
    ``apiKeyHelper`` is someone else's. Its value is never shown: a helper
    command may carry a key.
``invalid``
    The file is not a JSON object (a parse error, or another JSON value).

Writing changes that one key and keeps everything else as it was: the other
keys and their order, the indentation, the trailing newline, a byte order
mark and the file mode. The change is made in the text itself (one member
added, replaced or removed), and the result is checked by parsing it; only if
the text edit does not give the expected object is the file written again in
its own indentation. qswap's member goes in first, so in a file with one
member per line deleting that line by hand leaves the file as it was, and
taking it out again gives back the same bytes (an empty object keeps its own
spelling). A write is atomic (a temporary file renamed over the target), goes
through a symbolic link rather than over it, and re-reads the file
immediately before the rename: Claude Code writes this file too, so when the
file changed since it was read the edit starts again from the new contents.
Nothing is ever written to a file that cannot be parsed or holds another
helper.

Whether the line can give Claude Code the key is :func:`check_command`: the
line's own command, run with ``--check`` the way Claude Code runs it. Doctor
reports it, and a switch to the active key writes the line again when it
does not answer (:func:`line_answers`).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap.installed import COMMAND_NAMES  # names the installed command goes by

#: The key qswap owns in Claude Code's settings.
HELPER_KEY = "apiKeyHelper"
#: The hidden verb every helper line runs.
HELPER_VERB = "key-helper"
#: How often a write is tried again when the file keeps changing under it.
WRITE_ATTEMPTS = 5

NO_FILE, NONE, OURS, FOREIGN, INVALID = "no-file", "none", "ours", "foreign", "invalid"


class SettingsError(Exception):
    """qswap did not change the settings file. ``reason`` is what follows the
    file's path in a message ("is not valid JSON (...)"); ``kind`` is the state
    that stopped it (``invalid``, ``unreadable``: reading it failed,
    ``foreign``, ``write``, ``changing``, or ``command``: the line would not
    read back as qswap's)."""

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
            if self.unreadable:
                return self.error  # "could not be read: PermissionError"
            return f"is not valid JSON ({self.error or 'parse error'})"
        return None

    @property
    def unreadable(self) -> bool:
        """The file is there but reading it failed (its permissions, say),
        rather than read and not parsed."""
        return self.kind == INVALID and (self.error or "").startswith("could not be read")

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
#
# The reading side is the exact inverse of _quote on each platform: on POSIX a
# word is single-quoted (taken as it is), double-quoted with backslash escapes
# (undone as sh undoes them), or bare; on Windows cmd.exe takes nothing out of
# a double-quoted word, so neither does qswap (a UNC path keeps its leading
# backslashes, and a backslash before $ stays).

# A bare word holds only what _quote leaves bare: the complement of the
# characters it quotes. Anything else (; $( ) a backtick | & > ...) must be
# quoted to read as qswap's, so a line the shell would run as two commands is
# never taken for qswap's own, shown as active, or run by doctor's --check.
# On POSIX a backslash, which only escapes the next character and cannot end a
# command, is let through, so a line a Windows qswap wrote still reads as one.
_POSIX_BARE = r"""[^\s"'$`!*?&;|<>()\[\]{}#~=]+"""
_WINDOWS_BARE = r"""[^\s"'$`!*?&;|<>()^%,=]+"""
_POSIX_WORD = rf"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|{_POSIX_BARE})"""
_WINDOWS_WORD = rf"""(?:"[^"]*"|{_WINDOWS_BARE})"""


def _line_re() -> re.Pattern:
    word = _WINDOWS_WORD if sys.platform == "win32" else _POSIX_WORD
    return re.compile(
        rf"""^\s*(?P<exe>{word})(?P<mod>(?:\s+-P)?\s+-m\s+ai_quota_swap)?"""
        rf"""\s+key-helper\s+(?P<num>\d+)"""
        rf"""(?:\s+--data-dir\s+(?P<dir>{word}))?\s*$""",
        re.DOTALL,
    )


def _unquote(token: str) -> str:
    """A word as the shell Claude Code runs the line in reads it."""
    token = token.strip()
    if sys.platform == "win32":
        if len(token) >= 2 and token[0] == token[-1] == '"':
            return token[1:-1]
        return token
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1]
    if len(token) >= 2 and token[0] == token[-1] == '"':
        # Inside double quotes sh undoes a backslash before \ " $ ` and drops
        # one before a newline; any other backslash stays.
        return re.sub(r'\\([\\"$`\n])', lambda m: "" if m.group(1) == "\n" else m.group(1),
                      token[1:-1])
    # A bare word: _quote writes one only when it holds nothing the shell
    # would change.
    return token


def _base_name(token: str) -> str:
    token = _unquote(token)
    name = re.split(r"[\\/]", token)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


@dataclass(frozen=True)
class HelperLine:
    """qswap's line taken apart, each word as the shell reads it."""

    #: The program the line runs: qswap's console script, or an interpreter.
    program: str
    number: str
    #: ``-m`` or ``-P -m`` when ``program`` is an interpreter, else None.
    module: str | None = None
    data_dir: str | None = None


def read_line(value: object) -> HelperLine | None:
    """qswap's line taken apart, or None when ``value`` is not one: a
    ``qswap`` or ``ai-quota-swap`` program, or a Python interpreter with
    ``-P -m ai_quota_swap`` (``-m`` alone, as older lines have it), then
    ``key-helper N`` and optionally ``--data-dir DIR``."""
    if not isinstance(value, str):
        return None
    m = _line_re().match(value)
    if not m:
        return None
    base = _base_name(m.group("exe"))
    module = None
    if m.group("mod"):
        if not base.startswith("python") and base != "py":
            return None
        module = "-P -m" if re.search(r"\s-P\s", m.group("mod")) else "-m"
    elif base not in COMMAND_NAMES:
        return None
    return HelperLine(
        program=_unquote(m.group("exe")),
        number=str(int(m.group("num"))),
        module=module,
        data_dir=_unquote(m.group("dir")) if m.group("dir") else None,
    )


def parse_line(value: object) -> str | None:
    """The account number a helper command is qswap's line for, or None."""
    line = read_line(value)
    return None if line is None else line.number


def _is_absolute(path: str) -> bool:
    import ntpath
    import posixpath

    if sys.platform == "win32":
        return ntpath.isabs(path)
    return posixpath.isabs(path)


def command_missing(command: str | None) -> bool:
    """Whether qswap's line names a program by absolute path that does not
    exist. A bare name (found on PATH by the shell) is not checked here."""
    line = read_line(command)
    if line is None or not _is_absolute(line.program):
        return False
    return not os.path.exists(line.program)


# -- whether the line answers -------------------------------------------------------

#: How long a check of the line waits for its command. Claude Code 2.1.322
#: says "apiKeyHelper is taking a while" after 10 seconds.
CHECK_TIMEOUT = 10.0
_HELPER_PREFIX = "qswap key-helper: "


def command_problem(command: str | None) -> str | None:
    """Why qswap's line cannot answer, from the line and the file it runs
    alone (no process is started); None when nothing there is wrong. The
    reason follows "the line" in a sentence."""
    line = read_line(command)
    if line is None:
        return "is not a key line of qswap's"
    if not _is_absolute(line.program):
        return (
            "runs qswap by name only, which a session without it on its PATH "
            "cannot find"
        )
    if command_missing(command):
        return "runs a qswap that is no longer there"
    if not os.path.isfile(line.program):
        return f"runs {line.program}, which is not a program"
    if sys.platform != "win32" and not os.access(line.program, os.X_OK):
        return f"runs {line.program}, which is not executable"
    if line.module == "-m":
        return (
            "runs Python with -m, which can import files from the project "
            "directory of each session in place of qswap"
        )
    return None


def _check_dir() -> str:
    """Where the check runs: qswap's own data directory (a directory of
    nobody's project), else the home directory."""
    from ai_quota_swap import key_helper

    for candidate in (key_helper.data_dir(), Path.home()):
        if candidate.is_dir():
            return str(candidate)
    return os.getcwd()


def _run_check(command: str, *, cwd: str, timeout: float) -> tuple[int, str]:
    """Run qswap's line with ``--check`` added, the way Claude Code runs the
    line (through the shell). Returns the exit status and standard error;
    standard output is never read. On a timeout the command and everything
    it started are stopped and ``subprocess.TimeoutExpired`` is raised."""
    import signal
    import subprocess

    text = command + " --check"
    if sys.platform == "win32":
        argv: str | list[str] = text
        options: dict = {"shell": True}
    else:
        argv = ["/bin/sh", "-c", text]
        options = {"start_new_session": True}
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, **options,
    )
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform == "win32":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)  # a child that left the group may hold the pipe
        except subprocess.TimeoutExpired:
            pass
        raise
    return proc.returncode, (err or b"").decode("utf-8", "replace")


def check_command(command: str | None, *, timeout: float = CHECK_TIMEOUT,
                  cwd: str | None = None) -> str | None:
    """Whether qswap's line can give Claude Code the key: None when its
    command, run with ``--check`` the way Claude Code runs it, answers; else
    why not, in words that follow "the line" ("fails (#5 has no saved key;
    save one: qswap add-token KEY --slot 5)"). Of what the command prints
    only the helper's own one-line message is ever shown. Never raises."""
    import subprocess

    problem = command_problem(command)
    if problem is not None:
        return problem
    try:
        code, err = _run_check(command or "", cwd=cwd or _check_dir(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"did not answer within {timeout:g} seconds"
    except Exception as e:
        return f"could not be run ({getattr(e, 'strerror', None) or type(e).__name__})"
    if code == 0:
        return None
    first = (err.strip().splitlines() or [""])[0]
    if first.startswith(_HELPER_PREFIX):
        return f"fails ({first[len(_HELPER_PREFIX):]})"
    return f"fails (exit {code})"


def line_answers(state: HelperState) -> str | None:
    """Whether qswap's line in ``state`` answers (:func:`check_command`):
    None when it does, else the reason. A switch to the key that is already
    active calls this and writes the line again when it does not answer."""
    if not state.ours:
        return "is not a key line of qswap's"
    return check_command(state.command)


def _quote(path: str) -> str:
    """``path`` as one word for the shell Claude Code runs the helper in.
    :func:`read_line` reads back every word this writes. On Windows (cmd.exe)
    double quotes, which a Windows path cannot contain. On POSIX single
    quotes, inside which ``sh`` expands nothing; a path with a single quote
    in it gets double quotes with ``\\``, ``"``, ``$`` and the backtick
    escaped."""
    if sys.platform == "win32":
        if re.search(r"""[\s"'$`!*?&;|<>()^%,=]""", path):
            return '"' + path + '"'
        return path
    if not re.search(r"""[\s"'$`\\!*?&;|<>()\[\]{}#~=]""", path):
        return path
    if "'" not in path:
        return "'" + path + "'"
    return '"' + re.sub(r'([\\"$`])', r"\\\1", path) + '"'


def qswap_executable() -> list[str]:
    """The command that runs this qswap, as argv: this install's console
    script (the PATH link to it when there is one), or this interpreter with
    ``-P -m ai_quota_swap`` when it has none. The menu bar's LaunchAgent
    uses the same resolver (:func:`ai_quota_swap.installed.command`)."""
    from ai_quota_swap import installed

    return installed.command()


def helper_command(number: str | int) -> str:
    """qswap's line for account ``number``: ``/abs/path/qswap key-helper 5``.
    On Linux the data directory is named too (``--data-dir``): Claude Code
    runs the line with its own environment, whose ``XDG_DATA_HOME`` may
    differ from qswap's in either direction."""
    from ai_quota_swap import key_helper

    parts = [_quote(p) for p in qswap_executable()]
    line = [*parts, HELPER_VERB, str(int(number))]
    pinned = key_helper.pinned_data_dir()
    if pinned is not None:
        line += ["--data-dir", _quote(str(pinned))]
    return " ".join(line)


# -- reading ------------------------------------------------------------------------


#: A UTF-8 byte order mark. Some editors write one; qswap reads past it and
#: keeps it on every write.
_BOM = "\ufeff"


def _decode(raw: bytes) -> str:
    """The file's text as it is, a byte order mark included."""
    return raw.decode("utf-8")


def _split_bom(text: str | None) -> tuple[str, str | None]:
    """``(mark, text)``: the byte order mark (or ``""``) and the text after it."""
    if text is not None and text.startswith(_BOM):
        return _BOM, text[1:]
    return "", text


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
    _bom, text = _split_bom(text)
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


def _split_lead(text: str, open_at: int, close_at: int, members: list[_Member]) -> bool:
    """Whether the file has several lines inside its braces while its first
    member shares the brace's line: the new line then goes on a line of its
    own, splitting the brace's line (:func:`brace_lead`)."""
    return bool(members) and "\n" not in text[open_at + 1 : members[0].start] and (
        "\n" in text[open_at + 1 : close_at]
    )


def _member_indent(text: str, members: list[_Member]) -> str:
    """The indentation of the first member that starts a line of its own,
    else two spaces."""
    for m in members[1:]:
        line_start = text.rfind("\n", 0, m.start)
        lead = text[line_start + 1 : m.start]
        if line_start >= 0 and lead.strip() == "":
            return lead
    return "  "


def _edit_text(text: str, value: str | None, *, restore_lead: str | None = None) -> str:
    """``text`` with ``apiKeyHelper`` set to ``value`` (None: removed).

    A new line goes in as the first member, followed by what separated the
    brace from the old first member: in a file with one member per line it
    is a whole line of its own ending in a comma, so deleting that line by
    hand leaves the file as it was (a last member would leave a trailing
    comma on the line above). Taking the first member out removes exactly
    that text again, and taking out the only member removes it with the
    whitespace before it, so every insertion here is undone byte for byte.
    A file of several lines whose first member shares the brace's line gets
    the line on a line of its own too, after the brace (in the indentation
    of the members below), so deleting it by hand still leaves valid JSON;
    ``restore_lead`` (what :func:`brace_lead` read before) puts the brace's
    line back as it was when the line comes out."""
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
            if restore_lead is not None and "\n" in text[open_at + 1 : m.start] and (
                "\n" not in restore_lead
            ):
                # The brace's line was split for the line: join it again.
                return text[: open_at + 1] + restore_lead + text[nxt.start :]
            return text[: m.start] + text[nxt.start :]
        # The only member: the object is left with what followed it.
        return text[: open_at + 1] + text[m.end :]
    if encoded is None:
        return text
    member = f"{json.dumps(HELPER_KEY)}{_separator(text, members)}{encoded}"
    if not members:
        # One line or several is decided by the text inside the braces only.
        inner = text[open_at + 1 : close_at]
        if "\n" in inner:
            newline = "\r\n" if "\r\n" in inner else "\n"
            return text[: open_at + 1] + f"{newline}  {member}" + inner + text[close_at:]
        return text[: open_at + 1] + member + inner + text[close_at:]
    first = members[0]
    lead = text[open_at + 1 : first.start]
    if _split_lead(text, open_at, close_at, members):
        newline = "\r\n" if "\r\n" in text else "\n"
        indent = _member_indent(text, members)
        return (
            text[: open_at + 1] + newline + indent + member + "," + newline + indent
            + text[first.start :]
        )
    if "\n" in lead:
        between = "," + lead
    elif len(members) > 1:
        between = text[first.end : members[1].start]  # the file's own ", " or ","
    else:
        between = ", " if lead or _separator(text, members).endswith(" ") else ","
    return text[: first.start] + member + between + text[first.start :]


def edited(text: str | None, value: str | None, *, restore_lead: str | None = None) -> str | None:
    """The file's new text with ``apiKeyHelper`` set to ``value`` (None:
    removed); None when the file should not exist (it did not, and nothing is
    to be added). ``restore_lead``: see :func:`_edit_text`. Raises
    :class:`SettingsError` when ``text`` cannot be parsed."""
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
    elif HELPER_KEY in expected:
        expected[HELPER_KEY] = value
    else:
        expected = {HELPER_KEY: value, **expected}  # first, as the text edit puts it
    try:
        out = _edit_text(text, value, restore_lead=restore_lead)
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
    """``text`` with any ``apiKeyHelper`` removed (for a run profile's copy),
    read past a byte order mark (which the copy keeps). Text that cannot be
    parsed is returned as it is."""
    bom, body = _split_bom(text)
    try:
        out = edited(body, None)
    except SettingsError:
        return text
    return text if out is None else bom + out


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
            want: str | None = None, restore_blank: str | None = None,
            restore_lead: str | None = None) -> tuple[str | None, str | None]:
    """Set (or remove) the helper, re-reading before the write and starting
    again when the file changed. Returns ``(before, after)`` texts (without a
    byte order mark; a file that has one keeps it). ``restore_blank``: the
    text the file held before qswap's line when it held no settings
    (:func:`blank_text`), put back when removing the line leaves an empty
    object."""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            raw = _read(path)
        except (OSError, UnicodeDecodeError) as e:
            raise SettingsError(f"could not be read: {type(e).__name__}", kind="unreadable",
                                path=path) from e
        bom, before = _split_bom(raw)
        state = state_of_text(before, path)
        if state.kind == INVALID:
            raise SettingsError(f"is not valid JSON ({state.error})", kind=INVALID, path=path)
        if state.kind == FOREIGN:
            raise SettingsError("already sets another apiKeyHelper", kind=FOREIGN, path=path)
        if value is None and state.kind != OURS:
            return before, before
        if want is not None and value is None and state.number != want:
            return before, before
        after = edited(before, value, restore_lead=restore_lead)
        if value is None and after is not None and (remove_if_empty or restore_blank is not None):
            obj, _ = _parse(after)
            if obj == {}:
                after = None if remove_if_empty else restore_blank
        if after == before:
            return before, after
        mode = _mode_of(path)
        try:
            again = _read(path)
        except (OSError, UnicodeDecodeError):
            again = None if raw is None else ""
        if again != raw:
            time.sleep(0.01 * (attempt + 1))
            continue
        try:
            _publish(path, None if after is None else bom + after, mode)
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
    if parse_line(value) != str(int(number)):
        # qswap could neither recognise nor remove a line it cannot read back.
        raise SettingsError(
            "would get a key line qswap cannot read back as its own", kind="command",
            path=path,
        )
    before, _after = _change(path, value)
    return before is None, read_state(path)


def blank_text(path: Path | None = None) -> str | None:
    """The file's text when it exists and holds no settings: only whitespace
    (Claude Code reads that as no settings) or an empty object in any
    spelling (``{ }``, ``{\\n\\n}``, ``{}`` with blank lines around it).
    None otherwise. Recorded when a key is entered and put back, in place of
    the empty object, when the line comes out: so the file is as it was even
    when Claude Code rewrote it in its own format meanwhile. A byte order
    mark is not part of the text (the file keeps its own). Never raises."""
    path = settings_path() if path is None else path
    try:
        _bom, text = _split_bom(_read(path))
    except (OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    if not text.strip():
        return text
    obj, _error = _parse(text)
    return text if obj == {} else None


def brace_lead(path: Path | None = None) -> str | None:
    """What follows the brace before the first member, when the file has
    several lines inside its braces while that member shares the brace's
    line (``{"model": "opus",`` then more lines): the line then splits the
    brace's line, and this is what joins it again. None otherwise, or when
    the file already holds an ``apiKeyHelper``. Recorded when a key is
    entered, like :func:`blank_text`. Never raises."""
    path = settings_path() if path is None else path
    try:
        _bom, text = _split_bom(_read(path))
        if text is None or not text.strip():
            return None
        open_at, close_at, members = _members(text)
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return None
    if any(m.key == HELPER_KEY for m in members):
        return None
    if not _split_lead(text, open_at, close_at, members):
        return None
    return text[open_at + 1 : members[0].start]


def remove(*, path: Path | None = None, remove_file_if_empty: bool = False,
           number: str | None = None, restore_blank: str | None = None,
           restore_lead: str | None = None) -> bool:
    """Take qswap's line out of the settings (only qswap's; ``number`` limits
    it to the line for that account). With ``remove_file_if_empty`` a file
    left holding nothing is removed (qswap created it); ``restore_blank`` and
    ``restore_lead`` put back what :func:`blank_text` and :func:`brace_lead`
    read before the line went in. Returns whether
    anything changed. Raises :class:`SettingsError` when the file cannot be
    parsed; another helper is left alone (and is not an error)."""
    path = settings_path() if path is None else path
    try:
        before, after = _change(
            path, None, remove_if_empty=remove_file_if_empty, want=number,
            restore_blank=restore_blank, restore_lead=restore_lead,
        )
    except SettingsError as e:
        if e.kind == FOREIGN:
            return False
        raise
    return before != after
