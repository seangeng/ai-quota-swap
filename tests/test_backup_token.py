"""A setup-token kept as the backup of a subscription login (experimental;
docs/design/backup-token.md).

Every request is faked: the token's header probe (``oauth.probe_token_account``)
and the usage reads (``oauth.try_fetch_usage_for_account``). The tokens, emails
and organization ids are made up.
"""

from __future__ import annotations

import json
import time
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_quota_swap import backup_token, display, oauth
from ai_quota_swap.autoswitch import AllExhaustedEvent, NoSwitchEvent, SwitchEvent, TickOutcome
from ai_quota_swap.exceptions import (
    AccountNotFoundError,
    ConfirmationRequiredError,
    SwitchError,
    ValidationError,
)
from ai_quota_swap.json_output import USAGE_RELOGIN_REQUIRED, USAGE_STANDING_BY
from ai_quota_swap.oauth import credential_fingerprint
from ai_quota_swap.settings import set_setting
from ai_quota_swap.usage_store import FetchRecord, UsageEntry
from tests.test_autoswitch import EngineHarness

TOKEN = "sk-ant-oat01-" + "fakebackup" * 4   # made up; never sent anywhere
OTHER_TOKEN = "sk-ant-oat01-" + "fakeother0" * 4
ORG = "00000000-0000-4000-8000-00000000aaaa"
OTHER_ORG = "00000000-0000-4000-8000-00000000bbbb"
H = 3600.0
DAY = 86400.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _usage(h5: float, d7: float = 10.0, *, h5_reset: float | None = None,
           d7_reset: float | None = None) -> dict:
    out: dict = {"five_hour": {"pct": h5}, "seven_day": {"pct": d7}}
    if h5_reset is not None:
        out["five_hour"]["resets_at"] = _iso(h5_reset)
    if d7_reset is not None:
        out["seven_day"]["resets_at"] = _iso(d7_reset)
    return out


def _token_creds(token: str = TOKEN) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": token, "scopes": ["user:inference"]}})


def _seed_token(h, num: int, *, parent: int | None = None,
                token: str = TOKEN, added: str = "2026-11-01T00:00:00Z") -> None:
    """A setup-token account in slot ``num``, linked to ``parent`` when given.
    ``h`` is an :class:`EngineHarness` or a bare switcher."""
    s = getattr(h, "switcher", h)
    email = f"setup-token-{num}@token.local"
    s._write_account_credentials(str(num), email, _token_creds(token))
    s._write_account_config(str(num), email, json.dumps({
        "oauthAccount": {"emailAddress": email, "accountUuid": ""},
    }))
    data = s._get_sequence_data()
    record = {"email": email, "uuid": "", "organizationUuid": "", "organizationName": "",
              "added": added}
    if parent is not None:
        record["backupFor"] = str(parent)
        record["backupAdded"] = added
    data["accounts"][str(num)] = record
    data["sequence"] = sorted({*data["sequence"], num})
    s._write_json(s.sequence_file, data)


def _kill(h: EngineHarness, num: int) -> None:
    """Login ``num``'s saved login is refused (the dead-login state)."""
    s = h.switcher
    email = s.account_email(str(num))
    fp = credential_fingerprint(s.read_account_credentials(str(num), email))
    s._usage_store.record(
        {str(num): FetchRecord(error="invalid_grant", struck_fp=fp)}, {str(num): (email, "")}
    )


def _set_org(h: EngineHarness, num: int, org: str) -> None:
    data = h.switcher._get_sequence_data()
    data["accounts"][str(num)]["organizationUuid"] = org
    h.switcher._write_json(h.switcher.sequence_file, data)


@pytest.fixture
def fleet(temp_home: Path, monkeypatch) -> EngineHarness:
    """#1 a (active), #2 b, #3 c; no backup yet."""
    monkeypatch.setattr("ai_quota_swap.switcher._FETCH_STAGGER_S", 0)
    h = EngineHarness(temp_home)
    for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        h.seed(num, email)
    h.make_live("a@example.com", 1)
    monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
    return h


# == The pure model ============================================================================


