"""One spelling for an account everywhere (docs/design/ux.md)."""

from ai_quota_swap.labels import account_label


def test_alias_wins_and_email_is_the_fallback():
    assert account_label(2, alias="work", email="maya@example.com") == "#2 work"
    assert account_label(2, email="maya@example.com") == "#2 maya@example.com"
    assert account_label("2", alias="work") == "#2 work"


def test_long_form_adds_the_email_after_an_alias_only():
    assert account_label(2, alias="work", email="m@example.com", long=True) == "#2 work (m@example.com)"
    assert account_label(2, email="m@example.com", long=True) == "#2 m@example.com"


def test_provider_prefix_for_mixed_views():
    assert account_label(2, alias="work", provider="codex") == "Codex #2 work"
    assert account_label(1, email="a@example.com", provider="claude") == "Claude #1 a@example.com"
    assert account_label(1, alias="x", provider="other") == "other #1 x"


def test_unmanaged_and_empty():
    assert account_label(None, email="a@example.com") == "a@example.com"
    assert account_label(None) == "account"
    assert account_label(3) == "#3"
