"""Tests for the menu bar.

None of these import or run rumps or AppKit (neither is installed on the
Linux and Windows CI runners, nor in the dev environment): they exercise the
pure layer (settings, the menu description, rows, title, health row,
dialogs), the notification texts and their de-duplication, the engine
supervisor with fake engines, the outside-change watcher with fake stats,
and the background reads against temporary files.
"""

from __future__ import annotations

import datetime as _dt
import json
import plistlib
import sqlite3
import sys
import time
import threading
from pathlib import Path

import pytest

from ai_quota_swap import menubar, menubar_data, menubar_engines, menubar_notify, oauth
from ai_quota_swap.autoswitch import (
    AllExhaustedEvent,
    BudgetWarningEvent,
    ConfigWarningEvent,
    ErrorEvent,
    EventContext,
    PollEvent,
    QuarantineEvent,
    ResetEvent,
    SwitchEvent,
)
from ai_quota_swap.display import Lines, Reading
from ai_quota_swap.exceptions import (
    ClaudeSwitchError,
    CodexLoginError,
    ConfigError,
    SwitchError,
)
from ai_quota_swap.lines import BindingWindow
from ai_quota_swap.menubar import (
    ENGINE_ERROR_REPEAT_S,
    AccountState,
    Action,
    CostSummary,
    EngineState,
    MenuBarSettings,
    MenuInput,
    ProviderState,
    SwitchRecord,
    should_surface_engine_error,
)
from ai_quota_swap.switcher import USAGE_API_KEY, USAGE_RELOGIN_REQUIRED
from tests.test_reset_kinds import fable, session, weekly
from tests.test_resets import _usage as _reset_usage

_NOW = 1_000_000.0
_STALE = 900.0


def _iso(delta_s):  # ISO-8601 for _NOW + delta_s, UTC
    return _dt.datetime.fromtimestamp(_NOW + delta_s, _dt.timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _no_engine_threads_left():
    """No test leaves an engine thread running."""
    yield
    alive = [t.name for t in threading.enumerate() if t.name.startswith("qswap-") and t.is_alive()]
    assert not alive, f"engine threads still running: {alive}"


# --- notification identity -----------------------------------------------------

def test_notification_identity_creates_and_preserves_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    info.write_bytes(plistlib.dumps({"ExistingKey": "kept"}))

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.ai-quota-swap.menubar"
    assert data["CFBundleName"] == "AI Quota Swap"
    assert data["ExistingKey"] == "kept"


def test_notification_identity_heals_corrupt_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    # truncated XML plist: plistlib raises ExpatError, not InvalidFileException
    info.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<plist version="1.0"><dict><key>CFBundle'
    )

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.ai-quota-swap.menubar"
    assert not (executable.parent / "Info.plist.tmp").exists()


def test_notification_identity_is_noop_off_macos(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    assert menubar.ensure_notification_identity(executable, platform="linux") is None
    assert not (executable.parent / "Info.plist").exists()


# --- settings ------------------------------------------------------------------

def test_settings_defaults_when_file_missing(tmp_path: Path):
    s = MenuBarSettings.load(tmp_path / "nope.json")
    assert s.show_account_name is True
    assert s.title_pct == "both"
    assert s.refresh_interval == 60
    assert s.auto_switch_enabled is False
    assert s.codex_auto_switch_enabled is False
    assert s.title_follows == "claude"


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = MenuBarSettings(
        show_account_name=False, title_pct="5h", refresh_interval=300,
        auto_switch_enabled=True, codex_auto_switch_enabled=True, title_follows="closest",
    )
    original.save(path)
    assert MenuBarSettings.load(path) == original
    assert json.loads(path.read_text())["codex_auto_switch_enabled"] is True


def test_an_old_file_loads_as_it_did(tmp_path: Path):
    # Written by a build before Codex: Claude Code's auto-switch as it was,
    # Codex off, the title on Claude Code.
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({
        "show_account_name": True, "title_pct": "5h", "title_scoped": False,
        "refresh_interval": 30, "auto_switch_enabled": True,
    }))
    s = MenuBarSettings.load(path)
    assert s.auto_switch_enabled is True
    assert s.codex_auto_switch_enabled is False
    assert s.title_follows == "claude"
    assert s.title_pct == "5h" and s.refresh_interval == 30


def test_saving_keeps_keys_this_build_does_not_know(tmp_path: Path):
    # The CLI and the menu bar both write the file; a newer build's key must
    # survive an older writer (this build) toggling something.
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({"auto_switch_enabled": True, "some_future_key": {"x": 1}}))
    s = MenuBarSettings.load(path)
    s.codex_auto_switch_enabled = True
    s.save(path)
    raw = json.loads(path.read_text())
    assert raw["some_future_key"] == {"x": 1}
    assert raw["auto_switch_enabled"] is True and raw["codex_auto_switch_enabled"] is True
    assert MenuBarSettings.load(path).extra == {"some_future_key": {"x": 1}}


def test_settings_save_is_atomic(tmp_path: Path, monkeypatch):
    # load() reads a torn write as defaults (auto-switch off), so save() must
    # not truncate in place.
    calls = []
    monkeypatch.setattr(
        menubar, "atomic_write_text", lambda path, text, **kw: calls.append((path, kw))
    )
    path = tmp_path / "menubar_settings.json"
    MenuBarSettings(auto_switch_enabled=True).save(path)
    assert calls == [(path, {"mode": 0o600})]


def test_settings_corrupt_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert MenuBarSettings.load(path) == MenuBarSettings()


def test_settings_ignores_bad_types_and_unknown_choices(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({
        "refresh_interval": "fast", "show_account_name": False, "title_follows": "gemini",
        "title_pct": "always", "codex_auto_switch_enabled": "yes", "auto_switch_enabled": 1,
    }))
    s = MenuBarSettings.load(path)
    assert s.refresh_interval == 60
    assert s.show_account_name is False
    assert s.title_follows == "claude" and s.title_pct == "both"
    assert s.codex_auto_switch_enabled is False and s.auto_switch_enabled is False


def test_settings_a_bool_is_not_an_interval(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({"refresh_interval": True}))
    assert MenuBarSettings.load(path).refresh_interval == 60


def test_settings_toggle_per_provider():
    s = MenuBarSettings()
    s.set_auto_enabled("codex", True)
    assert s.auto_enabled("codex") and not s.auto_enabled("claude")
    s.set_auto_enabled("claude", True)
    assert s.auto_switch_enabled is True


# --- usage summary (one reading as row segments) -----------------------------------

_USAGE = {
    "five_hour": {"pct": 42.0},
    "seven_day": {"pct": 18.0},
    "spend": {"pct": 30.0, "used": 3.0, "limit": 10.0},
}


def test_tightest_pct_uses_max_window():
    assert menubar.tightest_pct(_USAGE) == 42.0
    assert menubar.tightest_pct("no credentials") is None
    assert menubar.tightest_pct({"spend": {"pct": 90.0}}) is None


def test_usage_summary_says_extra_for_extra_usage():
    assert menubar.usage_summary(_USAGE, _NOW) == "5h 42% · 7d 18% · extra 30%"


def test_usage_summary_partial_windows():
    assert menubar.usage_summary({"five_hour": {"pct": 5.0}}, _NOW) == "5h 5%"


def test_usage_summary_includes_scoped_model_limits():
    usage = {
        "five_hour": {"pct": 82.0},
        "seven_day": {"pct": 12.0},
        "scoped": [{"name": "Fable", "pct": 4.0}],
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 82% · 7d 12% · Fable 4% · extra 30%"


def test_usage_summary_leads_with_a_refused_probe_as_limit_reached():
    usage = {
        "five_hour": {"pct": 20.0},
        "seven_day": {"pct": 30.0},
        "rejected": {"retry_after_s": 3600.0, "resets_at": _iso(3600)},
    }
    assert menubar.usage_summary(usage, _NOW) == "limit reached (resets in 1h 0m) · 5h 20% · 7d 30%"


def test_usage_summary_window_at_its_limit_says_when_it_resets():
    assert menubar.usage_summary({"scoped": [{"name": "Fable", "pct": 100.0}]}, _NOW) == "Fable 100%"
    assert menubar.usage_summary({"five_hour": {"pct": 100.0, "resets_at": _iso(600)}}, _NOW) == (
        "5h 100% (resets in 10m)"
    )


def test_usage_summary_never_rounds_up_to_the_limit():
    assert menubar.usage_summary({"five_hour": {"pct": 99.6}}, _NOW) == "5h 99%"


def test_usage_summary_bare_percentages_below_the_line():
    usage = {"scoped": [
        {"name": "Fable", "pct": 4.0, "resets_at": _iso(2 * 3600)},
        {"name": "Opus", "pct": 55.0},
    ]}
    assert menubar.usage_summary(usage, _NOW, Lines(five_hour=90.0)) == "Fable 4% · Opus 55%"


def test_usage_summary_no_countdown_below_the_limit():
    # Past the switch line the account still works: the percentage is enough.
    usage = {
        "five_hour": {"pct": 93.0, "resets_at": _iso(38 * 60)},
        "seven_day": {"pct": 81.0, "resets_at": _iso(86400 + 19 * 3600)},
        "scoped": [{"name": "Fable", "pct": 97.0, "resets_at": _iso(86400 + 19 * 3600)}],
    }
    assert menubar.usage_summary(usage, _NOW, Lines(five_hour=90.0)) == "5h 93% · 7d 81% · Fable 97%"


def test_usage_summary_one_countdown_on_the_window_that_resets_last():
    # Blocked on two windows: usable again when the later one resets.
    usage = {
        "five_hour": {"pct": 100.0, "resets_at": _iso(38 * 60)},
        "seven_day": {"pct": 81.0, "resets_at": _iso(86400 + 19 * 3600)},
        "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(86400 + 19 * 3600)}],
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 100% · 7d 81% · Fable 100% (resets in 1d 19h)"
    # A refused probe is at the limit too, and can be the one that lasts.
    usage = {"five_hour": {"pct": 100.0, "resets_at": _iso(600)},
             "rejected": {"resets_at": _iso(3600)}}
    assert menubar.usage_summary(usage, _NOW) == "limit reached (resets in 1h 0m) · 5h 100%"
    # A window at the limit with no reset time known: no countdown at all.
    assert menubar.usage_summary({"seven_day": {"pct": 100.0}}, _NOW) == "7d 100%"


def test_usage_summary_never_says_ahead_of_pace():
    # Pace is in the tooltip (the list block), not the row.
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    assert menubar.usage_summary(usage, _NOW) == "7d 50%"


def test_usage_summary_rolls_a_passed_weekly_window_to_zero():
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-3 * 86400)}}
    assert menubar.usage_summary(usage, _NOW, Lines(five_hour=90.0)) == "7d 0%"
    usage = {"scoped": [{"name": "Fable", "pct": 95.0, "resets_at": _iso(-3 * 86400)}]}
    assert menubar.usage_summary(usage, _NOW) == "Fable 0%"


def test_usage_summary_omits_a_countdown_that_passed_or_is_missing():
    usage = {"five_hour": {"pct": 53.0, "resets_at": _iso(-60)}, "seven_day": {"pct": 8.0}}
    assert menubar.usage_summary(usage, _NOW) == "5h 53% · 7d 8%"


def test_usage_summary_with_a_null_five_hour_window():
    usage = oauth.build_usage_result({"five_hour": None, "seven_day": {"utilization": 25.0}})
    assert menubar.usage_summary(usage) == "7d 25%"


# --- weekly roll-forward ----------------------------------------------------------

def test_rolled_weekly_window_advances_passed_reset():
    w = {"pct": 95.0, "resets_at": _iso(-3 * 86400), "countdown": "stale", "clock": "old"}
    rolled = menubar._rolled_weekly_window(w, _NOW)
    assert rolled["pct"] == 0.0
    assert abs(menubar._resets_at_ts(rolled) - (_NOW + 4 * 86400)) < 1
    assert "countdown" not in rolled and "clock" not in rolled


def test_rolled_weekly_window_leaves_future_or_unknown_untouched():
    future = {"pct": 42.0, "resets_at": _iso(2 * 86400)}
    assert menubar._rolled_weekly_window(future, _NOW) is future
    assert menubar._rolled_weekly_window(None, _NOW) is None


def test_resets_at_ts_orders_and_handles_missing():
    early = {"resets_at": "2026-06-24T07:00:00+00:00"}
    late = {"resets_at": "2026-06-26T07:00:00+00:00"}
    assert menubar._resets_at_ts(early) < menubar._resets_at_ts(late)
    assert menubar._resets_at_ts({"pct": 5.0}) == float("inf")
    assert menubar._resets_at_ts({"resets_at": "garbage"}) == float("inf")


def test_live_countdown():
    assert menubar._live_countdown({"resets_at": _iso(9 * 3600 + 5 * 60)}, _NOW) == "9h 5m"
    assert menubar._live_countdown({"resets_at": _iso(-60)}, _NOW) is None
    assert menubar._live_countdown("no credentials", _NOW) is None


# --- account rows -------------------------------------------------------------------

def _acct(number="1", email="maya@example.com", *, usage=None, provider="claude",
          fetched_at=_NOW - 60, **kw) -> AccountState:
    entry = kw.pop("entry", None)
    if entry is None and (usage is not None or fetched_at is not None):
        entry = Reading(last_good=usage, fetched_at=fetched_at if usage is not None else None)
    return AccountState(provider, number, email, entry=entry, **kw)


def _row(acc: AccountState, now: float = _NOW):
    return menubar.account_row(acc, now=now, stale_after_s=_STALE)


_OK = {"five_hour": {"pct": 46.0, "resets_at": _iso(2 * 3600 + 35 * 60)},
       "seven_day": {"pct": 38.0, "resets_at": _iso(3 * 86400 + 6 * 3600)}}


def test_row_names_the_account_with_its_short_label():
    row = _row(_acct(alias="personal", usage=_OK, active=True))
    assert row.text == "#1 personal · 5h 46% · 7d 38%"
    assert row.checked and not row.attention
    assert _row(_acct(usage=_OK)).text == "#1 maya@example.com · 5h 46% · 7d 38%"


def test_row_tooltip_is_the_block_list_prints():
    row = _row(_acct(alias="personal", usage=_OK, active=True, plan="Max 20x"))
    assert row.tooltip.splitlines()[0] == "#1 personal (maya@example.com) · active · Max 20x"
    assert "5h      46%  resets" in row.tooltip
    assert "\x1b[" not in row.tooltip


def test_row_disabled():
    assert _row(_acct(usage=_OK, disabled=True)).text.startswith("#1 maya@example.com · disabled · 5h 46%")


