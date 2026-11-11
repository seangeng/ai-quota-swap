"""Exceptions for ai-quota-swap."""


class ProviderError(Exception):
    """Base for every provider's errors; surfaces and the engine catch this.

    The provider design (docs/design/providers.md) calls this base
    ``SwitchError``, but that name already belongs to the switch-operation
    failure below, which is raised, asserted and shown by class name in the
    JSON error envelope, so the base has its own name.
    """

    pass


class ConfirmationRequiredError(ProviderError):
    """A command needs a yes from the user and there is no terminal to ask on.

    Nothing was changed. The message names the flag that confirms it
    (``--yes``), so a script can add it (docs/design/ux.md: a prompt is only
    shown on a terminal).
    """

    pass


class ProviderUnavailableError(ProviderError):
    """The selected provider has no implementation in this version."""

    pass


class CredentialFormatError(ProviderError):
    """A stored credential is not a login in the provider's format."""

    pass


class FeatureUnavailableError(ProviderError):
    """The provider implements this feature in a later version, not this one."""

    pass


class CodexLoginError(ProviderError):
    """Codex's live login cannot be read or managed.

    Missing or unreadable ``auth.json``, a login kept in the OS keyring, or a
    login that is not a ChatGPT one (an API key). Nothing was changed.
    """

    pass


class ClaudeSwitchError(ProviderError):
    """Base for the errors of the Claude Code provider and of the shared code
    (settings, locks, transfers). The name is older than Codex support."""

    pass


class CredentialError(ClaudeSwitchError):
    """Error related to credential operations."""

    pass


class CredentialReadError(CredentialError):
    """Failed to read credentials."""

    pass


class CredentialWriteError(CredentialError):
    """Failed to write credentials."""

    pass


class ConfigError(ClaudeSwitchError):
    """Error related to configuration operations."""

    pass


class SwitchError(ClaudeSwitchError):
    """Error during account switch operation."""

    pass


class RetryableSwitchError(SwitchError):
    """A switch refused for now: nothing was changed and retrying later is safe."""

    pass


class SessionsRunningError(SwitchError):
    """A switch to an API-key account refused because Claude Code sessions
    are running on the shared login (docs/design/api-key-sessions.md).

    Nothing was changed. ``sessions`` is how many were running (readable
    session records); ``unreadable`` how many records could not be read, which
    refuses too ("could not tell" is never "nobody there"), and ``records``
    their paths. ``from_key``: the active account is an API key as well, so
    the switch logs no one out. The JSON error envelope carries
    ``error.sessions``; ``error.unreadable`` and ``error.unreadableRecords``
    when records could not be read; ``error.fromKey`` when ``from_key``.
    """

    def __init__(
        self, message: str, *, number: str, sessions: int, unreadable: int = 0,
        from_key: bool = False, records: tuple[str, ...] | list[str] = (),
    ):
        super().__init__(message)
        self.number = str(number)
        self.sessions = sessions
        self.unreadable = unreadable
        self.from_key = from_key
        self.records = tuple(str(r) for r in records)

    @property
    def envelope_fields(self) -> dict:
        fields: dict = {"sessions": self.sessions}
        if self.unreadable:
            fields["unreadable"] = self.unreadable
        if self.records:
            fields["unreadableRecords"] = list(self.records)
        if self.from_key:
            fields["fromKey"] = True
        return fields


class SessionError(ClaudeSwitchError):
    """Error setting up or launching a session-mode profile."""

    pass


class LockError(ClaudeSwitchError):
    """Error acquiring lock."""

    pass


class ClaudeCodeLockTimeout(LockError):
    """Timed out acquiring one of Claude Code's own advisory locks.

    Raised when ``~/.claude.lock`` / ``~/.claude.json.lock`` stays held past
    our bounded wait — usually Claude Code mid-token-refresh. Nothing has been
    mutated when this raises; the operation is safe to retry.
    """

    pass


class AccountNotFoundError(ClaudeSwitchError):
    """Account not found."""

    pass


class ValidationError(ClaudeSwitchError):
    """Validation error."""

    pass


class TransferError(ClaudeSwitchError):
    """Error during account export or import."""

    pass


class MigrationError(ClaudeSwitchError):
    """Error migrating the backup directory between layouts (e.g. legacy → XDG)."""

    pass


class MigrationIncomplete(ClaudeSwitchError):
    """A one-time data migration could not finish for every record.

    Raised by run-once migrations (see ``migrations.py``) when some entries
    failed or the source backend was inaccessible. The migration runner treats
    this as "not applied" so the migration is retried on the next run rather
    than being recorded as done with records left behind.
    """

    pass
