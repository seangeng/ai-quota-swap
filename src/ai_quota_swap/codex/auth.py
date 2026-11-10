"""Codex CLI's login file: locate it, read it, decode it, lock it, replace it.

What this rests on (aqs-notes ``codex-protocol.md`` §1, §3, §4, checked against
the open-source CLI at ``rust-v0.164.0`` and ``rust-v0.167.0``):

- A ChatGPT login is one JSON record, ``$CODEX_HOME/auth.json``:
  ``tokens.{id_token, access_token, refresh_token, account_id}`` plus
  ``last_refresh``. Older records carry no ``auth_mode``; Codex then treats a
  record with a non-null ``OPENAI_API_KEY`` as API-key auth.
- The tokens are JWTs whose claims name the account. They are read here
  without verifying signatures: they are the user's own tokens, read locally.
- Codex 0.167 serializes every credential write with a blocking ``flock`` on
  ``$CODEX_HOME/auth.lock`` and then a 5 s try-lock on ``auth.json.lock``
  beside the resolved ``auth.json``. Neither file is ever deleted.
- ``cli_auth_credentials_store`` in ``config.toml`` can move the login into the
  OS keyring; then there is no ``auth.json`` to swap and qswap refuses.

Records are copied byte for byte: they are parsed to read claims and never
re-serialized.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import stat
import sys
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap import macos_keychain
from ai_quota_swap.exceptions import CodexLoginError, LockError
from ai_quota_swap.fsutil import atomic_write_bytes, readlink
from ai_quota_swap.locking import FileLock
from ai_quota_swap.paths import get_codex_home

_logger = logging.getLogger("ai-quota-swap")

AUTH_FILENAME = "auth.json"
AUTH_LOCK_FILENAME = "auth.lock"
CONFIG_FILENAME = "config.toml"

#: Claim namespaces in Codex's tokens (``login/src/token_data.rs``).
AUTH_CLAIMS = "https://api.openai.com/auth"
PROFILE_CLAIMS = "https://api.openai.com/profile"

#: Where Codex keeps a keyring-mode login on macOS and Linux (``storage.rs``).
KEYRING_SERVICE = "Codex Auth"
#: The encrypted-file keyring backend (default on Windows when keyring mode is on).
SECRETS_FILE = Path("secrets") / "codex_auth.age"

#: Codex blocks on ``auth.lock``; we wait this long before giving up.
AUTH_LOCK_TIMEOUT_S = 10.0
#: Codex's own budget for the commit lock (``credential commit lock timed out``).
COMMIT_LOCK_TIMEOUT_S = 5.0

#: How often a read that finds half a file is retried. Codex 0.164 writes
#: ``auth.json`` in place (truncate, then write), so a reader can catch it empty.
_READ_ATTEMPTS = 3
_READ_RETRY_DELAY_S = 0.05

#: Storage modes, as ``cli_auth_credentials_store`` spells them.
FILE_MODE = "file"
_KEYRING_MODES = ("keyring", "auto")
_KNOWN_MODES = ("file", "keyring", "auto", "ephemeral")

#: Record fields that make Codex pick a non-ChatGPT mode when ``auth_mode`` is
#: absent (``manager.rs:1835-1852``), checked before ``OPENAI_API_KEY``.
_OTHER_MODE_FIELDS = (
    ("personal_access_token", "personal access token"),
    ("bedrock_api_key", "Amazon Bedrock API key"),
    ("bedrock_access_keys", "Amazon Bedrock access keys"),
)


# -- the home ---------------------------------------------------------------------


def codex_home() -> Path:
    """Codex's home, refusing a ``CODEX_HOME`` that Codex itself would refuse.

    Codex errors out when ``CODEX_HOME`` is set to a path that does not exist
    or is not a directory; qswap does the same rather than invent a login file
    somewhere Codex will never look.
    """
    env = os.environ.get("CODEX_HOME")
    home = get_codex_home()
    if not env:
        return home
    if not home.is_dir():
        raise CodexLoginError(
            f"CODEX_HOME points to {env!r}, which is not an existing directory"
        )
    # Codex canonicalizes a set CODEX_HOME, symlinks resolved.
    return home.resolve()


def auth_path(home: Path) -> Path:
    return home / AUTH_FILENAME


# -- storage mode -----------------------------------------------------------------


def configured_store_mode(home: Path) -> str:
    """``cli_auth_credentials_store`` from ``<home>/config.toml``; ``file`` when unset.

    An unreadable or malformed config reads as unset, with a log line: Codex
    itself refuses to start on a malformed config, so there is no login in
    another store to protect, and the file checks still apply.
    """
    path = home / CONFIG_FILENAME
    try:
        with open(path, "rb") as fh:
            config = tomllib.load(fh)
    except FileNotFoundError:
        return FILE_MODE
    except (OSError, tomllib.TOMLDecodeError) as e:
        _logger.warning("Could not read %s (%s); assuming file storage", path, e)
        return FILE_MODE
    value = config.get("cli_auth_credentials_store", FILE_MODE)
    return value if isinstance(value, str) else repr(value)


def keyring_account(home: Path) -> str:
    """The keyring account Codex files a keyring-mode login under, for ``home``.

    ``cli|`` plus the first 16 hex digits of the SHA-256 of the canonical home
    path (``compute_store_key`` in ``login/src/auth/storage.rs``).
    """
    try:
        canonical = home.resolve(strict=True)
    except OSError:
        canonical = home
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return f"cli|{digest[:16]}"


def _keyring_login_present(home: Path) -> bool:
    """Whether a keyring-mode login for ``home`` exists, without reading it."""
    if (home / SECRETS_FILE).exists():
        return True
    if sys.platform == "darwin":
        # Attribute-only lookup: never decrypts, never prompts.
        return macos_keychain.item_exists(KEYRING_SERVICE, keyring_account(home))
    return False


def ensure_file_storage(home: Path) -> None:
    """Refuse a Codex home whose login is not kept in ``auth.json``.

    qswap swaps logins by replacing ``auth.json``; a login in the OS keyring is
    out of its reach (design: "file storage only").
    """
    mode = configured_store_mode(home)
    config = home / CONFIG_FILENAME
    if mode in _KEYRING_MODES:
        raise CodexLoginError(
            f"Codex keeps its login in the OS keyring (cli_auth_credentials_store = "
            f'"{mode}" in {config}); qswap supports file storage only'
        )
    if mode == "ephemeral":
        raise CodexLoginError(
            f"Codex keeps its login in memory only (cli_auth_credentials_store = "
            f'"ephemeral" in {config}); there is no login file for qswap to manage'
        )
    if mode not in _KNOWN_MODES:
        raise CodexLoginError(
            f"Unrecognized cli_auth_credentials_store = {mode} in {config}; "
            "qswap supports file storage only"
        )
    if not auth_path(home).exists() and _keyring_login_present(home):
        raise CodexLoginError(
            f"Codex's login is in the OS keyring (no {AUTH_FILENAME} in {home}, "
            "but a keyring entry exists); qswap supports file storage only"
        )


# -- reading and decoding ---------------------------------------------------------


def read_auth_bytes(path: Path) -> bytes | None:
    """The bytes of a login file, or None when it does not exist.

    A file that is empty or not yet JSON is re-read a few times before being
    returned as it is: Codex 0.164 rewrites it in place.
    """
    data = b""
    for attempt in range(_READ_ATTEMPTS):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise CodexLoginError(f"Could not read {path}: {e}") from e
        try:
            json.loads(data)
            return data
        except (ValueError, UnicodeDecodeError):
            if attempt < _READ_ATTEMPTS - 1:
                time.sleep(_READ_RETRY_DELAY_S)
    return data


def decode_jwt_claims(token: object) -> dict | None:
    """The payload of a JWT as a dict, or None when ``token`` is not one.

    No signature check: these are the user's own tokens, read locally, and
    only their identity claims are used.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _nested(claims: dict | None, namespace: str) -> dict:
    inner = claims.get(namespace) if isinstance(claims, dict) else None
    return inner if isinstance(inner, dict) else {}