def test_row_needs_relogin_from_the_sentinel():
    acc = _acct(alias="work", entry=Reading(sentinel=USAGE_RELOGIN_REQUIRED))
    row = _row(acc)
    assert row.text == "#1 work · needs re-login"
    assert row.attention
    assert "log in to it in Claude Code (/login), then run: qswap add" in row.tooltip
    assert "quarantine" not in row.text + row.tooltip


def test_row_an_account_the_engine_set_aside_needs_relogin():
    acc = _acct(alias="side", usage=_OK, set_aside="invalid_grant")
    row = _row(acc)
    assert row.text == "#1 side · needs re-login"
    assert row.attention
    assert "needs re-login (its saved login was refused): log in to it" in " ".join(row.tooltip.split())


def test_row_unavailable():
    acc = _acct(entry=Reading(last_error="http-503", fetched_at=None))
    assert _row(acc).text == "#1 maya@example.com · usage unavailable (HTTP 503), retrying"


def test_row_unavailable_with_a_known_backoff():
    acc = _acct(entry=Reading(last_error="http-503", backoff_until=_NOW + 240))
    assert _row(acc).text.endswith("usage unavailable (HTTP 503), retrying in 4m")


def test_row_stale_reading_says_as_of():
    acc = _acct(entry=Reading(last_good=_OK, fetched_at=_NOW - 3 * 3600, last_error="timeout"))
    assert _row(acc).text == "#1 maya@example.com · 5h 46% · 7d 38% · as of 3h 0m ago (timed out)"


def test_row_a_quiet_reading_is_not_called_old():
    # The engine polls an alternate every 5 to 10 minutes; that is not stale.
    acc = _acct(entry=Reading(last_good=_OK, fetched_at=_NOW - 10 * 60))
    assert "as of" not in _row(acc).text


def test_row_at_its_limit_needs_attention():
    usage = {"five_hour": {"pct": 100.0, "resets_at": _iso(72 * 60)}, "seven_day": {"pct": 64.0}}
    row = _row(_acct(usage=usage))
    assert row.text == "#1 maya@example.com · 5h 100% (resets in 1h 12m) · 7d 64%"
    assert row.attention


def test_row_past_its_own_line_is_orange_without_a_countdown():
    usage = {"five_hour": {"pct": 93.0, "resets_at": _iso(38 * 60)}, "seven_day": {"pct": 81.0}}
    row = _row(_acct(alias="night", usage=usage, lines=Lines(five_hour=90.0)))
    assert row.text == "#1 night · 5h 93% · 7d 81%"
    assert row.attention
    # Its own threshold of 95 puts 93% under the line.
    assert not _row(_acct(alias="night", usage=usage, lines=Lines(five_hour=95.0))).attention


def test_row_held_resets_last_and_compact():
    far = _NOW + 120 * 86400
    usage = _reset_usage(grants=[fable("a", ends=far), weekly("w", ends=far)])
    # Several kinds read as one phrase, not as separate facts.
    assert _row(_acct(usage=usage)).text == "#1 maya@example.com · 7d 30% · resets: 1 Fable, 1 weekly"
    usage = _reset_usage(grants=[fable("a", ends=far, left=2)])
    assert _row(_acct(usage=usage)).text.endswith("· 7d 30% · 2 Fable resets")
    usage = _reset_usage(grants=[session("s", ends=far)])
    assert _row(_acct(usage=usage)).text.endswith("· 1 five-hour reset")
    assert "holds" not in _row(_acct(usage=usage)).text


def test_row_codex_reset_credits():
    far = _NOW + 30 * 86400
    usage = _reset_usage(grants=[fable("c", ends=far)])
    row = _row(_acct(provider="codex", usage=usage))
    assert row.text.endswith("· 1 reset credit")


def _api_reading(used=212.40, limit=500.0, today=4.12, source="ledger"):
    pct = used / limit * 100 if limit else None
    # Measured just now: a budget is held only with a fresh figure (A4).
    return {"spend": {"used": used, "limit": limit, "pct": pct}, "today": today,
            "source": source, "as_of": time.time(), "unpriced_tokens": 0}


def test_row_api_key_with_a_budget_is_compact():
    acc = _acct(number="5", email="ci@example.com", alias="ci", kind="api_key",
                entry=Reading(sentinel=USAGE_API_KEY, api_spend=_api_reading()))
    row = _row(acc)
    assert row.text == "#5 ci · API key · $4.12 today · $212.40/$500 (42%) est."
    assert "$212.40 this month of $500 (42%) · estimate" in row.tooltip  # qswap list's words
    assert not row.attention


def test_row_api_key_without_a_budget_as_the_command_line_says():
    acc = _acct(number="5", alias="ci", kind="api_key", entry=Reading(
        sentinel=USAGE_API_KEY, api_spend=_api_reading(limit=None)))
    assert _row(acc).text == "#5 ci · API key · $4.12 today · $212.40 this month · estimate"


def test_row_api_key_over_budget_is_never_cut_where_it_matters():
    acc = _acct(number="7", email="ci@x.com", kind="api_key",
                entry=Reading(sentinel=USAGE_API_KEY, api_spend=_api_reading(used=520.0)))
    row = _row(acc)
    assert row.attention
    # The name gives way first (its number stays); the budget stays whole.
    assert row.text == "#7 ci@x… · API key · $4.12 today · $520.00/$500 (104%, over budget) est."
    # A placeholder name is one word: it stays whole or goes, never "api-…".
    long_name = _acct(number="5", email="api-key-5@token.local", kind="api_key",
                      entry=Reading(sentinel=USAGE_API_KEY, api_spend=_api_reading(used=520.0)))
    text = _row(long_name).text
    assert text == "#5 · API key · $4.12 today · $520.00/$500 (104%, over budget) est."
    assert len(text) <= menubar.ROW_LIMIT


def test_row_api_key_at_its_budget_with_a_credit_never_cuts_the_credit():
    """The re-check's case: the budget reached and a credit with cents. The
    row read "... · $1,187.03 of $1…", at a glance a credit of one dollar.
    The credit gives way whole (the tooltip keeps it)."""
    reading = _api_reading(used=25.22, limit=20.0, today=2.06)
    reading["credit"] = {"amount": 1187.03, "used": 0.0, "left": 1187.03, "spent": False,
                         "expired": False, "since": "2026-11-01T00:00:00Z", "expires": None,
                         "source": "ledger", "unpriced_tokens": 0}
    acc = _acct(number="5", email="ci@example.com", alias="ci", kind="api_key",
                active=True, entry=Reading(sentinel=USAGE_API_KEY, api_spend=reading))
    row = _row(acc)
    assert len(row.text) <= menubar.ROW_LIMIT
    assert "$25.22/$20 (126%, over budget) est." in row.text
    assert row.text.endswith(("credit left", "over budget) est.")), row.text
    assert "$1,187.03 of $1,187.03 credit left" in row.tooltip


def test_row_placeholder_name_is_never_an_address_or_cut_mid_word():
    # The maintainer's first-day menu: three unused keys added without an email.
    for n in ("5", "6", "7"):
        acc = _acct(number=n, email=f"api-key-{n}@token.local", kind="api_key",
                    entry=Reading(sentinel=USAGE_API_KEY))
        row = _row(acc)
        assert row.text == f"#{n} api-key-{n} · API key · no usage recorded"
        assert "token.local" not in row.tooltip
    # After a scan: a zero estimate reads the same; with a budget, $0 of it.
    zero = _api_reading(used=0.0, limit=None, today=0.0)
    acc = _acct(number="5", email="api-key-5@token.local", kind="api_key",
                entry=Reading(sentinel=USAGE_API_KEY, api_spend=zero))
    assert _row(acc).text == "#5 api-key-5 · API key · no usage recorded"
    budget = _api_reading(used=0.0, limit=500.0, today=0.0)
    acc = _acct(number="5", email="api-key-5@token.local", kind="api_key",
                entry=Reading(sentinel=USAGE_API_KEY, api_spend=budget))
    assert _row(acc).text == "#5 api-key-5 · API key · $0 of $500 this month"
    # A setup-token placeholder with held resets keeps its whole name or none.
    far = _NOW + 120 * 86400
    usage = _reset_usage(grants=[fable("a", ends=far, left=3), weekly("w", ends=far)])
    acc = _acct(number="3", email="setup-token-3@token.local", usage=usage)
    assert _row(acc).text == "#3 setup-token-3 · 7d 30% · resets: 3 Fable, 1 weekly"
    assert menubar.fit_row(["#3 setup-token-3", "x" * 60], whole_name=True) == "#3 · " + "x" * 60


def test_row_api_key_without_a_reading():
    acc = _acct(number="5", kind="api_key", entry=Reading(sentinel=USAGE_API_KEY))
    assert _row(acc).text == "#5 maya@example.com · API key · no usage recorded"


def test_row_not_read_yet():
    assert _row(_acct(entry=Reading())).text == "#1 maya@example.com · usage not read yet"
    assert _row(AccountState("claude", "1", "maya@example.com")).text.endswith("usage not read yet")


def test_row_a_provider_note_keeps_the_state_and_tooltips_the_rest():
    acc = _acct(provider="codex", entry=Reading(sentinel="usage unknown until used"))
    row = _row(acc)
    assert row.text == "#1 maya@example.com · usage unknown until used"
    assert "Codex renews the expired token on first use" in row.tooltip


def test_row_login_due_and_past():
    due = _row(_acct(usage=_OK, login_expires_at=_NOW + 5 * 86400 + 12 * 3600))
    assert due.text.endswith("· re-login in 5d 12h")
    past = _row(_acct(usage=_OK, login_expires_at=_NOW - 60))
    assert past.text.endswith("· needs re-login") and past.attention
    later = _row(_acct(usage=_OK, login_expires_at=_NOW + 23 * 86400))
    assert "re-login" not in later.text


_H, _D = 3600, 86400
_FAR = _NOW + 60 * _D
_CLAUDE_LINES = Lines(five_hour=90.0)
_CODEX_LINES = Lines(five_hour=99.0)


def _w(pct, resets_in):
    return {"pct": float(pct), "resets_at": _iso(resets_in)}


# The demo harness's `fleet` and `trouble` scenarios (scripts/demo/scenarios.py),
# as the menu sees them: Claude Code's line at 90%, Codex's at 99%.
_FLEET = [
    _acct("1", "maya@example.com", alias="personal", active=True, lines=_CLAUDE_LINES, usage={
        "five_hour": _w(46, 2 * _H + 35 * 60), "seven_day": _w(38, 3 * _D + 6 * _H),
        "scoped": [{"name": "Fable", **_w(52, 3 * _D + 6 * _H)}],
        "resets": _reset_usage(grants=[fable("f", ends=_FAR)])["resets"]}),
    _acct("2", "maya@northwind.example.com", alias="work", lines=_CLAUDE_LINES, usage={
        "spend": {"pct": 18.0, "used": 18.4, "limit": 100.0, "resets_at": _iso(20 * _D)},
        "five_hour": _w(12, 4 * _H + 10 * 60), "seven_day": _w(21, 5 * _D + 2 * _H)}),
    _acct("3", "ops@example.net", alias="night-shift", lines=_CLAUDE_LINES, usage={
        "five_hour": _w(93, 38 * 60), "seven_day": _w(81, _D + 19 * _H),
        "scoped": [{"name": "Fable", **_w(97, _D + 19 * _H)}],
        "resets": _reset_usage(grants=[session("s", ends=_FAR)])["resets"]}),
    _acct("4", "sam@example.org", alias="side", disabled=True, lines=_CLAUDE_LINES, usage={
        "five_hour": _w(0, 5 * _H), "seven_day": _w(9, 6 * _D + _H)}),
    _acct("5", "api-key-5@token.local", alias="ci", kind="api_key", entry=Reading(
        sentinel=USAGE_API_KEY, api_spend=_api_reading(used=0.0, limit=None, today=0.0))),
    _acct("1", "maya@example.com", provider="codex", active=True, lines=_CODEX_LINES, usage={
        "five_hour": _w(22, 3 * _H + 40 * 60), "seven_day": _w(47, 4 * _D + 3 * _H)}),
    _acct("2", "maya@northwind.example.com", provider="codex", alias="work", lines=_CODEX_LINES, usage={
        "five_hour": _w(8, 4 * _H + 55 * 60), "seven_day": _w(15, 6 * _D + 2 * _H),
        "resets": _reset_usage(grants=[fable("c", ends=_FAR)])["resets"]}),
]
_TROUBLE = [
    _acct("1", "maya@example.com", alias="personal", active=True, lines=_CLAUDE_LINES, usage={
        "five_hour": _w(100, _H + 12 * 60), "seven_day": _w(64, 3 * _D + 6 * _H)}),
    _acct("2", "maya@northwind.example.com", alias="work", entry=Reading(sentinel=USAGE_RELOGIN_REQUIRED)),
    _acct("3", "ops@example.net", alias="night-shift", entry=Reading(last_error="http-503")),
    _acct("4", "sam@example.org", alias="side", set_aside="invalid_grant", lines=_CLAUDE_LINES,
          usage={"five_hour": _w(5, 4 * _H + 40 * 60), "seven_day": _w(30, 4 * _D)}),
    _acct("5", "lee@example.net", alias="spare", lines=_CLAUDE_LINES, entry=Reading(
        last_good={"five_hour": _w(40, 2 * _H + 30 * 60), "seven_day": _w(55, 3 * _D)},
        fetched_at=_NOW - (3 * _H + 4 * 60), last_error="timeout")),
    _acct("1", "maya@example.com", provider="codex", active=True, lines=_CODEX_LINES, usage={
        "five_hour": _w(41, 2 * _H + 5 * 60), "seven_day": _w(100, _D + 7 * _H)}),
    _acct("2", "maya@northwind.example.com", provider="codex", alias="work",
          entry=Reading(sentinel="usage unknown until used")),
]


def test_fleet_rows_are_short():
    assert [_row(a).text for a in _FLEET] == [
        "#1 personal · 5h 46% · 7d 38% · Fable 52% · 1 Fable reset",
        "#2 work · 5h 12% · 7d 21% · extra 18%",
        "#3 night-shift · 5h 93% · 7d 81% · Fable 97% · 1 five-hour reset",
        "#4 side · disabled · 5h 0% · 7d 9%",
        "#5 ci · API key · no usage recorded",
        "#1 maya@example.com · 5h 22% · 7d 47%",
        "#2 work · 5h 8% · 7d 15% · 1 reset credit",
    ]


