"""A setup-token kept as the backup of a subscription login (experimental;
docs/design/backup-token.md).

Every request is faked: the token's header probe goes through a stand-in for
``urlopen`` (the real ``oauth.probe_token_account`` parses it), and the usage
reads through a stand-in for ``oauth.try_fetch_usage_for_account``. The
tokens, emails and organization ids are made up.
"""

from __future__ import annotations

import email.message
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
    ConfigError,
    ConfirmationRequiredError,
    SwitchError,
    ValidationError,
)
from ai_quota_swap.json_output import USAGE_STANDING_BY
from ai_quota_swap.oauth import credential_fingerprint
from ai_quota_swap.settings import set_setting
from ai_quota_swap.usage_store import FetchRecord, UsageEntry
from tests.test_autoswitch import EngineHarness

TOKEN = "sk-ant-oat01-" + "fakebackup" * 4   # made up; never sent anywhere
OTHER_TOKEN = "sk-ant-oat01-" + "fakeother0" * 4
H = 3600.0
DAY = 86400.0
EMAILS = {1: "a@example.com", 2: "b@example.com", 3: "c@example.com", 4: "d@example.com"}


def _org(num: int) -> str:
    return f"00000000-0000-4000-8000-{num:012d}"


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


def _creds(num: int, gen: int = 0, *, expires_at: float | None = None) -> str:
    blob: dict = {"accessToken": f"sk-{num}-{gen}", "refreshToken": f"rt-{num}-{gen}"}
    if expires_at is not None:
        blob["expiresAt"] = int(expires_at * 1000)
    return json.dumps({"claudeAiOauth": blob})


def _token_creds(token: str = TOKEN) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": token, "scopes": ["user:inference"]}})


# == The world =================================================================================


def _login(h: EngineHarness, num: int, *, creds: str | None = None) -> None:
    """A subscription login in slot ``num``, with its organization."""
    s = h.switcher
    mail = EMAILS[num]
    h.seed(num, mail)
    s._write_account_credentials(str(num), mail, creds or _creds(num))
    s._write_account_config(str(num), mail, json.dumps({"oauthAccount": {
        "emailAddress": mail, "accountUuid": f"uuid-{num}", "organizationUuid": _org(num),
    }}))
    data = s._get_sequence_data()
    data["accounts"][str(num)]["organizationUuid"] = _org(num)
    s._write_json(s.sequence_file, data)


def _go_live(h: EngineHarness, num: int, creds: str | None = None) -> None:
    """Claude Code logged in as account ``num``, with its saved lineage."""
    s = h.switcher
    rec = s._get_sequence_data()["accounts"][str(num)]
    live = creds or s.read_account_credentials(str(num), rec["email"])
    (h.temp_home / ".claude").mkdir(exist_ok=True)
    (h.temp_home / ".claude" / ".credentials.json").write_text(live)
    (h.temp_home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": rec["email"], "accountUuid": rec.get("uuid", ""),
        "organizationUuid": rec.get("organizationUuid") or None,
    }}))
    data = s._get_sequence_data()
    data["activeAccountNumber"] = num
    s._write_json(s.sequence_file, data)


def _seed_token(h, num: int, *, parent: int | None = None, token: str = TOKEN,
                added: str = "2026-11-01T00:00:00Z") -> None:
    """A setup-token account in slot ``num``, linked to ``parent`` when given.
    ``h`` is an :class:`EngineHarness` or a bare switcher."""
    s = getattr(h, "switcher", h)
    mail = f"setup-token-{num}@token.local"
    s._write_account_credentials(str(num), mail, _token_creds(token))
    s._write_account_config(str(num), mail, json.dumps({
        "oauthAccount": {"emailAddress": mail, "accountUuid": ""},
    }))
    data = s._get_sequence_data()
    record = {"email": mail, "uuid": "", "organizationUuid": "", "organizationName": "",
              "added": added}
    if parent is not None:
        record.update(backup_token.link_fields(str(parent), data["accounts"][str(parent)], added))
    data["accounts"][str(num)] = record
    data["sequence"] = sorted({*data["sequence"], num})
    s._write_json(s.sequence_file, data)


def _kill(h: EngineHarness, num: int) -> None:
    """Login ``num``'s refresh was refused, bound to the credential it uses
    (the live one when it is the active account, as the active path binds)."""
    s = h.switcher
    rec = s._get_sequence_data()["accounts"][str(num)]
    if s.login_account_number() == str(num):
        creds = (h.temp_home / ".claude" / ".credentials.json").read_text()
    else:
        creds = s.read_account_credentials(str(num), rec["email"])
    s._usage_store.record(
        {str(num): FetchRecord(error="invalid_grant", struck_fp=credential_fingerprint(creds))},
        {str(num): (rec["email"], rec.get("organizationUuid", "") or "")},
    )


def _renew(h: EngineHarness, num: int, gen: int = 1) -> str:
    """What ``qswap login N`` leaves for a login that is not live: a new
    saved login and no dead-login state."""
    s = h.switcher
    rec = s._get_sequence_data()["accounts"][str(num)]
    new = _creds(num, gen)
    s._write_account_credentials(str(num), rec["email"], new)
    s._usage_store.clear_dead_token(
        [str(num)], {str(num): (rec["email"], rec.get("organizationUuid", "") or "")}
    )
    return new


def _record(h: EngineHarness, num: str) -> dict:
    return h.switcher._get_sequence_data()["accounts"].get(num) or {}


class Net:
    """The usage reads, per account: a reading, a refusal (``refused``), or
    a timeout. Records each call with its header-probe flag."""

    def __init__(self, usage: dict | None = None):
        self.usage = dict(usage or {})
        self.refused: set[str] = set()
        self.calls: list[tuple[str, bool, object]] = []

    def count(self, num: str) -> int:
        return sum(1 for c in self.calls if c[0] == num)

    def probes(self, num: str) -> list:
        return [c[2] for c in self.calls if c[0] == num]

    def fetch(self, num, mail, creds, is_active=False, persist_credentials=None, **kwargs):
        self.calls.append((num, is_active, kwargs.get("header_probe")))
        if num in self.refused:
            return oauth.UsageOutcome(
                None, error="invalid_grant", struck_fp=credential_fingerprint(creds),
            )
        value = self.usage.get(num)
        if value is None:
            return oauth.UsageOutcome(None, error="timeout")
        return oauth.UsageOutcome(dict(value))

    def __enter__(self):
        self._patch = patch("ai_quota_swap.oauth.try_fetch_usage_for_account",
                            side_effect=self.fetch)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _world(temp_home: Path, monkeypatch, logins=(1, 2, 3), active: int = 1,
           **settings) -> EngineHarness:
    monkeypatch.setattr("ai_quota_swap.switcher._FETCH_STAGGER_S", 0)
    h = EngineHarness(temp_home, **settings)
    for num in logins:
        _login(h, num)
    _go_live(h, active)
    monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
    return h


@pytest.fixture
def fleet(temp_home: Path, monkeypatch) -> EngineHarness:
    """#1 a (active), #2 b, #3 c, each with its organization; no backup."""
    return _world(temp_home, monkeypatch)


def _tick(h: EngineHarness, net: Net) -> TickOutcome:
    with net:
        return h.engine.tick()


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


# == The pure model ============================================================================


