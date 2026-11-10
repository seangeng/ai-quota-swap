"""The one manual reset sequence (``manual_reset.spend``), with a fake switcher.

``qswap reset ACCOUNT --grant ID --yes``, ``qswap codex reset …`` and the menu
bar's *Use a reset* all run it. Nothing here reaches a network or a real store:
the switcher is a fake that records what was asked of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_quota_swap import manual_reset
from ai_quota_swap.autoswitch import STATE_FILENAME, _read_state_file, record_reset_claim
from ai_quota_swap.exceptions import ValidationError
from ai_quota_swap.resets import ClaimResult
from ai_quota_swap.settings import ResetSettings
from ai_quota_swap.usage_store import UsageEntry
from tests.test_reset_kinds import fable
from tests.test_resets import NOW, _usage


class FakeSwitcher:
    def __init__(self, tmp: Path, usage: dict, answer: ClaimResult, *, settings=None):
        self.backup_dir = tmp
        self.usage = usage
        self.answer = answer
        self.settings = settings or ResetSettings()
        self.calls: list = []

    def reset_settings(self):
        return self.settings

    def resolve_account(self, account):
        self.calls.append(("resolve", account))
        return "1", "maya@example.com", ""

    def label(self, num, email=None, *, long=True):
        return "#1 personal"

    def refresh_account_usage(self, num):
        self.calls.append(("refresh", num))
        return UsageEntry(last_good=self.usage, fetched_at=NOW)

    def claim_usage_reset(self, num, grant_id, request_id):
        state = _read_state_file(self.backup_dir / STATE_FILENAME)
        # The pending record, with this very request id, is on disk first.
        self.calls.append(("claim", num, grant_id, request_id, state["resetClaims"][num]))
        return self.answer


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(manual_reset.time, "time", lambda: NOW)


def _fake(tmp_path, answer=None, **kw):
    usage = _usage(fable=100.0, grants=[fable("fb", ends=NOW + 40 * 86400)])
    return FakeSwitcher(tmp_path, usage, answer or ClaimResult("reset", resets_left=0), **kw)


def test_the_sequence_re_reads_records_claims_and_logs_a_manual_use(tmp_path):
    sw = _fake(tmp_path)
    spent = manual_reset.spend(sw, "1", "fb")
    assert [c[0] for c in sw.calls] == ["resolve", "refresh", "claim"]
    _claim, num, grant_id, request_id, pending = sw.calls[2]
    assert (num, grant_id) == ("1", "fb")
    assert pending == {"at": NOW, "grantId": "fb", "requestId": request_id, "reason": "manual",
                       "auto": False, "result": "pending", "settled": False}
    state = _read_state_file(tmp_path / STATE_FILENAME)
    assert state["resetClaims"]["1"]["result"] == "reset" and state["resetClaims"]["1"]["settled"]
    assert state["resetLog"] == [
        {"at": NOW, "account": "1", "grantId": "fb", "result": "reset", "auto": False},
    ]
    assert spent.result.ok and spent.choice.grant["id"] == "fb"


def test_an_unanswered_claim_is_retried_with_the_same_request_id(tmp_path):
    sw = _fake(tmp_path, ClaimResult("error", detail="timed out"))
    manual_reset.spend(sw, "1", "fb")
    first = sw.calls[-1][3]
    manual_reset.spend(sw, "1", "fb")
    assert sw.calls[-1][3] == first


def test_confirm_no_spends_nothing_and_confirm_sees_the_fresh_choice(tmp_path):
    sw = _fake(tmp_path)
    seen = []
    assert manual_reset.spend(sw, "1", "fb", confirm=lambda c: seen.append(c) or False) is None
    assert [c[0] for c in sw.calls] == ["resolve", "refresh"]
    assert not (tmp_path / STATE_FILENAME).exists()
    assert seen[0].reset_name == "Fable reset" and manual_reset.question(seen[0]) == "Use this Fable reset?"


def test_refusals_are_the_command_lines(tmp_path):
    sw = _fake(tmp_path)
    with pytest.raises(ValidationError, match="has no reset 'nope'"):
        manual_reset.spend(sw, "1", "nope")
    sw = _fake(tmp_path, settings=ResetSettings(enabled=False))
    with pytest.raises(ValidationError, match="usage resets are off"):
        manual_reset.spend(sw, "1", "fb")
    assert not any(c[0] == "claim" for c in sw.calls)


def test_outcome_texts():
    choice = manual_reset.Choice("1", "m@example.com", "#1 personal", fable("fb"), None, {}, NOW, 95.0)
    ok = manual_reset.Spent(choice, ClaimResult("reset", resets_left=1, cleared=("seven_day_overage_included",)))
    assert manual_reset.outcome_text(ok, "#1 personal") == (
        "Used a Fable reset on #1 personal · Fable refilled · 1 left on this grant"
    )
    no = manual_reset.Spent(choice, ClaimResult("already_redeemed"))
    assert manual_reset.outcome_text(no, "#1 personal") == "Did not use a Fable reset on #1 personal: already redeemed"


def test_record_reset_claim_is_what_the_sequence_writes(tmp_path):
    # The engine's daily brake counts only automatic uses; a manual one is logged as such.
    record_reset_claim(tmp_path, "1", {"at": NOW, "grantId": "g", "result": "reset", "auto": False})
    assert _read_state_file(tmp_path / STATE_FILENAME)["resetLog"][0]["auto"] is False