def test_trouble_rows_are_short():
    assert [_row(a).text for a in _TROUBLE] == [
        "#1 personal · 5h 100% (resets in 1h 12m) · 7d 64%",
        "#2 work · needs re-login",
        "#3 night-shift · usage unavailable (HTTP 503), retrying",
        "#4 side · needs re-login",
        "#5 spare · 5h 40% · 7d 55% · as of 3h 4m ago (timed out)",
        "#1 maya@example.com · 5h 41% · 7d 100% (resets in 1d 7h)",
        "#2 work · usage unknown until used",
    ]


def test_no_account_row_exceeds_the_row_limit():
    assert menubar.ROW_LIMIT == 72
    for acc in _FLEET + _TROUBLE:
        row = _row(acc)
        assert len(row.text) <= menubar.ROW_LIMIT, row.text
        # The tooltip keeps everything the row leaves out.
        assert row.tooltip.splitlines()[0].startswith(acc.label(long=False))
    night = _row(_FLEET[2])
    assert night.attention  # past its line, not at its limit: orange, no countdown
    assert "Fable   97%  resets" in night.tooltip and "Holds 1 five-hour reset" in night.tooltip
    # No ordinary row in either fixture needs cutting.
    assert not any(_row(a).text.endswith("…") for a in _FLEET + _TROUBLE)


def test_a_long_label_gives_way_before_the_numbers():
    acc = _acct("9", "a.very.long.address.for.a.shared.mailbox@example-corporation.com",
                usage=_FLEET[2].entry.last_good, lines=_CLAUDE_LINES)
    text = _row(acc).text
    assert len(text) == menubar.ROW_LIMIT
    assert text.endswith(" · 5h 93% · 7d 81% · Fable 97% · 1 five-hour reset")
    assert text.startswith("#9 a.very.long.addres…")


def test_fit_row_cuts_the_end_once_the_label_is_at_its_floor():
    from ai_quota_swap import display

    parts = ["#9 work", "x" * 80]
    assert menubar.fit_row(parts) == display.fit("#9 work · " + "x" * 80, menubar.ROW_LIMIT)
    assert menubar.fit_row(["#1 a", "5h 3%"]) == "#1 a · 5h 3%"


def test_every_sentinel_note_fits_a_menu_row():
    for note in menubar.SENTINEL_NOTES.values():
        short = menubar.menu_note(note)
        assert " — " not in short
        assert len(short) <= menubar.MENU_NOTE_LIMIT


def test_menu_note_truncates_and_leaves_short_notes():
    assert menubar.menu_note("x" * 60).endswith("…")
    assert len(menubar.menu_note("x" * 60)) == menubar.MENU_NOTE_LIMIT
    assert menubar.menu_note("API key") == "API key"


def test_set_tooltip_reaches_the_native_item_and_tolerates_its_absence():
    class _NSMenuItem:
        tip = None

        def setToolTip_(self, text):
            self.tip = text

    class _Item:
        def __init__(self):
            self._menuitem = _NSMenuItem()

    item = _Item()
    menubar._set_tooltip(item, "the full text")
    assert item._menuitem.tip == "the full text"
    menubar._set_tooltip(object(), "ignored")


# --- usage log lines ------------------------------------------------------------------

def test_format_usage_log():
    usage = {"five_hour": {"pct": 35.0, "clock": "06:59"}, "seven_day": {"pct": 55.0, "clock": "Jun 29 21:59"}}
    assert menubar.format_usage_log("#1 personal", usage) == (
        "usage #1 personal: 5h 35% (resets 06:59) · 7d 55% (resets Jun 29 21:59)"
    )
    assert menubar.format_usage_log("#1", None) is None
    assert menubar.format_usage_log("#1", {"spend": {"pct": 5.0}}) is None


def test_usage_log_key_ignores_clock_tracks_pct():
    u1 = {"five_hour": {"pct": 35.0, "clock": "06:59"}, "seven_day": {"pct": 55.0}}
    u2 = {"five_hour": {"pct": 35.0, "clock": "07:59"}, "seven_day": {"pct": 55.0}}
    u3 = {"five_hour": {"pct": 36.0}, "seven_day": {"pct": 55.0}}
    assert menubar._usage_log_key(u1) == menubar._usage_log_key(u2)
    assert menubar._usage_log_key(u1) != menubar._usage_log_key(u3)


# --- title ------------------------------------------------------------------------

def test_format_title_variants():
    s = MenuBarSettings(show_account_name=True, title_pct="5h")
    assert menubar.format_title("loc@example.com", _USAGE, s) == "⇄ loc · 42%"
    assert menubar.format_title("loc@example.com", _USAGE, s, alias="dev") == "⇄ dev · 42%"
    s = MenuBarSettings(show_account_name=False, title_pct="7d")
    assert menubar.format_title("loc@example.com", _USAGE, s) == "⇄ 18%"
    s = MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@example.com", _USAGE, s) == "⇄ 42% · 18%"
    s = MenuBarSettings(show_account_name=False, title_pct="off")
    assert menubar.format_title("loc@example.com", _USAGE, s) == "⇄"
    assert menubar.format_title(None, None, MenuBarSettings()) == "⇄"


def test_format_title_says_limit_reached_for_a_refused_probe():
    s = MenuBarSettings(show_account_name=True, title_pct="5h")
    usage = {**_USAGE, "rejected": {"retry_after_s": None, "resets_at": None}}
    assert menubar.format_title("loc@example.com", usage, s) == "⇄ loc · limit reached · 42%"


def test_format_title_scoped_and_truncation():
    s = MenuBarSettings(show_account_name=False, title_pct="both", title_scoped=True)
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 55.0}, {"name": "Opus", "pct": 7.0}]}
    assert menubar.format_title("loc@example.com", usage, s) == "⇄ 42% · 18% · Fable 55% · Opus 7%"
    s = MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("averylonglocalpart@example.com", None, s) == "⇄ averylonglo*"


def test_format_title_shows_a_placeholder_name_whole():
    s = MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("setup-token-13@token.local", None, s) == "⇄ setup-token-13"
    assert menubar.format_title("api-key-5@token.local", None, s) == "⇄ api-key-5"


def test_format_title_reflects_a_passed_weekly_reset():
    s = MenuBarSettings(show_account_name=False, title_pct="7d")
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-86400)}}
    assert menubar.format_title("a@x.com", usage, s, _NOW) == "⇄ 0%"


def test_format_title_attention_prefix():
    assert menubar.format_title(None, None, MenuBarSettings(), attention=True) == "⚠ ⇄"


def _claude(*accounts, **kw) -> ProviderState:
    return ProviderState("claude", tuple(accounts), **kw)


def _codex(*accounts, **kw) -> ProviderState:
    kw.setdefault("managed", len(accounts))
    return ProviderState("codex", tuple(accounts), **kw)


def _inp(claude=None, codex=None, **kw) -> MenuInput:
    kw.setdefault("settings", MenuBarSettings())
    kw.setdefault("thresholds", {"claude": 90.0, "codex": 99.0})
    return MenuInput(claude=claude or _claude(), codex=codex or _codex(), now=_NOW, **kw)


def _usage_at(h5, d7=10.0):
    return {"five_hour": {"pct": h5}, "seven_day": {"pct": d7}}


_C1 = _acct("1", "maya@example.com", alias="personal", usage=_usage_at(46, 38), active=True)
_C2 = _acct("2", "maya@northwind.example.com", alias="work", usage=_usage_at(12, 21))
_X1 = _acct("1", "maya@example.com", provider="codex", usage=_usage_at(80, 47), active=True)
_X2 = _acct("2", "maya@northwind.example.com", provider="codex", alias="work", usage=_usage_at(8, 15))


@pytest.mark.parametrize(
    "follows, codex_pct, expected",
    [
        ("claude", 80, "claude"),
        ("codex", 80, "codex"),
        ("closest", 80, "codex"),   # Codex 80% is closer than Claude's 46%
        ("closest", 46, "claude"),  # a tie keeps Claude Code
        ("closest", 20, "claude"),
    ],
)
def test_title_follows_each_setting(follows, codex_pct, expected):
    x1 = _acct("1", "maya@example.com", provider="codex", usage=_usage_at(codex_pct), active=True)
    inp = _inp(_claude(_C1, _C2), _codex(x1, _X2), settings=MenuBarSettings(title_follows=follows))
    assert menubar.title_provider(inp) == expected


def test_title_following_codex_shows_codex_s_account():
    s = MenuBarSettings(title_follows="codex")
    inp = _inp(_claude(_C1), _codex(_X1, _X2), settings=s)
    assert menubar.build_menu(inp).title == "⇄ maya · 80% · 47%"


def test_title_follows_claude_code_without_codex_accounts():
    inp = _inp(_claude(_C1), _codex(), settings=MenuBarSettings(title_follows="codex"))
    assert menubar.title_provider(inp) == "claude"
    assert menubar.build_menu(inp).title == "⇄ personal · 46% · 38%"


def test_title_falls_back_to_the_provider_with_an_active_account():
    no_active = _acct("1", "maya@example.com", usage=_usage_at(10))
    inp = _inp(_claude(no_active), _codex(_X1), settings=MenuBarSettings(title_follows="claude"))
    assert menubar.title_provider(inp) == "codex"
    inp = _inp(_claude(_C1), _codex(_X2), settings=MenuBarSettings(title_follows="codex"))
    assert menubar.title_provider(inp) == "claude"


def test_title_of_an_account_that_needs_relogin_shows_no_numbers():
    acc = _acct("1", "maya@example.com", alias="personal", usage=_OK, active=True, set_aside="invalid_grant")
    assert menubar.build_menu(_inp(_claude(acc))).title == "⇄ personal"


# --- health row --------------------------------------------------------------------

def test_health_label_claude_only():
    assert menubar.health_label(auto_enabled=False, engine_running=False, usage_age=90,
                                refresh_interval=60) == ("Auto-switch off · usage read 1m ago", False)
    assert menubar.health_label(auto_enabled=True, engine_running=True, usage_age=30,
                                refresh_interval=60) == ("Auto-switch running · usage read just now", False)
    label, attention = menubar.health_label(auto_enabled=True, engine_running=False, usage_age=30,
                                            refresh_interval=60)
    assert label.startswith("⚠ Auto-switch stopped, click to restart") and attention


def test_health_label_dates_an_old_reading_and_a_missing_one():
    label, _ = menubar.health_label(auto_enabled=False, engine_running=False, usage_age=2 * 3600,
                                    refresh_interval=60)
    assert label.endswith("usage as of 2h 0m ago")
    label, _ = menubar.health_label(auto_enabled=False, engine_running=False, usage_age=None,
                                    refresh_interval=60)
    assert label.endswith("no usage reading yet")


def test_health_label_stale_threshold_has_a_floor():
    def text(age, interval):
        return menubar.health_label(auto_enabled=True, engine_running=True, usage_age=age,
                                    refresh_interval=interval)[0]
    assert "as of" not in text(600, 30)
    assert "as of" in text(16 * 60, 30)
    assert "as of" not in text(20 * 60, 600)


@pytest.mark.parametrize(
    "claude, codex, both, expected, attention",
    [
        ("off", "off", True, "Auto-switch off", False),
        ("running", "running", True, "Auto-switch running for Claude Code and Codex", False),
        ("running", "off", True, "Auto-switch: Claude Code running, Codex off", False),
        ("off", "stopped", True, "⚠ Auto-switch: Claude Code off, Codex stopped, click to restart", True),
        ("stopped", "stopped", True, "⚠ Auto-switch stopped for Claude Code and Codex, click to restart", True),
        ("running", "stopped", False, "Auto-switch running", False),  # Codex not shown
    ],
)
def test_engines_in_words(claude, codex, both, expected, attention):
    states = [EngineState("claude", claude), EngineState("codex", codex)]
    assert menubar.engines_text(states, both=both) == (expected, attention)


def test_engines_running_elsewhere():
    states = [EngineState("claude", "elsewhere", host="qswap auto"), EngineState("codex", "off")]
    assert menubar.engines_text(states, both=False) == ("Auto-switch running in qswap auto", False)


def test_health_row_dates_the_oldest_active_reading():
    c1 = _acct("1", usage=_OK, active=True, fetched_at=_NOW - 60)
    x1 = _acct("1", provider="codex", usage=_OK, active=True, fetched_at=_NOW - 300)
    assert menubar.usage_age(_inp(_claude(c1), _codex(x1))) == 300
    assert menubar.usage_age(_inp(_claude(c1))) == 60
    assert menubar.usage_age(_inp(_claude(_acct("1", usage=None, active=True)))) is None


def test_health_row_restarts_a_stopped_engine():
    engines = {"claude": EngineState("claude", "stopped", failure="RuntimeError: boom")}
    menu = menubar.build_menu(_inp(_claude(_C1), engines=engines))
    health = menu.section("health").rows[0]
    assert health.action == Action("restart-engines") and health.attention
    assert health.tooltip == "RuntimeError: boom"
    assert menu.title.startswith("⚠ ⇄")


@pytest.mark.parametrize(
    "enabled, running, beat, pid, expected",
    [
        (False, False, None, 1, "off"),
        (True, True, None, 1, "running"),
        (True, False, None, 1, "stopped"),
        (False, False, {"mode": "loop", "nextTickAt": _NOW + 30, "pid": 2, "host": "qswap auto"}, 1, "elsewhere"),
        (False, False, {"mode": "loop", "nextTickAt": _NOW + 30, "pid": 1}, 1, "off"),  # our own, old
        (False, False, {"mode": "loop", "nextTickAt": _NOW - 3600, "pid": 2}, 1, "off"),  # long gone
    ],
)
def test_engine_state(enabled, running, beat, pid, expected):
    state = menubar.engine_state("claude", enabled=enabled, running=running, heartbeat=beat, now=_NOW, pid=pid)
    assert state.state == expected


# --- the menu, Claude Code only --------------------------------------------------------

def _texts(row) -> list[str]:
    return [c.text for c in row.children if not c.separator]


def test_claude_only_menu_looks_as_it_did():
    menu = menubar.build_menu(_inp(_claude(_C1, _C2, live_email="maya@example.com")))
    assert [s.key for s in menu.sections] == ["health", "claude", "switch", "manage", "app"]
    assert menu.section("claude").heading is None
    rows = menu.section("claude").rows
    assert rows[0].text == "#1 personal · 5h 46% · 7d 38%"
    assert rows[0].checked and rows[0].action == Action("switch", "claude", "1")
    assert not rows[1].checked and rows[1].action == Action("switch", "claude", "2")
    assert [r.text for r in menu.section("switch").rows] == ["Rotate to next", "Switch to best", "Next available"]
    assert menu.find("Rotate to next").action == Action("rotate", "claude", value=None)
    assert menu.find("Switch to best").action == Action("rotate", "claude", value="best")
    assert _texts(menu.find("Disable / enable account")) == [
        "#1 personal (maya@example.com)", "#2 work (maya@northwind.example.com)",
    ]
    assert _texts(menu.find("Remove account"))[0] == "#1 personal (maya@example.com)"
    assert menu.find("Refresh current credentials").text == "Refresh current credentials"
    settings = _texts(menu.find("Settings"))
    assert "Auto-switch Claude Code" in settings and "Auto-switch threshold" in settings
    assert not any("Codex" in t or "Title follows" in t for t in settings)
    assert "Codex" not in menu.outline()
    assert menu.title == "⇄ personal · 46% · 38%"


