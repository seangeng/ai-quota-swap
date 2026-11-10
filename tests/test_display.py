"""display.py: the shared state phrases and the account block (docs/design/ux.md)."""

from __future__ import annotations

import pytest

from ai_quota_swap import display
from ai_quota_swap.display import AccountBlock, Lines, Reading, WindowRow
from ai_quota_swap.json_output import (
    USAGE_API_KEY,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)

NOW = 1_800_000_000.0


def _fresh(window_key="five_hour", pct=46.0):
    return {window_key: {"pct": pct, "clock": "16:55", "countdown": "2h 35m"}}


def _strings(window):
    return (window["countdown"], window["clock"]) if "clock" in window else None


def _no_pace(window, fetched_at):
    return False


def _block(entry, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("stale_after_s", 180.0)
    kw.setdefault("reset_strings", _strings)
    kw.setdefault("ahead_of_pace", _no_pace)
    return display.block_from_entry(1, entry, email="maya@example.com", **kw)


class TestPhrases:
    def test_nothing_managed_names_the_next_step(self):
        assert display.nothing_managed_lines("claude", live_email="maya@example.com") == [
            "No accounts are managed yet.",
            "Claude Code is logged in as maya@example.com. Add it with: qswap add",
        ]
        codex = display.nothing_managed_lines("codex", live_email=None, sentence=False)
        assert codex == ["Codex is not logged in. Run codex login, then: qswap codex add"]
        missing = display.nothing_managed_lines("claude", cli_found=False)[1]
        assert missing == (
            "Claude Code was not found on PATH. Install it and log in, then run: qswap add"
        )

    def test_usage_unavailable(self):
        assert display.usage_unavailable("http-503") == "usage unavailable (HTTP 503), retrying"
        assert display.usage_unavailable("timeout", retry_in_s=240) == (
            "usage unavailable (timed out), retrying in 4m"
        )
        assert display.usage_unavailable(None, retrying=False) == "usage unavailable"

    @pytest.mark.parametrize("seconds, text", [
        (5, "just now"), (720, "12m"), (3 * 3600 + 300, "3h 5m"), (2 * 86400 + 4 * 3600, "2d 4h"),
    ])
    def test_one_age_format(self, seconds, text):
        assert display.age_text(seconds) == text

    def test_as_of(self):
        assert display.as_of(3 * 3600 + 300) == "as of 3h 5m ago"
        assert display.as_of(None) == ""

    def test_held_resets(self):
        assert display.held_resets([("Fable", 1)]) == "1 Fable reset"
        assert display.held_resets([("weekly", 2)]) == "2 weekly resets"
        assert display.held_resets([("Fable", 1), ("weekly", 1)]) == (
            "1 Fable reset and 1 weekly reset"
        )
        assert display.held_resets([("full", 1)], "codex") == "1 reset credit"

    def test_one_wording_for_a_reset_that_needs_a_limit(self):
        # list and reset used to say "not usable now" and "needs a limit hit first".
        assert display.grant_state("needs a limit hit first") == "usable once a limit is reached"
        assert display.grant_state(None) == "usable now"

    def test_relogin_step_per_provider(self):
        assert display.relogin_step("claude").endswith("then run: qswap add")
        assert display.relogin_step("codex").endswith("qswap codex login")

    def test_error_labels(self):
        assert display.error_label("http-503") == "HTTP 503"
        assert display.error_label("timeout") == "timed out"
        assert display.error_label("invalid_grant") == "refresh refused"


class TestSeverity:
    @pytest.mark.parametrize("pct, line, level", [
        (50, 90, "ok"), (90, 90, "warn"), (99.9, 90, "warn"), (100, 90, "limit"),
        (95, None, "ok"), (None, 90, "ok"),
    ])
    def test_levels(self, pct, line, level):
        assert display.severity(pct, line) == level

    def test_a_limit_says_so_in_words(self):
        rows = display.window_rows([WindowRow("5h", 100, "15:32", "1h 12m", line=90)])
        assert rows[0].endswith("limit reached")


class TestBlock:
    def test_held_resets_never_read_as_a_window(self):
        block = AccountBlock(
            number=1, email="maya@example.com", alias="personal", active=True, plan="Max 20x",
            windows=(WindowRow("5h", 46, "16:55", "2h 35m"), WindowRow("Fable", 52, "Nov 13 20:20", "3d 6h")),
            held="Holds 1 Fable reset · use by Dec 28",
        )
        lines = display.render_block(block)
        assert lines[0] == "  #1 personal (maya@example.com) · active · Max 20x"
        assert lines[1] == "     ├ 5h      46%  resets 16:55 · in 2h 35m"
        assert lines[2] == "     └ Fable   52%  resets Nov 13 20:20 · in 3d 6h"
        assert lines[3] == "     Holds 1 Fable reset · use by Dec 28"
        assert "resets:" not in "\n".join(lines)

    def test_extra_usage_is_labelled_extra(self):
        usage = {
            "spend": {"used": 18.4, "limit": 100.0, "pct": 18.4, "clock": "Nov 30 14:20",
                      "countdown": "20d 0h"},
            **_fresh(),
        }
        block = _block(Reading(last_good=usage, fetched_at=NOW, age_s=0))
        text = "\n".join(display.render_block(block))
        assert "extra   18%" in text and "$18.40 of $100.00" in text and "$$" not in text

    def test_needs_relogin_from_the_sentinel_or_the_engine(self):
        dead = _block(Reading(sentinel=USAGE_RELOGIN_REQUIRED))
        assert dead.state == "needs-relogin"
        assert display.state_lines(dead)[0].startswith("     needs re-login: log in to it")
        held = _block(Reading(last_good=_fresh(), fetched_at=NOW, age_s=0), set_aside="invalid_grant")
        lines = display.render_block(held)
        assert "needs re-login (its saved login was refused)" in lines[1]
        # The readable numbers are still shown, after the state.
        assert any("5h" in line for line in lines[2:])

    def test_unavailable_and_not_read(self):
        failing = _block(Reading(last_error="http-503", fetched_at=None))
        assert display.render_block(failing)[1] == "     usage unavailable (HTTP 503), retrying"
        assert display.render_block(_block(None))[1] == "     usage not read yet"

    def test_stale_reading_says_as_of(self):
        block = _block(Reading(last_good=_fresh(), fetched_at=NOW - 11100, age_s=11100,
                               last_error="timeout"))
        assert display.render_block(block)[-1] == (
            "     as of 3h 5m ago · usage unavailable (timed out), retrying"
        )

    def test_api_key_row_reads_like_its_neighbours(self):
        block = _block(Reading(sentinel=USAGE_API_KEY), kind="api_key", alias="ci")
        lines = display.render_block(block)
        assert lines[0] == "  #1 ci (maya@example.com) · API key"
        assert lines[1] == "     no quota; billed per token"

    def test_a_provider_note(self):
        block = _block(Reading(sentinel=USAGE_TOKEN_EXPIRED),
                       sentinel_notes={USAGE_TOKEN_EXPIRED: "token expired: it refreshes"})
        assert display.render_block(block)[1] == "     token expired: it refreshes"

    def test_switch_line_colours_from_the_account_line(self):
        block = _block(Reading(last_good=_fresh(pct=93), fetched_at=NOW, age_s=0),
                       lines=Lines(five_hour=90))
        assert block.windows[0].line == 90
        assert display.severity(block.windows[0].pct, block.windows[0].line) == "warn"

    def test_both_providers_in_one_view(self):
        block = AccountBlock(number=2, email="m@example.com", alias="work",
                             provider="codex", show_provider=True)
        assert display.header_line(block).startswith("  Codex #2 work")

    def test_every_line_fits_80_columns(self):
        block = AccountBlock(
            number=12, email="someone.with.a.very.long.address@subdomain.example-company.com",
            alias="a-rather-long-alias", active=True, disabled=True, plan="Max 20x",
            org="Northwind Laboratories International", state="needs-relogin",
            set_aside="its saved login was refused",
        )
        for line in display.render_block(block):
            assert len(line) <= 80, line
        assert "…" in display.render_block(block)[0]
