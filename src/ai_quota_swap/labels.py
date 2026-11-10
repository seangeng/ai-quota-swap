"""How an account is named in messages, log lines and notifications.

One spelling everywhere (docs/design/ux.md): ``#2 work`` when the account has an
alias, ``#2 maya@example.com`` when it does not. The long form adds the email
after an alias, for a first mention where there is room. A view that shows more
than one provider prefixes the provider's short name.

An account added from a token with no email (``qswap add-token``) is stored
under a placeholder address, ``api-key-5@token.local`` or
``setup-token-3@token.local``. It is a name, not an address, so a label shows
it without its domain (``#5 api-key-5``) and never in parentheses. The stored
value (and every JSON payload) keeps the full placeholder.
"""

from __future__ import annotations

PROVIDER_SHORT = {"claude": "Claude", "codex": "Codex"}

#: The domain of the placeholder address ``qswap add-token`` gives an account
#: added without an email (switcher.add_account_from_token).
PLACEHOLDER_DOMAIN = "@token.local"


def is_placeholder(email: str | None) -> bool:
    """Whether ``email`` is a placeholder (``api-key-5@token.local``), not an address."""
    if not email:
        return False
    return email.lower().endswith(PLACEHOLDER_DOMAIN) and len(email) > len(PLACEHOLDER_DOMAIN)


def shown_email(email: str | None) -> str:
    """``email`` as a label shows it: a placeholder without its domain
    (``api-key-5``), a real address unchanged."""
    if not email:
        return ""
    return email[: -len(PLACEHOLDER_DOMAIN)] if is_placeholder(email) else email


def account_label(
    number: int | str | None,
    *,
    alias: str | None = None,
    email: str = "",
    provider: str | None = None,
    long: bool = False,
) -> str:
    """``#2 work`` / ``#2 maya@example.com``; ``long`` gives ``#2 work (maya@example.com)``.

    ``number`` may be None for a login that is not managed: the label is then
    the alias or the email alone. ``provider`` is a provider id (``"claude"``,
    ``"codex"``); pass it only where both providers appear together. A
    placeholder email reads as its name (``#5 api-key-5``) and the long form
    leaves it out.
    """
    name = alias or shown_email(email)
    parts: list[str] = []
    if provider:
        parts.append(PROVIDER_SHORT.get(provider, provider))
    if number is not None and str(number) != "":
        parts.append(f"#{number}")
    if name:
        parts.append(name)
    label = " ".join(parts) or "account"
    if long and alias and email and not is_placeholder(email):
        label += f" ({email})"
    return label