def test_claude_only_add_menu():
    menu = menubar.build_menu(_inp(_claude(_C1, live_email="maya@example.com")))
    assert _texts(menu.find("Add account")) == ["Add from a setup-token or API key…"]
    # A live login qswap does not manage adds itself.
    unmanaged = _claude(_C2, live_email="new@example.com")
    menu = menubar.build_menu(_inp(unmanaged))
    add = menu.find("Add account")
    assert add.children[0].text == "Add the current Claude Code login (new@example.com)"
    assert add.children[0].action == Action("add-current", "claude")


def test_disable_submenu_checks_the_disabled_one():
    c2 = _acct("2", "b@x.com", usage=_OK, disabled=True)
    menu = menubar.build_menu(_inp(_claude(_C1, c2)))
    rows = menu.find("Disable / enable account").children
    assert [r.checked for r in rows] == [False, True]
    assert rows[1].action == Action("toggle-disabled", "claude", "2", False)
    assert rows[0].action == Action("toggle-disabled", "claude", "1", True)


def test_settings_reflect_threshold_and_checks():
    s = MenuBarSettings(auto_switch_enabled=True, title_pct="7d", refresh_interval=300)
    menu = menubar.build_menu(_inp(_claude(_C1), settings=s, start_at_login=True))
    assert menu.find("Auto-switch Claude Code").checked
    assert menu.find("Start at login").checked
    assert [r.text for r in menu.find("Auto-switch threshold").children if r.checked] == ["90%"]
    assert [r.text for r in menu.find("Title percentage").children if r.checked] == ["Weekly window (7d)"]
    assert [r.text for r in menu.find("Refresh interval").children if r.checked] == ["5 minutes"]
    assert menu.find("80%").action == Action("threshold", "claude", value=80)


# --- the menu, both providers ------------------------------------------------------------

def _both_menu(**kw):
    return menubar.build_menu(_inp(_claude(_C1, _C2), _codex(_X1, _X2), **kw))


def test_both_providers_have_a_section_each():
    menu = _both_menu()
    assert [s.key for s in menu.sections] == ["health", "claude", "codex", "switch", "manage", "app"]
    assert menu.section("claude").heading == "Claude Code accounts"
    assert menu.section("codex").heading == "Codex accounts"
    codex = menu.section("codex").rows
    assert codex[0].text == "#1 maya@example.com · 5h 80% · 7d 47%"
    assert codex[0].checked and codex[0].action == Action("switch", "codex", "1")
    assert codex[1].action == Action("switch", "codex", "2")


def test_both_providers_name_the_provider_in_the_account_submenus():
    menu = _both_menu()
    assert _texts(menu.find("Remove account")) == [
        "Claude #1 personal (maya@example.com)", "Claude #2 work (maya@northwind.example.com)",
        "Codex #1 maya@example.com", "Codex #2 work (maya@northwind.example.com)",
    ]
    assert menu.find("Remove account").children[2].action == Action("remove", "codex", "1")
    assert _texts(menu.find("Disable / enable account"))[3] == "Codex #2 work (maya@northwind.example.com)"


def test_both_providers_settings_and_strategies():
    s = MenuBarSettings(codex_auto_switch_enabled=True, title_follows="closest")
    menu = _both_menu(settings=s)
    assert [r.text for r in menu.section("switch").rows] == [
        "Rotate to next (Claude Code)", "Switch to best (Claude Code)", "Next available (Claude Code)",
    ]
    assert menu.find("Auto-switch Codex").checked
    assert menu.find("Auto-switch Codex").action == Action("toggle-auto", "codex")
    assert not menu.find("Auto-switch Claude Code").checked
    assert [r.text for r in menu.find("Codex threshold").children if r.checked] == ["99%"]
    assert [r.text for r in menu.find("Title follows").children if r.checked] == [
        "Whichever is closest to its limit",
    ]
    assert _texts(menu.find("Add account")) == [
        "Add from a setup-token or API key…", "Log in another Codex account…",
    ]
    assert menu.find("Refresh current credentials").text == "Refresh current credentials (Claude Code)"


def test_codex_managed_but_claude_not():
    menu = menubar.build_menu(_inp(_claude(live_email="maya@example.com"), _codex(_X1)))
    assert [r.text for r in menu.section("claude").rows] == [
        "No Claude Code accounts are managed yet.",
        "Claude Code is logged in as maya@example.com. Add it with: qswap add",
    ]
    assert menu.section("claude").rows[1].action == Action("add-current", "claude")
    assert menu.section("switch") is None


def test_codex_that_cannot_be_read_says_why():
    codex = ProviderState("codex", managed=2, error="Codex keeps this login in the OS keyring")
    menu = menubar.build_menu(_inp(_claude(_C1), codex))
    assert [r.text for r in menu.section("codex").rows] == [
        "Codex could not be read: Codex keeps this login in the OS keyring",
    ]


def test_an_unmanaged_codex_login_can_be_added_without_codex_accounts():
    menu = menubar.build_menu(_inp(_claude(_C1), _codex(live_email="x@example.com")))
    assert menu.section("codex") is None
    assert menu.find("Add the current Codex login (x@example.com)").action == Action("add-current", "codex")


# --- first run -------------------------------------------------------------------------

def test_first_run_with_nothing_logged_in():
    menu = menubar.build_menu(_inp(_claude(cli_found=True)))
    rows = menu.section("accounts").rows
    assert [r.text for r in rows] == [
        "No accounts are managed yet.",
        "Claude Code is not logged in. Log in with claude (/login), then run: qswap add",
    ]
    assert all(r.disabled for r in rows)
    assert menu.section("switch") is None
    assert _texts(menu.find("Remove account")) == ["No accounts are managed yet."]
    assert menu.title == "⇄"


def test_first_run_without_claude_code_installed():
    rows = menubar.build_menu(_inp(_claude(cli_found=False))).section("accounts").rows
    assert rows[1].text == "Claude Code was not found on PATH. Install it and log in, then run: qswap add"


def test_first_run_with_a_claude_login():
    menu = menubar.build_menu(_inp(_claude(live_email="maya@example.com")))
    rows = menu.section("accounts").rows
    assert rows[1].text == "Claude Code is logged in as maya@example.com. Add it with: qswap add"
    assert rows[1].action == Action("add-current", "claude")
    assert _texts(menu.find("Add account"))[0] == "Add the current Claude Code login (maya@example.com)"


def test_first_run_with_both_logins():
    menu = menubar.build_menu(_inp(_claude(live_email="maya@example.com"), _codex(live_email="maya@example.com")))
    rows = menu.section("accounts").rows
    assert [r.text for r in rows] == [
        "No accounts are managed yet.",
        "Claude Code is logged in as maya@example.com. Add it with: qswap add",
        "Codex is logged in as maya@example.com. Add it with: qswap codex add",
    ]
    assert rows[2].action == Action("add-current", "codex")
    assert _texts(menu.find("Add account"))[:2] == [
        "Add the current Claude Code login (maya@example.com)",
        "Add the current Codex login (maya@example.com)",
    ]


# --- trouble states in the menu -----------------------------------------------------------

def test_trouble_menu():
    far = _NOW + 120 * 86400
    accounts = [
        _acct("1", alias="personal", active=True,
              usage={"five_hour": {"pct": 100.0, "resets_at": _iso(72 * 60)}, "seven_day": {"pct": 64.0}}),
        _acct("2", "w@x.com", entry=Reading(sentinel=USAGE_RELOGIN_REQUIRED)),
        _acct("3", "o@x.com", entry=Reading(last_error="http-503")),
        _acct("4", "s@x.com", usage=_OK, set_aside="invalid_grant"),
        _acct("5", "l@x.com", entry=Reading(last_good=_OK, fetched_at=_NOW - 3 * 3600, last_error="timeout")),
        _acct("6", "f@x.com", usage=_reset_usage(grants=[fable("a", ends=far), weekly("w", ends=far)])),
        _acct("7", "ci@x.com", kind="api_key",
              entry=Reading(sentinel=USAGE_API_KEY, api_spend=_api_reading(used=520.0))),
    ]
    rows = menubar.build_menu(_inp(_claude(*accounts))).section("claude").rows
    assert [r.attention for r in rows] == [True, True, False, True, False, False, True]
    assert rows[6].text.endswith("· API key · $4.12 today · $520.00/$500 (104%, over budget) est.")
    assert rows[0].text == "#1 personal · 5h 100% (resets in 1h 12m) · 7d 64%"
    assert rows[1].text == "#2 w@x.com · needs re-login"
    assert rows[2].text == "#3 o@x.com · usage unavailable (HTTP 503), retrying"
    assert rows[3].text == "#4 s@x.com · needs re-login"
    assert rows[4].text == "#5 l@x.com · 5h 46% · 7d 38% · as of 3h 0m ago (timed out)"
    assert rows[5].text == "#6 f@x.com · 7d 30% · resets: 1 Fable, 1 weekly"
    assert all(len(r.text) <= menubar.ROW_LIMIT for r in rows)
    words = " ".join(r.text for r in rows)
    for banned in ("quarantined", "usage unknown", "Account-", "$$", "(!)", "rejected"):
        assert banned not in words


# --- cost row --------------------------------------------------------------------------

def test_cost_row():
    menu = menubar.build_menu(_inp(_claude(_C1), cost=CostSummary(today=12.4, week=179.72)))
    row = menu.section("cost").rows[0]
    assert row.text == "Today $12.40 · 7 days $179.72 (estimated)"
    assert row.action is None and row.disabled


def test_cost_row_hidden_without_a_ledger():
    assert menubar.build_menu(_inp(_claude(_C1))).section("cost") is None


def test_cost_row_marks_unpriced_tokens_as_the_report_does():
    assert CostSummary(today=0.0, week=5.0, today_unpriced=10, week_unpriced=10).text() == (
        "Today n/a · 7 days $5.00+ (estimated)"
    )


# --- switch history --------------------------------------------------------------------

_SWITCH_LOG = (
    "2026-06-27 00:57:50,178 - INFO - Switched from account 1 to 3\n"
    "2026-06-27 02:06:21,302 - INFO - usage a@x.com: 5h 10%\n"
    "2026-06-27 02:08:00,000 - INFO - Switched Codex from account None to 2\n"
    "2026-06-27 02:10:00,000 - INFO - Switched from account 3 to 1\n"
)


def test_parse_switch_history_both_providers_newest_first():
    assert menubar.parse_switch_history(_SWITCH_LOG) == [
        SwitchRecord("claude", "3", "1", "2026-06-27 02:10"),
        SwitchRecord("codex", None, "2", "2026-06-27 02:08"),
        SwitchRecord("claude", "1", "3", "2026-06-27 00:57"),
    ]
    assert menubar.parse_switch_history("") == []


def test_parse_switch_history_respects_limit():
    lines = "\n".join(f"2026-06-27 0{i}:00:00,000 - INFO - Switched from account 1 to 2" for i in range(1, 6))
    out = menubar.parse_switch_history(lines, limit=2)
    assert len(out) == 2 and out[0].stamp == "2026-06-27 05:00"


def test_history_rows_use_account_labels():
    history = tuple(menubar.parse_switch_history(_SWITCH_LOG))
    c3 = _acct("3", "o@x.com", alias="ops", usage=_OK)
    claude_only = menubar.build_menu(_inp(_claude(_C1, c3), history=history))
    assert _texts(claude_only.find("Switch history")) == [
        "#3 ops → #1 personal · 2026-06-27 02:10",
        "#1 personal → #3 ops · 2026-06-27 00:57",
        "Open full log…",
    ]
    both = menubar.build_menu(_inp(_claude(_C1, c3), _codex(_X1, _X2), history=history))
    assert _texts(both.find("Switch history"))[:2] == [
        "Claude #3 ops → #1 personal · 2026-06-27 02:10",
        "to Codex #2 work · 2026-06-27 02:08",
    ]


def test_history_empty():
    menu = menubar.build_menu(_inp(_claude(_C1)))
    assert _texts(menu.find("Switch history")) == ["No switches logged yet", "Open full log…"]


def test_every_action_has_a_handler():
    import re as _re

    source = (Path(menubar.__file__).parent / "menubar_app.py").read_text(encoding="utf-8")
    handlers = set(_re.findall(r"def _do_(\w+)\(", source))
    assert {k.replace("-", "_") for k in menubar.ACTION_KINDS} <= handlers
    menus = [
        _both_menu(engines={"claude": EngineState("claude", "stopped")}, history=(
            SwitchRecord("claude", "1", "2", "2026-06-27 02:10"),)),
        menubar.build_menu(_inp(_claude(live_email="a@x.com"), _codex(live_email="a@x.com"))),
    ]

    def walk(rows):
        for row in rows:
            if row.action is not None:
                assert row.action.kind in menubar.ACTION_KINDS, row.action
            walk(row.children)

    for menu in menus:
        walk(menu.rows())


def test_outline_marks_state():
    out = _both_menu().outline(actions=True)
    assert "✓ #1 personal · 5h 46%" in out
    assert "Codex accounts  (heading)" in out
    assert "Add account ▸" in out
    assert "[switch codex 2]" in out


# --- dialogs ---------------------------------------------------------------------------

def test_remove_asks_what_the_command_line_asks():
    title, message = menubar.remove_dialog("claude", "#2 work (maya@example.com)")
    assert title == "Remove account"
    assert message == "Remove #2 work (maya@example.com) and delete its saved login?"
    _, message = menubar.remove_dialog("codex", "#2 work (maya@example.com)")
    assert message.startswith("Stop managing #2 work (maya@example.com) and delete qswap's saved copy of its login?")
    assert "codex logout" in message


def test_the_switchers_ask_the_same_question(monkeypatch):
    from ai_quota_swap import display

    assert display.remove_question("claude", "#1 a") == "Remove #1 a and delete its saved login?"
    for path in ("switcher.py", "codex/switcher.py"):
        text = (Path(menubar.__file__).parent / path).read_text(encoding="utf-8")
        assert "display.remove_question(" in text


