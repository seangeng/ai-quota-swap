"""Registered organizations and their Admin keys (``qswap api org``).

An organization is a name, Anthropic's organization id and name, and an Admin
key. The registry (``<data root>/api_orgs.json``) holds everything but the
key. The key is stored the way saved logins are, with the same primitives
(``credentials.CredentialStore`` keeps per-account backups this way):

- macOS: the Keychain item ``ai-quota-swap`` / ``admin-org-<name>`` through
  :mod:`ai_quota_swap.macos_keychain` (the ``security`` CLI, secret on stdin,
  never in argv), falling back to the file below when the Keychain is
  unusable;
- elsewhere: ``<data root>/credentials/.admin-org-<name>.enc``, base64,
  written atomically with mode 0600 by :func:`fsutil.atomic_write_text`.

Reads are file-first, like the backups: a fallback file written while the
Keychain was locked is newer than any Keychain copy.

``ANTHROPIC_ADMIN_KEY`` in the environment stands in for a registered
organization when none is registered. It is read for the command that sees
it, kept in memory, and never written anywhere.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from ai_quota_swap import macos_keychain
from ai_quota_swap.credentials import SECURITY_SERVICE
from ai_quota_swap.fsutil import atomic_write_text
from ai_quota_swap.models import Platform, get_timestamp

_logger = logging.getLogger("ai-quota-swap")

ADMIN_KEY_PREFIX = "sk-ant-admin"
ENV_VAR = "ANTHROPIC_ADMIN_KEY"
ENV_ORG_NAME = "env"
REGISTRY_FILENAME = "api_orgs.json"
REGISTRY_SCHEMA_VERSION = 1
KEYCHAIN_ACCOUNT_PREFIX = "admin-org-"

STORAGE_KEYCHAIN = "keychain"
STORAGE_FILE = "file"
STORAGE_ENV = "environment"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_KEY_CHARS_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
# A loaded key is reused for this long in the process, so a pass over several
# accounts reads the Keychain once.
_KEY_MEMO_S = 60.0


class OrgError(ValueError):
    """A refused ``qswap api org`` operation; the message is for the user."""


def looks_like_admin_key(value: str | None) -> bool:
    """Whether ``value`` has an Admin key's shape: ``sk-ant-admin...`` and
    only the characters keys use. Says nothing about whether it works."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return (
        v.startswith(ADMIN_KEY_PREFIX)
        and len(v) >= len(ADMIN_KEY_PREFIX) + 12
        and bool(_KEY_CHARS_RE.match(v))
    )


def normalize_org_name(name: str) -> str:
    """A registry name: lower case letters, digits, ``.``, ``_``, ``-``; up to 40."""
    value = (name or "").strip().lower()
    if not _NAME_RE.match(value):
        raise OrgError(
            f"{name!r} is not a usable name: use up to 40 letters, digits, '.', '_' "
            "or '-', starting with a letter or digit"
        )
    if value == ENV_ORG_NAME:
        raise OrgError(f"'{ENV_ORG_NAME}' is reserved for {ENV_VAR}")
    return value


def default_org_name(organization_name: str) -> str:
    """A registry name derived from Anthropic's organization name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (organization_name or "").lower()).strip("-")
    slug = slug[:40].strip("-")
    if not slug or slug == ENV_ORG_NAME or not _NAME_RE.match(slug):
        return "default"
    return slug


@dataclass(frozen=True)
class Org:
    """One organization qswap may read reports for. The key is loaded on
    demand (``admin_key()``) and is not part of the object's repr."""

    name: str
    organization_id: str
    organization_name: str
    storage: str
    added: str = ""
    _loader: Callable[[], str] = field(repr=False, compare=False, default=lambda: "")
    _cache_id: str = field(repr=False, compare=False, default="")

    @property
    def persistent(self) -> bool:
        return self.storage != STORAGE_ENV

    def admin_key(self) -> str:
        return self._loader()

    def cache_id(self) -> str:
        return self._cache_id or f"org:{self.name}"