class TestCompare:
    NOW = 2_000_000_000.0

    def _parent(self, h5=None, d7=None):
        return _usage(40, 20, h5_reset=h5, d7_reset=d7)

    def test_same_reset_times_agree(self):
        n = self.NOW
        match = backup_token.compare(
            self._parent(n + 2 * H, n + 3 * DAY), self._parent(n + 2 * H + 1, n + 3 * DAY - 30),
            now=n,
        )
        assert match.verdict == "same"
        assert match.agreed == ("5-hour", "weekly")

    def test_one_window_disagreeing_refuses_even_if_the_other_agrees(self):
        n = self.NOW
        match = backup_token.compare(
            self._parent(n + 2 * H, n + 3 * DAY), self._parent(n + 2 * H, n + 5 * DAY), now=n,
        )
        assert match.verdict == "other"
        assert "weekly" in match.why

    def test_a_window_that_has_ended_is_not_compared(self):
        n = self.NOW
        # The login's 5-hour window ended; the probe started a new one.
        match = backup_token.compare(
            self._parent(n - H, n + 3 * DAY), self._parent(n + 5 * H, n + 3 * DAY), now=n,
        )
        assert match.verdict == "same"
        assert match.agreed == ("weekly",)

    def test_nothing_to_compare(self):
        n = self.NOW
        assert backup_token.compare(None, self._parent(n + H, n + DAY), now=n).verdict == "unknown"
        no_window = backup_token.compare(_usage(0, 0), self._parent(n + H, n + DAY), now=n)
        assert no_window.verdict == "unknown"
        assert "no usage reading" in no_window.why
        ended = backup_token.compare(self._parent(n - H, n - 60), self._parent(n + H, n + DAY), now=n)
        assert ended.verdict == "unknown"
        assert "running" in ended.why

    def test_an_organization_header_that_differs_refuses(self):
        n = self.NOW
        reading = self._parent(n + 2 * H, n + 3 * DAY)
        match = backup_token.compare(reading, reading, now=n, parent_org=ORG, token_org=OTHER_ORG)
        assert match.verdict == "other"
        assert "organization" in match.why

    def test_a_matching_organization_header_proves_nothing_alone(self):
        n = self.NOW
        assert backup_token.compare(
            None, None, now=n, parent_org=ORG, token_org=ORG.upper(),
        ).verdict == "unknown"
        reading = self._parent(n + 2 * H, n + 3 * DAY)
        assert backup_token.compare(
            reading, reading, now=n, parent_org=ORG, token_org=ORG,
        ).verdict == "same"

    def test_no_organization_header_is_not_assumed(self):
        n = self.NOW
        reading = self._parent(n + 2 * H, n + 3 * DAY)
        assert backup_token.compare(
            reading, reading, now=n, parent_org=ORG, token_org=None,
        ).verdict == "same"


class TestLinks:
    def _data(self, **extra):
        accounts = {
            "1": {"email": "a@example.com"},
            "2": {"email": "key@token.local", "kind": "api_key"},
            "7": {"email": "setup-token-7@token.local", "backupFor": "1"},
        }
        accounts.update(extra)
        return {"sequence": [int(n) for n in accounts], "accounts": accounts}

    def test_a_link_needs_a_managed_subscription_parent(self):
        data = self._data(
            **{"8": {"email": "t8@token.local", "backupFor": "2"},       # an API key
               "9": {"email": "t9@token.local", "backupFor": "4"},       # not managed
               "10": {"email": "t10@token.local", "backupFor": "7"}},    # a token
        )
        assert backup_token.links(data) == {"7": "1"}
        assert backup_token.dangling(data) == {"8": "2", "9": "4", "10": "7"}
        assert backup_token.backups_by_parent(backup_token.links(data)) == {"1": "7"}

    def test_renumber_and_unlink(self):
        data = self._data()
        backup_token.renumber(data, {"1": "5"})
        assert data["accounts"]["7"]["backupFor"] == "5"
        data["accounts"]["7"]["backupAdded"] = "2026-11-01T00:00:00Z"
        assert backup_token.unlink(data, "5") == ["7"]
        assert "backupFor" not in data["accounts"]["7"]
        assert "backupAdded" not in data["accounts"]["7"]

    def test_the_end_is_a_year_after_it_was_added(self):
        record = {"backupAdded": "2026-11-23T09:00:00Z"}
        added = backup_token.added_at(record)
        assert backup_token.ends_at(record) == added + 365 * DAY
        ends = backup_token.ends_at(record)
        assert not backup_token.due(ends, added)
        assert backup_token.due(ends, ends - 29 * DAY)
        assert backup_token.ends_words(ends).startswith("about ")
        assert backup_token.ends_words(ends).endswith(", 2027")


# == Adding a backup ===========================================================================


def _parent_reading(n: float) -> dict:
    return _usage(30, 20, h5_reset=n + 2 * H, d7_reset=n + 4 * DAY)


def _probe_raw(n: float, *, h5: float | None = None, d7: float | None = None) -> dict:
    """The raw shape ``parse_unified_ratelimit_headers`` gives the check."""
    return {
        "five_hour": {"utilization": 31.0, "resets_at": _iso(h5 if h5 is not None else n + 2 * H)},
        "seven_day": {"utilization": 20.0, "resets_at": _iso(d7 if d7 is not None else n + 4 * DAY)},
    }