def test_relogin_confirmation_is_the_refusal_without_the_flag():
    refusal = (
        "#4 side needs re-login (its saved login was refused); not switched.\n"
        "Log in to it in Claude Code (/login), then run: qswap add\n"
        "To switch to it anyway: qswap switch 4 --anyway"
    )
    assert menubar.relogin_confirmation(refusal) == (
        "#4 side needs re-login (its saved login was refused).\n"
        "Log in to it in Claude Code (/login), then run: qswap add"
    )


def test_relogin_confirmation_matches_the_switchers_refusal(tmp_path, monkeypatch):
    # The real Codex refusal, so a change to its wording shows up here.
    from ai_quota_swap.codex.switcher import CodexSwitcher

    switcher = CodexSwitcher(data_root=tmp_path, configure_logging=False)
    monkeypatch.setattr(switcher, "set_aside_accounts", lambda: {"2": "invalid_grant"})
    monkeypatch.setattr(switcher, "label", lambda number, email=None, long=True: "#2 work")
    with pytest.raises(SwitchError) as caught:
        switcher.check_switch_target("2")
    text = menubar.relogin_confirmation(str(caught.value))
    assert text == "#2 work needs re-login (its saved login was refused).\nLog in to it again: qswap codex login"
    assert "--anyway" not in text and "--force" not in text


def test_codex_toggle_says_a_switch_interrupts_work():
    text = menubar.codex_toggle_message(99.0)
    assert "99%" in text and "codex.threshold" in text
    assert "restarts Codex's background server, which interrupts work in progress" in text
    assert "restartDaemon is off" in menubar.codex_toggle_message(99.0, restart_daemon=False)


def test_token_entry_follows_the_command_line_rules():
    assert menubar.parse_token_entry("sk-ant-oat01-abc", "") == ("sk-ant-oat01-abc", None)
    assert menubar.parse_token_entry(" sk-ant-api03-x ", "me@example.com") == ("sk-ant-api03-x", "me@example.com")
    assert menubar.parse_token_entry("sk-ant-oat01-abc me@example.com") == ("sk-ant-oat01-abc", "me@example.com")
    with pytest.raises(ValueError, match="Token cannot be empty"):
        menubar.parse_token_entry("  ", "me@example.com")
    with pytest.raises(ValueError, match="Invalid email format"):
        menubar.parse_token_entry("sk-ant-oat01-abc", "not an email")
    with pytest.raises(ValueError):
        menubar.parse_token_entry("a b c")
    assert "setup-token" in menubar.TOKEN_DIALOG_MESSAGE and "API key" in menubar.TOKEN_DIALOG_MESSAGE


def test_codex_login_help_names_the_command():
    assert "qswap codex login" in menubar.codex_login_help()


def test_manual_switch_followup():
    assert menubar.manual_switch_followup("claude", {}) == (
        "Claude Code picks up the new account within 30 seconds; restart it to apply now."
    )
    assert menubar.manual_switch_followup("claude", {}, backend="file") == (
        "Claude Code uses the new account from its next message."
    )
    restarted = {"backgroundServer": {"pid": 812, "restarted": True}}
    assert menubar.manual_switch_followup("codex", restarted) == (
        "Restarted Codex's background server (pid 812); new sessions use this account."
    )
    assert menubar.manual_switch_followup("codex", {}) == "New codex sessions use this account."


# --- errors ----------------------------------------------------------------------------

def test_error_text_uses_the_command_line_words():
    assert menubar.error_text(ConfigError("No accounts are managed yet.")) == "No accounts are managed yet."
    # The provider-neutral base, not only Claude Code's subclass.
    assert not issubclass(CodexLoginError, ClaudeSwitchError)
    assert menubar.error_text(CodexLoginError("Codex keeps this login in the OS keyring")) == (
        "Codex keeps this login in the OS keyring"
    )
    text = menubar.error_text(RuntimeError("boom"))
    assert text.startswith("Something went wrong: RuntimeError: boom.")


def test_guarded_never_lets_an_error_out():
    alerts, logged = [], []
    assert menubar.guarded(lambda: None, alert=alerts.append, log=logged.append) is True

    def provider_failure():
        raise CodexLoginError("not a ChatGPT login")

    def bug():
        raise KeyError("x")

    assert menubar.guarded(provider_failure, alert=alerts.append, log=logged.append) is False
    assert alerts == ["not a ChatGPT login"] and logged == []
    assert menubar.guarded(bug, alert=alerts.append, log=logged.append) is False
    assert alerts[-1].startswith("Something went wrong: KeyError")
    assert len(logged) == 1

    def broken_alert(_text):
        raise RuntimeError("no window server")

    assert menubar.guarded(bug, alert=broken_alert) is False  # still nothing escapes


def test_no_claude_only_catches_left():
    root = Path(menubar.__file__).parent
    for name in ("menubar.py", "menubar_app.py", "menubar_data.py", "menubar_engines.py",
                 "menubar_notify.py", "update_check.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "except ClaudeSwitchError" not in text and "(ClaudeSwitchError," not in text, name


def test_run_keeps_the_app_out_of_the_dock():
    # rumps sets no activation policy; without Accessory a framework Python
    # parks a "Python" icon in the Dock. Lost once in a rewrite already.
    import inspect

    assert "NSApplicationActivationPolicyAccessory" in inspect.getsource(menubar.run)


def test_run_without_rumps_raises_clean_error(monkeypatch):
    """A missing menubar extra surfaces as a provider error with the install
    hint, not a traceback."""
    monkeypatch.setitem(sys.modules, "rumps", None)
    with pytest.raises(ClaudeSwitchError, match=r"ai-quota-swap\[menubar\]"):
        menubar.run(switcher=None)


# --- notifications -----------------------------------------------------------------------

_CTX = EventContext(provider="claude", names={"1": "personal", "2": "work", "4": "side", "5": "ci"})
_CODEX_CTX = EventContext(provider="codex", names={"2": "work"})


def _switch_event(context=_CTX, **kw):
    return SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "maya@example.com"},
        to_ref={"number": 2, "email": "maya@northwind.example.com"},
        binding=BindingWindow("5h", 91.0, 90.0, None, 0.0), context=context, **kw,
    )


def test_switch_notification():
    n = menubar_notify.Notifier()
    assert n.event("claude", _switch_event(), both=False, now=0).body == (
        "Switched #1 personal → #2 work · 5h reached 91%"
    )
    note = n.event("claude", _switch_event(), both=True, now=0)
    assert note.body == "Switched Claude #1 personal → #2 work · 5h reached 91%"
    assert note.title == "ai-quota-swap"


def test_codex_switch_notification_names_codex():
    event = SwitchEvent(trigger="at-limit", from_ref={"number": 1, "email": "maya@example.com"},
                        to_ref={"number": 2, "email": "w@x.com"}, context=_CODEX_CTX)
    note = menubar_notify.Notifier().event("codex", event, both=True, now=0)
    assert note.body == "Switched Codex #1 maya@example.com → #2 work · limit reached"


def test_dry_run_and_routine_events_never_notify():
    n = menubar_notify.Notifier()
    assert n.event("claude", _switch_event(dry_run=True), both=False, now=0) is None
    poll = PollEvent(active=None, headroom={}, threshold=90.0)
    assert n.event("claude", poll, both=False, now=0) is None


def _reset_event(result="reset", number=1, **kw):
    plan = {"reason": "last-resort", "window": "Fable", "pct": 100.0, "hoursLeft": 72.0,
            "grantKind": "Fable", "scope": "model"}
    return ResetEvent(account={"number": number, "email": "maya@example.com"}, plan=plan,
                      result={"result": result, "resetsLeft": 0} if result else None, context=_CTX, **kw)


def test_reset_used_notification_by_kind():
    note = menubar_notify.Notifier().event("claude", _reset_event(), both=True, now=0)
    assert note.body.startswith("Used a Fable reset on Claude #1 personal · Fable reached 100%")


def test_reset_not_used_is_said_once_per_account_and_result():
    n = menubar_notify.Notifier()
    first = n.event("claude", _reset_event("refused"), both=False, now=0)
    assert first.body.startswith("Did not use a Fable reset on #1 personal: refused")
    assert n.event("claude", _reset_event("refused"), both=False, now=60) is None
    assert n.event("claude", _reset_event("error"), both=False, now=60) is not None
    # Codex account 1 is another account.
    assert n.event("codex", _reset_event("refused"), both=True, now=60) is not None


def test_needs_relogin_notification_once_with_the_next_step():
    n = menubar_notify.Notifier()
    event = QuarantineEvent(number="4", email="sam@example.org", reason="invalid_grant", context=_CTX)
    note = n.event("claude", event, both=True, now=0)
    assert note.body == (
        "Claude #4 side needs re-login: its saved login was refused. "
        "Log in to it in Claude Code (/login), then run: qswap add"
    )
    assert "quarantine" not in note.body
    assert n.event("claude", event, both=True, now=10) is None
    codex = QuarantineEvent(number="2", email="w@x.com", reason="invalid_grant",
                            recover="Log in to it again: qswap codex login", context=_CODEX_CTX)
    assert n.event("codex", codex, both=True, now=10).body == (
        "Codex #2 work needs re-login: its saved login was refused. Log in to it again: qswap codex login"
    )


def test_all_exhausted_once_per_episode():
    n = menubar_notify.Notifier()
    event = AllExhaustedEvent(earliest_reset_at="2026-11-10T15:32:00Z", ts="2026-11-10T14:20:00Z")
    note = n.event("claude", event, both=False, now=0)
    assert note.body.startswith("All accounts are at their limit; the first resets ")
    assert note.body.endswith("· in 1h 12m")
    assert n.event("claude", event, both=False, now=600) is None
    # A switch ends the episode; the next one is said again.
    n.event("claude", _switch_event(), both=False, now=700)
    assert n.event("claude", event, both=False, now=800) is not None
    both = n.event("codex", event, both=True, now=800)
    assert both.body.startswith("All Codex accounts are at their limit")


def test_budget_warning_once_per_account_and_month():
    n = menubar_notify.Notifier()
    event = BudgetWarningEvent(account={"number": 5, "email": "ci@x.com"}, used=410.0, limit=500.0,
                               month="2026-11", source="admin-api", context=_CTX)
    assert n.event("claude", event, both=True, now=0).body == (
        "Claude #5 ci has spent $410.00 of its $500.00 monthly budget (82%)"
    )
    assert n.event("claude", event, both=True, now=1) is None


def test_no_duplicates_across_the_two_engines():
    n = menubar_notify.Notifier()
    warning = ConfigWarningEvent(message="autoswitch.model 'Fabel' matches no limit any account reports")
    assert n.event("claude", warning, both=True, now=0) is not None
    assert n.event("codex", warning, both=True, now=1) is None
    error = ErrorEvent(message="network unreachable")
    first = n.event("claude", error, both=True, now=0)
    assert first.body == "Claude Code auto-switch error: network unreachable. It retries by itself."
    assert n.event("codex", error, both=True, now=5) is None
    assert n.event("codex", error, both=True, now=ENGINE_ERROR_REPEAT_S + 1) is not None


def test_engine_lifecycle_notifications():
    n = menubar_notify.Notifier()
    assert n.engine_stopped("claude", "RuntimeError: boom", both=False).body == (
        "Auto-switch stopped: RuntimeError: boom. Open the menu to restart it."
    )
    assert n.engine_failed("codex", "No Codex accounts are managed yet.", both=True).body == (
        "Codex auto-switch did not start: No Codex accounts are managed yet."
    )


def test_manual_switch_text():
    label = menubar_notify.ref_labeler({"1": {"alias": "personal"}, "2": {"alias": "work"}})
    result = {"switched": True, "from": {"number": 1, "email": "a@x.com"},
              "to": {"number": 2, "email": "b@x.com"}, "warnings": ["a warning"]}
    assert menubar_notify.manual_switch_text(
        "claude", result, both=False, label=label, followup="Claude Code uses the new account."
    ) == "Switched #1 personal → #2 work. Claude Code uses the new account. a warning"
    assert menubar_notify.manual_switch_text("codex", result, both=True, label=label).startswith(
        "Switched Codex #1 personal → #2 work."
    )
    assert menubar_notify.manual_switch_text("claude", {"switched": False}, both=False, label=label) is None


# engine errors must not vanish (field case: every tick's switch raised
# "Current account credential is empty" for two days, the icon looking healthy).

def test_engine_error_is_surfaced_once_then_limited():
    ev = ErrorEvent(message="Current account credential is empty")
    seen: dict = {}
    assert should_surface_engine_error(ev, seen, now=1000.0) is True
    assert should_surface_engine_error(ev, seen, now=1060.0) is False
    assert should_surface_engine_error(ev, seen, now=1000.0 + ENGINE_ERROR_REPEAT_S) is True


def test_two_alternating_engine_errors_are_each_limited():
    a = ErrorEvent(message="Current account credential is empty")
    b = ErrorEvent(message="Claude config file not found")
    seen: dict = {}
    surfaced = [should_surface_engine_error(ev, seen, now=1000.0 + 60 * i)
                for i, ev in enumerate([a, b, a, b, a, b])]
    assert surfaced == [True, True, False, False, False, False]


def test_engine_errors_differing_only_in_numbers_are_one_error():
    seen: dict = {}
    assert should_surface_engine_error(ErrorEvent(message="lock held by pid 4242 for 31s"), seen, now=1000.0)
    assert not should_surface_engine_error(ErrorEvent(message="lock held by pid 4243 for 92s"), seen, now=1060.0)


def test_engine_error_table_is_pruned():
    seen: dict = {}
    for i in range(50):
        should_surface_engine_error(ErrorEvent(message=f"error kind {chr(65 + i % 26)}{i}"), seen, now=0.0)
    should_surface_engine_error(ErrorEvent(message="x"), seen, now=ENGINE_ERROR_REPEAT_S)
    assert list(seen) == ["x"]


# --- hosting engines ----------------------------------------------------------------------

class FakeEngine:
    def __init__(self, provider, emit, *, crash: Exception | None = None):
        self.provider = provider
        self.emit = emit
        self.crash = crash
        self._stop = threading.Event()
        self.started = threading.Event()

    def run_loop(self):
        self.started.set()
        if self.crash is not None:
            raise self.crash
        self._stop.wait(10)
        return 0

    def stop(self):
        self._stop.set()


