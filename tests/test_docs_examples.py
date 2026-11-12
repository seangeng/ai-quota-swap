"""The documents quote what qswap prints about API keys, and the reinstall
command for macOS 26; these pin the quotes to the code that prints them, so a
change to either shows up here.

Each check builds the text the way the command does (the shared phrases in
``display`` and ``autolog``, with the documents' example accounts) and looks
for it in the page that quotes it. Whitespace is compared loosely only where
a page quotes a wrapped block inline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai_quota_swap import api_spend, autolog, display
from ai_quota_swap.autoswitch import NoSwitchEvent

REPO = Path(__file__).resolve().parents[1]
SETTINGS = "~/.claude/settings.json"


def _page(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Runs of whitespace as one space: a wrapped block quoted inline."""
    return re.sub(r"\s+", " ", text).strip()


def _switch_to_key() -> str:
    """``qswap switch 5`` from #1 personal, as api-accounts.md and the README show it."""
    return "\n".join([
        "Switched #1 personal → #5 ci (API key)",
        *display.key_switch_followup(
            target="#5 ci", underneath="#1 personal", back="qswap switch 1",
        ),
    ])


@pytest.mark.parametrize("page", ["docs/api-accounts.md", "README.md"])
def test_the_switch_to_a_key(page):
    assert f"$ qswap switch 5\n{_switch_to_key()}\n```" in _page(page)


def test_the_menu_bar_notification_after_a_switch_to_a_key():
    followup = " ".join(display.key_switch_followup(
        target="#5 ci", underneath="#1 personal", back="qswap switch 1",
    ))
    assert f"`Switched #1 personal → #5 ci. {followup}`" in _page("docs/menubar.md")


def test_the_refusal_for_another_helper():
    text = display.key_helper_foreign_refusal("#5 ci", 5, SETTINGS)
    assert f"$ qswap switch 5\nError: {text}\n```" in _page("docs/api-accounts.md")


def test_the_refusal_for_a_credential_in_the_env_block():
    text = display.key_line_defeated_refusal("#5 ci", 5, SETTINGS, ["ANTHROPIC_API_KEY"])
    assert f"$ qswap switch 5\nError: {text}\n```" in _page("docs/api-accounts.md")


def test_the_warning_for_another_base_url():
    text = display.key_sent_elsewhere(SETTINGS, "llm-gateway.example.com", "#5 ci")
    assert f"`{text}`" in _page("docs/api-accounts.md")


def test_the_refusal_for_a_settings_file_that_cannot_be_parsed():
    reason = (
        f"{SETTINGS} is not valid JSON (line 2: Illegal trailing comma before end of object)"
    )
    text = display.key_helper_refusal(
        "#5 ci", 5, reason, 0, fix=f"Fix the JSON in {SETTINGS}",
    )
    assert f"$ qswap switch 5\nError: {text}\n```" in _page("docs/api-accounts.md")


def test_the_settings_unreadable_sentence():
    lines = display.key_settings_unreadable(
        SETTINGS, "line 1: Illegal trailing comma before end of object", "#5 ci",
    )
    quoted = _flat(_page("docs/troubleshooting.md"))
    assert _flat("Error: " + " ".join(lines)) in quoted
    # The shared sentence, as the menu bar's notification table quotes it.
    general = " ".join(display.key_settings_unreadable(SETTINGS, "...", "#5 ci"))
    assert f"`{general}`" in _page("docs/menubar.md")


def test_the_broken_line_says_running_sessions_keep_their_key():
    assert display.BROKEN_LINE_RUNNING in _flat(_page("docs/troubleshooting.md"))
    assert display.BROKEN_LINE_RUNNING in _flat(_page("docs/api-accounts.md"))


def test_the_replaced_key_sentence():
    assert f"`{display.KEY_REFRESH}`" in _page("docs/api-accounts.md")
    assert display.KEY_REFRESH in _page("docs/coming-from-claude-swap.md")


def test_the_add_under_a_key_sentence():
    text = " ".join(display.key_still_active("#5 ci", 8))
    assert f"`{text}`" in _page("docs/api-accounts.md")