class _Fakes:
    """The parent's usage read and the token's probe, recorded."""

    def __init__(self, parent_usage, probe=None, org=None):
        self.parent_usage = parent_usage
        self.probe = probe
        self.org = org
        self.probes: list[str] = []
        self.reads: list[str] = []

    def fetch(self, num, email, creds, is_active=False, **kwargs):
        self.reads.append(num)
        if num == "2":
            return oauth.UsageOutcome(dict(self.parent_usage) if self.parent_usage else None)
        return oauth.UsageOutcome(None, error="timeout")

    def probe_fn(self, token):
        self.probes.append(token)
        if isinstance(self.probe, Exception):
            raise self.probe
        return self.probe, self.org

    def __enter__(self):
        self._patches = [
            patch("ai_quota_swap.oauth.try_fetch_usage_for_account", side_effect=self.fetch),
            patch("ai_quota_swap.oauth.probe_token_account", side_effect=self.probe_fn),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _record(h: EngineHarness, num: str) -> dict:
    return (h.switcher._get_sequence_data()["accounts"]).get(num) or {}


class TestAddBackup:
    def test_agreeing_check_saves_a_linked_setup_token(self, fleet, capsys):
        n = time.time()
        with _Fakes(_parent_reading(n), _probe_raw(n)) as fakes:
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")
        assert fakes.probes == [TOKEN]   # one small request, with the token
        record = _record(fleet, "4")
        assert record["backupFor"] == "2"
        assert record["email"] == "setup-token-4@token.local"
        assert backup_token.parse_iso(record["backupAdded"]) is not None
        assert "kind" not in record      # an ordinary setup-token account
        saved = oauth.extract_oauth_data(fleet.switcher.read_account_credentials("4", record["email"]))
        assert oauth.is_setup_token(saved)
        out = capsys.readouterr().out
        assert "one small request" in out
        assert "Added #4 setup-token-4 (from token) as the backup for #2 b@example.com" in out
        assert "token ends about" in out

    def test_a_token_of_another_account_is_refused(self, fleet):
        n = time.time()
        with _Fakes(_parent_reading(n), _probe_raw(n, h5=n + 3 * H)), pytest.raises(
            ValidationError, match="belongs to another account than #2 b@example.com"
        ):
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")
        assert "4" not in fleet.switcher._get_sequence_data()["accounts"]

    def test_nothing_to_compare_refuses_and_says_what_unverified_risks(self, fleet):
        n = time.time()
        with _Fakes(None, _probe_raw(n)), pytest.raises(ValidationError) as e:
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")
        assert "could not check" in str(e.value)
        assert "--unverified" in str(e.value)
        assert "spends that account's quota" in str(e.value)
        assert "4" not in fleet.switcher._get_sequence_data()["accounts"]

    def test_unverified_saves_it_with_a_warning(self, fleet, capsys):
        n = time.time()
        with _Fakes(None, _probe_raw(n)):
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2", unverified=True)
        assert _record(fleet, "4")["backupFor"] == "2"
        assert "Not checked" in capsys.readouterr().out

    def test_unverified_does_not_override_a_disagreement(self, fleet):
        n = time.time()
        with _Fakes(_parent_reading(n), _probe_raw(n, d7=n + 6 * DAY)), pytest.raises(
            ValidationError, match="another account"
        ):
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2", unverified=True)

    def test_an_organization_header_for_another_organization_refuses(self, fleet):
        _set_org(fleet, 2, ORG)
        n = time.time()
        with _Fakes(_parent_reading(n), _probe_raw(n), org=OTHER_ORG), pytest.raises(
            ValidationError, match="another organization"
        ):
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")

    def test_the_same_organization_header_passes(self, fleet):
        _set_org(fleet, 2, ORG)
        n = time.time()
        with _Fakes(_parent_reading(n), _probe_raw(n), org=ORG):
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")
        assert _record(fleet, "4")["backupFor"] == "2"

    def test_a_refused_token_saves_nothing(self, fleet):
        refused = urllib.error.HTTPError(oauth.USAGE_PROBE_URL, 401, "no", hdrs=None, fp=None)
        with _Fakes(_parent_reading(time.time()), refused), pytest.raises(
            ValidationError, match="refused the token"
        ):
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")

    def test_an_api_key_is_refused(self, fleet):
        with pytest.raises(ValidationError, match="An API key cannot be a backup"):
            fleet.switcher.add_account_from_token("sk-ant-api03-" + "x" * 30, backup_for="2")

    def test_a_parent_that_is_not_a_subscription_login_is_refused(self, fleet):
        _seed_token(fleet, 5)
        with pytest.raises(ValidationError, match="is not a subscription login"):
            fleet.switcher.add_account_from_token(OTHER_TOKEN, backup_for="5")
        with pytest.raises(AccountNotFoundError):
            fleet.switcher.add_account_from_token(OTHER_TOKEN, backup_for="9")

    def test_a_second_backup_replaces_the_first_after_confirmation(self, fleet, capsys):
        _seed_token(fleet, 4, parent=2, added="2026-01-01T00:00:00Z")
        n = time.time()
        with _Fakes(_parent_reading(n), _probe_raw(n)) as fakes, pytest.raises(
            ConfirmationRequiredError, match="--yes"
        ):
            fleet.switcher.add_account_from_token(OTHER_TOKEN, backup_for="2")
        assert fakes.probes == []   # asked first, before any request
        with _Fakes(_parent_reading(n), _probe_raw(n)):
            fleet.switcher.add_account_from_token(OTHER_TOKEN, backup_for="b@example.com",
                                                  assume_yes=True)
        record = _record(fleet, "4")
        assert record["backupFor"] == "2"
        assert record["backupAdded"] != "2026-01-01T00:00:00Z"
        saved = oauth.extract_oauth_data(fleet.switcher.read_account_credentials("4", record["email"]))
        assert saved["accessToken"] == OTHER_TOKEN
        assert "5" not in fleet.switcher._get_sequence_data()["accounts"]
        assert "Replaced the token of #4 setup-token-4" in capsys.readouterr().out

    def test_a_token_saved_as_its_own_account_is_linked_where_it_is(self, fleet, capsys):
        _seed_token(fleet, 4)
        n = time.time()
        with _Fakes(_parent_reading(n), _probe_raw(n)):
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")
        assert _record(fleet, "4")["backupFor"] == "2"
        assert sorted(fleet.switcher._get_sequence_data()["accounts"]) == ["1", "2", "3", "4"]
        assert "Linked #4 setup-token-4 as the backup for #2" in capsys.readouterr().out

    def test_an_ended_parent_is_compared_with_its_last_reading(self, fleet):
        n = time.time()
        email = "b@example.com"
        fleet.switcher._usage_store.record(
            {"2": FetchRecord(usage=_parent_reading(n))}, {"2": (email, "")}
        )
        _kill(fleet, 2)
        with _Fakes(None, _probe_raw(n)) as fakes:
            fleet.switcher.add_account_from_token(TOKEN, backup_for="2")
        assert "2" not in fakes.reads   # a login that needs re-login is not read
        assert _record(fleet, "4")["backupFor"] == "2"


class TestCommandLine:
    def _run(self, argv):
        import sys

        from ai_quota_swap import cli

        with patch.object(sys, "argv", ["qswap", *argv]), pytest.raises(SystemExit) as e:
            cli.main()
        return e.value.code

    def test_flags_only_with_add_token(self, temp_home, capsys):
        assert self._run(["list", "--backup-for", "2"]) == 64
        assert "--backup-for can only be used with 'add-token'" in capsys.readouterr().err
        assert self._run(["add-token", "x", "--unverified"]) == 64
        assert "--unverified can only be used" in capsys.readouterr().err
        assert self._run(["add-token", "x", "--backup-for", "2", "--slot", "4"]) == 64
        assert "--slot cannot be used with --backup-for" in capsys.readouterr().err

    def test_help_names_the_backup(self, capsys):
        from ai_quota_swap.helptext import LEGACY_VERB_HELP

        text = LEGACY_VERB_HELP["add-token"]().format_help()
        assert "--backup-for ACCOUNT" in text
        assert "--unverified" in text
        assert "experimental" in text
        assert "one small request" in text


# == The engine ================================================================================


def _counting(counts, probes, usage_by_num):
    def fake(num, email, creds, is_active=False, persist_credentials=None, **kwargs):
        counts[num] = counts.get(num, 0) + 1
        probes[num] = kwargs.get("header_probe")
        value = usage_by_num.get(num)
        return oauth.UsageOutcome(dict(value) if value else None, error=None if value else "timeout")
    return fake


def _tick(h, counts, probes, usage_by_num):
    with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
               side_effect=_counting(counts, probes, usage_by_num)):
        return h.engine.tick()


