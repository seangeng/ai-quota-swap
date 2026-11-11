"""``qswap key-helper N``: print API-key account N's saved key for Claude Code.

This is the command every ``apiKeyHelper`` line qswap writes runs
(docs/design/api-key-sessions.md, part 2). Claude Code runs it from any
working directory, with whatever environment the session has, when a session
moves to the key and again when its cached value expires. So it:

- prints the key and one newline on standard output, and nothing else ever:
  no log line, no update notice, no warning;
- on any failure prints one line on standard error and nothing on standard
  output, and exits 1 (64 for a usage error: a missing or extra argument);
- with ``--check`` prints nothing at all and exits 0 when the key can be
  read (``qswap doctor`` and a switch to the active key run the line that
  way to learn whether it answers), and fails as above when it cannot;
- imports only the standard library and two leaf modules (``fsutil`` is not
  even needed; ``macos_keychain`` for the Keychain read), never the switcher,
  the dashboard, the menu bar or anything that opens a connection;
- takes no lock and writes no file: it reads ``sequence.json`` and the saved
  key, which qswap replaces atomically, so a read never sees half a write;
- finds the data directory the way ``paths.get_backup_root`` does (the rule
  is repeated here so the import stays this small; a test pins the two
  together).

The saved key is read the way the switcher reads a backup: the ``.enc`` file
first (base64), then on macOS the Keychain item ``ai-quota-swap`` /
``account-N-<email>``. When the saved credential is not a key but the previous
copy is (the state an older qswap left behind), the previous copy is printed;
nothing is repaired here, since this command never writes.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from pathlib import Path

#: Keychain service holding qswap's saved logins and keys (credentials.SECURITY_SERVICE).
SECURITY_SERVICE = "ai-quota-swap"
_OAUTH_TOKEN_PREFIXES = ("sk-ant-oat", "sk-ant-ort")

USAGE = "usage: qswap key-helper N [--data-dir DIR] [--check]"
HELP = f"""{USAGE}

Print the saved key of API-key account N, for Claude Code.

qswap writes this command into Claude Code's settings (apiKeyHelper) while an
API-key account is active, and into the --settings of a qswap run session on a
key. It prints the key and nothing else, and exits 0. When the key cannot be
read it prints one line on standard error and exits 1; a usage error exits 64.
You do not need to run it yourself.

options:
  --data-dir DIR  qswap's data directory. On Linux the line always names it,
                  since Claude Code may run the command with another
                  XDG_DATA_HOME.
  --check         print nothing, and exit 0 only when the key can be read

examples:
  qswap key-helper 5
  qswap key-helper 5 --check