def _no_switch(reason: str, **fields) -> NoSwitchEvent:
    return NoSwitchEvent(reason=reason, **fields)


def test_the_key_line_cannot_be_added_words():
    event = _no_switch("key-helper-unavailable", account="5")
    fragment, _tone = autolog.no_switch_fragment(event)
    assert f"`{fragment}`" in _page("docs/auto-switch.md")
    sentence = event.human().replace("#5", "#5 ci", 1)
    for page in ("docs/auto-switch.md", "docs/menubar.md"):
        assert f"`{sentence}`" in _page(page)


def test_the_no_limit_and_spend_unknown_words():
    no_limit = _no_switch("key-no-limit", account="5").human().replace("#5", "#5 ci", 1)
    unknown = _no_switch("spend-unknown", account="5").human().replace("#5", "#5 ci", 1)
    for page in ("docs/auto-switch.md", "docs/menubar.md"):
        assert f"`{no_limit}`" in _page(page)
        assert f"`{unknown}`" in _page(page)
    assert api_spend.SPEND_UNKNOWN_WORDS in _page("docs/api-accounts.md")


def test_the_stuck_on_key_words():
    event = _no_switch("stuck-on-key", account="5", limit_reason="budget",
                       way_off="1")
    sentence = event.human().replace("#5", "#5 ci", 1)
    for page in ("docs/auto-switch.md", "docs/menubar.md"):
        assert f"`{sentence}`" in _page(page)


def test_the_on_key_fragments():
    chose, _ = autolog.no_switch_fragment(_no_switch("active-api-key", detail="you chose it"))
    room, _ = autolog.no_switch_fragment(_no_switch("no-subscription-room"))
    page = _page("docs/auto-switch.md")
    assert f"`#5 ci is an API key · {chose}`" in page
    assert f"`{room}`" in page


def test_the_limit_not_enforced_words():
    for page in ("docs/api-accounts.md", "docs/dashboard.md"):
        assert api_spend.LIMIT_NOT_ENFORCED in _page(page)
    assert api_spend.LIMIT_NOT_ENFORCED_SHORT in _page("docs/menubar.md")


def test_the_unwatched_key_words(tmp_path):
    from types import SimpleNamespace

    from ai_quota_swap.autoswitch import unwatched_key_notice

    # A stopping engine on #5 ci, which it entered over #1 personal; no
    # other engine's heartbeat in the data directory.
    switcher = SimpleNamespace(
        helper_account=lambda: "5", key_entered_by=lambda: "engine",
        underneath_account_number=lambda: "1", key_way_back=lambda: "qswap switch 1",
        backup_dir=tmp_path, label=lambda number, long=False: f"#{number} ci",
    )
    notice = unwatched_key_notice(switcher, host="qswap auto")
    for page in ("docs/auto-switch.md", "docs/menubar.md"):
        assert f"`{notice}`" in _page(page)
    shown = " ".join(display.key_unwatched("#5 ci", "qswap switch 1"))
    assert shown.startswith("Auto-switch entered #5 ci and is not running, so nothing watches its li")
    assert "Auto-switch entered #5 ci and is not running, so nothing watches its li…" in (
        _page("docs/menubar.md")
    )


@pytest.mark.parametrize("page", ["README.md", "docs/menubar.md"])
def test_the_macos_26_reinstall_command(page):
    """The command ``qswap menubar`` prints, with its ``--force``: without it,
    uv keeps an existing install on the old Python and says it is already
    installed."""
    from ai_quota_swap import menubar

    text = menubar.framework_build_warning(
        framework="Python", install_method="uv", mac_ver="26.1"
    )
    command = text.splitlines()[-1].strip()
    assert "--force" in command
    assert f"`{command}`" in _page(page)


def test_the_connectors_sentence_is_quoted_where_the_switch_is_described():
    for page in ("docs/api-accounts.md", "README.md", "docs/design/api-key-sessions.md"):
        assert "Remote Control" in _page(page)
