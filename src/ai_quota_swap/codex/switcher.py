"""The Codex provider's switcher (docs/design/codex.md, docs/design/providers.md).

``CodexSwitcher`` exposes the members of both provider contracts
(``providers.EngineHost``, ``providers.SurfaceHost``) plus what ``qswap codex``
needs. It is its own class, not a ``ClaudeAccountSwitcher`` subclass; it reuses
the generic modules (file lock, atomic writes, usage store, poll policy,
account policy model, JSON row builders) and a few pure display helpers of the
Claude switcher.

Data root ``<qswap data root>/codex/``:

- ``sequence.json``: the registry, in the Claude registry's shape. An account
  is ``(uuid, organizationUuid)`` = ``(chatgpt_user_id, account_id)``.
- ``credentials/account-<n>.json``: the saved login, byte for byte, 0600.
- ``cache/usage.json``, ``.lock``: usage store and registry lock.
- ``activations.jsonl``: which account was live when (``ledger.activations``),
  for ``qswap usage``.

Rules (design): qswap never refreshes a Codex login, never runs ``codex
logout`` or calls the revoke endpoint, and copies records byte for byte.
Registry mutations take ``.lock``; no network call is made while it is held.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ai_quota_swap import oauth, poll_policy, resets
from ai_quota_swap import switcher as claude_switcher
from ai_quota_swap.codex import auth as codex_auth
from ai_quota_swap.codex import processes as codex_processes
from ai_quota_swap.codex import usage as codex_usage
from ai_quota_swap.codex.auth import CodexLogin
from ai_quota_swap.exceptions import (
    AccountNotFoundError,
    CodexLoginError,
    ConfigError,
    CredentialFormatError,
    FeatureUnavailableError,
    RetryableSwitchError,
    SwitchError,
    ValidationError,
)
from ai_quota_swap.fsutil import atomic_write_bytes, atomic_write_text, read_text_with_retry
from ai_quota_swap.json_output import (
    SCHEMA_VERSION,
    USAGE_NO_CREDENTIALS,
    USAGE_TOKEN_EXPIRED,
    USAGE_UNKNOWN_UNTIL_USED,
    account_ref,
    account_row,
    last_good_usage_fields,
    usage_failure_fields,
    usage_fields,
    usage_freshness_fields,
)
from ai_quota_swap.locking import FileLock
from ai_quota_swap.logging_config import setup_logging
from ai_quota_swap.models import (
    AccountPolicy,
    AccountSnapshot,
    AccountsSnapshot,
    Platform,
    get_timestamp,
    normalize_account_priority,
    normalize_account_threshold,
    normalize_alias,
)
from ai_quota_swap.paths import (
    get_backup_root,
    get_default_codex_home,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)
from ai_quota_swap.printer import (
    say,
    accent,
    bolded,
    dimmed,
    warning,
)
from ai_quota_swap.settings import (
    ResetSettings,
    load_codex_settings,
    load_engine_settings,
    load_reset_settings,
)
from ai_quota_swap.usage_store import FetchRecord, UsageEntry, UsageStore, with_sentinel

#: The access token must have at least this long left, while any Codex process
#: runs on the home, for a switch to go ahead: Codex renews a token about five
#: minutes before it expires, and a swap during that renewal can strand the
#: outgoing login's new refresh token (design, switch step 1).
NEAR_EXPIRY_S = 600.0

#: ``qswap codex login``'s temporary Codex homes, in the data root: their name
#: prefix, and the age past which a leftover one is swept.
LOGIN_HOME_PREFIX = "login-home-"
LOGIN_HOME_MAX_AGE_S = 86_400.0

#: What a Codex verb says when nothing is managed: the state and the next step.
NO_CODEX_ACCOUNTS = "No Codex accounts are managed yet. Run codex login, then: qswap codex add"

#: Human notes for the sentinel states a Codex row can be in. The state comes
#: first and a remedy follows " — ", as in Claude Code's notes.
SENTINEL_NOTES = {
    USAGE_TOKEN_EXPIRED: "token expired — Codex renews it the next time it runs",
    USAGE_UNKNOWN_UNTIL_USED: (
        "usage unknown until used — Codex renews the expired token on first use"
    ),
    USAGE_NO_CREDENTIALS: "no saved login — log in to it again: qswap codex login",
}

_PLAN_LABELS = {
    "guest": "Guest",
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "promax": "Pro Max",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
    "education": "Education",
    "free_workspace": "Free workspace",
    "k12": "K-12",
}
#: Plans on a personal ChatGPT account; everything else is a workspace seat.
_PERSONAL_PLANS = {"guest", "free", "go", "plus", "pro", "prolite", "promax"}

_logger_name = "ai-quota-swap"


def plan_label(plan_type: str | None) -> str | None:
    """``Pro``, ``Plus``, ``Team``… for a ``plan_type``; None when unknown."""
    if not plan_type or plan_type == "unknown":
        return None
    return _PLAN_LABELS.get(plan_type, plan_type.replace("_", " ").capitalize())


def is_workspace_plan(plan_type: str | None) -> bool:
    return bool(plan_type) and plan_type != "unknown" and plan_type not in _PERSONAL_PLANS


def _same_dir(left: Path | None, right: Path) -> bool:
    if left is None:
        return False
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


@dataclass(frozen=True)
class _Info:
    """One managed account in one collect pass."""

    number: str
    email: str
    account_id: str
    is_active: bool
    login: CodexLogin | None  # the live record for the active account, else the saved one
    alias: str


class CodexSwitcher:
    """Multi-account switcher for OpenAI Codex CLI (ChatGPT logins)."""

    provider_id = "codex"
    display_name = "Codex"
    cli_name = "codex"
    # Optional engine-host members (``providers.py``): the commands the
    # engine's event text quotes. Claude Code's are the engine's defaults.
    add_command = "qswap codex add"
    recover_hint = "Log in to it again: qswap codex login"

    def __init__(
        self,
        debug: bool = False,
        *,
        data_root: Path | None = None,
        configure_logging: bool = True,
    ):
        """``data_root`` is qswap's data root (default: the backup root), whose
        ``codex/`` holds this provider's data and whose ``settings.json`` is the
        shared settings file. ``configure_logging=False`` leaves the process
        logger alone, for a caller that already set it up (the unified list)."""
        self.platform = Platform.detect()
        if data_root is None:
            data_root = get_backup_root()
            # The shared root's one-time legacy move (Linux/WSL) must run
            # before anything is written under it, or the Claude switcher
            # would later find a non-empty target and refuse to migrate.
            if migrate_legacy_backup_dir(data_root):
                print(
                    f"ai-quota-swap: migrated data from {get_legacy_backup_root()} "
                    f"to {data_root}",
                    file=sys.stderr,
                )
        self.data_root = data_root
        self.backup_dir = data_root / "codex"
        self.sequence_file = self.backup_dir / "sequence.json"
        self.credentials_dir = self.backup_dir / "credentials"
        self.lock_file = self.backup_dir / ".lock"
        if configure_logging:
            self._logger = setup_logging(self.backup_dir, debug=debug)
        else:
            self._logger = logging.getLogger(_logger_name)
        self._usage_store = UsageStore(self.backup_dir / "cache")
        self._poll_inputs_override: tuple[float, float | None, tuple[str, ...]] | None = None

    # -- housekeeping ---------------------------------------------------------

    def _is_running_in_container(self) -> bool:
        """Same container check as the Claude switcher (it reads only ``platform``)."""
        return claude_switcher.ClaudeAccountSwitcher._is_running_in_container(self)  # type: ignore[arg-type]

    def _setup_directories(self) -> None:
        for directory in (self.backup_dir, self.credentials_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def check_storage(self) -> Path:
        """Codex's home, after refusing a home whose login is not in ``auth.json``."""
        home = codex_auth.codex_home()
        codex_auth.ensure_file_storage(home)
        return home

    def require_accounts(self) -> None:
        """Refuse, in one line, a keyring-mode home or a registry with no
        account (``qswap codex auto`` has nothing to watch then)."""
        self.check_storage()
        data = self._read_registry()
        if not data or not data.get("accounts"):
            raise ConfigError(NO_CODEX_ACCOUNTS)

    # -- registry -------------------------------------------------------------

    def _read_registry(self) -> dict | None:
        """``sequence.json``, or None when absent. Unreadable raises ConfigError."""
        if not self.sequence_file.exists():
            return None
        try:
            data = json.loads(read_text_with_retry(self.sequence_file))
        except (OSError, ValueError) as e:
            raise ConfigError(
                f"{self.sequence_file} could not be read ({e}). Repair or move "
                "it, then retry."
            ) from e
        if not isinstance(data, dict):
            raise ConfigError(f"{self.sequence_file} is not a JSON object")
        data.setdefault("sequence", [])
        data.setdefault("accounts", {})
        return data

    def _require_registry(self) -> dict:
        data = self._read_registry()
        if not data or not data.get("accounts"):
            raise ConfigError(NO_CODEX_ACCOUNTS)
        return data

    def _write_registry(self, data: dict) -> None:
        self._setup_directories()
        data["lastUpdated"] = get_timestamp()
        try:
            text = json.dumps(data, indent=2, allow_nan=False)
        except ValueError as e:
            raise ConfigError(f"Generated invalid JSON: {e}") from e
        atomic_write_text(self.sequence_file, text)

    @staticmethod
    def _empty_registry() -> dict:
        return {
            "activeAccountNumber": None,
            "lastUpdated": get_timestamp(),
            "sequence": [],
            "accounts": {},
        }

    def _lock(self) -> FileLock:
        return FileLock(self.lock_file)

    def _credential_path(self, num: str) -> Path:
        return self.credentials_dir / f"account-{num}.json"

    def _read_saved(self, num: str) -> bytes | None:
        try:
            return self._credential_path(str(num)).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            self._logger.warning(f"Could not read Codex account {num}'s saved login: {e}")
            return None

    def _write_saved(self, num: str, raw: bytes) -> None:
        self._setup_directories()
        atomic_write_bytes(self._credential_path(str(num)), raw, mode=0o600)

    def _saved_login(self, num: str) -> CodexLogin | None:
        return codex_auth.try_parse_login(self._read_saved(num))

    @staticmethod
    def _slot_for_identity(data: dict, login: CodexLogin) -> str | None:
        """The managed number for a login: same ``(uuid, organizationUuid)``.

        A record stored without a uuid matches on ``(email, organizationUuid)``.
        """
        for num, record in (data.get("accounts") or {}).items():
            org = record.get("organizationUuid", "") or ""
            uuid = (record.get("uuid") or "").strip()
            if org != login.account_id:
                continue
            if uuid and uuid == login.user_id:
                return num
            if not uuid and record.get("email") == login.email:
                return num
        return None

    def _resolve(self, data: dict, identifier: str) -> str:
        """NUM, alias, or email → account number, or a clear error."""
        accounts = data.get("accounts") or {}
        ident = (identifier or "").strip()
        if ident.isdigit():
            if ident not in accounts:
                raise AccountNotFoundError(f"#{ident} does not exist; qswap codex list shows the accounts")
            return ident
        key = ident.lower()
        for num, record in accounts.items():
            if key and (record.get("alias") or "").lower() == key:
                return num
        matches = [num for num, record in accounts.items() if record.get("email") == ident]
        if not matches:
            raise AccountNotFoundError(f"No account found with identifier: {identifier}")
        if len(matches) > 1:
            details = ", ".join(sorted(matches, key=int))
            raise ConfigError(
                f"Email '{identifier}' is ambiguous — matches accounts: {details}. "
                "Use the account number instead (e.g., qswap codex switch 1)."
            )
        return matches[0]

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        """NUM|EMAIL|ALIAS → ``(number, email, organizationUuid)``."""
        data = self._require_registry()
        num = self._resolve(data, identifier)
        record = data["accounts"][num]
        return num, record.get("email", ""), record.get("organizationUuid", "") or ""

    def _sequence(self, data: dict) -> list[str]:
        accounts = data.get("accounts") or {}
        return [str(n) for n in data.get("sequence", []) if str(n) in accounts]

    # -- the live login -------------------------------------------------------

    def _live(self) -> tuple[Path, bytes | None, CodexLogin | None, str | None]:
        """``(home, raw, login, problem)`` for the live ``auth.json``.

        ``raw`` is None when there is no file; ``login`` None with ``problem``
        set when the file holds something qswap cannot manage (an API key, a
        broken record). A keyring-mode home raises :class:`CodexLoginError`.
        """
        home = self.check_storage()
        raw = codex_auth.read_auth_bytes(codex_auth.auth_path(home))
        if raw is None:
            return home, None, None, None
        try:
            return home, raw, codex_auth.parse_login(raw), None
        except CodexLoginError as e:
            return home, raw, None, str(e)

    def current_account_number(self) -> str | None:
        """Number of the managed account whose login is live; None when there
        is no live login or it is not managed."""
        _home, _raw, login, _problem = self._live()
        if login is None:
            return None
        data = self._read_registry() or {}
        return self._slot_for_identity(data, login)

    def has_live_login(self) -> bool:
        """Whether ``auth.json`` holds some login, managed or not."""
        _home, raw, _login, _problem = self._live()
        return bool(raw and raw.strip())

    # -- engine host: accounts and policy -------------------------------------

    def switchable_account_numbers(self) -> list[str]:
        data = self._read_registry() or {}
        return [
            num for num in self._sequence(data)
            if self._credential_path(num).exists()
            and not self._disabled_from_data(data, num)
        ]

    @staticmethod
    def _disabled_from_data(data: dict, num: str) -> bool:
        return claude_switcher.ClaudeAccountSwitcher._disabled_from_data(data, num)

    @staticmethod
    def _policy_from_data(data: dict, num: str) -> AccountPolicy:
        return claude_switcher.ClaudeAccountSwitcher._policy_from_data(data, num)

    def disabled_account_numbers(self) -> list[str]:
        data = self._read_registry() or {}
        return [n for n in self._sequence(data) if self._disabled_from_data(data, n)]

    def account_policies(self) -> dict[str, AccountPolicy]:
        data = self._read_registry() or {}
        return {n: self._policy_from_data(data, n) for n in self._sequence(data)}

    def account_kind_for(self, account_num: str) -> str:
        """Every managed Codex account is a ChatGPT login (API keys are refused)."""
        return "oauth"

    def account_email(self, account_num: str) -> str:
        data = self._read_registry() or {}
        return (data.get("accounts", {}).get(str(account_num)) or {}).get("email", "")

    def account_identity(self, account_num: str) -> dict:
        """``{email, organizationUuid: account_id, uuid: chatgpt_user_id}``."""
        data = self._read_registry() or {}
        record = data.get("accounts", {}).get(str(account_num)) or {}
        return {
            "email": record.get("email", ""),
            "organizationUuid": record.get("organizationUuid", "") or "",
            "uuid": (record.get("uuid") or "").strip(),
        }

    def backfill_account_uuid(
        self,
        account_num: str,
        uuid: str,
        expected_email: str | None = None,
        expected_org: str | None = None,
    ) -> None:
        """Fill an empty uuid (never rewrite one), under the registry lock."""
        if not uuid:
            return
        with self._lock():
            data = self._read_registry() or {}
            record = data.get("accounts", {}).get(str(account_num))
            if (
                record is not None
                and not (record.get("uuid") or "").strip()
                and (expected_email is None or record.get("email") == expected_email)
                and (
                    expected_org is None
                    or (record.get("organizationUuid", "") or "") == expected_org
                )
            ):
                record["uuid"] = uuid
                self._write_registry(data)

    # -- engine host: stored logins -------------------------------------------

    def read_account_credentials(self, account_num: str, email: str) -> str:
        """The saved record as text (opaque to the engine); "" when missing."""
        raw = self._read_saved(str(account_num))
        if not raw:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    def credential_expires_at_ms(self, credentials: str) -> int | float | None:
        """The access token's ``exp`` in epoch ms; None when it carries none.

        Raises :class:`CredentialFormatError` for anything that is not a
        ChatGPT login record, which the engine reads as a dead lineage.
        """
        login = codex_auth.try_parse_login(credentials)
        if login is None:
            raise CredentialFormatError("the stored credential is not a Codex ChatGPT login")
        if login.access_expires_at is None:
            return None
        return login.access_expires_at * 1000

    def credential_fingerprint(self, credentials: str) -> str | None:
        """Hash of the refresh token (of the whole record when it has none).

        Changes whenever the login is replaced, and whenever Codex renews it
        (refresh tokens rotate), which is the evidence a quarantined account
        needs to be released.
        """
        if not credentials:
            return None
        login = codex_auth.try_parse_login(credentials)
        if login is not None and login.refresh_token:
            return codex_auth.token_fingerprint(login.refresh_token)
        return "sha256:" + hashlib.sha256(credentials.encode("utf-8")).hexdigest()

    def live_session_pids_for(self, account_num: str, email: str) -> list[int]:
        """No ``qswap codex run`` yet (next milestone), so no session holds one."""
        return []

    def consume_backup_grant(
        self, account_num: str, email: str, snapshot: str
    ) -> oauth.RefreshOutcome:
        """Never refreshes: the saved login is handed back as ready, offline.

        qswap never refreshes a Codex login (design rule): Codex renews a
        login itself the first time it runs on it, even when the access token
        expired while saved. So the "gate" answers success with the current
        saved record and no network call, and the engine's ``_freshen_target``
        reads that as ``ok``. A record that is not a login answers
        ``invalid_grant`` (a dead lineage), a missing one ``transient``.
        """
        current = self.read_account_credentials(account_num, email) or snapshot
        if not current:
            return oauth.RefreshOutcome(None, "transient")
        if codex_auth.try_parse_login(current) is None:
            return oauth.RefreshOutcome(None, "invalid_grant")
        return oauth.RefreshOutcome(current, None)

    # -- reset credits ----------------------------------------------------------

    @staticmethod
    def new_reset_request_id() -> str:
        """A consume request id: a UUID v4, which Codex's endpoint expects
        (optional engine-host member, read by ``autoswitch.reset_request_id``)."""
        return codex_usage.new_request_id()

    @staticmethod
    def is_reset_request_id(value: str) -> bool:
        """Whether a stored request id may be reused: a canonical UUID v4."""
        return codex_usage.is_uuid4(value)

    def _claim_login(self, num: str) -> tuple[CodexLogin | None, str]:
        """``(login, "")`` whose access token can spend one of the account's
        credits right now, or ``(None, why)``.

        The live ``auth.json`` for the active account, the saved record for
        any other; both are only read. An expired access token is refused,
        never refreshed: Codex renews a login itself the next time it runs on
        it (design rule).
        """
        data = self._read_registry() or {}
        record = (data.get("accounts") or {}).get(num)
        if record is None:
            return None, "account not found"
        _home, _raw, live, _problem = self._live()
        if live is not None and self._slot_for_identity(data, live) == num:
            login, whose = live, "the live login's"
        else:
            login, whose = self._saved_login(num), "the saved login's"
            if login is None:
                return None, "no saved login; log in to it and run: qswap codex login"
        if login.account_id != (record.get("organizationUuid") or ""):
            return None, "the saved login belongs to another account"
        if login.access_expires_at is not None and login.access_expires_at <= self._usage_store.clock():
            return None, (
                f"{whose} access token has expired; Codex renews it the next "
                "time it runs on this account"
            )
        return login, ""

    def claim_usage_reset(
        self, account_num: str, grant_id: str, request_id: str
    ) -> resets.ClaimResult:
        """Spend reset credit ``grant_id`` on an account, then re-read its usage.

        Never raises: a login that cannot be used (missing, expired, a
        keyring-mode home) is ``auth_error`` with nothing sent, and network
        trouble is in the result (``codex_usage.consume_reset_credit``). Any
        answer the server decided (``reset``, ``already_redeemed``,
        ``nothing_to_reset``, ``no_credit``) makes the stored reading
        obsolete, so the account is made due and fetched before returning.
        """
        num = str(account_num)
        try:
            login, why = self._claim_login(num)
        except Exception as e:  # ProviderError (keyring home, registry), OSError
            login, why = None, str(e) or type(e).__name__
        if login is None:
            result = resets.ClaimResult("auth_error", detail=why)
        else:
            result = codex_usage.consume_reset_credit(
                login.access_token, login.account_id, grant_id, request_id
            )
        self._logger.info(
            "Codex reset credit claim on account %s: %s%s",
            num, result.result,
            f" ({result.reason or result.detail})" if (result.reason or result.detail) else "",
        )
        if result.settled or result.reason == "no_credit":
            try:
                self.refresh_usage({num})
            except Exception as e:  # the claim stands; the next poll re-reads
                self._logger.warning(f"Re-reading Codex account {num} after a claim failed: {e}")
        return result

    def reset_settings(self) -> ResetSettings:
        """The shared ``resets`` section (data root's ``settings.json``)."""
        return load_reset_settings(self.data_root)

    def set_poll_policy_inputs(
        self,
        threshold: float,
        weekly_threshold: float | None,
        models: tuple[str, ...],
    ) -> None:
        self._poll_inputs_override = (threshold, weekly_threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None

    def _poll_policy_inputs(self) -> tuple[float, float | None, tuple[str, ...]]:
        """Switch lines for poll planning: the Codex engine's own (its
        threshold is ``codex.threshold``), unless an engine pinned them. No
        Codex model names exist, so the ``autoswitch.model`` setting (Claude
        model names) is not applied here."""
        if self._poll_inputs_override is not None:
            return self._poll_inputs_override
        loaded = load_engine_settings(self.data_root, self.provider_id)
        return loaded.threshold, loaded.weekly_threshold, ()

    # -- usage ----------------------------------------------------------------

    def _accounts_info(self) -> list[_Info]:
        data = self._read_registry() or {}
        _home, _raw, live, _problem = self._live()
        live_num = self._slot_for_identity(data, live) if live is not None else None
        if live is not None:
            self._note_live_login(live_num, live)
        infos = []
        for num in self._sequence(data):
            record = data["accounts"][num]
            active = num == live_num
            infos.append(_Info(
                number=num,
                email=record.get("email", ""),
                account_id=record.get("organizationUuid", "") or "",
                is_active=active,
                login=live if active else self._saved_login(num),
                alias=record.get("alias", "") or "",
            ))
        return infos

    @staticmethod
    def _expired_sentinel(info: _Info) -> str:
        return USAGE_TOKEN_EXPIRED if info.is_active else USAGE_UNKNOWN_UNTIL_USED

    def _static_sentinel(self, info: _Info, entry: UsageEntry, now: float) -> str | None:
        """A state known without asking the server."""
        login = info.login
        if login is None:
            return USAGE_NO_CREDENTIALS
        if login.access_expires_at is not None and login.access_expires_at <= now:
            return self._expired_sentinel(info)
        if entry.rejected_fingerprint and entry.rejected_fingerprint == (
            codex_auth.token_fingerprint(login.access_token)
        ):
            # Refused before with this very token; ask again once it changes.
            return self._expired_sentinel(info)
        return None

    def _fetch_one(self, info: _Info, *, read_credits: bool = True) -> FetchRecord:
        login = info.login
        assert login is not None
        outcome = codex_usage.fetch_usage(
            login.access_token, login.account_id, read_credits=read_credits
        )
        if outcome.error == "http-401":
            # Live: the token expired while Codex was idle; Codex renews it.
            # Saved: unknown until Codex next uses it. Either way, stamp the
            # refused token so no pass asks again until it has changed.
            return FetchRecord(
                sentinel=self._expired_sentinel(info),
                rejected_fp=codex_auth.token_fingerprint(login.access_token),
            )
        return FetchRecord(
            usage=outcome.usage, error=outcome.error, retry_after_s=outcome.retry_after_s
        )

    def _plans_after_fetch(
        self,
        records: dict[str, FetchRecord],
        pre: dict[str, UsageEntry],
        info_by_num: dict[str, _Info],
    ) -> dict[str, tuple[float | None, float | None]]:
        now = self._usage_store.clock()
        threshold, weekly_threshold, models = self._poll_policy_inputs()
        plans: dict[str, tuple[float | None, float | None]] = {}
        for num, rec in records.items():
            if rec.sentinel is not None or rec.error is not None:
                continue
            before = pre.get(num)
            plans[num] = poll_policy.plan_after_fetch(
                prev_interval_s=before.poll_interval_s if before else None,
                prev_usage=before.last_good if before else None,
                new_usage=rec.usage,
                is_active=info_by_num[num].is_active,
                threshold=threshold,
                weekly_threshold=weekly_threshold,
                models=models,
                recent_429=before is not None and before.recent_429(now),
                now=now,
            )
        return plans

    def _collect_usage_entries(
        self,
        infos: list[_Info],
        fetch: set[str] | None = None,
        *,
        scheduled: bool = False,
    ) -> dict[str, UsageEntry]:
        """Store-backed collection with the shared poll policy and backoff.

        Same contract as the Claude collector: ``fetch=None`` makes every
        account eligible but respects stored poll plans (so a second ``list``
        inside the interval makes no request); a set restricts which accounts
        may be fetched. Eligibility is decided under the store's lock by
        ``UsageStore.reserve``. Fetches run one after another, outside every
        lock.
        """
        store = self._usage_store
        identities = {i.number: (i.email, i.account_id) for i in infos}
        info_by_num = {i.number: i for i in infos}
        _threshold, _weekly, models = self._poll_policy_inputs()
        entries = store.entries(identities, models)
        now = store.clock()
        sentinels: dict[str, str] = {}
        for info in infos:
            static = self._static_sentinel(info, entries[info.number], now)
            if static is not None:
                sentinels[info.number] = static
        requested = [
            n for n in identities
            if n not in sentinels and (fetch is None or n in fetch)
        ]
        claims = store.reserve(
            requested,
            identities,
            respect_plans=fetch is None,
            repair_overslept=fetch is None or scheduled,
        )
        if claims:
            # The shared ``resets.enabled``: whether the credit list is read.
            read_credits = self.reset_settings().enabled
            records = {
                num: self._fetch_one(info_by_num[num], read_credits=read_credits)
                for num in claims
            }
            plans = self._plans_after_fetch(records, entries, info_by_num)
            accepted = store.record(records, identities, claims, plans)
            for num in accepted:
                if records[num].sentinel is not None:
                    sentinels[num] = records[num].sentinel
            entries = store.entries(identities, models)
        return {n: with_sentinel(entries[n], sentinels.get(n)) for n in identities}

    def usage_entries_by_account(
        self, fetch: set[str] | None = None, *, scheduled: bool = False
    ) -> dict[str, UsageEntry]:
        return self._collect_usage_entries(self._accounts_info(), fetch, scheduled=scheduled)

    def refresh_usage(self, account_nums: set[str]) -> dict[str, UsageEntry]:
        infos = self._accounts_info()
        identities = {i.number: (i.email, i.account_id) for i in infos}
        nums = {str(n) for n in account_nums} & identities.keys()
        self._usage_store.mark_due(sorted(nums), identities)
        return self._collect_usage_entries(infos, fetch=nums)

    def refresh_account_usage(self, account_num: str) -> UsageEntry | None:
        """:meth:`refresh_usage` for one account; its entry, None if no such account."""
        return self.refresh_usage({str(account_num)}).get(str(account_num))

    def _plan_record(self, info: _Info, entry: UsageEntry) -> dict | None:
        """The plan as a ``subscription.PlanStore`` record, for the shared renderers.

        From the last usage response's ``plan_type`` (current), else the
        id_token's ``chatgpt_plan_type`` (a snapshot from the last refresh).
        """
        plan = None
        fetched_at = self._usage_store.clock()
        if isinstance(entry.last_good, dict):
            plan = entry.last_good.get(codex_usage.PLAN_KEY)
            if plan and entry.fetched_at is not None:
                fetched_at = entry.fetched_at
        if not plan and info.login is not None:
            plan = info.login.plan_type
        label = plan_label(plan)
        if label is None:
            return None
        return {"plan": {"tier": plan, "planDisplayName": label}, "fetchedAt": fetched_at}

    @staticmethod
    def _plan_type(record: dict | None) -> str | None:
        return record["plan"]["tier"] if record else None

    # -- surfaces -------------------------------------------------------------

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        infos = self._accounts_info()
        entries = self._collect_usage_entries(infos, fetch)
        data = self._read_registry() or {}
        active: str | None = None
        accounts = []
        for info in infos:
            if info.is_active:
                active = info.number
            entry = entries[info.number]
            accounts.append(AccountSnapshot(
                number=info.number,
                email=info.email,
                org_name="",
                org_uuid=info.account_id,
                is_active=info.is_active,
                kind="oauth",
                switchable=self._credential_path(info.number).exists(),
                usage=entry,
                alias=info.alias,
                disabled=self._disabled_from_data(data, info.number),
                policy=self._policy_from_data(data, info.number),
                subscription_plan=self._plan_record(info, entry),
                login_expires_at=None,
            ))
        return AccountsSnapshot(
            active_number=active, accounts=tuple(accounts), taken_at=self._usage_store.clock()
        )

    def _entry_lines(self, entry: UsageEntry) -> list[str]:
        """Usage lines for one row (sans the header), as ``list`` prints them."""
        from ai_quota_swap import display

        block = display.block_from_entry(
            None, entry, provider="codex",
            lines=self._display_lines(),
            sentinel_notes={k: v.replace(" — ", ": ") for k, v in SENTINEL_NOTES.items()},
            now=self._usage_store.clock(), stale_after_s=claude_switcher._USAGE_AGE_NOTE_S,
        )
        return [line.strip() for line in display.render_block(block)[1:]]

    def _display_lines(self):
        """Codex's switch lines, for severity colours (codex.threshold)."""
        from ai_quota_swap import display
        from ai_quota_swap.settings import load_engine_settings

        try:
            settings = load_engine_settings(self.data_root, self.provider_id)
        except Exception:
            return display.Lines()
        return display.Lines(five_hour=settings.threshold, weekly=settings.weekly_threshold)

    def label(self, number: str | int | None, email: str | None = None, *, long: bool = True) -> str:
        """How this account is named in a message (labels.account_label)."""
        from ai_quota_swap.labels import account_label

        if number is None:
            return email or "account"
        record = ((self._read_registry() or {}).get("accounts") or {}).get(str(number)) or {}
        return account_label(
            number, alias=record.get("alias") or None,
            email=email if email is not None else record.get("email", ""), long=long,
        )

    def check_switch_target(self, number: str, *, anyway: bool = False) -> list[str]:
        """Refuse a target the engine set aside until it is logged in again
        (unless ``anyway``, the ``--anyway`` flag: its only effect); return a
        one-line warning for one whose usage is unknown or old. Its
        expired-token state (usage unknown until used) is switchable by
        design: Codex renews the token when it next runs. ``--force`` keeps
        its own meaning (replace a live login qswap does not manage)."""
        from ai_quota_swap import display

        name = self.label(number, long=False)
        held = self.set_aside_accounts()
        if str(number) in held:
            why = display.set_aside_reason(held[str(number)])
            if not anyway:
                raise SwitchError(
                    f"{name} {display.NEEDS_RELOGIN} ({why}); not switched.\n"
                    f"Log in to it again: qswap codex login\n"
                    f"To switch to it anyway: qswap codex switch {number} --anyway"
                )
            return [f"{name} {display.NEEDS_RELOGIN} ({why}); switching anyway"]
        try:
            entry = self.usage_entries_by_account(fetch=set()).get(str(number))
        except Exception:
            return [f"{name}: {display.usage_unavailable(None, retrying=False)}; switching anyway"]
        if entry is None:
            return []
        if entry.sentinel == USAGE_UNKNOWN_UNTIL_USED:
            return [f"{name}: usage unknown until used; Codex renews its token when it runs"]
        if entry.decision_value() is not None or entry.sentinel is not None:
            return []
        if entry.last_good is not None and entry.age_s is not None:
            return [f"{name}: usage {display.as_of(entry.age_s)}; switching anyway"]
        if entry.last_error:
            return [f"{name}: {display.usage_unavailable(entry.last_error, retrying=False)}; "
                    "switching anyway"]
        return []

    def alias_for(self, number: str | int) -> str:
        """The account's alias, or ``""``."""
        record = ((self._read_registry() or {}).get("accounts") or {}).get(str(number)) or {}
        return record.get("alias", "") or ""

    def set_aside_accounts(self) -> dict[str, str]:
        """Accounts the Codex engine holds out until they are logged in again."""
        from ai_quota_swap.autoswitch import set_aside_accounts

        def identity(number: str) -> tuple[str, str | None] | None:
            email = self.account_email(number)
            if not email:
                return None
            creds = self.read_account_credentials(number, email)
            return email, (self.credential_fingerprint(creds) if creds else None)

        try:
            return set_aside_accounts(self.backup_dir, identity=identity)
        except Exception as e:
            self._logger.debug(f"set-aside read failed: {e!r}")
            return {}

    def account_block(
        self, info: _Info, entry: UsageEntry | None, data: dict, *,
        set_aside: dict[str, str] | None = None, show_provider: bool = False,
    ):
        """One account's :class:`display.AccountBlock`, as ``list`` shows it."""
        from ai_quota_swap import display

        now = self._usage_store.clock()
        record = self._plan_record(info, entry) if entry is not None else None
        plan, plan_warning = claude_switcher.ClaudeAccountSwitcher._plan_text(record, now)
        policy = self._policy_from_data(data, info.number)
        lines = self._display_lines()
        if policy.threshold is not None:
            lines = display.Lines(five_hour=policy.threshold, weekly=lines.weekly)
        return display.block_from_entry(
            info.number, entry,
            email=info.email, alias=info.alias, provider="codex",
            show_provider=show_provider, active=info.is_active,
            disabled=self._disabled_from_data(data, info.number),
            plan=plan, plan_warning=plan_warning, lines=lines,
            set_aside=(set_aside or {}).get(info.number),
            sentinel_notes={k: v.replace(" — ", ": ") for k, v in SENTINEL_NOTES.items()},
            now=now, stale_after_s=claude_switcher._USAGE_AGE_NOTE_S,
        )

    def _print_account(self, number: str) -> None:
        """One account's block, as ``list`` shows it (after a switch)."""
        from ai_quota_swap import display

        infos = [i for i in self._accounts_info() if i.number == str(number)]
        if not infos:
            return
        entries = self._collect_usage_entries(infos)
        block = self.account_block(
            infos[0], entries.get(str(number)), self._read_registry() or {},
            set_aside=self.set_aside_accounts(),
        )
        for line in display.render_block(block):
            print(line)

    def print_account_rows(self, infos: list[_Info], entries: dict[str, UsageEntry]) -> None:
        from ai_quota_swap import display

        data = self._read_registry() or {}
        set_aside = self.set_aside_accounts()
        for i, info in enumerate(infos):
            block = self.account_block(info, entries.get(info.number), data, set_aside=set_aside)
            for line in display.render_block(block):
                print(line)
            if i < len(infos) - 1:
                print()

    def _json_row(self, info: _Info, entry: UsageEntry, data: dict) -> dict:
        now = self._usage_store.clock()
        plan = self._plan_record(info, entry)
        row = account_row(
            int(info.number), info.email, "", info.account_id, info.is_active,
            entry.decision_value(),
            usage_fetched_at=entry.fetched_at,
            usage_age_s=entry.age_s,
            last_good_usage=entry.last_good,
            last_error=entry.last_error,
            backoff_until=entry.backoff_until if entry.in_backoff(now) else None,
            alias=info.alias,
            disabled=self._disabled_from_data(data, info.number),
            policy=self._policy_from_data(data, info.number),
            subscription_plan=plan,
        )
        # A ChatGPT account id names personal accounts too; only a workspace
        # plan makes the account an organization.
        row["isOrganization"] = is_workspace_plan(self._plan_type(plan))
        held = self.set_aside_accounts().get(info.number)
        row["needsRelogin"] = held is not None
        if held is not None:
            row["setAside"] = {"reason": held}
        return row

    def list_payload(self, fetch: set[str] | None = None) -> dict:
        """``{schemaVersion, activeAccountNumber, accounts}`` in the Claude list's shape."""
        data = self._read_registry()
        if not data or not data.get("accounts"):
            return {"schemaVersion": SCHEMA_VERSION, "activeAccountNumber": None, "accounts": []}
        infos = self._accounts_info()
        entries = self._collect_usage_entries(infos, fetch)
        active = next((int(i.number) for i in infos if i.is_active), None)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "activeAccountNumber": active,
            "accounts": [self._json_row(i, entries[i.number], data) for i in infos],
        }

    def list_accounts(self, json_output: bool = False, fetch: set[str] | None = None) -> dict | None:
        """``qswap codex list``: print the rows, or return the JSON payload."""
        if json_output:
            return self.list_payload(fetch)
        data = self._read_registry()
        if not data or not data.get("accounts"):
            for line in self.nothing_managed_lines():
                print(line)
            return None
        infos = self._accounts_info()
        entries = self._collect_usage_entries(infos, fetch)
        from ai_quota_swap import display

        print(bolded(display.provider_heading("codex", both=False)))
        self.print_account_rows(infos, entries)
        return None

    def nothing_managed_lines(self, *, sentence: bool = True) -> list[str]:
        """No Codex account managed: why, and the next step for this machine."""
        from ai_quota_swap import display

        email = self.live_login_email()
        from ai_quota_swap import clis

        found = None if email else clis.cli_found("codex")
        return [
            line.replace(display.NOTHING_MANAGED, "No Codex accounts are managed yet.")
            for line in display.nothing_managed_lines(
                "codex", live_email=email, cli_found=found, sentence=sentence,
            )
        ]

    def managed_count(self) -> int:
        """How many Codex accounts are managed."""
        data = self._read_registry()
        return len((data or {}).get("accounts") or {})

    def live_login_email(self) -> str | None:
        """The live Codex login's email, managed or not; None when there is
        none (or it cannot be read)."""
        try:
            _home, _raw, login, _problem = self._live()
        except Exception:
            return None
        return login.email if login is not None else None

    def status_lines(self) -> list[str]:
        """The live login's block under a "Codex" heading, or one line saying
        why there is none and what to do."""
        from ai_quota_swap import display

        out = [bolded(display.provider_name("codex"))]
        home, raw, login, problem = self._live()
        data = self._read_registry() or {}
        if login is None:
            if raw is not None:
                out.append(f"  The Codex login cannot be managed ({problem}).")
            elif data.get("accounts"):
                out.append("  No account is active. Activate one with: qswap codex switch")
            else:
                out.extend(f"  {line}" for line in self.nothing_managed_lines(sentence=False))
            return out
        num = self._slot_for_identity(data, login)
        if num is None:
            out.append(f"  {login.email} · not managed · add it with: qswap codex add")
            return out
        info = self._info_for(num, data, login)
        entry = self._collect_usage_entries([info])[num]
        block = self.account_block(info, entry, data, set_aside=self.set_aside_accounts())
        out.extend(display.render_block(block))
        return out

    def status(self, json_output: bool = False) -> dict | None:
        """``qswap codex status``: the live login, and its usage when managed."""
        home, raw, login, problem = self._live()
        data = self._read_registry() or {}
        num = self._slot_for_identity(data, login) if login is not None else None
        if json_output:
            if login is None:
                if raw is None:
                    return {"schemaVersion": SCHEMA_VERSION, "active": None}
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "active": {"email": None, "managed": False, "problem": problem},
                }
            if num is None:
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "active": {"email": login.email, "managed": False},
                }
            info = self._info_for(num, data, login)
            entry = self._collect_usage_entries([info])[num]
            plan = self._plan_record(info, entry)
            status, usage = usage_fields(entry.decision_value(), entry.fetched_at)
            active: dict = {
                "number": int(num),
                "email": login.email,
                "organizationName": "",
                "organizationUuid": login.account_id,
                "isOrganization": is_workspace_plan(self._plan_type(plan)),
                "managed": True,
                "usageStatus": status,
                "usage": usage,
            }
            if info.alias:
                active["alias"] = info.alias
            if usage is not None:
                active.update(usage_freshness_fields(entry.fetched_at, entry.age_s))
            else:
                active.update(last_good_usage_fields(entry.last_good, entry.fetched_at, entry.age_s))
                now = self._usage_store.clock()
                active.update(usage_failure_fields(
                    status, entry.last_error,
                    entry.backoff_until if entry.in_backoff(now) else None,
                ))
            return {
                "schemaVersion": SCHEMA_VERSION,
                "active": active,
                "totalManagedAccounts": len(data.get("accounts", {})),
            }

        if not data.get("accounts") and (raw is None or login is not None):
            # Nothing managed: the empty state and its next step, as `qswap
            # status` says it.
            for line in self.nothing_managed_lines():
                print(line)
            return None
        for line in self.status_lines():
            print(line)
        return None

    def _info_for(self, num: str, data: dict, live: CodexLogin) -> _Info:
        record = data["accounts"][num]
        return _Info(
            number=num,
            email=record.get("email", ""),
            account_id=record.get("organizationUuid", "") or "",
            is_active=True,
            login=live,
            alias=record.get("alias", "") or "",
        )

    # -- add / remove ---------------------------------------------------------

    @staticmethod
    def _checked_add_args(slot: int | None, alias: str | None) -> str | None:
        """The alias, normalized; refuses a bad alias or slot before anything runs."""
        if alias is not None:
            try:
                alias = normalize_alias(alias)
            except ValueError as e:
                raise ValidationError(str(e)) from e
        if slot is not None and slot < 1:
            raise ConfigError("Slot number must be >= 1")
        return alias

    def add_account(
        self,
        slot: int | None = None,
        assume_yes: bool = False,
        alias: str | None = None,
    ) -> None:
        """Save the login currently in ``auth.json`` as a managed account.

        A login that is already managed has its saved copy replaced, byte for
        byte, instead of being added twice.
        """
        alias = self._checked_add_args(slot, alias)
        home, raw, login, problem = self._live()
        path = codex_auth.auth_path(home)
        if raw is None:
            raise CodexLoginError(
                f"No Codex login found at {path}. Run codex login, then: qswap codex add"
            )
        if login is None:
            raise CodexLoginError(problem or f"{path} holds no ChatGPT login")
        self._save_login(login, slot=slot, assume_yes=assume_yes, alias=alias, live=True)

    def _save_login(
        self,
        login: CodexLogin,
        *,
        slot: int | None,
        assume_yes: bool,
        alias: str | None,
        live: bool,
    ) -> str | None:
        """Save ``login`` as a managed account: the body of ``add`` and ``login``.

        ``live`` says the login is the one in Codex's home, so it becomes the
        recorded active account; a login captured elsewhere (``qswap codex
        login``) does not. Returns the account number, None when cancelled.
        """
        # Ask before taking the lock (never hold it across a prompt), then
        # re-check what was asked about under the lock.
        displaced: str | None = None
        data = self._read_registry() or self._empty_registry()
        if slot is not None:
            occupant = data["accounts"].get(str(slot))
            if occupant is not None and self._slot_for_identity(
                {"accounts": {str(slot): occupant}}, login
            ) is None:
                from ai_quota_swap import prompts

                if not assume_yes and not prompts.interactive():
                    raise prompts.needs_confirmation(f"qswap codex add --slot {slot}")
                warning(f"Slot {slot} holds {self.label(slot, occupant.get('email', ''))}")

                if not prompts.confirm(
                    f"Overwrite slot {slot}?", assume_yes=assume_yes,
                    command=f"qswap codex add --slot {slot}",
                ):
                    print(dimmed("Cancelled; nothing was changed."))
                    return None
                displaced = occupant.get("email", "")

        carry = claude_switcher.ClaudeAccountSwitcher._carried_settings
        with self._lock():
            data = self._read_registry() or self._empty_registry()
            accounts = data["accounts"]
            existing = self._slot_for_identity(data, login)

            if slot is None and existing is not None:
                self._check_alias_free(accounts, alias, {existing})
                self._write_saved(existing, login.raw)
                record = accounts[existing]
                record["email"] = login.email
                record["uuid"] = login.user_id
                if alias is not None:
                    record["alias"] = alias
                self._write_registry(data)
                self._logger.info(f"Updated saved login for Codex account {existing}")
                print(
                    f"{accent('Updated')} the saved login of {self.label(existing, login.email)}"
                )
                return existing

            # Settings belong to the account, so only the same account's
            # records hand theirs on; a displaced occupant's end with it.
            carried: dict = {}
            moved_from: str | None = None
            if slot is not None:
                num = str(slot)
                occupant = accounts.get(num)
                same = occupant is not None and self._slot_for_identity(
                    {"accounts": {num: occupant}}, login
                ) is not None
                if occupant is not None and not same and occupant.get("email", "") != displaced:
                    raise ConfigError(
                        f"Slot {slot} changed while waiting for confirmation; nothing "
                        "was written, try again"
                    )
                if same:
                    carried = carry(occupant)
                if existing is not None and existing != num:
                    moved_from = existing
                    carried.update(carry(accounts[existing]))
            else:
                numbers = [int(n) for n in accounts if str(n).isdigit()]
                num = str(max(numbers) + 1 if numbers else 1)
            self._check_alias_free(accounts, alias, {num, moved_from})
            self._write_saved(num, login.raw)
            if moved_from is not None:
                accounts.pop(moved_from, None)
                self._credential_path(moved_from).unlink(missing_ok=True)
            record = {
                "email": login.email,
                "uuid": login.user_id,
                "organizationUuid": login.account_id,
                "organizationName": "",
                "added": get_timestamp(),
                **carried,
            }
            if alias is not None:
                record["alias"] = alias
            accounts[num] = record
            data["sequence"] = sorted(
                {int(n) for n in data.get("sequence", []) if str(n) in accounts} | {int(num)}
            )
            if live or (moved_from is not None and str(data.get("activeAccountNumber")) == moved_from):
                data["activeAccountNumber"] = int(num)
            self._write_registry(data)

        self._logger.info(f"Added Codex account {num}")
        if moved_from is not None:
            print(dimmed(f"Moved from slot {moved_from} → {num}"))
        print(f"{accent('Added')} {self.label(num, login.email)}")
        if live and len(data["accounts"]) == 1:
            print(dimmed(
                "To add another account without signing this one out, run: "
                "qswap codex login"
            ))
        return num

    # -- logging in a further account -------------------------------------------

    def _login_homes(self) -> list[Path]:
        """Temporary login homes under the data root (``qswap codex login``)."""
        try:
            return [p for p in self.backup_dir.iterdir() if p.name.startswith(LOGIN_HOME_PREFIX)]
        except OSError:
            return []

    def _remove_login_home(self, home: Path) -> bool:
        """Delete a temporary login home; True when it is gone. Never raises.

        When the tree cannot be removed whole, the login file itself is
        deleted at least, so no token is left behind; the next ``qswap codex
        login`` sweeps what remains.
        """
        try:
            shutil.rmtree(home)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            self._logger.warning(f"Could not delete temporary Codex home {home}: {e}")
            try:
                codex_auth.auth_path(home).unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _sweep_login_homes(self, now: float | None = None) -> None:
        """Delete temporary login homes a run left behind (a crash, a kill)
        once they are a day old; a younger one may belong to a ``qswap codex
        login`` running in another terminal. Never follows a symlink."""
        now = time.time() if now is None else now
        for home in self._login_homes():
            try:
                st = home.lstat()
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or now - st.st_mtime < LOGIN_HOME_MAX_AGE_S:
                continue
            self._remove_login_home(home)

    def login_new_account(
        self,
        codex_args: list[str] | tuple[str, ...] = (),
        *,
        slot: int | None = None,
        alias: str | None = None,
        assume_yes: bool = False,
        runner=None,
    ) -> str | None:
        """``qswap codex login``: log a further account in and save it.

        ``codex login`` signs out (revokes) whatever login is already in its
        home before it logs in, so the new login is made under an empty,
        private (0700) temporary home in the data root, with ``CODEX_HOME``
        set for ``codex`` alone and the user's terminal attached;
        ``codex_args`` are passed through (``--device-auth``). The login it
        leaves is saved with ``add``'s code (an account already managed has
        its saved copy updated), then the temporary home is deleted, whatever
        happened. Codex's own home is never touched and the live account does
        not change.

        ``runner(home, args) -> processes.LoginRun`` stands in for
        :func:`processes.run_codex_login` (tests never start ``codex``).
        Returns the account number; raises :class:`CodexLoginError` with
        nothing saved when ``codex login`` fails, cannot start, or leaves no
        ``auth.json``. Ctrl-C deletes the temporary home and propagates.
        """
        alias = self._checked_add_args(slot, alias)
        self._setup_directories()
        self._sweep_login_homes()
        run = runner or codex_processes.run_codex_login
        home = Path(tempfile.mkdtemp(prefix=LOGIN_HOME_PREFIX, dir=self.backup_dir))
        try:
            if sys.platform != "win32":
                os.chmod(home, 0o700)
            print(dimmed(
                "Logging in under a temporary Codex home; your current Codex "
                "login stays live."
            ))
            try:
                outcome = run(home, list(codex_args))
            except KeyboardInterrupt:
                print(dimmed("codex login was interrupted; nothing was saved."))
                raise
            if outcome.returncode is None:
                raise CodexLoginError(
                    f"Could not run codex login ({outcome.detail}); nothing was saved."
                )
            if outcome.returncode != 0:
                raise CodexLoginError(
                    f"codex login failed (exit code {outcome.returncode}); nothing was saved."
                )
            path = codex_auth.auth_path(home)
            raw = codex_auth.read_auth_bytes(path)
            if raw is None or not raw.strip():
                raise CodexLoginError(
                    "codex login left no auth.json, so there was nothing to save. "
                    "Codex may be set to keep logins in the OS keyring "
                    "(cli_auth_credentials_store), which qswap does not support."
                )
            try:
                login = codex_auth.parse_login(raw, source="The new login")
            except CodexLoginError as e:
                raise CodexLoginError(f"{e}; nothing was saved.") from e
            return self._save_login(
                login, slot=slot, assume_yes=assume_yes, alias=alias, live=False
            )
        finally:
            self._remove_login_home(home)

    @staticmethod
    def _check_alias_free(accounts: dict, alias: str | None, owners: set[str | None]) -> None:
        if alias is None:
            return
        for num, record in accounts.items():
            if num not in owners and (record.get("alias") or "").lower() == alias:
                raise ConfigError(f"Alias '{alias}' is already used by account {num}")

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Delete qswap's saved copy of a login and stop managing it.

        Never runs ``codex logout`` and never calls the revoke endpoint: the
        login itself stays valid wherever Codex holds it.
        """
        data = self._require_registry()
        num = self._resolve(data, identifier)
        email = data["accounts"][num].get("email", "")
        try:
            live_num = self.current_account_number()
        except CodexLoginError:
            live_num = None
        from ai_quota_swap import prompts

        name = self.label(num, email)
        if live_num == num:
            warning(f"Warning: {name} is the live Codex login")
        if not prompts.confirm(
            f"Stop managing {name} and delete qswap's saved copy of its login?",
            assume_yes=assume_yes, command="qswap codex remove",
        ):
            print(dimmed("Cancelled; nothing was removed."))
            return
        with self._lock():
            data = self._require_registry()
            if num not in data["accounts"] or data["accounts"][num].get("email", "") != email:
                raise ConfigError(
                    f"{self.label(num, long=False)} changed while waiting for confirmation; nothing "
                    "was removed, try again"
                )
            self._credential_path(num).unlink(missing_ok=True)
            del data["accounts"][num]
            data["sequence"] = [n for n in data.get("sequence", []) if str(n) != num]
            if str(data.get("activeAccountNumber")) == num:
                data["activeAccountNumber"] = None
            self._write_registry(data)
        self._logger.info(f"Removed Codex account {num}")
        print(f"{accent('Removed')} {name}")
        print(dimmed(
            "  Only qswap's saved copy was deleted; the login itself is untouched."
        ))
        warning(
            "  Do not run `codex logout` to clean up: it signs the login out at\n"
            "  OpenAI, and every saved copy of it stops working."
        )

    # -- aliases and policy ---------------------------------------------------

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        try:
            normalized = normalize_alias(alias)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        with self._lock():
            data = self._require_registry()
            num = self._resolve(data, identifier)
            for other, record in data["accounts"].items():
                if other != num and (record.get("alias") or "").lower() == normalized:
                    raise ConfigError(f"Alias '{normalized}' is already used by account {other}")
            data["accounts"][num]["alias"] = normalized
            self._write_registry(data)
        return num, normalized

    def unset_alias(self, identifier: str) -> str:
        with self._lock():
            data = self._require_registry()
            num = self._resolve(data, identifier)
            if "alias" in data["accounts"][num]:
                del data["accounts"][num]["alias"]
                self._write_registry(data)
        return num

    def list_aliases(self) -> list[tuple[str, str, str]]:
        data = self._read_registry() or {}
        rows = [
            (num, record.get("alias"), record.get("email", ""))
            for num, record in (data.get("accounts") or {}).items()
            if record.get("alias")
        ]
        return sorted(rows, key=lambda r: int(r[0]))

    def set_account_threshold(self, identifier: str, threshold: object | None) -> None:
        normalized = None if threshold is None else normalize_account_threshold(threshold)
        with self._lock():
            data = self._require_registry()
            num = self._resolve(data, identifier)
            record = data["accounts"][num]
            email = record.get("email", "")
            if normalized is None and "threshold" not in record:
                say(dimmed(
                    f"{self.label(num, email)} has no line of its own; it already "
                    "uses codex.threshold"
                ))
                return
            if normalized is not None and record.get("threshold") == normalized:
                say(dimmed(f"{self.label(num, email)} already switches at {normalized:g}%"))
                return
            if normalized is None:
                record.pop("threshold", None)
            else:
                record["threshold"] = normalized
            self._write_registry(data)
        if normalized is None:
            say(f"{accent('Cleared')} the switch line of {self.label(num, email)}; "
                  "it uses codex.threshold again")
        else:
            say(f"{accent('Set')} the switch line of {self.label(num, email)} to {normalized:g}%")

    def set_account_priority(self, identifier: str, priority: object | None) -> None:
        normalized = 0 if priority is None else normalize_account_priority(priority)
        with self._lock():
            data = self._require_registry()
            num = self._resolve(data, identifier)
            record = data["accounts"][num]
            if normalized:
                changed = record.get("priority") != normalized
                record["priority"] = normalized
            else:
                changed = "priority" in record
                record.pop("priority", None)
            if changed:
                self._write_registry(data)
        say(f"{accent('Set')} the priority of {self.label(num)} to {normalized} "
              "(used by qswap codex auto)")

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        verb = "disabled" if disabled else "enabled"
        with self._lock():
            data = self._require_registry()
            num = self._resolve(data, identifier)
            record = data["accounts"][num]
            email = record.get("email", "")
            if bool(record.get("disabled")) == disabled:
                print(dimmed(f"{self.label(num, email)} is already {verb}"))
                return
            if disabled:
                record["disabled"] = True
            else:
                record.pop("disabled", None)
            self._write_registry(data)
        print(f"{accent(verb.capitalize())} {self.label(num, email)}")
        if disabled:
            try:
                live = self.current_account_number()
            except CodexLoginError:
                live = None
            if live == num:
                print(dimmed(
                    "  It is the live login: it stays in use until you switch away,\n"
                    "  but a bare switch and auto-switch will not pick it again."
                ))
            if not self.switchable_account_numbers():
                warning(
                    "  No account is left in rotation, so a bare switch has nothing\n"
                    "  to pick. Bring one back: qswap codex enable N"
                )
        else:
            print(dimmed("  It is back in rotation."))

    # -- switching ------------------------------------------------------------

    def _noop(self, *, strategy: str, reason: str, message: str, ref: dict | None,
              warnings: list[str] | None = None) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "switched": False,
            "from": ref,
            "to": ref,
            "strategy": strategy,
            "reason": reason,
            "message": message,
            "warnings": warnings or [],
        }

    def _result(self, op: dict, strategy: str, extra_warnings: list[str] | None = None) -> dict:
        switched = op["from"] != op["to"]
        to = op["to"]
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "switched": switched,
            "from": op["from"],
            "to": to,
            "strategy": strategy,
            "reason": "switched" if switched else "already-active",
            "message": (
                f"Switched to Account-{to['number']} ({to['email']})" if switched
                else f"Already on Account-{to['number']} ({to['email']})"
            ),
            "warnings": (extra_warnings or []) + op["warnings"],
        }
        if op.get("backgroundServer") is not None:
            result["backgroundServer"] = op["backgroundServer"]
        return result

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict | None:
        """Make an account's saved login the live one (design: switch procedure).

        ``force`` overwrites a live login qswap does not manage (it is lost
        unless saved elsewhere) and rewrites the live login even when it is
        already the target.
        """
        data = self._require_registry()
        target = self._resolve(data, identifier)
        record = data["accounts"][target]
        if not force:
            _home, _raw, live, _problem = self._live()
            if live is not None and self._slot_for_identity(data, live) == target:
                ref = account_ref(int(target), record.get("email", ""))
                message = f"Already on Account-{target} ({record.get('email', '')})"
                if json_output:
                    return self._noop(strategy="direct", reason="already-active",
                                      message=message, ref=ref)
                print(f"{accent('Already on')} {self.label(target)}")
                return None
        op = self._perform_switch(target, emit_output=not json_output, force=force)
        return self._result(op, "direct") if json_output else None

    def switch(
        self,
        strategy: str | None = None,
        json_output: bool = False,
        models: tuple[str, ...] = (),
        model_source: str | None = None,
    ) -> dict | None:
        """Rotate to the next account in sequence (plain rotation only)."""
        if strategy is not None:
            raise FeatureUnavailableError(
                "Usage-aware switch strategies are not available for Codex yet"
            )
        data = self._require_registry()
        _home, raw, live, _problem = self._live()
        sequence = self._sequence(data)
        current = self._slot_for_identity(data, live) if live is not None else None
        if live is not None and current is None:
            ref = account_ref(None, live.email)
            if json_output:
                return self._noop(
                    strategy="rotation", reason="unmanaged-account", ref=ref,
                    message="Active Codex login is not managed; run qswap codex add",
                )
            print(f"{accent('Notice:')} Active Codex login '{live.email}' was not managed.")
            self.add_account()
            added = self.current_account_number()
            print(f"It has been automatically added as {self.label(added, long=False)}.")
            print(dimmed("Please run the switch command again to switch to the next account."))
            return None
        if current is not None and len(sequence) < 2:
            ref = account_ref(int(current), live.email if live else "")
            message = "Only one account is managed. Add more accounts to switch between."
            if json_output:
                return self._noop(strategy="rotation", reason="only-one-account",
                                  message=message, ref=ref)
            print(dimmed(message))
            return None

        warnings: list[str] = []
        if current is None:
            # No live login (or nothing qswap could read): start from the
            # recorded account, else the first in sequence.
            preferred = str(data.get("activeAccountNumber") or "")
            order = ([preferred] if preferred in sequence else []) + [
                n for n in sequence if n != preferred
            ]
        else:
            idx = sequence.index(current) if current in sequence else -1
            order = [sequence[(idx + k) % len(sequence)] for k in range(1, len(sequence))]
        target = None
        held = self.set_aside_accounts()
        skipped: dict[str, list[str]] = {"relogin": [], "disabled": [], "broken": []}
        for candidate in order:
            if self._disabled_from_data(data, candidate):
                skipped["disabled"].append(candidate)
                self._skip(warnings, json_output, candidate, "disabled")
                continue
            if self._saved_login(candidate) is None:
                skipped["broken"].append(candidate)
                self._skip(warnings, json_output, candidate, "no saved login")
                continue
            if candidate in held:
                # The engine set it aside: landing on it would leave Codex on
                # a login that no longer works.
                skipped["relogin"].append(candidate)
                self._skip(warnings, json_output, candidate, "needs re-login")
                continue
            target = candidate
            break
        if target is None:
            ref = account_ref(int(current), live.email) if current and live else None
            message = "No other accounts have a usable saved login."
            if json_output:
                return self._noop(strategy="rotation", reason="no-valid-target",
                                  message=message, ref=ref, warnings=warnings)
            steps = []
            if skipped["relogin"]:
                steps.append("log in to one that needs re-login again: qswap codex login")
            if skipped["disabled"]:
                steps.append(
                    f"bring a disabled one back: qswap codex enable {skipped['disabled'][0]}"
                )
            if skipped["broken"]:
                steps.append("save a login for one: qswap codex login")
            if current is not None:
                say(dimmed(
                    f"No other account can take over, so Codex stays on "
                    f"{self.label(current, long=False)}."
                ))
            else:
                say(dimmed("No account can be activated."))
            if steps:
                say(dimmed("To fix that, " + "; or ".join(steps)))
            return None
        # Never forced: a live login qswap cannot read is refused, not overwritten.
        op = self._perform_switch(target, emit_output=not json_output, force=False)
        return self._result(op, "rotation", warnings) if json_output else None

    def _skip(self, warnings: list[str], json_output: bool, number: str, why: str) -> None:
        # JSON keeps its wording; text names the account the new way, in the
        # Claude switcher's form ("Skipped #2 work: needs re-login.").
        if json_output:
            warnings.append(f"Skipped Account-{number} ({why})")
        else:
            say(f"{accent('Skipped')} {self.label(number, long=False)}: {why}.")

    def _processes_on_home(self, home: Path) -> list[codex_processes.CodexProcess]:
        """Codex processes that may hold this home's login in memory."""
        procs = codex_processes.list_codex_processes()
        on_default = _same_dir(home, get_default_codex_home())
        holders = [
            p for p in procs
            if (p.kind == codex_processes.DESKTOP and on_default)
            or (p.kind != codex_processes.DESKTOP
                and (p.codex_home is None or _same_dir(p.codex_home, home)))
        ]
        daemon = codex_processes.background_server_pid(home)
        if daemon is not None and all(p.pid != daemon for p in holders):
            holders.append(codex_processes.CodexProcess(
                daemon, codex_processes.BACKGROUND_SERVER, home
            ))
        return holders

    def _perform_switch(self, target: str, *, emit_output: bool, force: bool) -> dict:
        """The design's switch procedure, steps 1 to 7."""
        data = self._require_registry()
        record = data["accounts"][target]
        target_email = record.get("email", "")
        target_raw = self._read_saved(target)
        if not target_raw:
            raise SwitchError(
                f"{self.label(target, long=False)} has no saved login; log in to it and run qswap codex add"
            )
        try:
            target_login = codex_auth.parse_login(target_raw, source=f"Account-{target}'s saved login")
        except CodexLoginError as e:
            raise SwitchError(str(e)) from e
        if target_login.account_id != (record.get("organizationUuid") or ""):
            raise SwitchError(
                f"{self.label(target, long=False)}'s saved login belongs to another account; "
                "log in to it and run qswap codex add"
            )

        home = self.check_storage()
        path = codex_auth.auth_path(home)

        # Step 1: a token about to be renewed by a running Codex must not be
        # swapped out from under it.
        live_login = codex_auth.try_parse_login(codex_auth.read_auth_bytes(path))
        now = time.time()
        if (
            live_login is not None
            and live_login.access_expires_at is not None
            and live_login.access_expires_at - now < NEAR_EXPIRY_S
        ):
            holders = self._processes_on_home(home)
            if holders:
                pids = ", ".join(str(p.pid) for p in holders)
                raise RetryableSwitchError(
                    f"The live Codex login's access token is about to expire and "
                    f"Codex is running on {home} (pid {pids}), so Codex may be "
                    "renewing it now. Nothing was changed; try again in a few minutes."
                )

        warnings: list[str] = []
        from_ref: dict | None = None
        with self._lock():
            with codex_auth.codex_auth_locks(home):
                # Step 3: re-read under the locks and save the outgoing login,
                # which keeps any renewal Codex did since it was saved.
                data = self._require_registry()
                if target not in data["accounts"]:
                    raise AccountNotFoundError(f"#{target} does not exist; qswap codex list shows the accounts")
                # The saved copy may have been replaced since it was read above
                # (another qswap saving a renewal); write what is there now.
                target_raw = self._read_saved(target) or b""
                current = codex_auth.try_parse_login(target_raw)
                if current is None or current.identity != target_login.identity:
                    raise SwitchError(
                        f"{self.label(target, long=False)} changed while switching; nothing was "
                        "changed, try again"
                    )
                live_raw = codex_auth.read_auth_bytes(path)
                if live_raw is not None and live_raw.strip():
                    try:
                        live = codex_auth.parse_login(live_raw)
                        problem = None
                    except CodexLoginError as e:
                        live, problem = None, str(e)
                    live_num = self._slot_for_identity(data, live) if live else None
                    if live_num is not None:
                        self._write_saved(live_num, live_raw)
                        from_ref = account_ref(int(live_num), live.email)
                        if live_num == target:
                            target_raw = live_raw
                    elif not force:
                        who = live.email if live is not None else f"a login qswap cannot read ({problem})"
                        raise SwitchError(
                            f"The live Codex login ({who}) is not managed by qswap, and "
                            "switching would overwrite it. Run qswap codex add to keep it, "
                            "or pass --force to overwrite it."
                        )
                    else:
                        from_ref = account_ref(None, live.email if live else "")
                        msg = (
                            f"Overwrote an unmanaged Codex login ({live.email if live else 'unreadable'})"
                        )
                        warnings.append(msg)
                # Step 4: the target's bytes, atomically, through any symlink.
                codex_auth.check_replaceable(path)
                codex_auth.write_auth_bytes(path, target_raw)
                data["activeAccountNumber"] = int(target)
                self._write_registry(data)
                self._logger.info(
                    f"Switched Codex from account {from_ref['number'] if from_ref else None} "
                    f"to {target}"
                )
        # Step 5: locks released; confirm the file now names the target.
        after = codex_auth.try_parse_login(codex_auth.read_auth_bytes(path))
        if after is None or after.identity != target_login.identity:
            now_holds = after.email if after is not None else "no readable login"
            raise SwitchError(
                f"{path} changed right after the switch and now holds {now_holds}, "
                f"not {self.label(target, long=False)}. A running Codex may have written its login "
                "back; quit Codex and switch again."
            )
        to_ref = account_ref(int(target), target_email)

        # Step 6: the activation log, so ``qswap usage`` can tell which account
        # produced the Codex sessions from here on.
        if from_ref != to_ref:
            self._record_activation(target, target_email, target_login.account_id)

        # Step 7: whoever still holds the old login in memory.
        background = None
        if from_ref != to_ref:
            background = self._handle_holders(home, target, warnings)

        if emit_output:
            source = (
                f"{self.label(from_ref.get('number'), long=False)} → "
                if from_ref and from_ref.get("number") is not None else "to "
            )
            print(f"{accent('Switched')} {source}{self.label(target, target_email)}")
            try:
                self._print_account(target)
            except Exception as e:  # display only
                self._logger.debug(f"post-switch display failed: {e!r}")
            for msg in warnings:
                warning(msg)
            if background is not None and background.get("restarted"):
                print(dimmed(
                    f"Restarted Codex's background server (pid {background['pid']}); "
                    "new sessions use this account."
                ))
            elif background is None:
                print(dimmed("New codex sessions use this account."))
        self._replan_new_active(target, target_email, target_login.account_id)
        op = {"from": from_ref, "to": to_ref, "warnings": warnings}
        if background is not None:
            op["backgroundServer"] = background
        return op

    def _handle_holders(self, home: Path, target: str, warnings: list[str]) -> dict | None:
        """Step 7: restart the background server (``codex.restartDaemon``), and
        warn about the desktop app and other running Codex processes.

        Never raises: the switch has already happened, so a failed or timed-out
        restart is a warning in the result.
        """
        procs = codex_processes.list_codex_processes()
        daemon = codex_processes.background_server_pid(home)
        background = None
        if daemon is not None:
            restart = "codex app-server daemon restart"
            if load_codex_settings(self.data_root).restart_daemon:
                result = codex_processes.restart_background_server(home)
                if result.ok:
                    background = {"pid": daemon, "restarted": True}
                else:
                    background = {"pid": daemon, "restarted": False, "reason": result.detail}
                    warnings.append(
                        f"Could not restart the Codex background server (pid {daemon}): "
                        f"{result.detail}. New codex sessions keep the old account "
                        f"until you run: {restart}"
                    )
            else:
                background = {"pid": daemon, "restarted": False,
                              "reason": "codex.restartDaemon is off"}
                warnings.append(
                    f"The Codex background server (pid {daemon}) still holds the old "
                    f"account (codex.restartDaemon is off); new codex sessions use it "
                    f"until you run: {restart}"
                )
        if _same_dir(home, get_default_codex_home()) and any(
            p.kind == codex_processes.DESKTOP for p in procs
        ):
            warnings.append(
                "The ChatGPT desktop app is running and keeps the old account until "
                "you quit and reopen it"
            )
        others = sorted(
            p.pid for p in procs
            if p.kind != codex_processes.DESKTOP
            and p.pid != daemon
            and (p.codex_home is None or _same_dir(p.codex_home, home))
        )
        if others:
            many = len(others) > 1
            warnings.append(
                f"{len(others)} running codex process{'es' if many else ''} "
                f"(pid{'s' if many else ''} {', '.join(map(str, others))}) "
                f"stay{'' if many else 's'} on the old account until "
                f"{'they exit' if many else 'it exits'}"
            )
        return background

    # -- activation log (ledger) -------------------------------------------------

    def _record_activation(self, number: str, email: str, account_id: str) -> None:
        """Append a ``switch`` line to ``activations.jsonl`` (``qswap usage``
        attributes Codex tokens by it). Never raises; takes no lock."""
        try:
            from ai_quota_swap.ledger.activations import record_activation

            record_activation(
                self.backup_dir, number=number, email=email, org=account_id, source="switch"
            )
        except Exception:
            pass

    def _note_live_login(self, number: str | None, live: CodexLogin) -> None:
        """Append an ``observed`` line when the live login differs from the
        log's last line (a ``codex login``, another tool). Cheap when nothing
        changed (a stat). Never raises; takes no lock."""
        try:
            from ai_quota_swap.ledger.activations import observe_live_login

            observe_live_login(
                self.backup_dir, number=number, email=live.email, org=live.account_id
            )
        except Exception:
            pass

    def observe_activation(self) -> None:
        """Note the live login in the activation log, for callers that have not
        resolved it themselves. Never raises."""
        try:
            _home, _raw, live, _problem = self._live()
            if live is not None:
                data = self._read_registry() or {}
                self._note_live_login(self._slot_for_identity(data, live), live)
        except Exception:
            pass

    def _replan_new_active(self, number: str, email: str, account_id: str) -> None:
        """Pull the new live account's next poll forward to the active floor
        (best effort: the switch has committed)."""
        try:
            identities = {number: (email, account_id)}
            now = self._usage_store.clock()
            entry = self._usage_store.entries(identities).get(number)
            if entry is None or entry.fetched_at is None:
                return
            next_poll = max(now, entry.fetched_at + poll_policy.MIN_INTERVAL_S)
            if entry.next_poll_at is not None and entry.next_poll_at <= next_poll:
                return
            self._usage_store.set_poll_plan(
                {number: (next_poll, poll_policy.MIN_INTERVAL_S)}, identities
            )
        except Exception as e:
            self._logger.warning(f"Post-switch poll re-plan failed (switch succeeded): {e}")