class TestCompare:
    NOW = 2_000_000_000.0

    def _r(self, h5, d7, h5_at=2 * H, d7_at=3 * DAY):
        return _usage(h5, d7, h5_reset=self.NOW + h5_at, d7_reset=self.NOW + d7_at)

    def test_the_same_figures_and_resets_agree(self):
        match = backup_token.compare(
            self._r(30, 20), self._r(31, 20.5, 2 * H + 1, 3 * DAY - 1), now=self.NOW, fresh=True,
        )
        assert match.verdict == "same"
        assert match.agreed == ("5-hour", "weekly")

    def test_equal_resets_with_other_figures_are_another_account(self):
        # Windows start on the hour: two accounts first used in one hour
        # share their resets. The figures tell them apart.
        match = backup_token.compare(self._r(80, 60), self._r(3, 1), now=self.NOW, fresh=True)
        assert match.verdict == "other"
        assert "5-hour window is 3% used, the login's 80%" in match.why

    def test_windows_that_started_90_s_apart_disagree(self):
        match = backup_token.compare(
            self._r(30, 20), self._r(30, 20, 2 * H + 90, 3 * DAY + 90), now=self.NOW, fresh=True,
        )
        assert match.verdict == "other"

    def test_against_an_old_reading_a_disagreement_is_unknown(self):
        match = backup_token.compare(self._r(80, 60), self._r(3, 1), now=self.NOW, fresh=False)
        assert match.verdict == "unknown"
        assert "not from now" in match.why

    def test_the_weekly_window_must_agree(self):
        # The 5-hour window alone agrees: not enough.
        parent = _usage(30, 20, h5_reset=self.NOW + 2 * H)
        token = self._r(30, 20)
        assert backup_token.compare(parent, token, now=self.NOW, fresh=True).verdict == "unknown"
        assert backup_token.nothing_to_compare(parent, now=self.NOW)

    def test_unused_on_both_sides_is_no_evidence(self):
        match = backup_token.compare(self._r(0, 0.5), self._r(0, 0), now=self.NOW, fresh=True)
        assert match.verdict == "unknown"
        assert backup_token.nothing_to_compare(self._r(0, 0.5), now=self.NOW)

    def test_an_ended_window_is_not_compared(self):
        match = backup_token.compare(
            self._r(30, 20, h5_at=-H), self._r(1, 20, h5_at=5 * H), now=self.NOW, fresh=True,
        )
        assert match.verdict == "same"
        assert match.agreed == ("weekly",)

    def test_an_organization_header_that_differs(self):
        same = self._r(30, 20)
        alone = backup_token.compare(same, same, now=self.NOW, fresh=True,
                                     parent_org=_org(1), token_org=_org(9))
        assert alone.verdict == "unknown"
        assert "another organization" in alone.why
        with_usage = backup_token.compare(self._r(80, 60), self._r(3, 1), now=self.NOW,
                                          fresh=False, parent_org=_org(1), token_org=_org(9))
        assert with_usage.verdict == "other"

    def test_a_matching_or_unknown_shaped_header_proves_nothing(self):
        r = self._r(30, 20)
        assert backup_token.compare(None, None, now=self.NOW, fresh=True, parent_org=_org(1),
                                    token_org=_org(1).upper()).verdict == "unknown"
        assert backup_token.compare(r, r, now=self.NOW, fresh=True, parent_org=_org(1),
                                    token_org=_org(1)).verdict == "same"
        assert backup_token.compare(r, r, now=self.NOW, fresh=True, parent_org=_org(1),
                                    token_org="org-elsewhere").verdict == "same"
        assert backup_token.compare(r, r, now=self.NOW, fresh=True, parent_org=_org(1),
                                    token_org=None).verdict == "same"


class TestLinks:
    def _login(self, num, **extra):
        return {"email": EMAILS[num], "organizationUuid": _org(num), **extra}

    def _token(self, parent, num=1, added="2026-11-01T00:00:00Z", **extra):
        return {"email": f"t{num}@token.local", "organizationUuid": "",
                **backup_token.link_fields(str(parent), self._login(parent), added), **extra}

    def _data(self, **accounts):
        return {"sequence": [int(n) for n in accounts], "accounts": accounts}

    def test_a_link_names_a_managed_subscription_login_by_who_it_is(self):
        data = self._data(**{
            "1": self._login(1),
            "2": {"email": "key@token.local", "kind": "api_key", "organizationUuid": ""},
            "5": {"email": "t5@token.local", "organizationUuid": ""},
            "7": self._token(1, 7),
            "8": {**self._token(1, 8), "backupFor": "2"},   # an API key
            "9": {**self._token(1, 9), "backupFor": "4"},   # not managed
            "10": {**self._token(1, 10), "backupFor": "5"},  # an ordinary setup-token
        })
        assert backup_token.links(data) == {"7": "1"}
        assert backup_token.dangling(data) == {"8": "2", "9": "4", "10": "5"}

    def test_a_slot_that_now_holds_another_login_is_no_link(self):
        # After a swap or a remove and add on a build that kept no identity.
        data = self._data(**{"1": self._login(2), "7": self._token(1, 7)})
        assert backup_token.links(data) == {}
        assert backup_token.dangling(data) == {"7": "1"}

    def test_one_backup_per_login_the_newest(self):
        data = self._data(**{
            "1": self._login(1),
            "7": self._token(1, 7, added="2026-10-01T00:00:00Z"),
            "8": self._token(1, 8, added="2026-11-01T00:00:00Z"),
        })
        assert backup_token.links(data) == {"8": "1"}
        assert backup_token.duplicates(data) == {"7": "8"}
        assert backup_token.dangling(data) == {}

    def test_renumber_and_unlink(self):
        data = self._data(**{"1": self._login(1), "7": self._token(1, 7)})
        backup_token.renumber(data, {"1": "5"})
        assert data["accounts"]["7"]["backupFor"] == "5"
        assert backup_token.unlink(data, "5") == ["7"]
        assert not set(backup_token.LINK_KEYS) & set(data["accounts"]["7"])

    def test_the_end_is_a_year_after_it_was_added(self):
        record = {"backupAdded": "2026-11-23T09:00:00Z"}
        added = backup_token.added_at(record)
        ends = backup_token.ends_at(record)
        assert ends == added + 365 * DAY
        assert not backup_token.due(ends, added)
        assert backup_token.due(ends, ends - 29 * DAY)
        assert backup_token.ends_words(ends).startswith("about ")
        assert backup_token.ends_words(ends).endswith(", 2027")


# == The probe =================================================================================