class TestEngineStandingBy:
    def test_a_standing_by_backup_is_never_read(self, fleet):
        _seed_token(fleet, 7, parent=2)
        counts: dict = {}
        probes: dict = {}
        # #1 near its line, which escalates to a read of every candidate.
        usage = {"1": _usage(85), "2": _usage(10), "3": _usage(20), "7": _usage(10)}
        for _ in range(12):
            _tick(fleet, counts, probes, usage)
            fleet.clock.advance(60)
        assert "7" not in counts
        assert counts["2"] >= 1 and counts["3"] >= 1
        assert "7" not in fleet.switcher.switchable_account_numbers()

    def test_all_exhausted_does_not_count_a_standing_by_backup(self, fleet):
        _seed_token(fleet, 7, parent=2)
        counts: dict = {}
        probes: dict = {}
        usage = {"1": _usage(100), "2": _usage(100), "3": _usage(100)}
        outcome = _tick(fleet, counts, probes, usage)
        assert outcome is TickOutcome.BLOCKED
        assert any(isinstance(e, AllExhaustedEvent) for e in fleet.events)
        assert "7" not in counts

    def test_its_view_says_standing_by_not_no_login(self, fleet):
        from ai_quota_swap import autolog

        _seed_token(fleet, 7, parent=2)
        _tick(fleet, {}, {}, {"1": _usage(20), "2": _usage(10), "3": _usage(20)})
        poll = next(e for e in fleet.events if e.kind == "poll")
        view = next(v for v in poll.context.accounts if v.number == "7")
        assert view.state == "standing-by"
        text, _level = autolog.account_facts(view)
        assert text == "backup for #2 b@example.com, standing by"