class Host:
    """A supervisor over fake engines, with the events it reported."""

    def __init__(self, *, fail: dict | None = None, crash: dict | None = None):
        self.built: list[FakeEngine] = []
        self.events: list[tuple[str, object]] = []
        self.fail = dict(fail or {})
        self.crash = dict(crash or {})
        self.lock = threading.Lock()
        self.sup = menubar_engines.EngineSupervisor(self.factory, self.on_event)

    def factory(self, provider, emit):
        if provider in self.fail:
            raise self.fail[provider]
        engine = FakeEngine(provider, emit, crash=self.crash.get(provider))
        self.built.append(engine)
        return engine

    def on_event(self, provider, event):
        with self.lock:
            self.events.append((provider, event))

    def close(self):
        self.sup.stop_all(join_timeout=5)
        assert self.sup.join(5)


@pytest.fixture
def host():
    h = Host()
    yield h
    h.close()


def test_wanted_engines_per_toggle():
    s = MenuBarSettings(auto_switch_enabled=True, codex_auto_switch_enabled=True)
    assert menubar.wanted_engines(s, codex_accounts=2) == {"claude": True, "codex": True}
    assert menubar.wanted_engines(s, codex_accounts=0) == {"claude": True, "codex": False}
    assert menubar.wanted_engines(MenuBarSettings(), codex_accounts=2) == {"claude": False, "codex": False}
    old_file = MenuBarSettings(auto_switch_enabled=True)
    assert menubar.wanted_engines(old_file, codex_accounts=2) == {"claude": True, "codex": False}


def test_each_enabled_toggle_hosts_its_engine_in_its_own_thread(host):
    host.sup.apply({"claude": True, "codex": False})
    assert host.sup.running("claude") and not host.sup.running("codex")
    host.sup.apply({"claude": True, "codex": True})
    assert host.sup.running("codex")
    for engine in host.built:
        assert engine.started.wait(5)
    names = {t.name for t in threading.enumerate() if t.name.startswith("qswap-")}
    assert names == {"qswap-claude-engine", "qswap-codex-engine"}
    assert [e.provider for e in host.built] == ["claude", "codex"]
    host.sup.apply({"claude": False, "codex": True})
    assert not host.sup.running("claude") and host.sup.running("codex")
    assert host.built[0]._stop.is_set()


def test_applying_twice_starts_nothing_new(host):
    host.sup.apply({"claude": True})
    host.sup.apply({"claude": True})
    assert len(host.built) == 1


def test_a_failed_start_is_remembered_not_retried_until_asked():
    h = Host(fail={"codex": ConfigError("No Codex accounts are managed yet.")})
    try:
        assert h.sup.apply({"claude": True, "codex": True}) == {"codex": "No Codex accounts are managed yet."}
        assert h.sup.failure("codex") == "No Codex accounts are managed yet."
        assert h.sup.apply({"claude": True, "codex": True}) == {}  # not retried every refresh
        h.fail.clear()
        assert h.sup.retry_failed() == {"codex": None}
        assert h.sup.running("codex") and h.sup.failure("codex") is None
    finally:
        h.close()


def test_turning_a_toggle_off_clears_its_failure():
    h = Host(fail={"claude": RuntimeError("boom")})
    try:
        h.sup.apply({"claude": True})
        assert h.sup.failure("claude").startswith("Something went wrong: RuntimeError: boom")
        h.sup.apply({"claude": False})
        assert h.sup.failure("claude") is None
    finally:
        h.close()


def test_an_engine_that_dies_is_reported_and_remembered():
    h = Host(crash={"claude": RuntimeError("tick blew up")})
    try:
        h.sup.start("claude")
        assert h.sup.join(5)
        (provider, event), = [e for e in h.events if e[1].kind == "engine-stopped"]
        assert provider == "claude" and event.error == "RuntimeError: tick blew up"
        assert h.sup.stopped("claude", event) is True
        assert not h.sup.running("claude") and h.sup.failure("claude") == "RuntimeError: tick blew up"
        state = menubar.engine_state("claude", enabled=True, running=False, failure=h.sup.failure("claude"))
        assert state.state == "stopped"
    finally:
        h.close()


def test_a_restarted_engine_is_not_cleared_by_its_predecessor_s_exit(host):
    host.sup.start("claude")
    old = host.sup.engine("claude")
    host.sup.restart("claude")
    new = host.sup.engine("claude")
    assert new is not old and old._stop.is_set()
    assert host.sup.stopped("claude", menubar_engines.EngineStoppedEvent(old)) is False
    assert host.sup.running("claude")


def test_engine_events_carry_their_provider(host):
    host.sup.start("codex")
    host.built[0].emit(ErrorEvent(message="x"))
    assert host.events[0][0] == "codex"


def test_engine_exit_applies_only_to_the_hosted_engine():
    old, new = object(), object()
    assert menubar.engine_exit_applies(old, old) is True
    assert menubar.engine_exit_applies(new, old) is False
    assert menubar.engine_exit_applies(None, old) is False


def test_engine_stopped_event_names_the_error_or_the_plain_end():
    engine = object()
    assert menubar.EngineStoppedEvent(engine, "RuntimeError: boom").human() == "RuntimeError: boom"
    assert menubar.EngineStoppedEvent(engine).human() == "the engine loop ended on its own"


# --- noticing changes made elsewhere -----------------------------------------------------

class _Stat:
    def __init__(self, mtime):
        self.st_mtime = mtime


class FakeFiles:
    def __init__(self):
        self.mtimes: dict[str, float] = {}
        self.stats = 0

    def stat(self, path):
        self.stats += 1
        if str(path) not in self.mtimes:
            raise FileNotFoundError(path)
        return _Stat(self.mtimes[str(path)])


def _watcher(files, logins, reads):
    def reader(provider):
        def read():
            reads.append(provider)
            return logins.get(provider)
        return read

    return menubar_engines.ActiveChangeWatcher(
        paths={"claude": lambda: Path("claude.json"), "codex": lambda: Path("auth.json")},
        read_active={"claude": reader("claude"), "codex": reader("codex")},
        stat=files.stat,
    )


def test_outside_change_is_noticed_for_both_providers():
    files, reads = FakeFiles(), []
    logins = {"claude": "a@x.com", "codex": "c@x.com"}
    files.mtimes = {"claude.json": 1.0, "auth.json": 1.0}
    w = _watcher(files, logins, reads)
    shown = {"claude": "a@x.com", "codex": "c@x.com"}
    assert w.poll(shown) == []           # first look: read, nothing changed
    assert reads == ["claude", "codex"]
    assert w.poll(shown) == []           # unchanged files are not read again
    assert reads == ["claude", "codex"]
    files.mtimes["auth.json"] = 2.0
    logins["codex"] = "d@x.com"          # `qswap codex switch 2` in a terminal
    assert w.poll(shown) == ["codex"]
    files.mtimes["claude.json"] = 2.0    # Claude Code rewrote its config, same login
    assert w.poll(shown) == []
    files.mtimes["claude.json"] = 3.0
    logins["claude"] = "b@x.com"
    assert w.poll(shown) == ["claude"]


def test_outside_change_tolerates_missing_files_and_failed_reads():
    files, reads = FakeFiles(), []
    w = _watcher(files, {"claude": "a@x.com"}, reads)
    assert w.poll({"claude": None}) == []  # no files at all
    assert reads == []
    files.mtimes = {"claude.json": 1.0}

    def broken():
        raise OSError("unreadable")

    w._read["claude"] = broken
    assert w.poll({"claude": None}) == []


# --- background reads --------------------------------------------------------------------

class _Snap:
    def __init__(self, accounts):
        self.accounts = accounts


def _snap_account(number, email, *, active=False, usage=None, **kw):
    from ai_quota_swap.models import AccountSnapshot
    from ai_quota_swap.usage_store import UsageEntry

    return AccountSnapshot(
        number=number, email=email, org_name=kw.pop("org_name", ""), org_uuid="",
        is_active=active, kind=kw.pop("kind", "oauth"), switchable=True,
        usage=usage or UsageEntry(), **kw,
    )


def test_account_states_from_a_snapshot():
    from ai_quota_swap.usage_store import UsageEntry

    entry = UsageEntry(last_good={"five_hour": {"pct": 10.0}}, fetched_at=123.0)
    snap = _Snap([
        _snap_account("1", "a@x.com", active=True, usage=entry, alias="dev", login_expires_at=456.0,
                      org_name="Northwind"),
        _snap_account("2", "b@x.com", disabled=True),
    ])
    states = menubar_data.account_states(snap, "claude", {"2": "invalid_grant"}, now=_NOW)
    assert states[0] == AccountState("claude", "1", "a@x.com", alias="dev", active=True, entry=entry,
                                     login_expires_at=456.0, org="Northwind")
    assert states[1].disabled and states[1].set_aside == "invalid_grant" and states[1].needs_relogin


class _FakeSource:
    def __init__(self, snap=None, error=None):
        self.calls = []
        self.snap, self.error = snap, error

    def take(self, *, full=False, store_only=False):
        self.calls.append((full, store_only))
        if self.error:
            raise self.error
        return self.snap


class _FakeClaude:
    def __init__(self, live="a@x.com", set_aside=None):
        self.live, self.held = live, set_aside or {}

    def live_login_email(self):
        return self.live

    def set_aside_accounts(self):
        return self.held


def test_claude_state_reads_store_only_while_an_engine_runs():
    source = _FakeSource(_Snap([_snap_account("1", "a@x.com", active=True)]))
    state = menubar_data.claude_state(_FakeClaude(set_aside={"1": "x"}), source, full=False,
                                      store_only=True, now=_NOW)
    assert source.calls == [(False, True)]
    assert state.live_email == "a@x.com" and state.accounts[0].set_aside == "x"
    assert state.cli_found is None  # only asked on a first run


def test_claude_state_says_why_it_could_not_be_read():
    source = _FakeSource(error=ConfigError("sequence.json could not be read"))
    state = menubar_data.claude_state(_FakeClaude(), source, full=True, store_only=False, now=_NOW)
    assert state.error == "sequence.json could not be read"


def test_claude_state_first_run_asks_whether_claude_is_installed():
    state = menubar_data.claude_state(_FakeClaude(live=None), _FakeSource(_Snap([])),
                                      full=False, store_only=False, now=_NOW)
    assert state.cli_found is True  # conftest: both CLIs found


def test_codex_reader_without_codex_accounts(tmp_path):
    state = menubar_data.CodexReader(tmp_path).state(full=False, store_only=False, now=_NOW)
    assert state == ProviderState("codex", managed=0)
    assert menubar_data.codex_registry_count(tmp_path) == 0
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "sequence.json").write_text(json.dumps({"accounts": {"1": {}, "2": {}}}))
    assert menubar_data.codex_registry_count(tmp_path) == 2


def test_read_history_reads_the_end_of_the_log(tmp_path):
    log = tmp_path / "ai-quota-swap.log"
    log.write_text("x" * (menubar_data.HISTORY_TAIL_BYTES + 10) + "\n" + _SWITCH_LOG)
    assert menubar_data.read_history(log)[0] == SwitchRecord("claude", "3", "1", "2026-06-27 02:10")
    assert menubar_data.read_history(tmp_path / "missing.log") == ()


def _ledger(root: Path, rows):
    from ai_quota_swap.ledger.store import LedgerStore

    store = LedgerStore(root / "ledger.db")
    try:
        store.begin()
        store.add_buckets(rows)
        store.commit()
    finally:
        store.close()