def _hour_at(offset_h: int) -> float:
    return (int(time.time()) // 3600 + offset_h) * 3600.0


def _headers(h5: float, d7: float, *, h5_reset: float, d7_reset: float,
             org: str | None = None, extra: dict | None = None) -> email.message.Message:
    msg = email.message.Message()
    msg["anthropic-ratelimit-unified-5h-utilization"] = str(h5 / 100)
    msg["anthropic-ratelimit-unified-5h-reset"] = str(int(h5_reset))
    msg["anthropic-ratelimit-unified-7d-utilization"] = str(d7 / 100)
    msg["anthropic-ratelimit-unified-7d-reset"] = str(int(d7_reset))
    if org is not None:
        msg[oauth.ORGANIZATION_HEADER] = org
    for key, value in (extra or {}).items():
        msg[key] = value
    return msg


class _Response:
    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Probe:
    """A stand-in for ``urlopen`` that answers the header probe (and only
    it): ``answer`` is header ``Message`` or an exception to raise."""

    def __init__(self, answer):
        self.answer = answer
        self.requests: list = []

    def __call__(self, req, timeout=None):
        assert req.full_url == oauth.USAGE_PROBE_URL, req.full_url
        self.requests.append(req)
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Response(self.answer)

    def __enter__(self):
        self._patch = patch("ai_quota_swap.oauth.urllib.request.urlopen", side_effect=self)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class TestProbeTokenAccount:
    def test_reads_the_headers_and_the_organization(self):
        h5, d7 = _hour_at(2), _hour_at(70)
        with Probe(_headers(31, 20, h5_reset=h5, d7_reset=d7, org=_org(2))) as probe:
            raw, org = oauth.probe_token_account(TOKEN)
        req = probe.requests[0]
        assert req.get_method() == "POST"
        assert req.get_header("Authorization") == f"Bearer {TOKEN}"
        assert json.loads(req.data)["max_tokens"] == 1
        assert org == _org(2)
        usage = oauth.build_usage_result(raw)
        assert usage["five_hour"]["pct"] == 31.0
        assert backup_token.parse_iso(usage["seven_day"]["resets_at"]) == d7

    def test_no_organization_header_is_none(self):
        with Probe(_headers(1, 1, h5_reset=_hour_at(1), d7_reset=_hour_at(9))):
            _raw, org = oauth.probe_token_account(TOKEN)
        assert org is None

    def test_a_429_with_headers_still_answers(self):
        hdrs = _headers(100, 40, h5_reset=_hour_at(1), d7_reset=_hour_at(9),
                        extra={"anthropic-ratelimit-unified-status": "rejected"})
        refused = urllib.error.HTTPError(oauth.USAGE_PROBE_URL, 429, "limit", hdrs=hdrs, fp=None)
        with Probe(refused):
            raw, org = oauth.probe_token_account(TOKEN)
        assert raw["five_hour"]["utilization"] == 100.0
        assert "rejected" in raw and org is None

    def test_a_refused_token_raises(self):
        with Probe(urllib.error.HTTPError(oauth.USAGE_PROBE_URL, 401, "no", hdrs=None, fp=None)), \
                pytest.raises(urllib.error.HTTPError):
            oauth.probe_token_account(TOKEN)


# == Adding a backup ===========================================================================


def _login_reading(h5: float = 30, d7: float = 20, *, h5_reset=None, d7_reset=None) -> dict:
    return _usage(h5, d7, h5_reset=h5_reset or _hour_at(2), d7_reset=d7_reset or _hour_at(70))


def _same_headers(h5: float = 31, d7: float = 20.5, **kw) -> email.message.Message:
    return _headers(h5, d7, h5_reset=kw.pop("h5_reset", _hour_at(2)),
                    d7_reset=kw.pop("d7_reset", _hour_at(70)), **kw)


def _add(h, net: Net, probe: Probe, token=TOKEN, **kwargs):
    with net, probe:
        h.switcher.add_account_from_token(token, **kwargs)


class TestAddBackup:
    def test_an_agreeing_check_saves_a_linked_setup_token(self, fleet, capsys):
        probe = Probe(_same_headers())
        _add(fleet, Net({"2": _login_reading()}), probe, backup_for="2")
        assert len(probe.requests) == 1   # one small request, with the token
        record = _record(fleet, "4")
        assert record["backupFor"] == "2"
        assert record["backupForEmail"] == "b@example.com"
        assert record["backupForOrganization"] == _org(2)
        assert backup_token.parse_iso(record["backupAdded"]) is not None
        assert "kind" not in record     # an ordinary setup-token account
        saved = oauth.extract_oauth_data(
            fleet.switcher.read_account_credentials("4", record["email"]))
        assert oauth.is_setup_token(saved)
        out = capsys.readouterr().out
        assert "one small request" in out
        assert "Added #4 setup-token-4 (from token) as the backup for #2 b@example.com" in out
        assert "token ends about" in out
        assert fleet.switcher.backup_links() == {"4": "2"}

    def test_another_accounts_figures_on_the_same_resets_refuse(self, fleet):
        probe = Probe(_same_headers(3, 1))
        with pytest.raises(ValidationError, match="belongs to another account than #2"):
            _add(fleet, Net({"2": _login_reading(80, 60)}), probe, backup_for="2")
        assert "4" not in fleet.switcher._get_sequence_data()["accounts"]

    def test_windows_that_started_90_s_apart_refuse(self, fleet):
        probe = Probe(_same_headers(30, 20, h5_reset=_hour_at(2) + 90,
                                    d7_reset=_hour_at(70) + 90))
        with pytest.raises(ValidationError, match="another account"):
            _add(fleet, Net({"2": _login_reading()}), probe, backup_for="2")

    def test_no_weekly_reset_on_the_login_refuses_before_any_request(self, fleet):
        reading = {"five_hour": {"pct": 30.0, "resets_at": _iso(_hour_at(2))},
                   "seven_day": {"pct": 20.0}}
        probe = Probe(_same_headers())
        with pytest.raises(ValidationError) as e:
            _add(fleet, Net({"2": reading}), probe, backup_for="2")
        assert probe.requests == []
        assert "no request was sent" in str(e.value)
        assert "--unverified" in str(e.value) and "spends that account's quota" in str(e.value)

    def test_an_unused_login_refuses_before_any_request(self, fleet):
        probe = Probe(_same_headers(0, 0))
        with pytest.raises(ValidationError, match="shows no use yet"):
            _add(fleet, Net({"2": _login_reading(0, 0.5)}), probe, backup_for="2")
        assert probe.requests == []

    def test_a_disagreement_with_an_old_reading_is_not_another_account(self, fleet):
        s = fleet.switcher
        s._usage_store.record({"2": FetchRecord(usage=_login_reading(80, 60))},
                              {"2": ("b@example.com", _org(2))})
        fleet.clock.advance(2 * H)
        probe = Probe(_same_headers(3, 1))
        # The read for the check fails, so only the old reading is there.
        with pytest.raises(ValidationError) as e:
            _add(fleet, Net({}), probe, backup_for="2")
        assert "could not tell" in str(e.value)
        assert "not from now" in str(e.value)
        _add(fleet, Net({}), Probe(_same_headers(3, 1)), backup_for="2", unverified=True)
        assert _record(fleet, "4")["backupFor"] == "2"

    def test_unverified_still_checks_that_the_token_works(self, fleet, capsys):
        probe = Probe(_same_headers())
        _add(fleet, Net({}), probe, backup_for="2", unverified=True)
        assert len(probe.requests) == 1
        out = capsys.readouterr().out
        assert "Checking that the token works" in out
        assert "Not checked" in out
        refused = Probe(urllib.error.HTTPError(oauth.USAGE_PROBE_URL, 403, "no", hdrs=None, fp=None))
        with pytest.raises(ValidationError, match="refused the token"):
            _add(fleet, Net({}), refused, token=OTHER_TOKEN, backup_for="3", unverified=True)

    def test_unverified_never_overrides_a_disagreement(self, fleet):
        with pytest.raises(ValidationError, match="another account"):
            _add(fleet, Net({"2": _login_reading(80, 60)}), Probe(_same_headers(3, 1)),
                 backup_for="2", unverified=True)

    def test_an_organization_header_for_another_organization(self, fleet):
        with pytest.raises(ValidationError) as e:
            _add(fleet, Net({"2": _login_reading()}), Probe(_same_headers(org=_org(9))),
                 backup_for="2")
        assert "names another organization" in str(e.value)
        assert "could not tell" in str(e.value)
        with pytest.raises(ValidationError, match="another account"):
            _add(fleet, Net({"2": _login_reading(80, 60)}),
                 Probe(_same_headers(3, 1, org=_org(9))), backup_for="2")

    def test_the_same_organization_header_passes(self, fleet):
        _add(fleet, Net({"2": _login_reading()}), Probe(_same_headers(org=_org(2))),
             backup_for="2")
        assert _record(fleet, "4")["backupFor"] == "2"

    def test_a_login_at_its_limit_still_compares(self, fleet):
        hdrs = _same_headers(100, 41, extra={"anthropic-ratelimit-unified-status": "rejected"})
        limit = Probe(urllib.error.HTTPError(oauth.USAGE_PROBE_URL, 429, "lim", hdrs=hdrs, fp=None))
        _add(fleet, Net({"2": _login_reading(100, 40)}), limit, backup_for="2")
        assert _record(fleet, "4")["backupFor"] == "2"

    def test_a_refused_token_saves_nothing(self, fleet):
        refused = Probe(urllib.error.HTTPError(oauth.USAGE_PROBE_URL, 401, "no", hdrs=None, fp=None))
        with pytest.raises(ValidationError, match="refused the token"):
            _add(fleet, Net({"2": _login_reading()}), refused, backup_for="2")

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
        probe = Probe(_same_headers())
        with pytest.raises(ConfirmationRequiredError, match="--yes"):
            _add(fleet, Net({"2": _login_reading()}), probe, token=OTHER_TOKEN, backup_for="2")
        assert probe.requests == []   # asked first, before any request
        _add(fleet, Net({"2": _login_reading()}), Probe(_same_headers()), token=OTHER_TOKEN,
             backup_for="b@example.com", assume_yes=True)
        record = _record(fleet, "4")
        assert record["backupFor"] == "2"
        assert record["backupAdded"] != "2026-01-01T00:00:00Z"
        saved = oauth.extract_oauth_data(
            fleet.switcher.read_account_credentials("4", record["email"]))
        assert saved["accessToken"] == OTHER_TOKEN
        assert "Replaced the token of #4 setup-token-4" in capsys.readouterr().out

    def test_a_backup_that_is_the_login_underneath_a_key_is_not_replaced(self, fleet, monkeypatch):
        _seed_token(fleet, 4, parent=2)
        monkeypatch.setattr(fleet.switcher, "current_account_number", lambda: "6")
        monkeypatch.setattr(fleet.switcher, "login_account_number", lambda: "4")
        with pytest.raises(ConfigError, match="is the login Claude Code is using"):
            fleet.switcher.add_account_from_token(OTHER_TOKEN, backup_for="2", assume_yes=True)

    def test_a_token_saved_as_its_own_account_is_linked_where_it_is(self, fleet, capsys):
        _seed_token(fleet, 4)
        _add(fleet, Net({"2": _login_reading()}), Probe(_same_headers()), backup_for="2")
        assert _record(fleet, "4")["backupFor"] == "2"
        assert sorted(fleet.switcher._get_sequence_data()["accounts"]) == ["1", "2", "3", "4"]
        assert "Linked #4 setup-token-4 as the backup for #2" in capsys.readouterr().out

    def test_an_ended_login_is_compared_with_its_last_reading(self, fleet):
        fleet.switcher._usage_store.record({"2": FetchRecord(usage=_login_reading())},
                                           {"2": ("b@example.com", _org(2))})
        _kill(fleet, 2)
        net = Net({})
        _add(fleet, net, Probe(_same_headers()), backup_for="2")
        assert net.count("2") == 0   # a login that needs re-login is not read
        assert _record(fleet, "4")["backupFor"] == "2"


class TestPlainAddOverABackup:
    """Only ``--backup-for`` rewrites a backup's record (finding 3)."""

    def test_add_token_with_its_slot(self, fleet):
        _seed_token(fleet, 7, parent=2)
        with pytest.raises(ConfigError) as e:
            fleet.switcher.add_account_from_token(OTHER_TOKEN, slot=7, assume_yes=True)
        assert "#7 setup-token-7 is the backup for #2 b@example.com" in str(e.value)
        assert "qswap add-token --backup-for 2" in str(e.value)
        assert _record(fleet, "7")["backupFor"] == "2"

    def test_add_token_with_its_email(self, fleet):
        _seed_token(fleet, 7, parent=2)
        with pytest.raises(ConfigError, match="qswap add-token --backup-for 2"):
            fleet.switcher.add_account_from_token(OTHER_TOKEN, email="setup-token-7@token.local")
        saved = oauth.extract_oauth_data(
            fleet.switcher.read_account_credentials("7", "setup-token-7@token.local"))
        assert saved["accessToken"] == TOKEN

    def test_add_with_its_slot(self, fleet):
        _seed_token(fleet, 7, parent=2)
        with pytest.raises(ConfigError, match="qswap add-token --backup-for 2"):
            fleet.switcher.add_account(slot=7, assume_yes=True)
        assert _record(fleet, "7")["email"] == "setup-token-7@token.local"


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

    def test_help_names_the_backup(self):
        from ai_quota_swap.helptext import LEGACY_VERB_HELP

        text = LEGACY_VERB_HELP["add-token"]().format_help()
        assert "--backup-for ACCOUNT" in text
        assert "--unverified" in text
        assert "experimental" in text
        assert "one small request" in text


# == The engine, through the real collector =====================================================


class TestEngineStandingBy:
    def test_a_standing_by_backup_is_never_read(self, fleet):
        _seed_token(fleet, 7, parent=2)
        # #1 near its line, which escalates to a read of every candidate.
        net = Net({"1": _usage(85), "2": _usage(10), "3": _usage(20), "7": _usage(10)})
        for _ in range(12):
            _tick(fleet, net)
            fleet.clock.advance(60)
        assert net.count("7") == 0
        assert net.count("2") >= 1 and net.count("3") >= 1
        assert "7" not in fleet.switcher.switchable_account_numbers()

    def test_all_exhausted_does_not_count_a_standing_by_backup(self, fleet):
        _seed_token(fleet, 7, parent=2)
        net = Net({"1": _usage(100), "2": _usage(100), "3": _usage(100)})
        assert _tick(fleet, net) is TickOutcome.BLOCKED
        assert any(isinstance(e, AllExhaustedEvent) for e in fleet.events)
        assert net.count("7") == 0

    def test_its_view_says_standing_by(self, fleet):
        from ai_quota_swap import autolog

        _seed_token(fleet, 7, parent=2)
        _tick(fleet, Net({"1": _usage(20), "2": _usage(10), "3": _usage(20)}))
        poll = next(e for e in fleet.events if e.kind == "poll")
        view = next(v for v in poll.context.accounts if v.number == "7")
        assert view.state == "standing-by"
        assert autolog.account_facts(view)[0] == "backup for #2 b@example.com, standing by"

    def test_a_disabled_logins_backup_stays_out(self, fleet):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.set_account_disabled("2", True)
        _kill(fleet, 2)
        net = Net({"1": _usage(85), "3": _usage(20), "7": _usage(10)})
        _tick(fleet, net)
        assert net.count("7") == 0
        assert "7" not in fleet.switcher.switchable_account_numbers()
        assert fleet.switcher.backup_view(active="1")["7"].state == "standing-by"
        assert not fleet.switcher.backup_view(active="1")["7"].covers


class TestEngineStandingIn:
    def test_read_by_probe_with_the_setting_off(self, fleet):
        _seed_token(fleet, 7, parent=2)
        _seed_token(fleet, 8, token=OTHER_TOKEN)   # an unlinked setup-token
        _kill(fleet, 2)
        net = Net({"1": _usage(85), "3": _usage(20), "7": _usage(40), "8": _usage(30)})
        _tick(fleet, net)
        assert net.probes("7") and all(p is True for p in net.probes("7"))
        assert all(p is False for p in net.probes("8"))  # as before: the setting decides
        assert net.count("2") == 0                        # the dead login is not read
        assert "7" in fleet.switcher.switchable_account_numbers()

    def test_it_uses_its_logins_line_when_it_has_none(self, fleet):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.set_account_threshold("2", 70)
        fleet.switcher.set_account_priority("2", 3)
        policies = fleet.switcher.account_policies()
        assert policies["7"].threshold == 70 and policies["7"].priority == 3
        fleet.switcher.set_account_threshold("7", 80)
        assert fleet.switcher.account_policies()["7"].threshold == 80

    def _one_login(self, temp_home, monkeypatch, **settings):
        h = _world(temp_home, monkeypatch, logins=(1, 2), **settings)
        _seed_token(h, 7, parent=1)
        return h

    def test_the_failover_goes_to_the_backup_first(self, temp_home, monkeypatch):
        h = self._one_login(temp_home, monkeypatch, unhealthy_ticks=1)
        _kill(h, 1)
        # #2 has more room, so `best` alone would pick it.
        net = Net({"2": _usage(5), "7": _usage(40)})
        assert _tick(h, net) is TickOutcome.SWITCHED
        assert net.count("1") == 0 and net.probes("7")[0] is True
        switch = _switches(h)[0]
        assert switch.trigger == "failover" and switch.to_ref["number"] == 7

    def test_not_first_over_its_own_line(self, temp_home, monkeypatch):
        h = self._one_login(temp_home, monkeypatch, unhealthy_ticks=1)
        _kill(h, 1)
        _tick(h, Net({"2": _usage(5), "7": _usage(97)}))
        assert _switches(h)[0].to_ref["number"] == 2

    def test_without_a_link_the_failover_takes_the_most_room(self, temp_home, monkeypatch):
        h = _world(temp_home, monkeypatch, logins=(1, 2), unhealthy_ticks=1)
        _seed_token(h, 7)   # unlinked: an ordinary candidate, as before
        _kill(h, 1)
        _tick(h, Net({"2": _usage(5), "7": _usage(40)}))
        assert _switches(h)[0].to_ref["number"] == 2

    def test_a_live_login_emptied_in_place_lets_the_backup_stand_in(self, temp_home, monkeypatch):
        """Claude Code met the refusal first and emptied both tokens; the
        saved copy is read in the same check, and its refusal ends the login."""
        h = _world(temp_home, monkeypatch, logins=(1,), unhealthy_ticks=3)
        s = h.switcher
        s._write_account_credentials("1", EMAILS[1], _creds(1, expires_at=1.0))
        _seed_token(h, 7, parent=1)
        wiped = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "",
                                              "scopes": ["user:inference", "user:profile"]}})
        (temp_home / ".claude" / ".credentials.json").write_text(wiped)
        net = Net({"7": _usage(20)})
        net.refused.add("1")
        outcomes = []
        for _ in range(4):
            outcomes.append(_tick(h, net))
            h.clock.advance(60)
        assert net.calls[0][:2] == ("1", False)   # the saved copy, as an idle account
        assert net.count("1") == 1                 # refused once: then ended
        assert net.probes("7") and all(p is True for p in net.probes("7"))
        assert TickOutcome.SWITCHED in outcomes
        assert _switches(h)[0].to_ref["number"] == 7

    def test_a_saved_copy_that_works_changes_nothing(self, temp_home, monkeypatch):
        h = _world(temp_home, monkeypatch, logins=(1,), unhealthy_ticks=50)
        _seed_token(h, 7, parent=1)
        wiped = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})
        (temp_home / ".claude" / ".credentials.json").write_text(wiped)
        net = Net({"1": _usage(20), "7": _usage(20)})
        for _ in range(3):
            _tick(h, net)
            h.clock.advance(60)
        assert net.count("7") == 0
        entries = h.switcher.usage_entries_by_account(fetch=set())
        assert entries["1"].sentinel == "no credentials"


