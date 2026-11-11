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


def test_a_placeholder_is_a_name_not_an_address():
    # qswap add-token without an email stores api-key-N@token.local.
    assert account_label(5, email="api-key-5@token.local") == "#5 api-key-5"
    assert account_label(3, email="setup-token-3@token.local", long=True) == "#3 setup-token-3"
    # The long form never repeats a placeholder in parentheses.
    assert account_label(5, alias="ci", email="api-key-5@token.local", long=True) == "#5 ci"
    assert account_label(5, email="api-key-5@token.local", provider="claude") == "Claude #5 api-key-5"
    # Real addresses are untouched, including ones that merely look similar.
    assert account_label(2, email="ops@token.local.example.com") == "#2 ops@token.local.example.com"
    assert account_label(2, alias="w", email="m@example.com", long=True) == "#2 w (m@example.com)"


def test_shown_email_and_is_placeholder():
    from ai_quota_swap.labels import is_placeholder, shown_email

    assert shown_email("api-key-5@token.local") == "api-key-5"
    assert shown_email("maya@example.com") == "maya@example.com"
    assert shown_email("") == "" and shown_email(None) == ""
    assert is_placeholder("setup-token-12@token.local")
    assert not is_placeholder("@token.local") and not is_placeholder("maya@example.com")