"""


def _parse_args(argv: list[str]) -> tuple[str, Path | None, bool] | None:
    """``(number, data directory, check)`` from ``N [--data-dir DIR]
    [--check]`` (the options in any order after N), or None for a usage
    error."""
    if not argv or not argv[0].isdigit():
        return None
    root: Path | None = None
    check = False
    rest = argv[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--check" and not check:
            check = True
        elif flag == "--data-dir" and root is None and rest and rest[0]:
            root = Path(rest.pop(0))
        else:
            return None
    return str(int(argv[0])), root, check


class HelperFailure(Exception):
    """One line for standard error."""


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def data_dir() -> Path:
    """qswap's data directory (``paths.get_backup_root``'s rule)."""
    if _platform() == "linux":
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            xdg_path = Path(os.path.expanduser(xdg))
            if xdg_path.is_absolute():
                return xdg_path / "ai-quota-swap"
        return Path.home() / ".local" / "share" / "ai-quota-swap"
    return Path.home() / ".ai-quota-swap"


def pinned_data_dir() -> Path | None:
    """The data directory to name in the helper line: the one in use when it
    is not the one the rule gives without ``XDG_DATA_HOME`` (Linux with the
    variable set), else None."""
    current = data_dir()
    if _platform() != "linux":
        return None
    default = Path.home() / ".local" / "share" / "ai-quota-swap"
    return None if current == default else current


def is_key(value: str | None) -> bool:
    """``credentials.looks_like_api_key``: a bare ``sk-ant-`` value that is
    not an OAuth token."""
    if not value:
        return False
    text = value.strip()
    return (
        text.startswith("sk-ant-")
        and not text.startswith(_OAUTH_TOKEN_PREFIXES)
        and not any(c.isspace() for c in text)
    )


def _account(root: Path, number: str) -> tuple[str, str]:
    """``(email, kind)`` of account ``number`` from ``sequence.json``."""
    try:
        data = json.loads((root / "sequence.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HelperFailure("qswap manages no accounts on this machine") from None
    except (OSError, ValueError) as e:
        raise HelperFailure(f"qswap's account list could not be read ({type(e).__name__})") from None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    record = accounts.get(number) if isinstance(accounts, dict) else None
    if not isinstance(record, dict):
        raise HelperFailure(f"#{number} does not exist; qswap list shows the accounts")
    email = record.get("email")
    if not isinstance(email, str) or not email:
        raise HelperFailure(f"#{number} has no saved identity; re-add it with qswap add-token")
    return email, str(record.get("kind") or "oauth")


def _read_enc(path: Path) -> str | None:
    try:
        raw = path.read_bytes().strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise HelperFailure(f"the saved key could not be read ({type(e).__name__})") from None
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8") or None
    except (binascii.Error, ValueError):
        return None


def _read_keychain(username: str) -> str | None:
    from ai_quota_swap import macos_keychain

    try:
        return macos_keychain.get_password(SECURITY_SERVICE, username)
    except macos_keychain.KEYCHAIN_ERRORS as e:
        raise HelperFailure(
            f"the Keychain could not be read ({type(e).__name__}); unlock it and retry"
        ) from None


def _saved(root: Path, number: str, email: str, *, previous: bool) -> str | None:
    suffix = ".prev" if previous else ""
    value = _read_enc(root / "credentials" / f".creds-{number}-{email}.enc{suffix}")
    if value:
        return value
    if _platform() == "macos":
        return _read_keychain(f"account-{number}-{email}{suffix}")
    return None


def read_key(number: str, root: Path | None = None) -> str:
    """Account ``number``'s saved key, or :class:`HelperFailure`."""
    root = data_dir() if root is None else root
    email, kind = _account(root, number)
    if kind != "api_key":
        raise HelperFailure(f"#{number} is a subscription account, not an API-key account")
    value = _saved(root, number, email, previous=False)
    if is_key(value):
        return value.strip()  # type: ignore[union-attr]
    older = _saved(root, number, email, previous=True)
    if is_key(older):
        return older.strip()  # type: ignore[union-attr]
    if value:
        raise HelperFailure(
            f"#{number}'s saved credential is not an API key; save it again: "
            f"qswap add-token KEY --slot {number}"
        )
    raise HelperFailure(
        f"#{number} has no saved key; save one: qswap add-token KEY --slot {number}"
    )


def main(argv: list[str]) -> int:
    """Run ``qswap key-helper ARGV``; the exit status: 0 when the key was
    printed (with ``--check``: could be read), 1 when it cannot be read, 64
    for a usage error. Every failure is one line on standard error."""
    if argv in (["-h"], ["--help"]):
        sys.stdout.write(HELP)
        return 0
    parsed = _parse_args(argv)
    if parsed is None:
        sys.stderr.write(f"qswap key-helper: {USAGE}\n")
        return 64
    number, root, check = parsed
    try:
        key = read_key(number, root)
    except HelperFailure as e:
        sys.stderr.write(f"qswap key-helper: {e}\n")
        return 1
    except Exception as e:  # never a traceback into Claude Code's output
        sys.stderr.write(f"qswap key-helper: failed ({type(e).__name__})\n")
        return 1
    if check:
        return 0
    try:
        sys.stdout.write(key + "\n")
        sys.stdout.flush()
    except OSError:
        return 1
    return 0