class TestTimeline:
    def test_the_login_ends_the_backup_stands_in_and_the_renewal_brings_it_back(
        self, temp_home, monkeypatch,
    ):
        h = _world(temp_home, monkeypatch, logins=(1,), unhealthy_ticks=3)
        s = h.switcher
        _seed_token(h, 7, parent=1)
        net = Net({"1": _usage(20), "7": _usage(20)})
        # The login works: the backup stands by.
        _tick(h, net)
        assert net.count("7") == 0
        # Its login ends: the refusal bound to the live credential.
        h.clock.advance(60)
        _kill(h, 1)
        for _ in range(4):
            _tick(h, net)
            h.clock.advance(60)
        assert [sw.to_ref["number"] for sw in _switches(h)] == [7]
        assert _switches(h)[0].trigger == "failover"
        assert s.current_account_number() == "7"
        # It stays while the login still needs re-login.
        for _ in range(5):
            _tick(h, net)
            h.clock.advance(60)
        assert len(_switches(h)) == 1
        # The login is renewed: back at the next check, cooldown or not.
        new = _renew(h, 1)
        net.usage["1"] = _usage(25)
        _tick(h, net)
        assert [sw.trigger for sw in _switches(h)] == ["failover", "backup-return"]
        assert "its login works again" in _switches(h)[1].human()
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert credential_fingerprint(live) == credential_fingerprint(new)
        assert credential_fingerprint(s.read_account_credentials("1", EMAILS[1])) == (
            credential_fingerprint(new)
        )
        saved = oauth.extract_oauth_data(
            s.read_account_credentials("7", "setup-token-7@token.local"))
        assert saved["accessToken"] == TOKEN

    def test_no_move_back_on_a_reading_older_than_the_move(self, temp_home, monkeypatch):
        """The login looks usable, but its only reading is from before the
        move onto the backup, and it cannot be read now: no move back."""
        h = _world(temp_home, monkeypatch, logins=(1,))
        _seed_token(h, 7, parent=1)
        s = h.switcher
        s._usage_store.record({"1": FetchRecord(usage=_usage(20))}, {"1": (EMAILS[1], _org(1))})
        h.clock.advance(30)
        _go_live(h, 7)
        h.engine._mutate_state(lambda st: st.update(lastSwitchAt=h.clock.now, lastSwitchTo="7"))
        h.clock.advance(60)
        net = Net({"7": _usage(20)})   # the login does not answer
        _tick(h, net)
        assert net.count("1") >= 1     # it was asked, now
        assert _switches(h) == []
        # Once a read works, back it goes.
        net.usage["1"] = _usage(20)
        h.clock.advance(60)
        _tick(h, net)
        assert [sw.trigger for sw in _switches(h)] == ["backup-return"]

    def test_no_move_back_while_the_login_still_needs_re_login(self, temp_home, monkeypatch):
        """One earlier reading of the login, then it ends: no move back."""
        h = _world(temp_home, monkeypatch, logins=(1,), unhealthy_ticks=3)
        _seed_token(h, 7, parent=1)
        net = Net({"1": _usage(20), "7": _usage(20)})
        _tick(h, net)   # an earlier reading of #1
        h.clock.advance(60)
        _kill(h, 1)
        net.refused.add("1")
        for _ in range(8):
            _tick(h, net)
            h.clock.advance(60)
        assert [sw.trigger for sw in _switches(h)] == ["failover"]
        assert h.switcher.current_account_number() == "7"

    def test_a_login_over_its_line_is_left_to_the_normal_rules(self, temp_home, monkeypatch):
        h = _world(temp_home, monkeypatch, logins=(1,), unhealthy_ticks=1)
        _seed_token(h, 7, parent=1)
        _kill(h, 1)
        net = Net({"7": _usage(20)})
        _tick(h, net)
        h.clock.advance(60)
        _tick(h, net)
        assert h.switcher.current_account_number() == "7"
        _renew(h, 1)
        net.usage["1"] = _usage(95)
        h.clock.advance(60)
        _tick(h, net)
        assert [sw.trigger for sw in _switches(h)] == ["failover"]
        assert any(isinstance(e, NoSwitchEvent) and e.reason == "below-threshold"
                   for e in h.events)

    def test_a_backup_switched_to_by_hand_goes_back_on_a_read(self, temp_home, monkeypatch):
        h = _world(temp_home, monkeypatch, logins=(1,))
        _seed_token(h, 7, parent=1)
        net = Net({"1": _usage(20), "7": _usage(20)})
        with net:
            h.switcher.switch_to("7")
        h.clock.advance(60)
        _tick(h, net)
        assert [sw.trigger for sw in _switches(h)] == ["backup-return"]
        assert net.count("1") >= 1