@dataclass(frozen=True)
class CodexLogin:
    """One ChatGPT login record, decoded. ``raw`` is the record byte for byte."""

    raw: bytes
    email: str
    user_id: str  # chatgpt_user_id: the person
    account_id: str  # tokens.account_id: the ChatGPT account or workspace
    plan_type: str  # id_token chatgpt_plan_type at the last refresh; "" if absent
    access_token: str
    refresh_token: str
    access_expires_at: float | None  # access-token ``exp``, epoch seconds
    last_refresh: str
    auth_mode: str | None

    @property
    def identity(self) -> tuple[str, str]:
        """``(chatgpt_user_id, account_id)``: what makes two logins one account."""
        return (self.user_id, self.account_id)


def _resolved_mode(record: dict) -> tuple[str, str]:
    """``(mode, description)`` the way Codex resolves a stored record's mode."""
    mode = record.get("auth_mode")
    if isinstance(mode, str) and mode:
        return mode, mode
    for field, description in _OTHER_MODE_FIELDS:
        if record.get(field) is not None:
            return field, description
    if record.get("OPENAI_API_KEY") is not None:
        return "apikey", "an API key (OPENAI_API_KEY set and no auth_mode)"
    return "chatgpt", "ChatGPT"


def parse_login(raw: bytes, *, source: str = AUTH_FILENAME) -> CodexLogin:
    """Decode a login record; raise :class:`CodexLoginError` for anything qswap
    cannot manage (not JSON, not a ChatGPT login, incomplete)."""
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise CodexLoginError(f"{source} is not valid JSON ({e})") from e
    if not isinstance(record, dict):
        raise CodexLoginError(f"{source} is not a JSON object")

    mode, description = _resolved_mode(record)
    if mode == "apikey":
        raise CodexLoginError(
            f"{source} is an API-key login; qswap manages ChatGPT logins only"
            if record.get("auth_mode") else
            f"{source} is used by Codex as {description}; qswap manages ChatGPT "
            "logins only"
        )
    if mode != "chatgpt":
        raise CodexLoginError(
            f"{source} holds a {description} login, not a ChatGPT one; qswap "
            "manages ChatGPT logins only"
        )

    tokens = record.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexLoginError(f"{source} has no ChatGPT tokens")
    id_token = tokens.get("id_token")
    access_token = _str(tokens.get("access_token"))
    if not access_token:
        raise CodexLoginError(f"{source} has no access token")
    last_refresh = _str(record.get("last_refresh"))
    if not last_refresh:
        # Codex reads such a record as "Token data is not available".
        raise CodexLoginError(
            f"{source} has no last_refresh, so Codex cannot use it either"
        )

    id_claims = decode_jwt_claims(id_token)
    access_claims = decode_jwt_claims(access_token)
    id_auth = _nested(id_claims, AUTH_CLAIMS)
    access_auth = _nested(access_claims, AUTH_CLAIMS)

    email = _str((id_claims or {}).get("email")) or _str(
        _nested(access_claims, PROFILE_CLAIMS).get("email")
    )
    user_id = (
        _str(id_auth.get("chatgpt_user_id"))
        or _str(id_auth.get("user_id"))
        or _str(access_auth.get("chatgpt_user_id"))
        or _str(access_auth.get("user_id"))
    )
    account_id = (
        _str(tokens.get("account_id"))
        or _str(id_auth.get("chatgpt_account_id"))
        or _str(access_auth.get("chatgpt_account_id"))
    )
    if not email:
        raise CodexLoginError(f"{source}: the tokens carry no email address")
    if not user_id:
        raise CodexLoginError(f"{source}: the tokens carry no ChatGPT user id")
    if not account_id:
        raise CodexLoginError(f"{source} has no ChatGPT account id")

    exp = (access_claims or {}).get("exp")
    access_expires_at = (
        float(exp)
        if isinstance(exp, (int, float)) and not isinstance(exp, bool)
        else None
    )
    plan = _str(id_auth.get("chatgpt_plan_type")) or _str(
        access_auth.get("chatgpt_plan_type")
    )
    return CodexLogin(
        raw=raw,
        email=email,
        user_id=user_id,
        account_id=account_id,
        plan_type=plan,
        access_token=access_token,
        refresh_token=_str(tokens.get("refresh_token")),
        access_expires_at=access_expires_at,
        last_refresh=last_refresh,
        auth_mode=record.get("auth_mode") if isinstance(record.get("auth_mode"), str) else None,
    )