class OrgRegistry:
    """``api_orgs.json`` plus the secret store, rooted at the data root."""

    def __init__(self, root: Path, platform: Platform | None = None):
        self.root = Path(root)
        self.path = self.root / REGISTRY_FILENAME
        self.credentials_dir = self.root / "credentials"
        self.platform = platform if platform is not None else Platform.detect()
        self._memo: dict[str, tuple[float, str]] = {}

    # -- registry file -------------------------------------------------------

    def _load(self) -> dict:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeDecodeError) as e:
            _logger.warning("Could not read %s: %s", self.path.name, type(e).__name__)
            return {}
        if not isinstance(doc, dict) or not isinstance(doc.get("orgs"), dict):
            return {}
        return {
            name: rec for name, rec in doc["orgs"].items()
            if isinstance(name, str) and _NAME_RE.match(name) and isinstance(rec, dict)
        }

    def _save(self, orgs: dict) -> None:
        doc = {"schemaVersion": REGISTRY_SCHEMA_VERSION, "orgs": orgs}
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(doc, indent=2) + "\n")

    def _org(self, name: str, rec: dict) -> Org:
        return Org(
            name=name,
            organization_id=str(rec.get("organizationId") or ""),
            organization_name=str(rec.get("organizationName") or ""),
            storage=str(rec.get("storage") or ""),
            added=str(rec.get("added") or ""),
            _loader=lambda n=name: self.read_secret(n),
        )

    def orgs(self) -> list[Org]:
        """Registered organizations, by name."""
        return [self._org(n, r) for n, r in sorted(self._load().items())]

    def get(self, name: str) -> Org | None:
        rec = self._load().get(name)
        return self._org(name, rec) if rec is not None else None

    def active_orgs(self, env: Mapping[str, str] | None = None) -> list[Org]:
        """The registered organizations, or, when none is registered, the one
        ``ANTHROPIC_ADMIN_KEY`` names (in memory only)."""
        registered = self.orgs()
        if registered:
            return registered
        env = os.environ if env is None else env
        value = (env.get(ENV_VAR) or "").strip()
        if not looks_like_admin_key(value):
            return []
        import hashlib

        fp = hashlib.sha256(("qswap-env\0" + value).encode("utf-8")).hexdigest()[:16]
        return [
            Org(
                name=ENV_ORG_NAME,
                organization_id="",
                organization_name=f"from {ENV_VAR}",
                storage=STORAGE_ENV,
                _loader=lambda v=value: v,
                _cache_id=f"env:{fp}",
            )
        ]

    # -- secret store --------------------------------------------------------

    def _keychain_account(self, name: str) -> str:
        return f"{KEYCHAIN_ACCOUNT_PREFIX}{name}"

    def secret_path(self, name: str) -> Path:
        return self.credentials_dir / f".{KEYCHAIN_ACCOUNT_PREFIX}{name}.enc"

    def _write_file(self, name: str, key: str) -> None:
        self.credentials_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = base64.b64encode(key.encode("utf-8")).decode("ascii")
        atomic_write_text(self.secret_path(name), encoded, mode=0o600)

    def store_secret(self, name: str, key: str) -> str:
        """Store ``key`` for ``name``; returns where (``keychain`` or ``file``)."""
        self._memo.pop(name, None)
        if self.platform == Platform.MACOS:
            try:
                macos_keychain.set_password(
                    SECURITY_SERVICE, self._keychain_account(name), key
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                _logger.warning(
                    "Keychain write for the Admin key failed (%s); using a 0600 file",
                    type(e).__name__,
                )
            else:
                try:
                    self.secret_path(name).unlink()
                except FileNotFoundError:
                    pass
                return STORAGE_KEYCHAIN
        self._write_file(name, key)
        return STORAGE_FILE

    def read_secret(self, name: str) -> str:
        """The stored key for ``name``; ``""`` when there is none. Raises the
        Keychain's error when the Keychain cannot be read."""
        hit = self._memo.get(name)
        now = time.monotonic()
        if hit is not None and now - hit[0] < _KEY_MEMO_S:
            return hit[1]
        value = ""
        path = self.secret_path(name)
        try:
            raw = path.read_bytes().strip()
        except FileNotFoundError:
            raw = b""
        if raw:
            try:
                value = base64.b64decode(raw, validate=True).decode("utf-8").strip()
            except (ValueError, UnicodeDecodeError):
                _logger.warning("The stored Admin key file for %s is corrupt", name)
                value = ""
        if not value and self.platform == Platform.MACOS:
            value = (
                macos_keychain.get_password(SECURITY_SERVICE, self._keychain_account(name))
                or ""
            ).strip()
        if value:
            self._memo[name] = (now, value)
        return value

    def delete_secret(self, name: str) -> bool:
        """Delete every stored copy of ``name``'s key. True when none is left."""
        self._memo.pop(name, None)
        ok = True
        try:
            self.secret_path(name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            _logger.warning("Could not delete the Admin key file for %s: %s", name, e)
            ok = False
        if self.platform == Platform.MACOS:
            try:
                macos_keychain.delete_password(SECURITY_SERVICE, self._keychain_account(name))
            except macos_keychain.KEYCHAIN_ERRORS as e:
                _logger.warning(
                    "Could not delete the Admin key from the Keychain for %s: %s",
                    name, type(e).__name__,
                )
                ok = False
        return ok

    # -- operations ----------------------------------------------------------

    def add(
        self, name: str, key: str, organization_id: str, organization_name: str
    ) -> Org:
        """Register ``name``. Refuses a taken name or an organization already
        registered under another name. Stores the key first, then the entry."""
        orgs = self._load()
        if name in orgs:
            raise OrgError(
                f"'{name}' is already registered; remove it first "
                f"(qswap api org remove {name})"
            )
        for other, rec in orgs.items():
            if organization_id and rec.get("organizationId") == organization_id:
                raise OrgError(
                    f"this organization is already registered as '{other}'"
                )
        storage = self.store_secret(name, key)
        orgs[name] = {
            "organizationId": organization_id,
            "organizationName": organization_name,
            "storage": storage,
            "added": get_timestamp(),
        }
        try:
            self._save(orgs)
        except BaseException:
            self.delete_secret(name)
            raise
        return self._org(name, orgs[name])

    def remove(self, name: str) -> tuple[bool, bool]:
        """Unregister ``name`` and delete its key. ``(was registered, key gone)``."""
        orgs = self._load()
        known = name in orgs
        gone = self.delete_secret(name)
        if known:
            del orgs[name]
            self._save(orgs)
        return known, gone


def purge_secrets(root: Path, platform: Platform) -> list[str]:
    """Delete every registered organization's stored key (``qswap purge``,
    before the data root goes). Never raises; returns what was removed."""
    removed: list[str] = []
    try:
        registry = OrgRegistry(root, platform)
        for org in registry.orgs():
            if registry.delete_secret(org.name):
                removed.append(f"Admin key: {org.name}")
    except Exception as e:
        _logger.warning("Could not remove stored Admin keys: %s", type(e).__name__)
    return removed
