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
from tests.test_resets import h  # noqa: F401  (the engine harness fixture)


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
        return UsageEntry(last_good=self.usage, fetched_at=manual_reset.time.time())

    def claim_usage_reset(self, num, grant_id, request_id, *, before_send=None):
        if before_send is not None:
            before_send()  # the request goes out now
        state = _read_state_file(self.backup_dir / STATE_FILENAME)
        # The pending record, with this very request id, is on disk first.
        self.calls.append(("claim", num, grant_id, request_id, state["resetClaims"][num]))
        _later(1.0)  # a request takes time: the next reading comes after it
        return self.answer


_now = [NOW]


def _later(seconds: float) -> None:
    _now[0] += seconds


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    _now[0] = NOW
    monkeypatch.setattr(manual_reset.time, "time", lambda: _now[0])


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


# -- a spent reset is never reported as not used (review finding 1) -------------------


def _runner_notes(sw):
    from ai_quota_swap import menubar_engines

    notes = []
    runner = menubar_engines.ResetRunner(manual_reset.spend, notes.append, start=lambda fn: fn())
    assert runner.run(sw, "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    return [n.body for n in notes]


class _LockTakingSwitcher(FakeSwitcher):
    """The server answers `reset`; meanwhile another holder (the in-process
    engine's recheck, switch and record) takes the state lock."""

    held = None

    def claim_usage_reset(self, num, grant_id, request_id, **kw):
        from ai_quota_swap.autoswitch import _state_lock_for

        result = super().claim_usage_reset(num, grant_id, request_id, **kw)
        self.held = _state_lock_for(self.backup_dir / STATE_FILENAME)
        assert self.held.acquire(timeout=1)
        return result


def test_final_record_blocked_by_the_state_lock_keeps_the_answer(tmp_path, monkeypatch):
    from ai_quota_swap import autoswitch
    from ai_quota_swap.locking import FileLock

    # The same lock file, a 0.3 s wait instead of 10 s so the test is quick.
    monkeypatch.setattr(
        autoswitch, "_state_lock_for",
        lambda p: FileLock(p.parent / ".autoswitch_state.lock", timeout=0.3),
    )
    sw = _LockTakingSwitcher(tmp_path, _fake(tmp_path).usage, ClaimResult(
        "reset", resets_left=0, cleared=("seven_day_overage_included",)))
    notes = _runner_notes(sw)
    sw.held.release()
    assert [c[0] for c in sw.calls].count("claim") == 1
    assert notes == ["Used a Fable reset on #1 personal · Fable refilled · 0 left on this grant"]
    # The pending record still holds the request id, so the brake and a
    # retry see the claim.
    pending = _read_state_file(tmp_path / STATE_FILENAME)["resetClaims"]["1"]
    assert pending["result"] == "pending" and pending["requestId"] == sw.calls[-1][3]


def test_final_record_write_error_keeps_the_answer(tmp_path, monkeypatch):
    from ai_quota_swap import autoswitch

    sw = _fake(tmp_path, ClaimResult("reset", resets_left=0, cleared=("seven_day_overage_included",)))
    real = autoswitch.atomic_write_json
    writes = []

    def flaky(path, data, *a, **kw):
        writes.append(path)
        if len(writes) == 2:  # the final record
            raise OSError(28, "No space left on device")
        return real(path, data, *a, **kw)

    monkeypatch.setattr(autoswitch, "atomic_write_json", flaky)
    notes = _runner_notes(sw)
    assert [c[0] for c in sw.calls].count("claim") == 1
    assert notes == ["Used a Fable reset on #1 personal · Fable refilled · 0 left on this grant"]
    # The command line keeps the answer too.
    writes.clear()
    spent = manual_reset.spend(_fake(tmp_path / "cli", ClaimResult("reset", resets_left=0)), "1", "fb")
    assert spent.result.ok


def test_a_claim_that_raises_after_the_request_may_have_been_used(tmp_path):
    class Raising(FakeSwitcher):
        def claim_usage_reset(self, num, grant_id, request_id, **kw):
            super().claim_usage_reset(num, grant_id, request_id, **kw)
            raise OSError("connection reset after the POST")

    notes = _runner_notes(Raising(tmp_path, _fake(tmp_path).usage, ClaimResult("reset")))
    assert notes == [
        "A Fable reset on #1 personal may have been used: error (OSError: connection reset "
        "after the POST). Run qswap reset to see where things stand."
    ]


# -- the manual claim reserves as the engine does (review finding 2) --------------------


def _engine_fixtures():
    from tests.test_reset_kinds import weekly
    from tests.test_resets import _engine_usage, _grant

    return {
        "1": _engine_usage(fable=100, d7=95, share=40, grants=[_grant(), weekly("wk")]),
        "2": _engine_usage(fable=0, d7=93),
    }


def test_an_engine_claim_in_flight_refuses_a_manual_one_on_another_grant(h, monkeypatch):  # noqa: F811
    import threading
    from unittest.mock import patch

    from tests.test_resets import _entries

    monkeypatch.setattr(manual_reset.time, "time", lambda: h.clock.now)
    before = _engine_fixtures()
    in_post, release = threading.Event(), threading.Event()
    posts: list = []

    def claim(num, grant_id, request_id, before_send=None):
        if before_send is not None:
            before_send()
        posts.append((threading.current_thread().name, num, grant_id, request_id))
        if threading.current_thread().name == "engine":
            in_post.set()
            assert release.wait(20)
        return ClaimResult("reset", resets_left=0, cleared=("seven_day_overage_included",))

    with (
        patch.object(h.switcher, "usage_entries_by_account",
                     side_effect=lambda *a, **k: _entries(before, h.clock.now)),
        patch.object(h.switcher, "refresh_usage", side_effect=lambda nums: _entries(before, h.clock.now)),
        patch.object(h.switcher, "claim_usage_reset", side_effect=claim),
    ):
        engine = threading.Thread(target=h.engine.tick, name="engine", daemon=True)
        engine.start()
        assert in_post.wait(20), "the engine never claimed"
        _who, _num, engine_grant, engine_rid = posts[0]
        other = "wk" if engine_grant != "wk" else "g-fable"
        # The user's confirmed "Use a reset" on the same account's other row.
        with pytest.raises(manual_reset.ResetBusy, match=r"is already being used; try again in a moment\."):
            manual_reset.spend(h.switcher, "1", other)
        assert len(posts) == 1  # nothing sent
        assert h.state()["resetClaims"]["1"]["requestId"] == engine_rid  # nothing overwritten
        # A reading taken after the engine's record, on the same grant: a
        # retry of that claim, with its stored request id; it keeps counting
        # as the engine's (automatic) claim, marked as retried by hand.
        h.clock.now += 1
        seen: dict = {}
        real_claim = claim

        def watch(num, grant_id, request_id, before_send=None):
            seen["pending"] = h.state()["resetClaims"]["1"]
            return real_claim(num, grant_id, request_id, before_send)

        h.switcher.claim_usage_reset.side_effect = watch
        spent = manual_reset.spend(h.switcher, "1", engine_grant)
        assert posts[-1][2:] == (engine_grant, engine_rid) and spent.result.ok
        assert seen["pending"]["auto"] is True and seen["pending"]["manual"] is True
        release.set()
        engine.join(20)


def test_two_manual_claims_racing_on_one_account_send_one(tmp_path):
    import threading

    in_post, release = threading.Event(), threading.Event()

    class Slow(FakeSwitcher):
        def claim_usage_reset(self, num, grant_id, request_id, **kw):
            result = super().claim_usage_reset(num, grant_id, request_id, **kw)
            in_post.set()
            assert release.wait(20)
            return result

    from tests.test_reset_kinds import weekly

    usage = _usage(fable=100.0, grants=[fable("fb", ends=NOW + 40 * 86400), weekly("wk", ends=NOW + 40 * 86400)])
    first = Slow(tmp_path, usage, ClaimResult("reset", resets_left=0))
    second = FakeSwitcher(tmp_path, usage, ClaimResult("reset", resets_left=0))
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("spent", manual_reset.spend(first, "1", "fb")))
    t.start()
    assert in_post.wait(20)
    with pytest.raises(manual_reset.ResetBusy):
        manual_reset.spend(second, "1", "wk")
    release.set()
    t.join(20)
    claims = [c for sw in (first, second) for c in sw.calls if c[0] == "claim"]
    assert len(claims) == 1 and claims[0][2] == "fb" and box["spent"].result.ok
    # Once the first is answered, the other grant can be spent.
    assert manual_reset.spend(second, "1", "wk").result.ok