class TestEngineStandingIn:
    def test_read_by_probe_with_the_setting_off(self, fleet):
        _seed_token(fleet, 7, parent=2)
        _seed_token(fleet, 8, token=OTHER_TOKEN)   # an unlinked setup-token
        _kill(fleet, 2)
        counts: dict = {}
        probes: dict = {}
        usage = {"1": _usage(85), "3": _usage(20), "7": _usage(40), "8": _usage(30)}
        _tick(fleet, counts, probes, usage)
        assert counts.get("7", 0) >= 1
        assert probes["7"] is True          # usage.headerProbe is off
        assert probes.get("8") is False      # as before: the setting decides
        assert "2" not in counts             # the dead login is not read
        assert "7" in fleet.switcher.switchable_account_numbers()

    def test_it_uses_its_logins_line_when_it_has_none(self, fleet):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.set_account_threshold("2", 70)
        fleet.switcher.set_account_priority("2", 3)
        policies = fleet.switcher.account_policies()
        assert policies["7"].threshold == 70
        assert policies["7"].priority == 3
        fleet.switcher.set_account_threshold("7", 80)
        assert fleet.switcher.account_policies()["7"].threshold == 80

    def test_the_failover_goes_to_the_backup_first(self, temp_home, monkeypatch):
        h = EngineHarness(temp_home, unhealthy_ticks=1)
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            h.seed(num, email)
        h.make_live("a@example.com", 1)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        _seed_token(h, 7, parent=1)
        _kill(h, 1)
        # #2 has more room, so `best` alone would pick it.
        outcome = h.tick_with_usage({
            "1": USAGE_RELOGIN_REQUIRED, "2": _usage(5), "7": _usage(40),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert switch.to_ref["number"] == 7

    def test_the_failover_reads_the_backup_in_time(self, temp_home, monkeypatch):
        """Through the real collector: the tick that finds the active login
        dead reads its backup (by probe) and fails over to it."""
        monkeypatch.setattr("ai_quota_swap.switcher._FETCH_STAGGER_S", 0)
        h = EngineHarness(temp_home, unhealthy_ticks=1)
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            h.seed(num, email)
        h.make_live("a@example.com", 1)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        _seed_token(h, 7, parent=1)
        _kill(h, 1)
        counts: dict = {}
        probes: dict = {}
        outcome = _tick(h, counts, probes, {"2": _usage(5), "7": _usage(40)})
        assert "1" not in counts and probes["7"] is True
        assert outcome is TickOutcome.SWITCHED
        assert next(e for e in h.events if isinstance(e, SwitchEvent)).to_ref["number"] == 7

    def test_without_a_link_the_failover_takes_the_most_room(self, temp_home, monkeypatch):
        h = EngineHarness(temp_home, unhealthy_ticks=1)
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            h.seed(num, email)
        h.make_live("a@example.com", 1)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        _seed_token(h, 7)   # unlinked: an ordinary candidate, as before
        _kill(h, 1)
        h.tick_with_usage({"1": USAGE_RELOGIN_REQUIRED, "2": _usage(5), "7": _usage(40)})
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_ref["number"] == 2


class TestEngineMovesBack:
    def _on_backup(self, temp_home, monkeypatch, **settings):
        h = EngineHarness(temp_home, **settings)
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            h.seed(num, email)
        _seed_token(h, 7, parent=1)
        (temp_home / ".claude" / ".credentials.json").write_text(_token_creds())
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "setup-token-7@token.local", "accountUuid": ""},
        }))
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        # A switch a moment ago: inside the cooldown.
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now - 10, lastSwitchTo="7"))
        return h

    def test_back_to_the_renewed_login_at_the_next_check_cooldown_or_not(
        self, temp_home, monkeypatch
    ):
        h = self._on_backup(temp_home, monkeypatch)
        assert h.switcher.current_account_number() == "7"
        outcome = h.tick_with_usage({"1": _usage(20), "2": _usage(5), "7": _usage(20)})
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "backup-return"
        assert switch.to_ref["number"] == 1
        assert "its login works again" in switch.human()

    def test_a_login_over_its_line_leaves_it_to_the_normal_rules(self, temp_home, monkeypatch):
        h = self._on_backup(temp_home, monkeypatch)
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(5), "7": _usage(20)})
        assert outcome is TickOutcome.NO_ACTION
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert any(isinstance(e, NoSwitchEvent) and e.reason == "below-threshold"
                   for e in h.events)

    def test_no_move_while_the_login_still_needs_re_login(self, temp_home, monkeypatch):
        h = self._on_backup(temp_home, monkeypatch)
        _kill(h, 1)
        outcome = h.tick_with_usage({"1": USAGE_RELOGIN_REQUIRED, "2": _usage(5), "7": _usage(20)})
        assert outcome is TickOutcome.NO_ACTION
        assert not any(isinstance(e, SwitchEvent) for e in h.events)