def _hour(day: _dt.date, h: int = 12) -> int:
    local = _dt.datetime(day.year, day.month, day.day, h).astimezone()
    return int(local.timestamp() // 3600)


def test_read_cost_today_and_seven_days(tmp_path):
    from ai_quota_swap.ledger.pricing import PriceTable

    today = _dt.date(2026, 11, 10)
    model = "claude-opus-5"
    _ledger(tmp_path, [
        ("claude", _hour(today), "a", model, "p", "s1", 1_000_000, 100_000, 0, 0, 0, 0, 3),
        ("claude", _hour(today - _dt.timedelta(days=3)), "a", model, "p", "s2", 2_000_000, 0, 0, 0, 0, 0, 1),
        ("codex", _hour(today - _dt.timedelta(days=9)), "a", model, "p", "s3", 9_000_000, 0, 0, 0, 0, 0, 1),
    ])
    price = PriceTable().lookup(model)[1]
    cost = menubar_data.read_cost(tmp_path, today=today)
    assert cost.today == pytest.approx(price.cost(1_000_000, 100_000, 0, 0, 0))
    assert cost.week == pytest.approx(price.cost(1_000_000, 100_000, 0, 0, 0) + price.cost(2_000_000, 0, 0, 0, 0))
    assert cost.week_unpriced == 0


def test_read_cost_hidden_without_a_ledger_and_never_creates_one(tmp_path):
    assert menubar_data.read_cost(tmp_path) is None
    assert not (tmp_path / "ledger.db").exists()


def test_read_cost_hidden_while_the_ledger_is_empty(tmp_path):
    _ledger(tmp_path, [])
    assert menubar_data.read_cost(tmp_path) is None


def test_read_cost_counts_unpriced_tokens(tmp_path):
    today = _dt.date(2026, 11, 10)
    _ledger(tmp_path, [("claude", _hour(today), "a", "no-such-model", "p", "s", 50, 50, 0, 0, 0, 0, 1)])
    cost = menubar_data.read_cost(tmp_path, today=today)
    assert cost.today == 0.0 and cost.today_unpriced == 100
    assert cost.text() == "Today n/a · 7 days n/a (estimated)"


def test_read_cost_opens_the_ledger_read_only(tmp_path, monkeypatch):
    _ledger(tmp_path, [("claude", _hour(_dt.date.today()), "a", "claude-opus-5", "p", "s", 1, 1, 0, 0, 0, 0, 1)])
    uris = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        uris.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(menubar_data.sqlite3, "connect", spy)
    assert menubar_data.read_cost(tmp_path) is not None
    assert uris and all(u.endswith("?mode=ro") for u in uris)


def test_ledger_stamp_changes_with_the_ledger(tmp_path):
    before = menubar_data.ledger_stamp(tmp_path)
    _ledger(tmp_path, [])
    assert menubar_data.ledger_stamp(tmp_path) != before


def test_scan_interval_is_modest():
    assert 5 * 60 <= menubar_data.COST_SCAN_INTERVAL_S <= 60 * 60


# --- usage resets: the automatic mode (Settings → Use resets automatically) -------------

import inspect  # noqa: E402

from ai_quota_swap import manual_reset, menubar_app  # noqa: E402
from ai_quota_swap.resets import ClaimResult  # noqa: E402
from ai_quota_swap.settings import (  # noqa: E402
    RESET_AUTO_MODES,
    ResetSettings,
    load_reset_settings,
    set_setting,
)
from tests.test_manual_reset import FakeSwitcher  # noqa: E402
from tests.test_reset_kinds import full  # noqa: E402
from tests.test_resets import _grant as _reset_grant  # noqa: E402

_MODE_LABELS = [
    "Off",
    "When the active account reaches its limit",
    "As a last resort, when no account can take over",
    "When any account reaches its limit",
]


def _resets_submenu(menu=None, **rs):
    menu = menu or menubar.build_menu(_inp(_claude(_C1), resets=ResetSettings(**rs)))
    return menu.find("Use resets automatically")


def _choices(sub):
    return [r for r in sub.children if r.action is not None and r.action.kind == "resets-auto"]


@pytest.mark.parametrize("mode, label", list(zip(RESET_AUTO_MODES, _MODE_LABELS)))
def test_resets_submenu_checks_the_one_mode(mode, label):
    sub = _resets_submenu(auto=mode)
    choices = _choices(sub)
    assert [r.text for r in choices] == _MODE_LABELS
    assert [r.action.value for r in choices] == list(RESET_AUTO_MODES)
    assert [r.text for r in choices if r.checked] == [label]
    # Each choice's tooltip is qswap reset's "Auto:" line for it.
    assert choices[3].tooltip == "on-limit · when any account reaches 95%"
    assert choices[0].tooltip == "off · only qswap reset N spends one"


def test_resets_submenu_names_codex_only_when_codex_is_managed():
    # Claude Code alone: the menu never mentions Codex.
    assert sub_first(_resets_submenu()) == "Off"
    both = _resets_submenu(_both_menu())
    first = both.children[0]
    assert first.text == "Applies to Claude Code and Codex" and first.disabled


def sub_first(sub):
    return sub.children[0].text


def _toggle(sub, kind):
    return next(r for r in sub.children if r.action is not None and r.action.kind == kind)


def test_resets_submenu_weekly_toggle():
    sub = _resets_submenu()
    toggle = _toggle(sub, "toggle-weekly-for-model")
    assert sub.children[sub.children.index(toggle) - 1].separator
    assert toggle.text == "Use a weekly reset for a single model's limit"
    assert toggle.action == Action("toggle-weekly-for-model") and not toggle.checked
    on = _toggle(_resets_submenu(weekly_for_model_limit=True), "toggle-weekly-for-model")
    assert on.checked
    assert menubar.resets_setting(toggle.action, ResetSettings()) == ("resets.weeklyForModelLimit", "true")
    assert menubar.resets_setting(on.action, ResetSettings(weekly_for_model_limit=True)) == (
        "resets.weeklyForModelLimit", "false")
    assert menubar.resets_setting(Action("resets-auto", value="active"), ResetSettings()) == (
        "resets.auto", "active")


BEFORE_SWITCH_TOOLTIP = (
    "resets.beforeSwitch: auto-switch stays past the active account's switch line "
    "until its own reset is spent at resets.threshold"
)


@pytest.mark.parametrize("auto", ["active", "on-limit"])
def test_resets_submenu_before_switch_toggle(auto):
    sub = _resets_submenu(auto=auto)
    toggle = sub.children[-1]
    assert sub.children[-2] == _toggle(sub, "toggle-weekly-for-model")
    assert toggle.text == "Use the active account's reset before switching"
    assert toggle.action == Action("toggle-before-switch") and not toggle.checked
    assert toggle.tooltip == BEFORE_SWITCH_TOOLTIP
    on = _resets_submenu(auto=auto, before_switch=True).children[-1]
    assert on.checked and on.action == Action("toggle-before-switch")
    assert menubar.resets_setting(toggle.action, ResetSettings(auto=auto)) == (
        "resets.beforeSwitch", "true")
    assert menubar.resets_setting(on.action, ResetSettings(auto=auto, before_switch=True)) == (
        "resets.beforeSwitch", "false")


@pytest.mark.parametrize("auto", ["off", "last-resort"])
@pytest.mark.parametrize("before_switch", [False, True])
def test_resets_submenu_greys_the_before_switch_toggle_where_it_does_nothing(
    auto, before_switch
):
    toggle = _resets_submenu(auto=auto, before_switch=before_switch).children[-1]
    assert toggle.text == "Use the active account's reset before switching"
    assert toggle.disabled and toggle.checked is before_switch
    assert toggle.tooltip == (
        f"{BEFORE_SWITCH_TOOLTIP}; no effect while resets.auto is {auto}"
    )


@pytest.mark.parametrize("auto", list(RESET_AUTO_MODES))
@pytest.mark.parametrize("before_switch", [False, True])
def test_resets_submenu_greys_the_before_switch_toggle_while_resets_are_off(
    auto, before_switch
):
    """With resets.enabled off the submenu says so first; the toggle is
    greyed, keeps its check mark, and names resets.enabled, the outer
    switch, whatever resets.auto is (as qswap config set does)."""
    sub = _resets_submenu(enabled=False, auto=auto, before_switch=before_switch)
    assert sub.children[0].text == "Usage resets are not read (resets.enabled is off)"
    toggle = sub.children[-1]
    assert toggle.text == "Use the active account's reset before switching"
    assert toggle.disabled and toggle.checked is before_switch
    assert toggle.tooltip == f"{BEFORE_SWITCH_TOOLTIP}; no effect while resets.enabled is false"


def test_the_before_switch_toggle_writes_without_asking(tmp_path: Path):
    changed, asked, wrote = _change(Action("toggle-before-switch"), ResetSettings(auto="active"))
    assert changed and asked == [] and wrote == [("resets.beforeSwitch", "true")]
    set_setting(tmp_path, *wrote[0])
    assert load_reset_settings(tmp_path).before_switch is True
    handlers = inspect.getsource(menubar_app.run_app)
    assert "def _do_toggle_before_switch(self, a: Action):\n            self._change_resets(a)" in (
        handlers)


def test_resets_submenu_sits_with_the_auto_switch_settings_for_both_providers():
    for menu in (menubar.build_menu(_inp(_claude(_C1))), _both_menu()):
        texts = [r.text for r in menu.find("Settings").children]
        assert texts.index("Use resets automatically") == texts.index("Start at login") - 2
    off = _resets_submenu(enabled=False)
    assert off.children[0].text == "Usage resets are not read (resets.enabled is off)"


def test_turning_automatic_use_on_asks_once():
    assert menubar.resets_auto_confirmation("off", "on-limit", codex=True) == (
        "Whenever auto-switch runs, it will spend a usage reset or Codex reset credit by "
        "itself when any account reaches its limit, at most three in 24 hours per provider."
    )
    # Claude Code alone: usage resets only, nothing about Codex.
    assert menubar.resets_auto_confirmation("off", "active") == (
        "Whenever auto-switch runs, it will spend a usage reset by itself when the active "
        "account reaches its limit, at most three in 24 hours."
    )
    assert "as a last resort, when no account can take over" in (
        menubar.resets_auto_confirmation("off", "last-resort"))
    # Only from off: moving between automatic modes, or turning it off, asks nothing.
    assert menubar.resets_auto_confirmation("last-resort", "on-limit") is None
    assert menubar.resets_auto_confirmation("active", "off") is None
    assert menubar.resets_auto_confirmation("off", "off") is None


def _change(action, resets, *, answer=True, codex=False):
    asked, wrote = [], []
    changed = menubar.change_resets_setting(
        action, resets, codex=codex,
        confirm=lambda title, question: asked.append((title, question)) or answer,
        write=lambda key, value: wrote.append((key, value)),
    )
    return changed, asked, wrote


def test_off_to_on_asks_first_and_cancel_writes_nothing():
    on = Action("resets-auto", value="on-limit")
    changed, asked, wrote = _change(on, ResetSettings(auto="off"), answer=False)
    assert not changed and len(asked) == 1 and wrote == []
    assert asked[0][0] == "Use resets automatically"
    changed, asked, wrote = _change(on, ResetSettings(auto="off"))
    assert changed and len(asked) == 1 and wrote == [("resets.auto", "on-limit")]
    # Between automatic modes, to off, and the toggle: no question.
    for action, current in ((on, "last-resort"), (Action("resets-auto", value="off"), "active"),
                            (Action("toggle-weekly-for-model"), "off")):
        changed, asked, wrote = _change(action, ResetSettings(auto=current))
        assert changed and asked == [] and len(wrote) == 1


def test_a_terminal_change_to_off_since_the_menu_was_drawn_still_asks(tmp_path: Path):
    # The menu was drawn with last-resort; `qswap config set resets.auto off`
    # ran since. The click is judged on the file, so it asks.
    drawn = ResetSettings(auto="last-resort")
    set_setting(tmp_path, "resets.auto", "off")
    assert menubar.resets_auto_confirmation(drawn.auto, "on-limit") is None  # the stale view
    changed, asked, _wrote = _change(Action("resets-auto", value="on-limit"),
                                     load_reset_settings(tmp_path), answer=False)
    assert not changed and len(asked) == 1
    # The app hands it the file as it is at the click.
    source = inspect.getsource(menubar_app.run_app)
    body = source[source.index("def _change_resets"):source.index("def _ask_on_main")]
    assert body.index("current = load_reset_settings(data_root)") < body.index("change_resets_setting(")
    assert "a, current, codex=self._both()," in body


def test_the_writer_is_the_one_qswap_config_set_uses(tmp_path: Path):
    # The menu's choice and `qswap config set resets.auto active` write the
    # same key in the same file, through settings.set_setting.
    menu_dir, cli_dir = tmp_path / "menu", tmp_path / "cli"
    key, value = menubar.resets_setting(Action("resets-auto", value="active"), ResetSettings())
    set_setting(menu_dir, key, value)
    set_setting(cli_dir, "resets.auto", "active")
    assert (menu_dir / "settings.json").read_text() == (cli_dir / "settings.json").read_text()
    assert json.loads((menu_dir / "settings.json").read_text())["resets"] == {"auto": "active"}
    key, value = menubar.resets_setting(Action("toggle-weekly-for-model"), load_reset_settings(menu_dir))
    set_setting(menu_dir, key, value)
    assert json.loads((menu_dir / "settings.json").read_text())["resets"] == {
        "auto": "active", "weeklyForModelLimit": True}
    assert "write=lambda key, value: set_setting(data_root, key, value)," in (
        inspect.getsource(menubar_app.run_app))


def test_a_value_changed_from_the_command_line_shows_at_the_next_build(tmp_path: Path):
    set_setting(tmp_path, "resets.auto", "on-limit")  # qswap config set resets.auto on-limit
    sub = _resets_submenu(menubar.build_menu(_inp(_claude(_C1), resets=load_reset_settings(tmp_path))))
    assert [r.text for r in sub.children if r.checked] == ["When any account reaches its limit"]


def test_a_running_engine_reads_the_new_mode_without_a_restart(temp_home):
    # The engine asks its switcher for resets.* on every tick.
    from ai_quota_swap.switcher import ClaudeAccountSwitcher

    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    assert sw.reset_settings().auto == "last-resort"
    set_setting(sw.backup_dir, "resets.auto", "off")
    assert sw.reset_settings().auto == "off"


# --- the app's guards on the irreversible path (menubar_app imports without rumps) ----------


class _FakeAlert:
    def __init__(self, kit):
        self.kit, self.buttons = kit, []

    def init(self):
        return self

    def setMessageText_(self, t):
        self.kit.title = t

    def setInformativeText_(self, t):
        self.kit.message = t

    def addButtonWithTitle_(self, t):
        self.buttons.append(t)

    def runModal(self):
        self.kit.buttons = list(self.buttons)
        # NSAlert: the Nth button added answers 1000 + (N - 1); Return presses the first.
        return 1000 + self.buttons.index(self.kit.press) if self.kit.press in self.buttons else 1000


class _FakeAppKit:
    NSAlertFirstButtonReturn, NSAlertSecondButtonReturn = 1000, 1001

    def __init__(self, press):
        self.press = press
        kit = self

        class NSAlert:
            @staticmethod
            def alloc():
                return _FakeAlert(kit)

        self.NSAlert = NSAlert


def test_cancel_is_the_default_button_and_only_ok_spends():
    kit = _FakeAppKit(press="<Return>")  # Return presses the first (default) button
    assert menubar_app._ask_cancel_default(kit, "Use this Fable reset?", "m", "Use reset") is False
    assert kit.buttons == ["Cancel", "Use reset"]  # the first button is the default
    assert kit.title == "Use this Fable reset?" and kit.message == "m"
    assert menubar_app._ask_cancel_default(_FakeAppKit("Cancel"), "t", "m", "Use reset") is False
    assert menubar_app._ask_cancel_default(_FakeAppKit("Use reset"), "t", "m", "Use reset") is True


def test_the_app_wires_the_real_spend_the_lock_and_both_questions():
    source = inspect.getsource(menubar_app.run_app)
    assert "ResetRunner(\n                manual_reset.spend," in source
    assert "lock=self._action_lock," in source
    assert "confirm=self._ask_on_main," in source
    assert source.count('self._confirm_cancel_default(title, message, "Use reset")') == 2
    assert 'confirm=lambda title, question: self._confirm(title, question, "Turn on"),' in source


# --- usage resets: Use a reset ------------------------------------------------------------

_FAR = _NOW + 120 * 86400


def _waiting(gid="wk", **kw):
    """A weekly reset that needs a limit hit first."""
    return weekly(gid, ends=_FAR, usable=False, use_requires_limit=True, **kw)


def _holder(grants, number="1", alias="personal", provider="claude", usage_kw=None,
            email="maya@example.com", **kw):
    usage = _reset_usage(grants=grants, **(usage_kw or {}))
    return _acct(number, email, alias=alias, provider=provider, usage=usage, **kw)


def _use_rows(*claude, codex=(), **kw):
    menu = menubar.build_menu(_inp(_claude(*claude), _codex(*codex), **kw))
    sub = menu.find("Use a reset")
    return sub.children if sub is not None else None


def test_use_a_reset_one_row_per_kind():
    acc = _holder([fable("fb", ends=_FAR, left=2), _waiting(), fable("fb2", ends=_FAR - 86400)])
    rows = _use_rows(acc)
    assert [r.text for r in rows] == [
        "#1 personal · Fable reset · clears Fable usage only",
        # 72 columns: what it clears goes before the reason; the tooltip has it all.
        "#1 personal · weekly reset · usable once a limit is reached",
    ]
    assert rows[1].tooltip == (
        "#1 personal · weekly reset · clears all weekly usage · usable once a limit is reached")
    # The usable Fable grant that expires first is the one spent.
    assert rows[0].action == Action("use-reset", "claude", "1", "fb2")
    assert rows[1].disabled


def test_use_a_reset_split_by_provider():
    claude = _holder([fable("fb", ends=_FAR)], active=True)
    codex = _holder([full("cr", ends=_FAR)], alias="", provider="codex")
    rows = _use_rows(claude, codex=(codex,))
    assert [r.text for r in rows] == [
        "Claude #1 personal · Fable reset · clears Fable usage only",
        "",
        "Codex #1 maya@example.com · reset credit · clears all usage",
    ]
    assert rows[1].separator
    assert rows[2].action == Action("use-reset", "codex", "1", "cr")


def test_use_a_reset_unknown_and_unusable_rows_are_greyed():
    odd = _reset_grant("u", clears=["five_hour", "seven_day_overage_included"], gives_back=False,
                       type="session", ends=_FAR)
    rows = _use_rows(_holder([odd]), _holder([_waiting()], number="2", alias="work"))
    assert rows[0].text == "#1 personal · reset · clears 5h, Fable" and rows[0].disabled
    assert "qswap reset 1 --grant u" in rows[0].tooltip
    assert rows[1].text.endswith("· usable once a limit is reached") and rows[1].disabled
    relogin = _holder([fable("fb", ends=_FAR)], set_aside="invalid_grant")
    assert _use_rows(relogin)[0].text.endswith("· needs re-login")


def test_use_a_reset_rows_in_progress_are_greyed():
    acc = _holder([fable("fb", ends=_FAR), _waiting()])
    rows = _use_rows(acc, resets_busy=frozenset({("claude", "1")}))
    assert all(r.text.endswith("· in progress") and r.disabled for r in rows)


def test_use_a_reset_rows_fit_the_row_limit():
    long = _holder([_waiting()], number="12", alias="", email="maya.longname@example-corp.example.com")
    codex = _holder([full("cr", ends=_FAR)], alias="", provider="codex")
    rows = _use_rows(long, codex=(codex,))
    row = rows[0]
    assert len(row.text) <= menubar.ROW_LIMIT
    # The name gives way first (number and provider kept), then what it
    # clears; the kind and the reason a greyed row gives are kept.
    assert row.text.startswith("Claude #12 m")
    assert row.text.endswith("· weekly reset · usable once a limit is reached")
    assert row.tooltip == (
        "Claude #12 maya.longname@example-corp.example.com · weekly reset · clears all weekly "
        "usage · usable once a limit is reached"
    )
    # A placeholder name is never cut mid-word.
    token = _holder([fable("fb", ends=_FAR)], number="3", alias="", email="setup-token-3@token.local")
    assert _use_rows(token)[0].text == "#3 setup-token-3 · Fable reset · clears Fable usage only"
    assert menubar.fit_reset_row("#3 setup-token-3", "weekly reset", "clears all weekly usage",
                                 "usable once a limit is reached", floor=16, limit=64) == (
        "#3 setup-token-3 · weekly reset · usable once a limit is reached")


def test_use_a_reset_is_absent_when_nothing_is_held():
    assert _use_rows(_C1, _C2) is None
    assert _use_rows(_holder([fable("fb", ends=_NOW - 60)])) is None  # expired
    assert _use_rows(_holder([fable("fb", ends=_FAR)]), resets=ResetSettings(enabled=False)) is None


def _choice_of(acc, grant_id, *, codex=False, fetched_at=_NOW, now=_NOW):
    usage = acc.usage()
    grant = next(g for g in usage["resets"]["grants"] if g["id"] == grant_id)
    return manual_reset.Choice(acc.number, acc.email, acc.label(long=False), grant, usage,
                               usage["resets"], now, 95.0, codex=codex, fetched_at=fetched_at)


def test_use_a_reset_dialog_for_a_narrow_reset():
    acc = _holder([fable("fb", ends=_FAR)], usage_kw={"fable": 100.0, "d7": 31.0})
    title, message = menubar.reset_dialog(_choice_of(acc, "fb"), name="#1 personal (maya@example.com)")
    assert title == "Use this Fable reset?"
    lines = message.splitlines()
    assert lines[0] == "#1 personal (maya@example.com) · Fable reset"
    assert "Clears Fable usage only" in lines
    assert lines[-1].endswith("a used reset cannot be undone")
    assert "only Fable is at its limit" not in message and "could not be refreshed" not in message


def test_use_a_reset_dialog_for_a_broad_reset_on_a_narrow_limit():
    acc = _holder([weekly("wk", ends=_FAR)], usage_kw={"fable": 100.0, "d7": 31.0})
    choice = _choice_of(acc, "wk")
    title, message = menubar.reset_dialog(choice, name="#1 personal (maya@example.com)")
    assert title == "Use this weekly reset?"
    assert "This weekly reset clears all weekly usage; only Fable is at its limit (7d is at 31%)." in (
        message.splitlines())
    assert message.splitlines()[-1].endswith("a used reset cannot be undone")
    # Word for word what `qswap reset 1` prints before asking.
    cli_text = manual_reset.describe(choice, name="#1 personal (maya@example.com)")
    assert message == "\n".join(menubar._ANSI.sub("", ln).strip() for ln in cli_text.splitlines())


def test_use_a_reset_dialog_for_a_codex_credit():
    acc = _holder([full("cr", ends=_FAR)], alias="", provider="codex")
    title, message = menubar.reset_dialog(_choice_of(acc, "cr", codex=True),
                                          name="Codex #1 maya@example.com")
    assert title == "Use this reset credit?"
    assert message.splitlines()[0] == "Codex #1 maya@example.com · reset credit"
    assert message.splitlines()[-1].endswith("a spent credit cannot be undone")


def test_use_a_reset_dialog_says_when_the_reading_could_not_be_refreshed():
    acc = _holder([fable("fb", ends=_FAR)], usage_kw={"fable": 100.0})
    _title, message = menubar.reset_dialog(_choice_of(acc, "fb", fetched_at=_NOW - 12 * 60),
                                           name="#1 personal")
    assert "Usage as of 12m ago; it could not be refreshed" in message.splitlines()
    # Within the engine's freshness limit: nothing said.
    _title, message = menubar.reset_dialog(_choice_of(acc, "fb", fetched_at=_NOW - 60), name="#1 personal")
    assert "could not be refreshed" not in message
    # The command line's confirmation says the same.
    choice = _choice_of(acc, "fb", fetched_at=_NOW - 12 * 60)
    assert "Usage as of 12m ago; it could not be refreshed" in menubar._ANSI.sub(
        "", manual_reset.describe(choice, name="#1 personal"))


def _click(inp, action, *, answer=True):
    asked, ran = [], []

    def run(provider, number, grant_id, **kw):
        ran.append((provider, number, grant_id, {k: v for k, v in kw.items() if k != "ask"}))
        acc = next(a for a in inp.provider(provider).accounts if a.number == number)
        kw["ask"](_choice_of(acc, grant_id, codex=provider == "codex"))  # the worker asks
        return True

    started = menubar.use_reset(
        inp, action,
        confirm=lambda title, message: asked.append((title, message)) or answer,
        run=run,
    )
    return started, asked, ran


def test_use_a_reset_click_hands_the_spend_to_the_worker_which_asks():
    acc = _holder([fable("fb", ends=_FAR)])
    action = Action("use-reset", "claude", "1", "fb")
    started, asked, ran = _click(_inp(_claude(acc)), action)
    assert started and ran == [("claude", "1", "fb", {"who": "#1 personal", "what": "a Fable reset"})]
    assert asked[0][0] == "Use this Fable reset?"
    assert asked[0][1].splitlines()[0] == "#1 personal (maya@example.com) · Fable reset"
    # In flight: nothing at all; a grant the menu no longer shows: nothing.
    started, asked, ran = _click(_inp(_claude(acc), resets_busy=frozenset({("claude", "1")})), action)
    assert not started and asked == [] and ran == []
    started, asked, ran = _click(_inp(_claude(acc)), Action("use-reset", "claude", "1", "gone"))
    assert not started and ran == []


# The worker with the real shared function (manual_reset.spend) over a fake switcher.


def _real_runner(tmp_path, answer, *, start=None):
    usage = _reset_usage(fable=100.0, grants=[fable("fb", ends=_NOW + 40 * 86400)])
    sw = FakeSwitcher(tmp_path, usage, answer)
    queued, notes = [], []
    runner = menubar_engines.ResetRunner(manual_reset.spend, notes.append,
                                         start=start or queued.append)
    return sw, runner, queued, notes


@pytest.fixture
def _reset_clock(monkeypatch):
    monkeypatch.setattr(manual_reset.time, "time", lambda: _NOW)


def test_confirm_calls_the_shared_function_once_and_cancel_calls_nothing(tmp_path, _reset_clock):
    ok = ClaimResult("reset", resets_left=0, cleared=("seven_day_overage_included",))
    sw, runner, queued, notes = _real_runner(tmp_path, ok)
    asked = []
    assert runner.run(sw, "claude", "1", "fb", who="#1 personal", what="a Fable reset",
                      ask=lambda choice: asked.append(choice) or True)
    assert runner.busy() == {("claude", "1")}
    # A second click while it is in flight does nothing.
    assert not runner.run(sw, "claude", "1", "fb", who="#1 personal", what="a Fable reset",
                          ask=lambda choice: True)
    assert len(queued) == 1 and sw.calls == []
    queued[0]()  # the worker: re-read, ask, claim
    assert [c[0] for c in sw.calls] == ["resolve", "refresh", "claim"]
    assert sw.calls[2][1:3] == ("1", "fb") and asked[0].grant["id"] == "fb"
    assert runner.busy() == frozenset()
    assert [n.body for n in notes] == ["Used a Fable reset on #1 personal · Fable refilled · 0 left on this grant"]
    # Cancel: re-read and asked, nothing claimed, no notification.
    sw, runner, queued, notes = _real_runner(tmp_path / "b", ok)
    (tmp_path / "b").mkdir()
    runner.run(sw, "claude", "1", "fb", who="#1 personal", what="a Fable reset", ask=lambda c: False)
    queued[0]()
    assert [c[0] for c in sw.calls] == ["resolve", "refresh"] and notes == [None]


class _FakeSpend:
    """manual_reset.spend's seam: records each call, answers what it is given."""

    def __init__(self, answer, *, after_start=False):
        self.answer = answer
        self.after_start = after_start
        self.calls: list = []

    def __call__(self, switcher, number, grant_id, *, codex=False, confirm=None, started=None):
        self.calls.append((switcher, number, grant_id, codex))
        if self.after_start and started is not None:
            started()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _runner(answer, **kw):
    queued, notes = [], []
    spend = _FakeSpend(answer, **kw)
    runner = menubar_engines.ResetRunner(spend, notes.append, start=queued.append)
    return runner, spend, queued, notes


def _spent(result, grant=None, codex=False):
    grant = grant or fable("fb", ends=_FAR)
    choice = manual_reset.Choice("1", "maya@example.com", "#1 personal", grant, None, {}, _NOW, 95.0,
                                 codex=codex)
    return manual_reset.Spent(choice, result)


def test_reset_runner_codex_and_refusal():
    runner, spend, queued, notes = _runner(_spent(ClaimResult("cooldown"), codex=True))
    assert runner.run("sw", "codex", "2", "cr", who="Codex #2 work", what="a reset credit")
    queued[0]()
    assert spend.calls == [("sw", "2", "cr", True)]
    assert notes[0].body == "Did not use a reset credit on Codex #2 work: cooldown"


def test_reset_runner_an_unanswered_claim_may_have_been_used():
    runner, _spend, queued, notes = _runner(_spent(ClaimResult("error", detail="network")))
    runner.run("sw", "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    queued[0]()
    assert notes[0].body == (
        "A Fable reset on #1 personal may have been used: error (network). "
        "Run qswap reset to see where things stand."
    )


def test_reset_runner_says_why_the_command_line_refused():
    from ai_quota_swap.exceptions import ValidationError

    runner, _spend, queued, notes = _runner(ValidationError(
        "that reset cannot be used right now (usable once a limit is reached)"))
    runner.run("sw", "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    queued[0]()
    assert notes[0].body == (
        "Did not use a Fable reset on #1 personal: that reset cannot be used right now "
        "(usable once a limit is reached)"
    )
    runner, _spend, queued, notes = _runner(ValidationError(
        "#1 personal needs re-login; nothing was spent.\nLog in to it in Claude Code (/login), then run: qswap add"))
    runner.run("sw", "claude", "1", "fb", who="Claude #1 personal", what="a Fable reset")
    queued[0]()
    assert notes[0].body.startswith(
        "Did not use a Fable reset on Claude #1 personal: needs re-login; nothing was spent. Log in")
    busy = manual_reset.ResetBusy("A reset on #1 personal is already being used; try again in a moment.")
    runner, _spend, queued, notes = _runner(busy)
    runner.run("sw", "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    queued[0]()
    assert notes[0].body == "A reset on #1 personal is already being used; try again in a moment."


def test_reset_runner_a_failure_after_the_request_started_may_have_been_used():
    runner, _spend, queued, notes = _runner(OSError("disk full"), after_start=True)
    runner.run("sw", "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    queued[0]()
    assert notes[0].body == (
        "A Fable reset on #1 personal may have been used: error (OSError: disk full). "
        "Run qswap reset to see where things stand."
    )
    assert "Did not use" not in notes[0].body
    from ai_quota_swap.exceptions import LockError

    runner, _spend, queued, notes = _runner(LockError("Failed to acquire lock"), after_start=True)
    runner.run("sw", "codex", "1", "cr", who="Codex #1 maya@example.com", what="a reset credit")
    queued[0]()
    assert notes[0].body.startswith("A reset credit on Codex #1 maya@example.com may have been used: ")
    assert notes[0].body.endswith("Run qswap codex reset to see where things stand.")


def test_reset_runner_never_lets_an_exception_out():
    runner, _spend, queued, notes = _runner(RuntimeError("boom"))
    runner.run("sw", "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    queued[0]()  # does not raise
    assert notes[0].body == "Did not use a Fable reset on #1 personal: error (RuntimeError: boom)"
    assert runner.busy() == frozenset()

    def broken_report(_note):
        raise OSError("notification center gone")

    runner = menubar_engines.ResetRunner(_FakeSpend(RuntimeError("x")), broken_report, start=lambda fn: fn())
    assert runner.run("sw", "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    assert runner.busy() == frozenset()


def test_reset_runner_holds_the_lock_while_spending():
    lock, seen = threading.Lock(), []

    def spend(switcher, number, grant_id, *, codex=False, confirm=None, started=None):
        seen.append(lock.locked())
        return None

    runner = menubar_engines.ResetRunner(spend, lambda note: None, start=lambda fn: fn(), lock=lock)
    assert runner.run("sw", "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    assert seen == [True] and not lock.locked()