def try_parse_login(raw: bytes | str | None) -> CodexLogin | None:
    """:func:`parse_login`, or None for anything that is not a ChatGPT login."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw:
        return None
    try:
        return parse_login(raw)
    except CodexLoginError:
        return None


def token_fingerprint(token: str) -> str:
    """A stable, non-reversible tag for a token, safe to store and compare."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


# -- locks and the write ----------------------------------------------------------


def resolve_auth_file(path: Path) -> Path:
    """Follow a symlink chain the way Codex 0.167 does (``resolve_auth_file``).

    Each hop is read with ``readlink`` and joined to the link's directory when
    relative; a missing end is allowed; more than 40 hops is an error. On
    Windows an absolute hop comes back as ``\\\\?\\C:\\...``; the prefix is
    dropped (see :func:`fsutil.readlink`) so the path, the commit lock beside
    it and every message spell the file the ordinary way, and a later relative
    hop's ``..`` is normalized. The lock file is the same file either way.
    """
    current = path
    for _ in range(40):
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return current
        if not stat.S_ISLNK(st.st_mode):
            return current
        target = Path(readlink(current))
        current = target if target.is_absolute() else current.parent / target
    raise CodexLoginError(f"{path}: too many symlinks")


def commit_lock_path(path: Path) -> Path:
    """``auth.json.lock`` beside the resolved login file.

    Codex 0.167 builds it with Rust's ``with_extension("json.lock")`` on the
    resolved path, so ``auth.json`` gives ``auth.json.lock`` and a link to
    ``creds`` gives ``creds.json.lock``. Spelled the same way so both programs
    lock one file.
    """
    resolved = resolve_auth_file(path)
    name = resolved.name
    dot = name.rfind(".")
    stem = name[:dot] if dot > 0 else name
    return resolved.with_name(f"{stem}.json.lock")