class TestUnlinkedSetupToken:
    def test_an_unlinked_setup_token_is_a_candidate_as_before(self, fleet):
        _seed_token(fleet, 8)
        assert "8" in fleet.switcher.switchable_account_numbers()
        assert fleet.switcher.backup_links() == {}
        net = Net({"1": _usage(100), "2": _usage(100), "3": _usage(100), "8": _usage(10)})
        assert _tick(fleet, net) is TickOutcome.SWITCHED
        assert _switches(fleet)[0].to_ref["number"] == 8

    def test_the_header_probe_setting_still_decides_for_it(self, fleet):
        _seed_token(fleet, 8)
        net = Net({"1": _usage(85), "2": _usage(1), "3": _usage(1), "8": _usage(1)})
        _tick(fleet, net)
        assert net.probes("8")[-1] is False
        set_setting(fleet.switcher.backup_dir, "usage.headerProbe", "true")
        fleet.switcher._header_probe_cache = None
        fleet.clock.advance(3600)
        _tick(fleet, net)
        assert net.probes("8")[-1] is True


class TestCost:
    def test_the_census_is_worked_out_once_per_check(self, fleet, monkeypatch):
        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        s = fleet.switcher
        calls = {"set_aside": 0, "dead": 0}
        real_set_aside, real_dead = s.set_aside_accounts, s._slot_token_dead

        def set_aside():
            calls["set_aside"] += 1
            return real_set_aside()

        def dead(num, mail):
            calls["dead"] += 1
            return real_dead(num, mail)

        monkeypatch.setattr(s, "set_aside_accounts", set_aside)
        monkeypatch.setattr(s, "_slot_token_dead", dead)
        _tick(fleet, Net({"1": _usage(85), "3": _usage(20), "7": _usage(20)}))
        assert calls["set_aside"] <= 1
        assert calls["dead"] <= 1

    def test_no_backup_reads_the_registry_no_more(self, fleet, monkeypatch):
        """With no backup, every backup question in a check is answered from
        the registry the check has loaded already: none reads it again."""
        from ai_quota_swap.switcher import ClaudeAccountSwitcher

        net = Net({"1": _usage(85), "2": _usage(20), "3": _usage(20)})
        _tick(fleet, net)
        inside = {"depth": 0, "reads": 0, "calls": 0}
        real_links = ClaudeAccountSwitcher.backup_links
        real_read = ClaudeAccountSwitcher._get_sequence_data

        def links(self_, data=None):
            inside["depth"] += 1
            inside["calls"] += 1
            try:
                return real_links(self_, data)
            finally:
                inside["depth"] -= 1

        def read(self_):
            if inside["depth"]:
                inside["reads"] += 1
            return real_read(self_)

        monkeypatch.setattr(ClaudeAccountSwitcher, "backup_links", links)
        monkeypatch.setattr(ClaudeAccountSwitcher, "_get_sequence_data", read)
        fleet.clock.advance(60)
        _tick(fleet, net)
        assert inside["calls"] > 0
        assert inside["reads"] == 0