def test_a_settled_claim_does_not_hold_a_manual_one_back(tmp_path):
    # A reset spent a moment ago (decided) never blocks a deliberate second one.
    record_reset_claim(tmp_path, "1", {"at": NOW - 5, "grantId": "other", "requestId": "r" * 32,
                                       "reason": "manual", "auto": False, "result": "reset",
                                       "settled": True})
    sw = _fake(tmp_path)
    assert manual_reset.spend(sw, "1", "fb").result.ok


# -- second review: what each answer means, and the claim's guards ------------------------


def test_a_retry_answered_already_used_was_used(tmp_path):
    sw = _fake(tmp_path, ClaimResult("error", detail="network"))
    first = _runner_notes(sw)
    sw.answer = ClaimResult("already_used", reason="already_redeemed")
    second = _runner_notes(sw)
    rids = [c[3] for c in sw.calls if c[0] == "claim"]
    assert rids[0] == rids[1]  # the same request id
    assert "may have been used" in first[0]
    assert second == [
        "A Fable reset on #1 personal was already used: an earlier attempt went through. "
        "Run qswap reset to see where things stand."
    ]


def test_no_credit_left_is_a_decided_nothing_spent(tmp_path):
    notes = _runner_notes(_fake(tmp_path, ClaimResult("unavailable", reason="no_credit")))
    assert notes == ["Did not use a Fable reset on #1 personal: no reset credit left; nothing was spent"]
    assert manual_reset.outcome_kind(ClaimResult("unavailable")) == "unknown"
    assert manual_reset.outcome_kind(ClaimResult("unavailable", reason="no_credit")) == "not-used"
    assert manual_reset.outcome_kind(ClaimResult("already_used")) == "already"


