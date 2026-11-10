"""How an account is named in messages, log lines and notifications.

One spelling everywhere (docs/design/ux.md): ``#2 work`` when the account has an
alias, ``#2 maya@example.com`` when it does not. The long form adds the email
after an alias, for a first mention where there is room. A view that shows more
than one provider prefixes the provider's short name.
"""

from __future__ import annotations

PROVIDER_SHORT = {"claude": "Claude", "codex": "Codex"}


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
    ``"codex"``); pass it only where both providers appear together.
    """
    name = alias or email or ""
    parts: list[str] = []
    if provider:
        parts.append(PROVIDER_SHORT.get(provider, provider))
    if number is not None and str(number) != "":
        parts.append(f"#{number}")
    if name:
        parts.append(name)
    label = " ".join(parts) or "account"
    if long and alias and email:
        label += f" ({email})"
    return label