# == What every view says ======================================================================


def _list(h, net, **kw):
    with net:
        return h.switcher.list_accounts(**kw)


class TestViews:
    def test_list_standing_by(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        net = Net({"1": _usage(5), "2": _usage(5), "3": _usage(5)})
        _list(fleet, net)
        out = capsys.readouterr().out
        assert net.count("7") == 0
        block = out.split("#7 setup-token-7", 1)[1]
        assert "· backup for #2 b@example.com · standing by" in out
        assert "token ends about" in block
        assert display.USAGE_UNAVAILABLE not in block
        assert "headerProbe" not in block

    def test_list_standing_in_and_the_login_it_covers(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        _list(fleet, Net({"1": _usage(5), "3": _usage(5), "7": _usage(42)}))
        out = capsys.readouterr().out
        assert "#7 setup-token-7 · backup for #2 b@example.com · standing in" in out
        assert "42%" in out.split("#7 setup-token-7", 1)[1]
        parent = out.split("#2 b@example.com", 1)[1].split("#3", 1)[0]
        assert "needs re-login" in parent
        assert "its backup #7 setup-token-7 covers it" in parent
        assert "qswap login 2" in parent

    def test_a_disabled_backup_covers_nothing(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.set_account_disabled("7", True)
        _kill(fleet, 2)
        _list(fleet, Net({"1": _usage(5), "3": _usage(5), "7": _usage(42)}))
        parent = capsys.readouterr().out.split("#2 b@example.com", 1)[1].split("#3", 1)[0]
        assert "needs re-login" in parent and "covers it" not in parent

    def test_a_backup_without_its_token_is_not_standing_by(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher._delete_account_credentials("7", "setup-token-7@token.local")
        _list(fleet, Net({"1": _usage(5), "2": _usage(5), "3": _usage(5)}))
        block = capsys.readouterr().out.split("#7 setup-token-7", 1)[1]
        assert "standing by" not in block
        assert "no saved login" in block

    def test_json_rows(self, fleet):
        _seed_token(fleet, 7, parent=2, added="2026-11-01T00:00:00Z")
        payload = _list(fleet, Net({"1": _usage(5), "2": _usage(5), "3": _usage(5)}),
                        json_output=True)
        rows = {row["number"]: row for row in payload["accounts"]}
        assert rows[7]["usageStatus"] == "standing_by"
        assert rows[7]["usage"] is None
        assert rows[7]["backupFor"] == {
            "accountNumber": 2, "state": "standing-by",
            "added": "2026-11-01T00:00:00Z", "endsAbout": "2027-11-01T00:00:00Z",
        }
        assert rows[2]["backupAccountNumber"] == 7
        assert "backupFor" not in rows[1] and "backupAccountNumber" not in rows[1]

    def test_status_of_an_active_backup(self, temp_home, monkeypatch):
        h = _world(temp_home, monkeypatch, logins=(1,))
        _seed_token(h, 7, parent=1)
        _kill(h, 1)
        _go_live(h, 7)
        with Net({"7": _usage(33)}):
            text = "\n".join(h.switcher.status_lines())
            payload = h.switcher._build_status_payload_inner()
        assert "#7 setup-token-7 · active · backup for #1 a@example.com · standing in" in text
        assert payload["active"]["backupFor"]["state"] == "standing-in"
        assert payload["active"]["usageStatus"] == "ok"

    def test_the_dashboard_card_and_row(self, fleet):
        from ai_quota_swap.tui import data, widgets

        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        with Net({"1": _usage(5), "3": _usage(5), "7": _usage(40)}):
            snap = fleet.switcher.accounts_snapshot()
        view = data.ProviderView("claude", snap)
        token = view.account("7")
        assert token.backup_for == "2" and token.backup_state == "standing-in"
        parts = display.header_parts(data.block_for(view, token))
        assert ("backup for #2 b@example.com", "backup") in parts.facts
        assert ("standing in", "backup") in parts.facts
        parent = data.block_for(view, view.account("2"))
        words = "".join(t for t, _ in widgets.summary_parts(parent))
        assert words == "needs re-login · its backup #7 setup-token-7 covers it"

    def test_the_dashboard_row_while_standing_by(self, fleet):
        from ai_quota_swap.tui import autoview, data, widgets

        _seed_token(fleet, 7, parent=2)
        with Net({"1": _usage(5), "2": _usage(5), "3": _usage(5)}):
            snap = fleet.switcher.accounts_snapshot()
        view = data.ProviderView("claude", snap)
        block = data.block_for(view, view.account("7"))
        assert block.state == "standing-by"
        words = "".join(t for t, _ in widgets.summary_parts(block))
        assert words.startswith("token ends about")
        assert "7" not in [c.number for c in autoview.next_best(view, fleet.settings)]

    def test_the_menu_bar_rows(self, fleet):
        from ai_quota_swap import menubar, menubar_data

        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        with Net({"1": _usage(5), "3": _usage(5), "7": _usage(40)}):
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
        with Net({"1": _usage(5), "2": _usage(5), "3": _usage(5)}):
            snap = fleet.switcher.accounts_snapshot()
        now = fleet.clock.now
        acc = next(a for a in menubar_data.account_states(snap, "claude", {}, now=now)
                   if a.number == "7")
        row = menubar.account_row(acc, now=now, stale_after_s=600)
        assert "standing by" in row.text
        assert display.USAGE_UNAVAILABLE not in row.text
        assert not row.attention

    def test_the_menu_bar_notices_once_per_ending(self):
        from ai_quota_swap.menubar import AccountState
        from ai_quota_swap.menubar_notify import backup_notices

        now = 2_000_000_000.0

        def accounts(state, login_ended=True):
            facts = display.BackupFacts("2", "#2 work", state, ends_at=now + 300 * DAY)
            return [
                AccountState(provider="claude", number="2", email="b@example.com", alias="work",
                             set_aside="invalid_grant" if login_ended else None),
                AccountState(provider="claude", number="7", email="setup-token-7@token.local",
                             backup=facts),
            ]

        notes, said = backup_notices(accounts("standing-by", False), now=now, said={})
        assert notes == [] and said == {}
        notes, said = backup_notices(accounts("standing-in"), now=now, said=said)
        assert [n.body for n in notes] == [
            "#2 work needs re-login; its backup #7 setup-token-7 stands in for it. Bring the "
            "login back with Sign in again in the menu, or run: qswap login 2"
        ]
        # A flap while the login still needs re-login says nothing twice.
        notes, said = backup_notices(accounts("standing-by"), now=now + 60, said=said)
        notes, said = backup_notices(accounts("standing-in"), now=now + 120, said=said)
        assert notes == []
        # Renewed, then ended again: a notice again.
        _notes, said = backup_notices(accounts("standing-by", False), now=now + 180, said=said)
        notes, _said = backup_notices(accounts("standing-in"), now=now + 240, said=said)
        assert len(notes) == 1

    def test_the_auto_log_names_the_backup_of_a_login_that_needs_re_login(self, fleet):
        from ai_quota_swap import autolog

        _seed_token(fleet, 7, parent=2)
        _kill(fleet, 2)
        _tick(fleet, Net({"1": _usage(20), "3": _usage(5), "7": _usage(30)}))
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

    def test_switching_to_a_standing_by_backup_says_what_auto_switch_does(
        self, fleet, monkeypatch,
    ):
        _seed_token(fleet, 7, parent=2)
        with Net({}):
            notes = fleet.switcher.check_switch_target("7")
        assert notes == [
            "#7 setup-token-7 is the backup for #2 b@example.com, whose login works; "
            "once auto-switch runs, it moves back to #2 b@example.com"
        ]
        monkeypatch.setattr(fleet.switcher, "_auto_switch_running", lambda: True)
        with Net({}):
            notes = fleet.switcher.check_switch_target("7")
        assert notes[0].endswith("auto-switch moves back to #2 b@example.com at its next check")


class TestRotation:
    def test_a_bare_switch_skips_a_standing_by_backup(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        _go_live(fleet, 3)
        with Net({}):
            fleet.switcher.switch()
        out = capsys.readouterr().out
        assert "Skipped #7 setup-token-7: backup for #2 b@example.com, standing by." in out
        assert fleet.switcher.login_account_number() == "1"

    def test_best_is_not_held_up_by_an_unread_backup(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        net = Net({"1": _usage(10), "2": _usage(50), "3": _usage(50)})
        with net:
            fleet.switcher.switch(strategy="best")
        out = capsys.readouterr().out
        assert net.count("7") == 0
        assert "Already on the account with the most remaining quota" in out
        assert fleet.switcher.login_account_number() == "1"

    def test_a_first_switch_with_no_live_login_skips_it(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        (fleet.temp_home / ".claude.json").unlink()
        (fleet.temp_home / ".claude" / ".credentials.json").unlink()
        data = fleet.switcher._get_sequence_data()
        data["activeAccountNumber"] = 7
        fleet.switcher._write_json(fleet.switcher.sequence_file, data)
        with Net({}):
            fleet.switcher.switch()
        out = capsys.readouterr().out
        assert "Skipped #7 setup-token-7: backup for #2 b@example.com, standing by." in out
        assert fleet.switcher.login_account_number() == "1"

    def test_a_rotation_off_a_key_skips_it(self, fleet, monkeypatch, capsys):
        s = fleet.switcher
        s._write_account_credentials("6", "key6@token.local", "sk-ant-api03-" + "k" * 30)
        data = s._get_sequence_data()
        data["accounts"]["6"] = {"email": "key6@token.local", "uuid": "", "kind": "api_key",
                                 "organizationUuid": "", "organizationName": "", "added": ""}
        data["sequence"] = sorted({*data["sequence"], 6})
        s._write_json(s.sequence_file, data)
        _seed_token(fleet, 7, parent=2)
        (fleet.temp_home / ".claude.json").unlink()
        landed = []
        monkeypatch.setattr(s, "_leave_key_to", lambda target, **kw: landed.append(target) or {})
        s._rotate_from_key("6", strategy_label="next", warnings=[], json_output=False)
        assert landed == ["1"]
        assert "Skipped #7 setup-token-7: backup for #2 b@example.com, standing by." in (
            capsys.readouterr().out
        )


class TestRemoveAndRenumber:
    def test_removing_the_login_leaves_an_ordinary_setup_token(self, fleet, capsys):
        _seed_token(fleet, 7, parent=2)
        fleet.switcher.remove_account("2", assume_yes=True)
        record = _record(fleet, "7")
        assert not set(backup_token.LINK_KEYS) & set(record)
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

    def test_with_the_backup_active(self, temp_home, monkeypatch):
        h = _world(temp_home, monkeypatch, logins=(1,))
        _seed_token(h, 7, parent=1)
        _kill(h, 1)
        _go_live(h, 7)
        s = h.switcher
        assert s._standing_in_backup("1") == "7"
        assert s._back_to_standing_by("7", "1") == (
            "#7 setup-token-7, its backup, is still in use; switch back with: qswap switch 1"
        )
        monkeypatch.setattr(s, "_auto_switch_running", lambda: True)
        assert s._back_to_standing_by("7", "1") == (
            "#7 setup-token-7, its backup, is still in use; auto-switch moves back to "
            "#1 a@example.com at its next check, or switch now: qswap switch 1"
        )

    def test_no_note_without_a_backup_standing_in(self, fleet):
        _seed_token(fleet, 7, parent=2)
        assert fleet.switcher._standing_in_backup("2") is None


class TestExportImport:
    def test_export_carries_the_link_and_import_follows_the_login(
        self, fleet, tmp_path, tmp_path_factory,
    ):
        from ai_quota_swap import transfer

        _seed_token(fleet, 7, parent=3, added="2026-10-01T00:00:00Z")
        dest = tmp_path / "out.json"
        transfer.export_accounts(fleet.switcher, str(dest))
        exported = {a["number"]: a for a in json.loads(dest.read_text())["accounts"]}
        assert exported[7]["backupFor"] == 3
        assert exported[7]["backupForEmail"] == "c@example.com"
        assert exported[7]["backupForOrganizationUuid"] == _org(3)
        assert exported[7]["backupAdded"] == "2026-10-01T00:00:00Z"
        assert "backupFor" not in exported[3]

        # Elsewhere, slots 3 and 7 are taken: both land on new numbers.
        other = EngineHarness(tmp_path_factory.mktemp("elsewhere"))
        other.seed(3, "z@example.com")
        other.seed(7, "y@example.com")
        transfer.import_accounts(other.switcher, str(dest))
        data = other.switcher._get_sequence_data()
        by_email = {r["email"]: n for n, r in data["accounts"].items()}
        token_slot = by_email["setup-token-7@token.local"]
        assert other.switcher.backup_links() == {token_slot: by_email["c@example.com"]}
        assert data["accounts"][token_slot]["backupAdded"] == "2026-10-01T00:00:00Z"

    def test_a_backup_exported_alone_links_to_its_login_here(
        self, fleet, tmp_path, tmp_path_factory,
    ):
        from ai_quota_swap import transfer

        _seed_token(fleet, 7, parent=3)
        dest = tmp_path / "one.json"
        transfer.export_accounts(fleet.switcher, str(dest), account="7")
        other = EngineHarness(tmp_path_factory.mktemp("elsewhere"))
        _login(other, 3)          # the same login, here under its own number
        other.seed(4, "z@example.com")
        transfer.import_accounts(other.switcher, str(dest))
        data = other.switcher._get_sequence_data()
        by_email = {r["email"]: n for n, r in data["accounts"].items()}
        assert other.switcher.backup_links() == {by_email["setup-token-7@token.local"]: "3"}

    def test_import_drops_a_link_to_a_login_that_is_not_here(
        self, fleet, tmp_path, capsys, tmp_path_factory,
    ):
        from ai_quota_swap import transfer

        _seed_token(fleet, 7, parent=3)
        dest = tmp_path / "one.json"
        transfer.export_accounts(fleet.switcher, str(dest), account="7")
        other = EngineHarness(tmp_path_factory.mktemp("elsewhere"))
        other.seed(3, "z@example.com")   # another account in slot 3
        capsys.readouterr()
        transfer.import_accounts(other.switcher, str(dest))
        assert other.switcher.backup_links() == {}
        assert "was the backup of account 3 in the export, which is not managed here" in (
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
        facts = doctor._Facts(platform=Platform.LINUX, home=temp_home, data_dir=data_dir,
                              codex_home=None, claude_bin=None, codex_bin=None)
        return doctor._backup_token_checks(probes, facts)

    def _token(self, parent_email, org, added, parent="1"):
        return {"backupFor": parent, "backupForEmail": parent_email,
                "backupForOrganization": org, "backupAdded": added}

    def test_nothing_without_a_backup(self, temp_home):
        assert self._checks(temp_home, {"sequence": [1], "accounts": {
            "1": {"email": "a@example.com", "organizationUuid": _org(1)}}}) == []

    def test_a_gone_login_a_second_backup_and_one_near_its_end(self, temp_home):
        near = _iso(2_000_000_000.0 - 350 * DAY)
        far = _iso(2_000_000_000.0 - 10 * DAY)
        older = _iso(2_000_000_000.0 - 360 * DAY)
        checks = self._checks(temp_home, {"sequence": [1, 7, 8, 9, 10], "accounts": {
            "1": {"email": "a@example.com", "alias": "work", "organizationUuid": _org(1)},
            "7": {"email": "setup-token-7@token.local", **self._token("a@example.com", _org(1), near)},
            "8": {"email": "setup-token-8@token.local",
                  **self._token("x@example.com", _org(4), far, parent="4")},
            "9": {"email": "setup-token-9@token.local"},
            "10": {"email": "setup-token-10@token.local",
                   **self._token("a@example.com", _org(1), older)},
        }})
        gone = next(c for c in checks if "#8" in c.summary)
        assert gone.status == "warn"
        assert "#4, which is not that subscription login any more" in gone.summary
        assert "qswap remove 8" in gone.fix
        second = next(c for c in checks if c.summary.startswith("#10"))
        assert second.status == "warn"
        assert "a second backup for #1 work; only #7 setup-token-7 is used" in second.summary
        assert second.fix == "qswap remove 10"
        due = next(c for c in checks if c.summary.startswith("the token of #7"))
        assert due.status == "warn"
        assert "the backup for #1 work, ends about" in due.summary
        assert due.fix == "claude setup-token, then qswap add-token --backup-for 1"
        assert not any("#9" in c.summary for c in checks)