def test_a_failure_before_the_request_is_decided_and_blocks_nothing(tmp_path):
    # The switcher could not even look up the account's token: nothing was
    # sent, so it reads "Did not use", and the account's other grants are
    # not held back.
    class NoToken(FakeSwitcher):
        def claim_usage_reset(self, num, grant_id, request_id, *, before_send=None):
            self.calls.append(("claim", num, grant_id, request_id, None))
            return ClaimResult("auth_error", detail="Timed out waiting for ~/.claude.lock")

    sw = NoToken(tmp_path, _fake(tmp_path).usage, ClaimResult("reset"))
    notes = _runner_notes(sw)
    assert notes == [
        "Did not use a Fable reset on #1 personal: auth error (Timed out waiting for ~/.claude.lock)"
    ]
    assert _read_state_file(tmp_path / STATE_FILENAME)["resetClaims"]["1"]["result"] == "auth_error"
    _later(1.0)
    from tests.test_reset_kinds import weekly

    other = _fake(tmp_path)
    other.usage["resets"]["grants"].append(weekly("wk", ends=NOW + 40 * 86400))
    assert manual_reset.spend(other, "1", "wk").result.ok


def test_the_claude_switcher_sends_nothing_when_the_token_lookup_fails(temp_home):
    from unittest.mock import patch

    from ai_quota_swap.exceptions import LockError
    from ai_quota_swap.switcher import ClaudeAccountSwitcher

    sw = ClaudeAccountSwitcher()
    sent = []
    with patch.object(sw, "account_identity", return_value={"organizationUuid": "org-1-00000000"}), \
         patch.object(sw, "_claim_access_token", side_effect=LockError("Timed out waiting for ~/.claude.lock")), \
         patch("ai_quota_swap.resets.claim", side_effect=lambda *a, **k: sent.append(a)):
        result = sw.claim_usage_reset("1", "fb", "r" * 32, before_send=lambda: sent.append("before"))
    assert result.result == "auth_error" and "claude.lock" in result.detail
    assert sent == []  # neither the hook nor the request


def test_a_reset_recorded_while_the_dialog_was_open_stops_the_claim(tmp_path):
    sw = _fake(tmp_path, ClaimResult("reset", resets_left=0))

    def confirm(choice):
        # While the dialog is open, the engine claims this account and the server says reset.
        _later(5.0)
        record_reset_claim(tmp_path, "1", {"at": manual_reset.time.time(), "grantId": "fb",
                                           "requestId": "e" * 32, "reason": "on-limit",
                                           "auto": True, "result": "reset", "settled": True})
        return True

    with pytest.raises(manual_reset.ResetChanged,
                       match=r"^Usage on #1 personal changed while this was open; nothing was used\.$"):
        manual_reset.spend(sw, "1", "fb", confirm=confirm)
    assert [c[0] for c in sw.calls] == ["resolve", "refresh"]  # nothing sent
    # The menu bar says so, and where to look again.
    from ai_quota_swap import menubar_engines

    notes = []
    runner = menubar_engines.ResetRunner(manual_reset.spend, notes.append, start=lambda fn: fn())
    sw2 = _fake(tmp_path)
    _later(-10.0)  # its reading is older than that record
    runner.run(sw2, "claude", "1", "fb", who="#1 personal", what="a Fable reset")
    assert [n.body for n in notes] == [
        "Usage on #1 personal changed while this was open; nothing was used. Open the menu again."
    ]


def test_a_manual_retry_of_an_engine_claim_keeps_counting_as_automatic(tmp_path):
    from ai_quota_swap.autoswitch import (
        MAX_AUTO_RESETS_PER_DAY,
        reserve_reset_claim,
        reset_claim_settled,
        reset_claims_today,
    )

    path = tmp_path / STATE_FILENAME
    engine = reserve_reset_claim(
        path, "1", "fb", at=NOW - 5, reason="on-limit", auto=True, host=None,
        admit=lambda st, previous: (
            reset_claims_today(st, NOW) < MAX_AUTO_RESETS_PER_DAY
            and reset_claim_settled(previous, NOW - 6, NOW)
        ),
    )
    assert reset_claims_today(_read_state_file(path), NOW) == 1
    sw = _fake(tmp_path, ClaimResult("reset", resets_left=0))
    spent = manual_reset.spend(sw, "1", "fb")
    claim = [c for c in sw.calls if c[0] == "claim"][0]
    assert claim[3] == engine["requestId"]  # the same request id, retried
    assert claim[4]["auto"] is True and claim[4]["manual"] is True
    # Whatever happens to the engine now, its claim still counts.
    assert reset_claims_today(_read_state_file(path), NOW) == 1
    assert spent.result.ok