class TestUnlinkedSetupToken:
    def test_an_unlinked_setup_token_is_a_candidate_as_before(self, fleet):
        _seed_token(fleet, 8)
        assert "8" in fleet.switcher.switchable_account_numbers()
        assert fleet.switcher.backup_links() == {}
        outcome = fleet.tick_with_usage({
            "1": _usage(100), "2": _usage(100), "3": _usage(100), "8": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED
        assert next(e for e in fleet.events if isinstance(e, SwitchEvent)).to_ref["number"] == 8

    def test_the_header_probe_setting_still_decides_for_it(self, fleet):
        _seed_token(fleet, 8)
        counts: dict = {}
        probes: dict = {}
        _tick(fleet, counts, probes, {"1": _usage(85), "2": _usage(1), "3": _usage(1), "8": _usage(1)})
        assert probes["8"] is False
        set_setting(fleet.switcher.backup_dir, "usage.headerProbe", "true")
        fleet.switcher._header_probe_cache = None
        fleet.clock.advance(3600)
        _tick(fleet, counts, probes, {"1": _usage(85), "2": _usage(1), "3": _usage(1), "8": _usage(1)})
        assert probes["8"] is True


# == What every view says ======================================================================


class TestViews:
    def test_list_standing_by(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        counts: dict = {}
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting(counts, {}, {"1": _usage(5), "2": _usage(5), "3": _usage(5)})):
            fleet.switcher.list_accounts()
        out = capsys.readouterr().out
        assert "7" not in counts
        block = out.split("#7 setup-token-7", 1)[1]
        assert "· backup for #2 b@example.com · standing by" in out
        assert "token ends about" in block
        assert display.USAGE_UNAVAILABLE not in block
        assert "headerProbe" not in block

    def test_list_standing_in_and_the_login_it_covers(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {"1": _usage(5), "3": _usage(5), "7": _usage(42)})):
            fleet.switcher.list_accounts()
        out = capsys.readouterr().out
        assert "#7 setup-token-7 · backup for #2 b@example.com · standing in" in out
        assert "42%" in out.split("#7 setup-token-7", 1)[1]
        parent = out.split("#2 b@example.com", 1)[1].split("#3", 1)[0]
        assert "needs re-login" in parent
        assert "its backup #7 setup-token-7 covers it" in parent
        assert "qswap login 2" in parent

    def test_json_rows(self, fleet):
        _seed_token(fleet, 7, parent=2, added="2026-11-01T00:00:00Z")
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {"1": _usage(5), "2": _usage(5), "3": _usage(5)})):
            payload = fleet.switcher.list_accounts(json_output=True)
        rows = {row["number"]: row for row in payload["accounts"]}
        assert rows[7]["usageStatus"] == "standing_by"
        assert rows[7]["usage"] is None
        assert rows[7]["backupFor"] == {
            "accountNumber": 2, "state": "standing-by",
            "added": "2026-11-01T00:00:00Z", "endsAbout": "2027-11-01T00:00:00Z",
        }
        assert rows[2]["backupAccountNumber"] == 7
        assert "backupFor" not in rows[1] and "backupAccountNumber" not in rows[1]

    def test_status_of_an_active_backup(self, temp_home, monkeypatch, capsys):
        h = TestEngineMovesBack()._on_backup(temp_home, monkeypatch)
        _kill(h, 1)
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {"7": _usage(33)})):
            lines = h.switcher.status_lines()
            payload = h.switcher._build_status_payload_inner()
        text = "\n".join(lines)
        assert "#7 setup-token-7 · active · backup for #1 a@example.com · standing in" in text
        assert payload["active"]["backupFor"]["state"] == "standing-in"
        assert payload["active"]["usageStatus"] == "ok"

    def test_the_dashboard_card_and_row(self, fleet):
        from ai_quota_swap.tui import data, widgets

        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {"1": _usage(5), "3": _usage(5), "7": _usage(40)})):
            snap = fleet.switcher.accounts_snapshot()
        view = data.ProviderView("claude", snap)
        token = view.account("7")
        assert token.backup_for == "2" and token.backup_state == "standing-in"
        block = data.block_for(view, token)
        parts = display.header_parts(block)
        assert ("backup for #2 b@example.com", "backup") in parts.facts
        assert ("standing in", "backup") in parts.facts
        parent = data.block_for(view, view.account("2"))
        words = "".join(t for t, _ in widgets.summary_parts(parent))
        assert words == "needs re-login · its backup #7 setup-token-7 covers it"

    def test_the_dashboard_row_while_standing_by(self, fleet):
        from ai_quota_swap.tui import autoview, data, widgets

        _seed_token(fleet, 7, parent=2)
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {"1": _usage(5), "2": _usage(5), "3": _usage(5)})):
            snap = fleet.switcher.accounts_snapshot()
        view = data.ProviderView("claude", snap)
        block = data.block_for(view, view.account("7"))
        assert block.state == "standing-by"
        words = "".join(t for t, _ in widgets.summary_parts(block))
        assert words.startswith("token ends about")
        # "Next best" leaves it out, as the engine does.
        assert "7" not in [c.number for c in autoview.next_best(view, fleet.settings)]

    def test_the_menu_bar_rows(self, fleet):
        from ai_quota_swap import menubar, menubar_data

        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {"1": _usage(5), "3": _usage(5), "7": _usage(40)})):
            snap = fleet.switcher.accounts_snapshot()
        now = fleet.clock.now
        states = {a.number: a for a in menubar_data.account_states(snap, "claude", {}, now=now)}
        row = menubar.account_row(states["7"], now=now, stale_after_s=600)
        assert row.text.startswith("#7 setup-token-7 · backup for #2 · standing in · 5h 40%")
        parent = menubar.account_row(states["2"], now=now, stale_after_s=600)
        assert "needs re-login · its backup #7 setup-token-7 covers it" in parent.text

    def test_the_menu_bar_row_while_standing_by(self, fleet):
        from ai_quota_swap import menubar, menubar_data

        _seed_token(fleet, 7, parent=2)
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {"1": _usage(5), "2": _usage(5), "3": _usage(5)})):
            snap = fleet.switcher.accounts_snapshot()
        now = fleet.clock.now
        acc = next(a for a in menubar_data.account_states(snap, "claude", {}, now=now)
                   if a.number == "7")
        row = menubar.account_row(acc, now=now, stale_after_s=600)
        assert "standing by" in row.text
        assert display.USAGE_UNAVAILABLE not in row.text
        assert not row.attention

    def test_the_menu_bar_notices_once_when_it_starts_standing_in(self):
        from ai_quota_swap.menubar import AccountState
        from ai_quota_swap.menubar_notify import backup_notices

        now = 2_000_000_000.0

        def accounts(state):
            facts = display.BackupFacts("2", "#2 work", state, ends_at=now + 300 * DAY)
            return [AccountState(provider="claude", number="2", email="b@example.com", alias="work"),
                    AccountState(provider="claude", number="7", email="setup-token-7@token.local",
                                 backup=facts)]

        notes, said = backup_notices(accounts("standing-by"), now=now, said={})
        assert notes == [] and said == {}
        notes, said = backup_notices(accounts("standing-in"), now=now, said=said)
        assert [n.body for n in notes] == [
            "#2 work needs re-login; its backup #7 setup-token-7 stands in for it. Bring the "
            "login back with Sign in again in the menu, or run: qswap login 2"
        ]
        notes, said = backup_notices(accounts("standing-in"), now=now + 60, said=said)
        assert notes == []
        _notes, said = backup_notices(accounts("standing-by"), now=now + 120, said=said)
        notes, _said = backup_notices(accounts("standing-in"), now=now + 180, said=said)
        assert len(notes) == 1   # the next time it stands in, again

    def test_the_auto_log_names_the_backup_of_a_login_that_needs_re_login(self, fleet):
        from ai_quota_swap import autolog

        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        fleet.tick_with_usage({"1": _usage(20), "2": USAGE_RELOGIN_REQUIRED, "3": _usage(5),
                               "7": _usage(30)})
        poll = next(e for e in fleet.events if e.kind == "poll")
        views = {v.number: v for v in poll.context.accounts}
        assert autolog.account_facts(views["2"])[0] == (
            "needs re-login, its backup #7 setup-token-7 covers it"
        )
        assert views["7"].backup_for == "#2 b@example.com"

    def test_a_backup_near_its_end_says_how_to_make_a_new_one(self):
        now = 2_000_000_000.0
        facts = display.BackupFacts("2", "#2 work", "standing-by", ends="about Dec 1, 2033",
                                    ends_at=now + 10 * DAY)
        block = display.block_from_entry(
            "7", UsageEntry(sentinel=USAGE_STANDING_BY), email="setup-token-7@token.local",
            backup=facts, now=now, stale_after_s=600,
        )
        text = "\n".join(display.render_block(block))
        assert "token ends about Dec 1, 2033; make a new one with `claude setup-token`" in text
        assert "qswap add-token --backup-for 2" in text


