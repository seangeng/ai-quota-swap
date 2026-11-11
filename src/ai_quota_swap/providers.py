"""Providers: the contracts a provider's switcher implements, and the registry.

A provider is a switcher object with its own data root and its own engine
instance (docs/design/providers.md). ``ClaudeAccountSwitcher`` is the Claude
Code provider as it stands; ``codex.switcher.CodexSwitcher`` is the Codex one
and exposes the same members.

The two Protocols below are written against the members' real signatures.
``tests/test_providers.py`` checks that both switchers have every member with a
matching signature, so it fails when a switcher stops fitting them. The project
runs no type checker.

Optional engine-host members, outside the Protocol: the engine reads them with
``getattr`` and falls back to Claude Code's behaviour when a switcher lacks
them, so the Claude switcher defines none.

- ``new_reset_request_id() -> str`` and ``is_reset_request_id(value) -> bool``:
  the format of the idempotency key a reset claim carries
  (``autoswitch.reset_request_id``). Default: 32 hex digits, any stored string
  reused. Codex: a UUID v4.
- ``add_command: str``: the qswap command that captures the live login, quoted
  in ``no-switch`` details. Default ``qswap --add-account``.
- ``recover_hint: str``: the sentence that tells a quarantined account's owner
  how to recover it. Default: re-add it with ``qswap add --slot N``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ai_quota_swap.exceptions import ProviderError, ProviderUnavailableError

if TYPE_CHECKING:
    from ai_quota_swap import oauth, resets
    from ai_quota_swap.models import AccountPolicy, AccountsSnapshot
    from ai_quota_swap.settings import ResetSettings
    from ai_quota_swap.usage_store import UsageEntry

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "EngineHost",
    "ProviderError",
    "ProviderUnavailableError",
    "SurfaceHost",
    "ensure_available",
    "make_switcher",
    "split_provider_prefix",
]


@runtime_checkable
class EngineHost(Protocol):
    """Everything the auto-switch engine reaches its switcher through (Contract 1)."""

    provider_id: str
    """Stable provider id, e.g. ``"claude"``."""

    display_name: str
    """Product name shown to people, e.g. ``"Claude Code"``."""

    cli_name: str
    """Name of the provider's own command-line tool, e.g. ``"claude"``."""

    backup_dir: Path
    """The provider's data root; the engine keeps its state file here."""

    def current_account_number(self) -> str | None:
        """Number of the managed account whose login is live, or None."""
        ...

    def has_live_login(self) -> bool:
        """True when some login is live, managed or not."""
        ...

    def switchable_account_numbers(self) -> list[str]:
        """Account numbers eligible for automatic selection, in rotation order."""
        ...

    def disabled_account_numbers(self) -> list[str]:
        """Accounts the user holds out of rotation, in sequence order."""
        ...

    def account_kind_for(self, account_num: str) -> str:
        """``"oauth"`` or ``"api_key"``."""
        ...

    def account_email(self, account_num: str) -> str:
        """Stored email for an account; empty string when unknown."""
        ...

    def account_identity(self, account_num: str) -> dict:
        """Stored identity as ``{"email", "organizationUuid", "uuid"}``."""
        ...

    def backfill_account_uuid(
        self,
        account_num: str,
        uuid: str,
        expected_email: str | None = None,
        expected_org: str | None = None,
    ) -> None:
        """Record a resolved uuid on an account that lacks one (may be a no-op)."""
        ...

    def account_policies(self) -> dict[str, AccountPolicy]:
        """Per-account threshold, priority and disabled flag."""
        ...

    def read_account_credentials(self, account_num: str, email: str) -> str:
        """The stored login, opaque; empty string when missing."""
        ...

    def credential_expires_at_ms(self, credentials: str) -> int | float | None:
        """Access-token expiry in epoch ms, None if none; raises CredentialFormatError for a non-login."""
        ...

    def credential_fingerprint(self, credentials: str) -> str | None:
        """Stable lineage hash of a stored login; None only for empty input."""
        ...

    def live_session_pids_for(self, account_num: str, email: str) -> list[int]:
        """PIDs of live ``run`` sessions using this account."""
        ...

    def consume_backup_grant(
        self, account_num: str, email: str, snapshot: str
    ) -> oauth.RefreshOutcome:
        """Refresh an idle account's stored login through the provider's gate."""
        ...

    def usage_entries_by_account(
        self, fetch: set[str] | None = None, *, scheduled: bool = False
    ) -> dict[str, UsageEntry]:
        """Usage entry per account, fetching only accounts in ``fetch``."""
        ...

    def refresh_usage(self, account_nums: set[str]) -> dict[str, UsageEntry]:
        """Usage entry per account, with ``account_nums`` fetched now."""
        ...

    def claim_usage_reset(
        self, account_num: str, grant_id: str, request_id: str,
        *, before_send: Callable[[], None] | None = None,
    ) -> resets.ClaimResult:
        """Spend one usage reset of ``grant_id`` on an account; ``before_send``
        runs immediately before the request (nothing is sent without it
        having run)."""
        ...

    def switch_to(
        self,
        identifier: str,
        json_output: bool = False,
        force: bool = False,
        log_out_sessions: bool = False,
    ) -> dict | None:
        """Make an account the live login; JSON mode returns ``{switched, from, to, warnings, reason}``.

        ``log_out_sessions`` goes ahead with a switch to an API-key account
        while Claude Code sessions run on the shared login (refused
        otherwise); a provider without API-key accounts ignores it."""
        ...

    def set_poll_policy_inputs(
        self,
        threshold: float,
        weekly_threshold: float | None,
        models: tuple[str, ...],
    ) -> None:
        """Pin the switch lines and models that poll planning keys on."""
        ...

    def clear_poll_policy_inputs(self) -> None:
        """Drop the pin set by :meth:`set_poll_policy_inputs`."""
        ...

    def reset_settings(self) -> ResetSettings:
        """The ``resets`` settings section."""
        ...