def _ensure_lock_file(path: Path) -> None:
    """Create a lock file (0600) if missing; never truncate or delete one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)


@contextlib.contextmanager
def codex_auth_locks(home: Path) -> Iterator[None]:
    """Hold Codex's two credential locks, in Codex 0.167's order.

    First ``<home>/auth.lock`` (Codex blocks on it), then the commit lock beside
    the resolved ``auth.json`` (Codex gives up after 5 s). Both files are created
    if missing and never deleted. Both are ``flock`` locks on macOS and Linux
    (Rust's ``File::lock``/``try_lock``, Python's ``fcntl.flock``), so the two
    programs exclude each other; Codex 0.164 takes neither.

    Raises :class:`LockError` when either cannot be had in time; nothing has
    changed then and the operation can be retried.
    """
    auth_lock = home / AUTH_LOCK_FILENAME
    commit_lock = commit_lock_path(auth_path(home))
    _ensure_lock_file(auth_lock)
    _ensure_lock_file(commit_lock)
    first = FileLock(auth_lock, timeout=AUTH_LOCK_TIMEOUT_S)
    if not first.acquire():
        raise LockError(
            f"Codex is holding {auth_lock} (a token refresh or login in "
            "progress); nothing was changed, try again"
        )
    try:
        second = FileLock(commit_lock, timeout=COMMIT_LOCK_TIMEOUT_S)
        if not second.acquire():
            raise LockError(
                f"Codex is holding {commit_lock}; nothing was changed, try again"
            )
        try:
            yield
        finally:
            second.release()
    finally:
        first.release()


def check_replaceable(path: Path) -> None:
    """Refuse a login file Codex 0.167 would refuse to replace atomically:
    read-only, or hard-linked (a rename would leave the other name behind)."""
    target = resolve_auth_file(path)
    try:
        st = os.stat(target)
    except FileNotFoundError:
        return
    if not os.access(target, os.W_OK):
        raise CodexLoginError(f"{target} is read-only; refusing to replace it")
    if st.st_nlink > 1 and stat.S_ISREG(st.st_mode):
        raise CodexLoginError(
            f"{target} is hard-linked; replacing it would leave the other link "
            "on the old login"
        )


def write_auth_bytes(path: Path, data: bytes) -> None:
    """Publish ``data`` as the login file: 0600 temp beside the resolved target,
    fsync, rename. A symlinked ``auth.json`` stays a symlink."""
    atomic_write_bytes(path, data, mode=0o600, follow_symlinks=True)