# == By hand, and around it ====================================================================


class TestSwitchByHand:
    def test_the_refusal_names_the_backup(self, fleet):
        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        with pytest.raises(SwitchError) as e:
            fleet.switcher.check_switch_target("2")
        text = str(e.value)
        assert "Its backup #7 setup-token-7 covers it; switch to that with: qswap switch 7" in text
        assert "qswap login 2" in text

    def test_without_a_backup_the_refusal_is_as_before(self, fleet):
        _kill(fleet, 2)
        with pytest.raises(SwitchError) as e:
            fleet.switcher.check_switch_target("2")
        assert "backup" not in str(e.value)


class TestRotation:
    def test_a_bare_switch_skips_a_standing_by_backup(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        fleet.make_live("c@example.com", 3)
        data = fleet.switcher._get_sequence_data()
        data["activeAccountNumber"] = 3
        fleet.switcher._write_json(fleet.switcher.sequence_file, data)
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting({}, {}, {})):
            fleet.switcher.switch()
        out = capsys.readouterr().out
        assert "Skipped #7 setup-token-7: backup for #2 b@example.com, standing by." in out
        assert fleet.switcher.login_account_number() == "1"

    def test_best_is_not_held_up_by_an_unread_backup(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        counts: dict = {}
        with patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                   side_effect=_counting(counts, {}, {"1": _usage(10), "2": _usage(50),
                                                      "3": _usage(50)})):
            fleet.switcher.switch(strategy="best")
        out = capsys.readouterr().out
        assert "7" not in counts
        assert "Already on the account with the most remaining quota" in out
        assert "some usage is unavailable" not in out
        assert fleet.switcher.login_account_number() == "1"