@runtime_checkable
class SurfaceHost(Protocol):
    """What the list view, TUI and menu bar render from and act through (Contract 3)."""

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        """One structured snapshot of every managed account."""
        ...

    def switch_to(
        self,
        identifier: str,
        json_output: bool = False,
        force: bool = False,
        log_out_sessions: bool = False,
    ) -> dict | None:
        """Make an account the live login (``log_out_sessions``: see
        :meth:`EngineHost.switch_to`)."""
        ...

    def switch(
        self,
        strategy: str | None = None,
        json_output: bool = False,
        models: tuple[str, ...] = (),
        model_source: str | None = None,
    ) -> dict | None:
        """Switch to the next account, by rotation or by a usage strategy."""
        ...

    def add_account(
        self,
        slot: int | None = None,
        assume_yes: bool = False,
        alias: str | None = None,
    ) -> None:
        """Capture the live login as a managed account."""
        ...

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Stop managing an account."""
        ...

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        """Hold an account out of rotation, or return it."""
        ...


# -- registry ------------------------------------------------------------------

PROVIDERS: tuple[str, ...] = ("claude", "codex")
"""Provider ids, in display order."""

DEFAULT_PROVIDER = "claude"
"""The provider a bare ``qswap <verb>`` acts on."""

# Providers with no switcher in this version, and what to tell the user.
_UNAVAILABLE: dict[str, str] = {}


def split_provider_prefix(argv: Sequence[str]) -> tuple[str, list[str]]:
    """Split a leading provider argument off a command line.

    Returns ``(provider_id, remaining_argv)``. Only a non-default provider is a
    prefix (``qswap codex list``); ``claude`` is not, so a bare verb keeps
    meaning Claude Code and ``qswap claude`` keeps whatever it meant before.
    """
    if argv and argv[0] in PROVIDERS and argv[0] != DEFAULT_PROVIDER:
        return argv[0], list(argv[1:])
    return DEFAULT_PROVIDER, list(argv)


def ensure_available(provider_id: str) -> None:
    """Raise :class:`ProviderUnavailableError` unless this version implements the provider."""
    if provider_id not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider_id!r}")
    if provider_id in _UNAVAILABLE:
        raise ProviderUnavailableError(_UNAVAILABLE[provider_id])


def make_switcher(provider_id: str = DEFAULT_PROVIDER, *, debug: bool = False) -> EngineHost:
    """A new switcher for ``provider_id``.

    Raises :class:`ProviderUnavailableError` for a provider this version does
    not implement before constructing anything.
    """
    ensure_available(provider_id)
    # Imported here, not at module level: switcher.py imports most of the
    # package, and this module must stay importable from any of it.
    if provider_id == "codex":
        from ai_quota_swap.codex.switcher import CodexSwitcher

        return CodexSwitcher(debug=debug)
    from ai_quota_swap.switcher import ClaudeAccountSwitcher

    return ClaudeAccountSwitcher(debug=debug)
