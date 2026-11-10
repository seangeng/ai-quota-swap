"""The Codex provider: OpenAI Codex CLI logins (docs/design/codex.md).

- :mod:`.auth`: Codex's ``auth.json``: locate, read, decode, lock, replace.
- :mod:`.usage`: the ``/wham/usage`` read and its normalization.
- :mod:`.processes`: which Codex processes hold a login, and the
  background-server restart.
- :mod:`.switcher`: :class:`CodexSwitcher`, the provider's switcher object.

Nothing here refreshes a Codex login or signs one out: Codex rotates its own
tokens, and ``codex logout`` revokes a login at OpenAI for every copy of it.
"""