class TestRemoveAndRenumber:
    def test_removing_the_login_leaves_an_ordinary_setup_token(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.remove_account("2", assume_yes=True)
        record = _record(fleet, "7")
        assert "backupFor" not in record and "backupAdded" not in record
        assert "#7 setup-token-7 was its backup and stays as an ordinary setup-token account" in (
            capsys.readouterr().out
        )
        assert "7" in fleet.switcher.switchable_account_numbers()

    def test_removing_the_backup_needs_nothing_special(self, fleet):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.remove_account("7", assume_yes=True)
        assert fleet.switcher.backup_links() == {}

    def test_the_link_follows_a_swap_and_a_move(self, fleet):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.swap_accounts("2", "3")
        assert fleet.switcher.backup_links() == {"7": "3"}
        fleet.switcher.move_account("3", "5")
        assert fleet.switcher.backup_links() == {"7": "5"}


class TestLogin:
    def test_a_renewal_sends_the_backup_back_to_standing_by(self, fleet):
        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        assert fleet.switcher._standing_in_backup("2") == "7"
        assert fleet.switcher._back_to_standing_by("7", "2") == (
            "#7 setup-token-7, its backup, goes back to standing by."
        )

    def test_no_note_without_a_backup_standing_in(self, fleet):
        _seed_token(fleet, 7, parent=2)
        assert fleet.switcher._standing_in_backup("2") is None


class TestExportImport:
    def _other_home(self, tmp_path_factory, monkeypatch):
        return EngineHarness(tmp_path_factory.mktemp("elsewhere"))

    def test_export_carries_the_link_and_import_remaps_it(self, fleet, tmp_path, capsys,
                                                         tmp_path_factory, monkeypatch):
        from ai_quota_swap import transfer

        _seed_token(fleet, 7, parent=3, added="2026-10-01T00:00:00Z")
        dest = tmp_path / "out.json"
        transfer.export_accounts(fleet.switcher, str(dest))
        exported = {a["number"]: a for a in json.loads(dest.read_text())["accounts"]}
        assert exported[7]["backupFor"] == 3
        assert exported[7]["backupAdded"] == "2026-10-01T00:00:00Z"
        assert "backupFor" not in exported[3]

        # Elsewhere, slot 3 and slot 7 are taken: both land on new numbers.
        other = self._other_home(tmp_path_factory, monkeypatch)
        other.seed(3, "z@example.com")
        other.seed(7, "y@example.com")
        transfer.import_accounts(other.switcher, str(dest))
        data = other.switcher._get_sequence_data()
        by_email = {r["email"]: n for n, r in data["accounts"].items()}
        token_slot = by_email["setup-token-7@token.local"]
        assert other.switcher.backup_links() == {token_slot: by_email["c@example.com"]}
        assert data["accounts"][token_slot]["backupAdded"] == "2026-10-01T00:00:00Z"

    def test_import_drops_a_link_to_a_login_it_does_not_bring(self, fleet, tmp_path, capsys,
                                                             tmp_path_factory, monkeypatch):
        from ai_quota_swap import transfer

        _seed_token(fleet, 7, parent=3)
        dest = tmp_path / "one.json"
        transfer.export_accounts(fleet.switcher, str(dest), account="7")
        other = self._other_home(tmp_path_factory, monkeypatch)
        other.seed(3, "z@example.com")   # another account in slot 3
        capsys.readouterr()
        transfer.import_accounts(other.switcher, str(dest))
        assert other.switcher.backup_links() == {}
        assert "was the backup of account 3 in the export, which was not imported" in (
            capsys.readouterr().err
        )


class TestDoctor:
    def _checks(self, temp_home, registry):
        from ai_quota_swap import doctor

        from ai_quota_swap.models import Platform

        data_dir = temp_home / "data"
        data_dir.mkdir()
        (data_dir / "sequence.json").write_text(json.dumps(registry))
        probes = replace(doctor.default_probes(), now=lambda: 2_000_000_000.0)
        facts = doctor._Facts(
            platform=Platform.LINUX, home=temp_home, data_dir=data_dir, codex_home=None,
            claude_bin=None, codex_bin=None,
        )
        return doctor._backup_token_checks(probes, facts)

    def test_nothing_without_a_backup(self, temp_home):
        assert self._checks(temp_home, {"sequence": [1], "accounts": {
            "1": {"email": "a@example.com"}}}) == []

    def test_a_backup_whose_login_is_gone_and_one_near_its_end(self, temp_home):
        near = _iso(2_000_000_000.0 - 350 * DAY)
        far = _iso(2_000_000_000.0 - 10 * DAY)
        checks = self._checks(temp_home, {"sequence": [1, 7, 8, 9], "accounts": {
            "1": {"email": "a@example.com", "alias": "work"},
            "7": {"email": "setup-token-7@token.local", "backupFor": "1", "backupAdded": near},
            "8": {"email": "setup-token-8@token.local", "backupFor": "4", "backupAdded": far},
            "9": {"email": "setup-token-9@token.local"},
        }})
        gone = next(c for c in checks if "#8" in c.summary)
        assert gone.status == "warn"
        assert "#4, which is not a subscription login qswap manages" in gone.summary
        assert "qswap remove 8" in gone.fix
        due = next(c for c in checks if "#7" in c.summary)
        assert due.status == "warn"
        assert due.summary.startswith("the token of #7 setup-token-7, the backup for #1 work, ends about")
        assert due.fix == "claude setup-token, then qswap add-token --backup-for 1"
        assert not any("#9" in c.summary for c in checks)
